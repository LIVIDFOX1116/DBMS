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


if __name__ == "__main__":
    unittest.main(verbosity=2)
