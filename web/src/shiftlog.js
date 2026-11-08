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
  key = `freeboard.shiftlog.${clock}.${incident}`;
  try {
    const saved = JSON.parse(localStorage.getItem(key) || "null");
    if (saved && Array.isArray(saved.list)) {
      list = saved.list;
      t0 = saved.t0 || t0;
    }
  } catch {
    list = [];
  }
}

export function clockNow(seconds = false) {
  const s = Math.floor((Date.now() - t0) / 1000);
  const x = (baseMin * 60 + s) % 86400;
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
