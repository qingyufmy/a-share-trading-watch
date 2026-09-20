"""Per-symbol V2 tracking state and append-only material event ledger."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import sqlite3
from typing import Any, Iterable


ENTRY_SCENARIOS = {
    "STRATEGY_LEADER_ENTRY",
    "STRATEGY_520_ENTRY",
    "STRATEGY_MA5_ENTRY",
}
EXIT_SCENARIOS = {"V2_REDUCE", "V2_TAKE_PROFIT", "V2_STRUCTURAL_EXIT"}
TERMINAL_STATES = {"CLOSED", "CANCELLED", "EXPIRED"}


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_tracks (
          trading_date TEXT NOT NULL,
          symbol TEXT NOT NULL,
          track_id TEXT NOT NULL,
          state TEXT NOT NULL,
          grade TEXT,
          score INTEGER,
          scenario TEXT,
          current_price REAL,
          trigger_distance_pct REAL,
          distance_bucket TEXT,
          hard_veto_json TEXT,
          gaps_json TEXT,
          data_timestamp TEXT,
          config_hash TEXT,
          first_seen_at TEXT NOT NULL,
          last_state_change_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          material_fingerprint TEXT,
          PRIMARY KEY (trading_date, symbol),
          UNIQUE (track_id)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_track_events (
          event_id TEXT PRIMARY KEY,
          track_id TEXT NOT NULL,
          trading_date TEXT NOT NULL,
          symbol TEXT NOT NULL,
          old_state TEXT,
          new_state TEXT NOT NULL,
          event_type TEXT NOT NULL,
          grade TEXT,
          score INTEGER,
          scenario TEXT,
          price REAL,
          trigger_distance_pct REAL,
          distance_bucket TEXT,
          hard_veto_json TEXT,
          gaps_json TEXT,
          data_timestamp TEXT,
          config_hash TEXT,
          payload_json TEXT,
          created_at TEXT NOT NULL
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_signal_track_events_symbol ON signal_track_events(trading_date, symbol, created_at)")
    con.commit()


def track_id(trading_date: str, symbol: str) -> str:
    digest = hashlib.sha1(f"{trading_date}:{symbol}".encode("utf-8")).hexdigest()[:16]
    return f"trk_{trading_date.replace('-', '')}_{symbol}_{digest}"


def load_track(con: sqlite3.Connection, trading_date: str, symbol: str) -> dict[str, Any]:
    row = con.execute(
        """SELECT track_id, state, grade, score, scenario, current_price,
                  trigger_distance_pct, distance_bucket, hard_veto_json, gaps_json,
                  data_timestamp, config_hash, first_seen_at, last_state_change_at,
                  updated_at, material_fingerprint
           FROM signal_tracks WHERE trading_date=? AND symbol=?""",
        (trading_date, symbol),
    ).fetchone()
    if not row:
        return {}
    keys = (
        "track_id", "state", "grade", "score", "scenario", "current_price",
        "trigger_distance_pct", "distance_bucket", "hard_veto_json", "gaps_json",
        "data_timestamp", "config_hash", "first_seen_at", "last_state_change_at",
        "updated_at", "material_fingerprint",
    )
    item = dict(zip(keys, row))
    item["hard_veto"] = _json(item.pop("hard_veto_json"), [])
    item["gaps"] = _json(item.pop("gaps_json"), [])
    return item


def _json(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return default


def _distance_pct(signal: dict[str, Any]) -> float | None:
    direct = signal.get("distance_pct")
    if direct is not None:
        try:
            return round(float(direct), 4)
        except (TypeError, ValueError):
            pass
    trigger = signal.get("trigger_price")
    current = signal.get("current_price")
    try:
        if trigger and current:
            return round((float(current) - float(trigger)) / float(trigger) * 100, 4)
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return None


def distance_bucket(distance: float | None, rating: dict[str, Any]) -> str:
    if distance is not None:
        absolute = abs(distance)
        if absolute <= 0.25:
            return "AT_TRIGGER"
        if absolute <= 0.75:
            return "NEAR_0_75"
        if absolute <= 1.5:
            return "NEAR_1_5"
        return "FAR"
    soft_count = int(rating.get("soft_gap_count") or 0)
    if not rating.get("hard_vetoed") and soft_count <= 1:
        return "ONE_GATE_LEFT"
    if not rating.get("hard_vetoed") and soft_count <= 2:
        return "TWO_GATES_LEFT"
    return "UNMEASURED"


def _has_veto(rating: dict[str, Any], code: str) -> bool:
    return any(str(item.get("code")) == code for item in rating.get("hard_veto") or [])


def state_for(signal: dict[str, Any], rating: dict[str, Any], paper: dict[str, Any] | None = None) -> str:
    paper = paper or {}
    scenario = str(signal.get("scenario") or "")
    paper_status = str(paper.get("status") or "")
    if paper_status == "FILLED":
        return "FILLED"
    if paper_status == "PARTIAL_FILLED":
        return "PARTIAL"
    if paper_status in {"UNFILLED", "REJECTED"}:
        return "UNFILLED"
    if paper_status == "CANCELLED":
        return "CANCELLED"
    if _has_veto(rating, "DATA"):
        return "DATA_BLOCKED"
    if _has_veto(rating, "LIMIT_LOCKED"):
        return "LIMIT_LOCKED"
    timing = signal.get("timing_v2") or {}
    if timing.get("trigger_missed") and signal.get("previous_formal_entry_at") and not rating.get("hard_vetoed"):
        return "TRIGGERED_BUT_MISSED"
    if _has_veto(rating, "EXCHANGE_RULE"):
        return "EXPIRED"
    if scenario == "V2_STRUCTURAL_EXIT":
        return "EXIT"
    if scenario in {"V2_REDUCE", "V2_TAKE_PROFIT"}:
        return "REDUCE"
    if scenario == "V2_NO_ADD":
        return "MANAGING_REENTRY" if str(timing.get("setup_15m") or "WAIT") != "WAIT" and not timing.get("path_hard_block") else "MANAGING"
    if scenario in ENTRY_SCENARIOS and signal.get("external_status") == "立即处理":
        return "TRIGGERED"
    if rating.get("hard_vetoed"):
        return "DISCOVERED"
    grade = str(rating.get("grade") or "D")
    soft_count = int(rating.get("soft_gap_count") or 0)
    if grade in {"A", "B"} and soft_count <= 2:
        return "NEAR_TRIGGER"
    if str(timing.get("setup_15m") or "WAIT") != "WAIT" or grade in {"A", "B", "C"}:
        return "SETUP_FORMING"
    if grade == "D":
        return "DISCOVERED"
    return "QUALIFIED"


def states_for_tick(signal: dict[str, Any], rating: dict[str, Any], paper: dict[str, Any] | None = None) -> list[str]:
    final = state_for(signal, rating, paper)
    scenario = str(signal.get("scenario") or "")
    if scenario in ENTRY_SCENARIOS and signal.get("external_status") == "立即处理":
        if final in {"FILLED", "PARTIAL", "UNFILLED"}:
            return ["TRIGGERED", "ORDER_PENDING", final]
        if final == "TRIGGERED":
            return ["TRIGGERED"]
    if scenario in EXIT_SCENARIOS and final in {"FILLED", "PARTIAL", "UNFILLED"}:
        intent = "EXIT" if scenario == "V2_STRUCTURAL_EXIT" else "REDUCE"
        return [intent, "ORDER_PENDING", final]
    return [final]


def _compact_gaps(rating: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"category": str(item.get("category") or "OTHER"), "reason": str(item.get("reason") or "")}
        for item in (rating.get("soft_gaps") or [])[:8]
    ]


def _fingerprint(snapshot: dict[str, Any]) -> str:
    gaps = sorted({str(item.get("category") or "OTHER") for item in snapshot.get("gaps") or []})
    veto = sorted({str(item.get("code") or "OTHER") for item in snapshot.get("hard_veto") or []})
    payload = {
        "state": snapshot.get("state"),
        "grade": snapshot.get("grade"),
        "scenario": snapshot.get("scenario"),
        "distance_bucket": snapshot.get("distance_bucket"),
        "gaps": gaps,
        "hard_veto": veto,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:20]


def build_snapshot(signal: dict[str, Any], rating: dict[str, Any], state: str) -> dict[str, Any]:
    distance = _distance_pct(signal)
    timing = signal.get("timing_v2") or {}
    data_time = ((timing.get("source_bar_close") or {}).get("5m") or signal.get("updated_at"))
    snapshot = {
        "track_id": track_id(str(signal.get("trading_date") or ""), str(signal.get("symbol") or "")),
        "trading_date": str(signal.get("trading_date") or ""),
        "symbol": str(signal.get("symbol") or ""),
        "state": state,
        "grade": str(rating.get("grade") or "D"),
        "score": int(rating.get("score") or 0),
        "scenario": str(signal.get("scenario") or ""),
        "current_price": signal.get("current_price"),
        "trigger_distance_pct": distance,
        "distance_bucket": distance_bucket(distance, rating),
        "hard_veto": list(rating.get("hard_veto") or []),
        "gaps": _compact_gaps(rating),
        "data_timestamp": str(data_time or ""),
        "config_hash": str(timing.get("config_hash") or signal.get("timing_v2_config_hash") or ""),
        "updated_at": str(signal.get("updated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    }
    snapshot["material_fingerprint"] = _fingerprint(snapshot)
    return snapshot


def _event_type(previous: dict[str, Any], snapshot: dict[str, Any]) -> str:
    if not previous:
        return "track_started"
    if previous.get("state") != snapshot.get("state"):
        return "state_changed"
    if previous.get("grade") != snapshot.get("grade"):
        return "grade_changed"
    if previous.get("distance_bucket") != snapshot.get("distance_bucket"):
        return "distance_changed"
    return "gaps_changed"


def upsert_track(
    con: sqlite3.Connection,
    signal: dict[str, Any],
    rating: dict[str, Any],
    states: Iterable[str],
) -> list[dict[str, Any]]:
    ensure_schema(con)
    events = []
    for state in states:
        snapshot = build_snapshot(signal, rating, state)
        previous = load_track(con, snapshot["trading_date"], snapshot["symbol"])
        if previous.get("material_fingerprint") == snapshot["material_fingerprint"]:
            con.execute(
                "UPDATE signal_tracks SET current_price=?, data_timestamp=?, updated_at=? WHERE trading_date=? AND symbol=?",
                (snapshot["current_price"], snapshot["data_timestamp"], snapshot["updated_at"], snapshot["trading_date"], snapshot["symbol"]),
            )
            continue
        event_type = _event_type(previous, snapshot)
        state_changed_at = snapshot["updated_at"] if previous.get("state") != state else previous.get("last_state_change_at") or snapshot["updated_at"]
        first_seen = previous.get("first_seen_at") or snapshot["updated_at"]
        con.execute(
            """
            INSERT INTO signal_tracks (
              trading_date, symbol, track_id, state, grade, score, scenario, current_price,
              trigger_distance_pct, distance_bucket, hard_veto_json, gaps_json,
              data_timestamp, config_hash, first_seen_at, last_state_change_at,
              updated_at, material_fingerprint
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trading_date, symbol) DO UPDATE SET
              state=excluded.state, grade=excluded.grade, score=excluded.score,
              scenario=excluded.scenario, current_price=excluded.current_price,
              trigger_distance_pct=excluded.trigger_distance_pct,
              distance_bucket=excluded.distance_bucket,
              hard_veto_json=excluded.hard_veto_json, gaps_json=excluded.gaps_json,
              data_timestamp=excluded.data_timestamp, config_hash=excluded.config_hash,
              last_state_change_at=excluded.last_state_change_at,
              updated_at=excluded.updated_at, material_fingerprint=excluded.material_fingerprint
            """,
            (
                snapshot["trading_date"], snapshot["symbol"], snapshot["track_id"], state,
                snapshot["grade"], snapshot["score"], snapshot["scenario"], snapshot["current_price"],
                snapshot["trigger_distance_pct"], snapshot["distance_bucket"],
                json.dumps(snapshot["hard_veto"], ensure_ascii=False), json.dumps(snapshot["gaps"], ensure_ascii=False),
                snapshot["data_timestamp"], snapshot["config_hash"], first_seen, state_changed_at,
                snapshot["updated_at"], snapshot["material_fingerprint"],
            ),
        )
        event_id_raw = f"{snapshot['track_id']}:{snapshot['updated_at']}:{state}:{snapshot['material_fingerprint']}"
        event_id = hashlib.sha1(event_id_raw.encode("utf-8")).hexdigest()
        payload = {"tracking": snapshot, "rating": rating}
        con.execute(
            """INSERT OR IGNORE INTO signal_track_events VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id, snapshot["track_id"], snapshot["trading_date"], snapshot["symbol"],
                previous.get("state") or "IDLE", state, event_type, snapshot["grade"], snapshot["score"],
                snapshot["scenario"], snapshot["current_price"], snapshot["trigger_distance_pct"],
                snapshot["distance_bucket"], json.dumps(snapshot["hard_veto"], ensure_ascii=False),
                json.dumps(snapshot["gaps"], ensure_ascii=False), snapshot["data_timestamp"],
                snapshot["config_hash"], json.dumps(payload, ensure_ascii=False), snapshot["updated_at"],
            ),
        )
        events.append({
            "track_id": snapshot["track_id"], "symbol": snapshot["symbol"],
            "from": previous.get("state") or "IDLE", "to": state,
            "event_type": event_type, "grade": snapshot["grade"], "score": snapshot["score"],
        })
    con.commit()
    return events
