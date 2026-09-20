#!/usr/bin/env python3
import argparse
import json
import os
import time
import uuid
from datetime import datetime, time as dtime
from pathlib import Path

import after_close_report as base
import intraday_report as intra


BASE_DIR = Path(os.environ.get("A_SHARE_BASE_DIR", Path(__file__).resolve().parent))
REPORT_DATE = os.environ.get("A_SHARE_REPORT_DATE") or datetime.now().strftime("%Y-%m-%d")
AUCTION_PATH_DIR = BASE_DIR / "data" / "auction_path"
DEFAULT_START = "09:15:00"
DEFAULT_END = "09:24:56"


def parse_clock(value):
    parts = [int(x) for x in str(value).split(":")]
    if len(parts) == 2:
        parts.append(0)
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"invalid HH:MM[:SS] time: {value}")
    return dtime(parts[0], parts[1], parts[2])


def wall_now():
    return datetime.now()


def timestamp(dt=None):
    return (dt or wall_now()).strftime("%Y-%m-%d %H:%M:%S")


def infer_universe():
    try:
        levels = intra.read_premarket_levels()
        if levels:
            return [(code, base.infer_market(code)) for code in levels]
    except Exception:
        pass
    return base.read_watchlist()


def compact_quote(q, rank=None, source=None):
    out = {
        "code": q.get("code"),
        "name": q.get("name"),
        "price": q.get("close"),
        "prev_close": q.get("prev_close"),
        "open": q.get("open"),
        "pct": q.get("pct"),
        "change": q.get("change"),
        "high": q.get("high"),
        "low": q.get("low"),
        "amount_wan": q.get("amount_wan"),
        "volume_lot": q.get("volume_lot"),
        "turnover": q.get("turnover"),
        "datetime": q.get("datetime"),
        "industry": q.get("industry"),
        "concepts": q.get("concepts"),
    }
    if rank is not None:
        out["rank"] = rank
    if source:
        out["source"] = source
    return {k: v for k, v in out.items() if v not in (None, "")}


def fetch_watchlist_quotes(universe):
    quotes = base.parse_tencent_quotes(universe)
    rows = []
    for code, _market in universe:
        q = quotes.get(code)
        if q:
            rows.append(compact_quote(q, source="tencent"))
    return rows


def fetch_market_top(fid, size):
    rows = []
    for idx, raw in enumerate(intra.fetch_market_scan_rows(fid, size), start=1):
        q = intra.em_row_to_quote(raw)
        if q:
            rows.append(compact_quote(q, rank=idx, source="eastmoney"))
    return rows


def update_watch_summary(summary, ts, rows):
    for q in rows:
        code = q.get("code")
        if not code:
            continue
        item = summary.setdefault(code, {
            "code": code,
            "name": q.get("name"),
            "first_ts": ts,
            "last_ts": ts,
            "samples": 0,
            "first_price": q.get("price"),
            "first_pct": q.get("pct"),
            "first_amount_wan": q.get("amount_wan"),
            "min_price": q.get("price"),
            "max_price": q.get("price"),
            "max_amount_wan": q.get("amount_wan"),
        })
        item["last_ts"] = ts
        item["samples"] += 1
        item["last_price"] = q.get("price")
        item["last_pct"] = q.get("pct")
        item["last_amount_wan"] = q.get("amount_wan")
        price = q.get("price")
        amount = q.get("amount_wan")
        if isinstance(price, (int, float)):
            item["min_price"] = price if item.get("min_price") is None else min(item["min_price"], price)
            item["max_price"] = price if item.get("max_price") is None else max(item["max_price"], price)
        if isinstance(amount, (int, float)):
            item["max_amount_wan"] = amount if item.get("max_amount_wan") is None else max(item["max_amount_wan"], amount)


def update_market_summary(summary, ts, list_name, rows):
    rank_key = f"best_{list_name}_rank"
    hits_key = f"{list_name}_hits"
    for q in rows:
        code = q.get("code")
        if not code:
            continue
        item = summary.setdefault(code, {
            "code": code,
            "name": q.get("name"),
            "industry": q.get("industry"),
            "concepts": q.get("concepts"),
            "first_ts": ts,
            "last_ts": ts,
            "amount_hits": 0,
            "pct_hits": 0,
            "best_amount_rank": None,
            "best_pct_rank": None,
            "max_amount_wan": None,
            "max_pct": None,
        })
        item["last_ts"] = ts
        item["last_price"] = q.get("price")
        item["last_pct"] = q.get("pct")
        item[hits_key] += 1
        rank = q.get("rank")
        if isinstance(rank, int):
            item[rank_key] = rank if item[rank_key] is None else min(item[rank_key], rank)
        amount = q.get("amount_wan")
        pct = q.get("pct")
        if isinstance(amount, (int, float)):
            item["max_amount_wan"] = amount if item["max_amount_wan"] is None else max(item["max_amount_wan"], amount)
        if isinstance(pct, (int, float)):
            item["max_pct"] = pct if item["max_pct"] is None else max(item["max_pct"], pct)


def write_jsonl(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def write_summary(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def build_summary(run_id, started_at, ended_at, samples, watch_summary, market_summary):
    market_rows = sorted(
        market_summary.values(),
        key=lambda x: (
            -(x.get("amount_hits") or 0) - (x.get("pct_hits") or 0),
            -(x.get("max_amount_wan") or 0),
            -(x.get("max_pct") or -999),
        ),
    )
    return {
        "trading_date": REPORT_DATE,
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "samples": samples,
        "note": "Public quote snapshot path only; not Level-2 tick/order/order-queue data.",
        "watchlist": sorted(watch_summary.values(), key=lambda x: x["code"]),
        "market_seen_top": market_rows[:300],
    }


def collect_once(universe, args, ts, include_market):
    errors = []
    watch_rows = []
    market = {}
    try:
        watch_rows = fetch_watchlist_quotes(universe)
    except Exception as exc:
        errors.append({"source": "tencent_watchlist", "error": str(exc)})
    if include_market and not args.watchlist_only:
        try:
            market["amount"] = fetch_market_top("f6", args.amount_size)
        except Exception as exc:
            errors.append({"source": "eastmoney_amount", "error": str(exc)})
        try:
            market["pct"] = fetch_market_top("f3", args.pct_size)
        except Exception as exc:
            errors.append({"source": "eastmoney_pct", "error": str(exc)})
    return {
        "event": "snapshot",
        "trading_date": REPORT_DATE,
        "ts": ts,
        "watchlist": watch_rows,
        "market": market,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description="Collect public 09:15-09:25 auction quote snapshot path")
    parser.add_argument("--start", type=parse_clock, default=parse_clock(DEFAULT_START), help="Auction path start time, HH:MM[:SS]")
    parser.add_argument("--end", type=parse_clock, default=parse_clock(DEFAULT_END), help="Auction path end time, HH:MM[:SS]")
    parser.add_argument("--sample-interval", type=float, default=3.0, help="Watchlist quote sampling interval in seconds")
    parser.add_argument("--market-interval", type=float, default=15.0, help="Full-market top-list sampling interval in seconds")
    parser.add_argument("--amount-size", type=int, default=260, help="Eastmoney amount-ranked rows per market sample")
    parser.add_argument("--pct-size", type=int, default=180, help="Eastmoney pct-ranked rows per market sample")
    parser.add_argument("--watchlist-only", action="store_true", help="Skip Eastmoney full-market top-list sampling")
    parser.add_argument("--once", action="store_true", help="Collect one snapshot immediately")
    parser.add_argument("--force", action="store_true", help="Run outside trading day/window")
    args = parser.parse_args()

    now = wall_now()
    if not args.force and not base.is_a_share_trading_day(now.date()):
        print(json.dumps({"status": "idle", "reason": "non_trading_day", "now": timestamp(now)}, ensure_ascii=False))
        return 0

    universe = infer_universe()
    run_id = uuid.uuid4().hex[:12]
    compact_date = REPORT_DATE.replace("-", "")
    jsonl_path = AUCTION_PATH_DIR / f"auction_path_{compact_date}.jsonl"
    summary_path = AUCTION_PATH_DIR / f"auction_path_summary_{compact_date}.json"
    latest_path = AUCTION_PATH_DIR / "auction_path_latest.json"
    started_at = timestamp()
    watch_summary = {}
    market_summary = {}
    samples = 0
    last_market_ts = 0.0

    start_record = {
        "event": "start",
        "trading_date": REPORT_DATE,
        "run_id": run_id,
        "started_at": started_at,
        "window": {"start": args.start.isoformat(), "end": args.end.isoformat()},
        "sample_interval": args.sample_interval,
        "market_interval": args.market_interval,
        "watchlist_size": len(universe),
        "watchlist_only": args.watchlist_only,
        "note": "This collector samples public quote snapshots. It does not capture Level-2 orders, cancels, or trades.",
    }
    write_jsonl(jsonl_path, start_record)

    try:
        while True:
            now = wall_now()
            if not args.once and not args.force:
                if now.time() < args.start:
                    time.sleep(min(5.0, max(0.5, args.sample_interval)))
                    continue
                if now.time() >= args.end:
                    break
            include_market = args.once or (time.time() - last_market_ts >= args.market_interval)
            if include_market:
                last_market_ts = time.time()
            record = collect_once(universe, args, timestamp(now), include_market)
            record["run_id"] = run_id
            write_jsonl(jsonl_path, record)
            update_watch_summary(watch_summary, record["ts"], record["watchlist"])
            for list_name, rows in (record.get("market") or {}).items():
                update_market_summary(market_summary, record["ts"], list_name, rows)
            samples += 1
            if args.once:
                break
            time.sleep(max(0.5, args.sample_interval))
    finally:
        ended_at = timestamp()
        summary = build_summary(run_id, started_at, ended_at, samples, watch_summary, market_summary)
        summary["jsonl_path"] = str(jsonl_path)
        write_summary(summary_path, summary)
        write_summary(latest_path, summary)

    print(json.dumps({
        "status": "ok",
        "trading_date": REPORT_DATE,
        "run_id": run_id,
        "samples": samples,
        "jsonl": str(jsonl_path),
        "summary": str(summary_path),
        "latest": str(latest_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
