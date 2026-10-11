/*
 * Instanced WebGL2 graph renderer -- replaces sigma.js + graphology for the
 * Wallet graph view. Validated as a rendering-tech spike first (see
 * spikes/webgl2-instanced/): 95-112fps sustained at the real 19,119-node/
 * 35,756-edge graph, measured inside the actual Tauri/WebView2 window, with
 * pan and zoom live. This file is that spike brought up to feature parity
 * with the old sigma-based view (risk-tier coloring, hover tooltip, click,
 * labels) plus real LOD, which the spike didn't need to prove the fps case.
 *
 * No external dependencies (no CDN, no npm) -- one <script src> include,
 * same convention as the rest of this app.
 *
 * LOD, present from the first version (not a follow-up):
 *   - a node cap with a visible "showing N of M" indicator -- nodes are
 *     ranked by `size` (already tx_count-derived, same priority the old
 *     sigma view used) so the cap naturally keeps hubs and drops low-value
 *     leaves first, not an arbitrary prefix of whatever order the API
 *     returned.
 *   - edges are filtered to only those whose *both* endpoints survive the
 *     cap -- the rendering spike's node-cap slider didn't do this (all
 *     edges always drew), which was a known, called-out gap in that demo.
 *   - labels are hidden below a zoom threshold, and even above it, only the
 *     top-N on-screen nodes by size get one -- otherwise a dense hub still
 *     produces label soup at any zoom level.
 *   - degree (computed from the *full*, pre-cap edge list, not just what's
 *     rendered) is shown in a hub's label once labels are visible, so a
 *     collapsed/dropped neighborhood is still visible as a number instead
 *     of silently disappearing. This is the honest subset of "collapsible
 *     hubs" this version implements -- an interactive expand/collapse UI
 *     for a hub's hidden neighbors is real, deferred scope, not built here.
 */

const NODE_VERT_SRC = `#version 300 es
layout(location=0) in vec2 aQuadPos;
layout(location=1) in vec2 aInstancePos;
layout(location=2) in float aInstanceSize;
layout(location=3) in vec4 aInstanceColor;
uniform vec4 uView; // centerX, centerY, scale, aspect
uniform vec2 uCanvasSize;
out vec2 vLocal;
out vec4 vColor;
void main() {
  vec2 centered = (aInstancePos - uView.xy) * uView.z;
  vec2 offsetPx = aQuadPos * aInstanceSize;
  vec2 offsetClip = offsetPx / uCanvasSize * 2.0;
  vLocal = aQuadPos;
  vColor = aInstanceColor;
  gl_Position = vec4(centered.x * uView.w + offsetClip.x, centered.y + offsetClip.y, 0.0, 1.0);
}`;

const NODE_FRAG_SRC = `#version 300 es
precision mediump float;
in vec2 vLocal;
in vec4 vColor;
out vec4 outColor;
void main() {
  float d = length(vLocal);
  if (d > 1.0) discard;
  float alpha = smoothstep(1.0, 0.82, d);
  outColor = vec4(vColor.rgb, vColor.a * alpha);
}`;

const EDGE_VERT_SRC = `#version 300 es
layout(location=1) in vec2 aPos;
layout(location=4) in vec4 aColor;
uniform vec4 uView;
out vec4 vColor;
void main() {
  vec2 centered = (aPos - uView.xy) * uView.z;
  vColor = aColor;
  gl_Position = vec4(centered.x * uView.w, centered.y, 0.0, 1.0);
}`;

const EDGE_FRAG_SRC = `#version 300 es
precision mediump float;
in vec4 vColor;
out vec4 outColor;
void main() { outColor = vColor; }`;

function compile(gl, type, src) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(s);
    gl.deleteShader(s);
    throw new Error("shader compile failed: " + log);
  }
  return s;
}
function link(gl, vsSrc, fsSrc) {
  const p = gl.createProgram();
  gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, vsSrc));
  gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, fsSrc));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
    const log = gl.getProgramInfoLog(p);
    gl.deleteProgram(p);
    throw new Error("program link failed: " + log);
  }
  return p;
}
function hexToRgba(hex, alpha) {
  const h = hex.replace("#", "");
  const r = parseInt(h.substring(0, 2), 16) / 255;
  const g = parseInt(h.substring(2, 4), 16) / 255;
  const b = parseInt(h.substring(4, 6), 16) / 255;
  return [r, g, b, alpha];
}

class GraphRenderer {
  /**
   * @param {HTMLElement} container
   * @param {object} opts
   *   opts.onHover(nodeOrNull), opts.onClick(node), opts.nodeCap (default 2000),
   *   opts.labelZoomThreshold (default 8 -- a *multiple* of the initial
   *     fit-to-view scale, not an absolute value; see _fitCamera), opts.maxLabels (default 40)
   */
  constructor(container, opts = {}) {
    this.container = container;
    this.onHover = opts.onHover || (() => {});
    this.onClick = opts.onClick || (() => {});
    this.nodeCap = opts.nodeCap ?? 2000;
    this.labelZoomThreshold = opts.labelZoomThreshold ?? 8;
    this.fitScale = 1;
    this.maxLabels = opts.maxLabels ?? 40;

    this.canvas = document.createElement("canvas");
    this.canvas.style.cssText = "display:block;width:100%;height:100%;cursor:grab;";
    container.appendChild(this.canvas);

    this.labelLayer = document.createElement("div");
    this.labelLayer.style.cssText = "position:absolute;inset:0;pointer-events:none;overflow:hidden;";
    container.appendChild(this.labelLayer);

    this.showFps = !!opts.showFps;
    this.fpsBadge = document.createElement("div");
    this.fpsBadge.style.cssText = "position:absolute;top:8px;right:8px;background:rgba(0,0,0,.55);"
      + "color:#8cffa0;font:11px ui-monospace,Consolas,monospace;padding:3px 7px;"
      + "border-radius:6px;pointer-events:none;z-index:10;white-space:nowrap;"
      + (this.showFps ? "" : "display:none;");
    container.appendChild(this.fpsBadge);
    this._fpsFrames = 0;
    this._fpsLastT = 0;

    const gl = this.canvas.getContext("webgl2", { antialias: true });
    if (!gl) throw new Error("WebGL2 is not available in this context");
    this.gl = gl;

    this.nodeProgram = link(gl, NODE_VERT_SRC, NODE_FRAG_SRC);
    this.edgeProgram = link(gl, EDGE_VERT_SRC, EDGE_FRAG_SRC);

    this.quadBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quadBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);

    this.instancePosBuf = gl.createBuffer();
    this.instanceSizeBuf = gl.createBuffer();
    this.instanceColorBuf = gl.createBuffer();
    this.nodeVao = gl.createVertexArray();
    this.edgeVao = gl.createVertexArray();
    this.edgePosBuf = gl.createBuffer();
    this.edgeColorBuf = gl.createBuffer();

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    this.scale = 1;
    this.panX = 0;
    this.panY = 0;
    this.nodesAll = [];
    this.nodesShown = [];
    this.edgesShown = [];
    this.degreeById = new Map();
    this._running = false;
    this._bindInput();
  }

  /** nodes: [{id, x, y, size, color:"#rrggbb", alpha, label}], edges: [{src,dst,color:"#rrggbb",alpha}] */
  setData(nodes, edges) {
    this.nodesAll = nodes;
    this.degreeById = new Map();
    for (const e of edges) {
      this.degreeById.set(e.src, (this.degreeById.get(e.src) || 0) + 1);
      this.degreeById.set(e.dst, (this.degreeById.get(e.dst) || 0) + 1);
    }
    this._edgesAll = edges;
    this._recomputeLod();
    this._fitCamera();
  }

  setNodeCap(cap) {
    this.nodeCap = cap;
    this._recomputeLod();
  }

  /** Applies newly-arrived positions (id -> [x,y]) without touching the
   * node/edge SET (no LOD recompute) -- used while layout iterations stream
   * in from the Rust "layout-progress" event, so the graph visibly settles
   * instead of jumping into place only once at the very end. Re-fits the
   * camera each call too: the seed layout's extent is usually nowhere near
   * the final settled extent (which can be orders of magnitude larger), so
   * without this most nodes would simply drift off-screen mid-animation. */
  applyPositionUpdates(positionsById) {
    for (const node of this.nodesAll) {
      const p = positionsById[node.id];
      if (p) { node.x = p[0]; node.y = p[1]; }
    }
    this._fitCamera();
    this._uploadBuffers();
  }

  _recomputeLod() {
    const ranked = [...this.nodesAll].sort((a, b) => (b.size || 1) - (a.size || 1));
    let shown = ranked.slice(0, this.nodeCap);
    // A focused set (see focusCluster) must always be visible regardless of
    // the LOD cap -- a cluster's members are often low-size/low-degree
    // nodes that a pure top-N-by-size cap would otherwise drop, defeating
    // the whole point of focusing on them.
    if (this.focusedIds && this.focusedIds.size) {
      const shownIds = new Set(shown.map(n => n.id));
      const missingFocused = this.nodesAll.filter(n => this.focusedIds.has(n.id) && !shownIds.has(n.id));
      shown = [...missingFocused, ...shown];
    }
    this.nodesShown = shown;
    const shownIds = new Set(this.nodesShown.map(n => n.id));
    this.edgesShown = this._edgesAll.filter(e => shownIds.has(e.src) && shownIds.has(e.dst));
    this._uploadBuffers();
  }

  /** Fits the camera to `memberIds` and dims everything else. Pass an empty
   * array/clearFocus() to restore normal display. */
  focusCluster(memberIds) {
    this.focusedIds = new Set(memberIds);
    this._recomputeLod();
    const members = this.nodesAll.filter(n => this.focusedIds.has(n.id));
    if (!members.length) return;
    const cx = members.reduce((s, n) => s + n.x, 0) / members.length;
    const cy = members.reduce((s, n) => s + n.y, 0) / members.length;
    let maxDist = 1;
    for (const n of members) maxDist = Math.max(maxDist, Math.hypot(n.x - cx, n.y - cy));
    this.panX = cx;
    this.panY = cy;
    this.scale = 1.6 / maxDist;
  }

  clearFocus() {
    this.focusedIds = null;
    this._recomputeLod();
    this._fitCamera();
  }

  _uploadBuffers() {
    const gl = this.gl;
    const n = this.nodesShown.length;
    const positions = new Float32Array(n * 2);
    const sizes = new Float32Array(n);
    const colors = new Float32Array(n * 4);
    this._indexOf = new Map();
    this.nodesShown.forEach((node, i) => {
      this._indexOf.set(node.id, i);
      positions[i * 2] = node.x; positions[i * 2 + 1] = node.y;
      sizes[i] = node.size || 4;
      const [r, g, b] = hexToRgba(node.color || "#7C948F", 1);
      const isFocused = !this.focusedIds || this.focusedIds.has(node.id);
      const baseAlpha = node.alpha ?? 0.92;
      colors[i * 4] = r; colors[i * 4 + 1] = g; colors[i * 4 + 2] = b;
      colors[i * 4 + 3] = isFocused ? baseAlpha : baseAlpha * 0.12;
    });

    gl.bindBuffer(gl.ARRAY_BUFFER, this.instancePosBuf);
    gl.bufferData(gl.ARRAY_BUFFER, positions, gl.DYNAMIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.instanceSizeBuf);
    gl.bufferData(gl.ARRAY_BUFFER, sizes, gl.DYNAMIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.instanceColorBuf);
    gl.bufferData(gl.ARRAY_BUFFER, colors, gl.DYNAMIC_DRAW);

    gl.bindVertexArray(this.nodeVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quadBuf);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.instancePosBuf);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 2, gl.FLOAT, false, 0, 0);
    gl.vertexAttribDivisor(1, 1);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.instanceSizeBuf);
    gl.enableVertexAttribArray(2);
    gl.vertexAttribPointer(2, 1, gl.FLOAT, false, 0, 0);
    gl.vertexAttribDivisor(2, 1);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.instanceColorBuf);
    gl.enableVertexAttribArray(3);
    gl.vertexAttribPointer(3, 4, gl.FLOAT, false, 0, 0);
    gl.vertexAttribDivisor(3, 1);
    gl.bindVertexArray(null);

    const m = this.edgesShown.length;
    const edgePos = new Float32Array(m * 4);
    const edgeColor = new Float32Array(m * 8);
    this.edgesShown.forEach((e, i) => {
      const ia = this._indexOf.get(e.src), ib = this._indexOf.get(e.dst);
      const pa = this.nodesShown[ia], pb = this.nodesShown[ib];
      edgePos[i * 4] = pa.x; edgePos[i * 4 + 1] = pa.y;
      edgePos[i * 4 + 2] = pb.x; edgePos[i * 4 + 3] = pb.y;
      const bothFocused = !this.focusedIds || (this.focusedIds.has(e.src) && this.focusedIds.has(e.dst));
      const edgeAlpha = (e.alpha ?? 0.35) * (bothFocused ? 1 : 0.08);
      const [r, g, b] = hexToRgba(e.color || "#7C948F", edgeAlpha);
      for (let v = 0; v < 2; v++) {
        edgeColor[i * 8 + v * 4] = r; edgeColor[i * 8 + v * 4 + 1] = g;
        edgeColor[i * 8 + v * 4 + 2] = b; edgeColor[i * 8 + v * 4 + 3] = edgeAlpha;
      }
    });
    gl.bindBuffer(gl.ARRAY_BUFFER, this.edgePosBuf);
    gl.bufferData(gl.ARRAY_BUFFER, edgePos, gl.DYNAMIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.edgeColorBuf);
    gl.bufferData(gl.ARRAY_BUFFER, edgeColor, gl.DYNAMIC_DRAW);

    gl.bindVertexArray(this.edgeVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.edgePosBuf);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 2, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.edgeColorBuf);
    gl.enableVertexAttribArray(4);
    gl.vertexAttribPointer(4, 4, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
  }

  _fitCamera() {
    if (!this.nodesAll.length) return;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const n of this.nodesAll) {
      minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x);
      minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y);
    }
    this.panX = (minX + maxX) / 2;
    this.panY = (minY + maxY) / 2;
    const extent = Math.max(maxX - minX, maxY - minY) || 1;
    this.scale = 1.8 / extent;
    // Label visibility is checked against this, not a fixed absolute scale
    // -- world-unit extent varies wildly by position source (the JS
    // concentric fallback spans ~1800 units; the Rust GPU layout's settled
    // positions are much tighter), so a hardcoded threshold either never
    // triggers or triggers immediately depending on which one produced the
    // current positions. Found by testing: labels never appeared at any
    // zoom level reachable in the UI until this was made relative.
    this.fitScale = this.scale;
  }

  _resize() {
    const dpr = window.devicePixelRatio || 1;
    const w = this.container.clientWidth, h = this.container.clientHeight;
    if (this.canvas.width !== Math.round(w * dpr) || this.canvas.height !== Math.round(h * dpr)) {
      this.canvas.width = Math.round(w * dpr);
      this.canvas.height = Math.round(h * dpr);
      this.gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    }
  }

  worldToScreen(x, y) {
    this._resize();
    const aspect = this.canvas.height / this.canvas.width;
    const cx = (x - this.panX) * this.scale;
    const cy = (y - this.panY) * this.scale;
    const clipX = cx * aspect, clipY = cy;
    const sx = (clipX * 0.5 + 0.5) * this.canvas.width;
    const sy = (-clipY * 0.5 + 0.5) * this.canvas.height;
    return [sx / (window.devicePixelRatio || 1), sy / (window.devicePixelRatio || 1)];
  }

  screenToWorld(sx, sy) {
    const dpr = window.devicePixelRatio || 1;
    const aspect = this.canvas.height / this.canvas.width;
    const clipX = ((sx * dpr) / this.canvas.width) * 2 - 1;
    const clipY = -(((sy * dpr) / this.canvas.height) * 2 - 1);
    const cx = clipX / aspect, cy = clipY;
    return [cx / this.scale + this.panX, cy / this.scale + this.panY];
  }

  _nearestNode(sx, sy, maxPx = 14) {
    let best = null, bestD2 = maxPx * maxPx;
    for (const n of this.nodesShown) {
      const [nx, ny] = this.worldToScreen(n.x, n.y);
      const d2 = (nx - sx) ** 2 + (ny - sy) ** 2;
      if (d2 < bestD2) { bestD2 = d2; best = n; }
    }
    return best;
  }

  _bindInput() {
    const c = this.canvas;
    let dragging = false, lastX = 0, lastY = 0, moved = false;
    c.addEventListener("mousedown", e => { dragging = true; moved = false; lastX = e.clientX; lastY = e.clientY; c.style.cursor = "grabbing"; });
    window.addEventListener("mouseup", () => { dragging = false; c.style.cursor = "grab"; });
    window.addEventListener("mousemove", e => {
      if (dragging) {
        const dx = e.clientX - lastX, dy = e.clientY - lastY;
        lastX = e.clientX; lastY = e.clientY;
        if (Math.abs(dx) > 2 || Math.abs(dy) > 2) moved = true;
        this.panX -= dx / (this.canvas.height / (window.devicePixelRatio || 1) / 2) / this.scale;
        this.panY += dy / (this.canvas.height / (window.devicePixelRatio || 1) / 2) / this.scale;
      } else {
        const rect = c.getBoundingClientRect();
        if (e.clientX >= rect.left && e.clientX <= rect.right && e.clientY >= rect.top && e.clientY <= rect.bottom) {
          const node = this._nearestNode(e.clientX - rect.left, e.clientY - rect.top);
          this.onHover(node, e);
        }
      }
    });
    c.addEventListener("wheel", e => {
      e.preventDefault();
      const factor = Math.exp(-e.deltaY * 0.001);
      this.scale *= factor;
    }, { passive: false });
    c.addEventListener("click", e => {
      if (moved) return; // was a drag, not a click
      const rect = c.getBoundingClientRect();
      const node = this._nearestNode(e.clientX - rect.left, e.clientY - rect.top);
      if (node) this.onClick(node);
    });
  }

  _updateLabels() {
    const dpr = window.devicePixelRatio || 1;
    this.labelLayer.innerHTML = "";
    if (this.scale < this.fitScale * this.labelZoomThreshold) return;
    const candidates = [...this.nodesShown].sort((a, b) => (b.size || 1) - (a.size || 1)).slice(0, this.maxLabels);
    const cw = this.canvas.width / dpr, ch = this.canvas.height / dpr;
    for (const n of candidates) {
      const [sx, sy] = this.worldToScreen(n.x, n.y);
      if (sx < -50 || sx > cw + 50 || sy < -50 || sy > ch + 50) continue;
      const degree = this.degreeById.get(n.id) || 0;
      const el = document.createElement("div");
      el.style.cssText = `position:absolute;left:${sx}px;top:${sy + (n.size || 4) + 4}px;transform:translateX(-50%);`
        + `font:600 11px ui-monospace,Consolas,monospace;color:#EDF6F4;text-shadow:0 0 3px #070d0c,0 0 3px #070d0c;white-space:nowrap;`;
      el.textContent = degree > 20 ? `${n.label} (${degree})` : n.label;
      this.labelLayer.appendChild(el);
    }
  }

  render() {
    // Measures the render() call's own CPU-side duration (draw submission +
    // driver overhead), not just wall-clock time between rAF callbacks.
    // requestAnimationFrame is vsync-throttled, so "still 60fps" alone can't
    // tell a genuinely cheap frame (2ms of work, 14.6ms idle) apart from one
    // that's using the whole budget (16ms of work, 0 margin) -- both read as
    // a clean 60fps. This is a lower bound on true GPU cost, not a forced
    // sync (no gl.finish() -- that would stall the pipeline and distort
    // normal operation just to measure it), but for a two-shader scene like
    // this, CPU-side submission is typically the dominant cost anyway.
    const renderT0 = performance.now();
    this._resize();
    const gl = this.gl;
    gl.clearColor(0.027, 0.051, 0.047, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    const aspect = this.canvas.height / this.canvas.width;
    const view = [this.panX, this.panY, this.scale, aspect];

    gl.useProgram(this.edgeProgram);
    gl.uniform4fv(gl.getUniformLocation(this.edgeProgram, "uView"), view);
    gl.bindVertexArray(this.edgeVao);
    gl.drawArrays(gl.LINES, 0, this.edgesShown.length * 2);

    gl.useProgram(this.nodeProgram);
    gl.uniform4fv(gl.getUniformLocation(this.nodeProgram, "uView"), view);
    gl.uniform2f(gl.getUniformLocation(this.nodeProgram, "uCanvasSize"), this.canvas.width, this.canvas.height);
    gl.bindVertexArray(this.nodeVao);
    gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, this.nodesShown.length);

    this._updateLabels();
    this._lastRenderMs = performance.now() - renderT0;
    this._worstRenderMs = Math.max(this._worstRenderMs || 0, this._lastRenderMs);
  }

  setShowFps(show) {
    this.showFps = show;
    this.fpsBadge.style.display = show ? "" : "none";
  }

  _tickFps() {
    if (!this.showFps) return;
    const now = performance.now();
    this._fpsFrames++;
    const dt = now - this._fpsLastT;
    if (dt >= 500) {
      const fps = (this._fpsFrames / dt) * 1000;
      this.fpsBadge.textContent = `${fps.toFixed(0)} fps · ${(dt / this._fpsFrames).toFixed(2)} ms/frame (cadence) · render() ${this._lastRenderMs.toFixed(2)}ms, worst ${this._worstRenderMs.toFixed(2)}ms`;
      this._fpsFrames = 0;
      this._fpsLastT = now;
      this._worstRenderMs = 0;
    }
  }

  start() {
    if (this._running) return;
    this._running = true;
    const loop = () => {
      if (!this._running) return;
      this.render();
      this._tickFps();
      this._raf = requestAnimationFrame(loop);
    };
    this._raf = requestAnimationFrame(loop);
  }

  destroy() {
    this._running = false;
    if (this._raf) cancelAnimationFrame(this._raf);
    this.canvas.remove();
    this.labelLayer.remove();
    this.fpsBadge.remove();
    // Dropping the canvas does NOT free the WebGL context -- it lingers
    // until GC, and browsers cap live contexts (~16) then silently kill
    // the OLDEST one to make room. Multiview opens eight at a time, so a
    // few open/close cycles would start blanking panels that were working.
    // loseContext() releases it now rather than eventually.
    const gl = this.gl;
    if (gl) {
      try {
        gl.getExtension("WEBGL_lose_context")?.loseContext();
      } catch { /* extension unavailable: nothing to release early */ }
      this.gl = null;
    }
  }
}
