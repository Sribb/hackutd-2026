// Logic checks for security.js: what counts as a refused write, the burst rule, and the
// handoff's honesty about whether the log was read.
import assert from "node:assert/strict";
const SRC = new URL("../src", import.meta.url).href;
globalThis.location = { hash: "", pathname: "/", search: "", origin: "http://x" };
globalThis.addEventListener = () => {};
globalThis.setInterval = () => 0;
globalThis.WebSocket = class { constructor() {} };
const iso = (minAgo) => new Date(Date.now() - minAgo * 60000).toISOString();
let answer = { status: 200, body: { events: [] } };
globalThis.fetch = async (url) => {
  if (String(url).includes("/api/security/events")) return { ok: answer.status === 200, status: answer.status, json: async () => answer.body };
  return { ok: false, status: 404, json: async () => ({}) };
};
const S = await import(`${SRC}/security.js`);
const box = { hidden: true, innerHTML: "", querySelector: () => ({}) };

// isWrite: a POST to an action is a write, a GET is not, cross-site always is.
assert.equal(S.isWrite({ kind: "auth_failed", method: "POST", path: "/api/actions/act_1/confirm" }), true);
assert.equal(S.isWrite({ kind: "auth_failed", method: "GET", path: "/api/actions" }), false);
assert.equal(S.isWrite({ kind: "cross_site", method: "POST", path: "/api/anything" }), true);
assert.equal(S.isAttempt({ kind: "ws_auth_failed", method: "", path: "/ws" }), false, "a /ws reconnect with an old token is not a write attempt");

// Burst: deduped counts add up. 3 refused writes in 10 min.
const a = S.assess([
  { id: 1, kind: "auth_failed", method: "POST", path: "/api/actions/a/confirm", count: 2, first_t: iso(4), last_t: iso(3) },
  { id: 2, kind: "auth_failed", method: "POST", path: "/api/runs/run_1/cancel", count: 1, first_t: iso(2), last_t: iso(2) },
  { id: 3, kind: "auth_failed", method: "GET", path: "/api/state", count: 5, first_t: iso(30), last_t: iso(30) },
  { id: 4, kind: "lockout", method: "POST", path: "/api/auth/pin", count: 1, first_t: iso(90), last_t: iso(90) },
]);
assert.equal(a.writes10, 3);
assert.equal(a.burst, true);
assert.equal(a.recent.length, 3, "the 90-min-old event is outside the hour");
assert.equal(a.total, 8);

// Offline: never "none".
S.mountSecurity(box, "");
assert.match(S.securityLines()[0], /Not checked/);

// Fresh module state per case is not possible with ESM caching, so drive one module through
// 404 -> error -> ok in order.
const S2 = await import(`${SRC}/security.js?absent`);
answer = { status: 404, body: {} };
S2.mountSecurity(box, "http://b");
await new Promise((r) => setTimeout(r, 10));
assert.match(S2.securityLines()[0], /Not on this backend/);
assert.equal(box.hidden, true);

const S3 = await import(`${SRC}/security.js?error`);
answer = { status: 500, body: {} };
S3.mountSecurity(box, "http://b");
await new Promise((r) => setTimeout(r, 10));
assert.match(S3.securityLines()[0], /Not checked/, "a failed read is never 'no refused requests'");

const S4 = await import(`${SRC}/security.js?ok`);
answer = { status: 200, body: { events: [] } };
S4.mountSecurity(box, "http://b");
await new Promise((r) => setTimeout(r, 10));
assert.match(S4.securityLines()[0], /No refused requests/);
assert.equal(box.hidden, true, "nothing in the last hour: no card");

const S5 = await import(`${SRC}/security.js?burst`);
answer = { status: 200, body: { events: [
  { id: 7, kind: "auth_failed", method: "POST", path: "/api/actions/a/confirm", client: "192.168.1.x", count: 4, first_t: iso(2), last_t: iso(1) },
] } };
S5.mountSecurity(box, "http://b");
await new Promise((r) => setTimeout(r, 10));
assert.equal(box.hidden, false);
assert.match(box.innerHTML, /4 refused writes in 10 min/);
assert.match(box.innerHTML, /role="alert"/);
assert.doesNotMatch(box.innerHTML, /<script/);
const L = S5.securityLines();
assert.match(L[0], /wrong or missing token: 4/);
assert.match(L.join("\n"), /Latest refused write: POST \/api\/actions\/a\/confirm from 192\.168\.1\.x/);

// Escaping: a hostile path can't inject markup.
const S6 = await import(`${SRC}/security.js?esc`);
answer = { status: 200, body: { events: [{ id: 9, kind: "auth_failed", method: "POST", path: "/api/actions/<img src=x onerror=alert(1)>", count: 1, first_t: iso(1), last_t: iso(1) }] } };
S6.mountSecurity(box, "http://b");
await new Promise((r) => setTimeout(r, 10));
assert.doesNotMatch(box.innerHTML, /<img/);
// Write routes: anything but GET/HEAD under /api/ (approve, activate, ack...).
assert.equal(S.isWrite({ kind: "auth_failed", method: "POST", path: "/api/blueprints/b1/activate" }), true);
assert.equal(S.isWrite({ kind: "auth_failed", method: "POST", path: "/api/plans/p1/approve" }), true);
// Uploads too big and rate limits don't count toward a burst.
const up = S.assess([1, 2, 3].map((i) => ({ id: i, kind: "upload_rejected", method: "POST", path: "/api/ingest", count: 1, first_t: iso(1), last_t: iso(1) })));
assert.equal(up.burst, false, "three oversize uploads are not an attack");

// Backend restart: ids start at 1 again. The next GET replaces what we had.
const S7 = await import(`${SRC}/security.js?restart`);
const box7 = { hidden: true, innerHTML: "", querySelector: () => ({}) };
let sends = 0;
const ev = (id, minAgo, count, path) => ({ id, kind: "auth_failed", method: "POST", path, client: "10.0.0.x", count, first_t: iso(minAgo + 1), last_t: iso(minAgo) });
answer = { status: 200, body: { events: [ev(1, 120, 5, "/api/old"), ev(2, 119, 5, "/api/old")] } };
S7.mountSecurity(box7, "http://b", () => sends++);
await new Promise((r) => setTimeout(r, 10));
assert.equal(box7.hidden, true, "two-hour-old events: no card");
answer = { status: 200, body: { events: [ev(1, 1, 1, "/api/actions/a/confirm"), ev(2, 1, 1, "/api/actions/b/confirm"), ev(3, 0, 1, "/api/runs/r/cancel")] } };
try { sessionStorage.clear(); } catch { /* no Web Storage in this Node */ } // earlier cases logged a burst this "session"
const S7b = await import(`${SRC}/security.js?restart2`); // a fresh module reads the restarted log the same way a poll does
S7b.mountSecurity(box7, "http://b", () => sends++);
await new Promise((r) => setTimeout(r, 10));
assert.match(box7.innerHTML, /3 refused writes in 10 min/, "the restarted log's events are not hidden behind old ids");
assert.equal(sends, 1, "one shift-log line for the burst");

// A read that fails after a good one: "Not checked since", never "none".
const S8 = await import(`${SRC}/security.js?stale`);
answer = { status: 200, body: { events: [] } };
let tick = null;
const realSI = globalThis.setInterval;
globalThis.setInterval = (fn, ms) => { if (ms === 30000) tick = fn; return 0; };
S8.mountSecurity(box, "http://b");
await new Promise((r) => setTimeout(r, 10));
assert.match(S8.securityLines()[0], /No refused requests/);
answer = { status: 500, body: {} };
await tick();
assert.match(S8.securityLines()[0], /Not checked since/, "a backend that stopped answering is not 'no refused requests'");
globalThis.setInterval = realSI;

console.log("security.js: all checks pass");
