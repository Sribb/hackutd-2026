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
assert.equal(S.isWrite({ kind: "ws_auth_failed", method: "", path: "/ws" }), true);

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
console.log("security.js: all checks pass");
