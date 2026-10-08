# Stability log

Each phase ends with a stability pass: launch, click through, hit endpoints, try bad inputs; fix and log here.

## Phase 1: CI/CD (2026-10-08)

Scope: CI only, no app code changed beyond lint fixes, so the pass checks the pipeline from a clean checkout.

| Check | Result |
|---|---|
| `ruff check .` locally and in CI | clean (22 import-order fixes, 1 `raise ... from None`, 1 unused variable, `E402` ignored in `scripts/` where it follows `stdout.reconfigure`) |
| pytest offline in CI (ubuntu, no Postgres/GPU/secrets) | 144 passed, 5 skipped |
| `cargo test` in CI (Windows, empty placeholder sidecar) | 29 passed |
| Installer job from a clean runner: PyInstaller sidecar, `cargo tauri build --bundles nsis`, artifact upload | success; artifact `framecheck-installer` uploaded |
| `release.yml` | not exercised: it only fires on a `v*` tag, and a tag would publish a public Release |

Findings: none outstanding. Pass clean.

## Phase 2: FrameCheck UI (2026-10-08)

Method: headless Chrome driven over the DevTools protocol (the Chrome extension was not connected), against the mock API and the live API (Postgres in WSL2, snapshot with 1,775 flags). Views visited: Overview, Flags, Wallets, Clusters, Alerts, Graph (with and without a flag overlay). Uncaught exceptions and console errors were collected on every run.

| Check | Result |
|---|---|
| Mock: every view, flag select, "View in graph", clear overlay | no exceptions, no console errors |
| Live: same click-through, 100 flags listed, flag 1 overlay (495 edges, 81 wallets) | no exceptions, no console errors |
| API down (port closed) | Overview and Flags show an error card with Retry; header says "API unreachable"; only the browser's own connection-refused lines in the console |
| Unknown flag id: `/flags/999999/evidence-edges` -> 200 `[]`; `/flags/999999/audit` -> 404; non-numeric id -> 422; `/audit/overview?snapshot_id=99999` -> 200 empty | handled, UI shows "no evidence" |
| Live SQL of `/audit/overview`, `/audit/verdicts`, `/flags/{id}/evidence-edges` | verified against the real DB |

Findings and fixes:
1. The newest snapshot (id 3) has no flags, so the Flags view was empty. The UI now uses the newest snapshot that has flags.
2. One `/flags/{id}/audit` request per flag logged ~100 console 404s for unaudited flags. Added additive `GET /audit/verdicts` (latest audit per flag, one call) and the list uses it.
3. The evidence overlay's layout diverged (coordinates ~1e122, blank canvas) because parallel transfers between one pair multiplied the spring force. Layout now receives each wallet pair once.

Not covered: the desktop (Tauri) window was not launched in this pass; it loads the same `frontend/` files and the unchanged sidecar API. Postgres-down was not retested (unchanged from Phase 1 behaviour). Pass clean for the browser.
