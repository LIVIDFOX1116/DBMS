"""Generates docs/DATA_DICTIONARY.md from docs/schema_v2.dbml, so the
dictionary, the ER diagram and the DBML can never disagree.

    python scripts/gen_data_dictionary.py

Parses only the DBML subset docs/schema_v2.dbml is written in (Table blocks
with inline column settings, indexes, Notes, TableGroups) -- deliberately
not a general DBML parser, so it needs no dependency. Run by
scripts/render_er.ps1 / render_er.sh.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DBML = REPO / "docs" / "schema_v2.dbml"
OUT = REPO / "docs" / "DATA_DICTIONARY.md"

GROUP_TITLES = {
    "ingestion": "Ingestion (on-chain data)",
    "snapshot_and_analysis": "Snapshot and analysis",
    "framecheck_audit": "FrameCheck audit",
    "app_and_ops": "App and ops",
}


def _split_settings(s: str) -> list[str]:
    """Splits `pk, not null, note: 'a, b'` on commas outside quotes/backticks."""
    parts, buf, quote = [], "", None
    for ch in s:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote, buf = ch, buf + ch
        elif ch == ",":
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())
    return parts


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"`":
        return v[1:-1]
    return v


def _note(body: str) -> str:
    m = re.search(r"^\s*Note:\s*'''(.*?)'''", body, re.M | re.S) or re.search(r"^\s*Note:\s*'(.*?)'\s*$", body, re.M)
    return " ".join(m.group(1).split()) if m else ""


def parse(dbml: str):
    tables = {}
    for m in re.finditer(r"^Table\s+\"?(\w+)\"?[^{\n]*\{(.*?)^\}", dbml, re.M | re.S):
        name, body = m.group(1), m.group(2)
        columns, composite_pk = [], []
        idx = re.search(r"^\s*indexes\s*\{(.*?)^\s*\}", body, re.M | re.S)
        if idx:
            for line in idx.group(1).splitlines():
                pk = re.match(r"\s*\(([^)]*)\)\s*\[pk\]", line)
                if pk:
                    composite_pk = [c.strip() for c in pk.group(1).split(",")]
        column_body = body.replace(idx.group(0), "") if idx else body
        column_body = re.sub(r"^\s*Note:\s*'''.*?'''", "", column_body, flags=re.M | re.S)
        for line in column_body.splitlines():
            cm = re.match(r"\s*(\w+)\s+(\"[^\"]+\"|[\w()]+(?:\([\d,]+\))?)\s*(?:\[(.*)\])?\s*$", line)
            if not cm or cm.group(1) == "Note":
                continue
            settings = _split_settings(cm.group(3) or "")
            col = {"name": cm.group(1), "type": _unquote(cm.group(2)), "pk": cm.group(1) in composite_pk,
                   "not_null": cm.group(1) in composite_pk, "unique": False, "increment": False,
                   "default": "", "ref": "", "note": ""}
            for s in settings:
                key, _, val = s.partition(":")
                key = key.strip().lower()
                if key == "pk":
                    col["pk"] = col["not_null"] = True
                elif key == "not null":
                    col["not_null"] = True
                elif key == "unique":
                    col["unique"] = True
                elif key == "increment":
                    col["increment"] = True
                elif key == "default":
                    col["default"] = _unquote(val)
                elif key == "ref":
                    col["ref"] = val.strip()
                elif key == "note":
                    col["note"] = _unquote(val)
            columns.append(col)
        tables[name] = {"columns": columns, "note": _note(body), "composite_pk": composite_pk}
    groups = {g: re.findall(r"^\s*(\w+)\s*$", b, re.M)
              for g, b in re.findall(r"^TableGroup\s+(\w+)\s*\{(.*?)^\}", dbml, re.M | re.S)}
    return tables, groups


def _key(col: dict) -> str:
    keys = []
    if col["pk"]:
        keys.append("PK")
    if col["ref"]:
        arrow, target = col["ref"][0], col["ref"][1:].strip()
        keys.append(f"FK → `{target}`" + (" (1:1)" if arrow == "-" else ""))
    if col["unique"] and not col["pk"]:
        keys.append("UNIQUE")
    return ", ".join(keys)


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def routines_note(dbml: str) -> str:
    """The body of the standalone `Note views_and_routines` block."""
    m = re.search(r"^Note views_and_routines \{\s*'''(.*?)'''\s*^\}", dbml, re.M | re.S)
    return m.group(1).strip() if m else ""


def render(tables: dict, groups: dict, routines: str = "") -> str:
    fk_count = sum(1 for t in tables.values() for c in t["columns"] if c["ref"])
    one_to_one = sum(1 for t in tables.values() for c in t["columns"] if c["ref"].startswith("-"))
    referenced = {c["ref"][1:].strip().split(".")[0]
                  for t in tables.values() for c in t["columns"] if c["ref"]}
    no_fk = [n for n, t in tables.items()
             if n not in referenced and not any(c["ref"] for c in t["columns"])]
    out = [
        "# FrameCheck data dictionary",
        "",
        "<!-- GENERATED by scripts/gen_data_dictionary.py from docs/schema_v2.dbml -- do not edit by hand. -->",
        "",
        "Generated from `docs/schema_v2.dbml`, the same file the ER diagram "
        "(`docs/ER_Diagram_TransactionGraphAnalyzer.svg` / `.png`) is rendered from. The DDL itself "
        "(`db/schema.sql` + `db/migrations/*.sql`) stays the source of truth; "
        "`tests/test_er_coverage.py` fails if a table, column or foreign key there is missing here.",
        "",
        "## Counts",
        "",
        "| | |",
        "|---|---|",
        f"| Tables (entities) | {len(tables)} |",
        f"| Foreign keys (relationship lines in the ER diagram) | {fk_count} "
        f"({fk_count - one_to_one} many-to-one, {one_to_one} one-to-one) |",
        f"| Tables with no foreign key in either direction, by design | {len(no_fk)}: "
        + ", ".join(f"`{n}`" for n in no_fk) + " |",
        "| Materialized views (not tables, not counted) | 1: `mv_cluster_flag_stats` (migration 008) |",
        "",
        "**Key**: PK primary key, FK foreign key (target in backticks), UNIQUE unique constraint. "
        "Null = `NO` means `NOT NULL`. `auto` = generated by a sequence (`SERIAL`/`BIGSERIAL`).",
        "",
    ]
    for group, members in groups.items():
        out += [f"## {GROUP_TITLES.get(group, group)}", ""]
        for name in members:
            t = tables[name]
            out += [f"### `{name}`", "", _cell(t["note"]) or "_No note._", ""]
            out += ["| Column | Type | Null | Key | Default | Description |",
                    "|---|---|---|---|---|---|"]
            for c in t["columns"]:
                default = "auto" if c["increment"] else (f"`{c['default']}`" if c["default"] else "")
                out.append(f"| `{c['name']}` | `{c['type']}` | {'NO' if c['not_null'] else 'YES'} | "
                           f"{_key(c)} | {default} | {_cell(c['note'])} |")
            out.append("")
    if routines:
        out += ["## Views and routines", "", routines, ""]
    return "\n".join(out)


def main() -> None:
    dbml = DBML.read_text(encoding="utf-8")
    tables, groups = parse(dbml)
    grouped = {n for members in groups.values() for n in members}
    if grouped != set(tables):
        sys.exit(f"every table must be in exactly one TableGroup; ungrouped: {sorted(set(tables) - grouped)}")
    OUT.write_text(render(tables, groups, routines_note(dbml)), encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(REPO)} ({len(tables)} tables)")


if __name__ == "__main__":
    main()
