/* FrameCheck: the create/update/delete views (Users, Labels), plus the request
   helper and pager the paginated tables share. The server enforces every rule
   here; these controls only offer what the role can use. */

/* ── Requests ── */
async function fcRequest(method, path, { params, body } = {}) {
  const url = new URL(apiBase() + path);
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
  });
  const res = await fetch(url, {
    method, credentials: "include", headers: fcJsonHeaders(),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401) { fcShowSignIn(); throw new Error("sign-in required"); }
  if (!res.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : `${res.status} ${res.statusText}`);
  }
  return { data, headers: res.headers };
}

/* ── Pager: Prev/Next, page size, "Showing 51–100 of 52,581" ── */
const FC_PAGE_SIZES = [25, 50, 100, 200, 500];

function fcPageFromHeaders(headers, fallbackCount) {
  return {
    total: Number(headers.get("X-Total-Count") ?? fallbackCount),
    page: Number(headers.get("X-Page") ?? 1),
    pageSize: Number(headers.get("X-Page-Size") ?? (fallbackCount || 50)),
  };
}

function fcRenderPager(hostId, { total, page, pageSize }, onChange) {
  const host = document.getElementById(hostId);
  if (!host) return;
  const first = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const last = Math.min(page * pageSize, total);
  host.innerHTML = `<div class="fc-pager">
    <span class="fc-pager-range">Showing ${first.toLocaleString()}–${last.toLocaleString()} of ${total.toLocaleString()}</span>
    <label class="fc-pager-size">Per page
      <select>${FC_PAGE_SIZES.map(n => `<option value="${n}"${n === pageSize ? " selected" : ""}>${n}</option>`).join("")}</select>
    </label>
    <button class="btn" data-go="prev"${page <= 1 ? " disabled" : ""}>Prev</button>
    <button class="btn" data-go="next"${last >= total ? " disabled" : ""}>Next</button>
  </div>`;
  host.querySelector("select").addEventListener("change", e => onChange(1, Number(e.target.value)));
  host.querySelector('[data-go="prev"]').addEventListener("click", () => onChange(page - 1, pageSize));
  host.querySelector('[data-go="next"]').addEventListener("click", () => onChange(page + 1, pageSize));
}

/* ── Users (admin) ── */
const FC_USERS = { page: 1, pageSize: 50, q: "" };

async function fcOpenUsersView() {
  const note = document.getElementById("fcUsersNote");
  const locked = typeof FC_AUTH_MODE !== "undefined" && FC_AUTH_MODE === "disabled";
  note.hidden = !locked;
  note.textContent = locked
    ? "Sign-in is disabled on this server, so there is no signed-in admin to make changes as. The list is read-only here."
    : "";
  const body = document.getElementById("fcUserRows");
  body.innerHTML = `<tr><td colspan="6" class="muted">Loading…</td></tr>`;
  try {
    const { data, headers } = await fcRequest("GET", "/admin/users", {
      params: { page: FC_USERS.page, page_size: FC_USERS.pageSize, q: FC_USERS.q },
    });
    body.innerHTML = data.length ? data.map(u => fcUserRow(u, locked)).join("")
      : `<tr><td colspan="6" class="muted">No users${FC_USERS.q ? " match that search" : " yet"}.</td></tr>`;
    fcRenderPager("fcUsersPager", fcPageFromHeaders(headers, data.length), (page, pageSize) => {
      Object.assign(FC_USERS, { page, pageSize });
      fcOpenUsersView();
    });
  } catch (err) {
    body.innerHTML = `<tr><td colspan="6" class="fc-warn">${fcEsc(err.message || String(err))}</td></tr>`;
  }
}

function fcUserRow(u, locked) {
  const off = locked ? " disabled" : "";
  const roles = ["viewer", "analyst", "admin"].map(r =>
    `<option value="${r}"${r === u.role ? " selected" : ""}>${r}</option>`).join("");
  return `<tr data-user="${u.user_id}">
    <td>${fcEsc(u.username || "—")}</td>
    <td>${fcEsc(u.email || "")}</td>
    <td>${fcEsc(u.display_name || "")}</td>
    <td><select class="fc-user-role"${off}>${roles}</select></td>
    <td>${u.is_active ? "Active" : "Inactive"}</td>
    <td><button class="btn fc-user-toggle" data-active="${u.is_active ? "1" : "0"}"${off}>${u.is_active ? "Deactivate" : "Activate"}</button></td>
  </tr>`;
}

async function fcUserAction(row, change) {
  const id = row.dataset.user;
  try {
    await fcRequest("PATCH", `/admin/users/${id}`, { body: change });
    toast("User updated", "success");
  } catch (err) {
    // Refused (e.g. the last active admin): say why and show the real state.
    toast(err.message || String(err), "error");
  }
  fcOpenUsersView();
}

function fcInitUsersView() {
  document.getElementById("fcUserSearch").addEventListener("input", debounce(e => {
    FC_USERS.q = e.target.value.trim();
    FC_USERS.page = 1;                     // a new search starts at the first page
    fcOpenUsersView();
  }, 250));
  const rows = document.getElementById("fcUserRows");
  rows.addEventListener("change", e => {
    if (e.target.classList.contains("fc-user-role")) {
      fcUserAction(e.target.closest("tr"), { role: e.target.value });
    }
  });
  rows.addEventListener("click", e => {
    if (e.target.classList.contains("fc-user-toggle")) {
      fcUserAction(e.target.closest("tr"), { is_active: e.target.dataset.active !== "1" });
    }
  });
}

/* ── Labels (read: everyone; change: analyst and up) ── */
const FC_LABELS = { page: 1, pageSize: 50, q: "", category: "", editing: null };

async function fcOpenLabelsView() {
  const body = document.getElementById("fcLabelRows");
  body.innerHTML = `<tr><td colspan="5" class="muted">Loading…</td></tr>`;
  try {
    const { data, headers } = await fcRequest("GET", "/labels", {
      params: { page: FC_LABELS.page, page_size: FC_LABELS.pageSize, q: FC_LABELS.q, category: FC_LABELS.category },
    });
    body.innerHTML = data.length ? data.map(fcLabelRow).join("")
      : `<tr><td colspan="5" class="muted">No labels${FC_LABELS.q || FC_LABELS.category ? " match" : " yet"}.</td></tr>`;
    fcApplyRoleToControls(body);
    fcRenderPager("fcLabelsPager", fcPageFromHeaders(headers, data.length), (page, pageSize) => {
      Object.assign(FC_LABELS, { page, pageSize });
      fcOpenLabelsView();
    });
  } catch (err) {
    body.innerHTML = `<tr><td colspan="5" class="fc-warn">${fcEsc(err.message || String(err))}</td></tr>`;
  }
}

function fcLabelRow(l) {
  return `<tr data-label="${l.label_id}" data-name="${fcEsc(l.label_name)}" data-category="${fcEsc(l.category || "")}"
              data-source="${fcEsc(l.source || "")}" data-sanctioned="${l.is_sanctioned ? "1" : "0"}">
    <td>${fcEsc(l.label_name)}</td>
    <td>${fcEsc(l.category || "–")}</td>
    <td>${fcEsc(l.source || "–")}</td>
    <td>${l.is_sanctioned ? "Yes" : "No"}</td>
    <td class="fc-row-actions">
      <button class="btn" data-min-role="analyst" data-act="edit">Edit</button>
      <button class="btn" data-min-role="analyst" data-act="delete">Delete</button>
    </td>
  </tr>`;
}

function fcLabelFormMessage(text) {
  document.getElementById("fcLabelMsg").textContent = text || "";
}

function fcResetLabelForm() {
  FC_LABELS.editing = null;
  ["fcLabelName", "fcLabelCategory", "fcLabelSource"].forEach(id => { document.getElementById(id).value = ""; });
  document.getElementById("fcLabelSanctioned").checked = false;
  document.getElementById("fcLabelSave").textContent = "Add label";
  document.getElementById("fcLabelCancel").hidden = true;
  fcLabelFormMessage("");
}

async function fcSaveLabel(event) {
  event.preventDefault();
  const name = document.getElementById("fcLabelName").value.trim();
  if (!name) return fcLabelFormMessage("A label needs a name.");
  const body = {
    label_name: name,
    category: document.getElementById("fcLabelCategory").value.trim() || null,
    source: document.getElementById("fcLabelSource").value.trim() || null,
    is_sanctioned: document.getElementById("fcLabelSanctioned").checked,
  };
  try {
    const editing = FC_LABELS.editing;
    if (editing) await fcRequest("PATCH", `/labels/${editing}`, { body });
    else await fcRequest("POST", "/labels", { body });
    toast(editing ? "Label updated" : "Label added", "success");
    fcResetLabelForm();
    fcOpenLabelsView();
  } catch (err) {
    fcLabelFormMessage(err.message || String(err));
  }
}

// Deleting takes two clicks, and a label still on wallets takes an explicit
// "Detach & delete": the server refuses it (409) until asked with force=true.
async function fcDeleteLabel(button, row) {
  if (!button.dataset.armed) {
    button.dataset.armed = "confirm";
    button.textContent = "Confirm delete";
    return;
  }
  const force = button.dataset.armed === "force";
  try {
    await fcRequest("DELETE", `/labels/${row.dataset.label}`, { params: force ? { force: "true" } : {} });
    toast("Label deleted", "success");
    fcOpenLabelsView();
  } catch (err) {
    if (!force && /attached/.test(err.message || "")) {
      button.dataset.armed = "force";
      button.textContent = "Detach & delete";
    }
    toast(err.message || String(err), "error");
  }
}

function fcInitLabelsView() {
  const reload = debounce(() => { FC_LABELS.page = 1; fcOpenLabelsView(); }, 250);
  document.getElementById("fcLabelSearch").addEventListener("input", e => { FC_LABELS.q = e.target.value.trim(); reload(); });
  document.getElementById("fcLabelFilter").addEventListener("input", e => { FC_LABELS.category = e.target.value.trim(); reload(); });
  document.getElementById("fcLabelForm").addEventListener("submit", fcSaveLabel);
  document.getElementById("fcLabelCancel").addEventListener("click", fcResetLabelForm);
  document.getElementById("fcLabelRows").addEventListener("click", e => {
    const button = e.target.closest("[data-act]");
    if (!button) return;
    const row = button.closest("tr");
    if (button.dataset.act === "delete") return fcDeleteLabel(button, row);
    FC_LABELS.editing = row.dataset.label;
    document.getElementById("fcLabelName").value = row.dataset.name;
    document.getElementById("fcLabelCategory").value = row.dataset.category;
    document.getElementById("fcLabelSource").value = row.dataset.source;
    document.getElementById("fcLabelSanctioned").checked = row.dataset.sanctioned === "1";
    document.getElementById("fcLabelSave").textContent = "Save changes";
    document.getElementById("fcLabelCancel").hidden = false;
  });
}

/* ── A wallet's labels, in the wallet modal ── */
async function fcLoadWalletLabels(walletId) {
  const host = document.getElementById("wmLabels");
  host.innerHTML = "";
  try {
    const { data: tags } = await fcRequest("GET", `/wallets/${walletId}/labels`);
    const canEdit = fcRoleAllows("analyst");
    const chips = tags.map(t => `<span class="badge fc-tag">${fcEsc(t.label_name)}${
      canEdit ? ` <button class="fc-tag-x" title="Remove" data-remove="${t.label_id}">×</button>` : ""}</span>`).join(" ");
    let picker = "";
    if (canEdit) {
      const { data: labels } = await fcRequest("GET", "/labels", { params: { page_size: 500 } });
      const have = new Set(tags.map(t => t.label_id));
      const options = labels.filter(l => !have.has(l.label_id))
        .map(l => `<option value="${l.label_id}">${fcEsc(l.label_name)}</option>`).join("");
      picker = options ? `<div class="fc-tag-add"><select id="wmLabelPick">${options}</select>
        <input type="number" id="wmLabelConf" min="0" max="1" step="0.05" placeholder="Confidence 0–1" />
        <button class="btn" id="wmLabelAdd">Add label</button></div>` : "";
    }
    host.innerHTML = `<div class="modal-label">Labels</div>${chips || '<span class="muted">None yet.</span>'}${picker}`;
    host.querySelectorAll("[data-remove]").forEach(b => b.addEventListener("click", async () => {
      try {
        await fcRequest("DELETE", `/wallets/${walletId}/labels/${b.dataset.remove}`);
      } catch (err) { toast(err.message || String(err), "error"); }
      fcLoadWalletLabels(walletId);
    }));
    document.getElementById("wmLabelAdd")?.addEventListener("click", async () => {
      const raw = document.getElementById("wmLabelConf").value;
      try {
        await fcRequest("POST", `/wallets/${walletId}/labels`, {
          body: { label_id: Number(document.getElementById("wmLabelPick").value),
                  confidence: raw === "" ? null : Number(raw) },
        });
      } catch (err) { toast(err.message || String(err), "error"); }
      fcLoadWalletLabels(walletId);
    });
  } catch { /* labels are extra detail: the rest of the modal still works */ }
}

/* ── Alert review ── */
const FC_ALERT_STATUSES = ["NEW", "IN_REVIEW", "CONFIRMED", "DISMISSED"];

function fcAlertStatusCell(alert) {
  if (!fcRoleAllows("analyst")) {
    return `<span class="badge ${statusBadgeClass(alert.review_status)}">${fcEsc(alert.review_status)}</span>`;
  }
  const options = FC_ALERT_STATUSES.map(s =>
    `<option value="${s}"${s === alert.review_status ? " selected" : ""}>${s.replace("_", " ")}</option>`).join("");
  return `<select class="fc-alert-status" data-alert="${alert.alert_id}">${options}</select>`;
}

async function fcReviewAlert(select) {
  const id = Number(select.dataset.alert);
  try {
    const { data } = await fcRequest("PATCH", `/alerts/${id}`, { body: { review_status: select.value } });
    const alert = ALERTS.find(a => a.alert_id === id);
    if (alert) alert.review_status = data.review_status;
    toast("Alert updated", "success");
  } catch (err) {
    toast(err.message || String(err), "error");
  }
  renderAlerts();
}
