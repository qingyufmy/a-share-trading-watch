"""Secret-safe client for Tonghuashun Financial-API.

The public service exposes A-share snapshots and daily bars, but not minute
or tick data. It improves quote/daily data without replacing intraday bars.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any


API_BASE_URL = "https://fuyao.aicubes.cn"
KEYCHAIN_SERVICE = "a-share-trading-watch.hithink-finance"
SNAPSHOT_TTL_SECONDS = max(1, int(os.environ.get("A_SHARE_HITHINK_SNAPSHOT_TTL_SECONDS", "5")))
_KEY_CACHE: dict[str, Any] = {"checked_at": None, "key": None, "source": "missing"}
_SNAPSHOT_CACHE: dict[tuple[str, ...], dict[str, Any]] = {}
_LAST_STATUS: dict[str, Any] = {"state": "not_configured", "source": "none", "updated_at": None}


def thscode(code: str) -> str:
    """Convert a six-digit A-share symbol to the official API code."""
    symbol = str(code or "").strip().upper()
    if "." in symbol:
        return symbol
    if symbol.startswith(("4", "8", "92")):
        suffix = "BJ"
    elif symbol.startswith(("5", "6", "9")):
        suffix = "SH"
    else:
        suffix = "SZ"
    return f"{symbol}.{suffix}"


def _read_keychain() -> str | None:
    """Read an existing user credential without logging or persisting it."""
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-w", "-s", KEYCHAIN_SERVICE],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip() if result.returncode == 0 else ""
    return value or None


def api_key() -> tuple[str | None, str]:
    """Read Keychain first, then a user-level process environment variable."""
    now = time.monotonic()
    checked_at = _KEY_CACHE.get("checked_at")
    if checked_at is not None and now - float(checked_at) < 60:
        return _KEY_CACHE.get("key"), str(_KEY_CACHE.get("source") or "missing")
    value = _read_keychain()
    source = "keychain" if value else "missing"
    if not value:
        value = os.environ.get("HITHINK_FINANCE_API_KEY", "").strip() or None
        source = "environment" if value else "missing"
    _KEY_CACHE.update({"checked_at": now, "key": value, "source": source})
    return value, source


def api_status() -> dict[str, Any]:
    key, source = api_key()
    status = {
        **_LAST_STATUS,
        "configured": bool(key),
        "credential_source": source,
        "minute_bars_supported": False,
    }
    if key and status.get("state") == "not_configured":
        status.update({"state": "configured_idle", "source": "hithink"})
    return status


def _record_status(state: str, source: str, **extra: Any) -> None:
    _LAST_STATUS.update({
        "state": state,
        "source": source,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **extra,
    })


def _request_data(path: str, params: dict[str, Any], timeout: int = 8) -> dict[str, Any] | None:
    key, credential_source = api_key()
    if not key:
        _record_status("not_configured", "none")
        return None
    query = urllib.parse.urlencode({name: value for name, value in params.items() if value is not None})
    request = urllib.request.Request(
        f"{API_BASE_URL}{path}?{query}",
        headers={"X-api-key": key, "User-Agent": "a-share-trading-watch/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
        _record_status("request_failed", "hithink", credential_source=credential_source)
        return None
    if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        _record_status("api_rejected", "hithink", credential_source=credential_source, api_code=payload.get("code") if isinstance(payload, dict) else None)
        return None
    _record_status("ok", "hithink", credential_source=credential_source, request_id=payload.get("request_id"))
    return payload["data"]


def _timestamp_text(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value) / 1000).strftime("%Y%m%d%H%M%S")
    except (TypeError, ValueError, OSError):
        return datetime.now().strftime("%Y%m%d%H%M%S")


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_price_snapshots(codes: list[str] | tuple[str, ...], timeout: int = 8) -> dict[str, dict[str, Any]]:
    """Fetch official batch snapshots in the project's quote schema."""
    clean = tuple(dict.fromkeys(str(code).strip() for code in codes or [] if str(code).strip()))
    if not clean:
        return {}
    cached = _SNAPSHOT_CACHE.get(clean)
    now = time.monotonic()
    if cached and now - float(cached.get("fetched_at") or 0) < SNAPSHOT_TTL_SECONDS:
        return dict(cached.get("quotes") or {})
    data = _request_data("/api/a-share/prices/snapshot", {"thscodes": ",".join(thscode(code) for code in clean)}, timeout=timeout)
    if not data:
        return {}
    timestamp = _timestamp_text(data.get("timestamp"))
    quotes: dict[str, dict[str, Any]] = {}
    for item in data.get("item") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("ticker") or "").strip()
        close = _number(item.get("last_price"))
        prev_close = _number(item.get("prev_price"))
        if not code or close is None or close <= 0 or prev_close is None or prev_close <= 0:
            continue
        volume = _number(item.get("volume"), 0.0) or 0.0
        amount = _number(item.get("turnover"), 0.0) or 0.0
        quotes[code] = {
            "name": code,
            "code": code,
            "close": close,
            "prev_close": prev_close,
            "open": _number(item.get("open_price")),
            "volume_lot": volume / 100,
            "datetime": timestamp,
            "change": _number(item.get("price_change")),
            "pct": _number(item.get("price_change_ratio_pct")),
            "high": _number(item.get("high_price")),
            "low": _number(item.get("low_price")),
            "amount_wan": amount / 10000,
            "turnover": None,
            "quote_source": "hithink_finance_api",
        }
    _SNAPSHOT_CACHE[clean] = {"fetched_at": now, "quotes": dict(quotes)}
    return quotes


def fetch_daily_bars(code: str, start_ms: int, end_ms: int, timeout: int = 12) -> list[dict[str, Any]]:
    """Fetch forward-adjusted daily bars. Minute bars remain unsupported."""
    data = _request_data(
        "/api/a-share/prices/historical",
        {"thscode": thscode(code), "interval": "1d", "start": int(start_ms), "end": int(end_ms), "adjust": "forward"},
        timeout=timeout,
    )
    if not data:
        return []
    bars = []
    for item in data.get("item") or []:
        if not isinstance(item, dict):
            continue
        close = _number(item.get("close_price"))
        open_price = _number(item.get("open_price"))
        high = _number(item.get("high_price"))
        low = _number(item.get("low_price"))
        if None in (close, open_price, high, low) or min(close, open_price, high, low) <= 0:
            continue
        try:
            day = datetime.fromtimestamp(float(item.get("date_ms")) / 1000).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            continue
        bars.append({
            "date": day,
            "open": open_price,
            "close": close,
            "low": low,
            "high": high,
            "volume_lot": (_number(item.get("volume"), 0.0) or 0.0) / 100,
            "amount_wan": (_number(item.get("turnover"), 0.0) or 0.0) / 10000,
            "turnover": None,
            "source": "hithink_finance_api",
        })
    return bars
