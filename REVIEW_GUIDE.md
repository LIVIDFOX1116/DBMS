# REVIEW_GUIDE.md

Reference notes for the project review.

## Database: Location, Purpose & Failure Recovery

Every fact below was verified directly against this project on 2026-10-23 --
nothing here is assumed. The one scenario that could not be demonstrated
exactly as specified is noted inline, with what was done instead and why.

### 1. Where is the database?

**Say:** "Postgres runs natively on this Windows machine -- not Docker, not
WSL -- as the Windows service `postgresql-x64-17`, PostgreSQL version 17.11.
It listens on localhost, port 5432. There's one database, called
`framecheck`. Inside it, the real project data lives in the `public` schema
-- that's 26 tables. For today's demo there's a second, completely separate
schema called `demo`, used only for the schema-management walkthrough, so
the two can never collide."

- **Physical data files**: `SHOW data_directory;` returns
  `C:/Program Files/PostgreSQL/17/data` -- run live with:
  ```sql
  SHOW data_directory;
  ```
- **Connection config file**: `.env` at the repo root holds `PGHOST`,
  `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`. `ingestion/config.py`
  loads it (`load_dotenv()`) into a `PG_DSN` dict at import time -- every
  script in `graph/`, `scripts/`, and `ingestion/` connects with
  `psycopg2.connect(**config.PG_DSN)`.
- **How the running app connects** (two separate paths, both real):
  - The dashboard's live API traffic goes through a connection **pool**:
    `api/main.py`'s `_get_pool()` / `get_conn()`
    (`psycopg2.pool.ThreadedConnectionPool`, 1-10 connections), created lazily
    on the first live-mode request.
  - The **Database tab**'s own admin actions (status, create, import, adopt)
    use a separate one-off connection function, `api/dbsetup.py`'s
    `connect()` / `current_dsn()` -- `current_dsn()` returns whatever the UI
    saved at runtime, falling back to `.env` if nothing was saved.
- **Live demo commands** (run these in front of the reviewer):
  ```bash
  # databases on the server
  "C:\Program Files\PostgreSQL\17\bin\psql.exe" -h 127.0.0.1 -U postgres -l

  # schemas inside framecheck (real `public` vs the demo's isolated `demo`)
  "C:\Program Files\PostgreSQL\17\bin\psql.exe" -h 127.0.0.1 -U postgres -d framecheck -c "\dn"

  # tables + row counts, the real project data
  "C:\Program Files\PostgreSQL\17\bin\psql.exe" -h 127.0.0.1 -U postgres -d framecheck -c "\dt+ public.*"

  # the same thing the Database tab itself shows -- open it in the app
  ```
  Real output, captured verbatim:
  ```
  public schema table count: 26
  public.wallet rows: 52581
  ```

### 2. How is it useful to the project?

**Say:** "Postgres is the backbone the whole pipeline writes to and reads
from. Ingestion pulls raw wallets, blocks, and transactions from Etherscan
and writes them here. The analysis stage reads a snapshot of that graph,
runs PageRank, Louvain, and BFS, and writes the results -- metrics, clusters,
alerts -- back into Postgres. FrameCheck's audit layer then reads that same
graph structure to decide whether each flag could have been faked by an
outsider, and writes its verdicts here too, with a tamper-evident hash chain
the database itself enforces. The API reads all of this to serve the
dashboard. Nothing in live mode works without it -- only the offline demo
mode, which reads a static JSON file instead, keeps working with no database
at all."

**The 26 tables, grouped by purpose, one line each** (confirmed against
`db/schema.sql` and `db/migrations/002..009`):

*Chain data ingested from Etherscan (8 tables)*
| Table | What it holds |
|---|---|
| `wallet` | one row per address seen, with running balance/sent/received totals and a risk score |
| `block` | one row per Ethereum block (hash, timestamp, gas, proposer) |
| `transaction` | every signed transaction, who sent it, who received it, value in wei |
| `token` | ERC-20 contracts seen (symbol, name, decimals) |
| `token_transfer` | individual ERC-20 Transfer events, linked back to the transaction that carried them |
| `label` | named tags (exchange, mixer, sanctioned, etc.) that can be attached to a wallet |
| `wallet_label` | which wallet has which label, who tagged it, how confident |
| `analyst` | who is allowed to review/tag things (seeded with one system user; no real auth gate here) |

*Graph snapshots and algorithm output (8 tables)*
| Table | What it holds |
|---|---|
| `graph_snapshot` | one row per exported slice of the graph (Parquet file, block range, vertex/edge counts) |
| `snapshot_edge` | the aggregated wallet-to-wallet edges that make up one snapshot |
| `snapshot_edge_tx` | bridge table: which individual transactions fed into one aggregated edge |
| `algorithm` | the 3 algorithms this project runs (pagerank, louvain, bfs) and their parameters |
| `analysis_run` | one row per algorithm run (which snapshot, which algorithm, GPU or CPU, how long it took) |
| `vertex_metric` | per-wallet results of a run (e.g. its PageRank score) |
| `cluster` | one row per Louvain community found in a run (size, modularity, risk type) |
| `cluster_member` | which wallets belong to which cluster, and how central they are in it |

*Review and visualization (2 tables)*
| Table | What it holds |
|---|---|
| `alert` | plausible suspicious patterns (e.g. Sybil clusters) flagged for human review |
| `visualization` | saved renders of a run's graph layout |

*FrameCheck's audit layer (6 tables, added by migration 002 and 009)*
| Table | What it holds |
|---|---|
| `framing_scenario` | a synthetic framing attack the simulator injected, for testing the audit engine itself |
| `flag` | one row per signal a detector raised (which wallet, which detector, score vs. threshold) |
| `flag_evidence` | the exact transactions a flag's case rests on, each marked signed-by-the-wallet or unsolicited |
| `flag_audit` | the verdict for a flag -- frameable or robust, and at what cost, append-only |
| `flag_audit_tombstone` | a record of any audit row ever deleted, who did it and why (the chain's "logged door") |
| `flag_history` | every prior version of a `flag` row, with the time range it was valid for (time-travel queries) |

*Accounts and plumbing (2 tables)*
| Table | What it holds |
|---|---|
| `app_user` | Google sign-in accounts (keyed on Google's stable `sub`, not email) |
| `schema_migration` | which of the 8 migration files have been applied to this database |

**Why Postgres, not flat files/JSON/SQLite** -- based on what this project
actually uses, not a generic argument:
- **Real foreign keys everywhere** -- every table above references another
  by id (`transaction.from_wallet_id REFERENCES wallet`, `flag.wallet_id
  REFERENCES wallet`, etc.). A flat file can't refuse to write an orphaned
  reference; Postgres does, by construction.
- **Constraints that are actually load-bearing**: `flag_audit.verdict CHECK
  (verdict IN ('frameable','robust'))`, `token_transfer` has a
  `NUMERIC(78,0)` column specifically because an ERC-20 raw amount can
  exceed what a 64-bit float or JSON number can hold exactly.
- **Triggers enforcing the audit trail's guarantees**: `flag_audit` refuses
  `UPDATE` outright and hash-chains every insert to the one before it (`db/
  migrations/009_framecheck_audit_trail.sql`) -- this is a database-level
  guarantee, not something application code promises and might forget.
- **A GiST exclusion constraint** on `flag_history` makes two overlapping
  version periods for the same flag physically unstorable -- the kind of
  invariant only a real RDBMS enforces at write time.
- **Materialized views** (`mv_cluster_flag_stats`) for a fast aggregate the
  API needs on every page load, refreshed on demand rather than recomputed
  per request.
- **Real concurrent access**: the dashboard's API pool serves many
  simultaneous requests from one process; a single JSON file has no
  equivalent of row-level locking or MVCC.
- **Migrations as a first-class, trackable concept** -- `db/schema.sql` +
  `db/migrations/002..009_*.sql`, with `schema_migration` recording exactly
  which have run. A directory of JSON files has no analogous "what version
  of the shape is this" story.

**What stops working without it**: every live-mode feature -- ingestion,
the analysis pipeline, all five FrameCheck detectors, the audit engine, the
Database tab, sign-in (no `app_user` table), and every dashboard view except
when switched to demo/mock mode, which reads `api/mock_data.json` and needs
no database at all by design.

### 3. What happens if the database is deleted?

Every case below was actually tested and the real output captured --
against the isolated `demo` schema for case 1, and against a **temporary
restored copy** of the real database for cases 2 and 3, never the real data
(confirmed untouched before and after: `public.wallet` stayed at 52,581 rows
and the `public` schema stayed at 26 tables throughout). Case 4 could not be
demonstrated by literally stopping the Windows service -- this shell does
not have the OS permission to control Windows services -- so it was
demonstrated the honest alternative way: connecting to a port nothing is
listening on, which produces the identical OS-level "connection refused"
error a stopped service would. The resulting exception class and the
app's handling of it are exactly the same either way.

**Case 1 -- Demo schema dropped.** Safe to actually do; it's the one
scenario that touches nothing but the isolated demo data.
```bash
# dropped it (real command actually run):
DROP SCHEMA IF EXISTS demo CASCADE;
# -> demo_schemas/demo_db.py list: "`demo` schema is empty or does not exist yet -- run `seed` first."
python demo_schemas/reset_demo.py
# -> recovered in under 2 seconds, all 10 tables back with the same row counts
```
**What's lost**: nothing real -- this is demo data by design.
**Recovery**: `python demo_schemas/reset_demo.py`.

**Case 2 -- One real table dropped** (tested: dropped `cluster` on a
restored temporary copy, `framecheck_restore_test`, never the real
database).
- The Database tab's own status check did **not** notice: `schema_state()`
  still reported `schema_applied: True` -- it only checks for one sentinel
  table, `wallet`, not every table. The missing table only breaks whichever
  feature actually queries it.
- Querying the dropped table directly raised:
  ```
  psycopg2.errors.UndefinedTable: relation "cluster" does not exist
  ```
  This is a `ProgrammingError`, **not** an `OperationalError` -- and
  `api/main.py`'s database-down handler (`_call()`, line ~119) only catches
  `(OperationalError, InterfaceError)`. So this does **not** show the
  friendly "database unavailable" banner; it falls through to the generic
  handler and returns a plain `500 {"detail": "internal server error"}` on
  whichever endpoint needs that table. (This is exactly the shape of a real
  bug fixed earlier in this project's life, when `mv_cluster_flag_stats`
  was missing and `/clusters/top-flagged` returned the same kind of 500.)
- **What's lost**: every row in that table, plus anything that referenced
  it by foreign key.
- **Recovery**: `python db/init_db.py` re-creates the missing table
  definition (every statement is `IF NOT EXISTS`, safe to re-run) -- but
  that only brings back the empty table, not its rows. The actual data can
  only come back from a backup restore (below) or a re-import of a
  previously exported dataset.

**Case 3 -- The whole FrameCheck database dropped** (tested: against the
temporary copy's own name resolving to nothing, never the real `framecheck`
database).
- `api/dbsetup.py`'s `server_probe()` correctly reported
  `reachable: True, database_exists: False` -- the Database tab shows
  "Server: reachable" / "Database: does not exist yet" with a **Create
  database** button.
- A direct connection attempt raised:
  ```
  psycopg2.OperationalError: connection to server at "127.0.0.1", port 5432
  failed: FATAL: database "..." does not exist
  ```
  This **is** an `OperationalError`, so live-mode API requests correctly
  return `503 {"detail": "database unavailable"}` (`api/main.py` line
  ~127-129), and the dashboard's health badge flips to "API unreachable"
  with the `fcShowApiDown` overlay (`frontend/dashboard.html` line ~1030).
- **What's lost**: everything -- schema and all data.
- **Recovery**:
  ```bash
  createdb -h 127.0.0.1 -U postgres framecheck    # recreate the empty database
  python db/init_db.py                             # re-apply schema.sql + all 8 migrations
  # then restore data -- see the one-line restore command below
  ```

**Case 4 -- Postgres not running** (tested via an unreachable port, not an
actual service stop -- see the note above).
- Connecting raised:
  ```
  psycopg2.OperationalError: connection to server at "127.0.0.1", port 5433
  failed: Connection refused (0x0000274D/10061)
  Is the server running on that host and accepting TCP/IP connections?
  ```
- Same `OperationalError` path as Case 3: the Database tab shows "Server:
  unreachable" with the raw error text, and explicitly says **"Demo data
  keeps working meanwhile"** (`frontend/database.js` line ~97) -- the app
  does not crash, it just can't show live data.
- **What's lost**: nothing -- no data is touched by the service being down.
- **Recovery**: start the `postgresql-x64-17` Windows service again
  (`services.msc`, or an elevated `Start-Service postgresql-x64-17`). The
  app's connection pool rebuilds itself automatically on the next request --
  no restart of the app needed.

**Said plainly: what genuinely cannot be recovered without a backup.**
The *schema* -- every table definition, constraint, and index -- costs
nothing to recreate: `python db/init_db.py` rebuilds all 26 tables from
`db/schema.sql` and the 8 migration files, idempotently, any time. But the
*rows* -- every ingested wallet and transaction, every analysis run, every
flag and its audit verdict -- exist nowhere else. Re-running the Etherscan
pipeline from scratch would eventually refill the chain data, but it is
slow, rate-limited, and would **not** reproduce the same audit trail: the
hash chain in `flag_audit` is defined by insertion order and each row's
link to the one before it, so a fresh run starts a fresh, different chain.
**The only way to get the exact same data and the exact same audit history
back is restoring a backup.**

### Backup and restore

A real, timestamped backup of the live database was taken and its restore
path verified end to end -- `pg_dump` into a `.dump` file, restored into a
**separate temporary database** (`framecheck_restore_test`), row counts
compared against the original, then the temporary database dropped. All real
output:

```
pg_dump: ... (44.6 MB custom-format dump)
backups/framecheck_backup_20261023T082430.dump created

restored into framecheck_restore_test -- verified:
 wallets | transactions | clusters | flags
---------+--------------+----------+-------
   52581 |       117705 |    70254 | 23143
(matches the live database)

dropdb framecheck_restore_test -- temp database removed
```

`backups/` is gitignored (it holds real ingested data) -- the backup file
stays local only.

**Take a fresh backup:**
```bash
"C:\Program Files\PostgreSQL\17\bin\pg_dump.exe" -h 127.0.0.1 -U postgres -d framecheck -Fc -f "backups\framecheck_backup_$(date +%Y%m%dT%H%M%S).dump"
```

**The one-line restore command** (into an existing, empty-or-damaged
`framecheck` database -- run `createdb` first if the database itself is
gone, per Case 3 above):
```bash
"C:\Program Files\PostgreSQL\17\bin\pg_restore.exe" -h 127.0.0.1 -U postgres -d framecheck --clean --if-exists --no-owner --no-privileges backups\framecheck_backup_20261023T082430.dump
```

## Live Demo: Schema Management

### Why two mechanisms

FrameCheck's own `/db/*` API and Database tab are hard-wired to **one**
schema -- FrameCheck's own (wallet/transaction/cluster/flag/etc., defined in
`db/schema.sql` + `db/migrations/*.sql`). There is no generic "upload any
schema" feature anywhere in the app -- confirmed by reading every `/db/*`
route in `api/main.py` and all of `api/dbsetup.py`. So this demo uses two
things side by side:

1. **`demo_schemas/`** -- a new, standalone CLI (`demo_schemas/demo_db.py`)
   built for this review. It manages illustrative schemas (student
   management, library, e-commerce, and a live-uploaded hospital schema) in
   an isolated Postgres schema namespace called `demo`, inside the same
   `framecheck` database the app already uses. `demo` is completely separate
   from FrameCheck's own tables (all in `public`) -- dropping it cascades
   only within itself and cannot touch a single wallet/transaction/cluster
   row. Zero core project code was changed to build this.
2. **FrameCheck's real Database tab** (`frontend/dashboard.html` →
   Database) -- the project's actual, already-implemented dataset-import
   feature, for showing reviewers the project's own data pipeline if asked.

### Commands

```bash
# Load all 3 base schemas (student management, library, e-commerce) + sample data
python demo_schemas/seed_demo.py

# Wipe demo schema and reseed from scratch -- instant recovery if anything breaks live
python demo_schemas/reset_demo.py

# List every table currently in the demo namespace, with row counts
python demo_schemas/demo_db.py list

# Run a .sql file against the demo namespace (DDL or DML) -- used for the
# live upload, each modification, and each invalid-schema example below
python demo_schemas/demo_db.py load <file.sql>

# Run a .sql file's SELECT(s) and print each result set
python demo_schemas/demo_db.py query <file.sql>
```

All of these were actually run while building this guide; the outputs below
are real, not hypothetical.

### Step-by-step order

**0. Before reviewers arrive:** run `python demo_schemas/reset_demo.py` once
so you start from a known-clean state (3 base schemas, nothing else).

**1. Show the preloaded, related schemas.**
```bash
python demo_schemas/demo_db.py list
```
Expected: 10 tables -- `students`, `courses`, `enrollments`, `members`,
`books`, `loans`, `customers`, `products`, `orders`, `order_items` -- with
row counts (8, 6, 11, 7, 8, 9, 8, 8, 10, 13).

Then:
```bash
python demo_schemas/demo_db.py query demo_schemas/queries/base_schema_queries.sql
```
Expected: three joined result sets -- each student's courses and grades,
books currently on loan, and per-order totals (customer → orders →
order_items → products). Real multi-table joins, not empty tables.

**Say:** "Each of these is a real schema with primary keys, foreign keys,
and CHECK constraints -- for example `demo.enrollments` has a foreign key to
both `students` and `courses`, plus a UNIQUE constraint so the same student
can't be enrolled in the same course twice in one semester. The files are in
`demo_schemas/01_student_management.sql`, `02_library.sql`,
`03_ecommerce.sql`."

**2. Upload a new schema live.**
```bash
python demo_schemas/demo_db.py load demo_schemas/live/00_hospital_new.sql
python demo_schemas/demo_db.py list
```
Expected: `OK: 00_hospital_new.sql applied successfully.`, then the table
list grows from 10 to 13 (`doctors`, `patients`, `appointments` added, with
4/6/8 rows).

**Say:** "This is a schema that wasn't loaded a second ago -- patients,
doctors, appointments, with its own foreign keys. The tool just ran the SQL
file directly against Postgres; there's no custom parser, Postgres is the
one validating it."

**3. Modify an existing schema, one step at a time.** Run the "before" query
first, then each step, showing the "after" query after each one:
```bash
python demo_schemas/demo_db.py query demo_schemas/live/queries/inspect_schema.sql   # before

python demo_schemas/demo_db.py load demo_schemas/live/modifications/01_add_column.sql
python demo_schemas/demo_db.py query demo_schemas/live/queries/inspect_schema.sql   # phone column appears

python demo_schemas/demo_db.py load demo_schemas/live/modifications/02_rename_column.sql
python demo_schemas/demo_db.py query demo_schemas/live/queries/inspect_schema.sql   # phone -> phone_number

python demo_schemas/demo_db.py load demo_schemas/live/modifications/03_add_constraint.sql
# (this one's proof is a constraint, not a column -- see note below)

python demo_schemas/demo_db.py load demo_schemas/live/modifications/04_add_table.sql
python demo_schemas/demo_db.py query demo_schemas/live/queries/inspect_schema.sql   # product_reviews appears, 14 tables

python demo_schemas/demo_db.py load demo_schemas/live/modifications/05_drop_table.sql
python demo_schemas/demo_db.py query demo_schemas/live/queries/inspect_schema.sql   # back to 13 tables
```
Expected output at each step (all actually observed while building this):
- add column: `customers` columns go from 4 to 5 (`phone` appears, nullable)
- rename: `phone` becomes `phone_number` in the same list
- add constraint: no column changes, but `uq_customer_phone` now exists on
  `demo.customers` (if asked to prove it: a 7th table-driven query --
  `SELECT conname FROM pg_constraint WHERE conrelid = 'demo.customers'::regclass`
  -- shows it, or just try inserting two customers with the same phone
  number live and watch it get rejected)
- add table: table count 13 → 14, `product_reviews` appears
- drop table: table count 14 → 13, `product_reviews` gone

**Say:** "Each of these is one plain ALTER/CREATE/DROP statement -- Postgres
enforces the constraint, the rename, the table boundaries. Nothing here is
FrameCheck-specific; it's the same mechanism `db/migrations/*.sql` uses for
the project's own schema, just pointed at a throwaway namespace for the
demo."

**4. Show invalid schemas being rejected.**
```bash
python demo_schemas/demo_db.py load demo_schemas/live/invalid/01_bad_type.sql
python demo_schemas/demo_db.py load demo_schemas/live/invalid/02_broken_fk.sql
python demo_schemas/demo_db.py load demo_schemas/live/invalid/03_duplicate_table.sql
```
Expected (real output, captured verbatim):
```
REJECTED: 01_bad_type.sql
  type "notarealtype" does not exist
LINE 6:     amount NOTAREALTYPE

REJECTED: 02_broken_fk.sql
  relation "demo.nonexistent_table" does not exist

REJECTED: 03_duplicate_table.sql
  relation "students" already exists
```
Each exits with code 1 and writes nothing -- confirmed with
`python demo_schemas/demo_db.py list` straight after all three: table count
and every row count unchanged, no `invalid_bad_type`/`invalid_broken_fk`
tables leaked.

**Say:** "Nothing special was written to catch these -- that's Postgres's
own type system, foreign key, and name-collision checks surfacing as plain
errors. The tool runs each file inside a transaction, so a rejected file
leaves zero trace, not a half-created table."

**5. (If a reviewer asks to see the project's own schema/data pipeline)**
Open the Database tab in the running app. Point out: "Schema: applied · 26
tables", "Migrations: up to date · 8 applied" at the top, then click
**Import the demo dataset** -- since this database already has data loaded,
it will show (in the UI, inline, not a crash):
> "this database already holds data: alert (197 rows), analysis_run (38
> rows), block (104,775 rows), cluster (70,254 rows). Importing would
> collide with it, and FrameCheck will not delete your rows to make room --
> import into an empty database (create a new one in Connection above)
> instead."

**Say:** "This is the project's real schema and real dataset-import
pipeline -- `db/schema.sql`, `db/migrations/`, and `api/dbsetup.py`'s
`import_dataset()`. It refuses to import on top of existing data rather than
silently merging or deleting it; the only way to see the success path live
would be against a fresh, empty database."

### Which file does what

| Concern | File |
|---|---|
| Demo schemas' table definitions + sample data | `demo_schemas/01_student_management.sql`, `02_library.sql`, `03_ecommerce.sql` |
| Live-upload/modification/invalid demo files | `demo_schemas/live/**/*.sql` |
| Demo CLI (seed/reset/list/load/query) | `demo_schemas/demo_db.py` (thin wrappers: `seed_demo.py`, `reset_demo.py`) |
| FrameCheck's own base schema (DDL) | `db/schema.sql` |
| FrameCheck's own incremental schema changes | `db/migrations/002..009_*.sql` |
| Terminal-based schema setup (no app needed) | `db/init_db.py` |
| App-level schema/dataset logic (Database tab's backend) | `api/dbsetup.py` -- `apply_schema()`, `schema_state()`, `mark_existing_as_applied()`, `import_dataset()` |
| HTTP endpoints exposing the above | `api/main.py`, lines ~1527-1765 (`/db/status`, `/db/schema`, `/db/adopt`, `/db/tables`, `/db/datasets`, `/db/import`, `/db/mode`) |
| Database tab UI | `frontend/dashboard.html` (`#view-database`) + `frontend/database.js` |

Validation itself is never reimplemented: every CREATE/ALTER/DROP in this
demo is plain SQL that Postgres parses and type/constraint-checks. The one
app-level validation layer that *is* custom is `import_dataset()`'s dataset
file format check (`framecheck-dataset v1` marker) and its table whitelist
(`DATASET_TABLES` in `api/dbsetup.py`) -- that's specific to FrameCheck's
own dataset files, not a general schema validator.

### If a reviewer asks you to create a custom schema on the spot

1. Say what you're about to do: "I'll write a plain CREATE TABLE and run it
   against an isolated namespace, so if anything's wrong I can reset in one
   command."
2. Open a new file anywhere convenient (or `demo_schemas/live/scratch.sql`)
   and write ordinary DDL, **always prefixing table names with `demo.`**
   (e.g. `CREATE TABLE demo.whatever_they_ask_for (...)`) -- this keeps it in
   the isolated namespace, not FrameCheck's real tables.
3. Run it: `python demo_schemas/demo_db.py load demo_schemas/live/scratch.sql`
4. If they want constraints, show one or two (`CHECK`, `UNIQUE`, a FK to an
   existing demo table like `demo.customers`) -- the same syntax used
   throughout this demo.
5. Safety net: the tool refuses to run any file that mentions `public` or a
   FrameCheck table name (`wallet`, `transaction`, `cluster`, etc.) unless
   you explicitly pass `--allow-public` -- so a typo during a live,
   improvised query cannot reach the real demo data. If it refuses something
   that genuinely is safe, that flag overrides it.
6. If it breaks or gets confusing, say so and run
   `python demo_schemas/reset_demo.py` -- it takes under a few seconds and
   returns to the exact 3-schema baseline, with zero impact on FrameCheck's
   own 52,581-wallet dataset sitting right next to it.
