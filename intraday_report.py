#!/usr/bin/env python3
import json
import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import after_close_report as base
import render_report_dashboard as dashboard
from core import global_risk
from core import theme_validation
from core import observation_strategy_router
from core import emotion_leader_pool
from core.trading_rules import calc_limit_prices


BASE_DIR = Path(os.environ.get("A_SHARE_BASE_DIR", Path(__file__).resolve().parent))
REPORT_DATE = os.environ.get("A_SHARE_REPORT_DATE") or base.current_datetime().strftime("%Y-%m-%d")
PREMARKET_REPORT = BASE_DIR / f"同花顺我的股票盘前全面分析_{REPORT_DATE}.md"
LATEST_SIGNAL_PATH = BASE_DIR / "web_dashboard" / "data" / "runtime" / "latest_signals.json"
FEISHU_AUDIT_STATE_PATH = BASE_DIR / "web_dashboard" / "data" / "runtime" / "intraday_feishu_audit_state.json"
FEISHU_WEBHOOK = base.FEISHU_WEBHOOK
THREE_METHOD_ENTRY_SCENARIOS = set(observation_strategy_router.STRATEGY_ENTRY_SCENARIO_SET)
V2_EXIT_SCENARIOS = {"V2_REDUCE", "V2_TAKE_PROFIT", "V2_STRUCTURAL_EXIT"}
RADAR_SCAN_LIMIT = 6
RADAR_THEME_KEYWORDS = [
    "PCB", "印制电路板", "元件", "通信", "光通信", "CPO", "算力", "数据中心",
    "半导体", "芯片", "存储", "先进封装", "AI", "服务器", "电子布", "铜箔",
    "创新药", "医药", "医疗", "生物", "电力", "储能", "新能源", "有色", "黄金",
    "化工", "军工", "商业航天", "消费", "零售", "食品", "白酒", "旅游", "券商",
    "银行", "地产", "煤炭", "石油", "油气", "航运", "基建", "机械",
]


class FeishuPushError(RuntimeError):
    pass


def f2(value):
    try:
        return f"{float(value):.2f}"
    except Exception:
        return "-"


def pct(value):
    try:
        return f"{float(value):.2f}%"
    except Exception:
        return "-"


def amount_yi(q):
    return (q.get("amount_wan") or 0) / 10000


def price_text(value, empty="暂无"):
    if value is None:
        return empty
    try:
        return f"{float(value):.2f}"
    except Exception:
        return empty


def status_icon(value, fallback="🟡"):
    text = str(value or "")
    if text.startswith(("🔴", "🟥")) or text in ("P0", "red", "risk"):
        return "🔴"
    if text.startswith(("🟢", "🟩")) or text in ("P2", "green", "strong"):
        return "🟢"
    if text.startswith(("🟡", "🟨")) or text in ("P1", "yellow", "watch"):
        return "🟡"
    return fallback


def strip_md(text):
    return re.sub(r"[*`]", "", text or "").strip()


def parse_level_float(value):
    text = strip_md(value)
    if not text or text in {"-", "暂无", "None"}:
        raise ValueError(f"empty price: {value!r}")
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        raise ValueError(f"invalid price: {value!r}")
    return float(match.group(0))


def parse_position_pct(value):
    """Parse dashboard values such as ``6%`` into a 0-1 portfolio fraction."""
    text = str(value or "").strip()
    parsed = parse_level_float(value)
    if parsed < 0:
        raise ValueError(f"negative position percentage: {value!r}")
    # ``1%`` must be interpreted as one percent as well.  The former
    # magnitude-only branch treated it as 1.0 (100%), which could inflate a
    # valid small-probe plan at the runtime handoff boundary.
    return parsed / 100 if "%" in text or parsed > 1 else parsed


def first_present(row, keys):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


NON_ENTRY_PREMARKET_ACTIONS = frozenset({"防守降风险", "等待结构修复", "观察池待分类"})
NON_ENTRY_STRATEGY_KEYS = frozenset({"OBSERVE_UNCLASSIFIED"})


def premarket_plan_allows_entry(row):
    """Return explicit new-entry authority from a serialized premarket row.

    ``观察池待分类`` rows are deliberately present in the plan so they remain
    monitored, but have a zero position envelope and must never be treated as
    a malformed trade plan.  The strategy key is checked as well because it
    remains stable if the human-readable action text changes.
    """
    action = str(row.get("当日计划") or row.get("premarket_plan_action") or "").strip()
    strategy_key = str(row.get("策略键") or row.get("strategy_key") or "").strip()
    daily_qualified = row.get("日线策略资格", row.get("strategy_daily_qualified"))
    if strategy_key not in observation_strategy_router.STRATEGY_ENTRY_SCENARIOS:
        return False
    if str(daily_qualified).strip() not in {"通过", "1", "true", "True"}:
        return False
    if action in NON_ENTRY_PREMARKET_ACTIONS or strategy_key in NON_ENTRY_STRATEGY_KEYS:
        return False
    return bool(action)


def current_watchlist_map():
    """Return the executable core plus explicitly planned observation groups."""
    try:
        rows = list(base.read_watchlist())
        observation_plan = base.premarket_plan_observation_details()
        snapshot = emotion_leader_pool.load_latest_snapshot(BASE_DIR)
        if str(snapshot.get("source_date") or "") == base.previous_trading_date(REPORT_DATE):
            observation_plan, _metadata = emotion_leader_pool.merge_into_plan(observation_plan, snapshot)
        rows.extend(observation_plan.get("rows") or [])
        return {code: market for code, market in rows}
    except Exception:
        return {}


def plan_identity_metrics(row):
    from core import sector_identity
    raw = str(row.get('主线锁定') or '')
    names = [raw] if '-' in raw and raw != '-' else sector_identity.tokens(raw)
    metrics = {'resonance_boards': names}
    if row.get('双榜状态'):
        metrics['emotion_pool_cross_status'] = row['双榜状态']
    if row.get('主线状态'):
        metrics.update(resonance_policy=sector_identity.POLICY,
                       resonance_board_ids=sector_identity.tokens(row.get('主线代码')),
                       resonance_identity={'status': row['主线状态'], 'reason': row.get('主线依据')})
    return metrics


def read_premarket_levels_from_json():
    path = BASE_DIR / "web_dashboard" / "data" / "reports" / f"premarket_{REPORT_DATE.replace('-', '')}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    watchlist = current_watchlist_map()
    rows = {}
    for row in data.get("rows") or []:
        code = str(row.get("代码") or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        if watchlist and code not in watchlist:
            continue
        try:
            rows[code] = {
                "code": code,
                "name": str(row.get("名称") or code).strip(),
                "state": str(row.get("状态") or "").strip(),
                "priority": str(row.get("优先级") or "").strip(),
                "industry": str(row.get("行业") or "").strip().strip("-"),
                "concepts": str(row.get("完整概念") or row.get("概念") or "").strip().strip("-"),
                "framework_theme_labels": [
                    item for item in re.split(r"[、,/，\s]+", str(row.get("概念") or "")) if item and item != "-"
                ][:8],
                "focus": str(row.get("同花顺专题映射") or "").strip(),
                "community": str(row.get("社区温度") or "").strip(),
                "plan_universe": str(row.get("计划范围") or "核心股票池").strip(),
                "observation_plan_group": str(row.get("计划范围") or "") if "观察池" in str(row.get("计划范围") or "") else "",
                "strategy_style": str(row.get("股票类型") or row.get("交易类型") or "").strip(),
                "strategy_name": str(row.get("交易策略") or row.get("对应方法") or "").strip(),
                "strategy_formal_entry_signal": str(row.get("正式买入信号") or "").strip(),
                "strategy_key": str(row.get("策略键") or "").strip(),
                "strategy_daily_qualified": str(row.get("日线策略资格") or "").strip() in {"通过", "1", "true", "True"},
                "strategy_daily_evidence": str(row.get("日线策略证据") or "").strip(),
                "strategy_daily_metrics": plan_identity_metrics(row),
                "strategy_allowed_patterns": [
                    item for item in re.split(r"[、,/，\s]+", str(row.get("允许时机证据") or row.get("允许V2形态") or "")) if item
                ],
                "strategy_leader_profile": str(row.get("龙头形态") or "").strip(),
                "strategy_leader_cross_verified": str(row.get("龙头双榜验证") or "").strip() in {"是", "1", "true", "True"},
                "strategy_prior_day_touched_limit": str(row.get("昨日触板") or "").strip() in {"是", "1", "true", "True"},
                "strategy_prior_day_closed_limit": str(row.get("昨日封板") or "").strip() in {"是", "1", "true", "True"},
                "strategy_entry_rule": str(row.get("策略入场纪律") or "").strip(),
                "strategy_exit_rule": str(row.get("策略退出纪律") or "").strip(),
                "strategy_reason": str(row.get("策略依据") or "").strip(),
                "defense": parse_level_float(first_present(row, ["防守", "防守/止损"])),
                "repair": parse_level_float(first_present(row, ["修复", "修复触发", "修复/加仓条件"])),
                "pressure": parse_level_float(first_present(row, ["压力", "压力/减仓"])),
                "premarket_advice": str(row.get("盘前建议") or row.get("建议") or "").strip(),
                "premarket_plan_action": str(row.get("当日计划") or "").strip(),
                "planned_target_position_pct": parse_position_pct(row["目标仓位"]) if row.get("目标仓位") else None,
                "planned_max_position_pct": parse_position_pct(row["单票上限"]) if row.get("单票上限") else None,
                "planned_v2_probe_position_pct": parse_position_pct(row.get("策略首笔") or row.get("V2单次")) if (row.get("策略首笔") or row.get("V2单次")) else None,
                "premarket_plan_allows_entry": premarket_plan_allows_entry(row),
                "premarket_plan_loaded": True,
                "premarket_plan_source": "premarket",
            }
        except Exception:
            continue
    return rows


def read_afterclose_levels_from_json():
    """Use the prior close's next-trading-day plan when no premarket artifact exists."""
    path = BASE_DIR / "web_dashboard" / "data" / "reports" / f"afterclose_{REPORT_DATE.replace('-', '')}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    watchlist = current_watchlist_map()
    rows = {}
    for row in data.get("rows") or []:
        code = str(row.get("代码") or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        if watchlist and code not in watchlist:
            continue
        try:
            rows[code] = {
                "code": code,
                "name": str(row.get("名称") or code).strip(),
                "state": str(row.get("状态") or "").strip(),
                "priority": str(row.get("优先级") or "").strip(),
                "focus": str(row.get("同花顺专题映射") or "盘后订盘计划").strip(),
                "community": str(row.get("社区温度") or "社区情绪不可用").strip(),
                "plan_universe": str(row.get("计划范围") or "核心股票池").strip(),
                "observation_plan_group": str(row.get("计划范围") or "") if "观察池" in str(row.get("计划范围") or "") else "",
                "defense": parse_level_float(first_present(row, ["防守/止损（硬失效）", "防守/止损", "防守"])),
                "repair": parse_level_float(first_present(row, ["加仓触发", "条件加仓价", "修复", "修复触发"])),
                "pressure": parse_level_float(first_present(row, ["趋势压力", "压力", "压力/减仓"])),
                "premarket_advice": str(row.get("加仓确认") or row.get("加仓取消") or "盘后订盘关键位回退；盘中仍须实时确认。").strip(),
                "premarket_plan_action": "盘后计划回退",
                "premarket_plan_allows_entry": False,
                "premarket_plan_loaded": True,
                "premarket_plan_source": "afterclose_next_day_plan",
                "level_source": "afterclose_next_day_plan",
            }
        except Exception:
            continue
    return rows


def build_level_from_quote(code, market=None):
    market = market or base.infer_market(code)
    quote = base.parse_tencent_quotes([(code, market)]).get(code)
    if not quote:
        return None
    daily = base.fetch_sohu_daily(code)
    if not daily:
        return None
    quote["code"] = code
    daily = base.daily_with_quote(daily, quote)
    tech = base.trend_text(daily, quote)
    return {
        "code": code,
        "name": quote.get("name") or code,
        "state": tech.get("state") or "盘中临时纳入",
        "priority": tech.get("priority") or "P1",
        "focus": "同花顺我的股票当前标签；盘前缓存缺失，盘中按最新日线结构临时补位",
        "community": "盘中临时纳入，社区温度未缓存",
        "plan_universe": "盘前计划缺失回退",
        "defense": tech.get("defense"),
        "repair": tech.get("repair"),
        "pressure": tech.get("pressure"),
        "premarket_advice": "当前同花顺我的股票新增/盘前未缓存；只按实时量价与日线关键位观察。",
    }


def observation_fallback_level(code, market, observation_details=None):
    """Keep newly added/stale-plan observation names monitored but non-tradable.

    A missing same-day premarket row must never accidentally turn a new
    Tonghuashun self-selection into the core V2 route.  The next premarket
    build will classify it from its daily data; until then it is explicitly
    covered as ``待分类观察`` with zero new-entry authority.
    """
    details = observation_details or base.read_observation_watchlist_details()
    memberships = details.get("memberships") or {}
    groups = [str(item).strip() for item in memberships.get(str(code), []) if str(item).strip()]
    if not groups:
        return None
    group = groups[0]
    meta = observation_strategy_router.STRATEGY_META[observation_strategy_router.OBSERVE]
    return {
        "code": str(code),
        "name": str(code),
        "state": "待分类观察",
        "priority": "P2",
        "focus": f"{group}新增或盘前计划缺失；维持全量盯盘，等待下一次盘前日线分类。",
        "community": "观察池回退覆盖",
        "plan_universe": base.observation_plan_scope_label(group),
        "observation_plan_group": group,
        "strategy_style": meta["style"],
        "strategy_name": meta["name"],
        "strategy_key": observation_strategy_router.OBSERVE,
        "strategy_allowed_patterns": [],
        "strategy_entry_rule": meta["entry_rule"],
        "strategy_exit_rule": meta["exit_rule"],
        "strategy_reason": "盘前策略行缺失时的保守覆盖；不以临时回退授予新增仓资格。",
        "premarket_advice": "待分类仅观察，不产生新增仓信号。",
        "premarket_plan_action": "观察池待分类",
        "planned_target_position_pct": 0.0,
        "planned_max_position_pct": 0.0,
        "planned_v2_probe_position_pct": 0.0,
        "premarket_plan_allows_entry": False,
        "premarket_plan_loaded": True,
        "premarket_plan_source": "observation_watchlist_fallback",
        "level_source": "observation_watchlist_fallback",
        "market": str(market or base.infer_market(code)),
    }


def normalize_levels_to_watchlist(rows):
    watchlist = current_watchlist_map()
    if not watchlist:
        return rows
    observation_details = base.read_observation_watchlist_details()
    normalized = {}
    for code, market in watchlist.items():
        if code in rows:
            normalized[code] = rows[code]
            continue
        observation_level = observation_fallback_level(code, market, observation_details)
        if observation_level:
            normalized[code] = observation_level
            continue
        try:
            row = build_level_from_quote(code, market)
        except Exception:
            row = None
        if row:
            normalized[code] = row
    return normalized


def read_premarket_levels():
    json_rows = read_premarket_levels_from_json()
    if json_rows:
        return normalize_levels_to_watchlist(json_rows)
    afterclose_rows = read_afterclose_levels_from_json()
    if afterclose_rows:
        return normalize_levels_to_watchlist(afterclose_rows)
    if not PREMARKET_REPORT.exists():
        return normalize_levels_to_watchlist({})
    text = PREMARKET_REPORT.read_text(encoding="utf-8")
    rows = {}
    header = None
    for line in text.splitlines():
        if not line.startswith("|"):
            header = None
            continue
        cols = [strip_md(x) for x in line.strip("|").split("|")]
        if not cols or all(set(x) <= {"-", ":", " "} for x in cols):
            continue
        if cols[0] == "代码" and {"防守", "修复", "压力"}.issubset(set(cols)):
            header = {name: idx for idx, name in enumerate(cols)}
            continue
        if not header or not re.fullmatch(r"\d{6}", cols[header["代码"]].strip()):
            continue
        code = cols[header["代码"]].strip()
        try:
            rows[code] = {
                "code": code,
                "name": cols[header.get("名称", header["代码"])].strip(),
                "state": cols[header.get("状态", header["代码"])].strip(),
                "priority": cols[header.get("优先级", header["代码"])].strip(),
                "industry": cols[header["行业"]].strip().strip("-") if "行业" in header else "",
                "concepts": (cols[header["完整概念"]].strip().strip("-") if "完整概念" in header
                             else cols[header["概念"]].strip().strip("-") if "概念" in header else ""),
                "framework_theme_labels": [
                    item for item in re.split(r"[、,/，\s]+", cols[header["概念"]]) if item and item != "-"
                ][:8] if "概念" in header else [],
                "focus": cols[header.get("同花顺专题映射", header["代码"])].strip(),
                "community": cols[header.get("社区温度", header["代码"])].strip(),
                "plan_universe": cols[header["计划范围"]].strip() if "计划范围" in header else "核心股票池",
                "observation_plan_group": (
                    cols[header["计划范围"]].strip()
                    if "计划范围" in header and "观察池" in cols[header["计划范围"]]
                    else ""
                ),
                "strategy_style": cols[header.get("股票类型", header.get("交易类型", -1))].strip() if ("股票类型" in header or "交易类型" in header) else "",
                "strategy_name": cols[header.get("交易策略", header.get("对应方法", -1))].strip() if ("交易策略" in header or "对应方法" in header) else "",
                "strategy_formal_entry_signal": cols[header["正式买入信号"]].strip() if "正式买入信号" in header else "",
                "strategy_key": cols[header["策略键"]].strip() if "策略键" in header else "",
                "strategy_daily_qualified": cols[header["日线策略资格"]].strip() in {"通过", "1", "true", "True"} if "日线策略资格" in header else False,
                "strategy_daily_evidence": cols[header["日线策略证据"]].strip() if "日线策略证据" in header else "",
                "strategy_daily_metrics": plan_identity_metrics({k: cols[i].strip() for k, i in header.items() if i < len(cols)}),
                "strategy_allowed_patterns": [
                    item for item in re.split(r"[、,/，\s]+", cols[header.get("允许时机证据", header.get("允许V2形态"))]) if item
                ] if ("允许时机证据" in header or "允许V2形态" in header) else [],
                "strategy_leader_profile": cols[header["龙头形态"]].strip() if "龙头形态" in header else "",
                "strategy_leader_cross_verified": cols[header["龙头双榜验证"]].strip() in {"是", "1", "true", "True"} if "龙头双榜验证" in header else False,
                "strategy_prior_day_touched_limit": cols[header["昨日触板"]].strip() in {"是", "1", "true", "True"} if "昨日触板" in header else False,
                "strategy_prior_day_closed_limit": cols[header["昨日封板"]].strip() in {"是", "1", "true", "True"} if "昨日封板" in header else False,
                "strategy_entry_rule": cols[header["策略入场纪律"]].strip() if "策略入场纪律" in header else "",
                "strategy_exit_rule": cols[header["策略退出纪律"]].strip() if "策略退出纪律" in header else "",
                "strategy_reason": cols[header["策略依据"]].strip() if "策略依据" in header else "",
                "defense": parse_level_float(cols[header["防守"]]),
                "repair": parse_level_float(cols[header["修复"]]),
                "pressure": parse_level_float(cols[header["压力"]]),
                "premarket_advice": cols[header.get("盘前建议", len(cols) - 1)].strip(),
                "premarket_plan_action": cols[header["当日计划"]].strip() if "当日计划" in header else "",
                "planned_target_position_pct": parse_position_pct(cols[header["目标仓位"]]) if "目标仓位" in header else None,
                "planned_max_position_pct": parse_position_pct(cols[header["单票上限"]]) if "单票上限" in header else None,
                "planned_v2_probe_position_pct": parse_position_pct(cols[header.get("策略首笔", header.get("V2单次"))]) if ("策略首笔" in header or "V2单次" in header) else None,
                "premarket_plan_allows_entry": premarket_plan_allows_entry({
                    "当日计划": cols[header["当日计划"]].strip() if "当日计划" in header else "",
                    "策略键": cols[header["策略键"]].strip() if "策略键" in header else "",
                }),
                "premarket_plan_loaded": True,
                "premarket_plan_source": "premarket_markdown",
            }
        except Exception:
            continue
    return normalize_levels_to_watchlist(rows)


def fetch_live(levels=None):
    try:
        core_universe = base.read_watchlist()
        markets = {code: market for code, market in core_universe}
        for code in (levels or {}):
            markets.setdefault(str(code), base.infer_market(code))
        universe = list(markets.items())
    except Exception:
        if not levels:
            raise
        universe = [(code, "17" if code.startswith("6") else "33") for code in levels]
    quotes = base.parse_tencent_quotes(universe)
    return universe, quotes


def fetch_fast_boards():
    try:
        boards, _ = base.fetch_boards()
        return boards
    except Exception:
        return []


def quote_metrics(q):
    close = q["close"]
    open_price = q.get("open") or q.get("prev_close") or close
    prev = q.get("prev_close") or close
    high = q.get("high") or close
    low = q.get("low") or close
    gap = (open_price - prev) / prev * 100 if prev else 0
    from_open = (close - open_price) / open_price * 100 if open_price else 0
    drawdown = (close - high) / high * 100 if high else 0
    pos = (close - low) / (high - low) * 100 if high > low else 50
    return gap, from_open, drawdown, pos


def to_float(value):
    try:
        return float(value)
    except Exception:
        return None


def minline_stats(rows):
    points = []
    for row in rows or []:
        price = to_float(row.get("p"))
        if price is None:
            continue
        volume = to_float(row.get("v")) or 0
        avg_price = to_float(row.get("avg_p"))
        points.append({
            "time": row.get("m") or "",
            "price": price,
            "volume": volume,
            "avg_price": avg_price,
        })
    if not points:
        return {"available": False}

    prices = [x["price"] for x in points]
    volumes = [x["volume"] for x in points]
    total_volume = sum(volumes)
    vwap = next((x["avg_price"] for x in reversed(points) if x.get("avg_price")), None)
    if vwap is None and total_volume:
        vwap = sum(x["price"] * x["volume"] for x in points) / total_volume
    open_range = points[:30] or points
    recent30 = points[-30:] if len(points) >= 30 else points
    recent60 = points[-60:] if len(points) >= 60 else points
    avg_recent_volume = sum(x["volume"] for x in recent30) / len(recent30) if recent30 else 0
    prev_recent = points[-60:-30] if len(points) >= 60 else []
    avg_prev_volume = sum(x["volume"] for x in prev_recent) / len(prev_recent) if prev_recent else 0
    volume_trend = "放量" if avg_prev_volume and avg_recent_volume >= avg_prev_volume * 1.15 else "缩量/平量"
    return {
        "available": True,
        "minutes": len(points),
        "vwap": vwap,
        "last_price": prices[-1],
        "opening_high": max(x["price"] for x in open_range),
        "opening_low": min(x["price"] for x in open_range),
        "recent30_high": max(x["price"] for x in recent30),
        "recent30_low": min(x["price"] for x in recent30),
        "recent60_high": max(x["price"] for x in recent60),
        "recent60_low": min(x["price"] for x in recent60),
        "volume_trend": volume_trend,
    }


def eastmoney_secid(code):
    code = str(code)
    market = "1" if code.startswith("6") else "0"
    return f"{market}.{code}"


def fetch_minline_eastmoney(code, timeout=5):
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/trends2/get?"
        f"secid={eastmoney_secid(code)}&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&iscca=0&ndays=1"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        trends = (data.get("data") or {}).get("trends") or []
    except Exception:
        return []
    rows = []
    for item in trends:
        parts = str(item).split(",")
        if len(parts) < 8:
            continue
        ts = parts[0]
        hhmm = ts[-5:] if len(ts) >= 5 else ts
        rows.append({
            "m": f"{hhmm}:00" if len(hhmm) == 5 else hhmm,
            "p": parts[2],
            "v": parts[5],
            "avg_p": parts[7],
        })
    return rows


def fetch_minline_with_fallback(code):
    rows = fetch_minline_eastmoney(code)
    return rows or base.fetch_minline(code)


def fetch_intraday_stats(codes):
    codes = list(codes)
    out = {}

    def load_one(code):
        return code, minline_stats(fetch_minline_with_fallback(code))

    with ThreadPoolExecutor(max_workers=min(10, max(1, len(codes)))) as pool:
        futures = [pool.submit(load_one, code) for code in codes]
        for future in as_completed(futures):
            try:
                code, stats = future.result()
                out[code] = stats
            except Exception:
                pass
    return out


def nearest_above(close, candidates, min_gap=0.0015):
    valid = []
    floor = close * (1 + min_gap)
    for value in candidates:
        if value is None:
            continue
        try:
            value = float(value)
        except Exception:
            continue
        if value > floor:
            valid.append(value)
    return min(valid) if valid else None


def nearest_below(close, candidates):
    valid = []
    for value in candidates:
        if value is None:
            continue
        try:
            value = float(value)
        except Exception:
            continue
        if value < close:
            valid.append(value)
    return max(valid) if valid else None


def dynamic_levels(row, q, stats):
    close = q["close"]
    open_price = q.get("open") or close
    vwap = stats.get("vwap") if stats and stats.get("available") else None
    live_pressure = nearest_above(close, [
        q.get("high"),
        stats.get("recent30_high") if stats else None,
        stats.get("recent60_high") if stats else None,
        stats.get("opening_high") if stats else None,
    ], min_gap=0)
    trend_pressure = row["pressure"]
    if trend_pressure <= close:
        trend_pressure = live_pressure or max(q.get("high") or close, trend_pressure)
    limit_up, _ = calc_limit_prices(q.get("code") or row.get("code") or "", q.get("name") or "", q.get("prev_close"))
    execution_pressure = min(trend_pressure, limit_up) if limit_up else trend_pressure
    first_bounce = nearest_above(close, [
        stats.get("recent30_high") if stats else None,
        stats.get("recent60_high") if stats else None,
        vwap,
        open_price,
        stats.get("opening_high") if stats else None,
        row["repair"],
        execution_pressure,
    ])
    if first_bounce is None:
        first_bounce = row["repair"] if row["repair"] > close else execution_pressure

    intraday_line = vwap or nearest_above(close, [open_price, row["repair"]]) or row["repair"]
    pullback_support = nearest_below(close, [
        stats.get("recent30_low") if stats else None,
        stats.get("recent60_low") if stats else None,
        vwap,
        stats.get("opening_low") if stats else None,
        row["defense"],
    ]) or row["defense"]

    strong_invalid = max([x for x in [row["repair"], vwap, stats.get("recent30_low") if stats else None] if x and x < close] or [row["repair"]])
    add_trigger = max([x for x in [row["repair"], vwap] if x] or [row["repair"]])
    return {
        "trend_defense": row["defense"],
        "trend_repair": row["repair"],
        "trend_pressure": trend_pressure,
        "execution_pressure": execution_pressure,
        "limit_up": limit_up,
        "vwap": vwap,
        "intraday_line": intraday_line,
        "first_bounce": first_bounce,
        "pullback_support": pullback_support,
        "strong_invalid": strong_invalid,
        "add_trigger": add_trigger,
        "volume_trend": (stats.get("volume_trend") if stats else None) or "分钟线不可用",
        "minutes": stats.get("minutes") if stats else 0,
    }


def axis_mark(level):
    return {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(level, "🟡")


def axis_score(level):
    return {"green": 2, "yellow": 1, "red": 0}.get(level, 1)


def axis_profile(row):
    q = row["quote"]
    dyn = row.get("dynamic") or {}
    close = q["close"]
    vwap = dyn.get("vwap")
    focus = row.get("focus") or ""
    amt = amount_yi(q)
    topic_hit = any(k in focus for k in ("直接点名", "题材承接", "PCB", "算力", "CPO", "光通信", "AI", "半导体", "存储"))

    if amt >= 80 or "直接点名" in focus or ("框架筛选" in focus and topic_hit and amt >= 30):
        recognition = "green"
    elif topic_hit or amt >= 8:
        recognition = "yellow"
    else:
        recognition = "red"

    if vwap and close < vwap and q["pct"] < 0:
        position = "red"
    elif vwap and close >= vwap and q["pct"] >= 0 and row.get("pos", 50) >= 50:
        position = "green"
    elif vwap and close >= vwap:
        position = "yellow"
    elif close >= row.get("defense", close):
        position = "yellow"
    else:
        position = "red"

    pressure = dyn.get("execution_pressure") or dyn.get("trend_pressure") or row.get("pressure")
    upside = (pressure - close) / close if pressure and close else 0
    if position == "green" and recognition in ("green", "yellow") and upside >= 0.002:
        odds = "green"
    elif position == "red" or (vwap and close < vwap and q["pct"] < 0):
        odds = "red"
    else:
        odds = "yellow"

    total = axis_score(recognition) + axis_score(position) + axis_score(odds)
    return {
        "recognition": recognition,
        "position": position,
        "odds": odds,
        "total": total,
    }


def axis_text(row):
    axes = row.get("axes") or axis_profile(row)
    return (
        f"辨识度{axis_mark(axes['recognition'])}｜"
        f"位置{axis_mark(axes['position'])}｜"
        f"赔率{axis_mark(axes['odds'])}"
    )


def compass_action(row, bucket):
    q = row["quote"]
    dyn = row["dynamic"]
    sig = row.get("signal", {})
    if bucket == "P0":
        line = dyn.get("vwap") or dyn.get("intraday_line")
        return f"跌破VWAP/日内强弱线 {price_text(line)} 且弱于盘面，反抽不过第一反抽 {price_text(sig.get('bounce_reduce_price'))} 不补；继续弱按风险减压。"
    if bucket == "P1":
        return f"已有利润优先持强；急拉不追，回踩VWAP {price_text(dyn.get('vwap'))} 不破才考虑100股小仓试错，跌回 {price_text(sig.get('invalid_price'))} 失效。"
    return f"等待重新站回VWAP/日内强弱线 {price_text(dyn.get('intraday_line'))}；不站回不加仓，反抽无量先看减压。"


def load_global_risk_context():
    path = BASE_DIR / "data" / "runtime" / "global_risk_context.json"
    try:
        context = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return context if str(context.get("date") or "") == str(REPORT_DATE) else {}


def radar_entry_action(row, global_context=None, now=None):
    q = row["quote"]
    dyn = row["dynamic"]
    axes = row.get("axes") or axis_profile(row)
    vwap = dyn.get("vwap")
    invalid = dyn.get("pullback_support") or vwap or q.get("low")
    trigger = q["close"] if axes["position"] == "green" else dyn.get("intraday_line")
    gate_reason = global_risk.market_opportunity_gate_reason(global_context, row=row, now=now)
    if gate_reason:
        return (
            f"{gate_reason}；等待全球风险解除且回踩VWAP {price_text(vwap)} 不破或放量站稳 "
            f"{price_text(trigger)}；失效 {price_text(invalid)}，急拉不追。"
        )
    if axes["position"] == "green" and axes["odds"] in ("green", "yellow"):
        return (
            f"替代机会：只等回踩VWAP {price_text(vwap)} 不破或重新放量站稳 {price_text(trigger)}；"
            f"可小仓试错，失效 {price_text(invalid)}，急拉不追。"
        )
    if vwap:
        return f"暂不介入：位置未满足，等重新站回VWAP {price_text(vwap)} 且不冲高回落；失效看 {price_text(invalid)}。"
    return "暂不介入：分钟线/VWAP不可用，无法确认小周期承接。"


def radar_opportunity_status(row):
    axes = row.get("axes") or axis_profile(row)
    if axes["position"] == "green" and axes["odds"] in ("green", "yellow"):
        return "🟢 替代机会"
    if axes["recognition"] == "green" and axes["position"] != "red":
        return "🟡 等待确认"
    return "🔴 暂不介入"


def radar_trigger_price(row):
    q = row["quote"]
    dyn = row["dynamic"]
    axes = row.get("axes") or axis_profile(row)
    if axes["position"] == "green":
        return q["close"]
    return dyn.get("intraday_line") or dyn.get("vwap") or q.get("open") or q["close"]


def radar_invalid_price(row):
    q = row["quote"]
    dyn = row["dynamic"]
    return dyn.get("pullback_support") or dyn.get("vwap") or q.get("low") or q["close"]


def radar_opportunity_table_lines(radar_rows, global_context=None, now=None, limit=6):
    lines = []
    for r in (radar_rows or [])[:limit]:
        q = r["quote"]
        dyn = r["dynamic"]
        lines.append(
            f"| {r['code']} | {q['name']} | {radar_opportunity_status(r)} | {f2(q['close'])} | {pct(q['pct'])} | "
            f"{price_text(dyn.get('vwap'))} | {price_text(radar_trigger_price(r))} | {price_text(radar_invalid_price(r))} | "
            f"{amount_yi(q):.1f}亿 | {axis_text(r)} | {r.get('focus')} | {radar_entry_action(r, global_context, now)} |"
        )
    return lines


def board_names(boards, n=6):
    return [str(x.get("f14") or "") for x in boards[:n] if x.get("f14")]


def market_thread_summary(rows, boards, radar_rows=None):
    radar_rows = radar_rows or []
    sentiment = board_names(boards, 6)
    sentiment_text = "、".join(sentiment[:4]) if sentiment else "暂缺"
    sentiment_blob = " ".join(sentiment)
    ai_keywords = ("PCB", "算力", "CPO", "光通信", "通信", "半导体", "AI", "数据中心")
    consumer_keywords = ("超市", "旅游", "百货", "零售", "白酒", "医美", "商业")
    power_keywords = ("火力发电", "水力发电", "电力", "管材")

    radar_blob = " ".join((r.get("focus") or "") + " " + (r["quote"].get("industry") or "") for r in radar_rows)
    holding_blob = " ".join(r.get("focus", "") for r in rows if r["quote"]["pct"] > 0 or (r["dynamic"].get("vwap") and r["quote"]["close"] >= r["dynamic"]["vwap"]))
    capacity_ai = any(k in radar_blob or k in holding_blob for k in ai_keywords)

    if any(k in sentiment_blob for k in consumer_keywords):
        emotion = f"涨幅情绪偏消费/零售修复（{sentiment_text}）"
    elif any(k in sentiment_blob for k in power_keywords):
        emotion = f"涨幅情绪偏防御/电力基建（{sentiment_text}）"
    else:
        emotion = f"涨幅情绪在{sentiment_text}"

    if capacity_ai:
        capacity = "容量交易仍在光通信/CPO/PCB/半导体等AI硬件链，但内部明显分化"
    else:
        capacity = "容量交易暂未形成清晰AI硬件主攻，替代机会从当下强势板块中筛选"
    return {
        "emotion": emotion,
        "capacity": capacity,
        "summary": f"{emotion}；{capacity}",
    }


def compass_groups(rows):
    p0 = [r for r in rows if r["signal"]["priority"] == "P0" or (r["axes"]["position"] == "red" and r["quote"]["pct"] < 0)]
    p1 = [
        r for r in rows
        if r not in p0 and r["axes"]["position"] == "green" and r["axes"]["recognition"] in ("green", "yellow")
    ]
    p2 = [r for r in rows if r not in p0 and r not in p1]
    p0.sort(key=lambda r: (axis_score(r["axes"]["odds"]), r["quote"]["pct"]))
    p1.sort(key=lambda r: (-r["axes"]["total"], -r["quote"]["pct"]))
    p2.sort(key=lambda r: (-r["axes"]["total"], r["quote"]["pct"]))
    return p0, p1, p2


def safe_float(value):
    try:
        if value == "-":
            return None
        return float(value)
    except Exception:
        return None


def market_for_code(code):
    return "17" if str(code).startswith("6") else "33"


def em_row_to_quote(row):
    code = str(row.get("f12") or "")
    close = safe_float(row.get("f2"))
    prev = safe_float(row.get("f18"))
    open_price = safe_float(row.get("f17")) or close
    high = safe_float(row.get("f15")) or close
    low = safe_float(row.get("f16")) or close
    amount_yuan = safe_float(row.get("f6")) or 0
    if not code or close is None or prev is None:
        return None
    return {
        "name": str(row.get("f14") or code),
        "code": code,
        "close": close,
        "prev_close": prev,
        "open": open_price,
        "volume_lot": safe_float(row.get("f5")) or 0,
        "datetime": "",
        "change": safe_float(row.get("f4")) or 0,
        "pct": safe_float(row.get("f3")) or 0,
        "high": high,
        "low": low,
        "amount_wan": amount_yuan / 10000,
        "turnover": safe_float(row.get("f8")),
        "industry": str(row.get("f100") or ""),
        # Upstream concepts remain available for research, but never prove a
        # tradable sector/theme relationship by themselves.
        "concepts": str(row.get("f103") or ""),
        "raw_concepts": str(row.get("f103") or ""),
    }


def active_theme_keywords(boards):
    text = " ".join(str(x.get("f14") or "") for x in boards[:12])
    return list(dict.fromkeys(key for key in RADAR_THEME_KEYWORDS if key in text))


def sector_momentum_for_candidate(q, boards=None, matched=None, focus="", contract=None, rotations=None):
    """Attach a sector-emotion fact to every radar/core candidate.

    The global technology gate may only leave a non-tech direction open when
    this board snapshot is strong *and* the realtime engine later confirms
    individual minute volume.  A name/theme match without board strength is
    deliberately insufficient.
    """
    q = q or {}
    evidence = q.get("theme_evidence") or theme_validation.candidate_theme_evidence(q, boards)
    matched = list(evidence.get("labels") or matched or [])
    industry = str(q.get("industry") or "").strip()
    raw_concepts = q.get("concepts") or q.get("concept") or ""
    if isinstance(raw_concepts, (list, tuple, set)):
        concept_tokens = [str(value).strip() for value in raw_concepts if str(value).strip()]
    else:
        concept_tokens = [
            value.strip()
            for value in re.split(r"[\s、,/，;；|]+", str(raw_concepts))
            if value.strip()
        ]
    choices = []
    metrics = (contract or {}).get('daily_metrics') or {}
    identity_v2 = metrics.get('resonance_policy') == 'company_board_identity_v2'
    if identity_v2 and (metrics.get('resonance_identity') or {}).get('status') != 'ready':
        return {'board_name': '', 'board_id': '', 'board_pct': None, 'emotion_ok': False,
                'reason': '计划主线不可执行：' + str((metrics.get('resonance_identity') or {}).get('reason') or '身份未验证')}
    locked = metrics.get('resonance_boards') if metrics.get('resonance_policy') == 'locked_mainline_v1' else None
    if identity_v2:
        locked = metrics.get('resonance_boards') or []
    for board in boards or []:
        name = str(board.get("f14") or "").strip()
        strength = safe_float(board.get("f3"))
        if not name or strength is None:
            continue
        if identity_v2 and str(board.get('f12') or '') not in (metrics.get('resonance_board_ids') or []):
            continue
        if locked is not None and name not in locked:
            continue
        exact_industry = bool(industry and industry == name)
        industry_contains = bool(
            industry and not exact_industry and min(len(industry), len(name)) >= 2
            and (industry in name or name in industry)
        )
        exact_concept = any(token == name for token in concept_tokens)
        concept_contains = any(
            token != name and min(len(token), len(name)) >= 2
            and (token in name or name in token)
            for token in concept_tokens
        )
        # A broad family such as "technology" is useful for discovery, but it
        # cannot prove execution resonance: otherwise a semiconductor stock can
        # be incorrectly attached to the strongest software board.
        if not (exact_industry or industry_contains or exact_concept or concept_contains or locked is not None and name in locked):
            continue
        score = (
            100 if exact_industry else
            80 if industry_contains else
            70 if exact_concept else
            60
        )
        choices.append((score, strength, name))
    if choices:
        # A weak legal industry must not hide an explicitly mapped strong theme.
        # Exact identity still wins over fuzzy containment within each tier.
        def selection_key(item):
            score, strength, name = item
            rotation = (rotations or {}).get(name) or {}
            confirmed = bool(strength >= .8 and rotation.get('sustained') and
                (rotation.get('leader_healthy') or ((contract or {}).get('key') in {'TREND_520','TREND_MA5'} and strength >= 1.5)))
            return (confirmed, strength >= .8, score, strength)
        _, board_pct, board_name = max(choices, key=selection_key)
        return {
            "sector_name": industry or board_name,
            "board_name": board_name,
            "board_id": next((str(b.get('f12') or '') for b in boards or []
                              if str(b.get('f14') or '') == board_name
                              and (not identity_v2 or str(b.get('f12') or '') in metrics.get('resonance_board_ids', []))), ''),
            "board_pct": round(board_pct, 2),
            "selection_basis": "authorized_identity_then_live_persistence_then_business_priority",
            "emotion_ok": bool(board_pct >= 0.8),
            "reason": f"板块情绪：{board_name} {board_pct:.2f}%",
            "theme_evidence": evidence,
            "matched_boards": [
                {"board_name": name, "board_pct": strength, "match_score": score}
                for score, strength, name in choices
            ],
        }
    return {
        "sector_name": industry or "行业暂缺",
        "board_name": "",
        "board_pct": None,
        "emotion_ok": False,
        "reason": evidence.get("reason") or "板块情绪未匹配到当日强势板块",
        "theme_evidence": evidence,
    }


def fetch_market_scan_rows(fid, size):
    query = (
        f"pn=1&pz={size}&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid={fid}"
        "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
        "&fields=f12,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18,f8,f10,f100,f102,f103"
    )
    hosts = (
        "https://push2delay.eastmoney.com/api/qt/clist/get?",
        "https://82.push2.eastmoney.com/api/qt/clist/get?",
        "https://push2.eastmoney.com/api/qt/clist/get?",
    )
    for host in hosts:
        try:
            data = base.fetch_json(host + query, headers={"Referer": "https://quote.eastmoney.com/center/gridlist.html"}, timeout=12)
            rows = data.get("data", {}).get("diff", []) or []
            if rows:
                return rows
        except Exception:
            continue
    return []


def radar_candidate_score(q, theme_keys, boards=None):
    amt = amount_yi(q)
    pct_chg = q.get("pct", 0)
    high = q.get("high") or q["close"]
    low = q.get("low") or q["close"]
    open_price = q.get("open") or q["close"]
    pos = (q["close"] - low) / (high - low) * 100 if high > low else 50
    # Do not use the provider's broad concept list as execution evidence.
    # It can contain stale or cross-sector tags and previously caused a
    # semiconductor candidate to be labelled as a medical theme.
    theme_text = f"{q.get('industry', '')} {q.get('name', '')}"
    evidence = theme_validation.candidate_theme_evidence(q, boards) if boards else None
    if evidence is not None:
        q["theme_evidence"] = evidence
        matched = list(evidence.get("labels") or [])
    else:
        matched = [k for k in theme_keys if k and k in theme_text]

    score = 0
    score += min(4, amt / 35)
    if pct_chg > 0:
        score += 2.2
    if 0 <= pct_chg <= 7:
        score += 1.2
    elif pct_chg > 9:
        score -= 1.5
    elif pct_chg < -2:
        score -= 2
    if pos >= 65:
        score += 1.5
    elif pos < 35:
        score -= 1
    if q["close"] >= open_price:
        score += 0.8
    if matched:
        score += 2 + min(1.5, len(matched) * 0.4)
    if amt < 8:
        score -= 3
    return score, matched, pos


def select_radar_candidates(existing_codes, boards):
    existing_codes = set(existing_codes or set())
    with ThreadPoolExecutor(max_workers=2) as pool:
        amount_future = pool.submit(fetch_market_scan_rows, "f6", 180)
        pct_future = pool.submit(fetch_market_scan_rows, "f3", 100)
        raw_rows = amount_future.result() + pct_future.result()
    theme_keys = active_theme_keywords(boards)
    board_text = " ".join(str(x.get("f14") or "") for x in boards[:12])
    seen = {}
    for raw in raw_rows:
        q = em_row_to_quote(raw)
        if not q or q["code"] in existing_codes:
            continue
        name = q.get("name", "")
        if "ST" in name or q["close"] <= 0:
            continue
        score, matched, pos = radar_candidate_score(q, theme_keys, boards=boards)
        evidence = q.get("theme_evidence") or {}
        if not evidence.get("valid"):
            continue
        if score < 4.6:
            continue
        old = seen.get(q["code"])
        if not old or score > old["score"]:
            seen[q["code"]] = {
                "quote": q,
                "score": score,
                "matched": matched,
                "pos": pos,
                "theme_evidence": evidence,
            }
    ranked = sorted(seen.values(), key=lambda x: (-x["score"], -amount_yi(x["quote"]), -x["quote"]["pct"]))
    return ranked[:RADAR_SCAN_LIMIT]


def build_radar_rows(candidates, stats_map=None, boards=None):
    if not candidates:
        return []
    stats_map = stats_map or fetch_intraday_stats([item["quote"]["code"] for item in candidates])
    rows = []
    for item in candidates:
        q = item["quote"]
        code = q["code"]
        stats = stats_map.get(code, {"available": False})
        matched = "、".join(item.get("matched") or [])
        industry = q.get("industry") or "行业暂缺"
        evidence = item.get("theme_evidence") or theme_validation.candidate_theme_evidence(q, boards)
        # Refresh against the current board snapshot even for replayed report
        # candidates; yesterday's label cannot stand in for today's evidence.
        q["theme_evidence"] = evidence
        focus = f"全市场框架筛选：{industry}"
        if matched:
            focus += f"｜主线匹配：{matched}"
        focus += f"｜强度评分：{item['score']:.1f}"
        base_row = {
            "code": code,
            "name": q.get("name") or code,
            "state": "雷达观察",
            "priority": "P2",
            "focus": focus,
            "community": "未纳入自选社区样本",
            "defense": q.get("low") or q["close"],
            "repair": (stats.get("vwap") if stats.get("available") else q.get("open")) or q["close"],
            "pressure": q.get("high") or q["close"],
            "premarket_advice": "全市场按框架筛选出的强势替代观察，不作为直接买入依据",
        }
        gap, from_open, drawdown, pos = quote_metrics(q)
        dynamic = dynamic_levels(base_row, q, stats)
        row = {
            **base_row,
            "quote": q,
            "theme_evidence": evidence,
            "intraday_stats": stats,
            "dynamic": dynamic,
            "gap": gap,
            "from_open": from_open,
            "drawdown": drawdown,
            "pos": pos,
        }
        # The live engine may attach an auditable intraday rotation plan.  Keep
        # it on the row so later gates and the paper executor see the same
        # position cap and three-cycle evidence, rather than only dashboard text.
        for key in (
            "framework_context",
            "framework_validation",
            "candidate_source",
            "tracked_candidate",
            "rotation_pilot",
            "entry_position_cap_pct",
            "premarket_plan_action",
            "premarket_plan_allows_entry",
            "premarket_plan_loaded",
            "premarket_plan_source",
            "planned_target_position_pct",
            "planned_max_position_pct",
            "planned_v2_probe_position_pct",
        ):
            if key in item:
                row[key] = item[key]
        row["sector_momentum"] = sector_momentum_for_candidate(q, boards=boards, matched=item.get("matched"), focus=focus)
        row["risk_bucket"] = global_risk.candidate_risk_bucket(row)
        row["axes"] = axis_profile(row)
        rows.append(row)
    rows.sort(key=lambda r: (-r["axes"]["total"], -amount_yi(r["quote"]), -r["quote"]["pct"]))
    return rows[:4]


def mode_from_rows(rows, quotes):
    idx_down = 0
    idx_total = 0
    for code in ("000001", "399001", "399006"):
        q = quotes.get(code)
        if not q:
            continue
        idx_total += 1
        if q.get("pct", 0) < 0:
            idx_down += 1
    up = sum(1 for r in rows if r["quote"]["pct"] > 0)
    down = sum(1 for r in rows if r["quote"]["pct"] < 0)
    broken = sum(1 for r in rows if r["quote"]["close"] <= r["defense"])
    if broken >= 3:
        return "破位处理"
    if idx_total and idx_down >= 2 and down >= max(7, up * 2):
        return "风控优先"
    return "正常盯盘"


def mode_rule_text(mode):
    if mode == "破位处理":
        return "多只自选股跌破防守位，按破位处理：先处理 P0 风险，不补仓摊平；反抽不放量优先减压。"
    if mode == "风控优先":
        return "指数与自选股共振偏弱，按风控优先：不新开仓，P0 执行风险减仓，P1 只守防守和等反抽减压。"
    return "按正常盯盘：强趋势可持有观察，加仓仍必须等修复位站稳且量价确认。"


def evaluate(row, q, mode="正常盯盘"):
    close = q["close"]
    gap, from_open, drawdown, pos = quote_metrics(q)
    defense = row["defense"]
    repair = row["repair"]
    pressure = row["pressure"]
    priority = row["priority"]
    dynamic = row.get("dynamic") or {}
    first_bounce = dynamic.get("first_bounce") or (q.get("open") if q.get("open", close) > close else repair)
    intraday_line = dynamic.get("intraday_line") or repair
    vwap = dynamic.get("vwap")
    add_trigger = dynamic.get("add_trigger") or repair
    pullback_support = dynamic.get("pullback_support") or defense
    strong_invalid = dynamic.get("strong_invalid") or repair
    pressure_target = dynamic.get("execution_pressure") or dynamic.get("trend_pressure") or pressure
    risk_off = mode in ("风控优先", "破位处理")

    def pack(color, signal_priority, state, command, risk_reduce, bounce_reduce, add_price, invalid_price, action):
        return {
            "color": color,
            "priority": signal_priority,
            "state": state,
            "command": command,
            "risk_reduce_price": risk_reduce,
            "bounce_reduce_price": bounce_reduce,
            "add_price": add_price,
            "invalid_price": invalid_price,
            "action": action,
        }

    if close <= defense:
        return pack(
            "🔴",
            "P0",
            "防守失守",
            "执行风险减仓",
            defense,
            first_bounce,
            None,
            defense,
            f"操作：现价已低于趋势防守 {f2(defense)}，按风险减仓处理；第一反抽 {f2(first_bounce)} 无量或回落继续减压；先站回日内强弱线 {f2(intraday_line)}，再站回趋势修复 {f2(repair)}，才取消风险状态。",
        )
    if q["high"] >= pressure * 0.995 and close < pressure:
        return pack(
            "🟡",
            "P1",
            "冲压力回落",
            "反抽减压",
            defense,
            pressure,
            None if risk_off else add_trigger,
            defense,
            f"操作：趋势压力 {f2(pressure)} 已测试但未站稳，反抽到 {f2(pressure)} 附近无量或冲高回落先减压；跌破 {f2(defense)} 执行风险减仓。",
        )
    if gap > 1 and from_open < -0.8:
        return pack(
            "🟡",
            "P1",
            "高开回落",
            "高开回落减压",
            defense,
            first_bounce,
            None,
            defense,
            f"操作：高开后回落，第一反抽 {f2(first_bounce)} 不能放量站稳先减压；跌破 {f2(defense)} 风险减仓，站回 {f2(intraday_line)} 前不加仓。",
        )
    if close >= repair and close >= q["open"] and q["pct"] >= 0:
        if risk_off:
            return pack(
                "🟢",
                "P1" if priority != "P0" else "P0",
                "逆势强/不追加",
                "持有不追加",
                strong_invalid,
                pressure_target if pressure_target > close else None,
                None,
                strong_invalid,
                f"操作：弱市逆势站上趋势修复 {f2(repair)}，已有仓位可持有观察；不追加。跌回日内失效 {f2(strong_invalid)} 下方先减压，放量突破 {f2(pressure_target)} 后再恢复进攻观察。",
            )
        return pack(
            "🟢",
            "P1" if priority != "P0" else "P0",
            "修复触发/承接较强",
            "条件加仓",
            defense,
            pressure_target,
            add_trigger,
            add_trigger,
            f"操作：只有站稳加仓触发 {f2(add_trigger)} 且5/15分钟不破，才允许小仓加；跌回 {f2(add_trigger)} 下方立即取消加仓。",
        )
    if q["pct"] <= -2:
        bounce_reduce = first_bounce
        signal_priority = "P1" if priority != "P0" else "P0"
        if close <= defense * 1.01 or (risk_off and vwap and close < vwap):
            signal_priority = "P0"
        return pack(
            "🔴" if signal_priority == "P0" else "🟡",
            signal_priority,
            "低开弱承接",
            "不加仓/反抽减压",
            defense,
            bounce_reduce,
            None,
            defense,
            f"操作：今天不加仓；跌破 {f2(defense)} 执行风险减仓。第一反抽 {f2(bounce_reduce)} 无量或回落先减压，只有先站回 {f2(intraday_line)} 并放量上修复 {f2(repair)} 才恢复观察。",
        )
    if pos < 35:
        return pack(
            "🟡",
            priority,
            "日内偏弱",
            "守位观察",
            defense,
            first_bounce,
            None if risk_off else add_trigger,
            defense,
            f"操作：现价靠近日内低位，先守趋势防守 {f2(defense)}；反抽 {f2(first_bounce)} 无量先减压，弱市下不主动加仓。",
        )
    color = status_icon(row["state"], "🟡")
    add_price = None if risk_off else add_trigger
    command = "持有观察" if risk_off else "按位执行"
    return pack(
        color,
        priority,
        "正常观察",
        command,
        defense,
        pressure_target,
        add_price,
        defense,
        f"操作：按盘前计划执行。守 {f2(defense)}，回踩 {f2(pullback_support)} 不破可观察；站稳 {f2(add_trigger)} 才恢复进攻；反抽 {f2(pressure_target)} 无量减压。",
    )


def ensure_monitoring_levels(row, q):
    """Supply observation-only fallbacks with safe monitor levels.

    A newly synced Tonghuashun observation can legitimately be absent from the
    premarket artifact.  It has no new-entry authority, but still flows
    through the shared intraday view.  Do not let absent plan levels turn that
    one passive row into a process-wide failure.
    """
    close = float(q.get("close") or q.get("prev_close") or 0.0)
    if close <= 0:
        return row
    low = float(q.get("low") or close)
    high = float(q.get("high") or close)
    defaults = {
        "defense": min(low, close * 0.94),
        "repair": close,
        "pressure": max(high, close * 1.03),
    }
    missing = [key for key in defaults if not isinstance(row.get(key), (int, float)) or float(row[key]) <= 0]
    if not missing:
        return row
    row = dict(row)
    for key in missing:
        row[key] = round(defaults[key], 4)
    row["level_source"] = str(row.get("level_source") or "") + "|observation_runtime_monitor_fallback"
    return row


def build_rows(levels, quotes, intraday_stats=None):
    rows = []
    intraday_stats = intraday_stats or fetch_intraday_stats(levels.keys())
    for code, row in levels.items():
        q = quotes.get(code)
        if not q:
            continue
        row = ensure_monitoring_levels(row, q)
        gap, from_open, drawdown, pos = quote_metrics(q)
        stats = intraday_stats.get(code, {"available": False})
        dynamic = dynamic_levels(row, q, stats)
        rows.append({
            **row,
            "quote": q,
            "intraday_stats": stats,
            "dynamic": dynamic,
            "gap": gap,
            "from_open": from_open,
            "drawdown": drawdown,
            "pos": pos,
        })
    mode = mode_from_rows(rows, quotes)
    for row in rows:
        row["signal"] = evaluate(row, row["quote"], mode)
        row["mode"] = mode
        row["axes"] = axis_profile(row)
    priority_rank = {"P0": 0, "P1": 1, "P2": 2}
    rows.sort(key=lambda x: (priority_rank.get(x["signal"]["priority"], 9), x["quote"]["pct"]))
    return rows


def market_summary(quotes):
    idx = []
    for code, name in [("000001", "上证"), ("399001", "深成"), ("399006", "创业板")]:
        q = quotes.get(code)
        if q:
            idx.append(f"{name} {pct(q['pct'])}")
    return " | ".join(idx) if idx else "指数行情暂缺"


def load_latest_realtime_signals(max_age_seconds=90):
    if not LATEST_SIGNAL_PATH.exists():
        return None
    try:
        data = json.loads(LATEST_SIGNAL_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    updated = data.get("updated_at")
    stale = True
    if updated:
        try:
            delta = (base.current_datetime() - datetime.strptime(updated, "%Y-%m-%d %H:%M:%S")).total_seconds()
            stale = delta > max_age_seconds
        except Exception:
            stale = True
    data["stale"] = stale
    return data


def v2_realtime_snapshot():
    """Return the active three-method view plus shared risk exits/waits."""
    data = load_latest_realtime_signals() or {}
    signals = [
        signal for signal in (data.get("signals") or [])
        if signal.get("strategy_family") == "THREE_METHOD"
        or str(signal.get("scenario") or "") in V2_EXIT_SCENARIOS
    ]
    executable = []
    waits = []
    for signal in signals:
        scenario = signal.get("scenario")
        contract = signal.get("signal_contract") or {}
        contract_authorized = bool(contract.get("sim_allowed"))
        entry_authorized = not data.get("stale") and is_formal_entry_reference(signal)
        exit_authorized = (
            scenario in V2_EXIT_SCENARIOS
            and signal.get("external_status") == "立即处理"
            and contract_authorized
        )
        if entry_authorized or exit_authorized:
            executable.append(signal)
        elif scenario in {"V2_WAIT", "V2_NO_ADD"} or scenario in THREE_METHOD_ENTRY_SCENARIOS or scenario in V2_EXIT_SCENARIOS:
            waits.append(signal)
    return {"data": data, "signals": signals, "executable": executable, "waits": waits}


def is_formal_entry_reference(signal, now=None):
    """Return true only for a fresh, complete three-method buy contract."""
    now = now or base.current_datetime()
    scenario = signal.get("scenario")
    timing = signal.get("timing_v2") or {}
    contract = signal.get("signal_contract") or {}
    paper_status = str((signal.get('paper_trade') or {}).get('status') or '')
    if paper_status.startswith(('NO_ORDER', 'REJECTED', 'CANCELLED', 'EXPIRED', 'ERROR')):
        return False
    if not (
        scenario in THREE_METHOD_ENTRY_SCENARIOS
        and bool(timing.get("entry_allowed"))
        and signal.get("external_status") == "立即处理"
        and signal.get("opportunity_grade") in {"A", "B"}
        and not bool(signal.get("opportunity_hard_veto") or signal.get("hard_veto"))
        and contract.get("side") == "BUY"
        and bool(contract.get("sim_allowed"))
        and not signal.get("contract_errors")
    ):
        return False
    try:
        expires_at = datetime.strptime(str(contract.get("expires_at") or ""), "%Y-%m-%d %H:%M:%S")
        if now > expires_at:
            return False
        current = float(signal.get("current_price"))
        exec_low = float(contract.get("exec_low"))
        exec_high = float(contract.get("exec_high"))
    except (TypeError, ValueError):
        return False
    return exec_low <= current <= exec_high


def unique_messages(items, limit=3):
    result = []
    for item in items or []:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


WAIT_BLOCKER_CATEGORIES = (
    ("time", "时段截止"),
    ("data", "数据依赖"),
    ("premarket", "盘前资格"),
    ("daily", "日线/大周期资格"),
    ("strategy", "策略合同"),
    ("structure", "120m结构/路径"),
    ("extension", "位置/追高/空间"),
    ("setup", "15m Setup"),
    ("execution", "5m执行"),
    ("volume", "量能确认"),
    ("sector", "板块确认"),
    ("other", "其他条件"),
)

TRACKING_STATE_LABELS = {
    "DISCOVERED": "已发现",
    "QUALIFIED": "已入围",
    "SETUP_FORMING": "结构成形",
    "NEAR_TRIGGER": "临近触发",
    "TRIGGERED": "已触发",
    "ORDER_PENDING": "待成交",
    "FILLED": "已成交",
    "PARTIAL": "部分成交",
    "UNFILLED": "未成交",
    "CANCELLED": "已取消",
    "DATA_BLOCKED": "数据阻断",
    "LIMIT_LOCKED": "涨停锁定",
    "EXPIRED": "时段失效",
    "TRIGGERED_BUT_MISSED": "触发但错过",
    "MANAGING": "持仓管理",
    "MANAGING_REENTRY": "持仓修复/再入场管理",
    "EXIT": "退出触发",
    "REDUCE": "减仓触发",
}


def signal_tracking_state(signal):
    tracking = signal.get("tracking") or {}
    state = str(tracking.get("state") or signal.get("tracking_state") or "").upper()
    if state:
        return state
    if signal.get("external_status") == "立即处理":
        return "TRIGGERED"
    if signal.get("external_status") == "接近触发":
        return "NEAR_TRIGGER"
    timing = signal.get("timing_v2") or {}
    if timing.get("setup_15m") not in (None, "", "WAIT", "MIXED_SETUP"):
        return "SETUP_FORMING"
    return "DISCOVERED"


def signal_rating_text(signal):
    grade = str(signal.get("opportunity_grade") or "D").upper()
    score = signal.get("opportunity_score")
    score_text = f"{float(score):.0f}" if score is not None else "-"
    return f"{grade}级 {score_text}分"


def tracking_funnel_summary(signals):
    order = (
        "DISCOVERED", "QUALIFIED", "SETUP_FORMING", "NEAR_TRIGGER", "TRIGGERED",
        "ORDER_PENDING", "FILLED", "PARTIAL", "UNFILLED", "DATA_BLOCKED",
        "LIMIT_LOCKED", "EXPIRED", "MANAGING", "EXIT", "REDUCE",
        "TRIGGERED_BUT_MISSED", "MANAGING_REENTRY",
    )
    counts = {state: 0 for state in order}
    for signal in signals or []:
        state = signal_tracking_state(signal)
        if state in counts:
            counts[state] += 1
    return [
        {"state": state, "label": TRACKING_STATE_LABELS[state], "count": counts[state]}
        for state in order
        if counts[state]
    ]


def opportunity_grade_summary(signals):
    counts = {grade: 0 for grade in "ABCD"}
    for signal in signals or []:
        grade = str(signal.get("opportunity_grade") or "D").upper()
        counts[grade if grade in counts else "D"] += 1
    return counts


def wait_blocker_category(reason):
    text = str(reason or "")
    if "14:45" in text or "不新开仓" in text:
        return "time"
    if any(key in text for key in ("历史不足", "数据降级", "数据不可用", "交叉校验", "fail-closed")):
        return "data"
    if "盘前计划" in text or "盘前仓位" in text:
        return "premarket"
    if any(key in text for key in ("月线", "周线", "日线", "大周期", "均线资格", "技术资格")):
        return "daily"
    if any(key in text for key in ("策略路由", "策略合同", "证据路径", "可交易策略")):
        return "strategy"
    if any(key in text for key in ("120分钟", "结构状态", "路径硬否决", "DISTRIBUTION_SHOCK")):
        return "structure"
    if any(key in text for key in ("MID_AIR", "CLIMAX", "EXTENDED", "延伸状态", "禁止追价", "Room", "盈亏比")):
        return "extension"
    if "15分钟" in text or "15m" in text or "二次转强" in text:
        return "setup"
    if any(key in text.lower() for key in ("量能", "rvol", "额比", "volume")):
        return "volume"
    if any(key in text for key in ("5分钟", "5m", "VWAP", "抬高低点")):
        return "execution"
    if "板块" in text or "单股脉冲" in text:
        return "sector"
    return "other"


def signal_blocker_diagnostics(signal):
    timing = signal.get("timing_v2") or {}
    diagnostics = [dict(item) for item in (timing.get("blocker_diagnostics") or []) if isinstance(item, dict)]
    seen = {str(item.get("reason") or "") for item in diagnostics}
    blockers = list(timing.get("blockers") or []) + list(signal.get("reasons") or [])
    for blocker in blockers:
        for reason in re.split(r"[；;]", str(blocker or "")):
            reason = reason.strip()
            if not reason or reason in seen:
                continue
            category = wait_blocker_category(reason)
            hard = (
                category in {"time", "data", "premarket", "daily", "strategy"}
                or "不允许" in reason
                or "硬否决" in reason
                or "DISTRIBUTION_SHOCK" in reason
            )
            diagnostics.append({
                "reason": reason,
                "severity": "HARD" if hard else "TEMPORARY",
                "retryable": not hard,
            })
            seen.add(reason)
    return diagnostics


def summarize_wait_blockers(waits):
    labels = dict(WAIT_BLOCKER_CATEGORIES)
    category_symbols = {key: set() for key, _ in WAIT_BLOCKER_CATEGORIES}
    hard_symbols = set()
    structural_hard_symbols = set()
    time_locked_symbols = set()
    temporary_only_symbols = set()
    for signal in waits or []:
        symbol = str(signal.get("symbol") or signal.get("name") or id(signal))
        diagnostics = signal_blocker_diagnostics(signal)
        has_hard = False
        for item in diagnostics:
            reason = item.get("reason") if isinstance(item, dict) else str(item)
            category = wait_blocker_category(reason)
            category_symbols[category].add(symbol)
            if str((item or {}).get("severity") if isinstance(item, dict) else "").upper() == "HARD":
                has_hard = True
                if category == "time":
                    time_locked_symbols.add(symbol)
                else:
                    structural_hard_symbols.add(symbol)
        if has_hard:
            hard_symbols.add(symbol)
        else:
            temporary_only_symbols.add(symbol)
    categories = [
        {"key": key, "label": labels[key], "count": len(category_symbols[key])}
        for key, _ in WAIT_BLOCKER_CATEGORIES
        if category_symbols[key]
    ]
    categories.sort(key=lambda item: (-item["count"], list(labels).index(item["key"])))
    return {
        "total": len(waits or []),
        "hard": len(hard_symbols),
        "structural_hard": len(structural_hard_symbols),
        "time_locked": len(time_locked_symbols),
        "retryable_before_cutoff": max(0, len(waits or []) - len(structural_hard_symbols)),
        "temporary_only": len(temporary_only_symbols),
        "categories": categories,
    }


def waiting_signal_score(signal):
    timing = signal.get("timing_v2") or {}
    score = {"TREND": 6, "REPAIR": 3, "BEAR": -8, "UNKNOWN": -10}.get(timing.get("regime"), 0)
    score += {
        "TIER1_SUPPORT": 5,
        "TIER2_MA20": 4,
        "STRUCTURAL_RECLAIM": 5,
        "MID_AIR": -4,
        "NEAR_RESISTANCE": -5,
    }.get(timing.get("location"), 0)
    score += 4 if timing.get("setup_15m") not in (None, "", "WAIT", "MIXED_SETUP") else 0
    score += {
        "VWAP_RECLAIM": 4,
        "HIGHER_LOW": 4,
        "BREAKOUT_HOLD": 4,
        "VWAP_HOLD": 2,
        "FAILURE": -4,
    }.get(timing.get("execution_5m"), 0)
    if (timing.get("execution_gates") or {}).get("volume_confirmed"):
        score += 2
    if timing.get("extension_state") in ("CLIMAX", "EXTENDED"):
        score -= 6
    diagnostics = signal_blocker_diagnostics(signal)
    non_time_hard = sum(
        1 for item in diagnostics
        if str(item.get("severity") or "").upper() == "HARD"
        and wait_blocker_category(item.get("reason")) != "time"
    )
    score -= non_time_hard * 10
    if not non_time_hard:
        score += 3
    return score


def near_ready_waits(waits, limit=3):
    eligible = []
    for signal in waits or []:
        diagnostics = signal_blocker_diagnostics(signal)
        active = [
            item for item in diagnostics
            if wait_blocker_category(item.get("reason")) != "time"
        ]
        if any(
            str(item.get("severity") or "").upper() == "HARD"
            for item in active
        ):
            continue
        if any(wait_blocker_category(item.get("reason")) == "extension" for item in active):
            continue
        if not active or len(active) > 2:
            continue
        eligible.append(signal)
    eligible.sort(key=lambda signal: (-waiting_signal_score(signal), str(signal.get("symbol") or "")))
    return eligible[:limit]


def wait_chain_text(signal):
    timing = signal.get("timing_v2") or {}
    return (
        f"{timing.get('regime') or '-'} / {timing.get('location') or '-'}｜"
        f"15m {timing.get('setup_15m') or '-'}｜5m {timing.get('execution_5m') or '-'}"
    )


def signal_gate_matrix(signal):
    timing = signal.get("timing_v2") or {}
    regime = str(timing.get("regime") or "UNKNOWN")
    path_state = str(timing.get("path_state") or "-")
    location = str(timing.get("location") or "-")
    setup = str(timing.get("setup_15m") or "WAIT")
    execution = str(timing.get("execution_5m") or "WAIT")
    if not signal.get("premarket_plan_loaded"):
        plan = "⚠️缺失"
    elif not signal.get("premarket_plan_allows_entry"):
        plan = "⛔禁开"
    elif not signal.get("premarket_plan_complete"):
        plan = "⚠️不完整"
    else:
        plan = "✅允许"
    if timing.get("path_hard_block") or regime == "BEAR":
        structure = f"⛔{regime}/{path_state}"
    elif regime == "TREND":
        structure = f"✅{regime}/{path_state}"
    else:
        structure = f"🟡{regime}/{path_state}"
    if location in {"TIER1_SUPPORT", "TIER2_MA20", "STRUCTURAL_RECLAIM", "NEAR_SUPPORT"}:
        location_text = f"✅{location}"
    elif location in {"MID_AIR", "NEAR_RESISTANCE"} or timing.get("extension_state") in {"CLIMAX", "EXTENDED"}:
        location_text = f"⛔{location}"
    else:
        location_text = f"🟡{location}"
    setup_text = f"{'✅' if setup != 'WAIT' else '⏳'}{setup}"
    execution_text = f"{'✅' if execution in {'VWAP_RECLAIM', 'VWAP_HOLD', 'HIGHER_LOW', 'BREAKOUT_HOLD'} else '⏳'}{execution}"
    room = timing.get("room_risk") or {}
    room_text = f"Room {f2(room.get('room_atr'))}ATR / RR {f2(room.get('reward_risk'))}"
    return (
        f"资格{plan}｜120m {structure}｜位置 {location_text}｜"
        f"15m {setup_text}｜5m {execution_text}｜{room_text}"
    )


def signal_level_summary(signal):
    timing = signal.get("timing_v2") or {}
    levels = timing.get("levels") or {}
    return (
        f"现价 {price_text(signal.get('current_price'))} / VWAP {price_text(signal.get('vwap'))}｜"
        f"支撑 {price_text(levels.get('tier1_support'))}｜入场失效 {price_text(levels.get('entry_invalidation'))}｜"
        f"硬防守 {price_text(levels.get('structural_invalidation'))}｜"
        f"阻力 {price_text(levels.get('nearest_resistance'))}"
    )


def position_cap_text(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return "按盘前计划"
    return f"{parsed * 100:.1f}%" if 0 <= parsed <= 1 else f"{parsed:.1f}%"


def formal_entry_reference_text(entries):
    if not entries:
        return "⚪ **当前无绿色正式买入信号。** 本轮不得根据评级、涨幅、已发现、资格允许或门控变化自行开仓。"
    blocks = []
    for signal in entries[:5]:
        contract = signal.get("signal_contract") or {}
        timing = signal.get("timing_v2") or {}
        levels = timing.get("levels") or {}
        blocks.append(
            f"🟢 **绿色正式买入信号｜立即处理｜{signal.get('name')}({signal.get('symbol')})**\n"
            f"- 质量：{signal_rating_text(signal)}｜场景 {signal.get('scenario')}｜现价 {price_text(signal.get('current_price'))}\n"
            f"- 买入参考区间：**{price_text(contract.get('exec_low'))}～{price_text(contract.get('exec_high'))}**｜"
            f"试仓上限 {position_cap_text(contract.get('position_cap_pct'))}\n"
            f"- 入场失效：**{price_text(contract.get('invalid_price') or levels.get('entry_invalidation'))}**｜"
            f"第一目标 {price_text(contract.get('target_price') or levels.get('nearest_resistance'))}｜"
            f"成本后RR {f2(contract.get('net_reward_risk'))}\n"
            f"- 有效至：**{contract.get('expires_at') or '-'}**｜确认：120m {timing.get('regime') or '-'} / "
            f"15m {timing.get('setup_15m') or '-'} / 5m {timing.get('execution_5m') or '-'}\n"
            "- 纪律：高于执行区上沿不追；跌破入场失效位取消；过期后必须等待新合同。"
        )
    return "\n\n".join(blocks)


def critical_wait_card_text(waits, limit=3):
    candidates = near_ready_waits(waits, limit=limit)
    if not candidates:
        return "当前没有仅差一至两项条件的临界候选；禁止从普通观察池自行挑票下单。"
    return "\n".join(
        f"- 🟡 **{signal.get('name')}({signal.get('symbol')})｜{signal_rating_text(signal)}**｜禁止下单\n"
        f"  证据：{wait_chain_text(signal)}｜下一关：{signal_next_condition(signal)}"
        for signal in candidates
    )


def signal_next_condition(signal, limit=2):
    diagnostics = [
        item for item in signal_blocker_diagnostics(signal)
        if wait_blocker_category(item.get("reason")) != "time"
    ]
    diagnostics.sort(key=lambda item: str(item.get("severity") or "").upper() != "HARD")
    reasons = unique_messages([item.get("reason") for item in diagnostics], limit=limit)
    if not reasons:
        return "等待下一根已收盘K线重新确认"
    return "；".join(reasons)


def detailed_watch_signals(signals, paper_symbols=None, limit=5):
    paper_symbols = set(paper_symbols or set())
    candidates = [signal for signal in signals or [] if str(signal.get("symbol") or "") not in paper_symbols]
    executable = [signal for signal in candidates if signal.get("scenario") in THREE_METHOD_ENTRY_SCENARIOS]
    near_ready = near_ready_waits([signal for signal in candidates if signal.get("scenario") == "V2_WAIT"], limit=limit)
    selected = executable + [signal for signal in near_ready if signal not in executable]
    if len(selected) < limit:
        remaining = [signal for signal in candidates if signal not in selected]
        remaining.sort(key=lambda signal: (-waiting_signal_score(signal), str(signal.get("symbol") or "")))
        selected.extend(remaining[:limit - len(selected)])
    return selected[:limit]


def build_feishu_audit_state(snapshot, now=None):
    now = now or base.current_datetime()
    return {
        "trading_date": now.strftime("%Y-%m-%d"),
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "delivery_key": now.strftime("%Y-%m-%d %H:%M"),
        "signals": {
            str(signal.get("symbol") or ""): {
                "name": signal.get("name"),
                "scenario": signal.get("scenario"),
                "tracking_state": signal_tracking_state(signal),
                "opportunity_grade": signal.get("opportunity_grade") or "D",
                "opportunity_score": signal.get("opportunity_score"),
                "categories": sorted({
                    wait_blocker_category(item.get("reason"))
                    for item in signal_blocker_diagnostics(signal)
                    if wait_blocker_category(item.get("reason")) != "time"
                }),
            }
            for signal in (snapshot.get("signals") or [])
            if str(signal.get("symbol") or "")
        },
    }


def load_feishu_audit_state():
    try:
        return json.loads(FEISHU_AUDIT_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_feishu_audit_state(state):
    FEISHU_AUDIT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FEISHU_AUDIT_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def audit_change_text(previous, current, limit=5):
    if not previous or previous.get("trading_date") != current.get("trading_date"):
        return "今日首个可比较节点；从本次起记录门控解除、恶化和状态升级。"
    labels = dict(WAIT_BLOCKER_CATEGORIES)
    old_signals = previous.get("signals") or {}
    changes = []
    for symbol, item in (current.get("signals") or {}).items():
        old = old_signals.get(symbol)
        if not old:
            changes.append(f"新增监控 {item.get('name') or symbol}({symbol})")
            if len(changes) >= limit:
                break
            continue
        parts = []
        if old.get("tracking_state") != item.get("tracking_state"):
            old_label = TRACKING_STATE_LABELS.get(old.get("tracking_state"), old.get("tracking_state") or "-")
            new_label = TRACKING_STATE_LABELS.get(item.get("tracking_state"), item.get("tracking_state") or "-")
            parts.append(f"跟踪{old_label}→{new_label}")
        if old.get("opportunity_grade") != item.get("opportunity_grade"):
            parts.append(f"评级{old.get('opportunity_grade') or '-'}→{item.get('opportunity_grade') or '-'}")
        if old.get("scenario") != item.get("scenario"):
            parts.append(f"{old.get('scenario')}→{item.get('scenario')}")
        old_categories = set(old.get("categories") or [])
        new_categories = set(item.get("categories") or [])
        cleared = [labels.get(key, key) for key in sorted(old_categories - new_categories)]
        added = [labels.get(key, key) for key in sorted(new_categories - old_categories)]
        if cleared:
            parts.append("解除" + "/".join(cleared))
        if added:
            parts.append("新增" + "/".join(added))
        if parts:
            changes.append(f"{item.get('name') or symbol}({symbol})：" + "；".join(parts))
        if len(changes) >= limit:
            break
    return "\n".join(f"- {item}" for item in changes) if changes else "本节点与上一固定节点相比，无关键门控类别变化。"


def append_realtime_signal_section(lines):
    snapshot = v2_realtime_snapshot()
    data = snapshot["data"]
    lines.append("## 三、三策略实时执行状态")
    if not data:
        lines.append("- 三策略实时引擎未运行或暂无 `latest_signals.json`；不使用旧策略信号替代。")
        lines.append("")
        return
    health = data.get("health") or {}
    signals = snapshot["signals"]
    stale_text = "（数据已过期，仅作历史参考）" if data.get("stale") else ""
    lines.append(f"- 更新时间：{data.get('updated_at')} {stale_text}")
    lines.append(f"- 健康状态：{health.get('engine_status', '未知')}；行情延迟 {health.get('quote_delay_sec', '未知')} 秒；当前信号 {len(signals)} 条。")
    if not signals:
        lines.append("- 当前未取得三策略逐标的状态；不使用旧策略的即时触发替代。")
        lines.append("")
        return
    executable = snapshot["executable"]
    if not executable:
        waits = snapshot["waits"]
        blocker_summary = summarize_wait_blockers(waits)
        category_text = "、".join(
            f"{item['label']} {item['count']}只" for item in blocker_summary["categories"][:5]
        ) or "暂无结构化拦截原因"
        lines.append(
            f"- **本轮未形成三策略可执行买卖事件**：{len(waits)}只处于等待/停止加仓状态，"
            "未触发方法专属正式买入或 REDUCE/TAKE_PROFIT/STRUCTURAL_EXIT；模拟盘不下单，飞书不推送普通等待状态。"
        )
        lines.append(
            f"- 等待性质：结构硬否决 {blocker_summary['structural_hard']}只；截止前仍属条件型等待 "
            f"{blocker_summary['retryable_before_cutoff']}只；时段截止 {blocker_summary['time_locked']}只。"
            f"主因分布：{category_text}。"
        )
        candidates = near_ready_waits(waits, limit=4)
        if candidates:
            lines.append("- 最接近完成但仍不可交易的标的（按三策略证据链排序）：")
        else:
            examples = []
            for signal in waits:
                reasons = [item.get("reason") for item in signal_blocker_diagnostics(signal)]
                if reasons:
                    examples.append(f"{signal.get('name')}({signal.get('symbol')})：{reasons[0]}")
                if len(examples) >= 3:
                    break
            if examples:
                lines.append("- 代表性拦截：" + "；".join(examples) + "。")
        for signal in candidates:
            timing = signal.get("timing_v2") or {}
            blockers = timing.get("blockers") or signal.get("reasons") or ["等待多周期确认"]
            lines.append(
                f"- 🟡 **{signal.get('name')}({signal.get('symbol')})**｜{wait_chain_text(signal)}；"
                f"还缺：{'；'.join(unique_messages(blockers, limit=2))}。"
            )
        lines.append("")
        return
    for s in executable[:6]:
        icon = "🔴" if s.get("scenario") == "V2_STRUCTURAL_EXIT" else "🟡" if s.get("scenario") in V2_EXIT_SCENARIOS else "🟢"
        lines.append(
            f"- {icon} **{s.get('scenario')}｜{s.get('name')}({s.get('symbol')})**："
            f"{s.get('scenario')}，现价 {price_text(s.get('current_price'))}，触发 {price_text(s.get('trigger_price'))}，"
            f"加仓 {price_text(s.get('add_price'))}，减仓 {price_text(s.get('reduce_price'))}，失效 {price_text(s.get('invalid_price'))}。"
            f"动作：{s.get('action')}"
        )
    lines.append("")


def append_v2_execution_table(lines, rows):
    """Render only three-method contracts; legacy price-line scenarios stay hidden."""
    snapshot = v2_realtime_snapshot()
    signals = snapshot["executable"] + snapshot["waits"]
    if not signals:
        lines.append("## 四、三策略核心执行表")
        lines.append("- 当前无三策略实时状态，禁止用旧策略表格替代。")
        lines.append("")
        return
    row_map = {str(row.get("code")): row for row in rows}
    lines.append("## 四、三策略核心执行表")
    lines.append("| 代码 | 名称 | 现价 | 涨跌 | 策略状态 | 120m/位置 | 15m/5m | 执行区 | 入场失效/硬防守 | 上方阻力 | 本轮结论 |")
    lines.append("|---|---|---:|---:|---|---|---|---:|---:|---:|---|")
    for signal in signals[:20]:
        timing = signal.get("timing_v2") or {}
        levels = timing.get("levels") or {}
        room = timing.get("room_risk") or {}
        code = str(signal.get("symbol") or "")
        row = row_map.get(code) or {}
        quote = row.get("quote") or {}
        band_low = room.get("execution_band_low")
        band_high = room.get("execution_band_high")
        band = f"{price_text(band_low)}-{price_text(band_high)}" if band_low is not None and band_high is not None else "仅动态计算"
        blockers = timing.get("blockers") or signal.get("reasons") or []
        conclusion = signal.get("action") if signal in snapshot["executable"] else "等待：" + "；".join(unique_messages(blockers, limit=2))
        lines.append(
            f"| {code} | {signal.get('name') or quote.get('name') or code} | {price_text(signal.get('current_price'))} | "
            f"{pct(quote.get('pct'))} | {signal.get('scenario')} | {timing.get('regime') or '-'} / {timing.get('location') or '-'} | "
            f"{timing.get('setup_15m') or '-'} / {timing.get('execution_5m') or '-'} | {band} | "
            f"{price_text(levels.get('entry_invalidation'))}/{price_text(levels.get('structural_invalidation'))} | "
            f"{price_text(levels.get('nearest_resistance'))} | {conclusion} |"
        )
    lines.append("")


def entry_alert_candidates(rows, radar_rows=None, global_context=None, now=None, limit=8):
    alerts = []
    for r in rows:
        sig = r.get("signal") or {}
        if sig.get("priority") == "P0" or not sig.get("add_price"):
            continue
        q = r["quote"]
        dyn = r.get("dynamic") or {}
        if "不追加" in str(sig.get("action") or ""):
            continue
        alerts.append({
            "source": "自选/持仓",
            "code": r["code"],
            "name": q.get("name") or r["code"],
            "priority": sig.get("priority") or r.get("priority"),
            "current": q.get("close"),
            "trigger": sig.get("add_price"),
            "invalid": sig.get("invalid_price"),
            "support": dyn.get("vwap") or dyn.get("intraday_line"),
            "confirm": "站稳触发价，5/15分钟不破，成交额不缩量",
            "action": sig.get("action"),
        })
    for r in radar_rows or []:
        q = r.get("quote") or {}
        dyn = r.get("dynamic") or {}
        trigger = dyn.get("add_trigger") or dyn.get("trend_repair") or q.get("close")
        invalid = dyn.get("strong_invalid") or dyn.get("trend_defense")
        if not trigger or not invalid:
            continue
        alerts.append({
            "source": "全市场机会池",
            "code": r["code"],
            "name": q.get("name") or r["code"],
            "priority": "P1" if r.get("axes", {}).get("position") == "green" else "P2",
            "current": q.get("close"),
            "trigger": trigger,
            "invalid": invalid,
            "support": dyn.get("vwap"),
            "confirm": "板块共振，站稳VWAP/触发价，回踩不破才模拟试错",
            "action": radar_entry_action(r, global_context, now),
        })
    rank = {"P1": 0, "P2": 1, "P0": 2}
    alerts.sort(key=lambda x: (rank.get(x.get("priority"), 9), abs(((x.get("trigger") or 0) - (x.get("current") or 0)) / (x.get("current") or 1))))
    return alerts[:limit]


def append_entry_alert_section(lines, rows, radar_rows, global_context=None, now=None):
    alerts = entry_alert_candidates(rows, radar_rows, global_context=global_context, now=now, limit=8)
    lines.append("## 三、入场/加仓提醒")
    if not alerts:
        lines.append("- 暂无满足纪律门槛的入场/加仓提醒；不因为题材热或反弹一根线就追。")
        lines.append("")
        return
    lines.append("- 触发逻辑：只在大周期未破坏、日线结构有效、盘中站稳触发价并有量能确认时执行；急拉远离触发价则等待二次回踩。")
    lines.append("")
    lines.append("| 来源 | 代码 | 名称 | 优先级 | 现价 | 入场/加仓触发 | 支撑/VWAP | 失效价 | 确认条件 | 动作 |")
    lines.append("|---|---|---|---|---:|---:|---:|---:|---|---|")
    for item in alerts:
        lines.append(
            f"| {item['source']} | {item['code']} | {item['name']} | {item['priority']} | "
            f"{price_text(item.get('current'))} | {price_text(item.get('trigger'))} | {price_text(item.get('support'))} | "
            f"{price_text(item.get('invalid'))} | {item['confirm']} | {item['action']} |"
        )
    lines.append("")


def next_slot_label(now):
    base = now.replace(second=0, microsecond=0)
    minute = ((base.minute // 15) + 1) * 15
    if minute >= 60:
        base = base.replace(minute=0) + timedelta(hours=1)
    else:
        base = base.replace(minute=minute)
    return base.strftime("%H:%M")


def append_compass_section(lines, rows, boards, radar_rows, now, global_context=None):
    p0, p1, p2 = compass_groups(rows)
    threads = market_thread_summary(rows, boards, radar_rows)
    lines.append("## 四、AI进攻罗盘")
    lines.append(f"### 罗盘结论｜{now:%H:%M}")
    lines.append(f"- 市场主线：{threads['summary']}。")
    lines.append("- 操作含义：持仓池弱票不因原题材幻想硬扛；替代机会只从当前强势且站 VWAP 的票里找。")
    lines.append("- 下午目标：保护利润，弱票减压，等待下一次更高赔率加速。")
    lines.append("")

    lines.append("### P0｜风险先处理")
    if not p0:
        lines.append("- 暂无 P0 风险项。")
    for r in p0[:5]:
        q = r["quote"]
        dyn = r["dynamic"]
        lines.append(
            f"- 🔴 **P0｜{q['name']}({r['code']})**｜现价 {f2(q['close'])}｜涨幅 {pct(q['pct'])}｜"
            f"VWAP {price_text(dyn.get('vwap'))}｜成交额 {amount_yi(q):.1f}亿｜三轴：{axis_text(r)}｜动作：{compass_action(r, 'P0')}"
        )
    lines.append("")

    lines.append("### P1｜明确主攻")
    if not p1:
        lines.append("- 暂无明确主攻；强行进攻赔率不够。")
    for r in p1[:3]:
        q = r["quote"]
        dyn = r["dynamic"]
        sig = r["signal"]
        trigger = q["close"] if q["close"] >= (dyn.get("vwap") or q["close"]) else dyn.get("vwap")
        lines.append(
            f"- 🟢 **P1｜{q['name']}({r['code']})**｜大周期：{r.get('focus')}｜小周期：站VWAP承接｜"
            f"现价 {f2(q['close'])}｜涨幅 {pct(q['pct'])}｜VWAP {price_text(dyn.get('vwap'))}｜三轴：{axis_text(r)}｜"
            f"触发：站稳 {price_text(trigger)}｜数量：100股/小仓试错｜失效：{price_text(sig.get('invalid_price'))}｜动作：{compass_action(r, 'P1')}"
        )
    lines.append("")

    lines.append("### P2｜等待确认/持有观察")
    if not p2:
        lines.append("- 暂无 P2。")
    for r in p2[:4]:
        q = r["quote"]
        dyn = r["dynamic"]
        lines.append(
            f"- 🟡 **{q['name']}({r['code']})**｜现价 {f2(q['close'])}｜VWAP {price_text(dyn.get('vwap'))}｜三轴：{axis_text(r)}｜动作：{compass_action(r, 'P2')}"
        )
    lines.append("")

    if radar_rows:
        opportunity_rows = [r for r in radar_rows if r["axes"]["position"] == "green" and r["axes"]["odds"] in ("green", "yellow")]
        wait_rows = [r for r in radar_rows if r not in opportunity_rows]
        lines.append("### 全市场雷达｜替代机会池")
        if opportunity_rows:
            lines.append("- 持仓票陷入弱势时，只从这里找“更强且有明确失效价”的替代机会；没有回踩/站稳条件就不追。")
        else:
            lines.append("- 暂无高赔率替代机会；持仓弱势时以减压和等待为主。")
        for r in opportunity_rows[:4]:
            q = r["quote"]
            dyn = r["dynamic"]
            lines.append(
                f"- 🟢 **机会｜{q['name']}({r['code']})**｜现价 {f2(q['close'])}｜涨幅 {pct(q['pct'])}｜"
                f"VWAP {price_text(dyn.get('vwap'))}｜成交额 {amount_yi(q):.1f}亿｜三轴：{axis_text(r)}｜{r.get('focus')}｜动作：{radar_entry_action(r, global_context, now)}"
            )
        for r in wait_rows[: max(0, 4 - len(opportunity_rows[:4]))]:
            q = r["quote"]
            dyn = r["dynamic"]
            lines.append(
                f"- 🟡 **等待｜{q['name']}({r['code']})**｜现价 {f2(q['close'])}｜涨幅 {pct(q['pct'])}｜"
                f"VWAP {price_text(dyn.get('vwap'))}｜成交额 {amount_yi(q):.1f}亿｜三轴：{axis_text(r)}｜{r.get('focus')}｜动作：{radar_entry_action(r, global_context, now)}"
            )
        lines.append("")
        lines.append("| 机会代码 | 机会名称 | 雷达状态 | 现价 | 涨跌 | VWAP | 触发价 | 失效价 | 成交额 | 三轴 | 框架依据 | 动作 |")
        lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---|")
        lines.extend(radar_opportunity_table_lines(radar_rows, global_context=global_context, now=now, limit=6))
        lines.append("")

    lines.append(f"### 下一轮触发价｜{next_slot_label(now)}前")
    trigger_rows = (p1[:2] + p2[:2] + p0[:2])[:5]
    for r in trigger_rows:
        q = r["quote"]
        dyn = r["dynamic"]
        sig = r["signal"]
        if r in p1:
            lines.append(f"- {q['name']}：守住VWAP {price_text(dyn.get('vwap'))}，强于 {f2(q['close'])} 继续持强；跌破 {price_text(sig.get('invalid_price'))} 先减压。")
        elif r in p0:
            lines.append(f"- {q['name']}：第一反抽 {price_text(sig.get('bounce_reduce_price'))} 不过不补；重新站回 {price_text(dyn.get('intraday_line'))} 才取消风险。")
        else:
            lines.append(f"- {q['name']}：重新站回VWAP/日内强弱线 {price_text(dyn.get('intraday_line'))} 才恢复观察价值。")
    lines.append("")


def build_paper_price_map(quotes, radar_rows=None):
    price_map = {}
    for code, quote in (quotes or {}).items():
        if re.fullmatch(r"\d{6}", str(code)):
            price_map[str(code)] = {
                "last_price": quote.get("close"),
                "name": quote.get("name"),
                "pct": quote.get("pct"),
            }
    for row in radar_rows or []:
        quote = row.get("quote") or {}
        code = str(row.get("code") or quote.get("code") or "")
        if re.fullmatch(r"\d{6}", code):
            price_map[code] = {
                "last_price": quote.get("close"),
                "name": quote.get("name"),
                "pct": quote.get("pct"),
            }
    return price_map


def paper_position_codes():
    try:
        return {
            str(pos.get("symbol"))
            for pos in base.paper_trading.load_positions(BASE_DIR)
            if int(pos.get("quantity") or 0) > 0
        }
    except Exception:
        return set()


def make_report(rows, quotes, boards, elapsed, radar_rows=None, paper_snapshot=None, global_context=None, observation_summary=None):
    now = base.current_datetime()
    radar_rows = radar_rows or []
    global_context = global_context or {}
    lines = []
    lines.append(f"# A股盘中订盘建议｜{now:%Y-%m-%d %H:%M}\n")
    lines.append(f"- 生成时间：{now:%Y-%m-%d %H:%M:%S}（Asia/Shanghai）")
    lines.append(f"- 执行耗时：{elapsed:.1f}s。")
    lines.append("- 说明：盘中轻量版使用实时行情 + 盘前同花顺专题映射/关键位缓存；仅为交易计划参考，不构成投资建议。")
    lines.append("")
    lines.append("## 一、市场开盘情绪")
    lines.append(f"- 指数：{market_summary(quotes)}")
    if boards:
        lines.append("- 当前强势板块：" + "、".join(f"{x.get('f14')} {pct(x.get('f3'))}" for x in boards[:8]))
    up = sum(1 for r in rows if r["quote"]["pct"] > 0)
    down = sum(1 for r in rows if r["quote"]["pct"] < 0)
    lines.append(f"- 自选股红绿：上涨 {up} 只，下跌 {down} 只。")
    lines.append("- 开盘判断：优先看高开是否有承接、低开是否快速收回修复位；题材映射不能替代量价确认。")
    lines.append("")
    lines.append("## 二、三策略执行规则")
    lines.append("- 盘前锁定分类、交易主线与仓位；盘中验证同一主线共振。趋势方法以日线MA5/MA20锚点及闭合5m触发，龙头以开盘承接或15m转强配合有时效的5m突破触发；共同保留量能、Room/RR和风险纪律。")
    lines.append("- 只有龙头、520、趋势5日线各自允许的时机模式通过全部门控，才生成对应策略的正式买入信号；原E1-E7只能作为内部证据，不能独立下单。")
    lines.append("- 已有仓位继续接受 REDUCE、TAKE_PROFIT、STRUCTURAL_EXIT 三类结构退出事件；没有三策略正式事件即为等待，不生成模拟订单。")
    lines.append("")
    append_realtime_signal_section(lines)
    append_v2_execution_table(lines, rows)
    lines.append("## 五、全自选观察池与策略合同")
    lines.append("- 覆盖同花顺全部非核心自选分组；每只均按盘前策略分类进入实时追踪，而不是仅做题材涨速观察。")
    lines.append("- 龙头、520、五日线策略各自生成正式买入事件；共享计算层仅提供闭合K线时机、量能、位置、Room/RR和风控证据。待分类标的不创建模拟订单。")
    lines.extend(base.observation_group_summary_lines(observation_summary, limit=8))
    lines.append("")
    base.append_paper_position_section(lines, paper_snapshot, heading="六、模拟账户持仓表现")
    return "\n".join(lines)


def lark_text(text):
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def market_permission_view(health, quotes):
    regime = health.get("a_share_market_regime") or {}
    breadth = health.get("market_breadth") or regime.get("breadth") or {}
    indexes = regime.get("indexes") or {}
    index_parts = []
    for key in ("shanghai", "shenzhen", "growth"):
        item = indexes.get(key) or {}
        if item.get("pct") is not None:
            index_parts.append(f"{item.get('label') or key} {pct(item.get('pct'))}")
    index_line = "｜".join(index_parts) or market_summary(quotes)
    preparation_blocked = (health.get("new_entry_status") == "blocked"
                           or (health.get("preopen_quality_gate") or {}).get("allowed") is False
                           or (health.get("storage") or {}).get("ready") is False)
    policy = (health.get('global_risk') or {}).get('policy') or {}
    risk_blocked = health.get('new_entry_status') == 'risk_blocked' or policy.get('allow_core_attack_buy') is False
    can_attack = bool(health.get("can_attack")) and not preparation_blocked and not risk_blocked
    permission = "OPEN（可继续等个股确认）" if can_attack else "CLOSED（停止新增仓）"
    if preparation_blocked:
        permission = "BLOCKED（执行依赖未就绪）"
    elif risk_blocked:
        permission = "CLOSED（全局风险禁止新增仓；不是等待个股买点）"
    coverage = breadth.get("coverage_ratio")
    coverage_text = pct(float(coverage) * 100) if coverage is not None else "未知"
    if breadth.get("coverage_ready"):
        state_label = {'broad_weak':'广度偏弱','shrinking_weak':'广度偏弱且成交速率下降',
                       'broad_strong':'广度强势','mixed':'涨跌分化'}.get(breadth.get('state'), '待确认')
        breadth_text = (
            f"涨/跌 {breadth.get('up', '-')}/{breadth.get('down', '-')}｜"
            f"覆盖 {coverage_text}｜{state_label}"
        )
    else:
        breadth_text = f"覆盖 {coverage_text}，样本未达门控标准；{breadth.get('reason') or '广度仅作旁证'}"
    return {
        "permission": permission,
        "index_line": index_line,
        "breadth_text": breadth_text,
        "regime": regime.get("label") or regime.get("state") or "市场状态未知",
        "can_attack": can_attack,
        "preparation_blocked": preparation_blocked,
    }


def paper_position_card_text(paper_snapshot, signal_map):
    paper_snapshot = paper_snapshot or {}
    account = paper_snapshot.get("account") or {}
    positions = [
        position for position in (paper_snapshot.get("positions") or [])
        if int(position.get("quantity") or 0) > 0
    ]
    if not positions:
        return ("模拟账本待对账，暂停新增仓，收益不可用。" if (account.get('ledger_quality') or {}).get('ready') is False
                else "当前模拟盘无持仓；信号台账不冒充成交。")
    lines = [
        f"持仓 {len(positions)}只｜仓位 {pct(account.get('position_pct'))}｜"
        f"当日 {f2(account.get('day_pnl'))}｜浮盈亏 {f2(account.get('unrealized_pnl'))}"
    ]
    if (account.get('ledger_quality') or {}).get('ready') is False:
        lines.insert(0, '模拟账本待对账：以下持仓/资产为待核记录，收益不可用；暂停新增仓及异常标的交易。')
    for position in positions[:4]:
        symbol = str(position.get("symbol") or "")
        signal = signal_map.get(symbol) or {}
        timing = signal.get("timing_v2") or {}
        levels = timing.get("levels") or {}
        scenario = signal.get("scenario") or "V2状态缺失"
        position_action = timing.get("position_action") or signal.get("execution_action") or "未评估"
        position_reason = timing.get("position_reason") or "未取得持仓退出原因"
        audit_warning = ""
        if signal and int(position.get("sellable") or 0) > 0 and signal.get("position_context") != "SELLABLE_POSITION":
            audit_warning = "｜⚠️V2未识别可卖持仓"
        review = position.get("review") or {}
        review_text = ""
        if review.get("level") and review.get("level") != "NORMAL":
            review_text = f"\n  持仓复核：**{review.get('level')}**｜{review.get('reason')}"
        lines.append(
            f"- **{position.get('name') or symbol}({symbol})**｜持仓/可卖 "
            f"{int(position.get('quantity') or 0)}/{int(position.get('sellable') or 0)}｜"
            f"成本 {price_text(position.get('avg_cost'))} / 现价 {price_text(position.get('last_price'))}｜"
            f"当日 {pct(position.get('day_pnl_pct'))} / 累计 {pct(position.get('unrealized_pnl_pct'))}{audit_warning}\n"
            f"  V2 {scenario} / {position_action}｜"
            f"{TRACKING_STATE_LABELS.get(signal_tracking_state(signal), signal_tracking_state(signal))}｜{signal_rating_text(signal)}｜"
            f"{timing.get('regime') or '-'} / {timing.get('path_state') or '-'} / "
            f"{timing.get('location') or '-'}｜15m {timing.get('setup_15m') or '-'} / 5m {timing.get('execution_5m') or '-'}\n"
            f"  防守：失效 {price_text(levels.get('structural_invalidation'))}｜阻力 "
            f"{price_text(levels.get('nearest_resistance'))}｜结论：{position_reason}"
            f"{review_text}"
        )
    return "\n".join(lines)


def next_fixed_report_node(now):
    nodes = ((10, 0, "整点盯盘"), (11, 0, "整点盯盘"), (13, 0, "午后开盘盯盘"),
             (14, 0, "整点盯盘"), (15, 0, "收盘盯盘"), (16, 30, "盘后复盘"))
    current_minutes = now.hour * 60 + now.minute
    for hour, minute, label in nodes:
        if current_minutes < hour * 60 + minute:
            return f"{hour:02d}:{minute:02d} {label}"
    return "下一交易日 08:30 盘前计划"


def detailed_watch_card_text(snapshot, paper_snapshot, limit=5):
    paper_symbols = {
        str(position.get("symbol") or "")
        for position in ((paper_snapshot or {}).get("positions") or [])
        if int(position.get("quantity") or 0) > 0
    }
    formal_symbols = {
        str(signal.get("symbol") or "") for signal in (snapshot.get("executable") or [])
        if signal.get("scenario") in THREE_METHOD_ENTRY_SCENARIOS
    }
    candidates = [
        signal for signal in (snapshot.get("signals") or [])
        if str(signal.get("symbol") or "") not in formal_symbols
    ]
    selected = detailed_watch_signals(candidates, paper_symbols=paper_symbols, limit=limit)
    if not selected:
        return "当前没有通过盘前资格且接近执行的候选；不以涨幅榜填充。"
    lines = []
    for signal in selected:
        diagnostics = signal_blocker_diagnostics(signal)
        has_hard = any(
            str(item.get("severity") or "").upper() == "HARD"
            and wait_blocker_category(item.get("reason")) != "time"
            for item in diagnostics
        )
        has_extension_risk = any(
            wait_blocker_category(item.get("reason")) == "extension"
            for item in diagnostics
        )
        status = "⛔硬否决｜禁止下单" if has_hard or has_extension_risk else "⚪仅观察｜禁止下单"
        strategy = signal.get("strategy_contract") or {}
        strategy_text = (
            f"策略：{strategy.get('name')}｜时机证据 {'、'.join(strategy.get('allowed_patterns') or []) or '待分类'}\n"
            if strategy.get("is_observation_strategy") else ""
        )
        lines.append(
            f"- **{signal.get('name')}({signal.get('symbol')})**｜{status}｜"
            f"{TRACKING_STATE_LABELS.get(signal_tracking_state(signal), signal_tracking_state(signal))}｜"
            f"{signal_rating_text(signal)}｜涨跌 {pct(signal.get('pct'))}\n"
            f"  {strategy_text}"
            f"  {signal_gate_matrix(signal)}\n"
            f"  {signal_level_summary(signal)}\n"
            f"  下一关：{signal_next_condition(signal)}"
        )
    return "\n".join(lines)


def observation_strategy_card_text(snapshot, limit=8):
    signals = [
        signal for signal in (snapshot.get("signals") or [])
        if (signal.get("strategy_contract") or {}).get("is_observation_strategy")
    ]
    if not signals:
        return "全自选观察池暂无已同步的策略状态。"
    buckets = {}
    for signal in signals:
        contract = signal.get("strategy_contract") or {}
        key = str(contract.get("key") or "OBSERVE_UNCLASSIFIED")
        buckets.setdefault(key, []).append(signal)
    lines = [f"覆盖 {len(signals)}只｜执行顺序：当日板块共振→日线策略资格→方法专属闭合时机→订单。"]
    for key, members in sorted(buckets.items(), key=lambda item: item[0]):
        contract = (members[0].get("strategy_contract") or {})
        name = contract.get("name") or key
        ready = [item for item in members if is_formal_entry_reference(item)]
        daily_count = sum(item.get('strategy_daily_qualified') is True for item in members)
        sector_count = sum(item.get('strategy_daily_qualified') is True and item.get('sector_resonance_ok') is True for item in members)
        setup_count = sum(bool((item.get('timing_v2') or {}).get('candidate_entry_pattern')) for item in members)
        labels = "、".join(
            f"{item.get('name')}({item.get('symbol')})"
            for item in sorted(members, key=lambda item: (item.get("scenario") == "V2_WAIT", -float(item.get("opportunity_score") or 0)))[:limit]
        )
        stock_type = '情绪龙头候选' if key == 'LEADER_EMOTION' else '待分类' if key == 'OBSERVE_UNCLASSIFIED' else '趋势票'
        lines.append(f"- {stock_type} / {name}：覆盖{len(members)}只｜日线合格{daily_count}｜"
                     f"合格且共振{sector_count}｜技术候选{setup_count}（非下单）｜当前有效正式信号{len(ready)}\n"
                     f"  跟踪：{labels or '暂无'}")
    return "\n".join(lines)


def market_opportunity_card_text(data, radar_rows=None, limit=4):
    opportunities = list(data.get("market_opportunities") or [])
    opportunities.sort(key=lambda row: (
        not bool(row.get("radar_gate_ok")),
        -int((row.get("axes") or {}).get("total") or 0),
        -float(row.get("pct") or 0),
    ))
    lines = []
    for row in opportunities[:limit]:
        sector = row.get("sector_momentum") or {}
        gate_reason = str(row.get("radar_gate_reason") or row.get("action") or "等待完整核验")
        reasons = unique_messages(re.split(r"[；;]", gate_reason), limit=2)
        lines.append(
            f"- **{row.get('name')}({row.get('code')})**｜{row.get('status') or '观察'}｜"
            f"{sector.get('board_name') or sector.get('sector_name') or '板块未映射'} "
            f"{pct(sector.get('board_pct'))}\n"
            f"  {row.get('axes_text') or '三轴未完成'}｜现价/VWAP "
            f"{price_text(row.get('current_price'))}/{price_text(row.get('vwap'))}｜"
            f"1m/5m量能 {f2(row.get('amount_ratio_1m'))}/{f2(row.get('amount_ratio_5m'))}\n"
            f"  仍不可交易：{'；'.join(reasons)}"
        )
    if lines:
        return "\n".join(lines)
    fallback = []
    for row in radar_rows or []:
        quote = row.get("quote") or {}
        if quote.get("name") and (row.get("code") or quote.get("code")):
            fallback.append(f"{quote.get('name')}({row.get('code') or quote.get('code')})")
        if len(fallback) >= limit:
            break
    return "仅取得一级发现：" + ("、".join(fallback) if fallback else "暂无") + "；尚未取得三策略盘前合同。"


def build_intraday_feishu_card(
    rows,
    quotes,
    boards,
    elapsed,
    radar_rows=None,
    global_context=None,
    paper_snapshot=None,
    observation_summary=None,
    now=None,
    snapshot=None,
    previous_audit_state=None,
):
    now = now or base.current_datetime()
    snapshot = snapshot or v2_realtime_snapshot()
    data = snapshot["data"]
    health = data.get("health") or {}
    executable = snapshot["executable"]
    waits = snapshot["waits"]
    market = market_permission_view(health, quotes)
    blocker_summary = summarize_wait_blockers(waits)
    blocker_text = "、".join(
        f"{item['label']} {item['count']}只" for item in blocker_summary["categories"][:5]
    ) or "暂无结构化拦截原因"
    entries = [signal for signal in executable if signal.get("scenario") in THREE_METHOD_ENTRY_SCENARIOS]
    exits = [signal for signal in executable if signal.get("scenario") in V2_EXIT_SCENARIOS]
    signal_map = {str(signal.get("symbol") or ""): signal for signal in snapshot["signals"]}
    near_ready_count = len(near_ready_waits(waits, limit=len(waits)))
    risk_count = len([
        signal for signal in snapshot["signals"]
        if signal.get("scenario") in V2_EXIT_SCENARIOS
        or str((signal.get("timing_v2") or {}).get("position_action") or "") in {
            "REDUCE", "TAKE_PROFIT", "STRUCTURAL_EXIT", "NO_ADD",
        }
    ])
    deep_checked = len(health.get("tracked_opportunity_codes") or [])
    coverage = f"{len(snapshot['signals'])}/{health.get('watchlist_count') or health.get('watched') or '-'}"
    funnel = tracking_funnel_summary(snapshot["signals"])
    funnel_text = " → ".join(f"{item['label']} {item['count']}" for item in funnel) or "尚无可追踪候选"
    grades = opportunity_grade_summary(snapshot["signals"])
    grade_text = " / ".join(f"{grade} {grades[grade]}" for grade in "ABCD")
    watchlist_sync = health.get("watchlist_sync") or {}
    universe_text = (
        f"股票池 {watchlist_sync.get('status') or '未知'}｜"
        f"新增仓 {'允许' if watchlist_sync.get('entry_allowed') else '关闭'}｜"
        f"分组晋升 {health.get('observation_group_promoted', 0)}只"
    )

    if executable:
        execution_text = "\n\n".join(
            f"{'🔴' if signal.get('scenario') == 'V2_STRUCTURAL_EXIT' else '🟢'} **{signal.get('scenario')}｜{signal.get('name')}({signal.get('symbol')})**\n"
            f"{signal_rating_text(signal)}｜{TRACKING_STATE_LABELS.get(signal_tracking_state(signal), signal_tracking_state(signal))}｜"
            f"现价 {price_text(signal.get('current_price'))}｜执行：{signal.get('action')}\n"
            f"入场失效 {price_text((signal.get('timing_v2') or {}).get('levels', {}).get('entry_invalidation'))}｜"
            f"硬防守 {price_text((signal.get('timing_v2') or {}).get('levels', {}).get('structural_invalidation'))}"
            for signal in executable[:5]
        )
        conclusion = f"形成 {len(entries)}个入场、{len(exits)}个退出事件；只按事件执行，不扩展到同板块其他股票。"
    else:
        execution_text = (
            f"扫描 {len(snapshot['signals'])}只｜执行 0｜等待 {len(waits)}｜"
            f"结构硬否决 {blocker_summary['structural_hard']}｜条件型等待 {blocker_summary['retryable_before_cutoff']}｜"
            f"时段截止 {blocker_summary['time_locked']}\n"
            f"主因：{blocker_text}\n"
            f"订单链路：本轮执行意图 0｜今日执行拒绝 {health.get('v2_execution_rejections_today', 0)}｜"
            f"今日模拟成交 {health.get('paper_fills_today', 0)}。"
            + ("执行依赖未就绪，不能把无单归因为没有买点。" if market["preparation_blocked"]
               else "当前未形成可执行订单；需结合数据质量和逐关阻断原因判断。")
        )
        if market["preparation_blocked"]:
            conclusion = "新增仓链路阻断：" + str((health.get("preopen_quality_gate") or {}).get("reason") or (health.get("storage") or {}).get("reason") or "检查执行依赖")
        elif not market["can_attack"]:
            conclusion = "当前市场/时段已关闭新增仓；继续管理持仓，不再把盘后波动解释成漏单。"
        elif blocker_summary["temporary_only"]:
            conclusion = "市场层允许寻找机会，但个股证据链尚未闭合；等待不是空转，而是明确缺少可重试条件。"
        else:
            conclusion = "市场层虽开放，但候选均存在硬否决；今日不为提高交易次数而放宽纪律。"

    order_reference_text = formal_entry_reference_text(entries)
    critical_wait_text = critical_wait_card_text(waits)

    board_line = "、".join(f"{x.get('f14')} {pct(x.get('f3'))}" for x in boards[:4]) if boards else "板块行情暂缺"
    tracked = health.get("tracked_opportunities_live", len(radar_rows or []))
    actionable = health.get("radar_actionable_signals", 0)
    discovery_names = []
    for row in radar_rows or []:
        quote = row.get("quote") or {}
        name = quote.get("name") or row.get("name")
        code = row.get("code") or quote.get("code")
        if name and code:
            discovery_names.append(f"{name}({code})")
        if len(discovery_names) >= 3:
            break
    discovery_detail = "、".join(discovery_names) or "暂无可展示标的"
    discovery_text = (
        f"实时发现 {tracked}只｜已接入深检 {deep_checked}只｜正式V2可执行 {actionable}只\n"
        f"一级扫描关注：{discovery_detail}\n"
        f"{market_opportunity_card_text(data, radar_rows=radar_rows)}\n"
        "分层纪律：发现池 ≠ 盘前资格 ≠ V2信号 ≠ 模拟成交；池外标的缺少当日仓位计划时只观察。"
    )

    current_audit_state = build_feishu_audit_state(snapshot, now=now)
    change_text = audit_change_text(previous_audit_state or {}, current_audit_state)

    breadth = health.get("market_breadth") or {}
    freshness_text = (
        f"行情延迟 {f2(health.get('quote_delay_sec'))}秒｜分钟线延迟 {f2(health.get('minute_bar_delay_sec'))}秒｜"
        f"分钟覆盖 {health.get('minute_bar_symbols', '-')}只｜广度覆盖 "
        f"{pct(float(breadth.get('coverage_ratio')) * 100) if breadth.get('coverage_ratio') is not None else '-'}"
    )
    header_template = "red" if exits else "green" if entries else "grey"
    header_conclusion = f"🟢绿色正式买入信号 {len(entries)}只" if entries else "⚪当前无绿色正式买入信号"
    order_section_title = "**🟢 绿色正式买入信号**" if entries else "**⚪ 绿色正式买入信号（当前无）**"
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": header_template,
                "title": {
                    "tag": "plain_text",
                    "content": f"A股策略整点盯盘｜{now:%H:%M}｜{header_conclusion}",
                },
            },
            "elements": [
                lark_text(
                    f"**下单权限结论**\n{header_conclusion}｜正式风险动作 {len(exits)}只｜临界等待 {near_ready_count}只\n"
                    "唯一口径：只有本卡或同名即时卡的绿色“正式买入信号”可以参考开仓；A/B评级本身不是买入信号。"
                ),
                lark_text(order_section_title + "\n" + order_reference_text),
                lark_text("**🟡 临界等待（禁止下单）**\n" + critical_wait_text),
                lark_text(
                    f"**整点总览**\n覆盖 {coverage}｜动态深检 {deep_checked}｜临近机会 {near_ready_count}｜"
                    f"风险动作 {risk_count}｜持仓 {health.get('paper_positions', 0)}｜许可 {market['permission']}\n"
                    f"跟踪漏斗：{funnel_text}\n机会评级：{grade_text}\n"
                    f"{universe_text}\n"
                    f"**一句话：**{conclusion}\n"
                    "评级用于质量排序；正式买卖、失效和风控事件仍以即时合同卡为准。"
                ),
                {"tag": "hr"},
                lark_text(
                    f"**市场与板块**\n{market['regime']}｜{market['index_line']}\n"
                    f"广度：{market['breadth_text']}\n强势方向：{board_line}"
                ),
                lark_text(
                    "**全自选观察池与策略合同**\n"
                    + (
                        "全量观察池已载入盘前策略计划；龙头、520、五日线策略必须各自完成合同，才可能生成模拟订单。\n"
                        if (health.get("observation_plan") or {}).get("codes") and not market["preparation_blocked"]
                        else "当前无已验证的全自选观察池计划标的，分组仅作题材跟踪。\n"
                    )
                    + "强势组内标的不因涨速、涨幅或量能自动买入；未分类标的全程观察但仓位资格为0。\n"
                    + observation_strategy_card_text(snapshot)
                    + "\n"
                    + "\n".join(base.observation_group_summary_lines(observation_summary, limit=7))
                ),
                lark_text("**模拟持仓与退出监控（先处理已有仓位）**\n" + paper_position_card_text(paper_snapshot, signal_map)),
                lark_text("**本轮执行审计与未下单原因**\n" + execution_text),
                lark_text("**⚪ 仅观察/禁止下单（按评级与距触发排序）**\n" + detailed_watch_card_text(snapshot, paper_snapshot)),
                lark_text("**发现层（仅观察）**\n" + discovery_text),
                lark_text("**状态变化审计（不构成下单）**\n" + change_text),
                {"tag": "hr"},
                lark_text(
                    f"**数据与审计**\n引擎 {health.get('engine_status', '未知')}｜最近成功 "
                    f"{health.get('last_success_at') or data.get('updated_at') or '-'}｜报告耗时 {elapsed:.1f}s\n"
                    f"{freshness_text}\n下一固定节点：{next_fixed_report_node(now)}；实时买卖事件仍即时推送。\n"
                    f"龙头母池日期 {(health.get('leader_pool') or {}).get('source_date', '未提供')}｜"
                    f"要求日期 {(health.get('leader_pool') or {}).get('expected_date', '未提供')}｜"
                    f"开盘啦复核 {'可用' if (health.get('leader_pool') or {}).get('kaipanla_ready') else '不可用或未验证'}。\n"
                    "规则：绿色正式买入信号还必须处于合同执行区、未过期、A/B且无硬否决；其余内容全部禁止据此开仓。仅为策略研究，不构成投资建议。"
                ),
            ],
        },
    }


def send_feishu(rows, quotes, boards, elapsed, radar_rows=None, global_context=None, paper_snapshot=None, observation_summary=None):
    snapshot = v2_realtime_snapshot()
    previous_audit_state = load_feishu_audit_state()
    now = base.current_datetime()
    current_audit_state = build_feishu_audit_state(snapshot, now=now)
    if previous_audit_state.get("delivery_key") == current_audit_state.get("delivery_key"):
        return json.dumps({"code": 0, "msg": "skipped duplicate hourly delivery"}, ensure_ascii=False)
    if os.environ.get("A_SHARE_SKIP_FEISHU") == "1":
        return json.dumps({"code": 0, "msg": "skipped by A_SHARE_SKIP_FEISHU"}, ensure_ascii=False)
    card = build_intraday_feishu_card(
        rows,
        quotes,
        boards,
        elapsed,
        radar_rows=radar_rows,
        global_context=global_context,
        paper_snapshot=paper_snapshot,
        observation_summary=observation_summary,
        now=now,
        snapshot=snapshot,
        previous_audit_state=previous_audit_state,
    )
    data = json.dumps(card, ensure_ascii=False).encode("utf-8")
    last_error = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(FEISHU_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode("utf-8", "ignore")
            payload = json.loads(text)
            code = payload.get("code", payload.get("StatusCode", 0))
            if code in (0, "0", None):
                try:
                    save_feishu_audit_state(current_audit_state)
                except Exception:
                    pass
                return text
            raise FeishuPushError(text)
        except Exception as exc:
            last_error = exc
            delay = 4 * (attempt + 1) if "11232" in str(exc) else 1.5 * (attempt + 1)
            time.sleep(delay)
    raise RuntimeError(f"飞书推送失败：{last_error}")


def main():
    start = time.time()
    report_now = base.current_datetime()
    levels = read_premarket_levels()
    _, quotes = fetch_live(levels)
    observation_details = base.read_observation_watchlist_details()
    observation_quotes = base.parse_tencent_quotes(observation_details.get("rows") or []) if observation_details.get("rows") else {}
    observation_summary = base.observation_group_summary(observation_details, observation_quotes)
    boards = fetch_fast_boards()
    excluded_radar_codes = set(levels) | paper_position_codes()
    radar_candidates = select_radar_candidates(excluded_radar_codes, boards)
    all_minute_codes = list(levels) + [item["quote"]["code"] for item in radar_candidates]
    intraday_stats = fetch_intraday_stats(all_minute_codes)
    rows = build_rows(levels, quotes, intraday_stats)
    radar_rows = build_radar_rows(radar_candidates, intraday_stats, boards=boards)
    elapsed = time.time() - start
    paper_snapshot = base.build_paper_position_snapshot(
        report_date=REPORT_DATE,
        now=report_now,
        extra_price_map=build_paper_price_map(quotes, radar_rows),
    )
    global_context = load_global_risk_context()
    report = make_report(rows, quotes, boards, elapsed, radar_rows, paper_snapshot, global_context, observation_summary)
    path = BASE_DIR / f"同花顺我的股票盘中订盘_{REPORT_DATE}_{report_now:%H%M}.md"
    path.write_text(report, encoding="utf-8")
    try:
        dashboard_result = dashboard.publish_report(path)
    except Exception as exc:
        dashboard_result = {"error": str(exc)}
    feishu_result = send_feishu(rows, quotes, boards, elapsed, radar_rows, global_context, paper_snapshot, observation_summary)
    print(json.dumps({
        "report": str(path),
        "dashboard": dashboard_result,
        "rows": len(rows),
        "observation_stocks": len(observation_details.get("rows") or []),
        "elapsed": round(elapsed, 2),
        "feishu_result": feishu_result,
        "v2_executable": [signal.get("symbol") for signal in v2_realtime_snapshot()["executable"]],
        "v2_waiting": len(v2_realtime_snapshot()["waits"]),
        "paper_positions": len(paper_snapshot.get("positions", [])),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
