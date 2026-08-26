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

from ingestion import config
from graph.backend import GraphBackend


def get_algorithm_id(conn, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT algorithm_id FROM algorithm WHERE algorithm_name = %s", (name,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Unknown algorithm '{name}' -- was db/schema.sql applied?")
        return row[0]


def start_run(conn, snapshot_id: int, algorithm_name: str, analyst_id: int = 1, parameters: dict = None) -> int:
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
    conn.commit()
    return run_id


def finish_run(conn, run_id: int, runtime_ms: int, backend_name: str, status: str = "DONE") -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE analysis_run
               SET status=%s, completed_at=NOW(), runtime_ms=%s, gpu_device=%s
               WHERE run_id=%s""",
            (status, runtime_ms, backend_name, run_id),
        )
    conn.commit()


def persist_vertex_metric(conn, run_id: int, df: pd.DataFrame, metric_name: str, value_col: str) -> None:
    with conn.cursor() as cur:
        for rank, row in enumerate(df.itertuples(), start=1):
            cur.execute(
                """
                INSERT INTO vertex_metric (run_id, wallet_id, metric_name, metric_value, rank_position)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (run_id, wallet_id, metric_name)
                DO UPDATE SET metric_value = EXCLUDED.metric_value, rank_position = EXCLUDED.rank_position
                """,
                (run_id, int(getattr(row, "wallet_id")), metric_name, float(getattr(row, value_col)), rank),
            )
    conn.commit()


def persist_clusters(conn, run_id: int, df: pd.DataFrame, modularity: float) -> None:
    with conn.cursor() as cur:
        for label, group in df.groupby("community_label"):
            cur.execute(
                """
                INSERT INTO cluster (run_id, community_label, member_count, modularity_score)
                VALUES (%s,%s,%s,%s)
                RETURNING cluster_id
                """,
                (run_id, int(label), len(group), modularity),
            )
            cluster_id = cur.fetchone()[0]
            for wallet_id in group.wallet_id:
                cur.execute(
                    """
                    INSERT INTO cluster_member (cluster_id, wallet_id)
                    VALUES (%s,%s) ON CONFLICT DO NOTHING
                    """,
                    (cluster_id, int(wallet_id)),
                )
    conn.commit()


def run_pagerank(conn, backend: GraphBackend, G, snapshot_id: int) -> pd.DataFrame:
    run_id = start_run(conn, snapshot_id, "pagerank")
    t0 = time.time()
    df = backend.pagerank(G)
    persist_vertex_metric(conn, run_id, df, "pagerank", "score")
    finish_run(conn, run_id, int((time.time() - t0) * 1000), backend.backend_name)
    return df


def run_louvain(conn, backend: GraphBackend, G, snapshot_id: int) -> pd.DataFrame:
    run_id = start_run(conn, snapshot_id, "louvain")
    t0 = time.time()
    df, modularity = backend.louvain(G)
    persist_clusters(conn, run_id, df, modularity)
    finish_run(conn, run_id, int((time.time() - t0) * 1000), backend.backend_name)
    return df


def run_bfs(conn, backend: GraphBackend, G, snapshot_id: int, seed_wallet_id: int) -> pd.DataFrame:
    run_id = start_run(conn, snapshot_id, "bfs", parameters={"seed_wallet_id": seed_wallet_id})
    t0 = time.time()
    df = backend.bfs(G, seed_wallet_id)
    persist_vertex_metric(conn, run_id, df, f"bfs_distance_from_{seed_wallet_id}", "distance")
    finish_run(conn, run_id, int((time.time() - t0) * 1000), backend.backend_name)
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

        lv = run_louvain(conn, backend, G, args.snapshot_id)
        print(f"louvain: {lv.community_label.nunique()} communities found")

        bfs_df = run_bfs(conn, backend, G, args.snapshot_id, args.seed_wallet_id)
        print(f"bfs: reached {len(bfs_df)} wallets from seed {args.seed_wallet_id}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
