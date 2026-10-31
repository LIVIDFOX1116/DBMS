# FrameCheck — GPU-Accelerated Ethereum Framing Audit

[![CI](https://github.com/LIVIDFOX1116/DBMS/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/LIVIDFOX1116/DBMS/actions/workflows/ci.yml)
[![Release](https://github.com/LIVIDFOX1116/DBMS/actions/workflows/release.yml/badge.svg)](https://github.com/LIVIDFOX1116/DBMS/actions/workflows/release.yml)

Full context, rationale, and the 8-week beat sheet live in
[`docs/Project_Bible_TransactionGraphAnalyzer.docx`](docs/Project_Bible_TransactionGraphAnalyzer.docx).
This README is just "how do I run the thing."

Case study: the [Poly Network attacker address](https://www.chainalysis.com/blog/poly-network-hack-august-2021/)
(`0xC8a65Fadf0e0dDAf421F28FEAb69Bf6E2E589963`) — well documented publicly,
outcome resolved (funds returned), good bounded fan-out/consolidation
structure for PageRank/Louvain/BFS to actually find something. Change it in
`.env` (`CASE_STUDY_ADDRESS`), not in code.

## Roles

| Role | Can |
|---|---|
| `viewer` | every read-only view and GET endpoint, CSV export |
| `analyst` | viewer + run analysis jobs, manage labels and wallet tags, review alerts |
| `admin` | analyst + Database view (`/db/*`), Etherscan ingest (`/ingest/*`), user management (`/admin/users*`) |

Roles live in the single Users table, `app_user` (migration 010), and are read on every request, so a change or a
deactivation applies at once. `api/rbac.py` holds one table of every route's minimum role; a route missing from it is
admin-only, and a test fails if one is missing. `FRAMECHECK_AUTH_MODE=disabled` (the desktop app) acts as a local
admin, `FRAMECHECK_PUBLIC_DEMO=1` as an anonymous viewer. After migrating an existing database, run
`python db/init_db.py` (back up first: migration 010 drops the emptied `analyst` table).

## Technology stack

| Layer | Technology |
|---|---|
| Database | PostgreSQL 16+ (25 tables, PL/pgSQL triggers, functions and procedures, a view, a materialized view) |
| **ORM** | **SQLAlchemy 2.x** (`db/orm/`), over the psycopg2 driver |
| Graph analytics | cuDF + cuGraph on the GPU (WSL2 / RAPIDS), pandas + NetworkX on the CPU |
| API | FastAPI + Uvicorn |
| Frontend | plain HTML/JS, no build step |
| Desktop app | Tauri 2 (Rust) with a PyInstaller-frozen Python sidecar |
| Sign-in | Google OAuth 2.0 and bcrypt local accounts; admin / analyst / viewer roles |
| Deployment | Docker, docker compose, Docker Hub (private), GitHub Actions, Render, Neon |

**DDL in SQL, data access through the ORM.** The schema is defined only by
`db/schema.sql` and `db/migrations/*.sql`, applied in order and tracked in
`schema_migration` — the ORM never creates or alters a table, and
`tests/test_orm_models.py` fails if a model drifts from that DDL. The application's
CRUD and sign-in paths read and write through the SQLAlchemy models and sessions.
Raw SQL remains only where it is the right tool — PL/pgSQL functions, DDL and catalogue
introspection, bulk/performance-critical paths, the recursive evidence CTE — and each
site is tagged in code and listed with its reason in
[`docs/API_CONTRACT.md`](docs/API_CONTRACT.md#orm-coverage-and-permitted-raw-sql).

**Schema documentation.** [`docs/ER_Diagram_TransactionGraphAnalyzer.svg`](docs/ER_Diagram_TransactionGraphAnalyzer.svg)
(and `.png`) and [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md) are generated from
[`docs/schema_v2.dbml`](docs/schema_v2.dbml) by `scripts/render_er.ps1` / `render_er.sh`:
**25 tables, 35 foreign keys (34 many-to-one, 1 one-to-one), 4 tables with no FK by design**
(each says why on the diagram). `tests/test_er_coverage.py` fails if a table, column or
foreign key in the DDL is missing from them.

## Docker

```bash
# .env needs POSTGRES_PASSWORD and FRAMECHECK_SESSION_SECRET (see .env.example)
docker compose up --build        # http://localhost:8000/dashboard.html
```

The image is CPU only (no RAPIDS) and about 1.08 GB. On each start it applies pending migrations; with
`FRAMECHECK_IMPORT_DEMO=1` (the compose default) it imports the demo dataset into an empty database:
443,954 rows, 133 MB restored. Create a first admin inside the container:

```bash
docker compose exec api python scripts/create_admin.py --username <name> --email <email>
```

Audit jobs replay snapshot Parquet files: put them in `./snapshots`, mounted at `/data/snapshots`
(`FRAMECHECK_DATA_DIR=/data`). Without the file, `POST /jobs` for an audit answers a clear 409.
Backups inside compose: [`docs/BACKUP_RESTORE.md`](docs/BACKUP_RESTORE.md).

## Deployment

**Private by design until the provisional patent application is filed:** a private Docker Hub repository,
sign-in required with an email allowlist, no `FRAMECHECK_PUBLIC_DEMO`, `FRAMECHECK_HIDE_LANDING=1`, and the
GitHub repository stays private. Secrets go only in the dashboards named below, never in code or chat.

**1. Docker Hub.** hub.docker.com → Repositories → Create repository → name `framecheck`, visibility **Private**
(check docker.com/pricing for the free private-repository allowance). Then Account settings → Personal access
tokens → Generate new token, Read & Write. In GitHub: repository → Settings → Secrets and variables → Actions →
add `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`. Pushing to `main` or a `v*` tag publishes
`<user>/framecheck:latest` and `:<git-sha>`.

Without a push to `main` or a tag, publish from your own machine: `docker login`, then
`docker build -t <user>/framecheck:latest .` and `docker push <user>/framecheck:latest`.

**2. Neon (database).** neon.tech → New project, PostgreSQL 17 (matching the local server that makes the dumps).
Copy the connection string from Connect; it ends in `?sslmode=require`. The free tier's 0.5 GB holds the data, about
131 MB restored, and idle compute wakes by itself on the next connection. Load data one of two ways: restore a dump
with `pg_restore --no-owner --no-privileges -d "<connection string>" backups/<file>.dump` and leave
`FRAMECHECK_IMPORT_DEMO` unset, or set `FRAMECHECK_IMPORT_DEMO=1` for the first deploy to import the demo dataset
into the empty database.

**3. Render (app).** render.com → New → Web Service → Existing image → `docker.io/<user>/framecheck:latest`, and add
Docker Hub credentials so it can pull a private image. Instance type Free (spins down after 15 idle minutes, about a
minute to wake). Environment:

| Variable | Value |
|---|---|
| `DATABASE_URL` | the Neon connection string |
| `FRAMECHECK_API_MODE` | `live` |
| `FRAMECHECK_AUTH_MODE` | `required` |
| `FRAMECHECK_SESSION_SECRET` | output of `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `FRAMECHECK_HIDE_LANDING` | `1` |
| `FRAMECHECK_ADMIN_EMAILS` | your Google address |
| `FRAMECHECK_ALLOWED_EMAILS` | team and faculty addresses, comma separated (`*@domain` also works) |
| `FRAMECHECK_IMPORT_DEMO` | `1` only if you did not restore a dump in step 2 |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | from step 4 |

**4. Google sign-in.** console.cloud.google.com → APIs & Services → Credentials → your OAuth client → add
`https://<app>.onrender.com/auth/callback` to Authorized redirect URIs, and add a custom domain's URL too if you
use one.

**5. Custom domain (bonus).** Render needs a CNAME to `<app>.onrender.com` for a subdomain, and accepts an A record
to `216.24.57.1` only as the fallback for apex domains. DuckDNS offers only A records, so try
`framecheck.duckdns.org` → `216.24.57.1`, add it under Render → Settings → Custom Domains, and see whether it
verifies and issues TLS. If it does not, use a domain that supports CNAME.

**Live URL:** not deployed yet: steps 1 to 4 need the project's own accounts.

## Setup: build it, then run it

Three paths, shortest first. Pick one — they stack, so path 2 assumes path 1
and path 3 assumes path 2. Every command below was run on the machine this
was developed on; where something is measured, the number is what it actually
took here.

**What you need in all cases:** Python 3.12 (what CI pins) and a terminal.
Nothing needs a GPU — `graph/backend.py` falls back to pandas/NetworkX
wherever RAPIDS is absent. No Node or npm anywhere: the frontend is plain
files and Tauri serves `frontend/` directly.

### Path 1 — run it with no database and no API key (demo mode)

```bash
python -m venv .venv-win                       # on Windows; `.venv` elsewhere
.venv-win\Scripts\pip install -r requirements.txt
```

```powershell
# terminal 1 -- the API. Demo mode is the default; the auth variable is not
# optional, see the note below.
$env:FRAMECHECK_AUTH_MODE = "disabled"
.venv-win\Scripts\python -m uvicorn api.main:app --port 8000
```

```powershell
# terminal 2 -- the pages
.venv-win\Scripts\python -m http.server 5500 --directory frontend
```

(In bash: `FRAMECHECK_AUTH_MODE=disabled python -m uvicorn api.main:app --port 8000`.)

Open <http://localhost:5500/dashboard.html>. Demo mode serves
`api/mock_data.json`, which carries real figures from a real run — flags,
clusters with verdicts, eight Multiview panels, and the audit-chain panel —
so every view has something in it before you have a database at all. Nothing
is written anywhere.

**Why that variable is required here.** The API's session gate defaults to ON
and fails closed, so without it every endpoint but `/health` answers
`401 {"detail": "sign-in required"}` and the dashboard shows a sign-in modal
with nothing behind it — verified by running Path 1 without it. The desktop
app (path 2) needs no such variable: it sets it for its own sidecar. Don't set
it on a machine others can reach.

**Check `mode` if the numbers look like real data.** `curl localhost:8000/health`
reports `{"status":"ok","mode":"mock"}` in demo mode. A checkout with no `.env`
defaults to demo, but an existing `.env` with `FRAMECHECK_API_MODE=live` wins
over an unset variable, so on a machine that has one, pass
`FRAMECHECK_API_MODE=mock` explicitly. (Worth knowing in PowerShell:
`$env:X = ""` *removes* a variable rather than blanking it, which is exactly
how the above went unnoticed here once.)

### Path 2 — build the Windows desktop app

One `.exe` instead of two terminals. In order, because step 4 must exist
before step 5 can even start:

```bash
# 1. Rust toolchain, if `rustc --version` doesn't already work:
#    https://rustup.rs -- needs the MSVC linker, from Visual Studio or the
#    standalone "Build Tools for Visual Studio" C++ workload.

# 2. The Tauri CLI (compiles from source; several minutes, once)
cargo install tauri-cli --version "^2"

# 3. The build-only Python deps (PyInstaller and friends)
.venv-win\Scripts\pip install -r requirements-build.txt

# 4. Freeze the backend into a sidecar executable -- REQUIRED before step 5,
#    for `cargo tauri dev` as well as for a release build
.venv-win\Scripts\python scripts/build_backend_sidecar.py

# 5. Build the app and its installers
cargo tauri build
```

That produces, under `src-tauri/target/release/`:

| Artifact | What it is |
|---|---|
| `framecheck.exe` | the app itself (~19 MB) |
| `bundle/nsis/FrameCheck_0.1.0_x64-setup.exe` | the installer most people want (~122 MB) |
| `bundle/msi/FrameCheck_0.1.0_x64_en-US.msi` | the same thing for policy-managed installs (~124 MB) |

The installer is large because the sidecar (~123 MB) carries a frozen Python
runtime, `db/schema.sql`, every migration, and the demo dataset — so an
installed copy can set up a database and load real data with no checkout
present. `cargo tauri dev` instead of step 5 gives a hot-reloading window
against `api/main.py` from source.

**Two things that will waste your time if you skip them.** Step 4 is checked
for existence at Tauri build-script time regardless of profile, so skipping
it fails before a window opens — and a *stale* sidecar is silently used
rather than erroring, so rebuild it whenever `api/` changes. And the build
needs the app not to be running: it fails with `Access is denied (os error 5)`
while `framecheck.exe` is open, which is easy to misread as a toolchain
problem.

### Path 3 — point it at your own Postgres (live data)

Install Postgres 16, then do the rest **in the app**: the **Database** tab
exists so this needs no terminal and no `.env`.

1. **Connection** — host, port, database name, user, password. The password
   goes to the OS keychain, is pushed to the backend over loopback, and is
   never read back.
2. **Create database**, then **Import schema** — `db/schema.sql` plus all
   eight migrations, applied in order and recorded in a `schema_migration`
   table. Measured on an empty database: 0.57 s for all of it.
3. **Import data** — either the bundled demo dataset (443,963 rows, ~9 s) or
   **Pull live data from Etherscan**, which needs a free key from
   <https://etherscan.io/apis> and runs with a rate limit, a hard call budget
   and a working Stop button.
4. **Use live data** — the toggle is disabled until a reachable database with
   the schema applied actually exists, so it cannot be switched on into a
   broken state.

An **existing** FrameCheck database from before migration tracking gets an
extra button, **Adopt this database**, which records migrations as applied
*without running them*. It deliberately refuses to claim any migration whose
work it cannot find: adopting a pre-009 database records seven and leaves
`009_framecheck_audit_trail.sql` pending, so the next click actually installs
the audit trail.

For repo tooling (`scripts/`, `python -m graph.*`) the database comes from
`PGDATABASE` in `.env` instead — `framecheck` on this machine. The app keeps
its own setting in the keychain, so the two are independent and can disagree.

### Gotchas that actually cost time here

- **Postgres inside WSL, app on Windows.** The Windows→WSL loopback relay
  stops answering about 20 seconds after the WSL instance goes idle —
  measured: fine immediately after touching WSL, refused at 20 s, 40 s and
  60 s; fine at 0/25/50/75 s with a process held alive in the distro. Keep
  one alive: `wsl -d Ubuntu-24.04 -- sleep 21600`. Nothing in this repo can
  fix that from the Windows side.
- **`localhost` is not `127.0.0.1`.** On Windows `localhost` resolves to
  `::1` first, where a WSL Postgres isn't listening, and the IPv6 attempt
  burns about 2 seconds before falling back — measured 2112 ms against 64 ms.
  It *succeeded*, so it showed up as lag rather than an error. The app now
  tries the IPv4 literal first for loopback names; if you see unexplained
  two-second pauses in your own tooling, that's where to look.
- **Free space on `C:`, even when the repo is on another drive.** Two
  separate ways this bites:
  - *Running.* Each launch extracts the ~230 MB sidecar into `%TEMP%`, and a
    backend that is force-killed leaves its copy behind. Enough of those will
    fill the drive and the app then fails to start for no visible reason.
    They are the `_MEI*` directories in `%TEMP%`; deleting them while nothing
    is running is safe.
  - *Building.* PyInstaller and WiX both want a couple of GB of system temp.
    With 80 MB free on `C:` (and 58 GB on `E:`) the sidecar build died as
    `PyInstaller ... returned non-zero exit status 4294967295` and the MSI
    step as ``failed to run ...\light.exe`` — neither message mentions disk
    space. `scripts/build_backend_sidecar.py` now points its own temp at
    `build/tmp` inside the repo so it no longer cares; for `cargo tauri
    build`, set `TMP`/`TEMP` yourself if `C:` is tight:
    ```powershell
    $env:TMP = "E:\DBMS\build\wix_tmp"; $env:TEMP = $env:TMP
    cargo tauri build
    ```
- **A standalone sidecar run locks the file the next build writes.** The
  build script's own closing advice is to run the sidecar directly to check
  it — and if you leave it running, the next build dies with
  `PermissionError: [WinError 5] Access is denied` on the output `.exe`, which
  looks like a toolchain or antivirus problem and is neither. Worse, the
  process is easy to miss: launched standalone it is named
  `framecheck-backend-x86_64-pc-windows-msvc`, so `Get-Process
  framecheck-backend` does **not** find it (the desktop app stages a copy
  under the shorter name, which is what you see when the app spawns it). Use
  a match, not an exact name:
  ```powershell
  Get-CimInstance Win32_Process | Where-Object { $_.Name -match "framecheck" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
  ```
- **Sign-in.** `uvicorn api.main:app` requires a Google sign-in by default and
  fails closed. The desktop app does not: the shell passes
  `FRAMECHECK_AUTH_MODE=disabled` to its own sidecar, which is loopback-bound
  on an ephemeral port behind a per-launch token, so the gate would only be an
  unpassable modal in a shipped build. Set the variable explicitly if you do
  want sign-in in the app.

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

## One-time setup for the repo tooling (no GPU needed)

*(The app itself needs none of this — see **Setup: build it, then run it**
above. This is for running the pipeline, the scripts and the tests from a
checkout.)*

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
job. `docs/benchmark_results.md` holds real CPU-vs-GPU numbers measured on the
GTX 1650 — Louvain 8×/34×/50× as the graph grows, but BFS is **5× slower** on
GPU at 36k edges and does not overtake the CPU until somewhere between 20,000
and 40,000. `scripts/benchmark_crossover.py` measures that crossover per
algorithm, and the dashboard's Benchmarks tab shows it. The two backends used
to disagree on PageRank; `scripts/pagerank_agreement.py` traced that to
NetworkX never converging (its tolerance is scaled by vertex count) and they
now agree on 100% of flags — the story is at the end of
`docs/benchmark_results.md`. `docs/GPU_SETUP.md` covers the WSL2 RAPIDS
install.

## The audit trail the database enforces (Feature 2)

FrameCheck's output is an audit: rows in `flag_audit` saying a flag either
could or could not have been manufactured by an outsider, and at what cost. A
verdict is only worth something if it can be shown not to have been changed
afterwards — and until migration 009, nothing enforced that. `flag_audit` was
append-only because the Python that writes it happens never to issue an
`UPDATE`.

`db/migrations/009_framecheck_audit_trail.sql` moves that guarantee into the
database. "Tamper-evident" here does not mean nobody can change a verdict — a
superuser can always change anything, and pretending otherwise would be the
dishonest version of this feature. It means **no change can happen without
leaving evidence**:

- **`UPDATE` on `flag_audit` is refused outright**, by trigger. There is no
  legitimate update: a corrected verdict is a new audit row, and the API
  already reports the latest one per flag.
- **Every audit row is hash-chained** to the one before it:
  `row_hash = sha256(prev_hash || canonical payload)`, computed by a
  `BEFORE INSERT` trigger using Postgres' built-in digest, no extension
  needed. Editing a row by any means — direct SQL, a restored backup, a
  hand-edited dump — breaks the chain at that row and every row after it, and
  `flag_audit_chain_verify()` names the first row that does not fit.
- **`DELETE` is refused by default**, and permitted only when a session says
  so explicitly (`SET LOCAL framecheck.allow_audit_delete = 'on'`), in which
  case the deletion is recorded in `flag_audit_tombstone` with who did it and
  why. This project's own tooling needs that door — `benchmark_audit.py`
  removes the rows it just created, `run_experiment.py` clears a run before
  re-running it — and a door with a log on it is worth more than a wall
  somebody has to tunnel under.

And because a verdict is a claim about a flag **at a moment**, `flag` is
system-versioned: every before-image lands in `flag_history` with the period
it was valid for (a `tstzrange`, with a GiST exclusion constraint making
overlapping versions unstorable), and time travel is a query:

```sql
SELECT * FROM flag_as_of('2026-10-11 14:30:00+00');   -- the table as it stood
SELECT * FROM flag_versions(1);                       -- one flag's full history
SELECT * FROM flag_audit_chain_verify();              -- does the chain still hold?
```

The Flags view shows the chain's state above the flag list, because a reader
needs to know whether the database still vouches for a verdict before reading
it, and shows a flag's version history next to its evidence when the row has
changed since it was raised.

Measured on 1,775 flags and 13 audit rows: chaining costs **0.054 ms per
inserted audit row** against 0.009 ms unchained (6.3x a very small number),
whole-chain verification **1.0 ms**, `flag_as_of(now())` **2.25 ms** against
0.14 ms for a plain count. The exclusion constraint's GiST index doubles as
the point-in-time index — a single flag's version at an instant comes back in
0.033 ms.

Applying it: the **Database** tab lists it as a pending migration like any
other. Existing audit rows are backfilled with hashes in id order, so an
established database ends up with a chain that verifies rather than one
permanently "broken at row 1" — which establishes the baseline from which
tampering becomes detectable, and claims nothing about what happened before.

## Setting the app up without a terminal (Feature 1)

The desktop app's **Database** tab does the whole setup itself, which is what
an installed copy has to do -- there is no `.env` to edit and no `psql` to
run. It covers:

- **Connection** -- host, port, database, user, password. The password goes
  into the OS keychain via a Tauri command and is pushed to the backend over
  loopback; it is never POSTed from the page and never read back.
- **Create database** and **Import schema** -- `db/schema.sql` plus every
  migration, applied in order and recorded in a `schema_migration` table.
  Measured on an empty database: 0.57 s for the schema and all seven
  migrations.
- **Adopt** -- for a database built by this project *before* migration
  tracking existed. It records the migrations as applied without running
  them, as a separate explicit action, because it is a claim about history
  that cannot be checked from inside the database.
- **Tables** -- every table with a row-count estimate and size, and an exact
  `count(*)` on demand. Estimates because counting every table of a real
  database (26 as of migration 009) took 6.85 s against 0.013 s for the
  estimate.
- **Import data** -- the bundled demo dataset, or any dataset file.
- **Pull live data from Etherscan** -- a crawl with a calls-per-second rate
  and a hard call budget, with progress and a Stop button that takes effect
  after the address in flight.

### Datasets

A dataset is one snapshot's slice of a database -- its edges, the wallets and
transactions behind them, and every analysis run over it:

```bash
python scripts/export_dataset.py --snapshot-id 1 --output datasets/snapshot1.sql.gz
```

Snapshot 1 comes to 443,963 rows in an 18.9 MB gzipped file, and imports in
about 9 seconds. The app ships with exactly that file, so a fresh install has
real flags, graphs and audit verdicts to look at before anyone has an
Etherscan key.

Two things a dataset deliberately leaves out: `app_user` (real people's
Google identities -- a dataset is for sharing, an account list is not) and
anything the framing simulator injected (`transaction.is_simulated`), since a
dataset is the one artefact that leaves the machine and those rows must never
be mistaken for real chain history.

Importing needs an empty database. FrameCheck will not delete existing rows
to make room -- create a new database in the Connection panel instead.

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

### Sharing a public demo link

For showing FrameCheck to someone outside the team -- a reviewer, faculty,
anyone without a login -- point a tunnel at the API and let it serve the
frontend too, in one read-only mode:

```bash
# 1. api/main.py now serves frontend/ itself (no second server) --
#    FRAMECHECK_PUBLIC_DEMO=1 disables sign-in and returns 403 on every
#    mutating route (anything under /db, /jobs, /ingest, /internal).
FRAMECHECK_API_MODE=live FRAMECHECK_PUBLIC_DEMO=1 uvicorn api.main:app --port 8000

# 2a. ngrok
ngrok http 8000

# 2b. or Cloudflare Tunnel (no account needed for a one-off link)
cloudflared tunnel --url http://localhost:8000
```

Either command prints a public `https://` URL -- send that, not a
`localhost` one. Because the frontend is served from the same origin as the
API (step 1), the dashboard's own API-base default resolves to that same
tunnel URL automatically; nobody needs to open Settings and type an address.

What the link can and can't do: every read-only view (wallets, clusters,
flags, the audit trail, benchmarks) works exactly as it does for you,
against the same live database. A small "read-only demo" badge appears next
to the health indicator, and every button that would create, import, or
change anything is disabled in the UI -- and refused by the server with a
403 regardless, even if someone calls the API directly instead of clicking
a button. Nothing in this mode writes to the database or changes a setting.

Stop sharing by killing the tunnel and the `uvicorn` process -- the link
stops working the moment either one does, and nothing persists server-side
from having shared it.

### Running the packaged desktop app against real data

The sidecar runs from the install directory, not the repo, so snapshot files
stored as relative paths cannot be found by guessing. Point it at the data:

```
FRAMECHECK_DATA_DIR=E:\DBMS      # the directory containing snapshots/
```

Without it, audits started from the app fail with a message naming the file
and this variable. Detectors are unaffected (they read from Postgres).

### Sign-in: Google, or a local account

**Why two methods.** Google sign-in is the primary one: no password is stored
here at all, and identity is Google's verified `sub`. Local accounts exist for
what Google cannot cover — the first admin on a fresh deployment before any
OAuth client is configured, a reviewer without an allowlisted Google account,
an offline demo machine. They are username + password, hashed with **bcrypt
(cost 12)** in `app_user.password_hash`, which no endpoint ever returns;
`POST /auth/local/login` returns the same session as Google sign-in, with one
generic error for every failure and at most 5 attempts a minute per address
and per username. Create or reset one with
`python scripts/create_admin.py --username <name> --email <email>` — it
prompts for the password with `getpass`, so it never appears as an argument or
in shell history. Change your own with `POST /auth/local/password` (current
password required, new one at least 12 characters).

**`uvicorn api.main:app` requires a sign-in by default.** `/health`
and `/benchmarks` stay public; everything else answers
`401 {"detail": "sign-in required"}` without a session. **The desktop app
does not** — it passes `FRAMECHECK_AUTH_MODE=disabled` to its own sidecar,
which is loopback-bound on an ephemeral port behind a per-launch token (see
`desired_auth_mode` in `src-tauri/src/backend.rs`); a shipped build has no
OAuth client, so the gate would be a wall with nothing behind it. For local
work without an OAuth client:

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

### Multiview (comparing clusters)

Opens up to 8 cluster graphs side by side in their own window — a separate
Tauri window in the desktop app, a pop-up in the browser. Two ways in, both
on the **Clusters** view: tick clusters (the 9th is blocked with a message),
or press **Top 8 flagged** for the clusters holding the most flagged wallets.

Nodes are coloured by verdict (frameable / robust / flagged-not-audited /
unflagged); each panel pans and zooms on its own and can be expanded to fill
the window, which refetches at a higher node cap.

Panels are capped at 8 because each one holds its own WebGL2 context and
browsers kill the oldest once they run out. Big clusters are truncated by
expanding outward from the highest-PageRank member rather than taking the
top N — the top N of a 26,594-member cluster is a scatter of disconnected
nodes, not a picture of a cluster. Every panel states how many of the
cluster's wallets it is showing.

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

**What the app does with a saved key.** Rust owns the key, the backend
receives it at spawn, and it can be hot-swapped without a restart. Two
endpoints consume it:

- `GET /ingest/plan` reports `api_key_set` plus the rate the app will crawl
  at. The rate is labelled `"configured"`, not detected -- Etherscan only
  reports an allowance for paid keys, so claiming otherwise would be a guess
  dressed as a fact.
- `POST /ingest/start` passes it into the crawl job (`api/jobs.py` builds the
  `EtherscanClient` from it). Without a key it refuses with
  `409 no Etherscan API key is set`; in demo mode it refuses with
  `409 ingestion needs live mode`, since there is no demo-mode equivalent of
  writing real wallets to a database.

Mock vs. live is a separate axis, set by `FRAMECHECK_API_MODE` or by the
**Use live data** toggle in the Database view. So a saved key does nothing on
its own in demo mode — switch to live data, then crawl.

*(This paragraph used to say the key was read by nothing at all, which was
true when the handoff was built and stopped being true when the Database
view's Etherscan ingestion landed.)*

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
               + orm/ (SQLAlchemy models, engine, session -- data access only, never DDL)
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
docs/          ER diagram (DBML -> SVG/PNG), DATA_DICTIONARY.md, API contract, benchmark_results.md, FRAMECHECK.md,
               and the full project bible
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
