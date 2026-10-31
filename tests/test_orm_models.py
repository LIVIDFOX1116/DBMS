"""The ORM models must match the DDL exactly -- checked offline.

db/schema.sql + db/migrations/*.sql are the single source of truth for the
schema; db/orm/models.py only describes it. Every model is compiled against
the PostgreSQL dialect (no database, no SQLite stand-in: JSONB, TSTZRANGE and
the triggers are PostgreSQL-only) and compared with tests/ddl_parse.py's
parse of the SQL files. A migration that changes a table without updating
its model -- or a model edited on its own -- fails here.
"""
import ast
import re
from pathlib import Path

import pytest
from sqlalchemy import DefaultClause, FetchedValue, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.orm.session import make_transient_to_detached
from sqlalchemy.schema import CreateTable

from db.orm import models
from tests.ddl_parse import REPO, normalise_type, parse_schema

DDL = parse_schema()
TABLES = models.Base.metadata.tables
DIALECT = postgresql.dialect()


def _type(column) -> str:
    return normalise_type(column.type.compile(dialect=DIALECT))


def _default_text(column) -> str | None:
    sd = column.server_default
    if isinstance(sd, DefaultClause):
        return str(sd.arg.text if hasattr(sd.arg, "text") else sd.arg).lower()
    return None


def test_every_table_is_mapped_and_nothing_else():
    assert len(TABLES) == 26
    assert sorted(TABLES) == sorted(DDL)


@pytest.mark.parametrize("name", sorted(DDL))
class TestModelMatchesDdl:
    def test_create_table_compiles(self, name):
        ddl = str(CreateTable(TABLES[name]).compile(dialect=DIALECT))
        assert f"CREATE TABLE {name}" in ddl or f'CREATE TABLE "{name}"' in ddl

    def test_columns_in_ddl_order(self, name):
        assert [c.name for c in TABLES[name].columns] == list(DDL[name].columns)

    def test_types(self, name):
        mismatched = {c.name: (_type(c), DDL[name].columns[c.name].type)
                      for c in TABLES[name].columns if _type(c) != DDL[name].columns[c.name].type}
        assert not mismatched, f"model type != DDL type: {mismatched}"

    def test_nullability(self, name):
        mismatched = [c.name for c in TABLES[name].columns
                      if c.nullable != DDL[name].columns[c.name].nullable]
        assert not mismatched, f"nullability differs from the DDL for {mismatched}"

    def test_primary_key(self, name):
        assert [c.name for c in TABLES[name].primary_key.columns] == DDL[name].primary_key

    def test_sequences_exactly_where_the_ddl_uses_serial(self, name):
        # The compiled CREATE TABLE renders SERIAL/BIGSERIAL for an
        # auto-incrementing key; a natural key (block_number, tx_hash) or a
        # composite key must not get one.
        compiled = str(CreateTable(TABLES[name]).compile(dialect=DIALECT))
        serial_cols = set(re.findall(r"^\s*(\w+) (?:BIG)?SERIAL\b", compiled, re.M))
        expected = {c.name for c in DDL[name].columns.values() if c.default == "autoincrement"}
        assert serial_cols == expected

    def test_foreign_keys(self, name):
        model_fks = {(fk.parent.name, fk.column.table.name, fk.column.name) for fk in TABLES[name].foreign_keys}
        ddl_fks = {(c.name, *c.references) for c in DDL[name].columns.values() if c.references}
        assert model_fks == ddl_fks

    def test_unique_constraints(self, name):
        table = TABLES[name]
        model = {(c.name,) for c in table.columns if c.unique}
        model |= {tuple(c.name for c in u.columns) for u in table.constraints if isinstance(u, UniqueConstraint)}
        ddl = {(c.name,) for c in DDL[name].columns.values() if c.unique}
        ddl |= set(DDL[name].unique)
        assert model == ddl

    def test_server_defaults_match(self, name):
        for col in TABLES[name].columns:
            ddl_default = DDL[name].columns[col.name].default
            ddl_default = None if ddl_default == "autoincrement" else ddl_default
            assert _default_text(col) == (ddl_default.lower() if ddl_default else None), col.name


class TestTriggerOwnedColumnsAndTables:
    def test_hash_columns_are_server_generated(self):
        # The flag_audit_chain trigger computes these; the ORM must never send
        # them, and must read back what the trigger wrote.
        table = TABLES["flag_audit"]
        for col in ("prev_hash", "row_hash"):
            assert isinstance(table.c[col].server_default, FetchedValue)
            assert not isinstance(table.c[col].server_default, DefaultClause)
        assert models.FlagAudit.__mapper__.eager_defaults is True

    def test_an_orm_insert_into_flag_audit_never_names_the_hashes(self):
        from sqlalchemy import insert
        sql = str(insert(models.FlagAudit).values(flag_id=1, verdict="robust", method="yes_no").compile(dialect=DIALECT))
        assert "prev_hash" not in sql and "row_hash" not in sql

    @pytest.mark.parametrize("model, kwargs", [
        (models.FlagHistory, {"flag_id": 1, "change_op": "U", "changed_by": "x", "before_image": {}, "sys_period": None}),
        (models.FlagAuditTombstone, {"flag_audit_id": 1, "flag_id": 1, "deleted_by": "x", "reason": "y"}),
    ])
    def test_trigger_owned_tables_refuse_orm_inserts(self, model, kwargs):
        session = Session()          # no engine: the guard fires before any connection is needed
        session.add(model(**kwargs))
        with pytest.raises(models.ReadOnlyTableError):
            session.flush()

    def _persistent_audit(self, session):
        audit = models.FlagAudit(flag_audit_id=1, flag_id=1, verdict="robust", method="yes_no")
        make_transient_to_detached(audit)
        session.add(audit)
        return audit

    def test_orm_update_of_flag_audit_is_refused(self):
        session = Session()
        audit = self._persistent_audit(session)
        audit.verdict = "frameable"
        with pytest.raises(models.ReadOnlyTableError, match="append-only"):
            session.flush()

    def test_orm_delete_of_flag_audit_is_refused(self):
        session = Session()
        audit = self._persistent_audit(session)
        session.delete(audit)
        with pytest.raises(models.ReadOnlyTableError, match="append-only"):
            session.flush()


def test_nothing_calls_create_all_or_drop_all():
    """DDL belongs to db/schema.sql + migrations. A create_all() against a real
    database would create tables without their triggers, functions, CHECKs
    and indexes -- a schema that looks right and enforces nothing."""
    offenders = []
    for path in REPO.rglob("*.py"):
        if any(part in {".venv-win", ".venv", "build", "node_modules", "target"} for part in path.parts):
            continue
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("create_all", "drop_all"):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert offenders == []
