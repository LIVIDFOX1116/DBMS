"""
Wallet-level risk scoring -- fills wallet.risk_score, which is documented in
docs/API_CONTRACT.md and read by the frontend everywhere (wallet grid, graph
node color, wallet-detail modal) but was never actually written by any
existing pipeline stage. run_analysis.py only ever set cluster.risk_score
(the Sybil-fanout heuristic); no prior code populated the wallet-level column
at all -- every wallet.risk_score in this database was NULL until this script.

Deliberately an explicit, documented formula, not a trained model -- same
"investigative lead, not proof" framing as classify_clusters() in
run_analysis.py (see its own docstring), and reusing exactly the signals that
heuristic and the BFS stage already computed, not inventing new ones:

  - BFS distance from the case-study seed (vertex_metric, metric_name
    'bfs_distance_from_<seed_wallet_id>'): closer to the known-bad seed
    address is a stronger investigative signal than being merely well-
    connected in general -- see api/mock_data.json's own worked example,
    where the highest-PageRank wallet (a hub/router-like contract) is scored
    LOW (3.0) while the seed itself is scored highest (91.0). Hub-ness alone
    is not risk; proximity to a known-bad actor is.
  - Sybil-cluster membership (cluster.risk_score from classify_clusters(),
    scaled down for peripheral members vs. core nodes -- a fan-out ring's
    hub is more clearly implicated than an edge member that might just be
    one of its many counterparties).

Combined by taking the STRONGER of the two signals, not their sum -- a
wallet that is both BFS-close AND in a flagged cluster shouldn't score
higher than either signal alone would justify; the two are correlated
(cluster membership often follows shared proximity to the seed), and adding
them would double-count the same underlying evidence. Capped at 95.0,
matching classify_clusters()'s own cap -- this tool never claims certainty.

A wallet with NEITHER signal is left untouched (NULL / "Unknown" in the UI)
-- not defaulted to some low nonzero score. At real crawl scale (hundreds of
thousands of wallets, only a bounded BFS neighborhood and a few hundred
clusters ever have signal), assigning every unscored wallet an arbitrary
"probably fine" number would be a false claim of considered judgment where
none exists. Silence is the honest answer for a wallet this pipeline has
no real evidence about.

    python -m graph.score_wallets --snapshot-id 1
    python -m graph.score_wallets --snapshot-id 1 --bfs-run-id 3 --louvain-run-id 2
"""
import argparse

import psycopg2

from ingestion import config

BFS_DISTANCE_COMPONENT = 90.0  # score at distance 0 (the seed itself)
BFS_DISTANCE_DECAY = 20.0      # points lost per additional hop
CORE_NODE_WEIGHT = 1.0         # cluster.risk_score inherited in full
PERIPHERAL_MEMBER_WEIGHT = 0.7  # discount for non-core cluster members
MAX_RISK_SCORE = 95.0          # never claim certainty -- same cap classify_clusters() uses


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


def fetch_bfs_scores(conn, bfs_run_id: int) -> dict:
    """wallet_id -> bfs_component, for every wallet the BFS run actually reached
    (metric_name is 'bfs_distance_from_<seed>' -- LIKE match since the exact
    seed wallet_id is baked into the metric name, not a separate column)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT wallet_id, metric_value FROM vertex_metric
            WHERE run_id = %s AND metric_name LIKE 'bfs_distance_from_%%'
            """,
            (bfs_run_id,),
        )
        rows = cur.fetchall()
    return {
        wallet_id: max(0.0, BFS_DISTANCE_COMPONENT - distance * BFS_DISTANCE_DECAY)
        for wallet_id, distance in rows
    }


def fetch_cluster_scores(conn, louvain_run_id: int) -> dict:
    """wallet_id -> cluster_component, for every member of a SYBIL-flagged
    cluster from this louvain run. Core nodes inherit the cluster's full
    risk_score; peripheral members get PERIPHERAL_MEMBER_WEIGHT of it."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cm.wallet_id, c.risk_score, cm.is_core_node
            FROM cluster_member cm
            JOIN cluster c ON c.cluster_id = cm.cluster_id
            WHERE c.run_id = %s AND c.cluster_type = 'SYBIL' AND c.risk_score IS NOT NULL
            """,
            (louvain_run_id,),
        )
        rows = cur.fetchall()
    scores: dict = {}
    for wallet_id, cluster_risk, is_core in rows:
        weight = CORE_NODE_WEIGHT if is_core else PERIPHERAL_MEMBER_WEIGHT
        component = float(cluster_risk) * weight
        # A wallet can belong to at most one cluster per Louvain run in
        # practice (see commands.rs's own comment on this), but take the max
        # defensively rather than assume that invariant holds forever.
        scores[wallet_id] = max(scores.get(wallet_id, 0.0), component)
    return scores


def compute_risk_scores(bfs_scores: dict, cluster_scores: dict) -> dict:
    wallet_ids = set(bfs_scores) | set(cluster_scores)
    return {
        wallet_id: min(MAX_RISK_SCORE, max(bfs_scores.get(wallet_id, 0.0), cluster_scores.get(wallet_id, 0.0)))
        for wallet_id in wallet_ids
    }


def persist_risk_scores(conn, scores: dict) -> int:
    with conn.cursor() as cur:
        for wallet_id, score in scores.items():
            cur.execute(
                "UPDATE wallet SET risk_score = %s WHERE wallet_id = %s",
                (round(score, 2), wallet_id),
            )
    conn.commit()
    return len(scores)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-id", type=int, required=True)
    parser.add_argument("--bfs-run-id", type=int, default=None, help="defaults to the latest completed bfs run for this snapshot")
    parser.add_argument("--louvain-run-id", type=int, default=None, help="defaults to the latest completed louvain run for this snapshot")
    args = parser.parse_args()

    conn = psycopg2.connect(**config.PG_DSN)
    try:
        bfs_run_id = args.bfs_run_id or resolve_latest_run_id(conn, args.snapshot_id, "bfs")
        louvain_run_id = args.louvain_run_id or resolve_latest_run_id(conn, args.snapshot_id, "louvain")

        bfs_scores = fetch_bfs_scores(conn, bfs_run_id)
        cluster_scores = fetch_cluster_scores(conn, louvain_run_id)
        scores = compute_risk_scores(bfs_scores, cluster_scores)
        updated = persist_risk_scores(conn, scores)

        print(
            f"wallet risk scores: {updated} wallets scored "
            f"({len(bfs_scores)} via BFS distance from bfs_run_id={bfs_run_id}, "
            f"{len(cluster_scores)} via Sybil-cluster membership from louvain_run_id={louvain_run_id}) "
            f"-- every other wallet stays NULL/Unknown, no signal about it."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
