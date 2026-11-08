// Logic checks for weather.js (Storm check) with browser globals stubbed. Run: npm test
import assert from "node:assert/strict";
const SRC = new URL("../src", import.meta.url).href;
const store = new Map();
globalThis.location = { hash: "", pathname: "/", search: "", origin: "http://x" };
globalThis.history = { replaceState() {} };
globalThis.sessionStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) };
globalThis.localStorage = globalThis.sessionStorage;
globalThis.matchMedia = () => ({ matches: false });
globalThis.addEventListener = () => {};
globalThis.document = { activeElement: null, querySelector: () => null };
let routes = {};
globalThis.fetch = async (url, init) => {
  for (const [k, v] of Object.entries(routes)) if (String(url).includes(k)) return typeof v === "function" ? v(url, init) : { ok: true, status: 200, json: async () => v, headers: new Map() };
  return { ok: false, status: 404, json: async () => ({}), headers: new Map() };
};
globalThis.setInterval = () => 0;

const W = await import(`${SRC}/weather.js`);
const field = { valves: [{ id: "VALVE_XO_34" }, { id: "VALVE_R", manual: false }] };
const p1 = { actions: [{ type: "SET_VALVE", asset_id: "VALVE_XO_34" }, { type: "SEND_TRUCK", asset_id: "TRUCK_1" }] };
const p2 = { actions: [{ type: "SET_VALVE", asset_id: "VALVE_R" }, { type: "CHOKE_WELL", asset_id: "WELL_1" }], keeps_running: true };
assert.equal(W.exposure(p1, field).length, 2);
assert.equal(W.exposure(p2, field).length, 0, "remote valve + choke send nobody");
assert.equal(W.remoteOnly([p1, p2], field).i, 1);
assert.equal(W.hazards({ status: "unavailable" }).length, 0);
assert.match(W.summary({ status: "unavailable", why: "offline mode" }), /not checked/);
const base = { status: "ok", alerts: [], alertsChecked: true, source: "t", thunderPct: 0, gustKmh: 10, visM: 16000, at: Date.now() };
// Never "no risk" from missing or old data (review #3), and Wind Chill is not wind (#16).
assert.doesNotMatch(W.summary(base), /no .*risk/i);
assert.match(W.summary({ ...base, alertsChecked: false }), /warnings not checked/);
assert.match(W.summary({ ...base, thunderPct: null, gustKmh: null, visM: null }), /thunder, wind, visibility not checked/);
assert.match(W.summary({ ...base, at: Date.now() - 25 * 60000 }), /last checked 25 min ago/);
assert.equal(W.hazards({ ...base, thunderPct: 90, at: Date.now() - 25 * 60000 }).length, 0, "old readings flag nothing, and the summary says they're old");
assert.equal(W.hazards({ ...base, alerts: [{ event: "Wind Chill Advisory" }] }).length, 0);
assert.deepEqual(W.hazards({ ...base, alerts: [{ event: "High Wind Warning" }] }).map((h) => h.key), ["wind"]);
assert.equal(W.hazards(base).length, 0);
assert.deepEqual(W.hazards({ ...base, thunderPct: 25 }).map((h) => h.key), ["lightning"]);
assert.deepEqual(W.hazards({ ...base, visM: 1000 }).map((h) => h.key), ["visibility"]);
assert.deepEqual(W.hazards({ ...base, gustKmh: 80 }).map((h) => h.key), ["wind"]);
assert.deepEqual(W.hazards({ ...base, alerts: [{ event: "Flash Flood Warning" }] }).map((h) => h.key), ["flooding"]);
assert.deepEqual(W.hazards({ ...base, alerts: [{ event: "Blowing Dust Advisory" }] }).map((h) => h.key), ["visibility"]);
// grid parsing through startWeather with a mocked NWS
const now = Date.now(), iso = (ms) => new Date(ms).toISOString().replace(".000Z", "+00:00");
routes = {
  "/points/": { properties: { gridId: "MAF", forecastGridData: "https://api.weather.gov/gridpoints/MAF/1,1" } },
  "/gridpoints/": { properties: { updateTime: iso(now),
    probabilityOfThunder: { values: [{ validTime: `${iso(now - 7200000)}/PT1H`, value: 90 }, { validTime: `${iso(now - 1800000)}/PT2H`, value: 30 }, { validTime: `${iso(now + 6 * 3600000)}/PT1H`, value: 99 }] },
    windGust: { values: [{ validTime: `${iso(now)}/PT3H`, value: 20 }] }, visibility: { values: [{ validTime: `${iso(now)}/P1DT2H`, value: 12000 }] } } },
  "/alerts/active": { features: [{ properties: { event: "Severe Thunderstorm Warning", status: "Actual", ends: iso(now + 3600000) } }, { properties: { event: "Test Message", status: "Test" } }] },
};
await W.startWeather({ lat: 31.7, lon: -103.6 });
const w = W.weather();
assert.equal(w.status, "ok");
assert.equal(w.thunderPct, 30, "only hours inside the next 4 h count (not the past 90, not the later 99)");
assert.equal(w.alerts.length, 1, "Test alerts dropped");
assert.equal(W.hazards().length, 1);
console.log("weather.js: all checks pass");
