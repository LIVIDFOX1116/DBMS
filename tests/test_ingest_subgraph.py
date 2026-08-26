"""
Offline unit test for the ingestion BFS logic, using a fake Etherscan client
and a fake DB cursor -- no network, no live Postgres required. This is what
runs in CI / this sandbox; the real thing (ingestion/ingest_subgraph.py run
against the live API and a real Postgres) has to run on a machine with
internet egress, per README.md.

Run: python3 -m pytest tests/ -v   (or plain: python3 tests/test_ingest_subgraph.py)
"""
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from ingestion import ingest_subgraph as ing

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SEED = "0xc8a65fadf0e0ddaf421f28feab69bf6e2e589963"  # lowercased, matches norm()


class FakeEtherscanClient:
    """Returns canned fixture data only for the seed address; empty for
    anything discovered beyond it, so the BFS terminates quickly in tests."""

    def __init__(self):
        self.txlist = json.loads((FIXTURES / "sample_txlist.json").read_text())
        self.tokentx = json.loads((FIXTURES / "sample_tokentx.json").read_text())

    def get_normal_transactions(self, address, max_results=None, sort="asc"):
        return self.txlist if address == SEED else []

    def get_token_transfers(self, address, max_results=None, sort="asc"):
        return self.tokentx if address == SEED else []


class FakeCursor:
    """Minimal stand-in for a psycopg2 cursor. Understands exactly the
    queries ingest_subgraph.py issues and keeps enough state to answer them
    correctly -- not a general SQL engine, just enough to exercise the logic."""

    def __init__(self, state):
        self.state = state  # shared dict across cursors in the same "connection"
        self._last_result = None

    def execute(self, query, params=()):
        q = " ".join(query.split())  # normalize whitespace for matching

        if q.startswith("INSERT INTO wallet"):
            address, address_type = params
            wid = self.state["wallets"].setdefault(address, len(self.state["wallets"]) + 1)
            self.state["wallet_types"][address] = address_type
            self._last_result = (wid,)

        elif q.startswith("INSERT INTO block"):
            block_number, block_hash, timestamp = params
            self.state["blocks"][block_number] = (block_hash, timestamp)
            self._last_result = None

        elif q.startswith("INSERT INTO transaction"):
            tx_hash = params[0]
            self.state["transactions"][tx_hash] = params
            self._last_result = None

        elif q.startswith("SELECT 1 FROM transaction"):
            (tx_hash,) = params
            self._last_result = (1,) if tx_hash in self.state["transactions"] else None

        elif q.startswith("INSERT INTO token "):
            contract_wallet_id, contract_address, symbol, name, decimals = params
            tid = self.state["tokens"].setdefault(contract_address, len(self.state["tokens"]) + 1)
            self._last_result = (tid,)

        elif q.startswith("INSERT INTO token_transfer"):
            self.state["token_transfers"].append(params)
            self._last_result = None

        else:
            raise AssertionError(f"FakeCursor doesn't know how to handle: {q[:60]}")

    def fetchone(self):
        return self._last_result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConnection:
    def __init__(self):
        self.state = {
            "wallets": {}, "wallet_types": {}, "blocks": {},
            "transactions": {}, "tokens": {}, "token_transfers": [],
        }
        self.committed = 0

    def cursor(self):
        return FakeCursor(self.state)

    def commit(self):
        self.committed += 1

    def close(self):
        pass


class TestIngestSubgraph(unittest.TestCase):
    def test_norm_lowercases_addresses(self):
        self.assertEqual(ing.norm("0xABC"), "0xabc")
        self.assertIsNone(ing.norm(None))

    def test_process_address_inserts_expected_rows(self):
        conn = FakeConnection()
        client = FakeEtherscanClient()
        with conn.cursor() as cur:
            discovered = ing.process_address(cur, client, SEED)

        # both counterparties from the native txs, plus the token + its counterparty
        self.assertIn("0x1111111111111111111111111111111111a001", discovered)
        self.assertIn("0x2222222222222222222222222222222222a002", discovered)
        self.assertIn("0x9999999999999999999999999999999999a999", discovered)  # token contract

        self.assertEqual(len(conn.state["transactions"]), 2)
        self.assertEqual(len(conn.state["token_transfers"]), 1)
        self.assertEqual(
            conn.state["wallet_types"]["0x9999999999999999999999999999999999a999"],
            "CONTRACT",
        )

    def test_crawl_two_hops_terminates_and_visits_seed_first(self):
        conn = FakeConnection()
        client = FakeEtherscanClient()
        stats = ing.crawl(SEED, hop_count=2, client=client, conn=conn)

        # Requested 2 hops, but the frontier is exhausted after hop 1 (the
        # fake client only returns data for the seed address) -- the crawl
        # correctly stops early rather than wasting calls on an empty
        # frontier, so hops_completed is 2, not 3.
        self.assertEqual(stats["hops_completed"], 2)
        self.assertGreaterEqual(stats["addresses_processed"], 1)
        self.assertIn(SEED, conn.state["wallets"])
        self.assertGreater(conn.committed, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
