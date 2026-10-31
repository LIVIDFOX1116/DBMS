#!/usr/bin/env bash
# Restore with PostgreSQL's native tools -- see scripts/dbtools.py and docs/BACKUP_RESTORE.md.
# Credentials come from PG* / .env only; never pass a password as an argument.
#   bash scripts/restore_db.sh backups/<file>.dump [--target NAME]
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-$( [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3 )}"
exec "$PYTHON" scripts/dbtools.py restore "$@"
