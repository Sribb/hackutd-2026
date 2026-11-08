// Live IoT layer (#54): one connection to the backend's live feed, shared by the console,
// the Jarvis panel and the phone page.
//
//   GET /api/live/state   { clock, assets: { ID: { status, level_pct?, running?, position?, stale? } }, connectors? }
//   /ws                   { type: "state" | "alert" | "proposal" | "ui", ... }
//
// Rules: a stale reading is UNKNOWN (never OK, never a stale alarm passed off as current). If the feed drops, every value we hold is
// marked stale and the UI says "live feed lost". Nothing freezes silently.

import { wsUrl, token } from "./auth.js";
let base = "";
let role = "web";
let ws = null, tries = 0, pollTimer = 0, lostTimer = 0;
let heard = false;       // this socket delivered a message
let queryToken = false;  // fall back to /ws?token= (a backend without first-message auth)
let authed = false, authTimer = 0;
let sentAuth = "", rejected = "";
let lastMsgAt = 0, ticks = 0;  // a socket that ticks state every second and then goes quiet is lost
const subs = new Map(); // type -> Set(fn)

export const live = {
  available: false,     // /api/live/state answered at least once
  connected: false,     // /ws open
  lostSince: null,      // Date.now() when the feed was lost, else null
  clock: null,
  t: null,              // live minute from the backend, when it sends one
  assets: {},           // ID -> { status, level_pct, running, position, stale, seenAt }
  connectors: [],       // [{ id, kind, status }]
};

export function on(type, fn) {
  if (!subs.has(type)) subs.set(type, new Set());
  subs.get(type).add(fn);
  return () => subs.get(type).delete(fn);
}
function emit(type, msg) {
  for (const fn of subs.get(type) || []) {
    try { fn(msg); } catch (e) { console.error(e); }
  }
}

export function start(apiBase, as = "web") {
  if (base || !apiBase) return;
  base = apiBase.replace(/\/$/, "");
  role = as;
  poll();
  connect();
}

// ?gauge=freeze:TANK_3 is a labeled drill: that tank's level stops moving after the first
// reading, the way a stuck gauge does, so the frozen-gauge check can be rehearsed.
const FREEZE = (() => { try { const m = /^freeze:([A-Z0-9_]+)$/.exec(new URLSearchParams(location.search).get("gauge") || ""); return m ? m[1] : null; } catch { return null; } })();
export const gaugeDrill = () => FREEZE;
let frozenAt = null;

function mergeState(s) {
  if (!s) return;
  const now = Date.now();
  if (FREEZE && s.assets && s.assets[FREEZE] && typeof s.assets[FREEZE].level_pct === "number") {
    if (frozenAt == null) frozenAt = s.assets[FREEZE].level_pct;
    s = { ...s, assets: { ...s.assets, [FREEZE]: { ...s.assets[FREEZE], level_pct: frozenAt } } };
  }
  if (s.clock) { live.clock = s.clock; live.clockAt = now; }
  if (typeof s.t === "number") live.t = s.t;
  if (typeof s.paused === "boolean") live.paused = s.paused; // POST /api/live/sim {paused}: no ticks, field time stands still
  if (typeof s.speed === "number" && s.speed > 0) live.speed = s.speed; // live minutes (CF-7 consequence_t / shelved_until_t use it)
  for (const [id, a] of Object.entries(s.assets || {})) {
    const prev = live.assets[id] || {};
    const staleNow = a.stale === true || (typeof a.stale === "number" && a.stale > 0);
    // A sensor that reports "stale" stopped reading when we first saw it stale, not now.
    const staleSince = staleNow ? (prev.staleSince || now) : null;
    // The last status that came from a fresh reading, for "last known: ALARM".
    const lastKnown = !staleNow && a.status && a.status !== "UNKNOWN" ? a.status : prev.last_known || null;
    live.assets[id] = { ...prev, ...a, staleSince, last_known: lastKnown, seenAt: staleNow ? (prev.seenAt || now) : now, recvAt: now };
    if (!staleNow) delete live.assets[id].stale;
  }
  if (Array.isArray(s.connectors)) live.connectors = s.connectors;
  live.available = true;
}

async function poll() {
  clearTimeout(pollTimer);
  try {
    const r = await fetch(`${base}/api/live/state`);
    if (r.ok) {
      mergeState(await r.json());
      if (!live.connectors.length) await healthConnectors();
      markLost(false); // the REST poll works even while the socket is down
      emit("state", live);
    } else if (r.status === 404) {
      return; // this backend has no live layer yet; stay quiet and don't mark anything
    }
  } catch {
    // One failed poll while the socket still carries state is a blip, not a lost feed. A socket
    // that says it's open but has delivered nothing for 6 s doesn't count.
    if (!live.connected || Date.now() - lastMsgAt > 6000) markLost(true);
  }
  // While the socket is up it carries state; poll slowly as a safety net, faster when it's down.
  pollTimer = setTimeout(poll, live.connected ? 15000 : 3000);
}

async function healthConnectors() {
  try {
    const h = await (await fetch(`${base}/api/health`)).json();
    if (Array.isArray(h.connectors)) live.connectors = h.connectors;
  } catch { /* badge falls back to "sim" */ }
}

function connect() {
  try {
    // The token goes in the first message, not the URL, so it never lands in a server log
    // (#57). A backend that predates that never answers {type: "auth"}; then this tab falls
    // back to ?token= for the rest of the visit (see onopen).
    const url = base.replace(/^http/, "ws") + `/ws?role=${role}`;
    ws = new WebSocket(queryToken ? wsUrl(url) : url);
    heard = false;
    authed = false;
    ticks = 0;               // the watchdog judges each socket on its own messages
    lastMsgAt = Date.now();
  } catch {
    return retry();
  }
  ws.onopen = () => {
    const t = token();
    if (t && !queryToken && t !== rejected) {
      try { ws.send(JSON.stringify({ type: "auth", token: t })); sentAuth = t; } catch { /* closing */ }
      // No {type: "auth", ok: true} within 1.5 s: this backend ignores first-message auth and
      // treats us as read-only. Reconnect with ?token= for the rest of the visit.
      const me = ws;
      clearTimeout(authTimer);
      authTimer = setTimeout(() => { if (ws === me && !authed) { queryToken = true; try { me.close(); } catch { /* gone */ } } }, 1500);
    }
    live.connected = true;
    ticks = 0;
    lastMsgAt = Date.now();
    markLost(false);
    emit("feed", live);
  };
  ws.onmessage = (m) => {
    // Back-off resets only once the socket delivers: a server that accepts and then closes
    // (a refused ?token=) must not get a 1 Hz loop, each try counted as a wrong token.
    if (!heard) tries = 0;
    heard = true;
    lastMsgAt = Date.now();
    let msg;
    try { msg = JSON.parse(m.data); } catch { return; }
    if (msg.type === "auth") {
      authed = msg.ok !== false;
      if (!authed) queryToken = false; // a wrong token: the URL wouldn't help either
      emit("auth", msg);
    } else if (msg.type === "state") {
      mergeState(msg.state || msg);
      ticks++;
      markLost(false);
      emit("state", live);
    } else {
      emit(msg.type, msg);
    }
  };
  ws.onclose = (ev) => {
    ws = null;
    // A token the server refused (1008 after our auth message): reconnect read-only rather
    // than knock with it forever. A new token (the PIN prompt) is tried again.
    // A lockout ("Too many wrong PINs") refuses even the right token for a while: not a verdict on it.
    if (ev && ev.code === 1008 && sentAuth && !/too many/i.test(ev.reason || "")) rejected = sentAuth;
    else if (!heard && !queryToken && token() && [1008, 4001, 4003, 4401, 4403].includes(ev && ev.code)) queryToken = true;
    sentAuth = "";
    const was = live.connected;
    live.connected = false;
    // A brief blip is normal. After 4 s without the socket, say the feed is lost.
    clearTimeout(lostTimer);
    lostTimer = setTimeout(() => { if (!live.connected) markLost(true); }, was ? 4000 : 0);
    emit("feed", live);
    retry();
  };
}

// Send a message on the shared socket, e.g. {type: "ui", action: "focus", asset_id} to point
// every console at an asset. Returns false if the socket isn't open.
export function send(msg) {
  if (!ws || ws.readyState !== 1) return false;
  ws.send(JSON.stringify(msg));
  return true;
}

// Half-open socket (Wi-Fi drop with no close event): once the socket has shown it sends state
// every second, 6 s of silence means the feed is lost. A ping every 2 s gets a pong back
// (live_api.py), so a paused sim, which sends no state, still counts as heard.
// A dead socket can take a minute to fire onclose, so it is dropped and replaced right away.
setInterval(() => {
  if (!ws || ws.readyState !== 1) return;
  try { ws.send(JSON.stringify({ type: "ping" })); } catch { /* closing */ }
  if (!live.connected || ticks < 3 || Date.now() - lastMsgAt < 6000) return;
  const dead = ws;
  dead.onopen = dead.onmessage = dead.onclose = dead.onerror = null;
  try { dead.close(); } catch { /* gone */ }
  ws = null;
  ticks = 0;
  sentAuth = "";
  live.connected = false;
  markLost(true);
  emit("feed", live);
  retry();
}, 2000);

function retry() {
  setTimeout(connect, Math.min(15000, 1000 * 2 ** tries++));
}

function markLost(lost) {
  const before = live.lostSince;
  if (lost && !live.lostSince) live.lostSince = Date.now();
  if (!lost) live.lostSince = null;
  if (before !== live.lostSince) emit("feed", live);
}

// Live minutes since a wall-clock moment. speed is live minutes per real second
// (FREEBOARD_SPEED); without one the field runs in real time.
// A paused sim (we are connected and it says paused) stands still; a lost feed always runs on.
const liveSince = (ms) => (live.paused && live.connected && !live.lostSince ? 0 : ((Date.now() - ms) / 1000) * (live.speed || 1 / 60));

// The backend's age_min as of now: its value when the message came, plus the live time since.
// While the feed is lost this extrapolates at the last known speed, so a 12 min old reading
// never turns into "just now" when the connection drops.
function ageNow(a) {
  return typeof a.age_min === "number" ? a.age_min + liveSince(a.recvAt || Date.now()) : null;
}

// The live clock and minute as of now, extrapolated from the last message when the feed is
// lost (the field keeps running without us). Null with no live layer.
export function clockEstimate() {
  if (!live.available || !live.clock || !/^\d{1,2}:\d{2}/.test(live.clock)) return null;
  const extra = live.lostSince && live.clockAt ? liveSince(live.clockAt) : 0;
  const [h, m] = live.clock.split(":").map(Number);
  const t = typeof live.t === "number" ? live.t + extra : null;
  const frac = typeof live.t === "number" ? live.t - Math.floor(live.t) : 0;
  const x = Math.floor(h * 60 + m + frac + extra);
  const hh = Math.floor(((x % 1440) + 1440) % 1440 / 60), mm = ((x % 60) + 60) % 60;
  return { clock: `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`, t, estimated: extra > 0 };
}

// How long this asset has gone without a good reading, in whole minutes. Null if we never had one.
export function staleMinutes(id) {
  const a = live.assets[id];
  if (!a) return null;
  // #38 sends age_min (live minutes since the newest reading), counted on since it came.
  const age = ageNow(a);
  if (age != null) return Math.round(age);
  if (typeof a.stale === "number") return Math.round(a.stale);
  const since = Math.min(a.staleSince || Infinity, live.lostSince || Infinity, a.staleSince || live.lostSince ? Infinity : a.seenAt);
  return Math.max(0, Math.round((Date.now() - since) / 60000));
}

// The live reading for an asset, with the rules applied, or null if there is none.
export function reading(id) {
  const a = live.assets[id];
  if (!a) return null;
  const stale = a.stale === true || (typeof a.stale === "number" && a.stale > 0) || !!live.lostSince;
  const out = { ...a, stale };
  // A stale reading is never shown as OK or WARNING: it becomes UNKNOWN, with the last
  // status kept so the panel can say "last known ...". The one exception is ALARM, which
  // stays ALARM (CONTRACT.md: an alarm is never hidden by a lost signal) until a fresh
  // reading clears it.
  if (stale) {
    const last = a.status && a.status !== "UNKNOWN" ? a.status : a.last_known;
    out.last_status = last;
    if (last === "ALARM") {
      const age = staleMinutes(id);
      out.status = "ALARM";
      out.reason = `${a.reason ? `${a.reason} ` : ""}No fresh reading${age != null ? ` for ${age < 1 ? "under a minute" : `${age} min`}` : ""}, so it stays in ALARM until a reading clears it.`;
    } else out.status = "UNKNOWN";
  }
  return out;
}

// CF-6: how old this asset's reading is, as "12 s ago" or "4 min ago", on the field clock.
// From the backend's age_min when it sends one, counted on between messages. Null without
// a live layer (the panel then says where the number came from instead).
export function ageText(id) {
  const a = live.assets[id];
  if (!a || !live.available) return null;
  const min = ageNow(a) ?? staleMinutes(id);
  if (min == null) return null;
  if (min < 1) { const sec = Math.round((min * 60) / 5) * 5; return sec < 5 ? "just now" : `${sec} s ago`; }
  return min < 90 ? `${Math.round(min)} min ago` : `${Math.round(min / 60)} h ago`;
}

export function connectorBadge() {
  const list = live.connectors.length ? live.connectors : [{ kind: "sim", status: live.available ? "connected" : "unavailable" }];
  return list.map((c) => ({ kind: String(c.kind || c.id || "sim").toLowerCase(), ok: !/unavail|down|error|fail|lost/i.test(String(c.status || "connected")) }));
}
