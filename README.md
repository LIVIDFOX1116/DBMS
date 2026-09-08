# GPU-Accelerated Ethereum Transaction-Graph Analyzer

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
cd txgraph-analyzer
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

## Running everything at once (Phase 6)

`scripts/run_pipeline.py` chains the three steps above into one command --
it resolves the seed wallet_id and snapshot_id itself instead of you reading
them off the terminal and pasting them into the next command by hand. Exit
criterion: "a fresh end-to-end run (ingestion -> GPU processing -> dashboard)
completes without manual intervention."

```bash
python scripts/run_pipeline.py                       # ingest + export + analyze, fresh
python scripts/run_pipeline.py --skip-ingestion       # reuse what's already in Postgres
python scripts/run_pipeline.py --hops 1 --force-cpu
```

Each stage is the exact same `python -m ...` command as above -- this only
removes the manual hand-off between them. Ingestion is upsert-based and safe
to re-run; export always creates a new, uniquely-named snapshot rather than
overwriting one.

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
job. See `docs/benchmark_results.md` for the current CPU-only baseline
(needs a run on a RAPIDS-capable machine to fill in the GPU column).

## Running the API (what the frontend talks to)

```bash
uvicorn api.main:app --reload --port 8000
curl localhost:8000/wallets
curl localhost:8000/docs        # interactive OpenAPI UI
```

Defaults to `TXGRAPH_API_MODE=mock`, serving `api/mock_data.json` — **the
partner can start the frontend against this right now, with zero setup
beyond this section.** See `docs/API_CONTRACT.md` for every endpoint shape.
Switch to `TXGRAPH_API_MODE=live` once Postgres actually has real analysis
results in it (Phase 3.5 in the bible).

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
colored by risk score, edges = `/snapshots/{id}/edges`) built with
vis-network. Settings (gear icon) let you point it at a different API base
URL or analysis run ID -- defaults to `http://localhost:8000` and run `1`,
matching the mock data.

Both pages share a visual design adapted from the free TemplateMo 622
"Clearwave" template (`landing.css`/`landing.js` for the landing page; the
dashboard's styling is inlined in `dashboard.html` as it always was).

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
db/            schema.sql (PostgreSQL DDL) + init_db.py
ingestion/     Etherscan client + BFS subgraph crawler (Phase 1)
graph/         backend.py (GPU/CPU abstraction), export_snapshot.py, run_analysis.py (Phases 2-3)
api/           FastAPI app + mock_data.json (the frontend's day-one contract)
frontend/      index.html (landing page) + dashboard.html (wallets/clusters/alerts/graph), no build step
scripts/       verify_gpu_env.py (Phase 0 exit criterion), run_pipeline.py (Phase 6, one-command end-to-end run), benchmark.py (Phase 5), promote_to_main.py (test-gated dev -> main)
tests/         offline unit tests -- run with `python -m pytest tests/ -v`
docs/          ER diagram (SVG + DBML), API contract, benchmark_results.md, and the full project bible
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
and `main`, and on PRs into `main`, so failures surface long before you'd
even try to promote.
