"""
Offline tests for scripts/run_pipeline.py's pure-Python pieces. The actual
subprocess orchestration (ingestion/export/analysis) and live Postgres
lookups are exercised by hand against a real database, not here -- same
split as tests/test_export_and_analysis.py and tests/test_benchmark.py.
"""
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from scripts.run_pipeline import parse_snapshot_id, default_snapshot_name


class TestParseSnapshotId(unittest.TestCase):
    def test_extracts_id_from_export_snapshot_stdout(self):
        stdout = "snapshot_id=42 written to snapshots\\polynetwork_1hop_v1.parquet\n"
        self.assertEqual(parse_snapshot_id(stdout), 42)

    def test_raises_a_clear_error_when_the_line_is_missing(self):
        with self.assertRaises(SystemExit):
            parse_snapshot_id("something unexpected happened, no snapshot line here")


class TestDefaultSnapshotName(unittest.TestCase):
    def test_is_unique_looking_and_timestamp_shaped(self):
        name = default_snapshot_name()
        self.assertTrue(name.startswith("pipeline_"))
        self.assertRegex(name, r"^pipeline_\d{8}T\d{6}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
