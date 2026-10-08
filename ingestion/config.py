"""
Shared configuration for the ingestion pipeline. All values are overridable
via environment variables / .env so the case-study address and hop count are
a one-line change, not a code change (see project bible, Section 7.2 -- the
case-study address is a joint decision, keep it in config, not hardcoded deep
in the crawl logic).
"""
import os

from dotenv import load_dotenv

load_dotenv()

# --- Etherscan API ---
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
ETHERSCAN_BASE_URL = "https://api.etherscan.io/v2/api"
CHAIN_ID = int(os.getenv("CHAIN_ID", "1"))  # 1 = Ethereum mainnet

# --- Case study (Phase 0.5 in the project bible) ---
# Poly Network attacker address #1 (August 2021 cross-chain DeFi exploit).
# Chosen because it is extensively publicly documented (Chainalysis, Elliptic,
# SlowMist, Merkle Science all published fund-flow analyses), the outcome is
# resolved (all funds were returned within ~15 days, so there's no ongoing
# legal/sanctions entanglement), and the fan-out/consolidation pattern of the
# stolen-fund movement gives PageRank, Louvain, and BFS all something real to
# find. Swap this to any other address without touching ingestion logic.
CASE_STUDY_ADDRESS = os.getenv(
    "CASE_STUDY_ADDRESS", "0xC8a65Fadf0e0dDAf421F28FEAb69Bf6E2E589963"
).lower()

# How many hops out from the seed address to crawl. Keep this small -- hop
# count grows the subgraph combinatorially, and the smaller GPU (GTX 1650,
# ~4GB VRAM) is the real budget constraint, not the bigger one.
HOP_COUNT = int(os.getenv("HOP_COUNT", "2"))

# Etherscan free tier: 5 calls/sec, 100k calls/day, max 10,000 tx per address
# via pagination. Cap per-address pulls so one heavily-used hub wallet (e.g.
# an exchange) can't blow the whole crawl budget.
MAX_TX_PER_ADDRESS = int(os.getenv("MAX_TX_PER_ADDRESS", "500"))
REQUESTS_PER_SECOND = float(os.getenv("REQUESTS_PER_SECOND", "4"))  # stay under the 5/s cap

# --- Postgres ---
PG_DSN = {
    "host": os.getenv("PGHOST", "localhost"),
    "port": os.getenv("PGPORT", "5432"),
    "dbname": os.getenv("PGDATABASE", "framecheck"),
    "user": os.getenv("PGUSER", "postgres"),
    "password": os.getenv("PGPASSWORD", ""),
}
