"""
Thin wrapper around the Etherscan v2 API (https://docs.etherscan.io).

Only the two endpoints this project needs are implemented:
  - account/txlist   -> native ETH transactions (maps to the `transaction` table)
  - account/tokentx  -> ERC-20 transfers        (maps to `token_transfer`)

Field names in the returned dicts match Etherscan's documented response schema
exactly (blockNumber, timeStamp, hash, from, to, value, ... / contractAddress,
tokenName, tokenSymbol, tokenDecimal, ...) -- verified against the current API
docs rather than assumed, since this is exactly the kind of detail that goes
stale silently.

Requires network access to api.etherscan.io, so it will NOT run inside a
sandboxed dev environment without internet egress -- run this on a machine
with normal internet access (e.g. the Zenbook). See tests/fixtures/ for a
canned sample response used to unit-test the parsing/DB logic offline.
"""
import logging
import time

import requests

from . import config

log = logging.getLogger("ingestion.etherscan_client")


def _redact(params: dict) -> dict:
    """Never let the real API key reach a log line -- it travels in the
    request query string (Etherscan's API has no header-auth option), so
    anything that logs params or a constructed URL must go through this
    first."""
    return {**params, "apikey": "***REDACTED***"} if "apikey" in params else dict(params)


class EtherscanError(RuntimeError):
    pass


class EtherscanClient:
    def __init__(self, api_key: str = None, requests_per_second: float = None):
        self.api_key = api_key or config.ETHERSCAN_API_KEY
        self.min_interval = 1.0 / (requests_per_second or config.REQUESTS_PER_SECOND)
        self._last_call = 0.0

    def _throttle(self):
        elapsed = time.time() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.time()

    def _get(self, params: dict) -> list:
        params = {
            "chainid": config.CHAIN_ID,
            "apikey": self.api_key,
            **params,
        }
        self._throttle()
        resp = requests.get(config.ETHERSCAN_BASE_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # Etherscan returns status "0" both for real errors and for
        # "no records found" -- treat "No transactions found" as an empty
        # result rather than an error.
        if data.get("status") == "0" and data.get("message") != "No transactions found":
            log.warning("Etherscan error for %s: %s", _redact(params), data.get("message"))
            raise EtherscanError(f"{data.get('message')}: {data.get('result')}")
        result = data.get("result")
        return result if isinstance(result, list) else []

    def get_normal_transactions(self, address: str, max_results: int = None, sort: str = "asc") -> list:
        """account/txlist -- native ETH transactions for `address`."""
        max_results = max_results or config.MAX_TX_PER_ADDRESS
        out, page, offset = [], 1, 100
        while len(out) < max_results:
            batch = self._get({
                "module": "account", "action": "txlist",
                "address": address, "startblock": 0, "endblock": 999999999,
                "page": page, "offset": offset, "sort": sort,
            })
            out.extend(batch)
            if len(batch) < offset:
                break  # last page
            page += 1
        return out[:max_results]

    def get_token_transfers(self, address: str, max_results: int = None, sort: str = "asc") -> list:
        """account/tokentx -- ERC-20 transfers for `address`."""
        max_results = max_results or config.MAX_TX_PER_ADDRESS
        out, page, offset = [], 1, 100
        while len(out) < max_results:
            batch = self._get({
                "module": "account", "action": "tokentx",
                "address": address, "startblock": 0, "endblock": 999999999,
                "page": page, "offset": offset, "sort": sort,
            })
            out.extend(batch)
            if len(batch) < offset:
                break
            page += 1
        return out[:max_results]

    def get_code(self, address: str) -> str:
        """proxy/eth_getCode -- the one reliable EOA-vs-contract signal.
        txlist/tokentx never say which an address is; ingest_subgraph.py only
        infers CONTRACT for addresses discovered *as* an ERC20 token, so
        anything reached solely through native transactions defaults to EOA
        even when it's obviously a contract (e.g. the Uniswap V2 Router).

        Doesn't reuse _get(): the proxy module returns a raw JSON-RPC shape
        (`{"result": "0x..."}`, a string) rather than the account module's
        `{"status", "message", "result": [...]}` shape -- _get() hard-codes
        `result if isinstance(result, list) else []`, which would silently
        discard a valid hex-string result here.
        """
        self._throttle()
        params = {
            "chainid": config.CHAIN_ID, "apikey": self.api_key,
            "module": "proxy", "action": "eth_getCode",
            "address": address, "tag": "latest",
        }
        resp = requests.get(config.ETHERSCAN_BASE_URL, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("result") or "0x"
