"""
Offline test for scripts/run_experiment.py's sim_env() -- the one pure
function here. Everything else (DB cloning, injection, the export/
analyze/detect/evidence/audit chain, cleanup cascade) was verified by
hand against a real cloned database -- see the commit message for the
full trace, including a real 5-target scenario where all 5 were flagged
by bfs_proximity and the framing cost matched the documented gas constant
exactly.
"""
import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))

from run_experiment import sim_env
from simulate_framing import SIM_DB_NAME


class TestSimEnv(unittest.TestCase):
    def test_overrides_pgdatabase_only(self):
        os.environ["SOME_OTHER_VAR"] = "untouched"
        env = sim_env()
        self.assertEqual(env["PGDATABASE"], SIM_DB_NAME)
        self.assertEqual(env["SOME_OTHER_VAR"], "untouched")

    def test_does_not_mutate_the_real_process_environment(self):
        os.environ.pop("PGDATABASE", None)
        sim_env()
        self.assertNotIn("PGDATABASE", os.environ)


if __name__ == "__main__":
    unittest.main(verbosity=2)
