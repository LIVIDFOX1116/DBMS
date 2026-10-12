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

## Phase 4: CPU vs GPU benchmark (2026-10-08)

Method: RAPIDS 26.08 installed in WSL2 (`docs/GPU_SETUP.md`), benchmarks run there so both
backends share one process; live API on port 8001 against Postgres; the new Benchmarks tab driven
over the DevTools protocol in headless Chrome, with the API up and with it pointed at a dead port.

| Check | Result |
|---|---|
| `scripts/verify_gpu_env.py` under WSL | PHASE 0 GPU CHECK: COMPLETE — cudf 26.08.01, cugraph 26.08.00, GTX 1650 visible (4.3 GB total, 3.5 GB free) |
| Benchmarks on snapshots 1, 2, 3, CPU and GPU, 3 repeats | all completed; see `docs/benchmark_results.md` |
| Audit-pass benchmark, CPU pass and GPU pass | 13.49 s/flag -> 0.72 s/flag; each pass deleted exactly the `flag_audit` rows it created |
| `flag_audit` row counts after both benchmark passes | unchanged from Phase 3 (74 / 2 / 25 / 25) — the benchmark left the DB as it found it |
| `GET /benchmarks` live | 200, 3 runs, 4 tasks each, GPU column present |
| Benchmarks tab, live API | 3 panels, 12 rows, correct speedups, no console errors, no horizontal page scroll |
| Benchmarks tab, API unreachable | error card with the API base and a Retry button; not blank, no uncaught exceptions |
| `GET /benchmarks` with the results file missing or corrupt | 200 `[]` (unit tests), and the tab shows an empty state with the command to run |
| Full suite | 173 passed, 5 skipped; ruff clean |

### Findings and fixes

1. **The exact framing-cost search's sibling problem, found in the UI**: sub-millisecond timings
   rendered as `0 ms`, which reads as "free" rather than "fast". CPU BFS on snapshot 1 really is
   0.4 ms. Now shown as `0.40 ms`.
2. **`scripts/benchmark_audit.py` hardcoded every result row as `CPU`**, so a GPU run would have
   produced a table labelled CPU containing GPU numbers. Rows are now labelled by the backend
   actually in effect, which is what makes the two-pass comparison readable.
3. **CPU and GPU PageRank disagree materially** on snapshot 3 (disjoint top-5, 84/100 top-100
   overlap, GPU top scores ~3.5x larger), while Spearman rank correlation stays at 0.9995. Ruled
   out: dropped edge weights (an unweighted CPU run matches the GPU *less*) and float32 precision
   (cuGraph keeps `edge_weight` float64). Cause: `edge_weight` is wei transferred, 71.3% of edges
   carry weight 0 on that snapshot, and the two libraries resolve zero-out-weight (dangling)
   vertices differently. This is **not fixed** — `pagerank_threshold` compares against an absolute
   threshold, so every candidate fix changes which wallets past runs would have flagged. Written
   up in `docs/benchmark_results.md`; needs a deliberate decision.

### Process notes

The RAPIDS install was killed twice by the host's low-memory watchdog (16 GB machine, WSL holding
~4.3 GB, ~2.4 GB free at the worst point). It completed after being restarted staged and
`--no-cache-dir`, run detached inside WSL rather than as a watched background task. Worth knowing
before anyone repeats the setup on this machine.

### Not covered

- **The desktop (Tauri) window has now not been launched for three phases running.** The newest
  local build (`src-tauri/target/debug/app.exe`) predates the Phase 2 rebrand and every frontend
  change since, so launching it would have exercised stale code rather than the current app; a
  rebuild was not attempted while the machine was already memory-constrained. Compilation and
  installer packaging are covered by CI (Phase 1), but **no one has clicked through the native
  window since the UI was rebuilt**. This should stop being deferred.
- `pagerank_threshold` and `sybil_cluster` on snapshot 2 remain sampled, not exhaustive (Phase 3).

## Phase 5: Google sign-in (2026-10-08)

Method: `pg_dump` (265 MB) before migration 006, then the API run both ways --
`FRAMECHECK_AUTH_MODE=required` and `disabled` -- with every route curled, and the dashboard
driven over the DevTools protocol in headless Chrome against each. Nothing in this pass reaches
Google: the ID-token verifier is substituted in tests, and no OAuth client exists yet.

| Check | Result |
|---|---|
| Migration 006 applied | `app_user` created with the unique `google_sub` constraint and the email index |
| Protected route, no session | 401 `{"detail": "sign-in required"}` |
| `/health`, `/benchmarks` | 200 without a session, as intended |
| Valid session cookie / bearer token | 200 on protected routes (both client shapes) |
| Tampered, foreign-secret, and expired sessions | 401, and the body never says which |
| `FRAMECHECK_AUTH_MODE` typos (`disable`, `false`, `0`, empty) | still required -- only the exact word `disabled` opens the gate |
| `/auth/login` with no `GOOGLE_CLIENT_ID` | 503, and the UI shows "Google sign-in is not configured on this server" rather than failing silently |
| Dashboard with auth required | sign-in card over the app, no data behind it, no uncaught exceptions |
| Dashboard with auth disabled | full click-through clean -- 100 flags, graph overlay (273 nodes, no NaN), Benchmarks tab (3 panels, 12 rows), no console errors |
| Python suite / Rust suite | 201 passed, 5 skipped; 37 Rust tests (8 new in `oauth.rs`); ruff clean |

### Findings and fixes

1. **CORS could not stay a wildcard.** A session cookie makes requests credentialed, and the CORS
   spec forbids `Access-Control-Allow-Origin: *` with credentials -- the browser drops the
   response entirely. This broke *every* cross-origin call from the dashboard, not just sign-in.
   The origin is now echoed for localhost and the Tauri webview only, and POST was added because
   signing in and out are mutating (the old config allowed GET alone, so `/auth/callback` would
   have been blocked too).
2. **A dead database looked like a CORS bug.** The generic exception handler runs *outside* the
   CORS middleware, so its 503 carried no `Access-Control-Allow-Origin` and the browser reported a
   CORS failure. I spent real time hunting a CORS problem that was Postgres having gone away.
   Database errors are now mapped to 503 inside the middleware stack, so the response carries the
   headers and says what actually happened. Covered by a test.
3. **The session TTL was frozen at import** as a default argument, so changing it did nothing.
   Read at call time now -- found because the expiry test could not make a session expire.

### Not covered

- **No real Google sign-in has ever been performed.** Creating the OAuth client needs the
  account owner's Google Cloud Console; until `GOOGLE_CLIENT_ID` exists, the verified behaviour is
  everything up to the redirect and everything after the ID token, with the token verification
  itself stubbed. The first real sign-in may still surface a redirect-URI or consent-screen
  mismatch that no amount of offline testing can predict.
- **The desktop loopback flow is unit-tested, not exercised.** `src-tauri/src/oauth.rs` has tests
  for redirect parsing, URL escaping and port binding, but no one has run `google_signin` from a
  real window -- which is the same desktop gap now outstanding since Phase 2.
- Postgres reachability from Windows to WSL dropped repeatedly during this pass; the API was moved
  inside WSL (local socket) to finish it. The flakiness is environmental, not application
  behaviour, but it is why the API ran on port 8003 for the later checks.

## Phase 6: Make it usable (2026-10-08)

Method: API run inside WSL (local Postgres socket) on port 8003, the dashboard driven over the
DevTools protocol in headless Chrome, and a **real audit job started from the UI** rather than a
mocked one -- 2 impurity flags on snapshot 2, 92 s, 2 frameable.

| Check | Result |
|---|---|
| Start a job from the UI, watch progress, see the result | queued -> running -> done, progress bar advanced, "2 frameable, 0 robust" |
| Job on a snapshot with no matching flags | done with "no impurity flags on snapshot 99999" instead of a silent "finished" |
| Second job while one runs | 409 "a audit job is already running" -- the UI shows the running job rather than starting a second |
| Cancel | stops at the next flag, reports what it actually completed; committed rows stay |
| Bad bodies: unknown `job_type`, missing fields, non-integer ids | 422 with the offending field named |
| Unknown `job_id` / cancel unknown job | 404, and the detail says jobs are forgotten on restart |
| Jobs in mock mode | 409 "analysis jobs need live mode" |
| CSV export, live | 200 `text/csv`, `Content-Disposition: attachment`, audit row plus evidence rows, values straight from Postgres |
| CSV for unknown / non-numeric flag | 404 / 422 |
| Flag search and verdict filter | 100 flags -> 23 with verdict `robust`; search narrows and the count label tracks it |
| Search with no matches | says "No matches among the 100 flags loaded. Only the first 100 are fetched…" rather than implying the flag does not exist |
| Filters after reload | detector tab, search text and verdict all restored |
| Full suite | 216 passed, 5 skipped; ruff clean |

### Findings and fixes

1. **The Cancel button stayed on screen after a job finished.** `[hidden]` is only a
   UA-stylesheet `display:none`, so `.btn { display:inline-flex }` silently beat it. Fixed with an
   explicit `[hidden] { display:none !important }`. Worth remembering: every `hidden` toggle in
   this UI was relying on that.
2. **A job on an empty snapshot just said "finished".** Now it names the combination that came up
   empty, because "done, 0 audited" with no explanation reads as a failure.
3. **Search could mislead.** It filters the flags already loaded, and the list is capped at 100 of
   a snapshot's 63,000. The empty state now says so.

### Two test-harness mistakes (not application bugs)

Both looked like app failures at first and are recorded so the next person does not chase them:

- Repeated `const s = …` across separate CDP `Runtime.evaluate` calls throws a redeclaration
  error, because they share one top-level scope. Two "failures" -- a job running against the wrong
  snapshot, and export buttons appearing to be missing -- were just statements that never ran.
- An earlier browser job appeared to return nothing; it had correctly run against snapshot 3,
  which genuinely has no impurity flags.

### Not covered

- **The desktop (Tauri) window still has not been launched** -- now five phases. Everything added
  here (Run analysis, search, CSV, the print view) is browser-verified only. The print-to-PDF path
  in particular depends on WebView2's print support, which is exactly the kind of thing that
  differs between a browser and an embedded webview.
- PDF export is the browser's print dialog against a print stylesheet, not a server-rendered
  file. That avoids adding a PDF renderer to the packaged sidecar; if a true downloadable PDF is
  wanted, that is a deliberate dependency to add.
- Jobs are in-memory and single-slot by design; see `api/jobs.py` for why, and what it costs.

## Phase 7: Desktop app verification (2026-10-09)

The desktop window had not been launched since the Phase 2 rebrand -- five phases of browser-only
verification. This pass rebuilt the PyInstaller sidecar, the Tauri shell and the NSIS installer,
then drove the **real** WebView2 window over the DevTools protocol
(`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222`), with native screenshots for
anything outside the webview (print dialog, error message boxes).

**Six bugs, every one of them invisible in the browser.**

| Check | Result |
|---|---|
| Build: sidecar, shell, NSIS installer | `FrameCheck_0.1.0_x64-setup.exe` produced |
| All eight views in the real window | all render; 100 flags; search narrows to 26 of 100; flag detail opens; no console errors, no uncaught exceptions |
| CSV export in WebView2 | download completes (110,905 bytes, correct header row) |
| Print to PDF in WebView2 | dialog opens with "Microsoft Print to PDF"; after the fix below, the preview shows the real report |
| Run analysis: start, progress, cancel | queued -> running -> cancelled ("cancelled - 1/8"), progress bar advances, Cancel hides when finished |
| Second job while one runs | 409 "an audit job is already running" |
| Sidecar killed under a running window | friendly error card, health pill "API unreachable", no blank screen, no uncaught exceptions, window alive |
| Sidecar missing at startup | native "FrameCheck failed to start" dialog plus `%TEMP%\framecheck-desktop-startup-error.log` naming the fix -- not a silent no-window failure |
| Postgres unreachable | 503s surface as error cards; the app keeps working |

### Findings and fixes

1. **Every audit failed in the packaged app.** `graph_snapshot.file_path` is stored relative, and
   "relative to what" was the process working directory -- the repo root from a terminal, the
   install directory for the sidecar. `FileNotFoundError` on a path that looked right. Resolved
   explicitly now against `FRAMECHECK_DATA_DIR` or the repo, with an error that says what to set.
2. **`docs/perf/benchmarks.json` was never bundled**, so the packaged Benchmarks tab was
   permanently empty.
3. **`db/queries/evidence.sql` was never bundled**, so every audit failed again once (1) was fixed.
   Same class as (2) and as `mock_data.json` before them: anything read off disk at runtime has to
   be declared to PyInstaller. A test now pins all three.
4. **Print produced a genuinely blank sheet** from any view except Flags: the print stylesheet
   showed `#view-flags`, which is `display:none` unless it is the active view. The PDF button now
   tags the body and the print rules key off that tag, so the report is the flag you have open
   wherever you are.
   - The first "fix verified" was wrong: I checked computed `display` on `#fcDetail`, which still
     reports `block` inside a `display:none` ancestor, and `innerText`, which falls back to
     `textContent` when hidden. Re-verified by geometry (`getBoundingClientRect`,
     `getClientRects()`): 0 px² before, 501,077 px² after -- and then in the real print preview.
5. **"Sign in with Google" froze the entire window.** A plain `#[tauri::command] fn` runs on the
   main thread, and this one waits on a loopback socket for up to 300 s, so the window went
   `Responding: False` for minutes. Now `async` with the blocking half on a blocking thread;
   verified responsive and navigable while the browser tab is open.
6. **The dead-backend message told desktop users to run `uvicorn --port 8000`.** The app spawns
   its own backend on an OS-assigned port, so that is advice they cannot act on about a port that
   does not exist. It now says to reopen the app and where the log is.

### What the loopback sign-in actually did

Pressing sign-in opened the user's real browser and bound a loopback listener (observed on port
49383), which is the flow working as designed. The exchange itself was never completed:

- **No real Google sign-in has been performed.** It needs an OAuth client that only the account
  owner can create. Verified: the gate renders in the shell, `/auth/login` returns 503 with
  "Google sign-in is not configured on this server" shown in the card, the Rust command is
  registered and reachable from the webview, the browser opens, and the listener binds. Not
  verified: consent, redirect, code exchange, session issue.
- A user who abandons the consent screen leaves the command waiting until its 300 s timeout, with
  no cancel button. The window stays usable, so this is untidy rather than broken.

### Not covered

- **"No network" was not tested by disabling the machine's network**, which would have disrupted
  the user's own session. The app's own function is entirely local; the outbound paths (Etherscan
  key test, Google token exchange) have explicit timeout/connect handling, and the Google path was
  exercised against an unreachable endpoint.
- **API port busy** is largely designed out: the shell asks the OS for a free port rather than
  using a fixed one, so there is no fixed port to collide with. The small race the code comments
  acknowledge was not forced.

### Environment note

The WSL distro auto-shuts-down when idle, taking Postgres' reachability from Windows with it
(the Postgres pid changes across the gap). That -- not application code -- is behind the
intermittent 503s seen throughout this pass, and is why a WSL session has to be held open while
testing against the live database.

## Phase 8: CI/CD verified on GitHub, not just locally (2026-10-09)

Phase 1 wrote the pipeline and checked it from a clean checkout; this pass confirms the jobs
actually go green **on GitHub's runners** for the work of Phases 2-7, and that the release path
really produces a downloadable installer.

| Check | Result |
|---|---|
| Phase 7 commit, all four jobs | pass -- [run 37900746949](https://github.com/LIVIDFOX1116/DBMS/actions/runs/37900746949): lint 10s, test 34s, rust 2m41s, installer 5m32s |
| Python suite on the Linux runner | **224 passed, 5 skipped** -- the same count as locally, so no test is silently skipped in CI |
| Rust suite on the Windows runner | **37 passed**, including the 8 new `oauth.rs` tests |
| Anything failing only in CI | **none.** The suspected candidates (new `google-auth`/`itsdangerous` deps, `oauth.rs`, and the `config.REPO_ROOT` path tests, which resolve differently on Linux) all passed unchanged |
| After pinning the runner | pass -- [run 37949207474](https://github.com/LIVIDFOX1116/DBMS/actions/runs/37949207474) |
| Release path, tag `v0.0.0-ci-test` | pass -- [run 37950731800](https://github.com/LIVIDFOX1116/DBMS/actions/runs/37950731800): installer 39m36s, release 16s |
| Release artifact | `FrameCheck_0.1.0_x64-setup.exe`, 110,634,930 bytes, attached to a real (non-draft) Release |
| Cleanup | release and tag deleted from the remote and locally; only `fc-p1`..`fc-p7` remain |

### Changes

1. **Pinned `ubuntu-latest` to `ubuntu-24.04`.** GitHub's own annotation says that label begins
   migrating to Ubuntu 26 on **2026-10-19** -- ten days after this pass. An unpinned runner image
   can break a build with no commit from us, and the first symptom would be a red build on
   unrelated work. Pinned so the image moves when we decide it does.
2. **CI and Release badges** in the README, pointing at the real workflows.

### A claim of mine that was wrong

The same commit bumped `actions/upload-artifact` and `download-artifact` from v4 to v5 and its
message says this "clears the Node 20 deprecation". **It does not.** The annotation fires
identically on v5:

> Node.js 20 is deprecated. The following actions target Node.js 20 but are being forced to run on
> Node.js 24: actions/upload-artifact@v5

`softprops/action-gh-release@v2` triggers it too. The bump is harmless and v5 is the newer line, so
it stays, but the warning is **not** fixed and the commit message overstates it. Recorded here
rather than by rewriting pushed history. The warning is cosmetic: the actions run on Node 24
regardless.

### Worth knowing

The Release workflow's installer job took **39m36s** against CI's 5m32s for the same build. The
difference is cache: `Swatinem/rust-cache` is keyed per workflow, so a tag build starts cold and
recompiles `tauri-cli` and the whole Tauri tree from scratch. Not a fault -- but a release is not a
five-minute operation, which matters when cutting one under time pressure.

## Phase 9: Multiview graph (2026-10-11)

New feature, so the pass is "does it work and is it honest", not only "does it crash". Verified in
headless Chrome against the live API **and** in the real packaged desktop app -- which is where
three of the five bugs below only appeared.

| Check | Result |
|---|---|
| `GET /clusters/top-flagged?limit=8` live | 8 rows in 1.1 s |
| `GET /clusters/{id}/subgraph` live | 0.4 s (1,872-member cluster), 1.3 s (26,594-member cluster) |
| `limit` bounds | `422` outside 1-8 (top-flagged) and 1-2000 (subgraph) |
| Route order | `/clusters/top-flagged` resolves, not shadowed by `/clusters/{cluster_id}` -- pinned by a test |
| Browser: 8 panels | 8 canvases, 8 live WebGL2 contexts, 150 nodes each, all settled, no console errors |
| Desktop: 8 panels via "Top 8 flagged" | same, in a second WebviewWindow, no errors |
| Per-panel zoom / pan | independent: panning panel 0 left panel 1's camera untouched |
| Hover tooltip | shows wallet, verdict, flag count, PageRank |
| Expand / collapse | one panel fills the window and refetches at 600 nodes; collapse restores all 8 |
| 9th cluster selected | checkbox disables itself with "8 selected -- the maximum"; a 9-id URL shows the first 8 and says so |
| Settled panels | render loop stops (`_raf` null on all 8) and restarts on input |
| Python / Rust suites | 240 passed, 5 skipped; 42 Rust tests; ruff clean |

### Findings and fixes

1. **Top-N-by-PageRank truncation produced a dust cloud, not a cluster.** On cluster 351299
   (26,594 members) the top 300 by PageRank came back as **76 disconnected fragments with 22% of
   nodes having no edge to anything else shown** -- the panel would have been visually
   meaningless. Truncation now expands outward from the highest-ranked member along real edges:
   the same 300-node budget returns **one connected component, zero isolated nodes**. Fetching
   every internal edge to do this costs 0.05 s even on that cluster.
2. **The force layout exploded at 600 nodes.** Coordinates reached **1e94** and the camera's fit
   scale collapsed to 0, so an expanded panel went blank. Two nodes landing nearly on top of each
   other produced `52000/0.01` of repulsion in one step with nothing damping it. A
   minimum-distance floor plus a per-step displacement cap bound it; the same panel now settles at
   3.8e3. **At 150 nodes the old code looked perfectly fine** -- this only existed on the expand
   path, which is why it needed exercising rather than reasoning about.
3. **`open_multiview` deadlocked as a sync command.** A sync `#[tauri::command]` runs on the main
   thread, and `WebviewWindowBuilder::build()` needs that thread's event loop to pump to finish.
   The symptom was not an error: a window appeared on `about:blank` and the invoke never resolved.
   Now `async`.
4. **`WebviewUrl::App` is a `PathBuf`**, so `multiview.html?clusters=1,2` did not survive into the
   window. The selection is passed as typed parameters and injected before the page runs. That
   also removed the need to accept a caller-supplied URL at all -- the earlier version did, and a
   command that opens an arbitrary URL in a window with Tauri IPC attached would hand a remote
   page the app's whole command surface.
5. **`GraphRenderer.destroy()` never released its WebGL context.** Dropping the canvas leaves the
   context alive until GC, and browsers cap live contexts (~16) then silently kill the *oldest* to
   make room. Multiview opens eight at a time, so repeated opens would have started blanking
   panels that had been working. `loseContext()` now releases on teardown.

### A process mistake worth recording

After adding the two endpoints I rebuilt the Tauri shell (`cargo tauri build`) and tested -- and
got `422 Unprocessable Entity` from `/clusters/top-flagged`, which looked exactly like the
route-ordering bug I had just written a test against. It was not: **the packaged app bundles a
frozen copy of the Python backend**, and `cargo tauri build` does not rebuild it.
`scripts/build_backend_sidecar.py` has to run first, or the desktop app keeps serving the previous
API. Worth knowing before chasing a phantom bug: the passing test was right and the binary was
stale.

### Honest limits

- **`frameable_share` reads "-" on every panel** against the current data. That is real: only ~126
  of snapshot 2's 126,862 flags have been audited, so almost no cluster has an audited flag in it.
  The column is correct and the data is thin; `null` is deliberately not rendered as `0%`.
- **Ranking by flagged wallets currently ranks by cluster size**, because `sybil_cluster` flags
  every member of a cluster -- all eight top clusters have `flagged_wallets == member_count`. The
  ranking is what was asked for and is defensible, but on this data it is not very discriminating.
- A panel showing 150 of 26,594 wallets is **0.6% of the cluster**. The footer states the ratio on
  every panel rather than leaving the impression that the picture is the whole cluster.

## Phase 10: Database improvement (2026-10-11)

A database phase, so the pass is mostly "did anything FrameCheck concludes change?" --
answered by `scripts/audit_regression.py` -- plus the usual click-through to confirm the
restructured queries still feed the UI correctly. Full numbers in `docs/perf/db_baseline.md`
and `docs/perf/db_after.md`.

| Check | Result |
|---|---|
| `pg_dump` before migrations 007 and 008 | 265 MB each, taken |
| Migration 007 (3 indexes, 4 CHECKs) | applied, `ANALYZE` after |
| Migration 008 (`mv_cluster_flag_stats`) | applied, 395,206 rows |
| Regression guard, snapshot 1 | 1,775 flags, 13 audits -- both sha256 **MATCH** |
| Regression guard, snapshot 2 | 126,862 flags, 126 audits -- both sha256 **MATCH** |
| `scripts/check_data_quality.py` | **0 failures**, 3 warnings (all real data, see below) |
| Matview vs live query, all clusters | full outer join difference query returned **0 rows** |
| `/clusters/{id}/subgraph` output | same 150 nodes, 249 edges, 1 component, 0 isolated, same verdict counts |
| `/clusters/top-flagged` output | same 8 clusters, identical counts and keys |
| Bad inputs: `limit` 0 / 9 / 5000, unknown cluster, non-numeric id, unknown run | 422 / 404 with specific messages |
| Browser: Multiview, 8 panels | edge counts per panel byte-identical to Phase 9 |
| Browser: dashboard click-through | 100 flags, no console errors |
| **Desktop app** (rebuilt sidecar + shell) | 8 panels, settled, expand to 600, collapse, no errors |
| Python suite | 242 passed, 5 skipped; ruff clean |

### A bug the verification caught before it shipped

The first draft of `mv_cluster_flag_stats` stored `count(DISTINCT wallet_id)` per flag run and
summed those at query time. It reported **26,761** flagged wallets for cluster 351299 where the
correct answer is **26,594** -- summing per-run distinct counts double-counts any wallet flagged
by two detectors. The view is now keyed at wallet grain so distinct counting happens across the
selected runs together, and it was checked against the live query for *every* cluster, not just
the top 8.

This is exactly what the "accuracy first" rule is for: the wrong version was 219x faster than the
live query and looked perfect on the eight rows anyone would have eyeballed.

### Two things deliberately NOT changed

1. **1,557 self-loop edges.** A CHECK constraint forbidding `src_wallet_id = dst_wallet_id` was
   drafted, then dropped after checking the data: these are real transactions where `from == to`
   with value 0 (contract self-calls). The constraint would have rejected correct on-chain data,
   and `ALTER TABLE` would have failed on 1,557 existing rows. Reported as a warning instead.
2. **`snapshot_edge.edge_weight` is a double holding wei**, which rounds beyond ~15 significant
   digits. Measured rather than assumed: 8.6% of value-carrying edges differ from the exact
   `NUMERIC` sum, but the **largest absolute error is 30,000,000 wei -- 0.00000003 ETH**
   (relative 5e-15). Widening the column rewrites a 174 MB table and perturbs the PageRank inputs
   that produce flags, risking the regression guard, to recover an error far below anything that
   changes a ranking. Framing costs never read it.

### Findings during the pass

1. **One 503 immediately after restarting Postgres.** Enabling `pg_stat_statements` needs a
   restart, which leaves the API's pooled connections dead; the first request using one failed.
   The pool discarded it and every subsequent request succeeded -- the broken-connection handling
   in `get_conn()` working as designed, confirmed rather than assumed.
2. **A self-inflicted bug worth recording.** Restructuring `/clusters/top-flagged` left the old
   `rows = rows_as_dicts(cur)` line in place after the new verdict query, so `rows` was
   reassigned from an already-exhausted cursor and the endpoint returned `[]` while still
   answering 200. The timing harness showed a *faster* endpoint; only checking the body caught
   it. Timings alone would have reported this as a win.
3. **The packaged app needed its sidecar rebuilt** before the desktop check, for the same reason
   as Phase 9: `cargo tauri build` does not rebuild the frozen Python backend, so the desktop app
   would have kept serving the pre-Phase-10 API.

### Not done

- **`postgresql.conf` tuning is proposed, not applied** (see `docs/perf/db_after.md`). The server
  is on defaults that assume a spinning disk; `random_page_cost = 4` is the one most likely to
  change plan choices and should be changed alone, with the regression guard re-run after.
- **Partitioning** was assessed and rejected: `transaction` never appeared in the slow query list,
  so partitioning 1.3 M rows would add planning overhead and migration risk for no measured gain.

## Phase 11: CPU vs GPU, re-measured (2026-10-11)

The brief's question was "did the GPU actually run, or did everything fall back to CPU?" --
answered by evidence rather than by the recorded backend string -- plus a re-measurement after
the Phase 10 database work and a hunt for the crossover point.

| Check | Result |
|---|---|
| GPU actually executing | **Yes.** Graph object is `cugraph.structure.graph_classes.Graph`; the CPU path returns `networkx.DiGraph`. Device memory 838 MB -> 876 MB on graph build |
| Re-benchmark, snapshots 1/2/3 | completed, 3 runs each, median; `backends.gpu` recorded on all three |
| Audit pass CPU vs GPU | 14.02 s/flag -> 0.76 s/flag (18.4x) |
| Audit benchmark cleanup | verified directly: 260 rows before, 260 after |
| Crossover sweep, 2k-640k edges | completed on snapshot 3 |
| Regression guard after all of it | snapshots 1 and 2 **identical** (both sha256 match) |
| Benchmarks tab | crossover table renders above the per-snapshot panels, no console errors |
| Python suite | 243 passed, 5 skipped; ruff clean |

### The answer to the brief's question

The GPU ran. Three independent signals, not one: `gpu_available()` is true, the graph object is a
cuGraph `Graph` (a fallback would be a NetworkX `DiGraph` -- which is exactly what the CPU path
returns in the same script), and GPU device memory genuinely grows when the graph is built.

### The crossover, which is the part the speedups were hiding

"The GPU is 48x faster" is true for Louvain at 698k edges and badly misleading in general. On the
same hardware **BFS is ~50x slower at 2,000 edges** and does not overtake the CPU until somewhere
between 20,000 and 40,000. `scripts/benchmark_crossover.py` (new) measures this rather than
asserting it:

- `pagerank`, `louvain`: GPU ahead from the smallest size measured (2,000 edges)
- `load_edgelist`: crosses between 5,000 and 10,000 edges
- `bfs`: crosses between 20,000 and 40,000 edges

BFS is not simply "slower"; it is doing more work. The CPU BFS stops once it has reached what it
can reach (76 wallets on snapshot 1), while `cugraph.bfs` returns a row for every vertex.

### Did the database work move the numbers? No.

Expected, and stated as a non-result rather than dressed up: `scripts/benchmark.py` reads the
snapshot Parquet files, not Postgres, so Phase 10's indexes and materialized view are not on this
path. Louvain on snapshot 2 went 33.68x -> 32.52x and on snapshot 3 44.75x -> 48.22x, which is
run-to-run variation, not an effect.

### Something I could not fully explain

Four `flag_audit` rows dated during this session remain for flags 1776-1779. They are
**byte-identical** to those flags' existing audits (same verdict, same framing cost, same method
-- the audit is deterministic), and the regression guard confirms no conclusion changed. But the
audit benchmark ran `--detector pagerank_threshold`, and these are `bfs_proximity` flags, so it
did not create them; the API logged no job in that window either. I could not pin the provenance
and am recording that rather than inventing an explanation.

One real weakness did surface while chasing it: `benchmark_audit.py` cleans up by deleting
`flag_audit_id > max_id_before`. That is correct in isolation (verified: 260 rows before, 260
after) but it is **not safe against concurrent writers** -- anything else inserting an audit while
the benchmark runs would have its row deleted too. Fine for a single-operator tool, worth knowing
before anything runs analyses in parallel.

### Still unresolved, and more important than any speedup

CPU and GPU PageRank still disagree on snapshot 3 (disjoint top-5, 84/100 top-100 overlap, GPU top
scores ~3.5x larger), because `edge_weight` is wei and 71.3% of edges carry weight 0, so the two
libraries' dangling-vertex conventions dominate. `pagerank_threshold` compares against an
**absolute** threshold, so whether RAPIDS is installed can change which wallets get flagged. Every
speedup in `docs/benchmark_results.md` is a timing comparison and explicitly **not** a claim that
both backends computed the same answer. This needs a deliberate decision and has not had one.

## PageRank convergence: the backend disagreement was ours (2026-10-11)

Carried as unresolved through Phases 4, 10 and 11, and wrong in all three: the
CPU/GPU PageRank disagreement was recorded as a weighting problem (wei values,
71.3% zero-weight edges, dangling-vertex conventions). It was **NetworkX never
converging.**

NetworkX converges when `sum|x - x_last| < N * tol`, with `N` the vertex
count, so the default `tol=1e-6` allows a total L1 error of 0.455 on snapshot
3's 455,073 vertices -- on a distribution summing to 1. Top score came out
0.00097 against a converged 0.00349, which is precisely the "GPU scores ~3.5x
larger" that three phases of docs attributed to the weights.

| Check | Result |
|---|---|
| Flag agreement, snapshot 3 (4,551 flags) | 3,053 agreed before, **4,551 after** (Jaccard 0.505 -> 1.0000) |
| Flag agreement, snapshot 2 (2,539 flags) | 2,290 -> **2,539** (0.821 -> 1.0000) |
| Flag agreement, snapshot 1 (192 flags) | 189 -> **192** (0.969 -> 1.0000) |
| Agreement under unweighted and `log1p` weights | also 1.0000 -- the weighting decision was moot |
| Ties at the percentile cut as an explanation | ruled out: exactly 1 wallet on the cut score, 0 disagreeing wallets within 1e-9 of it |
| `benchmark.py`'s own agreement check | same top PageRank wallet on both backends now (173, 1868, 1868); previously disjoint |
| Cost of converging | 75 iterations worst case; CPU PageRank 0.88s -> 1.03s on snapshot 3 |
| Python suite | 247 passed, 5 skipped; ruff clean |

### How it was found, and what that says about the earlier evidence

By measuring the disagreement in the units that matter -- how many wallets
`pagerank_threshold` actually flags, via the production selection path
(`scripts/pagerank_agreement.py`, new) -- rather than comparing scores.
That reframing is what exposed it: 1,498 of snapshot 3's 4,551 flags changed
with the backend, far worse than the "84/100 top-100 overlap" the earlier
write-ups reported, and a Spearman of 1.0000 had been hiding it because the
hundreds of thousands of low-rank vertices dominate the correlation.

Two hypotheses were tested and killed before the right one: ties at the
percentile cut (0 of the disagreeing wallets are within 1e-9 of the cut
score), and `log1p` weight compression, which made agreement **worse**
(0.4206 vs 0.5047). The previously-reported evidence that "an unweighted CPU
run matches the GPU *less*" was itself measured with the unconverged CPU
path, so it never meant what it was taken to mean.

### Fixes

1. `graph/backend.py`: CPU PageRank derives its tolerance from an absolute L1
   budget (`PAGERANK_L1_TARGET / N`) so the criterion stops getting looser as
   the graph grows, with `max_iter=1000`. Non-convergence logs a loud warning
   and falls back to the library default rather than killing a pipeline run --
   it must never be returned silently as if converged. The GPU tolerance is
   pinned at 1e-8 rather than inheriting cuGraph's default.
2. `graph/audit.py`: `PAGERANK_PASSES_PER_PROBE` was justified as
   "max_iter=100 in both backends" -- a cap that no longer holds. Re-derived
   as a measurement (50 iterations on snapshots 1-2, 75 on snapshot 3), so the
   value and therefore every audit budget, verdict and framing cost is
   unchanged.
3. `scripts/benchmark.py`: found while re-measuring. `merge_json_run` rewrote
   `docs/perf/benchmarks.json` as `{"runs": ...}` flat, which would have
   **deleted the crossover section** -- added one commit earlier and served by
   `GET /benchmarks` -- on the next benchmark run. It now rewrites the
   document and preserves keys it does not own. Pinned by a test.

### Honest consequences

- **PageRank speedups fall**, because the CPU column now does the whole job
  and the GPU is held to a tighter tolerance: snapshot 3 goes 9.69x -> 6.70x,
  snapshot 2 12.30x -> 10.16x, snapshot 1 3.09x -> 2.23x. Louvain and
  load_edgelist are unaffected (49.75x, 38.06x on snapshot 3).
- **The 18.4x audit-pass figure predates this** and has not been re-measured;
  its CPU side ran early-stopping PageRank probes, so it is a lower bound on
  the gap, not an overstatement. Left as-is rather than quietly rescaled.
- **The flags in the live database are still the unconverged ones.** Verified
  for snapshot 2, whose latest stored PageRank run is the CPU one: stored flags
  match the unconverged recomputation exactly (2,539/2,539) and differ from the
  converged one by 249 each way. 249 wallets are flagged that should not be,
  and 249 that should be are not. Correcting it means re-running the detectors,
  which changes stored results -- a deliberate data change, not done here.

## Correcting the stored pagerank_threshold flags (2026-10-11)

The convergence fix changed what the detector computes, so the flags already
in Postgres were the unconverged ones. Corrected on snapshot 2, the only
affected snapshot: snapshot 1's stored PageRank run came from the GPU (already
converged) and snapshot 3 has no stored PageRank run at all.

| Step | Result |
|---|---|
| `pg_dump -Fc` before any write | 277 MB, `backups/txgraph_before_pagerank_fix_20261011T145002.dump` |
| Re-ran PageRank only, CPU path | new `analysis_run` 38, top score 0.004099 (the converged value; the old run 10 gave 0.00461) |
| Re-ran the `pagerank_threshold` detector | new flag run 39, 2,539 flags |
| New flags vs an independent converged recomputation | **2,539 of 2,539 identical, 0 either way** |
| New flags vs the old run 29 | 2,290 shared, **249 added, 249 dropped** |
| Evidence for run 39 | 525,201 rows |
| Audit, bounded sample of the new flags | 25 flags, all frameable (`greedy_upper_bound`), ~1 s each on GPU |

Nothing was deleted: each re-run is a new `analysis_run`, so run 10's metrics
and run 29's flags are still in the database and the API resolves the latest.
That also makes this reversible without touching the dump.

`graph/run_analysis.py` gained `--only pagerank` for this. Re-running all
three algorithms would have created a new Louvain/cluster run as collateral,
changing what every cluster-based view resolves to -- a much larger change
than the one being made.

### A Phase 11 loose end, probably explained

Phase 11 recorded 4 unattributable `flag_audit` rows for flags 1776-1779 and
said the audit benchmark could not have made them because it ran
`--detector pagerank_threshold`. While auditing the new flags I reproduced
the signature by accident: `graph.audit --limit 25` orders by `flag_id`, so
it audits flags **1776-1800** -- the oldest `bfs_proximity` flags -- not the
newest ones. Any plain `--limit` audit call lands exactly there, and the
audit is deterministic, which is why those rows were byte-identical to the
existing ones. That is a mechanism, not a receipt: it explains the rows
without proving which invocation wrote them, so it is recorded as probable.

The practical lesson is about the tool, not the mystery: `--limit` without
`--run-id` or `--detector` silently audits the oldest flags in the database
rather than anything related to what was just computed.

## Phase 12, Feature 1: database setup and ingestion inside the app (2026-10-11)

What a fresh install actually hit before this, found by reading the code and
the bundle rather than assuming:

| Blocker | Detail |
|---|---|
| Sign-in wall with nothing behind it | `auth_mode()` fails closed, and no `GOOGLE_CLIENT_ID` or session secret exists, so a new user met a Google modal they could not pass. Hit for real during this session's app test. |
| No way to reach a database from the app | `PG_DSN` is read from the environment at import time; an installed app has no `.env`, so even a user with Postgres installed could not point the app at it. |
| Demo mode half-empty | First launch defaults to mock mode, but `mock_data.json` has no `clusters_top_flagged` or `cluster_subgraphs`, so Multiview renders empty panels. |
| No schema bootstrap | `db/schema.sql` and `db/migrations/` are not bundled in the sidecar. |

### What was built

`api/dbsetup.py` plus a Database view: connection settings, status, create
database, apply schema and migrations, browse tables, and automatic Etherscan
ingestion as a cancellable job.

| Check | Result |
|---|---|
| Fresh-install path on a throwaway database | create 0.68 s -> `empty`, 7 pending -> schema.sql + 7 migrations in 0.57 s -> `tracked`, 24 tables, 0 pending |
| Re-running create, and re-running schema | both no-ops, reported as such, nothing recreated |
| Fresh tables vs the real database's | **identical apart from the new `schema_migration` table** -- the setup path reproduces the real schema |
| Database view driven in headless Chrome against the live DB | 23 tables, estimates labelled, exact count on demand (wallet 610,055), 50-row peek at `flag`, 0 console errors, 0 uncaught exceptions, panel area 791,856 px² |
| `pg_shadow` through the table-browse route | refused -- the catalogue check rejects anything outside `public` base tables |
| `apply_schema` on the real (untracked) database | refused with the adopt pointer, as designed |
| Python suite | 311 passed, 5 skipped; `cargo test --lib` 42 passed; ruff clean |

### Two measurements that changed the design

1. **Exact row counts are too slow for a panel.** The first version used
   `count(*)` per table, with a comment arguing that `reltuples` can read -1
   and showing "-1 rows" is worse. Measured on the real database: **6.85 s**
   for 23 tables (2.1M `snapshot_edge_tx`, 1.3M `transaction`). Now one
   `pg_class` query at **0.013 s** (525x), labelled as estimates, with an
   exact count per table on demand (`wallet`: 0.41 s) and `null` +
   "unknown" for a never-analysed table rather than Postgres' raw -1. Three
   real tables (`algorithm`, `analyst`, `app_user`) are in that state.
2. **Three schema states, not two.** Every database this project built before
   today has FrameCheck tables and no migration record, so "has tables"
   cannot be read as "up to date". `untracked` is reported as unknown
   (`migrations_pending: null`, not `[]`), applying is refused on it, and
   adopting -- recording history without running it -- is a separate explicit
   action, because it is a claim that cannot be verified from inside the
   database.

### Things deliberately not built

- **Nothing that drops, truncates or unscoped-deletes.** A test asserts that
  about `api/dbsetup.py`'s source. Importing into a database that already
  holds FrameCheck tables is refused rather than resolved by wiping it.
- **No plan detection that is not real.** Etherscan reports an allowance only
  for paid keys, so `/ingest/plan` reports `source: "configured"` -- the rate
  the app is using -- and only says `"etherscan"` when Etherscan actually
  answered. Every non-answer from the probe (unsupported endpoint, error
  status, unexpected shape, network failure) degrades to configured. The app
  never claims to know a plan it was not told.
- **No progress percentage for a crawl.** A BFS frontier is discovered as it
  goes, so `total` stays 0 and the UI shows counts (addresses, frontier size,
  API calls) rather than a fabricated denominator.

### Mistakes made while testing this

Three of my own, all in the harness rather than the code, each of which first
looked like a bug in the feature:

1. A uvicorn started with PowerShell's `Start-Process` died with the tool
   call, so the page showed `ERR_CONNECTION_REFUSED` and looked broken.
2. The page reads its API base from `localStorage`, not a query string, so
   `?api=...` was silently ignored and it kept calling port 8000.
3. Serving the page from `file://` made every call fail CORS -- which is the
   middleware working correctly (the origin regex allows localhost and
   `tauri://localhost`, not `file://`). Fixed by serving the frontend over
   `127.0.0.1` like the real app does.

Also found and fixed while re-measuring benchmarks for the PageRank work:
`scripts/benchmark.py`'s `merge_json_run` rewrote `docs/perf/benchmarks.json`
as `{"runs": ...}` flat, which would have **deleted the crossover section**
added one commit earlier. Pinned by a test now.

### Left over

- The self-test database `framecheck_selftest` is still on the Postgres
  server. Dropping it needs a `DROP DATABASE`, which this project does not do
  without asking.
- `cargo test` (all targets) fails locally on the five `examples/` binaries
  with `E0463 can't find crate for app_lib`. **Pre-existing and not caused by
  this work** -- the same five errors appear with the changes stashed, CI
  builds them fine, and `cargo test --lib` passes 42 tests. Stale local
  `target/` artifacts (the E0460 "possibly newer version" hint points that
  way).
- Feature 1's "import a database" half -- restoring a dump, and a bundled
  demo dataset -- is not built yet; only "import schema" is.

## Phase 12, Feature 1 continued: datasets (2026-10-11)

The "or database" half of "import schema or database". `scripts/
export_dataset.py` writes one snapshot's slice; the Database view imports it;
the app ships with snapshot 1 so a fresh install has real data to look at.

| Check | Result |
|---|---|
| Export snapshot 1 | 443,963 rows -> **18.9 MB gzipped**, 12.8 s |
| Import into a clean database | 443,954 rows in **8.5 s**, 13 sequences realigned (the 9-row difference is the seed rows kept, see below) |
| Every table's count, source slice vs imported | identical across all 15 tables that carry data |
| Simulated transactions in the import | **0** |
| `app_user` / `framing_scenario` rows | **0 / 0** -- never exported |
| Orphan edge endpoints / orphan evidence | **0 / 0** |
| A new INSERT after importing | gets a fresh id -- sequences were realigned, not left at 1 |
| A second import into the same database | refused, with the reason |
| `README.md` offered as a dataset | refused on the header, before anything is written |
| The UI path in headless Chrome | manifest panel, import button, refusal rendering, and the dashboard then showing **1,775 imported flags**; 0 console errors |
| The packaged sidecar | resolves its bundled dataset and all 7 migrations out of the PyInstaller extraction dir (104.6 -> 123.4 MB) |
| Python suite | 330 passed, 5 skipped; ruff clean |

### Three bugs the foreign keys caught

Each of these was a wrong assumption that a FK turned into a clear error, so
they are worth recording as the FKs doing their job rather than as mishaps.

1. **"The wallets the edges touch" is not the right scope.**
   `token_contract_wallet_id_fkey: Key (contract_wallet_id)=(51040) is not
   present in table "wallet"` -- an ERC-20 contract has a wallet row of its
   own and need not be an endpoint of any edge in the snapshot. The same goes
   for transaction recipients, token-transfer counterparties, block
   proposers, and the wallets named by metrics, clusters, flags and alerts.
   The scope is now the closure of every wallet any carried row references:
   19,133 wallets became **25,462**.
2. **A database that has only had its schema applied is not empty.**
   `db/schema.sql` seeds 1 analyst and 8 algorithm rows, so the
   "import needs an empty database" check refused every freshly prepared
   database. Seed tables are now excluded from that check and kept as the
   target has them.
3. **Ids are not portable between FrameCheck databases.** This project's
   database numbers `pagerank_threshold` **12**; a database freshly created
   from `schema.sql` numbers it **6** -- same name, different id, because ids
   were handed out as detectors were added over time. Trusting the file's ids
   produced `analysis_run_algorithm_id_fkey: Key (algorithm_id)=(10) is not
   present`, and the obvious alternative -- merging the file's seed rows --
   would have collided with the unique constraint on `algorithm_name`. The
   importer now translates seed-referencing columns through a name-to-id map.
   A name the target does not know maps to nothing and the row fails its key,
   rolling the whole import back: a refused import is better than a quietly
   incomplete one.

### What a dataset deliberately does not carry

- `app_user` -- real people's Google identities. A dataset is for sharing; an
  account list is not, and an importing app mints its own.
- Anything the framing simulator injected (`transaction.is_simulated`). Every
  transaction-scoped table filters through one shared `TX_SCOPE`, so a
  simulated row cannot arrive sideways via a token transfer or a piece of
  flag evidence. A dataset is the one artefact that leaves the machine, which
  is exactly where that promise matters most.
- `framing_scenario` -- the simulator's own bookkeeping, which would be left
  pointing at transactions that were excluded.

A dataset file is treated as data, not instructions: its COPY headers are
matched by pattern and checked against a list of importable tables, so an
edited file cannot be used to write somewhere it should not.

### One real UX bug, found only by looking at the result

Straight after an import, every row count in the Tables panel read
"unknown" -- `pg_class.reltuples` is -1 until a table is analysed, and
autovacuum had not got round to it. That looks exactly like an import that
did not work. The import now runs `ANALYZE` on the tables it loaded:
**12 tables reporting unknown before, 0 after.**

### Test databases left on the server

Four, from this feature's passes: `framecheck_selftest`,
`framecheck_demo_import`, `framecheck_demo`, `framecheck_uitest`. Removing
them needs `DROP DATABASE`, which this project does not run without asking.
`framecheck_demo` and `framecheck_uitest` each hold a full copy of the
snapshot-1 dataset (~250 MB apiece).

## Phase 12, Feature 2: the audit trail the database enforces (2026-10-12)

`db/migrations/009_framecheck_audit_trail.sql`. Chosen as Feature 2 because
the project is a DBMS project and the audit layer had no database-side
integrity at all: `flag_audit` was append-only by convention, which is to say
not at all.

Everything below was run against `framecheck_demo` (1,775 flags, 13 audit
rows, the imported snapshot-1 dataset).

### Does it do what it claims

| Check | Result |
|---|---|
| Chain over the rows that were already there | **intact**, 13 rows, after backfill |
| `UPDATE flag_audit SET verdict = ...` | refused: `flag_audit is append-only: audit row 1 cannot be edited` |
| `DELETE FROM flag_audit` | refused, with a hint naming the setting that permits it |
| Delete through the logged door | allowed, and tombstoned: `#13, postgres, "migration 009 self-test"` |
| A new audit row | links onto the tail (`prev_hash` = previous `row_hash`), chain verifies at 14 rows |
| **Tampering: edit a row with the guard trigger disabled** | **detected** — `intact=false, first_bad_audit_id=5, "row contents do not match its own hash"` |
| 500 rows inserted with the chain trigger disabled | detected — `first_bad=516, "row has no hash"` |
| Two edits to one flag | 2 history rows, 3 contiguous versions, **0 overlapping periods** |
| Time travel | score 70 → edited to 999 → `flag_as_of(t0)` returns **70** |
| `flag_as_of` before any flag existed | 0 rows, rather than every row |
| Applying the migration twice | no-op, chain still intact |
| Python suite | 355 passed, 5 skipped (22 new, all offline); ruff clean; Rust 45 passed |

### What it costs

| Measured | Result |
|---|---|
| Insert with the chain trigger | **0.054 ms/row** (500 rows in 26.7 ms) |
| Insert without it | 0.009 ms/row (500 rows in 4.2 ms) |
| So chaining costs | **6.3x** — of a number this small, and the audit engine's own work per flag is milliseconds to seconds |
| Whole-chain verification, 13 rows | **1.0 ms** |
| `flag_as_of(now())` over 1,775 flags | **2.25 ms** (plan: hash anti-join against `flag_history`) |
| Plain `count(*)` on `flag`, for comparison | 0.14 ms |
| One flag's version at an instant | **0.033 ms**, `Index Scan using flag_history_no_overlap` |
| Storage added | `flag_history` 56 kB, `flag_audit_tombstone` 48 kB |

The last two rows are the detail worth keeping: the GiST **exclusion
constraint doubles as the point-in-time index**. The constraint that makes two
overlapping versions unstorable is the same structure the planner uses for
`sys_period @> now()`, so declarative integrity came free of a second index.

### Decisions that went against the obvious choice

**A logged door, not a wall.** The first design refused every `DELETE`
outright. That breaks two scripts that legitimately remove their own rows
(`benchmark_audit.py`, `run_experiment.py`), and the honest reading of
"tamper-evident" is not "nobody can change it" — a superuser always can — but
"no change without evidence". So `DELETE` needs an explicit per-transaction
setting and lands in `flag_audit_tombstone` with who and why. A gap in the
chain with no tombstone is tampering; a gap with one is housekeeping.

**One serialization, shared.** `flag_audit_payload(flag_audit)` is used by
both the insert trigger and the verifier. Two separate serializations would
drift, and the chain would start failing on rows nobody had touched.

**Columns listed explicitly, not `to_jsonb(row)`.** A column added by a later
migration must not silently change the hash of every row already written.

**`pg_advisory_xact_lock` in the insert trigger.** Two concurrent inserts
would both read the same tail row and fork the chain — two rows claiming the
same predecessor, and a verifier that cannot say which is the real successor.
The audit engine writes from one process today, so this costs nothing now and
is the difference between a chain and a guess the moment it does not.

**`clock_timestamp()`, not `now()`, for version periods.** Two changes inside
one transaction share a transaction timestamp, which would produce an empty
period and lose a version. Verified: two edits in one transaction produced two
distinct, non-overlapping periods.

**`flag_history.flag_id` is deliberately not a foreign key.** History has to
outlive the row it describes, and `run_experiment.py` deletes flags
wholesale. A FK would either block that or cascade the history away.

**A fallback for a server without `btree_gist`.** `EXCLUDE USING gist` needs
that contrib extension for the bigint equality operator class. It is present
here (1.7) and in every mainstream install, but this schema now ships inside
a desktop app, so the migration falls back to a unique index on
`(flag_id, lower(sys_period))` and says in a NOTICE why.

### A bug found by using it

Passing a real `flag_history` timestamp straight into `/flags/as-of` returned
422. The timestamp was correct: `2026-10-12T01:35:55+00:00` — and `+` in a URL
query string decodes to a space, so the server received
`2026-10-12T01:35:55 00:00`. Now repaired at the edge (a space followed by an
offset-shaped tail is read back as the `+` it was), while a date and time
separated by a space is left alone, since that is also a valid timestamp.

### What this does not claim

A hash added by this migration proves nothing about what happened to a row
before the migration ran. The backfill establishes the baseline from which
tampering becomes detectable, and that is all. Nor does an intact chain prove
a verdict is *correct* — only that it is the verdict that was recorded.

