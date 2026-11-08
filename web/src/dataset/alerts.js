// Alerts from the backend's dataset mode, in the sponsor SPEC events.json shape, turned
// into what the page shows: SPEC order, plain-English actions, and the shift handoff.
// Pure functions, no DOM.

import { fmtTime } from "./chart.js";
import { money } from "./plan.js";

const CATEGORY = {
  pump_failure: { label: "Pump failure", short: "FAIL", tone: "alarm" },
  pipeline_leak: { label: "Pipeline leak", short: "LEAK", tone: "warning" },
  pump_degradation: { label: "Pump wearing out", short: "WORN", tone: "warning" },
  other: { label: "Other", short: "CHECK", tone: "warning" },
};
export const categoryOf = (c) => CATEGORY[c] || CATEGORY.other;

const num = (v) => (v === null || v === undefined || v === "" ? null : Number.isFinite(Number(v)) ? Number(v) : null);

// Accepts a list, {alerts: [...]}, or {events: [...]} and fills in names the page uses.
// Fields the backend did not send stay null; nothing is guessed.
// Reads both the SPEC events.json shape (event_id, system_id, onset_unix,
// recommended_actions, est_cost) and the backend's Event shape (id, system, onset,
// trips_at, repair_end, lost_revenue_per_hour), with an optional nested `plan` (the
// backend's Plan: actions, cost) and `do_nothing` ({ cost }).
export function normAlerts(resp) {
  const list = Array.isArray(resp) ? resp : resp?.alerts || resp?.events || [];
  return list.map((a, i) => {
    const htl = a.hours_to_limit;
    const plan = a.plan && typeof a.plan === "object" ? a.plan : null;
    const actions = Array.isArray(a.recommended_actions) ? a.recommended_actions : Array.isArray(plan?.actions) ? plan.actions : [];
    return {
      raw: a,
      id: String(a.event_id ?? a.id ?? `alert_${i + 1}`),
      category: CATEGORY[a.category] ? a.category : "other",
      status: a.status === "resolved" ? "resolved" : "ongoing",
      system: a.system_id ?? a.system ?? null,
      facility: a.facility_id ?? a.facility ?? null,
      asset: a.asset_id ?? a.asset ?? null,
      onset: num(a.onset_unix ?? a.onset),
      end: num(a.end_unix ?? a.end),
      confidence: num(a.confidence),
      evidence: Array.isArray(a.evidence) ? a.evidence.map(String) : [],
      levelPct: num(a.level_pct),
      hours: htl && typeof htl === "object" ? num(htl.p50) : num(htl),
      hoursP10: htl && typeof htl === "object" ? num(htl.p10) : null,
      hoursP90: htl && typeof htl === "object" ? num(htl.p90) : null,
      actions,
      estCost: num(a.est_cost ?? plan?.cost),
      doNothingCost: num(a.do_nothing_cost ?? a.do_nothing?.cost),
      lostRevPerHour: num(a.lost_revenue_per_hour),
      tripsAt: num(a.trips_at_unix ?? a.trips_at),
      repairEnd: num(a.repair_end_unix ?? a.repair_end),
      tripped: a.tripped === true,
      planSummary: typeof plan?.summary === "string" ? plan.summary : null,
    };
  });
}

// SPEC alerts view: pump failures first, smallest hours_to_limit first, ties to the
// higher lost revenue per hour; then leaks and worn pumps. A failure with no countdown
// (it will not reach shutdown) sorts after the ones that will.
export function sortAlerts(list) {
  const pf = (a) => a.category === "pump_failure";
  const h = (a) => (a.tripped ? 0 : a.hours === null ? Infinity : a.hours);
  return [...list].sort((a, b) => {
    if (pf(a) !== pf(b)) return pf(a) ? -1 : 1;
    if (!pf(a)) return (a.onset ?? 0) - (b.onset ?? 0);
    return h(a) - h(b) || (b.lostRevPerHour ?? 0) - (a.lostRevPerHour ?? 0);
  });
}

export function durationText(hours) {
  if (hours === null || !Number.isFinite(hours)) return "?";
  if (hours < 0) return "0 min";
  const mins = Math.round(hours * 60);
  const h = Math.floor(mins / 60), m = mins % 60;
  return h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m} min`;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// One line per recommended action. `ctx` = { at, ops } adds clock times when it can.
export function actionLines(alert, ctx = {}) {
  const out = [];
  const at = ctx.at ?? null;
  for (const a of alert.actions) {
    if (a.type === "dispatch_truck") {
      const f = a.facility_id || alert.facility;
      const sys = alert.system;
      const resp = ctx.ops?.systems?.[sys]?.facilities?.[f]?.response_min;
      const when = at !== null && Number.isFinite(resp) ? `, first load ${fmtTime(at + resp * 60)} UTC` : "";
      out.push(`Send ${plural(a.trucks | 0, "truck")}, ${plural(a.trips_per_truck | 0, "trip")} each, to ${f}${when}`);
    } else if (a.type === "shut_in_wells") {
      const m = Number(a.start_after_min) || 0;
      const wells = (a.wells || []).join(", ");
      out.push(m > 0 && at !== null ? `Shut in ${wells} at ${fmtTime(at + m * 60)} UTC (+${Math.round(m)} min)` : `Shut in ${wells} now`);
    } else if (a.type === "field_inspection") {
      out.push(`Send an inspector to ${a.asset_id || alert.asset}`);
    } else if (a.type === "none") {
      out.push(alert.category === "pump_failure" && alert.tripped ? "The tank is at shutdown: its wells stay shut in until the repair. Get the pump fixed"
        : alert.category === "pump_failure" && alert.hours !== null ? "Accept the automatic shut-in: it is the cheapest option" : "No action needed");
    }
  }
  if (!out.length) {
    if (alert.category === "pump_failure" && alert.tripped) {
      out.push("The tank is at shutdown: its wells stay shut in until the repair. Get the pump fixed");
    } else if (alert.category === "pump_failure") {
      out.push(alert.hours === null ? "No action needed: the tank stays under shutdown until the repair" : "No plan from the backend yet");
    } else {
      out.push(`Send an inspector to ${alert.asset || alert.facility || "the site"}`);
    }
  }
  return out;
}

// The 30-second handoff the SPEC suggests. Never says "all clear" unless detection ran
// and found nothing.
export function handoffText({ folder, at, now, alerts, resolved = [], detection, ops }) {
  const lines = [];
  const travel = at !== now;
  lines.push(`Shift handoff · ${folder}`);
  lines.push(`Dataset time ${fmtTime(at, true)} UTC${travel ? ` (replaying; the data runs to ${fmtTime(now, true)} UTC)` : ""}.`);
  lines.push("");
  if (detection === "screen") {
    lines.push("The backend's detection did not run. This is gaia's own screen of the readings: pump fault signals, leaks and worn pumps.");
    lines.push("");
  } else if (detection !== "ok") {
    lines.push("Detection did not run, so this is NOT an all-clear. Check the alerts on the backend before you leave.");
    return lines.join("\n");
  }
  const live = sortAlerts(alerts.filter((a) => a.status !== "resolved"));
  if (!live.length) {
    lines.push(detection === "screen"
      ? "The screen finds no problem in progress among the pumps running now. A stopped pump's line can't be checked until it runs."
      : "No problems in progress. Every pump and pipeline reads normal at this time.");
  } else {
    const pf = live.filter((a) => a.category === "pump_failure");
    lines.push(`${plural(live.length, "problem")} in progress${pf.length ? `, ${pf.length} with a tank filling` : ""}:`);
    live.forEach((a, i) => {
      const c = categoryOf(a.category);
      const since = a.onset !== null ? ` since ${fmtTime(a.onset, true)}` : "";
      let line = `${i + 1}. ${c.label} at ${a.facility || a.asset}${since}.`;
      if (a.category === "pump_failure") {
        if (a.tripped) line += " The tank is at shutdown and its wells are shut in until the repair.";
        else if (a.hours !== null) line += ` Tank ${a.levelPct !== null ? Math.round(a.levelPct) + "%, " : ""}shuts in at ${fmtTime(at + a.hours * 3600)} UTC (in ${durationText(a.hours)}) if nobody acts.`;
        else line += " The tank will not reach shutdown before the repair.";
      }
      lines.push(line);
      for (const l of actionLines(a, { at, ops })) lines.push(`   ${l}.`);
      if (a.category === "pump_failure" && a.estCost !== null) {
        const vs = a.doNothingCost !== null ? ` vs ${money(a.doNothingCost)} doing nothing` : "";
        lines.push(`   Cost ${money(a.estCost)}${vs}.`);
      }
    });
  }
  if (resolved.length) lines.push("", `${plural(resolved.length, "problem")} resolved earlier this week.`);
  return lines.join("\n");
}

// Trucks the other ongoing pump-failure plans in a system already use, so a what-if on
// one tank only offers what is left (SPEC: shared trucks).
export function trucksInUse(alerts, system, exceptId) {
  let n = 0;
  for (const a of alerts) {
    if (a.status === "resolved" || a.system !== system || a.id === exceptId) continue;
    for (const act of a.actions) if (act.type === "dispatch_truck") n += act.trucks | 0;
  }
  return n;
}
