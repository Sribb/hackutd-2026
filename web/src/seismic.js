// Seismic check: recent earthquakes near the lease, and what they can mean for disposal.
//
// Why: every plan ends at a saltwater disposal well (SWD). In the Permian, the Texas Railroad
// Commission responds to induced-seismicity clusters area by area: in the Northern
// Culberson-Reeves Seismic Response Area it suspended every deep disposal permit (23 wells)
// effective January 12, 2024; Gardendale and Stanton have their own limits. A cluster near
// the lease is a reason to confirm SWD capacity with water management before leaning on it.
// It is planning context over days, not an alarm: nothing here pages anyone.
//
// Source: USGS FDSN event service (free, no key, CORS open), which carries the TexNet ("tx")
// catalog for West Texas:
//   https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&latitude=..&longitude=..
//     &maxradiuskm=50&minmagnitude=2&starttime=<30 days ago>&orderby=time
// Never "no risk" without data: offline or a failed read says the check didn't run.
// ?quake=drill runs a labeled made-up M3.4 nearby, for rehearsals.

const RADIUS_KM = 50;
const DAYS = 30;
const NEAR_KM = 16.1;      // 10 miles: the distance RRC and NM OCD responses are drawn around
const WATCH_MAG = 3.0;     // M3.0+ within 10 miles: confirm disposal capacity
const CACHE_MS = 30 * 60 * 1000;
const KEY = "freeboard.quakes";

let state = { status: "idle" };
const subs = new Set();
const param = (k) => new URLSearchParams(location.search).get(k);

// Great-circle distance in km.
export function km(lat1, lon1, lat2, lon2) {
  const R = Math.PI / 180, a = Math.sin(((lat2 - lat1) * R) / 2) ** 2
    + Math.cos(lat1 * R) * Math.cos(lat2 * R) * Math.sin(((lon2 - lon1) * R) / 2) ** 2;
  return 6371 * 2 * Math.asin(Math.sqrt(a));
}

// USGS GeoJSON -> [{ mag, place, t, km, net }], nearest-first not assumed.
export function parse(geo, site) {
  return ((geo && geo.features) || []).map((f) => {
    const p = f.properties || {}, c = (f.geometry && f.geometry.coordinates) || [];
    return { mag: p.mag, place: p.place || "", t: p.time, net: p.net, km: km(site.lat, site.lon, c[1], c[0]) };
  }).filter((q) => typeof q.mag === "number" && Number.isFinite(q.km));
}

// What the quakes mean, from a list. watch: an M3.0+ within 10 miles in the window.
export function assess(quakes) {
  const near = quakes.filter((q) => q.km <= NEAR_KM);
  const big = near.filter((q) => q.mag >= WATCH_MAG).sort((a, b) => b.mag - a.mag)[0] || null;
  const largest = quakes.slice().sort((a, b) => b.mag - a.mag)[0] || null;
  const nearest = quakes.slice().sort((a, b) => a.km - b.km)[0] || null;
  return { count: quakes.length, near: near.length, watch: big, largest, nearest };
}

function drill(site) {
  const t = Date.now() - 2 * 86400000;
  return { status: "ok", drill: true, at: Date.now(), source: "DRILL: made-up quake for rehearsal", quakes: [{ mag: 3.4, place: `6 km NW of ${site.name || "the lease"} (drill)`, t, net: "drill", km: 6 }] };
}

async function fetchQuakes(site) {
  const start = new Date(Date.now() - DAYS * 86400000).toISOString().slice(0, 10);
  const url = `https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&latitude=${Number(site.lat).toFixed(3)}&longitude=${Number(site.lon).toFixed(3)}&maxradiuskm=${RADIUS_KM}&minmagnitude=2&starttime=${start}&orderby=time`;
  const signal = typeof AbortSignal !== "undefined" && AbortSignal.timeout ? AbortSignal.timeout(6000) : undefined;
  const r = await fetch(url, { signal });
  if (!r.ok) throw new Error(`USGS ${r.status}`);
  return { status: "ok", drill: false, at: Date.now(), site: siteKey(site), source: "USGS / TexNet", quakes: parse(await r.json(), site) };
}

const siteKey = (site) => `${Number(site.lat).toFixed(3)},${Number(site.lon).toFixed(3)}`;

export function quakes() { return state; }
export function onQuakes(fn) { subs.add(fn); return () => subs.delete(fn); }
function set(next) {
  state = next;
  try { if (next.status === "ok" && !next.drill) sessionStorage.setItem(KEY, JSON.stringify(next)); } catch { /* private mode */ }
  for (const fn of subs) { try { fn(state); } catch (e) { console.error(e); } }
}

export async function startSeismic(site, { offline = false } = {}) {
  if (param("quake") === "drill") return set(drill(site || {}));
  if (offline) return set({ status: "unavailable", why: "offline mode" });
  if (!site || !Number.isFinite(Number(site.lat)) || !Number.isFinite(Number(site.lon))) return set({ status: "unavailable", why: "no site location" });
  try {
    // Another site's quakes are never "near the lease": the cache is per site.
    const cached = JSON.parse(sessionStorage.getItem(KEY) || "null");
    if (cached && cached.site === siteKey(site) && Date.now() - cached.at < CACHE_MS) { set(cached); setInterval(() => load(), CACHE_MS); return; }
  } catch { /* ignore */ }
  async function load() { try { set(await fetchQuakes(site)); } catch (e) { if (state.status !== "ok") set({ status: "unavailable", why: e.message || "no answer" }); } }
  await load();
  setInterval(load, CACHE_MS);
}

// Calendar days on this computer's clock: an 11 PM quake seen at 1 AM was yesterday.
const ago = (t) => {
  const day = (ms) => { const d = new Date(ms); return Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 86400000; };
  const d = day(Date.now()) - day(t);
  return d < 1 ? "today" : d === 1 ? "yesterday" : `${d} days ago`;
};
const asOf = (s) => (s.drill ? "" : ` As of ${new Date(s.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}.`);
const mi = (k) => `${Math.round(k / 1.609)} mi`;

// { flag, line, detail } for the panel and the handoff. flag: true when disposal capacity
// is worth confirming. line is short; detail says where the numbers came from.
export function seismicSummary(s = state, swds = []) {
  if (s.status === "idle") return { flag: false, line: "checking USGS…", detail: "" };
  if (s.status !== "ok") return { flag: false, line: `not checked (${s.why})`, detail: "No earthquake data. Confirm disposal capacity the usual way." };
  const a = assess(s.quakes);
  const where = `within ${mi(RADIUS_KM)}, last ${DAYS} days`;
  const tag = s.drill ? "DRILL · " : "";
  const swdText = swds.length ? swds.join(", ") : "the disposal wells";
  if (a.watch) {
    const q = a.watch;
    return {
      flag: true,
      line: `${tag}M${q.mag.toFixed(1)} ${mi(q.km)} away, ${ago(q.t)}. Confirm ${swdText} can take water.`,
      detail: `${q.place} (${s.source}).${asOf(s)} Texas RRC limits disposal near quake clusters area by area: every deep SWD permit in the Northern Culberson-Reeves area was suspended from Jan 12, 2024. Context for planning, not an alarm.`,
    };
  }
  if (!a.count) return { flag: false, line: `${tag}no M2+ quakes ${where}`, detail: `${s.source}.${asOf(s)}` };
  return {
    flag: false,
    line: `${tag}${a.near ? `${a.near} small quake${a.near === 1 ? "" : "s"} within 10 mi` : "none within 10 mi"}; ${a.count} M2+ ${where}`,
    detail: `Largest M${a.largest.mag.toFixed(1)}, ${a.largest.place}. Nearest ${mi(a.nearest.km)} (M${a.nearest.mag.toFixed(1)}). ${s.source}.${asOf(s)} Nothing M${WATCH_MAG.toFixed(1)}+ within 10 mi.`,
  };
}
