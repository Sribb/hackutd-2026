// The shift log: what the operator weighed, approved and finished, stamped with the
// shift clock. The clock starts at /api/state's clock and runs in real time from the
// moment the console opens. Entries stay in this browser (localStorage) so a reload
// mid-shift keeps them (30 min idle at most); a different incident, or ?fresh, starts a fresh log.

let baseMin = 0;
let t0 = Date.now();
let key = "freeboard.shiftlog";
let list = [];

export function start(clock, incident) {
  const [h, m] = String(clock).split(":").map(Number);
  baseMin = (h || 0) * 60 + (m || 0);
  // Keyed by the incident only: with one live clock (#42), /api/state's clock moves between reloads.
  key = `freeboard.shiftlog.${incident}`;
  try {
    // Restore only a log written to in the last 30 min (a reload mid-shift). An older one is a
    // rehearsal: restoring it would start the demo clock hours late, past the shutdown, with
    // old APPROVED lines in the handoff. ?fresh always starts clean.
    const saved = JSON.parse(localStorage.getItem(key) || "null");
    const fresh = new URLSearchParams(location.search).has("fresh");
    if (fresh || !saved || Date.now() - (saved.last || saved.t0 || 0) > 30 * 60 * 1000) localStorage.removeItem(key);
    else if (Array.isArray(saved.list)) {
      list = saved.list;
      t0 = saved.t0 || t0;
    }
  } catch {
    list = [];
  }
}

// With a live layer, the backend's clock is the shift clock (#42 one clock): "HH:MM" plus
// the live minute's fraction for seconds. Without one, the boot clock runs in real time.
let follow = null;
export function followClock(fn) { follow = fn; }

export function clockNow(seconds = false, plusMin = 0) {
  let x;
  const live = follow && follow();
  if (live && /^\d{1,2}:\d{2}/.test(live.clock || "")) {
    const [h, m] = live.clock.split(":").map(Number);
    const frac = typeof live.t === "number" ? live.t - Math.floor(live.t) : 0;
    x = Math.round((h * 60 + m + frac + plusMin) * 60);
  } else {
    x = baseMin * 60 + Math.floor((Date.now() - t0) / 1000) + Math.round(plusMin * 60);
  }
  x = ((x % 86400) + 86400) % 86400;
  const hh = Math.floor(x / 3600), mm = Math.floor(x / 60) % 60, ss = x % 60;
  const sec = seconds ? `:${String(ss).padStart(2, "0")}` : "";
  return `${hh % 12 || 12}:${String(mm).padStart(2, "0")}${sec} ${hh < 12 ? "AM" : "PM"}`;
}

// While the console is open and in view the log is alive, written or not: a quiet half hour
// followed by a reload must keep it. Only a log nobody had on screen for 30 min is a rehearsal.
// The timer only touches "last": it re-reads storage, so an idle second tab (a projector
// window) never writes its older list over the active tab's newer entries.
const read = () => { try { return JSON.parse(localStorage.getItem(key) || "null"); } catch { return null; } };
const sig = (e) => `${e.at}|${e.kind}|${e.text}`;
function merged() {
  const saved = read();
  if (!saved || !Array.isArray(saved.list)) return list;
  const have = new Set(saved.list.map(sig));
  return saved.list.concat(list.filter((e) => !have.has(sig(e))));
}
setInterval(() => {
  if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
  const saved = read();
  if (!saved || !Array.isArray(saved.list)) return;
  try { localStorage.setItem(key, JSON.stringify({ ...saved, last: Date.now() })); } catch { /* private window */ }
}, 60000);

export function add(kind, text) {
  const e = { at: clockNow(), kind, text };
  list = merged();
  list.push(e);
  try { localStorage.setItem(key, JSON.stringify({ t0, last: Date.now(), list })); } catch { /* private window: keep it in memory */ }
}

// Includes entries another tab on the same incident wrote.
export function entries() {
  list = merged();
  return list;
}
