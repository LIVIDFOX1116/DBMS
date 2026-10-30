/*
Database view — Feature 1's front end.

An installed FrameCheck starts with demo data and no database at all. This
view is how a user gets from there to live analysis without a terminal:
point the app at a Postgres server, create the database, apply the schema,
look at what is actually in there, and let the app pull wallets from
Etherscan itself.

Three rules it tries to keep honest:

- Never claim to know something it was told. Row counts are labelled as
  estimates; the Etherscan plan is labelled "configured" unless Etherscan
  itself reported one.
- Never hide a state behind a spinner. "Unreachable", "database missing",
  "schema missing" and "migration history unknown" are four different
  situations with four different next actions, so they are four different
  messages.
- Passwords are typed into a Tauri command that stores them in the OS
  keychain, never POSTed from the page and never read back. In a browser,
  where there is no keychain, the fields are disabled rather than silently
  doing something less safe.
*/

const FC_DB_POLL_MS = 1500;
let fcDbPollTimer = null;
let fcDbTables = [];

function fcIsDesktop() {
  return typeof window.__TAURI__ !== "undefined";
}

function fcBytes(n) {
  if (n == null) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n >= 10 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

function fcRows(n, unavailable) {
  if (unavailable || n == null) return "unknown";
  return n.toLocaleString();
}

async function fcDbPost(path, body) {
  // Every database.js mutation goes through here, so this one check covers
  // all of them (Database tab's Create/Schema/Adopt/Mode, import, ingest
  // start/cancel). The server enforces the real 403 regardless (see
  // api/main.py); this just skips the round trip for an error the caller's
  // own catch block would otherwise have to render.
  if (typeof fcPublicDemo !== "undefined" && fcPublicDemo) {
    throw new Error("Read-only public demo: this action is disabled on the shared link.");
  }
  const res = await fetch(apiBase() + path, {
    method: "POST", credentials: "include",
    headers: fcJsonHeaders(),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let payload = {};
  try { payload = await res.json(); } catch { /* empty body is fine */ }
  if (!res.ok) throw new Error(payload.detail || `${res.status} ${res.statusText}`);
  return payload;
}

/* ── status ── */

async function fcOpenDatabaseView() {
  await fcRefreshDbStatus();
  await fcRefreshDatasets();
  await fcRefreshPlan();
  fcResumeIngestJobIfRunning();
}

async function fcRefreshDbStatus() {
  const box = document.getElementById("fcDbStatus");
  box.innerHTML = `<div class="muted">checking…</div>`;
  let status;
  try {
    status = await api("/db/status");
  } catch (err) {
    box.innerHTML = `<div class="fc-err">Could not ask the backend about the database: ${err.message}</div>`;
    return null;
  }
  fcRenderDbStatus(status);
  fcFillSettingsForm(status.settings);
  if (status.schema) await fcRefreshTables();
  return status;
}

function fcRenderDbStatus(status) {
  const s = status.server, schema = status.schema;
  const dsn = status.settings;
  const where = `${dsn.host}:${dsn.port}/${dsn.dbname} as ${dsn.user}`;
  const rows = [];

  rows.push(`<div class="fc-db-row"><span>Mode</span><b>${status.mode === "live" ? "live data" : "demo data"}</b></div>`);
  rows.push(`<div class="fc-db-row"><span>Connection</span><b>${where}</b></div>`);

  // Four distinguishable states, each with the one action that follows.
  let next = "";
  if (!s.reachable) {
    rows.push(`<div class="fc-db-row"><span>Server</span><b class="bad">unreachable</b></div>`);
    rows.push(`<div class="fc-db-detail">${s.error || ""}</div>`);
    next = `<p class="muted">Postgres is not answering at that address. Check it is installed and running,
            then confirm the connection settings below. Demo data keeps working meanwhile.</p>`;
  } else if (!s.database_exists) {
    rows.push(`<div class="fc-db-row"><span>Server</span><b class="ok">reachable</b></div>`);
    rows.push(`<div class="fc-db-row"><span>Database</span><b class="bad">does not exist yet</b></div>`);
    next = `<button class="btn primary" id="fcDbCreate">Create database “${dsn.dbname}”</button>`;
  } else if (!schema || !schema.schema_applied) {
    rows.push(`<div class="fc-db-row"><span>Database</span><b class="ok">exists</b></div>`);
    rows.push(`<div class="fc-db-row"><span>Schema</span><b class="bad">not applied</b></div>`);
    next = `<button class="btn primary" id="fcDbSchema">Import schema (${(schema?.migrations_available || []).length + 1} files)</button>`;
  } else if (schema.state === "untracked") {
    rows.push(`<div class="fc-db-row"><span>Schema</span><b class="ok">applied</b> · ${schema.table_count} tables</div>`);
    rows.push(`<div class="fc-db-row"><span>Migrations</span><b class="warn">history unknown</b></div>`);
    next = `<p class="muted">This database has FrameCheck tables but no migration record, so which migrations
            have run cannot be determined from inside it. If it was built by this project before migration
            tracking existed, adopt it — that records the ${schema.migrations_available.length} migration files
            as applied <em>without running them</em>.</p>
            <button class="btn" id="fcDbAdopt">Adopt this database</button>`;
  } else {
    const pending = schema.migrations_pending || [];
    rows.push(`<div class="fc-db-row"><span>Schema</span><b class="ok">applied</b> · ${schema.table_count} tables</div>`);
    rows.push(`<div class="fc-db-row"><span>Migrations</span>` +
      (pending.length
        ? `<b class="warn">${pending.length} pending</b></div><div class="fc-db-detail">${pending.join(", ")}</div>`
        : `<b class="ok">up to date</b> · ${schema.migrations_applied.length} applied</div>`));
    if (pending.length) next = `<button class="btn primary" id="fcDbSchema">Apply ${pending.length} migration(s)</button>`;
  }

  const canGoLive = s.reachable && s.database_exists && schema && schema.schema_applied;
  const modeBtn = status.mode === "live"
    ? `<button class="btn" id="fcDbMode" data-mode="mock">Switch to demo data</button>`
    : `<button class="btn ${canGoLive ? "primary" : ""}" id="fcDbMode" data-mode="live"
         ${canGoLive ? "" : "disabled title='needs a reachable database with the schema applied'"}>Use live data</button>`;

  const box = document.getElementById("fcDbStatus");
  box.innerHTML =
    `<div class="fc-db-grid">${rows.join("")}</div>
     <div class="fc-db-actions">${next}${modeBtn}</div>`;
  // Every button this function ever renders (Create/Schema/Adopt/Mode) is a
  // mutating action -- disabling every <button> here, rather than touching
  // each of the 4 template strings above, can't drift out of sync with them.
  if (typeof fcPublicDemo !== "undefined" && fcPublicDemo) {
    box.querySelectorAll("button").forEach(b => { b.disabled = true; });
  }

  const bind = (id, fn) => { const el = document.getElementById(id); if (el) el.addEventListener("click", fn); };
  bind("fcDbCreate", () => fcDbAction("/db/create", "Creating database…"));
  bind("fcDbSchema", () => fcDbAction("/db/schema", "Applying schema…"));
  bind("fcDbAdopt", () => fcDbAction("/db/adopt", "Adopting database…"));
  bind("fcDbMode", async (ev) => {
    const mode = ev.currentTarget.dataset.mode;
    try {
      await fcDbPost("/db/mode", { mode });
      toast(mode === "live" ? "Using live data" : "Using demo data", "success");
      await fcRefreshDbStatus();
      // Every view's data just changed underneath it.
      if (typeof loadAll === "function") loadAll();
    } catch (err) { toast(err.message, "error"); }
  });
}

async function fcDbAction(path, pending) {
  const box = document.getElementById("fcDbStatus");
  box.insertAdjacentHTML("afterbegin", `<div class="muted" id="fcDbPending">${pending}</div>`);
  try {
    const out = await fcDbPost(path);
    const applied = out.applied || out.adopted;
    let message = out.detail || (applied ? `Applied: ${applied.join(", ") || "nothing to do"}` : "Done");
    // Adoption can now decline to claim a migration whose work isn't
    // actually in the database. Saying so is the whole point: a silent
    // "adopted everything" is what used to hide a missing migration.
    if (out.pending && out.pending.length) {
      message = `Adopted ${applied.length}; ${out.pending.length} still to apply ` +
        `(${out.pending.join(", ")}) — their objects aren't in this database yet.`;
    }
    toast(message, "success");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    document.getElementById("fcDbPending")?.remove();
    await fcRefreshDbStatus();
  }
}

/* ── connection settings ── */

function fcFillSettingsForm(settings) {
  if (!settings) return;
  const set = (id, value) => { const el = document.getElementById(id); if (el && document.activeElement !== el) el.value = value; };
  set("fcDbHost", settings.host);
  set("fcDbPort", settings.port);
  set("fcDbName", settings.dbname);
  set("fcDbUser", settings.user);
  const pw = document.getElementById("fcDbPassword");
  if (pw) pw.placeholder = settings.password_set ? "saved — leave blank to keep" : "(none set)";
  const note = document.getElementById("fcDbSettingsNote");
  if (note && !fcIsDesktop()) {
    note.textContent = "Connection settings are stored in the operating system's keychain by the desktop app. " +
                       "In a browser there is nowhere safe to keep a password, so set them with environment " +
                       "variables (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD) instead.";
    ["fcDbHost", "fcDbPort", "fcDbName", "fcDbUser", "fcDbPassword", "fcDbSave"].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.disabled = true;
    });
  }
}

async function fcSaveDbSettings() {
  if (!fcIsDesktop()) return;
  const payload = {
    host: document.getElementById("fcDbHost").value.trim(),
    port: document.getElementById("fcDbPort").value.trim() || "5432",
    dbname: document.getElementById("fcDbName").value.trim(),
    user: document.getElementById("fcDbUser").value.trim(),
    password: document.getElementById("fcDbPassword").value,
  };
  try {
    // The password goes to Rust, which puts it in the keychain and pushes
    // the DSN to the sidecar over loopback with the internal token. It is
    // never sent from this page to the API directly.
    await window.__TAURI__.core.invoke("set_db_settings", { settings: payload });
    document.getElementById("fcDbPassword").value = "";
    toast("Connection settings saved", "success");
  } catch (err) {
    toast(`Could not save: ${err}`, "error");
  }
  await fcRefreshDbStatus();
}

/* ── tables ── */

async function fcRefreshTables() {
  const box = document.getElementById("fcDbTables");
  try {
    const out = await api("/db/tables");
    fcDbTables = out.tables || [];
  } catch (err) {
    box.innerHTML = `<div class="fc-err">${err.message}</div>`;
    return;
  }
  if (!fcDbTables.length) {
    box.innerHTML = `<div class="muted">No tables yet.</div>`;
    return;
  }
  const withRows = fcDbTables.filter(t => (t.rows || 0) > 0).length;
  box.innerHTML = `
    <div class="muted" style="margin-bottom:8px;">
      ${fcDbTables.length} tables, ${withRows} with data · row counts are
      <abbr title="Read from Postgres' own statistics (pg_class.reltuples), maintained by ANALYZE. Counting every row of every table took 6.85 s on this database, too slow to do on every visit.">estimates</abbr>
    </div>
    <div class="table-wrap" style="max-height:340px;">
      <table class="dtable"><thead><tr>
        <th>Table</th><th style="text-align:right;">Rows (est.)</th><th style="text-align:right;">Size</th><th></th>
      </tr></thead><tbody>
        ${fcDbTables.map(t => `<tr>
          <td><code>${t.table}</code></td>
          <td style="text-align:right;" id="fcRows-${t.table}">${fcRows(t.rows, t.estimate_unavailable)}</td>
          <td style="text-align:right;">${fcBytes(t.bytes)}</td>
          <td style="text-align:right;white-space:nowrap;">
            <button class="btn tiny fc-db-count" data-table="${t.table}">count</button>
            <button class="btn tiny fc-db-peek" data-table="${t.table}">view</button>
          </td></tr>`).join("")}
      </tbody></table>
    </div>
    <div id="fcDbPeek"></div>`;

  box.querySelectorAll(".fc-db-count").forEach(b => b.addEventListener("click", async () => {
    const cell = document.getElementById(`fcRows-${b.dataset.table}`);
    cell.textContent = "counting…";
    try {
      const out = await api(`/db/tables/${b.dataset.table}/count`);
      cell.innerHTML = `${out.rows.toLocaleString()} <span class="muted">exact</span>`;
    } catch (err) {
      cell.textContent = "failed";
      toast(err.message, "error");
    }
  }));
  box.querySelectorAll(".fc-db-peek").forEach(b => b.addEventListener("click", () => fcPeekTable(b.dataset.table)));
}

async function fcPeekTable(table) {
  const box = document.getElementById("fcDbPeek");
  box.innerHTML = `<div class="muted">loading ${table}…</div>`;
  let out;
  try {
    out = await api(`/db/tables/${table}`, { limit: 50 });
  } catch (err) {
    box.innerHTML = `<div class="fc-err">${err.message}</div>`;
    return;
  }
  if (!out.rows.length) {
    box.innerHTML = `<h4 style="margin:14px 0 6px;"><code>${table}</code></h4><div class="muted">This table is empty.</div>`;
    return;
  }
  box.innerHTML = `
    <h4 style="margin:14px 0 6px;"><code>${table}</code> <span class="muted">first ${out.rows.length} rows</span></h4>
    <div class="table-wrap" style="max-height:300px;overflow:auto;">
      <table class="dtable"><thead><tr>${out.columns.map(c => `<th>${c}</th>`).join("")}</tr></thead>
      <tbody>${out.rows.map(r => `<tr>${r.map(v => `<td>${fcCell(v)}</td>`).join("")}</tr>`).join("")}</tbody></table>
    </div>`;
}

function fcCell(v) {
  if (v === null || v === undefined) return `<span class="muted">null</span>`;
  const text = String(v);
  const escaped = text.replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  return text.length > 44 ? `<span title="${escaped}">${escaped.slice(0, 44)}…</span>` : escaped;
}

/* ── automatic ingestion ── */

async function fcRefreshPlan() {
  const box = document.getElementById("fcIngestPlan");
  if (!box) return;
  let plan;
  try {
    plan = await api("/ingest/plan");
  } catch (err) {
    box.innerHTML = `<div class="fc-err">${err.message}</div>`;
    return;
  }
  if (!plan.api_key_set) {
    box.innerHTML = `<div class="muted">No Etherscan API key saved. Add one in Settings (top right) and the app
                     can pull wallet data itself. <a href="https://etherscan.io/apis" target="_blank" rel="noreferrer">Free keys</a>
                     are an email signup.</div>`;
    document.getElementById("fcIngestStart").disabled = true;
    return;
  }
  document.getElementById("fcIngestStart").disabled = false;
  const rpsField = document.getElementById("fcIngestRps");
  if (rpsField && !rpsField.value) rpsField.value = plan.requests_per_second;

  // Say where the number came from. Etherscan only reports an allowance for
  // paid keys, so for most users this is the app's own setting and must not
  // be presented as a detected plan.
  if (plan.source === "etherscan") {
    const r = plan.reported_by_etherscan;
    box.innerHTML = `<div>Etherscan reports this key's allowance:
      <b>${Object.entries(r).map(([k, v]) => `${k}: ${v}`).join(" · ")}</b></div>`;
  } else {
    box.innerHTML = `<div class="muted">Etherscan does not report an allowance for this key (it only does so for
      paid plans), so the app uses the rate below — currently <b>${plan.requests_per_second}/second</b>, chosen to
      stay under the free tier's documented ceiling. If you are on a paid plan, raise it.</div>`;
  }
}

async function fcStartIngest() {
  const body = {
    address: document.getElementById("fcIngestAddress").value.trim() || null,
    hops: Number(document.getElementById("fcIngestHops").value) || 1,
    requests_per_second: Number(document.getElementById("fcIngestRps").value) || null,
    max_calls: Number(document.getElementById("fcIngestBudget").value) || null,
  };
  const status = document.getElementById("fcIngestStatus");
  try {
    const job = await fcDbPost("/ingest/start", body);
    fcTrackIngestJob(job.job_id);
  } catch (err) {
    status.innerHTML = `<div class="fc-err">${err.message}</div>`;
  }
}

function fcTrackIngestJob(jobId) {
  clearInterval(fcDbPollTimer);
  const status = document.getElementById("fcIngestStatus");
  document.getElementById("fcIngestStart").disabled = true;
  const tick = async () => {
    let job;
    try {
      job = await api(`/jobs/${jobId}`);
    } catch (err) {
      clearInterval(fcDbPollTimer);
      status.innerHTML = `<div class="fc-err">${err.message}</div>`;
      document.getElementById("fcIngestStart").disabled = false;
      return;
    }
    const r = job.result || {};
    const done = ["done", "failed", "cancelled"].includes(job.status);
    status.innerHTML = `
      <div class="fc-db-row"><span>Status</span><b>${job.status}</b></div>
      <div class="fc-db-detail">${job.error || job.message || ""}</div>
      <div class="fc-db-grid" style="margin-top:6px;">
        <div class="fc-db-row"><span>Addresses ingested</span><b>${(r.addresses_processed ?? job.completed ?? 0).toLocaleString()}</b></div>
        <div class="fc-db-row"><span>Hops completed</span><b>${r.hops_completed ?? 0}</b></div>
        <div class="fc-db-row"><span>API calls used</span><b>${(r.calls_made ?? 0).toLocaleString()}${
          r.calls_remaining != null ? ` <span class="muted">(${r.calls_remaining.toLocaleString()} left in budget)</span>` : ""}</b></div>
      </div>
      ${done ? "" : `<button class="btn" id="fcIngestCancel" ${fcPublicDemo ? "disabled" : ""}>Stop</button>`}`;
    if (!done) {
      document.getElementById("fcIngestCancel").addEventListener("click", async () => {
        await fcDbPost(`/jobs/${jobId}/cancel`).catch(err => toast(err.message, "error"));
        toast("Stopping after the current address", "info");
      });
    } else {
      clearInterval(fcDbPollTimer);
      document.getElementById("fcIngestStart").disabled = false;
      // Ingestion changed the data every other view is showing.
      await fcRefreshTables();
      if (typeof loadAll === "function") loadAll();
    }
  };
  tick();
  fcDbPollTimer = setInterval(tick, FC_DB_POLL_MS);
}

async function fcResumeIngestJobIfRunning() {
  // The backend owns the job, not this page: re-opening the view (or
  // reloading the app) should pick up a crawl that is still going.
  try {
    const list = await api("/jobs");
    const live = list.find(j => j.job_type === "ingest" && ["queued", "running"].includes(j.status));
    if (live) fcTrackIngestJob(live.job_id);
  } catch { /* the view still works without it */ }
}

/* ── importing a dataset ── */

async function fcRefreshDatasets() {
  const box = document.getElementById("fcDbDatasets");
  if (!box) return;
  let out;
  try {
    out = await api("/db/datasets");
  } catch (err) {
    box.innerHTML = `<div class="fc-err">${err.message}</div>`;
    return;
  }
  if (!out.demo) {
    box.innerHTML = `<div class="muted">This build does not include a demo dataset.
                     Export one from an existing database with
                     <code>python scripts/export_dataset.py --snapshot-id 1 --output datasets/&lt;name&gt;.sql.gz</code>.</div>`;
    return;
  }
  const m = out.demo.manifest;
  box.innerHTML = `
    <div class="fc-db-grid">
      <div class="fc-db-row"><span>Demo dataset</span><b>${m.snapshot_name}</b></div>
      <div class="fc-db-row"><span>Contents</span><b>${m.total_rows.toLocaleString()} rows ·
        ${m.vertex_count.toLocaleString()} wallets · ${m.edge_count.toLocaleString()} edges</b></div>
      <div class="fc-db-row"><span>File</span><b>${fcBytes(out.demo.bytes)}</b></div>
    </div>
    <div class="fc-db-actions"><button class="btn primary" id="fcImportDemo"
      ${fcPublicDemo ? "disabled" : ""}>Import the demo dataset</button></div>`;
  document.getElementById("fcImportDemo").addEventListener("click", () => fcImportDataset("demo"));
}

async function fcImportDataset(source) {
  const status = document.getElementById("fcImportStatus");
  const body = { source };
  if (source === "file") {
    const path = document.getElementById("fcImportPath").value.trim();
    if (!path) { toast("Enter the path to a dataset file first", "error"); return; }
    body.path = path;
  }
  // No progress events: the import is one transaction on the server, and a
  // half-reported import would be worse than an honest wait. The measured
  // time for the bundled dataset is ~9 s.
  status.innerHTML = `<div class="muted">Importing… this takes about 10 seconds for the demo dataset,
                      and the whole thing is one transaction — nothing is left half-loaded if it fails.</div>`;
  ["fcImportDemo", "fcImportFile"].forEach(id => { const b = document.getElementById(id); if (b) b.disabled = true; });
  try {
    const out = await fcDbPost("/db/import", body);
    const skipped = Object.keys(out.skipped_seed_tables || {});
    const remapped = Object.values(out.remapped_ids || {}).reduce((a, b) => a + b, 0);
    status.innerHTML = `
      <div class="fc-db-grid">
        <div class="fc-db-row"><span>Imported</span><b class="ok">${out.total_rows.toLocaleString()} rows
          across ${Object.keys(out.loaded).length} tables</b></div>
        ${skipped.length ? `<div class="fc-db-row"><span>Kept as they were</span><b>${skipped.join(", ")}</b></div>` : ""}
        ${remapped ? `<div class="fc-db-row"><span>Ids translated</span><b>${remapped}</b>
          <span class="muted">(this database numbers its algorithms differently)</span></div>` : ""}
      </div>`;
    toast(`Imported ${out.total_rows.toLocaleString()} rows`, "success");
    await fcRefreshDbStatus();
    if (typeof loadAll === "function") loadAll();
  } catch (err) {
    status.innerHTML = `<div class="fc-err">${err.message}</div>`;
  } finally {
    ["fcImportDemo", "fcImportFile"].forEach(id => { const b = document.getElementById(id); if (b) b.disabled = false; });
  }
}
