/* FrameCheck audit UI: Overview metrics, the Flags view (flag -> evidence ->
   verdict -> framing cost) and the Graph view's flag-evidence overlay.
   Plain script, no build step. Uses globals from dashboard.html: api(),
   go(), toast(), shortAddr(), layoutGraph(), GraphRenderer, detectGpuAdapter(),
   graphRenderer, renderGraph, renderGraphGeneration. */

const FC_DETECTORS = {
  bfs_proximity: "BFS proximity",
  sybil_cluster: "Sybil cluster",
  pagerank_threshold: "PageRank threshold",
  impurity: "Impurity",
  engaged_proximity: "Engaged proximity",
};
const FC_METHOD = {
  exact: { label: "Exact", note: "True minimum cost to frame this wallet." },
  greedy_upper_bound: { label: "Upper bound (greedy)", note: "A greedy search found this frame. The true minimum may be lower." },
  yes_no: { label: "Yes / no only", note: "This detector's audit gives a verdict but no framing cost." },
};
const FC_SIGNED_COLOR = "#4C9AFF";
const FC_UNSOLICITED_COLOR = "#E5675A";

const FC = { snapshotId: undefined, filter: "all", flags: {}, selected: null, detailGen: 0, loadGen: 0 };

const fcEsc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fcDetectorName = d => FC_DETECTORS[d] || d;

function fcEth(wei) {
  if (wei == null) return "–";
  const eth = Number(wei) / 1e18;
  return `${eth >= 1 ? eth.toFixed(4) : eth.toFixed(6)} ETH`;
}

function fcErrorBlock(message, retryAction) {
  if (/failed to fetch|networkerror/i.test(message)) message = `The API is not reachable at ${apiBase()}. Is it running?`;
  return `<div class="fc-error"><div class="fc-error-title">Could not load this</div>
    <div class="fc-error-msg">${fcEsc(message)}</div>
    <button class="btn" data-fc-retry="${fcEsc(retryAction)}">Retry</button></div>`;
}

/* ── snapshot ── */
async function fcSnapshotId() {
  if (FC.snapshotId !== undefined) return FC.snapshotId;
  const snapshots = await api("/snapshots");
  FC.snapshotId = snapshots.length ? snapshots[0].snapshot_id : null;
  const label = document.getElementById("auditSnapshotLabel");
  if (label) label.textContent = FC.snapshotId != null ? `(snapshot ${FC.snapshotId})` : "";
  return FC.snapshotId;
}

/* ── Overview ── */
function fcSetMetrics(flags, audited, frameable, median, auditedNote, frameableNote) {
  document.getElementById("fcFlags").textContent = flags;
  document.getElementById("fcAudited").textContent = audited;
  document.getElementById("fcFrameable").textContent = frameable;
  document.getElementById("fcMedian").textContent = median;
  document.getElementById("fcAuditedLbl").textContent = auditedNote;
  document.getElementById("fcFrameableLbl").textContent = frameableNote;
}

async function fcLoadOverview() {
  const bars = document.getElementById("fcDetectorBars");
  bars.innerHTML = `<div class="empty-hint">Loading…</div>`;
  try {
    const snap = await fcSnapshotId();
    const o = await api("/audit/overview", snap != null ? { snapshot_id: snap } : {});
    if (!o || !o.per_detector || !o.per_detector.length) {
      fcSetMetrics("0", "0", "–", "–", "Audited", "Frameable");
      bars.innerHTML = `<div class="empty-hint">No flags yet. Run the detectors and the audit (<span class="mono">scripts/run_pipeline.py</span>) to populate this.</div>`;
      return;
    }
    const unaudited = o.total_flags - o.total_audited;
    fcSetMetrics(
      o.total_flags, o.total_audited,
      o.total_audited ? `${(o.frameable_share * 100).toFixed(0)}%` : "–",
      fcEth(o.framing_cost_median_wei),
      unaudited > 0 ? `Audited (${unaudited} not yet)` : "Audited",
      `Frameable (${o.frameable_count} of ${o.total_audited})`,
    );
    const max = Math.max(...o.per_detector.map(r => r.flag_count)) || 1;
    bars.innerHTML = o.per_detector.map(r => {
      const frameable = r.frameable_count;
      const robust = r.audited_count - r.frameable_count;
      const un = r.flag_count - r.audited_count;
      const pct = n => (n / max * 100).toFixed(2);
      return `<div class="fc-bar-row">
        <span>${fcEsc(fcDetectorName(r.detector))}</span>
        <div class="fc-bar-track" title="${frameable} frameable, ${robust} robust, ${un} not audited">
          <i class="fc-seg frameable" style="width:${pct(frameable)}%"></i><i class="fc-seg robust" style="width:${pct(robust)}%"></i><i class="fc-seg unaudited" style="width:${pct(un)}%"></i>
        </div>
        <span class="bar-value">${r.flag_count}</span>
      </div>`;
    }).join("");
  } catch (err) {
    fcSetMetrics("–", "–", "–", "–", "Audited", "Frameable");
    bars.innerHTML = fcErrorBlock(err.message, "overview");
  }
}

function fcShowApiDown(err) {
  fcSetMetrics("–", "–", "–", "–", "Audited", "Frameable");
  document.getElementById("fcDetectorBars").innerHTML = fcErrorBlock(
    `The API at ${apiBase()} is not reachable (${err.message}). Start it with: uvicorn api.main:app --port 8000`, "reload");
  if (currentPage === "flags") {
    document.getElementById("auditFlagRows").innerHTML =
      `<tr><td colspan="4">${fcErrorBlock("The API is not reachable.", "flags")}</td></tr>`;
  }
}

/* ── Flags view ── */
async function fcLoadFlags(filter) {
  if (FC.flags[filter]) return FC.flags[filter];
  const snap = await fcSnapshotId();
  const params = { limit: 100 };
  if (snap != null) params.snapshot_id = snap;
  if (filter !== "all") params.detector = filter;
  const flags = await api("/flags", params);
  const audits = await Promise.all(flags.map(f => fcFetchAudit(f.flag_id)));
  const merged = flags.map((f, i) => ({ ...f, audit: audits[i] }));
  FC.flags[filter] = merged;
  return merged;
}

async function fcFetchAudit(flagId) {
  try {
    return await api(`/flags/${flagId}/audit`);
  } catch (err) {
    if (String(err.message).startsWith("404")) return null; // not audited yet, a normal state
    throw err;
  }
}

function fcVerdictBadge(audit) {
  return audit
    ? `<span class="badge b-verdict-${fcEsc(audit.verdict)}">${fcEsc(audit.verdict)}</span>`
    : `<span class="badge b-status-new">not audited</span>`;
}

async function fcOpenFlagsView() {
  const tbody = document.getElementById("auditFlagRows");
  const myGen = ++FC.loadGen;
  if (!FC.flags[FC.filter]) tbody.innerHTML = `<tr><td colspan="4"><div class="empty-hint">Loading…</div></td></tr>`;
  try {
    await fcLoadFlags(FC.filter);
  } catch (err) {
    if (myGen !== FC.loadGen) return;
    tbody.innerHTML = `<tr><td colspan="4">${fcErrorBlock(err.message, "flags")}</td></tr>`;
    return;
  }
  if (myGen !== FC.loadGen) return;
  fcRenderFlagRows();
}

function fcRenderFlagRows() {
  const tbody = document.getElementById("auditFlagRows");
  const list = FC.flags[FC.filter] || [];
  if (!list.length) {
    tbody.innerHTML = `<tr><td colspan="4"><div class="empty-hint">${FC.filter === "all"
      ? "No flags yet. Run the detectors and the audit to create some."
      : "No flags from this detector on this snapshot."}</div></td></tr>`;
    return;
  }
  tbody.innerHTML = list.map(f => `
    <tr class="${f.flag_id === FC.selected ? "sel" : ""}" data-flag="${f.flag_id}" tabindex="0">
      <td>${fcEsc(fcDetectorName(f.detector))}</td>
      <td class="mono">#${f.wallet_id}</td>
      <td class="num">${f.score == null ? "–" : Number(f.score).toFixed(2)}</td>
      <td>${fcVerdictBadge(f.audit)}</td>
    </tr>`).join("");
}

function fcFindFlag(flagId) {
  for (const list of Object.values(FC.flags)) {
    const hit = list.find(f => f.flag_id === flagId);
    if (hit) return hit;
  }
  return null;
}

async function fcSelectFlag(flagId) {
  FC.selected = flagId;
  const myGen = ++FC.detailGen;
  fcRenderFlagRows();
  const box = document.getElementById("fcDetail");
  box.innerHTML = `<div class="empty-hint">Loading flag #${flagId}…</div>`;
  const flag = fcFindFlag(flagId);
  try {
    const [edges, audit] = await Promise.all([
      api(`/flags/${flagId}/evidence-edges`),
      flag ? Promise.resolve(flag.audit) : fcFetchAudit(flagId),
    ]);
    if (myGen !== FC.detailGen) return;
    box.innerHTML = fcDetailHtml(flagId, flag, edges, audit);
  } catch (err) {
    if (myGen !== FC.detailGen) return;
    box.innerHTML = fcErrorBlock(err.message, `detail:${flagId}`);
  }
}

function fcWalletLabel(id, addr, flaggedId) {
  const tag = id === flaggedId ? `<b class="fc-w">W</b> ` : "";
  return `${tag}<span class="mono" title="${fcEsc(addr || "")}">${addr ? fcEsc(shortAddr(addr)) : "#" + id}</span>`;
}

function fcDetailHtml(flagId, flag, edges, audit) {
  const wid = flag ? flag.wallet_id : null;
  let waddr = null;
  for (const e of edges) {
    if (e.from_wallet_id === wid) waddr = e.from_address;
    else if (e.to_wallet_id === wid) waddr = e.to_address;
  }
  const signed = edges.filter(e => e.role === "signed").length;
  const unsolicited = edges.length - signed;
  const sorted = [...edges].sort((a, b) => b.hop - a.hop || a.block_number - b.block_number);

  const evidence = edges.length ? `
    <p class="fc-step-sub">${edges.length} transfer${edges.length === 1 ? "" : "s"}: ${unsolicited} unsolicited, ${signed} signed by the wallet. Furthest hop first.</p>
    <div class="table-wrap fc-evidence"><table class="dtable">
      <thead><tr><th>Hop</th><th>Type</th><th>Transfer</th><th class="num">Value</th><th class="num">Block</th></tr></thead>
      <tbody>${sorted.map(e => `
        <tr>
          <td class="num">${e.hop}</td>
          <td><span class="badge ${e.role === "signed" ? "fc-b-signed" : "fc-b-unsolicited"}">${fcEsc(e.role)}</span></td>
          <td title="${fcEsc(e.tx_hash)}">${fcWalletLabel(e.from_wallet_id, e.from_address, wid)} <span class="fc-arrow">→</span> ${fcWalletLabel(e.to_wallet_id, e.to_address, wid)}</td>
          <td class="num">${fcEth(e.value_wei)}</td>
          <td class="num">${e.block_number}</td>
        </tr>`).join("")}</tbody>
    </table></div>`
    : `<div class="empty-hint">No evidence has been collected for this flag yet.</div>`;

  let verdict, cost;
  if (!audit) {
    verdict = `<div>${fcVerdictBadge(null)}</div><p>This flag has not been audited, so there is no verdict yet.</p>`;
    cost = `<p class="fc-muted">Available once the flag is audited.</p>`;
  } else if (audit.verdict === "frameable") {
    verdict = `<div>${fcVerdictBadge(audit)}</div>
      <p>With every transaction signed by the wallet removed, the flag still holds. Transfers from outsiders alone were enough, so the owner did nothing to earn it.</p>`;
    const m = FC_METHOD[audit.method] || { label: audit.method, note: "" };
    const n = Array.isArray(audit.minimal_tx_set) ? audit.minimal_tx_set.length : null;
    cost = audit.framing_cost_wei == null
      ? `<p class="fc-muted">${fcEsc(m.note)}</p>`
      : `<div class="fc-cost">${fcEth(audit.framing_cost_wei)}</div>
         <div class="fc-cost-meta"><span class="badge fc-b-method">${fcEsc(m.label)}</span>
           ${n != null ? `<span>${n} unsolicited transfer${n === 1 ? "" : "s"} needed</span>` : ""}
           <span class="mono">${fcEsc(audit.framing_cost_wei)} wei</span></div>
         <p class="fc-muted">${fcEsc(m.note)} The attacker pays value plus gas and can only add transfers.</p>`;
  } else {
    verdict = `<div>${fcVerdictBadge(audit)}</div>
      <p>The flag disappears once the wallet's own signed transactions are removed. It depended on the wallet's active behaviour, which an outsider cannot forge.</p>`;
    cost = `<p class="fc-muted">Not applicable: a robust flag cannot be manufactured by sending funds to the wallet.</p>`;
  }

  return `
    <div class="fc-d-head">
      <div>
        <div class="fc-d-title">Flag #${flagId} <span class="fc-d-det">${fcEsc(flag ? fcDetectorName(flag.detector) : "")}</span></div>
        <div class="fc-d-wallet">Wallet ${wid != null ? "#" + wid : ""} <span class="mono">${waddr ? fcEsc(waddr) : ""}</span></div>
      </div>
      <button class="btn" data-fc-graph="${flagId}" ${edges.length ? "" : "disabled"}>View in graph</button>
    </div>
    <div class="fc-step"><div class="fc-step-n">1</div><div class="fc-step-b"><h4>Evidence</h4>${evidence}</div></div>
    <div class="fc-step"><div class="fc-step-n">2</div><div class="fc-step-b"><h4>Verdict</h4>${verdict}</div></div>
    <div class="fc-step"><div class="fc-step-n">3</div><div class="fc-step-b"><h4>Framing cost</h4>${cost}</div></div>`;
}

/* ── Graph overlay: one flag's signed vs unsolicited edges ── */
function fcGraphOverlayWanted() {
  const sel = document.getElementById("graphFlagSelect");
  return !!(sel && sel.value);
}

async function fcPopulateGraphFlagSelect() {
  const sel = document.getElementById("graphFlagSelect");
  if (!FC.flags.all) {
    try { await fcLoadFlags("all"); } catch { return; } // the Flags view shows the real error state
  }
  const current = sel.value;
  const seen = new Set();
  const all = Object.values(FC.flags).flat().filter(f => !seen.has(f.flag_id) && seen.add(f.flag_id));
  all.sort((a, b) => a.flag_id - b.flag_id);
  sel.innerHTML = `<option value="">Flag evidence: none</option>` + all.map(f =>
    `<option value="${f.flag_id}">Flag #${f.flag_id} · ${fcEsc(fcDetectorName(f.detector))} · wallet #${f.wallet_id}</option>`).join("");
  if (current && [...sel.options].some(o => o.value === current)) sel.value = current;
}

async function fcDrawFlagOverlay(myGeneration) {
  const flagId = Number(document.getElementById("graphFlagSelect").value);
  const container = document.getElementById("graphCanvas");
  const tooltip = document.getElementById("graphTooltip");
  const legend = document.getElementById("fcGraphLegend");
  if (graphRenderer) { graphRenderer.destroy(); graphRenderer = null; }
  tooltip.classList.remove("show");
  container.innerHTML = `<div class="empty-hint">Loading evidence for flag #${flagId}…</div>`;
  legend.hidden = true;

  let edges;
  try {
    edges = await api(`/flags/${flagId}/evidence-edges`);
  } catch (err) {
    if (myGeneration !== renderGraphGeneration) return;
    container.innerHTML = fcErrorBlock(err.message, "graph");
    return;
  }
  if (myGeneration !== renderGraphGeneration) return;
  if (!edges.length) {
    container.innerHTML = `<div class="empty-hint">No evidence has been collected for flag #${flagId}.</div>`;
    return;
  }
  if (!detectGpuAdapter().available) {
    container.innerHTML = `<div class="empty-hint">Graph rendering requires WebGL2, which is not available in this window.</div>`;
    return;
  }

  const flag = fcFindFlag(flagId);
  const flaggedId = flag ? flag.wallet_id : null;
  const info = new Map();
  const touch = (id, addr, role) => {
    if (!info.has(id)) info.set(id, { id, address: addr, roles: new Set() });
    info.get(id).roles.add(role);
  };
  edges.forEach(e => {
    touch(e.from_wallet_id, e.from_address, e.role === "signed" ? "signs (flagged wallet)" : "sends unsolicited");
    if (e.to_wallet_id != null) touch(e.to_wallet_id, e.to_address, "receives");
  });
  const nodes = [...info.values()].map(n => ({
    id: n.id, x: 0, y: 0, alpha: 1,
    size: n.id === flaggedId ? 24 : 12,
    color: n.id === flaggedId ? "#F2F5F4" : (n.roles.has("sends unsolicited") ? "#E8A63D" : "#7C948F"),
    label: n.address ? shortAddr(n.address) : `#${n.id}`,
    fc: n,
  }));
  const ids = new Set(nodes.map(n => n.id));
  const usable = edges.filter(e => ids.has(e.from_wallet_id) && ids.has(e.to_wallet_id));
  const edgeObjs = usable.map(e => ({
    src: e.from_wallet_id, dst: e.to_wallet_id, alpha: 0.95,
    color: e.role === "signed" ? FC_SIGNED_COLOR : FC_UNSOLICITED_COLOR,
  }));
  layoutGraph(nodes, usable.map(e => [e.from_wallet_id, e.to_wallet_id]));

  container.innerHTML = "";
  try {
    graphRenderer = new GraphRenderer(container, {
      nodeCap: nodes.length,
      showFps: !!loadPref().showFps,
      onHover: (node, evt) => {
        if (!node) { tooltip.classList.remove("show"); return; }
        tooltip.innerHTML = `<div class="gt-addr">${fcEsc(node.fc.address || "#" + node.id)}</div>`
          + [...node.fc.roles].map(r => `<div class="gt-row"><span>Role</span><span>${fcEsc(r)}</span></div>`).join("");
        tooltip.style.left = evt.clientX + 14 + "px";
        tooltip.style.top = evt.clientY + 14 + "px";
        tooltip.classList.add("show");
      },
      onClick: node => { if (node.fc.address) copyToClipboard(node.fc.address); },
    });
    graphRenderer.setData(nodes, edgeObjs);
    graphRenderer.start();
  } catch (err) {
    console.error("evidence overlay render failed:", err);
    container.innerHTML = `<div class="empty-hint">Graph rendering requires WebGL2, which is not available in this window.</div>`;
    return;
  }
  legend.hidden = false;
  document.getElementById("gsNodes").textContent = nodes.length;
  document.getElementById("gsEdges").textContent = usable.length;
  document.getElementById("gsDensity").textContent = "–";
  document.getElementById("gsHub").textContent = flaggedId != null ? `#${flaggedId}` : "–";
  document.getElementById("nodeCapIndicator").textContent = `flag #${flagId} evidence: ${nodes.length} wallets`;
  document.getElementById("layoutDeviceBadge").textContent = "evidence subgraph (JS layout)";
}

/* ── wiring ── */
function fcInit() {
  document.getElementById("auditFilterBar").addEventListener("click", e => {
    const btn = e.target.closest("button");
    if (!btn) return;
    FC.filter = btn.dataset.f;
    document.querySelectorAll("#auditFilterBar button").forEach(x => x.classList.toggle("on", x === btn));
    fcOpenFlagsView();
  });
  const rows = document.getElementById("auditFlagRows");
  rows.addEventListener("click", e => {
    const tr = e.target.closest("tr[data-flag]");
    if (tr) fcSelectFlag(Number(tr.dataset.flag));
  });
  rows.addEventListener("keydown", e => {
    const tr = e.target.closest("tr[data-flag]");
    if (tr && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); fcSelectFlag(Number(tr.dataset.flag)); }
  });
  document.getElementById("graphFlagSelect").addEventListener("change", () => {
    if (!fcGraphOverlayWanted()) document.getElementById("fcGraphLegend").hidden = true;
    renderGraph();
  });
  document.addEventListener("click", async e => {
    const retry = e.target.closest("[data-fc-retry]");
    if (retry) {
      const what = retry.dataset.fcRetry;
      if (what === "reload") loadAll();
      else if (what === "overview") fcLoadOverview();
      else if (what === "flags") { FC.flags = {}; fcOpenFlagsView(); }
      else if (what === "graph") renderGraph();
      else if (what.startsWith("detail:")) fcSelectFlag(Number(what.slice(7)));
      return;
    }
    const g = e.target.closest("[data-fc-graph]");
    if (g) {
      const flagId = g.dataset.fcGraph;
      await fcPopulateGraphFlagSelect();
      document.getElementById("graphFlagSelect").value = flagId;
      go("graph");
      if (!WALLETS.length) renderGraph();
    }
  });
}
