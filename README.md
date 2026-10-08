# FrameCheck — GPU-Accelerated Ethereum Framing Audit

Full context, rationale, and the 8-week beat sheet live in
[`docs/Project_Bible_TransactionGraphAnalyzer.docx`](docs/Project_Bible_TransactionGraphAnalyzer.docx).
This README is just "how do I run the thing."

Case study: the [Poly Network attacker address](https://www.chainalysis.com/blog/poly-network-hack-august-2021/)
(`0xC8a65Fadf0e0dDAf421F28FEAb69Bf6E2E589963`) — well documented publicly,
outcome resolved (funds returned), good bounded fan-out/consolidation
structure for PageRank/Louvain/BFS to actually find something. Change it in
`.env` (`CASE_STUDY_ADDRESS`), not in code.

## Who runs what, where

| Machine | Role | Why |
|---|---|---|
| Zenbook | Everyday dev for both of you — code, DB, ingestion, API, frontend | No NVIDIA GPU needed for any of this |
| GTX 1650 laptop | RAPIDS testing (small subgraphs) | ~4GB VRAM is the real budget constraint — see bible 8.1 |
| RTX 4070 laptop | RAPIDS testing (larger subgraphs) | 8GB VRAM, the machine that "must work" |

Nothing in `ingestion/`, `db/`, or `api/` needs a GPU. Only `graph/backend.py`
does, and it **falls back to pandas/NetworkX automatically** wherever RAPIDS
isn't installed — see the module docstring. That's what let this entire repo
be built and unit-tested without touching a GPU at all.

## One-time setup (Zenbook, or any machine — no GPU needed)

```bash
cd DBMS
python3 -m venv .venv && source .venv/bin/activate     # or your preferred env tool
pip install -r requirements.txt
cp .env.example .env                                    # then fill in ETHERSCAN_API_KEY
```

Install Postgres locally (`apt install postgresql` / `brew install postgresql`
/ Postgres.app on Mac), then:

```bash
python db/init_db.py            # creates the DB + applies db/schema.sql
python -m pytest tests/ -v      # confirms everything before you touch real data
```

Get a free Etherscan API key at https://etherscan.io/apis (just an email
signup — no cloud account, keeps this local-first per the project's own
privacy goal).

## Running the real pipeline (Phases 1–3)

```bash
# 1. Pull the bounded subgraph from Etherscan into Postgres
python -m ingestion.ingest_subgraph --hops 2

# 2. Export a snapshot to Parquet + record it in graph_snapshot
python -m graph.export_snapshot --name polynetwork_2hop_v1 --start-block 0 --end-block 999999999

# 3. Run PageRank + Louvain + BFS (CPU here, GPU automatically on the RAPIDS laptops)
#    look up the seed's wallet_id first:
psql $PGDATABASE -c "SELECT wallet_id FROM wallet WHERE address = lower('0xC8a65Fadf0e0dDAf421F28FEAb69Bf6E2E589963')"
python -m graph.run_analysis --snapshot-id 1 --seed-wallet-id 1
```

## Running everything at once (Phase 6 + FrameCheck)

`scripts/run_pipeline.py` chains seven stages into one command -- ingest,
export, analyze, score (wallet risk), detect (FrameCheck's five flag
detectors), evidence (FrameCheck evidence collection), audit (FrameCheck
audit engine) -- resolving the seed wallet_id and snapshot_id itself
instead of you reading them off the terminal and pasting them into the
next command by hand. Exit criterion: "a fresh end-to-end run (ingestion
-> GPU processing -> dashboard) completes without manual intervention."

```bash
python scripts/run_pipeline.py                                   # every stage, fresh
python scripts/run_pipeline.py --skip-ingestion                  # reuse what's already in Postgres
python scripts/run_pipeline.py --snapshot-id 4                   # reuse an existing snapshot (implies analyze onward)
python scripts/run_pipeline.py --from-stage detect --until-stage audit --snapshot-id 4
python scripts/run_pipeline.py --audit-limit 50                  # passthrough to graph.audit's --limit
python scripts/run_pipeline.py --hops 1 --force-cpu
```

Each stage is the exact same `python -m ...` command documented under
"Running the real pipeline" above and in `docs/FRAMECHECK.md` -- this only
removes the manual hand-off between them. `--from-stage`/`--until-stage`
pick a contiguous sub-range of `ingest, export, analyze, score, detect,
evidence, audit` (both inclusive). Ingestion is upsert-based and safe to
re-run; export always creates a new, uniquely-named snapshot rather than
overwriting one.

## FrameCheck

A new audit layer: for every flag the detectors above raise, could an
outsider have produced it just by sending the wallet unsolicited funds
(which Ethereum lets anyone do, unrefusably), and how cheaply? Full
writeup -- definitions, per-detector audit method, the framing simulator,
the API/dashboard additions, and known limitations -- is in
[`docs/FRAMECHECK.md`](docs/FRAMECHECK.md). Quick reference:

```bash
python -m graph.detectors --snapshot-id 4 --seed-wallet-id 1476 --detector all
python -m graph.evidence --snapshot-id 4 --seed-wallet-id 1476
python -m graph.audit --snapshot-id 4 --seed-wallet-id 1476 --limit 50

# Framing simulator -- a separate database only, never the main one
python scripts/simulate_framing.py --snapshot-id 4 --seed-wallet-id 1476
python scripts/run_experiment.py --snapshot-id 4 --seed-wallet-id 1476   # inject + full chain + summary
python scripts/run_experiment.py --cleanup <scenario_id>

# Flags audited per minute, CPU vs GPU
python scripts/benchmark_audit.py --snapshot-id 4 --seed-wallet-id 1476 --limit 5
```

## Benchmarking (Phase 5)

GPU-vs-CPU/NetworkX runtime comparison for the same subgraph, across
PageRank, Louvain, and BFS -- real numbers for this team's own hardware
(project bible, Section 1's "no marketing claims" framing applied to
Objective 7):

```bash
python scripts/benchmark.py --repeats 5 --output docs/benchmark_results.md
```

Runs the CPU (pandas + NetworkX) path always; adds the GPU (cudf + cugraph)
path automatically whenever RAPIDS is importable (i.e. on the RTX 4070 /
GTX 1650 laptops, not the Zenbook). Read-only against Postgres -- it doesn't
write to `analysis_run` / `vertex_metric`; that's `graph/run_analysis.py`'s
job. `docs/benchmark_results.md` now holds real CPU-vs-GPU numbers measured on
the GTX 1650 (Louvain 11×/34×/45× as the graph grows; BFS on the smallest
snapshot is 25× *slower* on GPU) plus a known disagreement between the two
backends' PageRank. `docs/GPU_SETUP.md` covers the WSL2 RAPIDS install.

## Running the API (what the frontend talks to)

```bash
uvicorn api.main:app --reload --port 8000
curl localhost:8000/wallets
curl localhost:8000/docs        # interactive OpenAPI UI
```

Defaults to `FRAMECHECK_API_MODE=mock`, serving `api/mock_data.json` — **the
partner can start the frontend against this right now, with zero setup
beyond this section.** See `docs/API_CONTRACT.md` for every endpoint shape.
Switch to `FRAMECHECK_API_MODE=live` once Postgres actually has real analysis
results in it (Phase 3.5 in the bible).

### Sign-in (Google)

**The API requires a Google sign-in by default.** `/health` and `/benchmarks`
stay public; everything else answers `401 {"detail": "sign-in required"}`
without a session. For local work without an OAuth client:

```bash
FRAMECHECK_AUTH_MODE=disabled uvicorn api.main:app --port 8000
```

Only the exact word `disabled` opens the gate — a typo or an unset variable
leaves auth **on**, so the failure direction is closed. Never set it on a
machine others can reach.

To use real sign-in, create an OAuth 2.0 client at
[console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials):
a **Web application** client (redirect URI `http://localhost:8000/auth/callback`)
for the browser dashboard, and/or a **Desktop app** client for the Tauri shell,
which uses a loopback redirect. Then fill in `.env` (never commit it):

```
GOOGLE_CLIENT_ID=…apps.googleusercontent.com
FRAMECHECK_SESSION_SECRET=   # python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`FRAMECHECK_SESSION_SECRET` signs this backend's own session tokens — changing
it signs everyone out, which is the intended way to do that. No client secret
is used anywhere in the flow: both clients are public and rely on PKCE.

### Running analyses from the app

The dashboard's **Run analysis** view runs the detectors and the framing audit
without a terminal: pick a snapshot and seed wallet, optionally one detector,
and watch progress. Set an **audit limit** on a large snapshot — auditing every
flag can take hours, and a limit takes a reproducible sample (lowest flag ids
first), the same one `python -m graph.audit --limit` would take.

Jobs run one at a time inside the API process and are forgotten when the
backend restarts; what they wrote to Postgres is not. They only ever insert.

A flag's audit report exports as **CSV** (rendered server-side, straight from
the database) or as **PDF** via the browser's print dialog against a print
stylesheet.

## Running the frontend

Two static pages live in `frontend/` -- no build step, no npm install:

- `frontend/index.html` -- a marketing-style landing page (project pitch,
  pipeline explainer, case-study spotlight, FAQ). Links to the dashboard.
- `frontend/dashboard.html` -- the actual single-page dashboard app. Talks to
  the API over `fetch()`, so serve it from any local port (opening it as a
  `file://` URL also works, since the API's CORS is wide open -- see
  API_CONTRACT.md).

```bash
# with the API running (see above), in another terminal:
python -m http.server 5500 --directory frontend
# then open http://localhost:5500          (landing page)
# or open  http://localhost:5500/dashboard.html   (straight to the app)
```

The dashboard covers all four resource types: an overview status bar, a
searchable/sortable wallet grid, a cluster explorer with member drill-down, a
filterable alerts list, and a force-directed graph view (nodes = wallets
colored by risk score, edges = `/snapshots/{id}/edges`) -- a hand-built
d3-force + Canvas2D renderer (glow nodes, particle-flow edges), not a
graph-visualization library. Settings (gear icon) let you point it at a
different API base URL or analysis run ID -- defaults to
`http://localhost:8000` and run `1`, matching the mock data.

Both pages share a visual design adapted from the free TemplateMo 622
"Clearwave" template (`landing.css`/`landing.js` for the landing page; the
dashboard's styling is inlined in `dashboard.html` as it always was).

## Desktop app (Windows, optional)

`src-tauri/` wraps `dashboard.html` + the API in a native window (Tauri) so
the app is one .exe instead of "start a Python server, then open a browser
tab." The Etherscan API key lives in the OS keychain (Windows Credential
Manager), never in `.env` or the webview -- see `src-tauri/src/keychain.rs`
and the "Etherscan key handoff" comment in `api/main.py`. This is additive:
everything above (plain `uvicorn` + a browser tab) still works exactly as
described, with or without the desktop app installed.

### First-time setup, in order

Every step below is required once per machine, even for `cargo tauri dev`
-- **the frozen backend sidecar has to exist before the very first dev
launch, not just before a release build.** Tauri's `externalBin` bundling
(`src-tauri/tauri.conf.json`'s `bundle.externalBin`) validates the sidecar
file exists at build-script time regardless of debug/release profile, even
though `resolve_backend_command()` in `backend.rs` doesn't actually *run*
it in dev mode (dev mode spawns `.venv-win`'s `api/main.py` straight from
source instead). Skip this step and `cargo tauri dev` fails before opening
a window at all -- see Troubleshooting below.

```bash
# 1. Rust toolchain (skip if `rustc --version` already works)
#    https://rustup.rs -- accept the defaults; needs the MSVC linker, which
#    Visual Studio (any edition) or the standalone "Build Tools for Visual
#    Studio" C++ workload both provide.

# 2. tauri-cli (compiles from source -- several minutes the first time)
cargo install tauri-cli --version "^2"

# 3. Backend venv, same as requirements.txt above but Windows-native
python -m venv .venv-win && .venv-win\Scripts\pip install -r requirements.txt

# 4. The sidecar -- REQUIRED before step 5, dev or release
pip install -r requirements-build.txt
python scripts/build_backend_sidecar.py
# -> src-tauri/binaries/framecheck-backend-<host-triple>.exe
#    (e.g. framecheck-backend-x86_64-pc-windows-msvc.exe on a normal 64-bit PC)

# 5. Launch
cargo tauri dev          # from src-tauri/ -- opens the real window, hot-reloads
```

Re-run step 4 only when `api/main.py` (or anything it imports) changes --
dev mode never executes this file, but Tauri's build-time check only cares
that it *exists* with the right name, so a stale one silently goes unused
rather than erroring. There's no cheap way to tell "stale" from "current"
short of rebuilding, so when in doubt, rebuild it.

A release build needs its own, separate run of the same freeze step (the
dev sidecar above is never bundled) plus one more command:

```bash
python scripts/build_backend_sidecar.py   # if not already fresh
cargo tauri build                         # from src-tauri/
```

`src-tauri/src/backend.rs`'s `resolve_backend_command()` is where dev vs.
release diverge; it errors clearly (rather than silently doing the wrong
thing) if you try to build a release binary before running the freeze step.

### The Etherscan API key

Settings (gear icon) has a key field once the app detects it's running
inside the desktop shell (`window.__TAURI__`, not the plain browser page).
Saving a key writes it to the OS keychain via `set_api_key` and hot-swaps
it into the already-running backend over a loopback-only, token-authenticated
route (`/internal/api-key` in `api/main.py`) -- it's never written to `.env`,
localStorage, or logged. "Test" makes one real call to Etherscan to confirm
the key works, without saving it. "Clear" removes it from the keychain.

**What the app actually does with a saved key today: nothing yet.** The
handoff plumbing is fully built and works (Rust owns the key, the backend
receives it at spawn and can be hot-swapped without a restart), but no
endpoint in `api/main.py` reads `_runtime_state["etherscan_api_key"]` --
mock vs. live mode is controlled purely by the unrelated `FRAMECHECK_API_MODE`
env var (see "Running the API" above). Practically: with or without a
saved key, the desktop app serves the same mock data as the browser version
until something is built to actually use the key (see Backend, item 4, in
the current work order). Save/Test/Clear all work as described above --
"connected" from Test only confirms the key itself is valid, not that
anything downstream consumes it yet.

### Troubleshooting

**`resource path 'binaries\framecheck-backend-<triple>.exe' doesn't exist`**
(build fails around `Building [...] app`, before any window opens): the
sidecar hasn't been built yet, in dev or release. Run step 4 above
(`pip install -r requirements-build.txt && python scripts/build_backend_sidecar.py`)
and retry `cargo tauri dev` / `cargo tauri build`. Cargo caches everything
it already compiled, so the retry only rebuilds the `app` crate itself --
seconds to a couple of minutes, not the full initial build.

**Proposal (not implemented -- needs a decision, not a fix I made unilaterally):**
a fresh clone hitting the error above is a rough first-run experience.
Two ways to remove it:

1. **Auto-build the sidecar via `beforeDevCommand`** in `tauri.conf.json`
   (e.g. `python scripts/build_backend_sidecar.py --if-missing`, a new flag
   to skip the ~90s PyInstaller freeze when a sidecar already exists).
   Pro: `cargo tauri dev` just works on a fresh clone, matching how most
   Tauri templates behave. Con: silently masks a stale sidecar the same way
   the current design does (see the "re-run step 4" note above) --
   "if missing" doesn't catch "exists but outdated" either, and now that
   staleness is even less visible because the build step never fails to
   remind you it's there.
2. **A dev-mode flag that skips `externalBin` bundling entirely** (Tauri
   supports per-profile bundle config) so dev builds never reference the
   sidecar at all, matching the fact that dev mode doesn't run it. Pro:
   removes the false dependency cleanly, no build-time freeze step needed
   for `cargo tauri dev` ever again. Con: dev and release then diverge in
   one more way (dev has literally no way to exercise the frozen-sidecar
   code path), so a sidecar-specific bug (like the four found running the
   GPU backend against real RAPIDS for the first time) could just as easily
   go unnoticed until a release build, which is a worse time to find it.

Leaning toward (2) since it's the more honest fix (dev mode genuinely
doesn't need this file), but it trades away the only cheap way to
sanity-check the sidecar short of a full release build -- open to either.

## GPU laptop setup (RTX 4070 / GTX 1650, inside WSL2 Ubuntu only)

1. Windows 11 + WSL2 Ubuntu (WSL1 does not work): https://learn.microsoft.com/windows/wsl/install
2. Latest NVIDIA driver on the **Windows host** (not inside WSL2).
3. Install RAPIDS via conda using `environment-gpu.yml` — see that file's
   header comment for the exact steps and why we don't pin a version number.
4. `python scripts/verify_gpu_env.py` — must print `PHASE 0 GPU CHECK: COMPLETE`
   before writing any pipeline code against the GPU. This is the Phase 0 exit
   criterion in the bible; don't skip it.

Both laptops meet RAPIDS' compute-capability floor (7.0+; GTX 1650 is
Turing/7.5, RTX 4070 laptop is Ada Lovelace/8.9) — VRAM, not compute
capability, is what actually limits subgraph size on the GTX 1650.

## Repo layout

```
db/            schema.sql (PostgreSQL DDL) + init_db.py + migrations/ (FrameCheck additions)
ingestion/     Etherscan client + BFS subgraph crawler (Phase 1)
graph/         backend.py (GPU/CPU abstraction), export_snapshot.py, run_analysis.py, score_wallets.py (Phases 2-3),
               detectors.py, evidence.py, audit.py (FrameCheck)
api/           FastAPI app + mock_data.json (the frontend's day-one contract)
frontend/      index.html (landing page) + dashboard.html (wallets/clusters/alerts/graph/audit), no build step
scripts/       verify_gpu_env.py (Phase 0 exit criterion), run_pipeline.py (7-stage one-command end-to-end run),
               benchmark.py (Phase 5), promote_to_main.py (test-gated dev -> main),
               backend_entrypoint.py + build_backend_sidecar.py (desktop app release packaging),
               simulate_framing.py, run_experiment.py, benchmark_audit.py (FrameCheck)
tests/         offline unit tests -- run with `python -m pytest tests/ -v`
docs/          ER diagram (SVG + DBML), API contract, benchmark_results.md, FRAMECHECK.md, and the full project bible
src-tauri/     optional native desktop wrapper (Windows) around the frontend + API -- see "Desktop app" above
```

## Tests

Everything in `tests/` runs offline — no network, no live Postgres, no GPU.
It tests logic (BFS crawl behavior, backend algorithm correctness on a known
synthetic graph, Parquet round-trip, run-lifecycle bookkeeping), not the live
Etherscan/Postgres/RAPIDS integrations, which can only be exercised on a
machine with internet access and/or a GPU.

```bash
python -m pytest tests/ -v
```

## Promoting dev to main

`dev` is the working branch -- commit and push there. `main` only moves
forward via a manual, test-gated promotion:

```bash
git checkout dev
python scripts/promote_to_main.py
```

This runs the full test suite and pushes `dev` to `origin/main` (as a
fast-forward) only if every test passes; main is left untouched on any
failure. CI (`.github/workflows/ci.yml`) also runs on every push to `dev`
and `main`, and on PRs into either, so failures surface long before you'd
even try to promote.

## CI/CD

None of this needs Postgres, a GPU or any secret -- tests are offline, the
API runs in mock mode, graph code takes the CPU path.

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | push / PR to `dev` and `main` | `lint` (ruff), `test` (offline pytest), `rust` (`cargo test`, Windows), then `installer` |
| `build-installer.yml` | called by CI and release | builds the PyInstaller sidecar, then `cargo tauri build --bundles nsis`; uploads the installer as the `framecheck-installer` artifact |
| `release.yml` | push of a `v*` tag (e.g. `v0.2.0`) | builds the installer and attaches it to a GitHub Release |

Run the lint locally with `pip install ruff && ruff check .`. The installer is
unsigned, so Windows SmartScreen will warn on first run. Phase tags such as
`fc-p1` do not trigger releases; only `v*` tags do.

### Branch protection (set in GitHub: Settings > Branches)

Repository admins must configure this; the repo can't do it by itself.

- **`main`**: require a pull request before merging; require status checks
  `lint`, `test`, `rust` (and `installer` if you want the bundle gated) to pass
  and be up to date; block force pushes and deletion. `scripts/promote_to_main.py`
  pushes straight to `main`, so either let admins bypass for that or promote
  through a `dev` -> `main` pull request instead.
- **`dev`**: require the same status checks; block force pushes and deletion;
  direct pushes are fine for small commits.
