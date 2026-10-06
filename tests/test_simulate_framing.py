"""
Offline tests for scripts/simulate_framing.py's pure helpers and
validate_wallet_ids. CREATE DATABASE ... TEMPLATE and the real injection/
cleanup flow need a real Postgres and are exercised by hand (see the
module docstring) -- that's exactly the "never touches the main database"
property a fake can't meaningfully prove anyway.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from scripts.simulate_framing import (
    synthetic_block_number,
    synthetic_tx_hash,
    validate_wallet_ids,
    SYNTHETIC_BLOCK_BASE,
)


class TestSyntheticBlockNumber(unittest.TestCase):
    def test_deterministic_and_above_any_real_block(self):
        self.assertEqual(synthetic_block_number(3), SYNTHETIC_BLOCK_BASE + 3)
        self.assertGreater(synthetic_block_number(1), 30_000_000)  # well above real mainnet height

    def test_different_scenarios_never_collide(self):
        self.assertNotEqual(synthetic_block_number(1), synthetic_block_number(2))


class TestSyntheticTxHash(unittest.TestCase):
    def test_is_66_chars_and_starts_with_the_marker_prefix(self):
        h = synthetic_tx_hash(scenario_id=3, index=7)
        self.assertEqual(len(h), 66)
        self.assertTrue(h.startswith("0xdeadbeef"))

    def test_different_indices_within_a_scenario_never_collide(self):
        hashes = {synthetic_tx_hash(3, i) for i in range(50)}
        self.assertEqual(len(hashes), 50)

    def test_different_scenarios_never_collide(self):
        self.assertNotEqual(synthetic_tx_hash(1, 0), synthetic_tx_hash(2, 0))


class _FakeCursor:
    def __init__(self, existing_ids):
        self._existing = existing_ids

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        (requested,) = params
        self._result = [(wid,) for wid in requested if wid in self._existing]

    def fetchall(self):
        return self._result


class _FakeConn:
    def __init__(self, existing_ids):
        self._existing = existing_ids

    def cursor(self):
        return _FakeCursor(self._existing)


class TestValidateWalletIds(unittest.TestCase):
    def test_passes_when_all_ids_exist(self):
        conn = _FakeConn(existing_ids={1, 2, 3})
        validate_wallet_ids(conn, [1, 2])  # should not raise

    def test_raises_naming_the_missing_ids(self):
        conn = _FakeConn(existing_ids={1, 2})
        with self.assertRaises(ValueError) as ctx:
            validate_wallet_ids(conn, [1, 2, 999])
        self.assertIn("999", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
