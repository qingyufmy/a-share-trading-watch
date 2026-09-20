"""Scoped lunch release; no report generation, notification, or order replay."""
import argparse
from datetime import datetime, time
import difflib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install_launch_agent as install
from research.validate_domestic_entry_policy import sha, save, tests, protected_hashes
from research.deploy_strategy_optimization import account_snapshot

FILES = ("core/global_risk.py", "core/intraday_timing_v2.py",
         "core/opportunity_rating.py", "core/signal_contract.py",
         "configs/intraday_timing_v2.json")
RUNTIME = install.RUNTIME_DIR


def protected():
    values = protected_hashes()
    values.pop(str(RUNTIME / "configs/intraday_timing_v2.json"), None)
    return values


def main(out, apply):
    baseline = json.loads((out / "baseline.json").read_text())
    changes = [{"path": rel, "before": baseline["runtime/" + rel],
                "after": sha(ROOT / rel)} for rel in FILES]
    assert all(sha(RUNTIME / c["path"]) == c["before"] for c in changes)
    tests(out, "source_tests.json", ROOT / ".venv_quant/bin/python")
    replay = json.loads((out / "morning_replay.json").read_text())
    assert replay["counts"]["candidate_nodes"] == 22
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
        (out / "runtime" / rel).read_text().splitlines(True),
        (ROOT / rel).read_text().splitlines(True),
        fromfile="before/" + rel, tofile="after/" + rel)) for rel in FILES)
    with (out / "production.patch").open("x") as f:
        f.write(patch)
    original_account, original_protected = account_snapshot(), protected()
    save(out, "deployment_preview.json", {"changes": changes, "account": original_account,
                                         "protected": original_protected})
    if not apply:
        return
    assert datetime.now().date().isoformat() == "2026-09-14"
    assert time(11, 35) < datetime.now().time() < time(12, 55), "Only approved lunch window"
    assert sha(RUNTIME / "release_manifest.json") == baseline["runtime/release_manifest.json"]
    assert all(sha(RUNTIME / c["path"]) == c["before"] and
               sha(ROOT / c["path"]) == c["after"] for c in changes)
    domain = f"gui/{os.getuid()}"
    stopped = subprocess.run(["launchctl", "bootout", f"{domain}/{install.REALTIME_LABEL}"],
                             capture_output=True, text=True)
    save(out, "stop.json", {"exit_code": stopped.returncode, "output": stopped.stdout + stopped.stderr})
    assert stopped.returncode == 0, "Engine not stopped; deployment cancelled"
    # Fail closed if installed code, account preservation, or native tests fail.
    for rel in FILES:
        shutil.copy2(stage / rel, RUNTIME / rel)
    tests(out, "runtime_native_tests.json", install.PYTHON_APP_EXECUTABLE, RUNTIME)
    assert account_snapshot() == original_account
    assert protected() == original_protected
    assert all(sha(RUNTIME / c["path"]) == c["after"] for c in changes)
    old_manifest = json.loads((out / "runtime/release_manifest.json").read_text())
    manifest = install.release_manifest(RUNTIME)
    for field in ("strategy_version", "entry_policy_version"):
        manifest[field] = old_manifest.get(field)
    manifest["entry_alignment_version"] = "sector_ma5_rr_20260914"
    (RUNTIME / "release_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    started = subprocess.run(["launchctl", "bootstrap", domain, str(install.REALTIME_PLIST_PATH)],
                             capture_output=True, text=True)
    save(out, "start.json", {"exit_code": started.returncode, "output": started.stdout + started.stderr})
    assert started.returncode == 0, "Engine restart failed"
    save(out, "deployment.json", {"release_id": manifest["release_id"],
                                  "deployed_at": str(datetime.now()), "changes": changes,
                                  "entry_alignment_version": manifest["entry_alignment_version"],
                                  "account_unchanged": True, "plans_schedules_other_configs_unchanged": True,
                                  "manual_report_push_order_invoked": False, "engine_restarted": True})
    print(json.dumps({"release_id": manifest["release_id"], "engine_restarted": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(args.output.resolve(), args.apply)
