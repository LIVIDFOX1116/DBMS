"""
Export one snapshot's slice of the database as a single portable file, and
make the demo dataset the app ships with.

    python scripts/export_dataset.py --snapshot-id 1 --output datasets/snapshot1.sql.gz
    python scripts/export_dataset.py --snapshot-id 1 --output - | gzip -d | head

This is the other half of Feature 1's "import schema or database": the app
can import a dataset (api/dbsetup.import_dataset), and this is what produces
one -- for the bundled demo data, for moving a database between machines, and
for sharing a case study without shipping someone's whole Postgres.

Scoping. A snapshot is the natural unit: it has a fixed edge list, and
everything else follows from it -- the wallets those edges touch, the
transactions behind them, the blocks those sit in, the analysis runs over
that snapshot, and the clusters, metrics, flags, evidence and audits those
runs produced. Rows belonging to OTHER snapshots are left out, which is the
whole point: snapshot 1 is 330k rows against the full database's 7M+.

Format. Each table is a psql-style `COPY ... FROM stdin;` block followed by
its tab-separated rows and a `\\.` terminator. That means the file can be
loaded by `psql -f` as well as by this project's own importer, which parses
the blocks and streams each one through `copy_expert`. INSERT statements
would have been simpler to parse and several times larger and slower.

Read-only: SELECTs and COPY TO only, nothing is written to the database.
"""
import argparse
import gzip
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2

from ingestion import config

# The transactions this dataset carries: the ones behind the snapshot's
# edges, minus anything the framing simulator injected.
#
# Excluding simulated rows is not a detail. scripts/simulate_framing.py marks
# its synthetic transfers `transaction.is_simulated = true` precisely so no
# detector, query or UI ever mistakes one for real chain history -- and a
# dataset is the one artefact that leaves this machine, where that promise
# matters most. Every table scoped below filters through this same set, so a
# simulated transaction cannot arrive via a token transfer or a piece of flag
# evidence either.
TX_SCOPE = """
    SELECT set.tx_hash
      FROM snapshot_edge_tx set
      JOIN snapshot_edge e ON e.edge_id = set.snapshot_edge_id
      JOIN transaction t ON t.tx_hash = set.tx_hash
     WHERE e.snapshot_id = %(snapshot_id)s AND t.is_simulated IS NOT TRUE"""

# Every wallet any carried row points at -- not just the snapshot's edge
# endpoints.
#
# "The wallets the edges touch" was the obvious scope and it is wrong. The
# first import died on
#   token_contract_wallet_id_fkey: Key (contract_wallet_id)=(51040) is not
#   present in table "wallet"
# because an ERC-20 contract has a wallet row of its own and is not
# necessarily an endpoint of any edge in the snapshot. The same applies to a
# transaction's recipient, a token transfer's counterparties, a block
# proposer, and the wallets named by metrics, clusters, flags and alerts.
#
# So the scope is the closure: every wallet id referenced by anything this
# dataset carries. It terminates in one step, because `wallet` references
# nothing itself -- which is why the FK is the right thing to be guided by
# rather than guessing at the list.
WALLET_SCOPE = f"""
    SELECT src_wallet_id AS wallet_id FROM snapshot_edge WHERE snapshot_id = %(snapshot_id)s
    UNION SELECT dst_wallet_id FROM snapshot_edge WHERE snapshot_id = %(snapshot_id)s
    UNION SELECT t.from_wallet_id FROM transaction t WHERE t.tx_hash IN ({TX_SCOPE})
    UNION SELECT t.to_wallet_id FROM transaction t WHERE t.tx_hash IN ({TX_SCOPE})
    UNION SELECT tt.from_wallet_id FROM token_transfer tt WHERE tt.tx_hash IN ({TX_SCOPE})
    UNION SELECT tt.to_wallet_id FROM token_transfer tt WHERE tt.tx_hash IN ({TX_SCOPE})
    UNION SELECT tk.contract_wallet_id FROM token tk WHERE tk.token_id IN (
        SELECT tt.token_id FROM token_transfer tt WHERE tt.tx_hash IN ({TX_SCOPE}))
    UNION SELECT b.proposer_wallet_id FROM block b WHERE b.block_number IN (
        SELECT t.block_number FROM transaction t WHERE t.tx_hash IN ({TX_SCOPE}))
    UNION SELECT vm.wallet_id FROM vertex_metric vm
      JOIN analysis_run r ON r.run_id = vm.run_id WHERE r.snapshot_id = %(snapshot_id)s
    UNION SELECT cm.wallet_id FROM cluster_member cm JOIN cluster c ON c.cluster_id = cm.cluster_id
      JOIN analysis_run r ON r.run_id = c.run_id WHERE r.snapshot_id = %(snapshot_id)s
    UNION SELECT f.wallet_id FROM flag f
      JOIN analysis_run r ON r.run_id = f.run_id WHERE r.snapshot_id = %(snapshot_id)s
    UNION SELECT a.wallet_id FROM alert a
      JOIN analysis_run r ON r.run_id = a.run_id WHERE r.snapshot_id = %(snapshot_id)s"""

# Tables in foreign-key order, each with the query that scopes it to one
# snapshot. %(snapshot_id)s is bound, never formatted in.
#
# Order matters twice over: it is the order rows are written and therefore
# the order they are loaded, so every referenced row exists before the row
# that references it. Verified against db/schema.sql's REFERENCES clauses
# and the migrations' (flag -> analysis_run/wallet, flag_evidence -> flag/
# transaction, flag_audit -> flag).
TABLES = [
    # Reference data: small, and referenced by almost everything.
    ("analyst", "SELECT * FROM analyst"),
    ("algorithm", "SELECT * FROM algorithm"),
    ("label", "SELECT * FROM label"),

    ("wallet", f"""
        SELECT w.* FROM wallet w WHERE w.wallet_id IN (
            SELECT wallet_id FROM ({WALLET_SCOPE}) scoped WHERE wallet_id IS NOT NULL)"""),
    ("wallet_label", f"""
        SELECT wl.* FROM wallet_label wl WHERE wl.wallet_id IN (
            SELECT wallet_id FROM ({WALLET_SCOPE}) scoped WHERE wallet_id IS NOT NULL)"""),

    # Blocks must precede transactions, and only the blocks those
    # transactions actually sit in are worth carrying.
    ("block", f"""
        SELECT b.* FROM block b WHERE b.block_number IN (
            SELECT t.block_number FROM transaction t WHERE t.tx_hash IN ({TX_SCOPE}))"""),
    ("transaction", f"SELECT t.* FROM transaction t WHERE t.tx_hash IN ({TX_SCOPE})"),

    # Tokens, then the transfers that reference both a token and a carried
    # transaction.
    ("token", f"""
        SELECT tk.* FROM token tk WHERE tk.token_id IN (
            SELECT tt.token_id FROM token_transfer tt WHERE tt.tx_hash IN ({TX_SCOPE}))"""),
    ("token_transfer", f"SELECT tt.* FROM token_transfer tt WHERE tt.tx_hash IN ({TX_SCOPE})"),

    # The snapshot itself. snapshot_edge_tx is filtered through TX_SCOPE too,
    # so an edge backed only by simulated transactions keeps the edge and
    # drops the link, rather than pointing at a transaction that was left out.
    ("graph_snapshot", "SELECT * FROM graph_snapshot WHERE snapshot_id = %(snapshot_id)s"),
    ("snapshot_edge", "SELECT * FROM snapshot_edge WHERE snapshot_id = %(snapshot_id)s"),
    ("snapshot_edge_tx", f"""
        SELECT set.* FROM snapshot_edge_tx set
          JOIN snapshot_edge e ON e.edge_id = set.snapshot_edge_id
         WHERE e.snapshot_id = %(snapshot_id)s AND set.tx_hash IN ({TX_SCOPE})"""),

    # Analysis over it, and everything those runs produced.
    ("analysis_run", "SELECT * FROM analysis_run WHERE snapshot_id = %(snapshot_id)s"),
    ("vertex_metric", """
        SELECT vm.* FROM vertex_metric vm JOIN analysis_run r ON r.run_id = vm.run_id
         WHERE r.snapshot_id = %(snapshot_id)s"""),
    ("cluster", """
        SELECT c.* FROM cluster c JOIN analysis_run r ON r.run_id = c.run_id
         WHERE r.snapshot_id = %(snapshot_id)s"""),
    ("cluster_member", """
        SELECT cm.* FROM cluster_member cm JOIN cluster c ON c.cluster_id = cm.cluster_id
          JOIN analysis_run r ON r.run_id = c.run_id WHERE r.snapshot_id = %(snapshot_id)s"""),
    ("alert", """
        SELECT a.* FROM alert a JOIN analysis_run r ON r.run_id = a.run_id
         WHERE r.snapshot_id = %(snapshot_id)s"""),
    ("visualization", """
        SELECT v.* FROM visualization v JOIN analysis_run r ON r.run_id = v.run_id
         WHERE r.snapshot_id = %(snapshot_id)s"""),

    # FrameCheck's own output.
    ("flag", """
        SELECT f.* FROM flag f JOIN analysis_run r ON r.run_id = f.run_id
         WHERE r.snapshot_id = %(snapshot_id)s"""),
    ("flag_evidence", f"""
        SELECT fe.* FROM flag_evidence fe JOIN flag f ON f.flag_id = fe.flag_id
          JOIN analysis_run r ON r.run_id = f.run_id
         WHERE r.snapshot_id = %(snapshot_id)s AND fe.tx_hash IN ({TX_SCOPE})"""),
    ("flag_audit", """
        SELECT fa.* FROM flag_audit fa JOIN flag f ON f.flag_id = fa.flag_id
          JOIN analysis_run r ON r.run_id = f.run_id WHERE r.snapshot_id = %(snapshot_id)s"""),
]

# Deliberately NOT exported:
#   app_user          -- real people's Google identities. A dataset is for
#                        sharing; an account list is not, and an importing
#                        app mints its own.
#   framing_scenario  -- the framing simulator's own bookkeeping. Every row
#                        is a record of a synthetic experiment run on one
#                        machine, and the transactions it refers to are
#                        excluded above, so carrying the scenarios would
#                        leave records pointing at nothing.
#   schema_migration  -- the importing database's own history, written by
#                        whatever applied its schema.
EXCLUDED = {"app_user", "framing_scenario", "schema_migration"}

MARKER = "-- framecheck-dataset v1"


def table_columns(cur, table: str) -> list:
    cur.execute("""SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s
                    ORDER BY ordinal_position""", (table,))
    return [r[0] for r in cur.fetchall()]


def export(conn, snapshot_id: int, out, progress=None) -> dict:
    """Writes the dataset to the text stream `out`. Returns a manifest."""
    with conn.cursor() as cur:
        cur.execute("SELECT snapshot_name, vertex_count, edge_count FROM graph_snapshot WHERE snapshot_id = %s",
                    (snapshot_id,))
        row = cur.fetchone()
        if row is None:
            raise SystemExit(f"No graph_snapshot with id {snapshot_id}")
        snapshot_name, vertex_count, edge_count = row

        counts = {}
        body = io.StringIO()
        for table, query in TABLES:
            columns = table_columns(cur, table)
            if not columns:
                # A table the schema gained later, or one this database does
                # not have. Skipped rather than failing the whole export.
                counts[table] = None
                continue
            scoped = cur.mogrify(query, {"snapshot_id": snapshot_id}).decode()
            buf = io.StringIO()
            cur.copy_expert(f"COPY ({scoped}) TO STDOUT", buf)
            data = buf.getvalue()
            rows = data.count("\n")
            counts[table] = rows
            body.write(f"\n{MARKER} table {table} rows {rows}\n")
            body.write(f"COPY public.{table} ({', '.join(columns)}) FROM stdin;\n")
            body.write(data)
            body.write("\\.\n")
            if progress:
                progress(table, rows)

    manifest = {
        "format": "framecheck-dataset v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_id": snapshot_id,
        "snapshot_name": snapshot_name,
        "vertex_count": vertex_count,
        "edge_count": edge_count,
        "row_counts": counts,
        "total_rows": sum(v for v in counts.values() if v),
        "excluded_tables": sorted(EXCLUDED),
        "excludes_simulated_transactions": True,
    }
    out.write(f"{MARKER}\n")
    out.write(f"-- manifest {json.dumps(manifest)}\n")
    out.write("--\n")
    out.write("-- One snapshot's slice of a FrameCheck database: the edges, the wallets and\n")
    out.write("-- transactions behind them, and every analysis run over that snapshot.\n")
    out.write("-- Load it with the app's Database view (Import database), or psql -f.\n")
    out.write("-- Tables are in foreign-key order; load them in the order they appear.\n")
    out.write(body.getvalue())
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-id", type=int, required=True)
    parser.add_argument("--output", required=True, help="path ending .sql or .sql.gz, or - for stdout")
    args = parser.parse_args()

    conn = psycopg2.connect(**config.PG_DSN)
    t0 = time.perf_counter()
    try:
        if args.output == "-":
            manifest = export(conn, args.snapshot_id, sys.stdout)
            return
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        opener = (lambda: gzip.open(path, "wt", encoding="utf-8", newline="\n")) if path.suffix == ".gz" \
            else (lambda: path.open("w", encoding="utf-8", newline="\n"))
        with opener() as fh:
            manifest = export(conn, args.snapshot_id, fh,
                              progress=lambda t, n: print(f"  {t:<18} {n:>8,} rows", flush=True))
    finally:
        conn.close()

    size = Path(args.output).stat().st_size
    print(f"\nsnapshot {manifest['snapshot_id']} ({manifest['snapshot_name']}): "
          f"{manifest['total_rows']:,} rows -> {args.output} "
          f"({size / 1e6:.1f} MB) in {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
