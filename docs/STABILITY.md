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
