#!/usr/bin/env python3
import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import intraday_report as intra
import paper_trading
import render_report_dashboard as dashboard
from core import global_risk
from core import theme_validation
from core import cycle_framework
from core import emotion_leader_pool
from core import intraday_timing_v2
from core import local_market_data
from core import market_breadth_data
from core import opportunity_rating
from core import observation_strategy_router
from core import position_registry
from core import signal_tracking
from core.signal_contract import attach_contract
from core.runtime_reliability import storage_readiness


BASE_DIR = intra.BASE_DIR
LOG_DIR = Path(os.environ.get("A_SHARE_LOG_DIR", BASE_DIR / "logs"))
RUNTIME_DIR = BASE_DIR / "data" / "runtime"
WEB_RUNTIME_DIR = BASE_DIR / "web_dashboard" / "data" / "runtime"
STATE_DB = RUNTIME_DIR / "signal_state.sqlite"
EVENT_LOG = RUNTIME_DIR / "signal_events.jsonl"
SIGNAL_AUDIT_LOG = RUNTIME_DIR / f"signal_audit_{intra.REPORT_DATE.replace('-', '')}.jsonl"
LATEST_SIGNALS = WEB_RUNTIME_DIR / "latest_signals.json"
SIGNAL_HEALTH = WEB_RUNTIME_DIR / "signal_health.json"
MARKET_OPPORTUNITIES = WEB_RUNTIME_DIR / "market_opportunities.json"
PREOPEN_QUALITY_LATEST = WEB_RUNTIME_DIR / "preopen_quality" / "latest.json"
MARKDOWN_REPORT = BASE_DIR / f"同花顺我的股票实时信号_{intra.REPORT_DATE}.md"
RISK_SELL_PUSH_STATE = RUNTIME_DIR / "risk_sell_push_state.json"
CRITICAL_WATCH_PUSH_STATE = RUNTIME_DIR / "critical_watch_push_state.json"
ENGINE_HEALTH_ALERT_STATE = RUNTIME_DIR / "engine_health_alert_state.json"
QUOTE_CACHE = RUNTIME_DIR / "quote_cache.json"
MARKET_PROFILE_CACHE = RUNTIME_DIR / "market_profiles.json"
REPORTS_DIR = BASE_DIR / "web_dashboard" / "data" / "reports"
AUCTION_CANDIDATES_DIR = BASE_DIR / "data" / "auction_candidates"
ROTATION_CANDIDATES_DIR = BASE_DIR / "data" / "rotation_candidates"
GLOBAL_RISK_REFRESH_SECONDS = int(os.environ.get("A_SHARE_GLOBAL_RISK_REFRESH_SECONDS") or 60)
MARKET_BREADTH_REFRESH_SECONDS = int(os.environ.get("A_SHARE_MARKET_BREADTH_REFRESH_SECONDS") or 120)
MARKET_BREADTH_SCAN_SIZE = int(os.environ.get("A_SHARE_MARKET_BREADTH_SCAN_SIZE") or 6000)
MARKET_BREADTH_PAGE_SIZE = 100
MARKET_BREADTH_FETCH_BUDGET_SECONDS = 20.0
MARKET_BREADTH_MIN_COVERAGE_RATIO = float(os.environ.get("A_SHARE_MARKET_BREADTH_MIN_COVERAGE_RATIO") or 0.85)
MARKET_BREADTH_WEAK_DOWN_RATIO = float(os.environ.get("A_SHARE_MARKET_BREADTH_WEAK_DOWN_RATIO") or 0.62)
MARKET_BREADTH_WEAK_UP_RATIO = float(os.environ.get("A_SHARE_MARKET_BREADTH_WEAK_UP_RATIO") or 0.35)
MARKET_BREADTH_SHRINK_RATIO = float(os.environ.get("A_SHARE_MARKET_BREADTH_SHRINK_RATIO") or 0.92)
ROTATION_PERSIST_MIN_SAMPLES = int(os.environ.get("A_SHARE_ROTATION_PERSIST_MIN_SAMPLES") or 2)
ROTATION_PILOT_MAX_CANDIDATES = int(os.environ.get("A_SHARE_ROTATION_PILOT_MAX_CANDIDATES") or 2)
OBSERVATION_GROUP_PROMOTION_MIN_PCT = float(
    os.environ.get("A_SHARE_OBSERVATION_GROUP_PROMOTION_MIN_PCT") or 4.0
)
OBSERVATION_GROUP_PROMOTION_MIN_RISERS = int(
    os.environ.get("A_SHARE_OBSERVATION_GROUP_PROMOTION_MIN_RISERS") or 2
)
OBSERVATION_GROUP_PROMOTION_LIMIT = int(
    os.environ.get("A_SHARE_OBSERVATION_GROUP_PROMOTION_LIMIT") or 2
)
def normalized_position_pct(value, default):
    """Return a position fraction while accepting legacy whole-percent env values."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    if parsed > 1:
        parsed /= 100
    return max(0.0, min(parsed, 1.0))


ROTATION_PILOT_ENTRY_CAP_PCT = normalized_position_pct(
    os.environ.get("A_SHARE_ROTATION_PILOT_ENTRY_CAP_PCT"),
    0.015,
)

TICK = 0.01
VERSION = "intraday_timing_v2_0"
ENGINE_SOURCE_MTIME_NS = Path(__file__).stat().st_mtime_ns
P0_REPEAT_SECONDS = 15 * 60
P0_WORSEN_PCT = 0.008
P1_MAX_AMOUNT_RATIO_1M = 3.5
MAX_RADAR_SIM_SIGNALS_PER_TICK = int(os.environ.get("A_SHARE_PAPER_RADAR_MAX_PER_TICK") or 1)
MAX_RADAR_SIM_ORDERS_PER_DAY = int(os.environ.get("A_SHARE_PAPER_RADAR_MAX_PER_DAY") or 3)
RADAR_SIM_MIN_AMOUNT_RATIO_1M = float(os.environ.get("A_SHARE_PAPER_RADAR_MIN_AMOUNT_RATIO_1M") or 1.30)
RADAR_SIM_MIN_AMOUNT_RATIO_5M = float(os.environ.get("A_SHARE_PAPER_RADAR_MIN_AMOUNT_RATIO_5M") or 1.15)
# Green-market confirmation is a narrow fallback for the first 15-30 minutes:
# local structure must pass, while volume is allowed to be modestly below the
# full-session threshold. Yellow/red sessions continue using the strict gate.
RADAR_GREEN_CONFIRM_MIN_AMOUNT_RATIO_1M = float(
    os.environ.get("A_SHARE_PAPER_RADAR_GREEN_CONFIRM_MIN_AMOUNT_RATIO_1M") or 0.85
)
RADAR_GREEN_CONFIRM_MIN_AMOUNT_RATIO_5M = float(
    os.environ.get("A_SHARE_PAPER_RADAR_GREEN_CONFIRM_MIN_AMOUNT_RATIO_5M") or 0.95
)
RADAR_GREEN_CONFIRM_START = "09:45"
RADAR_CANDIDATE_MAX_AGE_SECONDS = int(os.environ.get("A_SHARE_PAPER_RADAR_CANDIDATE_MAX_AGE_SECONDS") or 15 * 60)
MINUTE_EXECUTION_MAX_DELAY_SECONDS = int(os.environ.get("A_SHARE_MINUTE_EXECUTION_MAX_DELAY_SECONDS") or 150)
RADAR_SIM_MAX_VWAP_DISTANCE_PCT = float(os.environ.get("A_SHARE_PAPER_RADAR_MAX_VWAP_DISTANCE_PCT") or 0.0065)
RADAR_SIM_MAX_VWAP_DISTANCE_ATR5 = float(os.environ.get("A_SHARE_PAPER_RADAR_MAX_VWAP_DISTANCE_ATR5") or 0.65)
RADAR_REPAIR_MAX_VWAP_DISTANCE_PCT = float(os.environ.get("A_SHARE_PAPER_RADAR_REPAIR_MAX_VWAP_DISTANCE_PCT") or 0.025)
RADAR_REPAIR_MAX_VWAP_DISTANCE_ATR5 = float(os.environ.get("A_SHARE_PAPER_RADAR_REPAIR_MAX_VWAP_DISTANCE_ATR5") or 1.25)
RADAR_SIM_SLIPPAGE_ROOM_PCT = float(os.environ.get("A_SHARE_PAPER_RADAR_SLIPPAGE_ROOM_PCT") or 0.0020)
REALTIME_SIGNAL_FEISHU_ENABLED = os.environ.get("A_SHARE_REALTIME_SIGNAL_FEISHU") == "1"
RISK_SELL_FEISHU_ENABLED = os.environ.get("A_SHARE_RISK_SELL_FEISHU") == "1"
ENGINE_HEALTH_FEISHU_ENABLED = os.environ.get("A_SHARE_ENGINE_HEALTH_FEISHU") == "1"
CRITICAL_WATCH_FEISHU_ENABLED = os.environ.get("A_SHARE_CRITICAL_WATCH_FEISHU") == "1"
CRITICAL_WATCH_COOLDOWN_SECONDS = int(
    os.environ.get("A_SHARE_CRITICAL_WATCH_COOLDOWN_SECONDS") or 15 * 60
)
CRITICAL_WATCH_GLOBAL_COOLDOWN_SECONDS = int(
    os.environ.get("A_SHARE_CRITICAL_WATCH_GLOBAL_COOLDOWN_SECONDS") or 15 * 60
)
CRITICAL_WATCH_MIN_PERSISTENCE_SECONDS = int(
    os.environ.get("A_SHARE_CRITICAL_WATCH_MIN_PERSISTENCE_SECONDS") or 60
)
CRITICAL_WATCH_MAX_CANDIDATES = int(
    os.environ.get("A_SHARE_CRITICAL_WATCH_MAX_CANDIDATES") or 3
)
ENGINE_ERROR_ALERT_MIN_CONSECUTIVE = int(os.environ.get("A_SHARE_ENGINE_ERROR_ALERT_MIN_CONSECUTIVE") or 3)
ENGINE_ERROR_ALERT_COOLDOWN_SECONDS = int(os.environ.get("A_SHARE_ENGINE_ERROR_ALERT_COOLDOWN_SECONDS") or 300)
PORTFOLIO_RISK_POSITION_PCT = float(os.environ.get("A_SHARE_PORTFOLIO_RISK_POSITION_PCT") or 90.0)
PORTFOLIO_RISK_DAY_LOSS_PCT = float(os.environ.get("A_SHARE_PORTFOLIO_RISK_DAY_LOSS_PCT") or -2.5)
PORTFOLIO_RISK_SOFT_BREAK_RATIO = float(os.environ.get("A_SHARE_PORTFOLIO_RISK_SOFT_BREAK_RATIO") or 0.50)
PORTFOLIO_RISK_MAX_UPGRADES = int(os.environ.get("A_SHARE_PORTFOLIO_RISK_MAX_UPGRADES") or 3)
PAPER_PROFIT_GUARD_MIN_PNL_PCT = float(os.environ.get("A_SHARE_PAPER_PROFIT_GUARD_MIN_PNL_PCT") or 20.0)
PAPER_PROFIT_GUARD_DAY_LOSS_PCT = float(os.environ.get("A_SHARE_PAPER_PROFIT_GUARD_DAY_LOSS_PCT") or -5.0)
PAPER_PROFIT_EROSION_MAX_PNL_PCT = float(os.environ.get("A_SHARE_PAPER_PROFIT_EROSION_MAX_PNL_PCT") or 10.0)
PAPER_PROFIT_EROSION_DAY_LOSS_PCT = float(os.environ.get("A_SHARE_PAPER_PROFIT_EROSION_DAY_LOSS_PCT") or -7.0)

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
SCENARIO_ORDER = {
    "V2_STRUCTURAL_EXIT": 0,
    "V2_TAKE_PROFIT": 1,
    "V2_REDUCE": 2,
    "V2_NO_ADD": 3,
    "STRATEGY_LEADER_ENTRY": 3,
    "STRATEGY_520_ENTRY": 4,
    "STRATEGY_MA5_ENTRY": 4,
    "V2_WAIT": 9,
    "P0_HARD_RISK_REDUCE": 0,
    "OVERNIGHT_RISK_REDUCE": 0,
    "PAPER_OPEN_RED_CARRY_DE_RISK": 0,
    "SOFT_VWAP_BREAK": 1,
    "HIGH_OPEN_ATTACK_CANCEL": 2,
    "NEAR_TRIGGER": 9,
}
SCENARIO_LABELS = {
    "V2_STRUCTURAL_EXIT": "V2结构退出",
    "V2_TAKE_PROFIT": "V2延伸兑现",
    "V2_REDUCE": "V2确认减仓",
    "V2_NO_ADD": "V2停止加仓",
    "STRATEGY_LEADER_ENTRY": "龙头战法正式首笔",
    "STRATEGY_520_ENTRY": "520战法正式首笔",
    "STRATEGY_MA5_ENTRY": "趋势5日线正式首笔",
    "V2_WAIT": "三策略等待多周期确认",
    "P0_HARD_RISK_REDUCE": "硬防守破位",
    "OVERNIGHT_RISK_REDUCE": "收盘前隔夜风险减仓",
    "PAPER_OPEN_RED_CARRY_DE_RISK": "红色门控早盘隔夜减压",
    "SOFT_VWAP_BREAK": "日内弱线/VWAP破位",
    "HIGH_OPEN_ATTACK_CANCEL": "高开回落取消进攻",
    "NEAR_TRIGGER": "接近关键价",
}


def now_dt():
    return intra.base.current_datetime()


def round_to_tick(value):
    return round(float(value) / TICK) * TICK


def ceil_to_tick(value):
    return math.ceil(float(value) / TICK) * TICK


def floor_to_tick(value):
    return math.floor(float(value) / TICK) * TICK


def f2(value, empty="-"):
    try:
        return f"{float(value):.2f}"
    except Exception:
        return empty


def pct(value, empty="-"):
    try:
        return f"{float(value):.2f}%"
    except Exception:
        return empty


def limit_rate_for(code, name=""):
    text = f"{code} {name}".upper()
    if "ST" in text:
        return 0.05
    if str(code).startswith(("300", "301", "688")):
        return 0.20
    return 0.10


def limit_prices(code, name, prev_close):
    if not prev_close:
        return None, None
    rate = limit_rate_for(code, name)
    return round_to_tick(prev_close * (1 + rate)), round_to_tick(prev_close * (1 - rate))


def is_trading_day(day):
    return intra.base.is_a_share_trading_day(day.date())


def is_engine_session(day):
    t = day.time()
    return dtime(9, 20) <= t < dtime(15, 10)


def is_realtime_session(day):
    t = day.time()
    return dtime(9, 30) <= t < dtime(11, 31) or dtime(13, 0) <= t < dtime(15, 1)


def is_attack_allowed(day):
    t = day.time()
    return dtime(9, 30) <= t < dtime(11, 31) or dtime(13, 0) <= t < dtime(14, 57)


def time_from_hhmm(value):
    if not value:
        return None
    try:
        hour, minute = str(value).split(":", 1)
        return dtime(int(hour), int(minute[:2]))
    except Exception:
        return None


def global_policy_value(context, key, default):
    try:
        return float((context or {}).get("policy", {}).get(key, default))
    except Exception:
        return float(default)


def is_attack_allowed_by_global(day, context, row=None):
    if not is_attack_allowed(day):
        return False
    # A session-level health flag is not a candidate-level verdict. A
    # technology-only shock is resolved later with the actual candidate.
    if row is not None and not global_risk.core_attack_buy_allowed(context, row=row, now=day):
        return False
    policy = (context or {}).get("policy") or {}
    cutoff = time_from_hhmm(policy.get("disable_attack_buy_before"))
    if cutoff and day.time() < cutoff:
        return False
    return True


def market_opportunity_buy_allowed(context, row=None, now=None):
    return global_risk.market_opportunity_buy_allowed(context, row=row, now=now)


def is_open_noise_window(day):
    return dtime(9, 30) <= day.time() < dtime(9, 35)


def is_open_carry_de_risk_window(day):
    """Allow a second, confirmed opening-risk check after the first noise window."""
    return dtime(9, 35) <= day.time() < dtime(9, 50)


def ensure_dirs():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    WEB_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def log_event(**payload):
    ensure_dirs()
    payload.setdefault("ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    with (LOG_DIR / "realtime_signal_engine.log").open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def load_engine_health_alert_state():
    try:
        return json.loads(ENGINE_HEALTH_ALERT_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_engine_health_alert_state(state):
    ensure_dirs()
    ENGINE_HEALTH_ALERT_STATE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _alert_age_seconds(timestamp, now):
    if not timestamp:
        return None
    try:
        return max(0.0, (now - datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")).total_seconds())
    except (TypeError, ValueError):
        return None


def engine_health_alert_transition(state, *, now, error=None, incident_kind="engine_error"):
    """Return an error/recovery alert decision with persistent anti-spam state."""
    state = dict(state or {})
    trading_date = now.strftime("%Y-%m-%d")
    if state.get("trading_date") != trading_date:
        state = {"trading_date": trading_date}

    if incident_kind == 'data_quality' and error is not None:
        fingerprint = ','.join(sorted(set(str(error).split(','))))
        previous = set(str(state.get('error_fingerprint') or '').split(','))
        new_dependency = bool(set(fingerprint.split(',')) - previous)
        new_episode = not state.get('active') or state.get('incident_kind') != incident_kind
        consecutive = int(state.get('consecutive_errors') or 0) + 1 if state.get('active') and not new_dependency else 1
        state.update(trading_date=trading_date, active=True, incident_kind=incident_kind,
                     error_fingerprint=fingerprint, consecutive_errors=consecutive,
                     consecutive_successes=0, recovery_started_at=None,
                     last_error_at=now.strftime('%Y-%m-%d %H:%M:%S'))
        if new_dependency or new_episode:
            state['error_alert_sent'] = False
        age = _alert_age_seconds(state.get('last_error_alert_at'), now)
        if consecutive >= ENGINE_ERROR_ALERT_MIN_CONSECUTIVE and (not state.get('error_alert_sent') or age is None or age >= 7200):
            state.update(error_alert_sent=True, episode_alert_sent=True,
                         last_error_alert_at=now.strftime('%Y-%m-%d %H:%M:%S'))
            return 'error', state
        return None, state

    if error is None and state.get('active') and state.get('incident_kind') == 'data_quality':
        state['consecutive_successes'] = int(state.get('consecutive_successes') or 0) + 1
        state['recovery_started_at'] = state.get('recovery_started_at') or now.strftime('%Y-%m-%d %H:%M:%S')
        if state['consecutive_successes'] < 3 or _alert_age_seconds(state['recovery_started_at'], now) < 120:
            return None, state

    if error is None:
        was_alerted = bool(state.get("episode_alert_sent", state.get("error_alert_sent")))
        was_active = bool(state.get("active"))
        state.update({
            "trading_date": trading_date,
            "active": False,
            "consecutive_errors": 0,
            "last_success_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        })
        if was_active and was_alerted:
            state["last_recovery_for"] = state.get("error_fingerprint")
            return "recovery", state
        return None, state

    fingerprint = str(error).strip() or "unknown_error"
    same_incident = state.get("active") and (
        state.get("error_fingerprint") == fingerprint
        or incident_kind == "data_quality" and state.get("incident_kind") == "data_quality"
    )
    if same_incident:
        consecutive = int(state.get("consecutive_errors") or 0) + 1
    else:
        consecutive = 1
        state["episode_alert_sent"] = False
        if not (incident_kind == "data_quality" and state.get("incident_kind") == "data_quality"):
            state["error_alert_sent"] = False
            state["last_error_alert_at"] = None
    state.update({
        "trading_date": trading_date,
        "active": True,
        "error_fingerprint": fingerprint,
        "incident_kind": incident_kind,
        "consecutive_errors": consecutive,
        "last_error_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    })
    age = _alert_age_seconds(state.get("last_error_alert_at"), now)
    eligible = (
        consecutive >= ENGINE_ERROR_ALERT_MIN_CONSECUTIVE
        and (not state.get("error_alert_sent") or age is None or age >= ENGINE_ERROR_ALERT_COOLDOWN_SECONDS)
    )
    if eligible:
        state["error_alert_sent"] = True
        state["episode_alert_sent"] = True
        state["last_error_alert_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        return "error", state
    return None, state


def init_db():
    ensure_dirs()
    con = sqlite3.connect(STATE_DB)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS signals (
          trading_date TEXT,
          symbol TEXT,
          scenario TEXT,
          priority TEXT,
          external_status TEXT,
          internal_state TEXT,
          current_price REAL,
          trigger_price REAL,
          add_price REAL,
          reduce_price REAL,
          invalid_price REAL,
          confirm_rule TEXT,
          cancel_rule TEXT,
          suggested_action TEXT,
          reason_json TEXT,
          last_update_ts TEXT,
          last_push_ts TEXT,
          cooldown_until TEXT,
          fingerprint TEXT,
          PRIMARY KEY(trading_date, symbol, scenario)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_events (
          id TEXT PRIMARY KEY,
          trading_date TEXT,
          symbol TEXT,
          scenario TEXT,
          event_type TEXT,
          from_state TEXT,
          to_state TEXT,
          price REAL,
          payload_json TEXT,
          created_at TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS market_decision_events (
          id TEXT PRIMARY KEY,
          trading_date TEXT,
          symbol TEXT,
          stage TEXT,
          reason TEXT,
          payload_json TEXT,
          created_at TEXT
        )
        """
    )
    signal_tracking.ensure_schema(con)
    con.commit()
    return con


def db_row(con, trading_date, symbol, scenario):
    cur = con.execute(
        """
        SELECT internal_state, external_status, priority, last_push_ts, cooldown_until,
               fingerprint, current_price, trigger_price, invalid_price
        FROM signals
        WHERE trading_date=? AND symbol=? AND scenario=?
        """,
        (trading_date, symbol, scenario),
    )
    row = cur.fetchone()
    if not row:
        return {}
    return {
        "internal_state": row[0],
        "external_status": row[1],
        "priority": row[2],
        "last_push_ts": row[3],
        "cooldown_until": row[4],
        "fingerprint": row[5],
        "current_price": row[6],
        "trigger_price": row[7],
        "invalid_price": row[8],
    }


def upsert_signal(con, signal, transition, pushed=False):
    con.execute(
        """
        INSERT INTO signals (
          trading_date, symbol, scenario, priority, external_status, internal_state,
          current_price, trigger_price, add_price, reduce_price, invalid_price,
          confirm_rule, cancel_rule, suggested_action, reason_json,
          last_update_ts, last_push_ts, cooldown_until, fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trading_date, symbol, scenario) DO UPDATE SET
          priority=excluded.priority,
          external_status=excluded.external_status,
          internal_state=excluded.internal_state,
          current_price=excluded.current_price,
          trigger_price=excluded.trigger_price,
          add_price=excluded.add_price,
          reduce_price=excluded.reduce_price,
          invalid_price=excluded.invalid_price,
          confirm_rule=excluded.confirm_rule,
          cancel_rule=excluded.cancel_rule,
          suggested_action=excluded.suggested_action,
          reason_json=excluded.reason_json,
          last_update_ts=excluded.last_update_ts,
          last_push_ts=CASE WHEN ? THEN excluded.last_push_ts ELSE signals.last_push_ts END,
          cooldown_until=CASE WHEN ? THEN excluded.cooldown_until ELSE signals.cooldown_until END,
          fingerprint=excluded.fingerprint
        """,
        (
            signal["trading_date"],
            signal["symbol"],
            signal["scenario"],
            signal["priority"],
            signal["external_status"],
            signal["internal_state"],
            signal["current_price"],
            signal.get("trigger_price"),
            signal.get("add_price"),
            signal.get("reduce_price"),
            signal.get("invalid_price"),
            signal.get("confirm_rule"),
            signal.get("cancel_rule"),
            signal.get("action"),
            json.dumps(signal.get("reasons") or [], ensure_ascii=False),
            signal["updated_at"],
            signal["updated_at"] if pushed else signal.get("last_push_ts"),
            signal.get("cooldown_until"),
            signal["fingerprint"],
            1 if pushed else 0,
            1 if pushed else 0,
        ),
    )
    if transition:
        event_id = f"{signal['updated_at']}:{signal['symbol']}:{signal['scenario']}:{transition['from']}:{transition['to']}"
        payload = {**signal, "transition": transition}
        con.execute(
            "INSERT OR IGNORE INTO signal_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                signal["trading_date"],
                signal["symbol"],
                signal["scenario"],
                transition["event_type"],
                transition["from"],
                transition["to"],
                signal["current_price"],
                json.dumps(payload, ensure_ascii=False),
                signal["updated_at"],
            ),
        )
        with EVENT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    con.commit()


def record_market_decision(con, row, stage, reason, now=None, extra=None):
    """Persist one meaningful candidate state per symbol/day for after-close review.

    The realtime dashboard intentionally shows only the current state.  This
    compact ledger preserves the first candidate, global block, technical
    block, and ready transition so the after-close report can explain a quiet
    trading day without replaying every 20-second quote tick.
    """
    if con is None:
        return
    quote = row.get("quote") or {}
    symbol = str(row.get("code") or quote.get("code") or "").strip()
    if not symbol:
        return
    ts = now or now_dt()
    event_id = f"market:{intra.REPORT_DATE}:{symbol}:{stage}"
    payload = {
        "name": quote.get("name") or row.get("name") or symbol,
        "price": quote.get("close"),
        "radar_status": intra.radar_opportunity_status(row),
        "risk_level": (extra or {}).get("risk_level"),
        **(extra or {}),
    }
    con.execute(
        """
        INSERT OR IGNORE INTO market_decision_events
        (id, trading_date, symbol, stage, reason, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            intra.REPORT_DATE,
            symbol,
            stage,
            str(reason or ""),
            json.dumps(payload, ensure_ascii=False),
            ts.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    con.commit()


def record_v2_decision(con, signal, rating, now=None):
    """Audit timing evidence and the strategy contract that owns an order."""
    timing = signal.get("timing_v2") or {}
    scenario = str(signal.get("scenario") or "")
    row = {
        "code": signal.get("symbol"),
        "name": signal.get("name"),
        "quote": {"code": signal.get("symbol"), "name": signal.get("name"), "close": signal.get("current_price")},
    }
    blockers = list(timing.get("blockers") or signal.get("reasons") or [])
    vetoes = list(rating.get("hard_veto") or [])
    if scenario in intraday_timing_v2.ENTRY_SCENARIOS and timing.get("entry_allowed"):
        strategy = signal.get("strategy_contract") or {}
        label = strategy.get("name") or "三策略合同"
        stage, reason = "STRATEGY_SIGNAL_READY", f"{label}正式首笔条件通过，已进入执行合同"
    elif scenario in intraday_timing_v2.EXIT_SCENARIOS:
        stage, reason = "V2_RISK_ACTION", "V2持仓风险动作已进入执行链路"
    elif any(item.get("code") == "DATA" for item in vetoes):
        stage, reason = "V2_DATA_BLOCKED", "；".join(blockers[:3]) or "V2数据质量未通过"
    elif any(item.get("code") == "SYSTEMIC_RISK" for item in vetoes):
        stage, reason = "V2_MARKET_BLOCKED", "；".join(blockers[:3]) or "V2市场门控阻断"
    else:
        stage, reason = "V2_WAIT", "；".join(blockers[:3]) or "V2多周期确认未完成"
    record_market_decision(
        con,
        row,
        stage,
        reason,
        now=now,
        extra={
            "strategy": signal.get("strategy_family") or "LEGACY_BLOCKED",
            "strategy_name": signal.get("strategy_name"),
            "strategy_entry_pattern": signal.get("strategy_entry_pattern") or timing.get("entry_pattern"),
            "scenario": scenario,
            "grade": rating.get("grade"),
            "score": rating.get("score"),
            "hard_veto": vetoes,
            "soft_gaps": rating.get("soft_gaps") or [],
            "regime": timing.get("regime"),
            "location": timing.get("location"),
            "setup_15m": timing.get("setup_15m"),
            "execution_5m": timing.get("execution_5m"),
            "candidate_source": signal.get("candidate_source"),
        },
    )


def block_new_entries_for_universe(signals, allowed, reason):
    """Fail closed for new buys while keeping exits and reduction actions alive."""
    if allowed:
        return signals
    for signal in signals or []:
        if signal.get("scenario") not in intraday_timing_v2.ENTRY_SCENARIOS:
            continue
        timing = signal.setdefault("timing_v2", {})
        timing["entry_allowed"] = False
        timing.setdefault("blockers", []).append(f"核心股票池门控：{reason}")
        signal.update({
            "scenario": "V2_WAIT",
            "priority": "P2",
            "external_status": "观察",
            "action": "WAIT_UNIVERSE_SYNC",
            "reasons": list(signal.get("reasons") or []) + [f"核心股票池门控：{reason}"],
        })
    return signals


def parse_hms(value):
    try:
        return datetime.strptime(value, "%H:%M:%S").time()
    except Exception:
        return None


def to_float(value):
    try:
        return float(value)
    except Exception:
        return None


def median(values, default=0):
    valid = [float(x) for x in values if x is not None]
    return statistics.median(valid) if valid else default


def minute_features(rows, current_price, current=None):
    points = []
    for row in rows or []:
        price = to_float(row.get("p"))
        volume = to_float(row.get("v")) or 0
        avg_price = to_float(row.get("avg_p"))
        tick_time = parse_hms(row.get("m") or "")
        if price is None or tick_time is None:
            continue
        points.append({"time": tick_time, "price": price, "volume": volume, "avg_price": avg_price})
    if not points:
        eps = max(2 * TICK, current_price * 0.0008)
        return {
            "available": False,
            "vwap": None,
            "epsilon": eps,
            "watch_band": max(5 * TICK, current_price * 0.0025),
            "amount_ratio_1m": 0,
            "amount_ratio_5m": 0,
            "atr1m": 0,
            "atr5m": 0,
            "last3_prices": [],
            "last5_prices": [],
            "quote_delay_sec": None,
        }

    prices = [x["price"] for x in points]
    vols = [x["volume"] for x in points]
    total_vol = sum(vols)
    vwap = next((x["avg_price"] for x in reversed(points) if x["avg_price"]), None)
    if vwap is None and total_vol:
        vwap = sum(x["price"] * x["volume"] for x in points) / total_vol

    diffs = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    atr1 = diffs[-1] if diffs else 0
    atr5 = statistics.mean(diffs[-5:]) if diffs else 0
    # Tencent's last minute is live and incomplete.  Comparing that partial
    # volume with completed bars made the gate fail at the start of nearly
    # every minute and allowed it to change merely because the poll landed a
    # few seconds later.  Execution volume must use completed windows only.
    volume_points = points
    if current is not None and points:
        current_minute = current.replace(second=0, microsecond=0).time()
        volume_points = [point for point in points if point["time"] < current_minute]
    volume_values = [point["volume"] for point in volume_points]
    recent_count = min(5, len(volume_values))
    baseline_values = volume_values[-25:-5] if len(volume_values) > 5 else volume_values[:-1]
    if not baseline_values and len(volume_values) > 1:
        baseline_values = volume_values[:-1]
    med_vol20 = median(baseline_values, default=0)
    last_vol = volume_values[-1] if volume_values else 0
    last5_vol = sum(volume_values[-recent_count:]) if recent_count else 0
    amount_ratio_1m = last_vol / med_vol20 if med_vol20 else 0
    amount_ratio_5m = last5_vol / (med_vol20 * recent_count) if med_vol20 and recent_count else 0
    eps = max(2 * TICK, current_price * 0.0008, 0.15 * atr1, 0.05 * atr5)
    watch_band = max(5 * TICK, current_price * 0.0025, 0.5 * atr5)

    def window_range(n):
        subset = points[-n:] if len(points) >= n else points
        return max(x["price"] for x in subset), min(x["price"] for x in subset)

    or5 = [x["price"] for x in points if dtime(9, 30) <= x["time"] < dtime(9, 35)]
    or15 = [x["price"] for x in points if dtime(9, 30) <= x["time"] < dtime(9, 45)]
    h30, l30 = window_range(30)
    h60, l60 = window_range(60)
    return {
        "available": True,
        "minutes": len(points),
        "vwap": vwap,
        "or5_high": max(or5) if or5 else None,
        "or5_low": min(or5) if or5 else None,
        "or15_high": max(or15) if or15 else None,
        "or15_low": min(or15) if or15 else None,
        "or15_mid": (max(or15) + min(or15)) / 2 if or15 else None,
        "h30": h30,
        "l30": l30,
        "h60": h60,
        "l60": l60,
        "atr1m": atr1,
        "atr5m": atr5,
        "median_volume_20": med_vol20,
        "last_volume": last_vol,
        "epsilon": eps,
        "watch_band": watch_band,
        "amount_ratio_1m": amount_ratio_1m,
        "amount_ratio_5m": amount_ratio_5m,
        "last3_prices": prices[-3:],
        "last5_prices": prices[-5:],
        "last10_prices": prices[-10:],
        "last_price": prices[-1],
        "last_minute_time": points[-1]["time"].strftime("%H:%M:%S"),
        "volume_sample_time": volume_points[-1]["time"].strftime("%H:%M:%S") if volume_points else None,
        "volume_trend": "放量" if amount_ratio_1m >= 1.15 else "缩量/平量",
        "quote_delay_sec": None,
    }


def load_positions():
    path = Path(os.environ.get("A_SHARE_POSITION_FILE") or BASE_DIR / "positions.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = data.values() if isinstance(data, dict) else data
    out = {}
    for row in rows:
        code = str(row.get("code") or row.get("symbol") or "")
        if code:
            out[code] = {
                "quantity": row.get("quantity"),
                "sellable": row.get("sellable"),
                "cost": row.get("cost"),
            }
    return out


def load_paper_position_rows():
    try:
        return [
            pos
            for pos in paper_trading.load_positions(BASE_DIR)
            if int(pos.get("quantity") or 0) > 0
        ]
    except Exception:
        return []


def paper_position_codes(rows=None):
    rows = rows if rows is not None else load_paper_position_rows()
    return {str(pos.get("symbol")) for pos in rows if str(pos.get("symbol") or "").strip()}


def merge_signal_positions(manual_positions, paper_rows):
    """Expose simulated holdings to V2 without hiding explicit position data."""
    return position_registry.merge_positions(manual_positions, paper_rows)


def paper_level_from_position(pos, quote=None):
    code = str(pos.get("symbol") or "").strip()
    name = (quote or {}).get("name") or pos.get("name") or code
    avg_cost = float(pos.get("avg_cost") or pos.get("cost") or 0)
    price = float((quote or {}).get("close") or avg_cost or 0)
    prev_close = float((quote or {}).get("prev_close") or price or avg_cost or 0)
    open_price = float((quote or {}).get("open") or price or prev_close or avg_cost or 0)
    if price <= 0:
        return None
    reference = avg_cost if avg_cost > 0 else prev_close or price
    profit_pct = (price - avg_cost) / avg_cost * 100 if avg_cost > 0 else None
    defense = floor_to_tick(max(price * 0.985, reference * 0.975))
    repair = ceil_to_tick(max(price * 1.006, open_price, prev_close))
    pressure = ceil_to_tick(max(price * 1.025, repair, reference * 1.012))
    return {
        "code": code,
        "name": name,
        "state": "模拟盘持仓实时盯市",
        "priority": "P1",
        "focus": "模拟盘持仓纳入实时订盘；轻量关键位基于成交成本、昨收/开盘与当前价生成",
        "community": "模拟盘持仓；社区温度不参与触发",
        "defense": defense,
        "repair": repair,
        "pressure": pressure,
        "paper_position": True,
        "paper_avg_cost": avg_cost,
        "paper_quantity": int(pos.get("quantity") or 0),
        "paper_sellable": int(pos.get("sellable") or 0),
        "paper_unrealized_pnl_pct": profit_pct,
        "premarket_advice": (
            f"模拟成本 {f2(avg_cost)}；跌破轻量防守 {f2(defense)} 按模拟盘风险处理，"
            f"站回修复 {f2(repair)} 才恢复进攻观察。"
        ),
    }


def extend_levels_with_paper_positions(levels, paper_rows, quotes=None):
    merged = dict(levels or {})
    added = []
    for pos in paper_rows or []:
        code = str(pos.get("symbol") or "").strip()
        if not code or code in merged:
            continue
        row = paper_level_from_position(pos, (quotes or {}).get(code))
        if row:
            merged[code] = row
            added.append(code)
    return merged, added


def parse_tencent_quote_text(text):
    quotes = {}
    for line in (text or "").strip().splitlines():
        if not line or '="' not in line:
            continue
        source_symbol = line.split("=", 1)[0].strip()
        if source_symbol.startswith("v_"):
            source_symbol = source_symbol[2:]
        body = line.split('"', 1)[1].rsplit('"', 1)[0]
        parts = body.split("~")
        if len(parts) < 40:
            continue
        code = parts[2]
        try:
            # Tencent uses the same six-digit code for the Shanghai Composite
            # (sh000001) and Ping An Bank (sz000001).  Keep benchmark indexes
            # under exchange-qualified keys so a mixed response can never let
            # an ordinary stock overwrite the market regime input.
            key = source_symbol if source_symbol in {"sh000001", "sz399001", "sz399006"} else code
            quotes[key] = {
                "name": parts[1],
                "code": code,
                "source_symbol": source_symbol,
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
    return quotes


def load_quote_cache():
    try:
        data = json.loads(QUOTE_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_quote_cache(cache_quotes):
    try:
        QUOTE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        QUOTE_CACHE.write_text(json.dumps(cache_quotes or {}, ensure_ascii=False), encoding="utf-8")
        from core import closed_liquidity
        closed_liquidity.archive_completed_quotes(BASE_DIR, cache_quotes or {}, now_dt())
    except Exception:
        pass


def load_market_profile_cache():
    try:
        payload = json.loads(MARKET_PROFILE_CACHE.read_text(encoding="utf-8"))
        profiles = payload.get("profiles") if isinstance(payload, dict) else None
        return profiles if isinstance(profiles, dict) else {}
    except Exception:
        return {}


def write_market_profile_cache(profiles):
    try:
        MARKET_PROFILE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        MARKET_PROFILE_CACHE.write_text(json.dumps({
            "updated_at": now_dt().strftime("%Y-%m-%d %H:%M:%S"),
            "profiles": profiles or {},
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def fetch_quotes_for_codes(codes, cache=None, timeout=4):
    universe = [(str(code), intra.base.infer_market(str(code))) for code in dict.fromkeys(codes or []) if str(code or "").strip()]
    if not universe:
        return [], {}
    index_codes = ["sh000001", "sz399001", "sz399006"]
    query = ",".join(
        [intra.base.stock_symbol(code) for code, _ in universe] + index_codes
    )
    fetched = {}
    error = None
    try:
        req = urllib.request.Request(
            f"https://qt.gtimg.cn/q={query}",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://stockpage.10jqka.com.cn/"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            fetched = parse_tencent_quote_text(resp.read().decode("gbk", "ignore"))
    except Exception as exc:
        error = str(exc)
    # Tencent may truncate a long mixed stock/index response. Refresh the
    # three A-share benchmarks separately so the local market gate never
    # mistakes a transport omission for a neutral market.
    if any(code not in fetched for code in index_codes):
        try:
            req = urllib.request.Request(
                "https://qt.gtimg.cn/q=sh000001,sz399001,sz399006",
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://stockpage.10jqka.com.cn/"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                fetched.update(parse_tencent_quote_text(resp.read().decode("gbk", "ignore")))
        except Exception as exc:
            error = error or str(exc)

    memory_cache = (cache or {}).setdefault("quotes", {})
    disk_cache = load_quote_cache()
    now_text = now_dt().strftime("%Y-%m-%d %H:%M:%S")
    if fetched:
        for code, quote in fetched.items():
            quote["_cached_at"] = now_text
            quote["_stale"] = False
            memory_cache[code] = quote
            disk_cache[code] = quote
        write_quote_cache(disk_cache)

    out = {}
    for code in [item[0] for item in universe] + index_codes:
        quote = fetched.get(code) or memory_cache.get(code) or disk_cache.get(code)
        if quote:
            quote = dict(quote)
            if code not in fetched:
                quote["_stale"] = True
            out[code] = quote
    if error:
        (cache or {})["quote_error"] = error
    else:
        (cache or {})["quote_error"] = None
    return universe, out


def build_a_share_market_regime(quotes, cache=None, now=None, breadth=None):
    """Classify the live A-share index regime used by new-buy gates."""
    cache = cache if isinstance(cache, dict) else {}
    current = now or now_dt()
    index_specs = {
        "shanghai": ("sh000001", "上证"),
        "shenzhen": ("sz399001", "深成"),
        "growth": ("sz399006", "创业板"),
    }
    values = {}
    stale = False
    for key, (code, label) in index_specs.items():
        quote = (quotes or {}).get(code) or {}
        # Compatibility for historical fixtures/caches only.  A fallback is
        # accepted when it is not visibly a same-code ordinary stock.
        if not quote:
            legacy_code = code[-6:]
            legacy = (quotes or {}).get(legacy_code) or {}
            legacy_name = str(legacy.get("name") or "")
            if legacy and legacy_name not in {"平安银行"}:
                quote = legacy
        try:
            values[key] = {"label": label, "pct": float(quote.get("pct")), "stale": bool(quote.get("_stale"))}
            stale = stale or bool(quote.get("_stale"))
        except Exception:
            values[key] = {"label": label, "pct": None, "stale": True}
            stale = True
    samples = cache.setdefault("a_share_market_samples", [])
    if any(item.get("pct") is not None for item in values.values()):
        samples.append({
            "timestamp": current.strftime("%Y-%m-%d %H:%M:%S"),
            "values": {key: item.get("pct") for key, item in values.items()},
        })
        cache["a_share_market_samples"] = samples[-240:]
    previous = (cache.get("a_share_market_samples") or [])[-2:-1]
    previous_values = (previous[0].get("values") if previous else {}) or {}
    shanghai = values["shanghai"].get("pct")
    shenzhen = values["shenzhen"].get("pct")
    growth = values["growth"].get("pct")
    state = "neutral"
    if growth is None or shenzhen is None or shanghai is None:
        state = "data_stale"
    elif growth <= -2.0 or (growth <= -1.2 and shenzhen <= -0.8):
        state = "risk_off"
    elif (growth <= -0.8 and shenzhen <= -0.5) or (growth <= -0.5 and shanghai <= -0.8 and shenzhen <= -0.3):
        state = "transition"
    elif growth >= 0.8 and shenzhen >= 0.5:
        state = "growth_lead"
    elif shanghai <= -0.5 and (growth >= 0.3 or shenzhen >= 0.3):
        state = "rotation"
    elif (
        isinstance(previous_values.get("growth"), (int, float))
        and growth - float(previous_values["growth"]) >= 0.8
        and growth >= -1.0
    ):
        state = "recovery"
    breadth = breadth or cache.get("market_breadth") or {}
    if state == "neutral" and breadth.get("state") == "broad_strong":
        # Indexes can be modestly positive while the actual opportunity set is
        # very broad.  Keep this distinct from a growth-led impulse: it opens
        # no automatic buy, but accurately preserves the market envelope for
        # plan sizing and post-trade review.
        state = "broad_strong"
    elif state in {"neutral", "rotation", "recovery"} and breadth.get("state") in {"shrinking_weak", "broad_weak"}:
        # A few indices can look calm while thousands of names are falling on
        # shrinking turnover.  Treat this as a defensive rotation, not a green
        # light for generic radar buys.
        state = "rotation_defensive"
    if stale and state == "neutral":
        state = "data_stale"
    labels = {
        "neutral": "A股指数中性",
        "transition": "A股成长风格转弱",
        "risk_off": "A股盘中风险偏好转弱",
        "growth_lead": "A股成长风格领涨",
        "broad_strong": "A股广度强势",
        "rotation": "A股结构性轮动",
        "recovery": "A股指数出现修复",
        "rotation_defensive": "A股弱广度防守",
        "data_stale": "A股指数数据缺失或滞后",
    }
    actions = {
        "neutral": "新增机会可继续等待个股量价确认",
        "transition": "科技链暂缓新增；非科技仅允许板块情绪与个股量能同步的轮动机会",
        "risk_off": "禁止新增科技链机会；非科技仅允许板块情绪与量能同步、三周期不破的轮动试错",
        "growth_lead": "成长板块领涨，只允许通过量价、盈亏比与仓位纪律的首笔试错",
        "broad_strong": "全市场广度强，但仍只允许盘前计划内标的通过V2三周期与量价确认后首笔试错",
        "rotation": "权重偏弱但成长未转弱，只允许强势方向通过量价确认后小仓试错",
        "recovery": "只允许已有观察候选重新通过量价确认，不追第一根反弹",
        "rotation_defensive": "全市场跌多涨少：仅在全局许可下评估持续共振板块；量能独立确认，不追第一波，不连续加仓",
        "data_stale": "指数数据未完成刷新，新增机会只观察",
    }
    return {
        "version": "a_share_market_regime_v2_breadth",
        "updated_at": current.strftime("%Y-%m-%d %H:%M:%S"),
        "state": state,
        "label": labels[state],
        "action": actions[state],
        "stale": stale,
        "indexes": values,
        "breadth": breadth,
        "samples": len(cache.get("a_share_market_samples") or []),
    }


def core_entry_scenario(row, add_scenario):
    """Keep a first paper entry distinct from an add to an existing position."""
    try:
        has_paper_position = int(row.get("paper_quantity") or 0) > 0
    except (TypeError, ValueError):
        has_paper_position = bool(row.get("paper_position"))
    return add_scenario if has_paper_position else "CORE_INITIAL_ENTRY"


def stock_symbol_label(row):
    q = row["quote"]
    return f"{q['name']}({row['code']})"


def preopen_quality_entry_gate(current=None):
    """Apply only critical pre-open failures globally; V2 handles quote gaps per code."""
    current = current or now_dt()
    if os.environ.get("A_SHARE_ENFORCE_PREOPEN_QUALITY", "0") != "1":
        return {"allowed": True, "reason": "盘前质检全局门控未启用"}
    if current.time() < dtime(9, 25):
        return {"allowed": True, "reason": "开盘前等待09:00质检结果"}
    try:
        payload = json.loads(PREOPEN_QUALITY_LATEST.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"allowed": False, "reason": f"未读取到当日盘前质检：{exc}"}
    if str(payload.get("trading_date") or "") != current.strftime("%Y-%m-%d"):
        return {"allowed": False, "reason": "当日盘前质检尚未生成"}
    if payload.get("entry_blocked"):
        names = [str(item.get("name") or "") for item in payload.get("entry_blockers") or []]
        return {"allowed": False, "reason": "盘前关键依赖未通过：" + "、".join(names[:4])}
    return {"allowed": True, "reason": "盘前关键执行依赖通过"}


def timing_v2_for_row(row, minute_rows, current=None, position=None, global_context=None):
    current = current or now_dt()
    history, market_data = local_market_data.history_120m(BASE_DIR, row["code"], current)
    history_15m, setup_data = local_market_data.history_15m(BASE_DIR, row["code"], current)
    # Keep 15m data for position exits, but daily-anchor entries do not depend
    # on a historical 15m setup. Missing 120m structure remains a hard veto.
    needs_15m = (row.get('strategy_contract') or {}).get('key') not in {'TREND_520', 'TREND_MA5'}
    market_data = {
        **(market_data or {}),
        "setup_15m": setup_data,
        "setup_15m_required_for_entry": needs_15m,
        "entry_ready": bool((market_data or {}).get("entry_ready") and (not needs_15m or setup_data.get("entry_ready"))),
        "blockers": list((market_data or {}).get("blockers") or []) + (list(setup_data.get("blockers") or []) if needs_15m else []),
    }
    market_allowed, market_stage, market_reason = global_risk.core_attack_gate_status(
        global_context,
        row=row,
        now=current,
    )
    market_data["market_gate"] = {
        "allowed": market_allowed,
        "stage": market_stage,
        "reason": market_reason or "市场与板块门控通过",
    }
    market_data["a_share_market_regime"] = dict((global_context or {}).get("a_share_market_regime") or {})
    market_data["market_breadth"] = dict((global_context or {}).get("market_breadth") or {})
    quality_gate = preopen_quality_entry_gate(current)
    market_data["preopen_quality_gate"] = quality_gate
    if not quality_gate["allowed"]:
        market_data["entry_ready"] = False
        market_data["blockers"].append(f"盘前质检门控：{quality_gate['reason']}")
    return intraday_timing_v2.evaluate(
        row,
        minute_rows,
        now=current,
        history_120m=history,
        history_15m=history_15m,
        market_data=market_data,
        position=position,
    )


def build_features(levels, quotes, minute_cache, boards=None, current=None, positions=None, global_context=None, board_state=None):
    from core import strategy_gap_research
    try:
        research_cohort = strategy_gap_research.leader_cohort(levels, quotes, current or now_dt())
    except Exception as exc:
        research_cohort = {'capture_error': type(exc).__name__, 'execution_authorized': False}
    rows = intra.build_rows(levels, quotes, {
        code: intra.minline_stats(minute_cache.get(code, [])) for code in levels
    })
    for row in rows:
        q = row["quote"]
        source_level = (levels or {}).get(row["code"]) or {}
        q["industry"] = q.get("industry") or source_level.get("industry") or ""
        q["concepts"] = q.get("concepts") or source_level.get("concepts") or ""
        q["framework_theme_labels"] = list(source_level.get("framework_theme_labels") or [])
        q["theme_evidence"] = theme_validation.candidate_theme_evidence(q, boards)
        for key in (
            "paper_position",
            "paper_avg_cost",
            "paper_quantity",
            "paper_sellable",
            "paper_unrealized_pnl_pct",
        ):
            if key in source_level:
                row[key] = source_level[key]
        for key in (
            "plan_universe",
            "observation_plan_group",
            "strategy_key",
            "strategy_name",
            "strategy_style",
            "strategy_allowed_patterns",
            "strategy_entry_rule",
            "strategy_exit_rule",
            "strategy_reason",
            "strategy_daily_qualified",
            "strategy_daily_evidence",
            "strategy_leader_profile",
            "strategy_leader_cross_verified",
            "strategy_prior_day_touched_limit",
            "strategy_prior_day_closed_limit",
        ):
            if key in source_level:
                row[key] = source_level[key]
        row["strategy_contract"] = observation_strategy_router.contract_from_level(source_level)
        row['gap_leader_cohort'] = research_cohort
        alternate_ids = {r.get('id') for r in
            (row['strategy_contract'].get('daily_metrics') or {}).get('mainline_research_candidates') or []}
        row['gap_mainline_evidence'] = [
            {'id': b.get('f12'), 'name': b.get('f14'), 'pct': b.get('f3'),
             'source_time': b.get('f124'), 'observed_at': (current or now_dt()).isoformat(),
             'rotation': (board_state or {}).get(b.get('f14')) or {},
             'execution_authorized': False, 'status': 'RESEARCH_OBSERVATION_NOT_AUTHORIZATION'}
            for b in boards or [] if b.get('f12') in alternate_ids]
        row["rt_features"] = minute_features(minute_cache.get(row["code"], []), q["close"], current=current)
        row["sector_momentum"] = intra.sector_momentum_for_candidate(
            q,
            boards=boards,
            focus=row.get("focus") or row.get("premarket_advice") or "",
            contract=row.get('strategy_contract'),
            rotations=board_state,
        )
        row["sector_rotation"] = (board_state or {}).get(
            str((row.get("sector_momentum") or {}).get("board_name") or "")
        ) or {"available": False, "sustained": False, "leader_healthy": False, "reason": "未取得板块持续性样本"}
        row["risk_bucket"] = global_risk.candidate_risk_bucket(row)
        row["timing_v2"] = timing_v2_for_row(
            row,
            minute_cache.get(row["code"], []),
            current=current,
            position=(positions or {}).get(row["code"]),
            global_context=global_context,
        )
    return rows


def quote_delay(quotes):
    times = []
    for q in quotes.values():
        raw = str(q.get("datetime") or "")
        if len(raw) >= 14:
            try:
                t = datetime.strptime(raw[:14], "%Y%m%d%H%M%S")
                times.append((now_dt() - t).total_seconds())
            except Exception:
                pass
    return min(times) if times else None


def minute_bar_health(features, now=None):
    """Return freshness/coverage of the minute bars used by the gates."""
    now = now or now_dt()
    latest = []
    for feat in features or []:
        if not isinstance(feat, dict) or not feat.get("available"):
            continue
        last = parse_hms(feat.get("last_minute_time") or "")
        if last is None:
            continue
        point = datetime.combine(now.date(), last)
        if point > now + timedelta(minutes=1):
            continue
        latest.append(point)
    if not latest:
        return {"available": False, "symbols": 0, "latest_time": None, "delay_sec": None}
    newest = max(latest)
    return {
        "available": True,
        "symbols": len(latest),
        "latest_time": newest.strftime("%Y-%m-%d %H:%M:%S"),
        "delay_sec": max(0.0, round((now - newest).total_seconds(), 2)),
    }


def minute_execution_freshness(feat, now=None):
    """Check whether a minute feature is fresh enough to support a new order."""
    now = now or now_dt()
    if not isinstance(feat, dict) or not feat.get("available"):
        return False, "分钟线不可用"
    last = parse_hms(feat.get("last_minute_time") or "")
    if last is None:
        return False, "分钟线最新时间缺失"
    point = datetime.combine(now.date(), last)
    delay = max(0.0, (now - point).total_seconds())
    if delay > MINUTE_EXECUTION_MAX_DELAY_SECONDS:
        return False, f"分钟线已滞后 {delay / 60:.1f} 分钟，禁止用旧VWAP/量能开仓"
    return True, f"分钟线最新 {point:%H:%M:%S}，延迟 {delay:.0f} 秒"


def line_distance(price, trigger):
    if price is None or trigger in (None, 0):
        return None
    return (price - trigger) / trigger * 100


def text_blob(row):
    return " ".join(str(row.get(k) or "") for k in ("state", "priority", "focus", "community", "premarket_advice"))


def weak_daily_structure(row):
    text = text_blob(row)
    weak_words = ("弱趋势", "弱修复", "高位急跌", "回避", "风险优先", "未站回修复")
    return any(word in text for word in weak_words)


def opening_hard_risk_decision(row, feat, price, hard_line, eps):
    """Separate first-minutes auction noise from confirmed structural breaks."""
    q = row["quote"]
    now = now_dt()
    if not is_open_noise_window(now):
        return True, [], []

    open_price = q.get("open") or price
    vwap = feat.get("vwap")
    or5_low = feat.get("or5_low")
    or15_low = feat.get("or15_low")
    hard_trigger = hard_line - eps
    amount_ratio_1m = feat.get("amount_ratio_1m") or 0
    amount_ratio_5m = feat.get("amount_ratio_5m") or 0
    recent_prices = feat.get("last3_prices") or []
    recent_below_hard = sum(1 for p in recent_prices if p <= hard_trigger)
    low_lines = [x for x in (or5_low, or15_low, open_price) if x]
    broke_open_structure = any(price <= line - eps for line in low_lines)
    volume_confirmed = amount_ratio_1m >= 1.2 or amount_ratio_5m >= 1.2
    sample_confirmed = recent_below_hard >= 2
    quick_repair = (
        (vwap and price >= vwap - eps)
        or (open_price and price >= open_price - eps)
        or recent_count_above(feat, hard_line, eps, n=3) >= 1
    )
    reasons = [
        "开盘前5分钟执行硬风控保护",
        f"硬防守 {f2(hard_line)}，当前 {f2(price)}",
        f"OR5低点 {f2(or5_low)}，开盘价 {f2(open_price)}，VWAP {f2(vwap)}",
        f"1m/5m量能比 {amount_ratio_1m:.2f}/{amount_ratio_5m:.2f}",
    ]
    blockers = []
    if not broke_open_structure:
        blockers.append("未跌破OR5/开盘结构，可能是开盘噪声")
    if not volume_confirmed and not sample_confirmed:
        blockers.append("缺少放量或连续采样确认")
    if quick_repair:
        blockers.append("价格仍有快速修复迹象")

    confirmed = broke_open_structure and (volume_confirmed or sample_confirmed) and not quick_repair
    return confirmed, reasons, blockers


def hard_risk_confirmation(row, feat, price, hard_line, eps):
    """Require structure/volume confirmation before a non-gap hard-risk P0."""
    if is_open_noise_window(now_dt()):
        return opening_hard_risk_decision(row, feat, price, hard_line, eps)

    prices = []
    for value in feat.get("last3_prices") or []:
        try:
            prices.append(float(value))
        except Exception:
            pass
    below_count = sum(1 for value in prices[-3:] if value <= hard_line - eps)
    amount_1m = float(feat.get("amount_ratio_1m") or 0)
    amount_5m = float(feat.get("amount_ratio_5m") or 0)
    volume_confirmed = amount_1m >= 1.50 or amount_5m >= 1.30
    atr_buffer = max(2 * eps, 0.25 * float(feat.get("atr1m") or 0))
    deep_break = price <= hard_line - atr_buffer
    structural_lines = [feat.get("or15_low"), feat.get("l30"), feat.get("l60")]
    structural_break = any(value and price <= float(value) - eps for value in structural_lines)
    quick_repair = recent_count_above(feat, hard_line, eps, n=3) >= 1
    confirmed = deep_break and (below_count >= 2 or (volume_confirmed and structural_break)) and not quick_repair
    reasons = [
        f"硬防守 {f2(hard_line)}，当前 {f2(price)}",
        f"最近3次低于风险线 {below_count}/3",
        f"1m/5m量能比 {amount_1m:.2f}/{amount_5m:.2f}",
    ]
    blockers = []
    if not deep_break:
        blockers.append("尚未形成超过ATR噪声缓冲的有效破位")
    if below_count < 2 and not (volume_confirmed and structural_break):
        blockers.append("缺少连续采样或放量叠加结构破坏确认")
    if quick_repair:
        blockers.append("风险线下仍有快速修复迹象")
    return confirmed, reasons, blockers


def p1_context_check(row, feat, price):
    q = row["quote"]
    eps = feat.get("epsilon") or max(2 * TICK, price * 0.0008)
    vwap = feat.get("vwap")
    if weak_daily_structure(row):
        return False, "日线/盘前状态仍是弱修复或风险观察，VWAP只能恢复观察，不能直接加仓"
    if q.get("pct", 0) < 0:
        return False, "个股仍为绿盘，进攻信号降级为观察"
    if vwap and price < vwap + eps:
        return False, "未有效站上VWAP+buffer"
    if feat.get("or15_mid") and price < feat["or15_mid"]:
        return False, "仍在OR15中轴下方，日内结构不支持进攻"
    if feat.get("amount_ratio_5m", 0) < 1.0:
        return False, "5分钟量能没有持续承接"
    return True, "通过日线结构、VWAP、OR15和量能门控"


def recent_count_above(feat, line, eps, n=3):
    prices = feat.get("last5_prices") or feat.get("last3_prices") or []
    recent = prices[-n:]
    return sum(1 for p in recent if p >= line + eps)


def recent_touched(feat, line, eps, n=5):
    prices = (feat.get("last10_prices") or feat.get("last5_prices") or [])[-n:]
    return any(line - eps <= p <= line + eps for p in prices)


def recent_left_line(feat, line, eps, price, n=10):
    prices = (feat.get("last10_prices") or feat.get("last5_prices") or [])[-n:]
    if not prices:
        return False
    away = max(0.5 * (feat.get("atr5m") or 0), price * 0.0025, 5 * TICK)
    return max(prices) >= line + max(eps, away)


def recent_reclaiming(feat, line, eps):
    prices = feat.get("last3_prices") or []
    if len(prices) < 3:
        return False
    return prices[-1] >= line + eps and prices[-1] >= prices[-2] >= prices[-3] - eps


def rapid_rise(feat, price):
    prices = feat.get("last3_prices") or []
    if len(prices) < 3:
        return False
    move = (prices[-1] - prices[0]) / max(prices[0], TICK)
    threshold = max(0.008, ((feat.get("atr1m") or 0) * 1.2) / max(price, TICK))
    return move >= threshold and feat.get("amount_ratio_1m", 0) >= 3.0


def p1_execution_band(line, feat, price, mode):
    atr5 = feat.get("atr5m") or 0
    if mode == "repair":
        width = max(0.6 * atr5, price * 0.005, 5 * TICK)
    else:
        width = max(0.35 * atr5, price * 0.0025, 5 * TICK)
    return ceil_to_tick(line), ceil_to_tick(line + width)


def level_float(value):
    try:
        value = float(value)
        return value if value > 0 else None
    except Exception:
        return None


def nearest_signal_pressure(price, candidates):
    price = level_float(price)
    if price is None:
        return None
    valid = []
    for value in candidates:
        level = level_float(value)
        if level is not None and level > price:
            valid.append(level)
    return min(valid) if valid else None


def framework_style_for_row(row):
    strategy = row.get("strategy_contract") or {}
    if strategy.get("is_observation_strategy"):
        style = str(strategy.get("style") or "")
        name = str(strategy.get("name") or "待分类观察")
        if style in {"情绪票", "趋势票"}:
            return style, f"盘前已归类为{name}；盘中只接受该方法允许的V2形态"
        return "未分类", f"盘前策略为{name}，未取得新增仓方法资格"
    q = row.get("quote") or {}
    text = " ".join(
        str(x or "")
        for x in (
            row.get("state"),
            row.get("focus"),
            row.get("community"),
            row.get("premarket_advice"),
            row.get("candidate_source"),
            row.get("tracked_status"),
        )
    )
    emotion_hit = any(word in text for word in ("涨停", "连板", "情绪", "高开", "炸板", "一致亢奋", "题材热", "游资", "接力"))
    # “框架筛选” only says how the name entered the pool.  It is not proof
    # of a completed monthly/daily/60m trend assessment.
    trend_hit = any(word in text for word in ("强趋势", "趋势", "修复", "回踩", "多头", "机构"))
    pct_value = level_float(q.get("pct")) or 0
    amount = intra.amount_yi(q)
    if emotion_hit and pct_value >= 5:
        return "情绪/高斜率", "高涨幅或情绪特征明显，严格执行不追高和快速失效"
    if emotion_hit and trend_hit:
        return "题材趋势混合", "题材热度与趋势修复并存，需价格、量能、空间共同确认"
    if trend_hit:
        return "趋势/波段", "以趋势延续、缩量回踩和放量修复为主，不用分时噪音替代结构"
    if amount >= 50:
        return "高辨识度待分类", "成交额有辨识度但风格未完全明确，盘后复盘必须补充分类"
    return "未分类", "信号缺少明确趋势/情绪风格标签"


def signal_pressure_for_row(row, feat, q):
    dyn = row.get("dynamic") or {}
    return nearest_signal_pressure(
        q.get("close"),
        [
            row.get("pressure"),
            dyn.get("trend_pressure"),
            dyn.get("first_bounce"),
            feat.get("h60"),
            feat.get("h30"),
            feat.get("or15_high"),
            feat.get("or5_high"),
            q.get("high"),
            q.get("limit_up"),
        ],
    )


def fingerprint(signal):
    parts = [
        signal["trading_date"],
        signal["symbol"],
        signal["scenario"],
        signal["external_status"],
        signal["priority"],
        f2(signal.get("trigger_price")),
        f2(signal.get("invalid_price")),
    ]
    return "|".join(parts)


def make_signal(row, scenario, priority, external, internal, trigger, add_price, reduce_price, invalid, confirm, cancel, action, reasons, extra=None):
    now = now_dt().strftime("%Y-%m-%d %H:%M:%S")
    q = row["quote"]
    feat = row.get("rt_features") or {}
    dyn = row.get("dynamic") or {}
    minute_fresh, minute_fresh_reason = minute_execution_freshness(feat)
    style, style_reason = framework_style_for_row(row)
    pressure_price = signal_pressure_for_row(row, feat, q)
    signal = {
        "version": VERSION,
        "trading_date": intra.REPORT_DATE,
        "symbol": row["code"],
        "name": q["name"],
        "priority": priority,
        "external_status": external,
        "internal_state": internal,
        "scenario": scenario,
        "current_price": q["close"],
        "pct": q.get("pct"),
        "prev_close": q.get("prev_close"),
        "trigger_price": trigger,
        "add_price": add_price,
        "reduce_price": reduce_price,
        "invalid_price": invalid,
        "confirm_rule": confirm,
        "cancel_rule": cancel,
        "action": action,
        "reasons": reasons,
        "vwap": feat.get("vwap"),
        "epsilon": feat.get("epsilon"),
        "watch_band": feat.get("watch_band"),
        "amount_ratio_1m": feat.get("amount_ratio_1m"),
        "amount_ratio_5m": feat.get("amount_ratio_5m"),
        "minute_data_fresh": minute_fresh,
        "minute_data_fresh_reason": minute_fresh_reason,
        "last_volume": feat.get("last_volume"),
        "median_volume_20": feat.get("median_volume_20"),
        "atr1m": feat.get("atr1m"),
        "atr5m": feat.get("atr5m"),
        "or5_high": feat.get("or5_high"),
        "or5_low": feat.get("or5_low"),
        "or15_high": feat.get("or15_high"),
        "or15_low": feat.get("or15_low"),
        "or15_mid": feat.get("or15_mid"),
        "h30": feat.get("h30"),
        "l30": feat.get("l30"),
        "h60": feat.get("h60"),
        "l60": feat.get("l60"),
        "last3_prices": feat.get("last3_prices"),
        "pressure_price": pressure_price,
        "trend_pressure": dyn.get("trend_pressure") or row.get("pressure"),
        "defense_price": row.get("defense"),
        "repair_price": row.get("repair"),
        "row_state": row.get("state"),
        "focus": row.get("focus"),
        "community": row.get("community"),
        "framework_trade_style": style,
        "framework_trade_style_reason": style_reason,
        "cooldown_until": None,
        "updated_at": now,
    }
    up, down = limit_prices(row["code"], q["name"], q.get("prev_close"))
    signal["limit_up"] = up
    signal["limit_down"] = down
    if extra:
        signal.update(extra)
    timing_v2 = row.get("timing_v2")
    if isinstance(timing_v2, dict):
        signal["timing_v2"] = timing_v2
        signal["timing_v2_version"] = timing_v2.get("version")
        signal["timing_v2_config_hash"] = timing_v2.get("config_hash")
    if scenario in intraday_timing_v2.BUY_SCENARIOS and isinstance(timing_v2, dict) and not timing_v2.get("entry_allowed"):
        gate_reason = intraday_timing_v2.gate_reason(timing_v2)
        signal.update({
            "priority": "P2",
            "external_status": "观察",
            "internal_state": "WAIT_MULTI_PERIOD",
            "action": f"三策略短周期等待：{gate_reason}",
            "sim_reason": f"三策略时机门控未通过：{gate_reason}",
            "timing_v2_gate_blocked": True,
        })
        signal["reasons"] = list(signal.get("reasons") or []) + [f"三策略时机门控：{gate_reason}"]
    signal["distance_pct"] = line_distance(signal["current_price"], trigger)
    rating = opportunity_rating.rate_signal(signal)
    signal.update({
        "opportunity_rating": rating,
        "opportunity_score": rating.get("score"),
        "opportunity_grade": rating.get("grade"),
        "opportunity_hard_veto": rating.get("hard_veto"),
    })
    if (
        scenario in intraday_timing_v2.BUY_SCENARIOS
        and signal.get("rotation_pilot")
        and (rating.get("grade") not in {"A", "B"} or rating.get("hard_vetoed"))
    ):
        signal.update({
            "external_status": "观察",
            "internal_state": "QUALIFIED",
            "sim_reason": "盘中动态候选评级未达到A/B或存在硬否决",
            "action": "V2形态成立但动态预算未授权，继续跟踪评级缺口。",
        })
        signal["reasons"] = list(signal.get("reasons") or []) + [signal["sim_reason"]]
    signal["fingerprint"] = fingerprint(signal)
    signal.update(attach_contract(signal))
    contract = signal.get("signal_contract") or {}
    if (
        scenario in intraday_timing_v2.BUY_SCENARIOS
        and signal.get("external_status") == "立即处理"
        and not contract.get("sim_allowed")
    ):
        signal.update({
            "external_status": "观察",
            "internal_state": "NEAR_TRIGGER",
            "action": "三策略触发但成本后执行契约未授权，继续观察，不生成订单。",
            "sim_reason": contract.get("sim_reason") or "执行契约未授权",
        })
        signal["reasons"] = list(signal.get("reasons") or []) + [signal["sim_reason"]]
        signal["fingerprint"] = fingerprint(signal)
        signal.update(attach_contract(signal))
    return signal


def transition_for(previous, signal):
    old_internal = previous.get("internal_state") or "IDLE"
    old_external = previous.get("external_status") or "观察"
    old_priority = previous.get("priority") or "P3"
    new_internal = signal["internal_state"]
    new_external = signal["external_status"]
    new_priority = signal["priority"]
    priority_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    changed = old_internal != new_internal or old_external != new_external
    upgraded = priority_rank.get(new_priority, 9) < priority_rank.get(old_priority, 9)
    if changed or upgraded:
        return {
            "from": old_internal,
            "to": new_internal,
            "event_type": "priority_upgrade" if upgraded and not changed else "state_change",
        }
    return None


def cooldown_seconds(signal):
    if signal["priority"] == "P0":
        return 90
    if signal["priority"] == "P1":
        return 300
    if signal["priority"] == "P2":
        return 900
    return 999999


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def cooldown_active(previous):
    cooldown_until = parse_dt(previous.get("cooldown_until"))
    return bool(cooldown_until and cooldown_until > now_dt())


def material_p0_worsening(previous, signal):
    if signal["scenario"] == "HIGH_OPEN_ATTACK_CANCEL":
        return False
    last_push = parse_dt(previous.get("last_push_ts"))
    if not last_push:
        return True
    if (now_dt() - last_push).total_seconds() < P0_REPEAT_SECONDS:
        return False
    price = signal.get("current_price")
    trigger = signal.get("trigger_price")
    if price is None or trigger is None:
        return False
    eps = signal.get("epsilon") or max(2 * TICK, price * 0.0008)
    worsen_gap = max(5 * TICK, price * P0_WORSEN_PCT, 5 * eps)
    return price <= trigger - worsen_gap


def should_push(previous, signal, transition):
    paper = signal.get("paper_trade") or {}
    if signal.get("no_push_reason"):
        return False
    if paper.get("status") == "FILLED_ALREADY":
        return False
    if signal["external_status"] not in ("立即处理", "接近触发", "取消"):
        return False
    if signal["priority"] == "P2" and signal["external_status"] == "接近触发":
        return transition is not None
    if transition is not None:
        return True
    if cooldown_active(previous):
        return False
    if signal["priority"] == "P0":
        return material_p0_worsening(previous, signal)
    return False


def formal_entry_retriggered(tracking_events):
    return any(
        item.get("to") == "TRIGGERED" and item.get("from") != "TRIGGERED"
        for item in (tracking_events or [])
    )


def with_cooldown(signal):
    until = now_dt() + timedelta(seconds=cooldown_seconds(signal))
    signal["cooldown_until"] = until.strftime("%Y-%m-%d %H:%M:%S")
    return signal


def profit_guard_trend_damage(row, quote, feat, price, vwap, eps):
    """Confirm low-profit erosion with actual intraday structure damage."""
    def as_float(value):
        try:
            return float(value)
        except Exception:
            return None

    price = as_float(price)
    if price is None:
        return False, "价格不可用，低浮盈风控不触发"
    open_price = as_float(quote.get("open"))
    prev_close = as_float(quote.get("prev_close"))
    l30 = as_float(feat.get("l30"))
    or15_low = as_float(feat.get("or15_low"))
    amount_1m = as_float(feat.get("amount_ratio_1m")) or 0.0
    amount_5m = as_float(feat.get("amount_ratio_5m")) or 0.0

    broken = []
    if vwap and price <= float(vwap) - eps:
        broken.append(f"VWAP {f2(vwap)}")
    if open_price and price <= open_price - eps:
        broken.append(f"开盘价 {f2(open_price)}")
    if prev_close and price <= prev_close - eps:
        broken.append(f"昨收 {f2(prev_close)}")
    if or15_low and price <= or15_low - eps:
        broken.append(f"OR15低点 {f2(or15_low)}")
    if l30 and price <= l30 - eps:
        broken.append(f"30分钟低点 {f2(l30)}")

    recent = []
    for value in feat.get("last3_prices") or []:
        parsed = as_float(value)
        if parsed is not None:
            recent.append(parsed)
    recent_failed = bool(vwap and recent and all(p <= float(vwap) - eps for p in recent[-3:]))
    volume_confirms = amount_1m >= 1.10 or amount_5m >= 1.05
    damaged = len(broken) >= 3 and (volume_confirms or recent_failed)
    detail = (
        f"低浮盈风控结构确认：破位 {','.join(broken) if broken else '-'}；"
        f"1m/5m量能 {amount_1m:.2f}/{amount_5m:.2f}；"
        f"连续不收回VWAP={'是' if recent_failed else '否'}。"
    )
    return damaged, detail


def opening_red_carry_confirmation(quote, feat, price, vwap, eps):
    """Confirm early red-gate weakness without treating one opening print as a sell.

    This is deliberately weaker than a P0 hard-defense break: it can trim one
    third of an inherited, sellable position, but it requires two broken
    reference lines plus volume or repeated failure to reclaim.
    """
    try:
        price = float(price)
        day_pct = float(quote.get("pct"))
    except (TypeError, ValueError):
        return False, "价格或当日涨跌幅不可用"
    open_price = quote.get("open") or price
    prev_close = quote.get("prev_close") or price
    try:
        open_price = float(open_price)
        prev_close = float(prev_close)
    except (TypeError, ValueError):
        return False, "开盘价或昨收不可用"
    broken = []
    for label, line in (("VWAP", vwap), ("开盘价", open_price), ("昨收", prev_close), ("OR5低点", feat.get("or5_low"))):
        try:
            if line and price <= float(line) - eps:
                broken.append(f"{label} {f2(line)}")
        except (TypeError, ValueError):
            continue
    anchor = max(float(x) for x in (vwap, open_price, prev_close) if x)
    recent = []
    for value in feat.get("last3_prices") or []:
        try:
            recent.append(float(value))
        except (TypeError, ValueError):
            continue
    recent_failed = len(recent) >= 3 and all(value <= anchor - eps for value in recent[-3:])
    volume_confirmed = float(feat.get("amount_ratio_1m") or 0) >= 1.15 or float(feat.get("amount_ratio_5m") or 0) >= 1.05
    repair = recent_reclaiming(feat, anchor, eps)
    confirmed = day_pct <= -3.0 and len(broken) >= 2 and (volume_confirmed or recent_failed) and not repair
    detail = (
        f"红色门控早盘确认：个股当日 {day_pct:.2f}%；破位 {','.join(broken) or '-'}；"
        f"1m/5m量能 {(feat.get('amount_ratio_1m') or 0):.2f}/{(feat.get('amount_ratio_5m') or 0):.2f}；"
        f"连续不收回={'是' if recent_failed else '否'}；快速修复={'是' if repair else '否'}。"
    )
    return confirmed, detail


def paper_profit_guard_reason(row, quote, global_context=None):
    profit_pct = row.get("paper_unrealized_pnl_pct")
    day_pct = quote.get("pct")
    sellable = int(row.get("paper_sellable") or 0)
    if sellable <= 0:
        return None
    try:
        profit_pct = float(profit_pct)
        day_pct = float(day_pct)
    except Exception:
        return None
    guard_min = global_policy_value(global_context, "profit_guard_min_pnl_pct", PAPER_PROFIT_GUARD_MIN_PNL_PCT)
    guard_loss = global_policy_value(global_context, "profit_guard_day_loss_pct", PAPER_PROFIT_GUARD_DAY_LOSS_PCT)
    erosion_max = global_policy_value(global_context, "profit_erosion_max_pnl_pct", PAPER_PROFIT_EROSION_MAX_PNL_PCT)
    erosion_loss = global_policy_value(global_context, "profit_erosion_day_loss_pct", PAPER_PROFIT_EROSION_DAY_LOSS_PCT)
    feat = row.get("rt_features") or {}
    price = quote.get("close")
    vwap = feat.get("vwap")
    eps = feat.get("epsilon") or max(2 * TICK, float(price or 0) * 0.0008)
    trend_damaged, trend_detail = profit_guard_trend_damage(row, quote, feat, price, vwap, eps)
    global_level = str((global_context or {}).get("risk_level") or "").lower()

    if (
        global_level == "red"
        and is_open_carry_de_risk_window(now_dt())
        and opening_red_carry_confirmation(quote, feat, price, vwap, eps)[0]
    ):
        _, detail = opening_red_carry_confirmation(quote, feat, price, vwap, eps)
        return (
            "OPEN_RED_CARRY_DE_RISK",
            "全球风险红色且为隔夜可卖持仓，早盘确认多条参考线同时失守；" + detail,
            0.34,
        )

    if (
        global_level == "red"
        and is_open_noise_window(now_dt())
        and profit_pct <= 8.0
        and day_pct <= -3.0
        and trend_damaged
    ):
        fraction = 0.50 if profit_pct <= 2.0 else 0.34
        return (
            "OPEN_RED_GATE_TREND_BREAK",
            f"全球风险为红色门控，开盘前5分钟个股跌幅 {day_pct:.2f}% <= -3.00%，"
            f"模拟持仓浮盈/浮亏 {profit_pct:.2f}%，且已确认日内结构破坏；{trend_detail}",
            fraction,
        )

    if profit_pct >= 35.0 and day_pct <= -2.0:
        return (
            "HIGH_PROFIT_FAST_DROP",
            f"模拟持仓浮盈 {profit_pct:.2f}% >= 35.00%，当日跌幅 {day_pct:.2f}% <= -2.00%，"
            "高浮盈票出现明显回撤，触发加速利润保护。",
            0.50,
        )
    if profit_pct >= guard_min and vwap and price is not None:
        below_vwap_confirmed = float(price) <= float(vwap) - eps and recent_count_above(feat, float(vwap), eps, n=3) == 0
        if below_vwap_confirmed:
            return (
                "PROFIT_VWAP_LOST",
                f"模拟持仓浮盈 {profit_pct:.2f}% >= {guard_min:.2f}%，"
                f"且连续采样未收回VWAP {vwap:.2f}，触发利润保护减仓。",
                0.34,
            )
    if profit_pct >= guard_min and day_pct <= guard_loss:
        return (
            "PROFIT_LOCK",
            f"模拟持仓浮盈 {profit_pct:.2f}% >= {guard_min:.2f}%，"
            f"但当日跌幅 {day_pct:.2f}% <= {guard_loss:.2f}%，触发利润保护分批减仓。"
            ,
            0.34,
        )
    if profit_pct <= 8.0 and day_pct <= -5.0 and trend_damaged:
        fraction = 0.50 if profit_pct <= 2.0 else 0.34
        return (
            "LOW_PROFIT_TREND_BREAK",
            f"模拟持仓浮盈已低于 8.00%（当前 {profit_pct:.2f}%），"
            f"当日跌幅 {day_pct:.2f}% <= -5.00%，且已确认日内结构破坏；{trend_detail}",
            fraction,
        )
    if profit_pct <= 12.0 and day_pct <= -7.0 and trend_damaged:
        return (
            "DEEP_DAY_LOSS_TREND_BREAK",
            f"模拟持仓浮盈已低于 12.00%（当前 {profit_pct:.2f}%），"
            f"当日跌幅 {day_pct:.2f}% <= -7.00%，且已确认深跌结构破坏；{trend_detail}",
            0.50,
        )
    if profit_pct <= erosion_max and day_pct <= erosion_loss and trend_damaged:
        return (
            "PROFIT_EROSION_TREND_BREAK",
            f"模拟持仓浮盈已收窄到 {profit_pct:.2f}% <= {erosion_max:.2f}%，"
            f"且当日跌幅 {day_pct:.2f}% <= {erosion_loss:.2f}%，并确认结构破坏；{trend_detail}",
            0.34,
        )
    return None


def paper_guard_execution_policy(guard_type, profit_pct, reduce_fraction):
    try:
        profit_pct = float(profit_pct)
    except Exception:
        profit_pct = None
    fraction_text = f"{reduce_fraction:.0%}"
    is_loss_or_low_profit = guard_type in (
        "OPEN_RED_GATE_TREND_BREAK",
        "OPEN_RED_CARRY_DE_RISK",
        "LOW_PROFIT_TREND_BREAK",
        "DEEP_DAY_LOSS_TREND_BREAK",
        "PROFIT_EROSION_TREND_BREAK",
    )
    if guard_type == "OPEN_RED_GATE_TREND_BREAK":
        return {
            "execution_action": "PAPER_OPEN_RED_GATE_TREND_BREAK_REDUCE",
            "confirm_rule": "开盘红色外部风险门控：全球科技/油价等外部冲击未解除，个股开盘急跌并确认日内结构破坏。",
            "action": "模拟盘持仓风险优先：停止加仓；按开盘红色门控减仓，先把高波动日的组合风险降下来。",
            "reduce_policy": f"开盘红色门控+趋势破坏，按 {fraction_text} 计划仓减仓",
        }
    if guard_type == "OPEN_RED_CARRY_DE_RISK":
        return {
            "execution_action": "PAPER_OPEN_RED_CARRY_DE_RISK_REDUCE",
            "confirm_rule": "红色全球门控下的隔夜持仓：09:35-09:50，至少两条开盘参考线失守，并有量能或连续采样确认，不能只因一笔低开卖出。",
            "action": "模拟盘持仓风险优先：停止加仓；先减约三分之一可卖仓，保留修复空间；若后续再破硬防守，再按硬风控处理。",
            "reduce_policy": f"红色门控早盘确认减压，按 {fraction_text} 计划仓减仓",
        }
    if is_loss_or_low_profit:
        if profit_pct is not None and profit_pct <= 0:
            title = "低浮盈转亏/趋势破坏"
        else:
            title = "低浮盈侵蚀/趋势破坏"
        return {
            "execution_action": "PAPER_TREND_BREAK_RISK_REDUCE",
            "confirm_rule": f"{title}：不是单纯盈利保护，必须同时满足价格跌破VWAP/开盘价/昨收/OR或30分钟低点等结构确认。",
            "action": "模拟盘持仓风险优先：停止加仓；按软风险比例分批减仓，保留修复观察但不在破位中补仓。",
            "reduce_policy": f"{title}，按 {fraction_text} 计划仓减仓",
        }
    return {
        "execution_action": "PAPER_PROFIT_PROTECTION_REDUCE",
        "confirm_rule": "模拟盘盈利保护：大浮盈票当日急跌、跌破VWAP或浮盈快速侵蚀，按软风控比例先锁定利润。",
        "action": "模拟盘持仓风险优先：停止加仓；按软风险比例分批减仓，防止大浮盈在高波动日继续回吐。",
        "reduce_policy": f"模拟盘盈利保护，按 {fraction_text} 计划仓分批减仓",
    }


def high_open_reclaim_ready(quote, feat, price, now=None):
    """Require a fresh OR15/VWAP repair after a high-open attack was cancelled."""
    now = now or now_dt()
    try:
        open_price = float(quote.get("open") or 0)
        prev_close = float(quote.get("prev_close") or 0)
    except (TypeError, ValueError):
        return False, "开盘/昨收数据不足"
    if prev_close <= 0 or (open_price - prev_close) / prev_close * 100 < 1.5:
        return False, "非高开攻击场景"
    if now.time() < dtime(10, 0):
        return False, "高开取消后二次入场需10:00后，先形成OR15结构"
    vwap = feat.get("vwap")
    or15_high = feat.get("or15_high")
    or15_mid = feat.get("or15_mid")
    epsilon = feat.get("epsilon") or max(2 * TICK, price * 0.0008)
    if not vwap or not or15_high:
        return False, "VWAP/OR15数据不足"
    reclaim_line = max(float(vwap), float(or15_high))
    prices = [float(value) for value in (feat.get("last10_prices") or []) if isinstance(value, (int, float))]
    cancel_line = min(value for value in (open_price, float(vwap), float(or15_mid or vwap)) if value > 0)
    had_cancel = bool(prices and min(prices) < cancel_line - epsilon)
    recent = prices[-3:]
    recent_confirmed = len(recent) >= 3 and all(value >= reclaim_line - epsilon for value in recent)
    amount_1m = float(feat.get("amount_ratio_1m") or 0)
    amount_5m = float(feat.get("amount_ratio_5m") or 0)
    if not had_cancel:
        return False, "尚未记录高开回落取消路径"
    if price < reclaim_line + epsilon or not recent_confirmed:
        return False, "未重新站稳OR15高点/VWAP并完成3分钟确认"
    if amount_1m < 1.40 or amount_5m < 1.15:
        return False, f"二次修复量能不足：1m/5m {amount_1m:.2f}/{amount_5m:.2f}"
    return True, "高开取消后重新站稳OR15高点/VWAP，且量能与3分钟确认通过"


def v2_signal_for_row(row, position=None):
    """Translate timing evidence into a core-V2 or named strategy contract.

    Observation-pool orders are emitted as 龙头/520/五日线 strategy events.
    V2 remains the shared evidence layer and is persisted in ``entry_pattern``
    so the signal is reproducible without letting the timing engine choose a
    different strategy path.
    """
    q = row["quote"]
    timing = row.get("timing_v2") or {}
    levels = timing.get("levels") or {}
    room = timing.get("room_risk") or {}
    position = position or {}
    sellable = int(position.get("sellable") or 0)
    pattern = timing.get("entry_pattern")
    candidate_pattern = timing.get("candidate_entry_pattern") or pattern
    position_action = str(timing.get("position_action") or "NO_POSITION_ACTION")
    current = q.get("close")
    entry_invalid = levels.get("entry_invalidation") or levels.get("structural_invalidation")
    structural_invalid = levels.get("structural_invalidation") or entry_invalid
    resistance = levels.get("nearest_resistance")
    plan_target = row.get("planned_target_position_pct")
    plan_max = row.get("planned_max_position_pct")
    plan_probe = row.get("planned_v2_probe_position_pct")
    plan_allows_entry = bool(row.get("premarket_plan_allows_entry")) if "premarket_plan_allows_entry" in row else False
    plan_action = str(row.get("premarket_plan_action") or "未加载当日盘前仓位计划")
    plan_source = str(row.get("premarket_plan_source") or "")
    plan_loaded = bool(row.get("premarket_plan_loaded"))
    strategy_contract = row.get("strategy_contract") or observation_strategy_router.contract_from_level(row)
    sector_resonance_ok, sector_resonance_reason = observation_strategy_router.sector_resonance_gate(strategy_contract, row)
    daily_strategy_ok, daily_strategy_reason = observation_strategy_router.daily_qualification_gate(strategy_contract)
    # ``entry_pattern`` is intentionally null until every execution blocker is
    # cleared.  The strategy contract must validate the earned 15m candidate
    # pattern, otherwise every legitimate near-trigger is mislabelled as a
    # strategy-route failure while it is merely waiting for 5m/volume.
    strategy_gate_ok, strategy_gate_reason = observation_strategy_router.execution_gate(
        strategy_contract, candidate_pattern
    )

    def valid_plan_pct(value):
        try:
            return 0 < float(value) <= 1
        except (TypeError, ValueError):
            return False

    plan_complete = bool(
        plan_loaded
        and plan_action
        and valid_plan_pct(plan_target)
        and valid_plan_pct(plan_max)
        and valid_plan_pct(plan_probe)
    )
    # A holding-management note must never manufacture a fresh entry permit.
    # Re-entry now requires the same named daily contract as every other buy.
    repair_reentry_authorized = False
    effective_plan_allows_entry = bool(plan_allows_entry)
    framework_validation = row.get("framework_validation") or {}
    framework_executable = framework_validation.get("executable") is not False
    base_extra = {
        "timing_v2": timing,
        "strategy_version": timing.get("version") or strategy_contract.get("version") or "intraday_timing_v2_0",
        "daily_contract_version": strategy_contract.get('version'),
        "strategy_family": "THREE_METHOD" if strategy_contract.get("is_observation_strategy") else "LEGACY_BLOCKED",
        "execution_band_low": room.get("execution_band_low"),
        "execution_band_high": room.get("execution_band_high"),
        "entry_invalidation": entry_invalid,
        "structural_invalidation": structural_invalid,
        "nearest_resistance": resistance,
        "room_atr": room.get("room_atr"),
        "reward_risk": room.get("reward_risk"),
        "position_multiplier": timing.get("position_multiplier") or 0.0,
        "position_context": "SELLABLE_POSITION" if sellable else "FLAT",
        "premarket_plan_action": plan_action,
        "planned_target_position_pct": plan_target,
        "planned_max_position_pct": plan_max,
        "planned_v2_probe_position_pct": plan_probe,
        "premarket_plan_allows_entry": effective_plan_allows_entry,
        "original_premarket_plan_allows_entry": plan_allows_entry,
        "repair_reentry_authorized": repair_reentry_authorized,
        "premarket_plan_loaded": plan_loaded,
        "premarket_plan_complete": plan_complete,
        "premarket_plan_source": plan_source,
        "entry_position_cap_pct": row.get("entry_position_cap_pct"),
        "rotation_pilot": bool(row.get("rotation_pilot")),
        "sector_momentum": row.get("sector_momentum") or {},
        "sector_rotation": row.get("sector_rotation") or {},
        "risk_bucket": row.get("risk_bucket") or {},
        "framework_validation": framework_validation,
        "strategy_contract": strategy_contract,
        "strategy_key": strategy_contract.get("key"),
        "strategy_name": strategy_contract.get("name"),
        "strategy_style": strategy_contract.get("style"),
        "strategy_formal_entry_signal": strategy_contract.get("formal_entry_signal"),
        "sector_resonance_ok": sector_resonance_ok,
        "sector_resonance_reason": sector_resonance_reason,
        "strategy_daily_qualified": daily_strategy_ok,
        "strategy_daily_evidence": daily_strategy_reason,
        "repair_shadow": timing.get('repair_shadow') or {},
        "gap_research": timing.get('gap_research') or {},
        "contract_readiness": timing.get('contract_readiness') or {},
        "strategy_gate_ok": strategy_gate_ok,
        "strategy_gate_reason": strategy_gate_reason,
    }
    if position_action in {"STRUCTURAL_EXIT", "TAKE_PROFIT", "REDUCE"}:
        scenario = f"V2_{position_action}"
        return make_signal(
            row,
            scenario,
            "P0" if position_action == "STRUCTURAL_EXIT" else "P1",
            "立即处理",
            position_action,
            timing.get("exit_reference") or current,
            None,
            current,
            timing.get("exit_reference") or structural_invalid,
            timing.get("position_reason") or "V2已收盘K线退出条件成立",
            "收复对应15分钟结构并重新完成V2 Setup前，不重新开仓。",
            f"V2 {position_action}：仅处理可卖 {sellable} 股，T+1锁定部分仅记录风险。",
            [timing.get("position_reason") or "V2持仓风控"],
            {**base_extra, "execution_action": position_action},
        )
    if position_action == "NO_ADD":
        return make_signal(
            row,
            "V2_NO_ADD",
            "P1",
            "观察",
            "NO_ADD",
            levels.get("ma5_15") or current,
            None,
            None,
            entry_invalid,
            timing.get("position_reason") or "V2软风险观察",
            "重新取得所属策略允许的盘中形态，并通过VWAP、量能与已收盘5分钟门控。",
            "停止加仓；已有仓位只做结构观察。",
            [timing.get("position_reason") or "V2停止加仓"],
            {**base_extra, "execution_action": "NO_ADD"},
        )
    if (
        timing.get("entry_allowed")
        and pattern in intraday_timing_v2.TIMING_PATTERNS
        and effective_plan_allows_entry
        and plan_complete
        and framework_executable
        and sector_resonance_ok
        and daily_strategy_ok
        and strategy_gate_ok
    ):
        trigger = room.get("entry_reference") or current
        scenario = observation_strategy_router.entry_scenario(strategy_contract, pattern)
        strategy_name = strategy_contract.get("name") or "待分类观察"
        formal_signal = strategy_contract.get("formal_entry_signal") or observation_strategy_router.formal_entry_signal(strategy_contract)
        return make_signal(
            row,
            scenario,
            "P1",
            "立即处理",
            "BUY_PROBE",
            trigger,
            trigger,
            None,
            entry_invalid,
            f"{sector_resonance_reason}；{daily_strategy_reason}；{formal_signal}；时机证据={pattern}；120m={timing.get('regime')}，位置={timing.get('location')}，"
            f"方法触发周期={timing.get('trigger_timeframe') or '15m/5m'}，5m执行、量能与Room/RR通过。",
            "现价跌破确认结构、进入下一根5分钟需重新评估；跌破入场失效位、路径硬失败或离开执行区即取消。",
            f"盘前仓位计划={plan_action}，目标/上限/本次 "
            f"{float(plan_target or 0) * 100:.0f}%/{float(plan_max or 0) * 100:.0f}%/{float(plan_probe or 0) * 100:.0f}%；"
            f"{strategy_name}决定交易方法；多周期时序层只核验本次入场，不追价。",
            [f"策略={strategy_name}", f"时机证据={pattern}", f"RoomATR={room.get('room_atr')}", f"RR={room.get('reward_risk')}"],
            {
                **base_extra,
                "execution_action": "BUY_PROBE",
                "signal_quality_gate": "strategy_closed_bar_setup",
                "strategy_entry_pattern": pattern,
            },
        )
    return make_signal(
        row,
        "V2_WAIT",
        "P2",
        "观察",
        "WAIT_MULTI_PERIOD",
        None,
        None,
        None,
        entry_invalid,
        f"等待{strategy_contract.get('name') or '最新策略合同'}完成方法专属触发（日线锚点/龙头转强）、Room/RR与闭合5m执行确认。",
        "任一Hard Veto、延伸、MID_AIR、数据降级或14:45后均继续等待。",
        "不预挂固定买入价；下一次已收盘K线重新计算策略时机与执行区间。",
        list(timing.get("blockers") or timing.get("reasons") or ["三策略多周期确认未完成"])
        + ([] if effective_plan_allows_entry else [f"盘前计划={plan_action}，当日不允许新增试仓"])
        + ([] if plan_complete or (plan_loaded and not plan_allows_entry) else ["盘前仓位计划缺失/不完整，只观察不生成买入事件"])
        + ([] if sector_resonance_ok else [sector_resonance_reason])
        + ([] if daily_strategy_ok else [daily_strategy_reason])
        + ([] if strategy_gate_ok else ["策略路由/策略合同：" + strategy_gate_reason])
        + ([] if framework_executable else [
            "框架依据未完成可执行核验，只观察：" + str(framework_validation.get("reason") or "行业/题材证据不足")
        ]),
        {**base_extra, "execution_action": "WAIT"},
    )


def evaluate_v2_signals(rows, positions):
    return [v2_signal_for_row(row, (positions or {}).get(row["code"])) for row in rows]


def evaluate_signals(rows, previous_prices, positions, allow_attack, global_context=None):
    """Compatibility entrypoint redirected to the three-method evaluator.

    Older command-line callers used this function directly.  Keeping the
    public name avoids a silent runtime failure, but it must never revive the
    retired price-line scenarios or their global attack switch.
    """
    return evaluate_v2_signals(rows, positions)


def _legacy_evaluate_signals_for_history(rows, previous_prices, positions, allow_attack, global_context=None):
    """Compatibility shim: historical callers are forced through the active engine."""
    return evaluate_v2_signals(rows, positions)

    # Historical implementation retained below for forensic comparison only.
    # It is intentionally unreachable and must not be re-enabled.
    signals = []
    for row in rows:
        q = row["quote"]
        feat = row.get("rt_features") or {}
        sig = row.get("signal") or {}
        price = q["close"]
        prev = previous_prices.get(row["code"], price)
        eps = feat.get("epsilon") or max(2 * TICK, price * 0.0008)
        watch = feat.get("watch_band") or max(5 * TICK, price * 0.0025)
        vwap = feat.get("vwap")
        defense = row["defense"]
        repair = row["repair"]
        open_price = q.get("open") or price
        position = positions.get(row["code"], {})
        sellable = position.get("sellable")
        sellable_note = "可卖数量未知，需人工核对" if sellable is None else f"可卖 {sellable}"
        paper_guard = paper_profit_guard_reason(row, q, global_context=global_context) if row.get("paper_position") else None

        hard_line = max(x for x in [defense, sig.get("risk_reduce_price") or defense] if x)
        hard_trigger = floor_to_tick(hard_line - eps)
        hard_invalid = ceil_to_tick(hard_line + max(2 * eps, 0.25 * (feat.get("atr1m") or 0)))
        if price <= hard_line - eps:
            hard_confirmed, hard_reasons, hard_blockers = hard_risk_confirmation(row, feat, price, hard_line, eps)
            if not hard_confirmed:
                signals.append(make_signal(
                    row,
                    "SOFT_VWAP_BREAK",
                    "P1",
                    "接近触发",
                    "ARMED",
                    hard_trigger,
                    None,
                    None,
                    hard_invalid,
                    "硬防守预警：必须形成超过ATR缓冲的破位，并满足连续采样或放量叠加结构确认，才升级P0硬减仓",
                    f"重新站回 {f2(hard_invalid)}，或收复VWAP/开盘价并连续3分钟不再跌破",
                    "停止加仓；先按风险预警观察，不立即卖。满足1m/3m确认或继续跌破下一结构层级后，才升级P0；快速收复关键线则撤销。",
                    hard_reasons + hard_blockers,
                    {
                        "execution_action": "HARD_RISK_PENDING",
                        "reduce_policy": "硬风险未确认前只保留Dashboard预警，不执行模拟减仓",
                        "hard_risk_blockers": hard_blockers,
                        "open_noise_window": is_open_noise_window(now_dt()),
                    },
                ))
                continue
            signals.append(make_signal(
                row,
                "P0_HARD_RISK_REDUCE",
                "P0",
                "立即处理",
                "FIRED",
                hard_trigger,
                None,
                price,
                hard_invalid,
                "0-20秒：价格穿越硬防守，报价级确认",
                f"重新站回 {f2(hard_invalid)} 且连续3分钟不再跌破",
                f"停止加仓；若有持仓且{sellable_note}，硬防守破位按剩余可卖全处理/降至0风险仓；若可卖为0，只记录风险暴露。",
                [f"跌破盘前防守/风险线 {f2(hard_line)}", f"当前价 {f2(price)} <= 触发价 {f2(hard_trigger)}"] + hard_reasons,
                {
                    "opening_confirmed": bool(hard_confirmed),
                    "open_noise_window": is_open_noise_window(now_dt()),
                },
            ))
            continue

        if paper_guard:
            guard_type, guard_reason, reduce_fraction = paper_guard
            guard_policy = paper_guard_execution_policy(guard_type, row.get("paper_unrealized_pnl_pct"), reduce_fraction)
            restore_line = max(x for x in [vwap, open_price, q.get("prev_close"), price] if x)
            guard_scenario = (
                "PAPER_OPEN_RED_CARRY_DE_RISK"
                if guard_type == "OPEN_RED_CARRY_DE_RISK"
                else "SOFT_VWAP_BREAK"
            )
            signals.append(make_signal(
                row,
                guard_scenario,
                "P0",
                "立即处理",
                "FIRED",
                floor_to_tick(price - eps),
                None,
                price,
                ceil_to_tick(restore_line + max(2 * eps, 0.25 * (feat.get("atr1m") or 0))),
                guard_policy["confirm_rule"],
                f"重新站回 {f2(restore_line)} 上方并连续3分钟不再跌破，才恢复持有观察。",
                guard_policy["action"],
                [
                    guard_reason,
                    f"当前价 {f2(price)}，模拟成本 {f2(row.get('paper_avg_cost'))}",
                    f"VWAP {f2(vwap)}，开盘价 {f2(open_price)}，昨收 {f2(q.get('prev_close'))}",
                ],
                {
                    "execution_action": guard_policy["execution_action"],
                    "reduce_policy": guard_policy["reduce_policy"],
                    "paper_reduce_fraction": reduce_fraction,
                    "paper_profit_guard": guard_type,
                    "paper_unrealized_pnl_pct": row.get("paper_unrealized_pnl_pct"),
                    "paper_sellable": row.get("paper_sellable"),
                },
            ))
            continue

        soft_candidates = [x for x in [vwap, feat.get("or15_low"), feat.get("l30")] if x]
        if soft_candidates:
            soft_line = max(soft_candidates)
            soft_trigger = floor_to_tick(soft_line - eps)
            crossed_down = prev >= soft_line + eps and price <= soft_line - eps
            confirmed = price <= soft_line - eps and (
                recent_count_above(feat, soft_line, eps, n=3) == 0
                or q.get("pct", 0) < 0
                or feat.get("amount_ratio_1m", 0) >= 1.1
            )
            if crossed_down or confirmed:
                signals.append(make_signal(
                    row,
                    "SOFT_VWAP_BREAK",
                    "P1",
                    "接近触发",
                    "ARMED",
                    soft_trigger,
                    None,
                    None,
                    ceil_to_tick(soft_line + max(2 * eps, 0.25 * (feat.get("atr1m") or 0))),
                    "软风控：跌破VWAP/OR15/L30之一，需1m/3m确认后才从预警升级为减压动作",
                    "重新站回软风险线+buffer，并至少收复VWAP/OR_low之一",
                    "停止加仓；这不是硬卖点。若反抽不过软风险线或继续跌向硬防守，再减试错仓。",
                    [f"跌破软风险线 {f2(soft_line)}", f"VWAP {f2(vwap)}", f"L30 {f2(feat.get('l30'))}"],
                    {
                        "execution_action": "SOFT_RISK_WARN",
                        "reduce_policy": "只减试错仓；未跌破硬防守前不按P0硬止损执行",
                    },
                ))

        high_open = q.get("open") and q.get("prev_close") and (q["open"] - q["prev_close"]) / q["prev_close"] * 100 >= 1.5
        or_mid = feat.get("or15_mid")
        high_open_cancel = high_open and (
            price < open_price - eps or (vwap and price < vwap - eps) or (or_mid and price < or_mid - eps)
        )
        cancel_line = min(x for x in [open_price, vwap, or_mid] if x)
        recent_prices = [
            float(value)
            for value in (feat.get("last10_prices") or [])
            if isinstance(value, (int, float))
        ]
        # Once a high-open plan has failed, ordinary pullback and repair rules
        # must not reopen it by accident.  A later entry has to pass the
        # dedicated OR15/VWAP reclaim test above.
        high_open_cancelled_path = bool(
            high_open
            and (
                high_open_cancel
                or any(value < cancel_line - eps for value in recent_prices)
            )
        )
        if high_open_cancel:
            signals.append(make_signal(
                row,
                "HIGH_OPEN_ATTACK_CANCEL",
                "P0",
                "取消",
                "CANCELLED",
                floor_to_tick(min(x for x in [open_price, vwap, or_mid] if x) - eps),
                None,
                None,
                ceil_to_tick(max(x for x in [open_price, vwap or open_price, or_mid or open_price] if x) + 2 * eps),
                "1-5分钟：高开后跌回开盘价/VWAP/OR中轴",
                "重新站回OR15_high且3分钟不跌回并有成交额确认",
                "取消原进攻/加仓计划；已有仓位转为压力减仓或风险观察，当日不追高。",
                ["高开后回落", f"开盘价 {f2(open_price)}", f"VWAP {f2(vwap)}", f"OR15中轴 {f2(or_mid)}"],
            ))

        if not allow_attack or not is_attack_allowed_by_global(now_dt(), global_context, row=row):
            continue

        reclaim_ok, reclaim_reason = high_open_reclaim_ready(q, feat, price, now=now_dt())
        if reclaim_ok and sig.get("priority") != "P0":
            reclaim_line = max(vwap or 0, feat.get("or15_high") or 0)
            context_ok, context_reason = p1_context_check(row, feat, price)
            band_low, band_high = p1_execution_band(reclaim_line + eps, feat, price, "repair")
            not_chasing = not rapid_rise(feat, price) and price <= band_high
            if context_ok and not_chasing:
                invalid = floor_to_tick(reclaim_line - max(2 * eps, 0.35 * (feat.get("atr5m") or 0)))
                scenario = core_entry_scenario(row, "REPAIR_RECLAIM_ADD")
                signals.append(make_signal(
                    row,
                    scenario,
                    "P1",
                    "立即处理",
                    "FIRED",
                    ceil_to_tick(reclaim_line + eps),
                    min(price, ceil_to_tick(reclaim_line + eps)),
                    None,
                    invalid,
                    "高开取消后，10:00后重新站上OR15高点/VWAP，连续3分钟确认且1m/5m量能至少1.40/1.15",
                    f"重新跌回 {f2(invalid)}，或量能不能维持，或再次跌回OR15高点下方",
                    f"原高开计划已取消；这是新的修复重入，只执行一档{'首笔试错仓' if scenario == 'CORE_INITIAL_ENTRY' else '加仓'}，区间 {f2(band_low)}-{f2(band_high)}，超过不追。",
                    [reclaim_reason, f"修复线 {f2(reclaim_line)}", context_reason],
                    {
                        "execution_band_low": band_low,
                        "execution_band_high": band_high,
                        "max_chase_distance": max(band_high - reclaim_line, 0),
                        "rapid_rise_blocked": not not_chasing,
                        "over_extension_blocked": price > band_high,
                        "gate_blockers": [],
                        "vwap_distance_pct": ((price - vwap) / max(price, TICK) * 100) if vwap else None,
                        "execution_action": "OPEN_INITIAL_POSITION" if scenario == "CORE_INITIAL_ENTRY" else "BUY_SMALL_ONLY_IN_BAND",
                        "signal_quality_gate": "or15_or_repair_reclaim",
                        "high_open_reclaim_after_cancel": True,
                        "post_p0_reentry_ok": True,
                    },
                ))

        if (
            vwap
            and price > vwap + eps
            and prev <= vwap + watch
            and sig.get("priority") != "P0"
            and not high_open_cancelled_path
        ):
            context_ok, context_reason = p1_context_check(row, feat, price)
            amount_ratio = feat.get("amount_ratio_1m", 0)
            amount_ok = 1.15 <= amount_ratio <= P1_MAX_AMOUNT_RATIO_1M and feat.get("amount_ratio_5m", 0) >= 1.10
            structure_ok = (
                recent_left_line(feat, vwap, eps, price, n=10)
                and recent_touched(feat, vwap, eps, n=6)
                and recent_count_above(feat, vwap, eps, n=3) >= 3
                and recent_reclaiming(feat, vwap, eps)
            )
            band_low, band_high = p1_execution_band(vwap + eps, feat, price, "vwap")
            near_enough = price <= band_high
            not_chasing = not rapid_rise(feat, price)
            if context_ok and amount_ok and structure_ok and near_enough and not_chasing and price > defense:
                add_trigger = ceil_to_tick(vwap + eps)
                invalid = floor_to_tick(vwap - max(3 * eps, 0.35 * (feat.get("atr5m") or 0)))
                scenario = core_entry_scenario(row, "VWAP_PULLBACK_ADD")
                signals.append(make_signal(
                    row,
                    scenario,
                    "P1",
                    "立即处理",
                    "FIRED",
                    add_trigger,
                    min(price, add_trigger),
                    None,
                    invalid,
                    "1-3分钟：先远离VWAP，再缩量回踩触线，随后3/3根分钟线站上VWAP+buffer且重新上拐",
                    f"跌破 {f2(invalid)} 或急拉后跌回VWAP",
                    f"只执行计划内一档{'首笔试错仓' if scenario == 'CORE_INITIAL_ENTRY' else '加仓'}；有效区间 {f2(band_low)}-{f2(band_high)}，超过不追，等二次回踩。",
                    [f"VWAP {f2(vwap)} 回踩后收复", f"1m量能比 {amount_ratio:.2f}", context_reason],
                    {
                        "execution_band_low": band_low,
                        "execution_band_high": band_high,
                        "max_chase_distance": max(band_high - vwap, 0),
                        "rapid_rise_blocked": not not_chasing,
                        "over_extension_blocked": price > band_high,
                        "gate_blockers": [],
                        "vwap_distance_pct": ((price - vwap) / max(price, TICK) * 100) if vwap else None,
                        "execution_action": "OPEN_INITIAL_POSITION" if scenario == "CORE_INITIAL_ENTRY" else "BUY_SMALL_ONLY_IN_BAND",
                        "signal_quality_gate": "left_vwap_then_pullback_reclaim",
                    },
                ))

        repair_line = max(x for x in [repair, feat.get("or15_high"), feat.get("h30")] if x)
        if (
            price >= repair_line + eps
            and sig.get("priority") != "P0"
            and not high_open_cancelled_path
        ):
            context_ok, context_reason = p1_context_check(row, feat, price)
            amount_ratio = feat.get("amount_ratio_1m", 0)
            amount_ok = 1.5 <= amount_ratio <= P1_MAX_AMOUNT_RATIO_1M and feat.get("amount_ratio_5m", 0) >= 1.2
            recent_ok = recent_count_above(feat, repair_line, eps, n=3) >= 3
            band_low, band_high = p1_execution_band(repair_line + eps, feat, price, "repair")
            not_far = price <= band_high
            not_chasing = not rapid_rise(feat, price)
            if context_ok and amount_ok and recent_ok and not_far and not_chasing:
                invalid = floor_to_tick(repair_line - max(2 * eps, 0.3 * (feat.get("atr5m") or 0)))
                scenario = core_entry_scenario(row, "REPAIR_BREAKOUT_ADD")
                signals.append(make_signal(
                    row,
                    scenario,
                    "P1",
                    "立即处理",
                    "FIRED",
                    ceil_to_tick(repair_line + eps),
                    ceil_to_tick(repair_line + eps),
                    None,
                    invalid,
                    "1-5分钟：放量站回修复位，3/3根分钟线站上修复线，且未远离触发区",
                    f"3分钟内重新跌回 {f2(repair_line - eps)} 或量价背离",
                    f"只做计划内一档{'首笔试错仓' if scenario == 'CORE_INITIAL_ENTRY' else '加仓'}；有效区间 {f2(band_low)}-{f2(band_high)}，远离修复位等待回踩。",
                    [f"修复线 {f2(repair_line)}", f"1m量能比 {amount_ratio:.2f}", context_reason],
                    {
                        "execution_band_low": band_low,
                        "execution_band_high": band_high,
                        "max_chase_distance": max(band_high - repair_line, 0),
                        "rapid_rise_blocked": not not_chasing,
                        "over_extension_blocked": price > band_high,
                        "gate_blockers": [],
                        "vwap_distance_pct": ((price - vwap) / max(price, TICK) * 100) if vwap else None,
                        "execution_action": "OPEN_INITIAL_POSITION" if scenario == "CORE_INITIAL_ENTRY" else "BUY_SMALL_ONLY_IN_BAND",
                        "signal_quality_gate": "repair_reclaim_volume_not_extended",
                        "post_p0_reentry_ok": True,
                    },
                ))

        trigger_candidates = [
            ("硬风险", hard_line),
            ("VWAP", vwap),
            ("修复", repair),
            ("压力", row.get("pressure")),
        ]
        near = [f"{label}{f2(level)}" for label, level in trigger_candidates if level and abs(price - level) <= watch]
        if near:
            signals.append(make_signal(
                row,
                "NEAR_TRIGGER",
                "P2",
                "接近触发",
                "ARMED",
                None,
                None,
                None,
                sig.get("invalid_price") or defense,
                "20秒内：价格进入watch_band",
                "价格远离关键位或进入更高优先级信号",
                "观察，不提前买卖；等待价格穿越与量能确认。",
                ["接近 " + "、".join(near)],
            ))

    signals.sort(key=signal_sort_key)
    return signals


def signal_sort_key(signal):
    return (
        PRIORITY_ORDER.get(signal["priority"], 9),
        SCENARIO_ORDER.get(signal["scenario"], 9),
        abs(signal["distance_pct"] or 0),
    )


def select_primary_signals(signals):
    """Keep one operable signal per stock so Feishu reads like an action list."""
    selected = {}
    for sig in sorted(signals, key=signal_sort_key):
        old = selected.get(sig["symbol"])
        if not old:
            selected[sig["symbol"]] = sig
            continue
        if signal_sort_key(sig) < signal_sort_key(old):
            selected[sig["symbol"]] = sig
    return sorted(selected.values(), key=signal_sort_key)


def paper_position_map(rows):
    out = {}
    for row in rows or []:
        code = str(row.get("symbol") or row.get("code") or "").strip()
        if code:
            out[code] = row
    return out


def portfolio_soft_break_candidates(signals, positions):
    candidates = []
    for sig in signals or []:
        if sig.get("scenario") != "SOFT_VWAP_BREAK":
            continue
        if sig.get("external_status") != "接近触发":
            continue
        pos = positions.get(str(sig.get("symbol"))) or {}
        if int(pos.get("sellable") or 0) <= 0:
            continue
        candidates.append(sig)
    return candidates


def soft_break_severity(signal):
    try:
        current = float(signal.get("current_price") or 0)
        trigger = float(signal.get("trigger_price") or 0)
        distance = (current - trigger) / trigger * 100 if trigger else 0.0
    except Exception:
        distance = 0.0
    try:
        pct_value = float(signal.get("pct") or 0)
    except Exception:
        pct_value = 0.0
    return distance, pct_value


def portfolio_risk_context(signals, positions, account, global_context=None):
    account = account or {}
    candidates = portfolio_soft_break_candidates(signals, positions)
    sellable_count = len([
        row for row in (positions or {}).values()
        if int(row.get("sellable") or 0) > 0
    ])
    denominator = max(1, sellable_count)
    soft_break_ratio = len(candidates) / denominator
    position_pct = float(account.get("position_pct") or 0.0)
    day_pnl_pct = account.get("day_pnl_pct")
    try:
        day_pnl_pct_value = float(day_pnl_pct)
    except Exception:
        day_pnl_pct_value = None
    position_threshold = global_policy_value(global_context, "portfolio_risk_position_pct", PORTFOLIO_RISK_POSITION_PCT)
    day_loss_threshold = global_policy_value(global_context, "portfolio_risk_day_loss_pct", PORTFOLIO_RISK_DAY_LOSS_PCT)
    soft_ratio_threshold = global_policy_value(global_context, "portfolio_risk_soft_break_ratio", PORTFOLIO_RISK_SOFT_BREAK_RATIO)
    active = (
        position_pct >= position_threshold
        and day_pnl_pct_value is not None
        and day_pnl_pct_value <= day_loss_threshold
        and soft_break_ratio >= soft_ratio_threshold
        and bool(candidates)
    )
    return {
        "active": active,
        "position_pct": position_pct,
        "day_pnl_pct": day_pnl_pct_value,
        "soft_break_count": len(candidates),
        "sellable_position_count": sellable_count,
        "soft_break_ratio": soft_break_ratio,
        "thresholds": {
            "position_pct": position_threshold,
            "day_loss_pct": day_loss_threshold,
            "soft_break_ratio": soft_ratio_threshold,
            "max_upgrades": PORTFOLIO_RISK_MAX_UPGRADES,
        },
        "global_risk_level": (global_context or {}).get("risk_level"),
        "mark_source": account.get("mark_source"),
        "unmarked_positions": account.get("unmarked_positions"),
    }


def upgrade_soft_break_for_portfolio_risk(signals, positions, account, global_context=None):
    ctx = portfolio_risk_context(signals, positions, account, global_context=global_context)
    if not ctx["active"]:
        return signals, ctx

    candidates = sorted(
        portfolio_soft_break_candidates(signals, positions),
        key=soft_break_severity,
    )[:max(1, PORTFOLIO_RISK_MAX_UPGRADES)]
    upgrade_keys = {(sig.get("symbol"), sig.get("scenario")) for sig in candidates}
    upgraded = []
    for sig in signals:
        if (sig.get("symbol"), sig.get("scenario")) not in upgrade_keys:
            upgraded.append(sig)
            continue
        current = sig.get("current_price")
        trigger = sig.get("trigger_price")
        invalid = sig.get("invalid_price")
        reason = (
            f"组合级软风控触发：仓位{ctx['position_pct']:.2f}% >= {ctx['thresholds']['position_pct']:.2f}%，"
            f"账户日内{ctx['day_pnl_pct']:.2f}% <= {ctx['thresholds']['day_loss_pct']:.2f}%，"
            f"可卖持仓软破比例{ctx['soft_break_ratio'] * 100:.1f}%"
        )
        next_sig = {
            **sig,
            "priority": "P0",
            "external_status": "立即处理",
            "internal_state": "FIRED",
            "reduce_price": current,
            "confirm_rule": (
                "组合级软风控：高仓位 + 账户日内回撤 + 多数可卖持仓跌破VWAP/OR15/L30，"
                "把单票软风险从观察升级为分批减仓。"
            ),
            "cancel_rule": f"账户回撤收窄，且价格重新站回软风险线/恢复观察线 {f2(invalid)}",
            "action": (
                f"组合风险优先：停止加仓；{sig.get('name')} 已跌破软风险线 {f2(trigger)}，"
                "按软风险比例分批减仓，保留后续修复观察。"
            ),
            "execution_action": "PORTFOLIO_SOFT_RISK_REDUCE",
            "reduce_policy": "组合级软风控升级，按 A_SHARE_PAPER_SOFT_RISK_SELL_PCT 分批减仓",
            "portfolio_risk_overlay": ctx,
            "reasons": [reason] + list(sig.get("reasons") or []),
        }
        next_sig["distance_pct"] = line_distance(next_sig.get("current_price"), next_sig.get("trigger_price"))
        next_sig["fingerprint"] = fingerprint(next_sig)
        next_sig.update(attach_contract(next_sig))
        upgraded.append(next_sig)
    return sorted(upgraded, key=signal_sort_key), ctx


def visible_signals_for_dashboard(signals):
    # The current Tonghuashun union is around 150 symbols.  Keeping the old
    # default of 80 silently hid named-strategy rows from the dashboard and
    # made live coverage look incomplete even though the engine evaluated
    # them.  Retain a configurable ceiling but cover the present full pool.
    limit = max(20, int(os.environ.get("A_SHARE_DASHBOARD_SIGNAL_LIMIT", "200")))
    return select_primary_signals(signals)[:limit]


def json_safe(value, seen=None):
    """Return a JSON-serializable copy and cut accidental self references."""
    if seen is None:
        seen = set()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    obj_id = id(value)
    if obj_id in seen:
        return "[Circular]"
    if isinstance(value, dict):
        seen.add(obj_id)
        out = {str(k): json_safe(v, seen) for k, v in value.items()}
        seen.remove(obj_id)
        return out
    if isinstance(value, (list, tuple, set)):
        seen.add(obj_id)
        out = [json_safe(v, seen) for v in value]
        seen.remove(obj_id)
        return out
    return str(value)


def compact_paper_trade(paper):
    if not isinstance(paper, dict):
        return {}
    keys = (
        "order_id",
        "status",
        "reason",
        "side",
        "qty",
        "signal_price",
        "limit_price",
        "fill_price",
        "estimated_fill_price",
        "fees_total",
        "fees",
        "event_warning",
        "slippage",
        "contract_hash",
    )
    return {key: json_safe(paper.get(key)) for key in keys if key in paper}


def serialize_signal_for_dashboard(signal):
    out = {}
    for key, value in (signal or {}).items():
        if key == "paper_trade":
            continue
        if key == "timing_v2" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k not in {"method_replay_input", "timing_replay_input", "replay"}}
        out[key] = json_safe(value)
    if isinstance(signal, dict) and signal.get("paper_trade"):
        out["paper_trade"] = compact_paper_trade(signal.get("paper_trade"))
    return out


def serialize_market_opportunities(radar_rows, global_context=None, now=None):
    out = []
    for row in (radar_rows or [])[:8]:
        q = row["quote"]
        feat = row.get("rt_features") or {}
        dyn = row.get("dynamic") or {}
        axes = row.get("axes") or intra.axis_profile(row)
        status = intra.radar_opportunity_status(row)
        tone = "green" if status.startswith("🟢") else "yellow" if status.startswith("🟡") else "red"
        gate_ok, gate_reason = radar_signal_gate(row)
        market_gate_ok, market_gate_stage, market_gate_reason = global_risk.market_opportunity_gate_status(
            global_context, row=row, now=now
        )
        if not market_gate_ok:
            gate_reason = market_gate_reason
            status = f"🔴 {gate_reason}"
            tone = "yellow"
        band_low, band_high = radar_execution_band(row)
        price = q.get("close") or 0
        vwap = dyn.get("vwap") or feat.get("vwap")
        distance_pct = ((price - vwap) / max(price, TICK) * 100) if price and vwap else None
        out.append({
            "code": row["code"],
            "name": q.get("name") or row["code"],
            "status": status,
            "tone": tone,
            "current_price": q.get("close"),
            "pct": q.get("pct"),
            "vwap": dyn.get("vwap"),
            "trigger_price": intra.radar_trigger_price(row),
            "invalid_price": intra.radar_invalid_price(row),
            "amount_yi": intra.amount_yi(q),
            "axes": axes,
            "axes_text": intra.axis_text(row),
            "focus": row.get("focus"),
            "theme_evidence": row.get("theme_evidence") or q.get("theme_evidence") or {},
            "framework_validation": row.get("framework_validation") or {"valid": True, "reason": "实时框架已核验"},
            "candidate_source": row.get("candidate_source"),
            "tracked_candidate": row.get("tracked_candidate"),
            "rotation_pilot": row.get("rotation_pilot"),
            "entry_position_cap_pct": row.get("entry_position_cap_pct"),
            "framework_execution": row.get("framework_context") or {},
            "framework_trade_style": (
                "趋势/波段"
                if (row.get("framework_context") or {}).get("route") == "TREND_CONTINUATION"
                else "未分类"
            ),
            "framework_trade_style_reason": "盘后月线/日线台账确认的趋势延续" if (row.get("framework_context") or {}).get("route") == "TREND_CONTINUATION" else "未取得可执行的盘后趋势分类",
            "tracked_status": row.get("tracked_status"),
            "action": (
                f"{gate_reason}；等待全球风险解除后，再等回踩VWAP不破或放量站稳触发价，"
                f"失效 {f2(intra.radar_invalid_price(row))}。"
                if not market_gate_ok
                else (
                    f"{market_gate_reason}；{radar_display_action(row, gate_ok, gate_reason)}"
                    if market_gate_stage == "SECTOR_ROTATION_ALLOWED"
                    else radar_display_action(row, gate_ok, gate_reason)
                )
            ),
            "radar_gate_ok": gate_ok,
            "radar_gate_reason": gate_reason,
            "market_opportunity_gate_ok": market_gate_ok,
            "market_opportunity_gate_stage": market_gate_stage,
            "market_opportunity_gate_reason": market_gate_reason,
            "risk_bucket": row.get("risk_bucket") or global_risk.candidate_risk_bucket(row),
            "sector_momentum": row.get("sector_momentum") or {},
            "sector_rotation": row.get("sector_rotation") or {},
            "sector_emotion": (row.get("sector_momentum") or {}).get("reason") or "板块情绪待核验",
            "amount_ratio_1m": feat.get("amount_ratio_1m"),
            "amount_ratio_5m": feat.get("amount_ratio_5m"),
            "sector_volume": "{:.2f}/{:.2f}".format(
                float(feat.get("amount_ratio_1m") or 0), float(feat.get("amount_ratio_5m") or 0)
            ),
            "vwap_distance_pct": distance_pct,
            "execution_band_low": band_low,
            "execution_band_high": band_high,
        })
    return out


def radar_execution_width(price, atr5):
    return max(
        RADAR_SIM_MAX_VWAP_DISTANCE_ATR5 * (atr5 or 0),
        price * RADAR_SIM_MAX_VWAP_DISTANCE_PCT,
        5 * TICK,
    )


def radar_execution_band(row):
    q = row["quote"]
    feat = row.get("rt_features") or {}
    dyn = row.get("dynamic") or {}
    price = float(q.get("close") or 0)
    vwap = dyn.get("vwap") or feat.get("vwap") or price
    eps = feat.get("epsilon") or max(2 * TICK, price * 0.0008)
    atr1 = feat.get("atr1m") or 0
    low = ceil_to_tick(min(price, vwap + eps))
    high = ceil_to_tick(price + max(RADAR_SIM_SLIPPAGE_ROOM_PCT * price, 0.35 * atr1, 3 * TICK))
    return low, high


def radar_display_action(row, gate_ok, gate_reason):
    q = row["quote"]
    dyn = row.get("dynamic") or {}
    feat = row.get("rt_features") or {}
    vwap = dyn.get("vwap") or feat.get("vwap")
    trigger = intra.radar_trigger_price(row)
    invalid = intra.radar_invalid_price(row)
    if gate_ok:
        low, high = radar_execution_band(row)
        return (
            f"模拟盘可成交：执行区 {f2(low)}-{f2(high)}，只做计划内一档；"
            f"条件为回踩VWAP不破或OR15/修复位站稳，失效 {f2(invalid)}。"
        )
    return (
        f"候选观察，暂不模拟成交：{gate_reason}。"
        f"等待回踩VWAP {f2(vwap)} 不破或放量站稳 {f2(trigger)}，失效 {f2(invalid)}。"
    )


def radar_action_pattern(row, min_amount_ratio_1m=None, min_amount_ratio_5m=None):
    q = row["quote"]
    feat = row.get("rt_features") or {}
    dyn = row.get("dynamic") or {}
    price = q.get("close") or 0
    vwap = dyn.get("vwap") or feat.get("vwap")
    if not price or not vwap:
        return None, "分钟线/VWAP不可用"
    eps = feat.get("epsilon") or max(2 * TICK, price * 0.0008)
    amount_1m = feat.get("amount_ratio_1m") or 0
    amount_5m = feat.get("amount_ratio_5m") or 0
    min_amount_ratio_1m = (
        RADAR_SIM_MIN_AMOUNT_RATIO_1M if min_amount_ratio_1m is None else min_amount_ratio_1m
    )
    min_amount_ratio_5m = (
        RADAR_SIM_MIN_AMOUNT_RATIO_5M if min_amount_ratio_5m is None else min_amount_ratio_5m
    )
    if amount_1m < min_amount_ratio_1m or amount_5m < min_amount_ratio_5m:
        return None, (
            f"量能未达成交门槛：1m {amount_1m:.2f}/{min_amount_ratio_1m:.2f}，"
            f"5m {amount_5m:.2f}/{min_amount_ratio_5m:.2f}"
        )

    above_vwap_count = recent_count_above(feat, vwap, eps, n=3)
    pullback_reclaim = (
        recent_left_line(feat, vwap, eps, price, n=10)
        and recent_touched(feat, vwap, eps, n=6)
        and above_vwap_count >= 3
        and recent_reclaiming(feat, vwap, eps)
    )
    if pullback_reclaim:
        return "vwap_pullback_reclaim", "严格形态：先远离VWAP，再回踩触线，随后3/3分钟重新站上"

    or15_low = feat.get("or15_low")
    or15_mid = feat.get("or15_mid")
    or15_high = feat.get("or15_high")
    repair = row.get("repair") or intra.radar_trigger_price(row)
    reclaim_lines = [x for x in (or15_high, repair) if x]
    reclaim_ok = any(price >= line + eps and recent_count_above(feat, line, eps, n=3) >= 3 for line in reclaim_lines)
    or_structure_ok = (
        above_vwap_count >= 3
        and (not or15_low or price >= or15_low + eps)
        and (not or15_mid or price >= or15_mid + eps)
    )
    if reclaim_ok and or_structure_ok:
        return "or15_or_repair_reclaim", "严格形态：站回OR15/修复位并连续分钟确认，且未跌破OR结构"

    return None, "尚未形成VWAP回踩不破或OR15/修复位站稳的可成交形态"


def sixty_minute_structure_gate(feat, price):
    """Require a recoverable 60-minute structure, not a one-minute bounce."""
    minutes = int(feat.get("minutes") or 0)
    h60 = to_float(feat.get("h60"))
    l60 = to_float(feat.get("l60"))
    eps = to_float(feat.get("epsilon")) or max(2 * TICK, float(price or 0) * 0.0008)
    if minutes < 30 or h60 is None or l60 is None:
        return False, "60分钟窗口不足30分钟，暂不以早盘噪声开新仓"
    if h60 <= l60 + eps:
        return True, "60分钟区间收敛，交由VWAP/量能确认"
    reclaim = l60 + (h60 - l60) * 0.382
    if price < reclaim - eps:
        return False, f"未收回60分钟区间38.2%（{f2(reclaim)}）"
    return True, f"60分钟回撤后收回38.2%（{f2(reclaim)}）"


def radar_signal_blockers(row, now=None):
    q = row["quote"]
    feat = row.get("rt_features") or {}
    dyn = row.get("dynamic") or {}
    axes = row.get("axes") or intra.axis_profile(row)
    status = intra.radar_opportunity_status(row)
    price = q.get("close") or 0
    vwap = dyn.get("vwap") or feat.get("vwap")
    eps = feat.get("epsilon") or max(2 * TICK, price * 0.0008)
    atr5 = feat.get("atr5m") or 0
    amount_ratio_1m = feat.get("amount_ratio_1m") or 0
    amount_ratio_5m = feat.get("amount_ratio_5m") or 0
    blockers = []
    framework_validation = row.get("framework_validation") or {"valid": True}
    framework_context = row.get("framework_context") or {}
    evidence = row.get("theme_evidence") or q.get("theme_evidence") or {}
    if framework_validation.get("executable") is False:
        blockers.append("框架依据未完成可执行核验，禁止模拟交易")
    elif evidence and evidence.get("valid") is False:
        blockers.append("行业/名称未与当日板块形成可验证交集")
    blockers.extend(cycle_framework.execution_blockers(framework_context))
    if row.get("rotation_pilot"):
        rotation = row.get("sector_rotation") or {}
        if not rotation.get("sustained") or not rotation.get("leader_healthy"):
            blockers.append("盘中轮动试错未保持板块持续性与领涨承接，取消首笔")
    if row.get("tracked_candidate") and row.get("candidate_requires_fresh_source", True):
        source_age = row.get("candidate_source_age_sec")
        if isinstance(source_age, (int, float)) and source_age > RADAR_CANDIDATE_MAX_AGE_SECONDS:
            blockers.append(
                f"候选来源已过期：{source_age / 60:.0f}分钟 > {RADAR_CANDIDATE_MAX_AGE_SECONDS / 60:.0f}分钟"
            )
    if not is_attack_allowed(now or now_dt()):
        blockers.append("非连续竞价进攻时段")
    if not status.startswith("🟢"):
        blockers.append("全市场机会未达到绿色替代观察")
    if not vwap:
        blockers.append("分钟线/VWAP不可用")
    fresh, freshness_reason = minute_execution_freshness(feat, now=now)
    if not fresh:
        blockers.append(freshness_reason)
    cycle60_ok, cycle60_reason = sixty_minute_structure_gate(feat, price)
    if not cycle60_ok:
        blockers.append(cycle60_reason)
    if axes.get("position") != "green" or axes.get("odds") not in ("green", "yellow"):
        blockers.append("三轴位置或赔率未通过")
    pct_chg = q.get("pct", 0) or 0
    if pct_chg < 0 or pct_chg > 7.5:
        blockers.append("涨跌幅不适合模拟介入")
    if vwap and price < vwap + eps:
        blockers.append("未站上VWAP+buffer")
    if amount_ratio_1m < RADAR_SIM_MIN_AMOUNT_RATIO_1M or amount_ratio_5m < RADAR_SIM_MIN_AMOUNT_RATIO_5M:
        blockers.append(
            f"分钟量能未达模拟门槛：1m {amount_ratio_1m:.2f}/{RADAR_SIM_MIN_AMOUNT_RATIO_1M:.2f}，"
            f"5m {amount_ratio_5m:.2f}/{RADAR_SIM_MIN_AMOUNT_RATIO_5M:.2f}"
        )
    if rapid_rise(feat, price):
        blockers.append("急拉状态，不追价")
    chase_limit = radar_execution_width(price, atr5)
    if vwap and price > vwap + chase_limit:
        distance_pct = (price - vwap) / max(price, TICK) * 100
        limit_pct = chase_limit / max(price, TICK) * 100
        blockers.append(f"现价离VWAP过远：{distance_pct:.2f}% > {limit_pct:.2f}%")
    amount_yi = intra.amount_yi(q)
    if amount_yi < 8:
        blockers.append("成交额不足")
    pattern, pattern_reason = radar_action_pattern(row)
    duplicate_volume_blocker = (
        not pattern
        and pattern_reason.startswith("量能未达成交门槛")
        and any("分钟量能未达模拟门槛" in blocker for blocker in blockers)
    )
    if not pattern and not duplicate_volume_blocker:
        blockers.append(pattern_reason)
    return blockers


def radar_signal_gate(row, now=None):
    blockers = radar_signal_blockers(row, now=now)
    if blockers:
        return False, "；".join(blockers[:3])
    pattern, pattern_reason = radar_action_pattern(row)
    return True, f"全市场机会池：绿色替代观察，且{pattern_reason}，进入严格模拟成交"


def green_confirmation_gate(row, global_context=None, now=None):
    """Use a narrowly relaxed volume gate only on a green, confirmed market.

    This is not a global-risk bypass. It is for a candidate that already has
    VWAP/OR structure, price location, liquidity and risk/reward potential, but
    whose early-session volume profile has not yet reached the full-session
    threshold. The normal discipline layer still decides whether an order can
    be created.
    """
    now = now or now_dt()
    context = global_context or {}
    if str(context.get("risk_level") or "").lower() != "green":
        return False, "绿色确认通道仅适用于全球风险绿色"
    if not market_opportunity_buy_allowed(context, row=row, now=now):
        return False, global_risk.market_opportunity_gate_reason(context, row=row, now=now)
    if now.time() < (time_from_hhmm(RADAR_GREEN_CONFIRM_START) or dtime(9, 45)):
        return False, f"绿色确认需 {RADAR_GREEN_CONFIRM_START} 后连续竞价确认"
    q = row.get("quote") or {}
    feat = row.get("rt_features") or {}
    dyn = row.get("dynamic") or {}
    axes = row.get("axes") or intra.axis_profile(row)
    price = q.get("close") or 0
    vwap = dyn.get("vwap") or feat.get("vwap")
    eps = feat.get("epsilon") or max(2 * TICK, price * 0.0008)
    if not price or not vwap:
        return False, "分钟线/VWAP不可用"
    fresh, freshness_reason = minute_execution_freshness(feat, now=now)
    if not fresh:
        return False, freshness_reason
    framework_blockers = cycle_framework.execution_blockers(row.get("framework_context"))
    if framework_blockers:
        return False, "；".join(framework_blockers)
    cycle60_ok, cycle60_reason = sixty_minute_structure_gate(feat, price)
    if not cycle60_ok:
        return False, cycle60_reason
    if not str(intra.radar_opportunity_status(row)).startswith("🟢"):
        return False, "全市场机会质量未达到绿色替代观察"
    if axes.get("position") != "green" or axes.get("odds") not in ("green", "yellow"):
        return False, "三轴位置或赔率未通过"
    pct_chg = q.get("pct", 0) or 0
    if pct_chg < 0.5 or pct_chg > 7.5:
        return False, "涨跌幅不适合绿色确认介入"
    if price < vwap + eps:
        return False, "未站上VWAP+buffer"
    if rapid_rise(feat, price):
        return False, "急拉状态，不追价"
    chase_limit = radar_execution_width(price, feat.get("atr5m") or 0)
    if price > vwap + chase_limit:
        return False, f"现价离VWAP过远：{(price - vwap) / max(price, TICK) * 100:.2f}%"
    amount_1m = feat.get("amount_ratio_1m") or 0
    amount_5m = feat.get("amount_ratio_5m") or 0
    if amount_1m < RADAR_GREEN_CONFIRM_MIN_AMOUNT_RATIO_1M or amount_5m < RADAR_GREEN_CONFIRM_MIN_AMOUNT_RATIO_5M:
        return False, (
            f"绿色确认量能仍不足：1m {amount_1m:.2f}/{RADAR_GREEN_CONFIRM_MIN_AMOUNT_RATIO_1M:.2f}，"
            f"5m {amount_5m:.2f}/{RADAR_GREEN_CONFIRM_MIN_AMOUNT_RATIO_5M:.2f}"
        )
    if intra.amount_yi(q) < 8:
        return False, "成交额不足"
    pattern, pattern_reason = radar_action_pattern(
        row,
        min_amount_ratio_1m=RADAR_GREEN_CONFIRM_MIN_AMOUNT_RATIO_1M,
        min_amount_ratio_5m=RADAR_GREEN_CONFIRM_MIN_AMOUNT_RATIO_5M,
    )
    if not pattern:
        return False, pattern_reason
    return True, f"绿色市场本地确认：{pattern_reason}，量能达到早盘确认门槛"


def repair_continuation_gate(row, global_context=None, now=None):
    """Allow a repaired-market starter without bypassing local confirmation.

    This path exists for the case where overseas markets repair before the
    first A-share tick. It is deliberately narrower than a normal green-radar
    entry: no rapid rise, no weak volume, no low-quality axes, and only a
    modest distance from VWAP are accepted. Strategy-discipline still checks
    the final risk/reward and position sizing before any paper order.
    """
    now = now or now_dt()
    context = global_context or {}
    policy = context.get("policy") or {}
    path = context.get("intraday_path") or {}
    if not (path.get("preopen_repair") or path.get("recovery_confirmed")):
        return False, "未确认跨市场修复路径"
    if not market_opportunity_buy_allowed(context, row=row, now=now):
        return False, global_risk.market_opportunity_gate_reason(context, row=row, now=now)
    if policy.get("allow_repair_continuation", True) is False:
        return False, "当前风险政策不允许修复延续新增"
    repair_start = time_from_hhmm(policy.get("repair_continuation_start")) or dtime(9, 45)
    if now.time() < repair_start or not is_attack_allowed(now):
        return False, "修复延续需 09:45 后连续竞价确认"
    q = row.get("quote") or {}
    feat = row.get("rt_features") or {}
    dyn = row.get("dynamic") or {}
    axes = row.get("axes") or intra.axis_profile(row)
    price = q.get("close") or 0
    vwap = dyn.get("vwap") or feat.get("vwap")
    eps = feat.get("epsilon") or max(2 * TICK, price * 0.0008)
    if not price or not vwap:
        return False, "分钟线/VWAP不可用"
    fresh, freshness_reason = minute_execution_freshness(feat, now=now)
    if not fresh:
        return False, freshness_reason
    framework_blockers = cycle_framework.execution_blockers(row.get("framework_context"))
    if framework_blockers:
        return False, "；".join(framework_blockers)
    cycle60_ok, cycle60_reason = sixty_minute_structure_gate(feat, price)
    if not cycle60_ok:
        return False, cycle60_reason
    if not str(intra.radar_opportunity_status(row)).startswith("🟢"):
        return False, "全市场机会质量未达到绿色替代观察"
    if axes.get("position") != "green" or axes.get("odds") not in ("green", "yellow"):
        return False, "三轴位置或赔率未通过"
    pct_chg = q.get("pct", 0) or 0
    if pct_chg < 0.5 or pct_chg > 7.5:
        return False, "修复延续涨幅不在 0.5%-7.5% 区间"
    amount_1m = feat.get("amount_ratio_1m") or 0
    amount_5m = feat.get("amount_ratio_5m") or 0
    if amount_1m < RADAR_SIM_MIN_AMOUNT_RATIO_1M or amount_5m < RADAR_SIM_MIN_AMOUNT_RATIO_5M:
        return False, (
            f"修复延续量能不足：1m {amount_1m:.2f}/{RADAR_SIM_MIN_AMOUNT_RATIO_1M:.2f}，"
            f"5m {amount_5m:.2f}/{RADAR_SIM_MIN_AMOUNT_RATIO_5M:.2f}"
        )
    if rapid_rise(feat, price):
        return False, "修复延续仍处急拉，不追价"
    distance = price - vwap
    max_vwap_distance_pct = global_policy_value(
        context,
        "repair_continuation_max_vwap_distance_pct",
        RADAR_REPAIR_MAX_VWAP_DISTANCE_PCT,
    )
    max_distance = max(
        price * max_vwap_distance_pct,
        (feat.get("atr5m") or 0) * RADAR_REPAIR_MAX_VWAP_DISTANCE_ATR5,
        5 * TICK,
    )
    if distance < eps:
        return False, "尚未重新站上 VWAP+buffer"
    if distance > max_distance:
        return False, f"修复延续离VWAP过远：{distance / max(price, TICK) * 100:.2f}%"
    if intra.amount_yi(q) < 8:
        return False, "成交额不足"
    if recent_count_above(feat, vwap, eps, n=3) < 3:
        return False, "站上VWAP的连续分钟确认不足"
    return True, "跨市场修复已确认，个股放量站稳VWAP且未急拉，进入修复延续严格模拟"


def make_market_opportunity_signal(row, execution_mode="standard", gate_reason=None, now=None):
    raise RuntimeError("retired entrypoint: discovery rows require next-day three-method classification")

    # Historical implementation is intentionally unreachable. Keeping the
    # signature lets old imports fail explicitly instead of creating orders.
    q = row["quote"]
    feat = row.get("rt_features") or {}
    dyn = row.get("dynamic") or {}
    price = q["close"]
    vwap = dyn.get("vwap") or feat.get("vwap") or price
    eps = feat.get("epsilon") or max(2 * TICK, price * 0.0008)
    atr5 = feat.get("atr5m") or 0
    chase_limit = radar_execution_width(price, atr5)
    rapid_blocked = rapid_rise(feat, price)
    over_extended = bool(vwap and price > vwap + chase_limit)
    band_low, band_high = radar_execution_band(row)
    invalid_raw = intra.radar_invalid_price(row)
    invalid = floor_to_tick(min(invalid_raw or vwap, vwap - max(2 * eps, 0.25 * atr5, 3 * TICK)))
    standard_gate_reason = radar_signal_gate(row, now=now)[1]
    reason = gate_reason or standard_gate_reason
    pattern, pattern_reason = (
        radar_action_pattern(
            row,
            min_amount_ratio_1m=RADAR_GREEN_CONFIRM_MIN_AMOUNT_RATIO_1M,
            min_amount_ratio_5m=RADAR_GREEN_CONFIRM_MIN_AMOUNT_RATIO_5M,
        )
        if execution_mode == "green_confirmation"
        else radar_action_pattern(row)
    )
    repair_mode = execution_mode == "repair_continuation"
    action_basis = (
        "全球风险绿色且本地形态已确认：早盘量能达到宽松确认门槛；仍需策略纪律校验风险收益比和仓位，"
        "不因全球市场上涨单独买入。"
        if execution_mode == "green_confirmation"
        else (
            "跨市场修复延续：修复确认后只允许放量站稳VWAP、未急拉、距VWAP不过远的首笔模拟；"
            "仍需策略纪律校验风险收益比和仓位。"
            if repair_mode
            else (
                "全市场机会严格模拟：绿色替代观察；必须形成VWAP回踩不破或OR15/修复位站稳；"
                f"1m/5m量能不低于 {RADAR_SIM_MIN_AMOUNT_RATIO_1M:.2f}/{RADAR_SIM_MIN_AMOUNT_RATIO_5M:.2f}，且未过度远离VWAP"
            )
        )
    )
    confirm_rule = (
        "绿色市场本地确认：VWAP回踩/OR15修复形态 + 1m/5m量能至少 "
        f"{RADAR_GREEN_CONFIRM_MIN_AMOUNT_RATIO_1M:.2f}/{RADAR_GREEN_CONFIRM_MIN_AMOUNT_RATIO_5M:.2f} + 未急拉；"
        "跌破失效位或量价转弱取消。"
        if execution_mode == "green_confirmation"
        else (
            "修复后板块共振 + 放量站稳VWAP并连续3分钟至少2分钟在上方 + 未急拉；"
            "跌破失效位或修复路径转弱取消。"
            if repair_mode
            else "必须形成VWAP回踩不破或OR15/修复位站稳"
        )
    )
    return make_signal(
        row,
        "MARKET_OPPORTUNITY_ACTIONABLE",
        "P1",
        "立即处理",
        "FIRED",
        band_low,
        min(price, band_high),
        None,
        invalid,
        confirm_rule,
        f"跌破 {f2(invalid)}，或急拉远离VWAP，或板块/个股同步转弱",
        f"仅模拟盘按{float(row.get('entry_position_cap_pct') or 3.0):.1f}%首仓、确认后阶梯加仓观察成交；真实盘不自动执行。"
        f"模拟执行区 {f2(band_low)}-{f2(band_high)}，超过不追。",
        [
            reason,
            intra.axis_text(row),
            row.get("focus") or "全市场框架筛选",
            f"VWAP {f2(vwap)}｜距VWAP {((price - vwap) / max(price, TICK) * 100):.2f}%｜1m/5m量能比 {(feat.get('amount_ratio_1m') or 0):.2f}/{(feat.get('amount_ratio_5m') or 0):.2f}",
        ],
        {
            "source": "market_radar",
            "execution_mode": execution_mode,
            "radar_action_pattern": pattern,
            "signal_quality_gate": pattern or "strict_market_opportunity",
            "radar_pattern_reason": pattern_reason,
            "post_p0_reentry_ok": pattern == "or15_or_repair_reclaim",
            "candidate_source": row.get("candidate_source"),
            "candidate_source_age_sec": row.get("candidate_source_age_sec"),
            "candidate_source_fresh": row.get("candidate_source_fresh"),
            "tracked_candidate": row.get("tracked_candidate"),
            "rotation_pilot": row.get("rotation_pilot"),
            "entry_position_cap_pct": row.get("entry_position_cap_pct"),
            "sector_rotation": row.get("sector_rotation") or {},
            "execution_band_low": band_low,
            "execution_band_high": band_high,
            "max_chase_distance": chase_limit,
            "rapid_rise_blocked": rapid_blocked,
            "over_extension_blocked": over_extended,
            "gate_blockers": (
                radar_signal_blockers(row, now=now)
                if execution_mode != "green_confirmation"
                else [
                    f"绿色确认门槛：1m/5m量能至少 {RADAR_GREEN_CONFIRM_MIN_AMOUNT_RATIO_1M:.2f}/{RADAR_GREEN_CONFIRM_MIN_AMOUNT_RATIO_5M:.2f}",
                    "最终仍需盈亏比、仓位、T+1与模拟撮合纪律校验",
                ]
            ),
            "vwap_distance_pct": ((price - vwap) / max(price, TICK) * 100) if vwap else None,
            "execution_action": "SIM_BUY_OBSERVATION_ONLY",
            "confirm_rule": confirm_rule,
            "sim_reason": "全市场机会池只做模拟盘跟踪，不代表真实买入建议",
        },
    )


def assess_market_opportunity_technical(row, global_context=None, now=None):
    """Evaluate a radar candidate without deciding whether it may be executed."""
    ok, reason = radar_signal_gate(row, now=now)
    execution_mode = "standard"
    repair_reason = ""
    if not ok:
        green_ok, green_reason = green_confirmation_gate(
            row,
            global_context=global_context,
            now=now,
        )
        if green_ok:
            ok = True
            reason = green_reason
            execution_mode = "green_confirmation"
    if not ok:
        repair_ok, repair_reason = repair_continuation_gate(row, global_context=global_context, now=now)
        if repair_ok:
            ok = True
            reason = repair_reason
            execution_mode = "repair_continuation"
    return ok, repair_reason or reason, execution_mode


def evaluate_market_opportunity_signals(radar_rows, remaining_daily_slots, excluded_symbols=None, global_context=None, now=None, decision_con=None):
    """Retired entrypoint: discovery rows cannot create same-day buy authority."""
    return []

    # Historical implementation retained below for forensic comparison only.
    # Candidates may enter a future premarket plan after daily classification.
    if remaining_daily_slots <= 0:
        return []
    excluded_symbols = set(excluded_symbols or [])
    signals = []
    for row in radar_rows or []:
        if row.get("code") in excluded_symbols:
            record_market_decision(
                decision_con,
                row,
                "EXCLUDED_FILLED",
                "当日同一全市场机会已完成模拟买入，不重复开仓",
                now=now,
                extra={"risk_level": (global_context or {}).get("risk_level")},
            )
            continue
        record_market_decision(
            decision_con,
            row,
            "CANDIDATE",
            "进入全市场机会池，等待全局与本地技术门控复核",
            now=now,
            extra={"risk_level": (global_context or {}).get("risk_level")},
        )
        gate_allowed, gate_stage, gate_reason = global_risk.market_opportunity_gate_status(
            global_context, row=row, now=now
        )
        if not gate_allowed:
            shadow_ok, shadow_reason, execution_mode = assess_market_opportunity_technical(
                row, global_context=global_context, now=now
            )
            record_market_decision(
                decision_con,
                row,
                gate_stage,
                gate_reason,
                now=now,
                extra={
                    "risk_level": (global_context or {}).get("risk_level"),
                    "a_share_market_regime": (global_context or {}).get("a_share_market_regime"),
                    "shadow_technical_passed": shadow_ok,
                    "shadow_execution_mode": execution_mode,
                },
            )
            record_market_decision(
                decision_con,
                row,
                "SHADOW_TECHNICAL_READY" if shadow_ok else "SHADOW_TECHNICAL_BLOCKED",
                shadow_reason,
                now=now,
                extra={
                    "risk_level": (global_context or {}).get("risk_level"),
                    "a_share_market_regime": (global_context or {}).get("a_share_market_regime"),
                    "shadow_only": True,
                    "shadow_execution_mode": execution_mode,
                },
            )
            continue
        ok, reason, execution_mode = assess_market_opportunity_technical(
            row, global_context=global_context, now=now
        )
        if ok:
            record_market_decision(
                decision_con,
                row,
                "SIGNAL_READY",
                reason,
                now=now,
                extra={"risk_level": (global_context or {}).get("risk_level"), "execution_mode": execution_mode},
            )
            signals.append(make_market_opportunity_signal(row, execution_mode=execution_mode, gate_reason=reason, now=now))
        else:
            record_market_decision(
                decision_con,
                row,
                "TECHNICAL_BLOCKED",
                reason,
                now=now,
                extra={"risk_level": (global_context or {}).get("risk_level")},
            )
    signals.sort(key=signal_sort_key)
    limit = max(0, min(MAX_RADAR_SIM_SIGNALS_PER_TICK, remaining_daily_slots))
    return signals[:limit]


def build_overnight_risk_signals(rows, positions, account, global_context=None, now=None):
    """Reduce only the excess exposure before a red/yellow risk day closes."""
    context = global_context or {}
    level = context.get("risk_level") or "green"
    if level not in ("red", "yellow"):
        return []
    now = now or now_dt()
    policy = context.get("policy") or {}
    start = time_from_hhmm(policy.get("overnight_de_risk_start")) or dtime(14, 30)
    if now.time() < start or now.time() >= dtime(14, 57):
        return []
    try:
        total_assets = float(account.get("total_assets_estimate") or account.get("total_assets") or 0)
    except (TypeError, ValueError):
        total_assets = 0.0
    if total_assets <= 0:
        return []
    cap_pct = global_policy_value(policy_context := {"policy": policy}, "overnight_position_cap_pct", 3.0 if level == "red" else 6.0)
    signals = []
    position_map = positions or {}
    for row in rows or []:
        code = str(row.get("code") or "").strip()
        position = position_map.get(code) or {}
        try:
            quantity = int(position.get("quantity") or 0)
            sellable = int(position.get("sellable") or 0)
            price = float((row.get("quote") or {}).get("close") or 0)
        except (TypeError, ValueError):
            continue
        if not code or quantity <= 0 or sellable <= 0 or price <= 0:
            continue
        exposure_pct = quantity * price / total_assets * 100
        if exposure_pct <= cap_pct:
            continue
        reduce_fraction = min(1.0, max(0.0, (exposure_pct - cap_pct) / exposure_pct))
        defense = row.get("defense") or floor_to_tick(price * 0.975)
        signal = make_signal(
            row,
            "OVERNIGHT_RISK_REDUCE",
            "P0",
            "立即处理",
            "FIRED",
            floor_to_tick(price),
            None,
            price,
            floor_to_tick(defense),
            f"收盘前：全球{level}风险下，单票暴露 {exposure_pct:.2f}% 超过隔夜上限 {cap_pct:.2f}%，只减超额部分",
            f"风险等级降至绿色，或单票暴露回到 {cap_pct:.2f}% 以下；不因VWAP波动重复减仓",
            f"收盘前隔夜控制：按超额暴露比例减仓，保留不超过 {cap_pct:.2f}% 的单票仓位",
            [
                f"全球风险 {level}",
                f"单票暴露 {exposure_pct:.2f}% > 上限 {cap_pct:.2f}%",
                "控制隔夜跳空风险，不替代硬防守止损",
            ],
            {
                "paper_reduce_fraction": reduce_fraction,
                "execution_action": "OVERNIGHT_EXPOSURE_REDUCE",
                "reduce_policy": "只减超过全球风险等级对应隔夜上限的超额仓位",
                "overnight_cap_pct": cap_pct,
                "position_exposure_pct": exposure_pct,
                "global_risk_level": level,
            },
        )
        signals.append(signal)
    return signals


def filled_market_radar_symbols_today(trading_date):
    try:
        orders = paper_trading.load_orders(BASE_DIR, trading_date=trading_date, filled_only=True)
    except Exception:
        return set()
    return {
        order.get("symbol")
        for order in orders
        if order.get("scenario") == "MARKET_OPPORTUNITY_ACTIONABLE" and order.get("side") == "BUY"
    }


def process_state(con, signals, positions=None, names=None, price_map=None):
    pushes = []
    transitions = []
    for sig in signals:
        rating = opportunity_rating.rate_signal(sig)
        sig["opportunity_rating"] = rating
        sig["opportunity_score"] = rating["score"]
        sig["opportunity_grade"] = rating["grade"]
        sig["hard_veto"] = rating["hard_veto"]
        record_v2_decision(con, sig, rating, now=now_dt())
        paper_result = paper_trading.maybe_execute_signal(
            BASE_DIR,
            sig,
            positions or {},
            now_dt(),
            names=names or {},
            price_map=price_map,
        )
        # Order payloads retain the source signal for their own audit trail.
        # Storing that payload back on the same signal would create
        # signal -> paper_trade -> signal and make the event ledger fail to
        # serialize.  The dashboard and ledger only need the execution result.
        sig["paper_trade"] = compact_paper_trade(paper_result)
        tracking_events = signal_tracking.upsert_track(
            con,
            sig,
            rating,
            signal_tracking.states_for_tick(sig, rating, paper_result),
        )
        sig["tracking"] = signal_tracking.load_track(
            con, sig["trading_date"], sig["symbol"]
        )
        previous = db_row(con, sig["trading_date"], sig["symbol"], sig["scenario"])
        transition = transition_for(previous, sig)
        push = should_push(previous, sig, transition)
        if (
            not push
            and formal_entry_retriggered(tracking_events)
            and (sig.get("paper_trade") or {}).get("status") != "FILLED_ALREADY"
            and intra.is_formal_entry_reference(sig, now=now_dt())
        ):
            push = True
        if push:
            sig = with_cooldown(sig)
            pushes.append(sig)
        upsert_signal(con, sig, transition, pushed=push)
        if transition:
            transitions.append({**transition, "symbol": sig["symbol"], "scenario": sig["scenario"]})
        transitions.extend({**item, "source": "symbol_track"} for item in tracking_events)
    return pushes, transitions


def lark_text(text):
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def status_icon(signal):
    if signal["priority"] == "P0":
        return "🔴"
    if signal["priority"] == "P1":
        return "🟡"
    return "🟢"


def primary_instruction(signal):
    scenario = signal["scenario"]
    if observation_strategy_router.is_strategy_entry(scenario):
        contract = signal.get("strategy_contract") or {}
        name = contract.get("name") or signal.get("strategy_name") or SCENARIO_LABELS.get(scenario, scenario)
        return f"{name}：仅按盘前授权首笔试仓；离开执行区、量能不足或距VWAP过远立即取消。"
    if scenario in intraday_timing_v2.ENTRY_SCENARIOS:
        return "三策略首笔试仓：仅在盘前仓位授权内按动态执行区参与；离开执行区、量能不足或距VWAP过远立即取消。"
    if scenario == "P0_HARD_RISK_REDUCE":
        return "立即处理：停止加仓，硬防守破位清掉剩余可卖/降至0风险仓。"
    if scenario == "OVERNIGHT_RISK_REDUCE":
        return "收盘前隔夜控制：只减超过风险上限的暴露，不因软线波动反复交易。"
    if scenario == "PAPER_OPEN_RED_CARRY_DE_RISK":
        return "早盘红色门控减压：隔夜可卖仓确认多条参考线失守后先减约三分之一；后续硬破再处理，不在单笔低开时全卖。"
    if scenario == "SOFT_VWAP_BREAK":
        return "软风控：停止加仓；先观察反抽，跌向硬防守再减压。"
    if scenario == "HIGH_OPEN_ATTACK_CANCEL":
        return "取消进攻：当日不追高；已有仓位只看反抽减压或风险线。"
    if scenario == "VWAP_PULLBACK_ADD":
        return "可执行：只做计划内一档小仓；超过加仓区不追。"
    if scenario == "REPAIR_BREAKOUT_ADD":
        return "可执行：只做计划内一档；远离修复位则等回踩。"
    if scenario == "REPAIR_RECLAIM_ADD":
        return "可执行：高开原计划已取消；仅在新的OR15/VWAP修复确认后做一档，不恢复追高计划。"
    if scenario == "MARKET_OPPORTUNITY_ACTIONABLE":
        return "模拟盘：全市场替代机会只有严格技术形态成立才做计划内一档；真实盘不自动执行。"
    return "观察：只设提醒，不提前买卖。"


def price_instruction(signal):
    scenario = signal["scenario"]
    current = f2(signal.get("current_price"))
    trigger = f2(signal.get("trigger_price"))
    invalid = f2(signal.get("invalid_price"))
    if scenario in ("P0_HARD_RISK_REDUCE", "OVERNIGHT_RISK_REDUCE", "SOFT_VWAP_BREAK", "PAPER_OPEN_RED_CARRY_DE_RISK"):
        if scenario == "OVERNIGHT_RISK_REDUCE":
            return f"现价 {current}｜隔夜上限 {f2(signal.get('overnight_cap_pct'))}%｜当前单票暴露 {f2(signal.get('position_exposure_pct'))}%｜按超额比例减仓"
        if scenario == "SOFT_VWAP_BREAK":
            return f"现价 {current}｜软风控线 {trigger}｜恢复观察 {invalid}｜未跌硬防守前不当硬止损"
        if scenario == "PAPER_OPEN_RED_CARRY_DE_RISK":
            return f"现价 {current}｜早盘红门控触发｜先减约三分之一可卖仓｜恢复观察 {invalid}"
        return f"现价 {current}｜硬风控线 {trigger}｜执行口径：现价附近处理，不挂等价｜恢复观察 {invalid}"
    if scenario == "HIGH_OPEN_ATTACK_CANCEL":
        return f"现价 {current}｜取消线 {trigger}｜恢复观察需站回 {invalid}"
    if scenario in ("VWAP_PULLBACK_ADD", "REPAIR_BREAKOUT_ADD", "REPAIR_RECLAIM_ADD"):
        band_low = f2(signal.get("execution_band_low") or signal.get("add_price"))
        band_high = f2(signal.get("execution_band_high") or signal.get("add_price"))
        return f"现价 {current}｜确认触发 {trigger}｜计划加仓区 {band_low}-{band_high}｜失效 {invalid}"
    if scenario == "MARKET_OPPORTUNITY_ACTIONABLE":
        band_low = f2(signal.get("execution_band_low") or signal.get("add_price"))
        band_high = f2(signal.get("execution_band_high") or signal.get("add_price"))
        return f"现价 {current}｜模拟触发 {trigger}｜模拟执行区 {band_low}-{band_high}｜失效 {invalid}"
    if scenario in intraday_timing_v2.ENTRY_SCENARIOS:
        contract = signal.get("signal_contract") or {}
        return (
            f"现价 {current}｜策略执行区 {f2(contract.get('exec_low'))}-{f2(contract.get('exec_high'))}｜"
            f"失效 {f2(contract.get('invalid_price') or invalid)}"
        )
    return f"现价 {current}｜关键位 {invalid}｜等待穿越与量能确认"


def signal_brief(signal):
    label = SCENARIO_LABELS.get(signal["scenario"], signal["scenario"])
    reasons = "；".join((signal.get("reasons") or [])[:3]) or "-"
    paper = signal.get("paper_trade") or {}
    paper_line = ""
    if paper:
        if paper.get("status") in ("FILLED", "PARTIAL_FILLED"):
            paper_line = "\n**模拟盘**：已触发模拟成交，成交确认卡单独推送"
        elif paper.get("status") == "FILLED_ALREADY":
            paper_line = "\n**模拟盘**：今日同一信号已成交，后续不重复提醒"
        elif paper.get("status") not in ("NO_ORDER", None):
            paper_line = f"\n**模拟盘**：未成交｜{paper.get('reason')}"
    return (
        f"{status_icon(signal)} **{signal['priority']}｜{signal['external_status']}｜{signal['name']}({signal['symbol']})**\n"
        f"**动作**：{primary_instruction(signal)}\n"
        f"**价格**：{price_instruction(signal)}\n"
        f"**场景**：{label}\n"
        f"**依据**：{reasons}\n"
        f"**取消/恢复**：{signal.get('cancel_rule')}"
        f"{paper_line}"
    )


def formal_entry_signal_brief(signal):
    contract = signal.get("signal_contract") or {}
    timing = signal.get("timing_v2") or {}
    levels = timing.get("levels") or {}
    strategy = signal.get("strategy_contract") or {}
    method_text = strategy.get('name') or signal.get('strategy_name') or SCENARIO_LABELS.get(signal.get('scenario'), '未提供')
    stock_type = '情绪龙头候选' if strategy.get('key') == 'LEADER_EMOTION' else '趋势票'
    sector = signal.get('sector_momentum') or {}
    closed = timing.get('source_bar_close') or {}
    quality = timing.get('entry_quality') or {}
    guard = contract.get('confirmation_guard') or {}
    target_source = (quality.get('target_evidence') or {}).get('source')
    source_text = {'planned_pressure': '计划压力', 'execution_pressure': '执行压力',
                   'trend_pressure': '趋势压力', 'daily_resistance': '日线阻力',
                   'method_target': '方法结构目标', 'limit_up': '涨停边界'}.get(target_source, '见结构证据')
    identity = quality.get('leader_identity') or {}
    identity_text = {'SAMPLE_LEADER': '当日板块样本领涨', 'LEADING_GROUP': '当日板块样本领先组',
                     'POPULAR_CANDIDATE': '仅人气候选，未确认领导力'}.get(identity.get('role'))
    quality_text = (f"- 本票地位：{identity_text}（不是全市场排名）\n" if identity_text else '')
    if guard:
        quality_text += f"- 确认保护线：{f2(guard.get('price_floor'))}，跌破即取消买点；下一根闭合5分钟重新评估。\n"
    paper = signal.get('paper_trade') or {}
    paper_state = ('模拟已成交（详见成交回执）' if paper.get('status') in {'FILLED', 'PARTIAL_FILLED', 'FILLED_ALREADY'}
                   else '尚未确认模拟成交；信号不等于成交')
    return (
        f"🟢 **绿色正式买入信号｜立即处理｜{signal.get('name')}({signal.get('symbol')})**\n"
        f"- 证据评分：{signal.get('opportunity_grade')}级 {signal.get('opportunity_score')}分（非胜率）｜"
        f"分类：{stock_type}｜策略：{method_text}\n"
        f"- 当日共振：{sector.get('board_name') or '见策略证据'}｜板块涨幅 {f2(sector.get('board_pct'))}%｜"
        f"闭合5分钟依据 {closed.get('5m') or '-'}\n"
        f"- 买入参考区间：**{f2(contract.get('exec_low'))}～{f2(contract.get('exec_high'))}**｜"
        f"现价 {f2(signal.get('current_price'))}｜试仓上限 {intra.position_cap_text(contract.get('position_cap_pct'))}\n"
        f"- 入场失效：**{f2(contract.get('invalid_price') or levels.get('entry_invalidation'))}**｜"
        f"第一目标 {f2(contract.get('target_price') or levels.get('nearest_resistance'))}｜"
        f"成本后RR {f2(contract.get('net_reward_risk'))}\n"
        f"- 目标依据：{source_text}；参考失效位不保证T+1期间可卖出。\n"
        f"{quality_text}"
        f"- 有效至：**{contract.get('expires_at') or '-'}**｜信号编号 {contract.get('contract_id') or signal.get('signal_id') or '-'}\n"
        f"- 执行状态：{paper_state}；真实账户不自动下单。\n"
        "- 取消：超出执行区、跌破失效位、板块共振失效或到期，均停止参考本信号；不追价。"
    )


def is_risk_sell_alert(signal):
    contract = signal.get("signal_contract") or {}
    return (
        signal.get("external_status") == "立即处理"
        and signal.get("priority") == "P0"
        and (contract.get("side") == "SELL" or signal.get("scenario") in ("P0_HARD_RISK_REDUCE", "P0_HARD_BREAK", "OVERNIGHT_RISK_REDUCE"))
    )


def load_risk_sell_push_state():
    try:
        return json.loads(RISK_SELL_PUSH_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_risk_sell_push_state(state):
    ensure_dirs()
    RISK_SELL_PUSH_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def risk_sell_state_key(signal):
    return "|".join([
        str(signal.get("trading_date") or intra.REPORT_DATE),
        str(signal.get("symbol") or ""),
        str(signal.get("scenario") or ""),
        str(signal.get("fingerprint") or ""),
    ])


def risk_sell_should_push(signal, state):
    key = risk_sell_state_key(signal)
    old = state.get(key) or {}
    last_ts = parse_dt(old.get("last_push_ts"))
    current = signal.get("current_price")
    last_price = old.get("last_price")
    if not last_ts:
        return True, key
    if (now_dt() - last_ts).total_seconds() < P0_REPEAT_SECONDS:
        return False, key
    try:
        current = float(current)
        last_price = float(last_price)
    except Exception:
        return False, key
    if current <= last_price * (1 - P0_WORSEN_PCT):
        return True, key
    return False, key


def mark_risk_sell_pushed(signals, state):
    ts = now_dt().strftime("%Y-%m-%d %H:%M:%S")
    for signal in signals:
        key = risk_sell_state_key(signal)
        state[key] = {
            "last_push_ts": ts,
            "last_price": signal.get("current_price"),
            "symbol": signal.get("symbol"),
            "name": signal.get("name"),
            "scenario": signal.get("scenario"),
            "fingerprint": signal.get("fingerprint"),
        }
    save_risk_sell_push_state(state)


def send_feishu_risk_sells(signals, health):
    if os.environ.get("A_SHARE_SKIP_FEISHU") == "1" or not RISK_SELL_FEISHU_ENABLED:
        return json.dumps({"code": 0, "msg": "risk sell feishu skipped"}, ensure_ascii=False)
    state = load_risk_sell_push_state()
    candidates = []
    for signal in select_primary_signals(signals):
        if not is_risk_sell_alert(signal):
            continue
        should_send, _ = risk_sell_should_push(signal, state)
        if should_send:
            candidates.append(signal)
    if not candidates:
        return json.dumps({"code": 0, "msg": "skipped or no risk sell alerts"}, ensure_ascii=False)
    now = now_dt()
    lines = [signal_brief(s) for s in candidates[:6]]
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": "red", "title": {"tag": "plain_text", "content": f"A股持仓风险卖出提醒｜{now:%H:%M:%S}"}},
            "elements": [
                lark_text(
                    f"**P0 风险卖出提醒 {len(candidates)} 只。**\n"
                    f"即使本地模拟盘因无仓位/T+1拒绝成交，也按持仓风险提示推送；请人工核对真实持仓和可卖数量。"
                    f"\n行情延迟 {health.get('quote_delay_sec', '未知')}秒｜状态 {health.get('engine_status')}"
                ),
                {"tag": "hr"},
                lark_text("\n\n".join(lines)),
                {"tag": "hr"},
                lark_text("执行口径：这是风险提醒，不是自动下单。若真实有仓且可卖，优先处理 P0；若无仓，忽略成交动作但保留风险记录。仅为交易计划参考，不构成投资建议。"),
            ],
        },
    }
    data = json.dumps(card, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(intra.FEISHU_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = resp.read().decode("utf-8", "ignore")
    mark_risk_sell_pushed(candidates, state)
    return result


def apply_runtime_quality(health):
    issues = []
    if (health.get("preopen_quality_gate") or {}).get("allowed") is False:
        issues.append("PREOPEN_PLAN_BLOCKED")
    if (health.get("storage") or {}).get("ready") is False:
        issues.append("STORAGE_NOT_READY")
    if (health.get("leader_pool") or {}).get("ready") is False:
        issues.append("LEADER_POOL_STALE")
    if (health.get("market_breadth") or {}).get("coverage_ready") is False:
        issues.append("MARKET_BREADTH_INCOMPLETE")
    if (health.get("ledger_quality") or {}).get("ready") is False:
        issues.append("PAPER_LEDGER_REVIEW_REQUIRED")
    revalidation = health.get("daily_contract_revalidation") or {}
    if revalidation.get("active_pending_retry", revalidation.get("pending_retry")):
        issues.append("DAILY_CONTRACT_INCOMPLETE")
    warnings = ["PREMARKET_MAINLINE_REVIEW_REQUIRED"] if revalidation.get("mainline_pending") else []
    if revalidation.get("nontrading_pending"):
        warnings.append("NONTRADING_STOCK_QUARANTINED")
    if health.get("paper_event_warning"):
        warnings.append("PAPER_EVENT_MIRROR_PENDING")
    if (health.get("leader_pool") or {}).get("kaipanla_ready") is False:
        warnings.append("KAIPANLA_CROSSCHECK_UNAVAILABLE")
    health["new_entry_status"] = "blocked" if any(
        key in issues for key in ("PREOPEN_PLAN_BLOCKED", "STORAGE_NOT_READY", "PAPER_LEDGER_REVIEW_REQUIRED")
    ) else "conditional"
    context = health.get('global_risk') or {}
    risk_blocked = (context.get('policy') or {}).get('allow_core_attack_buy') is False
    local_data_blocked = context.get('entry_risk_source') == 'a_share_data_unready'
    health['entry_permission'] = {
        'state': 'dependency_blocked' if health['new_entry_status'] == 'blocked' or local_data_blocked else 'risk_blocked' if risk_blocked else 'conditional',
        'reason': ('关键执行依赖未就绪' if health['new_entry_status'] == 'blocked' else
                   (context.get('policy') or {}).get('notes', '全局风险禁止新增仓；不是缺少个股买点') if risk_blocked else '仅允许逐股通过策略与执行检查，不是买入信号'),
        'risk_level': context.get('risk_level'), 'risk_scope': context.get('risk_scope'),
        'risk_source': context.get('entry_risk_source'),
        'a_share_evidence': context.get('a_share_entry_evidence'),
        'release_condition': '关键依赖恢复、实时风险许可通过，并重新确认当日板块与策略买点；旧卡不自动恢复有效',
    }
    if local_data_blocked:
        health['new_entry_status'] = 'blocked'
    elif risk_blocked:
        health['new_entry_status'] = 'risk_blocked'
    health.update(quality_issues=issues, quality_warnings=warnings,
                  engine_status="degraded" if issues else "ok")
    return ",".join(issues) or None


def engine_health_alert_content(action, state, health):
    """Keep data degradation distinct from an exception that stops a tick."""
    now = now_dt()
    if action == "error" and state.get("incident_kind") == "data_quality":
        breadth = health.get("market_breadth") or {}
        detail = state.get("error_fingerprint") or "关键数据待恢复"
        if "MARKET_BREADTH_INCOMPLETE" in detail and breadth.get("reason"):
            detail += "；" + breadth["reason"]
        daily = health.get("daily_contract_revalidation") or {}
        if "DAILY_CONTRACT_INCOMPLETE" in detail:
            detail += (f"；日线待重验 {daily.get('active_pending_retry', daily.get('pending_retry', '?'))}"
                       f"/{daily.get('targets', '?')}只；" + "；".join(daily.get("errors") or []))
        return f"A股盯盘数据降级｜{now:%H:%M:%S}", "orange", (
            f"**盯盘仍在运行，关键数据已连续 {state.get('consecutive_errors', 0)} 轮未通过质检。**\n"
            f"原因：{detail}\n"
            "影响：依赖缺失数据的交易条件不能放行；个股观察继续。系统自动重试，不使用旧数据冒充实时确认。"
        )
    if action == "error":
        return f"A股盯盘中断告警｜{now:%H:%M:%S}", "red", (
            f"**实时盯盘已连续失败 {state.get('consecutive_errors', 0)} 轮，本轮信号更新未完成。**\n"
            f"错误：`{state.get('error_fingerprint') or 'unknown'}`\n"
            "处理：系统会继续重试；若告警持续，请检查实时引擎日志与行情源。"
        )
    pending = (health.get("daily_contract_revalidation") or {}).get("mainline_pending") or []
    note = f"另有{len(pending)}只候选主线待核对，仍禁止新增仓。" if pending else ""
    isolated = (health.get("daily_contract_revalidation") or {}).get("nontrading_pending") or {}
    if isolated:
        note += "无成交隔离观察、仍禁止新增仓：" + "、".join(
            f"{item.get('name') or code}({code})" for code, item in isolated.items()) + "。"
    return f"A股盯盘恢复｜{now:%H:%M:%S}", "blue", (
        "**本次运行或数据异常已恢复。** 后续信号仍需通过板块共振、闭合K线和策略合同；"
        "不补造故障期间的信号，也不代表产生买入信号。" + note
    )


def send_feishu_engine_health_alert(action, state, health):
    """Notify an actual blind spot without turning routine WAIT states into noise."""
    if os.environ.get("A_SHARE_SKIP_FEISHU") == "1" or not ENGINE_HEALTH_FEISHU_ENABLED:
        return json.dumps({"code": 0, "msg": "engine health feishu skipped"}, ensure_ascii=False)
    if action not in {"error", "recovery"}:
        return json.dumps({"code": 0, "msg": "no engine health alert"}, ensure_ascii=False)
    title, template, summary = engine_health_alert_content(action, state, health)
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
            "elements": [
                lark_text(summary),
                {"tag": "hr"},
                lark_text(
                    f"引擎状态：{health.get('engine_status')}｜"
                    f"最近成功：{health.get('last_success_at') or '暂无'}｜"
                    f"分钟线：{health.get('minute_bar_latest_time') or '暂无'}"
                ),
                {"tag": "hr"},
                lark_text("这是一条运行健康提醒，不是交易信号，也不代表买入或卖出。仅为策略研究，不构成投资建议。"),
            ],
        },
    }
    data = json.dumps(card, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(intra.FEISHU_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", "ignore")


def send_feishu(signals, health):
    if os.environ.get("A_SHARE_SKIP_FEISHU") == "1" or not signals:
        return json.dumps({"code": 0, "msg": "skipped or no signals"}, ensure_ascii=False)
    if not REALTIME_SIGNAL_FEISHU_ENABLED:
        return json.dumps({"code": 0, "msg": "realtime signal feishu disabled; dashboard only"}, ensure_ascii=False)
    if (health.get('new_entry_status') in {'blocked', 'risk_blocked'}
            or ((health.get('global_risk') or {}).get('policy') or {}).get('allow_core_attack_buy') is False):
        return json.dumps({'code': 0, 'msg': 'no buy reference while entry permission is blocked'})
    now = now_dt()
    signals = [
        signal for signal in select_primary_signals(signals)
        if intra.is_formal_entry_reference(signal, now=now)
        and (signal.get("paper_trade") or {}).get("status") not in {"REJECTED", "REJECTED_ALREADY"}
    ][:6]
    if not signals:
        return json.dumps({"code": 0, "msg": "no formal order references after execution audit"}, ensure_ascii=False)
    lines = [formal_entry_signal_brief(s) for s in signals]
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": "green", "title": {"tag": "plain_text", "content": f"A股订盘引擎｜绿色正式买入信号｜{now:%H:%M:%S}"}},
            "elements": [
                lark_text(
                    f"**本轮绿色正式买入信号 {len(signals)}只。** 已同时通过策略合同、A/B评级、闭合K线时机证据、成本后RR、执行区和有效期检查。\n"
                    f"行情延迟 {health.get('quote_delay_sec', '未知')}秒｜状态 {health.get('engine_status')}"
                ),
                {"tag": "hr"},
                lark_text("\n\n".join(lines)),
                {"tag": "hr"},
                lark_text("唯一口径：只有标题为“绿色正式买入信号”的即时卡可参考开仓。观察池标的以龙头、520或五日线策略名为准；V2仅展示时机证据。评级升级、已发现、接近触发、解除门控和整点观察均不是买入信号。仅为策略研究，不构成投资建议。"),
            ],
        },
    }
    data = json.dumps(card, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(intra.FEISHU_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", "ignore")


def critical_watch_candidates(signals, limit=CRITICAL_WATCH_MAX_CANDIDATES):
    """Return truly near-trigger waits without weakening the green contract.

    Yellow alerts are reserved for a completed 15m setup with exactly one
    retryable 5m or volume gap.  Daily/weekly qualification, strategy routing,
    sector resonance and other structural work belong in scheduled reports,
    not in an interruptive intraday alert.
    """
    waits = []
    for signal in select_primary_signals(signals):
        if signal.get("scenario") != "V2_WAIT":
            continue
        if intra.signal_tracking_state(signal) != "NEAR_TRIGGER":
            continue
        if signal.get("hard_veto"):
            continue
        strategy = signal.get("strategy_contract") or {}
        if strategy.get("is_observation_strategy"):
            if not signal.get("strategy_daily_qualified") or not signal.get("sector_resonance_ok"):
                continue
            timing = signal.get("timing_v2") or {}
            candidate_pattern = timing.get("candidate_entry_pattern")
            allowed = set(strategy.get("allowed_patterns") or [])
            if not candidate_pattern or (allowed and candidate_pattern not in allowed):
                continue
        if str(strategy.get("key") or "") == observation_strategy_router.OBSERVE:
            continue
        if not signal.get("premarket_plan_allows_entry") or not signal.get("premarket_plan_complete"):
            continue
        if str(signal.get("opportunity_grade") or "").upper() not in {"A", "B"}:
            continue
        active = []
        seen = set()
        for item in intra.signal_blocker_diagnostics(signal):
            category = intra.wait_blocker_category(item.get("reason"))
            key = (category, str(item.get("reason") or ""))
            if key not in seen:
                active.append((category, item))
                seen.add(key)
        if len(active) != 1:
            continue
        category, item = active[0]
        if (
            category not in {"execution", "volume"}
            or str(item.get("severity") or "").upper() == "HARD"
            or item.get("retryable") is False
        ):
            continue
        waits.append(signal)
    waits.sort(key=lambda signal: (-intra.waiting_signal_score(signal), str(signal.get("symbol") or "")))
    return waits[:limit]


def critical_watch_fingerprint(signal):
    timing = signal.get("timing_v2") or {}
    gap_categories = sorted({
        intra.wait_blocker_category(item.get("reason"))
        for item in intra.signal_blocker_diagnostics(signal)
        if intra.wait_blocker_category(item.get("reason")) != "time"
    })
    payload = {
        "symbol": signal.get("symbol"),
        "scenario": signal.get("scenario"),
        "grade": signal.get("opportunity_grade"),
        "strategy": signal.get("strategy_key"),
        "tracking": (signal.get("tracking") or {}).get("state"),
        "regime": timing.get("regime"),
        "location": timing.get("location"),
        "setup_15m": timing.get("setup_15m"),
        "execution_5m": timing.get("execution_5m"),
        "candidate_entry_pattern": timing.get("candidate_entry_pattern"),
        "gap_categories": gap_categories,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def load_critical_watch_push_state():
    try:
        return json.loads(CRITICAL_WATCH_PUSH_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_critical_watch_push_state(state):
    ensure_dirs()
    CRITICAL_WATCH_PUSH_STATE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def critical_watch_to_send(candidates, state, now):
    """Batch stable yellow candidates behind persistence and global cooldown."""
    state = dict(state or {})
    trading_date = now.strftime("%Y-%m-%d")
    if state.get("trading_date") != trading_date:
        state = {"trading_date": trading_date, "candidates": {}, "last_global_sent_at": None}
    previous = state.setdefault("candidates", {})
    current_keys = {str(signal.get("symbol") or "") for signal in candidates}
    for key, old in previous.items():
        if key not in current_keys and isinstance(old, dict):
            old["active"] = False

    last_global = parse_dt(state.get("last_global_sent_at"))
    if last_global is None:
        # Migrate the old per-symbol state without causing an alert burst on
        # the first restart after this release.
        sent_times = [
            parse_dt(item.get("last_sent_at"))
            for item in previous.values()
            if isinstance(item, dict) and item.get("last_sent_at")
        ]
        sent_times = [item for item in sent_times if item is not None]
        last_global = max(sent_times, default=None)
        if last_global:
            state["last_global_sent_at"] = last_global.strftime("%Y-%m-%d %H:%M:%S")

    selected = []
    for signal in candidates:
        key = str(signal.get("symbol") or "")
        fingerprint = critical_watch_fingerprint(signal)
        old = previous.get(key) or {}
        last_sent = parse_dt(old.get("last_sent_at"))
        last_sent_fingerprint = old.get("last_sent_fingerprint")
        if last_sent and last_sent_fingerprint is None:
            last_sent_fingerprint = old.get("fingerprint")
        changed = old.get("fingerprint") != fingerprint or old.get("active") is False
        first_eligible = parse_dt(old.get("first_eligible_at")) if not changed else None
        first_eligible = first_eligible or now
        previous[key] = {
            **old,
            "fingerprint": fingerprint,
            "first_eligible_at": first_eligible.strftime("%Y-%m-%d %H:%M:%S"),
            "last_seen_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "name": signal.get("name"),
            "active": True,
            "last_sent_fingerprint": last_sent_fingerprint,
        }
        cooled_down = bool(
            last_sent and (now - last_sent).total_seconds() < CRITICAL_WATCH_COOLDOWN_SECONDS
        )
        persisted = (now - first_eligible).total_seconds() >= CRITICAL_WATCH_MIN_PERSISTENCE_SECONDS
        materially_unsent = last_sent_fingerprint != fingerprint
        if persisted and not cooled_down and (not last_sent or materially_unsent):
            selected.append(signal)
    globally_cooled = bool(
        last_global and (now - last_global).total_seconds() < CRITICAL_WATCH_GLOBAL_COOLDOWN_SECONDS
    )
    return ([] if globally_cooled else selected[:CRITICAL_WATCH_MAX_CANDIDATES]), state


def critical_watch_signal_brief(signal):
    timing = signal.get("timing_v2") or {}
    strategy = signal.get("strategy_contract") or {}
    sector = signal.get("sector_resonance_reason") or "板块共振待下一次刷新确认"
    candidate_pattern = timing.get("candidate_entry_pattern") or timing.get("setup_15m") or "-"
    if strategy.get("is_observation_strategy"):
        upstream = (
            f"盘前仓位授权 + {strategy.get('name') or '观察池策略'}日线资格 + "
            f"盘中板块共振 + 所属策略买点结构"
        )
    else:
        upstream = (
            f"核心池盘前计划 + 120m {timing.get('regime') or '-'}结构 + "
            f"市场/板块门控 + 15m {candidate_pattern}"
        )
    return (
        f"**{signal.get('name')}({signal.get('symbol')})｜"
        f"{signal.get('opportunity_grade')}级 {signal.get('opportunity_score')}分｜"
        f"{strategy.get('name') or signal.get('strategy_name') or '待确认策略'}**\n"
        f"- 当前：{f2(signal.get('current_price'))}｜120m {timing.get('regime') or '-'} / "
        f"15m {timing.get('setup_15m') or '-'} / 5m {timing.get('execution_5m') or '-'}\n"
        f"- 已通过：{upstream}\n"
        f"- 下一关：**{intra.signal_next_condition(signal)}**\n"
        f"- 板块：{sector}\n"
        "- 纪律：这是临界观察，尚未生成订单；仅收到绿色正式买入信号后才可按执行区试仓。"
    )


def send_feishu_critical_watch(candidates, health):
    """Deliver a compact yellow card for a newly near-ready strategy setup."""
    if os.environ.get("A_SHARE_SKIP_FEISHU") == "1" or not CRITICAL_WATCH_FEISHU_ENABLED:
        return json.dumps({"code": 0, "msg": "critical watch feishu disabled"}, ensure_ascii=False)
    now = now_dt()
    state = load_critical_watch_push_state()
    selected, state = critical_watch_to_send(candidates or [], state, now)
    # Persist first-seen/last-seen state even when no card is sent; otherwise
    # the persistence timer would restart on every 5-second loop.
    save_critical_watch_push_state(state)
    if not candidates:
        return json.dumps({"code": 0, "msg": "no critical watch candidates"}, ensure_ascii=False)
    if not selected:
        return json.dumps({"code": 0, "msg": "critical watch persistence/cooldown active"}, ensure_ascii=False)
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": "yellow", "title": {"tag": "plain_text", "content": f"A股订盘引擎｜临界策略观察｜禁止下单｜{now:%H:%M:%S}"}},
            "elements": [
                lark_text(
                    f"**{len(selected)}只标的持续进入临界观察。** 每只均已形成所属方法要求的买点结构，"
                    "上游资格按逐标的“已通过”说明；目前仅剩1项5m执行或量能确认。本卡不是买入指令。\n"
                    f"行情延迟 {health.get('quote_delay_sec', '未知')}秒｜状态 {health.get('engine_status')}"
                ),
                {"tag": "hr"},
                lark_text("\n\n".join(critical_watch_signal_brief(signal) for signal in selected[:CRITICAL_WATCH_MAX_CANDIDATES])),
                {"tag": "hr"},
                lark_text("唯一开仓口径：黄色卡只说明“值得继续盯”，不产生模拟订单；只有绿色正式买入信号同时给出执行区、失效位、目标位和有效期，才可参考开仓。仅为策略研究，不构成投资建议。"),
            ],
        },
    }
    data = json.dumps(card, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(intra.FEISHU_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = resp.read().decode("utf-8", "ignore")
    sent_at = now.strftime("%Y-%m-%d %H:%M:%S")
    for signal in selected:
        key = str(signal.get("symbol") or "")
        state["candidates"][key]["last_sent_at"] = sent_at
        state["candidates"][key]["last_sent_fingerprint"] = critical_watch_fingerprint(signal)
    state["last_global_sent_at"] = sent_at
    save_critical_watch_push_state(state)
    return result


def send_feishu_shadow_research(now):
    if os.environ.get('A_SHARE_SKIP_FEISHU') == '1':
        return {'status': 'disabled', 'sent': 0}
    from core import shadow_research_reporting

    def sender(card):
        request = urllib.request.Request(intra.FEISHU_WEBHOOK,
            data=json.dumps(card, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read().decode('utf-8')

    try:
        return shadow_research_reporting.notify(BASE_DIR, now, sender)
    except Exception as exc:
        return {'status': 'error', 'error': type(exc).__name__, 'sent': 0,
                'execution_affected': False}


def paper_side_label(side):
    return {"BUY": "买入", "SELL": "卖出", "CANCEL": "取消"}.get(str(side or ""), str(side or "-"))


def paper_fill_brief(signal):
    paper = signal.get("paper_trade") or {}
    source = "全市场机会池" if signal.get("source") == "market_radar" else "自选/持仓信号"
    label = SCENARIO_LABELS.get(signal.get("scenario"), signal.get("scenario"))
    side = paper_side_label(paper.get("side"))
    status = "部分成交" if paper.get("status") == "PARTIAL_FILLED" else "已成交"
    t1 = "\n- T+1：今日模拟买入不计入今日可卖，下一交易日才结转可卖。" if paper.get("side") == "BUY" else ""
    discipline = paper.get("discipline_check") or {}
    rationale = paper.get("strategy_rationale") or {}
    basis = "；".join((rationale.get("action_basis") or [])[:2]) or "订单依据已写入本地台账"
    discipline_line = discipline.get("summary") or "纪律检查已写入本地台账"
    boundary_label = "执行上限" if paper.get("side") == "BUY" else "保护线"
    return (
        f"**{status}｜{source}｜{signal.get('name')}({signal.get('symbol')})**\n"
        f"- 方向：{side}｜数量：{paper.get('qty')}股｜成交价：{f2(paper.get('fill_price'))}\n"
        f"- 模拟费用：{f2(paper.get('fees_total', 0))}元｜佣金为研究假设，滑点已计入成交价\n"
        f"- 信号价：{f2(paper.get('signal_price') or signal.get('current_price'))}｜{boundary_label}：{f2(paper.get('limit_price'))}｜失效价：{f2(signal.get('invalid_price'))}\n"
        f"- 场景：{label}｜原因：{paper.get('reason') or '-'}\n"
        f"- 纪律：{discipline_line}\n"
        f"- 依据：{basis}{t1}"
    )


def send_feishu_paper_fills(signals, health):
    fills = [s for s in signals if (s.get("paper_trade") or {}).get("status") in ("FILLED", "PARTIAL_FILLED")]
    if os.environ.get("A_SHARE_SKIP_FEISHU") == "1" or not fills:
        return json.dumps({"code": 0, "msg": "skipped or no paper fills"}, ensure_ascii=False)
    now = now_dt()
    buy_count = sum(1 for s in fills if (s.get("paper_trade") or {}).get("side") == "BUY")
    sell_count = sum(1 for s in fills if (s.get("paper_trade") or {}).get("side") == "SELL")
    radar_count = sum(1 for s in fills if s.get("source") == "market_radar")
    header_template = "red" if buy_count else "green"
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": header_template, "title": {"tag": "plain_text", "content": f"A股模拟盘成交｜{now:%H:%M:%S}"}},
            "elements": [
                lark_text(
                    f"**本轮模拟成交 {len(fills)} 笔**｜买入 {buy_count}｜卖出 {sell_count}｜全市场机会 {radar_count}\n"
                    f"成交后同一股票同一场景当天不再重复模拟成交。"
                ),
                {"tag": "hr"},
                lark_text("\n\n".join(paper_fill_brief(s) for s in fills[:8])),
                {"tag": "hr"},
                lark_text("说明：这是本地模拟盘成交回放，不是真实下单；用于盘后评估信号是否能在当时价格转化为可执行交易。"),
            ],
        },
    }
    data = json.dumps(card, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(intra.FEISHU_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", "ignore")


def send_feishu_execution_rejections(signals, health):
    """Expose a first-time strategy execution veto as an audit card, never as a buy alert."""
    rejected = [
        signal for signal in signals
        if (signal.get("paper_trade") or {}).get("status") == "REJECTED"
        and signal.get("scenario") in intraday_timing_v2.ENTRY_SCENARIOS
    ]
    if os.environ.get("A_SHARE_SKIP_FEISHU") == "1" or not rejected:
        return json.dumps({"code": 0, "msg": "skipped or no strategy execution rejection"}, ensure_ascii=False)
    now = now_dt()
    lines = []
    for signal in rejected[:6]:
        paper = signal.get("paper_trade") or {}
        lines.append(
            f"**{signal.get('name')}({signal.get('symbol')})｜{signal.get('scenario')}**\n"
            f"- 信号价：{f2(signal.get('current_price'))}｜状态：执行否决，不生成模拟订单\n"
            f"- 原因：{paper.get('reason') or '执行纪律未通过'}\n"
            "- 处理：保持观察；等待下一根已收盘K线重新计算，不得手工追单。"
        )
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": "yellow", "title": {"tag": "plain_text", "content": f"A股策略执行审计｜{now:%H:%M:%S}"}},
            "elements": [
                lark_text(f"**本轮策略执行否决 {len(rejected)} 笔。** 这是风控审计，不是买入提醒。\n行情延迟 {health.get('quote_delay_sec', '未知')}秒｜状态 {health.get('engine_status')}"),
                {"tag": "hr"},
                lark_text("\n\n".join(lines)),
                {"tag": "hr"},
                lark_text("系统将量能、VWAP距离、策略依据和盘前仓位授权前置到等待状态；若仍出现本卡，表示执行层发现了需要继续修复的防线。"),
            ],
        },
    }
    data = json.dumps(card, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(intra.FEISHU_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", "ignore")


def build_paper_price_map(rows, radar_rows, signals):
    price_map = {}
    for row in rows or []:
        q = row.get("quote") or {}
        code = row.get("code") or q.get("code")
        if code:
            price_map[code] = {"last_price": q.get("close"), "name": q.get("name"), "pct": q.get("pct")}
    for row in radar_rows or []:
        q = row.get("quote") or {}
        code = row.get("code") or q.get("code")
        if code:
            price_map[code] = {"last_price": q.get("close"), "name": q.get("name"), "pct": q.get("pct")}
    for signal in signals or []:
        code = signal.get("symbol")
        if code and signal.get("current_price") is not None:
            price_map[code] = {"last_price": signal.get("current_price"), "name": signal.get("name"), "pct": signal.get("pct")}
    return price_map


def write_signal_audit_snapshot(signals, health, cache, current):
    """Persist one compact full-universe decision snapshot per five minutes."""
    bucket = current.strftime("%Y-%m-%d %H:") + f"{(current.minute // 5) * 5:02d}"
    if cache.get("signal_audit_bucket") == bucket:
        return False
    rows = []
    for signal in signals or []:
        timing = signal.get("timing_v2") or {}
        rows.append({
            "symbol": signal.get("symbol"),
            "name": signal.get("name"),
            "scenario": signal.get("scenario"),
            "price": signal.get("current_price"),
            "pct": signal.get("pct"),
            "strategy_key": signal.get("strategy_key"),
            "daily_qualified": signal.get("strategy_daily_qualified"),
            "sector_ok": signal.get("sector_resonance_ok"),
            "sector_reason": signal.get("sector_resonance_reason"),
            "candidate_pattern": timing.get("candidate_entry_pattern"),
            "regime": timing.get("regime"),
            "path": timing.get("path_state"),
            "location": timing.get("location"),
            "setup": timing.get("setup_15m"),
            "execution_5m": timing.get("execution_5m"),
            "room_atr": (timing.get("room_risk") or {}).get("room_atr"),
            "reward_risk": (timing.get("room_risk") or {}).get("reward_risk"),
            "blockers": list(timing.get("blockers") or [])[:8],
            "board_rotation": signal.get("sector_rotation") or {},
            "plan_allowed": signal.get("premarket_plan_allows_entry"),
            "plan_complete": signal.get("premarket_plan_complete"),
            "strategy_contract": signal.get("strategy_contract") or {},
            "sector_momentum": signal.get("sector_momentum") or {},
            "execution_gates": timing.get("execution_gates") or {},
            "timing_metrics": timing.get('metrics') or {},
            "source_bar_close": timing.get('source_bar_close') or {},
            "room_risk": timing.get('room_risk') or {},
            "leader_routes": {key: timing.get(key) or {} for key in
                              ("leader_opening_hold", "leader_opening_reversal", "leader_second_leg")},
            "all_blockers": list(signal.get("reasons") or []),
            "repair_shadow": signal.get('repair_shadow') or {},
            "gap_research": timing.get('gap_research') or {},
            "contract_readiness": timing.get('contract_readiness') or {},
            "structure_freshness": ((timing.get('data_quality') or {}).get('market_data') or {}),
            "method_replay_input": timing.get("method_replay_input") or {},
            "entry_quality": timing.get('entry_quality') or {},
            "confirmation_guard": timing.get('confirmation_guard') or {},
            "timing_replay_input": timing.get('timing_replay_input') or {},
            "timing_config_hash": timing.get("config_hash"),
        })
    payload = {
        "timestamp": current.strftime("%Y-%m-%d %H:%M:%S"),
        "cycle_started_at": current.isoformat(),
        "decision_recorded_at": now_dt().isoformat(),
        "preopen_quality_gate": (health or {}).get("preopen_quality_gate") or {},
        "storage": (health or {}).get("storage") or {},
        "entry_permission": (health or {}).get('entry_permission') or {},
        "global_risk": (health or {}).get('global_risk') or {},
        "leader_pool": (health or {}).get("leader_pool") or {},
        "bucket": bucket,
        "engine_status": (health or {}).get("engine_status"),
        "board_snapshot": [{
            'board_id': b.get('f12'), 'board_name': b.get('f14'), 'pct': b.get('f3'),
            'coverage': b.get('_coverage') or {},
            'rotation': (cache.get('board_rotation_state') or {}).get(b.get('f14')) or {},
        } for b in cache.get('radar_boards') or []],
        "market_state": (health or {}).get('a_share_market_regime') or {},
        "entry_pipeline": entry_pipeline_summary(signals, health),
        "rows": rows,
    }
    payload["input_sha256"] = hashlib.sha256(
        json.dumps(json_safe(rows), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    SIGNAL_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SIGNAL_AUDIT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(json_safe(payload), ensure_ascii=False, separators=(",", ":")) + "\n")
    cache["signal_audit_bucket"] = bucket
    return True


def entry_pipeline_summary(signals, health=None):
    """Count unique planned symbols through the actual formal-entry gates."""
    stages = ("global_readiness", "market_permission", "plan", "daily", "sector", "setup", "execution", "formal")
    passed = {stage: 0 for stage in stages}
    blocked = {stage: [] for stage in stages}
    unique = {s.get("symbol"): s for s in signals or []
              if s.get("strategy_family") == "THREE_METHOD"}
    for code, signal in unique.items():
        timing = signal.get("timing_v2") or {}
        gate = ((timing.get('data_quality') or {}).get('market_data') or {}).get('market_gate') or {}
        policy = ((health or {}).get('global_risk') or {}).get('policy') or {}
        market_ok = (policy.get('allow_core_attack_buy') is not False
                     and (gate.get('allowed') is not False or timing.get('market_gate_override') is True))
        checks = (
            not any((health or {}).get(key, {}).get(field) is False for key, field in
                    (("preopen_quality_gate", "allowed"), ("storage", "ready"), ("watchlist_sync", "entry_allowed"))),
            market_ok,
            bool(signal.get("premarket_plan_allows_entry") and signal.get("premarket_plan_complete")),
            signal.get("strategy_daily_qualified") is True,
            signal.get("sector_resonance_ok") is True,
            bool(timing.get("candidate_entry_pattern") and signal.get("strategy_gate_ok")),
            timing.get("entry_allowed") is True,
            signal.get("scenario") in intraday_timing_v2.ENTRY_SCENARIOS,
        )
        for stage, ok in zip(stages, checks):
            if not ok:
                blocked[stage].append(code)
                break
            passed[stage] += 1
    return {"watched": len(unique), "passed": passed, "first_blocked_symbols": blocked}


def write_runtime_json(signals, health, radar_rows=None, paper_snapshot=None):
    ensure_dirs()
    paper_snapshot = paper_snapshot or paper_trading.write_latest_snapshot(BASE_DIR, trading_date=intra.REPORT_DATE)
    timestamp = now_dt().strftime("%Y-%m-%d %H:%M:%S")
    health = dict(health or {})
    # A successful tick is the authoritative freshness marker. Lifecycle status
    # updates (closed/idle/restarting) must retain this context rather than
    # replacing it with a four-field health stub.
    health.update({
        "version": VERSION,
        "updated_at": timestamp,
        "engine_status": health.get('engine_status', 'ok'),
        "last_success_at": timestamp,
        "last_success_trading_date": intra.REPORT_DATE,
        "last_error": None,
        "last_error_at": None,
    })
    global_context = health.get("global_risk") or {}
    opportunities = serialize_market_opportunities(radar_rows, global_context=global_context, now=now_dt())
    payload = {
        "version": VERSION,
        "updated_at": timestamp,
        "trading_date": intra.REPORT_DATE,
        "health": json_safe(health),
        "signals": [serialize_signal_for_dashboard(s) for s in visible_signals_for_dashboard(signals)],
        "market_opportunities": opportunities,
        "paper_account": json_safe(paper_snapshot.get("account") or {}),
        "paper_positions": json_safe(paper_snapshot.get("positions") or []),
        "paper_orders": json_safe(paper_snapshot.get("orders") or []),
    }
    LATEST_SIGNALS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    MARKET_OPPORTUNITIES.write_text(json.dumps({
        "updated_at": payload["updated_at"],
        "trading_date": intra.REPORT_DATE,
        "opportunities": opportunities,
        "rule": "全市场机会只做替代观察；只有自身回踩VWAP不破或放量站回触发价，才进入可执行信号。",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    SIGNAL_HEALTH.write_text(json.dumps(json_safe(health), ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def write_markdown(signals, health, radar_rows=None, paper_snapshot=None):
    now = now_dt()
    strategy_signals = [
        signal for signal in signals
        if signal.get("strategy_family") == "THREE_METHOD"
        or signal.get("scenario") in intraday_timing_v2.EXIT_SCENARIOS
    ]
    executable = [
        signal for signal in strategy_signals
        if signal.get("scenario") in intraday_timing_v2.EXIT_SCENARIOS
        or (
            signal.get("scenario") in intraday_timing_v2.ENTRY_SCENARIOS
            and bool((signal.get("timing_v2") or {}).get("entry_allowed"))
        )
    ]
    waits = [signal for signal in strategy_signals if signal.get("scenario") in {"V2_WAIT", "V2_NO_ADD"}]
    global_context = health.get("global_risk") or {}
    opportunities = serialize_market_opportunities(radar_rows, global_context=global_context, now=now_dt())
    lines = []
    lines.append(f"# A股盘中策略实时执行｜{now:%Y-%m-%d %H:%M:%S}\n")
    lines.append(f"- 生成时间：{now:%Y-%m-%d %H:%M:%S}（Asia/Shanghai）")
    lines.append(f"- 引擎版本：{VERSION}")
    lines.append(f"- 健康状态：{health.get('engine_status')}；行情延迟 {health.get('quote_delay_sec', '未知')} 秒；分钟线延迟 {health.get('minute_bar_delay_sec', '未知')} 秒。")
    lines.append(
        f"- 模拟盘：今日累计成交 {health.get('paper_fills_today', health.get('paper_fills', 0))} 笔；"
        f"买入 {health.get('paper_buy_fills_today', 0)} 笔、卖出 {health.get('paper_sell_fills_today', 0)} 笔；"
        f"本轮新增 {health.get('paper_fills_current_tick', 0)} 笔；"
        f"三策略执行否决 {health.get('v2_execution_rejections_today', 0)} 笔；成交后同一信号不再重复提醒。"
    )
    regime = health.get("a_share_market_regime") or {}
    pipeline = health.get("entry_pipeline") or entry_pipeline_summary(signals, health)
    counts = pipeline["passed"]
    lines.append(f"- 新增仓状态：{health.get('new_entry_status', 'unknown')}；盘前质检：{(health.get('preopen_quality_gate') or {}).get('reason', '未提供')}。")
    permission = health.get('entry_permission') or {}
    if permission:
        lines.append(f"- 当前动作：{permission.get('reason')}。恢复条件：{permission.get('release_condition')}。")
    lines.append(
        f"- 新增仓逐关通过：数据准备 {counts.get('global_readiness', 0)} → 交易许可 {counts.get('market_permission', 0)} → 盘前授权 {counts['plan']} → 日线资格 {counts['daily']} → "
        f"当日板块共振 {counts['sector']} → 所属策略形态 {counts['setup']} → "
        f"量价/空间/风控 {counts['execution']} → 正式买入 {counts['formal']}。"
    )
    if regime:
        indexes = regime.get("indexes") or {}
        index_text = " / ".join(
            f"{item.get('label', key)} {item.get('pct'):.2f}%"
            for key, item in indexes.items()
            if isinstance(item.get("pct"), (int, float))
        )
        lines.append(f"- A股盘中状态：{regime.get('label', '-')}; {index_text}; {regime.get('action', '-')}")
    path = global_context.get("intraday_path") or {}
    if path.get("markets"):
        kospi = (path.get("markets") or {}).get("kospi") or {}
        if isinstance(kospi.get("drawdown_from_high_pct"), (int, float)):
            lines.append(
                f"- 外部科技路径：韩国当前 {kospi.get('current_pct', 0):.2f}%；"
                f"较盘中高点回撤 {kospi.get('drawdown_from_high_pct', 0):.2f}%；"
                f"{global_risk.market_opportunity_gate_reason(global_context, now=now)}"
            )
    lines.append("- 说明：实时执行层只接受龙头、520、趋势5日线三种策略合同；盘前决定分类与仓位，盘中必须先有当日板块共振，再通过大周期、短周期闭合、VWAP、量能与Room/RR门控。仅为交易计划参考，不构成投资建议。")
    lines.append("")
    lines.append("## 一、三策略实时执行总览")
    lines.append(f"- 当前状态：可执行 {len(executable)} / 等待或停止加仓 {len(waits)} / 已展示 {len(strategy_signals)}。")
    lines.append("- 只有策略合同允许的时机形态才能生成正式首笔信号；原始E1-E7仅作内部证据。已有持仓仍可执行减仓、止盈和结构退出。")
    lines.append("")
    lines.append("## 二、三策略状态表")
    lines.append("| 代码 | 名称 | 策略信号 | 当前价 | 大周期/位置 | 15m/5m | 执行区 | 入场失效/硬防守 | 模拟盘 | 本轮结论 |")
    lines.append("|---|---|---|---:|---|---|---:|---:|---|---|")
    for s in visible_signals_for_dashboard(strategy_signals):
        timing = s.get("timing_v2") or {}
        levels = timing.get("levels") or {}
        room = timing.get("room_risk") or {}
        paper = s.get("paper_trade") or {}
        paper_text = paper.get("status") or "-"
        if paper.get("status") in ("FILLED", "PARTIAL_FILLED"):
            paper_text = f"{paper.get('status')} {paper.get('side')} {paper.get('qty')}股@{f2(paper.get('fill_price'))}"
        band_low = room.get("execution_band_low")
        band_high = room.get("execution_band_high")
        band = f"{f2(band_low)}-{f2(band_high)}" if band_low is not None and band_high is not None else "仅动态计算"
        blockers = timing.get("blockers") or s.get("reasons") or []
        conclusion = s.get("action") if s in executable else "等待：" + "；".join(str(item) for item in blockers[:2])
        lines.append(
            f"| {s['symbol']} | {s['name']} | {s['scenario']} | {f2(s['current_price'])} | "
            f"{timing.get('regime') or '-'} / {timing.get('location') or '-'} | "
            f"{timing.get('setup_15m') or '-'} / {timing.get('execution_5m') or '-'} | {band} | "
            f"{f2(levels.get('entry_invalidation'))}/{f2(levels.get('structural_invalidation'))} | {paper_text} | {conclusion} |"
        )
    if not strategy_signals:
        lines.append("| - | - | WAIT | - | - | - | - | - | - | 当前未取得三策略状态，不使用历史策略替代。 |")
    lines.append("")
    paper_snapshot = paper_snapshot or {}
    account = paper_snapshot.get("account") or {}
    positions = paper_snapshot.get("positions") or []
    lines.append("## 三、模拟账户持仓盯市")
    lines.append("- 用途：记录本地模拟盘真实成交后的持仓、T+1可卖数量和后续收益；不是实盘账户。")
    if (account.get('ledger_quality') or {}).get('ready') is False:
        lines.append('- 对账警报：历史证券导入待核验；以下资产/数量为待核记录，不作为收益或仓位决策依据。新增仓及异常标的交易暂停，其他已核验持仓的退出风控保留。')
    if positions:
        lines.append(
            f"- 账户：初始资金 {f2(account.get('initial_cash'))}｜总资产 {f2(account.get('total_assets') or account.get('total_amount'))}｜"
            f"可用现金 {f2(account.get('cash'))}｜持仓 {account.get('position_count', 0)} 只｜"
            f"持仓市值 {f2(account.get('market_value'))}｜"
            f"仓位 {pct(account.get('position_pct'))}｜浮盈亏 {f2(account.get('unrealized_pnl'))}｜"
            f"持仓收益率 {pct(account.get('unrealized_pnl_pct'))}。"
        )
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
        lines.append("- 当前模拟账户无持仓；只有成交信号通过模拟撮合后才会生成持仓。")
    lines.append("")
    lines.append("## 四、全市场机会池")
    lines.append("- 用途：持仓票陷入困境时寻找更强替代观察；不作为直接买入依据。")
    lines.append("- 科技专项红灯：科技链只观察；非科技必须板块涨幅不少于0.8%、个股1m/5m量能同步且三周期确认，才可模拟。")
    lines.append("- 触发：只看回踩 VWAP 不破或放量站回触发价；急拉远离触发价不追。")
    if opportunities:
        lines.append("")
        lines.append("| 机会代码 | 机会名称 | 雷达状态 | 模拟门控 | 执行区 | 现价 | 涨跌 | VWAP | 触发价 | 失效价 | 成交额 | 板块情绪 | 板块/个股量能 | 三轴 | 框架依据 | 动作 |")
        lines.append("|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---|---|---|")
        for item in opportunities:
            exec_band = f"{f2(item.get('execution_band_low'))}-{f2(item.get('execution_band_high'))}"
            market_gate = item.get("market_opportunity_gate_reason") or (
                "可模拟" if item.get("market_opportunity_gate_ok") else item.get("radar_gate_reason")
            )
            lines.append(
                f"| {item['code']} | {item['name']} | {item['status']} | "
                f"{market_gate} | "
                f"{exec_band} | "
                f"{f2(item.get('current_price'))} | {pct(item.get('pct'))} | "
                f"{f2(item.get('vwap'))} | {f2(item.get('trigger_price'))} | {f2(item.get('invalid_price'))} | "
                f"{item.get('amount_yi', 0):.1f}亿 | {item.get('sector_emotion') or '-'} | {item.get('sector_volume') or '-'} | "
                f"{item.get('axes_text')} | {item.get('focus')} | {item.get('action')} |"
            )
    else:
        lines.append("- 暂无满足框架门槛的全市场替代观察。")
    lines.append("")
    lines.append("## 四、执行规则")
    lines.append("- 20秒内识别价格进入触发区/穿越关键价；加仓类信号必须等1m/3m/5m确认，不提前追。")
    lines.append("- 14:57 之后只做风险提醒和状态展示，不推进攻加仓。")
    lines.append("- 社区情绪只作为温度计，不作为事实依据，也不单独触发买卖。")
    MARKDOWN_REPORT.write_text("\n".join(lines), encoding="utf-8")
    try:
        return dashboard.publish_report(MARKDOWN_REPORT)
    except Exception as exc:
        return {"error": str(exc)}


def fetch_minute_cache(codes, state=None, current=None, max_workers=8):
    """Tencent minute bars first; Sina only when Tencent has no usable series."""
    rows, quality = local_market_data.refresh_intraday_minutes(
        BASE_DIR,
        list(codes or []),
        now=current or now_dt(),
        state=state,
        max_workers=max_workers,
    )
    if isinstance(state, dict):
        state["minute_data_quality"] = quality
    return rows


def minute_rows_latest_time(rows):
    times = [parse_hms(row.get("m") or "") for row in rows or []]
    times = [item for item in times if item is not None]
    return max(times) if times else None


def merge_fresher_minute_cache(cache, latest):
    """Do not replace a usable minute series with an empty or older response."""
    cache = cache if isinstance(cache, dict) else {}
    for code, rows in (latest or {}).items():
        old = cache.get(code) or []
        new_time = minute_rows_latest_time(rows)
        old_time = minute_rows_latest_time(old)
        if new_time is not None and (old_time is None or new_time >= old_time):
            cache[code] = rows
        elif code not in cache:
            cache[code] = rows or []
    return cache


def fetch_json_fast(url, headers=None, timeout=4):
    req_headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/center/gridlist.html"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def fetch_market_breadth_snapshot_fast(size=MARKET_BREADTH_SCAN_SIZE):
    """Fetch a broad A-share snapshot for breadth and turnover, not selection.

    The radar's small candidate sample cannot describe a market where most
    stocks are falling.  This endpoint is deliberately independent from the
    top-gainer / top-turnover scans used to find candidates.
    """
    page_size = MARKET_BREADTH_PAGE_SIZE
    requested_size = max(page_size, int(size))
    deadline = time.monotonic() + MARKET_BREADTH_FETCH_BUDGET_SECONDS
    page_errors = {}

    def fetch_page(page):
        query = (
            f"pn={page}&pz={page_size}&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid=f12"
            "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
            "&fields=f12,f14,f3,f6,f100,f102,f103"
        )
        for host in (
            "https://push2.eastmoney.com/api/qt/clist/get?",
            "https://push2his.eastmoney.com/api/qt/clist/get?",
        ):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                page_errors[page] = "fetch_budget_exhausted"
                return [], 0
            try:
                data = fetch_json_fast(host + query, timeout=min(3, remaining))
                payload = data.get("data") or {}
                rows = payload.get("diff") or []
                total = int(payload.get("total") or 0)
                expected = min(page_size, max(0, total - (page - 1) * page_size))
                if isinstance(rows, list) and rows and (not total or len(rows) == expected):
                    page_errors.pop(page, None)
                    return rows, int(payload.get("total") or 0)
                page_errors[page] = "empty_or_truncated_page"
            except Exception as exc:
                page_errors[page] = type(exc).__name__
                continue
        return [], 0

    first_rows, reported_total = fetch_page(1)
    if not first_rows:
        return {"rows": [], "reported_total": 0, "requested_rows": requested_size, "page_count": 0,
                "failed_pages": [1], "page_errors": page_errors}
    requested_rows = reported_total or requested_size
    page_count = max(1, math.ceil(requested_rows / page_size))
    pages = {1: first_rows}
    if page_count > 1:
        # A smaller pool is faster in practice because the quote host starts
        # returning 503s when all 50+ pages arrive at once.
        with ThreadPoolExecutor(max_workers=min(6, page_count - 1)) as pool:
            futures = {pool.submit(fetch_page, page): page for page in range(2, page_count + 1)}
            for future in as_completed(futures):
                rows, _ = future.result()
                if rows:
                    pages[futures[future]] = rows
    missing = [page for page in range(1, page_count + 1) if page not in pages]
    # Retry at most one bounded parallel batch; a failing provider must not
    # extend a quote cycle by several minutes of serial retries.
    if missing:
        with ThreadPoolExecutor(max_workers=min(6, len(missing))) as pool:
            futures = {pool.submit(fetch_page, page): page for page in missing[:6]}
            for future in as_completed(futures):
                recovered, _ = future.result()
                if recovered:
                    pages[futures[future]] = recovered
    failed_pages = [page for page in missing if page not in pages]
    seen = set()
    rows = []
    for page in sorted(pages):
        for row in pages[page]:
            code = str(row.get("f12") or "")
            if code and code not in seen:
                seen.add(code)
                rows.append(row)
    return {
        "rows": rows,
        "reported_total": reported_total,
        "requested_rows": requested_rows,
        "page_count": page_count,
        "failed_pages": failed_pages,
        "sort_field": "f12",
        "duplicate_rows": sum(len(page) for page in pages.values()) - len(rows),
        "page_errors": page_errors,
        "fetch_budget_seconds": MARKET_BREADTH_FETCH_BUDGET_SECONDS,
    }


def fetch_market_breadth_rows_fast(size=MARKET_BREADTH_SCAN_SIZE):
    """Compatibility helper for callers that only need the raw rows."""
    return fetch_market_breadth_snapshot_fast(size).get("rows") or []


def _profile_from_market_row(row):
    code = str((row or {}).get("f12") or "").strip()
    if not code:
        return None
    return {
        "code": code,
        "name": str(row.get("f14") or "").strip(),
        "industry": str(row.get("f100") or "").strip(),
        "region": str(row.get("f102") or "").strip(),
        "concepts": str(row.get("f103") or "").strip(),
    }


def _fetch_stock_profile_fast(code):
    symbol = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", symbol):
        return None
    secid = f"{1 if symbol.startswith(('5', '6', '9')) else 0}.{symbol}"
    query = (
        f"secid={secid}&ut=fa5fd1943c7b386f172d6893dbfba10b"
        "&fields=f57,f58,f127,f128,f129"
    )
    for host in (
        "https://push2.eastmoney.com/api/qt/stock/get?",
        "https://push2his.eastmoney.com/api/qt/stock/get?",
    ):
        try:
            data = fetch_json_fast(host + query, timeout=3).get("data") or {}
            if data.get("f57"):
                return {
                    "code": str(data.get("f57") or symbol),
                    "name": str(data.get("f58") or "").strip(),
                    "industry": str(data.get("f127") or "").strip(),
                    "region": str(data.get("f128") or "").strip(),
                    "concepts": str(data.get("f129") or "").strip(),
                }
        except Exception:
            continue
    # The quote host can temporarily rate-limit broad scans.  CompanySurvey
    # and CoreConception are independent F10 endpoints and provide a slower,
    # targeted fallback for the most active watched names.
    industry = ""
    concepts = []
    try:
        survey = intra.base.fetch_json(
            f"https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={intra.base.em_code(symbol)}",
            timeout=6,
        )
        basic = ((survey.get("jbzl") or [None])[0] or {})
        industry = str(basic.get("EM2016") or basic.get("INDUSTRYCSRC1") or "").strip()
    except Exception:
        pass
    try:
        concept = intra.base.fetch_json(
            f"https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax?code={intra.base.em_code(symbol)}",
            timeout=6,
        )
        concepts = [
            str(item.get("BOARD_NAME") or "").strip()
            for item in (concept.get("ssbk") or [])[:16]
            if str(item.get("BOARD_NAME") or "").strip()
        ]
    except Exception:
        pass
    if industry or concepts:
        return {
            "code": symbol,
            "name": "",
            "industry": industry,
            "region": "",
            "concepts": "、".join(concepts),
        }
    return None


def enrich_quotes_with_market_profiles(quotes, cache=None, current=None):
    """Attach current industry/concept facts to every watched quote.

    The broad snapshot supplies almost all profiles at no additional network
    cost.  A small targeted fallback fills symbols omitted by a transient page
    failure, and is rate-limited so normal 20-second polling stays cheap.
    """
    cache = cache if isinstance(cache, dict) else {}
    if "market_stock_profiles" not in cache:
        cache["market_stock_profiles"] = load_market_profile_cache()
    profiles = cache.setdefault("market_stock_profiles", {})
    current = current or now_dt()
    symbols = [
        str(code) for code in (quotes or {})
        if re.fullmatch(r"\d{6}", str(code))
    ]
    missing = [code for code in symbols if not (profiles.get(code) or {}).get("industry")]
    missing.sort(key=lambda code: -abs(float((quotes.get(code) or {}).get("pct") or 0.0)))
    last_targeted = float(cache.get("market_profile_targeted_ts") or 0)
    if missing and time.time() - last_targeted >= 60:
        batch = missing[:32]
        before = len(profiles)
        with ThreadPoolExecutor(max_workers=min(4, len(batch))) as pool:
            futures = {pool.submit(_fetch_stock_profile_fast, code): code for code in batch}
            for future in as_completed(futures):
                profile = future.result()
                if profile:
                    profiles[profile["code"]] = profile
        cache["market_profile_targeted_ts"] = time.time()
        if len(profiles) > before:
            write_market_profile_cache(profiles)
    enriched = 0
    for code in symbols:
        profile = profiles.get(code) or {}
        quote = quotes.get(code) or {}
        if profile.get("name") and (not quote.get("name") or quote.get("name") == code):
            quote["name"] = profile["name"]
        if profile.get("industry"):
            quote["industry"] = quote.get("industry") or profile["industry"]
        if profile.get("concepts"):
            quote["concepts"] = quote.get("concepts") or profile["concepts"]
        if profile.get("region"):
            quote["region"] = quote.get("region") or profile["region"]
        if quote.get("industry"):
            enriched += 1
    cache["market_profile_health"] = {
        "updated_at": current.strftime("%Y-%m-%d %H:%M:%S"),
        "watched": len(symbols),
        "industry_enriched": enriched,
        "missing": [code for code in symbols if not (quotes.get(code) or {}).get("industry")],
    }
    return quotes


def build_market_breadth(rows, cache=None, now=None, expected_total=None):
    """Convert an all-market snapshot into an execution-safe breadth state."""
    cache = cache if isinstance(cache, dict) else {}
    current = now or now_dt()
    up = down = flat = 0
    amount_yuan = 0.0
    for row in rows or []:
        change = intra.safe_float(row.get("f3"))
        if change is None:
            continue
        if change > 0:
            up += 1
        elif change < 0:
            down += 1
        else:
            flat += 1
        amount_yuan += max(0.0, intra.safe_float(row.get("f6")) or 0.0)
    total = up + down + flat
    expected_total = int(expected_total or 0)
    coverage_ratio = total / expected_total if expected_total > 0 else None
    coverage_ready = bool(
        total and (
            expected_total <= 0
            or (total >= min(expected_total, MARKET_BREADTH_PAGE_SIZE) and coverage_ratio >= MARKET_BREADTH_MIN_COVERAGE_RATIO)
        )
    )
    day = current.strftime('%Y-%m-%d')
    samples = [s for s in cache.get('market_breadth_samples', [])
               if str(s.get('timestamp', '')).startswith(day)]
    cache['market_breadth_samples'] = samples
    universe = hashlib.sha256(json.dumps(sorted(str(r.get('f12') or i)
        for i, r in enumerate(rows or []) if intra.safe_float(r.get('f3')) is not None)).encode()).hexdigest()
    if coverage_ready:
        samples.append({
            "timestamp": current.strftime("%Y-%m-%d %H:%M:%S"),
            "total": total,
            "up": up,
            "down": down,
            "flat": flat,
            "amount_yuan": amount_yuan,
            "universe_hash": universe,
        })
        cache["market_breadth_samples"] = samples[-120:]
    # Compare turnover rates, not cumulative totals. Never bridge lunch,
    # universe changes, resets, duplicate timestamps or long collection gaps.
    amount_ratio = None
    turnover_state = 'unknown'
    recent = (cache.get('market_breadth_samples') or [])[-3:]
    rates = []
    for a, b in zip(recent, recent[1:]):
        try:
            ta, tb = datetime.fromisoformat(a['timestamp']), datetime.fromisoformat(b['timestamp'])
            seconds = (tb-ta).total_seconds()
            comparable = (a.get('universe_hash') == b.get('universe_hash') == universe
                          and a['total'] == b['total'] and 30 <= seconds <= 600
                          and (ta.hour < 12) == (tb.hour < 12))
            delta = float(b['amount_yuan'])-float(a['amount_yuan'])
            if comparable and delta < 0:
                turnover_state = 'cumulative_reset'
            rates.append(delta/seconds if comparable and delta >= 0 else None)
        except (ValueError, KeyError, TypeError, ZeroDivisionError):
            rates.append(None)
    if coverage_ready and len(rates) == 2 and rates[0] is not None and rates[0] > 0 and rates[1] is not None:
        amount_ratio = rates[1]/rates[0]
        turnover_state = 'shrinking' if amount_ratio <= MARKET_BREADTH_SHRINK_RATIO else 'stable_or_rising'
    up_ratio = up / total if total else None
    down_ratio = down / total if total else None
    if not total:
        state = "data_stale"
        reason = "全市场涨跌家数未刷新，新增机会只观察"
    elif not coverage_ready:
        state = "data_stale"
        reason = (
            f"全市场广度样本不完整：仅取得 {total}/{expected_total} 家"
            f"（覆盖 {coverage_ratio:.0%}），不参与新增机会门控"
        )
    elif (
        down_ratio >= MARKET_BREADTH_WEAK_DOWN_RATIO
        and up_ratio <= MARKET_BREADTH_WEAK_UP_RATIO
    ):
        state = "shrinking_weak" if turnover_state == 'shrinking' else 'broad_weak'
        reason = (
            f"跌家 {down}/{total}（{down_ratio:.0%}）、涨家 {up}/{total}（{up_ratio:.0%}），"
            f"弱广度；成交速率状态 {turnover_state}"
        )
    elif up_ratio >= 0.60:
        state = "broad_strong"
        reason = f"涨家 {up}/{total}（{up_ratio:.0%}），广度改善"
    else:
        state = "mixed"
        reason = f"涨/跌家 {up}/{down}，广度分化"
    return {
        "updated_at": current.strftime("%Y-%m-%d %H:%M:%S"),
        "state": state,
        "total": total,
        "expected_total": expected_total or None,
        "coverage_ratio": round(coverage_ratio, 4) if coverage_ratio is not None else None,
        "coverage_ready": coverage_ready,
        "up": up,
        "down": down,
        "flat": flat,
        "up_ratio": round(up_ratio, 4) if up_ratio is not None else None,
        "down_ratio": round(down_ratio, 4) if down_ratio is not None else None,
        "amount_yi": round(amount_yuan / 100000000, 2),
        "amount_ratio": round(amount_ratio, 4) if amount_ratio is not None else None,
        "amount_ratio_basis": "adjacent_intraday_turnover_rates",
        "turnover_state": turnover_state,
        "samples": len(cache.get("market_breadth_samples") or []),
        "reason": reason,
    }


def fetch_resilient_market_breadth(cache, current):
    started = time.monotonic()
    def usable(snapshot):
        total = int(snapshot.get("reported_total") or snapshot.get("requested_rows") or 0)
        valid = sum(1 for row in snapshot.get("rows") or []
                    if intra.safe_float(row.get("f3")) is not None)
        return bool(total > 0 and valid / total >= MARKET_BREADTH_MIN_COVERAGE_RATIO
                    and not snapshot.get("failed_pages"))

    primary = cache.get("breadth_primary_failure") or {}
    attempted = time.time() >= cache.get("breadth_primary_retry_at", 0)
    if attempted:
        primary = fetch_market_breadth_snapshot_fast()
        primary["source"] = "eastmoney_full_market"
        if usable(primary):
            cache["breadth_primary_retry_at"] = 0
            return primary
    fallback_now = current + timedelta(seconds=time.monotonic() - started)
    fallback = market_breadth_data.fetch_snapshot(cache, fallback_now, parse_tencent_quote_text)
    if usable(fallback):
        if attempted:
            cache["breadth_primary_retry_at"] = time.time() + 600
            cache["breadth_primary_failure"] = {
                "failed_pages": primary.get("failed_pages") or [],
                "page_errors": primary.get("page_errors") or {},
            }
        fallback["primary_failure"] = cache.get("breadth_primary_failure") or {}
        fallback["primary_retry_at"] = datetime.fromtimestamp(cache["breadth_primary_retry_at"]).isoformat(" ")
        return fallback
    # An independent-source outage must not wait for the primary cooldown.
    if not attempted:
        primary = fetch_market_breadth_snapshot_fast()
        primary["source"] = "eastmoney_full_market"
        cache["breadth_primary_retry_at"] = 0
        if usable(primary):
            return primary
    primary["fallback_failure"] = {key: value for key, value in fallback.items() if key != "rows"}
    return primary


def refresh_market_breadth(cache, now=None):
    """Refresh broad market data on its own cadence and retain last good data."""
    cache = cache if isinstance(cache, dict) else {}
    current = now or now_dt()
    if cache.get("market_breadth_ts", 0) + MARKET_BREADTH_REFRESH_SECONDS > time.time():
        return cache.get("market_breadth") or {"state": "data_stale", "reason": "市场广度等待首次刷新"}
    snapshot = fetch_resilient_market_breadth(cache, current)
    rows = snapshot.get("rows") or []
    if rows:
        if snapshot.get("source") != "tencent_full_universe":
            for row in rows:
                profile = _profile_from_market_row(row)
                if profile:
                    cache.setdefault("market_stock_profiles", {})[profile["code"]] = profile
            write_market_profile_cache(cache.get("market_stock_profiles") or {})
        sample_cache = {**cache, "market_breadth_samples": list(cache.get("market_breadth_samples") or [])}
        basis = (snapshot.get("source"), current.strftime("%Y-%m-%d"),
                 hashlib.sha256(",".join(sorted(str(r.get("f12")) for r in rows
                    if intra.safe_float(r.get("f3")) is not None)).encode()).hexdigest())
        if cache.get("breadth_sample_basis") != basis:
            sample_cache["market_breadth_samples"] = []
        breadth = build_market_breadth(
            rows,
            cache=sample_cache,
            now=current,
            expected_total=snapshot.get("reported_total") or snapshot.get("requested_rows"),
        )
        breadth["failed_pages"] = snapshot.get("failed_pages") or []
        breadth["duplicate_rows"] = snapshot.get("duplicate_rows", 0)
        if breadth["failed_pages"]:
            breadth.update(state="data_stale", coverage_ready=False,
                           reason="全市场广度分页失败：" + str(breadth["failed_pages"]))
        if breadth.get("coverage_ready"):
            cache["breadth_sample_basis"] = basis
            cache["market_breadth_samples"] = sample_cache.get("market_breadth_samples") or []
            cache["market_breadth_last_good"] = dict(breadth)
        else:
            # Preserve last-good evidence for diagnostics, never masquerade it
            # as a fresh trading gate or publish partial up/down percentages.
            breadth["partial_up_ratio"] = breadth.get("up_ratio")
            breadth["up_ratio"] = breadth["down_ratio"] = None
            breadth["last_good_at"] = (cache.get("market_breadth_last_good") or {}).get("updated_at")
            breadth["samples"] = len(cache.get("market_breadth_samples") or [])
        cache["market_breadth"] = breadth
        cache["market_breadth_error"] = None if breadth.get("coverage_ready") else breadth.get("reason")
    else:
        breadth = {"state": "data_stale", "coverage_ready": False, "updated_at": str(current),
                   "failed_pages": snapshot.get("failed_pages") or [1],
                   "last_good_at": (cache.get("market_breadth_last_good") or {}).get("updated_at"),
                   "reason": "全市场广度获取失败", "up_ratio": None, "down_ratio": None}
        cache["market_breadth"] = breadth
        cache["market_breadth_error"] = "全市场广度获取失败"
    breadth["page_errors"] = snapshot.get("page_errors") or {}
    for key in ("source", "scope", "universe_source", "universe_date", "universe_hash",
                "excluded_count", "oldest_quote_at", "newest_quote_at", "rejected_quotes",
                "fetch_seconds", "primary_failure", "primary_retry_at", "fallback_failure"):
        if key in snapshot:
            breadth[key] = snapshot[key]
    cache["market_breadth_ts"] = time.time()
    return breadth


def fetch_market_scan_rows_fast(fid, size):
    query = (
        f"pn=1&pz={size}&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid={fid}"
        "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
        "&fields=f12,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18,f20,f21,f8,f9,f23,f100,f109,f160,f161,f164,f165"
    )
    hosts = (
        "https://push2.eastmoney.com/api/qt/clist/get?",
        "https://push2his.eastmoney.com/api/qt/clist/get?",
    )
    for host in hosts:
        try:
            data = fetch_json_fast(host + query, timeout=4)
            rows = data.get("data", {}).get("diff", []) or []
            if rows:
                return rows
        except Exception:
            continue
    return []


def select_radar_candidates_fast(existing_codes, boards, limit=None):
    existing_codes = set(existing_codes or set())
    with ThreadPoolExecutor(max_workers=2) as pool:
        amount_future = pool.submit(fetch_market_scan_rows_fast, "f6", 140)
        pct_future = pool.submit(fetch_market_scan_rows_fast, "f3", 80)
        raw_rows = amount_future.result() + pct_future.result()
    theme_keys = intra.active_theme_keywords(boards)
    seen = {}
    for raw in raw_rows:
        q = intra.em_row_to_quote(raw)
        if not q or q["code"] in existing_codes:
            continue
        name = q.get("name", "")
        if "ST" in name or q["close"] <= 0:
            continue
        score, matched, pos = intra.radar_candidate_score(q, theme_keys, boards=boards)
        evidence = q.get("theme_evidence") or {}
        if not evidence.get("valid"):
            continue
        if score < 4.6:
            continue
        old = seen.get(q["code"])
        if not old or score > old["score"]:
            seen[q["code"]] = {
                "quote": q,
                "score": score,
                "matched": matched,
                "pos": pos,
                "theme_evidence": evidence,
            }
    ranked = sorted(seen.values(), key=lambda x: (-x["score"], -intra.amount_yi(x["quote"]), -x["quote"]["pct"]))
    return ranked[:(limit or intra.RADAR_SCAN_LIMIT)]


def read_json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def parse_float_text(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").replace("%", "").strip()
    if text in ("", "-", "暂无", "None"):
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def split_theme_words(text):
    text = str(text or "")
    if not text:
        return []
    match = re.search(r"题材匹配[:：]([^｜\n]+)", text)
    raw = match.group(1) if match else text.split("｜", 1)[0]
    words = []
    for item in re.split(r"[、,/，\s]+", raw):
        item = item.strip()
        if item and item not in ("全市场框架筛选", "技术确认"):
            words.append(item)
    return words[:8]


def score_from_focus(text, default=6.0):
    match = re.search(r"强度评分[:：]\s*(\d+(?:\.\d+)?)", str(text or ""))
    if match:
        try:
            return float(match.group(1))
        except Exception:
            pass
    return default


def candidate_meta_from_report_row(row, source, source_mtime):
    code = str(row.get("机会代码") or row.get("code") or "").strip()
    if not code:
        return None
    focus = row.get("框架依据") or row.get("focus") or row.get("动作") or ""
    status = row.get("雷达状态") or row.get("status") or ""
    default_score = 8.0 if str(status).startswith("🟢") else 6.0 if str(status).startswith("🟡") else 4.8
    framework_validation = theme_validation.framework_evidence_from_text(focus)
    return {
        "code": code,
        "name": row.get("机会名称") or row.get("name") or code,
        "score": score_from_focus(focus, default=default_score),
        # An old report can be retained for audit, but a contradictory theme
        # label must never become live candidate metadata again.
        "matched": split_theme_words(focus) if framework_validation.get("executable") else [],
        "focus": focus,
        # A persisted framework industry may be used only as a fallback when
        # the live quote omits its industry; it is still rechecked against the
        # current board snapshot before any execution decision.
        "industry": framework_validation.get("industry_text") if framework_validation.get("valid") else "",
        "framework_validation": framework_validation,
        "source": source,
        "source_mtime": source_mtime,
        "status": status,
        "trigger_price": parse_float_text(row.get("触发价") or row.get("trigger_price")),
        "invalid_price": parse_float_text(row.get("失效价") or row.get("invalid_price")),
    }


def candidate_meta_from_auction_row(row, source, source_mtime):
    code = str(row.get("code") or "").strip()
    if not code:
        return None
    quote = row.get("quote") or {}
    focus = row.get("focus") or row.get("action") or ""
    framework_validation = theme_validation.framework_evidence_from_text(focus)
    return {
        "code": code,
        "name": row.get("name") or quote.get("name") or code,
        "score": float(row.get("score") or 8.0),
        "matched": split_theme_words(focus) if framework_validation.get("executable") else [],
        "focus": focus,
        "framework_validation": framework_validation,
        "source": source,
        "source_mtime": source_mtime,
        "status": row.get("status") or "竞价候选",
        "trigger_price": parse_float_text(row.get("trigger_price")),
        "invalid_price": parse_float_text(row.get("invalid_price")),
        "industry": quote.get("industry"),
        "concepts": quote.get("concepts"),
        "axes": row.get("axes"),
    }


def candidate_meta_from_rotation_row(row, source, source_mtime, target_date):
    code = str(row.get("code") or "").strip()
    if not code:
        return None
    context = row.get("framework_context") or {}
    return {
        "code": code,
        "name": row.get("name") or code,
        "score": float(row.get("score") or 0),
        "matched": [],
        "focus": row.get("focus") or "盘后轮动候选",
        "framework_validation": {"valid": True, "executable": True, "reason": "盘后轮动台账"},
        "framework_context": context,
        "source": source,
        "source_mtime": source_mtime,
        "source_target_date": target_date,
        "requires_fresh_source": False,
        "status": row.get("status") or "盘后轮动候选",
    }


def merge_candidate_meta(existing, incoming):
    if not existing:
        return incoming
    old_ts = existing.get("source_mtime") or 0
    new_ts = incoming.get("source_mtime") or 0
    if new_ts >= old_ts:
        merged = {**existing, **incoming}
        merged["matched"] = list(dict.fromkeys((incoming.get("matched") or []) + (existing.get("matched") or [])))[:8]
        return merged
    merged = {**incoming, **existing}
    merged["matched"] = list(dict.fromkeys((existing.get("matched") or []) + (incoming.get("matched") or [])))[:8]
    return merged


def load_tracked_opportunity_candidates(limit=24):
    metas = {}
    compact = intra.REPORT_DATE.replace("-", "")

    auction_path = AUCTION_CANDIDATES_DIR / f"auction_candidates_{compact}.json"
    data = read_json_file(auction_path)
    if isinstance(data, dict):
        mtime = auction_path.stat().st_mtime if auction_path.exists() else 0
        for row in data.get("candidates") or []:
            meta = candidate_meta_from_auction_row(row, f"auction:{auction_path.name}", mtime)
            if meta:
                metas[meta["code"]] = merge_candidate_meta(metas.get(meta["code"]), meta)

    rotation_path = ROTATION_CANDIDATES_DIR / f"rotation_candidates_{compact}.json"
    rotation_data = read_json_file(rotation_path)
    if isinstance(rotation_data, dict) and str(rotation_data.get("target_date") or "") == intra.REPORT_DATE:
        mtime = rotation_path.stat().st_mtime if rotation_path.exists() else 0
        for row in rotation_data.get("candidates") or []:
            meta = candidate_meta_from_rotation_row(
                row,
                f"rotation:{rotation_path.name}",
                mtime,
                rotation_data.get("target_date"),
            )
            if meta:
                metas[meta["code"]] = merge_candidate_meta(metas.get(meta["code"]), meta)

    report_paths = sorted(
        REPORTS_DIR.glob(f"*{compact}*.json"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    for path in report_paths[:12]:
        data = read_json_file(path)
        if not isinstance(data, dict):
            continue
        kind = str(data.get("kind") or path.stem).lower()
        if kind.startswith(("realtime", "afterclose")) or path.stem.startswith(("realtime_", "afterclose_")):
            continue
        mtime = path.stat().st_mtime
        for row in data.get("market_opportunities") or []:
            meta = candidate_meta_from_report_row(row, f"report:{data.get('id') or path.stem}", mtime)
            if meta:
                metas[meta["code"]] = merge_candidate_meta(metas.get(meta["code"]), meta)

    ranked = sorted(
        metas.values(),
        key=lambda x: (-(x.get("source_mtime") or 0), -(x.get("score") or 0)),
    )
    return ranked[:limit]


def build_tracked_candidate_items(metas, quotes, existing_codes=None, boards=None):
    existing_codes = set(existing_codes or set())
    items = []
    for meta in metas or []:
        code = str(meta.get("code") or "").strip()
        if not code or code in existing_codes:
            continue
        q = dict((quotes or {}).get(code) or {})
        if not q:
            continue
        q["code"] = code
        q["name"] = q.get("name") or meta.get("name") or code
        q["industry"] = q.get("industry") or meta.get("industry") or ""
        # Historical matched words are metadata, not a replacement for a live
        # quote's concepts.  Re-injecting them was a feedback loop that could
        # keep a false medical/technology label alive all day.
        q["concepts"] = q.get("concepts") or meta.get("concepts") or ""
        q["framework_theme_labels"] = list(meta.get("matched") or [])
        q["theme_evidence"] = theme_validation.candidate_theme_evidence(q, boards)
        if q.get("close", 0) <= 0 or "ST" in str(q.get("name") or "").upper():
            continue
        high = q.get("high") or q["close"]
        low = q.get("low") or q["close"]
        pos = (q["close"] - low) / (high - low) * 100 if high and low and high > low else 50
        items.append({
            "quote": q,
            "score": float(meta.get("score") or 6.0),
            "matched": meta.get("matched") or [],
            "pos": pos,
            "tracked_meta": meta,
            "source_age_sec": max(0.0, time.time() - float(meta.get("source_mtime") or time.time())),
        })
    return items


def build_observation_group_candidate_metas(details, quotes):
    """Promote strong Tonghuashun folders into deep inspection, not direct buys.

    A single intraday spike is not enough to manufacture an entry plan.  The
    promotion only makes the group member visible to the V2 evidence chain;
    a later persistent-board rotation pilot must still create a capped plan.
    """
    metas = []
    seen = set()
    for group in (details or {}).get("groups") or []:
        group_name = str(group.get("name") or "同花顺分组")
        codes = [str(code) for code in (group.get("observation_codes") or group.get("codes") or [])]
        members = []
        for code in codes:
            quote = dict((quotes or {}).get(code) or {})
            if not quote or float(quote.get("close") or 0) <= 0:
                continue
            quote["code"] = code
            members.append(quote)
        rising = [quote for quote in members if float(quote.get("pct") or 0) >= 1.0]
        ranked = sorted(members, key=lambda quote: (
            -float(quote.get("pct") or 0),
            -intra.amount_yi(quote),
        ))
        top_pct = float((ranked[0] if ranked else {}).get("pct") or 0)
        if not ranked or not (
            top_pct >= OBSERVATION_GROUP_PROMOTION_MIN_PCT
            or len(rising) >= OBSERVATION_GROUP_PROMOTION_MIN_RISERS
        ):
            continue
        group_reason = (
            f"{group_name} 组内上涨 {len(rising)}/{len(members)}，最强 {top_pct:.2f}%"
        )
        for quote in ranked[:OBSERVATION_GROUP_PROMOTION_LIMIT]:
            code = str(quote.get("code") or "")
            if not code or code in seen or float(quote.get("pct") or 0) < 1.0:
                continue
            seen.add(code)
            metas.append({
                "code": code,
                "name": quote.get("name") or code,
                "industry": quote.get("industry") or "",
                "concepts": quote.get("concepts") or "",
                "score": min(10.0, 6.0 + max(0.0, float(quote.get("pct") or 0)) / 2),
                "matched": [group_name],
                "focus": f"同花顺分组主题晋升：{group_reason}",
                "framework_validation": {
                    "valid": True,
                    "executable": True,
                    "reason": "分组主题晋升仅授予深检资格，未授予开仓计划",
                },
                "framework_context": {},
                "source": f"tonghuashun_group:{group_name}",
                "source_mtime": time.time(),
                "requires_fresh_source": False,
                "status": "GROUP_PROMOTED_DEEP_CHECK",
                "observation_group": group_name,
            })
    return metas


def build_tracked_opportunity_rows(metas, quotes, cache, args, excluded_codes=None, boards=None):
    excluded_codes = set(excluded_codes or set())
    items = build_tracked_candidate_items(metas, quotes, existing_codes=excluded_codes, boards=boards)
    codes = [item["quote"]["code"] for item in items]
    if not codes:
        return []
    market_data_state = cache.setdefault("market_data", {})
    local_market_data.ensure_history(BASE_DIR, codes, now_dt(), state=market_data_state)
    now_ts = time.time()
    minute_cache = cache.setdefault("tracked_minute", {})
    if cache.get("tracked_minute_ts", 0) + args.bar_refresh <= now_ts:
        merge_fresher_minute_cache(minute_cache, fetch_minute_cache(codes, state=market_data_state, current=now_dt()))
        cache["tracked_minute_ts"] = now_ts
    stats = {code: intra.minline_stats(minute_cache.get(code, [])) for code in codes}
    rows = intra.build_radar_rows(items, stats, boards=boards)
    meta_by_code = {item["quote"]["code"]: item.get("tracked_meta") or {} for item in items}
    for row in rows:
        meta = meta_by_code.get(row["code"]) or {}
        row["candidate_source"] = meta.get("source")
        source_age = next(
            (item.get("source_age_sec") for item in items if item["quote"].get("code") == row["code"]),
            None,
        )
        row["candidate_source_age_sec"] = round(source_age, 1) if isinstance(source_age, (int, float)) else None
        row["candidate_source_fresh"] = bool(
            not meta.get("requires_fresh_source", True)
            or (isinstance(source_age, (int, float)) and source_age <= RADAR_CANDIDATE_MAX_AGE_SECONDS)
        )
        row["candidate_requires_fresh_source"] = meta.get("requires_fresh_source", True)
        row["tracked_candidate"] = True
        row["tracked_status"] = meta.get("status")
        row["framework_context"] = meta.get("framework_context") or {}
        row["framework_validation"] = meta.get("framework_validation") or {"valid": True, "reason": "实时候选"}
        row["auction_candidate"] = str(meta.get("source") or "").startswith("auction:")
        row["auction_trigger_price"] = meta.get("trigger_price")
        row["auction_invalid_price"] = meta.get("invalid_price")
        if meta.get("focus") and meta.get("framework_validation", {}).get("executable", True) and meta.get("focus") not in row.get("focus", ""):
            row["focus"] = f"{row.get('focus')}｜候选依据：{meta.get('focus')}"
        if meta.get("source"):
            row["focus"] = f"{row.get('focus')}｜来源：{meta.get('source')}"
        code = row["code"]
        row["rt_features"] = minute_features(minute_cache.get(code, []), row["quote"]["close"], current=now_dt())
        row["timing_v2"] = timing_v2_for_row(row, minute_cache.get(code, []), current=now_dt())
    return rows


def update_board_rotation_state(boards, candidates, cache=None, now=None):
    """Record same-session board persistence and leader health independently of ranking."""
    cache = cache if isinstance(cache, dict) else {}
    current = now or now_dt()
    history = cache.setdefault("board_rotation_history", {})
    trading_day = current.strftime("%Y-%m-%d")
    board_values = {}
    for board in boards or []:
        name = str(board.get("f14") or "").strip()
        board_pct = intra.safe_float(board.get("f3"))
        if not name or board_pct is None:
            continue
        # Yesterday's board strength is research context only.  A fresh
        # session must build its own consecutive samples before it can pass
        # the live resonance gate.
        samples = [
            sample for sample in history.get(name, [])
            if str(sample.get("timestamp") or "").startswith(trading_day)
        ]
        samples.append({"timestamp": current.strftime("%Y-%m-%d %H:%M:%S"), "pct": board_pct})
        history[name] = samples[-12:]
        board_values[name] = board_pct

    leaders = {}
    for item in candidates or []:
        quote = item.get("quote") or {}
        momentum = intra.sector_momentum_for_candidate(
            quote, boards=boards, matched=item.get("matched"), focus=item.get("focus") or ""
        )
        names = [str(match.get("board_name") or "") for match in momentum.get("matched_boards") or []]
        if not names:
            names = [str(momentum.get("board_name") or "")]
        if not any(names):
            continue
        leader = {
            "code": quote.get("code"), "name": quote.get("name"),
            "pct": float(quote.get("pct") or 0), "amount_yi": intra.amount_yi(quote),
            "close": quote.get("close"), "open": quote.get("open"),
            "turnover": intra.safe_float(quote.get("turnover")) or 0.0,
            "float_market_cap_yi": intra.safe_float(quote.get("float_market_cap_yi")) or 0.0,
        }
        for name in names:
            if not name:
                continue
            old = leaders.get(name)
            if not old or (leader["pct"], leader["amount_yi"]) > (old["pct"], old["amount_yi"]):
                leaders[name] = leader

    states = {}
    for name, board_pct in board_values.items():
        samples = history.get(name) or []
        previous_pct = samples[-2].get("pct") if len(samples) >= 2 else None
        leader = leaders.get(name) or {}
        elapsed_minutes = 0
        minute_of_day = current.hour * 60 + current.minute
        if minute_of_day >= 13 * 60:
            elapsed_minutes = 120 + min(120, max(0, minute_of_day - 13 * 60))
        elif minute_of_day >= 9 * 60 + 30:
            elapsed_minutes = min(120, minute_of_day - (9 * 60 + 30))
        elapsed_fraction = max(0.05, min(1.0, elapsed_minutes / 240.0))
        amount_yi = float(leader.get("amount_yi") or 0)
        turnover = float(leader.get("turnover") or 0)
        float_cap = float(leader.get("float_market_cap_yi") or 0)
        projected_amount_yi = amount_yi / elapsed_fraction
        traded_float_pct = amount_yi / float_cap * 100 if float_cap > 0 else 0.0
        # A fixed 8bn threshold rejects healthy small-cap leaders early in the
        # session.  Accept absolute liquidity OR time-normalised turnover/value.
        # Price leadership and a non-weak candle are still mandatory below.
        liquidity_ok = bool(
            amount_yi >= 8.0
            or (amount_yi >= 0.5 and projected_amount_yi >= 8.0)
            or turnover >= 4.0
            or traded_float_pct >= 1.5
        )
        sustained = bool(
            len(samples) >= ROTATION_PERSIST_MIN_SAMPLES and board_pct >= 0.8
            and previous_pct is not None and float(previous_pct) >= 0.5
            and board_pct >= float(previous_pct) - 0.35
        )
        leader_healthy = bool(
            leader and leader.get("pct", 0) >= max(0.8, board_pct)
            and liquidity_ok
            and leader.get("close") is not None and leader.get("open") is not None
            and float(leader["close"]) >= float(leader["open"])
        )
        states[name] = {
            "available": True, "board_name": name, "board_pct": round(board_pct, 2),
            "previous_pct": round(float(previous_pct), 2) if previous_pct is not None else None,
            "samples": len(samples), "sustained": sustained,
            "leader": leader, "leader_healthy": leader_healthy,
            "leader_liquidity": {
                "ok": liquidity_ok,
                "amount_yi": round(amount_yi, 3),
                "projected_amount_yi": round(projected_amount_yi, 3),
                "turnover": round(turnover, 3),
                "traded_float_pct": round(traded_float_pct, 3),
                "elapsed_fraction": round(elapsed_fraction, 4),
            },
            "reason": (
                f"{name} 连续{len(samples)}次刷新强势，领涨 {leader.get('name') or '-'} 承接有效"
                if sustained and leader_healthy
                else (f"{name} 尚未形成连续强势（{len(samples)}次刷新）" if not sustained else f"{name} 领涨承接未确认")
            ),
        }
    cache["board_rotation_state"] = states
    return states


def attach_sector_rotation_context(rows, board_state):
    """Give downstream gates the same board-persistence facts the UI shows."""
    for row in rows or []:
        momentum = row.get("sector_momentum") or {}
        rotation = (board_state or {}).get(str(momentum.get("board_name") or "")) or {
            "available": False, "sustained": False, "leader_healthy": False,
            "reason": "未取得该板块连续刷新与领涨承接数据",
        }
        row["sector_rotation"] = rotation
        momentum["rotation_confirmed"] = bool(rotation.get("sustained") and rotation.get("leader_healthy"))
        momentum["reason"] = f"{momentum.get('reason') or '板块情绪待核验'}；{rotation.get('reason')}"
        row["sector_momentum"] = momentum
    return rows


def rotation_pilot_budget(global_context=None):
    """Return a bounded pilot envelope derived from the live market regime."""
    context = global_context or {}
    local = context.get("a_share_market_regime") or {}
    breadth = context.get("market_breadth") or {}
    state = str(local.get("state") or "neutral")
    risk_level = str(context.get("risk_level") or "green").lower()
    cap = ROTATION_PILOT_ENTRY_CAP_PCT
    reason = f"市场状态{state}，使用基础轮动试错预算"
    if state in {"risk_off", "transition", "rotation_defensive", "data_stale"}:
        cap, reason = 0.0, f"市场状态{state}，暂停新增轮动试错"
    elif state in {"growth_lead", "broad_strong"}:
        up_ratio = float(breadth.get("up_ratio") or 0)
        multiplier = 1.5 if up_ratio >= 0.60 or state == "growth_lead" else 1.25
        cap = min(0.025, cap * multiplier)
        reason = f"市场{state}且广度改善，单标的试错上限动态提高至{cap * 100:.2f}%"
    elif state in {"recovery", "rotation"}:
        cap = min(0.02, cap * 1.15)
        reason = f"市场{state}，仅小幅提高轮动试错预算至{cap * 100:.2f}%"
    if risk_level == "yellow":
        cap *= 0.67
        reason += "；黄色风险折减33%"
    elif (context.get('policy') or {}).get('allow_core_attack_buy') is False or (
        risk_level == "red" and str(context.get("risk_scope") or "broad") != "technology"
        and context.get('entry_risk_source') != 'external_only'
    ):
        cap, reason = 0.0, "全市场红色风险，暂停新增轮动试错"
    cap = round(max(0.0, min(cap, 0.025)), 6)
    return {"cap_pct": cap, "probe_pct": round(min(0.01, cap), 6), "reason": reason, "market_state": state}


def build_rotation_pilot_candidates(candidates, boards, board_state, cache=None, global_context=None):
    """Retain discovery compatibility without creating an intraday plan bypass.

    New-theme rows remain visible in the radar.  They may become executable on
    the next trading day only after premarket daily classification and sizing.
    """
    return []


def merge_radar_rows(primary_rows, secondary_rows, limit=8):
    seen = {}
    for row in (primary_rows or []) + (secondary_rows or []):
        code = row.get("code")
        if not code:
            continue
        old = seen.get(code)
        if not old or (row.get("axes", {}).get("total") or 0) > (old.get("axes", {}).get("total") or 0):
            seen[code] = row
    rows = list(seen.values())
    rows.sort(key=lambda r: (
        0 if r.get("rotation_pilot") else 1,
        -(r.get("axes", {}).get("total") or 0),
        -intra.amount_yi(r["quote"]),
        -r["quote"].get("pct", 0),
    ))
    return rows[:limit]


def normalize_runtime_levels(levels):
    watchlist = intra.current_watchlist_map()
    if not watchlist:
        return levels, {"watchlist_count": 0, "core_filtered_codes": [], "plan_filtered_codes": []}
    normalized = {}
    for code, market in watchlist.items():
        row = (levels or {}).get(code)
        if not row:
            try:
                row = intra.build_level_from_quote(code, market)
            except Exception:
                row = None
        if row:
            normalized[code] = row
    filtered = [code for code in (levels or {}) if code not in watchlist]
    return normalized, {"watchlist_count": len(watchlist), "core_filtered_codes": filtered, "plan_filtered_codes": filtered}


def revalidate_planned_trend_contracts(levels, cache=None, report_date=None):
    """Route every watchlist name with completed T-1 daily bars only.

    This is a defensive handoff check for a premarket artifact generated after
    09:25, when some daily providers already expose today's partial candle.
    Price levels remain those of the signed-off plan; only the method contract,
    qualification flag and position envelope are corrected.
    """
    cache = cache if isinstance(cache, dict) else {}
    report_date = str(report_date or intra.REPORT_DATE)
    targets = dict(levels or {})
    emotion_snapshot = emotion_leader_pool.load_latest_snapshot(BASE_DIR)
    expected_source_date = intra.base.previous_trading_date(report_date)
    emotion_candidates = {}
    if str(emotion_snapshot.get("source_date") or "") == str(expected_source_date):
        emotion_candidates = {
            str(item.get("code") or ""): {**item, "source_date": emotion_snapshot.get("source_date")}
            for item in emotion_snapshot.get("candidates") or []
            if item.get("code")
        }
    from core import sector_identity
    catalog = sector_identity.load_catalog(BASE_DIR) or cache.get('radar_boards') or []
    identity_profiles = sector_identity.load_profiles(BASE_DIR)
    for level in targets.values():
        level.setdefault('_planned_identity_input', dict(level.get('strategy_daily_metrics') or {}))
    cache_key = (report_date, json.dumps({code: {k: level.get(k) for k in
                  ('industry', 'concepts', '_planned_identity_input')} for code, level in targets.items()},
                  sort_keys=True, ensure_ascii=False),
                 tuple(sorted((str(b.get('f12')), str(b.get('f14'))) for b in catalog)))
    cache_key += (tuple(sorted((code, str(value.get('cross_status'))) for code, value in emotion_candidates.items())),)
    cache_key += (json.dumps({code: identity_profiles.get(code) for code in targets}, sort_keys=True),)
    if cache.get("daily_contract_revalidation_key") != cache_key:
        cache["daily_contract_revalidation_key"] = cache_key
        cache["daily_contract_revalidation_overrides"] = {}
        cache["daily_contract_revalidation_failures"] = {}
        cache["daily_contract_nontrading_previous"] = {}
    overrides = cache.setdefault("daily_contract_revalidation_overrides", {})
    failures = cache.setdefault("daily_contract_revalidation_failures", {})
    retry_now = time.time()
    pending_targets = {
        code: level for code, level in targets.items() if code not in overrides
        and retry_now >= failures.get(code, {}).get("retry_at", 0)
    }
    if pending_targets:

        def classify(code_level):
            code, level = code_level
            daily = [
                row for row in (intra.base.fetch_sohu_daily(code) or [])
                if not str(row.get("date") or "") or str(row.get("date")) < report_date
            ]
            if not daily:
                return code, None, "无完整T-1日线"
            latest = daily[-1]
            if str(latest.get('date') or '') != str(expected_source_date):
                return code, None, f'日线未更新至上一交易日{expected_source_date}'
            quote = {
                **latest,
                "code": code,
                "name": level.get("name") or code,
                "industry": level.get("industry") or "",
                "concepts": level.get("concepts") or "",
            }
            if (level.get('_planned_identity_input') or {}).get('resonance_policy') != sector_identity.POLICY:
                quote.update(identity_profiles.get(code) or {})
            contract = observation_strategy_router.classify_stock({
                "observation_plan_group": level.get("observation_plan_group") or "盘中T-1校正",
                "daily": daily,
                "quote": quote,
                "tech": {},
                "emotion_leader_candidate": emotion_candidates.get(code) or {},
                "resonance_boards": (level.get('_planned_identity_input') or {}).get('resonance_boards') or [],
                "board_catalog": catalog,
            })
            return code, contract, None

        with ThreadPoolExecutor(max_workers=min(8, max(1, len(pending_targets)))) as pool:
            futures = {pool.submit(classify, item): item[0] for item in pending_targets.items()}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    code, contract, error = future.result()
                except Exception as exc:
                    contract, error = None, type(exc).__name__
                if contract and not error:
                    overrides[code] = contract
                    failures.pop(code, None)
                else:
                    attempts = failures.get(code, {}).get("attempts", 0) + 1
                    failures[code] = {"error": error or "无有效策略合同", "attempts": attempts,
                                      "retry_at": retry_now + min(300, 30 * 2 ** min(attempts - 1, 4))}

    cache["daily_contract_revalidation_health"] = {
        "report_date": report_date,
        "targets": len(targets),
        "revalidated": len(overrides),
        "pending_retry": len(targets) - len(overrides),
        "pending_errors": {code: {"name": targets[code].get("name") or code, **item}
                           for code, item in failures.items() if code in targets and code not in overrides},
        "errors": [f"{code}({targets[code].get('name') or code}):{item['error']}"
                   for code, item in sorted(failures.items()) if code in targets and code not in overrides][:8],
    }

    overrides = cache.get("daily_contract_revalidation_overrides") or {}
    for code in set(targets) - set(overrides):
        levels[code].update(strategy_daily_qualified=False, premarket_plan_allows_entry=False,
                            strategy_daily_evidence='T-1日线重验未完成，暂停新增仓')
    corrected = 0
    for code, contract in overrides.items():
        level = (levels or {}).get(code)
        if not level:
            continue
        original = (level.get("strategy_key"), bool(level.get("strategy_daily_qualified")))
        key = str(contract.get("key") or observation_strategy_router.OBSERVE)
        qualified = bool(contract.get("daily_qualified"))
        mainline_ready = sector_identity.ready(contract.get('daily_metrics') or {})
        meta = observation_strategy_router.STRATEGY_META[key]
        level.update({
            "strategy_key": key,
            "strategy_name": contract.get("name") or meta["name"],
            "strategy_style": contract.get("style") or meta["style"],
            "strategy_formal_entry_signal": contract.get("formal_entry_signal") or observation_strategy_router.formal_entry_signal(key),
            "strategy_allowed_patterns": list(contract.get("allowed_patterns") or []),
            "strategy_entry_rule": contract.get("entry_rule") or meta["entry_rule"],
            "strategy_exit_rule": contract.get("exit_rule") or meta["exit_rule"],
            "strategy_reason": contract.get("reason") or "T-1日线合同校正",
            "strategy_daily_qualified": qualified,
            "strategy_daily_metrics": dict(contract.get("daily_metrics") or {}),
            "strategy_daily_evidence": observation_strategy_router.daily_evidence_text(contract),
            "strategy_leader_profile": (contract.get("daily_metrics") or {}).get("leader_profile"),
            "strategy_leader_cross_verified": bool((contract.get("daily_metrics") or {}).get("emotion_pool_cross_verified")),
            "strategy_prior_day_touched_limit": bool((contract.get("daily_metrics") or {}).get("prior_day_touched_limit")),
            "strategy_prior_day_closed_limit": bool((contract.get("daily_metrics") or {}).get("prior_day_closed_limit")),
            "premarket_plan_allows_entry": bool(
                qualified
                and mainline_ready
                and key != observation_strategy_router.OBSERVE
                and str(level.get("priority") or "") != "P0"
            ),
            "premarket_plan_source": "premarket+t1_daily_revalidation",
        })
        if qualified and mainline_ready and key == observation_strategy_router.LEADER and str(level.get("priority") or "") != "P0":
            level.update({
                "premarket_plan_action": "龙头情绪首笔试仓",
                "planned_target_position_pct": 0.01,
                "planned_max_position_pct": 0.02,
                "planned_v2_probe_position_pct": 0.01,
            })
        elif qualified and mainline_ready and key in {observation_strategy_router.TREND_520, observation_strategy_router.TREND_MA5} and str(level.get("priority") or "") != "P0":
            level.update({
                "premarket_plan_action": "520趋势条件试仓" if key == observation_strategy_router.TREND_520 else "5日线趋势条件试仓",
                "planned_target_position_pct": 0.02,
                "planned_max_position_pct": 0.03,
                "planned_v2_probe_position_pct": 0.01,
            })
        else:
            level.update({
                "premarket_plan_action": "主线待核对，仅观察" if qualified and not mainline_ready else ("趋势修复影子观察" if (contract.get('daily_metrics') or {}).get('repair_watch') else "观察池待分类"),
                "planned_target_position_pct": 0.0,
                "planned_max_position_pct": 0.0,
                "planned_v2_probe_position_pct": 0.0,
            })
        if original != (key, qualified):
            corrected += 1
    health = cache.setdefault("daily_contract_revalidation_health", {})
    health["corrected"] = corrected
    health["qualified"] = sum(
        1 for contract in overrides.values() if contract.get("daily_qualified")
    )
    health['mainline_pending'] = [code for code, contract in overrides.items()
                                 if contract.get('daily_qualified')
                                 and not sector_identity.ready(contract.get('daily_metrics') or {})]
    health['mainline_issues'] = {code: (overrides[code].get('daily_metrics') or {}).get('resonance_identity')
                               for code in health['mainline_pending']}
    return levels


def reconcile_nontrading_contract_health(cache, quotes, current=None):
    """Scope fresh zero-trade quotes to a deny-only quarantine, never an exemption.

    Missing/old quotes and broad zero-data failures remain degradation errors.
    This is not proof of suspension; no contract is qualified by this check.
    """
    current = current or now_dt()
    health = cache.get("daily_contract_revalidation_health") or {}
    isolated = {}
    if current.strftime("%Y-%m-%d") == health.get("report_date") and current.strftime("%H:%M") >= "09:30":
        for code, item in (health.get("pending_errors") or {}).items():
            q = (quotes or {}).get(code) or {}
            if not str(item.get("error") or "").startswith("日线未更新至上一交易日") or q.get("_stale"):
                continue
            try:
                stamp = datetime.strptime(str(q.get("datetime") or ""), "%Y%m%d%H%M%S")
                age = (current - stamp).total_seconds()
                zero_trade = all(float(q[k]) == 0 for k in ("open", "high", "low", "volume_lot", "amount_wan"))
                unchanged = float(q["close"]) == float(q["prev_close"]) > 0
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if stamp.date() == current.date() and 0 <= age <= 120 and zero_trade and unchanged:
                isolated[code] = {"name": item.get("name") or code, "quote_time": str(stamp),
                                  "reason": "当日行情有效但无成交；日线未重验，仅隔离观察，禁止新增仓"}
    # A broad zero response could be a provider incident, not many suspensions.
    if len(isolated) > max(1, int(health.get("targets", 0) * 0.01)):
        isolated = {}
    previous = cache.get("daily_contract_nontrading_previous") or {}
    for code in set(previous) - set(isolated):
        (cache.get("daily_contract_revalidation_failures") or {}).get(code, {}).update(retry_at=0)
    health["nontrading_pending"] = isolated
    cache["daily_contract_nontrading_previous"] = isolated
    health["active_pending_retry"] = max(0, int(health.get("pending_retry") or 0) - len(isolated))
    cache["daily_contract_revalidation_health"] = health
    return health


def refresh_market_opportunities(levels, cache, args, excluded_codes=None, tracked_candidates=None, observation_candidates=None, quotes=None, global_context=None):
    now_ts = time.time()
    excluded_codes = set(excluded_codes or set())
    tracked_candidates = tracked_candidates or []
    observation_candidates = observation_candidates or []
    all_tracked_candidates = list(tracked_candidates) + list(observation_candidates)
    boards = cache.get("radar_boards") or []
    if not boards:
        try:
            boards = intra.fetch_fast_boards()
            cache["radar_boards"] = boards
        except Exception:
            boards = []
    tracked_rows = build_tracked_opportunity_rows(
        all_tracked_candidates,
        quotes or {},
        cache,
        args,
        excluded_codes=set(levels or set()) | excluded_codes,
        boards=boards,
    )
    cache["tracked_candidate_count"] = len(all_tracked_candidates)
    cache["observation_group_promoted"] = len(observation_candidates)
    cache["tracked_radar_rows"] = tracked_rows
    cached = lambda: [row for row in (cache.get("radar_rows") or []) if row.get("code") not in excluded_codes]
    first_ts = cache.setdefault("radar_first_seen_ts", now_ts)
    if "radar_rows" not in cache and args.radar_start_delay > 0 and now_ts - first_ts < args.radar_start_delay:
        # Full-market discovery is intentionally delayed at startup, but the
        # planned observation universe must not lose its sector gate during
        # that window.  Refresh a lightweight board sample every 30 seconds so
        # same-session persistence can be established without waiting for the
        # expensive market-wide scan.
        if cache.get("board_rotation_seed_ts", 0) + 30 <= now_ts:
            try:
                boards = intra.fetch_fast_boards() or boards
                seed_candidates = [
                    {"quote": quote}
                    for code, quote in (quotes or {}).items()
                    if str(code).isdigit() and len(str(code)) == 6
                ]
                update_board_rotation_state(boards, seed_candidates, cache=cache, now=now_dt())
                cache["radar_boards"] = boards
                cache["board_rotation_seed_ts"] = now_ts
            except Exception as exc:
                cache["board_rotation_seed_error"] = str(exc)
        wait = int(args.radar_start_delay - (now_ts - first_ts))
        if tracked_rows:
            cache["radar_error"] = f"全市场扫描启动延迟中，先监控报告/竞价候选 {len(tracked_rows)} 只（{wait}秒后扫描）"
            return tracked_rows
        cache["radar_error"] = f"全市场扫描启动延迟中，先刷新自选/模拟盘持仓（{wait}秒后扫描）"
        return []
    if cache.get("radar_ts", 0) + args.radar_refresh > now_ts and "radar_rows" in cache:
        return merge_radar_rows(tracked_rows, cached())
    try:
        boards = intra.fetch_fast_boards() or boards
        scan_candidates = select_radar_candidates_fast(
            set(levels) | excluded_codes,
            boards,
            limit=max(24, intra.RADAR_SCAN_LIMIT * 4),
        )
        observation_items = build_tracked_candidate_items(
            observation_candidates,
            quotes or {},
            existing_codes=set(levels) | excluded_codes,
            boards=boards,
        )
        candidate_by_code = {item["quote"]["code"]: item for item in scan_candidates}
        for item in observation_items:
            candidate_by_code[item["quote"]["code"]] = item
        scan_candidates = list(candidate_by_code.values())
        # Board leaders must be searched across both the discovery sample and
        # every planned quote. A full radar refresh previously replaced the
        # startup state using discovery candidates alone, losing watched leaders.
        rotation_candidates = list(scan_candidates)
        rotation_codes = {
            str((item.get("quote") or {}).get("code") or "")
            for item in rotation_candidates
        }
        for code, quote in (quotes or {}).items():
            if str(code) not in rotation_codes:
                rotation_candidates.append({"quote": quote})
        board_state = update_board_rotation_state(
            boards, rotation_candidates, cache=cache, now=now_dt()
        )
        candidates = scan_candidates[:intra.RADAR_SCAN_LIMIT]
        pilot_candidates = build_rotation_pilot_candidates(
            scan_candidates, boards, board_state, cache=cache, global_context=global_context
        )
        candidate_by_code = {item["quote"]["code"]: item for item in candidates + pilot_candidates}
        candidates = list(candidate_by_code.values())
        candidate_codes = [item["quote"]["code"] for item in candidates]
        market_data_state = cache.setdefault("market_data", {})
        local_market_data.ensure_history(BASE_DIR, candidate_codes, now_dt(), state=market_data_state)
        radar_minute = fetch_minute_cache(candidate_codes, state=market_data_state, current=now_dt())
        cache["radar_minute"] = radar_minute
        stats = {code: intra.minline_stats(rows) for code, rows in radar_minute.items()}
        radar_rows = intra.build_radar_rows(candidates, stats, boards=boards)
        attach_sector_rotation_context(radar_rows, board_state)
        for row in radar_rows:
            code = row["code"]
            row["rt_features"] = minute_features(radar_minute.get(code, []), row["quote"]["close"], current=now_dt())
            row["risk_bucket"] = global_risk.candidate_risk_bucket(row)
            # Radar candidates use the same V2 closed-bar and scoped
            # market/sector gate as tracked symbols.
            row["timing_v2"] = timing_v2_for_row(
                row,
                radar_minute.get(code, []),
                current=now_dt(),
                global_context=global_context,
            )
        cache["radar_ts"] = now_ts
        cache["radar_error"] = None
        for row in tracked_rows:
            row["sector_momentum"] = intra.sector_momentum_for_candidate(
                row.get("quote") or {},
                boards=boards,
                focus=row.get("focus") or "",
            )
            row["theme_evidence"] = (row.get("sector_momentum") or {}).get("theme_evidence") or {}
            row["risk_bucket"] = global_risk.candidate_risk_bucket(row)
        attach_sector_rotation_context(tracked_rows, board_state)
        tracked_minute = cache.get("tracked_minute") or {}
        for row in tracked_rows:
            code = row["code"]
            row["timing_v2"] = timing_v2_for_row(
                row,
                tracked_minute.get(code, []),
                current=now_dt(),
                global_context=global_context,
            )
        merged_rows = merge_radar_rows(tracked_rows, radar_rows)
        if merged_rows:
            cache["radar_rows"] = merged_rows
        elif "radar_rows" not in cache:
            cache["radar_rows"] = []
        cache["radar_boards"] = boards
        return merge_radar_rows(tracked_rows, cached())
    except Exception as exc:
        cache["radar_ts"] = now_ts
        cache["radar_error"] = str(exc)
        return merge_radar_rows(tracked_rows, cached())


def refresh_global_context_if_needed(cache, now):
    if cache.get("global_risk_ts", 0) + GLOBAL_RISK_REFRESH_SECONDS > time.time():
        return cache.get("global_risk_context") or global_risk.read_context(BASE_DIR, intra.REPORT_DATE)
    context = global_risk.read_context(BASE_DIR, intra.REPORT_DATE)
    try:
        import premarket_report

        global_quotes = premarket_report.fetch_global_markets()
        path_state = global_risk.record_intraday_path(
            BASE_DIR,
            intra.REPORT_DATE,
            global_quotes,
            now=now,
        )
        context = global_risk.infer_intraday_context(
            context,
            intra.REPORT_DATE,
            global_quotes,
            path_state=path_state,
            now=now,
        )
        global_risk.write_context(BASE_DIR, context)
        cache["global_risk_error"] = None
    except Exception as exc:
        cache["global_risk_error"] = str(exc)
    cache["global_risk_ts"] = time.time()
    cache["global_risk_context"] = context
    return context


def refresh_global_path_only(cache, now):
    """Sample overseas markets before 09:30 without generating trade signals."""
    if cache.get("global_path_ts", 0) + GLOBAL_RISK_REFRESH_SECONDS > time.time():
        return cache.get("global_path_state") or {}
    try:
        import premarket_report

        quotes = premarket_report.fetch_global_markets()
        path_state = global_risk.record_intraday_path(
            BASE_DIR,
            intra.REPORT_DATE,
            quotes,
            now=now,
        )
        cache["global_path_state"] = path_state
        cache["global_path_error"] = None
    except Exception as exc:
        cache["global_path_state"] = cache.get("global_path_state") or {}
        cache["global_path_error"] = str(exc)
    cache["global_path_ts"] = time.time()
    return cache.get("global_path_state") or {}


def run_tick(con, cache, args):
    start = time.time()
    now = now_dt()
    storage = storage_readiness(BASE_DIR)
    global_context = refresh_global_context_if_needed(cache, now)
    raw_levels = intra.read_premarket_levels()
    levels, core_scope = normalize_runtime_levels(raw_levels)
    from core import sector_identity
    if (not sector_identity.load_catalog(BASE_DIR) and not cache.get('radar_boards')
            and time.time() - cache.get('identity_catalog_retry_ts', 0) >= 60):
        cache['identity_catalog_retry_ts'] = time.time()
        cache['radar_boards'] = intra.fetch_fast_boards()
    revalidate_planned_trend_contracts(levels, cache=cache, report_date=intra.REPORT_DATE)
    # The user can grant Full Disk Access while this daemon is running.  Always
    # re-read the small core lists so a prior snapshot fallback cannot mask a
    # recovered Tonghuashun directory for the rest of the session.
    watchlist_sync = intra.base.read_watchlist_details()
    universe_entry_allowed, universe_gate_reason = intra.base.watchlist_sync_entry_gate(
        watchlist_sync, now=now
    )
    if not storage["ready"]:
        universe_entry_allowed, universe_gate_reason = False, storage["reason"]
    try:
        paper_con = paper_trading.init_db(BASE_DIR)
        paper_trading.settle_t1_buys(paper_con, intra.REPORT_DATE)
        cache["paper_event_warning"] = paper_trading.flush_fill_events(paper_con, BASE_DIR)
        ledger = paper_trading.ledger_quality(paper_con)
        if not ledger['ready']:
            universe_entry_allowed = False
            universe_gate_reason = ledger['reason']
        paper_con.close()
        cache["paper_settle_error"] = None
    except Exception as exc:
        cache["paper_settle_error"] = str(exc)
        universe_entry_allowed, universe_gate_reason = False, "模拟账本结算失败，禁止新增仓"
    paper_rows = load_paper_position_rows()
    paper_codes = paper_position_codes(paper_rows)
    tracked_candidates = load_tracked_opportunity_candidates()
    observation_details = intra.base.read_observation_watchlist_details()
    observation_plan = intra.base.premarket_plan_observation_details(observation_details)
    observation_codes = {
        str(code)
        for code, _market in (observation_details.get("rows") or [])
        if str(code).strip() and str(code) not in set(levels) and str(code) not in paper_codes
    }
    tracked_codes = {
        str(item.get("code"))
        for item in tracked_candidates
        if str(item.get("code") or "").strip()
        and str(item.get("code")) not in set(levels)
        and str(item.get("code")) not in paper_codes
    }
    _, quotes = fetch_quotes_for_codes(
        list(levels) + sorted(paper_codes) + sorted(tracked_codes) + sorted(observation_codes),
        cache=cache,
    )
    reconcile_nontrading_contract_health(cache, quotes, current=now_dt())
    observation_candidates = build_observation_group_candidate_metas(observation_details, quotes)
    levels, paper_added_codes = extend_levels_with_paper_positions(levels, paper_rows, quotes)
    market_breadth = refresh_market_breadth(cache, now=now_dt())
    enrich_quotes_with_market_profiles(quotes, cache=cache, current=now)
    a_share_market_regime = build_a_share_market_regime(quotes, cache=cache, now=now, breadth=market_breadth)
    global_context = global_risk.apply_a_share_entry_policy(
        global_context or {}, a_share_market_regime, now=now_dt(),
    )
    positions = merge_signal_positions(load_positions(), paper_rows)
    market_data_state = cache.setdefault("market_data", {})
    local_market_data.ensure_history(BASE_DIR, list(levels), now, state=market_data_state)
    if cache.get("minute_ts", 0) + args.bar_refresh <= time.time() or not cache.get("minute"):
        cache["minute"] = merge_fresher_minute_cache(
            cache.get("minute") or {},
            fetch_minute_cache(levels.keys(), state=market_data_state, current=now),
        )
        cache["minute_ts"] = time.time()
    radar_rows = refresh_market_opportunities(
        levels,
        cache,
        args,
        excluded_codes=paper_codes,
        tracked_candidates=tracked_candidates,
        observation_candidates=observation_candidates,
        quotes=quotes,
        global_context=global_context,
    )
    rows = build_features(
        levels,
        quotes,
        cache.get("minute") or {},
        boards=cache.get("radar_boards") or [],
        current=now,
        positions=positions,
        global_context=global_context,
        board_state=cache.get("board_rotation_state") or {},
    )
    attach_sector_rotation_context(rows, cache.get("board_rotation_state") or {})
    radar_minutes = cache.get("radar_minute") or {}
    tracked_minutes = cache.get("tracked_minute") or {}
    for row in radar_rows:
        if not row.get("sector_momentum"):
            row["sector_momentum"] = intra.sector_momentum_for_candidate(
                row.get("quote") or {},
                boards=cache.get("radar_boards") or [],
                focus=row.get("focus") or "",
            )
        attach_sector_rotation_context([row], cache.get("board_rotation_state") or {})
        row["risk_bucket"] = global_risk.candidate_risk_bucket(row)
        row["timing_v2"] = timing_v2_for_row(
            row,
            tracked_minutes.get(row["code"]) or radar_minutes.get(row["code"]) or [],
            current=now,
            global_context=global_context,
        )
    names = {row["code"]: row["quote"]["name"] for row in rows}
    minute_health = minute_bar_health(
        [row.get("rt_features") for row in rows]
        + [row.get("rt_features") for row in radar_rows],
        now=now_dt(),
    )
    try:
        official_market_data = intra.base.hithink_finance.api_status()
    except Exception:
        official_market_data = {"state": "unavailable", "minute_bars_supported": False}
    leader_snapshot = emotion_leader_pool.load_latest_snapshot(BASE_DIR)
    expected_leader_date = intra.base.previous_trading_date(intra.REPORT_DATE)
    leader_health = {
        "source_date": leader_snapshot.get("source_date"), "expected_date": expected_leader_date,
        "ready": str(leader_snapshot.get("source_date") or "") == str(expected_leader_date),
        "candidate_count": len(leader_snapshot.get("candidates") or []),
        "kaipanla_ready": (leader_snapshot.get("kaipanla") or {}).get("available") is True,
    }
    health = {
        "engine_status": "ok",
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "quote_delay_sec": quote_delay(quotes),
        "minute_bar_delay_sec": minute_health["delay_sec"],
        "minute_bar_available": minute_health["available"],
        "minute_bar_symbols": minute_health["symbols"],
        "minute_bar_latest_time": minute_health["latest_time"],
        "watched": len(levels),
        "watchlist_count": core_scope.get("watchlist_count"),
        "core_filtered_codes": core_scope.get("core_filtered_codes") or [],
        "watchlist_sync": {
            "status": watchlist_sync.get("status"),
            "source": watchlist_sync.get("source"),
            "entry_allowed": universe_entry_allowed,
            "reason": universe_gate_reason,
            "errors": (watchlist_sync.get("errors") or [])[:3],
        },
        "official_market_data": official_market_data,
        "paper_watch_codes": sorted(paper_codes),
        "paper_added_to_universe": paper_added_codes,
        "tracked_opportunity_candidates": len(tracked_candidates),
        "tracked_opportunity_codes": sorted(tracked_codes),
        "tracked_opportunities_live": len(cache.get("tracked_radar_rows") or []),
        "observation_groups": len(observation_details.get("groups") or []),
        "observation_group_promoted": len(observation_candidates),
        "observation_plan": {
            "status": observation_plan.get("status"),
            "source": observation_plan.get("source"),
            "groups": [item.get("name") for item in observation_plan.get("groups") or []],
            "codes": [code for code, _market in observation_plan.get("rows") or []],
        },
        "can_attack": is_attack_allowed_by_global(now, global_context),
        "a_share_market_regime": a_share_market_regime,
        "market_breadth": market_breadth,
        "global_risk": global_context,
        "global_risk_error": cache.get("global_risk_error"),
        "paper_settle_error": cache.get("paper_settle_error"),
        "paper_event_warning": cache.get("paper_event_warning"),
        "radar_error": cache.get("radar_error"),
        "quote_error": cache.get("quote_error"),
        "local_market_data": market_data_state.get("minute_data_quality") or {},
        "daily_contract_revalidation": cache.get("daily_contract_revalidation_health") or {},
        "preopen_quality_gate": preopen_quality_entry_gate(now),
        "storage": storage,
        "leader_pool": leader_health,
    }
    # The three named methods are the only source of executable entry intent.
    # Shared timing still owns exits and closed-bar evidence.
    raw_signals = evaluate_v2_signals(rows + radar_rows, positions)
    raw_signals = block_new_entries_for_universe(
        raw_signals, universe_entry_allowed, universe_gate_reason
    )
    apply_runtime_quality(health)
    write_signal_audit_snapshot(raw_signals, health, cache, now)
    try:
        from core import strategy_gap_research
        strategy_gap_research.append_opening_snapshot(raw_signals, RUNTIME_DIR, cache, now)
    except Exception as exc:
        health['gap_research_audit'] = {'status': 'degraded', 'error': type(exc).__name__,
                                      'execution_affected': False}
    try:
        from core import shadow_research_reporting
        research_quotes = {row['code']: row['quote'] for row in rows + radar_rows}
        health['shadow_research'] = shadow_research_reporting.record_tick(
            BASE_DIR, raw_signals, research_quotes, now)
    except Exception as exc:
        health['shadow_research'] = {'status': 'error', 'error': type(exc).__name__,
                                     'execution_affected': False}
    radar_signals = [
        signal for signal in raw_signals
        if signal.get("symbol") in {row.get("code") for row in radar_rows}
        and signal.get("scenario") in intraday_timing_v2.ENTRY_SCENARIOS
    ]
    signals = select_primary_signals(raw_signals)
    names.update({row["code"]: row["quote"].get("name") or row["code"] for row in radar_rows})
    paper_price_map = intra.base.paper_position_price_map(build_paper_price_map(rows, radar_rows, signals))
    paper_positions_by_symbol = paper_position_map(paper_rows)
    portfolio_account = paper_trading.account_exposure_for_base(
        BASE_DIR,
        trading_date=intra.REPORT_DATE,
        price_map=paper_price_map,
        now=now,
    )
    portfolio_risk = {
        "active": False,
        "reason": "旧组合软风控已停用；持仓退出由共享结构风控的REDUCE/TAKE_PROFIT/STRUCTURAL_EXIT决定。",
    }
    paper_price_map = intra.base.paper_position_price_map(build_paper_price_map(rows, radar_rows, signals))
    for sig in signals:
        sig["global_risk_level"] = (global_context or {}).get("risk_level") or "green"
    pushes, transitions = process_state(
        con,
        signals,
        positions=positions,
        names=names,
        price_map=paper_price_map,
    )
    paper_fills = [s for s in signals if (s.get("paper_trade") or {}).get("status") in ("FILLED", "PARTIAL_FILLED")]
    health["signals"] = len(signals)
    health["raw_signals"] = len(raw_signals)
    health["entry_pipeline"] = entry_pipeline_summary(raw_signals, health)
    health["radar_actionable_signals"] = len(radar_signals)
    health["pushes"] = len(pushes)
    health["transitions"] = len(transitions)
    watch_candidates = critical_watch_candidates(signals)
    health["critical_watch_candidates"] = len(watch_candidates)
    paper_fills_today = paper_trading.count_filled_orders(BASE_DIR, trading_date=intra.REPORT_DATE)
    paper_orders_today = paper_trading.load_orders(BASE_DIR, trading_date=intra.REPORT_DATE)
    for side, key in (("BUY", "paper_buy_fills_today"), ("SELL", "paper_sell_fills_today")):
        health[key] = sum(1 for order in paper_orders_today
                          if order.get("side") == side and order.get("status") in {"FILLED", "PARTIAL_FILLED"})
    v2_rejections_today = sum(
        1 for order in paper_orders_today
        if order.get("status") == "REJECTED"
        and order.get("scenario") in intraday_timing_v2.ENTRY_SCENARIOS
    )
    radar_filled_today = len([
        item for item in paper_fills
        if item.get("scenario") in intraday_timing_v2.ENTRY_SCENARIOS
        and item.get("symbol") in {row.get("code") for row in radar_rows}
    ])
    health["paper_fills"] = paper_fills_today
    health["paper_fills_today"] = paper_fills_today
    health["paper_fills_current_tick"] = len(paper_fills)
    health["v2_execution_rejections_today"] = v2_rejections_today
    health["radar_sim_filled_today"] = radar_filled_today
    health["radar_sim_slots_left"] = "三策略不使用旧机会池日配额"
    health["market_opportunities"] = len(radar_rows)
    health["portfolio_risk_overlay"] = portfolio_risk
    health["market_profile_health"] = cache.get("market_profile_health") or {}
    health["board_rotation_health"] = {
        "coverage": next((board.get("_coverage") for board in cache.get("radar_boards") or [] if board.get("_coverage")), {}),
        "boards": len(cache.get("board_rotation_state") or {}),
        "confirmed": sum(
            1 for state in (cache.get("board_rotation_state") or {}).values()
            if state.get("sustained") and state.get("leader_healthy")
        ),
        "seed_error": cache.get("board_rotation_seed_error"),
    }
    try:
        health['board_identity_cache'] = sector_identity.update_catalog(BASE_DIR, cache.get('radar_boards') or [])
    except OSError:
        health['board_identity_cache'] = {'status': 'write_failed'}
    paper_snapshot = paper_trading.write_latest_snapshot(
        BASE_DIR,
        trading_date=intra.REPORT_DATE,
        price_map=paper_price_map,
        names=names,
        now=now,
    )
    health["paper_positions"] = len(paper_snapshot.get("positions") or [])
    health["paper_unrealized_pnl"] = (paper_snapshot.get("account") or {}).get("unrealized_pnl")
    health["paper_unrealized_pnl_pct"] = (paper_snapshot.get("account") or {}).get("unrealized_pnl_pct")
    health['ledger_quality'] = (paper_snapshot.get('account') or {}).get('ledger_quality') or {}
    quality_error = apply_runtime_quality(health)
    write_runtime_json(signals, health, radar_rows, paper_snapshot=paper_snapshot)
    dashboard_result = write_markdown(signals, health, radar_rows, paper_snapshot=paper_snapshot)
    risk_feishu_result = send_feishu_risk_sells(signals, health)
    feishu_result = send_feishu(pushes, health)
    critical_watch_feishu_result = send_feishu_critical_watch(watch_candidates, health)
    paper_feishu_result = send_feishu_paper_fills(paper_fills, health)
    paper_rejection_feishu_result = send_feishu_execution_rejections(pushes, health)
    shadow_feishu_result = send_feishu_shadow_research(now)
    log_event(event='shadow_research_notification', **shadow_feishu_result)
    # V2 no longer consumes legacy price-line signals, but the cache is
    # retained for compatible observability.  A fresh daemon starts empty.
    previous_prices = cache.setdefault("prev_prices", {})
    for row in rows:
        previous_prices[row["code"]] = row["quote"]["close"]
    elapsed = time.time() - start
    log_event(event="tick", signals=len(signals), pushes=len(pushes), transitions=len(transitions), paper_fills=len(paper_fills), market_opportunities=len(radar_rows), radar_actionable=len(radar_signals), critical_watch_candidates=len(watch_candidates), elapsed=round(elapsed, 2), risk_feishu_result=risk_feishu_result[:300], feishu_result=feishu_result[:300], critical_watch_feishu_result=critical_watch_feishu_result[:300], paper_feishu_result=paper_feishu_result[:300], paper_rejection_feishu_result=paper_rejection_feishu_result[:300])
    return {
        "updated_at": health["updated_at"],
        "engine_status": health['engine_status'],
        "quality_error": quality_error,
        "quality_warnings": health.get("quality_warnings") or [],
        "market_breadth": market_breadth,
        "daily_contract_revalidation": health.get("daily_contract_revalidation") or {},
        "minute_bar_latest_time": health.get("minute_bar_latest_time"),
        "last_success_at": health.get("updated_at"),
        "signals": len(signals),
        "pushes": len(pushes),
        "transitions": len(transitions),
        "paper_fills": len(paper_fills),
        "market_opportunities": len(radar_rows),
        "radar_actionable_signals": len(radar_signals),
        "market_profile_health": cache.get("market_profile_health") or {},
        "board_rotation_health": {
            "boards": len(cache.get("board_rotation_state") or {}),
            "confirmed": sum(
                1 for state in (cache.get("board_rotation_state") or {}).values()
                if state.get("sustained") and state.get("leader_healthy")
            ),
            "seed_error": cache.get("board_rotation_seed_error"),
        },
        "elapsed": round(elapsed, 2),
        "dashboard": dashboard_result,
        "risk_feishu_result": risk_feishu_result,
        "feishu_result": feishu_result,
        "critical_watch_feishu_result": critical_watch_feishu_result,
        "shadow_feishu_result": shadow_feishu_result,
        "paper_feishu_result": paper_feishu_result,
        "paper_rejection_feishu_result": paper_rejection_feishu_result,
    }


def write_health(status, reason=None):
    ensure_dirs()
    try:
        previous = json.loads(SIGNAL_HEALTH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        previous = {}
    timestamp = now_dt().strftime("%Y-%m-%d %H:%M:%S")
    payload = dict(previous)
    payload.update({
        "version": VERSION,
        "updated_at": timestamp,
        "engine_status": status,
        "reason": reason,
    })
    if status == "engine_error":
        payload["last_error"] = reason
        payload["last_error_at"] = timestamp
    SIGNAL_HEALTH.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main():
    parser = argparse.ArgumentParser(description="A-share realtime event-triggered signal engine")
    parser.add_argument("--interval", type=float, default=5.0, help="Quote polling interval in seconds")
    parser.add_argument("--bar-refresh", type=float, default=60.0, help="Minute-line refresh cadence; source refreshes once per completed minute")
    parser.add_argument("--radar-refresh", type=float, default=120.0, help="Full-market opportunity refresh interval in seconds")
    parser.add_argument("--radar-start-delay", type=float, default=180.0, help="Delay first full-market scan so core holdings can refresh first")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    con = init_db()
    cache = {}
    while True:
        if Path(__file__).stat().st_mtime_ns != ENGINE_SOURCE_MTIME_NS:
            write_health("restarting", "source_updated")
            log_event(event="exit", reason="source_updated")
            return 75
        now = now_dt()
        if not args.force:
            if not is_trading_day(now):
                write_health("idle", "non_trading_day")
                log_event(event="exit", reason="non_trading_day")
                return 0
            if now.time() >= dtime(15, 10):
                write_health("closed", "after_1510")
                log_event(event="exit", reason="after_1510")
                return 0
            if dtime(8, 0) <= now.time() < dtime(9, 30):
                path_state = refresh_global_path_only(cache, now)
                write_health("preopen_monitoring", cache.get("global_path_error"))
                log_event(
                    event="preopen_global_path",
                    samples=path_state.get("sample_count", 0),
                    event_state=path_state.get("event_state"),
                    recovery_confirmed=path_state.get("recovery_confirmed"),
                    error=cache.get("global_path_error"),
                )
                if args.once:
                    return 0
                time.sleep(args.interval)
                continue
            if not is_engine_session(now):
                write_health("idle", "outside_engine_session")
                log_event(event="idle", reason="outside_engine_session")
                if args.once:
                    return 0
                time.sleep(10)
                continue
            if not is_realtime_session(now):
                write_health("idle", "outside_realtime_session")
                log_event(event="idle", reason="outside_realtime_session")
                if args.once:
                    return 0
                time.sleep(10)
                continue
        try:
            result = run_tick(con, cache, args)
            alert_action, alert_state = engine_health_alert_transition(
                load_engine_health_alert_state(), now=now_dt(), error=result.get('quality_error'),
                incident_kind="data_quality",
            )
            save_engine_health_alert_state(alert_state)
            if alert_action:
                alert_result = send_feishu_engine_health_alert(alert_action, alert_state, result)
                log_event(event="engine_health_alert", action=alert_action, result=alert_result[:300])
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as exc:
            health = write_health("engine_error", str(exc))
            alert_action, alert_state = engine_health_alert_transition(
                load_engine_health_alert_state(), now=now_dt(), error=str(exc)
            )
            save_engine_health_alert_state(alert_state)
            if alert_action:
                alert_result = send_feishu_engine_health_alert(alert_action, alert_state, health)
                log_event(event="engine_health_alert", action=alert_action, result=alert_result[:300])
            log_event(event="error", error=str(exc))
            if args.once:
                raise
            time.sleep(5)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
