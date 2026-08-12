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

/** Won / Lost / Break-even tag from result column or detail.outcome */
function orderResult(o) {
  const r = (o && (o.result || (o.detail && o.detail.outcome && o.detail.outcome.result))) || "";
  const key = String(r).toLowerCase();
  if (!key || key === "null" || key === "undefined") {
    if ((o && o.status) === "open") return "—";
    if ((o && o.status) === "closed") return pill("unknown");
    return "—";
  }
  const labels = {
    won: "Won",
    lost: "Lost",
    breakeven: "Break-even",
    unknown: "Unknown",
  };
  return `<span class="pill ${escapeHtml(key)}">${escapeHtml(labels[key] || key)}</span>`;
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
  const enabledCount = (state.me?.prefs || []).filter((p) => p.enabled).length;
  const totalCount = (state.me?.prefs || []).length;
  kv.innerHTML = `
    <dt>Trading</dt><dd>${u.trading_enabled ? "enabled" : "disabled"}</dd>
    <dt>Kill switch</dt><dd style="color:${killed ? "var(--danger)" : "var(--ok)"}">${
      killed ? "ON" : "off"
    }</dd>
    <dt>Prefs</dt><dd>${enabledCount} on · ${totalCount} saved</dd>
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
      <td>${orderResult(o)}</td>
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
          <th>Status</th><th>Result</th><th>Alert</th><th>Broker / reason</th>
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
  { id: "spike_neo", label: "Spike Neo" },
  { id: "impulse_retrace", label: "Impulse retrace" },
];

/**
 * Symbols offered in the Alerts UI.
 * `id` is stored and must match Reaktor alert.symbol (pair form).
 * Labels are broker-style names shown in the dropdown.
 */
const ALERT_SYMBOLS = [
  { id: "XAU/USD", label: "XAUUSD" },
  { id: "EUR/USD", label: "EURUSD" },
  { id: "BTC/USD", label: "BTC" },
];

/** Working copy of prefs in the form (field edits before Save; toggle/delete persist immediately). */
let prefsDraft = [];
let prefsSaving = false;

function newPref(kind = "candle") {
  // Spike-family defaults match typical tighter stops (editable after add)
  const spikeLike = kind === "spike" || kind === "spike_neo";
  // Continuation after pullback — same risk shell as range by default
  return {
    kind,
    symbol: ALERT_SYMBOLS[0].id,
    enabled: true,
    max_concurrent: 1,
    max_per_day: spikeLike ? 1 : 5,
    volume_lots: 0.01,
    sl_points: spikeLike ? 500 : 1000,
    tp_points: 1000,
    breakeven_half_tp: true,
    breakeven_fraction: 0.5,
  };
}

/** Display fraction of TP as percent for the form (0.5 → 50). */
function beFractionToPercent(frac) {
  const f = Number(frac);
  if (!Number.isFinite(f) || f <= 0) return 50;
  return Math.round(Math.min(100, Math.max(1, f * 100)));
}

/** Form percent → stored fraction (50 → 0.5). */
function bePercentToFraction(pct) {
  const p = Number(pct);
  if (!Number.isFinite(p) || p <= 0) return 0.5;
  return Math.min(1, Math.max(0.01, p / 100));
}

function kindSelect(selected) {
  return ALERT_KINDS.map(
    (k) =>
      `<option value="${k.id}" ${k.id === selected ? "selected" : ""}>${escapeHtml(
        k.label
      )}</option>`
  ).join("");
}

function symbolSelect(selected) {
  const known = new Set(ALERT_SYMBOLS.map((s) => s.id));
  const opts = ALERT_SYMBOLS.map(
    (s) =>
      `<option value="${escapeHtml(s.id)}" ${
        s.id === selected ? "selected" : ""
      }>${escapeHtml(s.label)}</option>`
  );
  // Keep a legacy/unknown value visible so save does not silently change it
  if (selected && !known.has(selected)) {
    opts.unshift(
      `<option value="${escapeHtml(selected)}" selected>${escapeHtml(
        selected
      )} (current)</option>`
    );
  }
  return opts.join("");
}

function setPrefsMsg(text, kind = "") {
  const msg = $("#prefs-msg");
  msg.textContent = text || "";
  msg.className = kind ? `hint ${kind}` : "hint";
}

function symbolLabel(id) {
  return ALERT_SYMBOLS.find((s) => s.id === id)?.label || id;
}

function validatePrefs(prefs) {
  const allowed = new Set(ALERT_SYMBOLS.map((s) => s.id));
  // Also allow symbols already in the draft (legacy rows) so save doesn't brick them
  for (const p of prefsDraft) {
    if (p?.symbol) allowed.add(p.symbol);
  }
  const keys = new Set();
  for (const p of prefs) {
    if (!p.kind || !p.symbol) {
      return `Each alert needs a type and symbol.`;
    }
    if (!allowed.has(p.symbol)) {
      const labels = ALERT_SYMBOLS.map((s) => s.label).join(", ");
      return `Unknown symbol “${p.symbol}”. Choose ${labels}.`;
    }
    const k = `${p.kind}|${p.symbol}`;
    if (keys.has(k)) {
      return `Duplicate alert: ${p.kind} / ${symbolLabel(p.symbol)}`;
    }
    keys.add(k);
  }
  return null;
}

/**
 * Persist full prefs list. Disabled rows are kept (enabled=false).
 * Delete = omit from list. Returns saved list or throws.
 */
async function persistPrefs(prefs, { quiet = false } = {}) {
  const err = validatePrefs(prefs);
  if (err) {
    if (!quiet) toast(err, "err");
    throw new Error(err);
  }
  prefsSaving = true;
  try {
    if (!quiet) setPrefsMsg("Saving…");
    const data = await api("/api/me/prefs", {
      method: "PUT",
      body: JSON.stringify({ prefs }),
    });
    const saved = data.prefs || prefs;
    if (!state.me) state.me = {};
    state.me.prefs = saved;
    prefsDraft = saved.map((p) => ({ ...p }));
    if (!quiet) {
      setPrefsMsg("Saved.", "ok");
      toast("Preferences saved");
    }
    renderStatus();
    return saved;
  } finally {
    prefsSaving = false;
  }
}

function renderPrefs() {
  const root = $("#prefs-list");
  const addWrap = $("#prefs-add-wrap");

  if (!prefsDraft.length) {
    root.innerHTML = `
      <div class="prefs-empty">
        <p class="muted">No alerts configured yet.</p>
        <p class="muted small">Add Range, Spike, Spike Neo, or Impulse retrace — set lots / SL / TP / caps, then Save.</p>
        <button type="button" class="btn primary" id="btn-add-pref-empty">+ Add alert</button>
      </div>`;
    // Hide bottom add when empty — primary CTA is inside empty state
    if (addWrap) addWrap.classList.add("hidden");
    const emptyBtn = $("#btn-add-pref-empty");
    if (emptyBtn) emptyBtn.addEventListener("click", onAddPref);
    return;
  }

  if (addWrap) addWrap.classList.remove("hidden");

  root.innerHTML = prefsDraft
    .map((p, i) => {
      const on = !!p.enabled;
      return `
      <div class="pref-card ${on ? "" : "pref-disabled"}" data-i="${i}">
        <div class="pref-card-top">
          <label class="toggle" title="${
            on ? "On — click to pause (keeps your settings)" : "Off — click to enable"
          }">
            <input type="checkbox" name="enabled" ${on ? "checked" : ""} ${
              prefsSaving ? "disabled" : ""
            } />
            <span class="toggle-track"><span class="toggle-thumb"></span></span>
            <span class="toggle-label">${on ? "On" : "Off"}</span>
          </label>
          <div class="pref-card-title">
            <select name="kind" class="kind-select" aria-label="Alert type">${kindSelect(
              p.kind
            )}</select>
            <select name="symbol" class="symbol-select" aria-label="Symbol">${symbolSelect(
              p.symbol
            )}</select>
          </div>
          <button type="button" class="btn-icon danger-icon btn-delete-pref" title="Delete alert permanently" aria-label="Delete" ${
            prefsSaving ? "disabled" : ""
          }>
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
          <label class="field field-check" title="Move SL to entry when price has made this fraction of the TP distance">
            <span>Breakeven</span>
            <input type="checkbox" name="breakeven_half_tp" ${
              p.breakeven_half_tp !== false ? "checked" : ""
            } />
          </label>
          <label class="field" title="Trigger BE when favorable move reaches this % of TP distance (50 = half TP)">
            BE at % of TP
            <input type="number" name="breakeven_pct" min="1" max="100" step="1" value="${beFractionToPercent(
              p.breakeven_fraction ?? 0.5
            )}" />
          </label>
        </div>
      </div>`;
    })
    .join("");

  $$("#prefs-list .pref-card").forEach((card) => {
    const i = Number(card.dataset.i);

    const syncToggleLabel = () => {
      const on = card.querySelector('[name="enabled"]').checked;
      card.classList.toggle("pref-disabled", !on);
      const lab = card.querySelector(".toggle-label");
      if (lab) lab.textContent = on ? "On" : "Off";
    };

    card.querySelector('[name="enabled"]').addEventListener("change", async () => {
      if (prefsSaving) return;
      // Sync all fields first so we don't drop unsaved edits
      collectPrefsFromDom();
      const on = card.querySelector('[name="enabled"]').checked;
      prefsDraft[i].enabled = on;
      syncToggleLabel();
      try {
        await persistPrefs(prefsDraft, { quiet: true });
        toast(on ? "Alert enabled" : "Alert paused — settings kept");
        setPrefsMsg(on ? "Alert on." : "Alert off (settings kept in DB).", "ok");
        renderPrefs();
      } catch (e) {
        // Revert toggle on failure
        prefsDraft[i].enabled = !on;
        renderPrefs();
        setPrefsMsg(e.message, "err");
        toast(e.message, "err");
      }
    });

    card.querySelector(".btn-delete-pref").addEventListener("click", async () => {
      if (prefsSaving) return;
      collectPrefsFromDom();
      const label = `${prefsDraft[i].kind} / ${symbolLabel(prefsDraft[i].symbol)}`;
      if (
        !confirm(
          `Delete alert “${label}” permanently?\n\nThis removes it from the database. Use the On/Off toggle if you only want to pause.`
        )
      ) {
        return;
      }
      const removed = prefsDraft.splice(i, 1)[0];
      renderPrefs();
      try {
        await persistPrefs(prefsDraft, { quiet: true });
        toast(`Deleted ${label}`);
        setPrefsMsg("Alert deleted.", "ok");
        renderPrefs();
      } catch (e) {
        // Restore on failure
        prefsDraft.splice(i, 0, removed);
        renderPrefs();
        setPrefsMsg(e.message, "err");
        toast(e.message, "err");
      }
    });
  });
}

function collectPrefsFromDom() {
  $$("#prefs-list .pref-card").forEach((card) => {
    const i = Number(card.dataset.i);
    if (!prefsDraft[i]) return;
    const g = (name) => card.querySelector(`[name="${name}"]`);
    const beEl = g("breakeven_half_tp");
    const bePctEl = g("breakeven_pct");
    prefsDraft[i] = {
      kind: g("kind").value.trim(),
      symbol: g("symbol").value.trim(),
      enabled: g("enabled").checked,
      max_concurrent: Number(g("max_concurrent").value),
      max_per_day: Number(g("max_per_day").value),
      volume_lots: Number(g("volume_lots").value),
      sl_points: Number(g("sl_points").value),
      tp_points: Number(g("tp_points").value),
      breakeven_half_tp: beEl ? beEl.checked : true,
      breakeven_fraction: bePctEl
        ? bePercentToFraction(bePctEl.value)
        : Number(prefsDraft[i].breakeven_fraction) || 0.5,
    };
  });
  return prefsDraft.slice();
}

function onAddPref() {
  collectPrefsFromDom();
  // Prefer first ALERT_KINDS id not already in the draft (candle → spike → spike_neo)
  const used = new Set(prefsDraft.map((p) => p.kind));
  const next = ALERT_KINDS.find((k) => !used.has(k.id));
  const kind = next ? next.id : "candle";
  prefsDraft.push(newPref(kind));
  renderPrefs();
  setPrefsMsg("New alert added — edit fields, then Save (or toggle Off to pause).");
  // Scroll to the new card
  const cards = $$("#prefs-list .pref-card");
  const last = cards[cards.length - 1];
  if (last) last.scrollIntoView({ behavior: "smooth", block: "nearest" });
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
        if (!h.supabase_configured) {
          hints.push("Supabase not configured on server.");
        }
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

  async function doLogout() {
    try {
      await api("/auth/logout", { method: "POST" });
    } catch {
      /* ignore */
    }
    state.me = null;
    show("login");
    toast("Logged out");
  }
  $("#btn-logout").addEventListener("click", doLogout);
  const btnLogoutDanger = $("#btn-logout-danger");
  if (btnLogoutDanger) btnLogoutDanger.addEventListener("click", doLogout);

  $("#btn-add-pref").addEventListener("click", onAddPref);

  $("#btn-save-prefs").addEventListener("click", async () => {
    if (prefsSaving) return;
    const prefs = collectPrefsFromDom();
    try {
      await persistPrefs(prefs);
      renderPrefs();
    } catch (e) {
      setPrefsMsg(e.message, "err");
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
