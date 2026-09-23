"""
Real EtherscanClient against a mocked requests.get -- unlike
tests/test_ingest_subgraph.py's FakeEtherscanClient (a hand-written stub
standing in for the whole client), this exercises the actual HTTP-calling
code in ingestion/etherscan_client.py.
"""
import pathlib
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from ingestion.etherscan_client import EtherscanClient, EtherscanError, _redact


def _fake_response(json_body, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.raise_for_status.return_value = None
    return resp


class TestRedact(unittest.TestCase):
    def test_redacts_apikey_only(self):
        params = {"apikey": "SECRET123KEY", "module": "account", "address": "0xabc"}
        redacted = _redact(params)
        self.assertEqual(redacted["apikey"], "***REDACTED***")
        self.assertEqual(redacted["module"], "account")
        self.assertEqual(redacted["address"], "0xabc")

    def test_leaves_params_without_apikey_untouched(self):
        params = {"module": "account"}
        self.assertEqual(_redact(params), params)


class TestApiKeyNeverLogged(unittest.TestCase):
    """The one test the API key's confidentiality actually depends on --
    if this ever regresses, the real key would end up in log output that's
    findable via the packaged app's get_backend_logs Tauri command."""

    REAL_KEY = "TOTALLY-SECRET-ETHERSCAN-KEY-98765"

    @patch("ingestion.etherscan_client.requests.get")
    def test_error_log_line_redacts_the_real_key(self, mock_get):
        mock_get.return_value = _fake_response(
            {"status": "0", "message": "NOTOK", "result": "Max rate limit reached"}
        )
        client = EtherscanClient(api_key=self.REAL_KEY, requests_per_second=1000)

        with self.assertLogs("ingestion.etherscan_client", level="WARNING") as cm:
            with self.assertRaises(EtherscanError):
                client._get({"module": "account", "action": "txlist", "address": "0xabc"})

        log_output = "\n".join(cm.output)
        self.assertNotIn(self.REAL_KEY, log_output, "the real API key must never appear in log output")
        self.assertIn("REDACTED", log_output)

    @patch("ingestion.etherscan_client.requests.get")
    def test_no_transactions_found_does_not_raise_or_warn(self, mock_get):
        # "No transactions found" is status "0" too, but must stay a normal
        # empty result, not an error -- and shouldn't spam a warning log for
        # what is expected, common behavior (many addresses genuinely have
        # zero transactions of a given kind).
        mock_get.return_value = _fake_response(
            {"status": "0", "message": "No transactions found", "result": []}
        )
        client = EtherscanClient(api_key=self.REAL_KEY, requests_per_second=1000)
        result = client._get({"module": "account", "action": "txlist", "address": "0xabc"})
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
