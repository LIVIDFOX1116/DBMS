"""
Do the CPU and GPU backends flag the SAME wallets, and does the edge
weighting decide it?

`docs/benchmark_results.md` records an unresolved disagreement: on snapshot 3
the two PageRank implementations produce materially different scores, and
`edge_weight` is wei with most edges carrying 0. Everything there was a
timing comparison; none of it showed whether the disagreement actually
changes a FrameCheck conclusion. This script measures that directly.

    python scripts/pagerank_agreement.py --snapshot-id 3
    python scripts/pagerank_agreement.py --snapshot-id 3 --json docs/perf/pagerank_agreement.json

For each weighting scheme it runs PageRank on both backends and compares the
output the way the detector does -- `select_pagerank_threshold_flags` from
graph/detectors.py, the production selection path, not a reimplementation --
so "disagreement" means "a different set of wallets gets flagged", which is
the only form of it that matters.

The schemes:

- `wei`        -- what the pipeline does today: edge_weight as-is, raw wei.
- `unweighted` -- every edge weight 1. PageRank on the topology alone.
- `log1p`      -- log1p(wei), which compresses ~22 orders of magnitude into
                  ~0-50 while keeping the ordering of transfer sizes.

Read-only: SELECTs the snapshot row, reads its Parquet file, writes nothing
back. Needs a RAPIDS machine to fill in the GPU column; without one it still
runs the CPU side and says the GPU column needs a GPU, rather than failing.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import psycopg2

from graph.backend import GraphBackend, gpu_available
from graph.detectors import DEFAULT_PAGERANK_PERCENTILE, select_pagerank_threshold_flags
from ingestion import config

SCHEMES = ("wei", "unweighted", "log1p")


def apply_weighting(edges_df, scheme, weight="edge_weight"):
    """Returns a copy of `edges_df` with `weight` rewritten per `scheme`.
    Never mutates the input -- the caller reuses one edgelist across every
    scheme, and a mutation here would silently make later schemes compound."""
    out = edges_df.copy()
    if scheme == "wei":
        return out
    if scheme == "unweighted":
        out[weight] = 1.0
        return out
    if scheme == "log1p":
        out[weight] = np.log1p(out[weight].astype("float64").clip(lower=0.0))
        return out
    raise ValueError(f"unknown weighting scheme {scheme!r} (expected one of {SCHEMES})")


def _flag_ids(pr_df, seed_wallet_id, percentile):
    pairs = list(zip(pr_df["wallet_id"].tolist(), pr_df["score"].tolist()))
    flags, threshold_value = select_pagerank_threshold_flags(pairs, seed_wallet_id, percentile)
    return {int(wallet_id) for wallet_id, _ in flags}, threshold_value


def _tie_band_count(excluded_ids, scores, threshold, rel_tol=1e-9):
    """Of the wallets in `excluded_ids`, how many scored close enough to
    `threshold` in this backend to be on the wrong side of the cut purely by
    tie-breaking order? A percentile cut takes a fixed COUNT, so every wallet
    tied with the threshold score is a coin flip -- which one lands inside
    depends on the sort's arbitrary ordering of equal keys, not on anything
    either backend computed differently."""
    if threshold is None or threshold == 0:
        return 0
    return sum(1 for w in excluded_ids
               if w in scores and abs(scores[w] - threshold) <= rel_tol * abs(threshold))


def compare_rankings(cpu_df, gpu_df, seed_wallet_id, percentile=DEFAULT_PAGERANK_PERCENTILE, top_n=100):
    """How much do two PageRank outputs disagree, in the terms the detector
    cares about? `cpu_df`/`gpu_df` are the DataFrame[wallet_id, score] that
    GraphBackend.pagerank returns.

    The headline number is `flag_jaccard`: 1.0 means the two backends flag
    exactly the same wallets, so which backend ran is invisible downstream.
    Score-level numbers are reported too, but a uniform rescale of every
    score changes nothing here -- the detector takes a percentile, so only
    the RANKING can move a flag."""
    cpu_scores = dict(zip(cpu_df["wallet_id"].astype("int64"), cpu_df["score"].astype("float64")))
    gpu_scores = dict(zip(gpu_df["wallet_id"].astype("int64"), gpu_df["score"].astype("float64")))
    shared = sorted(set(cpu_scores) & set(gpu_scores))

    cpu_flags, cpu_threshold = _flag_ids(cpu_df, seed_wallet_id, percentile)
    gpu_flags, gpu_threshold = _flag_ids(gpu_df, seed_wallet_id, percentile)
    union = cpu_flags | gpu_flags

    cpu_top = [int(w) for w in cpu_df["wallet_id"].head(top_n)]
    gpu_top = [int(w) for w in gpu_df["wallet_id"].head(top_n)]

    spearman = None
    if len(shared) > 1:
        cpu_rank = pd.Series([cpu_scores[w] for w in shared]).rank()
        gpu_rank = pd.Series([gpu_scores[w] for w in shared]).rank()
        spearman = float(cpu_rank.corr(gpu_rank))

    return {
        "vertices_cpu": len(cpu_scores),
        "vertices_gpu": len(gpu_scores),
        "vertices_shared": len(shared),
        "flags_cpu": len(cpu_flags),
        "flags_gpu": len(gpu_flags),
        "flags_agreed": len(cpu_flags & gpu_flags),
        "flags_cpu_only": len(cpu_flags - gpu_flags),
        "flags_gpu_only": len(gpu_flags - cpu_flags),
        # 1.0 == the backend choice is invisible to pagerank_threshold.
        "flag_jaccard": (len(cpu_flags & gpu_flags) / len(union)) if union else None,
        "threshold_cpu": cpu_threshold,
        "threshold_gpu": gpu_threshold,
        # How much of the disagreement is just ties at the cut, rather than
        # the two backends ranking wallets differently?
        "cpu_only_tied_at_gpu_cut": _tie_band_count(cpu_flags - gpu_flags, gpu_scores, gpu_threshold),
        "gpu_only_tied_at_cpu_cut": _tie_band_count(gpu_flags - cpu_flags, cpu_scores, cpu_threshold),
        "tied_at_cut_cpu": sum(1 for v in cpu_scores.values()
                               if cpu_threshold and abs(v - cpu_threshold) <= 1e-9 * abs(cpu_threshold)),
        "tied_at_cut_gpu": sum(1 for v in gpu_scores.values()
                               if gpu_threshold and abs(v - gpu_threshold) <= 1e-9 * abs(gpu_threshold)),
        "top_n": top_n,
        "top_n_overlap": len(set(cpu_top) & set(gpu_top)),
        "top1_same": bool(cpu_top and gpu_top and cpu_top[0] == gpu_top[0]),
        "top5_overlap": len(set(cpu_top[:5]) & set(gpu_top[:5])),
        "spearman": spearman,
        "max_score_cpu": float(cpu_df["score"].max()),
        "max_score_gpu": float(gpu_df["score"].max()),
    }


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
    return snapshot_name, pd.read_parquet(config.require_data_path(file_path)), vertex_count, edge_count


def run_scheme(edges_df, scheme, seed_wallet_id, percentile, with_gpu):
    weighted = apply_weighting(edges_df, scheme)
    cpu = GraphBackend(force_cpu=True)
    cpu_df = cpu.pagerank(cpu.load_edgelist(weighted))
    if not with_gpu:
        return {"scheme": scheme, "gpu": False, "max_score_cpu": float(cpu_df["score"].max())}
    gpu = GraphBackend()
    gpu_df = gpu.pagerank(gpu.load_edgelist(weighted))
    result = compare_rankings(cpu_df, gpu_df, seed_wallet_id, percentile)
    result.update({"scheme": scheme, "gpu": True})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-id", type=int, required=True)
    parser.add_argument("--seed-wallet-id", type=int, default=None)
    parser.add_argument("--percentile", type=float, default=DEFAULT_PAGERANK_PERCENTILE)
    parser.add_argument("--schemes", default=",".join(SCHEMES))
    parser.add_argument("--json", default=None, help="also write the raw measurements here")
    args = parser.parse_args()

    with_gpu = gpu_available()
    conn = psycopg2.connect(**config.PG_DSN)
    try:
        name, edges_df, vertex_count, edge_count = load_snapshot(conn, args.snapshot_id)
        seed_wallet_id = args.seed_wallet_id
        if seed_wallet_id is None:
            with conn.cursor() as cur:
                cur.execute("SELECT wallet_id FROM wallet WHERE address = lower(%s)", (config.CASE_STUDY_ADDRESS,))
                row = cur.fetchone()
            if row is None:
                raise SystemExit("Pass --seed-wallet-id: the case-study address is not in this DB.")
            seed_wallet_id = row[0]
    finally:
        conn.close()

    zero_weight = float((edges_df["edge_weight"] <= 0).mean())
    print(f"snapshot {args.snapshot_id} ({name}): {vertex_count:,} vertices, {edge_count:,} edges, "
          f"{zero_weight:.1%} of edges carry weight 0")
    print(f"seed wallet {seed_wallet_id}, percentile {args.percentile} "
          f"(GPU: {'yes' if with_gpu else 'NO -- CPU column only'})\n")

    results = [run_scheme(edges_df, s, seed_wallet_id, args.percentile, with_gpu)
               for s in args.schemes.split(",") if s]

    if with_gpu:
        print(f"{'scheme':<12} {'flags':>7} {'agreed':>7} {'cpu-only':>9} {'gpu-only':>9} "
              f"{'jaccard':>8} {'top100':>7} {'spearman':>9}")
        for r in results:
            print(f"{r['scheme']:<12} {r['flags_cpu']:>7} {r['flags_agreed']:>7} {r['flags_cpu_only']:>9} "
                  f"{r['flags_gpu_only']:>9} {r['flag_jaccard']:>8.4f} {r['top_n_overlap']:>7} "
                  f"{r['spearman']:>9.4f}")
        print("\njaccard 1.0 means the backend choice is invisible to pagerank_threshold.")
        print("")
        print(f"{'scheme':<12} {'disagreed':>10} {'tied at cut':>12} {'genuine':>8}   wallets sharing the cut score")
        for r in results:
            disagreed = r["flags_cpu_only"] + r["flags_gpu_only"]
            tied = r["cpu_only_tied_at_gpu_cut"] + r["gpu_only_tied_at_cpu_cut"]
            print(f"{r['scheme']:<12} {disagreed:>10} {tied:>12} {disagreed - tied:>8}   "
                  f"cpu {r['tied_at_cut_cpu']}, gpu {r['tied_at_cut_gpu']}")
    else:
        for r in results:
            print(f"{r['scheme']:<12} max score {r['max_score_cpu']:.6g} (CPU only)")

    if args.json:
        payload = {
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_id": args.snapshot_id,
            "snapshot_name": name,
            "vertex_count": vertex_count,
            "edge_count": edge_count,
            "zero_weight_fraction": zero_weight,
            "seed_wallet_id": seed_wallet_id,
            "percentile": args.percentile,
            "gpu_available": with_gpu,
            "schemes": results,
        }
        Path(args.json).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
