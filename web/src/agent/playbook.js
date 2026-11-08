// "Walk me through it" with no Claude (#171): the same visible steps an agent would take, from
// the console's own data, so the agentic feel works offline and with no ANTHROPIC_API_KEY.
//   1. fly to the failed asset (the most urgent root alert)   2. trace its pipes to the tank
//   3. fly to that tank   4. jump the time to its forecast shutdown   5. draw its level (a chart
//   card, #159)   6. open the plans
// The words name where each number came from (the live alert, the brief or the shift forecast)
// and never call the field safe while a tank is unknown. Nothing here moves equipment: the
// plans open on screen and a fix still needs a person to hold to confirm it.

import { toMin, clock12, usd, andList } from "../console/agents.js";

export const WALK = /\bwalk (?:me |us )?through\b|\bstep by step\b|\bshow me what'?s (?:wrong|happening|going on)\b|\bwhat happened\b|^(?:brief me|talk me through it)$/;

const PRIO = { P1: 0, P2: 1, P3: 2 };
const KIND = { PUMP_TRIP: "tripped", TANK_HIGH: "is running high", TANK_HIGH_HIGH: "is at high-high", VALVE_MISMATCH: "doesn't match its command", SENSOR_STALE: "stopped reporting" };
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

// H: { brief(), alerts(), unknown(), nice(id), hasId(id), tankOf(id), selected() }
// -> { steps: [directive], reply, ids } (always a reply; steps may be empty)
export function walkthrough(H) {
  const nice = (id) => String(H.nice(id));
  const b = H.brief() || { plans: {} };
  const unknown = H.unknown() || [];
  const act = (H.alerts() || []).filter((a) => a && a.active !== false && !a.shelved_until && H.hasId(a.asset_id));
  const isRoot = (a) => !a.root_id || a.root_id === a.id || !act.some((x) => x.id === a.root_id);
  const prio = (a) => PRIO[a.priority] ?? (a.severity === "ALARM" ? 1 : 2);
  act.sort((x, y) => (isRoot(y) - isRoot(x)) || (prio(x) - prio(y)) || ((x.kind === "PUMP_TRIP" ? 0 : 1) - (y.kind === "PUMP_TRIP" ? 0 : 1)));
  const sel = H.selected && H.selected();
  const root = act.find((a) => sel && (a.asset_id === sel || a.consequence_asset_id === sel)) || act[0] || null;
  const cd = b.countdown || null;
  const failed = root ? root.asset_id : null;
  const tank = [root && root.consequence_asset_id, failed && H.tankOf && H.tankOf(failed), cd && cd.asset_id]
    .find((x) => x && H.hasId(x) && x !== failed) || null;

  const steps = [];
  if (failed) {
    steps.push({ action: "focus", asset_id: failed, step: `flying to ${nice(failed).toLowerCase()}` });
    steps.push({ action: "trace_pipes", asset_id: failed, args: { direction: "downstream" }, step: tank ? `tracing pipes to ${nice(tank).toLowerCase()}` : "tracing its pipes" });
  }
  if (tank) steps.push({ action: "focus", asset_id: tank, step: `flying to ${nice(tank).toLowerCase()}` });
  if (cd && cd.at != null) steps.push({ action: "set_time", args: { clock: clock12(cd.at) }, step: `jumping to ${clock12(cd.at).toLowerCase()}` });
  const lvl = tank || (cd && H.hasId(cd.asset_id) ? cd.asset_id : null);
  if (lvl && /^TANK_/.test(lvl)) steps.push({ action: "chart", text: `plot ${nice(lvl).toLowerCase()} level`, step: "drawing the level" });
  const plan = (b.plans && (b.plans.robust || b.plans.cheapest)) || null;
  if (plan || cd) steps.push({ action: "open_panel", args: { panel: "plans" }, step: "opening the plans" });

  // Words: at most two sentences, every number with its source.
  const said = [];
  if (unknown.length) said.push(`No fresh reading from ${andList(unknown.map(nice))}, so I can't call ${unknown.length === 1 ? "it" : "them"} safe.`);
  if (root) {
    const at = toMin(root.clock || root.raised_at);
    const what = KIND[root.kind] || String(root.message || "raised an alarm").replace(/\.$/, "").toLowerCase();
    said.push(`${cap(nice(failed))} ${what}${at != null ? ` at ${clock12(at).toLowerCase()}` : ""} (live alert${root.priority ? `, ${root.priority}` : ""}).`);
  } else if (!unknown.length) {
    said.push("No open alarm to walk through.");
  }
  if (cd && cd.at != null) {
    const src = b.source === "brief" ? `the live brief${b.now_clock ? ` at ${clock12(toMin(b.now_clock)).toLowerCase()}` : ""}` : b.from === "alerts" ? "the live alerts' forecast" : "the shift forecast (every 30 s)";
    const fix = plan && plan.total_usd != null ? `; the cheapest safe plan is ${usd(plan.total_usd)} and nothing moves until a person holds to confirm` : "";
    said.push(`${cap(src)} says ${nice(cd.asset_id).toLowerCase()} reaches shutdown at ${clock12(cd.at).toLowerCase()}${fix}.`);
  }
  const reply = said.slice(0, unknown.length ? 3 : 2).join(" ");
  const ids = [failed, tank, cd && cd.asset_id, ...unknown].filter((x, i, a) => x && a.indexOf(x) === i);
  return { steps, reply, ids };
}
