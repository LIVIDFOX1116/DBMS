"""
FrameCheck Phase 3: detectors that read already-computed analysis results
(BFS distance, PageRank, Louvain/classify_clusters output, and native ETH
+ ERC-20 token transfer history) and write `flag` rows -- one row per
(run_id, wallet_id, detector) that FrameCheck's audit engine
(graph/audit.py, Phase 6) can later interrogate for frameability.
impurity/engaged_proximity track taint per currency independently (native
ETH and every ERC-20 contract get their own ledger) and flag a wallet if
ANY currency crosses the threshold -- see compute_impurity_ratios.

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
from collections import defaultdict

import psycopg2
from psycopg2.extras import execute_values

from graph.run_analysis import finish_run, start_run
from ingestion import config

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


def _apply_haircut_transfer(balance: dict, tainted: dict, from_id: int, to_id: int, value: float,
                             seed_wallet_id: int) -> float:
    """One step of the proportional-haircut taint-propagation model, shared
    by compute_impurity_ratios and compute_engaged_outflow_ratios below --
    see compute_impurity_ratios's docstring for the full model description
    and its partial-balance limitation, which applies wherever this is used.

    Mutates `balance`/`tainted` in place (both wallet_id -> float, created
    lazily). Returns taint_out -- the amount of taint that moved from
    from_id to to_id in this transfer -- since callers that track
    additional per-wallet bookkeeping (cumulative inflow, forwarding
    windows) need to see it directly, not just the updated running totals.
    """
    if from_id == seed_wallet_id:
        taint_out = value
    else:
        bal_before = balance.get(from_id, 0.0)
        taint_out = value * (tainted.get(from_id, 0.0) / bal_before) if bal_before > 0 else 0.0

    balance[from_id] = max(0.0, balance.get(from_id, 0.0) - value)
    tainted[from_id] = max(0.0, tainted.get(from_id, 0.0) - taint_out)
    balance[to_id] = balance.get(to_id, 0.0) + value
    tainted[to_id] = tainted.get(to_id, 0.0) + taint_out
    return taint_out


def compute_impurity_ratios(transfers: list, seed_wallet_id: int) -> dict:
    """Simplified Liu et al. "impurity" via proportional haircut taint
    propagation, in the block order given, tracked PER CURRENCY
    independently -- native ETH and each ERC-20 token contract get their
    own balance/tainted ledger, since a wallet's ETH holdings say nothing
    about its holdings of some unrelated token, and raw token amounts
    (arbitrary integer units, no shared scale across tokens) can't be
    mixed with wei or with each other.

    transfers: [(from_wallet_id, to_wallet_id, value, currency), ...],
    caller-sorted by (block_number, tx_index) WITHIN each currency (cross-
    currency order doesn't matter -- each currency's replay only ever
    looks at its own subsequence, re-grouped below) -- `currency` is None
    for native ETH (value in wei) or an ERC-20 contract_address string
    (value in that token's own raw integer units). The real signer is
    always the transfer's parent transaction.from_wallet_id, even for a
    token transfer -- see run_impurity's query; token_transfer.from_wallet_id
    itself is never trusted (it's attacker-spoofable via a fake Transfer
    event log, same reasoning as flag_evidence's role assignment).

    Model: the seed wallet is treated as a pure taint source for EVERY
    currency -- everything it sends is 100% tainted, regardless of its own
    tracked balance (we have no visibility into the seed's true prior
    balance split, and it is the known-bad address by construction of this
    whole case study, so diluting its outflow with an assumed-clean
    portion would be the wrong default). Every other wallet's outgoing
    taint fraction, per currency, is tainted_balance / balance in that
    currency at the moment it sends, carried forward proportionally (the
    standard "haircut"/proportional taint model used in blockchain
    forensics -- not a FIFO/LIFO UTXO tracing model, which this
    simplification deliberately does not attempt).

    KNOWN LIMITATION (partial balances): `balance` here is NOT a wallet's
    real on-chain balance in that currency -- it is only the net of
    transfers that happen to appear in this bounded 2-hop subgraph crawl.
    A wallet that received most of its real funds (in ETH or in some
    token) from outside the crawl will have an artificially low (or even
    negative, floored at 0) tracked balance here, which can inflate or
    deflate its computed ratio relative to its true on-chain impurity.
    This detector's output is only meaningful relative to the visible
    subgraph, never as an absolute on-chain claim.

    Returns wallet_id -> {currency: ratio}, one entry per currency the
    wallet ever held a nonzero balance of, EXCLUDING the seed itself.
    """
    by_currency = defaultdict(list)
    for from_id, to_id, value, currency in transfers:
        by_currency[currency].append((from_id, to_id, value))

    result = defaultdict(dict)
    for currency, currency_transfers in by_currency.items():
        balance = {}
        tainted = {}
        for from_id, to_id, value in currency_transfers:
            _apply_haircut_transfer(balance, tainted, from_id, to_id, float(value), seed_wallet_id)
        for wallet_id, bal in balance.items():
            if wallet_id != seed_wallet_id and bal > 0:
                result[wallet_id][currency] = tainted.get(wallet_id, 0.0) / bal
    return dict(result)


def select_impurity_flags(impurity_ratios: dict, threshold: float) -> list:
    """Returns [(wallet_id, best_ratio, best_currency), ...] for every
    wallet where ANY currency's ratio is at or above `threshold` -- "a
    flag fires if any token crosses the threshold," not an average or sum
    across currencies. best_ratio/best_currency are the highest-ratio
    qualifying currency, used as the flag's single score/token_contract
    (None for native ETH)."""
    flags = []
    for wallet_id, per_currency in impurity_ratios.items():
        qualifying = {c: r for c, r in per_currency.items() if r >= threshold}
        if qualifying:
            best_currency, best_ratio = max(qualifying.items(), key=lambda kv: kv[1])
            flags.append((wallet_id, best_ratio, best_currency))
    return flags


DEFAULT_ENGAGED_WINDOW_BLOCKS = 7200       # ~24h at ~12s/block
DEFAULT_ENGAGED_OUTFLOW_THRESHOLD = 0.50   # >= 50% of tainted inflow forwarded onward, quickly


def compute_engaged_outflow_ratios(transfers: list, seed_wallet_id: int,
                                    window_blocks: int = DEFAULT_ENGAGED_WINDOW_BLOCKS) -> dict:
    """engaged_proximity's signal: how much of the taint a wallet received,
    IN A GIVEN CURRENCY, did it turn around and forward onward (signed
    outgoing transfers, haircut-weighted by its tainted balance share in
    that currency at the moment of sending -- reusing
    _apply_haircut_transfer, the exact same proportional model
    compute_impurity_ratios uses above), within `window_blocks` of when
    taint first reached it. Tracked PER CURRENCY independently, same
    reasoning as compute_impurity_ratios -- a wallet that forwards 100% of
    the ETH it received but sits on an airdropped token (or vice versa)
    should be judged on each currency's own merits, not an average.

    transfers: [(from_wallet_id, to_wallet_id, value, currency, block_number), ...],
    block-ordered WITHIN each currency (see compute_impurity_ratios'
    docstring on why cross-currency order doesn't matter) -- note the
    extra block_number column vs compute_impurity_ratios's transfers,
    needed for the window check.

    A wallet that drains its ENTIRE balance in a currency always forwards
    ~100% of whatever taint it's currently holding IN THAT CURRENCY, by
    the arithmetic of the proportional model (taint_out = value_sent *
    tainted/balance; if value_sent == balance, taint_out == tainted
    exactly) -- this is a known, accepted property of a haircut model,
    not a bug: "sent its whole balance onward quickly" is itself the
    behavior this detector is looking for. It only under-flags a wallet
    that forwards everything MINUS a kept remainder, or that forwards
    only part of its holdings -- exactly the "busy wallet sent unrelated
    funds" case this is meant to leave unflagged.

    Returns wallet_id -> {currency: ratio}, one entry per currency the
    wallet ever received nonzero tainted inflow of, EXCLUDING the seed.
    """
    by_currency = defaultdict(list)
    for from_id, to_id, value, currency, block_number in transfers:
        by_currency[currency].append((from_id, to_id, value, block_number))

    result = defaultdict(dict)
    for currency, currency_transfers in by_currency.items():
        balance = {}
        tainted = {}
        total_inflow = {}
        first_taint_block = {}
        outflow_in_window = {}

        for from_id, to_id, value, block_number in currency_transfers:
            taint_out = _apply_haircut_transfer(balance, tainted, from_id, to_id, float(value), seed_wallet_id)

            if taint_out > 0 and from_id != seed_wallet_id:
                arrival = first_taint_block.get(from_id)
                if arrival is not None and block_number <= arrival + window_blocks:
                    outflow_in_window[from_id] = outflow_in_window.get(from_id, 0.0) + taint_out

            if taint_out > 0:
                total_inflow[to_id] = total_inflow.get(to_id, 0.0) + taint_out
                if to_id not in first_taint_block:
                    first_taint_block[to_id] = block_number

        for wallet_id, inflow in total_inflow.items():
            if wallet_id != seed_wallet_id and inflow > 0:
                result[wallet_id][currency] = outflow_in_window.get(wallet_id, 0.0) / inflow
    return dict(result)


def select_engaged_proximity_flags(bfs_distances: dict, outflow_ratios: dict, seed_wallet_id: int,
                                    bfs_threshold: float = DEFAULT_BFS_PROXIMITY_THRESHOLD,
                                    outflow_threshold: float = DEFAULT_ENGAGED_OUTFLOW_THRESHOLD) -> list:
    """Flags wallets that are BOTH within the BFS-proximity threshold of
    the seed AND forwarded a significant, prompt share of the tainted
    funds they received IN ANY SINGLE CURRENCY -- "a flag fires if any
    token crosses the threshold," not an average or sum across
    currencies. Returns [(wallet_id, bfs_score, best_ratio, best_currency), ...]."""
    flags = []
    for wallet_id, distance in bfs_distances.items():
        if wallet_id == seed_wallet_id:
            continue
        score = bfs_proximity_score(distance)
        if score < bfs_threshold:
            continue
        per_currency = outflow_ratios.get(wallet_id, {})
        qualifying = {c: r for c, r in per_currency.items() if r >= outflow_threshold}
        if qualifying:
            best_currency, best_ratio = max(qualifying.items(), key=lambda kv: kv[1])
            flags.append((wallet_id, score, best_ratio, best_currency))
    return flags


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
    """rows: [(wallet_id, detector, score, threshold), ...] (bfs_proximity,
    sybil_cluster, pagerank_threshold -- no currency dimension) or
    [(wallet_id, detector, score, threshold, token_contract), ...]
    (impurity, engaged_proximity -- token_contract is None for a
    native-ETH-triggered flag, or the ERC-20 contract_address of whichever
    token's ratio actually crossed the threshold; see select_impurity_
    flags/select_engaged_proximity_flags). Bulk via execute_values, same
    pattern Phase 2 established for vertex_metric/cluster -- see
    graph/run_analysis.py."""
    if not rows:
        if commit:
            conn.commit()
        return 0
    normalized = [(run_id, *row) if len(row) == 5 else (run_id, *row, None) for row in rows]
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO flag (run_id, wallet_id, detector, score, threshold, token_contract) VALUES %s",
            normalized,
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


def _fetch_native_and_token_transfers(cur, snapshot_id: int, with_block: bool) -> list:
    """Native ETH + ERC-20 token transfers whose parent transaction is
    part of this snapshot, tagged with `currency` (None for native ETH,
    the token's contract_address for an ERC-20 transfer) -- the shared
    fetch for both run_impurity and run_engaged_proximity. The real
    signer is always transaction.from_wallet_id, even for a token row --
    never token_transfer.from_wallet_id (attacker-spoofable via a fake
    Transfer event log; see compute_impurity_ratios' docstring).
    token_transfer.to_wallet_id is used as-is, matching how it's already
    trusted elsewhere in this codebase for routing/graph purposes.

    A transaction's parent row still lands in snapshot_edge_tx even for a
    pure token call with value_wei=0 -- export_snapshot's edge query has
    no value_wei filter, only `to_wallet_id IS NOT NULL` (satisfied: a
    contract call's native recipient is the contract's own wallet_id).

    Returns [(from_wallet_id, to_wallet_id, value, currency[, block_number]), ...]
    -- block_number included only when with_block=True (engaged_proximity
    needs it for the forwarding-window check; impurity doesn't)."""
    block_col = ", t.block_number" if with_block else ""
    cur.execute(
        f"""
        SELECT t.from_wallet_id, t.to_wallet_id, t.value_wei, NULL::char(42) {block_col}
        FROM snapshot_edge_tx st
        JOIN snapshot_edge se ON se.edge_id = st.snapshot_edge_id
        JOIN transaction t ON t.tx_hash = st.tx_hash
        WHERE se.snapshot_id = %s
        ORDER BY t.block_number, t.tx_index
        """,
        (snapshot_id,),
    )
    native = cur.fetchall()
    cur.execute(
        f"""
        SELECT t.from_wallet_id, tt.to_wallet_id, tt.token_amount, tok.contract_address {block_col}
        FROM snapshot_edge_tx st
        JOIN snapshot_edge se ON se.edge_id = st.snapshot_edge_id
        JOIN transaction t ON t.tx_hash = st.tx_hash
        JOIN token_transfer tt ON tt.tx_hash = t.tx_hash
        JOIN token tok ON tok.token_id = tt.token_id
        WHERE se.snapshot_id = %s
        ORDER BY t.block_number, tt.log_index
        """,
        (snapshot_id,),
    )
    token = cur.fetchall()
    return native + token


def run_impurity(conn, snapshot_id: int, seed_wallet_id: int, threshold: float = DEFAULT_IMPURITY_THRESHOLD) -> int:
    run_id = start_run(
        conn, snapshot_id, "impurity",
        parameters={
            "threshold": threshold, "seed_wallet_id": seed_wallet_id,
            "limitation": "balances are computed only from transfers visible in this bounded crawl, "
                           "not each wallet's true on-chain balance -- see compute_impurity_ratios docstring. "
                           "Tracks native ETH and every ERC-20 token seen in the crawl, each independently.",
        },
    )
    t0 = time.time()
    with conn.cursor() as cur:
        transfers = _fetch_native_and_token_transfers(cur, snapshot_id, with_block=False)
    ratios = compute_impurity_ratios(transfers, seed_wallet_id)
    flags = select_impurity_flags(ratios, threshold)
    rows = [(wallet_id, "impurity", ratio, threshold, currency) for wallet_id, ratio, currency in flags]
    count = persist_flags(conn, run_id, rows)
    finish_run(conn, run_id, int((time.time() - t0) * 1000), "impurity")
    return count


def run_engaged_proximity(conn, snapshot_id: int, seed_wallet_id: int,
                           bfs_threshold: float = DEFAULT_BFS_PROXIMITY_THRESHOLD,
                           outflow_threshold: float = DEFAULT_ENGAGED_OUTFLOW_THRESHOLD,
                           window_blocks: int = DEFAULT_ENGAGED_WINDOW_BLOCKS, bfs_run_id: int = None) -> int:
    bfs_run_id = bfs_run_id or resolve_latest_run_id(conn, snapshot_id, "bfs")
    run_id = start_run(
        conn, snapshot_id, "engaged_proximity",
        parameters={
            "bfs_threshold": bfs_threshold, "outflow_threshold": outflow_threshold,
            "window_blocks": window_blocks, "bfs_run_id": bfs_run_id, "seed_wallet_id": seed_wallet_id,
            "limitation": "same partial-balance limitation as impurity -- see compute_impurity_ratios docstring",
        },
    )
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT wallet_id, metric_value FROM vertex_metric WHERE run_id = %s AND metric_name LIKE 'bfs_distance_from_%%'",
            (bfs_run_id,),
        )
        bfs_distances = dict(cur.fetchall())
        transfers = _fetch_native_and_token_transfers(cur, snapshot_id, with_block=True)
    outflow_ratios = compute_engaged_outflow_ratios(transfers, seed_wallet_id, window_blocks)
    flags = select_engaged_proximity_flags(bfs_distances, outflow_ratios, seed_wallet_id, bfs_threshold, outflow_threshold)
    rows = [
        (wallet_id, "engaged_proximity", ratio, outflow_threshold, currency)
        for wallet_id, score, ratio, currency in flags
    ]
    count = persist_flags(conn, run_id, rows)
    finish_run(conn, run_id, int((time.time() - t0) * 1000), "engaged_proximity")
    return count


DETECTOR_NAMES = ("bfs_proximity", "sybil_cluster", "pagerank_threshold", "impurity", "engaged_proximity")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-id", type=int, required=True)
    parser.add_argument("--seed-wallet-id", type=int, required=True)
    parser.add_argument("--detector", choices=DETECTOR_NAMES + ("all",), default="all")
    parser.add_argument("--bfs-proximity-threshold", type=float, default=DEFAULT_BFS_PROXIMITY_THRESHOLD)
    parser.add_argument("--pagerank-percentile", type=float, default=DEFAULT_PAGERANK_PERCENTILE)
    parser.add_argument("--impurity-threshold", type=float, default=DEFAULT_IMPURITY_THRESHOLD)
    parser.add_argument("--engaged-outflow-threshold", type=float, default=DEFAULT_ENGAGED_OUTFLOW_THRESHOLD)
    parser.add_argument("--engaged-window-blocks", type=int, default=DEFAULT_ENGAGED_WINDOW_BLOCKS)
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
            elif name == "impurity":
                n = run_impurity(conn, args.snapshot_id, args.seed_wallet_id, args.impurity_threshold)
            else:
                n = run_engaged_proximity(conn, args.snapshot_id, args.seed_wallet_id,
                                           args.bfs_proximity_threshold, args.engaged_outflow_threshold,
                                           args.engaged_window_blocks)
            print(f"{name}: {n} wallets flagged")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
