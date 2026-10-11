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


class BudgetExhausted(RuntimeError):
    """The configured call budget ran out. Not an error in the usual sense --
    the caller is expected to stop cleanly and report how far it got, which
    is why it is its own type and not an EtherscanError."""


class EtherscanClient:
    def __init__(self, api_key: str = None, requests_per_second: float = None, max_calls: int = None):
        self.api_key = api_key or config.ETHERSCAN_API_KEY
        self.min_interval = 1.0 / (requests_per_second or config.REQUESTS_PER_SECOND)
        self._last_call = 0.0
        # Call accounting, for the app's automatic ingestion: a user on the
        # free plan has a per-second rate AND a daily allowance, and the
        # throttle above only addresses the first. `calls_made` is what the
        # UI shows; `max_calls` is the budget the job stops at rather than
        # burning through someone's whole day of API calls unasked.
        self.calls_made = 0
        self.max_calls = max_calls

    def calls_remaining(self):
        """None when no budget was set -- distinct from 0, which means the
        budget is used up."""
        return None if self.max_calls is None else max(0, self.max_calls - self.calls_made)

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
        if self.max_calls is not None and self.calls_made >= self.max_calls:
            raise BudgetExhausted(f"call budget of {self.max_calls} used up")
        self._throttle()
        self.calls_made += 1
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


    def get_api_limit(self) -> dict | None:
        """What Etherscan itself says this key's allowance is, or None if it
        will not say.

        Etherscan exposes a plan/allowance lookup only for paid keys; a free
        key gets an error back. This is therefore written to DEGRADE, not to
        be relied on: any non-answer -- unsupported endpoint, error status,
        unexpected shape, network failure -- returns None, and the caller
        falls back to the rate the user configured. The app never claims to
        know a plan it was not told about.
        """
        params = {"chainid": config.CHAIN_ID, "apikey": self.api_key,
                  "module": "getapilimit", "action": "getapilimit"}
        try:
            self._throttle()
            self.calls_made += 1
            resp = requests.get(config.ETHERSCAN_BASE_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            log.info("Etherscan did not report an API limit for this key: %s", e)
            return None
        result = data.get("result")
        if data.get("status") != "1" or not isinstance(result, dict):
            log.info("Etherscan did not report an API limit for this key: %s", data.get("message"))
            return None
        return result
