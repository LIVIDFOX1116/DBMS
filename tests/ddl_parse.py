"""Offline parser for the project's own DDL -- db/schema.sql, the migrations
in filename order, and the schema_migration table api/dbsetup.py creates.

Shared by tests/test_er_coverage.py (every table must be in the ER diagram
and the data dictionary) and tests/test_orm_models.py (every ORM model must
match the DDL). Neither needs a database: the SQL files are the single
source of truth for the schema, so these tests read them directly.

This understands the subset of PostgreSQL DDL this repo actually writes --
CREATE TABLE with inline and table-level constraints, ALTER TABLE ... ADD
COLUMN -- not SQL in general. A new construct it cannot parse fails loudly
rather than being skipped.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCHEMA_SQL = REPO / "db" / "schema.sql"
MIGRATIONS_DIR = REPO / "db" / "migrations"
DBSETUP_PY = REPO / "api" / "dbsetup.py"

# Words that end a column's type and start its constraints.
_CONSTRAINT_WORDS = {"NOT", "NULL", "PRIMARY", "REFERENCES", "UNIQUE", "DEFAULT", "CHECK", "CONSTRAINT"}
# Whole words only: a column called `checksum_sha256` starts with "CHECK".
_TABLE_CONSTRAINT = re.compile(r"^(PRIMARY\s+KEY|UNIQUE|CONSTRAINT|CHECK|FOREIGN\s+KEY|EXCLUDE)\b", re.I)


@dataclass
class Column:
    name: str
    type: str                 # normalised, see normalise_type()
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False
    default: str | None = None
    references: tuple[str, str] | None = None   # (table, column)


@dataclass
class Table:
    name: str
    source: str                                  # file that created it
    columns: dict[str, Column] = field(default_factory=dict)
    primary_key: list[str] = field(default_factory=list)
    unique: list[tuple[str, ...]] = field(default_factory=list)
    added_columns: list[str] = field(default_factory=list)   # via ALTER TABLE ... ADD COLUMN


def normalise_type(raw: str) -> str:
    """One spelling per type, so the DDL's `SERIAL`/`INT`/`DECIMAL(5,2)` and
    SQLAlchemy's compiled `INTEGER`/`NUMERIC(5, 2)` compare equal."""
    t = re.sub(r"\s+", " ", raw.strip().upper())
    t = re.sub(r"\s*,\s*", ",", t)
    t = re.sub(r"\s*\(\s*", "(", t).replace(" )", ")")
    aliases = {
        "SERIAL": "INTEGER", "BIGSERIAL": "BIGINT", "INT": "INTEGER", "INT4": "INTEGER",
        "INT8": "BIGINT", "TIMESTAMPTZ": "TIMESTAMP WITH TIME ZONE",
        "TIMESTAMP": "TIMESTAMP WITHOUT TIME ZONE", "FLOAT8": "DOUBLE PRECISION",
        "BOOL": "BOOLEAN",
    }
    if t in aliases:
        return aliases[t]
    return re.sub(r"^DECIMAL\b", "NUMERIC", t)


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def _balanced(text: str, open_at: int) -> int:
    """Index just past the parenthesis matching the one at open_at."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("unbalanced parentheses in DDL")


def _split_top_level(body: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(body):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(body[start:i])
            start = i + 1
    parts.append(body[start:])
    return [p.strip() for p in parts if p.strip()]


def _cols(spec: str) -> tuple[str, ...]:
    return tuple(c.strip().lower() for c in spec.split(","))


def _parse_column(item: str) -> Column:
    tokens = re.findall(r"\w+(?:\[\])?|\([^()]*\)|'[^']*'|\S", item)
    name = tokens[0].lower()
    type_tokens, i = [], 1
    while i < len(tokens) and tokens[i].upper() not in _CONSTRAINT_WORDS:
        type_tokens.append(tokens[i])
        i += 1
    raw_type = " ".join(type_tokens).replace(" (", "(")
    rest = item[item.upper().find(type_tokens[-1].upper(), len(name)) + len(type_tokens[-1]):] if type_tokens else ""
    upper = rest.upper()
    col = Column(name=name, type=normalise_type(raw_type))
    if "PRIMARY KEY" in upper:
        col.primary_key, col.nullable = True, False
    if "NOT NULL" in upper:
        col.nullable = False
    if re.search(r"\bUNIQUE\b", upper):
        col.unique = True
    ref = re.search(r"REFERENCES\s+(\w+)\s*\((\w+)\)", rest, re.I)
    if ref:
        col.references = (ref.group(1).lower(), ref.group(2).lower())
    default = re.search(r"\bDEFAULT\s+(.+?)(?=\s+(?:NOT|NULL|PRIMARY|REFERENCES|UNIQUE|CHECK|CONSTRAINT)\b|$)",
                        rest, re.I | re.S)
    if default:
        col.default = default.group(1).strip()
    if raw_type.upper() in ("SERIAL", "BIGSERIAL"):
        col.default = "autoincrement"
    return col


def _parse_create(name: str, body: str, source: str) -> Table:
    table = Table(name=name, source=source)
    for item in _split_top_level(body):
        if _TABLE_CONSTRAINT.match(item):
            pk = re.match(r"PRIMARY KEY\s*\(([^)]*)\)", item, re.I)
            uq = re.match(r"UNIQUE\s*\(([^)]*)\)", item, re.I)
            if pk:
                table.primary_key = list(_cols(pk.group(1)))
            elif uq:
                table.unique.append(_cols(uq.group(1)))
            continue
        col = _parse_column(item)
        table.columns[col.name] = col
        if col.primary_key:
            table.primary_key = [col.name]
    for c in table.primary_key:
        table.columns[c].primary_key = True
        table.columns[c].nullable = False
    return table


def _schema_migration_sql() -> str:
    """api/dbsetup.py builds this table's DDL with the name as a psycopg2
    Identifier; substitute the literal name back in so it parses like the
    rest."""
    text = DBSETUP_PY.read_text(encoding="utf-8")
    name = re.search(r'MIGRATION_TRACKING_TABLE\s*=\s*"(\w+)"', text).group(1)
    body = re.search(r"CREATE TABLE IF NOT EXISTS \{\} \((.*?)\)\"\"\"", text, re.S).group(1)
    return f"CREATE TABLE IF NOT EXISTS {name} ({body});"


def ddl_sources() -> list[tuple[str, str]]:
    """(label, sql) in the order the schema is actually applied."""
    sources = [("db/schema.sql", SCHEMA_SQL.read_text(encoding="utf-8"))]
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        sources.append((f"db/migrations/{path.name}", path.read_text(encoding="utf-8")))
    sources.append(("api/dbsetup.py", _schema_migration_sql()))
    return sources


def parse_schema() -> dict[str, Table]:
    tables: dict[str, Table] = {}
    for label, raw in ddl_sources():
        sql = _strip_comments(raw)
        for m in re.finditer(r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?(\w+)\s*\(", sql, re.I):
            end = _balanced(sql, m.end() - 1)
            name = m.group(1).lower()
            tables[name] = _parse_create(name, sql[m.end():end - 1], label)
        # Statements are applied in file order, as PostgreSQL would: a
        # constraint dropped and re-added must end up added.
        for m in re.finditer(r"\b(ALTER TABLE\s+(\w+)\s+([^;]+)|DROP TABLE\s+(?:IF EXISTS\s+)?(\w+))\s*;", sql, re.I):
            if m.group(4):
                tables.pop(m.group(4).lower(), None)
                continue
            _apply_alter(tables[m.group(2).lower()], m.group(3).strip())
    return tables


def _apply_alter(table: Table, action: str) -> None:
    """The ALTER TABLE forms the migrations use. Anything else (CHECK
    constraints, comments) does not change what these tests compare."""
    if m := re.match(r"ADD COLUMN\s+(?:IF NOT EXISTS\s+)?(.+)$", action, re.I | re.S):
        col = _parse_column(m.group(1).strip())
        table.columns[col.name] = col
        table.added_columns.append(col.name)
    elif m := re.match(r"ALTER COLUMN\s+(\w+)\s+TYPE\s+(.+)$", action, re.I | re.S):
        table.columns[m.group(1).lower()].type = normalise_type(m.group(2))
    elif m := re.match(r"ALTER COLUMN\s+(\w+)\s+DROP NOT NULL$", action, re.I):
        table.columns[m.group(1).lower()].nullable = True
    elif m := re.match(r"ALTER COLUMN\s+(\w+)\s+SET NOT NULL$", action, re.I):
        table.columns[m.group(1).lower()].nullable = False
    elif m := re.match(r"ADD\s+CONSTRAINT\s+\w+\s+UNIQUE\s*\(([^)]*)\)$", action, re.I):
        cols = _cols(m.group(1))
        if len(cols) == 1:
            table.columns[cols[0]].unique = True
        else:
            table.unique.append(cols)
    elif m := re.match(r"ADD\s+CONSTRAINT\s+\w+\s+FOREIGN KEY\s*\((\w+)\)\s*REFERENCES\s+(\w+)\s*\((\w+)\)$", action, re.I):
        table.columns[m.group(1).lower()].references = (m.group(2).lower(), m.group(3).lower())
    elif m := re.match(r"DROP CONSTRAINT\s+(?:IF EXISTS\s+)?(\w+)$", action, re.I):
        name = m.group(1).lower()
        # PostgreSQL's default names: <table>_<column>_fkey / _key.
        for col in table.columns.values():
            if name == f"{table.name}_{col.name}_fkey":
                col.references = None
            elif name == f"{table.name}_{col.name}_key":
                col.unique = False


if __name__ == "__main__":
    for t in parse_schema().values():
        print(f"{t.name} ({t.source}) pk={t.primary_key}")
        for c in t.columns.values():
            print(f"   {c.name:22} {c.type:30} null={c.nullable} ref={c.references} default={c.default}")
