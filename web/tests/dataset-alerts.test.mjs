// Alerts view rules from the sponsor SPEC, the handoff wording, and the telemetry parser.
import assert from "node:assert/strict";
import { normAlerts, sortAlerts, actionLines, durationText, money } from "../src/dataset/alerts.js";
import { parseTelemetry, sortFolders, checkDataset } from "../src/dataset/data.js";

const raw = [
  { event_id: "L", category: "pipeline_leak", status: "ongoing", facility_id: "S1_FACILITY_A", asset_id: "S1_PIPELINE_A", onset_unix: 100 },
  { event_id: "P2", category: "pump_failure", status: "ongoing", system_id: "S2", facility_id: "S2_FACILITY_B", hours_to_limit: { p50: 2.0 }, lost_revenue_per_hour: 1000,
    recommended_actions: [{ type: "dispatch_truck", facility_id: "S2_FACILITY_B", trucks: 1, trips_per_truck: 1 }] },
  { event_id: "P1", category: "pump_failure", status: "ongoing", system_id: "S2", facility_id: "S2_FACILITY_A", hours_to_limit: { p50: 2.0 }, lost_revenue_per_hour: 3000,
    recommended_actions: [{ type: "shut_in_wells", wells: ["S2_WELL_A1"], start_after_min: 30 }] },
  { event_id: "P0", category: "pump_failure", status: "ongoing", system_id: "S2", facility_id: "S2_FACILITY_C", hours_to_limit: 1.0, lost_revenue_per_hour: 10 },
  { event_id: "W", category: "pump_degradation", status: "ongoing", asset_id: "S3_PUMP_A", onset_unix: 50 },
  { event_id: "Q", category: "pump_failure", status: "ongoing", system_id: "S4", facility_id: "S4_FACILITY_A", hours_to_limit: null, level_pct: 40 },
  { event_id: "R", category: "pump_failure", status: "resolved", end_unix: 9 },
];
const list = normAlerts({ alerts: raw });
const order = sortAlerts(list.filter((a) => a.status !== "resolved")).map((a) => a.id);
// Soonest shutdown first; tie at 2.0 h goes to the bigger loss (P1); no countdown after; then leaks/worn.
assert.deepEqual(order, ["P0", "P1", "P2", "Q", "W", "L"]);
assert.equal(list.find((a) => a.id === "P0").hours, 1.0);
assert.equal(normAlerts([{ category: "weird" }])[0].category, "other");

const at = Date.UTC(2026, 9, 8, 12, 0) / 1000;
const ops = { systems: { S2: { facilities: { S2_FACILITY_B: { response_min: 70 } } } } };
assert.deepEqual(actionLines(list.find((a) => a.id === "P2"), { at, ops }), ["Send 1 truck, 1 trip each, to S2_FACILITY_B, first load 13:10 UTC"]);
assert.deepEqual(actionLines(list.find((a) => a.id === "P1"), { at }), ["Shut in S2_WELL_A1 at 12:30 UTC (+30 min)"]);
assert.match(actionLines(list.find((a) => a.id === "L"))[0], /inspector to S1_PIPELINE_A/);
assert.match(actionLines(list.find((a) => a.id === "Q"))[0], /stays under shutdown/);
assert.equal(durationText(1.5), "1 h 30 min");
assert.equal(durationText(0.25), "15 min");

// /api/eog/alerts: countdown and action are nested, and the backend's order wins.
const eog = normAlerts({ alerts: [
  { order: 2, event_id: "EV-2", category: "pipeline_leak", status: "ongoing", system: "S5", facility: "S5_FACILITY_B", asset: "S5_PIPELINE_B", onset: 10,
    countdown: null, action: { type: "field_inspection", asset_id: "S5_PIPELINE_B", summary: "Send an inspector to S5_PIPELINE_B." } },
  { order: 1, event_id: "EV-1", category: "pump_failure", status: "ongoing", system: "S2", facility: "S2_FACILITY_B", asset: "S2_PUMP_B", onset: 5,
    countdown: { level_pct: 78.6, hours_to_limit: { p50: 1.42, p10: 1.42, p90: 1.42 }, trips_at: 900, repair_end: 1200, tripped: false, lost_revenue_per_hour: 2450 },
    action: { type: "plan", summary: "1 truck, 1 trip (~$905)", cost: 905, expires_at: 640,
      actions: [{ type: "dispatch_truck", facility_id: "S2_FACILITY_B", trucks: 1, trips_per_truck: 1 }] } },
] });
assert.deepEqual(sortAlerts(eog).map((a) => a.id), ["EV-1", "EV-2"]);
const pf = eog.find((a) => a.id === "EV-1");
assert.equal(pf.hours, 1.42);
assert.equal(pf.levelPct, 78.6);
assert.equal(pf.tripsAt, 900);
assert.equal(pf.repairEnd, 1200);
assert.equal(pf.lostRevPerHour, 2450);
assert.equal(pf.estCost, 905);
assert.equal(pf.expiresAt, 640);
assert.equal(pf.actions[0].type, "dispatch_truck");
assert.match(actionLines(eog.find((a) => a.id === "EV-2"))[0], /inspector to S5_PIPELINE_B/);
assert.equal(money(3108.33), "$3,108");
// Review fixes: hours_to_limit without trips_at means the repair comes first; no level is never safe.
const rc = normAlerts([{ event_id: "R1", category: "pump_failure", status: "ongoing", countdown: { level_pct: 60, hours_to_limit: { p50: 6.5 }, trips_at: null, repair_end: 900 },
  action: { type: "plan", actions: [{ type: "none" }] } }])[0];
assert.match(actionLines(rc)[0], /No action needed/);
const nl = normAlerts([{ event_id: "N1", category: "pump_failure", status: "ongoing", countdown: { level_pct: null, hours_to_limit: null, trips_at: null } }])[0];
assert.match(actionLines(nl)[0], /No level readings/);

// Parser: shared grid, a missing cell stays NaN, stale readings are not carried forward.
const csv = [
  "timestamp,ts_unix,asset_id,metric,value",
  "x,1200,T,level_pct,10", "x,1200,P,status,1",
  "x,1500,T,level_pct,11",
  "x,1800,T,level_pct,12",
  "x,2700,T,level_pct,15", "x,2700,P,status,2",
].join("\r\n");
const st = parseTelemetry(csv);
assert.equal(st.start, 1200);
assert.equal(st.now, 2700);
assert.equal(st.times.length, 6);
assert.equal(st.latest("T", "level_pct", 1800), 12);
assert.equal(st.latest("T", "level_pct", 2100), 12); // one step old: fine
assert.equal(st.latest("T", "level_pct", 2400), 12); // two steps old: the default limit
assert.equal(st.latest("T", "level_pct", 2400, 300), null); // past the limit: unknown, not 12
assert.equal(st.latest("P", "status", 1500), 1);
assert.equal(st.latest("P", "status", 1900), null);
assert.equal(st.latest("nope", "x"), null);
assert.deepEqual(st.range("T", "level_pct", 1500, 1800), [[1500, 11], [1800, 12]]);
assert.ok(Math.abs(st.mean("T", "level_pct", 1800, 900) - 11) < 1e-9);
assert.throws(() => parseTelemetry("a,b\n1,2\n"), /columns/);
assert.deepEqual(sortFolders(["history/full_week", "test", "history/snapshot_1"]), ["test", "history/snapshot_1", "history/full_week"]);
assert.ok(checkDataset({ systems: [] }, {}).length >= 3);

console.log("dataset-alerts: all pass");

// The backend's own Event shape with a nested Plan reads the same way.
const ev = normAlerts([{ id: "EV-0001", category: "pump_failure", system: "S1", facility: "S1_FACILITY_C", asset: "S1_PUMP_C",
  onset: 1791418800, end: null, status: "ongoing", confidence: 0.9, trips_at: 1791434940, repair_end: 1791440400,
  hours_to_limit: { p50: 4.5 }, lost_revenue_per_hour: 3300, tripped: false,
  plan: { actions: [{ type: "dispatch_truck", facility_id: "S1_FACILITY_C", trucks: 1, trips_per_truck: 1 }], cost: 905, summary: "1 truck, 1 trip (~$905)" },
  do_nothing: { cost: 5016 } }])[0];
assert.equal(ev.id, "EV-0001");
assert.equal(ev.system, "S1");
assert.equal(ev.onset, 1791418800);
assert.equal(ev.repairEnd, 1791440400);
assert.equal(ev.tripsAt, 1791434940);
assert.equal(ev.estCost, 905);
assert.equal(ev.doNothingCost, 5016);
assert.equal(ev.actions.length, 1);
console.log("dataset-alerts backend Event shape: pass");

// Off-grid rows land on the step at or before them; "now" is the last ts_unix itself.
const offGrid = parseTelemetry("timestamp,ts_unix,asset_id,metric,value\nx,1000,T,level_pct,1\nx,1150,T,level_pct,2\nx,1450,T,level_pct,3\n");
assert.equal(offGrid.now, 1450);
assert.equal(offGrid.start, 900);
assert.equal(offGrid.latest("T", "level_pct", 1450), 3);
assert.throws(() => parseTelemetry("timestamp,ts_unix,asset_id,metric,value\nx,0,T,a,1\nx,1791158400,T,a,2\n"), /bad ts_unix/);
// A tripped pump failure sorts first and never reads as "stays under shutdown".
const tripList = normAlerts([
  { id: "A", category: "pump_failure", hours_to_limit: { p50: 0.5 } },
  { id: "T", category: "pump_failure", hours_to_limit: null, tripped: true },
]);
assert.deepEqual(sortAlerts(tripList).map((a) => a.id), ["T", "A"]);
assert.match(actionLines(tripList[1])[0], /at shutdown/);
console.log("dataset-alerts review fixes: pass");
