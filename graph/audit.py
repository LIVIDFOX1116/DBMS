"""
FrameCheck Phase 6: audit engine.

For each flag, answers: is it frameable (holds with every transaction
SIGNED BY the flagged wallet W removed -- W passive) or robust (disappears
without W's own activity)? For frameable flags, estimates framing cost:
starting from the graph with BOTH W's signed transactions AND W's real
unsolicited inbound transfers removed, add real unsolicited transfers
back cheapest-first (value_wei + gas) until the flag reappears, then
prune anything not needed. See CLAUDE.md's Definitions section for the
full, corrected rationale (2026-10-06).

Per-detector method:
- bfs_proximity: exact. Removing W's own signed (outgoing) edges can
  never change W's own BFS distance FROM the seed (distance into W only
  depends on edges pointing at W's neighborhood, never on W's outgoing
  edges) -- so frameable/robust is determined by a real BFS re-run (not
  assumed), and framing cost is the single cheapest real unsolicited
  sender whose OWN BFS distance already qualifies W outright -- no
  combination of transfers is ever needed, so this is exact, not
  approximated.
- impurity / engaged_proximity: exact for small candidate sets (<=
  EXACT_SUBSET_CUTOFF real unsolicited senders, and while the subset
  sizes it has to try fit the replay budget -- tries every subset),
  greedy_upper_bound (cheapest-first, then pruned) above that cutoff.
  Labelled honestly per-flag, not claimed exact when it wasn't.
- pagerank_threshold: greedy_upper_bound. PageRank is global -- there's no
  cheap closed form for "the single transfer that re-qualifies W", so
  candidates are added back cheapest-first with a real masked-PageRank
  re-run (GraphBackend.pagerank_excluding_signed_edges, GPU-accelerated
  when available) after each addition, then pruned.
- sybil_cluster: yes_no only, with a fixed Louvain seed (NetworkX's
  louvain_communities already takes seed=42 unconditionally -- see
  graph/backend.py). No framing cost is computed for this detector: Louvain
  cluster membership isn't a per-transfer-additive threshold the way the
  other four are, so "cheapest transfer to re-trigger it" isn't a
  well-posed question here.

Token-triggered flags (formerly a known gap): db/queries/evidence.sql's hop=0
term now also matches ERC-20 transfers INTO W (token_transfer.to_wallet_id
= W; transaction.to_wallet_id is the token contract there), so a flag that
only crossed the threshold in a token gets real framing-cost candidates.
The signer is still transaction.from_wallet_id. A token candidate's cost
is gas only (transaction.value_wei is 0 for it); the token amount is not
priced, since the attacker mints or already holds the dust token.

    python -m graph.audit --snapshot-id 4 --seed-wallet-id 1476 --limit 50
"""
import argparse
import itertools
import math
import time
from collections import defaultdict

import pandas as pd
import psycopg2

from graph.backend import GraphBackend
from graph.detectors import (
    DEFAULT_ENGAGED_WINDOW_BLOCKS,
    NOT_REVERTED_SQL,
    bfs_proximity_score,
    compute_engaged_outflow_ratios,
    compute_impurity_ratios,
)
from graph.evidence import collect_evidence_for_flag
from ingestion import config

EXACT_SUBSET_CUTOFF = 15  # 2**15 = 32768 subsets -- fast; above this, fall back to greedy

# One score_fn probe is not free: a haircut audit replays the snapshot's whole
# transfer history, and a pagerank audit runs PageRank over the whole edge
# list. The exact search stops at the first subset size that works, so a flag
# an attacker can produce with one transfer stays cheap -- but a threshold NO
# subset reaches forces all 2**n subsets, which on a 254k-transfer snapshot is
# hours for a single flag. These budgets cap the total probe work; the search
# spends them on the smallest subset sizes first and only drops to greedy once
# the next size is unaffordable (and says so in `method`).
EXACT_REPLAY_ROW_BUDGET = 20_000_000      # replayed transfer rows
EXACT_PAGERANK_EDGE_BUDGET = 20_000_000   # edges visited across PageRank runs
# A PageRank probe is not one pass over the edge list: it iterates to
# convergence, so one probe costs ~this many edge visits per edge. Counting
# a probe as a single pass is what made a snapshot-scale exact search look
# affordable when it isn't.
#
# This used to be justified as "max_iter=100 in both NetworkX and cuGraph",
# i.e. a cap, not a measurement -- and graph/backend.py now raises that cap
# to PAGERANK_MAX_ITER=1000 so the CPU path can actually converge. Re-derived
# as a measurement instead: a converged run needs 50 iterations on snapshots
# 1 and 2 and 75 on snapshot 3 (455k vertices), so 100 still prices a probe
# generously for the graphs this pipeline handles, and the budgets --
# therefore every audit verdict and framing cost -- are unchanged. A graph
# that needed the full 1000 would be under-priced here by 10x; backend.py
# logs loudly if one ever fails to converge.
PAGERANK_PASSES_PER_PROBE = 100


def max_probes_for_budget(cost_per_call: int, budget: int) -> int | None:
    """How many score_fn probes `budget` units of work buys, when one probe
    costs `cost_per_call` units. None means unmetered (unknown cost).

    Never returns less than EXACT_SUBSET_CUTOFF: one sweep of the size-1
    subsets is always worth affording, because a single unsolicited
    transfer is the framing an attacker actually sends, and it's the case
    where exact beats greedy on both cost and accuracy. Below that floor
    the search would skip straight to greedy and be slower AND looser."""
    if cost_per_call <= 0:
        return None
    return max(EXACT_SUBSET_CUTOFF, budget // cost_per_call)


# --- Pure verdict/cost logic (no DB) ----------------------------------------

def verdict_from_score(new_score: float, threshold: float) -> str:
    return "frameable" if new_score >= threshold else "robust"


def cheapest_qualifying_bfs_transfer(candidates: list, threshold: float):
    """candidates: [(tx_hash, value_wei, gas_cost_wei, sender_distance), ...]
    -- W's real unsolicited senders with their own BFS distance from the
    seed. A single sender qualifies W outright if bfs_proximity_score of
    (sender_distance + 1) already clears `threshold` -- no combination of
    transfers is ever needed for this detector. Returns
    (tx_hash, cost_wei) for the cheapest qualifying one, or None."""
    qualifying = [c for c in candidates if bfs_proximity_score(c[3] + 1) >= threshold]
    if not qualifying:
        return None
    best = min(qualifying, key=lambda c: c[1] + c[2])
    return best[0], best[1] + best[2]


def cheapest_subset_reaching_threshold(candidates: list, score_fn, threshold: float,
                                        exact_cutoff: int = EXACT_SUBSET_CUTOFF,
                                        max_probes: int | None = None):
    """candidates: [(tx_hash, cost_wei), ...] -- real unsolicited transfers
    into W, each with a precomputed cost (value_wei + gas). score_fn(subset
    of tx_hashes) -> the resulting score/ratio if exactly that subset of
    candidates is added back to the doubly-stripped graph.

    <= exact_cutoff candidates: tries every subset, smallest first, and
    returns the true minimum-cost one reaching `threshold` ('exact').
    Above that -- or once the next subset size would cost more than
    `max_probes` score_fn calls, which is what an unreachable threshold
    runs into -- greedy cheapest-first addition until the threshold is
    reached, then a prune pass that drops any member whose removal still
    leaves the threshold met ('greedy_upper_bound').

    Returns (tx_hashes: list, cost_wei: float, method: str) or
    (None, None, method) if no subset of ALL candidates reaches the
    threshold at all.
    """
    if not candidates:
        return None, None, "exact"

    if len(candidates) <= exact_cutoff:
        best = None
        probes = 0
        exhaustive = True
        for r in range(1, len(candidates) + 1):
            if max_probes is not None and probes + math.comb(len(candidates), r) > max_probes:
                # Can't afford this subset size, so "no subset works" can no
                # longer be concluded -- hand over to greedy.
                exhaustive = False
                break
            for combo in itertools.combinations(candidates, r):
                probes += 1
                tx_hashes = [c[0] for c in combo]
                if score_fn(tx_hashes) >= threshold:
                    cost = sum(c[1] for c in combo)
                    if best is None or cost < best[1]:
                        best = (tx_hashes, cost)
            if best is not None:
                break  # smaller r already found a solution; larger r can't be cheaper on sane cost data
        if best is not None:
            return best[0], best[1], "exact"
        if exhaustive:
            return None, None, "exact"

    ordered = sorted(candidates, key=lambda c: c[1])
    if max_probes is not None and len(ordered) > max_probes:
        chosen = _smallest_prefix_reaching_threshold(ordered, score_fn, threshold)
    else:
        chosen = None
        for i in range(len(ordered)):
            if score_fn([c[0] for c in ordered[:i + 1]]) >= threshold:
                chosen = ordered[:i + 1]
                break
    if chosen is None:
        return None, None, "greedy_upper_bound"

    # Pruning the most expensive members first is where the cost actually
    # drops, so a budget-truncated prune pass still tightens the bound.
    pruned = list(chosen)
    for candidate in sorted(chosen, key=lambda c: -c[1])[:max_probes or len(chosen)]:
        trial = [c for c in pruned if c[0] != candidate[0]]
        if trial and score_fn([c[0] for c in trial]) >= threshold:
            pruned = trial
    tx_hashes = [c[0] for c in pruned]
    return tx_hashes, sum(c[1] for c in pruned), "greedy_upper_bound"


def _smallest_prefix_reaching_threshold(ordered: list, score_fn, threshold: float):
    """The shortest cheapest-first prefix of `ordered` that reaches
    `threshold`, found by doubling then bisecting -- ~2*log2(n) probes
    instead of one per candidate. Needed because a flagged wallet can have
    thousands of real unsolicited senders (one snapshot-2 impurity flag has
    919), and probing after every single addition took over an hour.

    This assumes adding more cheap inbound transfers moves the score toward
    the threshold monotonically. Where it doesn't, the prefix found may be
    larger than the one a candidate-by-candidate scan would have found --
    which is why this path is only ever reported as an upper bound, and why
    the scan is still used whenever it fits the probe budget."""
    n = len(ordered)
    size, last_failed = 1, 0
    while True:
        if score_fn([c[0] for c in ordered[:size]]) >= threshold:
            break
        if size >= n:
            return None  # even every candidate together doesn't reach it
        last_failed = size
        size = min(size * 2, n)
    lo, hi = last_failed, size
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if score_fn([c[0] for c in ordered[:mid]]) >= threshold:
            hi = mid
        else:
            lo = mid
    return ordered[:hi]


# --- DB-I/O: shared fetchers -------------------------------------------------

# RAW SQL (not yet converted): batch-pipeline lookup on the caller's connection
def fetch_flag(conn, flag_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.flag_id, f.run_id, f.wallet_id, f.detector, f.score, f.threshold, f.token_contract,
                   ar.snapshot_id, ar.parameters
            FROM flag f JOIN analysis_run ar ON ar.run_id = f.run_id
            WHERE f.flag_id = %s
            """,
            (flag_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise SystemExit(f"no flag with flag_id={flag_id}")
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


# RAW SQL (allowed): recursive CTE (db/queries/evidence.sql)
def fetch_unsolicited_candidates(conn, flag_id: int, wallet_id: int, snapshot_id: int, seed_wallet_id: int) -> list:
    """Ensures evidence exists for this flag (collecting it lazily if not
    -- see graph/audit.py's module docstring on why bulk-precomputing
    evidence for every flag up front doesn't scale), then returns the
    real hop=0 unsolicited transfers into W with their value/gas/sender,
    which are the only rows ever eligible to be "added back" per the
    threat model (the attacker can only add transfers into the target,
    never rewrite an ancestor's own history -- see db/queries/evidence.sql)."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM flag_evidence WHERE flag_id = %s LIMIT 1", (flag_id,))
        if cur.fetchone() is None:
            collect_evidence_for_flag(conn, flag_id, snapshot_id, wallet_id, seed_wallet_id)

        cur.execute(
            """
            SELECT fe.tx_hash, t.from_wallet_id, t.value_wei, t.gas_used, t.gas_price, t.block_number
            FROM flag_evidence fe JOIN transaction t ON t.tx_hash = fe.tx_hash
            WHERE fe.flag_id = %s AND fe.hop = 0 AND fe.role = 'unsolicited'
            """,
            (flag_id,),
        )
        return cur.fetchall()


def persist_audit(conn, flag_id: int, verdict: str, framing_cost_wei=None, framing_gas=None,
                   minimal_tx_set=None, method: str = "yes_no", backend: str = None,
                   runtime_ms: int = 0, commit: bool = True) -> None:
    """Appends one flag_audit row through the ORM model. Insert-only, as
    migration 009 requires: prev_hash/row_hash are left to the
    flag_audit_chain trigger (FlagAudit marks them server-generated), and a
    corrected verdict is always a new row. Runs on the caller's connection
    so commit=False callers keep control of the transaction."""
    import json

    from sqlalchemy import insert

    from db.orm.models import FlagAudit
    from db.orm.session import execute_on_connection

    execute_on_connection(conn, insert(FlagAudit).values(
        flag_id=flag_id, verdict=verdict, framing_cost_wei=framing_cost_wei, framing_gas=framing_gas,
        # Serialised here because the statement runs on a raw psycopg2
        # connection, where SQLAlchemy's JSONB bind processing does not apply.
        minimal_tx_set=json.dumps(minimal_tx_set) if minimal_tx_set is not None else None,
        method=method, backend=backend, runtime_ms=runtime_ms,
    ))
    if commit:
        conn.commit()


# --- Per-detector audits -----------------------------------------------------

# RAW SQL (not yet converted): batch-pipeline snapshot-path lookup on the caller's connection
def audit_bfs_proximity(conn, flag_id: int, snapshot_id: int, wallet_id: int, seed_wallet_id: int,
                         threshold: float, bfs_run_id: int) -> dict:
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute("SELECT file_path FROM graph_snapshot WHERE snapshot_id = %s", (snapshot_id,))
        file_path = cur.fetchone()[0]
    edges_df = pd.read_parquet(config.require_data_path(file_path))

    backend = GraphBackend()
    masked_edges = edges_df[edges_df["src_wallet_id"] != wallet_id]
    G = backend.load_edgelist(masked_edges)
    bfs_df = backend.bfs(G, seed_wallet_id)
    row = bfs_df[bfs_df.wallet_id == wallet_id]
    new_distance = float(row.iloc[0].distance) if len(row) else float("inf")
    new_score = bfs_proximity_score(new_distance)
    verdict = verdict_from_score(new_score, threshold)

    framing_cost_wei = framing_gas = None
    minimal_tx_set = None
    if verdict == "frameable":
        # Distances come from the graph with W's signed edges removed, so a
        # sender whose own path to the seed ran through W isn't credited
        # with a distance an attacker could not reproduce.
        distances = dict(zip(bfs_df.wallet_id, bfs_df.distance))
        candidates = fetch_unsolicited_candidates(conn, flag_id, wallet_id, snapshot_id, seed_wallet_id)
        prepared = [
            (tx_hash, float(value_wei), float(gas_used or 0) * float(gas_price or 0), distances.get(from_id, float("inf")))
            for tx_hash, from_id, value_wei, gas_used, gas_price, _block in candidates
        ]
        found = cheapest_qualifying_bfs_transfer(prepared, threshold)
        if found:
            tx_hash, cost = found
            framing_cost_wei = cost
            framing_gas = 0.0
            minimal_tx_set = [tx_hash]

    return {
        "verdict": verdict, "framing_cost_wei": framing_cost_wei, "framing_gas": framing_gas,
        "minimal_tx_set": minimal_tx_set, "method": "exact", "backend": backend.backend_name,
        "runtime_ms": int((time.time() - t0) * 1000),
    }


# RAW SQL (allowed): performance-critical whole-history transfer fetch for the haircut replay (hundreds of thousands of rows)
def _fetch_native_and_token_transfers_for_audit(cur, snapshot_id: int) -> list:
    """Same shared fetch as graph/detectors.py's _fetch_native_and_token_
    transfers, duplicated here (not imported) because the audit engine
    additionally needs tx_hash on every row to strip/re-add by transaction
    -- detectors.py's version never needs that. Returns
    [(from_wallet_id, to_wallet_id, value, currency, block_number, tx_hash), ...]."""
    cur.execute(
        f"""
        SELECT t.from_wallet_id, t.to_wallet_id, t.value_wei, NULL::char(42), t.block_number, t.tx_hash
        FROM snapshot_edge_tx st
        JOIN snapshot_edge se ON se.edge_id = st.snapshot_edge_id
        JOIN transaction t ON t.tx_hash = st.tx_hash
        WHERE se.snapshot_id = %s AND {NOT_REVERTED_SQL}
        ORDER BY t.block_number, t.tx_index
        """,
        (snapshot_id,),
    )
    native = cur.fetchall()
    cur.execute(
        f"""
        SELECT t.from_wallet_id, tt.to_wallet_id, tt.token_amount, tok.contract_address, t.block_number, t.tx_hash
        FROM snapshot_edge_tx st
        JOIN snapshot_edge se ON se.edge_id = st.snapshot_edge_id
        JOIN transaction t ON t.tx_hash = st.tx_hash
        JOIN token_transfer tt ON tt.tx_hash = t.tx_hash
        JOIN token tok ON tok.token_id = tt.token_id
        WHERE se.snapshot_id = %s AND {NOT_REVERTED_SQL}
        ORDER BY t.block_number, tt.log_index
        """,
        (snapshot_id,),
    )
    token = cur.fetchall()
    return native + token


def _audit_haircut_detector(conn, flag_id: int, snapshot_id: int, wallet_id: int, seed_wallet_id: int,
                             threshold: float, currency, score_all_fn, extra_score_kwargs: dict,
                             tuple_arity: int) -> dict:
    """Shared body for impurity and engaged_proximity: both are scored by
    replaying the haircut model (compute_impurity_ratios /
    compute_engaged_outflow_ratios) over a transfer list covering every
    currency the wallet touched (native ETH and every ERC-20 token seen
    in the crawl), then reading off the ONE currency this flag actually
    fired for (`currency`, from flag.token_contract -- None for native
    ETH) -- a flag that crossed the threshold in one token must stay
    scoped to that token for its own audit, not get re-judged against
    some OTHER currency's ratio. The audit mechanics -- strip signed,
    check frameable/robust, add real unsolicited transfers back
    cheapest-first on the doubly-stripped graph -- are identical between
    the two detectors; only which score function is replayed, and that
    function's expected tuple shape (tuple_arity: 4 for
    compute_impurity_ratios -- (from,to,value,currency) -- or 5, with
    block_number, for compute_engaged_outflow_ratios), differ."""
    t0 = time.time()
    with conn.cursor() as cur:
        all_transfers = _fetch_native_and_token_transfers_for_audit(cur, snapshot_id)

    def ratio_for(ratios: dict) -> float:
        return ratios.get(wallet_id, {}).get(currency, 0.0)

    signed_removed = [row[:tuple_arity] for row in all_transfers if row[0] != wallet_id]
    ratios = score_all_fn(signed_removed, seed_wallet_id, **extra_score_kwargs)
    new_score = ratio_for(ratios)
    verdict = verdict_from_score(new_score, threshold)

    framing_cost_wei = framing_gas = None
    minimal_tx_set = None
    method = "exact"
    if verdict == "frameable":
        unsolicited_tx_hashes = {
            row[5] for row in all_transfers if row[0] != wallet_id and row[1] == wallet_id
        }
        # Rows keep their position in all_transfers (already chronological
        # within each currency), so a transfer added back replays where it
        # originally happened, not after the rest of the history.
        indexed = list(enumerate(all_transfers))
        doubly_stripped = [
            (pos, row) for pos, row in indexed
            if row[0] != wallet_id and not (row[1] == wallet_id and row[5] in unsolicited_tx_hashes)
        ]
        by_hash = defaultdict(list)
        for pos, row in indexed:
            if row[0] != wallet_id and row[1] == wallet_id:
                by_hash[row[5]].append((pos, row))

        candidates_cost = fetch_unsolicited_candidates(conn, flag_id, wallet_id, snapshot_id, seed_wallet_id)
        cost_by_hash = {
            tx_hash: float(value_wei) + float(gas_used or 0) * float(gas_price or 0)
            for tx_hash, _from_id, value_wei, gas_used, gas_price, _block in candidates_cost
        }
        candidates = [(h, cost_by_hash[h]) for h in unsolicited_tx_hashes if h in cost_by_hash]

        def score_fn(tx_hashes):
            merged = sorted(doubly_stripped + [pr for h in tx_hashes for pr in by_hash[h]], key=lambda pr: pr[0])
            r = score_all_fn([row[:tuple_arity] for _pos, row in merged], seed_wallet_id, **extra_score_kwargs)
            return ratio_for(r)

        tx_hashes, cost, method = cheapest_subset_reaching_threshold(
            candidates, score_fn, threshold,
            max_probes=max_probes_for_budget(len(all_transfers), EXACT_REPLAY_ROW_BUDGET))
        if tx_hashes:
            minimal_tx_set = tx_hashes
            framing_cost_wei = cost
            framing_gas = 0.0

    return {
        "verdict": verdict, "framing_cost_wei": framing_cost_wei, "framing_gas": framing_gas,
        "minimal_tx_set": minimal_tx_set, "method": method, "backend": "cpu-replay",
        "runtime_ms": int((time.time() - t0) * 1000),
    }


def audit_impurity(conn, flag_id: int, snapshot_id: int, wallet_id: int, seed_wallet_id: int, threshold: float,
                    currency=None) -> dict:
    return _audit_haircut_detector(conn, flag_id, snapshot_id, wallet_id, seed_wallet_id, threshold, currency,
                                    compute_impurity_ratios, {}, tuple_arity=4)


def audit_engaged_proximity(conn, flag_id: int, snapshot_id: int, wallet_id: int, seed_wallet_id: int,
                             threshold: float, window_blocks: int = DEFAULT_ENGAGED_WINDOW_BLOCKS,
                             currency=None) -> dict:
    return _audit_haircut_detector(conn, flag_id, snapshot_id, wallet_id, seed_wallet_id, threshold, currency,
                                    compute_engaged_outflow_ratios, {"window_blocks": window_blocks}, tuple_arity=5)


# RAW SQL (not yet converted): batch-pipeline snapshot-path lookup on the caller's connection
def audit_pagerank_threshold(conn, flag_id: int, snapshot_id: int, wallet_id: int, seed_wallet_id: int,
                              threshold_value: float) -> dict:
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute("SELECT file_path FROM graph_snapshot WHERE snapshot_id = %s", (snapshot_id,))
        file_path = cur.fetchone()[0]
    edges_df = pd.read_parquet(config.require_data_path(file_path))
    backend = GraphBackend()

    masked = backend.pagerank_excluding_signed_edges(edges_df, exclude_wallet_id=wallet_id)
    row = masked[masked.wallet_id == wallet_id]
    new_score = float(row.iloc[0].score) if len(row) else 0.0
    verdict = verdict_from_score(new_score, threshold_value)

    framing_cost_wei = framing_gas = minimal_tx_set = None
    method = "greedy_upper_bound"
    if verdict == "frameable":
        candidates_raw = fetch_unsolicited_candidates(conn, flag_id, wallet_id, snapshot_id, seed_wallet_id)
        # PageRank only sees the AGGREGATED edge per sender, not individual
        # transactions -- "add sender S's contribution back" (restoring
        # S's whole original edge into W) is the natural candidate unit
        # here, so real unsolicited transactions are grouped by sender.
        cost_by_sender = {}
        tx_hashes_by_sender = {}
        for tx_hash, from_id, value_wei, gas_used, gas_price, _block in candidates_raw:
            cost = float(value_wei) + float(gas_used or 0) * float(gas_price or 0)
            cost_by_sender[from_id] = cost_by_sender.get(from_id, 0.0) + cost
            tx_hashes_by_sender.setdefault(from_id, []).append(tx_hash)
        doubly_stripped = edges_df[(edges_df.src_wallet_id != wallet_id) & (edges_df.dst_wallet_id != wallet_id)]
        inbound_edges = edges_df[(edges_df.dst_wallet_id == wallet_id) & (edges_df.src_wallet_id != wallet_id)]
        # A token-only sender has no src->W edge to restore, so re-running
        # PageRank for it could never change the score.
        edge_senders = set(inbound_edges.src_wallet_id)
        candidates = [(s, c) for s, c in cost_by_sender.items() if s in edge_senders]

        def score_fn(senders):
            restore = inbound_edges[inbound_edges.src_wallet_id.isin(senders)]
            modified = pd.concat([doubly_stripped, restore], ignore_index=True)
            scored = backend.pagerank(backend.load_edgelist(modified))
            row = scored[scored.wallet_id == wallet_id]
            return float(row.iloc[0].score) if len(row) else 0.0

        senders, cost, method = cheapest_subset_reaching_threshold(
            candidates, score_fn, threshold_value,
            max_probes=max_probes_for_budget(len(edges_df) * PAGERANK_PASSES_PER_PROBE,
                                             EXACT_PAGERANK_EDGE_BUDGET))
        if senders:
            minimal_tx_set = [h for s in senders for h in tx_hashes_by_sender[s]]
            framing_cost_wei = cost
            framing_gas = 0.0

    return {
        "verdict": verdict, "framing_cost_wei": framing_cost_wei, "framing_gas": framing_gas,
        "minimal_tx_set": minimal_tx_set, "method": method, "backend": backend.backend_name,
        "runtime_ms": int((time.time() - t0) * 1000),
    }


# RAW SQL (not yet converted): batch-pipeline snapshot-path lookup on the caller's connection
def audit_sybil_cluster(conn, snapshot_id: int, wallet_id: int, louvain_run_id: int) -> dict:
    """yes_no only: re-run Louvain (fixed seed=42, see graph/backend.py)
    on the graph with W's signed edges removed; frameable if W still
    lands in a cluster classify_clusters() would mark SYBIL, robust
    otherwise. No framing cost -- cluster membership isn't a per-transfer
    threshold the way the other four detectors are."""
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute("SELECT file_path FROM graph_snapshot WHERE snapshot_id = %s", (snapshot_id,))
        file_path = cur.fetchone()[0]
    edges_df = pd.read_parquet(config.require_data_path(file_path))
    masked_edges = edges_df[edges_df["src_wallet_id"] != wallet_id]

    backend = GraphBackend()
    G = backend.load_edgelist(masked_edges)
    df, modularity = backend.louvain(G)

    from graph.run_analysis import compute_internal_edges
    internal_edges = compute_internal_edges(df, masked_edges)

    row = df[df.wallet_id == wallet_id]
    verdict = "robust"
    if len(row):
        label = row.iloc[0].community_label
        member_count = int((df.community_label == label).sum())
        edges_in = internal_edges.get(label, 0)
        if member_count >= 5 and edges_in / member_count <= 1.15:
            verdict = "frameable"

    return {
        "verdict": verdict, "framing_cost_wei": None, "framing_gas": None, "minimal_tx_set": None,
        "method": "yes_no", "backend": backend.backend_name, "runtime_ms": int((time.time() - t0) * 1000),
    }


# --- Orchestration -----------------------------------------------------------

def audit_flag(conn, flag_id: int, seed_wallet_id: int) -> dict:
    flag = fetch_flag(conn, flag_id)
    detector = flag["detector"]
    snapshot_id = flag["snapshot_id"]
    wallet_id = flag["wallet_id"]
    threshold = float(flag["threshold"]) if flag["threshold"] is not None else 0.0

    if detector == "bfs_proximity":
        bfs_run_id = flag["parameters"].get("bfs_run_id")
        result = audit_bfs_proximity(conn, flag_id, snapshot_id, wallet_id, seed_wallet_id, threshold, bfs_run_id)
    elif detector == "impurity":
        result = audit_impurity(conn, flag_id, snapshot_id, wallet_id, seed_wallet_id, threshold,
                                 currency=flag["token_contract"])
    elif detector == "engaged_proximity":
        window_blocks = flag["parameters"].get("window_blocks", DEFAULT_ENGAGED_WINDOW_BLOCKS)
        result = audit_engaged_proximity(conn, flag_id, snapshot_id, wallet_id, seed_wallet_id, threshold,
                                          window_blocks, currency=flag["token_contract"])
    elif detector == "pagerank_threshold":
        result = audit_pagerank_threshold(conn, flag_id, snapshot_id, wallet_id, seed_wallet_id, threshold)
    elif detector == "sybil_cluster":
        louvain_run_id = flag["parameters"].get("louvain_run_id")
        result = audit_sybil_cluster(conn, snapshot_id, wallet_id, louvain_run_id)
        # sybil_cluster's yes_no method never calls fetch_unsolicited_
        # candidates (no framing cost to search for), so unlike the other
        # four detectors it never triggers evidence collection as a side
        # effect -- without this, every sybil_cluster flag's dashboard
        # drill-down would show "No evidence collected for this flag yet"
        # forever, even though the detector-agnostic evidence chain is
        # just as collectible for it as for any other flag.
        collect_evidence_for_flag(conn, flag_id, snapshot_id, wallet_id, seed_wallet_id)
    else:
        raise ValueError(f"unknown detector: {detector}")

    persist_audit(conn, flag_id, **result)
    return result


# RAW SQL (not yet converted): batch-pipeline CLI target selection on the caller's connection
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-id", type=int, required=True)
    parser.add_argument("--seed-wallet-id", type=int, required=True)
    parser.add_argument("--run-id", type=int, default=None, help="limit to flags from this analysis_run")
    parser.add_argument("--detector", default=None, help="limit to this detector's flags")
    parser.add_argument("--limit", type=int, default=None, help="audit at most this many flags")
    args = parser.parse_args()

    conn = psycopg2.connect(**config.PG_DSN)
    try:
        with conn.cursor() as cur:
            query = """
                SELECT f.flag_id FROM flag f JOIN analysis_run ar ON ar.run_id = f.run_id
                WHERE ar.snapshot_id = %s
            """
            params = [args.snapshot_id]
            if args.run_id is not None:
                query += " AND f.run_id = %s"
                params.append(args.run_id)
            if args.detector is not None:
                query += " AND f.detector = %s"
                params.append(args.detector)
            # Ordered so that --limit takes a reproducible sample: a bounded
            # run is how the big detectors get audited at all, and its
            # numbers end up quoted in docs.
            query += " ORDER BY f.flag_id"
            if args.limit is not None:
                query += " LIMIT %s"
                params.append(args.limit)
            cur.execute(query, params)
            flag_ids = [row[0] for row in cur.fetchall()]

        print(f"auditing {len(flag_ids)} flag(s)...")
        for i, flag_id in enumerate(flag_ids, start=1):
            t0 = time.time()
            result = audit_flag(conn, flag_id, args.seed_wallet_id)
            print(f"[{i}/{len(flag_ids)}] flag_id={flag_id} verdict={result['verdict']} "
                  f"method={result['method']} runtime_ms={int((time.time() - t0) * 1000)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
