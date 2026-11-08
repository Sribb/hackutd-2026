// Refused requests (#126 security log): wrong tokens, writes from another site, a token
// without the right role, PIN lockouts, rejected uploads, and /ws connections refused.
//
// Why on the console: every command to equipment needs the operator token. A run of wrong
// tokens on a write route, or a write from another site, is someone trying to move a valve
// who shouldn't. The night operator should see that, and the next crew should read it in
// the handoff. It is not a field alarm: it never pages, and it never blocks anything.
//
//   GET /api/security/events   {"events": [{id, kind, method, path, client, first_t, last_t, count, detail}]}
//   /ws {"type": "security", "event": {...}}
//
// The backend masks the client address and never records tokens, PINs or query strings.
// Rules:
//   - Only a good GET says what the log holds. Each one replaces what we had (the backend's
//     log is in memory: a restart starts its ids at 1 again). /ws events add to it in between
//     but never make an unread log look read.
//   - A backend without the log (404) hides the card: "Not on this backend". A failed read
//     says "Not checked", and after a good one, "Not checked since HH:MM", never "none".
//   - Times are the server's. The browser's clock may be off, so ages are measured against
//     the server's Date header when the browser can read it, and never against a clock that
//     is behind the newest event.

import * as liveFeed from "./live.js";

const POLL_MS = 30000;
const RECENT_MIN = 60;      // the card shows the last hour
const BURST = 3;            // this many refused writes in 10 min is worth a look
const REARM_MIN = 10;       // a new burst line only after 10 min without one
// Refusals that mean "not allowed to do that". Uploads too big and rate limits are not.
const AUTH_KINDS = new Set(["auth_failed", "forbidden_role", "cross_site"]);

let base = "";
let box = null;
let status = "idle";        // idle | ok | stale (read before, failing now) | absent (404) | error | offline
let lastOkAt = 0, lastOkClock = ""; // when the log last answered: wall time, and the shift clock then
let clockFn = null;
let skew = 0;               // server clock minus this browser's, in ms
let dateSeen = false;       // skew came from the server's Date header
let events = new Map();     // `${id}@${first_t}` -> event (the larger count wins until the next GET)
let open = false;
let logFn = null, lastBurstAt = 0, burstOn = false;

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const ms = (iso) => { const t = Date.parse(iso || ""); return Number.isFinite(t) ? t : null; };
const serverNow = () => Date.now() + skew;
const minsAgo = (iso) => { const t = ms(iso); return t == null ? null : Math.max(0, Math.round((serverNow() - t) / 60000)); };
const evKey = (e) => `${e.id}@${e.first_t || ""}`;
const hhmm = (t) => new Date(t).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

const WORD = {
  auth_failed: "wrong or missing token",
  cross_site: "write from another site",
  forbidden_role: "token without that right",
  lockout: "locked out (too many tries)",
  upload_rejected: "upload refused",
  ws_auth_failed: "live feed refused (wrong token)",
};
export const kindWord = (k) => WORD[k] || String(k || "refused").replace(/_/g, " ");

// A write: every read on this backend is an open GET, so anything else under /api/ changes
// something (actions, plans, blueprints, acks, faults, readings, Jarvis).
export function isWrite(e) {
  if (e.kind === "cross_site") return true;
  const m = String(e.method || "").toUpperCase();
  return !!m && m !== "GET" && m !== "HEAD" && /^\/api\//.test(e.path || "");
}
// Counts toward a burst: a refused write for lack of the right token or site. Not an upload
// that was too big, not a rate limit, not a /ws reconnect with an old token.
export const isAttempt = (e) => AUTH_KINDS.has(e.kind) && isWrite(e);

// What the events add up to, at the server's time `now`.
export function assess(list, now = serverNow()) {
  const within = (e, min) => { const t = ms(e.last_t || e.first_t); return t != null && now - t <= min * 60000; };
  const recent = list.filter((e) => within(e, RECENT_MIN)).sort((a, b) => (ms(b.last_t) || 0) - (ms(a.last_t) || 0));
  const writes10 = list.filter((e) => within(e, 10) && isAttempt(e)).reduce((n, e) => n + (e.count || 1), 0);
  const total = recent.reduce((n, e) => n + (e.count || 1), 0);
  return { recent, total, burst: writes10 >= BURST, writes10 };
}

function add(e) {
  if (!e || e.id == null) return;
  const k = evKey(e), had = events.get(k);
  if (!had || (e.count || 1) >= (had.count || 1)) events.set(k, e);
  // Bounded: the backend keeps 500; keep the 300 most recent by last_t.
  if (events.size > 300) {
    const old = [...events.entries()].sort((a, b) => (ms(a[1].last_t) || 0) - (ms(b[1].last_t) || 0)).slice(0, events.size - 300);
    for (const [k2] of old) events.delete(k2);
  }
}
function newest() { return [...events.values()].reduce((t, e) => Math.max(t, ms(e.last_t) || 0), 0); }

async function load() {
  try {
    const signal = typeof AbortSignal !== "undefined" && AbortSignal.timeout ? AbortSignal.timeout(6000) : undefined;
    const r = await fetch(`${base}/api/security/events`, { signal });
    if (r.status === 404) { status = "absent"; draw(); return; }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    const d = ms(r.headers && r.headers.get ? r.headers.get("Date") : null); // readable same-origin, or if exposed
    events = new Map();
    for (const e of (j && j.events) || []) add(e);
    if (d != null) { skew = d - Date.now(); dateSeen = true; }
    else if (newest() > Date.now() + skew) skew = newest() - Date.now(); // never measure from a clock behind the newest event
    status = "ok";
    lastOkAt = Date.now();
    lastOkClock = clockFn ? clockFn() : hhmm(lastOkAt);
  } catch {
    status = status === "ok" || status === "stale" ? "stale" : "error";
  }
  draw();
}

function line(e) {
  const ago = minsAgo(e.last_t || e.first_t);
  const n = e.count > 1 ? ` ×${e.count}` : "";
  return `<li class="sec-ev ${isAttempt(e) ? "w" : ""}"><span class="sec-k">${esc(kindWord(e.kind))}${n}</span>
    <span class="sec-p">${esc(e.method || "")} ${esc(e.path || "(no path)")}</span>
    <span class="sec-c">${esc(e.client || "unknown")}</span><time>${ago == null ? "" : ago < 1 ? "now" : `${ago} min ago`}</time></li>`;
}

// One shift-log line when a burst starts. Another only after 10 min without a burst, so a
// slow steady trickle doesn't write a line every few minutes. Survives a reload.
function noteBurst(a) {
  const now = Date.now();
  let last = lastBurstAt;
  try { last = Math.max(last, Number(sessionStorage.getItem("freeboard.secburst")) || 0); } catch { /* private mode */ }
  if (a.burst) {
    if (!burstOn && now - last > REARM_MIN * 60000 && logFn) {
      logFn(`SECURITY: ${a.writes10} refused writes in 10 min (wrong token, a token without that right, or another site). Nothing was sent.`);
    }
    burstOn = true;
    lastBurstAt = now;
    try { sessionStorage.setItem("freeboard.secburst", String(now)); } catch { /* private mode */ }
  } else if (now - last > REARM_MIN * 60000) burstOn = false;
}

function draw() {
  if (!box) return;
  const a = assess([...events.values()]);
  if (status === "ok") noteBurst(a);
  // Nothing in the last hour (or no log here): no card. The handoff still says which.
  // A log that stopped answering always shows, even with nothing recent: an empty card
  // must never look like "no refused requests".
  if (status === "stale" && !a.recent.length) {
    box.hidden = false;
    box.innerHTML = `<div class="kicker">Refused requests</div><div class="sec-head hot">Security log not answering since ${esc(lastOkClock)}. Not checked since then.</div>`;
    return;
  }
  if ((status !== "ok" && status !== "stale") || !a.recent.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  const head = a.burst
    ? `${a.writes10} refused write${a.writes10 === 1 ? "" : "s"} in 10 min: commands without the operator token, or from another site. Nothing was sent.`
    : `${a.total} refused request${a.total === 1 ? "" : "s"} in the last hour. Nothing was sent.`;
  const stale = status === "stale" ? `<div class="sec-head hot">Security log not answering since ${esc(lastOkClock)}. These are the events as of then.</div>` : "";
  box.innerHTML = `<div class="kicker">Refused requests</div>${stale}
    <div class="sec-head ${a.burst ? "hot" : ""}" ${a.burst && status === "ok" ? 'role="alert"' : ""}>${esc(head)}</div>
    <button class="link" data-sec-toggle>${open ? "Hide" : "Show"} ${a.recent.length} event${a.recent.length === 1 ? "" : "s"}</button>
    ${open ? `<ul class="sec-list">${a.recent.slice(0, 12).map(line).join("")}</ul>
      <div class="muted sec-note">Addresses are masked by the server. Tokens and PINs are never logged.</div>` : ""}`;
  box.querySelector("[data-sec-toggle]").onclick = () => { open = !open; draw(); };
}

// For the handoff: honest about whether the log was read.
export function securityLines() {
  if (status === "offline") return ["  Not checked: no backend (offline data)."];
  if (status === "absent") return ["  Not on this backend (no security log)."];
  if (status === "idle") return ["  Not checked yet."];
  if (status === "error") return ["  Not checked: the security log did not answer."];
  const all = [...events.values()];
  const L = [];
  if (status === "stale") L.push(`  Not checked since ${lastOkClock}: the security log stopped answering. As of then:`);
  if (!all.length) { L.push("  No refused requests logged since the server started."); return L; }
  const a = assess(all);
  const byKind = {};
  for (const e of all) byKind[e.kind] = (byKind[e.kind] || 0) + (e.count || 1);
  L.push(`  ${Object.entries(byKind).map(([k, n]) => `${kindWord(k)}: ${n}`).join("; ")}. Nothing was sent by any of them.`);
  if (a.burst) L.push(`  ${a.writes10} refused writes in the last 10 min: check who has the console and the phone.`);
  const last = all.filter(isAttempt).sort((x, y) => (ms(y.last_t) || 0) - (ms(x.last_t) || 0))[0];
  if (last) L.push(`  Latest refused write: ${last.method || ""} ${last.path || "(no path)"} from ${last.client || "unknown"}, ${minsAgo(last.last_t) ?? "?"} min ago.`);
  return L;
}

// log(text): optional, writes one shift-log line when a burst of refused writes starts.
// clock(): optional, the shift clock ("2:14 AM") for "not answering since".
export function mountSecurity(el, apiBase, log, clock) {
  box = el;
  logFn = typeof log === "function" ? log : null;
  clockFn = typeof clock === "function" ? clock : null;
  // ?fresh starts a new shift log: a burst from the rehearsal must not stop the new log's line.
  try { if (new URLSearchParams(location.search).has("fresh")) sessionStorage.removeItem("freeboard.secburst"); } catch { /* private mode */ }
  base = (apiBase || "").replace(/\/$/, "");
  if (!base) { status = "offline"; return; }
  load();
  setInterval(load, POLL_MS);
  setInterval(draw, 60000); // "N min ago" and the one-hour window move on
  // A pushed event adds to what a GET read; it never stands in for a read.
  // A new event's first_t is the server's "now" (it is pushed the moment it is logged): a
  // clock sample for when the browser can't read the Date header (cross-origin).
  liveFeed.on("security", (msg) => {
    const e = msg && msg.event;
    if (!e) return;
    const t = ms(e.first_t);
    if (!dateSeen && t != null && e.first_t === e.last_t && Math.abs(t - Date.now()) < 86400000) skew = t - Date.now();
    add(e);
    draw();
  });
}
