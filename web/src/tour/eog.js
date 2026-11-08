// The tour in dataset mode (#158 on #153): the judge's own EOG data. It is a recording, so
// nothing can be tripped live. Step 4 finds the first pump failure the backend itself detected
// (never an answer key, never a hardcoded id or time, so it works on the unseen test/ folder
// too) and moves the replay clock to 15 minutes before it, playing. The alert then arrives by
// itself through the console's normal path. End tour pauses the clock again.

import { BASE } from "../api.js";
import { withToken } from "../auth.js";

async function get(path) {
  const r = await fetch(`${BASE}${path}`);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

// The earliest pump failure in the whole recording (events up to the clock's last row),
// preferring one whose do-nothing outcome trips the tank: the backend's own alert for it,
// read a little after its onset, has a trips_at or says it tripped.
export async function firstPumpFailure() {
  const clock = await get("/api/eog/clock");
  const last = Number(clock.last);
  const { events = [] } = await get(`/api/eog/events?at=${encodeURIComponent(last)}`);
  const fails = events
    .filter((e) => e && e.category === "pump_failure" && Number.isFinite(Number(e.onset_unix)))
    .sort((a, b) => a.onset_unix - b.onset_unix);
  for (const e of fails.slice(0, 6)) {
    const end = Number.isFinite(Number(e.end_unix)) ? Number(e.end_unix) : Infinity;
    const at = Math.min(last, e.onset_unix + 1800, Math.max(e.onset_unix + 60, (e.onset_unix + end) / 2));
    const al = await get(`/api/eog/alerts?at=${Math.round(at)}`).catch(() => null);
    const hit = al && (al.alerts || []).find((a) => a.asset === e.asset_id && Math.abs(Number(a.onset) - e.onset_unix) < 7200);
    const cd = hit && hit.countdown;
    if (cd && (cd.trips_at != null || cd.tripped)) return { ...e, trips: true };
  }
  return fails.length ? { ...fails[0], trips: false } : null;
}

// POST /api/eog/clock with the operator token: {at, play, speed} (speed in data-minutes a second).
export function setClock(body) {
  return fetch(`${BASE}/api/eog/clock`, withToken({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }));
}
