"""
Offline tests for scripts/benchmark.py's pure-Python pieces (timing helper +
report formatting). The live Postgres reads and the actual GraphBackend
timing runs are exercised by hand (see README/docs/benchmark_results.md),
not here -- same split as tests/test_export_and_analysis.py.
"""
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from scripts.benchmark import format_report, merge_json_run, time_it


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


class TestMergeJsonRun(unittest.TestCase):
    """docs/perf/benchmarks.json is shared: this script owns "runs",
    benchmark_crossover.py owns "crossover", and GET /benchmarks serves
    both."""

    def _tmp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        return pathlib.Path(self.tmp.name) / "benchmarks.json"

    def test_rebenchmarking_a_snapshot_replaces_its_row(self):
        path = self._tmp()
        merge_json_run(path, {"snapshot_id": 1, "edge_count": 10})
        runs = merge_json_run(path, {"snapshot_id": 1, "edge_count": 11})
        self.assertEqual(runs, [{"snapshot_id": 1, "edge_count": 11}])

    def test_rows_are_ordered_by_graph_size(self):
        path = self._tmp()
        merge_json_run(path, {"snapshot_id": 3, "edge_count": 900})
        runs = merge_json_run(path, {"snapshot_id": 2, "edge_count": 100})
        self.assertEqual([r["snapshot_id"] for r in runs], [2, 3])

    def test_the_crossover_section_survives_a_rerun(self):
        """It used to not: the file was rewritten as {"runs": ...} flat,
        which deleted the dashboard's crossover table."""
        path = self._tmp()
        path.write_text(json.dumps({
            "runs": [{"snapshot_id": 1, "edge_count": 10}],
            "crossover": {"measured_at": "2026-10-11", "sizes": [2000]},
        }), encoding="utf-8")
        merge_json_run(path, {"snapshot_id": 2, "edge_count": 20})
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["crossover"], {"measured_at": "2026-10-11", "sizes": [2000]})
        self.assertEqual([r["snapshot_id"] for r in document["runs"]], [1, 2])

    def test_an_unreadable_file_is_replaced_rather_than_crashing(self):
        path = self._tmp()
        path.write_text("not json at all", encoding="utf-8")
        runs = merge_json_run(path, {"snapshot_id": 1, "edge_count": 10})
        self.assertEqual(runs, [{"snapshot_id": 1, "edge_count": 10}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
