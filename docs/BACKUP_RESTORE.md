# Backup and restore (pg_dump / pg_restore)

FrameCheck is backed up with PostgreSQL's own tools only: `pg_dump` in custom format
(`-Fc`: compressed, and restorable table by table) and `pg_restore`. The logic is in
`scripts/dbtools.py`; `scripts/backup_db.{ps1,sh}` and `scripts/restore_db.{ps1,sh}` are
thin wrappers around it.

**Credentials** come from `PG*` in the environment or `.env` (the same settings every
other tool here uses), or from your own `.pgpass`. They reach `pg_dump`/`pg_restore`
only through the child process's environment — never on the command line, where any
local process can read them — and `-w` makes the tools fail instead of prompting.
`tests/test_dbtools.py` checks exactly that, offline.

## When to back up

- **Before every migration.** `db/migrations/010_single_users_table_and_roles.sql` drops
  the `analyst` table; take a backup first, every time, on every database it touches.
- Before importing a dataset or running `scripts/simulate_framing.py --cleanup`.
- Before a review or demo, so the demo database can be put back exactly.

## Back up

```powershell
powershell -ExecutionPolicy Bypass -File scripts\backup_db.ps1          # PGDATABASE
powershell -ExecutionPolicy Bypass -File scripts\backup_db.ps1 --dbname framecheck
```
```bash
bash scripts/backup_db.sh
```

Writes `backups/<db>_<UTC timestamp>.dump` (`backups/` is gitignored — dumps hold real
data) and prints its size and SHA-256. Real output, 2026-10-31:

```
pg_dump -Fc framecheck -> C:\VIT\DBMS_Project\DBMS\backups\framecheck_20261031T150554Z.dump
size   19,113,120 bytes
sha256 7eec589c96bde5b21ef449ca96f1e8e29fef59949ec372b26586b21384aa94b2
```

## Restore — into a new database, then verify

```powershell
powershell -ExecutionPolicy Bypass -File scripts\restore_db.ps1 backups\framecheck_20261031T150554Z.dump
```
```bash
bash scripts/restore_db.sh backups/framecheck_20261031T150554Z.dump
```

By default the dump goes into a **new** database, `framecheck_restore_check`; if that
already exists the script refuses and changes nothing. Restoring into a database that
already exists — including the real one — needs `--target <name>` **and** typing that
name back at the prompt. After restoring it compares exact row counts table by table
against the source (`--source`, default `PGDATABASE`) and runs
`flag_audit_chain_verify()` on the copy. Real output, 2026-10-31:

```
created database framecheck_restore_check
sha256 7eec589c96bde5b21ef449ca96f1e8e29fef59949ec372b26586b21384aa94b2  backups\framecheck_20261031T150554Z.dump
pg_restore -> framecheck_restore_check

Row counts, framecheck (source) vs framecheck_restore_check (restored):
  ok       alert                            34         34
  ok       algorithm                         8          8
  ok       analysis_run                      8          8
  ok       analyst                           1          1
  ok       app_user                          0          0
  ok       block                         90410      90410
  ok       cluster                        9558       9558
  ok       cluster_member                19133      19133
  ok       flag                           1775       1775
  ok       flag_audit                       13         13
  ...
  ok       wallet                        25462      25462
  ok       wallet_label                      0          0
audit chain: 13 rows checked, intact=True

RESTORE CHECK: PASS
```

The restore-check database is yours to drop once you have looked at it
(`DROP DATABASE framecheck_restore_check;`) — the scripts never drop anything.

## With docker compose

The `db` service has the same tools inside the container:

```bash
# back up
docker compose exec -T db pg_dump -U postgres -Fc framecheck > backups/framecheck_compose.dump
# restore into a new database and check it
docker compose exec -T db createdb -U postgres framecheck_restore_check
docker compose exec -T db pg_restore -U postgres --no-owner --exit-on-error -d framecheck_restore_check < backups/framecheck_compose.dump
docker compose exec -T db psql -U postgres -d framecheck_restore_check -c "SELECT * FROM flag_audit_chain_verify()"
```

(`-T` disables the pseudo-terminal so the binary dump is not mangled by line-ending
translation.) Inside the container the password comes from the service's own
`POSTGRES_PASSWORD`, so it is not typed anywhere either.

## Demo for the review

1. `scripts\backup_db.ps1` — show the file, its size and SHA-256.
2. `scripts\restore_db.ps1 <that file>` — show every table `ok` and `RESTORE CHECK: PASS`.
3. Point out that the audit chain verifies in the copy: the hash chain survived the round
   trip, so a restored database is provably the same audit history.
