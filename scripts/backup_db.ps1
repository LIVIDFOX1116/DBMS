# Backup with PostgreSQL's native tools -- see scripts/dbtools.py and docs/BACKUP_RESTORE.md.
# Credentials come from PG* / .env only; never pass a password as an argument.
#   powershell -ExecutionPolicy Bypass -File scripts\backup_db.ps1 [--dbname NAME]
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$Python = if (Test-Path ".venv-win\Scripts\python.exe") { ".venv-win\Scripts\python.exe" } else { "python" }
& $Python scripts\dbtools.py backup @args
exit $LASTEXITCODE
