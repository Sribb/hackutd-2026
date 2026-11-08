// Live IoT layer (#54): one connection to the backend's live feed, shared by the console,
// the Jarvis panel and the phone page.
//
//   GET /api/live/state   { clock, assets: { ID: { status, level_pct?, running?, position?, stale? } }, connectors? }
//   /ws                   { type: "state" | "alert" | "proposal" | "ui", ... }
//
// Rules: a stale reading is UNKNOWN (never OK, never a stale alarm passed off as current). If the feed drops, every value we hold is
// marked stale and the UI says "live feed lost". Nothing freezes silently.

let base = "";
let ws = null, tries = 0, pollTimer = 0, lostTimer = 0;
const subs = new Map(); // type -> Set(fn)

export const live = {
  available: false,     // /api/live/state answered at least once
  connected: false,     // /ws open
  lostSince: null,      // Date.now() when the feed was lost, else null
  clock: null,
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

export function start(apiBase) {
  if (base || !apiBase) return;
  base = apiBase.replace(/\/$/, "");
  poll();
  connect();
}

function mergeState(s) {
  if (!s) return;
  const now = Date.now();
  if (s.clock) live.clock = s.clock;
  for (const [id, a] of Object.entries(s.assets || {})) {
    const prev = live.assets[id] || {};
    const staleNow = a.stale === true || (typeof a.stale === "number" && a.stale > 0);
    // A sensor that reports "stale" stopped reading when we first saw it stale, not now.
    const staleSince = staleNow ? (prev.staleSince || now) : null;
    live.assets[id] = { ...prev, ...a, staleSince, seenAt: staleNow ? (prev.seenAt || now) : now };
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
      if (!live.connected) markLost(false); // the REST poll works even while the socket is down
      emit("state", live);
    } else if (r.status === 404) {
      return; // this backend has no live layer yet; stay quiet and don't mark anything
    }
  } catch {
    markLost(true);
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
    ws = new WebSocket(base.replace(/^http/, "ws") + "/ws");
  } catch {
    return retry();
  }
  ws.onopen = () => {
    tries = 0;
    live.connected = true;
    markLost(false);
    emit("feed", live);
  };
  ws.onmessage = (m) => {
    let msg;
    try { msg = JSON.parse(m.data); } catch { return; }
    if (msg.type === "state") {
      mergeState(msg.state || msg);
      emit("state", live);
    } else {
      emit(msg.type, msg);
    }
  };
  ws.onclose = () => {
    ws = null;
    const was = live.connected;
    live.connected = false;
    // A brief blip is normal. After 4 s without the socket, say the feed is lost.
    clearTimeout(lostTimer);
    lostTimer = setTimeout(() => { if (!live.connected) markLost(true); }, was ? 4000 : 0);
    emit("feed", live);
    retry();
  };
}

function retry() {
  setTimeout(connect, Math.min(15000, 1000 * 2 ** tries++));
}

function markLost(lost) {
  const before = live.lostSince;
  if (lost && !live.lostSince) live.lostSince = Date.now();
  if (!lost) live.lostSince = null;
  if (before !== live.lostSince) emit("feed", live);
}

// How long this asset has gone without a good reading, in whole minutes. Null if we never had one.
export function staleMinutes(id) {
  const a = live.assets[id];
  if (!a) return null;
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
  // A stale reading is UNKNOWN, whatever it last said. The last status is kept so the
  // panel can say "last known ALARM", but it is never shown as the current state.
  if (stale) { out.last_status = a.status || null; out.status = "UNKNOWN"; }
  return out;
}

export function connectorBadge() {
  const list = live.connectors.length ? live.connectors : [{ kind: "sim", status: live.available ? "connected" : "unavailable" }];
  return list.map((c) => ({ kind: String(c.kind || c.id || "sim").toLowerCase(), ok: !/unavail|down|error|fail|lost/i.test(String(c.status || "connected")) }));
}
