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
    synthetic_wallet_address,
    validate_wallet_ids,
    SYNTHETIC_BLOCK_BASE,
    DEFAULT_LAUNDER_FORWARD_FRACTION,
    LAUNDER_FORWARD_BLOCK_OFFSET,
)
from graph.detectors import compute_engaged_outflow_ratios, DEFAULT_ENGAGED_OUTFLOW_THRESHOLD


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


class TestSyntheticWalletAddress(unittest.TestCase):
    def test_is_42_chars_and_deterministic(self):
        a1 = synthetic_wallet_address(scenario_id=3, role="launderer", index=0)
        a2 = synthetic_wallet_address(scenario_id=3, role="launderer", index=0)
        self.assertEqual(len(a1), 42)
        self.assertTrue(a1.startswith("0x"))
        self.assertEqual(a1, a2)

    def test_different_roles_never_collide_for_the_same_index(self):
        launderer = synthetic_wallet_address(3, "launderer", 0)
        laundered = synthetic_wallet_address(3, "laundered", 0)
        self.assertNotEqual(launderer, laundered)

    def test_different_indices_never_collide(self):
        addrs = {synthetic_wallet_address(3, "launderer", i) for i in range(50)}
        self.assertEqual(len(addrs), 50)

    def test_different_scenarios_never_collide(self):
        self.assertNotEqual(
            synthetic_wallet_address(1, "launderer", 0),
            synthetic_wallet_address(2, "launderer", 0),
        )


class TestLaundererScenarioMath(unittest.TestCase):
    """Hand-verifies, offline, that the exact transfer shape
    inject_launderer_scenario produces (seed -> launderer amount_wei,
    launderer -> downstream forward_wei, forward_wei/amount_wei ==
    DEFAULT_LAUNDER_FORWARD_FRACTION, within LAUNDER_FORWARD_BLOCK_OFFSET
    blocks) actually clears engaged_proximity's outflow threshold via the
    real compute_engaged_outflow_ratios -- not asserted from reasoning
    alone. This is the same cross-check test_detectors.py's
    TestEngagedProximity already does for hand-built chains; this one
    specifically matches what the simulator will really inject."""

    SEED = 1
    LAUNDERER = 100
    DOWNSTREAM = 200
    AMOUNT_WEI = 10 ** 18  # 1 ETH, matching DEFAULT_LAUNDER_ETH

    def test_launderer_clears_the_engaged_outflow_threshold(self):
        forward_wei = int(self.AMOUNT_WEI * DEFAULT_LAUNDER_FORWARD_FRACTION)
        block_in = SYNTHETIC_BLOCK_BASE
        block_out = block_in + LAUNDER_FORWARD_BLOCK_OFFSET
        transfers = [
            (self.SEED, self.LAUNDERER, self.AMOUNT_WEI, None, block_in),
            (self.LAUNDERER, self.DOWNSTREAM, forward_wei, None, block_out),
        ]
        ratios = compute_engaged_outflow_ratios(transfers, self.SEED)
        self.assertAlmostEqual(ratios[self.LAUNDERER][None], DEFAULT_LAUNDER_FORWARD_FRACTION, places=9)
        self.assertGreaterEqual(ratios[self.LAUNDERER][None], DEFAULT_ENGAGED_OUTFLOW_THRESHOLD)

    def test_a_passive_dust_victim_never_clears_it(self):
        """Contrast case: a wallet that only ever RECEIVES (never signs
        an outgoing transfer, like a dust target) shows up with ratio
        exactly 0.0 -- present (it did receive tainted inflow), but never
        qualifying, since outflow_in_window has nothing recorded for it."""
        transfers = [(self.SEED, self.LAUNDERER, self.AMOUNT_WEI, None, SYNTHETIC_BLOCK_BASE)]
        ratios = compute_engaged_outflow_ratios(transfers, self.SEED)
        self.assertEqual(ratios[self.LAUNDERER][None], 0.0)
        self.assertLess(ratios[self.LAUNDERER][None], DEFAULT_ENGAGED_OUTFLOW_THRESHOLD)


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
