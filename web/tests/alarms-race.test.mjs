// A slow GET /api/alerts must not undo newer socket events (review finding #4). Run: npm test
import assert from "node:assert/strict";
const mem = new Map();
const kv = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, v), removeItem: (k) => mem.delete(k) };
globalThis.location = { hash: "", pathname: "/", search: "", origin: "http://x" };
globalThis.history = { replaceState() {} };
globalThis.sessionStorage = kv; globalThis.localStorage = kv;
globalThis.matchMedia = () => ({ matches: false });
globalThis.addEventListener = () => {};
globalThis.document = { activeElement: null, querySelector: () => null };
globalThis.setInterval = () => 0;
let sockets = [];
globalThis.WebSocket = class { constructor(u) { this.u = u; this.readyState = 1; sockets.push(this); setTimeout(() => this.onopen && this.onopen(), 0); } send() {} close() {} };
const A = (o) => ({ active: true, severity: "ALARM", message: "m", t: "2026-11-08T03:00:00Z", ...o });
let snapshot = [A({ id: "a1", asset_id: "PUMP_3", priority: "P1" })];
let release;
globalThis.fetch = async (url) => {
  url = String(url);
  if (url.includes("/api/alerts")) {
    const snap = JSON.parse(JSON.stringify(snapshot));
    if (release === null) { await new Promise((r) => { release = r; }); }
    return { ok: true, status: 200, json: async () => ({ alerts: snap }) };
  }
  if (url.includes("/api/live/state")) return { ok: true, status: 200, json: async () => ({ clock: "01:52", t: 8, assets: {} }) };
  return { ok: false, status: 404, json: async () => ({}) };
};
const live = await import(new URL("../src/live.js", import.meta.url).href);
live.start("http://b");
const { mountAlarms, alarms } = await import(new URL("../src/alarms.js", import.meta.url).href);
const box = { hidden: true, innerHTML: "", querySelector: () => null, querySelectorAll: () => [], contains: () => false };
mountAlarms(box, { base: "http://b", incident: () => null, firstShutdown: () => null, blindTanks: () => [], nice: (x) => x, usd: (n) => n, clockAt: () => "", startClock: () => "", clockIn: () => "", select() {}, log() {} });
await new Promise((r) => setTimeout(r, 20));
assert.deepEqual(alarms.roots().map((a) => a.id), ["a1"]);
// A slow /api/alerts read starts (e.g. the 30 s timer or a reconnect)...
release = null;
const p = alarms.refresh();
await new Promise((r) => setTimeout(r, 5));
// ...meanwhile the socket says a1 resolved and a new P1 a2 was raised.
const ws = sockets[sockets.length - 1];
ws.onmessage({ data: JSON.stringify({ type: "alert", event: "resolved", alert: A({ id: "a1", asset_id: "PUMP_3", active: false }) }) });
ws.onmessage({ data: JSON.stringify({ type: "alert", event: "raised", alert: A({ id: "a2", asset_id: "TANK_3", priority: "P1" }) }) });

release(); await p; await new Promise((r) => setTimeout(r, 5));
assert.deepEqual(alarms.roots().map((a) => a.id), ["a2"], "the new P1 stays and the resolved alarm stays resolved");
console.log("alarms.js race: all checks pass");
process.exit(0);
