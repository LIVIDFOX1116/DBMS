# FrameCheck

FrameCheck's audit layer, built on top of its own graph-analysis detectors, for a 24-hour hackathon
(DBTHON, 2 PM 7 Oct – 2 PM 8 Oct 2026).

## The idea

Ethereum lets anyone send funds to any wallet, and the receiver cannot refuse it. That means an
attacker can make an innocent wallet look connected to a hack just by sending it dust — a tiny,
unsolicited transfer that drags the wallet within range of a proximity detector, into a tainted
cluster's inflow, or into a PageRank-ranked neighborhood, without the wallet's owner doing
anything at all.

FrameCheck's detectors already flag wallets this way (BFS proximity to a seed, Sybil clusters,
PageRank rank, taint/impurity from a known-bad source). FrameCheck audits every flag those
detectors raise and answers one question: **could an outsider have produced this flag on their
own, without the wallet owner signing a single transaction, and how cheaply?**

## Prior work

FrameCheck's impurity detector reuses the proportional "haircut" taint model from Liu et al.,
*"Evasion Under Blockchain Sanctions"* (WWW '26) — tainted-balance-weighted outflow
(`impurity_wei / balance_wei`), propagated transfer-by-transfer in block order. That score, the
dusting-attack threat model it responds to, and the general idea of taint propagation are their
prior work, not FrameCheck's. FrameCheck's own contribution is the audit on top: given a flag any
detector raised (impurity included), work out whether an attacker's unsolicited transfers alone
could have produced it, and at what cost. Nothing here claims credit for dusting attacks, taint
models, or counterfactual explanations in general — see `CLAUDE.md`'s header for the same note in
force for all code and docs in this repo.

## Definitions

These exact terms are used in code, docs, and the dashboard UI. Corrected 2026-10-06 — see the
history note below.

- **Signed transfer**: a transaction whose signer (`transaction.from_wallet_id`) is the flagged
  wallet `W`. The signer always comes from the `transaction` row, never from
  `token_transfer.from_wallet_id` — that column is populated from ERC-20 Transfer event logs,
  which a spam contract can fake. The real signer of any transfer that also produced a
  `token_transfer` row is still `transaction.from_wallet_id`, joined via `tx_hash`.
- **Unsolicited transfer**: a transfer *into* `W` that `W` did not sign.
- **Frameable flag**: the flag still holds on the graph with every transaction *signed by* `W`
  removed (`W` passive). If a flag survives with `W` contributing nothing of its own, an
  attacker's unsolicited inbound transfers alone were sufficient to produce it.
- **Robust flag**: the flag disappears once `W`'s own signed transactions are removed. The flag
  depended on `W`'s own activity, which an outsider cannot forge.
- **Framing cost**: for a frameable flag, start from the graph with *both* `W`'s signed
  transactions and `W`'s real unsolicited inbound transfers removed, then add real unsolicited
  transfers back cheapest-first (`value_wei` + gas) until the flag reappears, then prune anything
  not needed. A greedy result is an upper bound on the true minimum cost, and is always labelled
  as such — never claimed to be the minimum unless the method is `exact` (see "Per-detector audit
  method" below).
- **Threat model**: the attacker can send any amount to any wallet and pays `value + gas` per
  transfer. The attacker can only *add* transfers into the target. The attacker cannot sign for
  the target and cannot remove or reorder existing history.

### Why the definition changed (2026-10-06)

The original definition tested frameability by removing `W`'s *unsolicited inbound* transfers and
checking whether the flag disappeared. That's backwards: a real launderer's flag often rests
*mostly* on inbound transfers it never signed (that's exactly what being downstream of a hack
looks like) — so that test called almost every flag frameable, including genuine launderers,
which defeats the point of the audit. The corrected test instead removes what `W` actually
*signed* and asks whether unsolicited money alone was still enough — the thing an outsider can
actually fabricate.

## Per-detector audit method

| Detector | Method | Why |
|---|---|---|
| `bfs_proximity` | `exact` | Removing `W`'s own outgoing (signed) edges can never change `W`'s own BFS distance from the seed — that distance only depends on edges pointing *at* `W`'s neighborhood. Frameable/robust is a real BFS re-run, not an assumption; framing cost is the single cheapest real unsolicited sender whose own BFS distance already qualifies `W` outright — no combination of transfers is ever needed. |
| `impurity` / `engaged_proximity` | `exact` for ≤ `EXACT_SUBSET_CUTOFF` (15) real unsolicited senders (tries every subset — 2¹⁵ = 32,768, fast), `greedy_upper_bound` above that | Both reuse the proportional-haircut taint replay; a per-flag exhaustive search is cheap enough below the cutoff to find the true minimum, and switches to cheapest-first-then-prune above it. Labelled per-flag, never claimed exact when it wasn't. |
| `pagerank_threshold` | `greedy_upper_bound` | PageRank is global — there's no cheap closed form for "the one transfer that re-qualifies `W`". Candidates are grouped by sender (PageRank only sees the aggregated edge per sender pair, not individual transactions) and added back cheapest-first, with a real masked-PageRank re-run (`GraphBackend.pagerank_excluding_signed_edges`, GPU-accelerated when RAPIDS is available) after each addition, then pruned. |
| `sybil_cluster` | `yes_no` | Re-runs Louvain (fixed `seed=42`, same as the original detector) with `W`'s signed edges removed and checks whether `W`'s cluster is still classified `SYBIL`. No framing cost is computed — Louvain cluster membership isn't a per-transfer-additive threshold the way the other four are, so "cheapest transfer to re-trigger it" isn't well-posed here. |

## Detectors

Five detectors write `flag` rows (one per `(run_id, wallet_id, detector)`), each with its own
`analysis_run` row and configurable threshold(s) stored in `analysis_run.parameters`:

- **`bfs_proximity`**: flags `W` if its BFS-distance-derived score (same formula
  `graph/score_wallets.py` uses: 90 at the seed, −20/hop, floored at 0) is ≥ 50 — within 2 hops
  downstream of the seed. Default threshold: `DEFAULT_BFS_PROXIMITY_THRESHOLD = 50.0`.
- **`sybil_cluster`**: flags every member of a cluster `classify_clusters` already marked
  `SYBIL`. Reads that existing output; does not re-detect.
- **`pagerank_threshold`**: flags the top 1% of wallets by PageRank, excluding the seed.
  Default: `DEFAULT_PAGERANK_PERCENTILE = 0.01`.
- **`impurity`**: a simplified Liu et al. haircut over native ETH transfers in block order within
  the subgraph — the seed is a pure taint source (100% of its outflow is tainted regardless of
  its own balance); every other wallet's taint-out is `value × (tainted_balance / balance)` at
  the time it sends, floored at 0. Flags at ≥ 10% of balance
  (`DEFAULT_IMPURITY_THRESHOLD = 0.10`). **Known limitation**: this only sees transfers visible in
  the bounded crawl, not a wallet's true on-chain balance — see "Known limitations" below.
- **`engaged_proximity`**: flags `W` only if it is within 2 hops downstream of the seed (same
  `bfs_proximity_score` test as above) *and* its haircut-tainted outflow — transfers `W` signed
  after tainted funds arrived, within `DEFAULT_ENGAGED_WINDOW_BLOCKS` (7200 blocks, ~24h) of first
  taint arrival — is ≥ `DEFAULT_ENGAGED_OUTFLOW_THRESHOLD` (50%) of `W`'s total tainted inflow. It
  reuses the same haircut replay as `impurity` (`_apply_haircut_transfer`), but asks a different
  question: not "how tainted is this wallet" but "did this wallet actually *act* on tainted funds
  quickly, rather than just receive and sit on them." This is what separates a launderer
  (forwards tainted funds fast — `engaged_proximity` stays `robust` under audit, since the
  forwarding was `W`'s own signed activity) from a dusted wallet (receives dust, does nothing with
  it — flagged by `bfs_proximity` alone, not by `engaged_proximity`) and from a busy wallet that
  happened to receive dust and separately sent unrelated funds (not flagged by
  `engaged_proximity`, since its outflow isn't *of* the tainted funds).

```bash
python -m graph.detectors --snapshot-id 4 --seed-wallet-id 1476 --detector all
```

## Evidence

For a flagged wallet `W`, `graph/evidence.py` runs the recursive CTE in
`db/queries/evidence.sql` and persists one `flag_evidence` row per discovered transaction:

- **hop=0** captures both of `W`'s directions: role `signed` if `W` is the sender, `unsolicited`
  if it's the receiver and didn't sign. These are the only rows the audit engine can ever add
  back or strip — an attacker can only add transfers *into* the target, never rewrite an
  ancestor's history.
- **hop≥1** recursively follows each unsolicited predecessor's own earliest inbound transaction
  back toward the seed (cycle-guarded), for narrative/provenance context only — not used by the
  audit algorithm itself.

Evidence is collected lazily (on first need, triggered from `graph/audit.py`), not bulk-computed
for every flag up front — a real snapshot can produce thousands of flags, most of which nobody
will ever drill into.

```bash
python -m graph.evidence --snapshot-id 4 --seed-wallet-id 1476 --run-id 20
```

## Framing simulator

`scripts/simulate_framing.py` injects a synthetic dusting scenario — `DEFAULT_NUM_TARGETS` (100)
wallets with no BFS path from the seed, each dusted with `DEFAULT_DUST_ETH` (0.001) ETH plus a
documented gas constant (21000 × 20 gwei) — to prove the whole pipeline reacts the way the theory
predicts. It **never touches the main database**: everything runs against a separate database,
`framecheck_sim`, created as a native Postgres template-clone of the main DB
(`CREATE DATABASE framecheck_sim TEMPLATE framecheck`). Synthetic rows are tagged
`transaction.is_simulated = true` and use a deterministic `0xdeadbeef...`-prefixed `tx_hash` and a
`SYNTHETIC_BLOCK_BASE`-offset `block_number`, both keyed by `scenario_id`, so `--cleanup
<scenario_id>` can find and remove exactly the right rows even if the `framing_scenario` row
itself is already gone.

```bash
python scripts/simulate_framing.py --snapshot-id 4 --seed-wallet-id 1476
python scripts/simulate_framing.py --cleanup <scenario_id>
python scripts/simulate_framing.py --drop-db
```

`scripts/run_experiment.py` wraps this into a full experiment: inject → re-export a snapshot →
re-run analyze/detect/evidence/audit, all against `framecheck_sim` (via a `PGDATABASE` override on
every subprocess call, never the main database) → print a per-detector summary (targets flagged,
share frameable, framing-cost range). Evidence and audit are scoped to only the injected target
wallets, not the whole snapshot's flags — a real snapshot can carry thousands of unrelated flags
that this experiment has no reason to touch.

```bash
python scripts/run_experiment.py --snapshot-id 4 --seed-wallet-id 1476
python scripts/run_experiment.py --cleanup <scenario_id>
python scripts/run_experiment.py --drop-db
```

A real 5-target run against the live data: all 5 injected targets were flagged by
`bfs_proximity`, 100% frameable (expected — they're passive wallets with nothing of their own to
remove), framing cost exactly `1,420,000,000,000,000` wei each — matching the documented
`0.001 ETH + 21000 × 20 gwei` constant to the wei.

## Audit engine

```bash
python -m graph.audit --snapshot-id 4 --seed-wallet-id 1476 --limit 50
```

Dispatches each flag to its detector's audit method (table above), persists one `flag_audit` row
per flag (`verdict`, `framing_cost_wei`, `framing_gas`, `minimal_tx_set`, `method`, `backend`,
`runtime_ms`). `--limit N` audits only the first `N` flags per detector — useful on a full run
where `pagerank_threshold`'s cost search (a masked-PageRank re-run per candidate) is the slow
path; see "Benchmark" below for real numbers.

## API

Four endpoints, additive only (never renaming or changing an existing one — see
`docs/API_CONTRACT.md`'s "additive only" rule):

- `GET /flags` — filterable by `run_id`, `detector`, `wallet_id`.
- `GET /flags/{id}/evidence` — never computes on demand; returns `[]` if evidence hasn't been
  collected for that flag yet.
- `GET /flags/{id}/audit` — `404` ("flag not audited yet") distinct from a `200` with
  `verdict: "robust"` — those are different facts and must not be conflated.
- `GET /audit/summary` — per-detector aggregates (flag counts, frameable share, median framing
  cost) computed directly in PostgreSQL via `FILTER` and
  `PERCENTILE_CONT(0.5) WITHIN GROUP`.

Full request/response shapes, with real captured examples, are in `docs/API_CONTRACT.md`.

## Dashboard

A new **Audit** view in `frontend/dashboard.html`: a per-detector summary table
(`/audit/summary`), a detector-filterable flags table (`/flags`, each row's verdict/cost filled
in via `/flags/{id}/audit`), and an evidence drill-down modal (`/flags/{id}/evidence`) — same
visual language and lazy-load-on-first-visit pattern as the existing Graph view.

## Benchmark

`scripts/benchmark_audit.py` times `graph.audit.audit_flag` against a real sample of flags per
detector, reporting median seconds/flag and flags/minute, CPU vs GPU (only `pagerank_threshold`
has a GPU path to compare — the other four detectors are CPU replay/NetworkX-only by design). It
writes real `flag_audit` rows and deletes exactly the ones it created afterward (by
`flag_audit_id` range, since `audit_flag()` commits internally and can't just be handed a rolled
back transaction).

```bash
python scripts/benchmark_audit.py --snapshot-id 4 --seed-wallet-id 1476 --limit 5
```

See `docs/benchmark_results.md`'s "Audit benchmark" section for real numbers on this machine.

## Known limitations

- **Partial-balance taint model**: `impurity`/`engaged_proximity`'s haircut only tracks transfers
  visible in this pipeline's bounded crawl, not a wallet's true on-chain balance — a wallet that
  drains its entire *visible* balance always emits ~100% of its *tracked* taint by the arithmetic,
  which is a property of the model (not a bug), but means the ratio can overstate taint for a
  wallet with off-graph funds.
- **No internal transactions**: ingestion never calls Etherscan's `txlistinternal` endpoint, so
  contract-initiated transfers (funds moved by a smart contract's internal logic, not a directly
  signed external transaction) are invisible to this pipeline and therefore to every FrameCheck
  audit built on top of it.
- **Greedy framing cost is an upper bound, not the minimum**, for `pagerank_threshold` and any
  `impurity`/`engaged_proximity` flag with more than `EXACT_SUBSET_CUTOFF` (15) real unsolicited
  candidate senders — always reported with `method: "greedy_upper_bound"`, never as if it were
  exact.
- **`sybil_cluster` has no framing cost**: cluster membership isn't a per-transfer-additive
  threshold, so "cheapest transfer to re-trigger it" isn't a well-posed question for this one
  detector; its audit is `yes_no` only.
- **Evidence collection has a performance cliff for high-inbound-degree wallets.** `db/queries/
  evidence.sql`'s recursive CTE spawns one backward-walk branch per hop=0 row, and the hop=0 base
  case produces one row *per direct inbound transaction*, not per distinct sender — a wallet with
  many direct senders (or many transactions from the same sender) produces that many redundant
  branches, each paying its own indexed lookup every hop. Found live on the full-extent snapshot:
  auditing flags on a wallet with 2,005 inbound transactions took 26+ seconds and climbing per
  flag (killed before completion); auditing 15 `bfs_proximity` flags on *other* wallets (excluding
  that one) took between 0.17s and 183.67s each, the slow outliers all correlating with higher
  in-degree, not with audit complexity — every one of those 15 was `method: exact`, the cheapest
  possible audit path, confirming the variance is entirely evidence-collection cost, not the audit
  algorithm. `sybil_cluster`'s audit never calls into evidence collection (no framing cost to
  compute), and was unaffected: 30/30 sampled flags audited in a tight 2.56-3.24s band. A fix
  (deduplicating the recursive term's branches by `from_wallet_id` before recursing, since the
  final `SELECT DISTINCT` already collapses duplicate output rows) was scoped but not implemented
  under this session's time budget — noted here rather than silently worked around.
