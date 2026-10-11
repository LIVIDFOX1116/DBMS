"""
Offline tests for the dataset export/import pair (Feature 1's "import a
database" half).

No Postgres: the export's scoping SQL is checked as text (every table's query
must be scoped, and nothing may carry a simulated transaction), and the
import's parsing, safety checks and id remapping are driven through fakes.
The real round trip is a manual pass -- 443,954 rows, recorded in
docs/STABILITY.md -- because it needs two live databases.
"""
import gzip
import io
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from api import dbsetup
from scripts import export_dataset


class TestExportScoping(unittest.TestCase):
    """Every table in the dataset has to be scoped to the one snapshot, and
    nothing may carry a simulated transaction -- the framing simulator marks
    those precisely so they are never mistaken for real history, and a
    dataset is the artefact that leaves the machine."""

    def test_every_table_query_is_scoped_or_reference_data(self):
        reference = {"analyst", "algorithm", "label"}
        for table, query in export_dataset.TABLES:
            if table in reference:
                continue
            self.assertIn("%(snapshot_id)s", query, f"{table} is not scoped to a snapshot")

    def test_transaction_bearing_tables_exclude_simulated_rows(self):
        """Any table scoped through a transaction must go through TX_SCOPE,
        which is the only place the is_simulated filter lives."""
        for table in ("transaction", "token_transfer", "snapshot_edge_tx", "flag_evidence", "block", "token"):
            query = dict(export_dataset.TABLES)[table]
            self.assertIn("is_simulated IS NOT TRUE", query,
                          f"{table} could carry a simulated transaction")

    def test_wallet_scope_covers_every_referencing_table(self):
        """The first import died on token.contract_wallet_id: a token's
        contract has a wallet row that need not be an edge endpoint. The
        closure must name every wallet-referencing column."""
        scope = export_dataset.WALLET_SCOPE
        for fragment in ("src_wallet_id", "dst_wallet_id", "from_wallet_id", "to_wallet_id",
                         "contract_wallet_id", "proposer_wallet_id", "vm.wallet_id",
                         "cm.wallet_id", "f.wallet_id", "a.wallet_id"):
            self.assertIn(fragment, scope, f"{fragment} is missing from the wallet closure")

    def test_identities_and_simulator_bookkeeping_are_never_exported(self):
        exported = {t for t, _ in export_dataset.TABLES}
        for table in ("app_user", "framing_scenario", "schema_migration"):
            self.assertNotIn(table, exported)
            self.assertIn(table, export_dataset.EXCLUDED)

    def test_tables_are_in_foreign_key_order(self):
        """Loading happens in this order, so a referenced table must appear
        before the one referencing it."""
        order = [t for t, _ in export_dataset.TABLES]
        must_precede = [
            ("wallet", "block"), ("block", "transaction"), ("wallet", "transaction"),
            ("wallet", "token"), ("token", "token_transfer"), ("transaction", "token_transfer"),
            ("graph_snapshot", "snapshot_edge"), ("snapshot_edge", "snapshot_edge_tx"),
            ("transaction", "snapshot_edge_tx"), ("algorithm", "analysis_run"),
            ("analyst", "analysis_run"), ("analysis_run", "vertex_metric"),
            ("analysis_run", "cluster"), ("cluster", "cluster_member"),
            ("analysis_run", "flag"), ("flag", "flag_evidence"), ("flag", "flag_audit"),
        ]
        for first, second in must_precede:
            self.assertLess(order.index(first), order.index(second),
                            f"{first} must be written before {second}")

    def test_every_exported_table_is_importable(self):
        for table, _ in export_dataset.TABLES:
            self.assertIn(table, dbsetup.DATASET_TABLES,
                          f"{table} is exported but the importer would refuse it")


def write_dataset(tmp, blocks, manifest=None, header=True):
    """Builds a dataset file. `blocks` is [(table, columns, rows)]."""
    manifest = manifest or {"format": "framecheck-dataset v1", "row_counts": {}}
    out = io.StringIO()
    if header:
        out.write(dbsetup.DATASET_MARKER + "\n")
        out.write("-- manifest " + json.dumps(manifest) + "\n")
    for table, columns, rows in blocks:
        out.write(f"COPY public.{table} ({', '.join(columns)}) FROM stdin;\n")
        for row in rows:
            out.write("\t".join(str(v) for v in row) + "\n")
        out.write("\\.\n")
    path = pathlib.Path(tmp) / "dataset.sql"
    path.write_text(out.getvalue(), encoding="utf-8")
    return path


class TestManifest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_dataset_manifest_is_read_without_the_body(self):
        path = write_dataset(self.tmp.name, [("wallet", ["wallet_id"], [[1]])],
                             manifest={"snapshot_id": 7, "total_rows": 1})
        self.assertEqual(dbsetup.read_dataset_manifest(path)["snapshot_id"], 7)

    def test_a_file_that_is_not_a_dataset_is_refused(self):
        path = pathlib.Path(self.tmp.name) / "other.sql"
        path.write_text("DROP TABLE wallet;\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            dbsetup.read_dataset_manifest(path)

    def test_a_dataset_without_a_manifest_line_is_refused(self):
        path = pathlib.Path(self.tmp.name) / "half.sql"
        path.write_text(dbsetup.DATASET_MARKER + "\nCOPY public.wallet (wallet_id) FROM stdin;\n\\.\n",
                        encoding="utf-8")
        with self.assertRaises(ValueError):
            dbsetup.read_dataset_manifest(path)

    def test_gzipped_and_plain_datasets_both_read(self):
        plain = write_dataset(self.tmp.name, [], manifest={"snapshot_id": 3})
        gz = pathlib.Path(self.tmp.name) / "dataset.sql.gz"
        with gzip.open(gz, "wt", encoding="utf-8") as fh:
            fh.write(plain.read_text(encoding="utf-8"))
        self.assertEqual(dbsetup.read_dataset_manifest(gz)["snapshot_id"], 3)


class TestSeedIdRemapping(unittest.TestCase):
    """The database this project developed against numbers pagerank_threshold
    12; a database freshly created from schema.sql numbers it 6. An import
    that trusted the dataset's ids failed its foreign key, and merging the
    rows would have collided on the unique name. So ids travel by name."""

    def test_ids_are_mapped_by_name_and_unchanged_ones_are_omitted(self):
        rows = ["1\tpagerank\n", "12\tpagerank_threshold\n"]
        mapping = dbsetup._seed_id_map(rows, ["algorithm_id", "algorithm_name"], "algorithm",
                                       {"pagerank": 1, "pagerank_threshold": 6})
        self.assertEqual(mapping, {"12": "6"}, "only the id that actually differs needs mapping")

    def test_a_name_the_target_does_not_have_is_left_to_fail_its_foreign_key(self):
        """Silently dropping the row would make the import quietly
        incomplete; letting the FK reject it rolls the whole thing back."""
        mapping = dbsetup._seed_id_map(["9\tbrand_new_detector\n"],
                                       ["algorithm_id", "algorithm_name"], "algorithm", {})
        self.assertEqual(mapping, {})

    def test_referencing_columns_are_rewritten_and_nulls_left_alone(self):
        dbsetup.SEED_ID_MAPS = {"algorithm": {"12": "6"}, "analyst": {}}
        self.addCleanup(lambda: setattr(dbsetup, "SEED_ID_MAPS", {}))
        rows = ["5\t1\t12\t1\n", "6\t1\t12\t\\N\n"]
        columns = ["run_id", "snapshot_id", "algorithm_id", "analyst_id"]
        out, changed = dbsetup._remap_lines(rows, columns, {"algorithm_id": "algorithm",
                                                            "analyst_id": "analyst"})
        self.assertEqual(changed, 2)
        self.assertEqual(out[0], "5\t1\t6\t1\n")
        self.assertEqual(out[1], "6\t1\t6\t\\N\n", "a NULL must stay NULL, not become an id")

    def test_line_endings_survive_a_rewrite(self):
        dbsetup.SEED_ID_MAPS = {"algorithm": {"12": "6"}}
        self.addCleanup(lambda: setattr(dbsetup, "SEED_ID_MAPS", {}))
        out, _ = dbsetup._remap_lines(["1\t12\r\n"], ["run_id", "algorithm_id"],
                                      {"algorithm_id": "algorithm"})
        self.assertTrue(out[0].endswith("\r\n"))

    def test_a_table_with_no_mapped_columns_is_passed_through_untouched(self):
        rows = ["1\t2\n"]
        out, changed = dbsetup._remap_lines(rows, ["a", "b"], {"algorithm_id": "algorithm"})
        self.assertEqual((out, changed), (rows, 0))


class TestCopyBlockSafety(unittest.TestCase):
    def test_a_copy_header_naming_an_unimportable_table_is_rejected(self):
        """A dataset file is data, not instructions: its COPY headers are
        checked against a list rather than trusted."""
        self.assertNotIn("app_user", dbsetup.DATASET_TABLES)
        self.assertNotIn("schema_migration", dbsetup.DATASET_TABLES)
        self.assertNotIn("pg_shadow", dbsetup.DATASET_TABLES)

    def test_a_copy_header_with_a_quoted_injection_does_not_parse(self):
        for line in ('COPY public."wallet; DROP TABLE wallet" (a) FROM stdin;',
                     "COPY public.wallet (a); DROP TABLE wallet; -- FROM stdin;",
                     "COPY public.wallet FROM stdin;"):
            self.assertIsNone(dbsetup.parse_copy_header(line), line)

    def test_a_copy_header_with_an_unusable_column_list_raises(self):
        with self.assertRaises(ValueError):
            dbsetup.parse_copy_header("COPY public.wallet (a, 1bad) FROM stdin;")

    def test_reading_a_block_stops_at_the_terminator(self):
        fh = io.StringIO("1\ta\n2\tb\n\\.\nCOPY public.flag (flag_id) FROM stdin;\n")
        rows = dbsetup._read_copy_block(fh)
        self.assertEqual(rows, ["1\ta\n", "2\tb\n"])
        self.assertTrue(fh.readline().startswith("COPY"), "the next block must still be readable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
