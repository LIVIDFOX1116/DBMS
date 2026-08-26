"""
Pull a bounded, reproducible subgraph (Phase 1 in the project bible) around
a case-study address and load it into Postgres per db/schema.sql.

    python -m ingestion.ingest_subgraph                # uses config defaults
    python -m ingestion.ingest_subgraph --hops 1        # override hop count
    python -m ingestion.ingest_subgraph --address 0x... # override seed

Design notes:
  - BFS by hop count, not by transaction count, so the subgraph boundary is a
    documented, defensible choice ("everything within N hops of X") rather
    than an arbitrary row limit.
  - All DB writes are upserts (ON CONFLICT DO NOTHING/UPDATE) so re-running
    the crawl is safe and idempotent -- you will re-run this.
  - block.proposer_wallet_id is left NULL here: Etherscan's txlist endpoint
    doesn't return the block proposer/fee-recipient address, only blockHash
    and blockNumber. Populating it would need a separate eth_getBlockByNumber
    call per block -- deliberately out of scope for the MVP (see the bible's
    descope order, Section 8.2).
"""
import argparse
import logging
from decimal import Decimal

import psycopg2
import psycopg2.extras

from . import config
from .etherscan_client import EtherscanClient

log = logging.getLogger("ingest_subgraph")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def norm(addr: str) -> str:
    return addr.lower() if addr else None


# ---------------------------------------------------------------- DB helpers

def upsert_wallet(cur, address: str, address_type: str = "EOA") -> int:
    cur.execute(
        """
        INSERT INTO wallet (address, address_type)
        VALUES (%s, %s)
        ON CONFLICT (address) DO UPDATE SET address_type = EXCLUDED.address_type
        RETURNING wallet_id
        """,
        (address, address_type),
    )
    return cur.fetchone()[0]


def upsert_block(cur, block_number: int, block_hash: str, timestamp: int) -> None:
    cur.execute(
        """
        INSERT INTO block (block_number, block_hash, block_timestamp)
        VALUES (%s, %s, to_timestamp(%s))
        ON CONFLICT (block_number) DO NOTHING
        """,
        (block_number, block_hash, timestamp),
    )


def insert_transaction(cur, tx: dict, from_wallet_id: int, to_wallet_id: int) -> None:
    cur.execute(
        """
        INSERT INTO transaction
            (tx_hash, block_number, from_wallet_id, to_wallet_id, value_wei,
             gas_price, gas_used, tx_index, nonce, status, method_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (tx_hash) DO NOTHING
        """,
        (
            tx["hash"], int(tx["blockNumber"]), from_wallet_id, to_wallet_id,
            Decimal(tx["value"]), int(tx["gasPrice"]), int(tx.get("gasUsed") or 0),
            int(tx.get("transactionIndex") or 0), int(tx.get("nonce") or 0),
            tx.get("txreceipt_status") == "1", (tx.get("methodId") or None),
        ),
    )


def upsert_token(cur, contract_wallet_id: int, contract_address: str, symbol: str, name: str, decimals: int) -> int:
    cur.execute(
        """
        INSERT INTO token (contract_wallet_id, contract_address, symbol, token_name, decimals)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (contract_address) DO UPDATE SET symbol = EXCLUDED.symbol
        RETURNING token_id
        """,
        (contract_wallet_id, contract_address, symbol, name, decimals),
    )
    return cur.fetchone()[0]


def insert_token_transfer(cur, tt: dict, token_id: int, from_wallet_id: int, to_wallet_id: int) -> None:
    cur.execute(
        """
        INSERT INTO token_transfer
            (tx_hash, token_id, from_wallet_id, to_wallet_id, token_amount, log_index)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (tx_hash, log_index) DO NOTHING
        """,
        (
            tt["hash"], token_id, from_wallet_id, to_wallet_id,
            Decimal(tt["value"]), int(tt.get("transactionIndex") or 0),
        ),
    )


# ------------------------------------------------------------------ crawler

def process_address(cur, client: EtherscanClient, address: str) -> set:
    """Ingest one address's transactions + token transfers. Returns the set of
    counterparty addresses discovered (the next BFS frontier)."""
    address = norm(address)
    discovered = set()

    for tx in client.get_normal_transactions(address):
        from_addr, to_addr = norm(tx["from"]), norm(tx.get("to")) or None
        if not to_addr:
            continue  # contract-creation tx: no recipient wallet to link
        from_id = upsert_wallet(cur, from_addr)
        to_id = upsert_wallet(cur, to_addr)
        upsert_block(cur, int(tx["blockNumber"]), tx["blockHash"], int(tx["timeStamp"]))
        insert_transaction(cur, tx, from_id, to_id)
        discovered.update([from_addr, to_addr])

    for tt in client.get_token_transfers(address):
        from_addr, to_addr = norm(tt["from"]), norm(tt.get("to"))
        contract_addr = norm(tt["contractAddress"])
        if not (from_addr and to_addr and contract_addr):
            continue
        from_id = upsert_wallet(cur, from_addr)
        to_id = upsert_wallet(cur, to_addr)
        contract_wallet_id = upsert_wallet(cur, contract_addr, address_type="CONTRACT")
        token_id = upsert_token(
            cur, contract_wallet_id, contract_addr,
            tt.get("tokenSymbol"), tt.get("tokenName"), int(tt.get("tokenDecimal") or 18),
        )
        # token_transfer.tx_hash is a foreign key into transaction. The parent
        # tx is almost always already inserted (get_normal_transactions runs
        # first, above) -- but if this address's own top-level tx wasn't
        # captured for some reason, inserting the transfer would violate the
        # FK. Skip rather than crash; this is a documented limitation, not a
        # silent data-quality bug.
        if not _transaction_exists(cur, tt["hash"]):
            log.warning("skipping token transfer %s: parent transaction not ingested", tt["hash"])
            continue
        insert_token_transfer(cur, tt, token_id, from_id, to_id)
        discovered.update([from_addr, to_addr, contract_addr])

    return discovered - {ZERO_ADDRESS}


def _transaction_exists(cur, tx_hash: str) -> bool:
    cur.execute("SELECT 1 FROM transaction WHERE tx_hash = %s", (tx_hash,))
    return cur.fetchone() is not None


def crawl(seed_address: str, hop_count: int, client: EtherscanClient = None, conn=None) -> dict:
    """BFS out to `hop_count` hops from `seed_address`. Returns simple stats."""
    client = client or EtherscanClient()
    own_conn = conn is None
    conn = conn or psycopg2.connect(**config.PG_DSN)
    stats = {"addresses_processed": 0, "hops_completed": 0}

    try:
        visited = set()
        frontier = {norm(seed_address)}
        for hop in range(hop_count + 1):
            if not frontier:
                break
            next_frontier = set()
            with conn.cursor() as cur:
                for addr in sorted(frontier):
                    if addr in visited:
                        continue
                    log.info("hop %d: processing %s", hop, addr)
                    next_frontier |= process_address(cur, client, addr)
                    visited.add(addr)
                    stats["addresses_processed"] += 1
            conn.commit()
            frontier = next_frontier - visited
            stats["hops_completed"] = hop + 1
    finally:
        if own_conn:
            conn.close()

    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default=config.CASE_STUDY_ADDRESS)
    parser.add_argument("--hops", type=int, default=config.HOP_COUNT)
    args = parser.parse_args()

    log.info("Starting crawl: seed=%s hops=%d", args.address, args.hops)
    stats = crawl(args.address, args.hops)
    log.info("Done: %s", stats)


if __name__ == "__main__":
    main()
