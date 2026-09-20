"""Validate original audit evidence without querying revised minute databases."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import method_replay

FAILURES = {"integrity_failed", "mismatch", "invalid_input", "capture_error", "audit_integrity_failed",
            "malformed_record", "identity_mismatch"}


def validate(path):
    counts, issues = Counter(), []
    file_hash = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for line_number, line in enumerate(stream, 1):
            file_hash.update(line)
            try:
                sample = json.loads(line)
                rows = sample["rows"]
                if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                    raise ValueError("Invalid rows")
                recorded_hash = sample.get("input_sha256")
                if recorded_hash and recorded_hash != method_replay.digest(rows):
                    counts["audit_integrity_failed"] += 1
                    issues.append({"line": line_number, "status": "audit_integrity_failed"})
                    continue
                if not recorded_hash:
                    counts["audit_hash_missing"] += 1
                for row in rows:
                    if row.get("strategy_key") not in method_replay.METHODS or row.get("daily_qualified") is not True:
                        counts["out_of_scope"] += 1
                        continue
                    frozen = row.get("method_replay_input")
                    result = method_replay.verify(frozen)
                    if result["status"] == "matched" and (
                            frozen["contract"] != row.get("strategy_contract")
                            or frozen["price"] != row.get("price")):
                        result = {"status": "identity_mismatch"}
                    counts["evaluations"] += 1
                    counts[result["status"]] += 1
                    if result["status"] != "matched":
                        issues.append({"line": line_number, "at": sample.get("timestamp"),
                                       "symbol": row.get("symbol"), **result})
            except (ValueError, KeyError, TypeError):
                counts["malformed_record"] += 1
                issues.append({"line": line_number, "status": "malformed_record"})
    failed = any(counts[s] for s in FAILURES)
    complete = (counts["evaluations"] > 0 and counts["matched"] == counts["evaluations"]
                and not counts["audit_hash_missing"] and not failed)
    return {"path": str(path), "sha256": file_hash.hexdigest(),
            "status": "failed" if failed else "passed" if complete else "incomplete",
            "counts": dict(counts), "issues": issues,
            "scope": "Frozen 520/MA5 branch only; not leader, sector, order, fill or profit validation"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, help="New JSON file only; refuses existing paths")
    args = parser.parse_args()
    results = [validate(path) for path in args.audit]
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(results, stream, ensure_ascii=False, indent=2)
    print(json.dumps([{k: v for k, v in r.items() if k != "issues"} for r in results],
                     ensure_ascii=False, indent=2))
    return 1 if any(r["status"] == "failed" for r in results) else 2 if any(
        r["status"] == "incomplete" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
