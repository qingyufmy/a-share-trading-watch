#!/usr/bin/env python3
import argparse
import html
import json
import os
import re
from datetime import datetime
from pathlib import Path

from core import mira_research_gate
from core import global_risk
from core import theme_validation
import paper_trading


BASE_DIR = Path(os.environ.get("A_SHARE_BASE_DIR", Path(__file__).resolve().parent))
WEB_DIR = Path(os.environ.get("A_SHARE_WEB_DIR", BASE_DIR / "web_dashboard"))
DATA_DIR = WEB_DIR / "data"
REPORT_DIR = DATA_DIR / "reports"
INDEX_PATH = DATA_DIR / "index.json"


def strip_md(text):
    text = re.sub(r"`([^`]*)`", r"\1", text or "")
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text.strip()


def split_table_row(line):
    return [strip_md(x.strip()) for x in line.strip().strip("|").split("|")]


def is_separator(line):
    cells = [x.strip() for x in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def parse_markdown_tables(lines):
    tables = []
    i = 0
    while i < len(lines):
        if not lines[i].lstrip().startswith("|"):
            i += 1
            continue
        start = i
        table_lines = []
        while i < len(lines) and lines[i].lstrip().startswith("|"):
            table_lines.append(lines[i])
            i += 1
        if len(table_lines) < 2 or not is_separator(table_lines[1]):
            continue
        headers = split_table_row(table_lines[0])
        rows = []
        for raw in table_lines[2:]:
            cells = split_table_row(raw)
            if len(cells) < len(headers):
                cells.extend([""] * (len(headers) - len(cells)))
            rows.append(dict(zip(headers, cells[:len(headers)])))
        section, heading = nearest_heading_context(lines, start)
        tables.append({"start": start, "section": section, "title": heading, "headers": headers, "rows": rows})
    return tables


def nearest_heading_context(lines, index):
    section = ""
    heading = ""
    for j in range(index - 1, -1, -1):
        match = re.match(r"^(#{2,4})\s+(.+?)\s*$", lines[j])
        if not match:
            continue
        title = strip_md(match.group(2))
        if not heading:
            heading = title
        if match.group(1) == "##":
            section = title
            break
    return section, heading or section


def parse_sections(lines):
    sections = []
    current = None
    for line in lines:
        match = re.match(r"^(#{2,3})\s+(.+?)\s*$", line)
        if match and match.group(1) == "##":
            if current:
                sections.append(current)
            current = {"title": strip_md(match.group(2)), "lines": []}
        elif current:
            current["lines"].append(line)
    if current:
        sections.append(current)
    return sections


def markdown_fragment_to_html(lines):
    out = []
    in_ul = False
    table_buffer = []

    def flush_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    def flush_table():
        nonlocal table_buffer
        if len(table_buffer) >= 2 and is_separator(table_buffer[1]):
            headers = split_table_row(table_buffer[0])
            rows = [split_table_row(row) for row in table_buffer[2:]]
            out.append('<div class="mini-table-wrap"><table class="mini-table"><thead><tr>')
            out.extend(f"<th>{html.escape(h)}</th>" for h in headers)
            out.append("</tr></thead><tbody>")
            for row in rows:
                out.append("<tr>")
                for cell in row[:len(headers)]:
                    out.append(f"<td>{inline_md(cell)}</td>")
                out.append("</tr>")
            out.append("</tbody></table></div>")
        elif table_buffer:
            out.extend(f"<p>{inline_md(row)}</p>" for row in table_buffer)
        table_buffer = []

    for raw in lines:
        line = raw.rstrip()
        if line.lstrip().startswith("|"):
            flush_ul()
            table_buffer.append(line)
            continue
        flush_table()
        if not line.strip():
            flush_ul()
            continue
        heading = re.match(r"^(#{3,4})\s+(.+?)\s*$", line)
        if heading:
            flush_ul()
            tag = "h3" if heading.group(1) == "###" else "h4"
            out.append(f"<{tag}>{inline_md(heading.group(2))}</{tag}>")
            continue
        bullet = re.match(r"^\s*-\s+(.+)$", line)
        if bullet:
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline_md(bullet.group(1))}</li>")
            continue
        flush_ul()
        out.append(f"<p>{inline_md(line)}</p>")
    flush_table()
    flush_ul()
    return "\n".join(out)


def inline_md(text):
    escaped = html.escape(text or "")
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" target="_blank" rel="noreferrer">\1</a>', escaped)
    return escaped


def parse_meta(lines):
    meta = []
    for line in lines[1:]:
        if line.startswith("## "):
            break
        match = re.match(r"^-\s*([^：:]+)[：:]\s*(.+)$", line.strip())
        if match:
            meta.append({"label": strip_md(match.group(1)), "value": strip_md(match.group(2))})
    return meta


def priority_of(row):
    joined = " ".join(str(v) for v in row.values())
    for key in ("P0", "P1", "P2"):
        if re.search(rf"\b{key}\b", joined):
            return key
    if any(icon in joined for icon in ("🔴", "🟥")) or "风险" in joined or "破位" in joined:
        return "P0"
    if any(icon in joined for icon in ("🟡", "🟨")) or "观察" in joined or "修复" in joined:
        return "P1"
    return "P2"


def status_color(row):
    joined = " ".join(str(v) for v in row.values())
    if any(icon in joined for icon in ("🔴", "🟥")) or priority_of(row) == "P0":
        return "red"
    if any(icon in joined for icon in ("🟡", "🟨")) or priority_of(row) == "P1":
        return "yellow"
    if any(icon in joined for icon in ("🟢", "🟩")):
        return "green"
    return "gray"


def parse_float(value):
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def format_pct_value(value):
    if value is None:
        return ""
    return f"{value:+.2f}%"


def side_label(value):
    text = str(value or "").upper()
    if text == "BUY" or "买" in text:
        return "买入"
    if text == "SELL" or "卖" in text or "减" in text:
        return "卖出"
    if text == "CANCEL" or "取消" in text:
        return "取消"
    return str(value or "")


def parse_price_path(value):
    parts = re.split(r"\s*/\s*", str(value or ""))
    prices = []
    for part in parts[:4]:
        prices.append(parse_float(part))
    while len(prices) < 4:
        prices.append(None)
    return dict(zip(("1m", "3m", "5m", "15m"), prices))


def paper_trade_tone(row):
    quality = str(row.get("交易质量") or row.get("trade_quality") or "")
    if any(word in quality for word in ("有效", "优秀", "正向")):
        return "green"
    if any(word in quality for word in ("失败", "偏早", "打脸", "反噬", "无效")):
        return "red"
    pnl15 = row.get("_pnl15m")
    if pnl15 is not None:
        if pnl15 >= 0.2:
            return "green"
        if pnl15 <= -0.2:
            return "red"
    return "yellow"


def enrich_paper_trade(row):
    out = dict(row)
    side = side_label(out.get("方向") or out.get("side"))
    fill_price = parse_float(out.get("成交价") or out.get("fill_price"))
    path = parse_price_path(out.get("1/3/5/15分钟价") or out.get("price_path"))
    out["_side_label"] = side
    out["_fill_price"] = fill_price
    out["_path"] = path
    multiplier = -1 if side == "卖出" else 1
    if side == "取消":
        multiplier = 0
    for key, price in path.items():
        pnl = None
        if fill_price and price is not None and multiplier:
            pnl = (price - fill_price) / fill_price * 100 * multiplier
        out[f"_pnl{key}"] = pnl
        out[f"_pnl{key}_text"] = format_pct_value(pnl)
    out["_tone"] = paper_trade_tone(out)
    out["_search"] = " ".join(str(v) for v in out.values())
    return out


def extract_paper_trades(tables):
    rows = []
    for table in tables:
        headers = set(table.get("headers") or [])
        if {"方向", "成交价", "交易质量"}.issubset(headers):
            rows.extend(enrich_paper_trade(row) for row in table.get("rows") or [])
    return rows


def paper_trade_counts(rows):
    counts = {"total": len(rows), "buy": 0, "sell": 0, "cancel": 0, "green": 0, "yellow": 0, "red": 0}
    pnl15_values = []
    for row in rows:
        side = row.get("_side_label")
        if side == "买入":
            counts["buy"] += 1
        elif side == "卖出":
            counts["sell"] += 1
        elif side == "取消":
            counts["cancel"] += 1
        tone = row.get("_tone") or "yellow"
        counts[tone] = counts.get(tone, 0) + 1
        if row.get("_pnl15m") is not None:
            pnl15_values.append(row["_pnl15m"])
    counts["avg_pnl15"] = round(sum(pnl15_values) / len(pnl15_values), 2) if pnl15_values else None
    return counts


def paper_trade_from_order(order):
    status = order.get("status")
    if status not in ("FILLED", "PARTIAL_FILLED"):
        return None
    side = side_label(order.get("side"))
    tone = "green" if side == "卖出" else "red" if side == "买入" else "yellow"
    path_values = paper_trading.fill_price_path(
        BASE_DIR,
        order.get("trading_date"),
        order.get("symbol"),
        order.get("created_at"),
    )
    path = {
        "1m": path_values.get("60"),
        "3m": path_values.get("180"),
        "5m": path_values.get("300"),
        "15m": path_values.get("900"),
    }
    path_text = " / ".join(f"{value:.2f}" if isinstance(value, (int, float)) else "-" for value in path.values())
    out = {
        "时间": str(order.get("created_at") or "")[-8:] or order.get("created_at") or "-",
        "代码": order.get("symbol") or "",
        "名称": order.get("name") or "",
        "方向": side,
        "数量": order.get("qty"),
        "成交价": order.get("fill_price"),
        "场景": order.get("scenario") or "",
        "交易质量": "成交待盘后复盘",
        "复盘结论": order.get("_discipline_summary") or order.get("reason") or "",
        "1/3/5/15分钟价": path_text,
        "_side_label": side,
        "_fill_price": parse_float(order.get("fill_price")),
        "_path": {},
        "_pnl5m": None,
        "_pnl5m_text": "",
        "_pnl15m": None,
        "_pnl15m_text": "",
        "_tone": tone,
    }
    out = enrich_paper_trade(out)
    pnl15 = out.get("_pnl15m")
    if pnl15 is not None:
        if pnl15 <= -1.5:
            out["交易质量"] = "成交后15分钟明显反噬"
        elif pnl15 <= -0.6:
            out["交易质量"] = "成交后短线承压"
        elif pnl15 >= 0.6:
            out["交易质量"] = "成交后短线延续"
        else:
            out["交易质量"] = "成交后短线震荡"
        out["复盘结论"] = (
            f"成交后1/3/5/15分钟价格 {path_text}；"
            f"15分钟相对成交价 {pnl15:+.2f}%；"
            f"{out.get('复盘结论') or '需结合后续量价确认'}"
        )
    out["_tone"] = paper_trade_tone(out)
    out["_search"] = " ".join(str(v) for v in out.values())
    return out


def paper_trades_from_orders(orders):
    rows = []
    for order in orders or []:
        row = paper_trade_from_order(order)
        if row:
            rows.append(row)
    return rows


def market_opportunity_tone(row):
    gate = str(row.get("模拟门控") or row.get("market_opportunity_gate") or "")
    if gate and gate != "可模拟":
        return "yellow"
    text = " ".join(str(row.get(k) or "") for k in ("雷达状态", "三轴", "动作"))
    if any(word in text for word in ("🟢", "替代机会", "位置🟢")):
        return "green"
    if any(word in text for word in ("🔴", "暂不介入", "位置🔴")):
        return "red"
    return "yellow"


def enrich_market_opportunity(row):
    out = dict(row)
    gate = str(out.get("模拟门控") or "").strip()
    out["_radar_gate_ok"] = gate == "可模拟"
    out["_radar_gate_reason"] = "" if gate == "可模拟" else gate
    out["_tone"] = market_opportunity_tone(out)
    out["_search"] = " ".join(str(v) for v in out.values())
    out["_trigger"] = parse_float(out.get("触发价"))
    out["_invalid"] = parse_float(out.get("失效价"))
    out["_current"] = parse_float(out.get("现价"))
    focus = str(out.get("框架依据") or out.get("focus") or "")
    out["focus"] = focus
    out["framework_validation"] = theme_validation.framework_evidence_from_text(focus)
    board_text = str(out.get("板块情绪") or "")
    board_match = re.search(r"([+-]?\d+(?:\.\d+)?)%", board_text)
    board_pct = float(board_match.group(1)) if board_match else None
    volume_text = str(out.get("板块/个股量能") or "")
    volume_parts = re.findall(r"\d+(?:\.\d+)?", volume_text)
    amount_1m = float(volume_parts[0]) if volume_parts else None
    amount_5m = float(volume_parts[1]) if len(volume_parts) > 1 else None
    out["sector_momentum"] = {
        "board_name": board_text.split()[0] if board_text else "",
        "board_pct": board_pct,
        "emotion_ok": bool(board_pct is not None and board_pct >= 0.8),
        "reason": board_text,
    }
    out["rt_features"] = {"amount_ratio_1m": amount_1m, "amount_ratio_5m": amount_5m}
    out["risk_bucket"] = global_risk.candidate_risk_bucket(out)
    return out


def compact_market_gate_action(action, technical_reason):
    """Keep one technical blocker and the useful next confirmation step."""
    text = str(action or "").strip()
    reason = str(technical_reason or "技术门控未通过").strip()
    raw_reason = reason
    prefix = "技术门控未通过："
    if reason.startswith(prefix):
        reason = reason[len(prefix):].strip() or "等待量价/形态确认"
    if text.startswith(prefix):
        text = text[len(prefix):].lstrip()
        repeated = raw_reason if text.startswith(raw_reason) else reason
        if text.startswith(repeated):
            text = text[len(repeated):].lstrip("；")
    if reason in {"技术门控未通过", "等待确认：技术门控未通过"}:
        reason = "等待量价/形态确认"
    candidate_prefix = "候选观察，暂不模拟成交："
    if text.startswith(candidate_prefix):
        text = text[len(candidate_prefix):]
        if "。" in text:
            text = text.split("。", 1)[1].strip()
    return f"技术门控未通过：{reason}；{text}" if text else f"技术门控未通过：{reason}"


def apply_market_opportunity_gate(rows, global_context):
    """Overlay the execution gate so technical strength cannot imply approval."""
    for row in rows or []:
        framework_validation = row.get("framework_validation") or {}
        if framework_validation.get("executable") is False:
            is_conflict = framework_validation.get("valid") is False
            label = "框架依据冲突" if is_conflict else "框架证据不足"
            reason = label + "：" + str(framework_validation.get("reason") or "主题归属未完成核验")
            row["_tone"] = "red"
            row["原框架依据"] = row.get("框架依据") or row.get("focus") or ""
            row["框架依据"] = f"{label}（不参与候选）"
            row["模拟门控"] = "只观察：" + reason
            row["雷达状态"] = f"🔴 {label}"
            row["动作"] = f"{reason}；禁止进入模拟盘，需以行业/名称与当日板块共振重新核验。"
            continue
        allowed, stage, reason = global_risk.market_opportunity_gate_status(global_context, row=row)
        if allowed and stage == "SECTOR_ROTATION_ALLOWED":
            row["模拟门控"] = reason
            row["雷达状态"] = "🟡 板块轮动确认"
            row["动作"] = f"{reason}；仍须通过VWAP、三周期与盈亏比复核后才进入模拟盘。"
            continue
        if reason:
            row["_tone"] = "yellow"
            row["模拟门控"] = reason
            row["雷达状态"] = f"🔴 {reason}"
        elif row.get("_radar_gate_ok") is False:
            technical_reason = row.get("_radar_gate_reason") or "技术门控未通过"
            row["_tone"] = "yellow"
            row["雷达状态"] = "🟡 等待确认"
            row["模拟门控"] = f"等待确认：{technical_reason}"
            original = str(row.get("动作") or row.get("建议") or "等待价格、量能和形态确认后再评估。")
            row["动作"] = compact_market_gate_action(original, technical_reason)
            continue
        else:
            continue
        original = str(row.get("动作") or row.get("建议") or "只作为替代机会观察，确认后才进入模拟盘。")
        original = original.replace("可小仓试错", "门控解除后再评估").replace(
            "进入模拟盘", "门控解除并再次确认后进入模拟盘"
        )
        row["动作"] = original if original.startswith(reason) else f"{reason}；{original}"
    return rows


def extract_market_opportunities(tables):
    rows = []
    for table in tables:
        headers = set(table.get("headers") or [])
        if {"机会代码", "机会名称", "雷达状态", "触发价", "失效价"}.issubset(headers):
            rows.extend(enrich_market_opportunity(row) for row in table.get("rows") or [])
    return rows


def enrich_paper_position(row):
    out = dict(row)
    out["_qty"] = parse_float(out.get("持仓"))
    out["_sellable"] = parse_float(out.get("可卖"))
    out["_avg_cost"] = parse_float(out.get("成本"))
    out["_last_price"] = parse_float(out.get("现价"))
    out["_market_value"] = parse_float(out.get("市值"))
    out["_day_pnl"] = parse_float(out.get("当日盈亏"))
    out["_day_pnl_pct"] = parse_float(out.get("当日收益率"))
    out["_unrealized_pnl"] = parse_float(out.get("浮盈亏"))
    out["_unrealized_pnl_pct"] = parse_float(out.get("收益率"))
    out["_holding_days"] = parse_float(out.get("持仓天数"))
    pnl = out["_unrealized_pnl"]
    out["_tone"] = "red" if pnl and pnl > 0 else "green" if pnl and pnl < 0 else "yellow"
    out["_search"] = " ".join(str(v) for v in out.values())
    return out


def extract_paper_positions(tables):
    rows = []
    for table in tables:
        headers = set(table.get("headers") or [])
        if {"代码", "名称", "持仓", "可卖", "成本", "现价", "浮盈亏", "收益率"}.issubset(headers):
            for row in table.get("rows") or []:
                enriched = enrich_paper_position(row)
                if (enriched.get("_qty") or 0) <= 0:
                    continue
                rows.append(enriched)
    return rows


def load_runtime_paper_payload(report_date):
    path = DATA_DIR / "runtime" / "paper_trading.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if report_date and data.get("trading_date") and data.get("trading_date") != report_date:
        return {}
    return data


def load_runtime_paper_account(report_date):
    data = load_runtime_paper_payload(report_date)
    return data.get("account") or {}


def paper_order_tone(order):
    status = str(order.get("status") or "")
    side = str(order.get("side") or "").upper()
    if status in ("FILLED", "PARTIAL_FILLED"):
        return "red" if side == "BUY" else "green" if side == "SELL" else "yellow"
    if status == "REJECTED":
        return "yellow"
    if status == "CANCELLED":
        return "yellow"
    return "gray"


def enrich_paper_order(order):
    out = dict(order)
    out["_side_label"] = side_label(out.get("side"))
    out["_tone"] = paper_order_tone(out)
    discipline = out.get("discipline_check") or {}
    if isinstance(discipline, str):
        try:
            discipline = json.loads(discipline)
        except Exception:
            discipline = {}
    rationale = out.get("strategy_rationale") or {}
    if isinstance(rationale, str):
        try:
            rationale = json.loads(rationale)
        except Exception:
            rationale = {}
    out["_discipline_summary"] = discipline.get("summary") or out.get("reason") or "-"
    out["_failed_checks"] = "、".join(discipline.get("failed_checks") or [])
    out["_basis"] = "；".join((rationale.get("action_basis") or [])[:4])
    out["_search"] = " ".join(str(v) for v in out.values())
    return out


def load_runtime_paper_orders(report_date):
    data = load_runtime_paper_payload(report_date)
    orders = data.get("orders") or []
    return [enrich_paper_order(order) for order in orders]


def runtime_trading_date_for_report(kind, title, text, report_date):
    if kind == "afterclose":
        match = re.search(r"基于\s*(20\d{2}-\d{2}-\d{2})\s*收盘", title + "\n" + text)
        if match:
            return match.group(1)
    return report_date


def load_realtime_health(report_date=None):
    """Read the live engine status without treating a stale snapshot as current."""
    runtime_dir = DATA_DIR / "runtime"
    health = {}
    latest = {}
    try:
        health = json.loads((runtime_dir / "signal_health.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    try:
        latest = json.loads((runtime_dir / "latest_signals.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass

    merged = dict((latest or {}).get("health") or {})
    merged.update(health or {})
    last_success_at = merged.get("last_success_at") or (latest or {}).get("updated_at")
    last_success_date = str(merged.get("last_success_trading_date") or last_success_at or "")[:10]
    report_date = str(report_date or "")
    engine_status = merged.get("engine_status") or "unknown"
    state = "ok"
    if engine_status == "engine_error":
        state = "engine_error"
    elif not last_success_at:
        state = "missing"
    elif report_date and last_success_date != report_date:
        state = "stale"
    elif engine_status in {"closed", "idle"}:
        state = "closed"
    merged.update({
        "last_success_at": last_success_at,
        "last_success_trading_date": last_success_date,
        "status_state": state,
    })
    return merged


def paper_order_counts(orders):
    counts = {"total": len(orders), "filled": 0, "rejected": 0, "cancelled": 0, "buy": 0, "sell": 0}
    for order in orders:
        status = order.get("status")
        side = str(order.get("side") or "").upper()
        if status in ("FILLED", "PARTIAL_FILLED"):
            counts["filled"] += 1
        elif status == "REJECTED":
            counts["rejected"] += 1
        elif status == "CANCELLED":
            counts["cancelled"] += 1
        if side == "BUY":
            counts["buy"] += 1
        elif side == "SELL":
            counts["sell"] += 1
    return counts


PAPER_ACCOUNT_LABELS = {
    "初始资金": "initial_cash",
    "总资产": "total_assets",
    "可用现金": "cash",
    "持仓市值": "market_value",
    "仓位": "position_pct",
    "浮盈亏": "unrealized_pnl",
    "持仓收益率": "unrealized_pnl_pct",
}


def parse_paper_account_from_lines(lines):
    for raw in lines:
        text = strip_md(raw)
        if "账户" not in text or "总资产" not in text:
            continue
        account = {}
        parts = re.split(r"[；;｜|]", text)
        for part in parts:
            for label, key in PAPER_ACCOUNT_LABELS.items():
                if label not in part:
                    continue
                value = parse_float(part)
                if value is not None:
                    account[key] = value
            count_match = re.search(r"(?:持仓|持仓数)\s*(\d+)\s*只", part)
            if count_match and "持仓市值" not in part:
                account["position_count"] = int(count_match.group(1))
        if account:
            if account.get("total_assets") is not None:
                account.setdefault("total_amount", account["total_assets"])
            if account.get("total_assets") and account.get("market_value") is not None and account.get("position_pct") is None:
                account["position_pct"] = account["market_value"] / account["total_assets"] * 100
            if account.get("initial_cash") is not None and account.get("total_assets") is not None:
                account.setdefault("total_pnl", account["total_assets"] - account["initial_cash"])
                if account["initial_cash"]:
                    account.setdefault("total_pnl_pct", (account["total_assets"] - account["initial_cash"]) / account["initial_cash"] * 100)
            return account
    return {}


def paper_position_counts(rows, account=None):
    account = account or {}
    total_value = sum((row.get("_market_value") or 0) for row in rows)
    day_pnl_values = [row.get("_day_pnl") for row in rows if row.get("_day_pnl") is not None]
    total_day_pnl = sum(day_pnl_values)
    total_pnl = sum((row.get("_unrealized_pnl") or 0) for row in rows)
    total_cost = total_value - total_pnl
    total_assets = account.get("total_assets", account.get("total_amount"))
    cash = account.get("cash")
    position_pct = account.get("position_pct")
    if total_assets is not None and position_pct is None:
        position_pct = total_value / total_assets * 100 if total_assets else None
    if total_assets is None and cash is not None:
        total_assets = cash + total_value
    if cash is None and total_assets is not None:
        cash = total_assets - total_value
    account_day_pnl = account.get("day_pnl")
    position_day_pnl = account.get("position_day_pnl")
    position_day_pnl_pct = account.get("position_day_pnl_pct")
    if position_day_pnl is None and day_pnl_values:
        position_day_pnl = total_day_pnl
    if position_day_pnl_pct is None and day_pnl_values and (total_value - total_day_pnl):
        position_day_pnl_pct = total_day_pnl / (total_value - total_day_pnl) * 100
    # Missing legacy fields may use position totals; explicit nulls mean unknown.
    market_value = account.get("market_value", total_value)
    unrealized_pnl = account.get("unrealized_pnl", total_pnl)
    unrealized_pnl_pct = account.get(
        "unrealized_pnl_pct", total_pnl / total_cost * 100 if total_cost else None
    )
    day_pnl = account.get("day_pnl", total_day_pnl if day_pnl_values else None)
    day_pnl_pct = account.get(
        "day_pnl_pct",
        total_day_pnl / (total_value - total_day_pnl) * 100
        if day_pnl_values and (total_value - total_day_pnl) else None,
    )
    return {
        "total": len(rows),
        "initial_cash": account.get("initial_cash"),
        "cash": round(cash, 2) if cash is not None else None,
        "total_amount": round(total_assets, 2) if total_assets is not None else round(total_value, 2),
        "total_assets": round(total_assets, 2) if total_assets is not None else round(total_value, 2),
        "market_value": round(market_value, 2) if market_value is not None else None,
        "position_pct": round(position_pct, 2) if position_pct is not None else None,
        "day_pnl": round(day_pnl, 2) if day_pnl is not None else None,
        "day_pnl_pct": round(day_pnl_pct, 2) if day_pnl_pct is not None else None,
        "day_pnl_source": account.get("day_pnl_source") or ("account_total_assets" if account_day_pnl is not None else "open_positions"),
        "previous_total_assets": round(account.get("previous_total_assets"), 2) if account.get("previous_total_assets") is not None else None,
        "previous_snapshot_at": account.get("previous_snapshot_at"),
        "previous_trading_date": account.get("previous_trading_date"),
        "position_day_pnl": round(position_day_pnl, 2) if position_day_pnl is not None else None,
        "position_day_pnl_pct": round(position_day_pnl_pct, 2) if position_day_pnl_pct is not None else None,
        "total_pnl": round(account.get("total_pnl"), 2) if account.get("total_pnl") is not None else None,
        "total_pnl_pct": round(account.get("total_pnl_pct"), 2) if account.get("total_pnl_pct") is not None else None,
        "unrealized_pnl": round(unrealized_pnl, 2) if unrealized_pnl is not None else None,
        "unrealized_pnl_pct": round(unrealized_pnl_pct, 2) if unrealized_pnl_pct is not None else None,
    }


SIGNAL_REVIEW_HEADERS = {"时间", "类型", "场景", "走势判定", "合理性", "迭代动作", "1/3/5/15分钟价"}
PAPER_TRADE_HEADERS = {"方向", "成交价", "交易质量"}
MARKET_OPPORTUNITY_HEADERS = {"机会代码", "机会名称", "雷达状态", "触发价", "失效价"}
PAPER_POSITION_HEADERS = {"持仓", "可卖", "成本", "现价", "浮盈亏", "收益率"}
OPERATION_TABLE_HINTS = {
    "状态", "优先级", "操作指令", "现价", "收盘", "涨跌", "涨跌幅", "开盘",
    "支撑", "支撑（回踩观察）", "防守", "防守/止损", "防守/止损（硬失效）", "压力", "加仓价", "加仓触发", "减仓价",
    "VWAP", "日内强弱线", "风险减仓价", "第一反抽减仓价", "反抽减仓价",
    "条件加仓价", "失效价", "趋势防守", "趋势修复", "趋势压力",
    "修复/加仓条件", "加仓模式", "加仓确认", "加仓取消", "压力/减仓", "盘前建议", "建议", "社区温度",
}


def operation_table_score(table):
    headers = set(table.get("headers") or [])
    if not {"代码", "名称"}.issubset(headers):
        return -1
    if SIGNAL_REVIEW_HEADERS.issubset(headers):
        return -1
    if PAPER_TRADE_HEADERS.issubset(headers):
        return -1
    if MARKET_OPPORTUNITY_HEADERS.issubset(headers):
        return -1
    if PAPER_POSITION_HEADERS.issubset(headers):
        return -1

    context = f"{table.get('section') or ''} {table.get('title') or ''}"
    if "实时信号质量复盘" in context:
        return -1

    hint_count = len(headers & OPERATION_TABLE_HINTS)
    score = hint_count * 3
    if "订盘总览" in context or "核心操作" in context or "操作表" in context:
        score += 50
    if "状态" in headers:
        score += 6
    if "优先级" in headers or "操作指令" in headers:
        score += 6
    if any(h in headers for h in ("支撑", "支撑（回踩观察）", "防守/止损", "防守/止损（硬失效）", "VWAP", "风险减仓价", "条件加仓价", "加仓触发")):
        score += 8
    if "时间" in headers:
        score -= 8
    return score if score >= 18 else -1


def enrich_stock_row(row):
    out = dict(row)
    aliases = {
        "现价": ("当前价", "收盘"),
        "涨跌": ("涨跌幅",),
        "支撑": ("支撑（回踩观察）",),
        "防守": ("防守/止损", "防守/止损（硬失效）", "趋势防守"),
        "条件加仓价": ("加仓触发", "加仓价", "修复/加仓条件", "趋势修复"),
        "风险减仓价": ("减仓价", "防守/止损", "防守/止损（硬失效）"),
        "压力/减仓": ("减仓价", "压力", "趋势压力"),
        "失效价": ("防守/止损", "防守/止损（硬失效）", "趋势防守"),
    }
    for target, sources in aliases.items():
        if out.get(target):
            continue
        for source in sources:
            if out.get(source):
                out[target] = out[source]
                break
    return out


def first_text(row, keys):
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def is_blank_price(value):
    text = str(value or "").strip()
    if not text or text in {"-", "—", "暂无", "无", "None", "null"}:
        return True
    return parse_float(text) is None


def pct_distance(current, trigger):
    if current is None or trigger is None or current <= 0:
        return ""
    return f"{(trigger - current) / current * 100:+.2f}%"


def entry_alert_from_stock_row(row):
    priority = row.get("_priority") or priority_of(row)
    if priority == "P0":
        return None
    joined = " ".join(str(v) for v in row.values())
    if "不加仓" in joined and "站回" not in joined and "修复" not in joined:
        return None
    trigger_text = first_text(row, ["加仓触发", "条件加仓价", "加仓价", "修复/加仓条件", "修复", "趋势修复"])
    if is_blank_price(trigger_text):
        return None
    current_text = first_text(row, ["现价", "当前价", "收盘"])
    invalid_text = first_text(row, ["失效价", "防守/止损", "防守/止损（硬失效）", "防守", "趋势防守"])
    pressure_text = first_text(row, ["压力/减仓", "压力", "趋势压力", "第一反抽减仓价", "反抽减仓价"])
    support_text = first_text(row, ["VWAP", "日内强弱线", "支撑", "支撑（回踩观察）", "防守"])
    trigger = parse_float(trigger_text)
    current = parse_float(current_text)
    command = first_text(row, ["操作指令", "状态", "优先级"])
    action = first_text(row, ["建议", "盘前建议", "操作建议"])
    mode = first_text(row, ["加仓模式"])
    tone = "green" if mode in {"回踩承接", "突破修复"} and "不追加" not in joined else "yellow"
    label = "重点入场/加仓" if tone == "green" else "接近入场观察"
    if "候选" in joined or "机会" in joined:
        label = "候选入场观察"
    return {
        "code": row.get("代码") or row.get("机会代码") or "",
        "name": row.get("名称") or row.get("机会名称") or "",
        "priority": priority,
        "tone": tone,
        "label": label,
        "status": command,
        "current_price": current_text,
        "trigger_price": trigger_text,
        "invalid_price": invalid_text,
        "support_line": support_text,
        "pressure_price": pressure_text,
        "distance": pct_distance(current, trigger),
        "confirm": first_text(row, ["加仓确认"]) or "站稳触发价 + VWAP/日内强弱线承接 + 量能确认",
        "action": action or first_text(row, ["加仓取消"]) or f"等待站稳 {trigger_text}，跌破 {invalid_text or '失效位'} 取消。",
        "source": "自选/持仓",
    }


def entry_alert_from_market_opportunity(row, global_context=None):
    trigger_text = first_text(row, ["触发价", "入场价", "加仓价"])
    if is_blank_price(trigger_text):
        return None
    current_text = first_text(row, ["现价", "当前价"])
    trigger = parse_float(trigger_text)
    current = parse_float(current_text)
    status = first_text(row, ["雷达状态", "状态"])
    tone = "green" if "🟢" in status or "高辨识度" in status else "yellow"
    gate_reason = global_risk.market_opportunity_gate_reason(global_context, row=row)
    technical_gate_ok = row.get("_radar_gate_ok")
    technical_gate_reason = row.get("_radar_gate_reason") or first_text(row, ["模拟门控"])
    action = first_text(row, ["动作", "建议"]) or "只作为替代机会观察，确认后才进入模拟盘。"
    framework_blocked = (row.get("framework_validation") or {}).get("executable") is False
    if framework_blocked:
        tone = "red"
        priority = "P2"
        label = "框架证据待重核"
        status = first_text(row, ["雷达状态", "状态"]) or "🔴 框架证据未核验"
    elif gate_reason:
        action = action.replace("可小仓试错", "门控解除后再评估").replace(
            "进入模拟盘", "门控解除并再次确认后进入模拟盘"
        )
        if not action.startswith(gate_reason):
            action = f"{gate_reason}；{action}"
        tone = "yellow"
        priority = "P2"
        label = "全市场候选观察"
        status = f"🔴 {gate_reason}"
    elif technical_gate_ok is False:
        tone = "yellow"
        priority = "P2"
        label = "观察候选（未触发）"
        technical_prefix = f"技术门控未通过：{technical_gate_reason or '等待量价/形态确认'}"
        if not action.startswith("技术门控未通过"):
            action = f"{technical_prefix}；{action}"
    else:
        priority = "P1" if tone == "green" else "P2"
        label = "全市场候选入场" if priority == "P1" else "全市场候选观察"
    return {
        "code": row.get("机会代码") or row.get("代码") or "",
        "name": row.get("机会名称") or row.get("名称") or "",
        "priority": priority,
        "tone": tone,
        "label": label,
        "status": status,
        "current_price": current_text,
        "trigger_price": trigger_text,
        "invalid_price": first_text(row, ["失效价", "防守"]),
        "support_line": first_text(row, ["VWAP", "加仓条件"]),
        "pressure_price": "",
        "distance": pct_distance(current, trigger),
        "confirm": "板块共振 + 站稳触发价 + 回踩不破，不追第一笔急拉",
        "action": action,
        "source": "全市场机会池",
    }


def build_entry_alerts(rows, market_opportunities, global_context=None):
    alerts = []
    seen = set()
    for row in rows or []:
        alert = entry_alert_from_stock_row(row)
        if not alert:
            continue
        key = (alert["source"], alert["code"], alert["trigger_price"])
        if key in seen:
            continue
        seen.add(key)
        alerts.append(alert)
    for row in market_opportunities or []:
        alert = entry_alert_from_market_opportunity(row, global_context=global_context)
        if not alert:
            continue
        key = (alert["source"], alert["code"], alert["trigger_price"])
        if key in seen:
            continue
        seen.add(key)
        alerts.append(alert)
    rank = {"green": 0, "yellow": 1, "red": 2}
    priority_rank = {"P1": 0, "P2": 1, "P0": 2}
    alerts.sort(key=lambda x: (rank.get(x.get("tone"), 9), priority_rank.get(x.get("priority"), 9), x.get("distance") or ""))
    return alerts[:12]


def should_include_section(title, has_rows=False, has_paper_trades=False, has_market_opportunities=False, has_paper_positions=False):
    if has_rows and re.search(r"订盘总览|个股盘后卡片", title or ""):
        return False
    if has_paper_trades and re.search(r"模拟盘成交", title or ""):
        return False
    if has_market_opportunities and re.search(r"全市场机会池", title or ""):
        return False
    if has_paper_positions and re.search(r"模拟账户持仓", title or ""):
        return False
    return True


def market_opportunity_counts(rows):
    counts = {"total": len(rows), "green": 0, "yellow": 0, "red": 0}
    for row in rows:
        tone = row.get("_tone") or "yellow"
        counts[tone] = counts.get(tone, 0) + 1
    return counts


def detect_kind(title, filename):
    text = f"{title} {filename}"
    if "系统质检" in text or "运行质检" in text:
        return "quality"
    if "集合竞价" in text:
        return "auction"
    if "实时信号" in text:
        return "realtime"
    if "盘前" in text:
        return "premarket"
    if "盘中" in text:
        return "intraday"
    if "盘后" in text:
        return "afterclose"
    return "report"


def load_mira_evidence_summary(kind, report_date, source_path):
    candidates = []
    if kind == "afterclose":
        source_match = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", Path(source_path).name)
        if source_match:
            candidates.append("".join(source_match.groups()))
    if report_date:
        candidates.append(report_date.replace("-", ""))
    seen = set()
    for compact in candidates:
        if not compact or compact in seen:
            continue
        seen.add(compact)
        path = BASE_DIR / "data" / "research" / "evidence_logs" / f"daily_{compact}.summary.json"
        if not path.exists():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"error": f"研究台账摘要读取失败：{path}"}
    return None


def load_global_risk_context(report_date):
    data = global_risk.read_context(BASE_DIR, report_date)
    if report_date and str(data.get("date") or "") != str(report_date):
        return None
    health = load_realtime_health(report_date)
    health_date = str(health.get("last_success_trading_date") or health.get("updated_at") or "")[:10]
    if report_date and health_date == str(report_date) and health.get("a_share_market_regime"):
        data["a_share_market_regime"] = health["a_share_market_regime"]
    return data


def parse_report(path):
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    title = strip_md(lines[0].lstrip("# ")) if lines else path.stem
    tables = parse_markdown_tables(lines)
    paper_trades = extract_paper_trades(tables)
    market_opportunities = extract_market_opportunities(tables)
    paper_positions = extract_paper_positions(tables)
    scored_tables = [(operation_table_score(t), t) for t in tables]
    stock_table = max(scored_tables, key=lambda item: item[0])[1] if scored_tables and max(score for score, _ in scored_tables) >= 0 else None
    rows = [enrich_stock_row(row) for row in (stock_table["rows"] if stock_table else [])]
    for row in rows:
        row["_priority"] = priority_of(row)
        row["_tone"] = status_color(row)
        row["_search"] = " ".join(str(v) for v in row.values())

    sections = []
    for section in parse_sections(lines):
        if not should_include_section(
            section["title"],
            has_rows=bool(rows),
            has_paper_trades=bool(paper_trades),
            has_market_opportunities=bool(market_opportunities),
            has_paper_positions=bool(paper_positions),
        ):
            continue
        sections.append({
            "title": section["title"],
            "html": markdown_fragment_to_html(section["lines"]),
            "plain": strip_md("\n".join(section["lines"]))[:1200],
        })

    generated = next((x["value"] for x in parse_meta(lines) if x["label"] == "生成时间"), "")
    date_match = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", title + " " + path.name)
    report_date = "-".join(date_match.groups()) if date_match else ""
    kind = detect_kind(title, path.name)
    runtime_trading_date = runtime_trading_date_for_report(kind, title, text, report_date)
    stale_afterclose = bool(kind == "afterclose" and runtime_trading_date and runtime_trading_date != report_date)
    paper_account = load_runtime_paper_account(runtime_trading_date)
    paper_orders = load_runtime_paper_orders(runtime_trading_date)
    if not paper_trades:
        paper_trades = paper_trades_from_orders(paper_orders)
    if not paper_account and paper_positions:
        paper_account = parse_paper_account_from_lines(lines)

    counts = {"P0": 0, "P1": 0, "P2": 0, "green": 0, "yellow": 0, "red": 0}
    for row in rows:
        counts[row["_priority"]] = counts.get(row["_priority"], 0) + 1
        counts[row["_tone"]] = counts.get(row["_tone"], 0) + 1

    evidence_summary = load_mira_evidence_summary(kind, runtime_trading_date, path)
    global_risk_context = load_global_risk_context(runtime_trading_date or report_date)
    realtime_health = load_realtime_health(runtime_trading_date or report_date)
    apply_market_opportunity_gate(market_opportunities, global_risk_context)
    record = {
        "id": report_id(path, title, kind, report_date),
        "source": str(path),
        "title": title,
        "kind": kind,
        "date": report_date,
        "runtime_trading_date": runtime_trading_date,
        "report_freshness": {
            "stale_afterclose": stale_afterclose,
            "message": (
                f"该盘后报告基于 {runtime_trading_date} 收盘数据，不应作为 {report_date} 的当日盘后复盘。"
                if stale_afterclose else ""
            ),
        },
        "realtime_health": realtime_health,
        "generated": generated,
        "meta": parse_meta(lines),
        "counts": counts,
        "paper_trades": paper_trades,
        "paper_trade_counts": paper_trade_counts(paper_trades),
        "paper_orders": paper_orders,
        "paper_order_counts": paper_order_counts(paper_orders),
        "paper_account": paper_account,
        "paper_positions": paper_positions,
        "paper_position_counts": paper_position_counts(paper_positions, paper_account),
        "market_opportunities": market_opportunities,
        "market_opportunity_counts": market_opportunity_counts(market_opportunities),
        "entry_alerts": build_entry_alerts(rows, market_opportunities, global_context=global_risk_context),
        "mira_evidence_log": evidence_summary,
        "global_risk": global_risk_context,
        "rows": rows,
        "headers": stock_table["headers"] if stock_table else [],
        "tables": [
            {
                "title": table.get("title") or "",
                "section": table.get("section") or "",
                "headers": table.get("headers") or [],
                "row_count": len(table.get("rows") or []),
                "operation_score": operation_table_score(table),
            }
            for table in tables
        ],
        "sections": sections,
        "raw": text,
        "published_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    record["mira_gate"] = mira_research_gate.build_report_gate(record)
    return record


def report_id(path, title, kind, report_date):
    time_match = re.search(r"_(\d{4})(?:\.md)?$", path.stem)
    compact_date = (report_date or "unknown").replace("-", "")
    suffix = f"_{time_match.group(1)}" if time_match else ""
    return f"{kind}_{compact_date}{suffix}"


def load_index():
    if not INDEX_PATH.exists():
        return []
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def write_index(record):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    items = [x for x in load_index() if x.get("id") != record["id"]]
    items.insert(0, {
        "id": record["id"],
        "title": record["title"],
        "kind": record["kind"],
        "date": record["date"],
        "generated": record["generated"],
        "published_at": record["published_at"],
        "rows": len(record["rows"]),
        "p0": record["counts"].get("P0", 0),
        "p1": record["counts"].get("P1", 0),
        "p2": record["counts"].get("P2", 0),
        "paper_trades": record.get("paper_trade_counts", {}).get("total", 0),
        "market_opportunities": record.get("market_opportunity_counts", {}).get("total", 0),
        "readiness": record.get("mira_gate", {}).get("readiness_level", ""),
        "evidence_claims": (record.get("mira_evidence_log") or {}).get("total_claims", 0),
    })
    items.sort(key=lambda x: (x.get("date") or "", x.get("generated") or "", x.get("published_at") or ""), reverse=True)
    INDEX_PATH.write_text(json.dumps(items[:120], ensure_ascii=False, indent=2), encoding="utf-8")
    return items


def publish_report(markdown_path):
    markdown_path = Path(markdown_path)
    record = parse_report(markdown_path)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{record['id']}.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    write_index(record)
    return {
        "id": record["id"],
        "json": str(out),
        "dashboard": str(WEB_DIR / "index.html"),
        "url_hint": f"web_dashboard/index.html?report={record['id']}&theme=light",
    }


def main():
    parser = argparse.ArgumentParser(description="Publish A-share markdown report to web dashboard data")
    parser.add_argument("markdown_path")
    args = parser.parse_args()
    print(json.dumps(publish_report(args.markdown_path), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
