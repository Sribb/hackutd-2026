// Alarm list (CF-1) with the alarm-rate meter (CF-2).
//
// Fed by GET /api/alerts and the /ws "alert" events (CONTRACT.md "Alerts", CF-7 fields:
// priority, root_id, minutes_to_consequence(_low), consequence, consequence_clock,
// consequence_t, usd_at_risk, shelved_until(_t), shelve_reason).
//
// - Rows are roots only. A consequential alarm (root_id set) folds under its root as
//   "+N related". Sorted by priority, then by the least time to consequence: the alarm
//   with the least time left is always on top, because the priority comes from the live
//   forecast, not from a table an engineer filled in.
// - Ack is one click. Shelve needs a reason and a duration (15 min to 4 h), shows the time
//   left, and the alarm comes back when the timer ends (the backend sends "unshelved").
// - Every ack and shelve writes a shift log line with the clock time.
// - No alarms is never "all clear" while a tank has no reading.
// - CF-2: "Alarms in the last 10 min: N". Over 10 (EEMUA 191 flood) it says "Alarm flood"
//   and the related alarms stay folded even when a row is opened.
// - Without /api/alerts (no backend, ?offline, or an older backend) the list is derived from
//   the incident and the baseline forecast, marked "derived", and ack/shelve stay on this
//   screen only, which the panel says.
//
// Other modules (the Jarvis console's Watch agent) can read the same list:
//   import { alarms } from "./alarms.js"; alarms.roots(), alarms.on(fn)

import { authFetch } from "./auth.js";
import * as liveFeed from "./live.js";
import { isHolding, afterHold } from "./fx.js";

const RANK = { P1: 0, P2: 1, P3: 2 };
const FLOOD = 10;          // EEMUA 191: more than 10 new alarms in 10 minutes
const FLOOD_WINDOW = 10 * 60 * 1000;
const SHELVE_MIN = [15, 30, 60, 120, 240];

const all = new Map();      // id -> alert
const seenAt = new Map();   // id -> Date.now() when this screen first saw it raised (CF-2)
const subs = new Set();
let base = "";
let derived = false;        // true: no alerts API, the list comes from the forecast
let hooks = null;
let box = null;
let openRow = null;         // root id whose related alarms are shown
let shelving = null;        // alert id with the shelve form open
let formMsg = "";
let loadSeq = 0;            // GET /api/alerts in flight; socket updates stamp the seq they arrived under
const touched = new Map();  // id -> loadSeq when the socket last changed it
let loadedOnce = false;     // the alerts feed answered at least once
let loadError = "";         // why the last load failed, while it keeps failing

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const label = (m) => (m < 60 ? `${m} min` : m % 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m / 60} h`);

// ------------------------------------------------------------------ model ----
function nowT() {
  return typeof liveFeed.live.t === "number" ? liveFeed.live.t : null;
}

const isShelved = (a) => a.shelved_until_t != null || a.shelved_until != null;

// Minutes until this alarm's consequence, counting down with the live clock when we can.
function minutesLeft(a) {
  if (a.derived_at && typeof a.minutes_to_consequence === "number") {
    return Math.max(0, Math.round(a.minutes_to_consequence - (Date.now() - a.derived_at) / 60000));
  }
  const t = nowT();
  if (t != null && typeof a.consequence_t === "number") return Math.max(0, Math.round(a.consequence_t - t));
  return typeof a.minutes_to_consequence === "number" ? a.minutes_to_consequence : null;
}
function shelfLeft(a) {
  if (a.shelved_local_until) return Math.max(0, Math.ceil((a.shelved_local_until - Date.now()) / 60000));
  const t = nowT();
  if (t != null && typeof a.shelved_until_t === "number") return Math.max(0, Math.ceil(a.shelved_until_t - t));
  return null;
}

// A shelf that ends after the shutdown it hides: the panel says so in plain words.
function shelfOutlasts(a) {
  return typeof a.consequence_t === "number" && typeof a.shelved_until_t === "number" && a.consequence_t < a.shelved_until_t && !!a.consequence_clock;
}

function order(a, b) {
  const pa = RANK[a.priority] ?? 3, pb = RANK[b.priority] ?? 3;
  if (pa !== pb) return pa - pb;
  const ma = a.minutes_to_consequence_low ?? minutesLeft(a), mb = b.minutes_to_consequence_low ?? minutesLeft(b);
  if ((ma == null) !== (mb == null)) return ma == null ? 1 : -1;
  if (ma != null && ma !== mb) return ma - mb;
  if (a.severity !== b.severity) return a.severity === "ALARM" ? -1 : 1;
  return String(b.t || "").localeCompare(String(a.t || ""));
}

// The root an alarm folds under: follow root_id while that root is still in the list.
function rootOf(a) {
  let r = a, hops = 0;
  while (r.root_id && all.has(r.root_id) && hops++ < 5) r = all.get(r.root_id);
  return r;
}

// Active alarms grouped under their root. Each group: { root, related: [], shelved }.
function groups() {
  const active = [...all.values()].filter((a) => a.active !== false);
  const by = new Map();
  for (const a of active) {
    const r = rootOf(a);
    // A root that already cleared still heads its group (its consequence is what matters),
    // but the group is shown under the first active alarm in it.
    const key = r.id;
    if (!by.has(key)) by.set(key, { root: r.active === false ? null : r, related: [] });
    const g = by.get(key);
    if (a === r) g.root = a; else g.related.push(a);
  }
  const out = [];
  for (const g of by.values()) {
    g.related.sort(order);
    if (!g.root) g.root = g.related.shift();
    // A group sorts by its most urgent member: a high-high under a P2 root still pulls it up.
    g.lead = [g.root, ...g.related].sort(order)[0];
    g.shelved = isShelved(g.root);
    // Shelving a root never hides what follows from it: related alarms that are not shelved
    // themselves stay on the open list, headed by the most urgent of them.
    if (g.shelved) {
      const loose = g.related.filter((a) => !isShelved(a));
      if (loose.length) {
        g.related = g.related.filter((a) => isShelved(a));
        const [head, ...rest] = loose.sort(order);
        out.push({ root: head, related: rest, lead: head, shelved: false, under: g.root.id });
      }
    }
    out.push(g);
  }
  return out.sort((x, y) => (x.shelved - y.shelved) || order(x.lead, y.lead));
}

function recent() {
  const since = Date.now() - FLOOD_WINDOW;
  let n = 0;
  for (const [id, at] of seenAt) if (at >= since && all.has(id)) n++;
  return n;
}

function put(a, fresh = false) {
  if (!a || !a.id) return;
  const prev = all.get(a.id);
  all.set(a.id, prev ? { ...prev, ...a } : a);
  if (!seenAt.has(a.id) && a.active !== false) {
    // Alarms already raised before this screen opened count by their own time when it is
    // a real timestamp, so a reload in a flood still shows the flood.
    const t = Date.parse(a.t);
    seenAt.set(a.id, fresh || !Number.isFinite(t) ? Date.now() : Math.min(Date.now(), t));
  }
}

// ---------------------------------------------------------------- backend ----
async function post(path, body) {
  const r = await authFetch(`${base}${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const out = await r.json().catch(() => ({}));
  if (r.status === 401) throw Object.assign(new Error("Not sent: control is locked without the operator PIN."), { status: 401 });
  if (r.status === 403) throw Object.assign(new Error("This device can't do that. Nothing was sent."), { status: 403 });
  if (!r.ok) {
    const d = out && out.detail;
    const text = typeof d === "string" ? d : Array.isArray(d) && d[0] && d[0].msg ? String(d[0].msg).replace(/^Value error, /, "") : `HTTP ${r.status}`;
    throw Object.assign(new Error(text), { status: r.status });
  }
  return out;
}

async function load() {
  if (!base) return useDerived();
  const seq = ++loadSeq;
  try {
    const r = await fetch(`${base}/api/alerts`);
    if (r.status === 404) return useDerived();
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    derived = false;
    // The snapshot is as old as the request. A row the socket changed after the request went
    // out is newer than the snapshot: keep the socket's version (a new P1 raised, or a
    // resolved alarm, must not be undone by a slow GET).
    const fresher = (id) => (touched.get(id) || 0) >= seq;
    const ids = new Set();
    for (const a of j.alerts || []) { ids.add(a.id); if (!fresher(a.id)) put(a); }
    for (const id of [...all.keys()]) if (!ids.has(id) && !fresher(id)) all.delete(id); // gone, or a derived row
    loadedOnce = true;
    loadError = "";
  } catch (e) {
    // Backend down: keep what we had, and say the list may be out of date (or never loaded).
    loadError = e.message || "no answer";
  }
  changed();
}

// No alerts API: one root alarm per failed asset in the incident, with the consequence and
// priority from the baseline forecast (P1 under 60 min, P2 under 180), per CONTRACT.md.
function useDerived() {
  derived = true;
  const h = hooks;
  if (!h) return;
  const inc = h.incident() || {};
  const ev = h.firstShutdown();
  const root = inc.root_asset_id || inc.asset_id;
  const failed = root ? [root] : [];
  const keep = new Set();
  failed.forEach((id) => {
    const key = `derived_${id}`;
    keep.add(key);
    const prev = all.get(key) || {};
    const m = ev ? Math.max(0, Math.round(ev.t_min)) : null;
    // The countdown runs from when this number was computed. A refreshed forecast already
    // counts the time gone by, so restart the count whenever the number changes.
    const at = prev.derived_at && prev.minutes_to_consequence === m ? prev.derived_at : Date.now();
    // Rank on the early end, as the backend does: inflow 15% high reaches shutdown sooner.
    const low = m == null ? null : Math.round(m / 1.15);
    put({
      ...prev,
      id: key, asset_id: id, kind: "PUMP_TRIP", severity: "ALARM", active: true,
      message: inc.summary || `${h.nice(id)} failed.`,
      clock: h.startClock(), root_id: null,
      priority: low == null ? "P3" : low <= 60 ? "P1" : low <= 180 ? "P2" : "P3",
      priority_source: "derived",
      minutes_to_consequence: m,
      minutes_to_consequence_low: low,
      derived_at: at,
      consequence: ev ? `${h.nice(ev.asset_id)} reaches shutdown at ${h.clockAt(ev.t_min)}.` : null,
      consequence_asset_id: ev ? ev.asset_id : null,
      consequence_clock: ev ? h.clockAt(ev.t_min) : null,
    });
  });
  for (const id of [...all.keys()]) if (!keep.has(id)) all.delete(id);
  changed();
}

// ---------------------------------------------------------------- actions ----
async function ack(id) {
  const a = all.get(id);
  if (!a || a.acked) return;
  try {
    const out = derived ? { acked: true } : await post(`/api/alerts/${encodeURIComponent(id)}/ack`, { by: "console" });
    put({ ...a, acked: true, ...(out && out.id ? out : {}) });
    hooks.log(`Acked ${a.priority ? `${a.priority} ` : ""}${hooks.nice(a.asset_id)}: ${a.message}${derived ? " (this screen only)" : ""}`);
  } catch (e) {
    formMsg = e.status === 409 ? "That alarm already cleared." : e.message;
  }
  changed();
}

async function shelve(id, minutes, reason) {
  const a = all.get(id);
  if (!a) return;
  reason = String(reason || "").trim();
  if (!reason) { formMsg = "A shelve needs a reason. Nothing was shelved."; return changed(); }
  if (!(minutes >= 15 && minutes <= 240)) { formMsg = "Shelve for 15 min to 4 h."; return changed(); }
  try {
    let out;
    if (derived) {
      const t = nowT();
      out = { ...a, shelve_reason: reason, shelved_until: hooks.clockIn(minutes), shelved_until_t: t != null ? t + minutes : null, shelved_local_until: Date.now() + minutes * 60000 };
    } else {
      out = await post(`/api/alerts/${encodeURIComponent(id)}/shelve`, { minutes, reason, by: "console" });
    }
    put(out);
    shelving = null;
    formMsg = "";
    hooks.log(`Shelved ${hooks.nice(a.asset_id)} for ${label(minutes)}${out.shelved_until ? `, back at ${out.shelved_until}` : ""}. Reason: ${reason}${derived ? " (this screen only)" : ""}`);
  } catch (e) {
    formMsg = e.status === 409 ? "That alarm already cleared, so there is nothing to shelve." : e.message;
  }
  changed();
}

async function unshelve(id) {
  const a = all.get(id);
  if (!a) return;
  try {
    const out = derived ? { ...a } : await post(`/api/alerts/${encodeURIComponent(id)}/unshelve`, { by: "console" });
    put({ ...out, shelved_until: null, shelved_until_t: null, shelve_reason: null, shelved_local_until: null });
    hooks.log(`Unshelved ${hooks.nice(a.asset_id)} by hand.`);
  } catch (e) {
    formMsg = e.message;
  }
  changed();
}

// --------------------------------------------------------------------- UI ----
function chip(a) {
  const p = a.priority || (a.severity === "ALARM" ? "ALARM" : "WARNING");
  return `<span class="prio ${esc(p)}" title="${a.priority_source === "derived" ? "Priority from the forecast on this screen" : "Priority from the live forecast"}">${esc(p)}</span>`;
}

function consequenceLine(a) {
  if (a.kind === "SENSOR_STALE" && a.consequence) return `<div class="al-cons unknown">${esc(a.consequence)}</div>`;
  if (!a.consequence_clock && !a.consequence) return "";
  const m = minutesLeft(a);
  const who = a.consequence_asset_id ? hooks.nice(a.consequence_asset_id) : "";
  const text = a.consequence_clock && who
    ? `${who} shuts in at ${a.consequence_clock}${m != null ? (m > 0 ? ` (${label(m)})` : " (now)") : ""}`
    : a.consequence;
  const usd = a.usd_at_risk ? ` · ${hooks.usd(a.usd_at_risk)} at risk` : "";
  return `<div class="al-cons">${esc(text)}<span class="muted">${esc(usd)}</span></div>`;
}

function row(a, extra = "") {
  const sh = isShelved(a);
  return `<li class="al ${esc(a.severity || "")} ${a.acked ? "acked" : ""} ${sh ? "shelved" : ""}" data-id="${esc(a.id)}">
    <div class="al-head">${chip(a)}<button class="link al-asset" data-show="${esc(a.asset_id || "")}">${esc(hooks.nice(a.asset_id || ""))}</button>
      <time>${esc(a.clock || "")}</time></div>
    <div class="al-msg">${esc(a.message || "")}</div>
    ${consequenceLine(a)}
    ${sh ? `<div class="al-shelf">Shelved${shelfLeft(a) != null ? `, back in ${label(shelfLeft(a))}` : ""}${a.shelved_until ? ` (${esc(a.shelved_until)})` : ""}. ${esc(a.shelve_reason || "")}</div>` : ""}
    ${sh && shelfOutlasts(a) ? `<div class="al-cons unknown">The shutdown at ${esc(a.consequence_clock)} comes before this shelf ends.</div>` : ""}
    ${extra}
  </li>`;
}

function buttons(a) {
  if (isShelved(a)) return `<div class="al-btns"><button data-unshelve="${esc(a.id)}">Unshelve</button></div>`;
  return `<div class="al-btns">${a.acked ? `<span class="muted al-acked">Acked${a.acked_by ? ` by ${esc(a.acked_by)}` : ""}</span>` : `<button data-ack="${esc(a.id)}">Ack</button>`}
    <button data-shelve="${esc(a.id)}">Shelve</button></div>`;
}

function shelveForm(a) {
  return `<form class="al-form" data-form="${esc(a.id)}">
    <label>Why<input name="reason" maxlength="200" placeholder="Crew is on the way to ${esc(hooks.nice(a.asset_id || ""))}" autocomplete="off"></label>
    <label>For<select name="minutes">${SHELVE_MIN.map((m) => `<option value="${m}" ${m === 30 ? "selected" : ""}>${label(m)}</option>`).join("")}</select></label>
    <div class="al-btns"><button type="button" data-cancel>Cancel</button><button type="submit" class="primary">Shelve</button></div>
  </form>`;
}

function draw() {
  if (!box) return;
  if (isHolding()) { afterHold(draw); return; }
  // Keep a half-typed reason across redraws (a countdown tick redraws every few seconds).
  // Only for the same alarm: Pump 3's reason must never prefill Tank 5's shelve form.
  const oldForm = box.querySelector(".al-form");
  const same = oldForm && oldForm.dataset.form === shelving;
  const typing = same ? oldForm.querySelector("input[name=reason]") : null;
  const keepReason = typing ? typing.value : "";
  const keepMin = same ? oldForm.querySelector("select")?.value : undefined;
  const hadFocus = typing && document.activeElement === typing;

  const gs = groups();
  const live = gs.filter((g) => !g.shelved), shelf = gs.filter((g) => g.shelved);
  const n = recent();
  const flood = n > FLOOD;
  const blind = hooks.blindTanks();
  const down = !derived && base && liveFeed.live.lostSince;

  const rows = live.map((g) => {
    const open = !flood && openRow === g.root.id;
    const rel = g.related.length
      ? `<button class="link al-rel" data-open="${esc(g.root.id)}" ${flood ? "disabled" : ""}>+${g.related.length} related${flood ? "" : open ? " ▾" : " ▸"}</button>`
        + (open ? `<ul class="al-sub">${g.related.map((a) => row(a)).join("")}</ul>` : "")
      : "";
    const why = g.under ? `<div class="al-shelf">Its root cause, ${esc(hooks.nice((all.get(g.under) || {}).asset_id || ""))}, is shelved. This one is not.</div>` : "";
    return row(g.root, why + rel + buttons(g.root) + (shelving === g.root.id ? shelveForm(g.root) : ""));
  }).join("");

  let empty = "";
  if (!live.length) {
    empty = blind.length
      ? `<li class="al-empty unknown">No alarms, but ${blind.length === 1 ? "1 tank has" : `${blind.length} tanks have`} no reading (${esc(blind.map(hooks.nice).join(", "))}). Not all clear.</li>`
      : shelf.length ? `<li class="al-empty">Nothing outside the shelf. ${shelf.length} shelved below.</li>`
      : base && !derived && !loadedOnce ? `<li class="al-empty unknown">Alarm list not loaded${loadError ? ` (${esc(loadError)})` : ""}. Not all clear.</li>`
      : `<li class="al-empty">No active alarms.</li>`;
  }

  box.innerHTML = `<div class="kicker">Alarms <span class="al-count">${[live.length ? `${live.length} active` : "", shelf.length ? `${shelf.length} shelved` : ""].filter(Boolean).join(" · ")}</span></div>
    <div class="al-meter ${flood ? "flood" : ""}">${flood ? "Alarm flood: " : ""}alarms in the last 10 min: ${n}${flood ? ". Showing root causes only." : ""}</div>
    ${derived ? `<div class="al-note">No alerts feed from the backend. Derived from the forecast; ack and shelve stay on this screen.</div>` : ""}
    ${down ? `<div class="al-note warn">Live feed lost. These may be out of date.</div>` : loadedOnce && loadError ? `<div class="al-note warn">Alarm list not refreshed (${esc(loadError)}). These may be out of date.</div>` : ""}
    ${formMsg ? `<div class="al-note warn" role="alert">${esc(formMsg)}</div>` : ""}
    <ol class="al-list">${rows}${empty}</ol>
    ${shelf.length ? `<div class="kicker al-shelf-k">Shelved ${shelf.length}</div><ol class="al-list">${shelf.map((g) => row(g.root, buttons(g.root))).join("")}</ol>` : ""}`;

  box.querySelectorAll("[data-ack]").forEach((b) => { b.onclick = () => ack(b.dataset.ack); });
  box.querySelectorAll("[data-unshelve]").forEach((b) => { b.onclick = () => unshelve(b.dataset.unshelve); });
  box.querySelectorAll("[data-shelve]").forEach((b) => { b.onclick = () => { shelving = shelving === b.dataset.shelve ? null : b.dataset.shelve; formMsg = ""; draw(); box.querySelector(".al-form input")?.focus(); }; });
  box.querySelectorAll("[data-open]").forEach((b) => { b.onclick = () => { openRow = openRow === b.dataset.open ? null : b.dataset.open; draw(); }; });
  box.querySelectorAll("[data-show]").forEach((b) => { b.onclick = () => b.dataset.show && hooks.select(b.dataset.show); });
  const form = box.querySelector(".al-form");
  if (form) {
    if (keepReason) form.reason.value = keepReason;
    if (keepMin) form.minutes.value = keepMin;
    if (hadFocus) form.reason.focus();
    form.onsubmit = (e) => { e.preventDefault(); shelve(form.dataset.form, Number(form.minutes.value), form.reason.value); };
    form.querySelector("[data-cancel]").onclick = () => { shelving = null; formMsg = ""; draw(); };
  }
}

function changed() {
  draw();
  for (const fn of subs) { try { fn(api); } catch (e) { console.error(e); } }
}

// A derived shelf runs on this screen's clock. The backend's comes back as "unshelved".
function tick() {
  if (derived) {
    for (const a of all.values()) {
      if (a.shelved_local_until && Date.now() >= a.shelved_local_until) {
        put({ ...a, shelved_until: null, shelved_until_t: null, shelve_reason: null, shelved_local_until: null });
        hooks.log(`${hooks.nice(a.asset_id)} is back from the shelf.`);
      }
    }
  }
  // Don't redraw under a typing operator: the countdown can wait for the next tick.
  if (!box || !box.contains(document.activeElement) || document.activeElement.tagName === "BUTTON") draw();
}

// ------------------------------------------------------------------ mount ----
export function mountAlarms(el, h) {
  box = el;
  hooks = h;
  base = h.base || "";
  if (!box) return api;
  box.hidden = false;
  load();
  if (base) {
    liveFeed.on("alert", (msg) => {
      const a = msg && msg.alert;
      if (!a) return;
      if (derived) { load(); return; }  // the backend has alerts after all
      const was = all.get(a.id);
      put(a, !was);
      touched.set(a.id, loadSeq);
      if (msg.event === "unshelved" && was && isShelved(was)) hooks.log(`${hooks.nice(a.asset_id)} is back from the shelf: ${a.message}`);
      if (msg.event === "resolved" && openRow === a.id) openRow = null;
      changed();
    });
    // Coming back from a lost feed, re-read: alerts raised meanwhile never reached the socket.
    let wasLost = false;
    liveFeed.on("feed", (L) => { if (wasLost && !L.lostSince) load(); wasLost = !!L.lostSince; draw(); });
    setInterval(load, 30000);
  }
  setInterval(tick, 5000);
  return api;
}

// Read-only view for other modules (the Jarvis console's Watch agent, the handoff).
const api = {
  roots: () => groups().map((g) => ({ ...g.root, related_count: g.related.length, shelved: g.shelved })),
  all: () => [...all.values()],
  recent,
  flood: () => recent() > FLOOD,
  derived: () => derived,
  minutesLeft,
  shelfLeft,
  on: (fn) => { subs.add(fn); return () => subs.delete(fn); },
  refresh: () => (derived ? useDerived() : load()),
};
export const alarms = api;
