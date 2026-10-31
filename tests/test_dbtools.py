"""scripts/dbtools.py -- offline: no pg_dump/pg_restore runs, no database.

What matters most here is argument safety: the password must reach the tools
only through their environment, never through argv (visible to every local
process), and database names must be checked before they are used.
"""
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from scripts import dbtools

DSN = {"host": "localhost", "port": "5432", "dbname": "framecheck", "user": "postgres",
       "password": "s3cret-pw"}


def test_backup_command_is_custom_format_and_never_prompts():
    cmd = dbtools.backup_command("pg_dump", "framecheck", Path("backups/x.dump"))
    assert cmd[:3] == ["pg_dump", "-w", "-Fc"]
    assert ["-d", "framecheck"] == cmd[3:5] and cmd[-2:] == ["-f", str(Path("backups/x.dump"))]


def test_restore_command_stops_on_first_error_and_drops_ownership():
    cmd = dbtools.restore_command("pg_restore", "framecheck_restore_check", Path("b.dump"))
    assert "-w" in cmd and "--no-owner" in cmd and "--exit-on-error" in cmd
    assert cmd[cmd.index("-d") + 1] == "framecheck_restore_check"


@pytest.mark.parametrize("build", [
    lambda: dbtools.backup_command("pg_dump", "framecheck", Path("o.dump")),
    lambda: dbtools.restore_command("pg_restore", "framecheck_restore_check", Path("o.dump")),
])
def test_no_password_on_any_command_line(build):
    argv = " ".join(build())
    assert "s3cret-pw" not in argv and "password" not in argv.lower()


def test_password_travels_only_in_the_child_environment():
    env = dbtools.tool_env(DSN, "framecheck")
    assert env["PGPASSWORD"] == "s3cret-pw"
    assert env["PGDATABASE"] == "framecheck" and env["PGUSER"] == "postgres"


def test_loopback_host_is_tried_as_ipv4_first():
    # `localhost` resolves to ::1 first on Windows and costs ~2 s per connection.
    assert dbtools.tool_env(DSN)["PGHOST"] == "127.0.0.1"
    assert dbtools.tool_env({**DSN, "host": "db.example.com"})["PGHOST"] == "db.example.com"


def test_no_password_means_no_pgpassword_so_pgpass_is_used(monkeypatch):
    monkeypatch.delenv("PGPASSWORD", raising=False)
    assert "PGPASSWORD" not in dbtools.tool_env({**DSN, "password": ""})


def test_backup_filename_is_utc_timestamped():
    when = datetime(2026, 11, 1, 9, 5, 7, tzinfo=timezone.utc)
    assert dbtools.backup_filename("framecheck", when) == "framecheck_20261101T090507Z.dump"


@pytest.mark.parametrize("name", ["framecheck; DROP DATABASE x", "a b", "", "x" * 64, "-d"])
def test_unsafe_database_names_are_refused(name):
    with pytest.raises(SystemExit):
        dbtools.check_identifier(name)


def test_overwrite_needs_the_exact_name_typed_back():
    assert dbtools.confirm_overwrite("framecheck", "framecheck\n")
    assert not dbtools.confirm_overwrite("framecheck", "yes")
    assert not dbtools.confirm_overwrite("framecheck", "Framecheck")


def test_restore_into_existing_default_target_is_refused_without_touching_anything(tmp_path):
    dump = tmp_path / "b.dump"
    dump.write_bytes(b"x")
    with mock.patch("api.dbsetup.current_dsn", return_value=DSN), \
         mock.patch.object(dbtools, "database_exists", return_value=True), \
         mock.patch.object(dbtools.subprocess, "run") as run:
        with pytest.raises(SystemExit, match="already exists"):
            dbtools.main(["restore", str(dump)])
    run.assert_not_called()


def test_restore_over_an_existing_target_stops_unless_confirmed(tmp_path, monkeypatch):
    dump = tmp_path / "b.dump"
    dump.write_bytes(b"x")
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    with mock.patch("api.dbsetup.current_dsn", return_value=DSN), \
         mock.patch.object(dbtools, "database_exists", return_value=True), \
         mock.patch.object(dbtools.subprocess, "run") as run:
        assert dbtools.main(["restore", str(dump), "--target", "framecheck"]) == 1
    run.assert_not_called()


def test_backup_runs_pg_dump_with_secrets_in_env_only(tmp_path):
    with mock.patch("api.dbsetup.current_dsn", return_value=DSN), \
         mock.patch.object(dbtools, "find_pg_tool", return_value="pg_dump"), \
         mock.patch.object(dbtools.subprocess, "run") as run, \
         mock.patch.object(dbtools, "sha256_of", return_value="0" * 64), \
         mock.patch.object(Path, "stat", return_value=mock.Mock(st_size=10)):
        dbtools.main(["backup", "--out-dir", str(tmp_path)])
    argv, kwargs = run.call_args.args[0], run.call_args.kwargs
    assert "s3cret-pw" not in " ".join(argv)
    assert kwargs["env"]["PGPASSWORD"] == "s3cret-pw" and kwargs["check"] is True
