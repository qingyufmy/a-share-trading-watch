#!/usr/bin/env python3
import json
import math
import os
import plistlib
import re
import sqlite3
import statistics
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path

import render_report_dashboard as dashboard
import paper_trading
from core import mira_evidence_log
from core import mira_research_gate
from core import cycle_framework
from core import emotion_leader_pool
from core import hithink_finance
from core import observation_strategy_router
from core import shadow_research_reporting
from core.strategy_discipline import BUY_SCENARIOS
from core.trading_rules import calc_limit_prices


def parse_env_datetime(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def current_datetime():
    return parse_env_datetime(os.environ.get("A_SHARE_NOW") or os.environ.get("A_SHARE_RUN_NOW")) or datetime.now()


BASE_DIR = Path(os.environ.get("A_SHARE_BASE_DIR", Path(__file__).resolve().parent))
REPORT_DATE = os.environ.get("A_SHARE_REPORT_DATE") or current_datetime().strftime("%Y-%m-%d")
REPORT_COMPACT_DATE = REPORT_DATE.replace("-", "")
THS_WATCHLIST_DIR = "/Users/tonyyu/Library/Containers/cn.com.10jqka.macstock/Data/Documents/cloud_store/blockstock_438990899/public"
DEFAULT_MY_STOCKS_FILE = f"{THS_WATCHLIST_DIR}/blockstock_438990899__public__CCM=.dat"
DEFAULT_OBSERVATION_POOL_FILE = f"{THS_WATCHLIST_DIR}/blockstock_438990899__public__CCk=.dat"
# Keep the legacy setting as the observation-pool override so existing launchd
# configuration continues to work while the complete priority universe expands.
MY_STOCKS_FILE = Path(os.environ.get("A_SHARE_THS_MY_STOCKS_FILE") or DEFAULT_MY_STOCKS_FILE)
OBSERVATION_POOL_FILE = Path(
    os.environ.get("A_SHARE_THS_OBSERVATION_POOL_FILE")
    or os.environ.get("A_SHARE_WATCHLIST_FILE")
    or DEFAULT_OBSERVATION_POOL_FILE
)
WATCHLIST_FILE = OBSERVATION_POOL_FILE
PRIORITY_WATCHLIST_GROUPS = (
    ("观察池", MY_STOCKS_FILE),
    ("我的股票", OBSERVATION_POOL_FILE),
)
# These are the user-maintained Tonghuashun folders visible in the desktop
# client. Every non-core folder is covered by the daily plan; classification
# and the matching strategy contract still decide whether it can trade.
THS_GROUP_FILE_ALIASES = {
    "CCM=": "观察池",
    "CCk=": "我的股票",
    "CCU=": "CPO",
    "CCY=": "铜相关",
    "CCg=": "商业航天",
    "CCQ=": "9月观察池",
    "CCc=": "策略盘",
    "CMsC": "消费",
    "CCo=": "0903",
}
DEFAULT_OBSERVATION_GROUP_SUFFIXES = tuple(THS_GROUP_FILE_ALIASES)
CORE_GROUP_FILES = {MY_STOCKS_FILE.resolve(), OBSERVATION_POOL_FILE.resolve()}
PREMARKET_PLAN_OBSERVATION_GROUPS = tuple(
    item.strip()
    for item in os.environ.get("A_SHARE_PREMARKET_PLAN_OBSERVATION_GROUPS", "ALL").split(",")
    if item.strip()
)
LAST_WATCHLIST_SYNC = {}
WATCHLIST_SNAPSHOT_MAX_AGE_SECONDS = int(
    os.environ.get("A_SHARE_WATCHLIST_SNAPSHOT_MAX_AGE_SECONDS") or 36 * 60 * 60
)
REPORT_PATH = BASE_DIR / f"同花顺我的股票盘后订盘_{REPORT_DATE}.md"
SIGNAL_STATE_DB = BASE_DIR / "data" / "runtime" / "signal_state.sqlite"
SIGNAL_REVIEW_DIR = BASE_DIR / "data" / "signal_reviews"
SIGNAL_REVIEW_HISTORY = BASE_DIR / "data" / "signal_quality_history.jsonl"
AUCTION_CANDIDATE_DIR = BASE_DIR / "data" / "auction_candidates"
ROTATION_CANDIDATE_DIR = BASE_DIR / "data" / "rotation_candidates"
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/fb89ba56-b06f-4197-aa69-9dda2f604f11"
DEFAULT_THS_COOKIE_FILE = "/Users/tonyyu/Library/Containers/cn.com.10jqka.macstock/Data/Library/Cookies/Cookies.binarycookies"
THS_COOKIE_FILE = Path(os.environ.get("A_SHARE_THS_COOKIE_FILE") or DEFAULT_THS_COOKIE_FILE)
FAST_REBUILD = os.environ.get("A_SHARE_FAST_REBUILD") == "1"
USE_QUOTE_CACHE = FAST_REBUILD or os.environ.get("A_SHARE_USE_QUOTE_CACHE") == "1"
SKIP_MINLINE = FAST_REBUILD or os.environ.get("A_SHARE_SKIP_MINLINE") == "1"
SKIP_THS_DISCOVER = FAST_REBUILD or os.environ.get("A_SHARE_SKIP_THS_DISCOVER") == "1"
SKIP_STOCK_CONTEXT = FAST_REBUILD or os.environ.get("A_SHARE_SKIP_STOCK_CONTEXT") == "1"


A_SHARE_HOLIDAY_RANGES = {
    2026: [
        ("2026-01-01", "2026-01-03"),
        ("2026-02-15", "2026-02-23"),
        ("2026-04-04", "2026-04-06"),
        ("2026-05-01", "2026-05-05"),
        ("2026-06-19", "2026-06-21"),
        ("2026-09-25", "2026-09-27"),
        ("2026-10-01", "2026-10-07"),
    ],
}


def holiday_dates(year):
    dates = set()
    for start, end in A_SHARE_HOLIDAY_RANGES.get(year, []):
        cur = datetime.strptime(start, "%Y-%m-%d").date()
        last = datetime.strptime(end, "%Y-%m-%d").date()
        while cur <= last:
            dates.add(cur)
            cur += timedelta(days=1)
    return dates


def is_a_share_trading_day(day):
    return day.weekday() < 5 and day not in holiday_dates(day.year)


def next_trading_date(report_date):
    day = datetime.strptime(report_date, "%Y-%m-%d").date() + timedelta(days=1)
    while not is_a_share_trading_day(day):
        day += timedelta(days=1)
    return day.strftime("%Y-%m-%d")


def previous_trading_date(report_date):
    day = datetime.strptime(report_date, "%Y-%m-%d").date() - timedelta(days=1)
    while not is_a_share_trading_day(day):
        day -= timedelta(days=1)
    return day.strftime("%Y-%m-%d")


NEXT_TRADING_DATE = os.environ.get("A_SHARE_NEXT_TRADING_DATE") or next_trading_date(REPORT_DATE)


def fetch_bytes(url, data=None, headers=None, timeout=15):
    req_headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://stockpage.10jqka.com.cn/",
    }
    if headers:
        req_headers.update(headers)
    if isinstance(data, str):
        data = data.encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers)
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.2 * (attempt + 1))
    raise last_error


def fetch_text(url, data=None, headers=None, timeout=15, encoding="utf-8"):
    raw = fetch_bytes(url, data=data, headers=headers, timeout=timeout)
    return raw.decode(encoding, "ignore")


def fetch_json(url, data=None, headers=None, timeout=15):
    text = fetch_text(url, data=data, headers=headers, timeout=timeout)
    return json.loads(text)


def load_ths_cookie_header():
    if not THS_COOKIE_FILE.exists():
        return ""
    try:
        raw = THS_COOKIE_FILE.read_bytes()
    except OSError:
        return ""
    if raw[:4] != b"cook":
        return ""
    page_count = int.from_bytes(raw[4:8], "big")
    page_sizes = [int.from_bytes(raw[8 + i * 4:12 + i * 4], "big") for i in range(page_count)]
    pos = 8 + 4 * page_count
    pairs = []
    for size in page_sizes:
        page = raw[pos:pos + size]
        pos += size
        if len(page) < 8:
            continue
        cookie_count = int.from_bytes(page[4:8], "little")
        offsets = [int.from_bytes(page[8 + i * 4:12 + i * 4], "little") for i in range(cookie_count)]
        for offset in offsets:
            cookie = page[offset:]
            if len(cookie) < 32:
                continue
            fields = re.findall(b".{4}", cookie[:32], flags=re.S)
            nums = [int.from_bytes(x, "little") for x in fields]

            def read_str(start):
                end = cookie.find(b"\x00", start)
                if end < 0:
                    return ""
                return cookie[start:end].decode("utf-8", "ignore")

            domain = read_str(nums[4])
            name = read_str(nums[5])
            value = read_str(nums[7])
            if "10jqka.com.cn" in domain and name and value:
                pairs.append(f"{name}={value}")
    return "; ".join(pairs)


THS_COOKIE_HEADER = None


def ths_cookie_header():
    """Load the optional Tonghuashun cookie only for community requests.

    The app-owned cookie file can block when macOS is updating it. Importing
    this module is part of the real-time engine path, so parsing it eagerly
    must never delay quote collection or signal evaluation.
    """
    global THS_COOKIE_HEADER
    if THS_COOKIE_HEADER is None:
        THS_COOKIE_HEADER = load_ths_cookie_header()
    return THS_COOKIE_HEADER


def pct(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return f"{v:.2f}%"


def money_yi(v):
    if v is None:
        return "-"
    return f"{v / 100000000:.2f}亿"


def f2(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return f"{v:.2f}"


def infer_market(code):
    code = str(code)
    if code.startswith("6"):
        return "17"
    if code.startswith(("8", "9")):
        return "151"
    return "33"


def read_watchlist_from_env():
    raw = os.environ.get("A_SHARE_WATCHLIST_CODES", "")
    if not raw.strip():
        return None
    rows = []
    for token in re.split(r"[,;\s]+", raw.strip()):
        if not token:
            continue
        parts = re.split(r"[:|]", token, maxsplit=1)
        code = parts[0].strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        market = parts[1].strip() if len(parts) > 1 and parts[1].strip() else infer_market(code)
        rows.append((code, market))
    return rows or None


def write_watchlist_fallback(rows):
    if not rows:
        return
    fallback = BASE_DIR / "watchlist_fallback.json"
    payload = [{"code": code, "market": market} for code, market in rows]
    try:
        fallback.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def watchlist_rows_payload(rows):
    return [{"code": code, "market": market} for code, market in rows]


def merge_watchlist_groups(group_rows):
    """Merge ordered Tonghuashun groups and retain every symbol's memberships."""
    merged = []
    memberships = {}
    seen = set()
    for name, rows in group_rows:
        for code, market in rows:
            key = (str(code), str(market))
            memberships.setdefault(key[0], []).append(name)
            if key in seen:
                continue
            seen.add(key)
            merged.append(key)
    return merged, memberships


def load_watchlist_snapshot(path=None):
    snapshot_path = path or (BASE_DIR / "data" / "runtime" / "watchlist_snapshot_latest.json")
    try:
        return json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def _snapshot_age_seconds(snapshot, now=None):
    updated_at = parse_env_datetime((snapshot or {}).get("updated_at"))
    if updated_at is None:
        return None
    return max(0.0, ((now or current_datetime()) - updated_at).total_seconds())


def watchlist_sync_entry_gate(sync=None, now=None):
    """Return whether the execution universe is verified enough for new risk.

    A stale fallback must never quietly authorize a new V2 buy.  A recently
    verified snapshot is allowed when launchd lacks container access, which
    keeps the realtime daemon usable after a user-context snapshot refresh.
    """
    sync = sync or LAST_WATCHLIST_SYNC or {}
    status = str(sync.get("status") or "unknown")
    if status in {"ready", "override"} and sync.get("rows"):
        return True, f"核心股票池已验证：{len(sync.get('rows') or [])}只（{status}）"
    if status == "snapshot_verified" and sync.get("rows"):
        age = _snapshot_age_seconds(sync, now=now)
        if age is not None and age <= WATCHLIST_SNAPSHOT_MAX_AGE_SECONDS:
            return True, (
                f"核心股票池使用已验证快照：{len(sync.get('rows') or [])}只，"
                f"快照滞后 {age / 3600:.1f} 小时"
            )
    detail = "；".join(str(item) for item in (sync.get("errors") or [])[:2])
    return False, (
        f"核心股票池未验证（{status}），禁止新增仓；"
        f"{detail or '需刷新同花顺我的股票/观察池快照'}"
    )


def watchlist_sync_changes(rows, groups, previous_snapshot=None):
    previous_snapshot = previous_snapshot or {}
    current_codes = {str(code) for code, _ in rows}
    previous_codes = {str(row.get("code")) for row in previous_snapshot.get("rows") or [] if row.get("code")}
    previous_groups = {
        str(group.get("name")): set(str(code) for code in group.get("codes") or [])
        for group in previous_snapshot.get("groups") or []
        if group.get("name")
    }
    group_changes = {}
    for group in groups:
        name = str(group.get("name") or "")
        current = set(str(code) for code in group.get("codes") or [])
        before = previous_groups.get(name, set())
        group_changes[name] = {
            "added": sorted(current - before),
            "removed": sorted(before - current),
        }
    return {
        "added": sorted(current_codes - previous_codes),
        "removed": sorted(previous_codes - current_codes),
        "group_changes": group_changes,
    }


def write_watchlist_snapshot(rows, source="unknown", sync=None):
    if not rows:
        return None
    sync = sync or {}
    groups = sync.get("groups") or []
    changes = watchlist_sync_changes(rows, groups, load_watchlist_snapshot())
    payload = {
        "updated_at": current_datetime().strftime("%Y-%m-%d %H:%M:%S"),
        "report_date": REPORT_DATE,
        "source": source,
        "count": len(rows),
        "rows": watchlist_rows_payload(rows),
        "groups": groups,
        "sync_status": sync.get("status") or "unknown",
        "sync_errors": sync.get("errors") or [],
        "changes": changes,
    }
    runtime_dir = BASE_DIR / "data" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    dated_path = runtime_dir / f"watchlist_snapshot_{REPORT_COMPACT_DATE}.json"
    latest_path = runtime_dir / "watchlist_snapshot_latest.json"
    # Preserve the last verified snapshot when a sandboxed launchd run can
    # only see the fallback list.  The dated artifact still records the
    # degraded run for audit, but it must not replace the execution baseline.
    paths = [dated_path]
    allowed, _reason = watchlist_sync_entry_gate({**sync, "rows": rows})
    if allowed or not latest_path.exists():
        paths.append(latest_path)
    for path in paths:
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    return dated_path


def plist_payload_to_bytes(payload):
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, dict):
        for key in ("NS.data", "NS.bytes", "self.value"):
            value = payload.get(key)
            if isinstance(value, bytes):
                return value
    return None


def parse_watchlist_payload(payload):
    raw = plist_payload_to_bytes(payload)
    if not raw:
        return None
    text = raw.decode("latin1", "ignore")
    # Tonghuashun group files may include Beijing Exchange codes such as
    # 920808.  The former [036] parser silently invalidated the entire group.
    m = re.search(r"((?:[03689]\d{5})(?:\|[03689]\d{5})*)\|,\s*([0-9|]+)\|", text)
    if not m:
        return None
    codes = m.group(1).split("|")
    markets = m.group(2).strip("|").split("|")
    if len(codes) != len(markets):
        return None
    return list(zip(codes, markets))


def read_watchlist_file(path):
    with Path(path).open("rb") as f:
        data = plistlib.load(f)
    for payload in data.get("$objects", []):
        rows = parse_watchlist_payload(payload)
        if rows:
            return rows
    raise RuntimeError("未找到可解析的同花顺自选代码")


def ths_group_name_for_path(path):
    path = Path(path)
    prefix = "blockstock_438990899__public__"
    suffix = path.name[len(prefix):-4] if path.name.startswith(prefix) and path.name.endswith(".dat") else path.stem
    return THS_GROUP_FILE_ALIASES.get(suffix, f"同花顺分组 {suffix}")


def read_observation_watchlist_details():
    """Load every populated user folder except the execution watchlist.

    This layer is daily/periodic research coverage only.  It never changes
    ``read_watchlist()`` and therefore cannot grant premarket plan or paper
    trading permissions to a stock merely because it appears in a folder.
    """
    root = Path(THS_WATCHLIST_DIR)
    groups = []
    successful = []
    errors = []
    from core import tracking_registry
    registry = tracking_registry.read(BASE_DIR)
    errors.extend(registry['errors'])
    try:
        # User-created group files are not consistently named ``CC*``.  For
        # example, the verified "消费" group is stored as ``CMsC.dat``.  Do
        # not glob every populated file: the Tonghuashun container also
        # contains broad internal aggregate groups that are not user folders.
        configured = set(DEFAULT_OBSERVATION_GROUP_SUFFIXES)
        configured.update(
            item.strip()
            for item in os.environ.get("A_SHARE_THS_OBSERVATION_GROUP_SUFFIXES", "").split(",")
            if item.strip()
        )
        prefix = "blockstock_438990899__public__"
        paths = sorted(root / f"{prefix}{suffix}.dat" for suffix in configured)
    except OSError as exc:
        snapshot = load_observation_watchlist_snapshot()
        age = _snapshot_age_seconds(snapshot)
        if snapshot.get("rows") and age is not None and age <= WATCHLIST_SNAPSHOT_MAX_AGE_SECONDS:
            return {
                **snapshot,
                "rows": snapshot_watchlist_rows(snapshot.get("rows")),
                "source": "observation_snapshot_verified",
                "status": "snapshot_verified",
                "errors": [str(exc), f"同花顺分组目录不可访问，使用已验证快照（{age / 3600:.1f}小时）"],
            }
        return {"rows": [], "groups": [], "errors": [str(exc)], "source": "tonghuashun_custom_groups", "status": "unavailable"}
    for path in paths:
        try:
            if path.resolve() in CORE_GROUP_FILES:
                continue
            rows = read_watchlist_file(path)
        except (OSError, plistlib.InvalidFileException, KeyError, TypeError, ValueError, RuntimeError) as exc:
            # Empty/default folders are normal in the local container; only
            # retain a real filesystem error.  It is material because the
            # realtime theme-promotion layer must know its coverage degraded.
            if isinstance(exc, OSError):
                errors.append(f"{ths_group_name_for_path(path)}读取失败：{exc}")
            continue
        if not rows:
            continue
        name = ths_group_name_for_path(path)
        group = {
            "name": name,
            "file": str(path),
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(rows),
            "codes": [code for code, _ in rows],
        }
        groups.append(group)
        successful.append((name, rows))
    # A verified registry preserves full-directory coverage, including stocks
    # outside the fixed native folders. It adds coverage, never order rights.
    for registered in registry['groups']:
        group = next((g for g in groups if g['name'] == registered['name']), None)
        if group is None:
            group = dict(registered)
            group['codes'] = list(registered['codes'])
            groups.append(group)
        else:
            group['codes'] = list(dict.fromkeys(group['codes'] + registered['codes']))
            group['count'] = len(group['codes'])
    successful = [(g['name'], [(c, infer_market(c)) for c in g['codes']]) for g in groups]
    core_codes = {code for code, _ in read_watchlist_details().get("rows") or []}
    rows, memberships = merge_watchlist_groups(successful)
    rows = [(code, market) for code, market in rows if code not in core_codes]
    for group in groups:
        group["core_overlap"] = [code for code in group["codes"] if code in core_codes]
        group["observation_codes"] = [code for code in group["codes"] if code not in core_codes]
        group["shared_codes"] = [code for code in group["observation_codes"] if len(memberships.get(code, [])) > 1]
    details = {
        "rows": rows,
        "groups": groups,
        "errors": errors,
        "source": "tonghuashun_custom_groups",
        "status": "ready" if groups else "empty",
        "memberships": {code: names for code, names in memberships.items() if code not in core_codes},
        "reference_instruments": registry['references'],
        "registry_verified_at": registry.get('verified_at'),
    }
    if groups:
        write_observation_watchlist_snapshot(details)
        return details

    snapshot = load_observation_watchlist_snapshot()
    age = _snapshot_age_seconds(snapshot)
    if snapshot.get("rows") and age is not None and age <= WATCHLIST_SNAPSHOT_MAX_AGE_SECONDS:
        return {
            **snapshot,
            "rows": snapshot_watchlist_rows(snapshot.get("rows")),
            "source": "observation_snapshot_verified",
            "status": "snapshot_verified",
            "errors": errors + [f"同花顺分组实时读取失败，使用已验证快照（{age / 3600:.1f}小时）"],
        }
    return details


def premarket_plan_all_groups_enabled():
    """Whether every Tonghuashun non-core group belongs to the plan universe."""
    requested = {str(item).strip().upper() for item in PREMARKET_PLAN_OBSERVATION_GROUPS}
    return bool(requested & {"ALL", "*", "全部", "全量", "全自选", "全自选观察池"})


def observation_plan_scope_label(group_name):
    """Keep the real folder visible while preserving one durable plan scope."""
    return f"全自选观察池/{str(group_name or '未分组')}"


def premarket_plan_observation_details(details=None):
    """Return the full Tonghuashun self-selected observation universe.

    Folder membership creates monitoring coverage, not trading eligibility.
    A symbol still needs a daily strategy classification, its matching entry
    contract, global permission and portfolio limits before a simulated order.
    """
    details = details or read_observation_watchlist_details()
    status = str(details.get("status") or "")
    allowed_statuses = {"ready", "snapshot_verified"}
    if status not in allowed_statuses:
        return {
            "rows": [],
            "groups": [],
            "status": status or "unavailable",
            "source": details.get("source"),
            "errors": list(details.get("errors") or []),
        }
    row_map = {str(code): str(market) for code, market in (details.get("rows") or [])}
    selected_all = premarket_plan_all_groups_enabled()
    selected_groups = []
    selected_rows = []
    seen = set()
    for group in details.get("groups") or []:
        name = str(group.get("name") or "")
        if not selected_all and name not in PREMARKET_PLAN_OBSERVATION_GROUPS:
            continue
        codes = [str(code) for code in (group.get("observation_codes") or [])]
        group_rows = []
        for code in codes:
            market = row_map.get(code) or infer_market(code)
            if code not in seen:
                selected_rows.append((code, market))
                seen.add(code)
            group_rows.append(code)
        selected_groups.append({"name": name, "codes": group_rows, "count": len(group_rows)})
    return {
        "rows": selected_rows,
        "groups": selected_groups,
        "status": status,
        "source": details.get("source"),
        "errors": list(details.get("errors") or []),
        "selection": "all_groups" if selected_all else "explicit_groups",
    }


def observation_watchlist_snapshot_path(path=None):
    return Path(path or (BASE_DIR / "data" / "runtime" / "watchlist_observation_snapshot_latest.json"))


def load_observation_watchlist_snapshot(path=None):
    try:
        return json.loads(observation_watchlist_snapshot_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def snapshot_watchlist_rows(rows):
    """Normalize persisted watchlist JSON back into quote-reader tuples."""
    normalized = []
    for row in rows or []:
        if isinstance(row, dict):
            code = str(row.get("code") or "").strip()
            market = str(row.get("market") or "").strip()
        elif isinstance(row, (list, tuple)) and row:
            code = str(row[0] or "").strip()
            market = str(row[1] or "").strip() if len(row) > 1 else ""
        else:
            continue
        if not code:
            continue
        normalized.append((code, market or infer_market(code)))
    return normalized


def write_observation_watchlist_snapshot(details):
    """Persist an accessible, auditable copy of non-core Tonghuashun groups."""
    details = details or {}
    rows = details.get("rows") or []
    if not rows:
        return None
    runtime_dir = BASE_DIR / "data" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": current_datetime().strftime("%Y-%m-%d %H:%M:%S"),
        "report_date": REPORT_DATE,
        "rows": watchlist_rows_payload(rows),
        "groups": details.get("groups") or [],
        "memberships": details.get("memberships") or {},
        "status": "ready",
        "reference_instruments": details.get("reference_instruments") or [],
        "registry_verified_at": details.get("registry_verified_at"),
    }
    dated = runtime_dir / f"watchlist_observation_snapshot_{REPORT_COMPACT_DATE}.json"
    latest = observation_watchlist_snapshot_path()
    for target in (dated, latest):
        try:
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    return dated


def observation_group_summary(details, quotes, per_group_limit=2):
    """Compact, non-executable status for every user-maintained group."""
    details = details or {}
    quotes = quotes or {}
    summaries = []
    references = details.get('reference_instruments') or []
    if references:
        summaries.append({
            'name': '非ETF参考观察（不下单）', 'count': len(references), 'quoted': 0,
            'strong': '、'.join(f"{r['name']}({r['code']})" for r in references),
            'weak': '仅登记参考观察，不进入个股策略；独立行情尚未接入',
            'updated_at': details.get('registry_verified_at'), 'core_overlap': [],
        })
    for group in details.get("groups") or []:
        codes = group.get("observation_codes") or []
        group_quotes = [quotes[code] for code in codes if code in quotes and quotes[code].get("close", 0) > 0]
        ranked = sorted(group_quotes, key=lambda quote: float(quote.get("pct") or 0), reverse=True)
        strongest = ranked[:per_group_limit]
        weakest = list(reversed(ranked[-per_group_limit:]))
        strong_text = "、".join(f"{q.get('name') or q.get('code')} {pct(q.get('pct'))}" for q in strongest) or "暂无有效行情"
        weak_text = "、".join(f"{q.get('name') or q.get('code')} {pct(q.get('pct'))}" for q in weakest) or "暂无有效行情"
        summaries.append({
            "name": group.get("name") or "同花顺分组",
            "count": len(codes),
            "quoted": len(group_quotes),
            "strong": strong_text,
            "weak": weak_text,
            "updated_at": group.get("updated_at"),
            "core_overlap": group.get("core_overlap") or [],
        })
    return summaries


def observation_group_summary_lines(summaries, limit=10):
    if not summaries:
        return ["- 同花顺其他自选分组暂无可覆盖标的。"]
    lines = []
    for item in summaries[:limit]:
        overlap = f"；与核心池重合 {len(item['core_overlap'])}只已去重" if item.get("core_overlap") else ""
        lines.append(
            f"- {item['name']}：观察 {item['count']}只，行情覆盖 {item['quoted']}/{item['count']}；"
            f"强势 {item['strong']}；弱势 {item['weak']}{overlap}。"
        )
    return lines


def observation_strategy_summary_lines(stocks, limit=8):
    """Compact, auditable coverage summary for every non-core self-select."""
    observation = [stock for stock in (stocks or []) if stock.get("observation_plan_group")]
    if not observation:
        return ["- 全自选观察池暂无已同步标的。"]
    buckets = {}
    for stock in observation:
        contract = stock.get("strategy_contract") or {}
        key = str(contract.get("key") or observation_strategy_router.OBSERVE)
        buckets.setdefault(key, []).append(stock)
    lines = [f"- 全自选观察池策略覆盖 {len(observation)}只：分类决定订单方法，未分类标的保持盯盘但仓位资格为0。"]
    for key in (
        observation_strategy_router.LEADER,
        observation_strategy_router.TREND_520,
        observation_strategy_router.TREND_MA5,
        observation_strategy_router.OBSERVE,
    ):
        members = buckets.get(key) or []
        if not members:
            continue
        meta = observation_strategy_router.STRATEGY_META.get(key, {})
        labels = "、".join(
            f"{stock['quote'].get('code')} {stock['quote'].get('name')}"
            for stock in members[:limit]
        )
        extra = f" 等{len(members)}只" if len(members) > limit else ""
        method = "；".join(meta.get("allowed_patterns") or []) or "不开放新增仓"
        formal_signal = observation_strategy_router.formal_entry_signal(key)
        lines.append(
            f"- {meta.get('style') or '未分类'}｜{meta.get('name') or key}：{len(members)}只｜"
            f"正式信号 {formal_signal}｜时机证据 {method}｜{labels}{extra}。"
        )
    return lines


def emotion_leader_summary_lines(stocks, limit=12):
    candidates = []
    source_date = None
    for stock in stocks or []:
        item = stock.get("emotion_leader_candidate") or {}
        if not item:
            continue
        candidates.append(item)
        source_date = source_date or item.get("source_date")
    return emotion_leader_pool.summary_lines(
        {"source_date": source_date, "candidates": candidates},
        limit=limit,
    )


def watchlist_sync_summary(sync=None):
    sync = sync or LAST_WATCHLIST_SYNC
    groups = sync.get("groups") or []
    group_text = "、".join(f"{item.get('name')} {item.get('count', 0)}只" for item in groups)
    count = len(sync.get("rows") or [])
    status = sync.get("status") or "unknown"
    if status == "ready":
        return f"同花顺重点自选已同步：{group_text}，去重后 {count} 只。"
    if status == "fallback_unverified":
        errors = "；".join(sync.get("errors") or [])
        return (
            f"同花顺重点自选未完成同步：当前仅使用本地回退清单 {count} 只，"
            f"无法确认与“我的股票/观察池”一致。{errors}"
        )
    errors = "；".join(sync.get("errors") or [])
    return f"同花顺重点自选同步{status}：{group_text or '无可用分组'}，去重后 {count} 只。{errors}"


def read_watchlist_details():
    global LAST_WATCHLIST_SYNC
    env_rows = read_watchlist_from_env()
    if env_rows:
        write_watchlist_fallback(env_rows)
        sync = {
            "rows": env_rows,
            "source": "env:A_SHARE_WATCHLIST_CODES",
            "status": "override",
            "errors": [],
            "groups": [{
                "name": "环境变量覆盖",
                "file": None,
                "count": len(env_rows),
                "codes": [code for code, _ in env_rows],
            }],
        }
        write_watchlist_snapshot(env_rows, source=sync["source"], sync=sync)
        LAST_WATCHLIST_SYNC = sync
        return sync

    groups = []
    errors = []
    successful_groups = []
    for name, path in PRIORITY_WATCHLIST_GROUPS:
        try:
            rows = read_watchlist_file(path)
            groups.append({
                "name": name,
                "file": str(path),
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "count": len(rows),
                "codes": [code for code, _ in rows],
            })
            successful_groups.append((name, rows))
        except (OSError, plistlib.InvalidFileException, KeyError, TypeError, ValueError, RuntimeError) as exc:
            groups.append({"name": name, "file": str(path), "count": 0, "codes": [], "error": str(exc)})
            errors.append(f"{name}读取失败：{exc}")

    if successful_groups:
        rows, memberships = merge_watchlist_groups(successful_groups)
        for group in groups:
            if group.get("codes"):
                group["shared_codes"] = [
                    code for code in group["codes"] if len(memberships.get(code, [])) > 1
                ]
        sync = {
            "rows": rows,
            "source": "tonghuashun_priority_groups",
            "status": "ready" if len(successful_groups) == len(PRIORITY_WATCHLIST_GROUPS) else "degraded",
            "errors": errors,
            "groups": groups,
        }
        write_watchlist_fallback(rows)
        write_watchlist_snapshot(rows, source=sync["source"], sync=sync)
        LAST_WATCHLIST_SYNC = sync
        return sync

    fallback = BASE_DIR / "watchlist_fallback.json"
    if fallback.exists():
        try:
            data = json.loads(fallback.read_text(encoding="utf-8"))
            rows = [(str(x["code"]), str(x.get("market") or infer_market(x["code"]))) for x in data]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append(f"本地备份读取失败：{exc}")
        else:
            snapshot = load_watchlist_snapshot()
            snapshot_age = _snapshot_age_seconds(snapshot)
            snapshot_rows = [
                (str(item.get("code")), str(item.get("market") or infer_market(item.get("code"))))
                for item in (snapshot.get("rows") or [])
                if item.get("code")
            ]
            snapshot_ok = bool(
                snapshot.get("sync_status") in {"ready", "override"}
                and snapshot_rows
                and snapshot_age is not None
                and snapshot_age <= WATCHLIST_SNAPSHOT_MAX_AGE_SECONDS
            )
            if snapshot_ok:
                sync = {
                    "rows": snapshot_rows,
                    "source": str(BASE_DIR / "data" / "runtime" / "watchlist_snapshot_latest.json"),
                    "status": "snapshot_verified",
                    "updated_at": snapshot.get("updated_at"),
                    "errors": errors + [
                        f"同花顺“我的股票/观察池”目录当前不可访问，使用已验证快照（{snapshot_age / 3600:.1f}小时）"
                    ],
                    "groups": snapshot.get("groups") or groups,
                }
                LAST_WATCHLIST_SYNC = sync
                return sync
            sync = {
                "rows": rows,
                "source": str(fallback),
                "status": "fallback_unverified",
                "errors": errors + ["同花顺“我的股票/观察池”目录当前不可访问，回退清单不能视为已同步"],
                "groups": groups,
            }
            write_watchlist_snapshot(rows, source=sync["source"], sync=sync)
            LAST_WATCHLIST_SYNC = sync
            return sync
    raise RuntimeError("无法解析同花顺我的股票自选列表，且无 watchlist_fallback.json")


def read_watchlist():
    return read_watchlist_details()["rows"]


def stock_symbol(code):
    code = str(code)
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("8", "9")):
        return "bj" + code
    return "sz" + code


def eastmoney_secid(code):
    code = str(code)
    market = "1" if code.startswith("6") else "0"
    return f"{market}.{code}"


def em_code(code):
    return ("SH" if code.startswith("6") else "SZ") + code


def parse_tencent_quotes(codes):
    # Prefer the official Tonghuashun snapshot when a user-level credential is
    # available. Tencent still enriches fields that the official snapshot does
    # not currently expose (notably turnover and the Chinese security name).
    try:
        from core import hithink_finance

        requested = [str(code) for code, _market in codes]
        official = hithink_finance.fetch_price_snapshots(requested)
    except Exception:
        official = {}
    query = ",".join(stock_symbol(c) for c, _ in codes) + ",sh000001,sz399001,sz399006"
    try:
        text = fetch_bytes(f"https://qt.gtimg.cn/q={query}", timeout=20).decode("gbk", "ignore")
    except Exception:
        if official:
            return official
        raise
    quotes = {}
    for line in text.strip().splitlines():
        if not line or '="' not in line:
            continue
        key = line.split("=", 1)[0].replace("v_", "")
        body = line.split('"', 1)[1].rsplit('"', 1)[0]
        parts = body.split("~")
        if len(parts) < 40:
            continue
        code = parts[2]
        try:
            quotes[code] = {
                "name": parts[1],
                "code": code,
                "close": float(parts[3]),
                "prev_close": float(parts[4]),
                "open": float(parts[5]),
                "volume_lot": float(parts[6]),
                "datetime": parts[30],
                "change": float(parts[31]),
                "pct": float(parts[32]),
                "high": float(parts[33]),
                "low": float(parts[34]),
                "amount_wan": float(parts[37]),
                "turnover": float(parts[38]) if parts[38] else None,
            }
        except ValueError:
            continue
    # Official coverage wins on populated price fields.  Legacy enrichment is
    # retained for fields absent from the official schema so a newly configured
    # API cannot silently turn turnover into zero in downstream strategy gates.
    for code, quote in official.items():
        legacy = quotes.get(code) or {}
        merged = dict(legacy)
        merged.update({key: value for key, value in quote.items() if value is not None})
        if legacy.get("name") and (not quote.get("name") or quote.get("name") == code):
            merged["name"] = legacy["name"]
        if quote.get("turnover") is None and legacy.get("turnover") is not None:
            merged["turnover"] = legacy["turnover"]
        quotes[code] = merged
    return quotes


def fetch_indexes():
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=1.000001,0.399001,0.399006&fields=f12,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18"
    try:
        data = fetch_json(url, timeout=8)
    except Exception:
        return fetch_tencent_indexes()
    out = []
    for row in data.get("data", {}).get("diff", []):
        out.append({
            "code": row.get("f12"),
            "name": row.get("f14"),
            "close": row.get("f2"),
            "pct": row.get("f3"),
            "amount": row.get("f6"),
            "high": row.get("f15"),
            "low": row.get("f16"),
            "open": row.get("f17"),
            "prev_close": row.get("f18"),
        })
    return out


def fetch_tencent_indexes():
    text = fetch_bytes("https://qt.gtimg.cn/q=sh000001,sz399001,sz399006", timeout=8).decode("gbk", "ignore")
    out = []
    for line in text.strip().splitlines():
        if not line or '="' not in line:
            continue
        body = line.split('"', 1)[1].rsplit('"', 1)[0]
        parts = body.split("~")
        if len(parts) < 38:
            continue
        try:
            out.append({
                "code": parts[2],
                "name": parts[1],
                "close": float(parts[3]),
                "pct": float(parts[32]),
                "amount": float(parts[37]) * 10000,
                "high": float(parts[33]),
                "low": float(parts[34]),
                "open": float(parts[5]),
                "prev_close": float(parts[4]),
            })
        except ValueError:
            continue
    return out


def fetch_boards():
    # The provider caps a page at 100 even when pz=120 is requested.
    query = "pz=100&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid=f12&fs=m:90+t:2,m:90+t:3&fields=f12,f14,f2,f3,f62"
    data = None
    for host in (
        "https://push2delay.eastmoney.com/api/qt/clist/get?",
        "https://82.push2.eastmoney.com/api/qt/clist/get?",
        "https://push2.eastmoney.com/api/qt/clist/get?",
    ):
        try:
            data = fetch_json(host + "pn=1&" + query, headers={"Referer": "https://quote.eastmoney.com/center/gridlist.html"}, timeout=10)
            if data.get("data", {}).get("diff"):
                break
        except Exception:
            data = None
    if not data:
        return [], {}
    first = data.get("data") or {}
    boards = list(first.get("diff") or [])
    total = int(first.get("total") or len(boards))
    page_count = min(30, math.ceil(total / 100))
    failed_pages = []

    def fetch_page(page):
        try:
            payload = fetch_json(host + f"pn={page}&" + query,
                                 headers={"Referer": "https://quote.eastmoney.com/center/gridlist.html"}, timeout=5)
            rows = (payload.get("data") or {}).get("diff") or []
            return page, rows
        except Exception:
            return page, []

    if page_count > 1:
        with ThreadPoolExecutor(max_workers=6) as pool:
            for page, rows in pool.map(fetch_page, range(2, page_count + 1)):
                if not rows:
                    failed_pages.append(page)
                boards.extend(rows)
    unique = {str(row.get("f12") or row.get("f14")): row for row in boards}
    def strength(row):
        try:
            return float(row.get("f3"))
        except (TypeError, ValueError):
            return -999.0

    boards = sorted(unique.values(), key=strength, reverse=True)
    coverage = {"expected": total, "received": len(boards), "failed_pages": failed_pages,
                "complete": len(boards) >= total, "fetched_at": current_datetime().strftime("%Y-%m-%d %H:%M:%S")}
    for row in boards:
        row["_coverage"] = coverage
    keep = {}
    keys = ["PCB", "印制电路板", "CPO", "光通信", "算力", "数据中心", "半导体", "先进封装", "培育钻石", "玻璃", "玻纤", "元件", "军工", "低空", "光刻", "MLCC", "被动元件"]
    for row in boards:
        name = row.get("f14", "")
        if any(k in name for k in keys):
            keep[name] = row
    # Realtime sector resonance must inspect the complete bounded response.
    # Cutting this to the first 30/40 made valid boards near the ranking edge
    # disappear between refreshes even though they remained strongly positive.
    return boards, keep


def fetch_sohu_daily(code):
    try:
        from core import hithink_finance

        start = int(datetime.strptime("20250101", "%Y%m%d").timestamp() * 1000)
        end = int(datetime.strptime(REPORT_COMPACT_DATE, "%Y%m%d").timestamp() * 1000)
        official_rows = hithink_finance.fetch_daily_bars(code, start, end)
        if official_rows:
            previous = None
            normalized = []
            for row in official_rows:
                close = row["close"]
                normalized.append({
                    **row,
                    "change": close - previous if previous is not None else 0.0,
                    "pct": ((close / previous) - 1) * 100 if previous else 0.0,
                })
                previous = close
            return normalized
    except Exception:
        pass
    url = f"https://q.stock.sohu.com/hisHq?code=cn_{code}&start=20250101&end={REPORT_COMPACT_DATE}&stat=1&order=D&period=d&callback=historySearchHandler&rt=jsonp"
    text = fetch_text(url, timeout=20)
    m = re.search(r"historySearchHandler\((.*)\)$", text, re.S)
    if not m:
        return []
    data = json.loads(m.group(1))
    rows = data[0].get("hq", []) if data and data[0].get("status") == 0 else []
    out = []
    for r in rows:
        try:
            out.append({
                "date": r[0],
                "open": float(r[1]),
                "close": float(r[2]),
                "change": float(r[3]),
                "pct": float(str(r[4]).replace("%", "")),
                "low": float(r[5]),
                "high": float(r[6]),
                "volume_lot": float(r[7]),
                "amount_wan": float(r[8]),
                "turnover": float(str(r[9]).replace("%", "")),
            })
        except (ValueError, IndexError):
            pass
    return list(reversed(out))


def fetch_minline_eastmoney(code, timeout=4):
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


def fetch_minline_sina(code, timeout=4):
    url = f"https://quotes.sina.cn/cn/api/openapi.php/CN_MinlineService.getMinlineData?symbol={stock_symbol(code)}&dpc=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.cn/"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        return data.get("result", {}).get("data", [])
    except Exception:
        return []


def fetch_minline(code):
    if SKIP_MINLINE:
        return []
    rows = fetch_minline_eastmoney(code)
    if rows:
        return rows
    return fetch_minline_sina(code)


def simple_ma(values, n):
    if len(values) < n:
        return None
    return statistics.mean(values[-n:])


def macd(closes):
    if not closes:
        return None, None, None
    ema12 = closes[0]
    ema26 = closes[0]
    dea = 0
    for c in closes:
        ema12 = ema12 * 11 / 13 + c * 2 / 13
        ema26 = ema26 * 25 / 27 + c * 2 / 27
        dif = ema12 - ema26
        dea = dea * 8 / 10 + dif * 2 / 10
    return dif, dea, (dif - dea) * 2


def aggregate_intraday(rows, minutes):
    if not rows:
        return []
    bars = []
    cur = None
    base_day = REPORT_DATE
    for row in rows:
        try:
            hh, mm, _ = row["m"].split(":")
            idx = int(hh) * 60 + int(mm)
            bucket = idx // minutes
            price = float(row["p"])
            vol = float(row.get("v") or 0)
        except Exception:
            continue
        if cur is None or cur["bucket"] != bucket:
            if cur:
                bars.append(cur)
            cur = {"bucket": bucket, "time": f"{base_day} {hh}:{mm}", "open": price, "high": price, "low": price, "close": price, "volume": vol}
        else:
            cur["high"] = max(cur["high"], price)
            cur["low"] = min(cur["low"], price)
            cur["close"] = price
            cur["volume"] += vol
    if cur:
        bars.append(cur)
    return bars


def trend_text(daily, quote):
    closes = [x["close"] for x in daily]
    mas = {n: simple_ma(closes, n) for n in (5, 10, 20, 30, 60)}
    dif, dea, hist = macd(closes)
    close = quote["close"]
    pct_chg = quote["pct"]
    amount = quote.get("amount_wan", 0)
    avg_amount5 = statistics.mean([x["amount_wan"] for x in daily[-6:-1]]) if len(daily) >= 6 else None
    vol_ratio = amount / avg_amount5 if avg_amount5 else None
    today_low = quote["low"]
    recent_high = max(x["high"] for x in daily[-20:]) if len(daily) >= 20 else quote["high"]
    recent_low = min(x["low"] for x in daily[-20:]) if len(daily) >= 20 else quote["low"]
    below20 = mas[20] is not None and close < mas[20]
    above20 = mas[20] is not None and close >= mas[20]
    high_vol_drop = pct_chg <= -4 and (vol_ratio or 0) >= 1.2
    limit_up = pct_chg >= 9.8 or (code_is_20cm(quote["code"]) and pct_chg >= 13)

    if high_vol_drop and below20:
        state = "高位放量下跌/弱修复"
        color = "🔴"
        priority = "P0"
    elif below20:
        state = "弱趋势修复"
        color = "🟡"
        priority = "P1"
    elif limit_up:
        state = "强情绪涨停/高波动"
        color = "🟡"
        priority = "P1"
    elif above20 and close >= (mas[5] or close) * 0.98 and pct_chg >= 0:
        state = "强趋势延续"
        color = "🟢"
        priority = "P2"
    else:
        state = "关键位观察"
        color = "🟡"
        priority = "P1"

    if quote["code"] == "605168":
        state, color, priority = "高位急跌后防守观察", "🟡", "P1"
    if quote["code"] == "300775":
        state, color, priority = "弱趋势修复/回避优先", "🔴", "P0"
    if quote["code"] == "600589":
        state, color, priority = "涨停弱修复观察", "🟡", "P1"

    repair_candidates = [mas[n] for n in (5, 10, 20) if mas[n] and mas[n] > close]
    # A repair trigger is an actionable next-session level, not a distant
    # 20-day high that cannot be reached within one trading day's price band.
    raw_repair = min(repair_candidates) if repair_candidates else max(quote["high"], close)
    # "支撑"是回踩后观察承接的位置；"防守"则是支撑失效后的硬风控线。
    # 两者不能从同一候选集取同一个最大值，否则页面看似给了两层风控，
    # 实际却没有留出结构失效的判断空间。
    support_candidates = [today_low, mas[5], mas[10], mas[20], mas[30], mas[60], recent_low]
    support_candidates = [float(x) for x in support_candidates if x and x <= close]
    support = max(support_candidates or [today_low])
    min_gap = max(0.01, close * 0.008)
    lower_structure = [x for x in support_candidates if x <= support - min_gap]
    # 优先使用下一层日线/低点结构；没有足够分离的结构时，采用 1.2% 的硬失效缓冲。
    defense = max(lower_structure) if lower_structure else support * 0.988
    defense = min(defense, support - 0.01)
    trend_pressure = recent_high
    next_limit_up, _ = calc_limit_prices(quote["code"], quote.get("name") or "", close)
    repair = min(raw_repair, next_limit_up) if next_limit_up and raw_repair > close else raw_repair
    pressure = min(trend_pressure, next_limit_up) if next_limit_up else trend_pressure
    pressure_limited_by_board = bool(next_limit_up and trend_pressure > next_limit_up + 0.005)
    pressure_kind = "当日涨停边界" if pressure_limited_by_board else "当日趋势压力"

    if color == "🟢":
        if support <= close * 0.997:
            add_trigger = support
            add_mode = "回踩承接"
            add_confirm = (
                f"情绪：所属方向不弱于市场；筹码：回踩 {f2(support)} 缩量止跌；"
                f"时间：避开开盘前15分钟追价；三周期：日线不破防守、30分钟守住修复结构、"
                "5分钟止跌后1/3分钟放量收回VWAP。"
            )
            add_cancel = f"5分钟收盘跌破 {f2(support)} 或30分钟跌破防守 {f2(defense)}，取消加仓。"
            if pressure_limited_by_board:
                add = f"回踩承接 {f2(support)}；{add_confirm} 趋势压力 {f2(trend_pressure)} 为跨日参考，冲当日涨停边界 {f2(pressure)} 不追。"
            else:
                add = f"回踩承接 {f2(support)}；{add_confirm} 接近压力 {f2(pressure)} 不追价。"
        elif pressure > close * 1.003 and not pressure_limited_by_board:
            add_trigger = pressure
            add_mode = "突破确认"
            add_confirm = (
                f"情绪：板块同步增强；筹码：放量突破 {f2(pressure)} 后回踩不破；"
                "时间：突破后至少延续15分钟；三周期：日线趋势向上、30分钟收盘站稳、"
                "5分钟量能不低于近20根均量的1.2倍。"
            )
            add_cancel = f"跌回 {f2(pressure)} 下方或30分钟跌破防守 {f2(defense)}，取消加仓。"
            add = f"突破确认 {f2(pressure)}；{add_confirm}"
        else:
            add_trigger = None
            add_mode = "等待回踩"
            add_confirm = "现价贴近支撑或压力，盈亏比不足，不追价；等待形成新的回踩承接结构。"
            add_cancel = f"跌破防守 {f2(defense)} 仅执行风控。"
            add = "暂无加仓价/等待回踩" if not pressure_limited_by_board else "暂无加仓价/接近当日涨停边界不追"
    elif color == "🟡":
        add_trigger = repair
        add_mode = "突破修复"
        add_confirm = (
            f"情绪：板块同步转强且非单股脉冲；筹码：放量突破 {f2(repair)} 后回踩不破；"
            f"时间：突破至少延续15分钟；三周期：日线收回修复位、30分钟收盘站稳、"
            "5分钟量能不低于近20根均量的1.2倍。"
        )
        add_cancel = f"跌回 {f2(repair)} 下方或30分钟收盘跌破防守 {f2(defense)}，取消加仓。"
        add = f"突破修复 {f2(repair)}；{add_confirm}"
    else:
        add_trigger = None
        add_mode = "禁止"
        add_confirm = "日线趋势和情绪未修复，禁止逆势摊平或追反弹。"
        add_cancel = f"先等待收回修复位 {f2(repair)}；跌破防守 {f2(defense)} 仅执行风控。"
        add = "暂无加仓价/不加仓"

    if pressure_limited_by_board:
        reduce = f"当日涨停边界 {f2(pressure)}；趋势压力 {f2(trend_pressure)} 仅作跨日参考，不作为当日减仓触发；风险减仓：放量跌破 {f2(defense)}"
    else:
        reduce = f"压力减仓：反抽 {f2(pressure)} 附近无量或冲高回落；风险减仓：放量跌破 {f2(defense)}"
    stop = f"{f2(defense)}"
    return {
        "mas": mas,
        "macd": {"dif": dif, "dea": dea, "hist": hist},
        "vol_ratio": vol_ratio,
        "state": state,
        "color": color,
        "priority": priority,
        "support": support,
        "defense": defense,
        "repair": repair,
        "pressure": pressure,
        "trend_pressure": trend_pressure,
        "next_limit_up": next_limit_up,
        "pressure_kind": pressure_kind,
        "add_trigger": add_trigger,
        "add_mode": add_mode,
        "add_confirm": add_confirm,
        "add_cancel": add_cancel,
        "add": add,
        "reduce": reduce,
        "stop": stop,
        "recent_low": recent_low,
    }


def code_is_20cm(code):
    return code.startswith("30") or code.startswith("68")


def fetch_ths_news(code, market):
    out = {"news": [], "notice": [], "community_available": False, "community_note": "同花顺社区接口未验证"}
    for key, path in [("news", "news"), ("notice", "noticeReport")]:
        url = f"https://flow.10jqka.com.cn/pc/timeline/v1/base/{path}/{market}/{code}/first/1"
        try:
            data = fetch_json(url, timeout=12)
            if isinstance(data, dict) and data.get("data"):
                out[key] = data.get("data")[:6]
        except Exception as exc:
            out[key + "_error"] = str(exc)

    try:
        cookie = ths_cookie_header()
        cookie_headers = {"Cookie": cookie} if cookie else {}
        html = fetch_text(f"https://t.10jqka.com.cn/guba/{code}/", headers=cookie_headers, timeout=10)
        fid = re.search(r'data-fid="(\d+)"', html)
        if fid:
            payload = f"type=1&fid={fid.group(1)}&first=1"
            data = fetch_json(
                "https://t.10jqka.com.cn/newcircle/post/getPostList/",
                data=payload,
                headers={
                    **cookie_headers,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"https://t.10jqka.com.cn/guba/{code}/",
                },
                timeout=10,
            )
            if data.get("errorCode") == 0:
                html_text = data.get("result", {}).get("html", "")
                blocks = re.findall(r'<div class="post-title">(.*?)</div>', html_text, re.S)
                contents = re.findall(r'<div class="post-content">(.*?)</div>', html_text, re.S)
                titles = []
                for block in blocks:
                    text = re.sub(r"<.*?>", " ", block)
                    text = unescape(re.sub(r"\s+", " ", text)).strip()
                    if text:
                        titles.append(text)
                content_texts = []
                for block in contents[:8]:
                    text = re.sub(r"<.*?>", " ", block)
                    text = unescape(re.sub(r"\s+", " ", text)).strip()
                    if text:
                        content_texts.append(text[:120])
                out["community_available"] = True
                out["community_titles"] = titles[:10]
                out["community_contents"] = content_texts
                out["community_note"] = "同花顺社区帖子可读，取 type=1 最新帖子列表" if titles else "同花顺社区可读，但最新帖子列表为空/样本不足"
            elif "未登录" in data.get("errorMsg", ""):
                out["community_note"] = "同花顺社区帖子接口返回未登录"
            else:
                out["community_note"] = f"同花顺社区接口返回：{data.get('errorMsg')}"
    except Exception as exc:
        out["community_note"] = f"同花顺社区读取失败：{exc}"
    return out


def ths_titles(items):
    titles = []
    for item in items:
        if isinstance(item, dict):
            title = item.get("title")
            tag = item.get("tag")
            abstract = item.get("abstract")
            if title:
                titles.append((tag or "资讯", title, abstract or ""))
    return titles


def fetch_f10(code):
    result = {"profile": None, "concepts": [], "holder": None}
    try:
        survey = fetch_json(f"https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={em_code(code)}", timeout=12)
        jbzl = (survey.get("jbzl") or [None])[0]
        if jbzl:
            profile = (jbzl.get("ORG_PROFILE") or "").strip()
            result["profile"] = {
                "industry": jbzl.get("EM2016") or jbzl.get("INDUSTRYCSRC1"),
                "business": (profile[:90] + "...") if len(profile) > 90 else profile,
            }
    except Exception as exc:
        result["profile_error"] = str(exc)
    try:
        concept = fetch_json(f"https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax?code={em_code(code)}", timeout=12)
        result["concepts"] = [x.get("BOARD_NAME") for x in concept.get("ssbk", [])[:10] if x.get("BOARD_NAME")]
    except Exception as exc:
        result["concept_error"] = str(exc)
    try:
        holders = fetch_json(f"https://emweb.securities.eastmoney.com/PC_HSF10/ShareholderResearch/PageAjax?code={em_code(code)}", timeout=12)
        h = (holders.get("gdrs") or [None])[0]
        if h:
            result["holder"] = {
                "date": (h.get("END_DATE") or "")[:10],
                "count": h.get("HOLDER_TOTAL_NUM"),
                "ratio": h.get("TOTAL_NUM_RATIO"),
                "focus": h.get("HOLD_FOCUS"),
            }
    except Exception as exc:
        result["holder_error"] = str(exc)
    return result


def summarize_ths(stock, ths):
    titles = ths_titles(ths.get("news", [])) + ths_titles(ths.get("notice", []))
    joined = "；".join(f"{tag}:{title}" for tag, title, _ in titles[:4])
    if not joined:
        joined = "同花顺 timeline/公告研报未返回有效摘要"
    return joined


def emotion_from_data(stock, boards_keep):
    code = stock["quote"]["code"]
    titles = summarize_ths(stock, stock["ths"])
    concepts = "、".join((stock["f10"].get("concepts") or [])[:5]) or "右侧概念资料暂缺"
    if code in ("600183", "000988"):
        return "PCB/CPO/算力链仍是今日主线之一，情绪强但高位波动放大"
    if code == "600589":
        return f"算力租赁/AIDC方向，涨停带来情绪修复，但需看 {NEXT_TRADING_DATE} 开板承接"
    if code == "688545":
        return "光刻胶/先进封装/电子化学品方向回暖，修复弹性较强"
    if code == "688396":
        return "半导体/电源/先进封装方向共振，仍是科技线强势分支"
    if code == "688127":
        return "AI眼镜/光学/MicroLED相关热度仍在，但价格未同步强修复"
    if code == "301071":
        return f"培育钻石板块领涨，情绪最热，{NEXT_TRADING_DATE} 防高开透支"
    if code == "002080":
        return "玻璃玻纤/先进材料方向强，趋势情绪继续改善"
    if code == "601208":
        return "高端材料/电子材料方向受益，但仍需跟踪业绩兑现和毛利率压力"
    if code == "300775":
        return "军工/低空概念有热度，但个股趋势弱，情绪不能替代修复"
    if code == "605168":
        return "算力概念分化且个股前期涨幅大，今日急跌后先看筹码修复"
    return f"同花顺资讯：{titles[:80]}；概念：{concepts}"


def daily_with_quote(daily, quote):
    """Append the latest quoted trading day when the daily API is lagging.

    Daily history providers can lag the close by one trading day.  Planning
    must use the quote timestamp rather than ``REPORT_DATE``: a next-day
    premarket report is often generated after the prior close, while its
    report date is deliberately tomorrow.  Appending under the report date
    would both distort MA values and create a fictional future bar.
    """
    if not daily:
        return []
    raw_datetime = re.sub(r"\D", "", str(quote.get("datetime") or ""))
    if len(raw_datetime) < 8:
        return daily
    quote_date = f"{raw_datetime[:4]}-{raw_datetime[4:6]}-{raw_datetime[6:8]}"
    latest_date = str(daily[-1].get("date") or "")
    if quote_date <= latest_date:
        return daily
    enriched = list(daily)
    enriched.append({
        "date": quote_date,
        "open": quote.get("open"),
        "close": quote.get("close"),
        "change": quote.get("change"),
        "pct": quote.get("pct"),
        "low": quote.get("low"),
        "high": quote.get("high"),
        "volume_lot": quote.get("volume_lot"),
        "amount_wan": quote.get("amount_wan"),
        "turnover": quote.get("turnover"),
    })
    return enriched


def fetch_ths_focus_snapshot():
    if SKIP_THS_DISCOVER:
        return {
            "fupan": {"stale": True},
            "fupan_stale": {"report_date": REPORT_DATE, "source_date": "本次重建跳过慢接口"},
            "zaopan": {},
            "zhangting": {},
            "bidu_special": {},
        }
    try:
        import premarket_report as thsdesk

        snapshot = thsdesk.fetch_ths_discover_focus()
    except Exception as exc:
        return {"error": str(exc), "zaopan": {}, "fupan": {}, "zhangting": {}, "bidu_special": {}}
    fupan = snapshot.get("fupan") or {}
    if fupan and not ths_date_matches_report(fupan.get("date") or fupan.get("title")):
        snapshot["fupan"] = {**fupan, "stale": True}
        snapshot["fupan_stale"] = {
            "report_date": REPORT_DATE,
            "source_date": fupan.get("date") or fupan.get("title") or "未标注",
        }
    return snapshot


def ths_date_matches_report(value):
    text = str(value or "").strip()
    if not text:
        return False
    if REPORT_DATE in text or REPORT_COMPACT_DATE in re.sub(r"\D", "", text):
        return True
    m = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        return f"{datetime.strptime(REPORT_DATE, '%Y-%m-%d').year:04d}-{month:02d}-{day:02d}" == REPORT_DATE
    return False


def load_quote_cache():
    path = BASE_DIR / "data" / "runtime" / "quote_cache.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def cached_quote(code, quote_cache=None):
    cache = quote_cache if quote_cache is not None else load_quote_cache()
    row = cache.get(str(code)) or {}
    if row.get("_stale"):
        return {}
    return row


def quotes_from_cache(universe, quote_cache):
    out = {}
    for code, market in universe:
        row = cached_quote(code, quote_cache)
        if not row:
            continue
        quote = dict(row)
        quote["code"] = code
        quote["market"] = market
        out[code] = quote
    return out


def indexes_from_cache(quote_cache):
    names = {"000001": "上证指数", "399001": "深证成指", "399006": "创业板指"}
    out = []
    for code, name in names.items():
        row = cached_quote(code, quote_cache)
        if row:
            out.append({"name": name, "close": row.get("close"), "pct": row.get("pct")})
    return out


def verified_closing_quotes(universe, report_date, quote_cache=None):
    """Use same-session closing snapshots only, with complete requested coverage."""
    cached = quote_cache if quote_cache is not None else load_quote_cache()
    try:
        live = parse_tencent_quotes(universe)
    except Exception:
        live = {}
    expected = report_date.replace("-", "")
    result = {}
    for code, market in universe:
        for source, row in (("live_snapshot", live.get(code)), ("closing_cache", cached.get(code))):
            row = row or {}
            stamp = re.sub(r"\D", "", str(row.get("datetime") or ""))
            if (row.get("_stale") or len(stamp) < 14 or stamp[:8] != expected
                    or not "150000" <= stamp[8:14] <= "235959"):
                continue
            try:
                if datetime.strptime(stamp, "%Y%m%d%H%M%S") > datetime.now():
                    continue
                close, low, high = [float(row.get(key) or 0) for key in ("close", "low", "high")]
                valid = all(math.isfinite(v) for v in (close, low, high)) and 0 < low <= close <= high and float(row.get("open") or 0) > 0
            except (TypeError, ValueError):
                valid = False
            if valid:
                result[code] = {**row, "code": code, "market": market,
                                "closing_evidence_source": source, "closing_evidence_date": report_date}
                break
    missing = sorted({str(code) for code, _ in universe} - set(result))
    if missing:
        raise RuntimeError(f"收盘行情覆盖不足：{len(result)}/{len(set(code for code, _ in universe))}；缺失或过期：{','.join(missing[:12])}")
    return result


def format_fund_inflow_row(row):
    if isinstance(row, dict):
        code = str(row.get("code") or "").strip()
        name = str(row.get("name") or "").strip()
        amount = str(row.get("amount") or "").strip()
    elif isinstance(row, (list, tuple)):
        code = str(row[0] if len(row) > 0 else "").strip()
        name = str(row[1] if len(row) > 1 else "").strip()
        amount = str(row[2] if len(row) > 2 else "").strip()
    else:
        return ""

    if code.lower() == "code" or name.lower() == "name":
        return ""
    if not code or not name:
        return ""
    if amount and "亿" not in amount:
        amount = f"{amount}亿"
    return f"{code} {name} {amount}".strip()


def ths_focus_lines(ths_focus, limit=8):
    lines = []
    fupan = ths_focus.get("fupan", {})
    stale = bool(fupan.get("stale"))
    if stale:
        stale_info = ths_focus.get("fupan_stale") or {}
        lines.append(
            f"同花顺复盘页未校验到 {REPORT_DATE}（当前标注：{stale_info.get('source_date', '未标注')}），"
            "不采用其指数涨跌和题材结论，今日复盘以实际行情/板块/自选股表现为准。"
        )
    if stale:
        fupan = {}
    if fupan.get("summary"):
        lines.append(f"复盘综合：{fupan.get('summary')}")
    if fupan.get("main_themes"):
        lines.append(f"主流看点：{fupan.get('main_themes')}")
    if fupan.get("active_thread") or fupan.get("market_thread"):
        lines.append(f"盘面脉络：{fupan.get('active_thread') or fupan.get('market_thread')}")
    moves = ths_focus.get("zhangting", {}).get("items", [])
    for item in moves[:3]:
        lines.append(f"异动观察：{item.get('title')}｜{item.get('intro')}")
    special = ths_focus.get("bidu_special", {})
    if special.get("fund_inflow"):
        rows = [
            text
            for text in (format_fund_inflow_row(row) for row in special["fund_inflow"][:5])
            if text
        ]
        if rows:
            lines.append("资金流向：" + "；".join(rows))
    if special.get("calendar_links"):
        lines.append("公告日历：" + "、".join(x["title"] for x in special["calendar_links"][:4]))
    if ths_focus.get("error"):
        lines.append(f"同花顺复盘/异动模块读取失败：{ths_focus['error']}")
    return lines[:limit]


def board_strength_text(boards_top, boards_keep, limit=8):
    rows = list(boards_top or [])
    if not rows and boards_keep:
        rows = sorted(boards_keep.values(), key=lambda x: x.get("f3") or -999, reverse=True)
    texts = []
    for row in rows[:limit]:
        name = row.get("f14")
        change = row.get("f3")
        if name:
            texts.append(f"{name} {pct(change)}")
    return "、".join(texts) or "板块数据暂缺"


def quote_theme_strength_text(quote_cache, limit=6):
    keys = [
        "半导体", "芯片", "存储", "光通信", "CPO", "通信", "PCB", "算力",
        "人工智能", "6G", "消费电子", "光刻", "先进封装", "面板", "玻璃",
    ]
    strong = [
        row for row in quote_cache.values()
        if isinstance(row, dict) and row.get("pct") is not None and (row.get("pct") or 0) > 0
    ]
    scores = {}
    for row in strong:
        text = "、".join(str(row.get(k) or "") for k in ("industry", "concepts", "name"))
        for key in keys:
            if key in text:
                bucket = scores.setdefault(key, {"count": 0, "pct": 0.0})
                bucket["count"] += 1
                bucket["pct"] += float(row.get("pct") or 0)
    ranked = sorted(scores.items(), key=lambda x: (x[1]["count"], x[1]["pct"]), reverse=True)
    if not ranked:
        return "板块接口暂缺；以本地行情强弱样本替代"
    return "、".join(f"{name}（样本{stat['count']}只）" for name, stat in ranked[:limit])


def market_review_lines(stocks, indexes, boards_top, boards_keep, quote_cache=None):
    quote_cache = quote_cache or {}
    lines = []
    up_indexes = [x for x in indexes if (x.get("pct") or 0) > 0]
    down_indexes = [x for x in indexes if (x.get("pct") or 0) < 0]
    if indexes:
        if len(down_indexes) >= 2:
            lines.append("指数层面偏弱，三大指数多数收跌，明日先看低开后能否收回昨日收盘/VWAP，不把单票反抽直接当成修复。")
        elif len(up_indexes) >= 2:
            lines.append("指数层面偏强，短线情绪仍有承接，但加仓仍需等待分钟线量价确认。")
        else:
            lines.append("指数层面分化，个股优先级要服从板块强弱和自身关键位。")

    all_quotes = [row for row in quote_cache.values() if isinstance(row, dict) and row.get("code") and row.get("pct") is not None]
    leaders = sorted(all_quotes, key=lambda x: x.get("pct") or -999, reverse=True)[:5]
    laggards = sorted(all_quotes, key=lambda x: x.get("pct") or 999)[:5]
    if leaders:
        lines.append("本地行情快照强势样本：" + "、".join(f"{x.get('code')} {x.get('name')} {pct(x.get('pct'))}" for x in leaders) + "。")
    if laggards:
        lines.append("本地行情快照弱势样本：" + "、".join(f"{x.get('code')} {x.get('name')} {pct(x.get('pct'))}" for x in laggards) + "。")

    watch_up = sorted(stocks, key=lambda s: s["quote"].get("pct") or -999, reverse=True)
    if watch_up:
        lines.append(
            "自选股强弱：" +
            "；".join(f"{s['quote']['code']} {s['quote']['name']} {pct(s['quote'].get('pct'))}" for s in watch_up[:3]) +
            " 较强，" +
            "；".join(f"{s['quote']['code']} {s['quote']['name']} {pct(s['quote'].get('pct'))}" for s in watch_up[-3:]) +
            " 偏弱。"
        )
    lines.append(f"{NEXT_TRADING_DATE} 执行口径：风险信号优先于进攻信号；集合竞价候选开盘后先进入观察，09:35后只有在全局门控、VWAP/OR15、量能、追价和盈亏比共同确认时才进入模拟成交。")
    return lines


def no_action_review_lines(stocks, limit=6):
    p0 = [s for s in stocks if s["tech"]["priority"] == "P0"]
    p1 = [s for s in stocks if s["tech"]["priority"] == "P1"]
    p2 = [s for s in stocks if s["tech"]["priority"] == "P2"]
    lines = [
        f"今日若在大盘放量下杀中未做减压，{NEXT_TRADING_DATE} 不做情绪化补救；先按风险优先级处理，不能用补仓摊平替代风控。",
    ]
    if p0:
        items = []
        for s in p0[:limit]:
            q, t = s["quote"], s["tech"]
            items.append(f"{q['code']} {q['name']}：防守 {f2(t['defense'])}，反抽 {f2(t['pressure'])} 无量减压")
        lines.append(f"P0 {NEXT_TRADING_DATE} 先处理：" + "；".join(items))
    if p1:
        items = []
        for s in p1[:limit]:
            q, t = s["quote"], s["tech"]
            items.append(f"{q['code']} {q['name']}：站回 {f2(t['repair'])} 才修复，跌破 {f2(t['defense'])} 转风险处理")
        lines.append("P1 只等修复或反抽：" + "；".join(items))
    if p2:
        items = []
        for s in p2[:limit]:
            q, t = s["quote"], s["tech"]
            items.append(f"{q['code']} {q['name']}：守 {f2(t['defense'])}，冲 {f2(t['pressure'])} 不追")
        lines.append("P2 保留强者观察：" + "；".join(items))
    return lines


def parse_signal_time(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def minline_points(rows):
    points = []
    for row in rows or []:
        try:
            raw_time = row.get("m") or ""
            price = float(row.get("p"))
            volume = float(row.get("v") or 0)
            if len(raw_time.split(":")) == 2:
                dt = datetime.strptime(f"{REPORT_DATE} {raw_time}", "%Y-%m-%d %H:%M")
            else:
                dt = datetime.strptime(f"{REPORT_DATE} {raw_time[:5]}", "%Y-%m-%d %H:%M")
            points.append({"time": dt, "price": price, "volume": volume})
        except Exception:
            continue
    return points


def first_point_at_or_after(points, target):
    for idx, point in enumerate(points):
        if point["time"] >= target.replace(second=0, microsecond=0):
            return idx
    return None


def point_price_at(points, target):
    idx = first_point_at_or_after(points, target)
    return points[idx]["price"] if idx is not None else None


REVIEW_WINDOWS = (1, 3, 5, 15, 30, 60)


def build_windows(points, start_idx, ts, minutes_list=REVIEW_WINDOWS):
    windows = {}
    for minutes in minutes_list:
        end = ts + timedelta(minutes=minutes)
        prices = [p["price"] for p in points[start_idx:] if p["time"] <= end]
        price = point_price_at(points, end)
        windows[minutes] = {
            "price": price,
            "min": min(prices) if prices else None,
            "max": max(prices) if prices else None,
            "available": price is not None or bool(prices),
            "target_time": end.strftime("%Y-%m-%d %H:%M:%S"),
        }
    return windows


def window_row(windows, minutes):
    """Read minute-review windows after either in-memory or JSON round trips."""
    if not isinstance(windows, dict):
        return {}
    return windows.get(minutes) or windows.get(str(minutes)) or {}


def window_price_text(windows, minutes):
    row = window_row(windows, minutes)
    value = row.get("price")
    return f2(value) if value is not None else "数据不足"


def mfe_mae_by_window(anchor_price, windows, side):
    out = {}
    for minutes, row in windows.items():
        high = row.get("max")
        low = row.get("min")
        close = row.get("price")
        if not anchor_price:
            out[str(minutes)] = {"mfe": None, "mae": None, "ret": None}
            continue
        if side == "BUY":
            mfe = ((high - anchor_price) / anchor_price * 100) if high is not None else None
            mae = ((anchor_price - low) / anchor_price * 100) if low is not None else None
            ret = ((close - anchor_price) / anchor_price * 100) if close is not None else None
        else:
            mfe = ((anchor_price - low) / anchor_price * 100) if low is not None else None
            mae = ((high - anchor_price) / anchor_price * 100) if high is not None else None
            ret = ((anchor_price - close) / anchor_price * 100) if close is not None else None
        out[str(minutes)] = {"mfe": mfe, "mae": mae, "ret": ret}
    return out


def signal_action_type(scenario):
    scenario = scenario or ""
    if scenario in BUY_SCENARIOS:
        return "三策略首笔试仓"
    if "CANCEL" in scenario:
        return "取消进攻"
    return "减仓/风控"


def rationality_from_validity(action_type, validity):
    if action_type == "三策略首笔试仓":
        if validity == "有效进攻":
            return "合理"
        if validity in ("失效过快", "修复不稳", "追高触发"):
            return "不合理"
        return "需优化"
    if action_type in ("减仓/风控", "取消进攻"):
        if validity in ("有效预警", "取消有效"):
            return "合理"
        if validity in ("过早触发", "弱预警"):
            return "需降噪"
        return "需复核"
    return "需复核"


def iteration_hint(action_type, validity):
    if action_type in ("建仓", "加仓") and validity in ("失效过快", "修复不稳"):
        return "降级：弱修复票只恢复观察；加仓必须同时满足日线结构、VWAP、OR15、量能和分钟线确认。"
    if action_type in ("建仓", "加仓") and validity == "赔率不足":
        return "优化：提高量能阈值或要求更接近回踩确认区，避免追在修复尾端。"
    if action_type in ("建仓", "加仓") and validity == "追高触发":
        return "降级：超过计划加仓区不推立即处理，只保留接近触发并等待二次回踩。"
    if action_type == "减仓/风控" and validity == "过早触发":
        return "降噪：软风险线改用1m/3m确认，避免一笔报价扫线。"
    if action_type == "减仓/风控" and validity == "弱预警":
        return "降噪：降低飞书推送等级，仅保留Dashboard状态，等待跌破下一层级再推。"
    if action_type == "取消进攻" and validity == "取消有效":
        return "保留：高开回落取消进攻有效，继续优先避免追高。"
    if action_type == "减仓/风控" and validity == "有效预警":
        return "保留：风险报警有效，但价格字段应表达为风控线，不作为挂单价。"
    return "观察：继续积累样本。"


def load_realtime_signal_events():
    if not SIGNAL_STATE_DB.exists():
        return []
    try:
        con = sqlite3.connect(SIGNAL_STATE_DB)
        rows = con.execute(
            """
            SELECT created_at, symbol, scenario, price, payload_json
            FROM signal_events
            WHERE trading_date=?
            ORDER BY created_at
            """,
            (REPORT_DATE,),
        ).fetchall()
    except Exception:
        return []
    events = []
    for created_at, symbol, scenario, price, payload_json in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except Exception:
            payload = {}
        priority = payload.get("priority")
        if priority not in ("P0", "P1"):
            continue
        events.append({
            "created_at": created_at,
            "symbol": symbol,
            "name": payload.get("name") or symbol,
            "scenario": scenario,
            "priority": priority,
            "external_status": payload.get("external_status"),
            "price": float(price),
            "trigger_price": payload.get("trigger_price"),
            "add_price": payload.get("add_price"),
            "reduce_price": payload.get("reduce_price"),
            "invalid_price": payload.get("invalid_price"),
            "execution_band_low": payload.get("execution_band_low"),
            "execution_band_high": payload.get("execution_band_high"),
            "amount_ratio_1m": payload.get("amount_ratio_1m"),
            "vwap": payload.get("vwap"),
            "reasons": payload.get("reasons") or [],
        })
    return events


def signal_snapshot_review(event, quote_cache=None):
    quote = cached_quote(event.get("symbol"), quote_cache)
    close = quote.get("close")
    if close is None:
        return {**event, "review": "分钟线和收盘快照均不足，无法复盘", "validity": "数据不足", "rationality": "需复核", "iteration_hint": "补齐本地分钟线缓存。"}
    current = event.get("price")
    action_type = signal_action_type(event.get("scenario"))
    invalid = event.get("invalid_price")
    windows = {m: {"price": close, "min": quote.get("low"), "max": quote.get("high")} for m in REVIEW_WINDOWS}
    if not current:
        return {**event, "windows": windows, "review": "仅有收盘快照，缺少触发价，不能评价执行质量。", "validity": "数据不足", "rationality": "需复核", "iteration_hint": "补齐事件触发价。"}

    if action_type == "加仓":
        close_ret = (close - current) / current * 100
        broke_invalid = bool(invalid and quote.get("low") is not None and quote.get("low") < invalid)
        if broke_invalid:
            validity = "收盘失效"
            review = "分钟线缺口，仅用收盘快照弱验证：日内最低价跌破失效位，加仓信号不应升级。"
        elif close_ret >= 0.8:
            validity = "收盘验证偏有效"
            review = "分钟线缺口，仅用收盘快照弱验证：触发后至收盘仍有顺向收益，但需补分钟线确认回撤。"
        else:
            validity = "收盘赔率不足"
            review = "分钟线缺口，仅用收盘快照弱验证：触发后至收盘空间不足，进攻信号暂不加分。"
    else:
        follow_down = (current - close) / current * 100
        recovered_invalid = bool(invalid and quote.get("high") is not None and quote.get("high") > invalid)
        if recovered_invalid and follow_down < 0.5:
            validity = "收盘收回"
            review = "分钟线缺口，仅用收盘快照弱验证：风险线触发后日内高点收回失效/恢复位，软风控需降噪。"
        elif follow_down >= 0.6:
            validity = "收盘验证有效"
            review = "分钟线缺口，仅用收盘快照弱验证：触发后至收盘继续走弱，风控提示方向有效。"
        else:
            validity = "收盘弱预警"
            review = "分钟线缺口，仅用收盘快照弱验证：触发后至收盘下行延续不足，应降低重复提醒。"
    if action_type == "加仓":
        rationality = "合理" if validity == "收盘验证偏有效" else ("不合理" if validity == "收盘失效" else "需优化")
    else:
        rationality = "合理" if validity == "收盘验证有效" else ("需降噪" if validity in ("收盘收回", "收盘弱预警") else "需复核")
    return {
        **event,
        "action_type": action_type,
        "windows": windows,
        "validity": validity,
        "rationality": rationality,
        "iteration_hint": "复盘降级：当前为收盘快照弱验证，明日优先落地本地分钟线持久化。",
        "review": review,
    }


def analyze_signal_event(event, points, quote_cache=None):
    ts = parse_signal_time(event["created_at"])
    if not ts or not points:
        return signal_snapshot_review(event, quote_cache)
    start_idx = first_point_at_or_after(points, ts)
    if start_idx is None:
        return {**event, "review": "触发时间之后无分时数据", "validity": "数据不足"}

    current = event["price"]
    windows = build_windows(points, start_idx, ts)

    invalid = event.get("invalid_price")
    scenario = event.get("scenario") or ""
    action_type = signal_action_type(scenario)
    is_add = scenario in BUY_SCENARIOS
    if is_add:
        mfe_mae = mfe_mae_by_window(current, windows, "BUY")
        mfe15 = mfe_mae["15"]["mfe"]
        mae15 = mfe_mae["15"]["mae"]
        broke_invalid_5 = bool(invalid and windows[5]["min"] is not None and windows[5]["min"] < invalid)
        broke_invalid_15 = bool(invalid and windows[15]["min"] is not None and windows[15]["min"] < invalid)
        chased = bool(event.get("execution_band_high") and current > float(event["execution_band_high"]))
        if broke_invalid_5:
            validity = "失效过快"
            review = "加仓触发后5分钟内跌破失效位，应降级为观察，不应给执行价。"
        elif chased:
            validity = "追高触发"
            review = "触发价已远离计划加仓区，后续应降级为等待回踩。"
        elif broke_invalid_15:
            validity = "修复不稳"
            review = "短线未立即失效，但15分钟内跌破失效位，只能作为弱修复观察。"
        elif (mfe15 or 0) >= 0.6 and (mae15 or 0) <= 0.35:
            validity = "有效进攻"
            review = "触发后有向上空间且回撤可控，加仓规则保留。"
        else:
            validity = "赔率不足"
            review = "触发后有利空间不足，后续需要更强量能/结构门控。"
        rationality = rationality_from_validity(action_type, validity)
        return {
            **event,
            "action_type": action_type,
            "windows": windows,
            "mfe15": mfe15,
            "mae15": mae15,
            "mfe_mae": mfe_mae,
            "chased": chased,
            "broke_invalid_5": broke_invalid_5,
            "broke_invalid_15": broke_invalid_15,
            "validity": validity,
            "rationality": rationality,
            "iteration_hint": iteration_hint(action_type, validity),
            "review": review,
        }

    mfe_mae = mfe_mae_by_window(current, windows, "SELL")
    follow_down15 = mfe_mae["15"]["mfe"]
    rebound5 = mfe_mae["5"]["mae"]
    recovered_invalid_5 = bool(invalid and windows[5]["max"] is not None and windows[5]["max"] > invalid)
    if "CANCEL" in scenario:
        if (follow_down15 or 0) >= 0.8:
            validity = "取消有效"
            review = "取消进攻后继续走弱，避免追高有效。"
        else:
            validity = "取消后震荡"
            review = "取消方向合理，但后续以观察为主，不应衍生卖出价。"
    elif recovered_invalid_5 and (follow_down15 or 0) < 0.5:
        validity = "过早触发"
        review = "风险线触发后很快收回，后续应要求1m/3m确认。"
    elif (follow_down15 or 0) >= 0.6:
        validity = "有效预警"
        review = "触发后继续下行，作为风险报警有效；价格应表述为风控线而非挂单价。"
    else:
        validity = "弱预警"
        review = "触发后下行延续不足，应提高确认条件或降低推送强度。"
    rationality = rationality_from_validity(action_type, validity)
    return {
        **event,
        "action_type": action_type,
        "windows": windows,
        "follow_down15": follow_down15,
        "rebound5": rebound5,
        "mfe_mae": mfe_mae,
        "recovered_invalid_5": recovered_invalid_5,
        "validity": validity,
        "rationality": rationality,
        "iteration_hint": iteration_hint(action_type, validity),
        "review": review,
    }


def build_signal_quality_review(events=None):
    events = events if events is not None else load_realtime_signal_events()
    if not events:
        return {"events": [], "summary": ["今日未发现可复盘的实时 P0/P1 信号事件。"]}
    by_symbol = {}
    for event in events:
        by_symbol.setdefault(event["symbol"], []).append(event)
    quote_cache = load_quote_cache()
    points_cache = {code: minline_points(fetch_minline(code)) for code in by_symbol}
    reviewed = []
    for event in events:
        reviewed.append(analyze_signal_event(event, points_cache.get(event["symbol"], []), quote_cache))

    total = len(reviewed)
    add_events = [x for x in reviewed if x.get("action_type") == "加仓"]
    reduce_events = [x for x in reviewed if x.get("action_type") in ("减仓/风控", "取消进攻")]
    p0_events = [x for x in reviewed if x["priority"] == "P0"]
    invalid_fast = [x for x in add_events if x.get("broke_invalid_5")]
    effective_risk = [x for x in p0_events if x.get("validity") in ("有效预警", "取消有效", "收盘验证有效")]
    add_reasonable = [x for x in add_events if x.get("rationality") == "合理"]
    add_bad = [x for x in add_events if x.get("rationality") == "不合理"]
    add_chased = [x for x in add_events if x.get("chased")]
    reduce_reasonable = [x for x in reduce_events if x.get("rationality") == "合理"]
    reduce_noisy = [x for x in reduce_events if x.get("rationality") in ("需降噪", "需复核")]
    summary = [
        f"实时信号复盘：P0/P1 共 {total} 条；P0 {len(p0_events)} 条，加仓类 {len(add_events)} 条。",
        f"加仓信号：合理 {len(add_reasonable)}/{len(add_events)}，不合理/失效过快 {len(add_bad)} 条，追高触发 {len(add_chased)} 条；减仓/风控信号：合理 {len(reduce_reasonable)}/{len(reduce_events)}，需降噪 {len(reduce_noisy)} 条。",
        f"P0 有效预警/取消有效 {len(effective_risk)} 条；加仓类5分钟内失效 {len(invalid_fast)} 条。",
    ]
    if invalid_fast:
        names = "、".join(f"{x['name']}({x['symbol']})" for x in invalid_fast[:5])
        summary.append(f"需要降级的进攻信号：{names}。弱修复票站回VWAP不再直接给加仓。")
    noisy = reduce_noisy[:5]
    if noisy:
        seen = set()
        names = []
        for item in noisy:
            key = item["symbol"]
            if key in seen:
                continue
            seen.add(key)
            names.append(f"{item['name']}({item['symbol']})")
        summary.append(f"需要降噪的风控信号：{'、'.join(names)}。软风险线优先进入Dashboard，等待1m/3m确认或跌破下一层级再推飞书。")
    summary.append("后续执行口径：P0 是风控报警线，不是等待成交的挂单价；P1 必须经过日线结构、VWAP、OR15、量能和分钟线共同确认。")
    metrics = {
        "total": total,
        "add_total": len(add_events),
        "add_reasonable": len(add_reasonable),
        "add_bad": len(add_bad),
        "add_chased": len(add_chased),
        "reduce_total": len(reduce_events),
        "reduce_reasonable": len(reduce_reasonable),
        "reduce_noisy": len(reduce_noisy),
        "p0_total": len(p0_events),
        "p0_effective": len(effective_risk),
        "add_invalid_fast": len(invalid_fast),
    }
    return {"events": reviewed, "summary": summary, "metrics": metrics}


def signal_quality_lines(review, limit=12):
    events = review.get("events") or []
    lines = []
    for item in events[:limit]:
        w = item.get("windows") or {}
        p1 = window_price_text(w, 1)
        p3 = window_price_text(w, 3)
        p5 = window_price_text(w, 5)
        p15 = window_price_text(w, 15)
        lines.append(
            f"| {item['created_at'][11:16]} | {item['symbol']} | {item['name']} | {item.get('action_type')} | {item['priority']} | {item['scenario']} | "
            f"{f2(item.get('price'))} | {f2(item.get('trigger_price'))} | {f2(item.get('invalid_price'))} | "
            f"{p1}/{p3}/{p5}/{p15} | {item.get('validity')} | {item.get('rationality')} | {item.get('iteration_hint')} |"
        )
    return lines


def write_signal_review_archive(signal_review):
    SIGNAL_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    SIGNAL_REVIEW_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "trading_date": REPORT_DATE,
        "next_trading_date": NEXT_TRADING_DATE,
        "generated_at": current_datetime().strftime("%Y-%m-%d %H:%M:%S"),
        "metrics": signal_review.get("metrics", {}),
        "summary": signal_review.get("summary", []),
        "events": signal_review.get("events", []),
    }
    day_path = SIGNAL_REVIEW_DIR / f"signal_quality_{REPORT_COMPACT_DATE}.json"
    day_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with SIGNAL_REVIEW_HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "trading_date": REPORT_DATE,
            "next_trading_date": NEXT_TRADING_DATE,
            "generated_at": payload["generated_at"],
            "metrics": payload["metrics"],
            "summary": payload["summary"],
        }, ensure_ascii=False) + "\n")
    return day_path


def analyze_paper_order(order, points):
    discipline = order.get("discipline_check") or {}
    rationale = order.get("strategy_rationale") or {}
    discipline_summary = discipline.get("summary") or "历史订单未记录纪律检查"
    discipline_passed = discipline.get("passed")
    ts = parse_signal_time(order.get("created_at"))
    if order.get("status") not in ("FILLED", "PARTIAL_FILLED"):
        return {
            **order,
            "trade_quality": "未成交",
            "discipline_summary": discipline_summary,
            "discipline_passed": discipline_passed,
            "strategy_basis": "；".join((rationale.get("action_basis") or [])[:3]),
            "review": order.get("reason") or "未成交，不计入成交质量。",
        }
    if not ts or not points:
        return {
            **order,
            "trade_quality": "已成交待复盘",
            "discipline_summary": discipline_summary,
            "discipline_passed": discipline_passed,
            "strategy_basis": "；".join((rationale.get("action_basis") or [])[:3]),
            "review": "模拟成交已记录，但分钟线数据不足，暂只计入成交与持仓，不评价1/3/5/15分钟质量。",
        }
    start_idx = first_point_at_or_after(points, ts)
    if start_idx is None:
        return {**order, "trade_quality": "数据不足", "review": "成交时间之后无分时数据"}
    fill = order.get("fill_price")
    windows = {}
    windows = build_windows(points, start_idx, ts)
    signal = order.get("signal") or {}
    invalid = signal.get("invalid_price")
    if order.get("side") == "BUY":
        mfe_mae = mfe_mae_by_window(fill, windows, "BUY")
        mfe15 = mfe_mae["15"]["mfe"]
        mae15 = mfe_mae["15"]["mae"]
        broke_invalid_5 = bool(invalid and windows[5]["min"] is not None and windows[5]["min"] < invalid)
        if broke_invalid_5:
            quality = "买入失败"
            review = "模拟买入后5分钟内跌破失效位，说明信号仍过早。"
        elif (mfe15 or 0) >= 0.6 and (mae15 or 0) <= 0.35:
            quality = "买入有效"
            review = "模拟买入后有可见浮盈且回撤可控。"
        else:
            quality = "买入低效"
            review = "模拟买入后赔率不足，继续提高门控或缩小有效成交区。"
        return {**order, "windows": windows, "mfe15": mfe15, "mae15": mae15, "mfe_mae": mfe_mae, "trade_quality": quality, "discipline_summary": discipline_summary, "discipline_passed": discipline_passed, "strategy_basis": "；".join((rationale.get("action_basis") or [])[:3]), "review": review}

    if order.get("side") == "SELL":
        mfe_mae = mfe_mae_by_window(fill, windows, "SELL")
        avoided15 = mfe_mae["15"]["mfe"]
        rebound15 = mfe_mae["15"]["mae"]
        if (avoided15 or 0) >= 0.6:
            quality = "减仓有效"
            review = "模拟减仓后继续下行，风控成交有效。"
        elif (rebound15 or 0) >= 0.8 and (avoided15 or 0) < 0.3:
            quality = "减仓偏早"
            review = "模拟减仓后快速反弹，软风控仍需确认。"
        else:
            quality = "减仓一般"
            review = "模拟减仓后波动不大，信号更多是风险暴露记录。"
        return {**order, "windows": windows, "avoided15": avoided15, "rebound15": rebound15, "mfe_mae": mfe_mae, "trade_quality": quality, "discipline_summary": discipline_summary, "discipline_passed": discipline_passed, "strategy_basis": "；".join((rationale.get("action_basis") or [])[:3]), "review": review}

    return {**order, "windows": windows, "trade_quality": "非成交动作", "discipline_summary": discipline_summary, "discipline_passed": discipline_passed, "strategy_basis": "；".join((rationale.get("action_basis") or [])[:3]), "review": "取消进攻不计入成交质量"}


def paper_order_counts(all_orders):
    counts = {"total": len(all_orders or []), "filled": 0, "rejected": 0, "cancelled": 0, "buy": 0, "sell": 0}
    for order in all_orders or []:
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


def load_market_decisions():
    """Read the de-duplicated intraday candidate funnel when the engine recorded it."""
    if not SIGNAL_STATE_DB.exists():
        return []
    try:
        con = sqlite3.connect(SIGNAL_STATE_DB)
        rows = con.execute(
            """
            SELECT symbol, stage, reason, payload_json, created_at
            FROM market_decision_events
            WHERE trading_date=?
            ORDER BY created_at, symbol, stage
            """,
            (REPORT_DATE,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass
    decisions = []
    for symbol, stage, reason, payload_json, created_at in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except Exception:
            payload = {}
        decisions.append({
            "symbol": symbol,
            "stage": stage,
            "reason": reason,
            "payload": payload,
            "created_at": created_at,
        })
    return decisions


def build_market_decision_funnel(decisions, orders):
    """Keep candidate generation distinct from risk, technical, and order outcomes."""
    decisions = decisions or []
    orders = orders or []
    stages = {
        "candidate": ("CANDIDATE", "V2_WAIT", "V2_DATA_BLOCKED", "V2_MARKET_BLOCKED", "V2_SIGNAL_READY", "V2_RISK_ACTION"),
        "global_risk_blocked": "GLOBAL_RISK_BLOCKED",
        "a_share_regime_blocked": "A_SHARE_REGIME_BLOCKED",
        "external_fade_blocked": "EXTERNAL_FADE_BLOCKED",
        "legacy_global_blocked": "GLOBAL_GATE_BLOCKED",
        "shadow_ready": "SHADOW_TECHNICAL_READY",
        "shadow_technical_blocked": "SHADOW_TECHNICAL_BLOCKED",
        "technical_blocked": ("TECHNICAL_BLOCKED", "V2_WAIT", "V2_DATA_BLOCKED", "V2_MARKET_BLOCKED"),
        "ready": ("SIGNAL_READY", "V2_SIGNAL_READY"),
        "v2_wait": "V2_WAIT",
        "v2_data_blocked": "V2_DATA_BLOCKED",
        "v2_market_blocked": "V2_MARKET_BLOCKED",
        "v2_risk_action": "V2_RISK_ACTION",
    }
    counts = {
        key: sum(
            1 for item in decisions
            if item.get("stage") in (stage if isinstance(stage, tuple) else (stage,))
        )
        for key, stage in stages.items()
    }
    counts["global_blocked"] = (
        counts["global_risk_blocked"]
        + counts["a_share_regime_blocked"]
        + counts["external_fade_blocked"]
        + counts["legacy_global_blocked"]
    )
    radar_orders = [
        item for item in orders
        if item.get("scenario") == "MARKET_OPPORTUNITY_ACTIONABLE"
    ]
    counts.update({
        "order_attempts": len(radar_orders),
        "order_rejected": sum(1 for item in radar_orders if item.get("status") == "REJECTED"),
        "order_cancelled": sum(1 for item in radar_orders if item.get("status") == "CANCELLED"),
        "order_filled": sum(1 for item in radar_orders if item.get("status") in ("FILLED", "PARTIAL_FILLED")),
    })
    if not decisions and radar_orders:
        counts["ready"] = len({str(item.get("symbol") or "") for item in radar_orders})
    shadow_stages = {"SHADOW_TECHNICAL_READY", "SHADOW_TECHNICAL_BLOCKED"}
    has_shadow_history = any(item.get("stage") in shadow_stages for item in decisions)
    if counts["global_blocked"] and has_shadow_history:
        shadow_summary = "；影子评估技术合格 {shadow_ready} 只、技术未过 {shadow_technical_blocked} 只".format(**counts)
    elif counts["global_blocked"]:
        shadow_summary = "；历史候选未保存影子技术评估，新版本起开始统计"
    else:
        shadow_summary = ""
    summary = (
        "盘中决策漏斗：候选 {candidate} 只 → 门控阻断 {global_blocked} 只（全球 {global_risk_blocked} / 本地 {a_share_regime_blocked} / 外部回落 {external_fade_blocked} / 历史未拆分 {legacy_global_blocked}）→ "
        "技术/量能/RR 未过 {technical_blocked} 只（V2等待 {v2_wait} / 数据 {v2_data_blocked} / 市场 {v2_market_blocked}）→ 进入可执行信号 {ready} 只 → "
        "订单成交 {order_filled} / 拒绝 {order_rejected} / 取消 {order_cancelled}{shadow_summary}。"
    ).format(**counts, shadow_summary=shadow_summary)
    return {"counts": counts, "decisions": decisions, "summary": summary}


def build_paper_trade_review():
    orders = paper_trading.load_orders(BASE_DIR, trading_date=REPORT_DATE, filled_only=True)
    all_orders = paper_trading.load_orders(BASE_DIR, trading_date=REPORT_DATE, filled_only=False)
    order_counts = paper_order_counts(all_orders)
    decision_funnel = build_market_decision_funnel(load_market_decisions(), all_orders)
    if not orders:
        rejected = [x for x in all_orders if x.get("status") == "REJECTED"]
        no_sellable = [x for x in rejected if "无可卖" in (x.get("reason") or "") or "T+1" in (x.get("reason") or "")]
        session_reject = [x for x in rejected if "当前时段" in (x.get("reason") or "")]
        buy_attempts = [x for x in all_orders if x.get("side") == "BUY"]
        sell_attempts = [x for x in all_orders if x.get("side") == "SELL"]
        summary = [
            "本地模拟盘今日无成交：没有任何 BUY/SELL 通过模拟撮合。",
            f"订单链路复核：共记录 {len(all_orders)} 条模拟订单/尝试；买入尝试 {len(buy_attempts)} 条，卖出风控尝试 {len(sell_attempts)} 条，拒单 {len(rejected)} 条。",
        ]
        if no_sellable:
            names = []
            seen = set()
            for order in no_sellable:
                key = order.get("symbol")
                if key in seen:
                    continue
                seen.add(key)
                names.append(f"{order.get('name')}({key})")
            summary.append(
                "主要未成交原因：P0 卖出信号对应股票不在模拟盘可卖持仓内，或受 T+1/无可卖数量限制；"
                f"涉及 {len(seen)} 只：{'、'.join(names[:8])}。这类只应作为风险提示，不计入成交胜率。"
            )
        if session_reject:
            summary.append(f"收盘集合竞价/非连续竞价阶段拒单 {len(session_reject)} 条：只记录状态，不模拟卖出。")
        if not buy_attempts:
            summary.append("全市场机会/候选池今日未产生可成交买入，说明信号只停留在观察或风控侧；明日继续看是否能从候选观察升级为 P1 可执行。")
        summary.append("复盘口径：无成交日不评价胜率，只评价“信号为何没有转成交”和“是否误把提醒当成交易”。")
        summary.append(decision_funnel["summary"])
        return {
            "orders": [],
            "summary": summary,
            "all_orders": all_orders,
            "order_counts": order_counts,
            "decision_funnel": decision_funnel,
        }
    by_symbol = {}
    for order in orders:
        by_symbol.setdefault(order["symbol"], []).append(order)
    points_cache = {code: minline_points(fetch_minline(code)) for code in by_symbol}
    reviewed = [analyze_paper_order(order, points_cache.get(order["symbol"], [])) for order in orders]
    buys = [x for x in reviewed if x.get("side") == "BUY"]
    sells = [x for x in reviewed if x.get("side") == "SELL"]
    good_buys = [x for x in buys if x.get("trade_quality") == "买入有效"]
    bad_buys = [x for x in buys if x.get("trade_quality") == "买入失败"]
    good_sells = [x for x in sells if x.get("trade_quality") == "减仓有效"]
    early_sells = [x for x in sells if x.get("trade_quality") == "减仓偏早"]
    discipline_passed = [x for x in reviewed if x.get("discipline_passed") is True]
    discipline_missing = [x for x in reviewed if x.get("discipline_passed") is None]
    discipline_failed = [x for x in reviewed if x.get("discipline_passed") is False]
    summary = [
        f"模拟盘成交复盘：成交 {len(reviewed)} 笔；买入 {len(buys)} 笔，卖出 {len(sells)} 笔。",
        f"本日已记录模拟费用 {f2(sum(float(x.get('fees_total') or 0) for x in reviewed))} 元；2026-09-11起计费，历史不追溯。短窗涨跌仅评估路径，不代表T+1可兑现净收益。",
        f"买入有效 {len(good_buys)}/{len(buys)}，买入失败 {len(bad_buys)}；减仓有效 {len(good_sells)}/{len(sells)}，减仓偏早 {len(early_sells)}。",
        f"纪律执行：通过 {len(discipline_passed)}/{len(reviewed)}，未通过 {len(discipline_failed)}，历史缺字段 {len(discipline_missing)}。",
    ]
    if bad_buys:
        summary.append("买入失败样本：" + "、".join(f"{x['name']}({x['symbol']})" for x in bad_buys[:5]) + "；后续继续收紧进攻信号。")
    if early_sells:
        summary.append("减仓偏早样本：" + "、".join(f"{x['name']}({x['symbol']})" for x in early_sells[:5]) + "；软风控需要更多确认。")
    summary.append(decision_funnel["summary"])
    return {
        "orders": reviewed,
        "summary": summary,
        "all_orders": all_orders,
        "order_counts": order_counts,
        "decision_funnel": decision_funnel,
    }


def paper_trade_lines(review, limit=12):
    lines = []
    for item in (review.get("orders") or [])[:limit]:
        w = item.get("windows") or {}
        p1 = window_price_text(w, 1)
        p3 = window_price_text(w, 3)
        p5 = window_price_text(w, 5)
        p15 = window_price_text(w, 15)
        lines.append(
            f"| {item['created_at'][11:16]} | {item['symbol']} | {item['name']} | {item['side']} | {item.get('scenario')} | "
            f"{item.get('qty')} | {f2(item.get('fill_price'))} | {p1}/{p3}/{p5}/{p15} | {item.get('trade_quality')} | {item.get('review')} |"
        )
    return lines


def paper_discipline_lines(review, limit=12):
    lines = []
    orders = review.get("orders") or review.get("all_orders") or []
    for item in orders[:limit]:
        discipline = item.get("discipline_check") or {}
        rationale = item.get("strategy_rationale") or {}
        checks = discipline.get("checks") or []
        passed = sum(1 for x in checks if x.get("passed"))
        failed = [x.get("name") for x in checks if not x.get("passed")]
        basis = "；".join((rationale.get("action_basis") or [])[:4]) or item.get("strategy_basis") or "-"
        side = str(item.get("side") or "").upper()
        if side == "CANCEL":
            discipline_text = "非成交动作：取消进攻计划"
            passed_text = "不适用"
            failed_text = "-"
        elif item.get("status") == "REJECTED":
            discipline_text = "策略拦截：" + (item.get("reason") or discipline.get("summary") or "未通过执行门槛")
            passed_text = f"{passed}/{len(checks)}"
            failed_text = "、".join(failed[:4]) or "-"
        else:
            discipline_text = discipline.get("summary") or item.get("discipline_summary") or "历史订单未记录"
            passed_text = f"{passed}/{len(checks)}"
            failed_text = "、".join(failed[:4]) or "-"
        lines.append(
            f"| {str(item.get('created_at') or '')[11:16]} | {item.get('symbol')} | {item.get('name')} | {item.get('side')} | {item.get('status')} | "
            f"{discipline_text} | {passed_text} | {failed_text} | {basis} |"
        )
    return lines


def paper_position_price_map(extra_price_map=None):
    price_map = dict(extra_price_map or {})
    positions = [p for p in paper_trading.load_positions(BASE_DIR) if int(p.get("quantity") or 0) > 0]
    filled_orders = paper_trading.load_orders(BASE_DIR, trading_date=REPORT_DATE, filled_only=True)
    tracked = {str(p.get("symbol")): p for p in positions}
    for order in filled_orders:
        tracked.setdefault(str(order.get("symbol")), order)
    missing = [p for p in tracked.values() if p.get("symbol") not in price_map]
    if missing:
        try:
            quotes = parse_tencent_quotes([(p["symbol"], infer_market(p["symbol"])) for p in missing])
        except Exception:
            quotes = {}
        for code, quote in quotes.items():
            price_map[code] = {
                "last_price": quote.get("close"),
                "name": quote.get("name"),
                "pct": quote.get("pct"),
            }
    return price_map


def build_paper_position_snapshot(report_date=None, now=None, extra_price_map=None):
    price_map = paper_position_price_map(extra_price_map)
    return paper_trading.write_latest_snapshot(
        BASE_DIR,
        trading_date=report_date or REPORT_DATE,
        price_map=price_map,
        now=now or current_datetime(),
    )


def append_paper_position_section(lines, paper_snapshot, heading="六、模拟账户持仓表现"):
    paper_snapshot = paper_snapshot or {}
    account = paper_snapshot.get("account") or {}
    positions = [pos for pos in (paper_snapshot.get("positions") or []) if int(pos.get("quantity") or 0) > 0]
    lines.append(f"## {heading}")
    lines.append("- 用途：盘中模拟盘成交后的账户盯市，跟踪持仓、T+1可卖和浮盈亏；不是实盘账户。")
    lines.append("- T+1：A股普通股票今日模拟买入不增加当日可卖数量，下一交易日才结转为可卖。")
    if positions:
        lines.append(
            f"- 账户：初始资金 {f2(account.get('initial_cash'))}；总资产 {f2(account.get('total_assets') or account.get('total_amount'))}；"
            f"可用现金 {f2(account.get('cash'))}；持仓 {account.get('position_count', len(positions))} 只；"
            f"持仓市值 {f2(account.get('market_value'))}；"
            f"仓位 {pct(account.get('position_pct'))}；浮盈亏 {f2(account.get('unrealized_pnl'))}；"
            f"持仓收益率 {pct(account.get('unrealized_pnl_pct'))}。"
        )
        lines.append("")
        lines.append("| 代码 | 名称 | 持仓 | 可卖 | 成本 | 现价 | 市值 | 当日盈亏 | 当日收益率 | 浮盈亏 | 收益率 | 持仓天数 |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        for pos in positions:
            holding_days = pos.get("holding_days")
            holding_text = f"{holding_days}天" if holding_days is not None else "-"
            lines.append(
                f"| {pos.get('symbol')} | {pos.get('name')} | {pos.get('quantity', 0)} | {pos.get('sellable', 0)} | "
                f"{f2(pos.get('avg_cost'))} | {f2(pos.get('last_price'))} | {f2(pos.get('market_value'))} | "
                f"{f2(pos.get('day_pnl'))} | {pct(pos.get('day_pnl_pct'))} | "
                f"{f2(pos.get('unrealized_pnl'))} | {pct(pos.get('unrealized_pnl_pct'))} | {holding_text} |"
            )
    else:
        lines.append("- 当前模拟账户无持仓；只有信号进入可执行区并通过模拟撮合后才会生成持仓。")
    lines.append("")


def load_auction_candidates():
    path = AUCTION_CANDIDATE_DIR / f"auction_candidates_{REPORT_COMPACT_DATE}.json"
    if not path.exists():
        return {"candidates": [], "summary": [f"{REPORT_DATE} 未发现 09:25 集合竞价候选跟踪文件。"]}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"candidates": [], "summary": [f"集合竞价候选文件读取失败：{exc}。"]}


def write_rotation_candidates(stocks):
    """Persist a next-session watchlist ranking without creating buy orders.

    The runtime engine reads this file only as higher-timeframe evidence.  A
    candidate still needs a live sector, 60-minute and minute confirmation.
    """
    ROTATION_CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for stock in stocks or []:
        quote = stock.get("quote") or {}
        code = str(quote.get("code") or "").strip()
        if not code:
            continue
        context = cycle_framework.build_plan_context(stock.get("daily") or [], quote)
        rows.append({
            "code": code,
            "name": quote.get("name") or code,
            "score": context.get("score"),
            "status": "盘后趋势候选" if context.get("large_cycle_ready") else "盘后观察/修复",
            "focus": (
                f"盘后{context.get('route')}｜日线 {context.get('daily', {}).get('state')}｜"
                f"月线 {context.get('monthly', {}).get('state')}｜筹码 {context.get('chips', {}).get('state')}｜"
                f"时间 {context.get('time', {}).get('state')}"
            ),
            "framework_context": context,
            "source": "afterclose_rotation_plan",
        })
    rows.sort(key=lambda row: (not bool((row.get("framework_context") or {}).get("large_cycle_ready")), -(row.get("score") or 0), row["code"]))
    payload = {
        "schema": "a_share_rotation_plan_v1",
        "source_date": REPORT_DATE,
        "target_date": NEXT_TRADING_DATE,
        "generated_at": current_datetime().strftime("%Y-%m-%d %H:%M:%S"),
        "rule": "盘后候选仅排序；量能仅辅助排序；盘中必须主题/板块、60分钟、VWAP/OR与分钟量能共同确认。",
        "candidates": rows,
    }
    path = ROTATION_CANDIDATE_DIR / f"rotation_candidates_{NEXT_TRADING_DATE.replace('-', '')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, payload


def analyze_auction_candidate_from_snapshot(item, quote_cache=None):
    quote = cached_quote(item.get("code"), quote_cache)
    trigger = item.get("trigger_price")
    invalid = item.get("invalid_price")
    if not quote or not trigger or not invalid:
        return {**item, "result": "数据不足", "review": "分时数据、收盘快照或触发/失效价缺失，无法复盘。"}
    open_price = quote.get("open")
    high = quote.get("high")
    low = quote.get("low")
    close = quote.get("close")
    if high is None or low is None or close is None:
        return {**item, "result": "数据不足", "review": "本地行情快照缺少开高低收，无法复盘。"}
    if high < trigger:
        return {
            **item,
            "result": "未触发",
            "triggered": False,
            "review": f"收盘快照验证：日内最高 {f2(high)} 未到触发价 {f2(trigger)}，候选未转交易。",
        }
    if low <= invalid and (open_price is None or open_price <= invalid):
        return {
            **item,
            "result": "先失效",
            "triggered": False,
            "review": f"收盘快照验证：开盘/低点先触及失效价 {f2(invalid)}，候选剔除有效。",
        }
    if low <= invalid:
        return {
            **item,
            "result": "顺序不明",
            "triggered": True,
            "trigger_time": "日内",
            "entry_price": trigger,
            "review": "收盘快照验证：日内同时到过触发价和失效价，缺少分钟线无法判断先后，不能评价为有效候选。",
        }
    mfe = (high - trigger) / trigger * 100 if trigger else None
    mae = (trigger - low) / trigger * 100 if trigger else None
    ret = (close - trigger) / trigger * 100 if trigger else None
    windows = {m: {"price": close, "min": low, "max": high} for m in (5, 15, 30, 60)}
    if (mfe or 0) >= 0.8 and (ret or 0) > 0:
        result = "收盘验证有效"
        review = "收盘快照验证：日内触发且未跌破失效价，收盘仍高于触发价；因缺分钟线，暂按弱有效候选跟踪。"
    else:
        result = "低效候选"
        review = "收盘快照验证：触发后顺向空间或收盘延续不足，候选不加分。"
    return {
        **item,
        "result": result,
        "triggered": True,
        "trigger_time": "日内",
        "entry_price": trigger,
        "windows": windows,
        "mfe_mae": {"30": {"mfe": mfe, "mae": mae, "ret": ret}},
        "review": review,
    }


def analyze_auction_candidate(item, points, quote_cache=None):
    trigger = item.get("trigger_price")
    invalid = item.get("invalid_price")
    start = parse_signal_time(f"{REPORT_DATE} 09:30:00")
    if not points or not trigger or not invalid or not start:
        return analyze_auction_candidate_from_snapshot(item, quote_cache)
    start_idx = first_point_at_or_after(points, start)
    if start_idx is None:
        return {**item, "result": "数据不足", "review": "09:30 后无分时数据。"}
    trigger_idx = None
    invalid_idx = None
    for idx in range(start_idx, len(points)):
        price = points[idx]["price"]
        if trigger_idx is None and price >= trigger:
            trigger_idx = idx
        if invalid_idx is None and price <= invalid:
            invalid_idx = idx
        if trigger_idx is not None or invalid_idx is not None:
            break
    if invalid_idx is not None and (trigger_idx is None or invalid_idx <= trigger_idx):
        return {
            **item,
            "result": "先失效",
            "triggered": False,
            "review": "开盘后先跌破失效价，候选剔除有效。",
        }
    if trigger_idx is None:
        return {
            **item,
            "result": "未触发",
            "triggered": False,
            "review": "未站上触发价，保留观察但不应模拟买入。",
        }
    ts = points[trigger_idx]["time"]
    entry = points[trigger_idx]["price"]
    windows = build_windows(points, trigger_idx, ts, minutes_list=(5, 15, 30, 60))
    mfe_mae = mfe_mae_by_window(entry, windows, "BUY")
    broke_invalid_15 = bool(windows[15]["min"] is not None and windows[15]["min"] <= invalid)
    mfe30 = (mfe_mae.get("30") or {}).get("mfe")
    mae30 = (mfe_mae.get("30") or {}).get("mae")
    if broke_invalid_15:
        result = "触发后失效"
        review = "触发后15分钟内跌破失效价，竞价候选过早或追高。"
    elif (mfe30 or 0) >= 0.8 and (mae30 or 0) <= 0.6:
        result = "候选有效"
        review = "触发后30分钟有可见顺向空间且回撤可控。"
    elif (mfe30 or 0) < 0.4:
        result = "低效候选"
        review = "触发后顺向空间不足，后续应提高题材/量能或形态门槛。"
    else:
        result = "一般候选"
        review = "触发后有一定波动，但赔率不突出，继续跟踪。"
    return {
        **item,
        "result": result,
        "triggered": True,
        "trigger_time": ts.strftime("%H:%M"),
        "entry_price": entry,
        "windows": windows,
        "mfe_mae": mfe_mae,
        "review": review,
    }


def build_auction_candidate_review():
    data = load_auction_candidates()
    candidates = data.get("candidates") or []
    if not candidates:
        return {"candidates": [], "summary": data.get("summary") or ["今日无集合竞价候选。"]}
    quote_cache = load_quote_cache()
    points_cache = {item["code"]: minline_points(fetch_minline(item["code"])) for item in candidates}
    reviewed = [analyze_auction_candidate(item, points_cache.get(item["code"], []), quote_cache) for item in candidates]
    orders = paper_trading.load_orders(BASE_DIR, trading_date=REPORT_DATE, filled_only=False)
    orders_by_code = {}
    for order in orders:
        if str(order.get("side") or "").upper() != "BUY":
            continue
        orders_by_code.setdefault(str(order.get("symbol") or ""), []).append(order)
    for item in reviewed:
        code_orders = orders_by_code.get(str(item.get("code") or ""), [])
        fills = [x for x in code_orders if x.get("status") in ("FILLED", "PARTIAL_FILLED")]
        rejects = [x for x in code_orders if x.get("status") == "REJECTED"]
        if fills:
            item["execution_status"] = "已模拟成交"
            item["execution_order_ids"] = [x.get("order_id") for x in fills]
        elif rejects:
            item["execution_status"] = "触发但被策略拦截"
            item["execution_reason"] = rejects[-1].get("reason")
        elif item.get("triggered"):
            item["execution_status"] = "未进入成交门控"
            item["execution_reason"] = "候选复盘触发不等于成交；需实时满足全局风险、VWAP/OR15、量能、追价和盈亏比门控。"
        else:
            item["execution_status"] = "未触发，不成交"
    triggered = [x for x in reviewed if x.get("triggered")]
    effective = [x for x in reviewed if x.get("result") in ("候选有效", "收盘验证有效")]
    failed = [x for x in reviewed if x.get("result") in ("先失效", "触发后失效")]
    summary = [
        f"集合竞价候选复盘：候选 {len(reviewed)} 只，开盘后触发 {len(triggered)} 只，候选有效 {len(effective)} 只，失效/先失效 {len(failed)} 只。",
    ]
    if effective:
        summary.append("有效候选：" + "、".join(f"{x['name']}({x['code']})" for x in effective[:5]) + "。")
    if failed:
        summary.append("失效候选：" + "、".join(f"{x['name']}({x['code']})" for x in failed[:5]) + "；后续继续收紧形态和竞价承接。")
    execution_counts = {}
    for item in reviewed:
        status = item.get("execution_status") or "未知"
        execution_counts[status] = execution_counts.get(status, 0) + 1
    if execution_counts:
        summary.append("模拟执行闭环：" + "、".join(f"{key} {value}只" for key, value in execution_counts.items()) + "。触发复盘与实际成交分开统计。")
    return {"candidates": reviewed, "summary": summary, "source": data}


def auction_candidate_lines(review, limit=12):
    lines = []
    for item in (review.get("candidates") or [])[:limit]:
        w = item.get("windows") or {}
        p15 = window_price_text(w, 15)
        p30 = window_price_text(w, 30)
        p60 = window_price_text(w, 60)
        mfe30 = f2(((item.get("mfe_mae") or {}).get("30") or {}).get("mfe"))
        mae30 = f2(((item.get("mfe_mae") or {}).get("30") or {}).get("mae"))
        lines.append(
            f"| {item.get('code')} | {item.get('name')} | {item.get('status')} | {item.get('execution_status', '-')} | {f2(item.get('trigger_price'))} | {f2(item.get('invalid_price'))} | "
            f"{item.get('result')} | {item.get('trigger_time') or '-'} | {p15}/{p30}/{p60} | {mfe30}/{mae30} | {item.get('review')} |"
        )
    return lines


def make_report(stocks, indexes, boards_top, boards_keep, ths_focus=None, signal_review=None, paper_review=None, auction_review=None, paper_snapshot=None, evidence_summary=None, observation_summary=None):
    now = current_datetime().strftime("%Y-%m-%d %H:%M:%S")
    index_line = "；".join(f"{x['name']} {x['close']:.2f} {pct(x['pct'])}" for x in indexes)
    top_board_line = board_strength_text(boards_top, boards_keep)
    quote_cache = load_quote_cache()
    if top_board_line == "板块数据暂缺":
        top_board_line = quote_theme_strength_text(quote_cache)
    ths_focus = ths_focus or {}
    signal_review = signal_review or {"events": [], "summary": ["实时信号复盘数据暂缺。"]}
    paper_review = paper_review or {"orders": [], "summary": ["模拟盘成交复盘数据暂缺。"]}
    auction_review = auction_review or {"candidates": [], "summary": ["集合竞价候选复盘数据暂缺。"]}
    lines = []
    lines.append(f"# 同花顺我的股票下个交易日订盘建议｜{NEXT_TRADING_DATE}（基于 {REPORT_DATE} 收盘）\n")
    lines.append(f"- 生成时间：{now}（Asia/Shanghai）")
    lines.append(f"- 订盘执行日：{NEXT_TRADING_DATE}（下个A股交易日，基于 {REPORT_DATE} 收盘复盘生成）")
    lines.append("- 说明：按“情绪、筹码、时间 + 三周期 + 顺大势逆小势”执行；仅为交易计划参考，不构成投资建议。")
    lines.append("- 数据源：同花顺本地自选、同花顺复盘/异动/公告日历、同花顺 timeline/news/noticeReport/社区尝试、腾讯行情、搜狐日K、东方财富/新浪分钟线、本地实时信号与模拟盘快照交叉。")
    lines.append("")
    lines.append("## 一、市场情绪")
    lines.append(f"- 指数：{index_line}。")
    lines.append(f"- 强势板块：{top_board_line}。")
    for item in market_review_lines(stocks, indexes, boards_top, boards_keep, quote_cache):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## 二、同花顺复盘/异动/日历")
    focus_lines = ths_focus_lines(ths_focus)
    if focus_lines:
        for item in focus_lines:
            lines.append(f"- {item}")
    else:
        lines.append("- 同花顺复盘/异动/日历模块暂未返回有效摘要，个股判断以行情、公告、F10 和量价结构为主。")
    lines.append("")
    lines.append("## 三、盘后专业复盘结论")
    lines.append("- 今日复盘以“真实行情快照 + 实时信号事件 + 模拟盘订单 + 集合竞价候选”四条链路交叉，不再采信日期未确认的旧复盘文案。")
    if auction_review.get("summary"):
        lines.append(f"- 集合竞价：{auction_review['summary'][0]}")
    if paper_review.get("summary"):
        lines.append(f"- 模拟盘：{paper_review['summary'][0]}")
    if signal_review.get("summary"):
        lines.append(f"- 实时信号：{signal_review['summary'][0]}")
        lines.append(f"- {NEXT_TRADING_DATE} 操作含义：强势候选只在开盘后再次完成价格/量能确认才可模拟成交；P0 风控是风险暴露线，不是机械挂单价。")
    lines.append("")
    lines.append("## 四、模拟盘成交质量复盘")
    for item in paper_review.get("summary", []):
        lines.append(f"- {item}")
    orders = paper_review.get("orders") or []
    if orders:
        lines.append("")
        lines.append("| 时间 | 代码 | 名称 | 方向 | 场景 | 数量 | 成交价 | 1/3/5/15分钟价 | 交易质量 | 复盘结论 |")
        lines.append("|---|---|---|---|---|---:|---:|---|---|---|")
        lines.extend(paper_trade_lines(paper_review, limit=16))
    lines.append("")
    funnel = paper_review.get("decision_funnel") or {}
    funnel_counts = funnel.get("counts") or {}
    lines.append("## 五、盘中候选到成交决策漏斗")
    lines.append("- 口径：候选生成、全球门控、技术/量能/RR、订单拒绝/取消和成交分别计数；终态快照不再覆盖盘中出现过的机会。")
    lines.append(
        f"- 候选 {funnel_counts.get('candidate', 0)} 只｜全球阻断 {funnel_counts.get('global_blocked', 0)} 只｜"
        f"技术未过 {funnel_counts.get('technical_blocked', 0)} 只｜可执行 {funnel_counts.get('ready', 0)} 只｜"
        f"成交 {funnel_counts.get('order_filled', 0)} 笔｜拒绝 {funnel_counts.get('order_rejected', 0)} 笔｜取消 {funnel_counts.get('order_cancelled', 0)} 笔。"
    )
    lines.append("")
    lines.append("## 六、模拟盘纪律执行复盘")
    lines.append("- 口径：模拟盘买入/卖出必须同时记录策略框架依据和纪律检查；纪律未通过的信号只允许记录为拒单或风险暴露，不应成交。")
    discipline_orders = paper_review.get("all_orders") or paper_review.get("orders") or []
    if discipline_orders:
        lines.append("| 时间 | 代码 | 名称 | 方向 | 状态 | 纪律结论 | 通过项 | 失败项 | 策略依据 |")
        lines.append("|---|---|---|---|---|---|---:|---|---|")
        lines.extend(paper_discipline_lines(paper_review, limit=18))
    else:
        lines.append("- 今日无模拟订单可做纪律复盘。")
    lines.append("")
    append_paper_position_section(lines, paper_snapshot)
    lines.append("## 七、集合竞价候选跟踪复盘")
    for item in auction_review.get("summary", []):
        lines.append(f"- {item}")
    candidates = auction_review.get("candidates") or []
    if candidates:
        lines.append("")
        lines.append("| 代码 | 名称 | 竞价状态 | 模拟执行 | 触发价 | 失效价 | 结果 | 触发时间 | 15/30/60分钟价 | 30分钟MFE/MAE | 复盘结论 |")
        lines.append("|---|---|---|---|---:|---:|---|---|---|---|---|")
        lines.extend(auction_candidate_lines(auction_review, limit=16))
    lines.append("")
    lines.append("## 八、实时信号质量复盘")
    for item in signal_review.get("summary", []):
        lines.append(f"- {item}")
    events = signal_review.get("events") or []
    if events:
        lines.append("")
        lines.append("| 时间 | 代码 | 名称 | 类型 | 级别 | 场景 | 触发价 | 关键线 | 失效/恢复 | 1/3/5/15分钟价 | 走势判定 | 合理性 | 迭代动作 |")
        lines.append("|---|---|---|---|---|---|---:|---:|---:|---|---|---|---|")
        lines.extend(signal_quality_lines(signal_review, limit=16))
    lines.append("")
    lines.append("## 九、全自选观察池策略覆盖")
    lines.append("- 覆盖范围：同花顺全部非核心自选分组；与“我的股票/观察池”重合标的已去重。")
    lines.append("- 边界：每只进入盘前计划、盘中信号和盘后复盘。仅龙头、520、五日线三类策略合同可生成对应的模拟买入事件；待分类标的持续盯盘但不开放新增仓。")
    lines.extend(emotion_leader_summary_lines(stocks))
    lines.extend(observation_group_summary_lines(observation_summary))
    lines.extend(observation_strategy_summary_lines(stocks))
    lines.append("")
    lines.append("## 影子研究信号表现（禁止下单，不计入账户）")
    lines.extend('- ' + line for line in shadow_research_reporting.text_lines(
        signal_review.get('shadow_research') or {}))
    lines.append("")
    lines.extend(mira_evidence_log.markdown_section(evidence_summary))
    lines.append("")
    lines.append(f"## 十、{NEXT_TRADING_DATE} 订盘总览")
    lines.append("| 代码 | 名称 | 计划范围 | 策略/分类 | 状态 | 优先级 | 收盘 | 涨跌幅 | 支撑（回踩观察） | 防守/止损（硬失效） | 压力 | 策略入场证据 | 减仓价 |")
    lines.append("|---|---|---|---|---|---|---:|---:|---:|---:|---:|---|---|")
    for s in stocks:
        q, t = s["quote"], s["tech"]
        strategy = s.get("strategy_contract") or {}
        evidence = "、".join(strategy.get("allowed_patterns") or []) or "仅观察"
        lines.append(f"| {q['code']} | {q['name']} | {s.get('plan_universe') or '核心股票池'} | {strategy.get('style') or '核心池'}/{strategy.get('name') or 'V2'} | {t['color']} {t['state']} | {t['priority']} | {f2(q['close'])} | {pct(q['pct'])} | {f2(t['support'])} | {f2(t['defense'])} | {f2(t['pressure'])} | {evidence} | {t['reduce']} |")
    lines.append("")
    lines.append(f"## 十一、{NEXT_TRADING_DATE} P0/P1/P2 操作分组")
    for p in ("P0", "P1", "P2"):
        group = [s for s in stocks if s["tech"]["priority"] == p]
        if not group:
            continue
        lines.append(f"### {p}")
        for s in group:
            q, t = s["quote"], s["tech"]
            lines.append(f"- {q['code']} {q['name']}：{t['color']} {t['state']}。{t['reduce']}。")
        lines.append("")
    lines.append(f"## 十二、今日未操作后的 {NEXT_TRADING_DATE} 处理")
    for item in no_action_review_lines(stocks):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## 十三、重点个股盘后卡片")
    detail_stocks = [
        stock for stock in stocks
        if not stock.get("observation_plan_group")
        or (stock.get("strategy_contract") or {}).get("key") != observation_strategy_router.OBSERVE
    ]
    for s in detail_stocks[:36]:
        q, t = s["quote"], s["tech"]
        f10 = s["f10"]
        profile = f10.get("profile") or {}
        holder = f10.get("holder") or {}
        concepts = "、".join((f10.get("concepts") or [])[:6]) or "右侧概念资料暂缺"
        mas = t["mas"]
        ths_summary = summarize_ths(s, s["ths"])
        lines.append(f"### {q['code']} {q['name']}")
        strategy = s.get("strategy_contract") or {}
        lines.append(f"- 范围/策略：{s.get('plan_universe') or '核心股票池'}｜{strategy.get('style') or '待分类'}｜{strategy.get('name') or '历史合同已停用'}；时机证据 {'、'.join(strategy.get('allowed_patterns') or []) or '无新增仓权限'}。")
        lines.append(f"- 状态：{t['color']} {t['state']}，优先级 {t['priority']}。收盘 {f2(q['close'])}，涨跌幅 {pct(q['pct'])}，成交额 {q['amount_wan'] / 10000:.2f}亿，换手 {f2(q.get('turnover'))}%。")
        lines.append(f"- 情绪：{s['emotion']}。")
        lines.append(f"- 同花顺特色数据：{ths_summary}")
        lines.append(f"- 右侧资料/F10：行业 {profile.get('industry', '暂缺')}；概念 {concepts}；股东户数 {holder.get('date', '暂缺')} {holder.get('count', '暂缺')}户，变化 {holder.get('ratio', '暂缺')}%，集中度 {holder.get('focus', '暂缺')}。")
        sample_titles = "；".join((s["ths"].get("community_titles") or [])[:3]) or "无可展示帖子"
        lines.append(f"- 社区温度：{s['community_temp']}；{s['ths'].get('community_note', '社区读取情况暂缺')}；样本：{sample_titles}。社区信息仅作温度计，不作为事实催化。")
        lines.append(f"- 筹码/时间：近20日高低 {f2(t['trend_pressure'])}/{f2(t['recent_low'])}；量比近5日均额 {f2(t['vol_ratio'])}；MA5/10/20/30/60 = {f2(mas[5])}/{f2(mas[10])}/{f2(mas[20])}/{f2(mas[30])}/{f2(mas[60])}。")
        lines.append(f"- 关键位：支撑 {f2(t['support'])}；防守/止损 {f2(t['defense'])}；修复触发 {f2(t['repair'])}；{t['pressure_kind']} {f2(t['pressure'])}；趋势压力 {f2(t['trend_pressure'])}；当日涨停边界 {f2(t['next_limit_up'])}。")
        lines.append(f"- 加仓计划：触发 {f2(t['add_trigger']) if t.get('add_trigger') else '暂无'}（{t['add_mode']}）；确认：{t['add_confirm']}；取消：{t['add_cancel']}。")
        lines.append(f"- 减仓价：{t['reduce']}。")
        lines.append(f"- {NEXT_TRADING_DATE} 计划：开盘先看量能和承接。若高开无量冲压力，优先减压；若低开但快速收回防守位且放量，再观察修复。")
        lines.append("")
    lines.append(f"## 十四、{NEXT_TRADING_DATE} 09:30 首次刷新重点")
    lines.append("- 集合竞价结束后重点看：指数高低开、自选股竞价量能、高开是否有承接、低开是否快速修复、同花顺特色资讯/右侧资料是否强化或削弱开盘情绪、社区是否出现一致亢奋或恐慌。")
    lines.append("- 加仓只看条件触发，不预设买入；跌破防守位按风险处理，不用“洗盘”解释高位放量下跌。")
    lines.append("")
    lines.extend(
        mira_research_gate.markdown_section_from_text(
            kind="afterclose",
            title=f"同花顺我的股票下个交易日订盘建议｜{NEXT_TRADING_DATE}",
            date=REPORT_DATE,
            generated=now,
            raw_text="\n".join(lines),
            row_count=len(stocks),
        )
    )
    return "\n".join(lines)


def lark_text(text):
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def send_feishu(stocks, indexes, boards_top, ths_focus=None, signal_review=None, paper_review=None, auction_review=None, observation_summary=None):
    if os.environ.get("A_SHARE_SKIP_FEISHU") == "1":
        return json.dumps({"code": 0, "msg": "skipped by A_SHARE_SKIP_FEISHU"}, ensure_ascii=False)

    ths_focus = ths_focus or {}
    signal_review = signal_review or {"summary": ["实时信号复盘数据暂缺。"]}
    paper_review = paper_review or {"summary": ["模拟盘成交复盘数据暂缺。"]}
    auction_review = auction_review or {"summary": ["集合竞价候选复盘数据暂缺。"]}
    index_line = " | ".join(f"{x['name']} {x['close']:.2f} {pct(x['pct'])}" for x in indexes)
    board_line = board_strength_text(boards_top, {}, limit=6)
    p0 = [s for s in stocks if s["tech"]["priority"] == "P0"]
    p1 = [s for s in stocks if s["tech"]["priority"] == "P1"]
    p2 = [s for s in stocks if s["tech"]["priority"] == "P2"]

    def group_line(group):
        if not group:
            return "无"
        return "\n".join(
            f"{s['tech']['color']} **{s['quote']['code']} {s['quote']['name']}**｜{s['tech']['state']}｜防守 {f2(s['tech']['defense'])}｜压力 {f2(s['tech']['pressure'])}"
            for s in group
        )

    focus = p0 + p1[:5]
    focus_lines = []
    community_ok = sum(1 for s in stocks if s["ths"].get("community_available"))
    for s in focus:
        q, t = s["quote"], s["tech"]
        focus_lines.append(
            f"{t['color']} **{q['code']} {q['name']}** `{t['priority']}`\n"
            f"支撑 {f2(t['support'])}｜压力 {f2(t['pressure'])}｜止损/失效 {f2(t['defense'])}\n"
            f"加仓：{t['add']}\n"
            f"减仓：{t['reduce']}\n"
            f"社区：{s['community_temp']}｜样本：{'；'.join((s['ths'].get('community_titles') or [])[:2]) or '无'}｜资料：{s['f10_summary']}"
        )
    fupan_lines = ths_focus_lines(ths_focus, limit=6)
    no_action_lines = no_action_review_lines(stocks, limit=4)

    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": f"A股下个交易日订盘建议｜{NEXT_TRADING_DATE}"},
            },
            "elements": [
                lark_text(f"**订盘基准**\n基于 {REPORT_DATE} 收盘复盘，执行日：{NEXT_TRADING_DATE}。"),
                lark_text(f"**市场情绪**\n{index_line}\n强势方向：{board_line}"),
                lark_text("**同花顺每日复盘/异动/日历**\n" + "\n".join(f"- {x}" for x in fupan_lines or ["同花顺特色复盘模块暂未返回有效摘要"])),
                lark_text("**模拟盘成交质量复盘**\n" + "\n".join(f"- {x}" for x in paper_review.get("summary", [])[:4])),
                lark_text("**集合竞价候选复盘**\n" + "\n".join(f"- {x}" for x in auction_review.get("summary", [])[:4])),
                lark_text("**实时信号质量复盘**\n" + "\n".join(f"- {x}" for x in signal_review.get("summary", [])[:4])),
                lark_text("**影子研究表现｜非成交收益**\n" + "\n".join(
                    shadow_research_reporting.text_lines(signal_review.get('shadow_research') or {}, limit=10))),
                lark_text(
                    "**全自选观察池策略覆盖**\n"
                    "全部同花顺非核心自选均进入次日计划、盘中信号与盘后复盘；待分类只观察，三类已分类策略才可能生成模拟买入。\n"
                    + "\n".join(emotion_leader_summary_lines(stocks, limit=8))
                    + "\n"
                    + "\n".join(observation_group_summary_lines(observation_summary, limit=7))
                    + "\n"
                    + "\n".join(observation_strategy_summary_lines(stocks, limit=4))
                ),
                {"tag": "hr"},
                lark_text(f"**{NEXT_TRADING_DATE} 核心订盘**\n以 {REPORT_DATE} 收盘复盘为基准，风险信号优先于进攻信号；竞价候选09:35后只有在全局门控、价格量能和盈亏比共同确认时才可执行模拟成交。"),
                lark_text(f"**今日未操作后的 {NEXT_TRADING_DATE} 处理**\n" + "\n".join(f"- {x}" for x in no_action_lines)),
                {"tag": "hr"},
                lark_text(f"**P0 风险处理**\n{group_line(p0)}"),
                lark_text(f"**P1 重点盯盘**\n{group_line(p1)}"),
                lark_text(f"**P2 持有观察**\n{group_line(p2)}"),
                {"tag": "hr"},
                lark_text(f"**同花顺资料/社区说明**\n已读取 timeline/news/noticeReport、F10/右侧资料、概念、股东信息；同花顺社区登录态读取成功 {community_ok}/{len(stocks)} 只。社区只作为情绪温度计，不作为事实催化。"),
                {"tag": "hr"},
                lark_text("**重点个股卡片**\n" + "\n\n".join(focus_lines)),
                {"tag": "hr"},
                lark_text("仅为交易计划参考，不构成投资建议。"),
            ],
        },
    }
    data = json.dumps(card, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(FEISHU_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode("utf-8", "ignore")
    try:
        payload = json.loads(text)
        code = payload.get("code", payload.get("StatusCode", 0))
        if code not in (0, "0", None):
            raise RuntimeError(text)
    except json.JSONDecodeError:
        pass
    return text


def main():
    core_universe = read_watchlist()
    observation_details = read_observation_watchlist_details()
    observation_plan = premarket_plan_observation_details(observation_details)
    try:
        emotion_snapshot = emotion_leader_pool.build_snapshot(BASE_DIR)
        emotion_leader_pool.write_snapshot(BASE_DIR, emotion_snapshot)
    except Exception:
        emotion_snapshot = emotion_leader_pool.load_latest_snapshot(BASE_DIR)
    if str(emotion_snapshot.get("source_date") or "") != REPORT_DATE:
        emotion_snapshot = {}
    observation_plan, emotion_candidates = emotion_leader_pool.merge_into_plan(observation_plan, emotion_snapshot)
    core_codes = {code for code, _market in core_universe}
    plan_group_by_code = {}
    for group in observation_plan.get("groups") or []:
        for code in group.get("codes") or []:
            plan_group_by_code.setdefault(str(code), str(group.get("name") or "未分组"))
    universe = list(core_universe)
    universe.extend(
        (code, market)
        for code, market in (observation_plan.get("rows") or [])
        if code not in core_codes
    )
    quote_cache = load_quote_cache()
    if USE_QUOTE_CACHE and quote_cache:
        quotes = quotes_from_cache(universe, quote_cache)
        indexes = indexes_from_cache(quote_cache) or fetch_indexes()
        boards_top, boards_keep = ([], {}) if SKIP_THS_DISCOVER else fetch_boards()
    else:
        quotes = verified_closing_quotes(universe, REPORT_DATE, quote_cache)
        try:
            indexes = fetch_indexes()
            if len(indexes) < 3:
                raise RuntimeError("收盘指数覆盖不足")
        except Exception:
            index_quotes = verified_closing_quotes(
                [("000001", "17"), ("399001", "33"), ("399006", "33")], REPORT_DATE, quote_cache)
            indexes = indexes_from_cache(index_quotes)
        boards_top, boards_keep = fetch_boards()
    observation_quotes = {code: quote for code, quote in quotes.items() if code in {row[0] for row in (observation_details.get("rows") or [])}}
    observation_summary = observation_group_summary(observation_details, observation_quotes)
    ths_focus = fetch_ths_focus_snapshot()
    stocks = []
    full_deep_context = os.environ.get("A_SHARE_FULL_WATCHLIST_DEEP_CONTEXT") == "1"
    for code, market in universe:
        quote = quotes.get(code)
        if not quote:
            continue
        emotion_candidate = emotion_candidates.get(code) or {}
        if emotion_candidate.get("float_market_cap_yi"):
            quote["float_market_cap_yi"] = emotion_candidate["float_market_cap_yi"]
        daily = [] if FAST_REBUILD else fetch_sohu_daily(code)
        if not daily and FAST_REBUILD:
            daily = [{
                "date": REPORT_DATE,
                "open": quote.get("open") or quote.get("close"),
                "close": quote.get("close"),
                "change": quote.get("change"),
                "pct": quote.get("pct"),
                "low": quote.get("low") or quote.get("close"),
                "high": quote.get("high") or quote.get("close"),
                "volume_lot": quote.get("volume_lot"),
                "amount_wan": quote.get("amount_wan") or 0,
                "turnover": quote.get("turnover"),
            }]
        if not daily:
            continue
        daily = daily_with_quote(daily, quote)
        quote["code"] = code
        tech = trend_text(daily, quote)
        deep_context = bool(
            full_deep_context
            or code in core_codes
            or abs(float(quote.get("pct") or 0)) >= 5.0
            or bool(emotion_candidate)
            or str(tech.get("priority") or "") in {"P0", "P1"}
        )
        minline = fetch_minline(code) if deep_context else []
        ths = {"news": [], "notice": [], "community_available": False, "community_note": "本次重建跳过同花顺社区/个股资讯慢接口"}
        f10 = {"concepts": [], "profile": {}, "holder": {}}
        if not SKIP_STOCK_CONTEXT and deep_context:
            ths = fetch_ths_news(code, market)
            f10 = fetch_f10(code)
        elif not deep_context:
            ths["community_note"] = "全量观察池已完成行情/日线/策略覆盖；未进入当日重点深检。"
        community_temp = "社区情绪不可用"
        if ths.get("community_available"):
            titles = " ".join(ths.get("community_titles", []) + ths.get("community_contents", []))
            if not titles.strip():
                community_temp = "🟢 冷静/低关注（样本不足）"
            else:
                bullish = len(re.findall(r"涨|牛|龙头|突破|加仓|看多|冲|爆发|行情|机会", titles))
                bearish = len(re.findall(r"跌|割|利空|出货|套|风险|减仓|下行|跑|当心", titles))
                if bullish + bearish >= 5 and abs(bullish - bearish) >= 3:
                    community_temp = "🔴 一致亢奋/恐慌"
                elif bullish + bearish >= 2:
                    community_temp = "🟡 分歧升温"
                else:
                    community_temp = "🟢 冷静/低关注"
        f10_summary = "、".join((f10.get("concepts") or [])[:3]) or "右侧资料暂缺"
        stock = {
            "quote": quote,
            "daily": daily,
            "min30": aggregate_intraday(minline, 30),
            "min60": aggregate_intraday(minline, 60),
            "ths": ths,
            "f10": f10,
            "tech": tech,
            "community_temp": community_temp,
            "f10_summary": f10_summary,
            "plan_universe": "核心股票池" if code in core_codes else observation_plan_scope_label(plan_group_by_code.get(code, "未分组")),
            "observation_plan_group": plan_group_by_code.get(code),
            "emotion_leader_candidate": emotion_candidate,
            "manual_leader_evidence": list(emotion_candidate.get("reasons") or []),
        }
        stock["emotion"] = emotion_from_data(stock, boards_keep)
        stock["strategy_contract"] = observation_strategy_router.classify_stock(stock)
        stocks.append(stock)
        time.sleep(0.04 if not deep_context else 0.15)

    signal_review = build_signal_quality_review()
    signal_review['shadow_research'] = shadow_research_reporting.safe_review(
        BASE_DIR, REPORT_DATE, quotes, current_datetime())
    paper_review = build_paper_trade_review()
    paper_snapshot = build_paper_position_snapshot(report_date=REPORT_DATE)
    auction_review = build_auction_candidate_review()
    rotation_candidate_path, rotation_candidates = write_rotation_candidates(stocks)
    signal_review_path = write_signal_review_archive(signal_review)
    generated_at = current_datetime().strftime("%Y-%m-%d %H:%M:%S")
    evidence_summary = mira_evidence_log.write_daily_evidence_log(
        base_dir=BASE_DIR,
        trading_date=REPORT_DATE,
        next_trading_date=NEXT_TRADING_DATE,
        generated_at=generated_at,
        signal_review=signal_review,
        paper_review=paper_review,
        auction_review=auction_review,
        paper_snapshot=paper_snapshot,
        signal_review_path=str(signal_review_path),
    )
    report = make_report(stocks, indexes, boards_top, boards_keep, ths_focus, signal_review, paper_review, auction_review, paper_snapshot, evidence_summary, observation_summary)
    REPORT_PATH.write_text(report, encoding="utf-8")
    try:
        dashboard_result = dashboard.publish_report(REPORT_PATH)
    except Exception as exc:
        dashboard_result = {"error": str(exc)}
    feishu_result = send_feishu(stocks, indexes, boards_top, ths_focus, signal_review, paper_review, auction_review, observation_summary)
    print(json.dumps({
        "report": str(REPORT_PATH),
        "base_date": REPORT_DATE,
        "next_trading_date": NEXT_TRADING_DATE,
        "dashboard": dashboard_result,
        "stocks": len(stocks),
        "observation_stocks": len(observation_details.get("rows") or []),
        "feishu_result": feishu_result,
        "p0": [s["quote"]["code"] for s in stocks if s["tech"]["priority"] == "P0"],
        "p1": [s["quote"]["code"] for s in stocks if s["tech"]["priority"] == "P1"],
        "p2": [s["quote"]["code"] for s in stocks if s["tech"]["priority"] == "P2"],
        "fupan_date": ths_focus.get("fupan", {}).get("date"),
        "fupan_themes": len(ths_focus.get("fupan", {}).get("themes", [])),
        "zhangting_items": len(ths_focus.get("zhangting", {}).get("items", [])),
        "signal_events": len(signal_review.get("events", [])),
        "paper_orders": (paper_review.get("order_counts") or {}).get("total", len(paper_review.get("all_orders", []))),
        "paper_fills": (paper_review.get("order_counts") or {}).get("filled", len(paper_review.get("orders", []))),
        "paper_order_counts": paper_review.get("order_counts") or {},
        "paper_positions": len(paper_snapshot.get("positions", [])),
        "auction_candidates": len(auction_review.get("candidates", [])),
        "rotation_candidates": len(rotation_candidates.get("candidates") or []),
        "rotation_candidate_path": str(rotation_candidate_path),
        "signal_review": str(signal_review_path),
        "evidence_log": evidence_summary.get("summary_path"),
        "evidence_claims": evidence_summary.get("total_claims"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
