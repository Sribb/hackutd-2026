// Logic checks for seismic.js (Seismic check) with browser globals stubbed. Run: npm test
import assert from "node:assert/strict";
const SRC = new URL("../src", import.meta.url).href;
const store = new Map();
globalThis.location = { search: "" };
globalThis.sessionStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) };
let answer = null;
globalThis.fetch = async () => (answer ? { ok: true, status: 200, json: async () => answer } : { ok: false, status: 503, json: async () => ({}) });
const Q = await import(`${SRC}/seismic.js`);
const site = { name: "Mentone, TX", lat: 31.7, lon: -103.6 };

assert.ok(Math.abs(Q.km(31.7, -103.6, 31.7, -103.0) - 56.8) < 1, "haversine: 0.6 deg of longitude at 31.7 N is about 57 km");
const feat = (mag, lat, lon, days, place = "x") => ({ properties: { mag, place, time: Date.now() - days * 86400000, net: "tx" }, geometry: { coordinates: [lon, lat, 5] } });

// Never "no risk" without data.
await Q.startSeismic(site);
assert.equal(Q.quakes().status, "unavailable");
assert.match(Q.seismicSummary().line, /not checked/);
assert.equal(Q.seismicSummary().flag, false);

// Small quakes far away: context only.
answer = { features: [feat(2.6, 32.0, -103.3, 3, "16 km SSW of Jal, NM"), feat(2.1, 31.75, -103.62, 5)] };
store.clear();
await Q.startSeismic(site);
let s = Q.seismicSummary(undefined, ["SWD 1", "SWD 2"]);
assert.equal(s.flag, false);
assert.match(s.line, /1 small quake within 10 mi; 2 M2\+/);

// M3.0+ within 10 miles: confirm disposal capacity (and say so plainly, not as an alarm).
answer = { features: [feat(3.2, 31.75, -103.62, 2, "6 km N of Mentone, TX"), feat(2.4, 31.7, -103.0, 1)] };
store.clear();
await Q.startSeismic(site);
s = Q.seismicSummary(undefined, ["SWD 1", "SWD 2"]);
assert.equal(s.flag, true);
assert.match(s.line, /M3\.2 4 mi away, 2 days ago\. Confirm SWD 1, SWD 2 can take water\./);
assert.match(s.detail, /not an alarm/);

// A big quake beyond 10 miles doesn't flag.
assert.equal(Q.assess(Q.parse({ features: [feat(4.0, 32.2, -103.6, 1)] }, site)).watch, null);
console.log("seismic.js: all checks pass");
