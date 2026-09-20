#!/usr/bin/env python3
import json
import math
import os
import re
import statistics
import time
import urllib.request
from datetime import datetime
from html import unescape
from pathlib import Path

import after_close_report as base
import intraday_report as intra
import render_report_dashboard as dashboard
from core import global_risk
from core import emotion_leader_pool
from core import mira_research_gate
from core import observation_strategy_router


BASE_DIR = Path(os.environ.get("A_SHARE_BASE_DIR", Path(__file__).resolve().parent))
REPORT_DATE = os.environ.get("A_SHARE_REPORT_DATE") or base.current_datetime().strftime("%Y-%m-%d")
REPORT_PATH = BASE_DIR / f"同花顺我的股票盘前全面分析_{REPORT_DATE}.md"
FEISHU_WEBHOOK = base.FEISHU_WEBHOOK


class FeishuPushError(RuntimeError):
    pass


def clean_html(text):
    text = re.sub(r"<.*?>", " ", text or "", flags=re.S)
    return unescape(re.sub(r"\s+", " ", text)).strip()


def clean_html_lines(text):
    text = re.sub(r"</(?:p|li|tr|h\d|div)>", "\n", text or "", flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<td[^>]*>", " | ", text, flags=re.I)
    text = re.sub(r"<.*?>", " ", text, flags=re.S)
    text = unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def fetch_gbk(url, timeout=15):
    raw = base.fetch_bytes(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    return raw.decode("gbk", "ignore")


def fetch_news_list(url, source, limit=8):
    try:
        html = fetch_gbk(url)
    except Exception as exc:
        return [{"title": f"{source}读取失败：{exc}", "url": url, "summary": ""}]
    pattern = re.compile(
        r'<a target="_blank" title="([^"]+)" href="([^"]+)" class="news-link"[^>]*>.*?</a>.*?'
        r'<a target="_blank" href="[^"]+"\s+rel="nofollow" class="arc-cont news-link"[^>]*>(.*?)</a>',
        re.S,
    )
    items = []
    for title, link, summary in pattern.findall(html):
        items.append({
            "source": source,
            "title": clean_html(title),
            "url": link.replace("http://", "https://"),
            "summary": clean_html(summary),
        })
        if len(items) >= limit:
            break
    return items


def fetch_realtime_news(limit=12):
    url = "https://stock.10jqka.com.cn/thsgd/ywjh.js"
    try:
        text = fetch_gbk(url)
        m = re.search(r"item:(\[.*?\])\s*};", text, re.S)
        if not m:
            return []
        arr = json.loads(m.group(1))
    except Exception as exc:
        return [{"title": f"7x24读取失败：{exc}", "content": "", "pubDate": "", "url": "https://news.10jqka.com.cn/realtimenews.html"}]
    items = []
    for row in arr[:limit]:
        items.append({
            "source": row.get("source") or "同花顺7x24",
            "title": row.get("title") or "",
            "content": row.get("content") or "",
            "pubDate": row.get("pubDate") or "",
            "url": (row.get("url") or "https://news.10jqka.com.cn/realtimenews.html").replace("http://", "https://"),
            "stocks": row.get("stocks") or "",
        })
    return items


GLOBAL_CODES = {
    "纳斯达克": "gzs_IXIC",
    "标普500": "gzs_SPX",
    "道琼斯": "gzs_DJI",
    "恒生指数": "gzs_HSZS",
    "日经225": "gzs_N225",
    "韩国综合": "gzs_KS11",
    "英国富时100": "gzs_FTSE",
    "德国DAX": "gzs_GDAXI",
}

SINA_GLOBAL_CODES = {
    "纳斯达克": ("gb_ixic", "us"),
    "标普500": ("gb_inx", "us"),
    "道琼斯": ("gb_dji", "us"),
    "恒生指数": ("rt_hkHSI", "hk"),
    "日经225": ("b_NKY", "global"),
    "韩国综合": ("b_KOSPI", "global"),
    "英国富时100": ("b_FTSE", "global"),
    "德国DAX": ("b_DAX", "global"),
}

GLOBAL_MARKET_CACHE = BASE_DIR / "data" / "runtime" / "global_markets_cache.json"


def safe_float(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except Exception:
        return None


def valid_global_quote(row):
    return safe_float(row.get("price")) is not None and safe_float(row.get("pct")) is not None


def fetch_ths_global_quote(label, code):
    url = f"https://d.10jqka.com.cn/v2/realhead/{code}/last.js"
    try:
        text = base.fetch_text(url, timeout=12)
        m = re.search(r"\((\{.*\})\)", text, re.S)
        if not m:
            return {"name": label, "status": "不可用"}
        data = json.loads(m.group(1))
        items = data.get("items", {})
        price = safe_float(items.get("10"))
        prev = safe_float(items.get("7"))
        change = safe_float(items.get("264648"))
        if change is None and price is not None and prev:
            change = price - prev
        pct = safe_float(items.get("199112"))
        if pct is None and price is not None and prev:
            pct = (price - prev) / prev * 100
        if price is None or pct is None:
            return {"name": label, "status": "同花顺字段不可用"}
        return {
            "name": items.get("name") or label,
            "price": price,
            "change": change if change is not None else 0,
            "pct": pct,
            "time": (items.get("time") or items.get("updateTime") or "") + " 同花顺",
            "source": "同花顺",
        }
    except Exception as exc:
        return {"name": label, "status": f"读取失败：{exc}"}


def parse_sina_global_line(label, symbol, kind, body):
    fields = body.split(",")
    if kind == "us" and len(fields) >= 5:
        price = safe_float(fields[1])
        pct = safe_float(fields[2])
        change = safe_float(fields[4])
        time_text = fields[3]
        name = fields[0] or label
    elif kind == "hk" and len(fields) >= 18:
        price = safe_float(fields[6])
        change = safe_float(fields[7])
        pct = safe_float(fields[8])
        time_text = f"{fields[17]} {fields[18] if len(fields) > 18 else ''}".strip()
        name = fields[1] or label
    elif kind == "global" and len(fields) >= 8:
        price = safe_float(fields[1])
        change = safe_float(fields[2])
        pct = safe_float(fields[3])
        time_text = f"{fields[6]} {fields[7]}".strip()
        name = fields[0] or label
    else:
        return None
    if price is None or pct is None:
        return None
    return {
        "name": name,
        "price": price,
        "change": change if change is not None else 0,
        "pct": pct,
        "time": f"{time_text} 新浪".strip(),
        "source": "新浪",
        "symbol": symbol,
    }


def fetch_sina_global_markets():
    symbol_to_meta = {symbol: (label, kind) for label, (symbol, kind) in SINA_GLOBAL_CODES.items()}
    url = "https://hq.sinajs.cn/list=" + ",".join(symbol_to_meta.keys())
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            text = resp.read().decode("gbk", "ignore")
    except Exception:
        return {}
    rows = {}
    for symbol, body in re.findall(r'var hq_str_([^=]+)="(.*?)";', text):
        label, kind = symbol_to_meta.get(symbol, (None, None))
        if not label or not body:
            continue
        row = parse_sina_global_line(label, symbol, kind, body)
        if row:
            rows[label] = row
    return rows


def load_global_market_cache():
    try:
        rows = json.loads(GLOBAL_MARKET_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return []
    for row in rows:
        row["time"] = f"{row.get('time', '')} 缓存".strip()
        row["source"] = "缓存"
    return rows


def save_global_market_cache(rows):
    valid_rows = [row for row in rows if valid_global_quote(row)]
    if not valid_rows:
        return
    GLOBAL_MARKET_CACHE.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_MARKET_CACHE.write_text(json.dumps(valid_rows, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_global_markets():
    sina_rows = fetch_sina_global_markets()
    rows = []
    for label, code in GLOBAL_CODES.items():
        ths_row = fetch_ths_global_quote(label, code)
        sina_row = sina_rows.get(label)
        if valid_global_quote(ths_row):
            row = ths_row
            if valid_global_quote(sina_row):
                row["cross_check"] = {
                    "source": "新浪",
                    "price": sina_row.get("price"),
                    "pct": sina_row.get("pct"),
                    "time": sina_row.get("time"),
                    "pct_delta": round((safe_float(ths_row.get("pct")) or 0) - (safe_float(sina_row.get("pct")) or 0), 2),
                }
        else:
            row = sina_row or ths_row
        rows.append(row)
    if any(valid_global_quote(row) for row in rows):
        save_global_market_cache(rows)
        return rows
    cached = load_global_market_cache()
    if cached:
        return cached
    return rows



def extract_div_block(html, block_id):
    m = re.search(rf'<div id="{re.escape(block_id)}">(.*?)</div>', html, re.S)
    return m.group(1) if m else ""


def parse_bracket_sections(block):
    text = clean_html_lines(block)
    parts = re.split(r"【([^】]+)】", text)
    sections = {}
    for title, body in zip(parts[1::2], parts[2::2]):
        lines = []
        for line in body.splitlines():
            line = re.sub(r"^\s*[·•]\s*", "", line).strip()
            if not line:
                continue
            if len(line) > 220:
                chunks = re.split(r"(?<=。)", line)
                lines.extend(x.strip() for x in chunks if x.strip())
            else:
                lines.append(line)
        sections[title.strip()] = lines
    return sections


def parse_table_after_title(html, title, limit=8):
    m = re.search(rf"<h4[^>]*>\s*{re.escape(title)}\s*</h4>.*?<table>(.*?)</table>", html, re.S)
    if not m:
        return []
    table = m.group(1)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)
    parsed = []
    for row in rows[1:]:
        cols = [clean_html(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        cols = [x for x in cols if x]
        if cols:
            parsed.append(cols)
        if len(parsed) >= limit:
            break
    return parsed


def parse_zaopan_page(html, url):
    date_matches = re.findall(r'Global\.date\s*=\s*"(\d+)"', html)
    left = re.search(r'<div id="block_2125">(.*?)</div>\s*</div><!--content-main-fl-->', html, re.S)
    block = left.group(1) if left else extract_div_block(html, "block_2125")
    return {
        "source": "同花顺早盘必读",
        "url": url,
        "date": date_matches[-1] if date_matches else "",
        "sections": parse_bracket_sections(block),
        "suspensions": parse_table_after_title(html, "今日停复牌", limit=8),
        "block_trades": parse_table_after_title(html, "大宗交易", limit=8),
    }


def fetch_zaopan_focus(expected_date=None):
    expected_date = (expected_date or REPORT_DATE).replace("-", "")
    urls = [
        f"https://stock.10jqka.com.cn/zaopan/{expected_date}.shtml",
        "https://stock.10jqka.com.cn/zaopan/",
    ]
    errors = []
    stale_page = None
    for url in urls:
        try:
            page = parse_zaopan_page(fetch_gbk(url), url)
            page["expected_date"] = expected_date
            if page.get("date") == expected_date:
                page["stale"] = False
                return page
            stale_page = page
        except Exception as exc:
            errors.append(f"{url}: {exc}")

    if stale_page:
        stale_date = stale_page.get("date") or "日期未识别"
        return {
            "source": "同花顺早盘必读",
            "url": stale_page.get("url") or urls[-1],
            "date": stale_date,
            "expected_date": expected_date,
            "stale": True,
            "error": f"同花顺早盘页日期为 {stale_date}，不是今日 {expected_date}；已停止使用旧早盘内容。",
            "fetch_errors": errors,
            "sections": {},
            "suspensions": [],
            "block_trades": [],
        }

    url = urls[0]
    try:
        html = fetch_gbk(url)
        page = parse_zaopan_page(html, url)
        page["expected_date"] = expected_date
        return page
    except Exception as exc:
        message = "; ".join(errors + [str(exc)])
        return {"source": "同花顺早盘必读", "url": url, "expected_date": expected_date, "error": message, "sections": {}, "suspensions": [], "block_trades": []}


def fetch_fupan_focus():
    url = "https://stock.10jqka.com.cn/fupan/"
    try:
        html = fetch_gbk(url)
        date_matches = re.findall(r'Global\.date\s*=\s*"(\d+)"', html)
        themes = []
        for theme, table in re.findall(r'<div class="rise_top3_tipbox"[^>]*>.*?<strong class="strong_s">(.*?)</strong>\s*<table.*?>(.*?)</table>', html, re.S):
            stocks = []
            for code, name, pct_value, price in re.findall(r'<a[^>]+/(\d{6})/"[^>]*>(.*?)</a>.*?<td class="trp">(.*?)</td>.*?<td class="trp">(.*?)</td>', table, re.S):
                stocks.append({"code": code, "name": clean_html(name), "pct": clean_html(pct_value), "price": clean_html(price)})
            themes.append({"theme": clean_html(theme), "stocks": stocks[:6]})
            if len(themes) >= 6:
                break
        return {
            "source": "同花顺复盘",
            "url": url,
            "date": date_matches[-1] if date_matches else "",
            "summary": clean_html(extract_div_block(html, "block_1887")),
            "market_thread": clean_html(extract_div_block(html, "block_1889")),
            "main_themes": clean_html(extract_div_block(html, "block_1890")),
            "active_thread": clean_html(extract_div_block(html, "block_1891")),
            "themes": themes,
        }
    except Exception as exc:
        return {"source": "同花顺复盘", "url": url, "error": str(exc), "themes": []}


def fetch_zhangting_moves(limit=8):
    url = "https://yuanchuang.10jqka.com.cn/zhangting/"
    try:
        html = fetch_gbk(url)
        items = []
        pattern = re.compile(
            r'<h2 class="title">(.*?)</h2>.*?<p class="intro">(.*?)</p>(?:\s*<p class="stocks">(.*?)</p>)?.*?<span>(异动观察\s*\|\s*.*?)</span>',
            re.S,
        )
        for title, intro, stocks_html, when in pattern.findall(html):
            stocks = []
            for text in re.findall(r'<a[^>]*>(.*?)</a>', stocks_html or "", re.S):
                stock_text = clean_html(text)
                if stock_text:
                    stocks.append(stock_text)
            link_match = re.search(r'href="([^"]+)"', intro)
            items.append({
                "title": clean_html(title),
                "intro": clean_html(intro).replace("[详细内容]", ""),
                "stocks": stocks,
                "time": clean_html(when),
                "url": (link_match.group(1) if link_match else url).replace("http://", "https://"),
            })
            if len(items) >= limit:
                break
        return {"source": "同花顺异动观察", "url": url, "items": items}
    except Exception as exc:
        return {"source": "同花顺异动观察", "url": url, "error": str(exc), "items": []}


def parse_bidu_links(block, limit=8):
    items = []
    for title, href in re.findall(r'<a title="([^"]+)" href="([^"]+)"', block, re.S):
        items.append({"title": clean_html(title), "url": href.replace("http://", "https://")})
        if len(items) >= limit:
            break
    return items


def fetch_bidu_special():
    url = "https://stock.10jqka.com.cn/bidu/"
    try:
        html = fetch_gbk(url)
        calendar_idx = html.find('data-taid="ggpd_calendar"')
        tips_idx = html.find('data-taid="ggpd_jrtbts"')
        funds_idx = html.find('data-taid="ggpd_zjtop"')
        limit_idx = html.find('data-taid="ggpd_ztgjm"')
        calendar_block = html[calendar_idx:calendar_idx + 2400] if calendar_idx >= 0 else ""
        tips_block = html[tips_idx:tips_idx + 2600] if tips_idx >= 0 else ""
        funds_block = html[funds_idx:funds_idx + 4500] if funds_idx >= 0 else ""
        limit_block = html[limit_idx:limit_idx + 2200] if limit_idx >= 0 else ""

        tips = []
        for count, label, stocks in re.findall(r'<h5[^>]*>.*?<span[^>]*>(.*?)</span>\s*(.*?)\s*</h5>\s*<p[^>]*>(.*?)</p>', tips_block, re.S):
            tips.append({"label": clean_html(label), "count": clean_html(count), "stocks": clean_html(stocks)})

        fund_rows = []
        first_tbody = re.search(r"<tbody>(.*?)</tbody>", funds_block, re.S)
        if first_tbody:
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", first_tbody.group(1), re.S)[:8]:
                cols = [clean_html(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
                if len(cols) >= 3:
                    fund_rows.append({"code": cols[0], "name": cols[1], "amount": cols[2]})

        return {
            "source": "同花顺必读右侧特色模块",
            "url": url,
            "calendar_links": parse_bidu_links(calendar_block, limit=8),
            "today_tips": tips,
            "fund_inflow": fund_rows,
            "limit_reasons": parse_bidu_links(limit_block, limit=8),
        }
    except Exception as exc:
        return {"source": "同花顺必读右侧特色模块", "url": url, "error": str(exc), "calendar_links": [], "today_tips": [], "fund_inflow": [], "limit_reasons": []}


def fetch_ths_discover_focus():
    return {
        "zaopan": fetch_zaopan_focus(REPORT_DATE),
        "fupan": fetch_fupan_focus(),
        "zhangting": fetch_zhangting_moves(),
        "bidu_special": fetch_bidu_special(),
    }


def ths_focus_text(ths_focus):
    parts = []
    zaopan = ths_focus.get("zaopan", {})
    for title, lines in zaopan.get("sections", {}).items():
        parts.append(title)
        parts.extend(lines[:12])
    fupan = ths_focus.get("fupan", {})
    parts.extend([fupan.get("summary", ""), fupan.get("market_thread", ""), fupan.get("main_themes", ""), fupan.get("active_thread", "")])
    for theme in fupan.get("themes", []):
        parts.append(theme.get("theme", ""))
        parts.extend(x.get("name", "") + x.get("code", "") for x in theme.get("stocks", []))
    for item in ths_focus.get("zhangting", {}).get("items", []):
        parts.extend([item.get("title", ""), item.get("intro", ""), " ".join(item.get("stocks", []))])
    special = ths_focus.get("bidu_special", {})
    for key in ("calendar_links", "limit_reasons"):
        parts.extend(x.get("title", "") for x in special.get(key, []))
    parts.extend(x.get("name", "") + x.get("code", "") for x in special.get("fund_inflow", []))
    return " ".join(x for x in parts if x)


THEME_HINTS = {
    "600183": ["PCB", "AI服务器", "电子布", "光通信", "算力", "铜箔"],
    "000988": ["光通信", "光纤", "CPO", "算力", "AI服务器", "通信"],
    "600589": ["算力", "AIDC", "DeepSeek", "数据中心", "国产算力"],
    "688545": ["光刻胶", "先进封装", "半导体", "国产替代", "电子化学品"],
    "688396": ["半导体", "国产替代", "芯片", "先进封装", "智驾芯片"],
    "688127": ["AI眼镜", "光学", "MicroLED", "消费电子", "苹果"],
    "301071": ["培育钻石", "金刚石", "CVD"],
    "600172": ["培育钻石", "金刚石", "超硬材料"],
    "002297": ["培育钻石", "金刚石", "超硬材料"],
    "301489": ["液冷", "先进散热", "热管理"],
    "300435": ["液冷", "先进散热", "热管理"],
    "002536": ["液冷", "热管理", "AI算力"],
    "601212": ["白银", "贵金属", "有色金属"],
    "002716": ["白银", "贵金属", "有色金属"],
    "601899": ["黄金", "铜", "贵金属", "有色金属"],
    "000506": ["黄金", "贵金属", "有色金属"],
    "002080": ["玻纤", "玻璃微纤维", "风电", "复合材料", "商业航天"],
    "601208": ["电子材料", "PET铜箔", "光伏", "MLCC", "绝缘材料"],
    "300775": ["军工", "商业航天", "低空", "航空"],
    "605168": ["算力", "数据中心", "DeepSeek", "AI"],
}

STOCK_CONTEXT_THEME_KEYWORDS = (
    "液冷", "先进散热", "热管理", "AI算力", "算力", "AI服务器", "服务器", "数据中心",
    "CPO", "光通信", "PCB", "半导体", "芯片", "存储", "先进封装", "机器人",
    "商业航天", "军工", "新能源", "储能", "光伏", "风电", "铜", "铝", "稀土",
    "黄金", "白银", "贵金属", "有色金属", "培育钻石", "金刚石", "超硬材料",
    "煤炭", "化工", "创新药", "医疗", "消费", "零售", "种业",
)


def stock_context_theme_labels(ths, f10):
    """Extract auditable theme labels from company-specific research text."""
    parts = []
    for key in ("news", "notice"):
        for item in (ths or {}).get(key) or []:
            if isinstance(item, dict):
                parts.extend((item.get("title") or "", item.get("abstract") or ""))
    profile = (f10 or {}).get("profile") or {}
    parts.append(profile.get("business") or "")
    text = " ".join(str(item) for item in parts if item)
    return [keyword for keyword in STOCK_CONTEXT_THEME_KEYWORDS if keyword.lower() in text.lower()]


def stock_focus_signal(stock, ths_focus):
    q = stock["quote"]
    text = ths_focus_text(ths_focus)
    direct = []
    if q["code"] in text:
        direct.append(q["code"])
    if q["name"] in text:
        direct.append(q["name"])
    matched = [x for x in THEME_HINTS.get(q["code"], []) if x and x in text]
    if direct:
        return "🔴 同花顺专题直接点名：" + "、".join(direct[:2])
    if matched:
        return "🟡 题材承接验证：" + "、".join(dict.fromkeys(matched[:4]))
    return "🟢 未见早盘专题直接催化"


def fetch_stock_context():
    core_universe = base.read_watchlist()
    observation_plan = base.premarket_plan_observation_details()
    emotion_snapshot = emotion_leader_pool.load_latest_snapshot(BASE_DIR)
    if str(emotion_snapshot.get("source_date") or "") != base.previous_trading_date(REPORT_DATE):
        emotion_snapshot = {}
    observation_plan, emotion_candidates = emotion_leader_pool.merge_into_plan(observation_plan, emotion_snapshot)
    core_codes = {code for code, _market in core_universe}
    plan_group_by_code = {}
    for group in observation_plan.get("groups") or []:
        for code in group.get("codes") or []:
            plan_group_by_code.setdefault(str(code), str(group.get("name") or "观察池"))
    universe = list(core_universe)
    universe.extend(
        (code, market)
        for code, market in (observation_plan.get("rows") or [])
        if code not in core_codes
    )
    quotes = base.parse_tencent_quotes(universe)
    from core import closed_liquidity
    closed_quote_cache = base.load_quote_cache()
    boards_top, boards_keep = base.fetch_boards()
    from core import sector_identity
    try:
        sector_identity.update_catalog(BASE_DIR, boards_top)
    except OSError:
        pass
    board_catalog = sector_identity.load_catalog(BASE_DIR) or boards_top
    identity_profiles = sector_identity.load_profiles(BASE_DIR)
    full_deep_context = os.environ.get("A_SHARE_FULL_WATCHLIST_DEEP_CONTEXT") == "1"
    skip_deep_context = os.environ.get("A_SHARE_PREMARKET_SKIP_DEEP_CONTEXT") == "1"
    stocks = []
    for code, market in universe:
        quote = quotes.get(code)
        if not quote:
            continue
        emotion_candidate = emotion_candidates.get(code) or {}
        if emotion_candidate.get("float_market_cap_yi"):
            quote["float_market_cap_yi"] = emotion_candidate["float_market_cap_yi"]
        daily = base.fetch_sohu_daily(code)
        if not daily:
            continue
        quote["code"] = code
        # Sohu's daily endpoint may still end on T-1 after the market closes.
        # Merge Tencent's timestamped T quote before computing MA5/MA20 and
        # strategy eligibility, otherwise a broken MA5 can be misclassified
        # as a valid trend pullback for tomorrow's plan.
        daily = merge_closed_quote_for_premarket(daily, quote)
        daily = closed_liquidity.enrich(daily, quote, closed_quote_cache.get(code) or {})
        if daily:
            daily = closed_liquidity.enrich(daily, quote,
                closed_liquidity.load_completed_quote(BASE_DIR, code, daily[-1].get('date')))
        tech = base.trend_text(daily, quote)
        # Every self-selected name gets quote, daily structure, classification
        # and a strategy plan.  Slow F10/community enrichment is reserved for
        # core holdings and objectively active/risky names so the premarket
        # job stays inside its launchd window instead of dropping coverage.
        deep_context = bool(
            not skip_deep_context
            and (
                full_deep_context
                or code in core_codes
                or abs(float(quote.get("pct") or 0)) >= 5.0
                or bool(emotion_candidate)
                or str(tech.get("priority") or "") in {"P0", "P1"}
            )
        )
        ths = {
            "news": [], "notice": [], "community_available": False,
            "community_note": "全量观察池已完成行情/日线/策略覆盖；当日未进入重点深检。",
        }
        f10 = {"concepts": [], "profile": {}, "holder": {}}
        if deep_context:
            ths = base.fetch_ths_news(code, market)
            f10 = base.fetch_f10(code)
        profile = f10.get("profile") or {}
        identity_profile = identity_profiles.get(code) or {}
        quote["industry"] = identity_profile.get('industry') or quote.get("industry") or profile.get("industry") or ""
        raw_concepts = identity_profile.get('concepts') or quote.get("concepts") or list(f10.get("concepts") or [])
        concepts = sector_identity.tokens(raw_concepts)
        news_theme_mentions = stock_context_theme_labels(ths, f10)
        quote["concepts"] = list(dict.fromkeys(str(item).strip() for item in concepts if str(item).strip()))
        quote["verified_concepts"] = list(quote["concepts"])
        community_temp = classify_community(ths)
        stock = {
            "quote": quote,
            "daily": daily,
            "ths": ths,
            "f10": f10,
            "tech": tech,
            "community_temp": community_temp,
            "emotion": "",
            "news_theme_mentions": news_theme_mentions,
            "board_catalog": board_catalog,
            "plan_universe": "核心股票池" if code in core_codes else base.observation_plan_scope_label(plan_group_by_code.get(code, "未分组")),
            "observation_plan_group": plan_group_by_code.get(code),
            "emotion_leader_candidate": emotion_candidate,
            "manual_leader_evidence": list(emotion_candidate.get("reasons") or []),
        }
        stock["emotion"] = base.emotion_from_data(stock, boards_keep)
        stock["strategy_contract"] = observation_strategy_router.classify_stock(stock)
        stocks.append(stock)
        time.sleep(0.03 if not deep_context else 0.08)
    return stocks, boards_top, boards_keep


def merge_closed_quote_for_premarket(daily, quote, report_date=None):
    """Return only closed daily bars and merge a lagging T-1 quote if needed."""
    report_date = str(report_date or REPORT_DATE)
    daily = [
        row for row in (daily or [])
        if not str(row.get("date") or "") or str(row.get("date")) < report_date
    ]
    raw_datetime = re.sub(r"\D", "", str((quote or {}).get("datetime") or ""))
    if len(raw_datetime) < 8:
        return daily
    quote_date = f"{raw_datetime[:4]}-{raw_datetime[4:6]}-{raw_datetime[6:8]}"
    if quote_date >= report_date:
        return daily
    return base.daily_with_quote(daily, quote)


def classify_community(ths):
    if not ths.get("community_available"):
        return "社区情绪不可用"
    text = " ".join(ths.get("community_titles", []) + ths.get("community_contents", []))
    if not text.strip():
        return "🟢 冷静/低关注（样本不足）"
    bullish = len(re.findall(r"涨|牛|龙头|突破|加仓|看多|冲|爆发|行情|机会", text))
    bearish = len(re.findall(r"跌|割|利空|出货|套|风险|减仓|下行|跑|当心", text))
    if bullish + bearish >= 5 and abs(bullish - bearish) >= 3:
        return "🔴 一致亢奋/恐慌"
    if bullish + bearish >= 2:
        return "🟡 分歧升温"
    return "🟢 冷静/低关注"


def summarize_macro(news_groups, realtime, global_quotes, ths_focus):
    titles = " ".join(item.get("title", "") + " " + item.get("summary", "") for group in news_groups.values() for item in group)
    realtime_text = " ".join(item.get("title", "") + " " + item.get("content", "") for item in realtime)
    focus_text = ths_focus_text(ths_focus)
    all_text = f"{titles} {realtime_text} {focus_text}"
    global_bias = "中性"
    nas = next((x for x in global_quotes if "纳斯达克" in x.get("name", "")), None)
    spx = next((x for x in global_quotes if "标普" in x.get("name", "")), None)
    if (nas and nas.get("pct", 0) > 0.5) or (spx and spx.get("pct", 0) > 0.5):
        global_bias = "偏多科技成长"
    elif (nas and nas.get("pct", 0) < -0.5) or (spx and spx.get("pct", 0) < -0.5):
        global_bias = "偏空科技成长"

    bullets = []
    domestic = first_zaopan_line(ths_focus, "昨日国内行情回顾")
    if domestic:
        bullets.append(f"同花顺早盘回顾：{clip_text(domestic, 150)}")
    fupan = ths_focus.get("fupan", {})
    if fupan.get("main_themes"):
        bullets.append(f"同花顺复盘主流看点：{fupan['main_themes']}。")
    if fupan.get("active_thread") or fupan.get("market_thread"):
        bullets.append(f"复盘盘面脉络：{clip_text(fupan.get('active_thread') or fupan.get('market_thread'), 140)}")
    major_news = ths_focus.get("zaopan", {}).get("sections", {}).get("重大新闻汇总", [])
    if major_news:
        bullets.append(f"早盘重大新闻：{clip_text(major_news[0], 130)}")
    company_news = [x for x in ths_focus.get("zaopan", {}).get("sections", {}).get("公司公告", []) if "|" in x and "公司名称" not in x]
    if company_news:
        bullets.append(f"公司公告关注：{clip_text(company_news[0], 120)}")
    if "城市更新" in all_text:
        bullets.append("城市更新“十五五”规划发酵，基建、建材、管网、地下空间等方向有政策催化。")
    if "工业企业利润" in all_text or "利润" in all_text:
        bullets.append("工业企业利润改善，宏观修复对顺周期和制造业情绪有支撑。")
    if "中美" in all_text or "关税" in all_text or "APEC" in all_text:
        bullets.append("中美经贸沟通继续推进，若盘中风险偏好改善，外向型科技与高端制造承接会更关键。")
    if "WTI" in all_text or "原油" in all_text:
        bullets.append("原油回落压低通胀与成本扰动，对部分制造下游偏中性偏多，但也提示全球需求预期仍需观察。")
    if "美伊" in all_text or "伊朗" in all_text:
        bullets.append("中东/美伊消息继续影响能源与避险情绪，盘前需观察油价和黄金联动。")
    if not bullets:
        bullets.append("宏观信息整体中性，重点看开盘量能和题材承接。")
    return global_bias, bullets


TECH_RISK_KEYWORDS = (
    "半导体", "芯片", "存储", "光通信", "CPO", "算力", "AI", "人工智能",
    "软件", "通信", "电子", "计算机", "机器人", "元件",
)


def stock_is_technology(stock):
    quote = stock.get("quote") or {}
    text = " ".join(str(quote.get(key) or "") for key in ("industry", "name", "theme", "concept"))
    return any(word in text for word in TECH_RISK_KEYWORDS)


def paper_position_for_stock(paper_snapshot, code):
    paper_snapshot = paper_snapshot or {}
    account = paper_snapshot.get("account") or {}
    total_assets = float(account.get("total_assets") or account.get("total_amount") or 0)
    for position in paper_snapshot.get("positions") or []:
        if str(position.get("symbol") or position.get("代码") or "") != str(code):
            continue
        market_value = float(position.get("market_value") or 0)
        return {
            "quantity": int(position.get("quantity") or 0),
            "market_value": market_value,
            "position_pct": market_value / total_assets if total_assets > 0 else 0.0,
        }
    return {"quantity": 0, "market_value": 0.0, "position_pct": 0.0}


def premarket_position_plan(stock, macro_bias, paper_snapshot=None):
    """Set the daily allocation envelope and named strategy authority.

    The plan deliberately contains no fixed entry or exit price.  Daily/weekly
    structure, emotion, chips and the major trend decide *whether* a symbol can
    absorb risk and how much. Observation-pool methods own the entry event;
    V2 is shared closed-bar timing evidence for execution band and exits.
    """
    quote = stock.get("quote") or {}
    tech = stock.get("tech") or {}
    code = str(quote.get("code") or "")
    priority = str(tech.get("priority") or "P2")
    state = str(tech.get("state") or "")
    holding = paper_position_for_stock(paper_snapshot, code)
    current_pct = float(holding.get("position_pct") or 0.0)
    tech_risk = "偏空科技成长" in str(macro_bias or "") and stock_is_technology(stock)
    observation_plan_group = str(stock.get("observation_plan_group") or "").strip()
    strategy = stock.get("strategy_contract") or {}

    if priority == "P0":
        plan = {
            "plan_action": "防守降风险",
            "target_position_pct": 0.0,
            "max_position_pct": 0.0,
            "v2_probe_position_pct": 0.0,
            "v2_entry_enabled": False,
            "reason": "日线/周线风险优先，盘中只接受V2减仓或结构退出，不新增风险暴露。",
        }
    elif priority == "P1":
        plan = {
            "plan_action": "修复试仓",
            "target_position_pct": 0.03,
            "max_position_pct": 0.05,
            "v2_probe_position_pct": 0.01,
            "v2_entry_enabled": True,
            "reason": "大势未完全确认，保留小仓修复资格；必须等V2完成三周期与5分钟执行确认。",
        }
    else:
        plan = {
            "plan_action": "顺势分批",
            "target_position_pct": 0.06,
            "max_position_pct": 0.08,
            "v2_probe_position_pct": 0.02,
            "v2_entry_enabled": True,
            "reason": "日线/周线趋势可承载风险，盘中仅在V2有效Setup出现时按计划分批。",
        }

    if tech_risk and plan["v2_entry_enabled"]:
        plan.update({
            "plan_action": "科技防守试仓",
            "target_position_pct": min(plan["target_position_pct"], 0.03),
            "max_position_pct": min(plan["max_position_pct"], 0.05),
            "v2_probe_position_pct": min(plan["v2_probe_position_pct"], 0.01),
            "reason": "隔夜科技风险偏弱，科技链只保留低风险试仓，等待盘中结构修复确认。",
        })

    if plan["v2_entry_enabled"] and (observation_plan_group or strategy.get("is_observation_strategy")):
        strategy_scope = observation_plan_group or str(stock.get("plan_universe") or "全自选池")
        plan.update({
            "plan_action": "观察池条件试仓",
            "target_position_pct": min(plan["target_position_pct"], 0.02),
            "max_position_pct": min(plan["max_position_pct"], 0.03),
            "v2_probe_position_pct": min(plan["v2_probe_position_pct"], 0.01),
                "reason": (
                    f"{strategy_scope}已纳入三策略盘前合同，仅保留1%首笔、3%单票上限；"
                    "盘中先过锁定主线的当日共振，再核日线资格、所属方法的闭合触发及Room/RR；趋势方法不另套15m MA20入场。"
            ),
            "plan_universe": strategy_scope,
        })
        strategy_key = str(strategy.get("key") or observation_strategy_router.OBSERVE)
        if strategy_key == observation_strategy_router.LEADER:
            plan.update({
                "plan_action": "龙头情绪首笔试仓",
                "target_position_pct": min(plan["target_position_pct"], 0.01),
                "max_position_pct": min(plan["max_position_pct"], 0.02),
                "v2_probe_position_pct": min(plan["v2_probe_position_pct"], 0.01),
                "reason": (
                    f"{strategy_scope}按龙头战法管理：只允许E5B开盘强承接、E5A开盘弱转强或E5二次转强时机证据，"
                    "首笔1%、单票上限2%；先过当日实时板块共振与日线龙头候选资格，涨停/急拉本身不构成买点。"
                ),
            })
        elif strategy_key == observation_strategy_router.TREND_520:
            plan.update({
                "plan_action": "520趋势条件试仓",
                "reason": (
                    f"{strategy_scope}按520战法管理：仅接受MA20受控回踩收复，或T-1新金叉的次日确认；"
                    "先过锁定主线当日共振，日线MACD/KDJ/量能及闭合5m确认；首笔1%、单票上限3%。"
                ),
            })
        elif strategy_key == observation_strategy_router.TREND_MA5:
            plan.update({
                "plan_action": "5日线趋势条件试仓",
                "reason": (
                    f"{strategy_scope}按5日线趋势法则管理：仅做上升趋势中日线MA5受控回踩；"
                    "先过锁定主线当日共振、日线量价至少3项及闭合5m收复；首笔1%、单票上限3%。"
                ),
            })
        else:
            plan.update({
                "plan_action": "趋势修复影子观察" if (strategy.get('daily_metrics') or {}).get('repair_watch') else "观察池待分类",
                "target_position_pct": 0.0,
                "max_position_pct": 0.0,
                "v2_probe_position_pct": 0.0,
                "v2_entry_enabled": False,
                "reason": f"{strategy_scope}：{strategy.get('daily_gate_reason') or '尚未取得日线资格'}；候选方法与修复条件继续跟踪，新增仓预算为0。",
            })
        if (
            strategy_key != observation_strategy_router.OBSERVE
            and "daily_qualified" in strategy
            and not strategy.get("daily_qualified")
        ):
            plan.update({
                "plan_action": f"{strategy.get('name') or '策略'}盘前确认待完成",
                "target_position_pct": 0.0,
                "max_position_pct": 0.0,
                "v2_probe_position_pct": 0.0,
                "v2_entry_enabled": False,
                "reason": (
                    f"已归类为{strategy.get('name') or strategy_key}，但{strategy.get('daily_gate_reason') or '盘前方法确认未通过'}；"
                    "当日只保留策略观察，不允许用盘中波动补足缺失的日线确认。"
                ),
            })
    else:
        plan["plan_universe"] = str(stock.get("plan_universe") or "核心股票池")

    metrics = strategy.get('daily_metrics') or {}
    if (metrics.get('resonance_policy') == 'company_board_identity_v2'
            and priority != 'P0'
            and strategy.get('daily_qualified')
            and (metrics.get('resonance_identity') or {}).get('status') != 'ready'):
        plan.update(plan_action='主线待核对，仅观察', target_position_pct=0.0,
                    max_position_pct=0.0, v2_probe_position_pct=0.0, v2_entry_enabled=False,
                    reason='日线合格，但计划主线不可执行：' + str((metrics.get('resonance_identity') or {}).get('reason')))
    plan["current_position_pct"] = current_pct
    plan["has_position"] = bool(holding.get("quantity"))
    if current_pct >= plan["target_position_pct"] - 1e-9 and plan["v2_entry_enabled"]:
        plan["reason"] += " 当前仓位已达到日内计划目标，V2即使触发也不再新增。"
    if state and priority == "P2" and "风险" in state:
        plan["v2_entry_enabled"] = False
        plan["plan_action"] = "等待结构修复"
        plan["reason"] = "状态与优先级不一致，先等待日线结构修复，禁止把盘中波动当作加仓理由。"
    return plan


def pct_plan(value):
    try:
        return f"{float(value or 0) * 100:.0f}%"
    except (TypeError, ValueError):
        return "0%"


def compact_meta(value, limit=5):
    if isinstance(value, (list, tuple, set)):
        text = "、".join(str(item) for item in list(value)[:limit] if str(item).strip())
    else:
        text = str(value or "")
    return re.sub(r"[|\r\n]+", "/", text).strip()


def stock_premarket_action(stock, macro_bias):
    q, t = stock["quote"], stock["tech"]
    plan = stock.get("premarket_plan") or premarket_position_plan(stock, macro_bias)
    plan_text = (
        f"盘前仓位：{plan['plan_action']}，目标 {pct_plan(plan['target_position_pct'])}、"
        f"上限 {pct_plan(plan['max_position_pct'])}、策略首笔 {pct_plan(plan['v2_probe_position_pct'])}。"
    )
    if not plan.get("v2_entry_enabled"):
        return plan_text + "不允许新增；盘中仅保留风控退出监控。"
    if (stock.get("strategy_contract") or {}).get("is_observation_strategy"):
        strategy = stock.get("strategy_contract") or {}
        return plan_text + f"方法：{strategy.get('name') or '待分类观察'}。{strategy.get('entry_rule') or '未确认前不得手工追价。'}"
    code = q["code"]
    if t["priority"] == "P0":
        return plan_text + "风险优先，未给出结构修复前不增加暴露。"
    if code in ("600589", "301071"):
        return plan_text + "情绪过热只看承接；不因高开或盘前压力价直接追买。"
    if code in ("605168", "688127"):
        return plan_text + "修复资格保留，V2必须确认15分钟Setup和5分钟执行结构。"
    if "科技" in macro_bias or code in ("600183", "000988", "688396", "688545"):
        return plan_text + "强趋势不追价，盘中只等V2回踩收复或突破回踩的已收盘确认。"
    return plan_text + "按所属三策略合同等待盘中板块共振与闭合确认，不预挂固定买卖价。"


POLICY_THEME_MAP = {
    "十五五/城市更新": ["十五五", "城市更新", "地下管网", "地下空间", "水利", "环保", "管材", "建材", "工程建设", "建筑装饰", "装配建筑"],
    "液冷/先进散热": ["液冷", "冷板", "换热器", "热管理", "先进散热", "散热材料"],
    "超硬材料/培育钻石": ["培育钻石", "超硬材料", "金刚石", "CVD钻石"],
    "贵金属/有色涨价": ["黄金", "白银", "贵金属", "金价", "银价", "铜价", "COMEX铜", "有色金属"],
    "新质生产力/AI": ["新质生产力", "AI", "人工智能", "算力", "数据中心", "服务器", "DeepSeek", "6G", "光通信", "CPO", "PCB"],
    "国产替代/半导体": ["国产替代", "半导体", "芯片", "先进封装", "光刻胶", "存储", "集成电路"],
    "低空/商业航天": ["低空经济", "商业航天", "卫星", "航天", "军工", "无人机"],
    "材料/新能源": ["PET铜箔", "铜箔", "玻纤", "复合材料", "固态电池", "超级电容"],
}

# 盘前主题不是一句新闻标题，也不是静态仓位建议。下面的规则只将不同
# 信息渠道形成的交集提升为盯盘优先级；真实买入仍由竞价、V2 和仓位计划决定。
PREMARKET_THEME_SOURCE_LABELS = {
    "fupan": "昨日复盘",
    "zaopan": "早盘必读",
    "zhangting": "异动雷达",
    "news": "新闻/7x24",
    "boards": "昨日强势板块",
}


def _source_texts_for_theme_evidence(ths_focus, news_groups, realtime, boards_top):
    fupan = ths_focus.get("fupan", {})
    zaopan = ths_focus.get("zaopan", {})
    zhangting = ths_focus.get("zhangting", {})
    return {
        "fupan": " ".join([
            str(fupan.get("main_themes") or ""),
            str(fupan.get("active_thread") or fupan.get("market_thread") or ""),
            str(fupan.get("summary") or ""),
            " ".join(str(item.get("theme") or "") for item in fupan.get("themes", [])),
        ]),
        "zaopan": " ".join(
            str(item)
            for items in zaopan.get("sections", {}).values()
            for item in items
        ),
        "zhangting": " ".join(
            " ".join([str(item.get("title") or ""), " ".join(item.get("stocks") or [])])
            for item in zhangting.get("items", [])
        ),
        "news": " ".join([
            " ".join(
                item.get("title", "") + " " + item.get("summary", "")
                for group in news_groups.values() for item in group
            ),
            " ".join(item.get("title", "") + " " + item.get("content", "") for item in realtime),
        ]),
        "boards": " ".join(str(item.get("f14") or item.get("name") or "") for item in boards_top[:12]),
    }


def _theme_keyword_hits(text, words):
    return unique_keep_order(word for word in words if word and word.lower() in str(text or "").lower())


def build_premarket_theme_evidence(ths_focus, news_groups, realtime, boards_top):
    """Build an auditable pre-open topic hierarchy from independent sources.

    A single headline may explain a move but is too weak to elevate a topic.
    We retain it as a clue, require two independent channels for a satellite
    watch, and three for a main-line candidate.  This deliberately does not
    create an executable signal.
    """
    source_texts = _source_texts_for_theme_evidence(ths_focus, news_groups, realtime, boards_top)
    evidence = []
    for theme, words in POLICY_THEME_MAP.items():
        sources = []
        hit_words = []
        for source, text in source_texts.items():
            hits = _theme_keyword_hits(text, words)
            if hits:
                sources.append(source)
                hit_words.extend(hits)
        source_count = len(sources)
        if not source_count:
            continue
        # Previous-day board leadership is mandatory for a "main" label. It
        # prevents a news-heavy topic from being mistaken for active money flow.
        if source_count >= 3 and "boards" in sources:
            tier = "MAIN"
            label = "主线候选"
        elif source_count >= 2:
            tier = "SATELLITE"
            label = "卫星观察"
        else:
            tier = "CLUE"
            label = "题材线索"
        evidence.append({
            "theme": theme,
            "tier": tier,
            "label": label,
            "source_count": source_count,
            "sources": sources,
            "source_labels": [PREMARKET_THEME_SOURCE_LABELS[source] for source in sources],
            "keywords": unique_keep_order(hit_words)[:6],
            "next_validation": (
                "09:25看同题材龙头竞价承接；09:30后确认板块强度、成交量与V2结构。"
                if tier == "MAIN" else
                "仅在09:25竞价与09:30板块共振后升级；未形成量价确认不介入。"
                if tier == "SATELLITE" else
                "仅保留资讯跟踪；不得据此加入候选池或生成买入。"
            ),
        })
    rank = {"MAIN": 0, "SATELLITE": 1, "CLUE": 2}
    return sorted(evidence, key=lambda item: (rank[item["tier"]], -item["source_count"], item["theme"]))


def theme_evidence_lines(theme_evidence, limit=5):
    if not theme_evidence:
        return ["- 当前资料未形成跨来源主题共识；盘前不把单条资讯升级为主线。"]
    lines = []
    for item in theme_evidence[:limit]:
        source_text = "、".join(item["source_labels"])
        keywords = "、".join(item["keywords"]) or "主题词命中"
        lines.append(
            f"- {item['label']}｜{item['theme']}｜证据 {item['source_count']} 路（{source_text}）｜"
            f"关键词：{keywords}｜验证：{item['next_validation']}"
        )
    return lines


def unique_keep_order(values):
    out = []
    seen = set()
    for value in values:
        value = str(value or "").strip()
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out


def clip_text(text, limit=150):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip("，。；、 ") + "…"


def stock_label(stock):
    q = stock["quote"]
    return f"{q['code']} {q['name']}"


def label_list(stocks, limit=5):
    if not stocks:
        return "无"
    labels = [stock_label(s) for s in stocks[:limit]]
    extra = len(stocks) - limit
    return "、".join(labels) + (f" 等{len(stocks)}只" if extra > 0 else "")


def first_zaopan_line(ths_focus, title):
    items = ths_focus.get("zaopan", {}).get("sections", {}).get(title, [])
    return items[0] if items else ""


def split_theme_words(text):
    words = []
    for part in re.split(r"[、,，/+｜|；;\s]+", str(text or "")):
        part = re.sub(r"(板块|概念|题材|Ⅲ|Ⅱ|Ⅰ)$", "", part.strip())
        if 2 <= len(part) <= 12 and part not in ("主流看点", "暂缺"):
            words.append(part)
    return words


def extract_strong_themes(ths_focus, boards_top=None):
    boards_top = boards_top or []
    themes = []
    fupan = ths_focus.get("fupan", {})
    themes.extend(split_theme_words(fupan.get("main_themes", "")))
    domestic = first_zaopan_line(ths_focus, "昨日国内行情回顾")
    match = re.search(r"板块题材上，(.+?)板块涨幅居前", domestic)
    if match:
        themes.extend(split_theme_words(match.group(1)))
    for item in ths_focus.get("zhangting", {}).get("items", [])[:8]:
        title = item.get("title", "")
        match = re.search(r"涨停雷达[:：](.+?)(?:\s+|触及涨停|$)", title)
        if match:
            themes.extend(split_theme_words(match.group(1)))
        elif "涨停复盘" in title:
            themes.extend(split_theme_words(title.replace("涨停复盘：", "")))
    themes.extend(str(x.get("f14") or "") for x in boards_top[:8])
    return unique_keep_order(themes)


def infer_market_mood(ths_focus, global_quotes):
    domestic = first_zaopan_line(ths_focus, "昨日国内行情回顾")
    mood = "中性震荡"
    if re.search(r"超4\d{3}.*个股下跌|近4\d{3}.*个股下跌|集体下跌|成交额.*缩量", domestic):
        mood = "偏防守，赚钱效应收缩"
    if re.search(r"超3\d{3}.*个股上涨|集体上涨|放量", domestic):
        mood = "偏修复，关注量能延续"
    if "科创50指数涨" in domestic and ("深证成指跌" in domestic or "创业板指跌" in domestic):
        mood = "指数分化，科创/硬科技强于多数个股"
    nas = next((x for x in global_quotes if "纳斯达克" in x.get("name", "")), None)
    if nas and nas.get("pct", 0) <= -0.5:
        mood += "；隔夜科技风险偏好偏弱"
    elif nas and nas.get("pct", 0) >= 0.5:
        mood += "；隔夜科技风险偏好偏强"
    return mood


def load_paper_snapshot():
    path = BASE_DIR / "web_dashboard" / "data" / "runtime" / "paper_trading.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def paper_position_labels(paper_snapshot, limit=5):
    positions = (paper_snapshot or {}).get("positions") or []
    labels = []
    for pos in positions[:limit]:
        symbol = pos.get("symbol") or pos.get("代码") or "-"
        name = pos.get("name") or pos.get("名称") or symbol
        labels.append(f"{symbol} {name}")
    extra = len(positions) - limit
    return "、".join(labels) + (f" 等{len(positions)}只" if extra > 0 else "")


def paper_account_summary(paper_snapshot):
    paper_snapshot = paper_snapshot or {}
    positions = paper_snapshot.get("positions") or []
    account = paper_snapshot.get("account") or {}
    if not positions:
        return "模拟盘当前无持仓。"
    total_assets = account.get("total_assets") or account.get("total_amount")
    position_pct = account.get("position_pct")
    pnl = account.get("unrealized_pnl")
    return (
        f"模拟盘持仓 {len(positions)}只（{paper_position_labels(paper_snapshot, 5)}）；"
        f"总资产 {base.f2(total_assets)}，仓位 {base.pct(position_pct)}，浮盈亏 {base.f2(pnl)}。"
    )


def build_premarket_conclusion(stocks, macro_bias, macro_bullets, ths_focus, boards_top, opportunities, opportunity_theme_keys, paper_snapshot=None):
    p0 = [s for s in stocks if s["tech"]["priority"] == "P0"]
    p1 = [s for s in stocks if s["tech"]["priority"] == "P1"]
    p2 = [s for s in stocks if s["tech"]["priority"] == "P2"]
    green = [s for s in stocks if s["tech"]["color"] == "🟢"]
    yellow = [s for s in stocks if s["tech"]["color"] == "🟡"]
    red = [s for s in stocks if s["tech"]["color"] == "🔴"]
    direct = [s for s in stocks if stock_focus_signal(s, ths_focus).startswith("🔴")]
    mapped = [s for s in stocks if stock_focus_signal(s, ths_focus).startswith("🟡")]
    hot = [s for s in stocks if "一致亢奋" in s.get("community_temp", "")]
    themes = extract_strong_themes(ths_focus, boards_top)
    theme_text = "、".join(themes[:8]) or "同花顺早盘/复盘暂未形成单一主线"
    domestic = first_zaopan_line(ths_focus, "昨日国内行情回顾")
    fupan = ths_focus.get("fupan", {})
    zaopan = ths_focus.get("zaopan", {})
    if zaopan.get("stale"):
        zaopan_date = f"今日未更新（返回{zaopan.get('date') or '日期未识别'}，目标{zaopan.get('expected_date') or REPORT_DATE.replace('-', '')}）"
    elif zaopan.get("error"):
        zaopan_date = f"不可用（目标{zaopan.get('expected_date') or REPORT_DATE.replace('-', '')}）"
    else:
        zaopan_date = zaopan.get("date") or "日期未识别"
    fupan_date = fupan.get("date") or "日期未识别"

    lines = [
        f"数据口径：同花顺早盘 {zaopan_date}，复盘 {fupan_date}；本段由当天早盘/复盘/异动/自选股状态动态生成。",
        f"全球风险偏好：{macro_bias}。",
        f"市场情绪：{infer_market_mood(ths_focus, [])}。{clip_text(domestic, 130) if domestic else '昨日A股回顾暂缺，开盘后以指数和量能校验。'}",
        f"今日主线：{theme_text}。同花顺复盘主流看点为 {fupan.get('main_themes') or '暂缺'}。",
        f"自选股优先级：P0 {len(p0)}只（{label_list(p0, 4)}）；P1 {len(p1)}只（{label_list(p1, 5)}）；P2 {len(p2)}只（{label_list(p2, 4)}）。",
        paper_account_summary(paper_snapshot),
        f"状态结构：强趋势 {len(green)}只，关键位/弱修复 {len(yellow)}只，风险 {len(red)}只；今天先处理风险和弱修复确认，强趋势只做回踩不破或放量站回。",
    ]
    if direct:
        lines.append(f"同花顺直接点名：{label_list(direct, 4)}，开盘重点看量能承接，不因点名直接追价。")
    elif mapped:
        lines.append(f"题材映射到自选：{label_list(mapped, 5)}；题材只提高盯盘优先级，买卖仍等价格和量能确认。")
    if hot:
        lines.append(f"社区温度过热：{label_list(hot, 4)}，若高开无量或冲压力回落，优先防一致预期兑现。")
    if opportunities:
        opp_labels = "、".join(f"{row['code']} {row['name']}" for row in opportunities[:5])
        lines.append(f"全市场机会池：盘前筛出 {len(opportunities)} 只候选（{opp_labels}）；只作为替代观察，9:25竞价与9:30后VWAP确认后才进入模拟盘。")
    else:
        theme_keys = "、".join((opportunity_theme_keys or themes)[:8])
        lines.append(f"全市场机会池：盘前严格筛选暂未给出候选；关注主题为 {theme_keys or '当日强势题材'}，9:25集合竞价后重新筛。")
    if macro_bullets:
        lines.append(f"宏观/海外补充：{clip_text(macro_bullets[0], 130)}")
    lines.append("今日执行：集合竞价后先看指数强弱、题材承接和个股竞价量；弱票不补仓摊平，强票不追急拉，所有加仓价只作为条件价。")
    return lines


def premarket_entry_alert_lines(stocks, opportunities, ths_focus, limit=8):
    alerts = []
    for s in stocks:
        tech = s.get("tech") or {}
        plan = s.get("premarket_plan") or {}
        if tech.get("priority") == "P0" or not plan.get("v2_entry_enabled", True):
            continue
        q = s.get("quote") or {}
        repair = tech.get("repair")
        defense = tech.get("defense")
        pressure = tech.get("pressure")
        if not repair or not defense:
            continue
        focus = stock_focus_signal(s, ths_focus)
        priority = tech.get("priority") or "P2"
        label = "重点加仓观察" if priority == "P1" else "低优先观察"
        alerts.append({
            "source": "自选/持仓",
            "code": q.get("code"),
            "name": q.get("name"),
            "priority": priority,
            "label": f"{label}｜计划单次{pct_plan(plan.get('v2_probe_position_pct'))}",
            "current": q.get("close"),
            "trigger": repair,
            "invalid": defense,
            "pressure": pressure,
            "confirm": "仅作日线分类与资格参考；盘中必须由所属策略完成板块共振及大/小周期闭合确认",
            "action": f"盘前计划目标{pct_plan(plan.get('target_position_pct'))}/上限{pct_plan(plan.get('max_position_pct'))}；V2未触发前不提前买。专题：{focus}",
        })
    for row in opportunities or []:
        alerts.append({
            "source": "全市场机会池",
            "code": row.get("code"),
            "name": row.get("name"),
            "priority": "P1" if str(row.get("status", "")).startswith("🟢") else "P2",
            "label": "候选入场观察",
            "current": (row.get("quote") or {}).get("close"),
            "trigger": row.get("trigger_price"),
            "invalid": row.get("invalid_price"),
            "pressure": (row.get("tech") or {}).get("pressure"),
            "confirm": "集合竞价承接 + 板块共振 + 开盘后站稳触发价，不追急拉",
            "action": row.get("action"),
        })
    rank = {"P1": 0, "P2": 1, "P0": 2}
    alerts.sort(key=lambda x: (rank.get(x.get("priority"), 9), x.get("source") != "自选/持仓"))
    lines = []
    if not alerts:
        lines.append("- 暂无满足纪律门槛的入场/加仓提醒；开盘前不主动放宽条件。")
        return lines
    lines.append("- 盘前只给条件价，不给无条件买入；集合竞价和开盘后量价确认是最终门槛。")
    lines.append("")
    lines.append("| 来源 | 代码 | 名称 | 优先级 | 现价 | 入场/加仓触发 | 失效价 | 压力/减仓 | 确认条件 | 动作 |")
    lines.append("|---|---|---|---|---:|---:|---:|---:|---|---|")
    for item in alerts[:limit]:
        lines.append(
            f"| {item['source']} | {item['code']} | {item['name']} | {item['priority']} | "
            f"{base.f2(item.get('current'))} | {base.f2(item.get('trigger'))} | {base.f2(item.get('invalid'))} | "
            f"{base.f2(item.get('pressure'))} | {item['confirm']} | {item['action']} |"
        )
    return lines


def watchlist_theme_mapping_line(stocks, ths_focus):
    direct = [s for s in stocks if stock_focus_signal(s, ths_focus).startswith("🔴")]
    mapped = [s for s in stocks if stock_focus_signal(s, ths_focus).startswith("🟡")]
    if direct:
        return f"- 映射到当前自选：同花顺直接点名 {label_list(direct, 6)}；优先看开盘量能承接。"
    if mapped:
        return f"- 映射到当前自选：{label_list(mapped, 8)}；仅代表题材承接验证，不能单独触发买卖。"
    return "- 映射到当前自选：当前“我的股票”未出现明确直接点名，只按各自关键位和量能执行。"


def paper_position_table_lines(paper_snapshot):
    paper_snapshot = paper_snapshot or {}
    positions = paper_snapshot.get("positions") or []
    account = paper_snapshot.get("account") or {}
    lines = []
    lines.append("## 九、模拟账户持仓盘前摘要")
    if positions:
        lines.append(
            f"- 账户：总资产 {base.f2(account.get('total_assets') or account.get('total_amount'))}；"
            f"可用现金 {base.f2(account.get('cash'))}；持仓市值 {base.f2(account.get('market_value'))}；"
            f"仓位 {base.pct(account.get('position_pct'))}；浮盈亏 {base.f2(account.get('unrealized_pnl'))}。"
        )
        lines.append("- 规则：A股普通股票按 T+1，今日买入不增加当日可卖；盘中持仓票继续参与风控信号。")
        lines.append("")
        lines.append("| 代码 | 名称 | 持仓 | 可卖 | 成本 | 现价 | 市值 | 当日盈亏 | 当日收益率 | 浮盈亏 | 收益率 | 持仓天数 |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        for pos in positions:
            holding_days = pos.get("holding_days")
            holding_text = f"{holding_days}天" if holding_days is not None else "-"
            lines.append(
                f"| {pos.get('symbol')} | {pos.get('name')} | {pos.get('quantity', 0)} | {pos.get('sellable', 0)} | "
                f"{base.f2(pos.get('avg_cost'))} | {base.f2(pos.get('last_price'))} | {base.f2(pos.get('market_value'))} | "
                f"{base.f2(pos.get('day_pnl'))} | {base.pct(pos.get('day_pnl_pct'))} | "
                f"{base.f2(pos.get('unrealized_pnl'))} | {base.pct(pos.get('unrealized_pnl_pct'))} | {holding_text} |"
            )
    else:
        lines.append("- 当前模拟账户无持仓；盘中只有可执行信号通过模拟撮合后才会生成持仓。")
    return lines


def premarket_theme_keywords(ths_focus, news_groups, realtime, boards_top):
    theme_evidence = build_premarket_theme_evidence(ths_focus, news_groups, realtime, boards_top)
    keys = []
    # A one-source topic is informational only. Only multi-source themes may
    # contribute policy keywords to the candidate radar.
    for item in theme_evidence:
        if item["tier"] != "CLUE":
            theme = item["theme"]
            words = POLICY_THEME_MAP[theme]
            keys.append(theme)
            keys.extend(words)
    keys.extend(intra.active_theme_keywords(boards_top))
    keys.extend(str(x.get("f14") or "") for x in boards_top[:12])
    return unique_keep_order(keys)


def policy_theme_hits(theme_text, theme_keys):
    hits = [key for key in theme_keys if key and key in theme_text]
    if "十五五/城市更新" in theme_keys and any(word in theme_text for word in ("工程", "建材", "环保", "水利", "管材", "建筑", "基建", "地下")):
        hits.append("十五五/城市更新")
    if "新质生产力/AI" in theme_keys and any(word in theme_text for word in ("通信", "电子", "计算机", "光模块", "PCB", "服务器", "数据")):
        hits.append("新质生产力/AI")
    if "国产替代/半导体" in theme_keys and any(word in theme_text for word in ("半导体", "芯片", "电子", "材料", "设备")):
        hits.append("国产替代/半导体")
    if "低空/商业航天" in theme_keys and any(word in theme_text for word in ("航天", "航空", "军工", "无人机", "卫星")):
        hits.append("低空/商业航天")
    if "材料/新能源" in theme_keys and any(word in theme_text for word in ("材料", "电池", "铜箔", "玻纤", "金刚石", "钻石")):
        hits.append("材料/新能源")
    return unique_keep_order(hits)


def premarket_candidate_score(q, theme_keys, board_text, theme_evidence=None):
    close = q["close"]
    high = q.get("high") or close
    low = q.get("low") or close
    open_price = q.get("open") or close
    pct_chg = q.get("pct", 0)
    amount = intra.amount_yi(q)
    pos = (close - low) / (high - low) * 100 if high > low else 50
    theme_text = f"{q.get('industry', '')} {q.get('concepts', '')} {q.get('name', '')}"
    matched = policy_theme_hits(theme_text, theme_keys)
    board_hit = bool(q.get("industry") and q.get("industry") in board_text)

    score = 0
    score += min(4.5, amount / 30)
    if 0.5 <= pct_chg <= 7.5:
        score += 2.0
    elif 0 <= pct_chg < 0.5:
        score += 0.8
    elif pct_chg > 8.5:
        score -= 2.0
    elif pct_chg < -1:
        score -= 2.5
    if 55 <= pos <= 92:
        score += 1.8
    elif pos > 96:
        score -= 1.0
    elif pos < 35:
        score -= 1.2
    if close >= open_price:
        score += 0.7
    if matched:
        score += 2.8 + min(1.2, len(matched) * 0.35)
        evidence_by_theme = {item["theme"]: item for item in (theme_evidence or [])}
        # Cross-source confirmation has a deliberately small influence. It
        # orders the watch queue; it must never substitute for an intraday
        # execution confirmation.
        if any(evidence_by_theme.get(hit, {}).get("tier") == "MAIN" for hit in matched):
            score += 0.7
        elif any(evidence_by_theme.get(hit, {}).get("tier") == "SATELLITE" for hit in matched):
            score += 0.3
    if board_hit:
        score += 1.4
    if amount < 8:
        score -= 3.5
    return score, matched, board_hit, pos


def opportunity_axes(q, tech, matched, board_hit, pos):
    amount = intra.amount_yi(q)
    recognition = "green" if amount >= 50 or len(matched) >= 2 or (matched and board_hit) else "yellow" if amount >= 12 or matched else "red"
    if q.get("pct", 0) > 8.5 or pos > 96:
        position = "red"
    elif q.get("pct", 0) >= 0 and pos >= 55 and tech.get("priority") != "P0":
        position = "green"
    elif tech.get("priority") != "P0":
        position = "yellow"
    else:
        position = "red"
    upside = (tech.get("pressure", q["close"]) - q["close"]) / q["close"] if q.get("close") else 0
    odds = "green" if position == "green" and upside >= 0.02 else "yellow" if position != "red" and upside >= 0.005 else "red"
    return {"recognition": recognition, "position": position, "odds": odds}


def axis_text_from_axes(axes):
    return (
        f"辨识度{intra.axis_mark(axes['recognition'])}｜"
        f"位置{intra.axis_mark(axes['position'])}｜"
        f"赔率{intra.axis_mark(axes['odds'])}"
    )


def opportunity_status(axes):
    if axes["recognition"] == "green" and axes["position"] == "green" and axes["odds"] in ("green", "yellow"):
        return "🟢 盘前高辨识度候选"
    if axes["recognition"] in ("green", "yellow") and axes["position"] != "red":
        return "🟡 盘前等待确认"
    return "🔴 暂不介入"


def opportunity_action(row):
    q = row["quote"]
    trigger = row["trigger_price"]
    invalid = row["invalid_price"]
    if row["status"].startswith("🟢"):
        return (
            f"盘中只等集合竞价后站稳 {base.f2(trigger)} 或回踩VWAP不破；"
            f"有效再小仓模拟，跌破 {base.f2(invalid)} 失效，急拉不追。"
        )
    if row["status"].startswith("🟡"):
        return f"先观察板块和量能共振，未站上 {base.f2(trigger)} 不介入；跌破 {base.f2(invalid)} 删除观察。"
    return "题材或位置不满足，只保留盘面跟踪，不进入模拟盘。"


def select_premarket_opportunities(stocks, boards_top, ths_focus, news_groups, realtime, limit=8):
    existing_codes = {s["quote"]["code"] for s in stocks}
    theme_evidence = build_premarket_theme_evidence(ths_focus, news_groups, realtime, boards_top)
    theme_keys = premarket_theme_keywords(ths_focus, news_groups, realtime, boards_top)
    board_text = " ".join(str(x.get("f14") or "") for x in boards_top[:15])
    raw_rows = []
    for fid, size in (("f6", 220), ("f3", 160)):
        raw_rows.extend(intra.fetch_market_scan_rows(fid, size))

    seen = {}
    for raw in raw_rows:
        q = intra.em_row_to_quote(raw)
        if not q or q["code"] in existing_codes:
            continue
        name = q.get("name", "")
        if "ST" in name or q["close"] <= 0:
            continue
        score, matched, board_hit, pos = premarket_candidate_score(q, theme_keys, board_text, theme_evidence)
        if score < 5.6 or not (matched or board_hit):
            continue
        old = seen.get(q["code"])
        if not old or score > old["score"]:
            seen[q["code"]] = {"quote": q, "score": score, "matched": matched, "board_hit": board_hit, "pos": pos}

    ranked = sorted(seen.values(), key=lambda x: (-x["score"], -intra.amount_yi(x["quote"]), -x["quote"]["pct"]))[:18]
    opportunities = []
    for item in ranked:
        q = item["quote"]
        try:
            daily = base.fetch_sohu_daily(q["code"])
            tech = base.trend_text(daily, q) if daily else {}
        except Exception:
            tech = {}
        if tech.get("priority") == "P0":
            continue
        axes = opportunity_axes(q, tech, item["matched"], item["board_hit"], item["pos"])
        status = opportunity_status(axes)
        high = q.get("high") or q["close"]
        low = q.get("low") or q["close"]
        prev = q.get("prev_close") or q["close"]
        trigger = max(q["close"], min(high, q["close"] * 1.015))
        invalid = max(low, tech.get("defense") or low, prev * 0.985 if prev else low)
        focus = "、".join(item["matched"][:4]) if item["matched"] else f"板块共振：{q.get('industry') or '行业'}"
        if item["board_hit"] and "板块共振" not in focus:
            focus += f"｜板块共振：{q.get('industry') or '-'}"
        row = {
            "code": q["code"],
            "name": q.get("name") or q["code"],
            "quote": q,
            "tech": tech,
            "score": item["score"],
            "status": status,
            "trigger_price": trigger,
            "invalid_price": invalid,
            "axes": axes,
            "focus": focus,
            "theme_evidence": [
                evidence_item for evidence_item in theme_evidence
                if evidence_item["theme"] in item["matched"] and evidence_item["tier"] != "CLUE"
            ],
        }
        row["action"] = opportunity_action(row)
        opportunities.append(row)
        time.sleep(0.03)
        if len(opportunities) >= limit:
            break
    return opportunities, theme_keys


def opportunity_table_lines(opportunities):
    lines = []
    for row in opportunities:
        q = row["quote"]
        evidence_label = "、".join(item["label"] for item in row.get("theme_evidence", [])[:2])
        framework = row["focus"] + (f"｜{evidence_label}" if evidence_label else "｜板块/量价待开盘验证")
        lines.append(
            f"| {row['code']} | {row['name']} | {row['status']} | {base.f2(q.get('close'))} | {base.pct(q.get('pct'))} | "
            f"开盘后确认 | {base.f2(row['trigger_price'])} | {base.f2(row['invalid_price'])} | {intra.amount_yi(q):.1f}亿 | "
            f"{axis_text_from_axes(row['axes'])} | {framework}｜评分{row['score']:.1f} | {row['action']} |"
        )
    return lines


def make_report(news_groups, realtime, global_quotes, stocks, boards_top, macro_bias, macro_bullets, ths_focus, opportunities=None, opportunity_theme_keys=None, paper_snapshot=None, observation_summary=None):
    now = base.current_datetime().strftime("%Y-%m-%d %H:%M:%S")
    opportunities = opportunities or []
    opportunity_theme_keys = opportunity_theme_keys or []
    zaopan = ths_focus.get("zaopan", {})
    fupan = ths_focus.get("fupan", {})
    zhangting = ths_focus.get("zhangting", {})
    special = ths_focus.get("bidu_special", {})
    theme_evidence = build_premarket_theme_evidence(ths_focus, news_groups, realtime, boards_top)
    lines = []
    lines.append(f"# 同花顺我的股票盘前全面分析｜{REPORT_DATE}\n")
    lines.append(f"- 生成时间：{now}（Asia/Shanghai）")
    lines.append("- 说明：基于同花顺早盘必读/复盘/异动观察/投资日历与右侧特色模块、同花顺发现菜单资讯流、同花顺个股特色数据/社区、全球市场动态与昨日收盘结构；仅为交易计划参考，不构成投资建议。")
    lines.append("- 核心框架：情绪、筹码、时间 + 三周期 + 顺大势逆小势。")
    lines.append("- 职责边界：盘前锁定股票分类、交易主线、方法与仓位；盘中按日线锚点或龙头转强规则独立计算入场，保留120m结构风控与既有退出保护，不用旧版固定价格下单。")
    watchlist_codes = "、".join(f"{s['quote']['code']} {s['quote'].get('name') or ''}".strip() for s in stocks)
    lines.append(f"- 自选确认：{base.watchlist_sync_summary()} 本次盘前行情校验 {len(stocks)} 只；核心与全自选观察池均进入策略覆盖。")
    lines.append("")
    lines.append("## 一、盘前结论")
    for b in build_premarket_conclusion(stocks, macro_bias, macro_bullets, ths_focus, boards_top, opportunities, opportunity_theme_keys, paper_snapshot):
        lines.append(f"- {b}")
    lines.append("")
    lines.append("## 二、主题证据链与开盘验证")
    lines.append("- 使用方式：主题证据链只排列盯盘顺序，不改变盘前仓位计划，更不能替代策略正式买入信号。昨日板块数据均标注为上一交易日，不以盘前新闻假定当日资金已流入。")
    lines.append("- 证据口径：机构净买与普通席位净买分开记录，普通席位不得标成机构抢筹；隔夜商品涨价、海外指数上涨、高开幅度和整点成交额仅用于情景验证，不直接授权仓位或买入。")
    lines.extend(theme_evidence_lines(theme_evidence))
    lines.append("")
    lines.append("### 全自选观察池与策略计划范围")
    lines.append("- 覆盖同花顺全部非核心自选分组；每只均完成行情、日线结构、策略分类与仓位资格判断。盘前行情通常是上一交易日收盘快照，09:25后才验证竞价。")
    lines.append("- 当日盘中执行顺序：先验证盘前锁定主线的实时共振（涨幅≥0.80%、连续刷新、领涨承接），再验证日线资格。趋势法用日线MA5/MA20锚点和闭合5m触发；龙头用开盘承接或15m转强配合有时效的5m突破；共同保留120m风险、量能、Room/RR和仓位纪律。")
    lines.extend(base.emotion_leader_summary_lines(stocks))
    plan_group_stocks = [stock for stock in stocks if stock.get("observation_plan_group")]
    if plan_group_stocks:
        lines.append(
            "- 计划层："
            + "、".join(f"{stock['quote']['code']} {stock['quote']['name']}" for stock in plan_group_stocks)
            + " 已纳入当日策略计划；未分类标的仓位为0，已分类标的仍须按板块共振→日线策略资格→V2已收盘时机的顺序通过才可进入模拟盘。"
        )
        for strategy_key, title in (
            (observation_strategy_router.LEADER, "龙头战法"),
            (observation_strategy_router.TREND_520, "520战法"),
            (observation_strategy_router.TREND_MA5, "趋势5日线法则"),
        ):
            members = [
                f"{stock['quote']['code']} {stock['quote']['name']}"
                for stock in plan_group_stocks
                if (stock.get("strategy_contract") or {}).get("key") == strategy_key
            ]
            if members:
                meta = observation_strategy_router.STRATEGY_META[strategy_key]
                formal_signal = observation_strategy_router.formal_entry_signal(strategy_key)
                lines.append(
                    f"- {meta['style']}｜{title}：{'、'.join(members)}。"
                    f"正式信号：{formal_signal}；先验证当日实时板块共振与日线策略资格，V2仅作为已收盘时机证据。"
                )
        unclassified = [
            f"{stock['quote']['code']} {stock['quote']['name']}"
            for stock in plan_group_stocks
            if (stock.get("strategy_contract") or {}).get("key") == observation_strategy_router.OBSERVE
            and not ((stock.get('strategy_contract') or {}).get('daily_metrics') or {}).get('repair_watch')
        ]
        repairs = [f"{s['quote']['code']} {s['quote']['name']}" for s in plan_group_stocks
                   if ((s.get('strategy_contract') or {}).get('daily_metrics') or {}).get('repair_watch')]
        if repairs:
            lines.append('- 趋势票｜520修复影子观察：' + '、'.join(repairs) + '；验证双线收复、MACD/KDJ、放量及锁定主线，不产生绿色卡或模拟订单。')
        if unclassified:
            lines.append("- 待分类覆盖：" + "、".join(unclassified) + "；没有明确方法资格，不开放新增仓。")
    lines.append("- 全部分组均进入计划与盯盘；但只有已归入龙头、520或趋势5日线合同，且当日实时板块共振、日线资格与策略时机证据同时闭合的标的，才会生成模拟订单。")
    lines.extend(base.observation_group_summary_lines(observation_summary, limit=8))
    lines.append("")
    lines.append("## 三、入场/加仓提醒")
    lines.extend(premarket_entry_alert_lines(stocks, opportunities, ths_focus))
    lines.append("")
    lines.append("## 四、同花顺早盘/复盘/异动/日历")
    if zaopan.get("error"):
        lines.append(f"- 早盘必读不可用：{zaopan['error']}。")
    else:
        lines.append(f"### 早盘必读（{zaopan.get('date') or '日期未识别'}）")
        for title in ("隔夜海外行情动态", "昨日国内行情回顾", "重大新闻汇总", "公司公告", "券商观点", "今日重点关注的财经数据与事件"):
            items = zaopan.get("sections", {}).get(title, [])
            if not items:
                continue
            lines.append(f"- {title}：")
            for item in items[:6]:
                lines.append(f"  - {item}")
        if zaopan.get("suspensions"):
            rows = [" / ".join(x) for x in zaopan["suspensions"][:5]]
            lines.append("- 今日停复牌：" + "；".join(rows))
        if zaopan.get("block_trades"):
            rows = [" / ".join(x) for x in zaopan["block_trades"][:5]]
            lines.append("- 大宗交易观察：" + "；".join(rows))
    lines.append("")
    lines.append("### 复盘映射")
    if fupan.get("error"):
        lines.append(f"- 复盘读取失败：{fupan['error']}。")
    else:
        lines.append(f"- 综合描述：{fupan.get('summary') or '暂缺'}")
        lines.append(f"- 主流看点：{fupan.get('main_themes') or '暂缺'}")
        lines.append(f"- 盘面脉络：{fupan.get('active_thread') or fupan.get('market_thread') or '暂缺'}")
        for theme in fupan.get("themes", [])[:4]:
            sample = "、".join(f"{x.get('code')} {x.get('name')} {x.get('pct')}" for x in theme.get("stocks", [])[:4])
            lines.append(f"- {theme.get('theme')}：{sample}")
    lines.append("")
    lines.append("### 异动观察")
    if zhangting.get("error"):
        lines.append(f"- 异动观察读取失败：{zhangting['error']}。")
    else:
        for item in zhangting.get("items", [])[:6]:
            stocks_text = f"；影响个股：{'、'.join(item.get('stocks', [])[:4])}" if item.get("stocks") else ""
            lines.append(f"- [{item['title']}]({item['url']})（{item.get('time','')}）{stocks_text}")
    lines.append("")
    lines.append("### 右侧特色模块/公告日历")
    if special.get("error"):
        lines.append(f"- 右侧特色模块读取失败：{special['error']}。")
    else:
        if special.get("today_tips"):
            tips = "；".join(f"{x['label']} {x['count']} {x['stocks']}" for x in special["today_tips"][:4])
            lines.append(f"- 今日特别提示：{tips}")
        if special.get("fund_inflow"):
            funds = "、".join(f"{x['code']} {x['name']} {x['amount']}亿" for x in special["fund_inflow"][:5])
            lines.append(f"- 右侧资金流入榜：{funds}")
        if special.get("limit_reasons"):
            lines.append("- 涨停股原因揭秘：" + "；".join(f"[{x['title']}]({x['url']})" for x in special["limit_reasons"][:5]))
        if special.get("calendar_links"):
            lines.append("- 公告日历入口：" + "、".join(f"[{x['title']}]({x['url']})" for x in special["calendar_links"][:5]))
    lines.append("")
    lines.append("## 五、同花顺发现菜单要闻")
    for group_name, items in news_groups.items():
        lines.append(f"### {group_name}")
        for item in items[:5]:
            summary = f"：{item['summary']}" if item.get("summary") else ""
            lines.append(f"- [{item['title']}]({item['url']}){summary}")
        lines.append("")
    lines.append("### 7×24 要闻精华")
    for item in realtime[:8]:
        when = f"{item.get('pubDate')} " if item.get("pubDate") else ""
        content = f"：{item.get('content')}" if item.get("content") else ""
        lines.append(f"- {when}[{item.get('title')}]({item.get('url')}){content}")
    lines.append("")
    lines.append("## 六、全球市场动态")
    lines.append("| 指数 | 最新 | 涨跌幅 | 时间 |")
    lines.append("|---|---:|---:|---|")
    for q in global_quotes:
        if "price" in q:
            lines.append(f"| {q['name']} | {base.f2(q['price'])} | {base.pct(q['pct'])} | {q.get('time','')} |")
        else:
            lines.append(f"| {q['name']} | - | - | {q.get('status','不可用')} |")
    lines.append("")
    lines.append("## 七、昨日强势方向对今日映射")
    lines.append("昨日强势板块：" + "、".join(f"{x.get('f14')} {base.pct(x.get('f3'))}" for x in boards_top[:10]))
    lines.append(watchlist_theme_mapping_line(stocks, ths_focus))
    lines.append(f"- 模拟盘持仓：{paper_account_summary(paper_snapshot)}")
    lines.append("")
    lines.append("## 八、全市场机会池")
    lines.append("- 用途：当持仓票陷入弱势时，提前准备更强替代观察；早盘只给候选，不给直接买入。")
    lines.append("- 筛选：同花顺早盘/复盘/异动/发现菜单主题 + 十五五/政策线 + 板块共振 + 成交额辨识度 + 日线位置不过热。")
    if opportunity_theme_keys:
        lines.append("- 今日主题关键词：" + "、".join(opportunity_theme_keys[:16]))
    if opportunities:
        lines.append("| 机会代码 | 机会名称 | 雷达状态 | 现价 | 涨跌 | VWAP | 触发价 | 失效价 | 成交额 | 三轴 | 框架依据 | 动作 |")
        lines.append("|---|---|---|---:|---:|---|---:|---:|---:|---|---|---|")
        lines.extend(opportunity_table_lines(opportunities))
    else:
        lines.append("- 暂无满足框架门槛的全市场替代机会；没有更强替代就以处理持仓风险和等待为主。")
    lines.append("")
    lines.extend(paper_position_table_lines(paper_snapshot))
    lines.append("")
    lines.append("## 十、自选股盘前操作与仓位计划")
    lines.append("| 代码 | 名称 | 计划范围 | 股票类型 | 交易策略 | 正式买入信号 | 策略键 | 日线策略资格 | 日线策略证据 | 允许时机证据 | 龙头形态 | 龙头双榜验证 | 昨日触板 | 昨日封板 | 策略入场纪律 | 策略退出纪律 | 策略依据 | 状态 | 优先级 | 行业 | 概念 | 同花顺专题映射 | 社区温度 | 当日计划 | 目标仓位 | 单票上限 | 策略首笔 | 防守 | 修复 | 压力 | 盘前建议 | 主线锁定 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---|")
    lines[-2] += ' 主线代码 | 主线状态 | 主线依据 | 双榜状态 | 完整概念 |'
    lines[-1] += '---|---|---|---|---|'
    for s in stocks:
        q, t = s["quote"], s["tech"]
        plan = s.get("premarket_plan") or premarket_position_plan(s, macro_bias, paper_snapshot)
        action = stock_premarket_action(s, macro_bias)
        focus_signal = stock_focus_signal(s, ths_focus)
        strategy = s.get("strategy_contract") or {}
        lines.append(
            f"| {q['code']} | {q['name']} | {s.get('plan_universe') or '核心股票池'} | {strategy.get('style') or '-'} | {strategy.get('name') or '-'} | {strategy.get('formal_entry_signal') or observation_strategy_router.formal_entry_signal(strategy)} | {strategy.get('key') or '-'} | {'通过' if strategy.get('daily_qualified') else '未通过'} | {observation_strategy_router.daily_evidence_text(strategy)} | {'、'.join(strategy.get('allowed_patterns') or []) or '-'} | {(strategy.get('daily_metrics') or {}).get('leader_profile') or '-'} | {'是' if (strategy.get('daily_metrics') or {}).get('emotion_pool_cross_verified') else '否'} | {'是' if (strategy.get('daily_metrics') or {}).get('prior_day_touched_limit') else '否'} | {'是' if (strategy.get('daily_metrics') or {}).get('prior_day_closed_limit') else '否'} | {strategy.get('entry_rule') or '-'} | {strategy.get('exit_rule') or '-'} | {strategy.get('reason') or '-'} | {t['color']} {t['state']} | {t['priority']} | "
            f"{compact_meta(q.get('industry')) or '-'} | {compact_meta(q.get('concepts') or q.get('concept')) or '-'} | "
            f"{focus_signal} | {s['community_temp']} | "
            f"{plan['plan_action']} | {pct_plan(plan['target_position_pct'])} | {pct_plan(plan['max_position_pct'])} | {pct_plan(plan['v2_probe_position_pct'])} | "
            f"{base.f2(t['defense'])} | {base.f2(t['repair'])} | {base.f2(t['pressure'])} | {action} | {'、'.join((strategy.get('daily_metrics') or {}).get('resonance_boards') or []) or '-'} |"
        )
        metrics = strategy.get('daily_metrics') or {}
        identity = metrics.get('resonance_identity') or {}
        lines[-1] += (f" {'、'.join(metrics.get('resonance_board_ids') or []) or '-'} | "
                      f"{identity.get('status') or 'pending_identity'} | {identity.get('reason') or '-'} | "
                      f"{metrics.get('emotion_pool_cross_status') or 'unavailable'} | "
                      f"{compact_meta(q.get('verified_concepts', q.get('concepts')), limit=200) or '-'} |")
    lines.append("")
    lines.append("## 十一、09:25-09:30 检查清单")
    lines.append("- 指数：上证/创业板/科创开盘是否共振，若创业板强于主板，科技成长承接更重要。")
    lines.append("- 量能：自选股竞价量是否显著大于昨日同时间，强势股高开必须有量，否则冲高回落概率上升。")
    check_themes = "、".join((extract_strong_themes(ths_focus, boards_top) or opportunity_theme_keys)[:8])
    lines.append(f"- 题材承接：把早盘必读/复盘/异动观察点出的{check_themes or '当日强势题材'}作为盘中承接验证项；有题材无量能不追。")
    lines.append("- 右侧特色数据：停复牌、大宗交易、今日特别提示、公告日历、资金流向若命中自选股或同赛道，提升到 P1/P0 复核。")
    lines.append("- 情绪：社区一致亢奋的票，开盘若无量高开，优先防回落；样本不足的票不因社区低关注而直接看多。")
    lines.append("- 风控：跌破防守位不补仓摊平；反抽压力位无量先减压。")
    lines.append("")
    lines.extend(
        mira_research_gate.markdown_section_from_text(
            kind="premarket",
            title=f"同花顺我的股票盘前全面分析｜{REPORT_DATE}",
            date=REPORT_DATE,
            generated=now,
            raw_text="\n".join(lines),
            row_count=len(stocks) + len(opportunities),
        )
    )
    return "\n".join(lines)


def lark_text(text):
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


FEISHU_CARD_MAX_BYTES = 90 * 1024


def encode_lark_card(card, max_bytes=FEISHU_CARD_MAX_BYTES):
    """Serialize a card below Feishu's hard 100KB limit, preserving every section."""
    compact = json.loads(json.dumps(card, ensure_ascii=False))
    data = json.dumps(compact, ensure_ascii=False).encode("utf-8")
    suffix = "\n- 内容较长，已压缩；完整清单见本地仪表盘。"
    while len(data) > max_bytes:
        candidates = []
        for element in compact.get("card", {}).get("elements", []):
            text = element.get("text") if isinstance(element, dict) else None
            content = text.get("content") if isinstance(text, dict) else None
            if isinstance(content, str) and len(content) > 320:
                candidates.append((len(content), text))
        if not candidates:
            raise ValueError(f"飞书卡片压缩后仍超过限制：{len(data)} bytes")
        _length, target = max(candidates, key=lambda item: item[0])
        content = target["content"]
        keep = max(256, int(len(content) * 0.72))
        target["content"] = content[:keep].rstrip() + suffix
        data = json.dumps(compact, ensure_ascii=False).encode("utf-8")
    return data


def send_feishu(news_groups, realtime, global_quotes, stocks, boards_top, macro_bias, macro_bullets, ths_focus, opportunities=None, opportunity_theme_keys=None, paper_snapshot=None, observation_summary=None):
    if os.environ.get("A_SHARE_SKIP_FEISHU") == "1":
        return json.dumps({"code": 0, "msg": "skipped by A_SHARE_SKIP_FEISHU"}, ensure_ascii=False)
    opportunities = opportunities or []
    opportunity_theme_keys = opportunity_theme_keys or []

    global_line = " | ".join(
        f"{q['name']} {base.pct(q['pct'])}" for q in global_quotes if "pct" in q and any(k in q["name"] for k in ["纳斯达克", "标普", "恒生", "日经"])
    )
    top_news = []
    for group in ("财经要闻", "国际财经", "金融市场"):
        if news_groups.get(group):
            top_news.append(f"- {group}：{news_groups[group][0]['title']}")
    p0 = [s for s in stocks if s["tech"]["priority"] == "P0"]
    p1 = [s for s in stocks if s["tech"]["priority"] == "P1"]
    p2 = [s for s in stocks if s["tech"]["priority"] == "P2"]
    zaopan = ths_focus.get("zaopan", {})
    fupan = ths_focus.get("fupan", {})
    zhangting = ths_focus.get("zhangting", {})
    special = ths_focus.get("bidu_special", {})
    theme_evidence = build_premarket_theme_evidence(ths_focus, news_groups, realtime, boards_top)
    conclusion_lines = build_premarket_conclusion(
        stocks,
        macro_bias,
        macro_bullets,
        ths_focus,
        boards_top,
        opportunities,
        opportunity_theme_keys,
        paper_snapshot,
    )
    entry_lines = []
    for s in (p1 + p2)[:5]:
        q, t = s["quote"], s["tech"]
        entry_lines.append(
            f"🟢 **{q['code']} {q['name']}** `{t['priority']}`\n"
            f"现价 {base.f2(q.get('close'))}｜入场/加仓触发 **{base.f2(t.get('repair'))}**｜"
            f"失效 {base.f2(t.get('defense'))}｜压力 {base.f2(t.get('pressure'))}\n"
            f"确认：9:25竞价不弱，9:30后站稳修复位/VWAP，回踩不破且量能确认。"
        )
    for row in opportunities[:3]:
        q = row["quote"]
        entry_lines.append(
            f"🟡 **全市场候选｜{row['code']} {row['name']}**\n"
            f"现价 {base.f2(q.get('close'))}｜入场触发 **{base.f2(row.get('trigger_price'))}**｜"
            f"失效 {base.f2(row.get('invalid_price'))}\n"
            f"确认：集合竞价承接 + 板块共振 + 开盘后站稳触发价，不追急拉。"
        )
    entry_text = "\n\n".join(entry_lines[:7]) if entry_lines else "暂无满足纪律门槛的入场/加仓提醒。"

    morning_lines = []
    if zaopan.get("error"):
        morning_lines.append(f"- 早盘必读：{zaopan['error']}")
    else:
        for title in ("昨日国内行情回顾", "重大新闻汇总", "今日重点关注的财经数据与事件"):
            items = zaopan.get("sections", {}).get(title, [])
            if items:
                morning_lines.append(f"- {title}：{items[0]}")
        if zaopan.get("suspensions"):
            morning_lines.append("- 停复牌：" + "；".join(" / ".join(x) for x in zaopan["suspensions"][:3]))
    if fupan.get("main_themes"):
        morning_lines.append(f"- 复盘主流看点：{fupan['main_themes']}")
    if zhangting.get("items"):
        morning_lines.append("- 异动观察：" + "；".join(x["title"] for x in zhangting["items"][:3]))
    if special.get("today_tips"):
        morning_lines.append("- 今日特别提示：" + "；".join(f"{x['label']} {x['count']}" for x in special["today_tips"][:3]))
    if special.get("fund_inflow"):
        morning_lines.append("- 右侧资金流入：" + "、".join(f"{x['code']} {x['name']} {x['amount']}亿" for x in special["fund_inflow"][:4]))

    def group_line(group, limit=12):
        if not group:
            return "无"
        lines = [
            f"{s['tech']['color']} **{s['quote']['code']} {s['quote']['name']}**｜{s['tech']['state']}｜防守 {base.f2(s['tech']['defense'])}｜修复 {base.f2(s['tech']['repair'])}｜压力 {base.f2(s['tech']['pressure'])}"
            for s in group[:limit]
        ]
        if len(group) > limit:
            lines.append(f"其余 {len(group) - limit} 只见本地仪表盘完整计划。")
        return "\n".join(lines)

    focus_lines = []
    for s in (p0[:4] + p1[:8] + p2[:3]):
        q, t = s["quote"], s["tech"]
        focus_lines.append(
            f"{t['color']} **{q['code']} {q['name']}** `{t['priority']}`\n"
            f"专题：{stock_focus_signal(s, ths_focus)}\n"
            f"社区：{s['community_temp']}｜防守 {base.f2(t['defense'])}｜修复 {base.f2(t['repair'])}｜压力 {base.f2(t['pressure'])}\n"
            f"建议：{stock_premarket_action(s, macro_bias)}"
        )

    strategy_lines = []
    for s in (stock for stock in stocks if stock.get("observation_plan_group")):
        q = s["quote"]
        plan = s.get("premarket_plan") or {}
        strategy = s.get("strategy_contract") or {}
        strategy_lines.append(
            f"**{q['code']} {q['name']}**｜{strategy.get('style') or '未分类'}｜{strategy.get('name') or '待分类观察'}\n"
            f"正式信号：{strategy.get('formal_entry_signal') or observation_strategy_router.formal_entry_signal(strategy)}\n"
            f"计划：{plan.get('plan_action') or '-'}，首笔 {pct_plan(plan.get('v2_probe_position_pct'))}，上限 {pct_plan(plan.get('max_position_pct'))}\n"
            f"盘中条件：{strategy.get('entry_rule') or '不开放新增仓。'}"
        )

    opportunity_lines = []
    for row in opportunities[:5]:
        q = row["quote"]
        opportunity_lines.append(
            f"{row['status']} **{row['code']} {row['name']}**\n"
            f"主题：{row['focus']}｜"
            f"{('、'.join(item['label'] for item in row.get('theme_evidence', [])[:2]) or '板块/量价待开盘验证')}｜"
            f"成交额 {intra.amount_yi(q):.1f}亿｜{axis_text_from_axes(row['axes'])}\n"
            f"触发：{base.f2(row['trigger_price'])}｜失效：{base.f2(row['invalid_price'])}\n"
            f"动作：{row['action']}"
        )
    if not opportunity_lines:
        opportunity_lines.append("暂无满足框架门槛的全市场替代机会。")

    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"A股盘前全面分析｜{REPORT_DATE}"}},
            "elements": [
                lark_text("**盘前结论**\n" + "\n".join(f"- {x}" for x in conclusion_lines[:7])),
                {"tag": "hr"},
                lark_text(
                    "**主题证据链｜只决定盯盘优先级**\n"
                    "单条资讯与昨日强势板块都只作盯盘线索，不能继承为当日板块资格。"
                    "机构净买与普通席位净买分开记录；隔夜涨价、高开幅度和整点成交额不直接授权买入。"
                    "任何主题仍须过09:25竞价、当日实时板块量价及策略闭合确认。\n"
                    + "\n".join(theme_evidence_lines(theme_evidence, 4))
                ),
                lark_text(
                    "**全自选观察池与策略计划范围**\n"
                    + (
                        f"全量自选观察池已覆盖 {sum(1 for stock in stocks if stock.get('observation_plan_group'))}只；"
                        "每只都有策略分类与盘前资格。未分类为0仓位；已分类仍须按当日实时板块共振→日线策略资格→方法专属短周期闭合后才可生成模拟订单。\n"
                        if any(stock.get("observation_plan_group") for stock in stocks)
                        else "全自选观察池计划同步不可用，今日不授予观察池新增仓资格。\n"
                    )
                    + "执行顺序：锁定主线的当日共振→日线资格→方法专属触发→模拟订单。趋势法为日线MA5/MA20锚点加闭合5m，龙头为开盘承接/15m转强加有时效5m突破。昨日板块强弱不继承；09:25后再校验竞价。\n"
                    + "\n".join(base.emotion_leader_summary_lines(stocks, limit=8))
                    + "\n"
                    + "\n".join(base.observation_group_summary_lines(observation_summary, limit=7))
                ),
                lark_text("**观察池策略合同｜类型、方法与正式信号**\n" + "\n\n".join(strategy_lines[:8] or ["当日无纳入计划的观察池标的。"])),
                {"tag": "hr"},
                lark_text("**入场/加仓提醒**\n" + entry_text),
                {"tag": "hr"},
                lark_text(f"**全球市场**\n{global_line or '全球指数部分字段不可用'}"),
                {"tag": "hr"},
                lark_text("**同花顺早盘/复盘/异动/日历重点**\n" + "\n".join(morning_lines[:10] or ["早盘专题模块暂未返回有效内容"])),
                {"tag": "hr"},
                lark_text("**同花顺发现菜单要闻**\n" + "\n".join(top_news[:5])),
                {"tag": "hr"},
                lark_text(f"**P0 风险处理**\n{group_line(p0)}"),
                lark_text(f"**P1 重点盯盘**\n{group_line(p1)}"),
                lark_text(f"**P2 持有观察**\n{group_line(p2)}"),
                {"tag": "hr"},
                lark_text(
                    "**全市场机会池｜盘前候选**\n"
                    f"主题：{'、'.join(opportunity_theme_keys[:10]) or '暂无明确主题'}\n\n"
                    + "\n\n".join(opportunity_lines)
                ),
                {"tag": "hr"},
                lark_text("**重点个股卡片**\n" + "\n\n".join(focus_lines)),
                {"tag": "hr"},
                lark_text("09:25 后重点看集合竞价量能、高低开质量、同花顺早盘/复盘/异动点名题材是否有承接。仅为交易计划参考，不构成投资建议。"),
            ],
        },
    }
    data = encode_lark_card(card)
    last_error = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(FEISHU_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                text = resp.read().decode("utf-8", "ignore")
            payload = json.loads(text)
            code = payload.get("code", payload.get("StatusCode", 0))
            if code in (0, "0", None):
                return text
            raise FeishuPushError(text)
        except Exception as exc:
            last_error = exc
            delay = 4 * (attempt + 1) if "11232" in str(exc) else 1.5 * (attempt + 1)
            time.sleep(delay)
    raise RuntimeError(f"飞书推送失败：{last_error}")


def main():
    ths_focus = fetch_ths_discover_focus()
    news_groups = {
        "财经要闻": fetch_news_list("https://news.10jqka.com.cn/today_list/", "财经要闻"),
        "国内经济": fetch_news_list("https://news.10jqka.com.cn/region_list/", "国内经济"),
        "国际财经": fetch_news_list("https://news.10jqka.com.cn/guojicj_list/", "国际财经"),
        "金融市场": fetch_news_list("https://news.10jqka.com.cn/jrsc_list/", "金融市场"),
    }
    realtime = fetch_realtime_news()
    global_quotes = fetch_global_markets()
    stocks, boards_top, _ = fetch_stock_context()
    observation_details = base.read_observation_watchlist_details()
    observation_quotes = base.parse_tencent_quotes(observation_details.get("rows") or []) if observation_details.get("rows") else {}
    observation_summary = base.observation_group_summary(observation_details, observation_quotes)
    macro_bias, macro_bullets = summarize_macro(news_groups, realtime, global_quotes, ths_focus)
    global_risk_context = global_risk.infer_premarket_context(
        REPORT_DATE,
        global_quotes,
        macro_bias=macro_bias,
        ths_focus=ths_focus,
        now=base.current_datetime(),
    )
    # Seed the cross-session global path before the A-share open. This keeps
    # an overnight repair visible even when the first intraday tick is green.
    path_state = global_risk.record_intraday_path(
        BASE_DIR,
        REPORT_DATE,
        global_quotes,
        now=base.current_datetime(),
    )
    global_risk_context["intraday_path"] = path_state
    global_risk_context["repair_mode"] = (
        "preopen_repair_continuation_pending"
        if path_state.get("preopen_repair") or path_state.get("recovery_confirmed")
        else "normal"
    )
    global_risk.write_context(BASE_DIR, global_risk_context)
    macro_bullets = [global_risk.summary_line(global_risk_context)] + macro_bullets
    opportunities, opportunity_theme_keys = select_premarket_opportunities(stocks, boards_top, ths_focus, news_groups, realtime)
    paper_snapshot = load_paper_snapshot()
    for stock in stocks:
        stock["premarket_plan"] = premarket_position_plan(stock, macro_bias, paper_snapshot)
    report = make_report(news_groups, realtime, global_quotes, stocks, boards_top, macro_bias, macro_bullets, ths_focus, opportunities, opportunity_theme_keys, paper_snapshot, observation_summary)
    REPORT_PATH.write_text(report, encoding="utf-8")
    try:
        dashboard_result = dashboard.publish_report(REPORT_PATH)
    except Exception as exc:
        dashboard_result = {"error": str(exc)}
    feishu_result = send_feishu(news_groups, realtime, global_quotes, stocks, boards_top, macro_bias, macro_bullets, ths_focus, opportunities, opportunity_theme_keys, paper_snapshot, observation_summary)
    print(json.dumps({
        "report": str(REPORT_PATH),
        "dashboard": dashboard_result,
        "stocks": len(stocks),
        "observation_stocks": len(observation_details.get("rows") or []),
        "market_opportunities": [row["code"] for row in opportunities],
        "paper_positions": len((paper_snapshot or {}).get("positions") or []),
        "opportunity_theme_keys": opportunity_theme_keys[:16],
        "feishu_result": feishu_result,
        "macro_bias": macro_bias,
        "global_risk": global_risk_context,
        "ths_focus": {
            "zaopan_date": ths_focus.get("zaopan", {}).get("date"),
            "zaopan_expected_date": ths_focus.get("zaopan", {}).get("expected_date"),
            "zaopan_stale": bool(ths_focus.get("zaopan", {}).get("stale")),
            "zaopan_error": ths_focus.get("zaopan", {}).get("error"),
            "fupan_date": ths_focus.get("fupan", {}).get("date"),
            "zhangting_items": len(ths_focus.get("zhangting", {}).get("items", [])),
            "bidu_fund_rows": len(ths_focus.get("bidu_special", {}).get("fund_inflow", [])),
        },
        "p0": [s["quote"]["code"] for s in stocks if s["tech"]["priority"] == "P0"],
        "p1": [s["quote"]["code"] for s in stocks if s["tech"]["priority"] == "P1"],
        "p2": [s["quote"]["code"] for s in stocks if s["tech"]["priority"] == "P2"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
