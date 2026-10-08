# Stability log

Each phase ends with a stability pass: launch, click through, hit endpoints, try bad inputs; fix and log here.

## Phase 1: CI/CD (2026-10-08)

Scope: CI only, no app code changed beyond lint fixes, so the pass checks the pipeline from a clean checkout.

| Check | Result |
|---|---|
| `ruff check .` locally and in CI | clean (22 import-order fixes, 1 `raise ... from None`, 1 unused variable, `E402` ignored in `scripts/` where it follows `stdout.reconfigure`) |
| pytest offline in CI (ubuntu, no Postgres/GPU/secrets) | 144 passed, 5 skipped |
| `cargo test` in CI (Windows, empty placeholder sidecar) | 29 passed |
| Installer job from a clean runner: PyInstaller sidecar, `cargo tauri build --bundles nsis`, artifact upload | success; artifact `framecheck-installer` uploaded |
| `release.yml` | not exercised: it only fires on a `v*` tag, and a tag would publish a public Release |

Findings: none outstanding. Pass clean.

## Phase 2: FrameCheck UI (2026-10-08)

Method: headless Chrome driven over the DevTools protocol (the Chrome extension was not connected), against the mock API and the live API (Postgres in WSL2, snapshot with 1,775 flags). Views visited: Overview, Flags, Wallets, Clusters, Alerts, Graph (with and without a flag overlay). Uncaught exceptions and console errors were collected on every run.

| Check | Result |
|---|---|
| Mock: every view, flag select, "View in graph", clear overlay | no exceptions, no console errors |
| Live: same click-through, 100 flags listed, flag 1 overlay (495 edges, 81 wallets) | no exceptions, no console errors |
| API down (port closed) | Overview and Flags show an error card with Retry; header says "API unreachable"; only the browser's own connection-refused lines in the console |
| Unknown flag id: `/flags/999999/evidence-edges` -> 200 `[]`; `/flags/999999/audit` -> 404; non-numeric id -> 422; `/audit/overview?snapshot_id=99999` -> 200 empty | handled, UI shows "no evidence" |
| Live SQL of `/audit/overview`, `/audit/verdicts`, `/flags/{id}/evidence-edges` | verified against the real DB |

Findings and fixes:
1. The newest snapshot (id 3) has no flags, so the Flags view was empty. The UI now uses the newest snapshot that has flags.
2. One `/flags/{id}/audit` request per flag logged ~100 console 404s for unaudited flags. Added additive `GET /audit/verdicts` (latest audit per flag, one call) and the list uses it.
3. The evidence overlay's layout diverged (coordinates ~1e122, blank canvas) because parallel transfers between one pair multiplied the spring force. Layout now receives each wallet pair once.

Not covered: the desktop (Tauri) window was not launched in this pass; it loads the same `frontend/` files and the unchanged sidecar API. Postgres-down was not retested (unchanged from Phase 1 behaviour). Pass clean for the browser.

## Phase 3: Logic (2026-10-08)

Method: live API (`FRAMECHECK_API_MODE=live`, port 8001) against Postgres in WSL2, every endpoint
by curl; the dashboard driven over the DevTools protocol in headless Chrome against that live API
and again with the API stopped; the audit engine run for real against snapshot 2 (356,989 edges,
842,231 replayable transfers, 63,431 flags).

| Check | Result |
|---|---|
| All 20 endpoints, happy path | 200, no 500s; slowest is `/stats/overview` at 2.4 s |
| Unknown ids: `/flags/999999999/evidence`, `/flags/999999999/evidence-edges` | 200 `[]` (evidence is never computed on demand) |
| Unknown ids: `/flags/999999999/audit`, `/wallets/999999999`, `/clusters/999999999` | 404 with a specific detail, distinct from "audited and robust" |
| Non-numeric ids and bad query types (`/flags/abc/audit`, `/flags?limit=abc`) | 422, FastAPI validation |
| Unknown ids that legitimately return empty: `/runs/999999/metrics`, `/snapshots/999999/edges` | 200 `[]` |
| Postgres unreachable (API pointed at a dead port) | **was 500 "internal server error"**; now 503 "database unavailable", with the real connection string kept out of the body |
| Live dashboard: Overview, Flags (100 rows), flag detail, Graph overlay (273 nodes, signed/unsolicited legend), clear overlay, Wallets, Clusters, Alerts | no uncaught exceptions, no console errors, no NaN coordinates |
| API stopped mid-session, same click-through | error card with Retry, zero rows, no blank screen, no uncaught exceptions |

### Snapshot 2 audit (the "settle snapshot 2" step)

Loading was not needed -- snapshot 2 was already fully in the DB (a `pg_dump` was taken anyway
before any write). Detectors were re-run (inserts only), then the audit:

| Detector | Flags on snapshot 2 | Audited | Frameable | Runtime per flag |
|---|---|---|---|---|
| `bfs_proximity` | 74 | 74 (all) | 74 | 1.1-3.5 s (median 1.4 s) |
| `impurity` | 2 | 2 (all) | 2 | 26.8 s (`exact`), 75.1 s (`greedy_upper_bound`) |
| `pagerank_threshold` | 2,539 | 25 (bounded sample) | 25 | 9.7-59.3 s (mean 16.3 s) |
| `sybil_cluster` | 60,816 | 25 (bounded sample) | 2 | 31.0-34.9 s (mean 33.1 s) |

Every audited `bfs_proximity`, `impurity` and `pagerank_threshold` flag on this snapshot came back
**frameable**, which is the expected shape rather than a surprise: all three thresholds rest on
inbound transfers the wallet never signed, and on a seed-anchored subgraph an attacker's transfers
alone can reach them. `sybil_cluster` is the opposite -- 2 of 25 -- because cluster membership
depends on the wallet's own outbound edges, which an attacker cannot forge.

The two big detectors are audited as **bounded samples, not exhaustively** -- 60,816 sybil flags at
~34 s each is ~24 days of single-threaded work. `--limit` now applies `ORDER BY f.flag_id` so a
sample is reproducible; before this it took whatever order Postgres chose, which made the numbers
above unrepeatable.

### Findings and fixes

1. **The exact framing-cost search did not scale with snapshot size.** One probe replays the whole
   transfer history, and a threshold no subset reaches forces all 2^15 = 32,768 subsets. On
   snapshot 2 (842,231 rows per replay, not the 254k transactions I first assumed) impurity flag
   65206 ran for over an hour and was killed. The search is now budgeted by real work
   (`EXACT_REPLAY_ROW_BUDGET`, `EXACT_PAGERANK_EDGE_BUDGET`), spending the budget on the smallest
   subset sizes first.
   - A first attempt set the budget too low and made things *worse*: flag 65205 went from `exact`
     in 24.7 s to `greedy_upper_bound` in 44.4 s, because the budget could not even afford one
     sweep of the single-transfer subsets. `max_probes_for_budget` now has an explicit floor of
     `EXACT_SUBSET_CUTOFF` -- a one-transfer framing is the realistic dust attack and the case
     where exact is both cheaper and tighter than greedy, so it is never budgeted away.
   - A PageRank probe is also priced at its iteration count (max_iter=100), not one pass over the
     edge list, which is ~100x closer to what it actually costs.
2. **The greedy fallback was unbounded too.** It probed once per candidate, and flag 65206's
   wallet has 919 distinct unsolicited senders (3,184 evidence rows) -- about 75 minutes for that
   one flag. Above the probe budget the search now doubles-then-bisects over the cheapest-first
   prefix (~2*log2(n) probes) and prunes the most expensive members first. Flag 65206: **>1 hour
   (killed) to 75 s.** The candidate-by-candidate scan still runs whenever it fits the budget, so
   small flags behave exactly as before.
3. **Postgres being down was a 500.** Any `OperationalError`/`InterfaceError` now maps to 503
   "database unavailable" instead of being lumped in with genuine bugs, and still logs the real
   error server-side only.
4. Two self-inflicted process mistakes worth recording, because both produced misleading timings:
   a superseded audit run was left alive and competed with its replacement for CPU and Postgres,
   and a background monitor was waiting on a log line that a killed process could never write.

### Not covered

- The **desktop (Tauri) window was not launched** in this pass either; it loads the same
  `frontend/` files and the unchanged sidecar API. Still open from Phase 2.
- `pagerank_threshold` and `sybil_cluster` on snapshot 2 are sampled, not exhaustive (above).
- The GPU path is untouched here -- that is Phase 4.

