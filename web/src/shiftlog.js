// The shift log: what the operator weighed, approved and finished, stamped with the
// shift clock. The clock starts at /api/state's clock and runs in real time from the
// moment the console opens. Entries stay in this browser (localStorage) so a reload
// mid-shift keeps them; a different incident starts a fresh log.

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
    const saved = JSON.parse(localStorage.getItem(key) || "null");
    if (saved && Array.isArray(saved.list) && Date.now() - (saved.t0 || 0) < 12 * 3600 * 1000) {
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

export function add(kind, text) {
  list.push({ at: clockNow(), kind, text });
  try { localStorage.setItem(key, JSON.stringify({ t0, list })); } catch { /* private window: keep it in memory */ }
}

export function entries() {
  return list;
}
