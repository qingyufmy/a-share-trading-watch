import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from core import method_entry, method_replay
import realtime_signal_engine as engine
from research.verify_frozen_method_replay import validate
from test_afterclose_iteration import contract, bars, NOW
import test_strategy_optimization as optimization


class FrozenMethodReplayTests(unittest.TestCase):
    def evidence(self, key="TREND_MA5", price=10.04):
        c = contract(key)
        if key == "TREND_520":
            c["daily_metrics"]["ma20_pullback_reclaim"] = True
        b = bars()
        expected = method_entry.evaluate(c, b, price, NOW, {})
        return method_replay.capture(c, b, price, NOW, {}, expected)

    def report(self, evidence=None):
        frozen = self.evidence() if evidence is None else evidence
        row = {"symbol": "000001", "strategy_key": "TREND_MA5", "daily_qualified": True,
               "price": 10.04, "strategy_contract": contract(), "method_replay_input": frozen}
        sample = {"timestamp": str(NOW), "rows": [row]}
        sample["input_sha256"] = method_replay.digest(sample["rows"])
        return sample

    def run_report(self, samples):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            with path.open("x") as stream:
                for sample in samples:
                    stream.write(json.dumps(sample) + "\n")
            before = path.read_bytes()
            result = validate(path)
            self.assertEqual(path.read_bytes(), before)
            return result

    def test_positive_methods_survive_serialization(self):
        for key in ("TREND_MA5", "TREND_520"):
            frozen = json.loads(json.dumps(self.evidence(key)))
            self.assertTrue(frozen["expected"]["eligible"])
            self.assertEqual(method_replay.verify(frozen)["status"], "matched")

    def test_negative_decision_is_also_reproduced(self):
        frozen = self.evidence(price=9.99)
        self.assertFalse(frozen["expected"]["eligible"])
        self.assertEqual(method_replay.verify(frozen)["status"], "matched")

    def test_capture_detaches_mutable_objects(self):
        c, b = contract(), bars()
        expected = method_entry.evaluate(c, b, 10.04, NOW, {})
        frozen = method_replay.capture(c, b, 10.04, NOW, {}, expected)
        c["daily_metrics"]["close_sum4"] = 0
        b[0]["low"] = 0
        expected["eligible"] = False
        self.assertEqual(method_replay.verify(frozen)["status"], "matched")

    def test_changed_price_or_expected_fails_integrity(self):
        for key in ("price", "expected"):
            frozen = self.evidence()
            frozen[key] = 0
            self.assertEqual(method_replay.verify(frozen)["status"], "integrity_failed")

    def test_version_change_not_mislabeled_as_logic_failure(self):
        frozen = self.evidence()
        with patch.object(method_replay, "EVALUATOR_SHA256", "different"):
            self.assertEqual(method_replay.verify(frozen)["status"], "version_mismatch")

    def test_logic_difference_reports_exact_fields(self):
        frozen = self.evidence()
        changed = {**frozen["expected"], "eligible": False}
        with patch.object(method_entry, "evaluate", return_value=changed):
            result = method_replay.verify(frozen)
        self.assertEqual(result["status"], "mismatch")
        self.assertEqual(result["changed_fields"], ["eligible"])

    def test_nonfinite_capture_does_not_interrupt_engine(self):
        frozen = method_replay.capture(contract(), bars(), float("nan"), NOW, {}, {})
        self.assertEqual(method_replay.verify(frozen)["status"], "capture_error")

    def test_missing_and_legacy_inputs_never_pass(self):
        self.assertEqual(method_replay.verify({})["status"], "missing_input")
        frozen = self.evidence()
        frozen.pop("schema")
        self.assertEqual(method_replay.verify(frozen)["status"], "unversioned_input")

    def test_real_timing_path_records_matching_input(self):
        result = optimization.TimingConsistencyTests().evaluate()
        frozen = json.loads(json.dumps(result["method_replay_input"]))
        self.assertEqual(method_replay.verify(frozen)["status"], "matched")
        self.assertTrue(result["entry_allowed"])

    def test_readonly_batch_passes_with_original_evidence(self):
        self.assertEqual(self.run_report([self.report()])["status"], "passed")

    def test_engine_audit_file_is_accepted_and_bucket_is_deduplicated(self):
        timing = optimization.TimingConsistencyTests().evaluate()
        signal = {"symbol": "000001", "current_price": 10.04, "strategy_key": "TREND_MA5",
                  "strategy_daily_qualified": True, "strategy_contract": contract(), "timing_v2": timing}
        with TemporaryDirectory() as tmp, patch.object(engine, "SIGNAL_AUDIT_LOG", Path(tmp) / "audit.jsonl"):
            cache = {}
            self.assertTrue(engine.write_signal_audit_snapshot([signal], {}, cache, NOW))
            self.assertFalse(engine.write_signal_audit_snapshot([signal], {}, cache, NOW))
            result = validate(engine.SIGNAL_AUDIT_LOG)
            self.assertEqual(result["status"], "passed", result)
            self.assertEqual(result["counts"]["matched"], 1)

    def test_historical_missing_input_is_incomplete(self):
        result = self.run_report([self.report({})])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["counts"]["missing_input"], 1)

    def test_no_scope_and_empty_file_are_incomplete(self):
        self.assertEqual(self.run_report([])["status"], "incomplete")
        sample = self.report()
        sample["rows"][0]["strategy_key"] = "LEADER_EMOTION"
        sample["input_sha256"] = method_replay.digest(sample["rows"])
        self.assertEqual(self.run_report([sample])["status"], "incomplete")

    def test_audit_corruption_fails(self):
        sample = self.report()
        sample["rows"][0]["price"] = 999
        self.assertEqual(self.run_report([sample])["status"], "failed")

    def test_mismatched_row_contract_fails_even_with_valid_row_hash(self):
        sample = self.report()
        sample["rows"][0]["strategy_contract"] = {}
        sample["input_sha256"] = method_replay.digest(sample["rows"])
        result = self.run_report([sample])
        self.assertEqual(result["counts"]["identity_mismatch"], 1)

    def test_missing_audit_hash_cannot_pass(self):
        sample = self.report()
        sample.pop("input_sha256")
        self.assertEqual(self.run_report([sample])["status"], "incomplete")

    def test_malformed_records_do_not_hide_later_valid_samples(self):
        result = self.run_report([{"rows": None}, self.report()])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["counts"]["matched"], 1)


if __name__ == "__main__":
    unittest.main()
