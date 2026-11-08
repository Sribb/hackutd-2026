// Alerts from the backend's dataset mode (/api/eog/alerts and /api/eog/events), turned
// into what the page shows: SPEC order and plain-English actions. Pure functions, no DOM.

import { fmtTime } from "./chart.js";

export const money = (n) => (Number.isFinite(n) ? "$" + Math.round(n).toLocaleString("en-US") : "?");

const CATEGORY = {
  pump_failure: { label: "Pump failure", short: "fail", tone: "alarm" },
  pipeline_leak: { label: "Pipeline leak", short: "leak", tone: "warning" },
  pump_degradation: { label: "Pump wearing out", short: "worn", tone: "warning" },
  other: { label: "Other", short: "check", tone: "warning" },
};
export const categoryOf = (c) => CATEGORY[c] || CATEGORY.other;

const num = (v) => (v === null || v === undefined || v === "" ? null : Number.isFinite(Number(v)) ? Number(v) : null);

// Accepts a list, {alerts: [...]}, or {events: [...]} and fills in names the page uses.
// Fields the backend did not send stay null; nothing is guessed.
// Reads /api/eog/alerts (event_id, system, onset, countdown {...}, action {...}) and the
// SPEC events.json shape of /api/eog/events (system_id, onset_unix, recommended_actions,
// est_cost).
export function normAlerts(resp) {
  const list = Array.isArray(resp) ? resp : resp?.alerts || resp?.events || [];
  return list.map((a0, i) => {
    const cd = a0.countdown && typeof a0.countdown === "object" ? a0.countdown : {};
    const a = { ...cd, ...a0 };
    const htl = a.hours_to_limit;
    const plan = a.action?.type === "plan" ? a.action : a.plan && typeof a.plan === "object" ? a.plan : null;
    const actions = Array.isArray(a.recommended_actions) ? a.recommended_actions : Array.isArray(plan?.actions) ? plan.actions
      : a.action?.type === "field_inspection" ? [{ type: "field_inspection", asset_id: a.action.asset_id }] : [];
    return {
      raw: a0,
      order: num(a.order),
      title: typeof a.title === "string" ? a.title : null,
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
      expiresAt: num(plan?.expires_at),
      approval: a.approval && typeof a.approval === "object" ? a.approval : null,
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
    if (a.order != null && b.order != null) return a.order - b.order; // the backend's order is the judged one
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
        : alert.category === "pump_failure" && alert.tripsAt !== null ? "Accept the automatic shut-in: it is the cheapest option"
        : alert.category === "pump_failure" && alert.levelPct === null ? "No level readings: treat the tank as filling and check it on site" : "No action needed");
    }
  }
  if (!out.length) {
    if (alert.category === "pump_failure" && alert.tripped) {
      out.push("The tank is at shutdown: its wells stay shut in until the repair. Get the pump fixed");
    } else if (alert.category === "pump_failure") {
      // trips_at is null when the repair comes first; a missing level is never "safe".
      out.push(alert.levelPct === null ? "No level readings: treat the tank as filling and check it on site"
        : alert.tripsAt === null ? "No action needed: the tank stays under shutdown until the repair" : "No plan from the backend yet");
    } else {
      out.push(`Send an inspector to ${alert.asset || alert.facility || "the site"}`);
    }
  }
  return out;
}
