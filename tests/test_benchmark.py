"""
Offline tests for scripts/benchmark.py's pure-Python pieces (timing helper +
report formatting). The live Postgres reads and the actual GraphBackend
timing runs are exercised by hand (see README/docs/benchmark_results.md),
not here -- same split as tests/test_export_and_analysis.py.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from scripts.benchmark import time_it, format_report


class TestTimeIt(unittest.TestCase):
    def test_returns_one_sample_per_repeat_and_the_last_result(self):
        calls = []

        def fn():
            calls.append(len(calls) + 1)
            return calls[-1]

        samples, result = time_it(fn, repeats=4)
        self.assertEqual(len(samples), 4)
        self.assertTrue(all(s >= 0 for s in samples))
        self.assertEqual(calls, [1, 2, 3, 4])  # confirms fn() actually ran 4 times, in order
        self.assertEqual(result, 4)  # last call's return value


class TestFormatReport(unittest.TestCase):
    def _fake_result(self, name, base_seconds):
        return {
            "backend_name": name,
            "timings": {
                "load_edgelist": [base_seconds],
                "pagerank": [base_seconds * 2],
                "louvain": [base_seconds * 10],
                "bfs": [base_seconds * 0.1],
            },
            "pagerank_top_wallet": 1,
            "louvain_communities": 3,
            "louvain_modularity": 0.5,
            "bfs_reached": 7,
        }

    def test_single_backend_report_has_no_speedup_section(self):
        cpu = self._fake_result("pandas+networkx (CPU)", 1.0)
        report = format_report("snap", 100, 200, seed_wallet_id=1, repeats=1, results=[cpu])
        self.assertIn("# Benchmark: snap", report)
        self.assertIn("pandas+networkx (CPU)", report)
        self.assertNotIn("Speedup", report)

    def test_two_backend_report_computes_speedup(self):
        cpu = self._fake_result("pandas+networkx (CPU)", 1.0)
        gpu = self._fake_result("cudf+cugraph (GPU)", 0.5)  # exactly 2x faster on every stage
        report = format_report("snap", 100, 200, seed_wallet_id=1, repeats=1, results=[cpu, gpu])
        self.assertIn("## Speedup (GPU vs CPU, median)", report)
        self.assertIn("pagerank: 2.00x", report)
        self.assertIn("louvain: 2.00x", report)

    def test_repeats_greater_than_one_includes_range_and_methodology_note(self):
        cpu = self._fake_result("pandas+networkx (CPU)", 1.0)
        cpu["timings"]["pagerank"] = [3.0, 0.9, 0.95]  # first call inflated, like the real backend
        report = format_report("snap", 100, 200, seed_wallet_id=1, repeats=3, results=[cpu])
        self.assertIn("range", report)
        self.assertIn("steady-state figure", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
