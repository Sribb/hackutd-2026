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
// A backend without the log (404) hides the card and the handoff says "not on this backend";
// a failed read says "not checked", never "none".

import * as liveFeed from "./live.js";

const POLL_MS = 30000;
const RECENT_MIN = 60;      // the card shows the last hour
const BURST = 3;            // this many refused writes in 10 min is worth a look
const WRITE = /^\/api\/(actions|runs|plans\/[^/]+\/execute|live\/sim|ingest|scenario)/;

let base = "";
let box = null;
let status = "idle";        // idle | ok | absent (404) | error | offline (no backend)
const events = new Map();   // id -> event (the newest copy wins: count and last_t grow)
let open = false;
let logFn = null, burstLogged = false;  // the shift log gets one line per burst

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const ms = (iso) => { const t = Date.parse(iso || ""); return Number.isFinite(t) ? t : null; };
const minsAgo = (iso) => { const t = ms(iso); return t == null ? null : Math.max(0, Math.round((Date.now() - t) / 60000)); };

const WORD = {
  auth_failed: "wrong or missing token",
  cross_site: "write from another site",
  forbidden_role: "token without that right",
  lockout: "locked out (too many tries)",
  upload_rejected: "upload refused",
  ws_auth_failed: "live feed refused (wrong token)",
};
export const kindWord = (k) => WORD[k] || String(k || "refused").replace(/_/g, " ");

// Is this event an attempt to change something? Writes and refused /ws tokens count.
export function isWrite(e) {
  return e.kind === "cross_site" || e.kind === "ws_auth_failed" || (e.method && e.method !== "GET" && WRITE.test(e.path || ""));
}

// What the events add up to. recent: last hour. burst: refused writes in the last 10 min
// add up to BURST or more (deduped events carry a count).
export function assess(list, now = Date.now()) {
  const within = (e, min) => { const t = ms(e.last_t || e.first_t); return t != null && now - t <= min * 60000; };
  const recent = list.filter((e) => within(e, RECENT_MIN)).sort((a, b) => (ms(b.last_t) || 0) - (ms(a.last_t) || 0));
  const writes10 = list.filter((e) => within(e, 10) && isWrite(e)).reduce((n, e) => n + (e.count || 1), 0);
  const total = recent.reduce((n, e) => n + (e.count || 1), 0);
  return { recent, total, burst: writes10 >= BURST, writes10 };
}

function merge(list) {
  for (const e of list || []) {
    if (!e || e.id == null) continue;
    const had = events.get(e.id);
    // A deduped event grows: keep the copy with the larger count.
    if (!had || (e.count || 1) >= (had.count || 1)) events.set(e.id, e);
  }
  // Keep the newest 200: the backend keeps 500, the card needs an hour.
  if (events.size > 200) [...events.keys()].sort((a, b) => a - b).slice(0, events.size - 200).forEach((k) => events.delete(k));
}

async function load() {
  try {
    const signal = typeof AbortSignal !== "undefined" && AbortSignal.timeout ? AbortSignal.timeout(6000) : undefined;
    const r = await fetch(`${base}/api/security/events`, { signal });
    if (r.status === 404) { status = "absent"; draw(); return; }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    merge((await r.json()).events);
    status = "ok";
  } catch {
    if (status !== "ok") status = "error"; // keep showing what we have; the handoff says when it was read
  }
  draw();
}

function line(e) {
  const ago = minsAgo(e.last_t || e.first_t);
  const n = e.count > 1 ? ` ×${e.count}` : "";
  return `<li class="sec-ev ${isWrite(e) ? "w" : ""}"><span class="sec-k">${esc(kindWord(e.kind))}${n}</span>
    <span class="sec-p">${esc(e.method || "")} ${esc(e.path || "")}</span>
    <span class="sec-c">${esc(e.client || "unknown")}</span><time>${ago == null ? "" : ago < 1 ? "now" : `${ago} min ago`}</time></li>`;
}

function draw() {
  if (!box) return;
  const a = assess([...events.values()]);
  // Nothing in the last hour (or no log here): no card. The handoff still says which.
  if (a.burst && !burstLogged && logFn) logFn(`SECURITY: ${a.writes10} refused writes in 10 min (wrong token or another site). Nothing was sent.`);
  burstLogged = a.burst;
  if (status !== "ok" || !a.recent.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  const head = a.burst
    ? `${a.writes10} refused write${a.writes10 === 1 ? "" : "s"} in 10 min: commands without the operator token, or from another site. Nothing was sent.`
    : `${a.total} refused request${a.total === 1 ? "" : "s"} in the last hour. Nothing was sent.`;
  box.innerHTML = `<div class="kicker">Refused requests</div>
    <div class="sec-head ${a.burst ? "hot" : ""}" ${a.burst ? 'role="alert"' : ""}>${esc(head)}</div>
    <button class="link" data-sec-toggle>${open ? "Hide" : "Show"} ${a.recent.length} event${a.recent.length === 1 ? "" : "s"}</button>
    ${open ? `<ul class="sec-list">${a.recent.slice(0, 12).map(line).join("")}</ul>
      <div class="muted sec-note">Addresses are masked by the server. Tokens and PINs are never logged.</div>` : ""}`;
  box.querySelector("[data-sec-toggle]").onclick = () => { open = !open; draw(); };
}

// For the handoff: honest about whether the log was read.
export function securityLines() {
  if (status === "offline") return ["  Not checked: no backend (offline data)."];
  if (status === "absent") return ["  Not on this backend (no security log)."];
  if (status !== "ok") return ["  Not checked: the security log did not answer."];
  const all = [...events.values()];
  if (!all.length) return ["  No refused requests logged since the server started."];
  const a = assess(all);
  const byKind = {};
  for (const e of all) byKind[e.kind] = (byKind[e.kind] || 0) + (e.count || 1);
  const L = [`  ${Object.entries(byKind).map(([k, n]) => `${kindWord(k)}: ${n}`).join("; ")}. Nothing was sent by any of them.`];
  if (a.burst) L.push(`  ${a.writes10} refused writes in the last 10 min: check who has the console and the phone.`);
  const last = all.filter(isWrite).sort((x, y) => (ms(y.last_t) || 0) - (ms(x.last_t) || 0))[0];
  if (last) L.push(`  Latest refused write: ${last.method || ""} ${last.path} from ${last.client || "unknown"}, ${minsAgo(last.last_t) ?? "?"} min ago.`);
  return L;
}

// log(text): optional, writes one shift-log line when a burst of refused writes starts.
export function mountSecurity(el, apiBase, log) {
  box = el;
  logFn = typeof log === "function" ? log : null;
  base = (apiBase || "").replace(/\/$/, "");
  if (!base) { status = "offline"; return; }
  load();
  setInterval(load, POLL_MS);
  setInterval(draw, 60000); // "N min ago" and the one-hour window move on
  liveFeed.on("security", (msg) => { if (msg && msg.event) { merge([msg.event]); if (status !== "absent" && status !== "offline") status = "ok"; draw(); } });
}
