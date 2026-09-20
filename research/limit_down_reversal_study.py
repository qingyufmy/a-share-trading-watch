#!/usr/bin/env python3
"""Study next-day and T+3 behavior after non-ST A-share limit-down events.

Data source: Eastmoney public quote/kline endpoints. Prices are unadjusted daily bars.
The script caches stock list and per-symbol klines under data/limit_down_reversal/cache.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "limit_down_reversal"
CACHE_DIR = DATA_DIR / "cache"
OUT_DIR = DATA_DIR / "output"
EASTMONEY_UT = "bd1d9ddb04089700cf9c27f6f7426281"
PUSH2_HOSTS = ("82.push2.eastmoney.com", "84.push2.eastmoney.com", "48.push2.eastmoney.com", "72.push2.eastmoney.com")


@dataclass(frozen=True)
class StockMeta:
    code: str
    market: int
    name: str
    industry: str

    @property
    def secid(self) -> str:
        return f"{self.market}.{self.code}"


def ensure_dirs() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def fetch_json(url: str, timeout: int = 12, retries: int = 3) -> dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    }
    last_error: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.8 + i * 1.2)
    try:
        result = subprocess.run(
            ["curl", "-L", "--compressed", "-sS", "-A", headers["User-Agent"], "-e", headers["Referer"], url],
            capture_output=True,
            text=True,
            timeout=timeout + 8,
            check=True,
        )
        return json.loads(result.stdout)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"fetch failed: {url} :: {last_error}; curl={exc}") from exc


def stock_list_cache_path() -> Path:
    return CACHE_DIR / "eastmoney_stock_list.json"


def fetch_stock_list(refresh: bool = False) -> list[StockMeta]:
    cache = stock_list_cache_path()
    if cache.exists() and not refresh:
        rows = json.loads(cache.read_text(encoding="utf-8"))
        return [StockMeta(**row) for row in rows]

    base_params = {
        "pz": 100,
        "po": 1,
        "np": 1,
        "ut": EASTMONEY_UT,
        "fltt": 2,
        "invt": 2,
        "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f12,f13,f14,f100",
    }
    diff = []
    total = None
    page = 1
    while True:
        params = {**base_params, "pn": page}
        last_error: Exception | None = None
        for host in PUSH2_HOSTS:
            url = f"https://{host}/api/qt/clist/get?" + urllib.parse.urlencode(params)
            try:
                data = fetch_json(url)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        else:
            raise RuntimeError(f"stock list fetch failed page={page}: {last_error}")
        payload = data.get("data") or {}
        rows = payload.get("diff") or []
        total = total or int(payload.get("total") or 0)
        diff.extend(rows)
        if not rows or len(diff) >= total:
            break
        page += 1
    out: list[StockMeta] = []
    for row in diff:
        code = str(row.get("f12") or "").zfill(6)
        name = str(row.get("f14") or "")
        market = int(row.get("f13") or (1 if code.startswith("6") else 0))
        industry = str(row.get("f100") or "未知")
        if not code or not name:
            continue
        out.append(StockMeta(code=code, market=market, name=name, industry=industry))
    cache.write_text(json.dumps([x.__dict__ for x in out], ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def board_limit_pct(code: str) -> float:
    if code.startswith(("300", "301", "688")):
        return 20.0
    if code.startswith(("8", "4", "9")):
        return 30.0
    return 10.0


def is_st_name(name: str) -> bool:
    upper = (name or "").upper()
    return "ST" in upper or "退" in name


def kline_cache_path(meta: StockMeta) -> Path:
    return CACHE_DIR / f"kline_{meta.secid.replace('.', '_')}.json"


def fetch_kline(meta: StockMeta, lmt: int = 120, refresh: bool = False) -> list[dict[str, Any]]:
    cache = kline_cache_path(meta)
    if cache.exists() and not refresh:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    params = {
        "secid": meta.secid,
        "klt": 101,
        "fqt": 0,
        "lmt": lmt,
        "end": 20500101,
        "ut": EASTMONEY_UT,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urllib.parse.urlencode(params)
    data = fetch_json(url)
    klines = ((data.get("data") or {}).get("klines") or [])
    rows = []
    for text in klines:
        parts = text.split(",")
        if len(parts) < 11:
            continue
        try:
            rows.append(
                {
                    "date": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]),
                    "amount": float(parts[6]),
                    "amp_pct": float(parts[7]),
                    "pct_chg": float(parts[8]),
                    "chg": float(parts[9]),
                    "turnover": float(parts[10]),
                }
            )
        except Exception:
            continue
    cache.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def fetch_all_bars(stocks: list[StockMeta], lmt: int, refresh: bool, workers: int) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    failures: list[tuple[str, str]] = []
    start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_kline, meta, lmt, refresh): meta for meta in stocks}
        for i, fut in enumerate(as_completed(futs), 1):
            meta = futs[fut]
            try:
                bars = fut.result()
                for bar in bars:
                    rows.append({**bar, "code": meta.code, "name": meta.name, "market": meta.market, "industry": meta.industry})
            except Exception as exc:  # noqa: BLE001
                failures.append((meta.code, str(exc)))
            if i % 500 == 0:
                print(f"fetched {i}/{len(stocks)} stocks, rows={len(rows)}, failures={len(failures)}, elapsed={time.time()-start:.1f}s", file=sys.stderr)
    if failures:
        (OUT_DIR / "fetch_failures.csv").write_text("code,error\n" + "\n".join(f"{c},{e!r}" for c, e in failures), encoding="utf-8")
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("no kline data fetched")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    return df


def add_market_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["prev_close"] = df.groupby("code")["close"].shift(1)
    df["next_open"] = df.groupby("code")["open"].shift(-1)
    df["next_close"] = df.groupby("code")["close"].shift(-1)
    df["next_high"] = df.groupby("code")["high"].shift(-1)
    df["next_low"] = df.groupby("code")["low"].shift(-1)
    df["t3_close"] = df.groupby("code")["close"].shift(-3)
    df["t3_high_max"] = df.groupby("code")["high"].transform(
        lambda s: s.shift(-1).iloc[::-1].rolling(3, min_periods=1).max().iloc[::-1]
    )
    df["limit_pct"] = df["code"].map(board_limit_pct)
    df["is_st"] = df["name"].map(is_st_name)
    threshold = -(df["limit_pct"] - 0.25)
    df["limit_down"] = (~df["is_st"]) & (df["pct_chg"] <= threshold) & (df["close"] <= df["low"] * 1.001)

    daily = df.groupby("date").agg(
        market_count=("code", "count"),
        up_count=("pct_chg", lambda x: int((x > 0).sum())),
        down_count=("pct_chg", lambda x: int((x < 0).sum())),
        median_pct=("pct_chg", "median"),
        limit_down_count=("limit_down", "sum"),
    )
    daily["red_rate"] = daily["up_count"] / daily["market_count"]
    daily["market_mood"] = pd.cut(
        daily["red_rate"],
        bins=[-0.01, 0.35, 0.55, 1.01],
        labels=["弱", "中性", "强"],
    ).astype(str)
    df = df.merge(daily[["red_rate", "median_pct", "limit_down_count", "market_mood"]], left_on="date", right_index=True, how="left")

    ind = df.groupby(["date", "industry"]).agg(
        industry_count=("code", "count"),
        industry_red_rate=("pct_chg", lambda x: float((x > 0).mean()) if len(x) else math.nan),
        industry_median_pct=("pct_chg", "median"),
    )
    df = df.merge(ind, on=["date", "industry"], how="left")
    return df


def build_events(df: pd.DataFrame, last_n_dates: int) -> tuple[pd.DataFrame, list[pd.Timestamp]]:
    dates = sorted(df["date"].dropna().unique())
    event_dates = dates[-last_n_dates:]
    ev = df[df["date"].isin(event_dates) & df["limit_down"]].copy()
    for col, numerator, denominator in [
        ("next_open_ret_from_ld_close", "next_open", "close"),
        ("next_close_ret_from_ld_close", "next_close", "close"),
        ("next_high_ret_from_ld_close", "next_high", "close"),
        ("next_low_ret_from_ld_close", "next_low", "close"),
        ("t3_close_ret_from_ld_close", "t3_close", "close"),
        ("next_open_to_close_ret", "next_close", "next_open"),
        ("next_open_to_t3_close_ret", "t3_close", "next_open"),
        ("t3_max_ret_from_ld_close", "t3_high_max", "close"),
    ]:
        ev[col] = ev[numerator] / ev[denominator] - 1
    ev["next_day_repair_positive"] = ev["next_close_ret_from_ld_close"] > 0
    ev["t3_positive"] = ev["t3_close_ret_from_ld_close"] > 0
    ev["next_intraday_repair_3pct"] = ev["next_high_ret_from_ld_close"] >= 0.03
    ev["market_mood"] = ev["market_mood"].fillna("未知")
    ev["industry_mood"] = pd.cut(
        ev["industry_red_rate"],
        bins=[-0.01, 0.35, 0.55, 1.01],
        labels=["板块弱", "板块中性", "板块强"],
    ).astype(str)
    return ev, event_dates


def summarize(events: pd.DataFrame) -> dict[str, Any]:
    def metric(s: pd.Series) -> dict[str, Any]:
        s = s.dropna()
        if s.empty:
            return {"n": 0}
        return {
            "n": int(s.shape[0]),
            "mean_pct": round(float(s.mean() * 100), 3),
            "median_pct": round(float(s.median() * 100), 3),
            "win_rate_pct": round(float((s > 0).mean() * 100), 2),
            "p25_pct": round(float(s.quantile(0.25) * 100), 3),
            "p75_pct": round(float(s.quantile(0.75) * 100), 3),
        }

    out = {
        "event_count": int(len(events)),
        "stock_count": int(events["code"].nunique()) if not events.empty else 0,
        "next_open_from_close": metric(events["next_open_ret_from_ld_close"]),
        "next_close_from_close": metric(events["next_close_ret_from_ld_close"]),
        "next_open_to_close_tradeable": metric(events["next_open_to_close_ret"]),
        "t3_close_from_close": metric(events["t3_close_ret_from_ld_close"]),
        "next_open_to_t3_close_tradeable": metric(events["next_open_to_t3_close_ret"]),
        "next_high_from_close": metric(events["next_high_ret_from_ld_close"]),
        "next_intraday_repair_3pct_rate": round(float(events["next_intraday_repair_3pct"].mean() * 100), 2) if len(events) else None,
    }
    return out


def group_summary(events: pd.DataFrame, by: list[str], metric_col: str) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    return (
        events.groupby(by, dropna=False)
        .agg(
            n=("code", "count"),
            stocks=("code", "nunique"),
            mean_ret=(metric_col, "mean"),
            median_ret=(metric_col, "median"),
            win_rate=(metric_col, lambda x: (x.dropna() > 0).mean() if x.dropna().shape[0] else math.nan),
            next_repair_3pct=("next_intraday_repair_3pct", "mean"),
        )
        .reset_index()
        .assign(
            mean_ret_pct=lambda x: (x["mean_ret"] * 100).round(3),
            median_ret_pct=lambda x: (x["median_ret"] * 100).round(3),
            win_rate_pct=lambda x: (x["win_rate"] * 100).round(2),
            next_repair_3pct_rate=lambda x: (x["next_repair_3pct"] * 100).round(2),
        )
        .drop(columns=["mean_ret", "median_ret", "win_rate", "next_repair_3pct"])
        .sort_values(["n", "mean_ret_pct"], ascending=[False, False])
    )


def write_outputs(events: pd.DataFrame, dates: list[pd.Timestamp], summary: dict[str, Any]) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    events_out = events.copy()
    for col in events_out.select_dtypes(include=["datetime64[ns]"]).columns:
        events_out[col] = events_out[col].dt.strftime("%Y-%m-%d")
    events_out.to_csv(OUT_DIR / "limit_down_events_latest.csv", index=False, encoding="utf-8-sig")
    events_out.to_csv(OUT_DIR / f"limit_down_events_{stamp}.csv", index=False, encoding="utf-8-sig")

    daily = group_summary(events, ["date"], "next_open_to_t3_close_ret")
    if not daily.empty:
        daily["date"] = pd.to_datetime(daily["date"]).dt.strftime("%Y-%m-%d")
        daily.to_csv(OUT_DIR / "daily_summary_latest.csv", index=False, encoding="utf-8-sig")
    group_summary(events, ["market_mood"], "next_open_to_t3_close_ret").to_csv(OUT_DIR / "market_mood_summary_latest.csv", index=False, encoding="utf-8-sig")
    group_summary(events, ["industry_mood"], "next_open_to_t3_close_ret").to_csv(OUT_DIR / "industry_mood_summary_latest.csv", index=False, encoding="utf-8-sig")
    industry = group_summary(events, ["industry"], "next_open_to_t3_close_ret")
    if not industry.empty:
        industry.to_csv(OUT_DIR / "industry_summary_latest.csv", index=False, encoding="utf-8-sig")

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "Eastmoney public clist + kline endpoints",
        "price_adjustment": "unadjusted daily bars",
        "event_dates": [pd.Timestamp(x).strftime("%Y-%m-%d") for x in dates],
        "summary": summary,
        "files": {
            "events": str(OUT_DIR / "limit_down_events_latest.csv"),
            "daily": str(OUT_DIR / "daily_summary_latest.csv"),
            "market_mood": str(OUT_DIR / "market_mood_summary_latest.csv"),
            "industry_mood": str(OUT_DIR / "industry_mood_summary_latest.csv"),
            "industry": str(OUT_DIR / "industry_summary_latest.csv"),
        },
    }
    (OUT_DIR / "summary_latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--last-n", type=int, default=30)
    parser.add_argument("--kline-lmt", type=int, default=90)
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--refresh-list", action="store_true")
    parser.add_argument("--refresh-kline", action="store_true")
    parser.add_argument("--include-bj", action="store_true", help="include Beijing Stock Exchange names if present")
    args = parser.parse_args()

    ensure_dirs()
    stocks = fetch_stock_list(refresh=args.refresh_list)
    if not args.include_bj:
        stocks = [s for s in stocks if not s.code.startswith(("8", "4", "9"))]
    stocks = [s for s in stocks if not is_st_name(s.name)]
    print(f"stock universe={len(stocks)}", file=sys.stderr)
    df = fetch_all_bars(stocks, lmt=args.kline_lmt, refresh=args.refresh_kline, workers=args.workers)
    df = add_market_features(df)
    events, dates = build_events(df, args.last_n)
    summary = summarize(events)
    write_outputs(events, dates, summary)
    print(json.dumps({"summary": summary, "event_dates": [pd.Timestamp(x).strftime('%Y-%m-%d') for x in dates], "out_dir": str(OUT_DIR)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
