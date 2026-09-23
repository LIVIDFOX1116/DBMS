# UI Handoff

Backend capabilities ready for UI work on the `app` branch. For each: what
to call, and the states the UI must handle (loading, error, fallback). See
`docs/API_CONTRACT.md` for full shapes.

## Ready now

### Analysis provenance (`analysis_backend`, `library_version`)
`/clusters`, `/clusters/{cluster_id}`, and `/runs/{run_id}/metrics` now
each carry two additive fields per row:
- `analysis_backend`: `"pandas+networkx (CPU)"` or `"cudf+cugraph (GPU)"` —
  which backend actually produced this run's clusters/metrics.
- `library_version`: the version string of whichever library ran (e.g. a
  NetworkX or cuGraph version) — `null` for any run recorded before this
  field existed (real data: every run so far, since RAPIDS was never
  reachable on any machine this project has run on).

States to handle:
- Both fields can be `null` — either mock mode (no real analysis run
  behind the data at all) or a pre-existing run recorded before this field
  was added. Treat `null` as "provenance unknown," not an error.
- `analysis_backend` is a free-form string, not a fixed enum — display it
  as-is rather than mapping it through a lookup table that could go stale.
- This matters specifically because a wallet clustering tool's CPU and GPU
  Louvain implementations are not guaranteed to produce identical
  partitions on the same data (different algorithms, no shared random
  seed) — if you ever show two runs of the same snapshot side by side, the
  backend/version fields are the way to explain why their clusters differ.

## Pending (not ready — backend work not yet done)
- Device selection dropdown and active-device indicator: waiting on the
  Step 4 device-selection Tauri commands (not yet built, not yet approved
  to start).
- A CPU fallback notice when the GPU fails, based on `run_layout`'s
  `device_used` field (already available today — see API_CONTRACT.md; the
  fallback string is `"cpu (no matching GPU adapter found, fell back)"`)
  or the future Step 4 commands' own fallback reporting.
