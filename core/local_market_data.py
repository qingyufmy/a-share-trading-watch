"""Free, source-segmented local market-data store for the intraday engine.

Tencent is the primary source.  Sina is a complete-history fallback and
cross-check, never a per-bar patch.  A data divergence blocks new exposure
but deliberately does not affect risk-reduction signals.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any


PRIMARY_SOURCE = "tencent"
FALLBACK_SOURCE = "sina"
HISTORY_TIMEFRAME = "60m"
MINUTE_TIMEFRAME = "1m"
SETUP_TIMEFRAME = "15m"
REQUIRED_120M_BARS = 240
REQUIRED_15M_BARS = 20
CROSS_SOURCE_HARD_MISMATCH_COUNT = 2
_RUNTIME_STATES: dict[str, dict[str, Any]] = {}


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def provider_symbol(code: str) -> str:
    code = str(code).strip()
    if code.startswith(("4", "8", "92")):
        return "bj" + code
    return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code


def db_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "data" / "runtime" / "market_data.sqlite"


def _connect(base_dir: str | Path) -> sqlite3.Connection:
    path = db_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS market_bars (
            source TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            bar_end TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL DEFAULT 0,
            amount REAL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (source, symbol, timeframe, bar_end)
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_bars_lookup ON market_bars(symbol, timeframe, source, bar_end)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS market_data_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    return con


def _meta_get(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM market_data_meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else None


def _meta_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO market_data_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _request_text(url: str, timeout: int = 6, referer: str = "https://finance.qq.com/") -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": referer},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "ignore")


def _parse_bar_time(value: Any, default_time: str = "15:00") -> datetime | None:
    digits = re.sub(r"\D", "", str(value or ""))
    for pattern, length in (("%Y%m%d%H%M", 12), ("%Y%m%d", 8)):
        if len(digits) >= length:
            try:
                if length == 8:
                    return datetime.strptime(digits[:8] + default_time.replace(":", ""), "%Y%m%d%H%M")
                return datetime.strptime(digits[:12], pattern)
            except ValueError:
                return None
    return None


def _normal_bar(timestamp: Any, values: list[Any], source: str) -> dict[str, Any] | None:
    if len(values) < 6:
        return None
    point = _parse_bar_time(timestamp)
    open_price = _float(values[1])
    close = _float(values[2])
    high = _float(values[3])
    low = _float(values[4])
    volume = _float(values[5], 0.0) or 0.0
    amount = _float(values[6]) if len(values) > 6 else None
    if point is None or None in (open_price, high, low, close) or min(open_price, high, low, close) <= 0:
        return None
    return {
        "source": source,
        "bar_end": point.strftime("%Y-%m-%d %H:%M:%S"),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": amount,
    }


def parse_tencent_bars(payload: str | dict[str, Any], code: str, timeframe: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except (TypeError, ValueError):
        return []
    groups = data.get("data") or {}
    symbol = provider_symbol(code)
    group = groups.get(symbol) or groups.get(symbol.upper()) or next(iter(groups.values()), {})
    series_key = {
        MINUTE_TIMEFRAME: "m1",
        SETUP_TIMEFRAME: "m15",
        HISTORY_TIMEFRAME: "m60",
    }.get(timeframe)
    if not series_key:
        return []
    series = group.get(series_key) or []
    out = []
    for item in series:
        if not isinstance(item, list) or not item:
            continue
        bar = _normal_bar(item[0], item, PRIMARY_SOURCE)
        if bar:
            out.append(bar)
    return out


def parse_sina_kline(payload: str, code: str) -> list[dict[str, Any]]:
    match = re.search(r"=\s*\((.*)\)\s*;?\s*$", payload or "", flags=re.S)
    if not match:
        return []
    try:
        values = json.loads(match.group(1))
    except ValueError:
        return []
    out = []
    for item in values or []:
        if not isinstance(item, dict):
            continue
        point = _parse_bar_time(item.get("day"))
        open_price = _float(item.get("open"))
        high = _float(item.get("high"))
        low = _float(item.get("low"))
        close = _float(item.get("close"))
        volume = _float(item.get("volume"), 0.0) or 0.0
        amount = _float(item.get("amount"))
        if point is None or None in (open_price, high, low, close) or min(open_price, high, low, close) <= 0:
            continue
        out.append({
            "source": FALLBACK_SOURCE,
            "bar_end": point.strftime("%Y-%m-%d %H:%M:%S"),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        })
    return out


def parse_sina_60m(payload: str, code: str) -> list[dict[str, Any]]:
    """Compatibility wrapper for existing 60-minute callers and tests."""
    return parse_sina_kline(payload, code)


def parse_sina_minline(payload: str | dict[str, Any]) -> list[dict[str, Any]]:
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except (TypeError, ValueError):
        return []
    out = []
    for item in ((data.get("result") or {}).get("data") or []):
        if not isinstance(item, dict):
            continue
        point = _parse_bar_time(item.get("m") or item.get("time"))
        price = _float(item.get("p") if "p" in item else item.get("close"))
        volume = _float(item.get("v") if "v" in item else item.get("volume"), 0.0) or 0.0
        if point is None or price is None or price <= 0:
            continue
        out.append({
            "source": FALLBACK_SOURCE,
            "bar_end": point.strftime("%Y-%m-%d %H:%M:%S"),
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": volume,
            "amount": None,
        })
    return out


def fetch_tencent_bars(code: str, timeframe: str, timeout: int = 6) -> list[dict[str, Any]]:
    period = {
        MINUTE_TIMEFRAME: "m1",
        SETUP_TIMEFRAME: "m15",
        HISTORY_TIMEFRAME: "m60",
    }.get(timeframe)
    if not period:
        return []
    url = f"http://ifzq.gtimg.cn/appstock/app/kline/mkline?param={provider_symbol(code)},{period},,,640"
    return parse_tencent_bars(_request_text(url, timeout=timeout), code, timeframe)


def fetch_sina_60m(code: str, timeout: int = 6) -> list[dict[str, Any]]:
    return fetch_sina_kline(code, HISTORY_TIMEFRAME, timeout=timeout)


def fetch_sina_kline(code: str, timeframe: str, timeout: int = 6) -> list[dict[str, Any]]:
    scale = {HISTORY_TIMEFRAME: 60, SETUP_TIMEFRAME: 15}.get(timeframe)
    if not scale:
        return []
    url = (
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20data=/"
        f"CN_MarketDataService.getKLineData?symbol={provider_symbol(code)}&scale={scale}&ma=no&datalen=500"
    )
    return parse_sina_kline(_request_text(url, timeout=timeout, referer="https://finance.sina.com.cn/"), code)


def fetch_sina_minline(code: str, timeout: int = 6) -> list[dict[str, Any]]:
    url = (
        "https://quotes.sina.cn/cn/api/openapi.php/"
        f"CN_MinlineService.getMinlineData?symbol={provider_symbol(code)}&dpc=1"
    )
    return parse_sina_minline(_request_text(url, timeout=timeout, referer="https://finance.sina.cn/"))


def upsert_bars(base_dir: str | Path, code: str, timeframe: str, bars: list[dict[str, Any]]) -> int:
    if not bars:
        return 0
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con = _connect(base_dir)
    try:
        accepted = list(bars)
        if timeframe == MINUTE_TIMEFRAME:
            accepted = []
            by_source: dict[str, list[dict[str, Any]]] = {}
            for item in bars:
                by_source.setdefault(str(item.get("source") or ""), []).append(item)
            for source, source_bars in by_source.items():
                stored = con.execute(
                    "SELECT MAX(bar_end) FROM market_bars WHERE source = ? AND symbol = ? AND timeframe = ?",
                    (source, str(code), timeframe),
                ).fetchone()
                stored_latest = str(stored[0]) if stored and stored[0] else ""
                incoming_latest = max(str(item.get("bar_end") or "") for item in source_bars)
                # Delayed provider responses must not roll the live minute
                # cache back to an older sequence.
                if stored_latest and incoming_latest < stored_latest:
                    continue
                accepted.extend(source_bars)
        if not accepted:
            return 0
        con.executemany(
            """
            INSERT INTO market_bars(source, symbol, timeframe, bar_end, open, high, low, close, volume, amount, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, symbol, timeframe, bar_end) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                volume=excluded.volume, amount=excluded.amount, fetched_at=excluded.fetched_at
            """,
            [
                (
                    item["source"], str(code), timeframe, item["bar_end"], item["open"], item["high"], item["low"],
                    item["close"], item.get("volume") or 0.0, item.get("amount"), fetched_at,
                )
                for item in accepted
            ],
        )
        con.commit()
    finally:
        con.close()
    return len(accepted)


def load_bars(base_dir: str | Path, code: str, timeframe: str, source: str) -> list[dict[str, Any]]:
    con = _connect(base_dir)
    try:
        rows = con.execute(
            """
            SELECT source, bar_end, open, high, low, close, volume, amount, fetched_at
            FROM market_bars WHERE symbol = ? AND timeframe = ? AND source = ? ORDER BY bar_end
            """,
            (str(code), timeframe, source),
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "source": row[0], "bar_end": row[1], "open": row[2], "high": row[3], "low": row[4],
            "close": row[5], "volume": row[6], "amount": row[7], "fetched_at": row[8],
        }
        for row in rows
    ]


def build_120m_bars(rows: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    current = now or datetime.now()
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows or []:
        try:
            point = datetime.strptime(str(row.get("bar_end")), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if point > current:
            continue
        slot = point.strftime("%H:%M")
        if slot not in {"10:30", "11:30", "14:00", "15:00"}:
            continue
        grouped.setdefault(point.strftime("%Y-%m-%d"), {})[slot] = row
    pairs = (("10:30", "11:30"), ("14:00", "15:00"))
    out = []
    for day in sorted(grouped):
        session = grouped[day]
        for first_slot, last_slot in pairs:
            first, last = session.get(first_slot), session.get(last_slot)
            if not first or not last:
                continue
            out.append({
                "time": f"{day} {last_slot}:00",
                "bar_end": f"{day} {last_slot}:00",
                "open": first["open"],
                "high": max(float(first["high"]), float(last["high"])),
                "low": min(float(first["low"]), float(last["low"])),
                "close": last["close"],
                "volume": float(first.get("volume") or 0) + float(last.get("volume") or 0),
                "amount": (float(first.get("amount") or 0) + float(last.get("amount") or 0)) or None,
            })
    return out


def _minute_timestamp_convention(rows: list[dict[str, Any]]) -> str:
    """Return whether provider minute timestamps mark bar start or bar end.

    Tencent/Sina persisted rows are provider bars whose 09:31 and 13:01
    timestamps close the first tradable minute.  Synthetic/legacy rows without
    a source keep the historical start-stamped convention for compatibility.
    """
    sources = {str(row.get("source") or "").lower() for row in (rows or [])}
    if sources & {PRIMARY_SOURCE, FALLBACK_SOURCE}:
        return "end"
    times = {str(row.get("bar_end") or "")[-8:] for row in (rows or [])}
    if "13:00:00" in times:
        return "start"
    if times & {"11:30:00", "15:00:00"}:
        return "end"
    return "start"


def _session_slot(point: dtime, convention: str = "start") -> int | None:
    minute = point.hour * 60 + point.minute
    if convention == "end":
        if 9 * 60 + 31 <= minute <= 11 * 60 + 30:
            return minute - (9 * 60 + 31)
        if 13 * 60 + 1 <= minute <= 15 * 60:
            return 120 + minute - (13 * 60 + 1)
        return None
    if 9 * 60 + 30 <= minute < 11 * 60 + 30:
        return minute - (9 * 60 + 30)
    if 13 * 60 <= minute < 15 * 60:
        return 120 + minute - 13 * 60
    return None


def build_session_bars(rows: list[dict[str, Any]], minutes: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """Aggregate complete unique minutes without spanning the lunch break."""
    if minutes not in (15, 120):
        raise ValueError('Only 15m and 120m session bars are supported')
    current = now or datetime.now()
    convention = _minute_timestamp_convention(rows)
    buckets: dict[tuple[str, int], dict[int, tuple[datetime, dict[str, Any]]]] = {}
    for row in rows or []:
        try:
            point = datetime.strptime(str(row.get("bar_end")), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if point > current:
            continue
        slot = _session_slot(point.time(), convention)
        if slot is None:
            continue
        buckets.setdefault((point.strftime("%Y-%m-%d"), slot // minutes), {})[slot] = (point, row)
    out = []
    for (day, bucket), slot_points in sorted(buckets.items()):
        points = list(slot_points.values())
        expected = min(minutes, 240 - bucket * minutes)
        end_slot = min(240, (bucket + 1) * minutes)
        required_slots = set(range(bucket * minutes, end_slot))
        if set(slot_points) != required_slots or len(points) != expected:
            continue
        if day == current.strftime("%Y-%m-%d") and _completed_session_minutes(current.time()) < end_slot:
            continue
        points.sort(key=lambda item: item[0])
        values = [item[1] for item in points]
        out.append({
            "time": points[-1][0].strftime("%Y-%m-%d %H:%M:%S"),
            "bar_end": points[-1][0].strftime("%Y-%m-%d %H:%M:%S"),
            "open": float(values[0]["open"]),
            "high": max(float(item["high"]) for item in values),
            "low": min(float(item["low"]) for item in values),
            "close": float(values[-1]["close"]),
            "volume": sum(float(item.get("volume") or 0) for item in values),
            "amount": sum(float(item.get("amount") or 0) for item in values) or None,
        })
    return out


def build_15m_bars(rows: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    return build_session_bars(rows, 15, now)


def merge_completed_120m(history, minute_rows, now):
    """Overlay complete current sessions; expose a missing closed session explicitly."""
    day = now.strftime('%Y-%m-%d')
    expected = [f'{day} {end}' for end in ('11:30:00', '15:00:00')
                if f'{day} {end}' <= now.strftime('%Y-%m-%d %H:%M:%S')]
    merged = {b['bar_end']: dict(b) for b in history if b['bar_end'] <= str(now)}
    completed = build_session_bars([r for r in minute_rows if str(r.get('bar_end', '')).startswith(day)], 120, now)
    for bar in completed:
        # Legacy start-stamped minutes still represent the session ending at :30/:00.
        end = '11:30:00' if bar['bar_end'][11:13] == '11' else '15:00:00'
        bar.update(bar_end=f'{day} {end}', time=f'{day} {end}')
        merged[bar['bar_end']] = bar
    missing = [end for end in expected if end not in merged]
    bars = [merged[k] for k in sorted(merged)]
    return bars, {'last_closed_120m': bars[-1]['bar_end'] if bars else None,
                  'expected_current_sessions': expected, 'missing_current_sessions': missing,
                  'current_session_ready': not missing, 'minute_sessions_merged': len(completed)}


def _completed_session_minutes(point: dtime) -> int:
    minute = point.hour * 60 + point.minute
    if minute < 9 * 60 + 30:
        return 0
    if minute < 11 * 60 + 30:
        return minute - (9 * 60 + 30)
    if minute < 13 * 60:
        return 120
    if minute < 15 * 60:
        return 120 + minute - 13 * 60
    return 240


def minute_completeness(rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Describe exact unique-minute coverage available at the decision time."""
    expected = _completed_session_minutes(now.time())
    convention = _minute_timestamp_convention(rows)
    observed: set[int] = set()
    valid_points: list[datetime] = []
    for row in rows or []:
        try:
            point = datetime.strptime(str(row.get("bar_end") or ""), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if point.date() != now.date() or point > now:
            continue
        slot = _session_slot(point.time(), convention)
        if slot is None or slot >= expected:
            continue
        observed.add(slot)
        valid_points.append(point)
    missing = max(0, expected - len(observed))
    ratio = 1.0 if expected == 0 else len(observed) / expected
    last_slot = max(observed) if observed else None
    stale = expected > 0 and (last_slot is None or last_slot < expected - 2)
    return {
        "expected_minutes": expected,
        "observed_unique_minutes": len(observed),
        "missing_minutes": missing,
        "completeness_ratio": round(ratio, 4),
        "last_valid_time": max(valid_points).strftime("%Y-%m-%d %H:%M:%S") if valid_points else None,
        "stale": stale,
        "entry_ready": expected == 0 or (not stale and ratio >= 0.98),
        "timestamp_convention": convention,
    }


def merge_live_15m_history(
    native: list[dict[str, Any]], live: list[dict[str, Any]], now: datetime | None = None
) -> list[dict[str, Any]]:
    """Replace a provider's current-day snapshot with locally completed 1m bars."""
    current = now or datetime.now()
    day = current.strftime("%Y-%m-%d")
    merged = [row for row in (native or []) if not str(row.get("bar_end") or "").startswith(day)]
    merged.extend(row for row in (live or []) if str(row.get("bar_end") or "").startswith(day))
    merged.sort(key=lambda row: str(row.get("bar_end") or ""))
    return merged


def cross_check_60m(primary: list[dict[str, Any]], fallback: list[dict[str, Any]], samples: int = 8) -> dict[str, Any]:
    left = {row.get("bar_end"): row for row in primary or []}
    right = {row.get("bar_end"): row for row in fallback or []}
    common = sorted(set(left) & set(right))[-samples:]
    if not common:
        return {"status": "unverified", "common_bars": 0, "max_close_diff": None}
    diffs = [abs(float(left[key]["close"]) - float(right[key]["close"])) for key in common]
    mismatches = [
        key for key in common
        if abs(float(left[key]["close"]) - float(right[key]["close"])) > max(0.02, abs(float(left[key]["close"])) * 0.003)
    ]
    # A lone intraday close disagreement is normally a provider bar-boundary
    # revision, not a broken price series.  Keep it auditable but reserve the
    # hard no-entry state for repeated disagreement across the recent window.
    status = "mismatch" if len(mismatches) >= CROSS_SOURCE_HARD_MISMATCH_COUNT else (
        "isolated_mismatch" if mismatches else "ok"
    )
    return {
        "status": status,
        "common_bars": len(common),
        "max_close_diff": round(max(diffs), 6),
        "mismatches": mismatches[-3:],
    }


def history_120m(base_dir: str | Path, code: str, now: datetime | None = None, required: int = REQUIRED_120M_BARS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    current = now or datetime.now()
    primary_60m = load_bars(base_dir, code, HISTORY_TIMEFRAME, PRIMARY_SOURCE)
    fallback_60m = load_bars(base_dir, code, HISTORY_TIMEFRAME, FALLBACK_SOURCE)
    primary = build_120m_bars(primary_60m, current)
    fallback = build_120m_bars(fallback_60m, current)
    audit = cross_check_60m(primary_60m, fallback_60m)
    blockers: list[str] = []
    source = None
    selected: list[dict[str, Any]] = []
    if len(primary) >= required:
        source, selected = PRIMARY_SOURCE, primary
        if audit["status"] == "mismatch":
            blockers.append("腾讯与新浪60分钟收盘交叉校验不一致")
    elif len(fallback) >= required:
        source, selected = FALLBACK_SOURCE, fallback
        blockers.append("腾讯60分钟历史不足，使用新浪完整备源")
    else:
        blockers.append(f"120分钟历史不足：腾讯{len(primary)}/{required}，新浪{len(fallback)}/{required}")
    today_minutes, _minute_source = _load_today_minute_rows(base_dir, code, current.strftime('%Y-%m-%d'))
    selected, current_quality = merge_completed_120m(selected, today_minutes, current)
    if not current_quality['current_session_ready']:
        blockers.append('已闭合120分钟尚未更新：' + '、'.join(current_quality['missing_current_sessions']))
    structural_history_pending = len(selected) < required and max(len(primary), len(fallback)) > 0 and audit["status"] != "mismatch"
    entry_ready = bool(selected) and len(selected) >= required and audit["status"] != "mismatch" and current_quality['current_session_ready']
    status = "FULL" if entry_ready else "STRUCTURAL_HISTORY_PENDING" if structural_history_pending else "DEGRADED"
    quality = {
        "status": status,
        "entry_ready": entry_ready,
        "history_source": source,
        "history_120m_bars": len(selected),
        "required_120m_bars": required,
        "source_counts": {PRIMARY_SOURCE: len(primary), FALLBACK_SOURCE: len(fallback)},
        "cross_check": audit,
        "structural_history_pending": structural_history_pending,
        "blockers": blockers,
        "updated_at": current.strftime("%Y-%m-%d %H:%M:%S"),
        **current_quality,
    }
    return selected[-required:], quality


def history_15m(base_dir: str | Path, code: str, now: datetime | None = None, required: int = REQUIRED_15M_BARS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return a source-segmented, closed-bar 15m history for V2 Setup."""
    current = now or datetime.now()
    # Prefer provider-native 15m history.  The one-day minute endpoint alone
    # cannot supply MA20 early in the session and previously made V2 Setup
    # structurally impossible for most of the trading day.
    primary = [row for row in load_bars(base_dir, code, SETUP_TIMEFRAME, PRIMARY_SOURCE) if str(row.get("bar_end")) <= current.strftime("%Y-%m-%d %H:%M:%S")]
    fallback = [row for row in load_bars(base_dir, code, SETUP_TIMEFRAME, FALLBACK_SOURCE) if str(row.get("bar_end")) <= current.strftime("%Y-%m-%d %H:%M:%S")]
    primary_live = build_15m_bars(load_bars(base_dir, code, MINUTE_TIMEFRAME, PRIMARY_SOURCE), current)
    fallback_live = build_15m_bars(load_bars(base_dir, code, MINUTE_TIMEFRAME, FALLBACK_SOURCE), current)
    primary = merge_live_15m_history(primary, primary_live, current)
    fallback = merge_live_15m_history(fallback, fallback_live, current)
    if len(primary) < required:
        primary = build_15m_bars(load_bars(base_dir, code, MINUTE_TIMEFRAME, PRIMARY_SOURCE), current)
    if len(fallback) < required:
        fallback = build_15m_bars(load_bars(base_dir, code, MINUTE_TIMEFRAME, FALLBACK_SOURCE), current)
    audit = cross_check_60m(primary, fallback)
    blockers: list[str] = []
    if len(primary) >= required:
        source, selected = PRIMARY_SOURCE, primary
    elif len(fallback) >= required:
        source, selected = FALLBACK_SOURCE, fallback
        blockers.append("腾讯15分钟历史不足，使用新浪完整备源")
    else:
        source, selected = None, []
    if selected and audit["status"] == "mismatch":
        blockers.append("腾讯与新浪15分钟收盘交叉校验不一致")
    quality = {
        "status": "FULL" if selected and audit["status"] != "mismatch" else "DEGRADED",
        "entry_ready": bool(selected) and audit["status"] != "mismatch",
        "history_source": source,
        "history_15m_bars": len(selected),
        "required_15m_bars": required,
        "source_counts": {PRIMARY_SOURCE: len(primary), FALLBACK_SOURCE: len(fallback)},
        "cross_check": audit,
        "blockers": blockers if selected else [f"15分钟历史不足：腾讯{len(primary)}/{required}，新浪{len(fallback)}/{required}"],
        "updated_at": current.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return selected[-max(required, 64):], quality


def bootstrap_history(base_dir: str | Path, codes: list[str], now: datetime | None = None, max_workers: int = 6) -> dict[str, Any]:
    current = now or datetime.now()
    clean = [str(code) for code in dict.fromkeys(codes or []) if str(code or "").strip()]
    results: dict[str, dict[str, Any]] = {
        code: {"tencent_60m": 0, "sina_60m": 0, "tencent_15m": 0, "sina_15m": 0, "errors": []}
        for code in clean
    }

    def fetch(code: str, source: str, timeframe: str) -> tuple[str, str, str, list[dict[str, Any]], str | None]:
        try:
            bars = fetch_tencent_bars(code, timeframe) if source == PRIMARY_SOURCE else fetch_sina_kline(code, timeframe)
            return code, source, timeframe, bars, None
        except Exception as exc:
            return code, source, timeframe, [], str(exc)

    if clean:
        with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(clean) * 2))) as pool:
            futures = [
                pool.submit(fetch, code, source, timeframe)
                for code in clean
                for source in (PRIMARY_SOURCE, FALLBACK_SOURCE)
                for timeframe in (HISTORY_TIMEFRAME, SETUP_TIMEFRAME)
            ]
            for future in as_completed(futures):
                code, source, timeframe, bars, error = future.result()
                results[code][f"{source}_{timeframe}"] = upsert_bars(base_dir, code, timeframe, bars) if bars else 0
                if error:
                    results[code]["errors"].append(f"{source}:{timeframe}:{error}")
    con = _connect(base_dir)
    try:
        _meta_set(con, f"history_refresh:{current:%Y%m%d}", current.strftime("%Y-%m-%d %H:%M:%S"))
        con.commit()
    finally:
        con.close()
    return {"updated_at": current.strftime("%Y-%m-%d %H:%M:%S"), "symbols": results}


def ensure_history(base_dir: str | Path, codes: list[str], now: datetime | None, state: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    current = now or datetime.now()
    state = state if isinstance(state, dict) else _RUNTIME_STATES.setdefault(str(Path(base_dir)), {})
    marker = current.strftime("%Y%m%d")
    clean = [str(code) for code in dict.fromkeys(codes or []) if str(code or "").strip()]
    loaded = set(state.get("history_loaded_codes") or set())
    con = _connect(base_dir)
    try:
        refreshed = _meta_get(con, f"history_refresh:{marker}")
    finally:
        con.close()
    pending = []
    if not refreshed:
        pending = clean
    else:
        for code in clean:
            if code in loaded:
                continue
            _history, quality = history_120m(base_dir, code, current)
            _setup_history, setup_quality = history_15m(base_dir, code, current)
            if quality.get("history_120m_bars", 0) < REQUIRED_120M_BARS or not setup_quality.get("entry_ready"):
                pending.append(code)
    if pending:
        bootstrap_history(base_dir, pending, now=current)
        loaded.update(pending)
        state["history_loaded_codes"] = loaded
    return {code: history_120m(base_dir, code, current)[1] for code in clean}


def _load_today_minute_rows(base_dir: str | Path, code: str, day: str) -> tuple[list[dict[str, Any]], str | None]:
    con = _connect(base_dir)
    con.row_factory = sqlite3.Row
    try:
        for source in (PRIMARY_SOURCE, FALLBACK_SOURCE):
            today = [dict(row) for row in con.execute(
                'SELECT * FROM market_bars WHERE source=? AND symbol=? AND timeframe=? AND bar_end>=? AND bar_end<=? ORDER BY bar_end',
                (source, code, MINUTE_TIMEFRAME, day + ' 09:30:00', day + ' 15:00:00'))]
            if today:
                return today, source
    finally:
        con.close()
    return [], None


def _as_minute_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    cumulative_value = 0.0
    cumulative_volume = 0.0
    for item in rows or []:
        close = float(item["close"])
        high = float(item.get("high") if item.get("high") is not None else close)
        low = float(item.get("low") if item.get("low") is not None else close)
        open_price = float(item.get("open") if item.get("open") is not None else close)
        volume = float(item.get("volume") or 0.0)
        typical = (high + low + close) / 3
        cumulative_value += typical * volume
        cumulative_volume += volume
        out.append({
            "m": str(item["bar_end"])[-8:],
            "p": close,
            "o": open_price,
            "h": high,
            "l": low,
            "c": close,
            "v": volume,
            "avg_p": cumulative_value / cumulative_volume if cumulative_volume else close,
            "source": item.get("source"),
        })
    return out


def refresh_intraday_minutes(base_dir: str | Path, codes: list[str], now: datetime | None = None, state: dict[str, Any] | None = None, max_workers: int = 8, budget_seconds: float = 20.0) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    current = now or datetime.now()
    state = state if isinstance(state, dict) else _RUNTIME_STATES.setdefault(str(Path(base_dir)), {})
    clean = [str(code) for code in dict.fromkeys(codes or []) if str(code or "").strip()]
    key = current.strftime("%Y%m%d%H%M")
    seen = set(state.get("minute_codes") or set()) if state.get("minute_key") == key else set()
    pending = [code for code in clean if code not in seen]
    deferred = set(state.get('minute_deferred') or [])
    pending.sort(key=lambda code: code not in deferred)
    deadline = time.monotonic() + max(0.01, budget_seconds)

    def fetch(code: str) -> tuple[str, str, list[dict[str, Any]], str | None]:
        if time.monotonic() >= deadline:
            return code, PRIMARY_SOURCE, [], 'minute_budget_exhausted'
        try:
            bars = fetch_tencent_bars(code, MINUTE_TIMEFRAME)
            if bars:
                return code, PRIMARY_SOURCE, bars, None
        except Exception as exc:
            primary_error = str(exc)
        else:
            primary_error = "empty"
        if time.monotonic() >= deadline:
            return code, PRIMARY_SOURCE, [], 'minute_budget_exhausted'
        try:
            bars = fetch_sina_minline(code)
            return code, FALLBACK_SOURCE, bars, None if bars else primary_error
        except Exception as exc:
            return code, FALLBACK_SOURCE, [], f"{primary_error}; sina:{exc}"

    errors: dict[str, str] = {}
    completed = set()
    if pending:
        pool = ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(pending))))
        try:
            futures = [pool.submit(fetch, code) for code in pending]
            for future in as_completed(futures, timeout=max(0.01, deadline-time.monotonic())):
                code, source, bars, error = future.result()
                if bars:
                    upsert_bars(base_dir, code, MINUTE_TIMEFRAME, bars)
                if error:
                    errors[code] = error
                if error != 'minute_budget_exhausted':
                    completed.add(code)
        except FuturesTimeoutError:
            pass
        finally:
            # Workers only fetch; late results never write to the minute store.
            pool.shutdown(wait=False, cancel_futures=True)
        for code in set(pending)-completed:
            errors[code] = 'minute_budget_exhausted'
        state["minute_key"] = key
        state["minute_codes"] = seen | completed
        state['minute_deferred'] = sorted(set(pending)-completed)
    day = current.strftime("%Y-%m-%d")
    data: dict[str, list[dict[str, Any]]] = {}
    sources: dict[str, str] = {}
    completeness: dict[str, dict[str, Any]] = {}
    for code in clean:
        rows, source = _load_today_minute_rows(base_dir, code, day)
        data[code] = _as_minute_rows(rows)
        completeness[code] = minute_completeness(rows, current)
        if source:
            sources[code] = source
    return data, {
        "minute_key": key,
        "available_symbols": sum(1 for rows in data.values() if rows),
        "sources": sources,
        "errors": errors,
        "deferred_symbols": sorted(set(pending)-completed),
        "fetch_budget_seconds": budget_seconds,
        "completeness": completeness,
        "stale_symbols": sorted(code for code, item in completeness.items() if item.get("stale")),
        "entry_ready_symbols": sum(1 for item in completeness.values() if item.get("entry_ready")),
        "updated_at": current.strftime("%Y-%m-%d %H:%M:%S"),
    }


def preopen_readiness(base_dir: str | Path, codes: list[str], now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now()
    bootstrap = bootstrap_history(base_dir, codes, current)
    quality = {
        code: {
            "history_120m": history_120m(base_dir, code, current)[1],
            "history_15m": history_15m(base_dir, code, current)[1],
        }
        for code in codes
    }
    pending = {
        code: item["history_120m"]["blockers"] for code, item in quality.items()
        if item["history_120m"].get("structural_history_pending")
    }
    blocked = {
        code: list(item["history_120m"].get("blockers") or []) + list(item["history_15m"].get("blockers") or [])
        for code, item in quality.items()
        if (
            not item["history_120m"].get("entry_ready")
            or not item["history_15m"].get("entry_ready")
        ) and code not in pending
    }
    return {
        "bootstrap": bootstrap,
        "checked": len(quality),
        "ready": sum(
            1 for item in quality.values()
            if item["history_120m"].get("entry_ready") and item["history_15m"].get("entry_ready")
        ),
        "structural_history_pending": pending,
        "blocked": blocked,
        "status": "ok" if not blocked else "failed",
    }
