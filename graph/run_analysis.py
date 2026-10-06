"""
Phases 3 and 6 in the project bible: load a GRAPH_SNAPSHOT's Parquet edge
list through graph/backend.py (GPU if RAPIDS is installed, CPU fallback
otherwise), run PageRank + Louvain + BFS, and persist results back to
Postgres tagged by a new ANALYSIS_RUN row -- so a Monday run and a Friday run
never overwrite each other (see the ER diagram rationale).

    python -m graph.run_analysis --snapshot-id 1 --seed-wallet-id 1

Runs all three algorithms in one invocation. --seed-wallet-id is the wallet_id
(not the address) for the case-study seed, used as the BFS start vertex --
look it up with:
    SELECT wallet_id FROM wallet WHERE address = lower('0x...');
"""
import argparse
import json
import time

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

from ingestion import config
from graph.backend import GraphBackend


def get_algorithm_id(conn, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT algorithm_id FROM algorithm WHERE algorithm_name = %s", (name,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Unknown algorithm '{name}' -- was db/schema.sql applied?")
        return row[0]


def start_run(conn, snapshot_id: int, algorithm_name: str, analyst_id: int = 1, parameters: dict = None,
              commit: bool = True) -> int:
    algorithm_id = get_algorithm_id(conn, algorithm_name)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analysis_run
                (snapshot_id, algorithm_id, analyst_id, parameters, status, started_at)
            VALUES (%s,%s,%s,%s,'RUNNING', NOW())
            RETURNING run_id
            """,
            (snapshot_id, algorithm_id, analyst_id, json.dumps(parameters or {})),
        )
        run_id = cur.fetchone()[0]
    if commit:
        conn.commit()
    return run_id


def finish_run(conn, run_id: int, runtime_ms: int, backend_name: str, library_version: str = None,
               status: str = "DONE", commit: bool = True) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE analysis_run
               SET status=%s, completed_at=NOW(), runtime_ms=%s, gpu_device=%s, backend_library_version=%s
               WHERE run_id=%s""",
            (status, runtime_ms, backend_name, library_version, run_id),
        )
    if commit:
        conn.commit()


def persist_vertex_metric(conn, run_id: int, df: pd.DataFrame, metric_name: str, value_col: str,
                           commit: bool = True) -> None:
    """Bulk via execute_values instead of one INSERT per row -- real crawls
    produce thousands of vertex_metric rows per run, and the row-by-row loop
    this replaced was the dominant cost in a full pipeline invocation (see
    scripts/benchmark.py's persistence-timing section, added alongside this).
    commit=False lets a caller (the benchmark) batch several of these inside
    one transaction it controls and rolls back itself, rather than each call
    committing on its own."""
    rows = [
        (run_id, int(getattr(row, "wallet_id")), metric_name, float(getattr(row, value_col)), rank)
        for rank, row in enumerate(df.itertuples(), start=1)
    ]
    if rows:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO vertex_metric (run_id, wallet_id, metric_name, metric_value, rank_position)
                VALUES %s
                ON CONFLICT (run_id, wallet_id, metric_name)
                DO UPDATE SET metric_value = EXCLUDED.metric_value, rank_position = EXCLUDED.rank_position
                """,
                rows,
            )
    if commit:
        conn.commit()


def persist_clusters(conn, run_id: int, df: pd.DataFrame, modularity: float, internal_edges: dict = None,
                      commit: bool = True) -> None:
    """Bulk via execute_values for both the cluster and cluster_member
    inserts. The cluster insert uses RETURNING cluster_id, community_label
    (fetch=True) and maps the new cluster_id back to each group by its
    community_label -- NOT by assuming the RETURNING rows come back in the
    same order the VALUES list was given in. Postgres makes no such
    ordering guarantee for a multi-row INSERT...RETURNING, unlike the old
    one-row-at-a-time loop where "the row I just inserted" was unambiguous."""
    internal_edges = internal_edges or {}
    groups = list(df.groupby("community_label"))
    if not groups:
        if commit:
            conn.commit()
        return

    cluster_rows = [
        (run_id, int(label), len(group), int(internal_edges.get(label, 0)), modularity)
        for label, group in groups
    ]
    with conn.cursor() as cur:
        returned = execute_values(
            cur,
            """
            INSERT INTO cluster (run_id, community_label, member_count, internal_edges, modularity_score)
            VALUES %s
            RETURNING cluster_id, community_label
            """,
            cluster_rows,
            fetch=True,
        )
        cluster_id_by_label = {label: cluster_id for cluster_id, label in returned}

        member_rows = [
            (cluster_id_by_label[int(label)], int(wallet_id))
            for label, group in groups
            for wallet_id in group.wallet_id
        ]
        if member_rows:
            execute_values(
                cur,
                "INSERT INTO cluster_member (cluster_id, wallet_id) VALUES %s ON CONFLICT DO NOTHING",
                member_rows,
            )
    if commit:
        conn.commit()


def compute_internal_edges(cluster_df: pd.DataFrame, edges_df: pd.DataFrame) -> dict:
    """How many of the snapshot's edges have both endpoints in the same
    community -- the structural signal classify_clusters() below uses. Plain
    Python over the edge list, not vectorized: fine at this project's bounded-
    subgraph scale (tens of thousands of edges), not meant for millions."""
    label_by_wallet = dict(zip(cluster_df.wallet_id, cluster_df.community_label))
    counts = {}
    for src, dst in zip(edges_df.src_wallet_id, edges_df.dst_wallet_id):
        src_label = label_by_wallet.get(int(src))
        if src_label is not None and src_label == label_by_wallet.get(int(dst)):
            counts[src_label] = counts.get(src_label, 0) + 1
    return counts


def classify_clusters(conn, run_id: int) -> int:
    """Phase 4 in the project bible: a first, deliberately simple heuristic
    for flagging plausible Sybil clusters -- not a general fraud classifier.

    A Sybil ring's signature is fan-out: one funding wallet connected to many
    otherwise-unconnected puppet addresses. Structurally that's a cluster
    whose internal edge count is close to its member count -- a star/tree
    topology has exactly member_count-1 edges. Anything much denser than
    that isn't this pattern and is deliberately left untriaged rather than
    force-fit into a category. Clusters under 5 members are also left
    untriaged: a fan-out shape isn't distinguishable from coincidence at
    that size.

    risk_score and alert.confidence are both explicit, documented formulas
    below, not a trained model -- exactly the "investigative lead, not
    proof" framing the project bible requires (Section 1). Re-running this
    against the same run_id is safe: it only ever UPDATEs cluster_type/
    risk_score and INSERTs new alerts, it doesn't delete or re-derive
    anything the run itself produced.

    Returns the number of clusters flagged.
    """
    flagged = 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cluster_id, member_count, internal_edges FROM cluster WHERE run_id = %s",
            (run_id,),
        )
        clusters = cur.fetchall()

    for cluster_id, member_count, internal_edges in clusters:
        if member_count < 5 or internal_edges is None:
            continue
        if internal_edges / member_count > 1.15:
            continue  # denser than a simple star -- not this heuristic's pattern

        risk_score = min(95.0, 30.0 + member_count * 1.5)
        confidence = min(0.9, 0.5 + member_count / 100)
        severity = "HIGH" if member_count >= 20 else "MEDIUM"

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cluster SET cluster_type = 'SYBIL', risk_score = %s WHERE cluster_id = %s",
                (risk_score, cluster_id),
            )
            cur.execute(
                """INSERT INTO alert (run_id, cluster_id, pattern_type, severity, confidence, review_status)
                   VALUES (%s,%s,'SYBIL_FANOUT',%s,%s,'NEW')""",
                (run_id, cluster_id, severity, confidence),
            )
        flagged += 1
    conn.commit()
    return flagged


def run_pagerank(conn, backend: GraphBackend, G, snapshot_id: int) -> pd.DataFrame:
    run_id = start_run(conn, snapshot_id, "pagerank")
    t0 = time.time()
    df = backend.pagerank(G)
    persist_vertex_metric(conn, run_id, df, "pagerank", "score")
    finish_run(conn, run_id, int((time.time() - t0) * 1000), backend.backend_name, backend.library_version)
    return df


def run_louvain(conn, backend: GraphBackend, G, snapshot_id: int, edges_df: pd.DataFrame) -> tuple:
    run_id = start_run(conn, snapshot_id, "louvain")
    t0 = time.time()
    df, modularity = backend.louvain(G)
    internal_edges = compute_internal_edges(df, edges_df)
    persist_clusters(conn, run_id, df, modularity, internal_edges)
    flagged = classify_clusters(conn, run_id)
    finish_run(conn, run_id, int((time.time() - t0) * 1000), backend.backend_name, backend.library_version)
    return df, flagged


def run_bfs(conn, backend: GraphBackend, G, snapshot_id: int, seed_wallet_id: int) -> pd.DataFrame:
    run_id = start_run(conn, snapshot_id, "bfs", parameters={"seed_wallet_id": seed_wallet_id})
    t0 = time.time()
    df = backend.bfs(G, seed_wallet_id)
    persist_vertex_metric(conn, run_id, df, f"bfs_distance_from_{seed_wallet_id}", "distance")
    finish_run(conn, run_id, int((time.time() - t0) * 1000), backend.backend_name, backend.library_version)
    return df


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-id", type=int, required=True)
    parser.add_argument("--seed-wallet-id", type=int, required=True)
    parser.add_argument("--force-cpu", action="store_true", help="skip RAPIDS even if installed")
    args = parser.parse_args()

    conn = psycopg2.connect(**config.PG_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT file_path FROM graph_snapshot WHERE snapshot_id = %s", (args.snapshot_id,))
            row = cur.fetchone()
            if row is None:
                raise SystemExit(f"No graph_snapshot with id {args.snapshot_id}")
            snapshot_path = row[0]

        edges_df = pd.read_parquet(snapshot_path)
        backend = GraphBackend(force_cpu=args.force_cpu)
        G = backend.load_edgelist(edges_df)

        pr = run_pagerank(conn, backend, G, args.snapshot_id)
        print(f"pagerank: top wallet_id={int(pr.iloc[0].wallet_id)} score={pr.iloc[0].score:.6f}")

        lv, flagged = run_louvain(conn, backend, G, args.snapshot_id, edges_df)
        print(f"louvain: {lv.community_label.nunique()} communities found, {flagged} flagged as plausible Sybil fan-out")

        bfs_df = run_bfs(conn, backend, G, args.snapshot_id, args.seed_wallet_id)
        print(f"bfs: reached {len(bfs_df)} wallets from seed {args.seed_wallet_id}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
