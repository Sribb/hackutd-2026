// Logic checks for alarms.js (CF-1) with browser globals stubbed. Run: npm test
import assert from "node:assert/strict";
const SRC = new URL("../src", import.meta.url).href;
const mem = new Map();
const kv = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, v), removeItem: (k) => mem.delete(k) };
globalThis.location = { hash: "", pathname: "/", search: "", origin: "http://x" };
globalThis.history = { replaceState() {} };
globalThis.sessionStorage = kv;
globalThis.localStorage = kv;
globalThis.matchMedia = () => ({ matches: false });
globalThis.addEventListener = () => {};
globalThis.document = { activeElement: null, querySelector: () => null };
globalThis.setInterval = () => 0;
globalThis.WebSocket = class { constructor() { setTimeout(() => this.onclose && this.onclose({ code: 1006 }), 0); } };
let alerts = [];
let alertsApi = true;
globalThis.fetch = async (url) => {
  if (alertsApi && String(url).includes("/api/alerts")) return { ok: true, status: 200, json: async () => ({ alerts }) };
  return { ok: false, status: 404, json: async () => ({}) };
};
const box = { hidden: true, innerHTML: "", querySelector: () => null, querySelectorAll: () => [], contains: () => false };
const logs = [];
const { mountAlarms, alarms } = await import(`${SRC}/alarms.js`);
const hooks = {
  base: "http://b", incident: () => ({ root_asset_id: "PUMP_3", summary: "Pump 3 failed." }), firstShutdown: () => ({ asset_id: "TANK_3", t_min: 60 }),
  blindTanks: () => [], nice: (x) => x, usd: (n) => `$${n}`, clockAt: () => "2:47 AM", startClock: () => "01:47", clockIn: () => "", select() {}, log: (t) => logs.push(t),
};
const A = (o) => ({ active: true, severity: "ALARM", message: "m", t: "2026-11-08T03:00:00Z", ...o });
alerts = [
  A({ id: "a1", asset_id: "PUMP_3", kind: "PUMP_TRIP", priority: "P1", minutes_to_consequence_low: 50, root_id: null }),
  A({ id: "a2", asset_id: "TANK_3", kind: "TANK_HIGH", priority: "P1", minutes_to_consequence_low: 50, root_id: "a1", severity: "WARNING" }),
  A({ id: "a3", asset_id: "TANK_3", kind: "TANK_HIGH_HIGH", priority: "P1", minutes_to_consequence_low: 50, root_id: "a2" }),
  A({ id: "b1", asset_id: "TANK_5", kind: "SENSOR_STALE", priority: "P2", minutes_to_consequence_low: null, root_id: null, severity: "WARNING" }),
  A({ id: "c1", asset_id: "PUMP_1", kind: "PUMP_TRIP", priority: "P1", minutes_to_consequence_low: 20, root_id: null }),
  A({ id: "z", asset_id: "PUMP_9", kind: "PUMP_TRIP", priority: "P1", active: false }),
];
mountAlarms(box, hooks);
await new Promise((r) => setTimeout(r, 30));
let roots = alarms.roots();
assert.deepEqual(roots.map((r) => r.id), ["c1", "a1", "b1"], "P1 with least time first, then P1, then P2; resolved left out");
assert.equal(roots.find((r) => r.id === "a1").related_count, 2, "high and high-high fold under the trip, through a chained root_id");

// Shelve the root: its related alarms must stay on the open list.
alerts = alerts.map((a) => (a.id === "a1" ? { ...a, shelved_until_t: 99, shelved_until: "3:00 AM", shelve_reason: "crew" } : a));
await alarms.refresh();
roots = alarms.roots();
const open = roots.filter((r) => !r.shelved).map((r) => r.id);
assert.ok(open.includes("a2") || open.includes("a3"), `a shelved root never hides its unshelved related alarms (open: ${open})`);
assert.deepEqual(roots.filter((r) => r.shelved).map((r) => r.id), ["a1"]);
const loose = roots.find((r) => r.id === "a3" || r.id === "a2");
assert.equal(loose.related_count, 1, "the other related alarm folds under the loose head");

// No alerts API: derived from the forecast, P1 on the early end (60 min / 1.15 = 52).
alertsApi = false;
await alarms.refresh();
roots = alarms.roots();
assert.equal(alarms.derived(), true);
assert.equal(roots.length, 1);
assert.equal(roots[0].priority, "P1", "60 min to shutdown ranks P1 on the early end, as the backend does");
assert.equal(roots[0].asset_id, "PUMP_3");
console.log("alarms.js: all checks pass");
