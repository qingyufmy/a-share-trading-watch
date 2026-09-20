#!/usr/bin/env python3
import json
import hashlib
import plistlib
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path.home() / "Library" / "Application Support" / "a-share-trading-watch"
PLIST_PATH = Path("/Users/tonyyu/Library/LaunchAgents/com.tonyyu.a-share-trading-watch.plist")
DASHBOARD_PLIST_PATH = Path("/Users/tonyyu/Library/LaunchAgents/com.tonyyu.a-share-trading-dashboard.plist")
REALTIME_PLIST_PATH = Path("/Users/tonyyu/Library/LaunchAgents/com.tonyyu.a-share-realtime-signal-engine.plist")
LABEL = "com.tonyyu.a-share-trading-watch"
DASHBOARD_LABEL = "com.tonyyu.a-share-trading-dashboard"
REALTIME_LABEL = "com.tonyyu.a-share-realtime-signal-engine"
PYTHON_APP_EXECUTABLE = Path(
    "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python"
)
RUNTIME_FILES = [
    "after_close_report.py",
    "preopen_quality_check.py",
    "premarket_report.py",
    "auction_path_collector.py",
    "auction_report.py",
    "intraday_report.py",
    "realtime_signal_engine.py",
    "paper_trading.py",
    "paper_close_report.py",
    "run_trading_job.py",
    "render_report_dashboard.py",
]


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_recency(path):
    """Use the market-data timestamp, not deployment mtime, to compare snapshots."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    return str(
        payload.get("fetched_at")
        or payload.get("updated_at")
        or payload.get("source_date")
        or ""
    ).replace("T", " ")


def copy_snapshot_if_fresher(source, destination):
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        return False
    if destination.is_file() and snapshot_recency(source) <= snapshot_recency(destination):
        return False
    shutil.copy2(source, destination)
    return True


def release_manifest(runtime_dir):
    runtime_dir = Path(runtime_dir)
    paths = [runtime_dir / name for name in RUNTIME_FILES]
    paths.extend(sorted((runtime_dir / "core").glob("*.py")))
    paths.extend(sorted((runtime_dir / "configs").glob("*.json")))
    paths.extend(
        runtime_dir / rel for rel in ("web_dashboard/index.html", "web_dashboard/assets/styles.css", "web_dashboard/assets/dashboard.js")
    )
    files = {
        str(path.relative_to(runtime_dir)): file_sha256(path)
        for path in paths if path.is_file()
    }
    identity = hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return {
        "release_id": f"three-method-{identity}",
        "strategy_version": "three_method_strategy_v1",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "files": files,
    }


def infer_market(code):
    return "17" if str(code).startswith("6") else "33"


def launchd_python_executable():
    """Use the macOS app bundle granted Full Disk Access, if available."""
    if PYTHON_APP_EXECUTABLE.is_file():
        return str(PYTHON_APP_EXECUTABLE)
    return str(Path(sys.executable).resolve())


def build_watchlist_fallback():
    latest_reports = sorted(SOURCE_DIR.glob("同花顺我的股票盘前全面分析_*.md"), reverse=True)
    if latest_reports:
        codes = []
        for line in latest_reports[0].read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(r"\|\s*(\d{6})\s*\|", line)
            if m and m.group(1) not in codes:
                codes.append(m.group(1))
        if codes:
            return [{"code": code, "market": infer_market(code)} for code in codes]

    try:
        import after_close_report as base

        rows = base.read_watchlist()
        return [{"code": code, "market": market or infer_market(code)} for code, market in rows]
    except Exception:
        return []


def trading_times():
    # Keep preparation/auction/after-close nodes, then send one readable desk
    # card at each full trading-session hour. Realtime execution events remain
    # independent and are still pushed immediately.
    return [
        (8, 30), (9, 0), (9, 15), (9, 25),
        (10, 0), (11, 0), (13, 0), (14, 0), (15, 0),
        (16, 30),
    ]


def build_calendar():
    entries = []
    for weekday in range(1, 6):
        for hour, minute in trading_times():
            entries.append({"Weekday": weekday, "Hour": hour, "Minute": minute})
    return entries


def realtime_calendar():
    # Start before the A-share open so overseas shock/recovery paths are not
    # lost before the first 09:30 executable tick.
    return [{"Weekday": weekday, "Hour": 8, "Minute": 0} for weekday in range(1, 6)]


def main():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    (RUNTIME_DIR / "logs").mkdir(parents=True, exist_ok=True)
    for filename in RUNTIME_FILES:
        shutil.copy2(SOURCE_DIR / filename, RUNTIME_DIR / filename)
    source_core = SOURCE_DIR / "core"
    if source_core.exists():
        runtime_core = RUNTIME_DIR / "core"
        runtime_core.mkdir(parents=True, exist_ok=True)
        for src in source_core.glob("*.py"):
            shutil.copy2(src, runtime_core / src.name)
    source_configs = SOURCE_DIR / "configs"
    if source_configs.exists():
        runtime_configs = RUNTIME_DIR / "configs"
        runtime_configs.mkdir(parents=True, exist_ok=True)
        for src in source_configs.glob("*.json"):
            shutil.copy2(src, runtime_configs / src.name)
    source_web = SOURCE_DIR / "web_dashboard"
    if source_web.exists():
        runtime_web = RUNTIME_DIR / "web_dashboard"
        for rel in ("index.html", "assets/styles.css", "assets/dashboard.js"):
            src = source_web / rel
            if src.exists():
                dst = runtime_web / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    # Refresh snapshots while deployment runs in the user's interactive
    # context.  The scheduled daemon may be denied access to the Tonghuashun
    # container by macOS privacy controls, but it can safely consume these
    # short-lived, auditable snapshots.
    try:
        import after_close_report as base

        base.read_watchlist_details()
        base.read_observation_watchlist_details()
    except Exception:
        pass
    fallback = build_watchlist_fallback()
    if fallback:
        (RUNTIME_DIR / "watchlist_fallback.json").write_text(json.dumps(fallback, ensure_ascii=False, indent=2), encoding="utf-8")
    source_runtime = SOURCE_DIR / "data" / "runtime"
    runtime_snapshot_dir = RUNTIME_DIR / "data" / "runtime"
    runtime_snapshot_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "watchlist_snapshot_latest.json",
        "watchlist_observation_snapshot_latest.json",
    ):
        source = source_runtime / name
        if source.is_file():
            shutil.copy2(source, runtime_snapshot_dir / name)
    for name in (
        "emotion_leader_pool_latest.json",
        "kaipanla_popularity_latest.json",
    ):
        copy_snapshot_if_fresher(source_runtime / name, runtime_snapshot_dir / name)
    for report in SOURCE_DIR.glob("同花顺我的股票盘前全面分析_*.md"):
        shutil.copy2(report, RUNTIME_DIR / report.name)
    manifest = release_manifest(RUNTIME_DIR)
    (RUNTIME_DIR / "release_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    python_executable = launchd_python_executable()
    schedule_payload = {
        "Label": LABEL,
        "ProgramArguments": [
            python_executable,
            str(RUNTIME_DIR / "run_trading_job.py"),
            "--mode",
            "auto",
        ],
        "WorkingDirectory": str(RUNTIME_DIR),
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
            "A_SHARE_BASE_DIR": str(RUNTIME_DIR),
            "A_SHARE_LOG_DIR": str(RUNTIME_DIR / "logs"),
            "A_SHARE_DISABLE_FEISHU": "0",
            "A_SHARE_INTRADAY_REPORT_FEISHU_ENABLED": "1",
            "A_SHARE_ENFORCE_PREOPEN_QUALITY": "1",
            "A_SHARE_PREMARKET_PLAN_OBSERVATION_GROUPS": "ALL",
        },
        "StartCalendarInterval": build_calendar(),
        "RunAtLoad": True,
        "StartInterval": 60,
        "StandardOutPath": str(RUNTIME_DIR / "logs" / "launchd.out.log"),
        "StandardErrorPath": str(RUNTIME_DIR / "logs" / "launchd.err.log"),
    }
    dashboard_payload = {
        "Label": DASHBOARD_LABEL,
        "ProgramArguments": [
            python_executable,
            "-m",
            "http.server",
            "8765",
            "--bind",
            "127.0.0.1",
        ],
        "WorkingDirectory": str(RUNTIME_DIR),
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(RUNTIME_DIR / "logs" / "dashboard.out.log"),
        "StandardErrorPath": str(RUNTIME_DIR / "logs" / "dashboard.err.log"),
    }
    realtime_payload = {
        "Label": REALTIME_LABEL,
        "ProgramArguments": [
            python_executable,
            str(RUNTIME_DIR / "realtime_signal_engine.py"),
            "--interval",
            "5",
            "--bar-refresh",
            "60",
        ],
        "WorkingDirectory": str(RUNTIME_DIR),
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
            "A_SHARE_BASE_DIR": str(RUNTIME_DIR),
            "A_SHARE_LOG_DIR": str(RUNTIME_DIR / "logs"),
            "A_SHARE_ENFORCE_PREOPEN_QUALITY": "1",
            "A_SHARE_REALTIME_SIGNAL_FEISHU": "1",
            "A_SHARE_CRITICAL_WATCH_FEISHU": "1",
            "A_SHARE_RISK_SELL_FEISHU": "1",
            "A_SHARE_ENGINE_HEALTH_FEISHU": "1",
            "A_SHARE_PREMARKET_PLAN_OBSERVATION_GROUPS": "ALL",
        },
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StartCalendarInterval": realtime_calendar(),
        "StandardOutPath": str(RUNTIME_DIR / "logs" / "realtime_signal_engine.out.log"),
        "StandardErrorPath": str(RUNTIME_DIR / "logs" / "realtime_signal_engine.err.log"),
    }
    with PLIST_PATH.open("wb") as f:
        plistlib.dump(schedule_payload, f, sort_keys=False)
    with DASHBOARD_PLIST_PATH.open("wb") as f:
        plistlib.dump(dashboard_payload, f, sort_keys=False)
    with REALTIME_PLIST_PATH.open("wb") as f:
        plistlib.dump(realtime_payload, f, sort_keys=False)
    print(PLIST_PATH)
    print(DASHBOARD_PLIST_PATH)
    print(REALTIME_PLIST_PATH)


if __name__ == "__main__":
    main()
