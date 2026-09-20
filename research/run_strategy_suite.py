"""Run the full source test suite against explicitly selected runtime modules."""
import argparse
import json
import sys
import os
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--runtime', type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.runtime.resolve()))
sys.path.insert(1, str(ROOT))
from core import method_entry, intraday_timing_v2, observation_strategy_router
from core import strategy_gap_research, closed_liquidity
from core import shadow_research_reporting
import after_close_report
import realtime_signal_engine

paths = {m.__name__: str(Path(m.__file__).resolve()) for m in
         (method_entry, intraday_timing_v2, observation_strategy_router, realtime_signal_engine,
          strategy_gap_research, closed_liquidity, shadow_research_reporting, after_close_report)}
assert all(Path(p).is_relative_to(args.runtime.resolve()) for p in paths.values()), paths

blocked_writes = []


def protect_runtime(event, args_):
    if event != 'open' or not isinstance(args_[0], (str, bytes, os.PathLike)):
        return
    path = Path(os.fsdecode(args_[0])).resolve()
    flags = args_[2] if len(args_) > 2 else 0
    if (path.is_relative_to(args.runtime.resolve()) and '__pycache__' not in path.parts
            and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
        blocked_writes.append({'path': str(path), 'stack': traceback.format_stack(limit=10)})
        raise PermissionError('Runtime tests must not write production files: ' + str(path))


sys.addaudithook(protect_runtime)


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


all_tests = list(flatten(unittest.defaultTestLoader.discover(str(ROOT / 'tests'))))
excluded = [t for t in all_tests if t.id().startswith('test_install_launch_agent.')]
result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(t for t in all_tests if t not in excluded))
print(json.dumps({'module_paths': paths, 'tests': result.testsRun, 'passed': result.wasSuccessful() and not blocked_writes,
                  'excluded': [t.id() for t in excluded], 'blocked_runtime_writes': blocked_writes}, ensure_ascii=False))
sys.exit(0 if result.wasSuccessful() and not blocked_writes else 1)
