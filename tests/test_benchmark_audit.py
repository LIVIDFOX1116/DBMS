"""
Offline test for scripts/benchmark_audit.py's format_report() -- the one
pure function here. The actual benchmarking (sample_flag_ids,
benchmark_detector, cleanup_benchmark_audits) needs a real database with
real flags and was verified by hand against it instead -- see the commit
message for the real numbers (sybil_cluster 24.2 flags/min, pagerank_
threshold 18.8 flags/min on this CPU-only machine) and the cleanup
verification (flag_audit row count/max id confirmed unchanged after a
benchmark + its own cleanup pass).
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))

from benchmark_audit import format_report


class TestFormatReport(unittest.TestCase):
    def test_reports_median_and_flags_per_minute(self):
        report = format_report([("bfs_proximity", "CPU", [1.0, 2.0, 3.0])])
        self.assertIn("| bfs_proximity | CPU | 3 | 2.00 | 30.0 |", report)

    def test_detector_with_no_sampled_flags_reports_na_not_a_crash(self):
        report = format_report([("impurity", "CPU", [])])
        self.assertIn("| impurity | CPU | 0 | n/a | n/a (no flags found) |", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
