// Logic checks for live.js (CF-6 ages, feed-lost clock) with browser globals stubbed. Run: npm test
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
let now = 1_000_000;
Date.now = () => now;
let down = false;
globalThis.fetch = async () => { if (down) throw new Error("down"); return ({ ok: true, status: 200, json: async () => ({ clock: "01:50", t: 6, speed: 0.25, assets: { TANK_3: { status: "OK", level_pct: 60, age_min: 12 } } }) }); };
const L = await import(`${SRC}/live.js`);
L.start("http://b");
await new Promise((r) => setTimeout(r, 10));
sock.onopen();
assert.equal(L.staleMinutes("TANK_3"), 12);
now += 60_000; // one wall minute = 15 live minutes at 0.25/s
assert.equal(L.ageText("TANK_3"), "27 min ago", "age runs at the live speed");
down = true;
sock.onclose({ code: 1006 });
await new Promise((r) => setTimeout(r, 4100));
assert.ok(L.live.lostSince, "feed lost after 4 s");
assert.ok(L.staleMinutes("TANK_3") >= 27, `age never resets when the feed drops (${L.staleMinutes("TANK_3")})`);
const before = L.clockEstimate();
now += 120_000; // 30 live minutes later
const after = L.clockEstimate();
assert.equal(after.estimated, true);
assert.equal(after.clock, "02:35", `clock carries on while lost (${before.clock} -> ${after.clock})`);
console.log("live.js: all checks pass");
process.exit(0);
