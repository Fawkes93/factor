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
  const current = document.querySelector(".tab.active");
  if (current && current.dataset.tab === "prefs" && name !== "prefs" && !confirmLeavePrefs()) {
    return;
  }
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab-panel").forEach((p) => {
    p.classList.toggle("hidden", p.id !== `tab-${name}`);
  });
  if (name === "orders" && Date.now() - ordersLoadedAt >= ORDERS_POLL_MS) loadOrders();
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

function orderDetail(o) {
  let d = o && o.detail;
  if (typeof d === "string") {
    try {
      d = JSON.parse(d);
    } catch {
      d = null;
    }
  }
  return d && typeof d === "object" ? d : {};
}

/** CRT row: TP1 state and the flatten reason, when this order has them. */
function crtManage(o) {
  if (!o || o.kind !== "crt") return "";
  const prot = orderDetail(o).protect || {};
  const bits = [];
  if (prot.partial_skipped) bits.push("TP1 skipped (min volume)");
  else if (prot.partial_done) bits.push("TP1 done");
  else if (String(o.status || "").toLowerCase() === "open") bits.push("TP1 open");
  if (prot.flatten_reason) bits.push(prot.flatten_reason);
  return bits.join(" · ");
}

function brokerCell(o) {
  const main = (o && (o.broker_order_id || o.skip_reason)) || "—";
  const crt = crtManage(o);
  if (!crt) return escapeHtml(main);
  return `${escapeHtml(main)}<div class="muted small">${escapeHtml(crt)}</div>`;
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

function accountLabel(a) {
  if (!a) return "cTrader";
  if (a.label) return a.label;
  const broker = String(a.broker_name || "cTrader").trim() || "cTrader";
  const env = a.is_live ? "Live" : "Demo";
  const ident = a.trader_login || a.account_id || "";
  return `${broker} · ${env} · ${ident}`;
}

function renderAccountSwitch() {
  const sel = $("#account-switch");
  if (!sel) return;
  const wrap = sel.closest(".account-switch-wrap");
  const accounts = state.me?.accounts || [];
  const active = state.me?.user?.active_account_id ?? state.me?.connection?.account_id;
  if (!accounts.length) {
    sel.classList.add("hidden");
    if (wrap) wrap.classList.add("hidden");
    sel.innerHTML = "";
    sel.disabled = true;
    return;
  }
  sel.innerHTML = accounts
    .map((a) => {
      const id = a.account_id;
      const selected = String(id) === String(active) ? " selected" : "";
      return `<option value="${escapeHtml(String(id))}"${selected}>${escapeHtml(
        accountLabel(a)
      )}</option>`;
    })
    .join("");
  sel.classList.remove("hidden");
  if (wrap) wrap.classList.remove("hidden");
  // One account still shows, so the active account is visible. It cannot switch.
  sel.disabled = accounts.length < 2;
}

function renderAccount() {
  const u = state.me?.user || {};
  const c = state.me?.connection;
  const kv = $("#account-kv");
  const env = c ? (c.is_live ? "Live" : "Demo") : "—";
  kv.innerHTML = `
    <dt>Broker</dt><dd>${c ? escapeHtml(c.broker_name || "cTrader") : "—"}</dd>
    <dt>Environment</dt><dd>${escapeHtml(env)}</dd>
    <dt>Login</dt><dd>${c ? escapeHtml(String(c.trader_login || "—")) : "—"}</dd>
    <dt>Account id</dt><dd>${c ? escapeHtml(String(c.account_id)) : "— not connected —"}</dd>
    <dt>Currency</dt><dd>${escapeHtml(accountCurrency() || "—")}</dd>
    <dt>Host</dt><dd>${c ? escapeHtml(c.host) : "—"}</dd>
  `;
  renderAccountSwitch();
  const label = c ? accountLabel(c) : u.display_name || u.ctrader_ctid || (u.id || "").slice(0, 8);
  $("#user-label").textContent = label;
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
      killed ? "ON (this account)" : "off"
    }</dd>
    <dt>Prefs</dt><dd>${enabledCount} on · ${totalCount} saved</dd>
  `;
  $("#kill-banner").classList.toggle("hidden", !killed);
}

function accountCurrency() {
  const raw = state.me?.deposit_currency || state.me?.connection?.deposit_currency || "";
  const code = String(raw || "").trim().toUpperCase();
  return /^[A-Z]{3}$/.test(code) ? code : "";
}

function fmtMoney(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return "—";
  const ccy = accountCurrency();
  const plain = new Intl.NumberFormat(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  if (!ccy) return plain.format(v);
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency: ccy }).format(v);
  } catch {
    return plain.format(v);
  }
}

function fmtPnl(o) {
  if (!o || o.pnl == null || o.pnl === "") return `<span class="muted">—</span>`;
  const n = Number(o.pnl);
  if (!Number.isFinite(n)) return `<span class="muted">—</span>`;
  const tone = n > 0.0000001 ? "pos" : n < -0.0000001 ? "neg" : "flat";
  const live = o.pnl_live ? " live" : "";
  const title = o.pnl_live ? ' title="unrealized"' : "";
  return `<span class="pnl ${tone}${live}"${title}>${escapeHtml(fmtMoney(n))}</span>`;
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
      <td>${orderResult(o)}<div class="pnl-inline">${fmtPnl(o)}</div></td>
      <td class="pnl-col">${fmtPnl(o)}</td>
      <td class="mono">${escapeHtml(o.reaktor_alert_id)}</td>
      <td class="mono">${brokerCell(o)}</td>
    </tr>`
    )
    .join("");
  return `
    <table class="data">
      <thead>
        <tr>
          <th>Time</th><th>Kind</th><th>Symbol</th><th>Side</th>
          <th>Status</th><th>Result</th><th class="pnl-col">P/L</th><th>Alert</th><th>Broker / reason</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function ordersSummary(orders) {
  const today0 = new Date();
  today0.setHours(0, 0, 0, 0);
  const week0 = new Date(today0);
  const weekday = week0.getDay();
  week0.setDate(week0.getDate() - (weekday === 0 ? 6 : weekday - 1));
  let today = 0;
  let week = 0;
  let open = 0;
  let todayAny = false;
  let weekAny = false;
  let openAny = false;
  for (const o of orders || []) {
    const n = Number(o && o.pnl);
    if (!Number.isFinite(n)) continue;
    const status = String((o && o.status) || "").toLowerCase();
    if (o.pnl_live || status === "open") {
      open += n;
      openAny = true;
      continue;
    }
    if (status !== "closed") continue;
    const when = new Date(o.closed_at || o.created_at || 0);
    if (Number.isNaN(when.getTime())) continue;
    if (when >= today0) {
      today += n;
      todayAny = true;
    }
    if (when >= week0) {
      week += n;
      weekAny = true;
    }
  }
  const cell = (label, value, any, live) => {
    const inner = any ? fmtPnl({ pnl: value, pnl_live: !!live }) : `<span class="muted">—</span>`;
    return `<div><span class="muted small">${label}</span>${inner}</div>`;
  };
  return `<div class="orders-summary" aria-label="P/L totals">${cell("Today", today, todayAny, false)}${cell(
    "This week",
    week,
    weekAny,
    false
  )}${cell("Open", open, openAny, true)}</div>`;
}

function renderHomeOrders() {
  $("#home-orders").innerHTML = ordersTable(state.orders.slice(0, 8), "No orders yet.");
}

const ALERT_KINDS = [
  { id: "candle", label: "Range (candle)" },
  { id: "spike", label: "Spike" },
  { id: "spike_neo", label: "Spike Neo" },
  { id: "impulse_retrace", label: "Impulse retrace" },
  { id: "asia_breakout", label: "Asia breakout" },
  { id: "asia_breakout_h", label: "Asia Overnight H" },
  { id: "dawn_cross", label: "Dawn Cross" },
  { id: "crt", label: "CRT" },
  { id: "ai_gold", label: "AI trader" },
];

function isAsiaBox(kind) {
  return kind === "asia_breakout" || kind === "asia_breakout_h";
}

function isSessionManaged(kind) {
  return isAsiaBox(kind) || kind === "dawn_cross";
}

function isCrt(kind) {
  return kind === "crt";
}

function isAiTrader(kind) {
  return kind === "ai_gold";
}

function isBreakevenOff(kind) {
  return isSessionManaged(kind) || isCrt(kind);
}

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

/** Saved rows from the server. Draft edits are dirty until that card is saved. */
let prefsBaseline = [];
/** Working copy. `_originKey` is the saved kind|symbol; null means not stored yet. */
let prefsDraft = [];
let prefsSaving = false;
let prefCid = 1;

const KIND_HINTS = {
  candle: "Range expansion.",
  spike: "Short-window spike.",
  spike_neo: "Reaktor spike_neo.",
  impulse_retrace: "Pullback after a blast (impulse_retrace).",
  asia_breakout:
    "Bucharest box, then pullback. SL/TP from Reaktor unless Override SL / TP is on.",
  asia_breakout_h:
    "Asia Overnight H: Bucharest box, then pullback. SL/TP from Reaktor unless Override is on.",
  dawn_cross: "Chișinău 02:00–06:00 5m cross. SL/TP and flatten hour come from Reaktor.",
  crt: "Half at TP1, stop stays, flat Fri 23:45.",
  ai_gold: "Grok recommends XAUUSD. These limits are enforced in code. In Telegram send /subscribe.",
};

const PREF_DIFF_FIELDS = [
  "enabled",
  "max_concurrent",
  "max_per_day",
  "volume_lots",
  "sl_points",
  "tp_points",
  "sl_tp_override",
  "breakeven_half_tp",
  "breakeven_fraction",
  "min_volume_lots",
  "max_volume_lots",
  "min_rr",
  "max_rr",
  "daily_loss_cap_usd",
];

function newPref(kind = "candle") {
  // Spike-family defaults match typical tighter stops (editable after add)
  const spikeLike = kind === "spike" || kind === "spike_neo";
  const session = isBreakevenOff(kind);
  const ai = isAiTrader(kind);
  // Continuation after pullback — same risk shell as range by default
  return {
    kind,
    symbol: ai ? "XAU/USD" : ALERT_SYMBOLS[0].id,
    enabled: true,
    max_concurrent: 1,
    max_per_day: session || spikeLike ? 1 : 5,
    volume_lots: 0.01,
    sl_points: spikeLike ? 500 : 1000,
    tp_points: 1000,
    breakeven_half_tp: session ? false : true,
    breakeven_fraction: 0.5,
    sl_tp_override: false,
    min_volume_lots: 0.01,
    max_volume_lots: 0.01,
    min_rr: 1.5,
    max_rr: "",
    daily_loss_cap_usd: 50,
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
    if (isAsiaBox(p.kind) && p.sl_tp_override) {
      if (!(Number(p.sl_points) > 0) || !(Number(p.tp_points) > 0)) {
        return "Asia override needs SL and TP points greater than 0.";
      }
    }
    const k = `${p.kind}|${p.symbol}`;
    if (keys.has(k)) {
      return `Duplicate alert: ${p.kind} / ${symbolLabel(p.symbol)}`;
    }
    keys.add(k);
  }
  return null;
}

function newCid() {
  prefCid += 1;
  return `c${prefCid}`;
}

function prefKey(p) {
  return `${p.kind}|${p.symbol}`;
}

function splitPrefKey(key) {
  const i = String(key).indexOf("|");
  return [String(key).slice(0, i), String(key).slice(i + 1)];
}

function prefUrl(kind, symbol) {
  const sym = String(symbol)
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/");
  return `/api/me/prefs/${encodeURIComponent(kind)}/${sym}`;
}

function stampPref(p, originKey) {
  return {
    ...p,
    _cid: p._cid || p.id || newCid(),
    _originKey: originKey,
  };
}

function baselineByKey() {
  const map = new Map();
  for (const row of prefsBaseline) map.set(prefKey(row), row);
  return map;
}

function sameValue(field, a, b) {
  if (
    field === "volume_lots" ||
    field === "breakeven_fraction" ||
    field === "min_volume_lots" ||
    field === "max_volume_lots" ||
    field === "min_rr" ||
    field === "max_rr" ||
    field === "daily_loss_cap_usd"
  ) {
    if (a === "" || a == null || b === "" || b == null) {
      return (a === "" || a == null) && (b === "" || b == null);
    }
    const na = Number(a);
    const nb = Number(b);
    if (Number.isFinite(na) && Number.isFinite(nb)) return Math.abs(na - nb) < 1e-9;
  }
  if (field === "enabled" || field === "sl_tp_override" || field === "breakeven_half_tp") {
    return !!a === !!b;
  }
  if (field === "max_concurrent" || field === "max_per_day" || field === "sl_points" || field === "tp_points") {
    return Number(a) === Number(b);
  }
  return a === b;
}

function diffFields(current, base) {
  const out = {};
  for (const field of PREF_DIFF_FIELDS) {
    if (!sameValue(field, current[field], base ? base[field] : undefined)) {
      out[field] = current[field];
    }
  }
  return out;
}

function cardIsDirty(p) {
  if (!p) return false;
  if (!p._originKey) return true;
  if (prefKey(p) !== p._originKey) return true;
  const base = baselineByKey().get(p._originKey);
  if (!base) return true;
  return Object.keys(diffFields(p, base)).length > 0;
}

function confirmLeavePrefs() {
  if (!prefsDraft.some(cardIsDirty)) return true;
  return confirm("You have unsaved alert changes. Leave without saving?");
}

function syncBaselineFromDraft() {
  if (!state.me) state.me = {};
  state.me.prefs = prefsBaseline.map((p) => ({ ...p }));
  renderStatus();
}

function rememberSaved(i, row, previousKey) {
  const cid = prefsDraft[i]._cid;
  const key = prefKey(row);
  prefsDraft[i] = stampPref(row, key);
  prefsDraft[i]._cid = cid;
  prefsBaseline = prefsBaseline.filter((b) => {
    const k = prefKey(b);
    if (previousKey && k === previousKey) return false;
    return k !== key;
  });
  prefsBaseline.push({ ...row });
  syncBaselineFromDraft();
}

function replaceBaselineRow(originKey, row) {
  const key = prefKey(row);
  prefsBaseline = prefsBaseline.filter((b) => prefKey(b) !== originKey && prefKey(b) !== key);
  prefsBaseline.push({ ...row });
  syncBaselineFromDraft();
}

function prefPayload(p) {
  return {
    enabled: !!p.enabled,
    max_concurrent: Number(p.max_concurrent),
    max_per_day: Number(p.max_per_day),
    volume_lots: Number(p.volume_lots),
    sl_points: Number(p.sl_points),
    tp_points: Number(p.tp_points),
    breakeven_half_tp: !!p.breakeven_half_tp,
    breakeven_fraction: Number(p.breakeven_fraction),
    sl_tp_override: !!p.sl_tp_override,
    min_volume_lots: Number(p.min_volume_lots ?? 0.01),
    max_volume_lots: Number(p.max_volume_lots ?? p.volume_lots ?? 0.01),
    min_rr: Number(p.min_rr ?? 1.5),
    max_rr: p.max_rr === "" || p.max_rr == null ? null : Number(p.max_rr),
    daily_loss_cap_usd: Number(p.daily_loss_cap_usd ?? 50),
  };
}

function validateOnePref(p, index) {
  const err = validatePrefs([p]);
  if (err) return err;
  const key = prefKey(p);
  for (let j = 0; j < prefsDraft.length; j++) {
    if (j !== index && prefKey(prefsDraft[j]) === key) {
      return `Duplicate alert: ${p.kind} / ${symbolLabel(p.symbol)}`;
    }
  }
  return null;
}

/** Save one card. New rows POST. A renamed row is deleted and created. Otherwise PATCH the diff. */
async function persistCard(i) {
  const p = prefsDraft[i];
  if (!p) return null;
  const err = validateOnePref(p, i);
  if (err) throw new Error(err);
  if (!p._originKey) {
    const data = await api(prefUrl(p.kind, p.symbol), {
      method: "POST",
      body: JSON.stringify(prefPayload(p)),
    });
    rememberSaved(i, data.pref || p, null);
    return data.pref;
  }
  if (prefKey(p) !== p._originKey) {
    const previous = p._originKey;
    const [oldKind, oldSymbol] = splitPrefKey(previous);
    await api(prefUrl(oldKind, oldSymbol), { method: "DELETE" });
    try {
      const data = await api(prefUrl(p.kind, p.symbol), {
        method: "POST",
        body: JSON.stringify(prefPayload(p)),
      });
      rememberSaved(i, data.pref || p, previous);
      return data.pref;
    } catch (e) {
      p._originKey = null;
      throw e;
    }
  }
  const fields = diffFields(p, baselineByKey().get(p._originKey) || {});
  if (!Object.keys(fields).length) return p;
  const data = await api(prefUrl(p.kind, p.symbol), {
    method: "PATCH",
    body: JSON.stringify(fields),
  });
  rememberSaved(i, data.pref || { ...p, ...fields }, p._originKey);
  return data.pref;
}

function refreshDirtyUi() {
  const n = prefsDraft.filter(cardIsDirty).length;
  const bar = $("#prefs-dirty-bar");
  if (bar) bar.classList.toggle("hidden", n === 0);
  const label = $("#prefs-dirty-count");
  if (label) label.textContent = n === 1 ? "1 alert changed" : `${n} alerts changed`;
}

/** CRT: Reaktor SL/TP, partial at TP1, original stop. No breakeven. */
function crtRiskFields(p) {
  const sl = Number(p.sl_points ?? 1000);
  const tp = Number(p.tp_points ?? 1000);
  return `
          <input type="hidden" name="sl_points" value="${sl}" />
          <input type="hidden" name="tp_points" value="${tp}" />
          <p class="muted small pref-note">SL / TP come from Reaktor. Half closes at TP1; the runner keeps the original stop and the broker TP. No breakeven. Flatten is Friday 23:45 Chișinău or max hold, whichever is first. Lots / caps still apply.</p>`;
}

/** Dawn Cross: Reaktor sends SL/TP and the flatten hour. No point override. */
function dawnRiskFields(p) {
  const sl = Number(p.sl_points ?? 1000);
  const tp = Number(p.tp_points ?? 1000);
  return `
          <input type="hidden" name="sl_points" value="${sl}" />
          <input type="hidden" name="tp_points" value="${tp}" />
          <p class="muted small pref-note">SL / TP come from Reaktor. Flatten follows the alert hour — no breakeven. Lots / caps still apply.</p>`;
}

/** Asia box risk: Reaktor box unless the user overrides with their own points. */
function asiaRiskFields(p) {
  const override = !!p.sl_tp_override;
  const sl = Number(p.sl_points ?? 1000);
  const tp = Number(p.tp_points ?? 1000);
  const note = override
    ? "Your SL / TP points replace the Reaktor Asia box. Trail + flatten still follow the alert — no breakeven."
    : "SL / TP come from Reaktor (Asia box). Trail + flatten follow the alert — no breakeven. Lots / caps still apply.";
  const points = override
    ? `<label class="field">SL points
            <input type="number" name="sl_points" min="1" step="1" value="${sl}" />
          </label>
          <label class="field">TP points
            <input type="number" name="tp_points" min="1" step="1" value="${tp}" />
          </label>`
    : `<input type="hidden" name="sl_points" value="${sl}" />
          <input type="hidden" name="tp_points" value="${tp}" />`;
  return `
          <label class="field field-check field-check-row" title="Use your SL and TP points instead of the Reaktor Asia box">
            <span>Override SL / TP</span>
            <input type="checkbox" name="sl_tp_override" ${override ? "checked" : ""} />
          </label>
          ${points}
          <p class="muted small pref-note">${note}</p>`;
}

function aiTraderFields(p) {
  const linked = !!(state.me && state.me.user && state.me.user.telegram_linked);
  const maxRr = p.max_rr == null ? "" : p.max_rr;
  return `
          <label class="field">Max concurrent
            <input type="number" name="max_concurrent" min="0" step="1" value="${Number(
              p.max_concurrent ?? 1
            )}" />
          </label>
          <label class="field">Min lots
            <input type="number" name="min_volume_lots" min="0.01" step="0.01" value="${Number(
              p.min_volume_lots ?? 0.01
            )}" />
          </label>
          <label class="field">Max lots
            <input type="number" name="max_volume_lots" min="0.01" step="0.01" value="${Number(
              p.max_volume_lots ?? p.volume_lots ?? 0.01
            )}" />
          </label>
          <label class="field">Min RR
            <input type="number" name="min_rr" min="0.1" step="0.1" value="${Number(
              p.min_rr ?? 1.5
            )}" />
          </label>
          <label class="field">Max RR
            <input type="number" name="max_rr" min="0.1" step="0.1" placeholder="no cap" value="${maxRr}" />
          </label>
          <label class="field">Daily loss cap USD
            <input type="number" name="daily_loss_cap_usd" min="1" step="1" value="${Number(
              p.daily_loss_cap_usd ?? 50
            )}" />
          </label>
          <input type="hidden" name="max_per_day" value="${Number(p.max_per_day ?? 5)}" />
          <input type="hidden" name="volume_lots" value="${Number(
            p.max_volume_lots ?? p.volume_lots ?? 0.01
          )}" />
          <input type="hidden" name="sl_points" value="${Number(p.sl_points ?? 1000)}" />
          <input type="hidden" name="tp_points" value="${Number(p.tp_points ?? 1000)}" />
          <p class="muted small pref-note">Stops and targets come from the analysis. Lots are clamped to this range.</p>
          <p class="muted small pref-note">Telegram: ${
            linked ? "linked" : "not linked"
          }. Send /subscribe to the bot.</p>`;
}

function renderPrefs() {
  const root = $("#prefs-list");
  const addWrap = $("#prefs-add-wrap");

  if (!prefsDraft.length) {
    root.innerHTML = `
      <div class="prefs-empty">
        <p class="muted">No alerts configured yet.</p>
        <p class="muted small">Add Range, Spike, Spike Neo, Impulse retrace, Asia, Dawn Cross, or CRT — set lots / caps, then Save.</p>
        <button type="button" class="btn primary" id="btn-add-pref-empty">+ Add alert</button>
      </div>`;
    // Hide bottom add when empty — primary CTA is inside empty state
    if (addWrap) addWrap.classList.add("hidden");
    const emptyBtn = $("#btn-add-pref-empty");
    if (emptyBtn) emptyBtn.addEventListener("click", onAddPref);
    refreshDirtyUi();
    return;
  }

  if (addWrap) addWrap.classList.remove("hidden");

  root.innerHTML = prefsDraft
    .map((p, i) => {
      const on = !!p.enabled;
      const dirty = cardIsDirty(p);
      const hint = KIND_HINTS[p.kind] || "";
      return `
      <div class="pref-card ${on ? "" : "pref-disabled"}${dirty ? " dirty" : ""}" data-i="${i}">
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
            <select name="kind" class="kind-select" aria-label="Alert type" title="${escapeHtml(
              hint
            )}">${kindSelect(p.kind)}</select>
            <span class="kind-help" title="${escapeHtml(hint)}" aria-label="About this alert type">?</span>
            <select name="symbol" class="symbol-select" aria-label="Symbol" ${
              isAiTrader(p.kind) ? "disabled" : ""
            }>${symbolSelect(isAiTrader(p.kind) ? "XAU/USD" : p.symbol)}</select>
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
          ${
            isAiTrader(p.kind)
              ? aiTraderFields(p)
              : `<label class="field">Max concurrent
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
          ${
            isAsiaBox(p.kind)
              ? asiaRiskFields(p)
              : p.kind === "dawn_cross"
              ? dawnRiskFields(p)
              : isCrt(p.kind)
              ? crtRiskFields(p)
              : `<label class="field">SL points
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
          </label>`
          }`
          }
        </div>
        <div class="pref-card-actions">
          <button type="button" class="btn small ghost btn-revert-pref">Revert</button>
          <button type="button" class="btn small primary btn-save-pref" ${
            prefsSaving ? "disabled" : ""
          }>Save</button>
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

    const markEdited = () => {
      collectPrefsFromDom();
      const dirty = cardIsDirty(prefsDraft[i]);
      card.classList.toggle("dirty", dirty);
      refreshDirtyUi();
    };

    card.addEventListener("input", (ev) => {
      if (!ev.target || ev.target.name === "enabled") return;
      markEdited();
    });
    card.addEventListener("change", (ev) => {
      const name = ev.target && ev.target.name;
      if (!name || name === "enabled" || name === "kind" || name === "sl_tp_override") return;
      markEdited();
    });

    card.querySelector('[name="kind"]').addEventListener("change", () => {
      collectPrefsFromDom();
      renderPrefs();
    });

    const overrideEl = card.querySelector('[name="sl_tp_override"]');
    if (overrideEl) {
      overrideEl.addEventListener("change", () => {
        collectPrefsFromDom();
        renderPrefs();
      });
    }

    card.querySelector('[name="enabled"]').addEventListener("change", async () => {
      if (prefsSaving) return;
      collectPrefsFromDom();
      const on = card.querySelector('[name="enabled"]').checked;
      prefsDraft[i].enabled = on;
      syncToggleLabel();
      prefsSaving = true;
      try {
        const current = prefsDraft[i];
        if (!current._originKey || prefKey(current) !== current._originKey) {
          await persistCard(i);
        } else {
          const data = await api(prefUrl(current.kind, current.symbol), {
            method: "PATCH",
            body: JSON.stringify({ enabled: on }),
          });
          const saved = data.pref || { ...baselineByKey().get(current._originKey), enabled: on };
          const kept = prefsDraft[i];
          replaceBaselineRow(current._originKey, saved);
          prefsDraft[i] = { ...kept, enabled: !!saved.enabled };
        }
        toast(on ? "Alert enabled" : "Alert paused — settings kept");
        setPrefsMsg(on ? "Alert on." : "Alert off (settings kept in DB).", "ok");
        prefsSaving = false;
        renderPrefs();
      } catch (e) {
        if (prefsDraft[i]) prefsDraft[i].enabled = !on;
        prefsSaving = false;
        renderPrefs();
        setPrefsMsg(e.message, "err");
        toast(e.message, "err");
      } finally {
        prefsSaving = false;
      }
    });

    card.querySelector(".btn-save-pref").addEventListener("click", () => saveCard(i));

    card.querySelector(".btn-revert-pref").addEventListener("click", () => {
      const current = prefsDraft[i];
      if (!current) return;
      if (!current._originKey) {
        prefsDraft.splice(i, 1);
      } else {
        const base = baselineByKey().get(current._originKey);
        prefsDraft[i] = base
          ? stampPref(base, current._originKey)
          : current;
        if (base) prefsDraft[i]._cid = current._cid;
      }
      renderPrefs();
      setPrefsMsg("Reverted.", "ok");
    });

    card.querySelector(".btn-delete-pref").addEventListener("click", async () => {
      if (prefsSaving) return;
      collectPrefsFromDom();
      const current = prefsDraft[i];
      if (!current) return;
      const label = `${current.kind} / ${symbolLabel(current.symbol)}`;
      if (
        !confirm(
          `Delete alert “${label}” permanently?\n\nThis removes it from the database. Use the On/Off toggle if you only want to pause.`
        )
      ) {
        return;
      }
      const removed = prefsDraft.splice(i, 1)[0];
      renderPrefs();
      if (!removed._originKey) {
        toast(`Deleted ${label}`);
        setPrefsMsg("Alert discarded.", "ok");
        return;
      }
      prefsSaving = true;
      try {
        const [kind, symbol] = splitPrefKey(removed._originKey);
        await api(prefUrl(kind, symbol), { method: "DELETE" });
        prefsBaseline = prefsBaseline.filter((b) => prefKey(b) !== removed._originKey);
        syncBaselineFromDraft();
        toast(`Deleted ${label}`);
        setPrefsMsg("Alert deleted.", "ok");
        prefsSaving = false;
        renderPrefs();
      } catch (e) {
        prefsDraft.splice(i, 0, removed);
        prefsSaving = false;
        renderPrefs();
        setPrefsMsg(e.message, "err");
        toast(e.message, "err");
      } finally {
        prefsSaving = false;
      }
    });
  });
  refreshDirtyUi();
}

async function saveCard(i) {
  if (prefsSaving) return;
  collectPrefsFromDom();
  prefsSaving = true;
  try {
    setPrefsMsg("Saving…");
    await persistCard(i);
    setPrefsMsg("Saved.", "ok");
    toast("Alert saved");
    prefsSaving = false;
    renderPrefs();
  } catch (e) {
    setPrefsMsg(e.message, "err");
    toast(e.message, "err");
    prefsSaving = false;
    refreshDirtyUi();
  } finally {
    prefsSaving = false;
  }
}

async function saveAllDirty() {
  if (prefsSaving) return;
  collectPrefsFromDom();
  const ids = prefsDraft.filter(cardIsDirty).map((p) => p._cid);
  if (!ids.length) return;
  prefsSaving = true;
  try {
    setPrefsMsg("Saving…");
    for (const cid of ids) {
      const i = prefsDraft.findIndex((p) => p._cid === cid);
      if (i < 0 || !cardIsDirty(prefsDraft[i])) continue;
      await persistCard(i);
    }
    setPrefsMsg("Saved.", "ok");
    toast("Alerts saved");
    prefsSaving = false;
    renderPrefs();
  } catch (e) {
    setPrefsMsg(e.message, "err");
    toast(e.message, "err");
    prefsSaving = false;
    renderPrefs();
  } finally {
    prefsSaving = false;
  }
}

function collectPrefsFromDom() {
  $$("#prefs-list .pref-card").forEach((card) => {
    const i = Number(card.dataset.i);
    if (!prefsDraft[i]) return;
    const g = (name) => card.querySelector(`[name="${name}"]`);
    const kind = g("kind").value.trim();
    const asia = isAsiaBox(kind);
    const session = isBreakevenOff(kind);
    const beEl = g("breakeven_half_tp");
    const bePctEl = g("breakeven_pct");
    const overrideEl = g("sl_tp_override");
    const num = (name, fallback) => {
      const el = g(name);
      if (!el || el.value === "") return fallback;
      const value = Number(el.value);
      return Number.isFinite(value) ? value : fallback;
    };
    const prev = prefsDraft[i];
    const maxRrEl = g("max_rr");
    prefsDraft[i] = {
      ...prev,
      kind,
      symbol: (g("symbol").value || prev.symbol || "").trim(),
      enabled: g("enabled").checked,
      max_concurrent: num("max_concurrent", prev.max_concurrent ?? 1),
      max_per_day: num("max_per_day", prev.max_per_day ?? 5),
      volume_lots: num("volume_lots", prev.volume_lots ?? 0.01),
      sl_points: num("sl_points", prev.sl_points ?? 1000),
      tp_points: num("tp_points", prev.tp_points ?? 1000),
      min_volume_lots: num("min_volume_lots", prev.min_volume_lots ?? 0.01),
      max_volume_lots: num("max_volume_lots", prev.max_volume_lots ?? prev.volume_lots ?? 0.01),
      min_rr: num("min_rr", prev.min_rr ?? 1.5),
      max_rr: maxRrEl && maxRrEl.value !== "" ? Number(maxRrEl.value) : "",
      daily_loss_cap_usd: num("daily_loss_cap_usd", prev.daily_loss_cap_usd ?? 50),
      breakeven_half_tp: session ? false : beEl ? beEl.checked : true,
      breakeven_fraction: bePctEl
        ? bePercentToFraction(bePctEl.value)
        : Number(prev.breakeven_fraction) || 0.5,
      sl_tp_override: asia && overrideEl ? overrideEl.checked : false,
      _cid: prev._cid,
      _originKey: prev._originKey,
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
  const row = stampPref(newPref(kind), null);
  prefsDraft.push(row);
  renderPrefs();
  setPrefsMsg("New alert added — edit the fields, then Save on the card.");
  // Scroll to the new card
  const cards = $$("#prefs-list .pref-card");
  const last = cards[cards.length - 1];
  if (last) last.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function loadMe() {
  try {
    state.me = await api("/api/me");
    prefsBaseline = (state.me.prefs || []).map((p) => ({ ...p }));
    prefsDraft = prefsBaseline.map((p) => stampPref(p, prefKey(p)));
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

const ORDERS_POLL_MS = 60000;
let ordersLoadedAt = 0;

function paintOrders() {
  const orders = state.orders || [];
  const summary = orders.length ? ordersSummary(orders) : "";
  $("#orders-table").innerHTML = summary + ordersTable(orders);
  renderHomeOrders();
}

function showAccountLoading() {
  state.orders = [];
  prefsDraft = [];
  prefsBaseline = [];
  if (state.me) state.me.prefs = [];
  const ordersEl = $("#orders-table");
  if (ordersEl) ordersEl.textContent = "Loading…";
  const homeEl = $("#home-orders");
  if (homeEl) homeEl.textContent = "Loading…";
  const prefsEl = $("#prefs-list");
  if (prefsEl) prefsEl.textContent = "Loading…";
  const kv = $("#account-kv");
  if (kv) kv.innerHTML = "<dt>Account</dt><dd>Loading…</dd>";
  refreshDirtyUi();
}

async function loadOrders() {
  if (loadOrders._busy) return;
  loadOrders._busy = true;
  ordersLoadedAt = Date.now();
  try {
    const data = await api("/api/me/orders?limit=50");
    state.orders = data.orders || [];
    if (data.deposit_currency && state.me) {
      state.me.deposit_currency = data.deposit_currency;
      if (state.me.connection && !state.me.connection.deposit_currency) {
        state.me.connection.deposit_currency = data.deposit_currency;
      }
    }
    paintOrders();
  } catch (e) {
    $("#orders-table").innerHTML = `<p class="hint err">${escapeHtml(e.message)}</p>`;
  } finally {
    loadOrders._busy = false;
  }
}

function maybeRefreshOrders() {
  if (document.visibilityState !== "visible") return;
  if (!state.me) return;
  if (Date.now() - ordersLoadedAt < ORDERS_POLL_MS) return;
  loadOrders();
}

function bind() {
  $$(".tab").forEach((b) =>
    b.addEventListener("click", () => setTab(b.dataset.tab))
  );

  async function doLogout() {
    if (!confirmLeavePrefs()) return;
    try {
      await api("/auth/logout", { method: "POST" });
    } catch {
      /* ignore */
    }
    state.me = null;
    prefsDraft = [];
    prefsBaseline = [];
    show("login");
    toast("Logged out");
  }
  $("#btn-logout").addEventListener("click", doLogout);

  const accountSwitch = $("#account-switch");
  if (accountSwitch) {
    accountSwitch.addEventListener("change", async (e) => {
      const id = Number(e.target.value);
      const prev = state.me?.user?.active_account_id;
      if (!Number.isFinite(id) || String(id) === String(prev)) return;
      if (!confirmLeavePrefs()) {
        e.target.value = prev != null ? String(prev) : "";
        return;
      }
      showAccountLoading();
      try {
        await api("/api/me/account", {
          method: "POST",
          body: JSON.stringify({ account_id: id }),
        });
        await loadMe();
        toast("Switched account");
      } catch (err) {
        toast(err.message || "Could not switch account", "err");
        await loadMe();
      }
    });
  }

  $("#btn-add-pref").addEventListener("click", onAddPref);

  const saveAll = $("#btn-save-all-prefs");
  if (saveAll) saveAll.addEventListener("click", () => saveAllDirty());

  $("#btn-refresh-orders").addEventListener("click", () => loadOrders());

  $("#btn-kill").addEventListener("click", async () => {
    const label = accountLabel(state.me?.connection) || "this account";
    if (!confirm(`Kill trading on ${label}?`)) {
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

window.addEventListener("beforeunload", (e) => {
  if (!prefsDraft.some(cardIsDirty)) return;
  e.preventDefault();
  e.returnValue = "";
});

document.addEventListener("visibilitychange", maybeRefreshOrders);
setInterval(maybeRefreshOrders, ORDERS_POLL_MS);

bind();
loadMe();
