// "Walk me through it" with no Claude (#171): the same visible steps an agent would take, from
// the console's own data, so the agentic feel works offline and with no ANTHROPIC_API_KEY.
//   1. fly to the failed asset (the most urgent root alert)   2. trace its pipes
//   3. fly to the tank it feeds   4. jump the time to that tank's forecast shutdown, when it is
//   ahead and within the 6 h forecast   5. draw the tank's level (a chart card, #159)
//   6. open the plans
// The words say only what the data shows: the alert's own message (or a plain verb for a known
// kind), the countdown and plan the status line shows, and where each came from. On a dataset
// (#153) the clock is the recording's UTC clock, not "live". A tank with no fresh reading is said
// first and never called safe. Nothing here moves equipment: the plans open, and a plan still
// needs a person to hold to approve it.

import { toMin, clock12, usd, andList, minsUntil } from "../console/agents.js";

export const WALK = /\bwalk (?:me |us )?through\b|\bstep by step\b|\bshow me what'?s (?:wrong|happening|going on)\b|\bwhat happened\b|^(?:brief me|talk me through it)$/;

const PRIO = { P1: 0, P2: 1, P3: 2 };
const KIND = { PUMP_TRIP: "tripped", PUMP_FAILURE: "failed", TANK_HIGH: "is running high", TANK_HIGH_HIGH: "is at high-high", VALVE_MISMATCH: "doesn't match its command", SENSOR_STALE: "stopped reporting" };
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const isTank = (id) => /(^|_)TANK(_|$)/.test(String(id || ""));

// H: { brief(), alerts(), unknown(), nice(id), hasId(id), tankOf(id), selected(),
//      now() -> minutes, clock(min) -> "2:47 am" | "11:57 utc", dataset() -> bool }
// -> { steps: [directive], reply, ids } (always a reply; steps may be empty)
export function walkthrough(H) {
  const nice = (id) => String(H.nice(id));
  const clock = (m) => (H.clock ? H.clock(m) : clock12(m).toLowerCase());
  const onData = !!(H.dataset && H.dataset());
  const b = H.brief() || { plans: {} };
  const unknown = H.unknown() || [];
  const act = (H.alerts() || []).filter((a) => a && a.active !== false && !a.shelved_until && H.hasId(a.asset_id));
  const isRoot = (a) => !a.root_id || a.root_id === a.id || !act.some((x) => x.id === a.root_id);
  const prio = (a) => PRIO[a.priority] ?? (a.severity === "ALARM" ? 1 : 2);
  act.sort((x, y) => (isRoot(y) - isRoot(x)) || (prio(x) - prio(y)) || ((/^PUMP_/.test(x.kind) ? 0 : 1) - (/^PUMP_/.test(y.kind) ? 0 : 1)));
  const sel = H.selected && H.selected();
  const root = act.find((a) => sel && (a.asset_id === sel || a.consequence_asset_id === sel)) || act[0] || null;
  const cd = b.countdown || null;
  const failed = root ? root.asset_id : null;
  const tank = [root && root.consequence_asset_id, failed && H.tankOf && H.tankOf(failed), cd && cd.asset_id]
    .find((x) => x && H.hasId(x) && x !== failed) || null;
  const now = H.now ? H.now() : null;
  const left = cd && cd.at != null && now != null ? minsUntil(cd.at, now) : null;

  const steps = [];
  if (failed) {
    steps.push({ action: "focus", asset_id: failed, step: `flying to ${nice(failed).toLowerCase()}` });
    steps.push({ action: "trace_pipes", asset_id: failed, args: { direction: "downstream" }, step: `tracing ${nice(failed).toLowerCase()}'s pipes` });
  }
  if (tank) steps.push({ action: "focus", asset_id: tank, step: `flying to ${nice(tank).toLowerCase()}` });
  // Only a shutdown that is still ahead, inside the forecast the slider can show.
  if (cd && cd.at != null && (left == null || (left > 0 && left <= 360))) steps.push({ action: "set_time", args: { clock: clock12(cd.at) }, step: `jumping to ${clock(cd.at)}` });
  const lvl = tank || (cd && H.hasId(cd.asset_id) ? cd.asset_id : null);
  if (lvl && isTank(lvl)) steps.push({ action: "chart", text: `plot ${nice(lvl).toLowerCase()} level`, step: "drawing the level" });
  const plan = (b.plans && (b.plans.robust || b.plans.cheapest)) || null;
  if (plan || cd) steps.push({ action: "open_panel", args: { panel: "plans" }, step: "opening the plans" });

  // Words: at most two sentences (three with a blind tank), every number with its source.
  const said = [];
  if (unknown.length) said.push(`No fresh reading from ${andList(unknown.map(nice))}, so I can't call ${unknown.length === 1 ? "it" : "them"} safe.`);
  if (root) {
    const at = toMin(root.clock || root.raised_at);
    const tag = `${onData ? "alert" : "live alert"}${root.priority ? ` ${root.priority}` : ""}${at != null ? `, raised ${clock(at)}` : ""}`;
    // A known kind gets a plain verb; anything else is the alert's own message, word for word.
    const head = KIND[root.kind] ? `${cap(nice(failed))} ${KIND[root.kind]}` : String(root.message || `${cap(nice(failed))} raised an alarm`).replace(/\.\s*$/, "");
    said.push(`${head} (${tag}).`);
  } else if (!unknown.length) {
    said.push("No open alarm to walk through.");
  }
  if (cd && cd.at != null) {
    const src = b.source === "brief" ? `the live brief${b.now_clock ? ` at ${clock(toMin(b.now_clock))}` : ""}`
      : onData ? `the dataset's forecast${now != null ? ` at ${clock(now)}` : ""}`
        : b.from === "alerts" ? "the live alerts' forecast" : "the shift forecast (every 30 s)";
    const when = left != null && left <= 0 ? `${nice(cd.asset_id).toLowerCase()} is at shutdown now` : `${nice(cd.asset_id).toLowerCase()} reaches shutdown at ${clock(cd.at)}`;
    let fix = "";
    if (plan && plan.total_usd != null) {
      const wait = b.do_nothing_usd != null ? ` against ${usd(b.do_nothing_usd)} if nothing changes` : "";
      const lost = (plan.cannot_save || []).filter((x) => H.hasId(x));
      fix = `; the best plan is ${usd(plan.total_usd)}${wait}${lost.length ? ` and can't save ${andList(lost.map(nice))}` : ""}, and nothing moves until a person holds to approve it`;
    }
    said.push(`${cap(src)} says ${when}${fix}.`);
  }
  const reply = said.slice(0, unknown.length ? 3 : 2).join(" ");
  const ids = [failed, tank, cd && cd.asset_id, ...unknown].filter((x, i, a) => x && a.indexOf(x) === i);
  return { steps, reply, ids };
}
