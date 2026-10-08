# FrameCheck

## What this project is

**FrameCheck** is a GPU-accelerated Ethereum transaction-graph analyzer and framing audit:
Etherscan ingestion -> PostgreSQL (18-table schema) -> Parquet snapshots -> PageRank/Louvain/BFS
via cuGraph with a pandas/NetworkX CPU fallback -> five flag detectors -> an audit layer -> FastAPI
-> a dashboard + Tauri desktop app. Full rationale for the original analyzer lives in
`docs/Project_Bible_TransactionGraphAnalyzer.docx`; this file is about how to work in the repo, not
why it exists. (The repo is named `DBMS` on GitHub and a local database may still be called `txgraph`, set via `PGDATABASE` — the
project itself, every endpoint, every page, and the desktop app are FrameCheck.)

The audit layer is what makes this FrameCheck rather than just a graph analyzer: on Ethereum,
anyone can send funds to any wallet and the receiver cannot refuse it — so an attacker can make an
innocent wallet look connected to a hack just by sending it dust. FrameCheck audits every flag its
detectors raise and answers: could an outsider have produced this flag without the wallet owner
doing anything, and how cheaply? Originally built in a 24-hour hackathon (DBTHON, 2 PM 7 Oct - 2 PM
8 Oct 2026) as a layer added to an existing analyzer; the project has since been rebranded
end-to-end as FrameCheck.

FrameCheck builds on, and must never claim to compete with or have invented, Liu et al., "Evasion
Under Blockchain Sanctions" (WWW '26) — dusting attacks and the proportional "impurity" score
(`impurity_wei / balance_wei`). Their impurity score is one of the detectors FrameCheck audits, not
something FrameCheck discovered. Docs must cite this prior work and must not claim credit for
dusting attacks, taint models, or counterfactual explanations in general.

## Definitions (use these exact terms in code, docs, and UI)

- **Signed transfer**: a transaction whose signer (`transaction.from_wallet_id`) is the flagged
  wallet `W`.
- **Unsolicited transfer**: a transfer INTO `W` that `W` did not sign. The signer always comes from
  the `transaction` row — never use `token_transfer.from_wallet_id` as a signer; it's populated
  from ERC-20 Transfer event logs, which spam contracts can fake. The real signer of any transfer
  that shows up as a `token_transfer` row is still `transaction.from_wallet_id`, joined via
  `tx_hash` — never `token_transfer.from_wallet_id` itself.
- **Frameable flag**: the flag still holds on the graph with every transaction SIGNED BY `W`
  removed (`W` passive). If a flag survives with `W` contributing nothing of its own, an
  attacker's unsolicited inbound transfers alone were sufficient to produce it. (Corrected
  2026-10-06: the original definition here — "remove unsolicited inbound transfers, check if the
  flag disappears" — was wrong. A real launderer's flag also rests on inbound transfers it didn't
  sign, so that test called almost every flag frameable.)
- **Robust flag**: the flag disappears when `W`'s signed transactions are removed. The flag
  depended on `W`'s own active behavior, which an attacker cannot forge — not on money that
  merely arrived.
- **Framing cost**: for a frameable flag, start from the graph with BOTH `W`'s signed
  transactions AND `W`'s unsolicited inbound transfers removed, then add real unsolicited inbound
  transfers back cheapest-first (`value_wei` + gas) until the flag reappears, then prune any not
  needed. Greedy results are an upper bound — always label them as such, never as the true
  minimum unless the method is `exact`.
- **Threat model**: the attacker can send any amount to any wallet and pays value + gas per
  transfer. The attacker can only ADD transfers into the target. The attacker cannot sign for the
  target and cannot remove or reorder existing history.

## Repo layout

```
db/            schema.sql (PostgreSQL DDL) + init_db.py + migrations/ (FrameCheck additions, new)
ingestion/     Etherscan client + BFS subgraph crawler
graph/         backend.py (GPU/CPU abstraction), export_snapshot.py, run_analysis.py, score_wallets.py,
               audit.py (FrameCheck audit engine, new), evidence.py (FrameCheck evidence collection, new)
api/           FastAPI app + mock_data.json (the frontend's day-one contract)
frontend/      index.html (landing page) + dashboard.html (wallets/clusters/alerts/graph/audit), no build step
scripts/       verify_gpu_env.py, run_pipeline.py, benchmark.py, benchmark_audit.py (FrameCheck, new),
               simulate_framing.py (FrameCheck framing simulator, new), promote_to_main.py,
               backend_entrypoint.py + build_backend_sidecar.py
tests/         offline unit tests -- run with `python -m pytest tests/ -v`
docs/          ER diagram (SVG + DBML), API_CONTRACT.md, UI_HANDOFF.md, benchmark_results.md,
               FRAMECHECK.md (new), and the full project bible
src-tauri/     optional native desktop wrapper (Windows) around the frontend + API
```

## How to run things

```bash
# Tests (fully offline -- see Standing rules below)
python -m pytest tests/ -v

# API (mock mode needs zero setup; live mode needs Postgres + a real pipeline run)
uvicorn api.main:app --reload --port 8000

# Real pipeline, step by step
python -m ingestion.ingest_subgraph --hops 2
python -m graph.export_snapshot --name <snapshot_name> --start-block 0 --end-block 999999999
python -m graph.run_analysis --snapshot-id <id> --seed-wallet-id <id>

# Real pipeline, one command
python scripts/run_pipeline.py

# Benchmark (algorithm timing; FrameCheck adds persistence timing alongside it)
python scripts/benchmark.py --repeats 5 --output docs/benchmark_results.md
```

See the README for the full first-time setup (venv, `.env`, Postgres, the desktop app's sidecar
build) — this file doesn't repeat it.

## Standing rules

- **Tests stay fully offline.** No network, no live Postgres, no GPU required to run
  `python -m pytest tests/ -v`. Keep the CPU fallback working everywhere — GPU paths are additive,
  never a hard requirement for anything to run or test.
- **Framing is simulation only.** Never send a real transaction or touch any live network as part
  of FrameCheck. The framing simulator (`scripts/simulate_framing.py`) only ever inserts synthetic
  rows into the local DB, tagged `transaction.is_simulated = true`, and must be fully reversible
  via its `--cleanup` flag.
  - Known limitation worth stating plainly here: ingestion never calls Etherscan's internal-
    transactions endpoint (`txlistinternal`) — contract-initiated transfers (e.g. funds moved by a
    smart contract's internal logic, not a directly signed external transaction) are invisible to
    this pipeline and therefore to every FrameCheck audit built on top of it. Any limitations
    section in FrameCheck's own docs must say so.
- **Never run destructive DB operations** (`DROP`, `TRUNCATE`, `DELETE` without a `WHERE`) without
  asking first.
- **Commit to `dev` in small, focused commits.** Run `python -m pytest tests/ -v` (and the Rust
  suite, `cargo test --manifest-path src-tauri/Cargo.toml`, when Rust files changed) before every
  commit. Never run `scripts/promote_to_main.py` or push to `main` without asking first.
- **Don't break the Tauri desktop app or existing API endpoints.** Extend, don't rename — the
  "additive only" rule in `docs/API_CONTRACT.md` applies to FrameCheck's new endpoints too.
