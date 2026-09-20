#!/usr/bin/env python3
import json
import os
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import after_close_report as base
import intraday_report as intra
import premarket_report as premarket
import render_report_dashboard as dashboard
from core import global_risk


BASE_DIR = Path(os.environ.get("A_SHARE_BASE_DIR", Path(__file__).resolve().parent))
REPORT_DATE = os.environ.get("A_SHARE_REPORT_DATE") or base.current_datetime().strftime("%Y-%m-%d")
REPORT_PATH = BASE_DIR / f"同花顺我的股票集合竞价后盘前更新_{REPORT_DATE}_0925.md"
FEISHU_WEBHOOK = base.FEISHU_WEBHOOK
AUCTION_TRACK_DIR = BASE_DIR / "data" / "auction_candidates"


class FeishuPushError(RuntimeError):
    pass


def f2(value, empty="-"):
    try:
        return f"{float(value):.2f}"
    except Exception:
        return empty


def pct(value, empty="-"):
    try:
        return f"{float(value):+.2f}%"
    except Exception:
        return empty


def amount_yi(q):
    return intra.amount_yi(q)


def report_time():
    return base.current_datetime().strftime("%Y-%m-%d %H:%M:%S")


def load_premarket_raw():
    candidates = [
        BASE_DIR / f"同花顺我的股票盘前全面分析_{REPORT_DATE}.md",
        Path.cwd() / f"同花顺我的股票盘前全面分析_{REPORT_DATE}.md",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8", errors="ignore")
    json_path = BASE_DIR / "web_dashboard" / "data" / "reports" / f"premarket_{REPORT_DATE.replace('-', '')}.json"
    if json_path.exists():
        try:
            return json.loads(json_path.read_text(encoding="utf-8")).get("raw", "")
        except Exception:
            return ""
    return ""


def load_premarket_levels():
    try:
        rows = intra.read_premarket_levels()
        if rows:
            return rows
    except Exception:
        pass
    json_candidates = [
        BASE_DIR / "web_dashboard" / "data" / "reports" / f"premarket_{REPORT_DATE.replace('-', '')}.json",
        Path.cwd() / "web_dashboard" / "data" / "reports" / f"premarket_{REPORT_DATE.replace('-', '')}.json",
    ]
    for path in json_candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = {}
        for row in data.get("rows") or []:
            code = str(row.get("代码") or "").strip()
            if not re.fullmatch(r"\d{6}", code):
                continue
            try:
                rows[code] = {
                    "code": code,
                    "name": row.get("名称") or code,
                    "state": row.get("状态") or "",
                    "priority": row.get("优先级") or "",
                    "focus": row.get("同花顺专题映射") or "",
                    "community": row.get("社区温度") or "",
                    "defense": float(row.get("防守") or row.get("防守/止损")),
                    "repair": float(row.get("修复") or row.get("修复触发")),
                    "pressure": float(row.get("压力")),
                    "premarket_advice": row.get("盘前建议") or "",
                }
            except Exception:
                continue
        if rows:
            return rows
    try:
        return intra.read_premarket_levels()
    except Exception:
        return {}


def theme_keywords_from_premarket(raw_text, boards):
    text = raw_text + " " + " ".join(str(x.get("f14") or "") for x in boards[:15])
    keys = []
    for theme, words in premarket.POLICY_THEME_MAP.items():
        if any(word in text for word in words):
            keys.append(theme)
            keys.extend(words)
    keys.extend(intra.active_theme_keywords(boards))
    keys.extend(str(x.get("f14") or "") for x in boards[:12])
    return premarket.unique_keep_order(keys)


def quote_gap(q):
    prev = q.get("prev_close") or q.get("close")
    open_price = q.get("open") or q.get("close")
    if not prev:
        return 0.0
    return (open_price - prev) / prev * 100


def intraday_position(q):
    close = q.get("close") or 0
    high = q.get("high") or close
    low = q.get("low") or close
    return (close - low) / (high - low) * 100 if high and high > low else 50


def auction_score(q, theme_keys, board_text):
    gap = quote_gap(q)
    amt = amount_yi(q)
    pos = intraday_position(q)
    theme_text = f"{q.get('industry', '')} {q.get('concepts', '')} {q.get('name', '')}"
    matched = premarket.policy_theme_hits(theme_text, theme_keys)
    board_hit = bool(q.get("industry") and q.get("industry") in board_text)

    score = 0.0
    score += min(5.0, amt / 6.0)
    if 0.3 <= gap <= 4.5:
        score += 3.0
    elif -0.3 <= gap < 0.3:
        score += 1.0
    elif 4.5 < gap <= 7.0:
        score += 0.8
    elif gap > 7.0:
        score -= 2.5
    elif gap < -1.5:
        score -= 2.0
    if 45 <= pos <= 92:
        score += 1.2
    elif pos > 96:
        score -= 0.8
    if matched:
        score += 3.0 + min(1.5, len(matched) * 0.4)
    if board_hit:
        score += 1.5
    if amt < 1.5:
        score -= 3.0
    return score, matched, board_hit, pos


def auction_axes(q, matched, board_hit, score):
    amt = amount_yi(q)
    gap = quote_gap(q)
    recognition = "green" if amt >= 8 or len(matched) >= 2 or (matched and board_hit) else "yellow" if amt >= 2 or matched else "red"
    if gap > 7.0 or q.get("pct", 0) > 9.0:
        position = "red"
    elif 0 <= gap <= 4.5:
        position = "green"
    elif gap > 0:
        position = "yellow"
    else:
        position = "red"
    odds = "green" if score >= 10 and position == "green" else "yellow" if score >= 7 and position != "red" else "red"
    return {"recognition": recognition, "position": position, "odds": odds}


def technical_shape(code, q):
    try:
        daily = base.fetch_sohu_daily(code)
    except Exception as exc:
        return False, f"日线不可用：{exc}", {}
    if len(daily) < 60:
        return False, "日线样本不足，暂不纳入严格候选", {}
    closes = [x["close"] for x in daily]
    mas = {n: base.simple_ma(closes, n) for n in (5, 10, 20, 30, 60)}
    close = q["close"]
    pct_chg = q.get("pct", 0)
    amount = q.get("amount_wan", 0)
    avg_amount5 = sum(x["amount_wan"] for x in daily[-6:-1]) / 5 if len(daily) >= 6 else None
    amount_ratio = amount / avg_amount5 if avg_amount5 else None
    recent_high = max(x["high"] for x in daily[-20:])
    recent_low = min(x["low"] for x in daily[-20:])

    ma5, ma10, ma20, ma30, ma60 = (mas[n] for n in (5, 10, 20, 30, 60))
    trend_ok = bool(ma5 and ma10 and ma20 and close >= ma20 and ma5 >= ma10 * 0.995 and ma10 >= ma20 * 0.97)
    medium_ok = bool(ma30 and ma60 and (close >= ma30 or ma30 >= ma60 * 0.98))
    stretch_ok = bool(ma20 and close <= ma20 * 1.28)
    damage_ok = not (pct_chg <= -4 and (amount_ratio or 0) >= 1.2)
    support_ok = bool(close >= recent_low * 1.06 and close >= (ma5 or close) * 0.97)
    near_high_not_exhausted = bool(close >= recent_high * 0.82 and pct_chg <= 8.5)
    passed = trend_ok and medium_ok and stretch_ok and damage_ok and support_ok and near_high_not_exhausted
    reasons = []
    reasons.append(f"MA5/10/20={f2(ma5)}/{f2(ma10)}/{f2(ma20)}")
    reasons.append("趋势OK" if trend_ok else "趋势未确认")
    reasons.append("中期OK" if medium_ok else "中期均线未确认")
    reasons.append("不过热" if stretch_ok else "乖离过大")
    reasons.append("无放量长阴" if damage_ok else "高位放量下跌")
    reasons.append(f"近20日高低={f2(recent_high)}/{f2(recent_low)}")
    if amount_ratio is not None:
        reasons.append(f"量比5日={amount_ratio:.2f}")
    metrics = {
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma30": ma30,
        "ma60": ma60,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "amount_ratio_5d": amount_ratio,
    }
    return passed, "；".join(reasons), metrics


def axis_text(axes):
    return (
        f"辨识度{intra.axis_mark(axes['recognition'])}｜"
        f"位置{intra.axis_mark(axes['position'])}｜"
        f"赔率{intra.axis_mark(axes['odds'])}"
    )


def opportunity_status(axes):
    if axes["recognition"] == "green" and axes["position"] == "green" and axes["odds"] in ("green", "yellow"):
        return "🟢 竞价强承接候选"
    if axes["recognition"] in ("green", "yellow") and axes["position"] != "red":
        return "🟡 竞价等待确认"
    return "🔴 暂不介入"


def trigger_invalid(q):
    price = q.get("close") or q.get("open") or 0
    open_price = q.get("open") or price
    prev = q.get("prev_close") or price
    trigger = max(price, open_price) * 1.003
    invalid = max(prev, open_price * 0.985) if quote_gap(q) >= 0 else min(open_price, prev) * 0.99
    return round(trigger + 1e-9, 2), round(invalid + 1e-9, 2)


def opportunity_action(row):
    q = row["quote"]
    gap = quote_gap(q)
    if row["status"].startswith("🟢"):
        return (
            f"9:30后看第一波承接：站稳 {f2(row['trigger_price'])} 且不跌回开盘价/VWAP，才进入模拟盘观察；"
            f"跌破 {f2(row['invalid_price'])} 删除。高开 {pct(gap)} 不追第一笔急拉。"
        )
    if row["status"].startswith("🟡"):
        return f"等开盘后 3-5 分钟量价确认；未站稳 {f2(row['trigger_price'])} 不介入，跌破 {f2(row['invalid_price'])} 删除。"
    return "竞价位置或赔率不足，只保留题材跟踪。"


def fetch_auction_candidates(existing_codes, boards, raw_text, limit=10):
    theme_keys = theme_keywords_from_premarket(raw_text, boards)
    board_text = " ".join(str(x.get("f14") or "") for x in boards[:15])
    raw_rows = []
    for fid, size in (("f6", 260), ("f3", 180)):
        raw_rows.extend(intra.fetch_market_scan_rows(fid, size))

    seen = {}
    for raw in raw_rows:
        q = intra.em_row_to_quote(raw)
        if not q or q["code"] in existing_codes:
            continue
        name = q.get("name", "")
        if "ST" in name or q.get("close", 0) <= 0:
            continue
        score, matched, board_hit, pos = auction_score(q, theme_keys, board_text)
        if score < 6.8 or not (matched or board_hit):
            continue
        tech_ok, tech_text, tech_metrics = technical_shape(q["code"], q)
        if not tech_ok:
            continue
        score += 1.8
        old = seen.get(q["code"])
        if not old or score > old["score"]:
            seen[q["code"]] = {
                "quote": q,
                "score": score,
                "matched": matched,
                "board_hit": board_hit,
                "pos": pos,
                "technical_text": tech_text,
                "technical_metrics": tech_metrics,
            }

    rows = []
    ranked = sorted(seen.values(), key=lambda x: (-x["score"], -amount_yi(x["quote"]), -quote_gap(x["quote"])))[:24]
    for item in ranked:
        q = item["quote"]
        axes = auction_axes(q, item["matched"], item["board_hit"], item["score"])
        trigger, invalid = trigger_invalid(q)
        focus = "、".join(item["matched"][:5]) if item["matched"] else f"板块共振：{q.get('industry') or '行业'}"
        if item["board_hit"] and "板块共振" not in focus:
            focus += f"｜板块共振：{q.get('industry') or '-'}"
        focus += f"｜技术确认：{item['technical_text']}"
        row = {
            "code": q["code"],
            "name": q.get("name") or q["code"],
            "quote": q,
            "score": item["score"],
            "status": opportunity_status(axes),
            "trigger_price": trigger,
            "invalid_price": invalid,
            "axes": axes,
            "focus": focus,
            "technical_text": item["technical_text"],
            "technical_metrics": item["technical_metrics"],
        }
        row["action"] = opportunity_action(row)
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows, theme_keys


def write_candidate_tracking(opportunities, indexes, boards, theme_keys):
    AUCTION_TRACK_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "trading_date": REPORT_DATE,
        "generated_at": report_time(),
        "theme_keys": theme_keys[:30],
        "indexes": indexes[:6],
        "boards": boards[:12],
        "candidates": [
            {
                "code": row["code"],
                "name": row["name"],
                "status": row["status"],
                "score": row["score"],
                "trigger_price": row["trigger_price"],
                "invalid_price": row["invalid_price"],
                "focus": row["focus"],
                "axes": row["axes"],
                "technical_text": row.get("technical_text"),
                "technical_metrics": row.get("technical_metrics"),
                "quote": row["quote"],
                "action": row["action"],
            }
            for row in opportunities
        ],
    }
    day_path = AUCTION_TRACK_DIR / f"auction_candidates_{REPORT_DATE.replace('-', '')}.json"
    day_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with (AUCTION_TRACK_DIR / "auction_candidates_history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "trading_date": REPORT_DATE,
            "generated_at": payload["generated_at"],
            "count": len(opportunities),
            "codes": [row["code"] for row in opportunities],
            "theme_keys": theme_keys[:12],
        }, ensure_ascii=False) + "\n")
    return day_path


def auction_signal_for_watch(row, quote):
    tech = row
    gap = quote_gap(quote)
    amt = amount_yi(quote)
    close = quote.get("close")
    open_price = quote.get("open") or close
    if gap >= 4.5:
        state = "高开偏多但防冲高回落"
        action = f"不追第一笔急拉；9:30后若回踩开盘价 {f2(open_price)} 不破再观察，跌回 {f2(tech.get('defense'))} 转风险。"
        priority = "P1"
    elif gap >= 1.0 and amt >= 0.5:
        state = "竞价温和高开有承接"
        action = f"看是否站稳修复位 {f2(tech.get('repair'))}；放量站稳再进攻，跌破开盘价先不动。"
        priority = "P1"
    elif gap <= -1.5:
        state = "低开弱承接"
        action = f"先防守，未快速收回 {f2(tech.get('defense'))}/{f2(open_price)} 不加仓；继续放量下破按风险处理。"
        priority = "P0" if tech.get("priority") == "P0" else "P1"
    else:
        state = "竞价中性"
        action = f"按原计划：守 {f2(tech.get('defense'))}，站回 {f2(tech.get('repair'))} 才恢复进攻观察。"
        priority = tech.get("priority") or "P2"
    return state, priority, action


def market_emotion(indexes, boards, opportunities):
    parts = []
    if indexes:
        parts.append("指数竞价：" + "、".join(f"{x.get('name')} {pct(x.get('pct'))}" for x in indexes[:3]))
    if boards:
        parts.append("板块共振：" + "、".join(f"{x.get('f14')} {pct(x.get('f3'))}" for x in boards[:8]))
    green = sum(1 for x in opportunities if x["status"].startswith("🟢"))
    yellow = sum(1 for x in opportunities if x["status"].startswith("🟡"))
    parts.append(f"全市场候选：绿色 {green}，等待确认 {yellow}")
    return "；".join(parts)


def opportunity_table_lines(rows):
    lines = []
    for row in rows:
        q = row["quote"]
        lines.append(
            f"| {row['code']} | {row['name']} | {row['status']} | {f2(q.get('close'))} | {pct(q.get('pct'))} | "
            f"竞价确认 | {f2(row['trigger_price'])} | {f2(row['invalid_price'])} | {amount_yi(q):.1f}亿 | "
            f"{axis_text(row['axes'])} | {row['focus']}｜竞价评分{row['score']:.1f} | {row['action']} |"
        )
    return lines


def make_report(levels, quotes, indexes, boards, opportunities, theme_keys):
    now = report_time()
    lines = []
    lines.append(f"# 同花顺我的股票集合竞价后盘前更新｜{REPORT_DATE} 09:25\n")
    lines.append(f"- 生成时间：{now}（Asia/Shanghai）")
    lines.append("- 说明：基于集合竞价结束后的A股竞价价量、同花顺早盘/复盘/异动主题映射、板块共振和全市场候选扫描；仅为交易计划参考，不构成投资建议。")
    lines.append("- 口径：吸收同花顺智能选股“集合竞价”思路，重点看高低开幅度、竞价成交额、板块共振、题材辨识度和开盘后承接；竞价强不等于直接买，仍需9:30后量价确认。")
    lines.append("")
    lines.append("## 一、集合竞价情绪")
    lines.append(f"- {market_emotion(indexes, boards, opportunities)}。")
    if theme_keys:
        lines.append("- 今日竞价主题关键词：" + "、".join(theme_keys[:18]))
    lines.append("- 交易含义：9:25 只重排优先级，9:30 后仍看开盘价、VWAP、OR5/OR15 和分钟量能确认。")
    lines.append("")
    lines.append("## 二、全市场集合竞价机会池")
    lines.append("- 用途：当持仓票弱于盘面时，从竞价已经出现板块共振和辨识度的股票里找替代观察。")
    lines.append("- 触发：只在开盘后站稳触发价、回踩开盘价/VWAP不破、分钟量能继续承接时进入模拟盘；急拉远离触发价不追。")
    if opportunities:
        lines.append("| 机会代码 | 机会名称 | 雷达状态 | 现价 | 涨跌 | VWAP | 触发价 | 失效价 | 成交额 | 三轴 | 框架依据 | 动作 |")
        lines.append("|---|---|---|---:|---:|---|---:|---:|---:|---|---|---|")
        lines.extend(opportunity_table_lines(opportunities))
    else:
        lines.append("- 暂无满足竞价框架门槛的全市场替代机会。")
    lines.append("")
    lines.append("## 三、自选股集合竞价更新")
    lines.append("| 代码 | 名称 | 状态 | 优先级 | 竞价价 | 高低开 | 竞价成交额 | 防守 | 修复 | 压力 | 操作建议 |")
    lines.append("|---|---|---|---|---:|---:|---:|---:|---:|---:|---|")
    for code, row in levels.items():
        quote = quotes.get(code)
        if not quote:
            continue
        state, priority, action = auction_signal_for_watch(row, quote)
        lines.append(
            f"| {code} | {quote.get('name') or row.get('name') or code} | {state} | {priority} | {f2(quote.get('close'))} | "
            f"{pct(quote_gap(quote))} | {amount_yi(quote):.2f}亿 | {f2(row.get('defense'))} | {f2(row.get('repair'))} | {f2(row.get('pressure'))} | {action} |"
        )
    lines.append("")
    lines.append("## 四、09:30-09:35 执行清单")
    lines.append("- P0：低开弱承接或跌破防守位的持仓，先处理风险，不补仓摊平。")
    lines.append("- P1：温和高开且竞价有量的票，等开盘后站稳开盘价/VWAP/修复位再执行，冲高回落先不动。")
    lines.append("- 全市场候选：只做替代观察和模拟盘跟踪；必须等自身触发价、VWAP和分钟量能确认。")
    lines.append("- 若指数高开低走或强板块开盘即分化，降低所有进攻信号一级。")
    return "\n".join(lines)


def lark_text(text):
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def send_feishu(levels, quotes, indexes, boards, opportunities, theme_keys):
    if os.environ.get("A_SHARE_SKIP_FEISHU") == "1":
        return json.dumps({"code": 0, "msg": "skipped by A_SHARE_SKIP_FEISHU"}, ensure_ascii=False)
    p0_lines = []
    p1_lines = []
    for code, row in levels.items():
        quote = quotes.get(code)
        if not quote:
            continue
        state, priority, action = auction_signal_for_watch(row, quote)
        line = f"**{code} {quote.get('name') or row.get('name') or code}**｜{state}｜高低开 {pct(quote_gap(quote))}｜竞价额 {amount_yi(quote):.2f}亿｜{action}"
        if priority == "P0":
            p0_lines.append(line)
        else:
            p1_lines.append(line)
    opp_lines = [
        f"{row['status']} **{row['code']} {row['name']}**｜{row['focus']}｜触发 {f2(row['trigger_price'])}｜失效 {f2(row['invalid_price'])}"
        for row in opportunities[:6]
    ] or ["暂无满足竞价框架门槛的全市场替代机会。"]
    board_line = "、".join(f"{x.get('f14')} {pct(x.get('f3'))}" for x in boards[:6]) if boards else "板块数据暂缺"
    index_line = "、".join(f"{x.get('name')} {pct(x.get('pct'))}" for x in indexes[:3]) if indexes else "指数数据暂缺"
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": "orange", "title": {"tag": "plain_text", "content": f"A股集合竞价后盘前更新｜{REPORT_DATE} 09:25"}},
            "elements": [
                lark_text(f"**竞价情绪**\n指数：{index_line}\n板块：{board_line}\n主题：{'、'.join(theme_keys[:10]) or '暂无明确主题'}"),
                {"tag": "hr"},
                lark_text("**P0 风险优先**\n" + "\n".join(p0_lines[:6] or ["暂无竞价P0。"])),
                lark_text("**P1 重点观察**\n" + "\n".join(p1_lines[:8] or ["暂无竞价P1。"])),
                {"tag": "hr"},
                lark_text("**全市场竞价候选**\n" + "\n".join(opp_lines)),
                {"tag": "hr"},
                lark_text("9:30 后只看开盘价/VWAP/OR5 承接；竞价强不直接买，急拉不追。仅为交易计划参考，不构成投资建议。"),
            ],
        },
    }
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
                return text
            raise FeishuPushError(text)
        except Exception as exc:
            last_error = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"飞书推送失败：{last_error}")


def main():
    raw = load_premarket_raw()
    levels = load_premarket_levels()
    if not levels:
        raise RuntimeError("未找到08:30盘前关键位，无法生成集合竞价更新")
    universe = [(code, base.infer_market(code)) for code in levels]
    quotes = base.parse_tencent_quotes(universe)
    indexes = base.fetch_indexes()
    boards_top, _ = base.fetch_boards()
    opportunities, theme_keys = fetch_auction_candidates(set(levels), boards_top, raw)
    base_risk_context = global_risk.read_context(BASE_DIR, REPORT_DATE)
    global_risk_context = global_risk.infer_auction_context(
        base_risk_context,
        REPORT_DATE,
        quotes,
        indexes=indexes,
        opportunities=opportunities,
        now=base.current_datetime(),
    )
    try:
        import premarket_report

        path_state = global_risk.record_intraday_path(
            BASE_DIR,
            REPORT_DATE,
            premarket_report.fetch_global_markets(),
            now=base.current_datetime(),
        )
        global_risk_context["intraday_path"] = path_state
        global_risk_context["repair_mode"] = (
            "preopen_repair_continuation_pending"
            if path_state.get("preopen_repair") or path_state.get("recovery_confirmed")
            else "normal"
        )
    except Exception as exc:
        global_risk_context["path_error"] = str(exc)
    global_risk.write_context(BASE_DIR, global_risk_context)
    tracking_path = write_candidate_tracking(opportunities, indexes, boards_top, theme_keys)
    report = make_report(levels, quotes, indexes, boards_top, opportunities, theme_keys)
    REPORT_PATH.write_text(report, encoding="utf-8")
    try:
        dashboard_result = dashboard.publish_report(REPORT_PATH)
    except Exception as exc:
        dashboard_result = {"error": str(exc)}
    feishu_result = send_feishu(levels, quotes, indexes, boards_top, opportunities, theme_keys)
    print(json.dumps({
        "report": str(REPORT_PATH),
        "dashboard": dashboard_result,
        "stocks": len(levels),
        "market_opportunities": [row["code"] for row in opportunities],
        "theme_keys": theme_keys[:16],
        "tracking": str(tracking_path),
        "global_risk": global_risk_context,
        "feishu_result": feishu_result,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
