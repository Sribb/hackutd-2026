// The shift log: what the operator weighed, approved and finished, stamped with the
// shift clock. The clock starts at /api/state's clock and runs in real time from the
// moment the console opens. Entries stay in this browser (localStorage) so a reload
// mid-shift keeps them (30 min idle at most); a different incident, or ?fresh, starts a fresh log.

let baseMin = 0;
let t0 = Date.now();
let key = "freeboard.shiftlog";
let list = [];
let logId = "";   // this log's id: tabs only merge entries of the same log
let started = false, opened = false;
const early = []; // entries logged before start() (a /ws alert during the model load): added after it

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
    if (fresh || !saved || !Array.isArray(saved.list) || Date.now() - (saved.last || saved.t0 || 0) > 30 * 60 * 1000) {
      // A new log, with its own id: a tab still open on the old one stops writing here.
      logId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
      list = [];
      localStorage.setItem(key, JSON.stringify({ id: logId, t0, last: Date.now(), list: [] }));
    } else {
      list = saved.list;
      t0 = saved.t0 || t0;
      logId = saved.id || "";
    }
  } catch {
    list = [];
  }
  started = true;
  try { localStorage.removeItem("freeboard.shiftlog"); } catch { /* an old build logged here before start() */ }
}

// Entries logged before start(), added now. The console calls it after its "Console opened" line,
// so the log reads in order. Also runs on the next add() or entries() if nobody calls it.
export function flushEarly() {
  if (!started) return;
  for (const [kind, text] of early.splice(0)) add(kind, text);
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

// While the console is open the log is alive, written or not: a quiet half hour followed by a
// reload must keep it. Only a log no tab had open for 30 min is a rehearsal.
// Several tabs on one incident (laptop and projector) share one log by id. The timer only
// touches "last" on the stored copy, so an idle tab never writes its older list over newer
// entries. A tab whose log was replaced (?fresh, or expiry, in another tab) keeps its own
// entries in memory and stops writing here.
const read = () => { try { return JSON.parse(localStorage.getItem(key) || "null"); } catch { return null; } };
const sig = (e) => `${e.at}|${e.kind}|${e.text}`;
const mine = (saved) => saved && Array.isArray(saved.list) && (saved.id || "") === logId;
// Stored entries, plus this tab's that aren't stored yet (as many copies as this tab has).
function merged() {
  if (!started) return list; // no log yet: never another log's entries
  const saved = read();
  if (!mine(saved)) return list;
  const out = saved.list.slice();
  const left = new Map();
  for (const e of saved.list) left.set(sig(e), (left.get(sig(e)) || 0) + 1);
  for (const e of list) {
    const k = sig(e), n = left.get(k) || 0;
    if (n > 0) left.set(k, n - 1); else out.push(e);
  }
  return out;
}
setInterval(() => {
  const saved = read();
  if (!mine(saved)) return;
  try { localStorage.setItem(key, JSON.stringify({ ...saved, last: Date.now() })); } catch { /* private window */ }
}, 60000);

// Automatic entries (live alarms, run steps): the same text in the same minute adds nothing,
// whether this tab or another open tab on the same log already wrote it.
const AUTO = new Set(["alarm", "alert", "step"]);

export function add(kind, text) {
  if (!started) { early.push([kind, text]); return; }
  if (kind === "open") opened = true;
  else if (early.length) { opened = true; flushEarly(); }
  const e = { at: clockNow(), kind, text };
  const saved = read();
  if (AUTO.has(kind) && merged().some((x) => sig(x) === sig(e))) { list = merged(); return; }
  list = merged();
  list.push(e);
  if (saved && !mine(saved)) return; // our log was replaced: keep this tab's entries in memory only
  try { localStorage.setItem(key, JSON.stringify({ id: logId, t0, last: Date.now(), list })); } catch { /* private window: keep it in memory */ }
}

// Includes entries another tab on the same incident wrote.
export function entries() {
  if (early.length && opened) flushEarly();
  list = merged();
  return list;
}
