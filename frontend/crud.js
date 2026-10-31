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
