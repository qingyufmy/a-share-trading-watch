#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path
from core.runtime_reliability import storage_readiness

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None


BASE_DIR = Path(os.environ.get("A_SHARE_BASE_DIR", Path(__file__).resolve().parent))
LOG_DIR = Path(os.environ.get("A_SHARE_LOG_DIR", BASE_DIR / "logs"))
LOG_PATH = LOG_DIR / "trading_scheduler.log"
LOCK_PATH = LOG_DIR / "trading_scheduler.lock"
# Each decision node has an auditable local report and a matching Feishu card.
# The realtime engine remains the only execution path; cards never place an
# order and should therefore communicate the same report state as the desk.
DEFAULT_FEISHU_REPORT_MODES = {"qualitycheck", "premarket", "auction", "intraday", "paperclose", "afterclose"}
SCHEDULED_AUTO_MODES = {"qualitycheck", "premarket", "auctionpath", "auction", "intraday", "paperclose", "afterclose"}


def parse_now(value):
    if not value:
        return datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise SystemExit(f"无法解析 --now：{value}")


def is_trading_day(now):
    """Use the project's A-share calendar, with weekday fallback on import errors."""
    try:
        from after_close_report import is_a_share_trading_day

        return bool(is_a_share_trading_day(now.date()))
    except Exception:
        return now.weekday() < 5


def in_premarket_window(now):
    return dtime(8, 0) <= now.time() < dtime(9, 5)


def in_auction_window(now):
    return dtime(9, 25) <= now.time() < dtime(9, 30)


def in_auction_path_window(now):
    return dtime(9, 15) <= now.time() < dtime(9, 25)


def in_qualitycheck_window(now):
    return dtime(8, 55) <= now.time() < dtime(9, 15)


def in_intraday_window(now):
    t = now.time()
    return dtime(9, 30) <= t < dtime(11, 31) or dtime(13, 0) <= t < dtime(15, 1)


def in_afterclose_window(now):
    return dtime(16, 30) <= now.time() < dtime(17, 31)


def in_paperclose_window(now):
    return dtime(15, 15) <= now.time() < dtime(16, 30)


def resolve_mode(mode, now):
    if mode != "auto":
        return mode
    # Produce the daily direction and position envelope before the quality
    # check.  The later 08:55 check validates that this artifact is present
    # and executable before auction-path collection starts.
    if in_premarket_window(now) and not in_qualitycheck_window(now):
        return "premarket"
    if in_qualitycheck_window(now):
        return "qualitycheck"
    if in_auction_path_window(now):
        return "auctionpath"
    if in_auction_window(now):
        return "auction"
    if in_intraday_window(now):
        return "intraday"
    if in_paperclose_window(now):
        return "paperclose"
    if in_afterclose_window(now):
        return "afterclose"
    return "skip"


def report_path_for_mode(mode, now):
    compact = now.strftime("%Y%m%d")
    reports_dir = BASE_DIR / "web_dashboard" / "data" / "reports"
    if mode == "premarket":
        return reports_dir / f"premarket_{compact}.json"
    return None


def scheduled_history(now):
    events = []
    try:
        with LOG_PATH.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if str(event.get("now") or event.get("ts") or "").startswith(now.strftime("%Y-%m-%d")):
                    events.append(event)
    except FileNotFoundError:
        pass
    return events


def automatic_node(now, events):
    """Recover preparation only before its deadline; never replay intraday cards."""
    successful = {e.get("mode") for e in events
                  if e.get("event") == "success" and not e.get("skip_feishu", False)}
    mode = ("premarket" if dtime(8, 30) <= now.time() < dtime(9, 5) and "premarket" not in successful
            else resolve_mode("auto", now))
    if mode == "premarket" and now.time() < dtime(8, 30):
        return "skip"
    if mode == "intraday":
        if (now.hour, now.minute) not in {(10, 0), (11, 0), (13, 0), (14, 0), (15, 0)}:
            return "skip"
        if any(e.get("event") == "success" and e.get("mode") == mode
               and not e.get("skip_feishu", False)
               and str(e.get("now") or "")[11:13] == now.strftime("%H") for e in events):
            return "skip"
    elif mode in successful:
        return "skip"
    if any(e.get("event") == "child_partial_failure" and e.get("mode") == mode
           and (mode != "intraday" or str(e.get("ts") or "")[11:13] == now.strftime("%H")) for e in events):
        return "skip"
    failures = [e for e in events if e.get("event") == "failure" and e.get("mode") == mode
                and not e.get("skip_feishu", False)]
    if mode == "intraday":
        failures = [e for e in failures if str(e.get("now") or "")[11:13] == now.strftime("%H")]
    if len(failures) >= 2:
        return "skip"
    if failures:
        stamp = str(failures[-1].get("ts") or failures[-1].get("now"))[:19].replace("T", " ")
        try:
            if (now - datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")).total_seconds() < 300:
                return "skip"
        except ValueError:
            return "skip"
    return mode


def log_event(**payload):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload.setdefault("ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def acquire_lock(lock_file):
    if fcntl:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    if msvcrt:
        lock_file.seek(0)
        lock_file.write("0")
        lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        return


def child_report_result(stdout):
    """Read the final JSON result, allowing diagnostic lines before it."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?m)^\s*\{", stdout):
        try:
            result, end = decoder.raw_decode(stdout[match.start():].lstrip())
            remaining = stdout[match.start():].lstrip()[end:]
        except ValueError:
            continue
        if isinstance(result, dict) and not remaining.strip():
            return result
    return {}


def run_child(mode, skip_feishu, now):
    script = {
        "qualitycheck": "preopen_quality_check.py",
        "premarket": "premarket_report.py",
        "auctionpath": "auction_path_collector.py",
        "auction": "auction_report.py",
        "intraday": "intraday_report.py",
        "afterclose": "after_close_report.py",
        "paperclose": "paper_close_report.py",
    }[mode]
    timeout = {
        "qualitycheck": 120,
        "premarket": 1200,
        "afterclose": 900,
        "paperclose": 120,
        "auctionpath": 660,
        "auction": 240,
        "intraday": 180,
    }[mode]
    env = os.environ.copy()
    env["A_SHARE_RUN_SOURCE"] = "launchd"
    env["A_SHARE_RUN_NOW"] = now.strftime("%Y-%m-%d %H:%M:%S")
    env.setdefault("A_SHARE_REPORT_DATE", now.strftime("%Y-%m-%d"))
    # Scheduled cards retain the explicit launchd delivery policy.
    report_feishu_enabled = (
        os.environ.get("A_SHARE_REPORT_FEISHU_ENABLED") == "1"
        or mode in DEFAULT_FEISHU_REPORT_MODES
    )
    if mode == "intraday" and os.environ.get("A_SHARE_INTRADAY_REPORT_FEISHU_ENABLED", "1") != "1":
        report_feishu_enabled = False
    if (
        os.environ.get("A_SHARE_DISABLE_FEISHU", "1") != "0"
        or skip_feishu
        or not report_feishu_enabled
    ):
        env["A_SHARE_SKIP_FEISHU"] = "1"
    else:
        # A parent shell/test run may carry a local no-push flag.  Scheduled
        # key nodes explicitly authorised by launchd must not inherit it.
        env.pop("A_SHARE_SKIP_FEISHU", None)
    cmd = [sys.executable, str(BASE_DIR / script)]
    # One retry covers transient quote/network failures while the outer lock
    # preserves a single report chain and avoids duplicate Feishu pushes.
    last_error = ""
    for attempt in (1, 2):
        if mode == "premarket" and os.environ.get("A_SHARE_PREMARKET_DEADLINE"):
            deadline = parse_now(os.environ["A_SHARE_PREMARKET_DEADLINE"])
            remaining = int((deadline - datetime.now()).total_seconds())
            if remaining <= 0:
                raise RuntimeError("盘前补跑已超过09:15截止，保持新增仓阻断")
            timeout = min(timeout, remaining)
        start = time.time()
        try:
            result = subprocess.run(
                cmd,
                cwd=str(BASE_DIR),
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            elapsed = time.time() - start
            report_result = child_report_result(result.stdout)
            dashboard = report_result.get("dashboard")
            dashboard_error = dashboard.get("error") if isinstance(dashboard, dict) else None
            log_event(
                event="child_finished",
                mode=mode,
                attempt=attempt,
                elapsed=round(elapsed, 2),
                returncode=result.returncode,
                stdout=result.stdout[-4000:],
                stderr=result.stderr[-4000:],
                report_result={
                    key: report_result[key]
                    for key in ("report", "dashboard", "feishu_result")
                    if key in report_result
                },
            )
            if dashboard_error:
                # The report and Feishu delivery may already have completed.
                # Retrying the whole child would repeat those side effects.
                log_event(event="child_partial_failure", mode=mode, attempt=attempt,
                          component="dashboard", error=str(dashboard_error), retryable=False)
                raise RuntimeError(f"{script}: dashboard publication failed: {dashboard_error}; report not retried")
            if result.returncode == 0:
                return result.stdout
            last_error = f"{script} failed with code {result.returncode}"
        except subprocess.TimeoutExpired as exc:
            elapsed = time.time() - start
            last_error = f"{script} timed out after {timeout}s"
            log_event(
                event="child_timeout",
                mode=mode,
                attempt=attempt,
                elapsed=round(elapsed, 2),
                stdout=(exc.stdout or "")[-4000:],
                stderr=(exc.stderr or "")[-4000:],
            )
        if attempt == 1:
            log_event(event="child_retry", mode=mode, reason=last_error, retry_in_seconds=5)
            time.sleep(5)
    raise RuntimeError(last_error)


def main():
    parser = argparse.ArgumentParser(description="A-share trading scheduler wrapper")
    parser.add_argument("--mode", choices=["auto", "qualitycheck", "premarket", "auctionpath", "auction", "intraday", "paperclose", "afterclose"], default="auto")
    parser.add_argument("--now", help="Override local current time for dry-runs, e.g. 2026-05-29 10:15:00")
    parser.add_argument("--skip-feishu", action="store_true", help="Run report generation but skip Feishu push")
    parser.add_argument("--force", action="store_true", help="Bypass weekday/time-window guards")
    args = parser.parse_args()

    now = parse_now(args.now)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    with LOCK_PATH.open("w", encoding="utf-8") as lock_file:
        try:
            acquire_lock(lock_file)
        except (BlockingIOError, OSError):
            log_event(event="skip", reason="locked", requested_mode=args.mode, now=now.isoformat())
            return 0

        mode = (automatic_node(now, scheduled_history(now)) if args.mode == "auto" and not args.force
                else resolve_mode(args.mode, now))
        if args.mode == "auto" and mode not in SCHEDULED_AUTO_MODES and mode != "skip":
            log_event(event="skip", reason="scheduled_mode_disabled", resolved_mode=mode, now=now.isoformat())
            return 0
        if not args.force:
            if not is_trading_day(now):
                log_event(event="skip", reason="non_trading_day", requested_mode=args.mode, now=now.isoformat())
                return 0
            if mode == "premarket" and not in_premarket_window(now):
                log_event(event="skip", reason="outside_premarket", requested_mode=args.mode, now=now.isoformat())
                return 0
            if mode == "qualitycheck" and not in_qualitycheck_window(now):
                log_event(event="skip", reason="outside_qualitycheck", requested_mode=args.mode, now=now.isoformat())
                return 0
            if mode == "auctionpath" and not in_auction_path_window(now):
                log_event(event="skip", reason="outside_auctionpath", requested_mode=args.mode, now=now.isoformat())
                return 0
            if mode == "auction" and not in_auction_window(now):
                log_event(event="skip", reason="outside_auction", requested_mode=args.mode, now=now.isoformat())
                return 0
            if mode == "intraday" and not in_intraday_window(now):
                log_event(event="skip", reason="outside_intraday", requested_mode=args.mode, now=now.isoformat())
                return 0
            if mode == "afterclose" and not in_afterclose_window(now):
                log_event(event="skip", reason="outside_afterclose", requested_mode=args.mode, now=now.isoformat())
                return 0
            if mode == "paperclose" and not in_paperclose_window(now):
                log_event(event="skip", reason="outside_paperclose", requested_mode=args.mode, now=now.isoformat())
                return 0
            if mode == "skip":
                log_event(event="skip", reason="outside_windows", requested_mode=args.mode, now=now.isoformat())
                return 0
            existing_report = report_path_for_mode(mode, now)
            # The evening can legitimately create a next-day draft.  The
            # scheduled 08:30 run must still refresh it with overnight news,
            # external-market inputs, and the current watchlist snapshot.
            # Other modes retain their idempotent report guard.
            if (
                mode != "premarket"
                and existing_report
                and existing_report.exists()
                and os.environ.get("A_SHARE_ALLOW_DUPLICATE_REPORT") != "1"
            ):
                log_event(
                    event="skip",
                    reason="report_already_exists",
                    requested_mode=args.mode,
                    resolved_mode=mode,
                    now=now.isoformat(),
                    report=str(existing_report),
                )
                return 0

        log_event(event="start", requested_mode=args.mode, resolved_mode=mode, now=now.isoformat(), skip_feishu=args.skip_feishu)
        previous_deadline = os.environ.get("A_SHARE_PREMARKET_DEADLINE")
        try:
            storage = storage_readiness(BASE_DIR)
            if not storage["ready"]:
                raise RuntimeError(storage["reason"])
            if args.mode == "auto" and mode == "premarket":
                os.environ["A_SHARE_PREMARKET_DEADLINE"] = now.replace(hour=9, minute=15, second=0).strftime("%Y-%m-%d %H:%M:%S")
            stdout = run_child(mode, args.skip_feishu, now)
            log_event(event="success", mode=mode, now=now.isoformat(), skip_feishu=args.skip_feishu, stdout=stdout[-1000:])
            return 0
        except Exception as exc:
            log_event(event="failure", mode=mode, now=now.isoformat(), skip_feishu=args.skip_feishu, error=str(exc))
            return 1
        finally:
            if previous_deadline is None:
                os.environ.pop("A_SHARE_PREMARKET_DEADLINE", None)
            else:
                os.environ["A_SHARE_PREMARKET_DEADLINE"] = previous_deadline


if __name__ == "__main__":
    raise SystemExit(main())
