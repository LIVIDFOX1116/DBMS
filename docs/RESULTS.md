# FrameCheck: results

All numbers below are from the live full-extent snapshot (**snapshot_id=5**, 30,322 wallets,
47,884 edges, seed wallet 1476 = the Poly Network attacker address) unless marked synthetic.
CPU only (pandas + NetworkX) on this machine — no RAPIDS/GPU was available to exercise
`pagerank_threshold`'s audit's GPU-accelerated masked-PageRank path.

## 1. The BFS frameability proof, in plain words

`bfs_proximity` flags a wallet based on how many hops it sits from the seed — and that distance
is determined entirely by *who points at it* (inbound edges into its neighborhood), never by
*what it points at* (its own outgoing edges). Stripping every transaction a wallet signed removes
only its outgoing edges. So removing a wallet's own signed activity can never move it farther from
the seed than it already was — which means every `bfs_proximity` flag is frameable by
construction, with no exceptions possible.

This isn't just asserted — it's checked two ways in this codebase:
- A dedicated test (`tests/test_audit.py::test_removing_a_wallets_own_outgoing_edge_never_changes_its_own_bfs_distance`) runs a real `GraphBackend` BFS before and after stripping a wallet's outgoing edges and confirms the distance is identical.
- Every real audit agrees: **61/61 real `bfs_proximity` flags audited on snapshot 5 came back frameable, 0 robust.** Combined with every `bfs_proximity` audit run earlier this session, the result is unanimous — 100% frameable, every time, on real data.

Framing cost for this detector is `exact`, not approximated: the cheapest real unsolicited sender
whose own BFS distance already qualifies the wallet outright is the answer — no combination of
transfers is ever needed, since a single transfer from a close-enough sender does the job.

## 2. Framing-cost distribution per detector (real audits, snapshot 5)

| Detector | Audited | Frameable | Robust | Cost min | Cost median | Cost max |
|---|---|---|---|---|---|---|
| bfs_proximity | 61 | 61 (100%) | 0 | 0.000189 ETH | 0.010127 ETH | 13.370793 ETH |
| pagerank_threshold | 29 (1 skipped) | 29 (100%) | 0 | 0.000430 ETH | 0.048841 ETH | 148.980658 ETH |
| impurity | 2 | 2 (100%) | 0 | 13.370793 ETH* | 13.370793 ETH* | 13.370793 ETH* |
| sybil_cluster | 52 (random sample) | 5 (9.6%) | 47 (90.4%) | n/a | n/a | n/a |
| engaged_proximity | 0 real flags | — | — | — | — | — |

\* Only 1 of impurity's 2 flags has a framing cost — see the token-evidence gap in Limitations.
`sybil_cluster` never computes a framing cost by design (see below). `engaged_proximity` raised
zero flags on real data — see §3 and the synthetic result in §4.

`pagerank_threshold`'s cost range (0.0004 to 149 ETH) is the widest by far, reflecting that it's
the one detector with no cheap closed form — `greedy_upper_bound` candidates are added back
cheapest-first with a real masked-PageRank re-run after each addition, so cost depends heavily on
how PageRank-influential the cheapest available senders happen to be, not just how close they are.

## 3. The Sybil framing result

Only **9.6%** of a random sample of real `sybil_cluster` flags turned out to be frameable; **90.4%
were robust**. This is a genuinely different picture from `bfs_proximity`'s unanimous 100%: a
SYBIL-cluster classification usually depends on the flagged wallet's *own* broader signed activity
(the edges it itself created that feed Louvain's community structure), not just on unsolicited
money arriving. An attacker dusting a wallet essentially never changes its Louvain community
membership — the one real victim-group overlap case found during this session's synthetic
experiment (§4) confirms this directly: a dust victim that happened to land in a SYBIL cluster
audited **robust**, because that specific wallet had pre-existing signed activity unrelated to the
injected scenario, and removing it was what broke the classification.

`sybil_cluster`'s audit is `yes_no` only (fixed Louvain `seed=42`, re-run with the wallet's signed
edges removed, check if it's still classified SYBIL) — no framing cost is computed for this
detector at all, by design: cluster membership isn't a per-transfer-additive threshold the way the
other four detectors are, so "cheapest transfer to re-trigger it" isn't a well-posed question here.

## 4. Victim vs. launderer (synthetic experiment)

Real data has zero positive `engaged_proximity` cases to audit (see §3/§5), so a controlled
experiment is what actually demonstrates this detector works as designed: 20 synthetic dusted
*victims* (passive — never sign anything) and 20 synthetic *launderers* (receive from the seed,
forward ≥80% onward within 50 blocks to a fresh wallet), injected into an isolated `txgraph_sim`
clone and cleaned up after.

| Detector | Victims (flagged/frameable/robust) | Launderers (flagged/frameable/robust) |
|---|---|---|
| bfs_proximity | 20/20/0 | 20/20/0 |
| **engaged_proximity** | **0/0/0** | **20/0/20** |
| impurity | 0/0/0 | 20/20/0 |
| sybil_cluster | 1/0/1 | 20/0/20 |

Exactly as predicted: `bfs_proximity` flags both groups and calls both frameable (§1's proof
applies equally to either). `engaged_proximity` flags **only** launderers, and audits every one of
them **robust** — the flag depends entirely on the launderer's own signed forwarding transaction,
which disappears the instant it's stripped. `impurity` also cleanly separates the groups, for an
independent reason: a launderer that forwards 80% and keeps 20% is still 100% tainted on what it
kept (ratio = tainted/balance *after* the forward, unaffected by whether the forward happened at
all) — frameable regardless of the forwarding.

## 5. Audit throughput

A bounded, time-boxed real pass across all five detectors on snapshot 5 (`scripts/
full_audit_pass.py`), CPU only, 120-second per-flag timeout:

| Detector | Audited | Skipped | Flags/min |
|---|---|---|---|
| bfs_proximity | 61 | 0 | 137.8 |
| impurity | 2 | 0 | 5.9 |
| engaged_proximity | 0 | 0 | n/a |
| pagerank_threshold | 29 | 1 | 2.7 |
| sybil_cluster | 50 | 0 | 28.2 |

**Total: 143 flags targeted, 13.2 minutes end to end.** `bfs_proximity`'s 61 flags excluded 13
"known hub" wallets by inbound-transaction count (>500; the worst, a single wallet, has 2,005) —
see the Limitations entry on the evidence-collection performance cliff below. `pagerank_
threshold`'s one skip is consistent with its cost-search looping a masked-PageRank re-run per
candidate — the one detector audit with no cheap closed form, and correspondingly the slowest.

## 6. Limitations

- **~30k wallets, not 254k.** This session's data is the ~30,322-wallet "full-extent" crawl already
  ingested during earlier phases, not a fresh 254k-wallet live crawl (that would need roughly 35+
  hours at this pipeline's rate-limited crawl speed). Every number above is relative to this
  bounded subgraph, not the full on-chain picture.
- **Native ETH + ERC-20 token transfers only.** `impurity`/`engaged_proximity` now track taint per
  currency (native ETH and every ERC-20 contract independently), but ingestion never calls
  Etherscan's `txlistinternal` endpoint — contract-initiated internal transfers (funds moved by a
  smart contract's own internal logic, not a directly signed external transaction or a standard
  ERC-20 Transfer event) are invisible to every detector and every audit in this pipeline.
- **CPU only.** No RAPIDS/GPU was available on this machine for any of the work above.
  `pagerank_threshold`'s audit has a GPU-accelerated masked-PageRank code path
  (`GraphBackend.pagerank_excluding_signed_edges`) that has never actually been exercised on a
  RAPIDS-capable machine this session — it's implemented and has an offline GPU/CPU agreement
  test, but its real-world speedup is unverified.
- **Greedy results are upper bounds, not minimums.** `pagerank_threshold` and any `impurity`/
  `engaged_proximity` flag with more than 15 real unsolicited candidate senders use
  `greedy_upper_bound` (cheapest-first, then pruned) — reported honestly as such, never claimed to
  be the true minimum framing cost.
- **Proxy ground truth, not real confirmed framing incidents.** There is no dataset of real wallets
  known to have been genuinely framed by an outsider to validate these detectors/audits against.
  "Frameable"/"robust" is a theoretical, counterfactual audit of what the detector *would* do with
  a wallet's own activity removed — the synthetic experiments (§4, and the dust-only experiment
  from the live launch) are the closest thing to ground truth this project has, and they are
  synthetic by construction, not drawn from a real incident.
- **Evidence-collection performance cliff for high-inbound-degree wallets.** `db/queries/
  evidence.sql`'s recursive CTE spawns one backward-walk branch per *inbound transaction*, not per
  distinct sender, so a wallet with many direct senders (or many transactions from the same
  sender) pays that many redundant branches. Found live: a wallet with 2,005 inbound transactions
  made its evidence collection take 26+ seconds and climbing (killed before completion); other
  wallets with lower but still substantial in-degree ranged from sub-second up to 183.67 seconds
  for the exact same audit method. `scripts/full_audit_pass.py` works around this by excluding
  high-indegree wallets from `bfs_proximity`'s pass rather than fixing the root cause (deduplicating
  the recursive term's branches by sender) — scoped but not implemented under this session's time
  budget.
- **Token-triggered flags have no framing-cost candidates.** A flag that crossed the impurity/
  engaged_proximity threshold in an ERC-20 token still audits correctly for frameable/robust (that
  verdict comes straight from the replay), but its framing-cost search currently finds zero
  candidates to add back: `flag_evidence`'s recursive CTE matches on `transaction.to_wallet_id = W`
  (the native recipient, which is the token *contract's* wallet for a token transfer, never W
  itself — W only appears via `token_transfer.to_wallet_id`). Confirmed live on snapshot 5's one
  token-triggered impurity flag. Fixing it needs `evidence.sql` extended with a
  `token_transfer`-based branch; out of scope for the session that found it.
- **`sybil_cluster` has no framing cost.** Cluster membership isn't a per-transfer-additive
  threshold, so "cheapest transfer to re-trigger it" isn't a well-posed question for this detector;
  its audit is `yes_no` only (§3).
- **`engaged_proximity` raised zero real flags on this snapshot.** Across both the native-only and
  the ERC-20-extended replay, no wallet in this crawl, in any currency, both sat within 2 hops of
  the seed *and* forwarded ≥50% of its tainted inflow within the window. This is a fact about what
  this particular bounded crawl happens to contain, not a defect in the detector — §4's synthetic
  experiment is what demonstrates the detector's logic is correct.
