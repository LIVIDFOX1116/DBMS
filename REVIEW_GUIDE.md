# REVIEW_GUIDE.md

Reference notes for the project review. Currently just the schema-management
demo; add other sections here as needed.

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
