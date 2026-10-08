"""
Offline test for graph/evidence.py's persist_evidence (bulk-insert shape,
no DB needed). The recursive CTE itself (db/queries/evidence.sql) is
exercised against the real database by hand -- a FakeCursor can't
meaningfully simulate a recursive CTE's own correctness.
"""
import ast
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from graph.evidence import persist_evidence


class _FakeCursor:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def mogrify(self, template, args):
        rendered = template.decode()
        for value in args:
            rendered = rendered.replace("%s", repr(value), 1)
        return rendered.encode()

    def execute(self, query, params=None):
        if isinstance(query, bytes):
            query = query.decode()
        q = " ".join(query.split())
        match = re.search(r"VALUES\s+(.+?)(?:\s+ON CONFLICT|\s+RETURNING|$)", q, re.IGNORECASE | re.DOTALL)
        self._sink.extend(ast.literal_eval("[" + match.group(1) + "]"))

    @property
    def connection(self):
        return type("_C", (), {"encoding": "UTF8"})()


class _FakeConn:
    def __init__(self):
        self.rows = []
        self.committed = 0

    def cursor(self):
        return _FakeCursor(self.rows)

    def commit(self):
        self.committed += 1


class TestPersistEvidence(unittest.TestCase):
    def test_bulk_inserts_chain_rows_tagged_with_flag_id(self):
        conn = _FakeConn()
        chain = [
            ("0xaaa", 0, 100, "unsolicited"),
            ("0xbbb", 1, 90, "unsolicited"),
        ]
        count = persist_evidence(conn, flag_id=7, chain=chain)
        self.assertEqual(count, 2)
        self.assertEqual(conn.rows, [
            (7, "0xaaa", 0, 100, "unsolicited"),
            (7, "0xbbb", 1, 90, "unsolicited"),
        ])
        self.assertEqual(conn.committed, 1)

    def test_empty_chain_still_commits_and_returns_zero(self):
        conn = _FakeConn()
        count = persist_evidence(conn, flag_id=7, chain=[])
        self.assertEqual(count, 0)
        self.assertEqual(conn.committed, 1)


class TestEvidenceQueryShape(unittest.TestCase):
    """Guards on db/queries/evidence.sql. A recursive CTE can't be run
    offline, so these pin the properties the Phase 3 fixes depend on; the
    real-DB equivalence check against the old query is in docs/perf/."""

    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).parent.parent / "db" / "queries" / "evidence.sql"
        cls.sql = re.sub(r"--[^\n]*", "", path.read_text())

    def test_recursive_term_is_deduplicated_by_wallet(self):
        self.assertRegex(self.sql, r"DISTINCT ON \(ev\.from_wallet_id\)")

    def test_never_joins_snapshot_edge_tx_by_tx_hash_alone(self):
        # no tx_hash index on snapshot_edge_tx: every join must come through snapshot_edge_id
        for m in re.finditer(r"JOIN snapshot_edge_tx st ON ([^\n]+)", self.sql):
            self.assertIn("st.snapshot_edge_id = se.edge_id", m.group(1))

    def test_token_recipients_are_hop0_unsolicited_candidates(self):
        self.assertIn("FROM token_transfer tt", self.sql)
        self.assertIn("tt.to_wallet_id = %(wallet_id)s", self.sql)
        self.assertIn("t.from_wallet_id <> %(wallet_id)s", self.sql)

    def test_signer_is_always_the_transaction_sender(self):
        self.assertNotIn("tt.from_wallet_id", self.sql)

    def test_all_named_params_are_the_ones_fetch_evidence_chain_supplies(self):
        self.assertEqual(
            set(re.findall(r"%\((\w+)\)s", self.sql)),
            {"snapshot_id", "wallet_id", "seed_wallet_id", "max_hops"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
