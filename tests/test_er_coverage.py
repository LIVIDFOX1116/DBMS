"""Every table the DDL creates must appear in the ER diagram and the data
dictionary, and every column a migration adds must be in the DBML.

The SVG and the data dictionary are generated from docs/schema_v2.dbml
(scripts/render_er.ps1 / render_er.sh), so in practice this is a check that
the DBML was updated alongside a migration and the outputs were regenerated.
Offline: reads files only.
"""
import re
import unittest

from tests.ddl_parse import REPO, parse_schema

DBML = REPO / "docs" / "schema_v2.dbml"
SVG = REPO / "docs" / "ER_Diagram_TransactionGraphAnalyzer.svg"
DATA_DICTIONARY = REPO / "docs" / "DATA_DICTIONARY.md"


def dbml_tables(text: str) -> dict[str, str]:
    """Table name -> the body of its block."""
    return {m.group(1).strip('"'): m.group(2)
            for m in re.finditer(r"^Table\s+(\"?\w+\"?)[^{\n]*\{(.*?)^\}", text, re.M | re.S)}


class TestErCoverage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = parse_schema()
        cls.dbml = dbml_tables(DBML.read_text(encoding="utf-8"))

    def test_the_ddl_defines_26_tables(self):
        # A guard on the parser itself: if this number moves, a migration
        # added or removed a table and the diagram needs to follow.
        self.assertEqual(len(self.schema), 26, sorted(self.schema))

    def test_every_table_is_in_the_dbml(self):
        missing = sorted(set(self.schema) - set(self.dbml))
        self.assertEqual(missing, [], f"tables missing from {DBML.name}: {missing}")

    def test_the_dbml_has_no_tables_the_ddl_does_not(self):
        extra = sorted(set(self.dbml) - set(self.schema))
        self.assertEqual(extra, [], f"tables in {DBML.name} that no DDL creates: {extra}")

    def test_every_column_is_in_the_dbml(self):
        missing = []
        for name, table in self.schema.items():
            body = self.dbml.get(name, "")
            for col in table.columns:
                if not re.search(rf"^\s*\"?{col}\"?\s", body, re.M):
                    missing.append(f"{name}.{col}")
        self.assertEqual(missing, [], f"columns missing from {DBML.name}: {missing}")

    def test_every_migration_added_column_is_in_the_dbml(self):
        # Called out separately because these are the ones that went stale
        # before: ALTER TABLE ... ADD COLUMN lands in a migration file, far
        # from the CREATE TABLE a reader would look at.
        added = [(n, c) for n, t in self.schema.items() for c in t.added_columns]
        self.assertTrue(added, "expected at least one ADD COLUMN in the migrations")
        missing = [f"{n}.{c}" for n, c in added
                   if not re.search(rf"^\s*\"?{c}\"?\s", self.dbml.get(n, ""), re.M)]
        self.assertEqual(missing, [], f"migration columns missing from {DBML.name}: {missing}")

    def test_every_foreign_key_is_a_ref_in_the_dbml(self):
        refs = set()
        for table, body in self.dbml.items():
            for line in body.splitlines():
                col = re.match(r"\s*\"?(\w+)\"?\s.*\bref:\s*[<>-]\s*(\w+)\.(\w+)", line)
                if col:
                    refs.add((table, col.group(1), col.group(2), col.group(3)))
        expected = {(t, c.name, *c.references) for t, tab in self.schema.items()
                    for c in tab.columns.values() if c.references}
        self.assertEqual(len(expected), 35, "the DDL's foreign-key count changed; update the diagram")
        self.assertEqual(sorted(expected - refs), [], "foreign keys missing from the DBML")
        self.assertEqual(sorted(refs - expected), [], "DBML refs with no foreign key in the DDL")

    def test_every_table_is_in_the_svg(self):
        svg = SVG.read_text(encoding="utf-8")
        # dbml-renderer emits one Graphviz node per table, titled with its name.
        missing = [t for t in self.schema if f"<title>{t}</title>" not in svg]
        self.assertEqual(missing, [], f"tables missing from {SVG.name}: {missing}")

    def test_every_table_is_in_the_data_dictionary(self):
        self.assertTrue(DATA_DICTIONARY.exists(), f"{DATA_DICTIONARY} has not been generated")
        text = DATA_DICTIONARY.read_text(encoding="utf-8")
        missing = [t for t in self.schema if not re.search(rf"^###\s+`{t}`", text, re.M)]
        self.assertEqual(missing, [], f"tables missing from {DATA_DICTIONARY.name}: {missing}")


if __name__ == "__main__":
    unittest.main()
