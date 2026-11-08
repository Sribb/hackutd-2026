// Watchdog checks for live.js: a paused sim is not a lost feed, a half-open socket is
// replaced at once, and a new socket is judged on its own messages.
import assert from "node:assert/strict";
const SRC = new URL("../src", import.meta.url).href;
const mem = new Map();
const kv = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, v), removeItem: (k) => mem.delete(k) };
globalThis.location = { hash: "", pathname: "/", search: "", origin: "http://x" };
globalThis.history = { replaceState() {} };
globalThis.sessionStorage = kv; globalThis.localStorage = kv;
globalThis.addEventListener = () => {};
let watchdog = null;
const realSI = setInterval;
globalThis.setInterval = (fn, ms) => { if (ms === 2000) watchdog = fn; return 0; };
const timers = [];
globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
globalThis.clearTimeout = () => {};
const socks = [];
let pong = true;
globalThis.WebSocket = class {
  constructor() { socks.push(this); this.readyState = 1; this.sent = []; }
  send(m) { this.sent.push(m); if (pong && JSON.parse(m).type === "ping") this.onmessage && this.onmessage({ data: '{"type":"pong"}' }); }
  close() { this.closed = true; } // half-open: onclose never fires
};
let now = 1_000_000; Date.now = () => now;
const st = { clock: "01:50", t: 6, speed: 0.25, assets: { TANK_3: { status: "OK", level_pct: 60 } } };
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => st });
const L = await import(`${SRC}/live.js`);
L.start("http://b");
const runTimers = () => { const t = timers.splice(0); t.forEach((x) => { if (x.ms > 100) return; try { x.fn(); } catch { /* */ } }); };
let s = socks.at(-1); s.onopen();
const tick = (sk) => sk.onmessage({ data: JSON.stringify({ type: "state", state: st }) });
for (let i = 0; i < 5; i++) { tick(s); now += 1000; }

// (A) Paused sim: no state for 10 s, but pongs come back. Not lost, socket kept.
for (let i = 0; i < 5; i++) { now += 2000; watchdog(); }
assert.equal(L.live.lostSince, null, "a paused sim that answers pings is not a lost feed");
assert.ok(!s.closed, "the healthy socket stays open");
assert.ok(s.sent.some((m) => JSON.parse(m).type === "ping"), "pings go out");

// (B) Half-open: nothing comes back at all. Lost, socket dropped, a new one started without waiting for onclose.
pong = false;
now += 2000; watchdog();
now += 2000; watchdog();
now += 3000; watchdog();
assert.ok(L.live.lostSince, "6 s of silence on a socket that ticked is a lost feed");
assert.ok(s.closed, "the dead socket is closed");
assert.equal(L.live.connected, false, "and no longer counts as connected");
const retry = timers.find((x) => x.ms >= 1000 && x.ms <= 15000);
assert.ok(retry, "a reconnect is scheduled at once");

// (C) The new socket opens after a long gap: it is not judged on the old socket's ticks.
const before = socks.length;
retry.fn();
assert.equal(socks.length, before + 1, "a new socket");
const s2 = socks.at(-1);
now += 9000; // the backend took a while
s2.onopen();
now += 2000; watchdog();
assert.ok(!s2.closed, "a fresh socket is not closed before it has had a chance to tick");
globalThis.setInterval = realSI;
console.log("live.js watchdog: all checks pass");
process.exit(0);
