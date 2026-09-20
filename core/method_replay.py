"""Evidence-only replay of the daily-method branch, never an order backtest."""
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

from core import method_entry

SCHEMA = "daily_method_replay_v1"
METHODS = {"TREND_520", "TREND_MA5"}
try:
    EVALUATOR_SHA256 = hashlib.sha256(Path(method_entry.__file__).read_bytes()).hexdigest()
except OSError:
    EVALUATOR_SHA256 = None


def _default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError("Unsupported replay input type")


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                     allow_nan=False, default=_default).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def capture(contract, bars5, price, now, config, expected):
    payload = {"contract": contract, "bars5": bars5, "price": price,
               "now": now.isoformat(), "config": config, "expected": expected,
               "schema": SCHEMA, "evaluator_sha256": EVALUATOR_SHA256}
    try:
        # Detach evidence from mutable signal objects before later rendering.
        frozen = json.loads(json.dumps(payload, allow_nan=False, default=_default))
        frozen["sha256"] = digest(frozen)
        return frozen
    except (TypeError, ValueError, OverflowError):
        # Audit failure must not interrupt position-defense calculations.
        return {"schema": SCHEMA, "capture_error": "non_serializable_input"}


def verify(frozen):
    if not frozen:
        return {"status": "missing_input"}
    if not isinstance(frozen, dict):
        return {"status": "invalid_input"}
    if frozen.get("capture_error"):
        return {"status": "capture_error"}
    if frozen.get("schema") != SCHEMA:
        return {"status": "unversioned_input"}
    try:
        if frozen.get("sha256") != digest({k: v for k, v in frozen.items() if k != "sha256"}):
            return {"status": "integrity_failed"}
        if not EVALUATOR_SHA256 or not frozen.get("evaluator_sha256"):
            return {"status": "version_unavailable"}
        if frozen["evaluator_sha256"] != EVALUATOR_SHA256:
            return {"status": "version_mismatch"}
        contract = frozen["contract"]
        if contract.get("key") not in METHODS or contract.get("daily_qualified") is not True:
            return {"status": "unsupported_scope"}
        actual = method_entry.evaluate(contract, frozen["bars5"], frozen["price"],
                                       datetime.fromisoformat(frozen["now"]), frozen["config"])
        expected = frozen["expected"]
        if not isinstance(expected, dict):
            return {"status": "invalid_input"}
        changes = sorted(k for k in set(expected) | set(actual)
                         if k not in expected or k not in actual or digest(expected[k]) != digest(actual[k]))
        return {"status": "mismatch" if changes else "matched", "changed_fields": changes,
                "expected_eligible": expected.get("eligible"), "actual_eligible": actual.get("eligible")}
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return {"status": "invalid_input"}
