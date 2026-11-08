// Frozen-gauge check (trend.js frozen()) with browser globals stubbed. Run: npm test
import assert from "node:assert/strict";
const SRC = new URL("../src", import.meta.url).href;
const mem = new Map();
const kv = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, v), removeItem: (k) => mem.delete(k) };
globalThis.location = { hash: "", pathname: "/", search: "", origin: "http://x" };
globalThis.history = { replaceState() {} };
globalThis.sessionStorage = kv; globalThis.localStorage = kv;
globalThis.addEventListener = () => {};
let sock;
globalThis.WebSocket = class { constructor() { sock = this; this.readyState = 1; } send() {} close() {} };
globalThis.fetch = async () => ({ ok: false, status: 404, json: async () => ({}) });
const L = await import(`${SRC}/live.js`);
const T = await import(`${SRC}/trend.js`);
L.start("http://b");
T.startTrend("");
await new Promise((r) => setTimeout(r, 10));
sock.onopen();
const send = (t, a, b) => sock.onmessage({ data: JSON.stringify({ type: "state", state: { clock: "02:00", t, speed: 0.25, assets: { TANK_3: { status: "OK", level_pct: a }, TANK_4: { status: "OK", level_pct: b } } } }) });
// Tank 3 jitters like a working gauge; Tank 4 reads exactly the same for 12 live minutes.
for (let i = 0; i <= 48; i++) send(i * 0.25, 60 + (i % 3) * 0.07, 55.34);
assert.equal(T.frozen("TANK_3"), null, "a jittering gauge is not frozen");
const f = T.frozen("TANK_4");
assert.ok(f, "an exactly flat gauge for 12 min is flagged");
assert.equal(f.v, 55.34);
assert.equal(f.minutes, 12);
// It moves again: no longer flagged.
send(12.5, 60.1, 55.41);
assert.equal(T.frozen("TANK_4"), null);
// Flat for under 10 min: not flagged.
for (let i = 1; i <= 30; i++) send(12.5 + i * 0.25, 60 + (i % 2) * 0.05, 55.41);
assert.equal(T.frozen("TANK_4"), null, "7.5 min flat is not enough");
console.log("trend.js frozen gauge: all checks pass");
process.exit(0);
