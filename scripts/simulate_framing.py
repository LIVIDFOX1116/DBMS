"""
FrameCheck Phase 5: framing simulator.

Injects a synthetic dust-attack scenario -- NEVER into the main database.
Runs entirely against a separate `txgraph_sim` database, created from the
main one via CREATE DATABASE ... TEMPLATE (a fast native clone, no dump/
restore round-trip) the first time it's needed. This script's injection
path never connects to the main database (config.PG_DSN) at all -- only
to 'postgres' (to create/drop the clone) and to txgraph_sim itself.

    python scripts/simulate_framing.py --snapshot-id 4 --seed-wallet-id 1476
    python scripts/simulate_framing.py --cleanup 3
    python scripts/simulate_framing.py --drop-db

Defaults: 100 target wallets with no recorded path from the seed (absent
from the seed's own BFS reachability set for the snapshot's latest bfs
run), 0.001 ETH dust each, gas = 21000 * 20 gwei -- a documented constant
for cost estimation, NOT a live gas-price oracle lookup; real gas prices
vary continuously and this is a simplification.
"""
import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
from psycopg2 import sql as pgsql
from psycopg2.extras import execute_values

from ingestion import config

SIM_DB_NAME = "txgraph_sim"
SYNTHETIC_BLOCK_BASE = 900_000_000  # comfortably above any real Ethereum block number
SYNTHETIC_TX_PREFIX = "deadbeef"    # clearly non-real; never a valid keccak256 digest in practice

DEFAULT_NUM_TARGETS = 100
DEFAULT_DUST_ETH = 0.001
DUST_GAS_USED = 21000               # a standard ETH transfer's gas cost
DUST_GAS_PRICE_GWEI = 20            # documented constant, not a live oracle lookup
DUST_GAS_PRICE_WEI = DUST_GAS_PRICE_GWEI * 10 ** 9

# "Engaged launderer" scenario -- the active counterpart to the passive
# dust targets above. See inject_launderer_scenario.
DEFAULT_NUM_LAUNDERERS = 20
DEFAULT_LAUNDER_ETH = 1.0
DEFAULT_LAUNDER_FORWARD_FRACTION = 0.80
LAUNDER_FORWARD_BLOCK_OFFSET = 50   # comfortably inside any "within 100 blocks" requirement


def _sim_dsn(dbname: str = SIM_DB_NAME) -> dict:
    dsn = dict(config.PG_DSN)
    dsn["dbname"] = dbname
    return dsn


def ensure_sim_db_exists() -> bool:
    """Clones the main database into txgraph_sim via CREATE DATABASE ...
    TEMPLATE, if it doesn't already exist. Connects to 'postgres' (the
    maintenance DB), never to the main database itself -- same pattern
    db/init_db.py's ensure_database_exists() uses for the main DB.
    Postgres requires no other connections to the template DB at clone
    time; if this fails with "source database is being accessed by other
    users", close other connections to the main database and retry.
    Returns True if it created the database, False if it already existed."""
    ctrl_dsn = dict(config.PG_DSN)
    ctrl_dsn["dbname"] = "postgres"
    conn = psycopg2.connect(**ctrl_dsn)
    conn.autocommit = True  # CREATE DATABASE cannot run inside a transaction
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (SIM_DB_NAME,))
            if cur.fetchone() is not None:
                return False
            cur.execute(
                pgsql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                    pgsql.Identifier(SIM_DB_NAME), pgsql.Identifier(config.PG_DSN["dbname"])
                )
            )
        return True
    finally:
        conn.close()


def drop_sim_db() -> None:
    ctrl_dsn = dict(config.PG_DSN)
    ctrl_dsn["dbname"] = "postgres"
    conn = psycopg2.connect(**ctrl_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(pgsql.SQL("DROP DATABASE IF EXISTS {}").format(pgsql.Identifier(SIM_DB_NAME)))
    finally:
        conn.close()


def synthetic_block_number(scenario_id: int) -> int:
    return SYNTHETIC_BLOCK_BASE + scenario_id


def synthetic_tx_hash(scenario_id: int, index: int) -> str:
    """66 chars total (0x + 64 hex), always starting with the
    SYNTHETIC_TX_PREFIX marker so a synthetic transaction is identifiable
    on sight, independent of is_simulated."""
    return "0x" + SYNTHETIC_TX_PREFIX + f"{scenario_id:08x}{index:048x}"


def select_target_wallets(conn, seed_wallet_id: int, bfs_run_id: int, num_targets: int) -> list:
    """Wallets with NO recorded path from the seed under this BFS run --
    i.e. absent from vertex_metric's bfs_distance_from_<seed> rows for
    this run_id. A random sample, not the first N by id, so repeated runs
    aren't biased toward low wallet_ids."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT w.wallet_id FROM wallet w
            WHERE w.wallet_id != %s
              AND w.wallet_id NOT IN (
                  SELECT wallet_id FROM vertex_metric
                  WHERE run_id = %s AND metric_name LIKE 'bfs_distance_from_%%'
              )
            ORDER BY RANDOM()
            LIMIT %s
            """,
            (seed_wallet_id, bfs_run_id, num_targets),
        )
        return [row[0] for row in cur.fetchall()]


def select_attacker_wallet(conn, seed_wallet_id: int) -> int:
    """A real wallet the seed has directly funded (hop=1) -- "funded by
    the seed" is a real, already-true precondition, not something to
    fabricate. Falls back to the seed itself if it has no direct outgoing
    transactions in this database at all."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_wallet_id FROM transaction WHERE from_wallet_id = %s AND to_wallet_id IS NOT NULL LIMIT 1",
            (seed_wallet_id,),
        )
        row = cur.fetchone()
    return row[0] if row else seed_wallet_id


def validate_wallet_ids(conn, wallet_ids: list) -> None:
    """Raises if any given wallet_id doesn't actually exist in this
    (simulation) database's wallet table -- required before any synthetic
    transaction referencing it is inserted."""
    ids = list(set(wallet_ids))
    with conn.cursor() as cur:
        cur.execute("SELECT wallet_id FROM wallet WHERE wallet_id = ANY(%s)", (ids,))
        found = {row[0] for row in cur.fetchall()}
    missing = set(ids) - found
    if missing:
        raise ValueError(f"wallet_id(s) not found in the simulation database: {sorted(missing)}")


def inject_framing_scenario(conn, seed_wallet_id: int, bfs_run_id: int, num_targets: int = DEFAULT_NUM_TARGETS,
                             dust_eth: float = DEFAULT_DUST_ETH, attacker_wallet_id: int = None) -> int:
    """Inserts one framing_scenario row plus one synthetic block and one
    synthetic dust transaction per target wallet, all tagged
    is_simulated=TRUE. Returns the new scenario_id."""
    targets = select_target_wallets(conn, seed_wallet_id, bfs_run_id, num_targets)
    if not targets:
        raise ValueError("no wallets with zero recorded path from the seed were found to target")

    attacker_wallet_id = attacker_wallet_id or select_attacker_wallet(conn, seed_wallet_id)
    validate_wallet_ids(conn, [attacker_wallet_id] + targets)

    dust_wei = int(dust_eth * 10 ** 18)
    total_value_wei = dust_wei * len(targets)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO framing_scenario (attacker_wallet_ids, target_wallet_ids, transfer_count, total_value_wei)
            VALUES (%s, %s, %s, %s) RETURNING scenario_id
            """,
            ([attacker_wallet_id], targets, len(targets), total_value_wei),
        )
        scenario_id = cur.fetchone()[0]

        block_number = synthetic_block_number(scenario_id)
        block_hash = "0x" + SYNTHETIC_TX_PREFIX + f"{scenario_id:056x}"
        cur.execute(
            """
            INSERT INTO block (block_number, block_hash, proposer_wallet_id, block_timestamp, gas_used, gas_limit, tx_count)
            VALUES (%s, %s, NULL, NOW(), %s, %s, %s)
            """,
            (block_number, block_hash, DUST_GAS_USED * len(targets), DUST_GAS_USED * len(targets) * 10, len(targets)),
        )

        tx_rows = [
            (
                synthetic_tx_hash(scenario_id, i), block_number, attacker_wallet_id, target_id, dust_wei,
                DUST_GAS_PRICE_WEI, DUST_GAS_USED, i, 0, True, None, True,
            )
            for i, target_id in enumerate(targets)
        ]
        execute_values(
            cur,
            """
            INSERT INTO transaction
                (tx_hash, block_number, from_wallet_id, to_wallet_id, value_wei,
                 gas_price, gas_used, tx_index, nonce, status, method_id, is_simulated)
            VALUES %s
            """,
            tx_rows,
        )
    conn.commit()
    return scenario_id


def cleanup_scenario(conn, scenario_id: int) -> dict:
    """Reverses inject_framing_scenario for this scenario_id: removes its
    synthetic transaction rows, its synthetic block, and the
    framing_scenario row itself. block_number is deterministically
    recomputed from scenario_id, so this works even if the
    framing_scenario row was already removed by hand."""
    block_number = synthetic_block_number(scenario_id)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM transaction WHERE block_number = %s AND is_simulated = TRUE", (block_number,))
        tx_deleted = cur.rowcount
        cur.execute("DELETE FROM block WHERE block_number = %s", (block_number,))
        block_deleted = cur.rowcount
        cur.execute("DELETE FROM framing_scenario WHERE scenario_id = %s", (scenario_id,))
        scenario_deleted = cur.rowcount
    conn.commit()
    return {"transactions": tx_deleted, "blocks": block_deleted, "scenarios": scenario_deleted}


def synthetic_wallet_address(scenario_id: int, role: str, index: int) -> str:
    """Deterministic 42-char hex address for a BRAND NEW synthetic wallet
    -- used only by inject_launderer_scenario. Dust targets in
    inject_framing_scenario reuse real existing wallets and never need
    this; a launderer and its downstream recipient must be wallets that
    cannot already exist by chance ("received from the seed and
    forwarded most of it on, fast" is exactly the behavior under test,
    not something to go looking for in real data). role ('launderer' or
    'laundered') is folded into the hash so the two roles can never
    collide for the same scenario_id/index."""
    digest = hashlib.sha256(f"{SYNTHETIC_TX_PREFIX}:{scenario_id}:{role}:{index}".encode()).hexdigest()
    return "0x" + digest[:40]


def insert_synthetic_wallet(conn, scenario_id: int, role: str, index: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO wallet (address, address_type) VALUES (%s, 'EOA') RETURNING wallet_id",
            (synthetic_wallet_address(scenario_id, role, index),),
        )
        return cur.fetchone()[0]


def inject_launderer_scenario(conn, seed_wallet_id: int, num_launderers: int = DEFAULT_NUM_LAUNDERERS,
                               amount_eth: float = DEFAULT_LAUNDER_ETH,
                               forward_fraction: float = DEFAULT_LAUNDER_FORWARD_FRACTION) -> dict:
    """The "engaged launderer" counterpart to inject_framing_scenario's
    passive dust victims: injects num_launderers BRAND NEW wallets, each
    receiving amount_eth directly from the seed (block block_in) then
    forwarding forward_fraction of it onward to its own BRAND NEW
    downstream wallet (block block_in + LAUNDER_FORWARD_BLOCK_OFFSET).
    This is meant to make bfs_proximity and engaged_proximity disagree in
    a controlled, legible way: bfs_proximity sees a wallet one hop from
    the seed either way (passive or active) and is provably invariant to
    removing a wallet's own signed edges (see graph/audit.py's
    audit_bfs_proximity docstring), so it should flag both this and dust
    victims, and call both frameable. engaged_proximity's signal IS the
    forwarding itself -- strip the launderer's own signed forward
    transaction and the signal disappears, so it should flag ONLY
    launderers (never passive dust victims, who forward nothing) and
    audit them as robust, not frameable.

    Returns {"scenario_id", "launderer_wallet_ids", "downstream_wallet_ids"}."""
    amount_wei = int(amount_eth * 10 ** 18)
    forward_wei = int(amount_wei * forward_fraction)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO framing_scenario (attacker_wallet_ids, target_wallet_ids, transfer_count, total_value_wei)
            VALUES (%s, %s, %s, %s) RETURNING scenario_id
            """,
            ([seed_wallet_id], [], 2 * num_launderers, amount_wei * num_launderers),
        )
        scenario_id = cur.fetchone()[0]

    launderer_ids = [insert_synthetic_wallet(conn, scenario_id, "launderer", i) for i in range(num_launderers)]
    downstream_ids = [insert_synthetic_wallet(conn, scenario_id, "laundered", i) for i in range(num_launderers)]

    block_in = synthetic_block_number(scenario_id)
    block_out = block_in + LAUNDER_FORWARD_BLOCK_OFFSET
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE framing_scenario SET target_wallet_ids = %s WHERE scenario_id = %s",
            (launderer_ids, scenario_id),
        )
        for block_number, suffix in ((block_in, "0"), (block_out, "1")):
            cur.execute(
                """
                INSERT INTO block (block_number, block_hash, proposer_wallet_id, block_timestamp, gas_used, gas_limit, tx_count)
                VALUES (%s, %s, NULL, NOW(), %s, %s, %s)
                """,
                (
                    block_number, "0x" + SYNTHETIC_TX_PREFIX + f"{scenario_id:055x}{suffix}",
                    DUST_GAS_USED * num_launderers, DUST_GAS_USED * num_launderers * 10, num_launderers,
                ),
            )

        inbound_rows = [
            (
                synthetic_tx_hash(scenario_id, i), block_in, seed_wallet_id, launderer_ids[i], amount_wei,
                DUST_GAS_PRICE_WEI, DUST_GAS_USED, i, 0, True, None, True,
            )
            for i in range(num_launderers)
        ]
        outbound_rows = [
            (
                synthetic_tx_hash(scenario_id, num_launderers + i), block_out, launderer_ids[i], downstream_ids[i],
                forward_wei, DUST_GAS_PRICE_WEI, DUST_GAS_USED, i, 0, True, None, True,
            )
            for i in range(num_launderers)
        ]
        execute_values(
            cur,
            """
            INSERT INTO transaction
                (tx_hash, block_number, from_wallet_id, to_wallet_id, value_wei,
                 gas_price, gas_used, tx_index, nonce, status, method_id, is_simulated)
            VALUES %s
            """,
            inbound_rows + outbound_rows,
        )
    conn.commit()
    return {"scenario_id": scenario_id, "launderer_wallet_ids": launderer_ids, "downstream_wallet_ids": downstream_ids}


def cleanup_launderer_scenario(conn, scenario_id: int) -> dict:
    """Reverses inject_launderer_scenario: removes its synthetic
    transactions, both of its synthetic blocks (an inbound AND an
    outbound block, vs cleanup_scenario's single block for passive dust),
    the launderer/downstream WALLET rows it created (unlike dust targets,
    which reuse real existing wallets and are never deleted), and the
    framing_scenario row itself. Downstream wallet ids aren't stored on
    framing_scenario (only launderer ids, as target_wallet_ids) -- found
    instead via the synthetic transactions' own to_wallet_id, which still
    works even if the framing_scenario row is already gone."""
    with conn.cursor() as cur:
        cur.execute("SELECT target_wallet_ids FROM framing_scenario WHERE scenario_id = %s", (scenario_id,))
        row = cur.fetchone()
        launderer_ids = row[0] if row else []

        block_in = synthetic_block_number(scenario_id)
        block_out = block_in + LAUNDER_FORWARD_BLOCK_OFFSET
        cur.execute(
            "SELECT DISTINCT to_wallet_id FROM transaction WHERE block_number = %s AND is_simulated = TRUE",
            (block_out,),
        )
        downstream_ids = [r[0] for r in cur.fetchall() if r[0] is not None]
        all_synthetic_ids = launderer_ids + downstream_ids

        cur.execute("DELETE FROM transaction WHERE block_number = ANY(%s) AND is_simulated = TRUE", ([block_in, block_out],))
        tx_deleted = cur.rowcount
        cur.execute("DELETE FROM block WHERE block_number = ANY(%s)", ([block_in, block_out],))
        block_deleted = cur.rowcount
        cur.execute("DELETE FROM wallet WHERE wallet_id = ANY(%s)", (all_synthetic_ids,))
        wallet_deleted = cur.rowcount
        cur.execute("DELETE FROM framing_scenario WHERE scenario_id = %s", (scenario_id,))
        scenario_deleted = cur.rowcount
    conn.commit()
    return {
        "transactions": tx_deleted, "blocks": block_deleted,
        "wallets": wallet_deleted, "scenarios": scenario_deleted,
    }


def resolve_latest_bfs_run_id(conn, snapshot_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ar.run_id FROM analysis_run ar JOIN algorithm a ON a.algorithm_id = ar.algorithm_id
            WHERE ar.snapshot_id = %s AND a.algorithm_name = 'bfs' AND ar.status = 'DONE'
            ORDER BY ar.completed_at DESC LIMIT 1
            """,
            (snapshot_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise SystemExit(f"no completed bfs run for snapshot {snapshot_id} in the simulation database")
        return row[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-id", type=int, help="snapshot whose BFS run defines 'no path from the seed'")
    parser.add_argument("--seed-wallet-id", type=int)
    parser.add_argument("--bfs-run-id", type=int, default=None,
                         help="defaults to the latest completed bfs run for --snapshot-id")
    parser.add_argument("--num-targets", type=int, default=DEFAULT_NUM_TARGETS)
    parser.add_argument("--dust-eth", type=float, default=DEFAULT_DUST_ETH)
    parser.add_argument("--cleanup", type=int, default=None, metavar="SCENARIO_ID")
    parser.add_argument("--drop-db", action="store_true", help="drop the entire simulation database")
    args = parser.parse_args()

    if args.drop_db:
        drop_sim_db()
        print(f"dropped database '{SIM_DB_NAME}'")
        return

    created = ensure_sim_db_exists()
    if created:
        print(f"created simulation database '{SIM_DB_NAME}' (cloned from '{config.PG_DSN['dbname']}')")

    conn = psycopg2.connect(**_sim_dsn())
    try:
        if args.cleanup is not None:
            result = cleanup_scenario(conn, args.cleanup)
            print(f"cleaned up scenario {args.cleanup}: {result}")
            return

        if args.snapshot_id is None or args.seed_wallet_id is None:
            parser.error("--snapshot-id and --seed-wallet-id are required unless --cleanup or --drop-db is given")

        bfs_run_id = args.bfs_run_id or resolve_latest_bfs_run_id(conn, args.snapshot_id)
        scenario_id = inject_framing_scenario(conn, args.seed_wallet_id, bfs_run_id, args.num_targets, args.dust_eth)
        print(f"scenario_id={scenario_id}: injected dust into {args.num_targets} target wallet(s) "
              f"(attacker funded by seed {args.seed_wallet_id}), {args.dust_eth} ETH each")
        print(f"cleanup with: python scripts/simulate_framing.py --cleanup {scenario_id}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
