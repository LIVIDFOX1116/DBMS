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
import json
import logging
import pathlib
import time
from decimal import Decimal

import psycopg2
import psycopg2.extras
import requests

from . import config
from .etherscan_client import EtherscanClient

log = logging.getLogger("ingest_subgraph")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
CHECKPOINT_DIR = pathlib.Path(".state")


def norm(addr: str) -> str:
    return addr.lower() if addr else None


# RAW SQL (not yet converted): ingestion crawler statistics refresh on the crawl's connection, one Etherscan record at a time
def update_wallet_stats(conn) -> int:
    """Backfills wallet.tx_count/first_seen_block/last_seen_block from the
    transaction table. Deliberately does NOT touch balance_wei/total_sent_wei/
    total_recv_wei: those would need the wallet's *complete* on-chain history
    (an eth_getBalance / full tx history call per wallet), not just what's
    visible in this bounded subgraph. Computing them from partial data would
    understate every wallet that wasn't itself a BFS seed and present a wrong
    number as if it were authoritative -- worse than leaving it NULL. tx_count
    and first/last_seen_block are honest either way: they're framed as "seen
    in this subgraph," which is what they actually are.

    Run once after a crawl (or re-run any time; it's a plain aggregate
    UPDATE, safe to repeat). Returns the number of wallets updated.
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE wallet w SET
                tx_count = t.cnt,
                first_seen_block = t.min_block,
                last_seen_block = t.max_block
            FROM (
                SELECT wallet_id, COUNT(*) AS cnt, MIN(block_number) AS min_block, MAX(block_number) AS max_block
                FROM (
                    SELECT from_wallet_id AS wallet_id, block_number FROM transaction
                    UNION ALL
                    SELECT to_wallet_id AS wallet_id, block_number FROM transaction WHERE to_wallet_id IS NOT NULL
                ) touched
                GROUP BY wallet_id
            ) t
            WHERE w.wallet_id = t.wallet_id
        """)
        updated = cur.rowcount
    conn.commit()
    return updated


# RAW SQL (not yet converted): ingestion crawler address reclassification on the crawl's connection, one Etherscan record at a time
def reclassify_address_types(conn, client: EtherscanClient, limit: int = 100) -> int:
    """Corrects wallet.address_type using eth_getCode -- the one reliable
    contract-vs-EOA signal (see EtherscanClient.get_code). ingest_subgraph
    only ever marks CONTRACT for addresses discovered *as* an ERC20 token
    (upsert_token); anything reached solely through native transactions
    defaults to EOA even when it's obviously a contract, e.g. the Uniswap V2
    Router, which has no ERC20 token of its own and so is never seen via
    upsert_token at all.

    Checking eth_getCode costs one full extra API call per address, which is
    too expensive to run against every wallet in a real crawl (tens of
    thousands). Scoped to the highest-tx_count EOA-tagged wallets instead --
    a wrong label there is both most likely (high activity skews toward
    infrastructure, not individuals) and most visible (these are exactly
    what "top wallets" views surface). Run update_wallet_stats() first so
    tx_count is populated -- otherwise this has nothing to rank by.

    Returns the number of wallets reclassified to CONTRACT.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT wallet_id, address FROM wallet
               WHERE address_type = 'EOA' ORDER BY tx_count DESC NULLS LAST LIMIT %s""",
            (limit,),
        )
        candidates = cur.fetchall()

    reclassified = 0
    for wallet_id, address in candidates:
        code = client.get_code(address)
        if code and code not in ("0x", "0x0"):
            with conn.cursor() as cur:
                cur.execute("UPDATE wallet SET address_type = 'CONTRACT' WHERE wallet_id = %s", (wallet_id,))
            conn.commit()
            reclassified += 1
    return reclassified


# ---------------------------------------------------------------- DB helpers

# RAW SQL (not yet converted): ingestion crawler upsert on the crawl's connection, one Etherscan record at a time
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


# RAW SQL (not yet converted): ingestion crawler upsert on the crawl's connection, one Etherscan record at a time
def upsert_block(cur, block_number: int, block_hash: str, timestamp: int) -> None:
    cur.execute(
        """
        INSERT INTO block (block_number, block_hash, block_timestamp)
        VALUES (%s, %s, to_timestamp(%s))
        ON CONFLICT (block_number) DO NOTHING
        """,
        (block_number, block_hash, timestamp),
    )


# RAW SQL (not yet converted): ingestion crawler insert on the crawl's connection, one Etherscan record at a time
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


# RAW SQL (not yet converted): ingestion crawler upsert on the crawl's connection, one Etherscan record at a time
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


# RAW SQL (not yet converted): ingestion crawler insert on the crawl's connection, one Etherscan record at a time
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
        # token.symbol/token_name are sized generously (see schema.sql), but
        # an ERC20 contract's symbol()/name() is fully attacker-controlled
        # and unbounded -- truncate defensively rather than trust any column
        # width to be enough for whatever a spam/phishing token embeds there.
        symbol = (tt.get("tokenSymbol") or "")[:128] or None
        token_name = (tt.get("tokenName") or "")[:255] or None
        token_id = upsert_token(
            cur, contract_wallet_id, contract_addr,
            symbol, token_name, int(tt.get("tokenDecimal") or 18),
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


# RAW SQL (not yet converted): ingestion crawler lookup on the crawl's connection, one Etherscan record at a time
def _transaction_exists(cur, tx_hash: str) -> bool:
    cur.execute("SELECT 1 FROM transaction WHERE tx_hash = %s", (tx_hash,))
    return cur.fetchone() is not None


MAX_ADDRESS_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5


def process_address_with_retry(cur, client: EtherscanClient, address: str) -> set:
    """process_address wrapped with a bounded retry for transient network
    failures. Confirmed in production: a single Etherscan ReadTimeoutError
    deep into an hours-long crawl (hop 2 of a 3-hop run, ~508k wallets in)
    propagated all the way up through main() and killed the whole process --
    not a bug in the parsing/DB logic, just an unhandled transient network
    exception with no retry anywhere in the call chain. Nothing noticed or
    resumed it until a human checked in later.

    Retrying the whole process_address() call (not just the failed API call
    inside it) is safe because every DB write it makes is an upsert -- see
    this module's own docstring ("re-running the crawl is safe and
    idempotent"), which already has to hold for the checkpoint/resume design
    to work at all. Only requests.exceptions.RequestException (timeouts,
    connection errors -- genuinely transient) is retried; anything else (a
    real parsing bug, a DB constraint violation) propagates immediately
    rather than being silently retried into a worse or masked state.
    """
    for attempt in range(1, MAX_ADDRESS_RETRIES + 1):
        try:
            return process_address(cur, client, address)
        except requests.exceptions.RequestException as e:
            if attempt == MAX_ADDRESS_RETRIES:
                log.warning(
                    "giving up on %s after %d attempts (%s) -- skipping for this run, "
                    "will be retried the next time this crawl resumes (not marked visited)",
                    address, MAX_ADDRESS_RETRIES, e,
                )
                raise
            log.warning(
                "transient error on %s (attempt %d/%d): %s -- retrying in %ds",
                address, attempt, MAX_ADDRESS_RETRIES, e, RETRY_BACKOFF_SECONDS,
            )
            time.sleep(RETRY_BACKOFF_SECONDS)


# ------------------------------------------------------------- checkpointing
# Per-address DB commits (below) make each address's *data* durable, but the
# BFS frontier itself only ever lived in memory -- so before this existed,
# re-running after any interruption restarted the walk from the seed and
# re-crawled every already-done address from scratch (harmless to the DB via
# upserts, but a full waste of the rate-limited API calls and time already
# spent). This persists (hop, frontier, visited, next_frontier) to disk after
# every address so a resumed run picks up exactly where it left off.

def _checkpoint_path(seed_address: str, hop_count: int) -> pathlib.Path:
    return CHECKPOINT_DIR / f"crawl_{norm(seed_address)}_{hop_count}.json"


def _load_checkpoint(path: pathlib.Path) -> dict:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("Ignoring unreadable checkpoint at %s", path)
        return None


def _save_checkpoint(path: pathlib.Path, hop: int, frontier: set, visited: set, next_frontier: set) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "hop": hop,
        "frontier": sorted(frontier),
        "visited": sorted(visited),
        "next_frontier": sorted(next_frontier),
    }))


def crawl(seed_address: str, hop_count: int, client: EtherscanClient = None, conn=None,
          resume: bool = False, progress=None, should_stop=None) -> dict:
    """BFS out to `hop_count` hops from `seed_address`. Returns simple stats.

    resume=True (the CLI default) checkpoints progress to .state/ and picks
    back up from there if a matching checkpoint exists; left off (False) for
    the unit tests, which should stay hermetic -- no file I/O against a fake
    client's data.

    `progress(stats)` and `should_stop()` exist for the app's automatic
    ingestion, which has to show what is happening and be cancellable from
    a button. Both are called once per address, at the point where a
    checkpoint is written, so stopping there loses no work: everything up to
    that address is committed and checkpointed, and the rest resumes later.
    A crawl runs for minutes to hours, so a Cancel that only takes effect at
    the end of a hop would not be a Cancel.
    """
    client = client or EtherscanClient()
    own_conn = conn is None
    conn = conn or psycopg2.connect(**config.PG_DSN)
    stats = {"addresses_processed": 0, "hops_completed": 0}

    ckpt_path = _checkpoint_path(seed_address, hop_count) if resume else None
    ckpt = _load_checkpoint(ckpt_path) if ckpt_path else None

    try:
        if ckpt:
            visited = set(ckpt["visited"])
            frontier = set(ckpt["frontier"])
            next_frontier = set(ckpt["next_frontier"])
            start_hop = ckpt["hop"]
            log.info(
                "Resuming from checkpoint: hop=%d, %d addresses already visited, %d pending this hop",
                start_hop, len(visited), len(frontier - visited),
            )
        else:
            visited = set()
            frontier = {norm(seed_address)}
            next_frontier = set()
            start_hop = 0

        for hop in range(start_hop, hop_count + 1):
            if not frontier:
                break
            for addr in sorted(frontier):
                if addr in visited:
                    continue
                log.info("hop %d: processing %s", hop, addr)
                # Commit per address, not once per whole hop: a hop can hold
                # hundreds of addresses and take tens of minutes end to end
                # (rate-limited API calls), so a crash or interruption
                # partway through used to roll back everything done since
                # the last hop boundary. Each address's writes are already a
                # natural atomic unit (upserts, safe to redo), so committing
                # here bounds the lost work to at most one in-flight address.
                try:
                    with conn.cursor() as cur:
                        next_frontier |= process_address_with_retry(cur, client, addr)
                except requests.exceptions.RequestException as e:
                    # process_address_with_retry already retried MAX_ADDRESS_RETRIES
                    # times -- this address's own API calls are persistently
                    # failing (Etherscan down, sustained network issue, etc.).
                    # Confirmed in production: before this existed, exactly
                    # this scenario (one address, one ReadTimeoutError) killed
                    # an hours-long crawl outright, and nothing resumed it
                    # until a human noticed. Rolling back and NOT adding addr
                    # to `visited` leaves it in `frontier` for the next resume
                    # -- its data isn't lost, just deferred -- while the rest
                    # of this hop keeps making progress instead of the whole
                    # process dying over one address.
                    conn.rollback()
                    log.error(
                        "%s still failing after retries (%s) -- leaving unvisited for the "
                        "next resume, continuing with the rest of this hop", addr, e,
                    )
                    continue
                conn.commit()
                visited.add(addr)
                stats["addresses_processed"] += 1
                stats["hops_completed"] = hop
                stats["frontier_size"] = len(next_frontier - visited)
                stats["calls_made"] = getattr(client, "calls_made", None)
                if ckpt_path:
                    _save_checkpoint(ckpt_path, hop, frontier, visited, next_frontier)
                if progress is not None:
                    progress(dict(stats))
                if should_stop is not None and should_stop():
                    stats["stopped_early"] = True
                    log.info("stopping after %d addresses: caller asked to stop",
                             stats["addresses_processed"])
                    return stats
            frontier = next_frontier - visited
            next_frontier = set()
            stats["hops_completed"] = hop + 1

        if ckpt_path and ckpt_path.exists():
            ckpt_path.unlink()  # full run completed -- nothing left to resume
    finally:
        if own_conn:
            conn.close()

    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default=config.CASE_STUDY_ADDRESS)
    parser.add_argument("--hops", type=int, default=config.HOP_COUNT)
    parser.add_argument("--reclassify-top", type=int, default=100,
                         help="how many top-tx_count EOA wallets to check via eth_getCode (0 to skip)")
    args = parser.parse_args()

    log.info("Starting crawl: seed=%s hops=%d", args.address, args.hops)
    stats = crawl(args.address, args.hops, resume=True)
    log.info("Done: %s", stats)

    conn = psycopg2.connect(**config.PG_DSN)
    try:
        updated = update_wallet_stats(conn)
        log.info("Backfilled tx_count/first_seen_block/last_seen_block for %d wallets", updated)

        if args.reclassify_top > 0:
            reclassified = reclassify_address_types(conn, EtherscanClient(), limit=args.reclassify_top)
            log.info("Reclassified %d of the top %d EOA-tagged wallets as CONTRACT",
                      reclassified, args.reclassify_top)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
