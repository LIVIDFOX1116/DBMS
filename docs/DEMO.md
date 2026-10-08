# FrameCheck demo runbook

One story: **a flag on a wallet can be manufactured by an outsider's unsolicited transfers (frameable), or it can depend on the wallet's own signed behavior (robust).** FrameCheck audits each flag and says which, and what the cheapest frame costs.

Prior work: Liu et al., "Evasion Under Blockchain Sanctions" (WWW '26) introduced dusting attacks and the proportional impurity score; FrameCheck audits those flags, it does not claim to have invented them.

## Startup order

Mock mode needs only steps 3-4 (zero setup). Live mode needs all of them.

1. **Postgres (live only; WSL2 Ubuntu-24.04, PG16, localhost:5432, the database named by `PGDATABASE` in `.env`, `txgraph` on this machine).** Windows can only reach it while a WSL session stays attached:
   `wsl -d Ubuntu-24.04 -- sleep 21600` (leave it running).
2. **`.env`:** `FRAMECHECK_API_MODE=live` (live) or `mock` (default). Never commit or show `.env`.
3. **API:** `uvicorn api.main:app --port 8000`
4. **UI:** either
   - browser: `python -m http.server 5500 --directory frontend`, or
   - desktop app: `python scripts/build_backend_sidecar.py` once, then `cargo tauri dev` from `src-tauri/`.

Close other heavy apps first (16 GB machine; the crawl, GPU jobs and the app have been OOM-killed together before).

## URLs

| What | URL |
|---|---|
| Landing page | http://localhost:5500 |
| Dashboard | http://localhost:5500/dashboard.html (Audit tab) |
| API health | http://localhost:8000/health |
| Swagger | http://localhost:8000/docs |
| Audit summary | http://localhost:8000/audit/summary |

## Flag IDs to show

| Story | Mock mode | Live mode (this machine's database, snapshot 1) |
|---|---|---|
| **Frameable** | `flag_id=1` (bfs_proximity, exact cost 0.004365 ETH) | `flag_id=7` (wallet 4, bfs_proximity, `exact`, 0.004365 ETH) |
| **Robust** | `flag_id=2` (sybil_cluster, yes_no) | `flag_id=75` (wallet 401, sybil_cluster, `yes_no`) |

Endpoints: `/flags` (list), `/flags/{id}/evidence`, `/flags/{id}/audit`.

Say out loud: wallet 401 carries *both* a frameable bfs_proximity flag (flag_id 1) and a robust sybil flag (flag_id 75). The first came from money that merely arrived; the second disappears when its own signed transactions are removed.

Live ids are specific to this machine's database (the upstream DB used for RESULTS.md has different ids, seed wallet 1476). If the DB changes, look them up:
`SELECT f.flag_id, f.wallet_id, f.detector, a.verdict, a.method FROM flag_audit a JOIN flag f USING(flag_id);`

## Headline numbers (snapshot 5, 30,322 wallets, 47,884 edges, seed wallet 1476; see docs/RESULTS.md)

- 61 of 61 audited `bfs_proximity` flags are frameable; framing cost 0.000189 to 13.37 ETH, median 0.0101 ETH.
- `pagerank_threshold`: 29 of 29 frameable, 0.00043 to 149 ETH.
- `sybil_cluster`: sample of 52, 9.6% frameable, 90.4% robust, but unstable across reruns (2 to 18 of 20 synthetic victims flagged); do not quote a single run.
- `engaged_proximity` is the one that separates launderers (robust) from dusted victims (not flagged).

Mock mode (`api/mock_data.json`) serves these same snapshot-5 audit summary figures and snapshot size, with the two example flags above.

## Known limits to state

- Ingestion never calls Etherscan's `txlistinternal`: contract-initiated (internal) transfers are invisible to the pipeline and to every audit.
- Greedy framing costs are upper bounds (`greedy_upper_bound`), only `exact` is a true minimum; `sybil_cluster` has no cost, only yes/no.
- Evidence collection has a performance cliff on high-in-degree wallets; baseline in `docs/perf/evidence_before.md` (fix planned live).
- Framing is simulation only; no real transactions are ever sent.
