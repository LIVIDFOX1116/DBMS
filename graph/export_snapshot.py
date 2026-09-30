"""
Phase 2 in the project bible: aggregate the ingested transaction table into a
directed, weighted edge list bounded by [start_block, end_block], write it to
Parquet (what cuDF loads on the GPU laptops), and record the GRAPH_SNAPSHOT /
SNAPSHOT_EDGE / SNAPSHOT_EDGE_TX rows in Postgres so every export is traceable
back to the exact transactions that produced it.

    python -m graph.export_snapshot --start-block 0 --end-block 999999999 \
        --name polynetwork_2hop_v1

edge_weight is defined here as SUM(value_wei) between each ordered wallet
pair -- documented explicitly because Louvain (graph/backend.py) needs this
symmetrized into an undirected weight later; keeping the raw directed sum
here (rather than pre-symmetrizing) keeps this stage a faithful, backend-
agnostic export of what's actually on-chain.
"""
import argparse
import hashlib
import pathlib

import pandas as pd
import psycopg2

from ingestion import config

EDGE_QUERY = """
    SELECT from_wallet_id AS src_wallet_id,
           to_wallet_id   AS dst_wallet_id,
           SUM(value_wei::numeric)::float8 AS edge_weight,
           COUNT(*)::int AS transfer_count,
           array_agg(tx_hash) AS tx_hashes
    FROM transaction
    WHERE block_number BETWEEN %(start_block)s AND %(end_block)s
      AND to_wallet_id IS NOT NULL
    GROUP BY from_wallet_id, to_wallet_id
"""


def build_edge_dataframe(conn, start_block: int, end_block: int) -> pd.DataFrame:
    return pd.read_sql(EDGE_QUERY, conn, params={"start_block": start_block, "end_block": end_block})


def write_parquet(edge_df: pd.DataFrame, out_path: pathlib.Path) -> str:
    """Writes the exportable columns only (tx_hashes is Postgres-side
    traceability, not part of the CSR edge list cuDF loads) and returns the
    file's sha256 for graph_snapshot.checksum_sha256."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    edge_df.drop(columns=["tx_hashes"]).to_parquet(out_path, index=False)
    return hashlib.sha256(out_path.read_bytes()).hexdigest()


def portable_path(out_path: pathlib.Path) -> str:
    """graph_snapshot.file_path must be readable from wherever
    graph.run_analysis actually runs -- per environment-gpu.yml, that's
    WSL2/Linux (RAPIDS needs a real NVIDIA driver + CUDA, not available
    natively on Windows), while this export script itself commonly runs on
    Windows (plain pandas/psycopg2, no GPU needed). str(WindowsPath) produces
    backslash-separated paths that are just literal characters to Linux, not
    a separator -- open() on WSL then fails with FileNotFoundError even
    though the file exists right there. Forward slashes work as a path
    separator on both platforms, so always store posix-style."""
    return out_path.as_posix()


def persist_snapshot(conn, snapshot_name, out_path, start_block, end_block, edge_df, checksum) -> int:
    vertex_count = len(set(edge_df.src_wallet_id) | set(edge_df.dst_wallet_id))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO graph_snapshot
                (snapshot_name, file_path, file_format, start_block, end_block,
                 vertex_count, edge_count, checksum_sha256)
            VALUES (%s,%s,'parquet',%s,%s,%s,%s,%s)
            RETURNING snapshot_id
            """,
            (snapshot_name, portable_path(out_path), start_block, end_block, vertex_count, len(edge_df), checksum),
        )
        snapshot_id = cur.fetchone()[0]

        for row in edge_df.itertuples():
            cur.execute(
                """
                INSERT INTO snapshot_edge
                    (snapshot_id, src_wallet_id, dst_wallet_id, edge_weight, transfer_count)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (snapshot_id, src_wallet_id, dst_wallet_id) DO NOTHING
                RETURNING edge_id
                """,
                (snapshot_id, int(row.src_wallet_id), int(row.dst_wallet_id),
                 float(row.edge_weight), int(row.transfer_count)),
            )
            fetched = cur.fetchone()
            if fetched:
                edge_id = fetched[0]
                for tx_hash in row.tx_hashes:
                    cur.execute(
                        """INSERT INTO snapshot_edge_tx (snapshot_edge_id, tx_hash)
                           VALUES (%s,%s) ON CONFLICT DO NOTHING""",
                        (edge_id, tx_hash),
                    )
    conn.commit()
    return snapshot_id


def export(start_block: int, end_block: int, snapshot_name: str, out_dir: str = "snapshots"):
    conn = psycopg2.connect(**config.PG_DSN)
    try:
        edge_df = build_edge_dataframe(conn, start_block, end_block)
        if edge_df.empty:
            raise SystemExit(
                f"No transactions found in block range [{start_block}, {end_block}]. "
                "Did ingestion/ingest_subgraph.py run first?"
            )
        out_path = pathlib.Path(out_dir) / f"{snapshot_name}.parquet"
        checksum = write_parquet(edge_df, out_path)
        snapshot_id = persist_snapshot(conn, snapshot_name, out_path, start_block, end_block, edge_df, checksum)
        return snapshot_id, out_path
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-block", type=int, default=0)
    parser.add_argument("--end-block", type=int, default=999_999_999)
    parser.add_argument("--name", required=True, help="unique snapshot_name, e.g. polynetwork_2hop_v1")
    parser.add_argument("--out-dir", default="snapshots")
    args = parser.parse_args()

    snapshot_id, out_path = export(args.start_block, args.end_block, args.name, args.out_dir)
    print(f"snapshot_id={snapshot_id} written to {out_path}")


if __name__ == "__main__":
    main()
