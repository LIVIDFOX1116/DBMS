"""
Where does the GPU actually start winning?

The headline speedups hide a real answer: on this hardware BFS is *slower*
on the GPU for small graphs and faster for large ones, so "GPU is Nx faster"
is only true above some size. This walks a range of graph sizes and reports,
per algorithm, the size at which the GPU crosses from slower to faster.

Sizes are taken by sampling edges from a real snapshot rather than generating
synthetic graphs, so the degree distribution stays realistic.

    python scripts/benchmark_crossover.py --snapshot-id 3 --repeats 3

Needs RAPIDS importable and FRAMECHECK_USE_GPU_GRAPH=1, like every other GPU
path in this repo -- it exits early and says so otherwise.
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
import psycopg2  # noqa: E402

from graph.backend import GraphBackend, gpu_available  # noqa: E402
from ingestion import config  # noqa: E402

DEFAULT_SIZES = (2_000, 5_000, 10_000, 20_000, 40_000, 80_000, 160_000, 320_000, 640_000)


def time_it(fn, repeats):
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def measure(edges, seed_wallet_id, repeats):
    """Median seconds per algorithm for both backends on one edge sample."""
    out = {}
    for label, force_cpu in (("cpu", True), ("gpu", False)):
        backend = GraphBackend(force_cpu=force_cpu)
        graph = backend.load_edgelist(edges)
        out[f"load_{label}"] = time_it(lambda b=backend, e=edges: b.load_edgelist(e), repeats)
        out[f"pagerank_{label}"] = time_it(lambda b=backend, g=graph: b.pagerank(g), repeats)
        out[f"louvain_{label}"] = time_it(lambda b=backend, g=graph: b.louvain(g), repeats)
        # The seed may not survive edge sampling; fall back to a vertex that
        # is definitely present so BFS measures traversal, not an error path.
        seed = seed_wallet_id
        present = set(edges.src_wallet_id.unique())
        if seed not in present:
            seed = int(edges.src_wallet_id.iloc[0])
        out[f"bfs_{label}"] = time_it(lambda b=backend, g=graph, s=seed: b.bfs(g, s), repeats)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-id", type=int, default=None)
    parser.add_argument("--seed-wallet-id", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--sizes", type=int, nargs="*", default=list(DEFAULT_SIZES))
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    if not gpu_available():
        sys.exit("RAPIDS is not importable or FRAMECHECK_USE_GPU_GRAPH is unset -- "
                 "this script compares two backends and needs both.")

    conn = psycopg2.connect(**config.PG_DSN)
    conn.set_session(readonly=True)
    try:
        with conn.cursor() as cur:
            if args.snapshot_id is None:
                cur.execute("SELECT snapshot_id FROM graph_snapshot ORDER BY edge_count DESC LIMIT 1")
                args.snapshot_id = cur.fetchone()[0]
            cur.execute("SELECT snapshot_name, file_path FROM graph_snapshot WHERE snapshot_id = %s",
                        (args.snapshot_id,))
            name, path = cur.fetchone()
    finally:
        conn.close()

    full = pd.read_parquet(config.require_data_path(path))
    print(f"source: snapshot {args.snapshot_id} ({name}), {len(full):,} edges, "
          f"median of {args.repeats} runs\n")

    algorithms = ("load", "pagerank", "louvain", "bfs")
    header = f"{'edges':>9} | " + " | ".join(f"{a:>22}" for a in algorithms)
    print(header)
    print("-" * len(header))

    rows = []
    for size in args.sizes:
        if size > len(full):
            continue
        # head(), not a random sample: edges are written in traversal order,
        # so a prefix is a connected neighbourhood rather than scattered dust.
        edges = full.head(size).reset_index(drop=True)
        timings = measure(edges, args.seed_wallet_id, args.repeats)
        cells = []
        row = {"edges": size}
        for algorithm in algorithms:
            cpu, gpu = timings[f"{algorithm}_cpu"], timings[f"{algorithm}_gpu"]
            speedup = cpu / gpu if gpu > 0 else float("inf")
            row[algorithm] = {"cpu": cpu, "gpu": gpu, "speedup": speedup}
            cells.append(f"{cpu*1000:7.1f}/{gpu*1000:7.1f}ms {speedup:5.2f}x")
        rows.append(row)
        print(f"{size:>9,} | " + " | ".join(cells))

    print("\nCrossover (first size where the GPU is faster than the CPU):")
    for algorithm in algorithms:
        crossed = next((r for r in rows if r[algorithm]["speedup"] >= 1.0), None)
        if crossed is None:
            print(f"  {algorithm:<10} GPU never overtook the CPU within the sizes measured")
        elif crossed is rows[0]:
            print(f"  {algorithm:<10} GPU already ahead at the smallest size measured "
                  f"({rows[0]['edges']:,} edges)")
        else:
            previous = rows[rows.index(crossed) - 1]
            print(f"  {algorithm:<10} between {previous['edges']:,} and {crossed['edges']:,} edges "
                  f"({previous[algorithm]['speedup']:.2f}x -> {crossed[algorithm]['speedup']:.2f}x)")

    if args.output:
        import json
        Path(args.output).write_text(json.dumps(
            {"snapshot_id": args.snapshot_id, "snapshot_name": name,
             "repeats": args.repeats, "rows": rows}, indent=1), encoding="utf-8")
        print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
