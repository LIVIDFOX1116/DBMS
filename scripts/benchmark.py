"""
Phase 5 in the project bible: a GPU-vs-CPU/NetworkX runtime comparison for
the same subgraph, across PageRank, Louvain, and BFS -- real numbers for
this team's own hardware, not a marketing claim (Section 1).

    python scripts/benchmark.py
    python scripts/benchmark.py --snapshot-id 1 --repeats 5 --output docs/benchmark_results.md

Runs graph/backend.py's CPU (pandas + NetworkX) path always, and its GPU
(cudf + cugraph) path too whenever RAPIDS is installed -- i.e. on the RTX
4070 / GTX 1650 laptops, not the Zenbook. On a machine without RAPIDS this
still runs and prints the CPU numbers, with a note that the GPU column needs
a RAPIDS-capable machine to fill in.

Deliberately read-only against Postgres (SELECT only, no INSERT/UPDATE): this
is supporting evidence, not part of the pipeline itself (see the bible's
descope order, 8.2) -- persisting real results to analysis_run/vertex_metric
is run_analysis.py's job, not this script's.
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import psycopg2

from ingestion import config
from graph.backend import GraphBackend, gpu_available

ALGORITHMS = ("load_edgelist", "pagerank", "louvain", "bfs")


def resolve_snapshot_id(conn, snapshot_id):
    if snapshot_id is not None:
        return snapshot_id
    with conn.cursor() as cur:
        cur.execute("SELECT snapshot_id FROM graph_snapshot ORDER BY created_at DESC LIMIT 1")
        row = cur.fetchone()
        if row is None:
            raise SystemExit("No graph_snapshot rows found -- run graph/export_snapshot.py first.")
        return row[0]


def resolve_seed_wallet_id(conn, seed_wallet_id):
    if seed_wallet_id is not None:
        return seed_wallet_id
    with conn.cursor() as cur:
        cur.execute("SELECT wallet_id FROM wallet WHERE address = lower(%s)", (config.CASE_STUDY_ADDRESS,))
        row = cur.fetchone()
        if row is None:
            raise SystemExit(
                f"Case-study address {config.CASE_STUDY_ADDRESS} not found in wallet table -- "
                "pass --seed-wallet-id explicitly."
            )
        return row[0]


def load_snapshot(conn, snapshot_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT snapshot_name, file_path, vertex_count, edge_count FROM graph_snapshot WHERE snapshot_id = %s",
            (snapshot_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise SystemExit(f"No graph_snapshot with id {snapshot_id}")
    snapshot_name, file_path, vertex_count, edge_count = row
    return snapshot_name, pd.read_parquet(file_path), vertex_count, edge_count


def time_it(fn, repeats):
    """Runs fn() `repeats` times (each call is real, independent work -- no
    warm-start reuse) and returns (samples_seconds, last_result). The first
    sample in-process is often noticeably slower than the rest (one-time
    library import / OS disk-cache costs, not the algorithm itself) -- kept
    in the samples list rather than discarded, since run_analysis.py pays
    that same first-call cost once per real pipeline invocation."""
    samples = []
    result = None
    for _ in range(repeats):
        t0 = time.perf_counter()
        result = fn()
        samples.append(time.perf_counter() - t0)
    return samples, result


def run_backend_suite(force_cpu, edges_df, seed_wallet_id, repeats):
    backend = GraphBackend(force_cpu=force_cpu)
    timings = {}

    samples, G = time_it(lambda: backend.load_edgelist(edges_df), repeats)
    timings["load_edgelist"] = samples
    samples, pr_df = time_it(lambda: backend.pagerank(G), repeats)
    timings["pagerank"] = samples
    samples, (lv_df, modularity) = time_it(lambda: backend.louvain(G), repeats)
    timings["louvain"] = samples
    samples, bfs_df = time_it(lambda: backend.bfs(G, seed_wallet_id), repeats)
    timings["bfs"] = samples

    summary = {
        "backend_name": backend.backend_name,
        "timings": timings,
        "pagerank_top_wallet": int(pr_df.iloc[0].wallet_id),
        "louvain_communities": int(lv_df["community_label"].nunique()),
        "louvain_modularity": modularity,
        "bfs_reached": len(bfs_df),
    }
    return summary


def format_report(snapshot_name, vertex_count, edge_count, seed_wallet_id, repeats, results):
    lines = []
    lines.append(f"# Benchmark: {snapshot_name}")
    lines.append("")
    lines.append(f"Subgraph: {vertex_count} vertices, {edge_count} edges. "
                  f"BFS seed wallet_id={seed_wallet_id}. Each stage timed {repeats} time(s); "
                  f"median and range reported below.")
    lines.append("")

    header = "| Stage | " + " | ".join(r["backend_name"] for r in results) + " |"
    sep = "|---|" + "---|" * len(results)
    lines.append(header)
    lines.append(sep)
    for stage in ALGORITHMS:
        cells = []
        for r in results:
            samples = r["timings"][stage]
            med = statistics.median(samples) * 1000
            if len(samples) > 1:
                cells.append(f"{med:.1f} ms median (range {min(samples)*1000:.1f}-{max(samples)*1000:.1f} ms, n={len(samples)})")
            else:
                cells.append(f"{med:.1f} ms")
        lines.append(f"| {stage} | " + " | ".join(cells) + " |")
    lines.append("")
    if repeats > 1:
        lines.append("Median is the steady-state figure to compare across backends; the range shows "
                      "how much the first in-process call can be inflated by one-time library-import / "
                      "OS disk-cache costs rather than the algorithm itself.")
        lines.append("")

    if len(results) == 2:
        cpu, gpu = results
        lines.append("## Speedup (GPU vs CPU, median)")
        lines.append("")
        for stage in ALGORITHMS:
            cpu_med = statistics.median(cpu["timings"][stage])
            gpu_med = statistics.median(gpu["timings"][stage])
            speedup = cpu_med / gpu_med if gpu_med > 0 else float("inf")
            lines.append(f"- {stage}: {speedup:.2f}x")
        lines.append("")

    lines.append("## Result agreement (sanity check)")
    lines.append("")
    for r in results:
        lines.append(
            f"- **{r['backend_name']}**: top PageRank wallet_id={r['pagerank_top_wallet']}, "
            f"{r['louvain_communities']} Louvain communities (modularity {r['louvain_modularity']:.4f}), "
            f"BFS reached {r['bfs_reached']} wallets"
        )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-id", type=int, default=None, help="defaults to the most recently created snapshot")
    parser.add_argument("--seed-wallet-id", type=int, default=None, help="defaults to the case-study address's wallet_id")
    parser.add_argument("--repeats", type=int, default=3, help="timed repetitions per stage (default: 3)")
    parser.add_argument("--force-cpu-only", action="store_true", help="skip the GPU pass even if RAPIDS is installed")
    parser.add_argument("--output", type=str, default=None, help="also write the report to this file")
    args = parser.parse_args()

    conn = psycopg2.connect(**config.PG_DSN)
    try:
        snapshot_id = resolve_snapshot_id(conn, args.snapshot_id)
        seed_wallet_id = resolve_seed_wallet_id(conn, args.seed_wallet_id)
        snapshot_name, edges_df, vertex_count, edge_count = load_snapshot(conn, snapshot_id)
    finally:
        conn.close()  # read-only lookups done -- no need to hold the connection during timing

    print(f"Benchmarking snapshot '{snapshot_name}' ({vertex_count} vertices, {edge_count} edges), "
          f"{args.repeats} repeat(s) per stage...\n")

    results = [run_backend_suite(force_cpu=True, edges_df=edges_df, seed_wallet_id=seed_wallet_id, repeats=args.repeats)]

    if gpu_available() and not args.force_cpu_only:
        results.append(run_backend_suite(force_cpu=False, edges_df=edges_df, seed_wallet_id=seed_wallet_id, repeats=args.repeats))
    else:
        print("[note] GPU backend unavailable on this machine (no RAPIDS/cugraph importable) -- "
              "re-run this script unmodified on the RTX 4070 / GTX 1650 laptop to fill in the GPU column.\n")

    report = format_report(snapshot_name, vertex_count, edge_count, seed_wallet_id, args.repeats, results)
    print(report)

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
