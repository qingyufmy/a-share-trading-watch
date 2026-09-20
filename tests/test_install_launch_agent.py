import json
import tempfile
import unittest
from pathlib import Path

import install_launch_agent as installer


class InstallLaunchAgentSnapshotTests(unittest.TestCase):
    def write_snapshot(self, path: Path, fetched_at: str, marker: str) -> None:
        path.write_text(
            json.dumps({"fetched_at": fetched_at, "marker": marker}),
            encoding="utf-8",
        )

    def test_runtime_snapshot_is_not_replaced_by_same_time_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            destination = root / "destination.json"
            self.write_snapshot(source, "2026-09-03 15:00:00", "old-selector")
            self.write_snapshot(destination, "2026-09-03 15:00:00", "live-selector")

            copied = installer.copy_snapshot_if_fresher(source, destination)

            self.assertFalse(copied)
            self.assertEqual(json.loads(destination.read_text())["marker"], "live-selector")

    def test_newer_source_snapshot_is_deployed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            destination = root / "destination.json"
            self.write_snapshot(source, "2026-09-04 15:00:00", "new-day")
            self.write_snapshot(destination, "2026-09-03 15:00:00", "prior-day")

            copied = installer.copy_snapshot_if_fresher(source, destination)

            self.assertTrue(copied)
            self.assertEqual(json.loads(destination.read_text())["marker"], "new-day")


if __name__ == "__main__":
    unittest.main()
