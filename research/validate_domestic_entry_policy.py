"""Frozen AM permission replay and scoped, lunch-only release of the risk fix."""
import argparse
from collections import Counter
from datetime import datetime, time
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install_launch_agent as install
from core import global_risk
from research.deploy_strategy_optimization import account_snapshot

FILES = ("core/global_risk.py", "realtime_signal_engine.py")
RUNTIME = install.RUNTIME_DIR


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(out, name, value):
    with (out / name).open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def replay(out):
    audit = RUNTIME / "data/runtime/signal_audit_20260914.jsonl"
    content = audit.read_bytes()
    snapshots = [json.loads(line) for line in content.splitlines()]
    snapshots = [s for s in snapshots if datetime.fromisoformat(s["decision_recorded_at"]).time() < time(12)]
    counts, nodes, candidate_nodes = Counter(), [], []
    for s in snapshots:
        # Availability is the decision time, not the earlier fetch start time.
        current = datetime.fromisoformat(s["decision_recorded_at"])
        ctx = global_risk.apply_a_share_entry_policy(s["global_risk"], s["market_state"], current)
        counts[ctx["entry_risk_source"]] += 1
        nodes.append({"decision_at": str(current), "before": s["entry_permission"],
                      "after_source": ctx["entry_risk_source"],
                      "after_evidence": ctx["a_share_entry_evidence"]})
        for r in s["rows"]:
            if not r.get("candidate_pattern"):
                continue
            remaining = [b for b in r.get("all_blockers", [])
                         if "[CORE_GLOBAL_RISK_BLOCKED]" not in b and "[GLOBAL_RISK_BLOCKED]" not in b]
            candidate_nodes.append({"decision_at": str(current), "symbol": r["symbol"], "name": r["name"],
                                    "method": r["strategy_key"], "remaining_blockers": remaining})
    result = {"date": "2026-09-14", "scope": "morning_frozen_permission_only_not_order_replay",
              "audit_sha256": hashlib.sha256(content).hexdigest(), "snapshots": len(nodes),
              "old_broad_blocks": sum(s["entry_permission"]["state"] == "risk_blocked" for s in snapshots),
              "new_permission_counts": dict(counts), "technical_candidate_nodes": len(candidate_nodes),
              "candidate_nodes_with_other_blockers": sum(bool(n["remaining_blockers"]) for n in candidate_nodes),
              "nodes": nodes, "candidates": candidate_nodes,
              "limits": "Only permission recomputed; other recorded blockers retained. Not a full strategy/fill replay or profitability estimate."}
    save(out, "morning_permission_replay.json", result)
    assert len(nodes) == 25 and counts["external_only"] == 24 and counts["a_share_systemic"] == 1
    assert all(n["remaining_blockers"] for n in candidate_nodes)
    return result


def tests(out, name, executable, runtime=None):
    command = ([str(executable), str(ROOT / "research/run_strategy_suite.py"), "--runtime", str(runtime)]
               if runtime else [str(executable), "-m", "unittest", "discover", "-s", "tests", "-q"])
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=180)
    save(out, name, {"exit_code": result.returncode, "output": result.stdout + result.stderr})
    assert result.returncode == 0, result.stdout + result.stderr


def protected_hashes():
    paths = list((RUNTIME / "configs").glob("*.json"))
    paths += list((RUNTIME / "data").rglob("*20260914*"))
    # Runtime tick/audit files are append-only operational logs, not frozen plans.
    paths = [p for p in paths if p.is_file() and ("plan" in p.name or "premarket" in p.name or "configs" in p.parts)]
    paths += [install.PLIST_PATH, install.REALTIME_PLIST_PATH]
    return {str(p): sha(p) for p in paths if p.is_file()}


def main(out, apply):
    baseline = json.loads((out / "baseline.json").read_text())
    changes = [{"path": rel, "before": baseline["runtime/" + rel], "after": sha(ROOT / rel)} for rel in FILES]
    assert all(sha(RUNTIME / c["path"]) == c["before"] for c in changes)
    tests(out, "source_tests.json", ROOT / ".venv_quant/bin/python")
    replay(out)
    stage = out / "staged_runtime"
    stage.mkdir()
    for rel in install.RUNTIME_FILES:
        shutil.copy2(RUNTIME / rel, stage / rel)
    for folder in ("core", "configs"):
        (stage / folder).mkdir()
        for p in (RUNTIME / folder).glob("*.py" if folder == "core" else "*.json"):
            shutil.copy2(p, stage / folder / p.name)
    for rel in FILES:
        shutil.copy2(ROOT / rel, stage / rel)
    tests(out, "staged_native_tests.json", install.PYTHON_APP_EXECUTABLE, stage)
    patch = "".join("".join(difflib.unified_diff(
        (out / "runtime" / rel).read_text().splitlines(True), (ROOT / rel).read_text().splitlines(True),
        fromfile="before/" + rel, tofile="after/" + rel)) for rel in FILES)
    with (out / "production.patch").open("x") as f:
        f.write(patch)
    original_account, protected = account_snapshot(), protected_hashes()
    save(out, "deployment_preview.json", {"changes": changes, "account": original_account, "protected": protected})
    if not apply:
        return
    assert datetime.now().date().isoformat() == "2026-09-14"
    assert time(11, 35) < datetime.now().time() < time(12, 55), "Only approved lunch window"
    assert sha(RUNTIME / "release_manifest.json") == baseline["runtime/release_manifest.json"]
    assert all(sha(RUNTIME / c["path"]) == c["before"] and sha(ROOT / c["path"]) == c["after"] for c in changes)
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{install.REALTIME_LABEL}"
    stopped = subprocess.run(["launchctl", "bootout", service], capture_output=True, text=True)
    save(out, "stop.json", {"exit_code": stopped.returncode, "output": stopped.stdout + stopped.stderr})
    assert stopped.returncode == 0, "Engine not stopped; deployment cancelled"
    # Fail closed: on any verification error leave service stopped for inspection.
    for rel in FILES:
        shutil.copy2(stage / rel, RUNTIME / rel)
    tests(out, "runtime_native_tests.json", install.PYTHON_APP_EXECUTABLE, RUNTIME)
    assert account_snapshot() == original_account
    assert protected_hashes() == protected
    assert all(sha(RUNTIME / c["path"]) == c["after"] for c in changes)
    old_manifest = json.loads((out / "runtime/release_manifest.json").read_text())
    manifest = install.release_manifest(RUNTIME)
    manifest["strategy_version"] = old_manifest.get("strategy_version")
    manifest["entry_policy_version"] = global_risk.LOCAL_ENTRY_POLICY_VERSION
    (RUNTIME / "release_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    started = subprocess.run(["launchctl", "bootstrap", domain, str(install.REALTIME_PLIST_PATH)], capture_output=True, text=True)
    save(out, "start.json", {"exit_code": started.returncode, "output": started.stdout + started.stderr})
    assert started.returncode == 0, "Release tested, but engine restart failed"
    save(out, "deployment.json", {"release_id": manifest["release_id"], "deployed_at": str(datetime.now()),
                                 "changes": changes, "entry_policy_version": global_risk.LOCAL_ENTRY_POLICY_VERSION,
                                 "account_unchanged": True, "plans_configs_schedules_unchanged": True,
                                 "manual_report_push_order_invoked": False, "engine_restarted": True})
    print(json.dumps({"release_id": manifest["release_id"], "engine_restarted": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(args.output.resolve(), args.apply)
