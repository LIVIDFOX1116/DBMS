"""
FrameCheck Phase 3: detectors that read already-computed analysis results
(BFS distance, PageRank, Louvain/classify_clusters output, and native ETH
transfer history) and write `flag` rows -- one row per (run_id, wallet_id,
detector) that FrameCheck's audit engine (graph/audit.py, Phase 6) can
later interrogate for frameability.

Each detector creates its OWN analysis_run row via graph/run_analysis.py's
existing start_run/finish_run, under a new algorithm_name (see
db/migrations/003_framecheck_detectors.sql), carrying its configurable
threshold(s) in analysis_run.parameters -- the same convention PageRank/
Louvain/BFS already use for their own run-scoped bookkeeping.

Detection logic is split into pure functions (select_*, compute_*) that
take plain Python data and return plain Python data -- fully unit-testable
offline, no DB -- from thin DB-I/O wrappers (run_*) that fetch rows, call
the pure function, and persist the result. Same separation
compute_internal_edges/persist_clusters already establishes in
graph/run_analysis.py.

    python -m graph.detectors --snapshot-id 1 --seed-wallet-id 1 --detector all
"""
import argparse
import math
import time

import psycopg2
from psycopg2.extras import execute_values

from ingestion import config
from graph.run_analysis import start_run, finish_run

# Mirrors graph/score_wallets.py's BFS-distance-to-score formula exactly --
# duplicated rather than imported, since that module computes OVERALL
# wallet risk (max of BFS proximity and Sybil-cluster signal together),
# while this detector needs the BFS component in isolation. Keeping them
# independent means this detector's semantics can't silently drift if
# score_wallets.py's combined formula changes for unrelated reasons.
BFS_DISTANCE_COMPONENT = 90.0
BFS_DISTANCE_DECAY = 20.0

DEFAULT_BFS_PROXIMITY_THRESHOLD = 50.0   # score >= this -> flag. 90 - 2*20 = 50, i.e. within 2 hops.
DEFAULT_PAGERANK_PERCENTILE = 0.01       # top 1% by PageRank
DEFAULT_IMPURITY_THRESHOLD = 0.10        # >= 10% of a wallet's (subgraph-visible) balance


# --- Pure detection logic (no DB) -------------------------------------------

def bfs_proximity_score(distance: float) -> float:
    """Same formula as graph/score_wallets.py's BFS component: 90 at the
    seed itself, losing 20 points per additional hop, floored at 0."""
    return max(0.0, BFS_DISTANCE_COMPONENT - distance * BFS_DISTANCE_DECAY)


def select_bfs_proximity_flags(bfs_distances: dict, seed_wallet_id: int, threshold: float) -> list:
    """bfs_distances: wallet_id -> hop distance from the seed (as stored in
    vertex_metric by run_bfs). Returns [(wallet_id, score), ...] for every
    wallet at or above `threshold`, excluding the seed itself (flagging the
    seed as "close to itself" is meaningless)."""
    flags = []
    for wallet_id, distance in bfs_distances.items():
        if wallet_id == seed_wallet_id:
            continue
        score = bfs_proximity_score(distance)
        if score >= threshold:
            flags.append((wallet_id, score))
    return flags


def select_sybil_cluster_flags(sybil_members: list, seed_wallet_id: int) -> list:
    """sybil_members: [(wallet_id, cluster_risk_score), ...] -- the exact
    rows classify_clusters() already produced for SYBIL-typed clusters.
    This detector does not re-detect anything; it only reads that output
    and excludes the seed. Returns [(wallet_id, score), ...]."""
    return [(wallet_id, score) for wallet_id, score in sybil_members if wallet_id != seed_wallet_id]


def select_pagerank_threshold_flags(pagerank_scores: list, seed_wallet_id: int, percentile: float) -> tuple:
    """pagerank_scores: [(wallet_id, score), ...], any order. Returns
    ([(wallet_id, score), ...], threshold_value) for the top `percentile`
    fraction of wallets by score, excluding the seed. threshold_value is
    the score of the lowest-ranked wallet still included (None if the
    input is empty after excluding the seed)."""
    candidates = sorted(
        (pair for pair in pagerank_scores if pair[0] != seed_wallet_id),
        key=lambda pair: pair[1],
        reverse=True,
    )
    if not candidates:
        return [], None
    cutoff_count = max(1, math.ceil(len(candidates) * percentile))
    top = candidates[:cutoff_count]
    return top, top[-1][1]


def compute_impurity_ratios(transfers: list, seed_wallet_id: int) -> dict:
    """Simplified Liu et al. "impurity" via proportional haircut taint
    propagation over native ETH transfers, in the block order given.

    transfers: [(from_wallet_id, to_wallet_id, value_wei), ...], caller-
    sorted by (block_number, tx_index) -- order matters, this function
    trusts it and does not re-sort.

    Model: the seed wallet is treated as a pure taint source -- every wei
    it sends is 100% tainted, regardless of its own tracked balance (we
    have no visibility into the seed's true prior balance split, and it is
    the known-bad address by construction of this whole case study, so
    diluting its outflow with an assumed-clean portion would be the wrong
    default). Every other wallet's outgoing taint fraction is
    tainted_balance / balance at the moment it sends, carried forward
    proportionally (the standard "haircut"/proportional taint model used
    in blockchain forensics -- not a FIFO/LIFO UTXO tracing model, which
    this simplification deliberately does not attempt).

    KNOWN LIMITATION (partial balances): `balance` here is NOT a wallet's
    real on-chain balance -- it is only the net of transfers that happen to
    appear in this bounded 2-hop subgraph crawl. A wallet that received
    most of its real funds from outside the crawl will have an artificially
    low (or even negative, floored at 0) tracked balance here, which can
    inflate or deflate its computed impurity ratio relative to its true
    on-chain impurity. This detector's output is only meaningful relative
    to the visible subgraph, never as an absolute on-chain claim.

    Returns wallet_id -> impurity_ratio (tainted_balance / balance) for
    every wallet with balance > 0, EXCLUDING the seed itself.
    """
    balance = {}
    tainted = {}
    for from_id, to_id, value_wei in transfers:
        value = float(value_wei)
        if from_id == seed_wallet_id:
            taint_out = value
        else:
            bal_before = balance.get(from_id, 0.0)
            taint_out = value * (tainted.get(from_id, 0.0) / bal_before) if bal_before > 0 else 0.0

        balance[from_id] = max(0.0, balance.get(from_id, 0.0) - value)
        tainted[from_id] = max(0.0, tainted.get(from_id, 0.0) - taint_out)
        balance[to_id] = balance.get(to_id, 0.0) + value
        tainted[to_id] = tainted.get(to_id, 0.0) + taint_out

    return {
        wallet_id: tainted.get(wallet_id, 0.0) / bal
        for wallet_id, bal in balance.items()
        if wallet_id != seed_wallet_id and bal > 0
    }


def select_impurity_flags(impurity_ratios: dict, threshold: float) -> list:
    """Returns [(wallet_id, ratio), ...] for every wallet at or above
    `threshold`."""
    return [(wallet_id, ratio) for wallet_id, ratio in impurity_ratios.items() if ratio >= threshold]


# --- DB-I/O wrappers ---------------------------------------------------------

def resolve_latest_run_id(conn, snapshot_id: int, algorithm_name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ar.run_id FROM analysis_run ar
            JOIN algorithm a ON a.algorithm_id = ar.algorithm_id
            WHERE ar.snapshot_id = %s AND a.algorithm_name = %s AND ar.status = 'DONE'
            ORDER BY ar.completed_at DESC LIMIT 1
            """,
            (snapshot_id, algorithm_name),
        )
        row = cur.fetchone()
        if row is None:
            raise SystemExit(
                f"No completed '{algorithm_name}' run found for snapshot {snapshot_id} -- "
                f"run graph.run_analysis first."
            )
        return row[0]


def persist_flags(conn, run_id: int, rows: list, commit: bool = True) -> int:
    """rows: [(wallet_id, detector, score, threshold), ...]. Bulk via
    execute_values, same pattern Phase 2 established for vertex_metric/
    cluster -- see graph/run_analysis.py."""
    if not rows:
        if commit:
            conn.commit()
        return 0
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO flag (run_id, wallet_id, detector, score, threshold) VALUES %s",
            [(run_id, wallet_id, detector, score, threshold) for wallet_id, detector, score, threshold in rows],
        )
    if commit:
        conn.commit()
    return len(rows)


def run_bfs_proximity(conn, snapshot_id: int, seed_wallet_id: int,
                       threshold: float = DEFAULT_BFS_PROXIMITY_THRESHOLD, bfs_run_id: int = None) -> int:
    bfs_run_id = bfs_run_id or resolve_latest_run_id(conn, snapshot_id, "bfs")
    run_id = start_run(
        conn, snapshot_id, "bfs_proximity",
        parameters={"threshold": threshold, "bfs_run_id": bfs_run_id, "seed_wallet_id": seed_wallet_id},
    )
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT wallet_id, metric_value FROM vertex_metric WHERE run_id = %s AND metric_name LIKE 'bfs_distance_from_%%'",
            (bfs_run_id,),
        )
        bfs_distances = dict(cur.fetchall())
    flags = select_bfs_proximity_flags(bfs_distances, seed_wallet_id, threshold)
    rows = [(wallet_id, "bfs_proximity", score, threshold) for wallet_id, score in flags]
    count = persist_flags(conn, run_id, rows)
    finish_run(conn, run_id, int((time.time() - t0) * 1000), "bfs_proximity")
    return count


def run_sybil_cluster(conn, snapshot_id: int, seed_wallet_id: int, louvain_run_id: int = None) -> int:
    louvain_run_id = louvain_run_id or resolve_latest_run_id(conn, snapshot_id, "louvain")
    run_id = start_run(
        conn, snapshot_id, "sybil_cluster",
        parameters={"louvain_run_id": louvain_run_id, "seed_wallet_id": seed_wallet_id},
    )
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cm.wallet_id, c.risk_score FROM cluster_member cm
            JOIN cluster c ON c.cluster_id = cm.cluster_id
            WHERE c.run_id = %s AND c.cluster_type = 'SYBIL'
            """,
            (louvain_run_id,),
        )
        sybil_members = [(wallet_id, float(score) if score is not None else 0.0) for wallet_id, score in cur.fetchall()]
    flags = select_sybil_cluster_flags(sybil_members, seed_wallet_id)
    rows = [(wallet_id, "sybil_cluster", score, None) for wallet_id, score in flags]
    count = persist_flags(conn, run_id, rows)
    finish_run(conn, run_id, int((time.time() - t0) * 1000), "sybil_cluster")
    return count


def run_pagerank_threshold(conn, snapshot_id: int, seed_wallet_id: int,
                            percentile: float = DEFAULT_PAGERANK_PERCENTILE, pagerank_run_id: int = None) -> int:
    pagerank_run_id = pagerank_run_id or resolve_latest_run_id(conn, snapshot_id, "pagerank")
    run_id = start_run(
        conn, snapshot_id, "pagerank_threshold",
        parameters={"percentile": percentile, "pagerank_run_id": pagerank_run_id, "seed_wallet_id": seed_wallet_id},
    )
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT wallet_id, metric_value FROM vertex_metric WHERE run_id = %s AND metric_name = 'pagerank'",
            (pagerank_run_id,),
        )
        pagerank_scores = cur.fetchall()
    flags, threshold_value = select_pagerank_threshold_flags(pagerank_scores, seed_wallet_id, percentile)
    rows = [(wallet_id, "pagerank_threshold", score, threshold_value) for wallet_id, score in flags]
    count = persist_flags(conn, run_id, rows)
    finish_run(conn, run_id, int((time.time() - t0) * 1000), "pagerank_threshold")
    return count


def run_impurity(conn, snapshot_id: int, seed_wallet_id: int, threshold: float = DEFAULT_IMPURITY_THRESHOLD) -> int:
    run_id = start_run(
        conn, snapshot_id, "impurity",
        parameters={
            "threshold": threshold, "seed_wallet_id": seed_wallet_id,
            "limitation": "balances are computed only from transfers visible in this bounded crawl, "
                           "not each wallet's true on-chain balance -- see compute_impurity_ratios docstring",
        },
    )
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.from_wallet_id, t.to_wallet_id, t.value_wei
            FROM snapshot_edge_tx st
            JOIN snapshot_edge se ON se.edge_id = st.snapshot_edge_id
            JOIN transaction t ON t.tx_hash = st.tx_hash
            WHERE se.snapshot_id = %s
            ORDER BY t.block_number, t.tx_index
            """,
            (snapshot_id,),
        )
        transfers = cur.fetchall()
    ratios = compute_impurity_ratios(transfers, seed_wallet_id)
    flags = select_impurity_flags(ratios, threshold)
    rows = [(wallet_id, "impurity", ratio, threshold) for wallet_id, ratio in flags]
    count = persist_flags(conn, run_id, rows)
    finish_run(conn, run_id, int((time.time() - t0) * 1000), "impurity")
    return count


DETECTOR_NAMES = ("bfs_proximity", "sybil_cluster", "pagerank_threshold", "impurity")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-id", type=int, required=True)
    parser.add_argument("--seed-wallet-id", type=int, required=True)
    parser.add_argument("--detector", choices=DETECTOR_NAMES + ("all",), default="all")
    parser.add_argument("--bfs-proximity-threshold", type=float, default=DEFAULT_BFS_PROXIMITY_THRESHOLD)
    parser.add_argument("--pagerank-percentile", type=float, default=DEFAULT_PAGERANK_PERCENTILE)
    parser.add_argument("--impurity-threshold", type=float, default=DEFAULT_IMPURITY_THRESHOLD)
    args = parser.parse_args()

    names = DETECTOR_NAMES if args.detector == "all" else (args.detector,)

    conn = psycopg2.connect(**config.PG_DSN)
    try:
        for name in names:
            if name == "bfs_proximity":
                n = run_bfs_proximity(conn, args.snapshot_id, args.seed_wallet_id, args.bfs_proximity_threshold)
            elif name == "sybil_cluster":
                n = run_sybil_cluster(conn, args.snapshot_id, args.seed_wallet_id)
            elif name == "pagerank_threshold":
                n = run_pagerank_threshold(conn, args.snapshot_id, args.seed_wallet_id, args.pagerank_percentile)
            else:
                n = run_impurity(conn, args.snapshot_id, args.seed_wallet_id, args.impurity_threshold)
            print(f"{name}: {n} wallets flagged")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
