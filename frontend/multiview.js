/* FrameCheck Multiview: up to 8 cluster graphs side by side, in their own
   window, with no dashboard chrome.

   Two things drive the design:

   1. Each panel is a real GraphRenderer, so each one holds its own WebGL2
      context. Browsers cap live contexts (~16) and silently kill the oldest
      to make room, so panels are capped at 8 and every renderer is
      destroyed (releasing its context) before the grid is rebuilt.

   2. The dashboard's layoutGraph() runs 150 O(n^2) iterations up front. For
      one graph that is a blink; for eight at once it is a multi-second
      frozen window. Here the layout is sliced across animation frames and
      stops as soon as a panel settles -- so the window stays responsive,
      panels visibly settle, and a settled panel costs nothing. */

const MV_PANEL_NODE_CAP = 150;    // per grid panel
const MV_EXPANDED_NODE_CAP = 600; // when one panel fills the window
const MV_MAX_PANELS = 8;

const MV_VERDICT_COLORS = {
  frameable: "#E5675A",
  robust: "#2FD9A8",
  unaudited: "#E8A63D",
  unflagged: "#5C7470",
};

const mvEsc = v => String(v ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* The desktop shell injects its selection here before the page runs:
   WebviewUrl::App is a PathBuf, so "multiview.html?clusters=1,2" does not
   survive it (the window came up about:blank). The browser build has no
   such limit and uses query parameters, so both paths are supported. */
const mvInjected = (typeof window !== "undefined" && window.__FC_MULTIVIEW__) || null;

function mvApiBase() {
  if (mvInjected?.api) return String(mvInjected.api).replace(/\/$/, "");
  const url = new URL(location.href);
  const fromQuery = url.searchParams.get("api");
  if (fromQuery) return fromQuery.replace(/\/$/, "");
  try {
    const prefs = JSON.parse(localStorage.getItem("framecheck_prefs") || "{}");
    if (prefs.apiBase) return String(prefs.apiBase).replace(/\/$/, "");
  } catch { /* private mode */ }
  return "http://localhost:8000";
}

function mvSessionToken() {
  try { return localStorage.getItem("framecheck_session_token") || ""; } catch { return ""; }
}

async function mvApi(path) {
  const bearer = mvSessionToken();
  const res = await fetch(mvApiBase() + path, {
    credentials: "include",
    headers: bearer ? { Authorization: `Bearer ${bearer}` } : {},
  });
  if (res.status === 401) throw new Error("Sign in on the dashboard first, then reopen Multiview.");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

/* ── Incremental force layout ──
   One step = one O(n^2) iteration for one panel. The scheduler below calls
   step() a few times per frame per panel and drops a panel the moment its
   total movement falls under SETTLE_EPSILON. */
class MvLayout {
  constructor(nodes, edges) {
    this.nodes = nodes;
    this.edges = edges;
    this.vel = new Map(nodes.map(n => [n.id, { x: 0, y: 0 }]));
    this.byId = new Map(nodes.map(n => [n.id, n]));
    this.iterations = 0;
    this.settled = nodes.length <= 1;

    // Deterministic start: the same cluster laid out twice looks the same,
    // which matters when comparing panels across two openings.
    let seed = 1337;
    const rand = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff; };
    const n = nodes.length || 1;
    nodes.forEach((node, i) => {
      const angle = (i / n) * Math.PI * 2;
      node.x = Math.cos(angle) * 400 + (rand() - 0.5) * 40;
      node.y = Math.sin(angle) * 400 + (rand() - 0.5) * 40;
    });
  }

  step() {
    if (this.settled) return 0;
    const REPULSION = 52000, SPRING_LEN = 170, SPRING_K = 0.022, DAMPING = 0.8;
    // Two guards against divergence, both found by expanding a panel to 600
    // nodes: coordinates reached 1e94 and the camera's fit scale collapsed
    // to 0, leaving a blank panel. At 150 nodes the same code is stable, so
    // this only appears once a panel is expanded.
    //   MIN_DIST2: without a floor, two nodes landing almost on top of each
    //   other give 52000/0.01 = 5.2e6 of repulsion in a single step.
    //   MAX_STEP: caps how far a node can move per iteration, so even an
    //   unlucky configuration cannot compound into an explosion.
    const MIN_DIST2 = 100, MAX_STEP = 60;
    const nodes = this.nodes;
    const force = new Map(nodes.map(n => [n.id, { x: 0, y: 0 }]));

    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        const dx = a.x - b.x, dy = a.y - b.y;
        const dist2 = Math.max(dx * dx + dy * dy, MIN_DIST2);
        const dist = Math.sqrt(dist2);
        const f = REPULSION / dist2;
        const fx = (dx / dist) * f, fy = (dy / dist) * f;
        const fa = force.get(a.id), fb = force.get(b.id);
        fa.x += fx; fa.y += fy;
        fb.x -= fx; fb.y -= fy;
      }
    }
    for (const [aId, bId] of this.edges) {
      if (aId === bId) continue;
      const a = this.byId.get(aId), b = this.byId.get(bId);
      if (!a || !b) continue;
      const dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = SPRING_K * (dist - SPRING_LEN);
      const fx = (dx / dist) * f, fy = (dy / dist) * f;
      const fa = force.get(aId), fb = force.get(bId);
      fa.x += fx; fa.y += fy;
      fb.x -= fx; fb.y -= fy;
    }

    let movement = 0;
    for (const node of nodes) {
      const v = this.vel.get(node.id), f = force.get(node.id);
      v.x = (v.x + f.x) * DAMPING;
      v.y = (v.y + f.y) * DAMPING;
      const speed = Math.hypot(v.x, v.y);
      if (speed > MAX_STEP) {
        const k = MAX_STEP / speed;
        v.x *= k;
        v.y *= k;
      }
      node.x += v.x;
      node.y += v.y;
      movement += Math.abs(v.x) + Math.abs(v.y);
    }
    this.iterations++;
    const perNode = movement / nodes.length;
    // Settled, or long enough that another 100 iterations will not change
    // what the reader sees.
    if (perNode < 0.35 || this.iterations >= 220) this.settled = true;
    return perNode;
  }
}

/* ── Panels ── */
const mvPanels = [];
let mvRaf = null;

function mvDestroyPanels() {
  while (mvPanels.length) {
    const panel = mvPanels.pop();
    try { panel.renderer?.destroy(); } catch { /* already gone */ }
  }
  if (mvRaf) { cancelAnimationFrame(mvRaf); mvRaf = null; }
}

function mvScheduleLayout() {
  if (mvRaf) return;
  const frame = () => {
    mvRaf = null;
    let active = false;
    for (const panel of mvPanels) {
      if (!panel.layout || panel.layout.settled) continue;
      active = true;
      // A few iterations per frame: enough to settle in a couple of
      // seconds, few enough that the frame budget is not blown with eight
      // panels running at once.
      for (let i = 0; i < 3 && !panel.layout.settled; i++) panel.layout.step();
      panel.renderer?.applyPositionUpdates(
        new Map(panel.layout.nodes.map(n => [n.id, [n.x, n.y]])));
      if (panel.layout.settled) mvMarkSettled(panel);
    }
    if (active) mvRaf = requestAnimationFrame(frame);
  };
  mvRaf = requestAnimationFrame(frame);
}

function mvMarkSettled(panel) {
  // A settled panel stops its render loop: eight idle rAF loops redrawing
  // an unchanged graph is exactly the "don't redraw panels that haven't
  // changed" cost. Input handlers restart it on demand (see mvWakePanel).
  panel.el.querySelector("[data-mv-status]")?.remove();
  panel.renderer?.render();
  panel.renderer?._running && (panel.renderer._running = false);
  if (panel.renderer?._raf) {
    cancelAnimationFrame(panel.renderer._raf);
    panel.renderer._raf = null;
  }
}

function mvWakePanel(panel) {
  // Pan/zoom/hover need frames again; start() is a no-op if already running.
  panel.renderer?.start();
  clearTimeout(panel.sleepTimer);
  panel.sleepTimer = setTimeout(() => {
    if (panel.layout?.settled) mvMarkSettled(panel);
  }, 1200);
}

function mvPanelHeader(cluster) {
  const share = cluster.frameable_share;
  const pct = share == null ? "–" : `${Math.round(share * 100)}%`;
  const flags = cluster.flag_count ?? cluster.flagged_wallets ?? "–";
  return `<div class="mv-head">
    <div class="mv-title">Cluster ${mvEsc(cluster.cluster_id)}</div>
    <div class="mv-meta">
      <span>${Number(cluster.member_count ?? 0).toLocaleString()} wallets</span>
      <span>${Number(flags).toLocaleString()} flags</span>
      <span title="Share of audited flags in this cluster that are frameable">${pct} frameable</span>
    </div>
    <button class="mv-expand" data-mv-expand="${mvEsc(cluster.cluster_id)}" title="Expand this panel">⤢</button>
  </div>`;
}

async function mvBuildPanel(cluster, host, nodeCap) {
  const el = document.createElement("section");
  el.className = "mv-panel";
  el.dataset.cluster = cluster.cluster_id;
  el.innerHTML = mvPanelHeader(cluster)
    + `<div class="mv-canvas"><div class="mv-status" data-mv-status>Loading…</div></div>`
    + `<div class="mv-foot" data-mv-foot></div>`;
  host.appendChild(el);

  const canvasHost = el.querySelector(".mv-canvas");
  const panel = { cluster, el, renderer: null, layout: null };
  mvPanels.push(panel);

  let data;
  try {
    data = await mvApi(`/clusters/${cluster.cluster_id}/subgraph?limit=${nodeCap}`);
  } catch (err) {
    canvasHost.innerHTML = `<div class="mv-status mv-error">${mvEsc(err.message)}</div>`;
    return panel;
  }

  if (!data.nodes.length) {
    canvasHost.innerHTML = `<div class="mv-status">This cluster has no members to draw.</div>`;
    return panel;
  }

  canvasHost.innerHTML = `<div class="mv-status" data-mv-status>Laying out…</div>`;

  try {
    panel.renderer = new GraphRenderer(canvasHost, {
      nodeCap,
      maxLabels: 0,
      onHover: (node, evt) => mvTooltip(node, evt),
      onClick: node => mvTooltip(node, null, true),
    });
  } catch (err) {
    canvasHost.innerHTML =
      `<div class="mv-status mv-error">WebGL2 is unavailable in this window, so this panel cannot draw.</div>`;
    console.error("panel renderer failed:", err);
    return panel;
  }

  mvApplySubgraph(panel, data, nodeCap);
  panel.renderer.start();

  canvasHost.addEventListener("pointerdown", () => mvWakePanel(panel));
  canvasHost.addEventListener("wheel", () => mvWakePanel(panel), { passive: true });
  canvasHost.addEventListener("pointermove", () => mvWakePanel(panel));
  return panel;
}

/** Loads a /clusters/{id}/subgraph payload into an existing panel. Shared
 *  by first render and by the larger refetch on expand, so both show the
 *  same colours, sizes and truncation wording. */
function mvApplySubgraph(panel, data, nodeCap) {
  const nodes = data.nodes.map(n => ({
    id: n.wallet_id,
    label: n.address ? n.address.slice(0, 10) : String(n.wallet_id),
    // Small: at panel scale a 7px node turns a dense neighbourhood into
    // one solid blob and hides the structure the panel exists to show.
    size: n.verdict === "unflagged" ? 3 : 4.5,
    color: MV_VERDICT_COLORS[n.verdict] || MV_VERDICT_COLORS.unflagged,
    raw: n,
  }));
  const edges = data.edges.map(e => ({ src: e.src_wallet_id, dst: e.dst_wallet_id }));

  panel.layout = new MvLayout(nodes, edges.map(e => [e.src, e.dst]));
  // setData first: setNodeCap recomputes level-of-detail from the current
  // node set, and on a fresh renderer there isn't one yet.
  panel.renderer.setData(nodes, edges);
  panel.renderer.setNodeCap(nodeCap);

  const foot = panel.el.querySelector("[data-mv-foot]");
  if (foot) {
    foot.textContent = data.truncated
      ? `Showing ${data.nodes.length} of ${Number(data.member_count).toLocaleString()} wallets — expanded outward from the highest-PageRank member, so what you see is connected.`
      : `Showing all ${data.nodes.length} wallets.`;
  }
}

function mvTooltip(node, evt, pinned = false) {
  const tip = document.getElementById("mvTooltip");
  if (!node) {
    if (!tip.dataset.pinned) tip.classList.remove("show");
    return;
  }
  const n = node.raw || {};
  tip.innerHTML = `<div class="mv-tip-addr">${mvEsc(n.address || node.label)}</div>
    <div class="mv-tip-row"><span>Wallet</span><span>#${mvEsc(n.wallet_id ?? node.id)}</span></div>
    <div class="mv-tip-row"><span>Verdict</span><span class="mv-v-${mvEsc(n.verdict)}">${mvEsc(n.verdict ?? "–")}</span></div>
    <div class="mv-tip-row"><span>Flags</span><span>${mvEsc(n.flag_count ?? 0)}</span></div>
    <div class="mv-tip-row"><span>PageRank</span><span>${n.pagerank == null ? "–" : Number(n.pagerank).toExponential(2)}</span></div>`;
  if (evt) {
    tip.style.left = Math.min(evt.clientX + 14, window.innerWidth - 240) + "px";
    tip.style.top = Math.min(evt.clientY + 14, window.innerHeight - 120) + "px";
  }
  if (pinned) tip.dataset.pinned = "1"; else delete tip.dataset.pinned;
  tip.classList.add("show");
}

/* ── Expand / collapse ── */
let mvExpanded = null;

function mvToggleExpand(clusterId) {
  const grid = document.getElementById("mvGrid");
  if (mvExpanded === clusterId) {
    grid.classList.remove("mv-has-expanded");
    grid.querySelectorAll(".mv-panel").forEach(p => p.classList.remove("mv-is-expanded"));
    mvExpanded = null;
  } else {
    grid.classList.add("mv-has-expanded");
    grid.querySelectorAll(".mv-panel").forEach(p =>
      p.classList.toggle("mv-is-expanded", p.dataset.cluster === String(clusterId)));
    mvExpanded = clusterId;
  }
  // The canvas element changed size, so every live renderer has to re-fit
  // or it draws into a stale viewport.
  requestAnimationFrame(() => {
    for (const panel of mvPanels) {
      panel.renderer?._resize?.();
      panel.renderer?.render?.();
    }
    if (mvExpanded !== null) {
      const panel = mvPanels.find(p => String(p.cluster.cluster_id) === String(mvExpanded));
      if (panel) mvLoadDetail(panel);
    }
  });
}

async function mvLoadDetail(panel) {
  // A full window showing 150 of 26,594 wallets wastes the space that
  // expanding just bought. Refetch at the larger cap once, then keep it.
  if (panel.detailed || panel.detailLoading) return;
  panel.detailLoading = true;
  const foot = panel.el.querySelector("[data-mv-foot]");
  const previous = foot ? foot.textContent : "";
  if (foot) foot.textContent = "Loading more of this cluster…";
  try {
    const data = await mvApi(`/clusters/${panel.cluster.cluster_id}/subgraph?limit=${MV_EXPANDED_NODE_CAP}`);
    if (data.nodes.length > (panel.renderer?.nodesAll?.length || 0)) {
      mvApplySubgraph(panel, data, MV_EXPANDED_NODE_CAP);
      mvScheduleLayout();
    } else if (foot) {
      foot.textContent = previous;
    }
    panel.detailed = true;
  } catch (err) {
    if (foot) foot.textContent = `Could not load more: ${err.message}`;
  } finally {
    panel.detailLoading = false;
  }
}

/* ── Boot ── */
async function mvInit() {
  const grid = document.getElementById("mvGrid");
  const status = document.getElementById("mvStatus");
  const params = new URLSearchParams(location.search);
  const explicit = mvInjected
    ? (mvInjected.clusters || [])
    : (params.get("clusters") || "")
      .split(",").map(s => s.trim()).filter(Boolean).map(Number).filter(Number.isFinite);

  mvDestroyPanels();
  grid.innerHTML = "";

  let clusters;
  try {
    if (explicit.length) {
      if (explicit.length > MV_MAX_PANELS) {
        status.textContent =
          `Multiview shows at most ${MV_MAX_PANELS} clusters; the first ${MV_MAX_PANELS} of your ${explicit.length} are shown.`;
      }
      const wanted = explicit.slice(0, MV_MAX_PANELS);
      clusters = await Promise.all(wanted.map(async id => {
        try { return await mvApi(`/clusters/${id}`); } catch { return { cluster_id: id }; }
      }));
      if (explicit.length <= MV_MAX_PANELS) {
        status.textContent = `${clusters.length} selected cluster${clusters.length === 1 ? "" : "s"}.`;
      }
    } else {
      const limit = Math.min(Number(mvInjected?.limit || params.get("limit")) || MV_MAX_PANELS, MV_MAX_PANELS);
      clusters = await mvApi(`/clusters/top-flagged?limit=${limit}`);
      if (!clusters.length) {
        status.innerHTML = `No flagged clusters found. Run the detectors, then reopen Multiview.`;
        return;
      }
      status.textContent = `Top ${clusters.length} clusters by flagged wallets.`;
    }
  } catch (err) {
    status.innerHTML = `<span class="mv-error">${mvEsc(err.message)}</span>`;
    return;
  }

  document.title = `Multiview — ${clusters.length} clusters — FrameCheck`;
  grid.dataset.count = clusters.length;
  for (const cluster of clusters) {
    await mvBuildPanel(cluster, grid, MV_PANEL_NODE_CAP);
  }
  mvScheduleLayout();
}

document.addEventListener("click", e => {
  const expand = e.target.closest("[data-mv-expand]");
  if (expand) { mvToggleExpand(Number(expand.dataset.mvExpand)); return; }
  if (!e.target.closest(".mv-canvas")) {
    const tip = document.getElementById("mvTooltip");
    delete tip.dataset.pinned;
    tip.classList.remove("show");
  }
});

document.addEventListener("keydown", e => {
  if (e.key === "Escape" && mvExpanded !== null) mvToggleExpand(mvExpanded);
});

window.addEventListener("beforeunload", mvDestroyPanels);
window.addEventListener("resize", () => {
  for (const panel of mvPanels) panel.renderer?._resize?.();
});

mvInit();
