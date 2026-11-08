// Storm check: the real weather on the lease, and which plans send people out into it.
//
// Why: the cheapest fix usually puts people at the tank battery at night: truck drivers at the
// loadout, the rover at a manual valve. Lightning ignites saltwater tanks (EPA response
// record, Greentide SWD, Wise County TX, 2011; Midland County 2013), and OSHA/NOAA's outdoor
// lightning guidance (OSHA 3863) is to stop outdoor work and shelter until 30 minutes after
// the last thunder. NWS Midland's blowing-dust products (visibility 1/4 to 1 mile) tell drivers
// to pull off and park, and Permian dust-storm crashes have happened on SWD truck routes. A
// remote-only plan (remote valves, chokes, pumps) sends nobody out.
//
// Source: api.weather.gov (NWS, free, no key, CORS open) for the site's lat/lon:
//   /points/{lat},{lon}                 -> the forecast office's grid
//   /gridpoints/{wfo}/{x},{y}           -> hourly probabilityOfThunder, windGust, visibility
//   /alerts/active?point={lat},{lon}    -> warnings and advisories in force
// The window is the next 4 hours of real time (the weather is real even when the field is the
// demo scenario). Cached for 10 minutes per tab.
//
// Rules (any one turns the check on):
//   lightning   a Thunderstorm/Tornado alert, or thunder 25% or more in any hour
//   visibility  a Dust/Fog alert, or visibility under 1 mile (1,609 m)
//   wind        a High Wind/Wind alert, or gusts of 46 mph (74 km/h, NWS Wind Advisory) or more
//   flooding    a Flood alert (lease roads and low-water crossings)
// Never "clear" without data: offline or failed reads say the weather was not checked.
// ?wx=storm runs a labeled drill (a made-up Severe Thunderstorm Warning) for rehearsals.

const WINDOW_H = 4;
const CACHE_MS = 10 * 60 * 1000;
const KEY = "freeboard.wx";
const MILE_M = 1609;
const THUNDER_PCT = 25;
const GUST_KMH = 74;

let state = { status: "idle" };
const subs = new Set();

const param = (k) => new URLSearchParams(location.search).get(k);
const mph = (kmh) => Math.round(kmh / 1.609);

// "2026-11-08T03:00:00+00:00/PT3H" -> [startMs, endMs]
function span(validTime) {
  const [start, dur] = String(validTime).split("/");
  const t0 = Date.parse(start);
  const m = /P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?/.exec(dur || "");
  const ms = m ? ((+m[1] || 0) * 24 * 60 + (+m[2] || 0) * 60 + (+m[3] || 0)) * 60000 : 3600000;
  return [t0, t0 + ms];
}

// Max (or min) of a gridpoint series over [from, to].
function over(series, from, to, pick) {
  let out = null;
  for (const v of (series && series.values) || []) {
    const [a, b] = span(v.validTime);
    if (b <= from || a >= to || v.value == null) continue;
    out = out == null ? v.value : pick(out, v.value);
  }
  return out;
}

async function getJson(url) {
  // Slow venue Wi-Fi must not leave "Checking the weather" up forever: 6 s, then "not checked".
  const signal = typeof AbortSignal !== "undefined" && AbortSignal.timeout ? AbortSignal.timeout(6000) : undefined;
  const r = await fetch(url, { headers: { Accept: "application/geo+json" }, signal });
  if (!r.ok) throw new Error(`NWS ${r.status}`);
  return r.json();
}

function drill() {
  const until = Date.now() + 75 * 60000;
  return {
    status: "ok", drill: true, source: "DRILL: made-up weather for rehearsal, not the real forecast",
    office: "MAF", at: Date.now(), windowEnd: Date.now() + WINDOW_H * 3600000,
    thunderPct: 60, gustKmh: 85, visM: 2400,
    alerts: [{ event: "Severe Thunderstorm Warning", ends: until, headline: "DRILL: Severe Thunderstorm Warning until " + new Date(until).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) }],
  };
}

async function fetchWx(site) {
  const lat = Number(site.lat).toFixed(4), lon = Number(site.lon).toFixed(4);
  const pt = await getJson(`https://api.weather.gov/points/${lat},${lon}`);
  const p = pt.properties || {};
  const [grid, alerts] = await Promise.all([
    getJson(p.forecastGridData),
    getJson(`https://api.weather.gov/alerts/active?point=${lat},${lon}`).catch(() => null), // null: warnings not checked
  ]);
  const g = grid.properties || {};
  const now = Date.now(), end = now + WINDOW_H * 3600000;
  return {
    status: "ok", drill: false, source: `NWS ${p.gridId || "office"} forecast (real weather, real time), updated ${g.updateTime ? new Date(g.updateTime).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "?"}`,
    office: p.gridId, at: now, windowEnd: end,
    thunderPct: over(g.probabilityOfThunder, now, end, Math.max),
    gustKmh: over(g.windGust, now, end, Math.max),
    visM: over(g.visibility, now, end, Math.min),
    alertsChecked: !!alerts,
    alerts: ((alerts && alerts.features) || []).map((f) => f.properties || {})
      .filter((a) => a.status !== "Test")
      .map((a) => ({ event: a.event, ends: Date.parse(a.ends || a.expires) || null, headline: a.headline || a.event })),
  };
}

// A reading older than two refreshes is not current: say when it was checked instead.
const STALE_MS = 2 * CACHE_MS;
const fresh = (w) => w && w.status === "ok" && (w.drill || Date.now() - w.at <= STALE_MS);

// What the weather means for people outside, per rule. Empty when nothing applies.
export function hazards(w = state) {
  if (!w || w.status !== "ok") return [];
  // Old reading: the grid numbers are out of date, but a warning whose end time hasn't come is
  // still in force. Keep those, marked with how old the reading is.
  if (!fresh(w)) {
    const age = Math.round((Date.now() - w.at) / 60000);
    const live = { ...w, thunderPct: null, gustKmh: null, visM: null, alerts: (w.alerts || []).filter((a) => a.ends && a.ends > Date.now()), at: Date.now() };
    return hazards(live).map((h) => ({ ...h, text: `${h.text} (as of ${age} min ago)` }));
  }
  const ev = (re) => w.alerts.find((a) => re.test(a.event || ""));
  const out = [];
  const storm = ev(/thunderstorm|tornado/i);
  if (storm || (w.thunderPct ?? 0) >= THUNDER_PCT) {
    out.push({ key: "lightning", text: storm ? storm.event : `thunder ${w.thunderPct}% in the next ${WINDOW_H} h`, why: "Lightning: OSHA/NOAA say stop outdoor work until 30 min after the last thunder, and lightning ignites saltwater tanks." });
  }
  const dust = ev(/dust|fog/i);
  if (dust || (w.visM != null && w.visM < MILE_M)) {
    out.push({ key: "visibility", text: dust ? dust.event : `visibility down to ${(w.visM / MILE_M).toFixed(1)} mi`, why: "Low visibility: NWS Midland tells drivers in blowing dust to pull off and park, so truck times don't hold." });
  }
  // Wind warnings and advisories, not "Wind Chill Advisory".
  const wind = ev(/\b(high |extreme )?wind (warning|advisory|watch)\b|extreme wind/i);
  if (wind || (w.gustKmh ?? 0) >= GUST_KMH) {
    out.push({ key: "wind", text: wind ? wind.event : `gusts to ${mph(w.gustKmh)} mph`, why: "High wind: hard driving for high-profile tank trucks." });
  }
  const flood = ev(/flood/i);
  if (flood) out.push({ key: "flooding", text: flood.event, why: "Flooding: lease roads and low-water crossings may not be passable." });
  return out;
}

// Who a plan sends to the field. field: /api/field. Remote valves (manual === false), chokes
// and pumps are worked from the console; trucks and manual valves need a person on site.
export function exposure(plan, field) {
  const people = [];
  for (const a of (plan && plan.actions) || []) {
    if (a.type === "SEND_TRUCK") people.push({ asset_id: a.asset_id, who: "truck driver", trucks: true });
    else if (a.type === "SET_VALVE") {
      const v = ((field && field.valves) || []).find((x) => x.id === a.asset_id);
      if (!v || v.manual !== false) people.push({ asset_id: a.asset_id, who: "rover at the valve" });
    }
  }
  return people;
}

// The cheapest plan on the list that sends nobody out, if any.
export function remoteOnly(plans, field) {
  return (plans || []).map((p, i) => ({ p, i })).find(({ p }) => p.keeps_running !== false && !exposure(p, field).length) || null;
}

export function weather() { return state; }
export function onWeather(fn) { subs.add(fn); return () => subs.delete(fn); }

function set(next) {
  state = next;
  try { if (next.status === "ok" && !next.drill) sessionStorage.setItem(KEY, JSON.stringify(next)); } catch { /* private mode */ }
  for (const fn of subs) { try { fn(state); } catch (e) { console.error(e); } }
}

// site: { lat, lon }. offline: true skips the network (and says so).
export async function startWeather(site, { offline = false } = {}) {
  if (param("wx") === "storm") return set(drill());
  if (offline) return set({ status: "unavailable", why: "offline mode" });
  if (!site || !Number.isFinite(Number(site.lat)) || !Number.isFinite(Number(site.lon))) return set({ status: "unavailable", why: "no site location" });
  try {
    const cached = JSON.parse(sessionStorage.getItem(KEY) || "null");
    if (cached && Date.now() - cached.at < CACHE_MS) set(cached);
  } catch { /* ignore */ }
  const load = async () => {
    try { set(await fetchWx(site)); } catch (e) { if (state.status !== "ok") set({ status: "unavailable", why: e.message || "no answer" }); }
  };
  if (state.status !== "ok") await load();
  setInterval(load, CACHE_MS);
}

// One line for the panel and the handoff.
export function summary(w = state) {
  if (w.status === "idle" || w.status === "loading") return "Checking the weather on the lease…";
  if (w.status !== "ok") return `Weather not checked (${w.why}). Look outside before sending anyone.`;
  if (!fresh(w)) return `Weather last checked ${Math.round((Date.now() - w.at) / 60000)} min ago; no newer answer. Look outside before sending anyone.`;
  const hz = hazards(w);
  const bits = [];
  if (w.thunderPct != null) bits.push(`thunder ${w.thunderPct}%`);
  if (w.gustKmh != null) bits.push(`gusts ${mph(w.gustKmh)} mph`);
  if (w.visM != null) bits.push(`visibility ${w.visM >= 16000 ? "10+" : (w.visM / MILE_M).toFixed(1)} mi`);
  // Never "no risk": only what NWS flagged, and what wasn't checked.
  const missing = [w.thunderPct == null && "thunder", w.gustKmh == null && "wind", w.visM == null && "visibility"].filter(Boolean);
  const unchecked = [w.alertsChecked === false && "warnings", ...missing].filter(Boolean);
  const head = hz.length ? hz.map((h) => h.text).join(" · ")
    : unchecked.length ? `No NWS storm, dust or wind flags, but ${unchecked.join(", ")} not checked`
    : "No NWS storm, dust or wind flags";
  return `${w.drill ? "DRILL · " : ""}${head}. Next ${WINDOW_H} h: ${bits.join(", ") || "no grid data"}. ${w.source}.`;
}
