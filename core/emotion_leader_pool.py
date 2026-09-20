"""Build an auditable daily emotion-leader pool from popularity rankings.

Eastmoney's top-100 list is the stable mother pool.  Kaipanla snapshots are
optional corroboration: they may raise confidence and add theme context, but
they can never make a symbol executable by themselves.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import string
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


EASTMONEY_XC_ID = "xc13a236b6f0c1010a13"
EASTMONEY_DETAIL_URL = (
    "https://np-tjxg-g.eastmoney.com/api/smart-tag/stock/v3/getXcIdDetail"
    f"?xcId={EASTMONEY_XC_ID}&client=WEB"
)
EASTMONEY_SEARCH_URL = "https://np-tjxg-g.eastmoney.com/api/smart-tag/stock/v3/pw/search-code?client=WEB"
SOURCE_URL = f"https://xuangu.eastmoney.com/Result?a=edit_way&id={EASTMONEY_XC_ID}&j=refreshUrl"
GROUP_NAME = "东方财富情绪龙头候选"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(str(value).replace(",", "").replace("%", "").strip())
        return number if number == number else default
    except (TypeError, ValueError):
        return default


def _money_yi(value: Any) -> float:
    text = str(value or "").replace(",", "").strip()
    if not text or text in {"-", "--"}:
        return 0.0
    if text.endswith("亿"):
        return _f(text[:-1])
    if text.endswith("万"):
        return _f(text[:-1]) / 10000.0
    return _f(text) / 100000000.0


def _market(code: str) -> str:
    if str(code).startswith("6"):
        return "17"
    if str(code).startswith(("8", "9")):
        return "151"
    return "33"


def _limit_pct(code: str) -> float:
    if str(code).startswith(("30", "68")):
        return 20.0
    if str(code).startswith(("8", "9")):
        return 30.0
    return 10.0


def candidate_trading_state_valid(row):
    code = str(row.get("code") or "")
    name = str(row.get("name") or "").upper()
    return bool(code and not code.startswith(("8", "9")) and "ST" not in name
                and not name.startswith(("N", "C")) and not row.get("special_trading_state")
                and abs(_f(row.get("pct"))) <= _limit_pct(code) + 0.5)


def _request_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 15) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": SOURCE_URL,
        "Content-Type": "application/json;charset=UTF-8",
    }
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "ignore"))


def _request_identity() -> tuple[str, str, str]:
    seed = f"{time.time_ns()}-{random.random()}-{os.getpid()}"
    fingerprint = hashlib.md5(seed.encode("utf-8")).hexdigest()
    timestamp = f"{time.time_ns() // 1_000_000}{random.randint(100, 999)}"
    suffix = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(32))
    return fingerprint, timestamp, f"{suffix}{timestamp}"


def fetch_eastmoney_top100(timeout: int = 18) -> dict[str, Any]:
    detail = _request_json(EASTMONEY_DETAIL_URL, timeout=timeout)
    detail_data = detail.get("data") or {}
    if str(detail.get("code")) != "100" or not detail_data:
        raise RuntimeError(f"东方财富人气条件读取失败：{detail.get('msg') or detail.get('code')}")
    fingerprint, timestamp, request_id = _request_identity()
    dx_info = ((detail_data.get("keywordInfoNew") or {}).get("dxInfo") or [])
    if not dx_info:
        dx_info = [{
            "params": [{"paramInfos": [{"children": [], "paramId": 233, "optionName": "前100名"}], "paramGroupId": 1}],
            "keyCode": "10317",
        }]
    normalized_dx = []
    for item in dx_info:
        row = dict(item)
        row.update({
            "id": int(row.get("keyCode") or 10317),
            "name": "股吧人气排名前100名",
            "label": "股吧人气排名",
            "detail": "股吧人气排名前100名",
            "desc": "前100名",
        })
        normalized_dx.append(row)
    custom_data = detail_data.get("customDataNew") or json.dumps(normalized_dx, ensure_ascii=False)
    payload = {
        "needAmbiguousSuggest": True,
        "pageSize": 100,
        "pageNo": 1,
        "fingerprint": fingerprint,
        "matchWord": "",
        "shareToGuba": False,
        "timestamp": timestamp,
        "requestId": request_id,
        "removedConditionIdList": [],
        "ownSelectAll": False,
        "needCorrect": True,
        "client": "WEB",
        "product": "",
        "needShowStockNum": False,
        "biz": "web_ai_select_stocks",
        "xcId": EASTMONEY_XC_ID,
        "gids": [],
        "dxInfoNew": normalized_dx,
        "keyWordNew": "股吧人气排名前100名;",
        "customDataNew": custom_data,
    }
    response = _request_json(EASTMONEY_SEARCH_URL, payload=payload, timeout=timeout)
    result = ((response.get("data") or {}).get("result") or {})
    rows = result.get("dataList") or []
    if str(response.get("code")) != "100" or len(rows) < 90:
        raise RuntimeError(f"东方财富人气前100返回异常：{response.get('msg')}，仅{len(rows)}只")
    normalized = normalize_eastmoney_response(response)
    if not normalized.get("source_date"):
        raise RuntimeError("东方财富人气前100缺少榜单日期，拒绝生成跨日候选")
    return normalized


def normalize_eastmoney_response(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data") or {}
    result = data.get("result") or {}
    columns = result.get("columns") or []
    rank_column = next((item.get("key") for item in columns if item.get("indexName") == "GUBA_TOP_REAL_TIME"), None)
    source_date = next((str(item.get("dateMsg") or "").replace(".", "-") for item in columns if item.get("indexName") == "GUBA_TOP_REAL_TIME"), "")
    rows = []
    for raw in result.get("dataList") or []:
        code = str(raw.get("SECURITY_CODE") or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        price = _f(raw.get("NEWEST_PRICE"))
        pct = _f(raw.get("CHG"))
        high = _f(raw.get("PEAK_PRICE<140>"))
        prev_close = price / (1.0 + pct / 100.0) if price > 0 and pct > -99 else 0.0
        limit_pct = _limit_pct(code)
        name = str(raw.get("SECURITY_SHORT_NAME") or "").strip()
        # A new listing / exceptional move is not evidence of a daily limit.
        special = name.upper().startswith(("N", "C", "ST", "*ST")) or abs(pct) > limit_pct + 0.5
        touched_limit = bool(not special and prev_close > 0 and high >= prev_close * (1.0 + limit_pct / 100.0) * 0.997)
        rows.append({
            "rank": int(_f(raw.get(rank_column or "SERIAL"), _f(raw.get("SERIAL")))),
            "code": code,
            "market": _market(code),
            "name": name,
            "special_trading_state": special,
            "price": price,
            "pct": pct,
            "high": high,
            "low": _f(raw.get("BOTTOM_PRICE<140>")),
            "turnover": _f(raw.get("TURNOVER_RATE")),
            "volume_ratio": _f(raw.get("QRR")),
            "amount_yi": _money_yi(raw.get("TRADING_VOLUMES")),
            "float_market_cap_yi": _money_yi(raw.get("CIRCULATION_MARKET_VALUE<140>")),
            "total_market_cap_yi": _money_yi(raw.get("TOAL_MARKET_VALUE<140>")),
            "touched_limit": touched_limit,
            "closed_limit": bool(not special and pct >= limit_pct * 0.985),
        })
    rows.sort(key=lambda item: (item["rank"] or 999, item["code"]))
    return {
        "version": "emotion_leader_pool_v1",
        "source": "eastmoney_guba_top100",
        "source_url": SOURCE_URL,
        "source_date": source_date,
        "quote_time": data.get("quoteTime"),
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "rows": rows,
    }


def load_kaipanla_snapshot(base_dir: Path, source_date=None) -> dict[str, Any]:
    from core import kaipanla_snapshot
    archived = kaipanla_snapshot.load(base_dir, source_date=source_date)
    if archived:
        return archived
    path = base_dir / "data" / "runtime" / "kaipanla_popularity_latest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def crosscheck_status(snapshot, kaipanla, code=None):
    kaipanla = kaipanla or {}
    if not kaipanla.get('source_date'):
        return 'unavailable'
    if kaipanla['source_date'] != snapshot.get('source_date'):
        return 'stale'
    rows = kaipanla.get('rows') or []
    if not rows:
        return 'unavailable'
    if code is None:
        return 'available'
    if code and any(str(r.get('code')) == code for r in rows):
        return 'verified'
    return 'not_listed' if kaipanla.get('complete') is True else 'not_observed_partial'


def select_leader_candidates(snapshot: dict[str, Any], kaipanla: dict[str, Any] | None = None, limit: int = 30) -> list[dict[str, Any]]:
    kpl_rows = (kaipanla or {}).get("rows") or []
    if crosscheck_status(snapshot, kaipanla) in {'stale', 'unavailable'}:
        kpl_rows = []
    kpl_map = {str(item.get("code")): item for item in kpl_rows if item.get("code")}
    selected = []
    for row in snapshot.get("rows") or []:
        name = str(row.get("name") or "")
        code = str(row.get("code") or "")
        pct = _f(row.get("pct"))
        turnover = _f(row.get("turnover"))
        amount_yi = _f(row.get("amount_yi"))
        float_cap = _f(row.get("float_market_cap_yi"))
        rank = int(_f(row.get("rank"), 999))
        touched = bool(row.get("touched_limit"))
        closed = bool(row.get("closed_limit"))
        kpl = kpl_map.get(code)
        kpl_rank = int(_f((kpl or {}).get("rank"), 999))
        if (not code or "ST" in name.upper() or code.startswith(("8", "9"))
                or name.upper().startswith(("N", "C")) or row.get("special_trading_state")
                or abs(pct) > _limit_pct(code) + 0.5):
            continue
        normal_strength = bool(pct >= 5.0 and turnover >= (5.0 if touched else 8.0))
        # A high-popularity failed limit can be the next session's weak-to-strong
        # leader setup even when its closing gain is below 5%.  Keep this path
        # deliberately narrow: it needs a real limit touch, top-20 Eastmoney
        # rank, top-10 Kaipanla cross-check and broad turnover/value evidence.
        failed_limit_reversal = bool(
            touched
            and not closed
            and pct >= -5.0
            and rank <= 20
            and kpl_rank <= 10
            and turnover >= 15.0
            and amount_yi >= 8.0
        )
        if not (normal_strength or failed_limit_reversal):
            continue
        if amount_yi < (3.0 if touched else 5.0) or not (15.0 <= float_cap <= 500.0):
            continue
        if not (touched or pct >= 7.0 or rank <= 20):
            continue
        score = 2 if rank <= 20 else (1 if rank <= 50 else 0)
        score += 3 if touched else (2 if pct >= 7.0 else 1)
        score += 2 if turnover >= 15.0 else 1
        score += 2 if amount_yi >= 10.0 else 1
        score += 1 if _f(row.get("volume_ratio")) >= 1.2 else 0
        score += 2 if 30.0 <= float_cap <= 300.0 else 1
        if kpl:
            score += 2 if int(_f(kpl.get("rank"), 999)) <= 10 else 1
        if score < 8:
            continue
        reasons = [
            f"东财人气第{rank}",
            f"涨幅{pct:.2f}%",
            f"换手{turnover:.2f}%",
            f"成交{amount_yi:.1f}亿",
            f"流通市值{float_cap:.0f}亿",
        ]
        if touched:
            reasons.append("触及涨停")
        if failed_limit_reversal:
            reasons.append("触板回落次日弱转强候选")
        if kpl:
            reasons.append(f"开盘啦{str(kpl.get('list_type') or '人气榜')}第{int(_f(kpl.get('rank'), 999))}")
            if kpl.get("theme"):
                reasons.append(str(kpl.get("theme")))
        selected.append({
            **row,
            "score": score,
            "tier": "L1核心候选" if score >= 10 else "L2备选候选",
            "cross_verified": bool(kpl),
            "cross_status": crosscheck_status(snapshot, kaipanla, code),
            "theme": str((kpl or {}).get("theme") or ""),
            "leader_profile": "FAILED_LIMIT_REVERSAL" if failed_limit_reversal else "STRONG_CLOSE",
            "reasons": reasons,
            "qualified": True,
        })
    selected.sort(key=lambda item: (-item["score"], item["rank"], -item["amount_yi"]))
    return selected[:limit]


def build_snapshot(base_dir: Path, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = raw or fetch_eastmoney_top100()
    kaipanla = load_kaipanla_snapshot(base_dir, source_date=raw.get('source_date'))
    candidates = select_leader_candidates(raw, kaipanla=kaipanla)
    return {
        **raw,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "kaipanla": {
            "available": crosscheck_status(raw, kaipanla) not in {'stale', 'unavailable'},
            "status": crosscheck_status(raw, kaipanla),
            "source_date": kaipanla.get("source_date"),
            "count": len(kaipanla.get("rows") or []),
            "role": "cross_check_only",
        },
    }


def write_snapshot(base_dir: Path, snapshot: dict[str, Any]) -> Path:
    runtime = base_dir / "data" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    compact = str(snapshot.get("source_date") or datetime.now().strftime("%Y-%m-%d")).replace("-", "")
    dated = runtime / f"emotion_leader_pool_{compact}.json"
    latest = runtime / "emotion_leader_pool_latest.json"
    text = json.dumps(snapshot, ensure_ascii=False, indent=2)
    dated.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    return dated


def load_latest_snapshot(base_dir: Path, max_age_hours: float = 72.0) -> dict[str, Any]:
    path = base_dir / "data" / "runtime" / "emotion_leader_pool_latest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched = datetime.strptime(str(payload.get("fetched_at")), "%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, TypeError):
        return {}
    age_hours = max(0.0, (datetime.now() - fetched).total_seconds() / 3600.0)
    if age_hours > max_age_hours:
        return {}
    kaipanla = load_kaipanla_snapshot(base_dir, source_date=payload.get('source_date'))
    if kaipanla.get('version') == 'kaipanla_local_ui_v1' and payload.get('rows'):
        payload['candidates'] = select_leader_candidates(payload, kaipanla)
        payload['candidate_count'] = len(payload['candidates'])
        payload['crosscheck_recomputed'] = True
    payload['candidates'] = [{**item,
        'cross_status': crosscheck_status(payload, kaipanla, str(item.get('code') or '')),
        'cross_verified': crosscheck_status(payload, kaipanla, str(item.get('code') or '')) == 'verified'}
        for item in payload.get('candidates') or [] if candidate_trading_state_valid(item)]
    payload['candidate_count'] = len(payload['candidates'])
    payload['kaipanla'] = {
        'available': crosscheck_status(payload, kaipanla) not in {'stale', 'unavailable'},
        'status': crosscheck_status(payload, kaipanla),
        'source_date': kaipanla.get('source_date'),
        'count': len(kaipanla.get('rows') or []),
        'complete': bool(kaipanla.get('complete')),
        'list_type': kaipanla.get('list_type'),
    }
    return payload


def merge_into_plan(plan: dict[str, Any], snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    candidates = [item for item in snapshot.get("candidates") or [] if candidate_trading_state_valid(item)]
    if not candidates:
        return plan, {}
    merged = {**(plan or {})}
    rows = list(merged.get("rows") or [])
    groups = list(merged.get("groups") or [])
    seen = {str(code) for code, _market_code in rows}
    metadata = {}
    codes = []
    for item in candidates:
        code = str(item.get("code") or "")
        if not code:
            continue
        codes.append(code)
        metadata[code] = {**item, "source_date": snapshot.get("source_date"), "source": snapshot.get("source")}
        if code not in seen:
            rows.append((code, str(item.get("market") or _market(code))))
            seen.add(code)
    groups.append({"name": GROUP_NAME, "codes": codes, "count": len(codes), "source_date": snapshot.get("source_date")})
    merged.update({"rows": rows, "groups": groups, "emotion_source": snapshot.get("source"), "emotion_source_date": snapshot.get("source_date")})
    return merged, metadata


def summary_lines(snapshot: dict[str, Any], limit: int = 12) -> list[str]:
    candidates = snapshot.get("candidates") or []
    if not candidates:
        return ["- 东方财富人气前100未形成可用龙头候选；该来源不参与放宽下单门槛。"]
    names = "、".join(
        f"{item.get('code')} {item.get('name')}({item.get('tier')}/{item.get('score')}分)"
        for item in candidates[:limit]
    )
    cross = sum(1 for item in candidates if item.get("cross_verified"))
    return [
        f"- 情绪母池：东方财富股吧人气前100（{snapshot.get('source_date') or '日期待核'}）筛出 {len(candidates)}只龙头候选；开盘啦交叉命中 {cross}只。",
        f"- 次日核心候选：{names}{' 等' + str(len(candidates)) + '只' if len(candidates) > limit else ''}。",
        "- 执行边界：榜单只授予龙头候选资格；次日仍须实时板块共振、E5二次转强、VWAP/量能/空间和组合风控全部通过。",
    ]


if __name__ == "__main__":
    root = Path(os.environ.get("A_SHARE_BASE_DIR", Path(__file__).resolve().parents[1]))
    result = build_snapshot(root)
    output = write_snapshot(root, result)
    print(json.dumps({"path": str(output), "source_date": result.get("source_date"), "count": result.get("count"), "candidates": result.get("candidates")}, ensure_ascii=False))
