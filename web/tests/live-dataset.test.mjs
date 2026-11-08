// Dataset mode in live.js (#153): /api/live/state says mode "dataset", so the demo feed (/ws) is
// never opened, and state and alerts are polled; a new alert is emitted as /ws would. Run: node tests/live-dataset.test.mjs
import assert from "node:assert/strict";
const SRC = new URL("../src", import.meta.url).href;
const mem = new Map();
const kv = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, v), removeItem: (k) => mem.delete(k) };
globalThis.location = { hash: "", pathname: "/", search: "", origin: "http://x" };
globalThis.history = { replaceState() {} };
globalThis.sessionStorage = kv; globalThis.localStorage = kv;
globalThis.addEventListener = () => {};
let sockets = 0;
globalThis.WebSocket = class { constructor() { sockets++; this.readyState = 1; } send() {} close() {} };
let clock = "08:45", alerts = [];
const state = () => ({ mode: "dataset", dataset: "history/snapshot", clock, clock_ts: `2026-10-10T${clock}:00Z`, t: 10, speed: 3, paused: false,
  assets: { S4_TANK_A: { status: alerts.length ? "ALARM" : "OK", level_pct: 60, age_min: 0, stale: false } }, connectors: [{ id: "dataset", kind: "dataset", status: "connected" }] });
globalThis.fetch = async (url) => ({ ok: true, status: 200, json: async () => (String(url).endsWith("/api/alerts") ? { alerts } : state()) });
const L = await import(`${SRC}/live.js`);
const got = [];
L.on("alert", (m) => got.push(m));
L.start("http://b");
await new Promise((r) => setTimeout(r, 50));
assert.equal(L.live.dataset, "history/snapshot");
assert.equal(L.live.clock, "08:45");
assert.equal(sockets, 0, "dataset mode never opens the demo /ws");
assert.equal(L.live.lostSince, null);
clock = "09:00";
alerts = [{ id: "EV-0021", asset_id: "S4_PUMP_A", severity: "ALARM", message: "S4_PUMP_A failed.", active: true }];
await new Promise((r) => setTimeout(r, 2200));
assert.equal(L.live.clock, "09:00", "the clock follows the dataset cursor");
assert.deepEqual(got.map((m) => [m.event, m.alert.id]), [["raised", "EV-0021"]]);
assert.equal(L.reading("S4_TANK_A").status, "ALARM");
alerts = [];
await new Promise((r) => setTimeout(r, 2200));
assert.deepEqual(got.map((m) => [m.event, m.alert.active]), [["raised", true], ["resolved", false]]);
assert.equal(sockets, 0);
console.log("live.js dataset mode: all checks pass");
process.exit(0);
