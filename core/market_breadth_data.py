"""Independent breadth transport; no quote-cache, selection or order side effects."""
import hashlib
import json
import math
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime


SINA_BASE = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center."
MIN_UNIVERSE_SIZE = 4000
PAGE_SIZE = 100
BATCH_SIZE = 200
MAX_QUOTE_AGE_SECONDS = 120


def _read(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("gbk", "strict")


def _timeout(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("breadth_fetch_budget")
    return min(4, remaining)


def fetch_universe(current, deadline):
    """Require stable counts, exact pages and unique identities before filtering."""
    def count():
        return int(json.loads(_read(SINA_BASE + "getHQNodeStockCount?node=hs_a", _timeout(deadline))))

    total = count()
    if not MIN_UNIVERSE_SIZE <= total <= 10000:
        raise ValueError("universe_count_out_of_range")

    def page(number):
        rows = json.loads(_read(
            SINA_BASE + f"getHQNodeData?page={number}&num={PAGE_SIZE}&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=page",
            _timeout(deadline)))
        expected = min(PAGE_SIZE, total - (number - 1) * PAGE_SIZE)
        if not isinstance(rows, list) or len(rows) != expected:
            raise ValueError("universe_page_incomplete")
        return rows

    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(page, range(1, math.ceil(total / PAGE_SIZE) + 1)))
    rows = [row for items in pages for row in items]
    codes = [str(row.get("code") or "") for row in rows]
    if len(set(codes)) != total or count() != total:
        raise ValueError("universe_count_changed_or_duplicate")
    symbols = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        code = str(row.get("code") or "")
        if not re.fullmatch(r"(?:sh|sz|bj)\d{6}", symbol) or symbol[2:] != code:
            raise ValueError("universe_identity_invalid")
        # Match the existing Shanghai/Shenzhen A-share breadth, not ETFs or B shares.
        if re.fullmatch(r"(?:sh(?:60|68)|sz(?:00|30))\d{4}", symbol):
            symbols.append(symbol)
    if len(symbols) < MIN_UNIVERSE_SIZE:
        raise ValueError("universe_scope_incomplete")
    symbols.sort()
    return {"date": current.strftime("%Y-%m-%d"), "symbols": symbols,
            "source": "sina_hs_a_complete_list", "listed_total": total,
            "excluded_count": total - len(symbols), "scope": "SH_SZ_A",
            "hash": hashlib.sha256(",".join(symbols).encode()).hexdigest()}


def fetch_snapshot(cache, current, parse_quotes):
    """Fetch fresh Tencent prices against a separately verified daily universe."""
    start = time.monotonic()
    deadline = start + 20
    result = {"source": "tencent_full_universe", "scope": "SH_SZ_A", "rows": [],
              "failed_pages": [], "page_errors": {}, "reported_total": 0}
    try:
        universe = cache.get("breadth_verified_universe") or {}
        if universe.get("date") != current.strftime("%Y-%m-%d"):
            universe = fetch_universe(current, deadline)
            cache["breadth_verified_universe"] = universe
        symbols = universe["symbols"]
        result.update(reported_total=len(symbols), universe_source=universe["source"],
                      universe_date=universe["date"], universe_hash=universe["hash"],
                      excluded_count=universe["excluded_count"])
        batches = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]

        def batch(item):
            number, requested = item
            try:
                quotes = parse_quotes(_read("https://qt.gtimg.cn/q=" + ",".join(requested), _timeout(deadline)))
                # Even a high overall coverage cannot excuse a dropped transport batch.
                if any((quotes.get(symbol[2:]) or {}).get("source_symbol") != symbol for symbol in requested):
                    return number, [], "quote_batch_incomplete"
                return number, [quotes[symbol[2:]] for symbol in requested], None
            except Exception as exc:
                return number, [], type(exc).__name__

        with ThreadPoolExecutor(max_workers=4) as pool:
            fetched = list(pool.map(batch, enumerate(batches, 1)))
        rejected = {"stale_or_future": 0, "invalid_values": 0}
        timestamps = []
        rows = []
        # Validate against completion time, allowing only elapsed fetch time, not future quotes.
        reference = current.timestamp() + time.monotonic() - start
        for number, quotes, error in fetched:
            if error:
                result["failed_pages"].append(number)
                result["page_errors"][number] = error
            for quote in quotes:
                try:
                    stamp = datetime.strptime(quote["datetime"], "%Y%m%d%H%M%S")
                    age = reference - stamp.timestamp()
                    if stamp.date() != current.date() or not -5 <= age <= MAX_QUOTE_AGE_SECONDS:
                        rejected["stale_or_future"] += 1
                        continue
                    values = [float(quote[key]) for key in ("close", "prev_close", "pct", "amount_wan")]
                    if not all(math.isfinite(v) for v in values) or min(values[:2]) <= 0 or values[3] < 0:
                        raise ValueError("invalid_quote")
                    rows.append({"f12": quote["code"], "f14": quote["name"],
                                 "f3": values[2], "f6": values[3] * 10000})
                    timestamps.append(stamp)
                except (ValueError, TypeError, KeyError):
                    rejected["invalid_values"] += 1
        result.update(rows=rows, rejected_quotes=rejected, page_count=len(batches),
                      oldest_quote_at=min(timestamps).isoformat(" ") if timestamps else None,
                      newest_quote_at=max(timestamps).isoformat(" ") if timestamps else None)
    except Exception as exc:
        result.update(failed_pages=["universe"], page_errors={"universe": type(exc).__name__})
    result["fetch_seconds"] = round(time.monotonic() - start, 3)
    return result
