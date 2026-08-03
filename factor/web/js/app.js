/* Factor dashboard — multi-user UI */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  me: null,
  orders: [],
};

function toast(msg, kind = "ok") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${kind}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3500);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(opts.body ? { "Content-Type": "application/json" } : {}),
      ...(opts.headers || {}),
    },
    ...opts,
  });
  let data = null;
  const text = await res.text();
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }
  if (!res.ok) {
    const err = new Error(
      (data && (data.detail || data.message)) || res.statusText || "request failed"
    );
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function show(view) {
  $("#view-login").classList.toggle("hidden", view !== "login");
  $("#view-app").classList.toggle("hidden", view !== "app");
}

function setTab(name) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab-panel").forEach((p) => {
    p.classList.toggle("hidden", p.id !== `tab-${name}`);
  });
  if (name === "orders") loadOrders();
}

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}

function pill(status) {
  const s = (status || "—").toLowerCase();
  return `<span class="pill ${s}">${escapeHtml(status || "—")}</span>`;
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderAccount() {
  const u = state.me?.user || {};
  const c = state.me?.connection;
  const kv = $("#account-kv");
  kv.innerHTML = `
    <dt>Name</dt><dd>${escapeHtml(u.display_name || "—")}</dd>
    <dt>User id</dt><dd>${escapeHtml(u.id || "—")}</dd>
    <dt>cTrader id</dt><dd>${escapeHtml(u.ctrader_ctid || "—")}</dd>
    <dt>Account</dt><dd>${c ? escapeHtml(String(c.account_id)) : "— not connected —"}</dd>
    <dt>Host</dt><dd>${c ? escapeHtml(c.host) : "—"}</dd>
  `;
  $("#user-label").textContent = u.display_name || u.ctrader_ctid || (u.id || "").slice(0, 8);
}

function renderStatus() {
  const u = state.me?.user || {};
  const kv = $("#status-kv");
  const killed = !!u.kill_switch;
  kv.innerHTML = `
    <dt>Trading</dt><dd>${u.trading_enabled ? "enabled" : "disabled"}</dd>
    <dt>Kill switch</dt><dd style="color:${killed ? "var(--danger)" : "var(--ok)"}">${
      killed ? "ON" : "off"
    }</dd>
    <dt>Prefs</dt><dd>${(state.me?.prefs || []).filter((p) => p.enabled).length} enabled</dd>
  `;
  $("#kill-banner").classList.toggle("hidden", !killed);
}

function ordersTable(orders, emptyMsg) {
  if (!orders || !orders.length) {
    return `<p class="muted">${escapeHtml(emptyMsg || "No orders yet.")}</p>`;
  }
  const rows = orders
    .map(
      (o) => `
    <tr>
      <td>${fmtTime(o.created_at)}</td>
      <td class="mono">${escapeHtml(o.kind)}</td>
      <td class="mono">${escapeHtml(o.symbol)}</td>
      <td>${escapeHtml(o.side)}</td>
      <td>${pill(o.status)}</td>
      <td class="mono">${escapeHtml(o.reaktor_alert_id)}</td>
      <td class="mono">${escapeHtml(o.broker_order_id || o.skip_reason || "—")}</td>
    </tr>`
    )
    .join("");
  return `
    <table class="data">
      <thead>
        <tr>
          <th>Time</th><th>Kind</th><th>Symbol</th><th>Side</th>
          <th>Status</th><th>Alert</th><th>Broker / reason</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderHomeOrders() {
  $("#home-orders").innerHTML = ordersTable(state.orders.slice(0, 8), "No orders yet.");
}

const ALERT_KINDS = [
  { id: "candle", label: "Range (candle)" },
  { id: "spike", label: "Spike" },
];

/** Working copy of prefs in the form (add/toggle/delete before Save). */
let prefsDraft = [];

function newPref(kind = "candle") {
  return {
    kind,
    symbol: "XAU/USD",
    enabled: true,
    max_concurrent: 1,
    max_per_day: 5,
    volume_lots: 0.01,
    sl_points: 1000,
    tp_points: 1000,
  };
}

function kindSelect(selected) {
  return ALERT_KINDS.map(
    (k) =>
      `<option value="${k.id}" ${k.id === selected ? "selected" : ""}>${escapeHtml(
        k.label
      )}</option>`
  ).join("");
}

function renderPrefs() {
  const root = $("#prefs-list");
  if (!prefsDraft.length) {
    root.innerHTML = `
      <div class="prefs-empty muted">
        No alerts configured yet. Click <strong>Add alert</strong> to choose a type
        (Range or Spike) and set lots, SL/TP, and caps.
      </div>`;
    return;
  }
  root.innerHTML = prefsDraft
    .map((p, i) => {
      const on = !!p.enabled;
      return `
      <div class="pref-card ${on ? "" : "pref-disabled"}" data-i="${i}">
        <div class="pref-card-top">
          <label class="toggle" title="${on ? "Enabled — click to pause" : "Paused — click to enable"}">
            <input type="checkbox" name="enabled" ${on ? "checked" : ""} />
            <span class="toggle-track"><span class="toggle-thumb"></span></span>
            <span class="toggle-label">${on ? "On" : "Off"}</span>
          </label>
          <div class="pref-card-title">
            <select name="kind" class="kind-select">${kindSelect(p.kind)}</select>
            <input type="text" name="symbol" class="symbol-input" value="${escapeHtml(
              p.symbol
            )}" />
          </div>
          <button type="button" class="btn-icon danger-icon btn-delete-pref" title="Delete alert" aria-label="Delete">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <polyline points="3 6 5 6 21 6"></polyline>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
              <line x1="10" y1="11" x2="10" y2="17"></line>
              <line x1="14" y1="11" x2="14" y2="17"></line>
            </svg>
          </button>
        </div>
        <div class="pref-fields">
          <label class="field">Max concurrent
            <input type="number" name="max_concurrent" min="0" step="1" value="${Number(
              p.max_concurrent ?? 1
            )}" />
          </label>
          <label class="field">Max / day
            <input type="number" name="max_per_day" min="0" step="1" value="${Number(
              p.max_per_day ?? 5
            )}" />
          </label>
          <label class="field">Lots
            <input type="number" name="volume_lots" min="0.01" step="0.01" value="${Number(
              p.volume_lots ?? 0.01
            )}" />
          </label>
          <label class="field">SL points
            <input type="number" name="sl_points" min="1" step="1" value="${Number(
              p.sl_points ?? 1000
            )}" />
          </label>
          <label class="field">TP points
            <input type="number" name="tp_points" min="1" step="1" value="${Number(
              p.tp_points ?? 1000
            )}" />
          </label>
        </div>
      </div>`;
    })
    .join("");

  // Bind per-card events
  $$("#prefs-list .pref-card").forEach((card) => {
    const i = Number(card.dataset.i);
    const syncToggleLabel = () => {
      const on = card.querySelector('[name="enabled"]').checked;
      card.classList.toggle("pref-disabled", !on);
      const lab = card.querySelector(".toggle-label");
      if (lab) lab.textContent = on ? "On" : "Off";
    };
    card.querySelector('[name="enabled"]').addEventListener("change", () => {
      prefsDraft[i].enabled = card.querySelector('[name="enabled"]').checked;
      syncToggleLabel();
    });
    card.querySelector(".btn-delete-pref").addEventListener("click", () => {
      const label = `${prefsDraft[i].kind} / ${prefsDraft[i].symbol}`;
      if (!confirm(`Delete alert “${label}”? This removes it from your list (after Save).`)) {
        return;
      }
      prefsDraft.splice(i, 1);
      renderPrefs();
    });
  });
}

function collectPrefsFromDom() {
  // Sync draft from DOM so unsaved edits aren't lost
  $$("#prefs-list .pref-card").forEach((card) => {
    const i = Number(card.dataset.i);
    if (!prefsDraft[i]) return;
    const g = (name) => card.querySelector(`[name="${name}"]`);
    prefsDraft[i] = {
      kind: g("kind").value.trim(),
      symbol: g("symbol").value.trim(),
      enabled: g("enabled").checked,
      max_concurrent: Number(g("max_concurrent").value),
      max_per_day: Number(g("max_per_day").value),
      volume_lots: Number(g("volume_lots").value),
      sl_points: Number(g("sl_points").value),
      tp_points: Number(g("tp_points").value),
    };
  });
  return prefsDraft.slice();
}

function collectPrefs() {
  return collectPrefsFromDom();
}

async function loadMe() {
  try {
    state.me = await api("/api/me");
    prefsDraft = (state.me.prefs || []).map((p) => ({ ...p }));
    show("app");
    renderAccount();
    renderStatus();
    renderPrefs();
    await loadOrders();
    return true;
  } catch (e) {
    if (e.status === 401) {
      show("login");
      try {
        const h = await api("/api/health");
        const hints = [];
        if (!h.supabase_configured) hints.push("Supabase not configured on server.");
        if (!h.multi_user) hints.push("MULTI_USER is false — set true after deploy for fan-out.");
        $("#login-hint").textContent = hints.join(" ");
      } catch {
        /* ignore */
      }
      return false;
    }
    show("login");
    $("#login-hint").textContent = e.message || "Failed to load session";
    return false;
  }
}

async function loadOrders() {
  try {
    const data = await api("/api/me/orders?limit=50");
    state.orders = data.orders || [];
    $("#orders-table").innerHTML = ordersTable(state.orders);
    renderHomeOrders();
  } catch (e) {
    $("#orders-table").innerHTML = `<p class="hint err">${escapeHtml(e.message)}</p>`;
  }
}

function bind() {
  $$(".tab").forEach((b) =>
    b.addEventListener("click", () => setTab(b.dataset.tab))
  );

  $("#btn-logout").addEventListener("click", async () => {
    try {
      await api("/auth/logout", { method: "POST" });
    } catch {
      /* ignore */
    }
    state.me = null;
    show("login");
    toast("Logged out");
  });

  $("#btn-add-pref").addEventListener("click", () => {
    collectPrefsFromDom();
    // Prefer adding spike if candle already present, else candle
    const hasCandle = prefsDraft.some((p) => p.kind === "candle");
    const hasSpike = prefsDraft.some((p) => p.kind === "spike");
    let kind = "candle";
    if (hasCandle && !hasSpike) kind = "spike";
    else if (hasCandle && hasSpike) kind = "candle";
    prefsDraft.push(newPref(kind));
    renderPrefs();
  });

  $("#btn-save-prefs").addEventListener("click", async () => {
    const prefs = collectPrefs();
    // Block duplicate kind+symbol in one save
    const keys = new Set();
    for (const p of prefs) {
      const k = `${p.kind}|${p.symbol}`;
      if (keys.has(k)) {
        toast(`Duplicate alert: ${p.kind} / ${p.symbol}`, "err");
        return;
      }
      keys.add(k);
    }
    const msg = $("#prefs-msg");
    msg.textContent = "Saving…";
    msg.className = "hint";
    try {
      const data = await api("/api/me/prefs", {
        method: "PUT",
        body: JSON.stringify({ prefs }),
      });
      state.me.prefs = data.prefs || prefs;
      prefsDraft = (state.me.prefs || []).map((p) => ({ ...p }));
      renderPrefs();
      renderStatus();
      msg.textContent = "Saved.";
      msg.className = "hint ok";
      toast("Preferences saved");
    } catch (e) {
      msg.textContent = e.message;
      msg.className = "hint err";
      toast(e.message, "err");
    }
  });

  $("#btn-refresh-orders").addEventListener("click", () => loadOrders());

  $("#btn-kill").addEventListener("click", async () => {
    if (
      !confirm(
        "Kill switch will CLOSE ALL open positions on your cTrader account and block new trades. Continue?"
      )
    ) {
      return;
    }
    const out = $("#kill-result");
    out.classList.remove("hidden");
    out.textContent = "Running…";
    try {
      const data = await api("/api/me/kill", { method: "POST" });
      out.textContent = JSON.stringify(data, null, 2);
      toast("Kill switch activated", "err");
      await loadMe();
    } catch (e) {
      out.textContent = e.message;
      toast(e.message, "err");
    }
  });

  $("#btn-resume").addEventListener("click", async () => {
    try {
      const data = await api("/api/me/resume", { method: "POST" });
      $("#kill-result").classList.remove("hidden");
      $("#kill-result").textContent = JSON.stringify(data, null, 2);
      toast("Trading resumed");
      await loadMe();
    } catch (e) {
      toast(e.message, "err");
    }
  });
}

bind();
loadMe();
