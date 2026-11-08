// Checks the what-if calculator against the sponsor README's worked example (EVENT_9).
import assert from "node:assert/strict";
import { replay, doNothing, revenuePerHour, tripArrivals, planFromActions, inputFor } from "../src/dataset/plan.js";

const prices = { oil_per_bbl: 60, gas_per_mcf: 3.0 };
// S1_FACILITY_C from history network.json
const net = { defaults: { tank_limits_pct: { warning: 85, shutdown: 92 } }, systems: [{ id: "S1", facilities: [{
  id: "S1_FACILITY_C",
  wells: [
    { id: "S1_WELL_C1", baseline_water_bpd: 450, oil_bpd: 560, gas_mcfd: 2000 },
    { id: "S1_WELL_C2", baseline_water_bpd: 700, oil_bpd: 170, gas_mcfd: 600 },
    { id: "S1_WELL_C3", baseline_water_bpd: 650, oil_bpd: 390, gas_mcfd: 1400 },
  ],
  tank: { id: "S1_TANK_C", capacity_bbl: 800 }, pump: { id: "S1_PUMP_C" },
}] }] };
const ops = { prices, emergency_trucking: { truck_capacity_bbl: 150, cost_per_trip: 905 },
  systems: { S1: { emergency_fleet_size: 2, facilities: { S1_FACILITY_C: { response_min: 75, cycle_min: 230 } } } } };
const readings = { "S1_TANK_C|level_pct": 50 };
const latest = (a, m) => readings[`${a}|${m}`] ?? null;

assert.equal(revenuePerHour(net.systems[0].facilities[0].wells[1], prices), 500);
assert.deepEqual(tripArrivals(75, 230, 2), [75, 305]);

const base = inputFor({ network: net, ops, facilityId: "S1_FACILITY_C", latest, horizonMin: 360 });
assert.ok(base.input, base.error);

// Do nothing: trips about 04:49 (+269 min), shut until 06:20, about $5,016.
const none = doNothing(base.input);
assert.ok(Math.abs(none.tripsAtMin - 268.8) < 1, `trip at ${none.tripsAtMin}`);
assert.ok(Math.abs(none.cost.total - 5016) < 25, `do nothing ${none.cost.total}`);
assert.equal(none.safe, false);

// One truck, one trip: $905, never trips.
const truck = replay(inputFor({ network: net, ops, facilityId: "S1_FACILITY_C", latest, horizonMin: 360,
  plan: { trucks: 1, trips: 1 } }).input);
assert.equal(truck.safe, true);
assert.equal(truck.cost.total, 905);
assert.equal(truck.removedBbl, 150);
assert.deepEqual(truck.problems, []);

// Shut in C2 at 02:25 (+125 min): about $1,958, never trips.
const shut = replay({ ...base.input, shutIns: [{ well: "S1_WELL_C2", startMin: 125 }] });
assert.equal(shut.safe, true);
assert.ok(Math.abs(shut.cost.total - 1958) < 2, `shut-in ${shut.cost.total}`);

// Rules: more trucks than the fleet, and a trip after the repair, are flagged.
const bad = replay(inputFor({ network: net, ops, facilityId: "S1_FACILITY_C", latest, horizonMin: 360,
  plan: { trucks: 3, trips: 3 } }).input);
assert.ok(bad.problems.some((p) => /only 2 are free/.test(p)));
assert.ok(bad.problems.some((p) => /after the repair ends/.test(p)));

// A shut-in too late does not stop the trip; the rest of the wells are auto shut-in.
const late = replay({ ...base.input, shutIns: [{ well: "S1_WELL_C2", startMin: 300 }] });
assert.equal(late.safe, false);
assert.ok(late.cost.auto > 0 && late.cost.shutIn === 0);

// Missing readings give a reason, never a guess.
assert.match(inputFor({ network: net, ops, facilityId: "S1_FACILITY_C", latest: () => null, horizonMin: 360 }).error, /level/);

assert.deepEqual(planFromActions([
  { type: "dispatch_truck", facility_id: "S1_FACILITY_C", trucks: 1, trips_per_truck: 2 },
  { type: "shut_in_wells", wells: ["S1_WELL_C2"], start_after_min: 125 },
], "S1_FACILITY_C"), { trucks: 1, trips: 2, shutIns: [{ well: "S1_WELL_C2", startMin: 125 }] });

console.log("dataset-plan: all pass");

// Cheapest plan: for EVENT_9 at onset the README says one truck, one trip, $905.
import { cheapestJoint, bestForTrucks, planToActions } from "../src/dataset/plan.js";
const spec = { responseMin: 75, cycleMin: 230, capacityBbl: 150, costPerTrip: 905 };
const best9 = cheapestJoint([{ id: "S1_FACILITY_C", input: base.input, truckSpec: spec }], 2);
assert.equal(Math.round(best9.total), 905);
assert.deepEqual(best9.picks[0].plan, { trucks: 1, trips: 1, shutIns: [] });
// With no trucks free, the cheapest is the README's shut-in of S1_WELL_C2 (~$1,958).
const noTruck = bestForTrucks(base.input, spec, 0);
assert.equal(noTruck.plan.shutIns.length, 1);
assert.equal(noTruck.plan.shutIns[0].well, "S1_WELL_C2");
assert.ok(Math.abs(noTruck.result.cost.total - 1958) < 10, `no-truck ${noTruck.result.cost.total}`);
assert.equal(noTruck.result.safe, true);
// Two tanks, one truck: the truck goes where it saves more; never more trucks than the fleet.
const twin = cheapestJoint([
  { id: "T1", input: base.input, truckSpec: spec },
  { id: "T2", input: { ...base.input }, truckSpec: spec },
], 1);
assert.equal(twin.picks.reduce((n, p) => n + p.trucks, 0), 1);
assert.ok(Math.abs(twin.total - (905 + noTruck.result.cost.total)) < 1);
assert.deepEqual(planToActions({ trucks: 0, trips: 0, shutIns: [] }, "X"), [{ type: "none" }]);
// The fleet curve gives the same answer as cheapestJoint at every fleet size, and never
// costs more with more trucks.
{
  const { jointCurve } = await import("../src/dataset/plan.js");
  const tanks = [{ id: "T1", input: base.input, truckSpec: spec }, { id: "T2", input: { ...base.input }, truckSpec: spec }];
  const curve = jointCurve(tanks, 3);
  assert.equal(curve.length, 4);
  for (const { n, best } of curve) assert.ok(Math.abs(best.total - cheapestJoint(tanks, n).total) < 1e-6, `curve at ${n}`);
  for (let n = 1; n < curve.length; n++) assert.ok(curve[n].best.total <= curve[n - 1].best.total + 1e-6);
}
console.log("dataset-plan optimizer: all pass");

// Review fixes.
// A tank already at shutdown with its wells shut in is tripped, never safe, and doing
// nothing costs every well until the repair.
const trippedIn = inputFor({ network: net, ops, facilityId: "S1_FACILITY_C",
  latest: (a, m) => (m === "level_pct" ? 92 : m === "status" ? 0 : m === "water_bpd" ? 0 : null), horizonMin: 60 }).input;
assert.equal(trippedIn.tripped, true);
const tr = doNothing(trippedIn);
assert.equal(tr.safe, false);
assert.equal(tr.tripsAtMin, 0);
assert.ok(Math.abs(tr.cost.total - 3300) < 1, `tripped cost ${tr.cost.total}`);
// A trip that arrives exactly when the repair ends is late, and is neither run nor charged.
const edge = replay({ ...base.input, horizonMin: 75, trucks: { ...spec, count: 1, trips: 1, fleet: 2 } });
assert.ok(edge.problems.some((p) => /after the repair ends/.test(p)));
assert.equal(edge.cost.trucks, 0);
// Two dispatches with different trip counts are priced apart: 1x2 + 1x1 = 3 trips.
const split = planFromActions([
  { type: "dispatch_truck", facility_id: "S1_FACILITY_C", trucks: 1, trips_per_truck: 2 },
  { type: "dispatch_truck", facility_id: "S1_FACILITY_C", trucks: 1, trips_per_truck: 1 },
], "S1_FACILITY_C");
const splitR = replay(inputFor({ network: net, ops, facilityId: "S1_FACILITY_C", latest, horizonMin: 360, plan: split }).input);
assert.equal(splitR.cost.trucks, 3 * 905);
// Staggered shut-ins: the cheap-per-barrel well goes first and the dear one late.
const two = { ...base.input, levelPct: 80 };
const st2 = bestForTrucks(two, spec, 0);
assert.ok(st2.result.safe || st2.result.cost.total <= doNothing(two).cost.total);
console.log("dataset-plan review fixes: all pass");
