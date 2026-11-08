// What-if calculator for one pump failure, following the sponsor SPEC (Plans).
// It does not detect anything and does not search for the cheapest plan: the backend's
// dataset mode does both. This replays a plan the operator is looking at (the backend's,
// or one they edited) minute by minute and prices it, so the page can show the tank
// staying under shutdown, or not, and what that costs next to doing nothing.
//
// Units: minutes from plan start, barrels, dollars. Pure functions, no DOM.

export const revenuePerDay = (well, prices) =>
  (well.oil_bpd || 0) * prices.oil_per_bbl + (well.gas_mcfd || 0) * prices.gas_per_mcf;

export const revenuePerHour = (well, prices) => revenuePerDay(well, prices) / 24;

// Trip arrival minutes for a truck plan: response, response + cycle, ...
export function tripArrivals(responseMin, cycleMin, trips) {
  const out = [];
  for (let k = 0; k < trips; k++) out.push(responseMin + k * cycleMin);
  return out;
}

// replay(input) -> { points, tripsAtMin, peakPct, removedBbl, cost, problems, safe }
//
// input = {
//   capacityBbl, levelPct,            tank at plan start
//   shutdownPct,                      tank limit (defaults.tank_limits_pct.shutdown)
//   horizonMin,                       repair time left; the pump runs again after it
//   wells: [{ id, waterBpd, revPerHour, producing }],
//   trucks: { count, trips, responseMin, cycleMin, capacityBbl, costPerTrip, fleet },
//   shutIns: [{ well, startMin }],
// }
//
// Rules from SPEC: trucks arrive together, every trip must arrive before the repair ends,
// a trip removes at most what is in the tank, a trip after the tank trips does not help.
// Shut-ins are instant and last until the repair ends. At shutdown every well still
// producing shuts in and the level holds until the repair ends.
export function replay(input) {
  const {
    capacityBbl, levelPct, shutdownPct, horizonMin,
    wells = [], trucks = null, shutIns = [],
  } = input;
  const problems = [];
  const limitBbl = (shutdownPct / 100) * capacityBbl;
  let vol = (levelPct / 100) * capacityBbl;
  const horizon = Math.max(0, horizonMin);

  // Truck groups: { count, trips } each. A plan may send one truck for two trips and
  // another for one, so `trucks.groups` keeps them apart; `count`/`trips` is one group.
  const groups = !trucks ? [] : (Array.isArray(trucks.groups) && trucks.groups.length ? trucks.groups : [{ count: trucks.count, trips: trucks.trips }])
    .map((g) => ({ count: Math.max(0, g.count | 0), trips: Math.max(0, g.trips | 0) })).filter((g) => g.count && g.trips);
  const count = groups.reduce((n, g) => n + g.count, 0);
  // Every trip, as [arrival minute, trucks arriving].
  const arrivalsBy = groups.flatMap((g) => tripArrivals(trucks.responseMin, trucks.cycleMin, g.trips).map((a) => [a, g.count]));
  const arrivals = arrivalsBy.map(([a]) => a);
  if (trucks && Number.isFinite(trucks.fleet) && count > trucks.fleet)
    problems.push(`Plan uses ${count} trucks but only ${trucks.fleet} are free in this system.`);
  // SPEC: every trip must arrive before the repair ends. One at the very minute it ends is late.
  const lateTrips = arrivalsBy.filter(([a]) => a >= horizon).reduce((n, [, c]) => n + c, 0);
  if (lateTrips)
    problems.push(`${lateTrips} truck trip${lateTrips === 1 ? "" : "s"} would arrive after the repair ends.`);

  const startOf = new Map();
  for (const s of shutIns) {
    if (!wells.some((w) => w.id === s.well)) { problems.push(`${s.well} is not a well at this facility.`); continue; }
    const m = Math.max(0, Number(s.startMin) || 0);
    if (!startOf.has(s.well) || m < startOf.get(s.well)) startOf.set(s.well, m);
  }

  const points = [[0, (vol / capacityBbl) * 100]];
  let tripsAtMin = null;
  let removedBbl = 0;
  let peak = vol;
  let tripsMade = 0;
  // A well is off from its shut-in start, or from the trip, whichever comes first.
  const offAt = new Map();
  for (const w of wells) if (w.producing === false) offAt.set(w.id, 0);
  for (const [id, m] of startOf) if (!offAt.has(id) || m < offAt.get(id)) offAt.set(id, m);

  const inflowAt = (m) => wells.reduce((s, w) => {
    const off = offAt.get(w.id);
    return off !== undefined && m >= off ? s : s + Math.max(0, w.waterBpd || 0);
  }, 0);

  const truckAt = new Map();
  for (const [a, c] of arrivalsBy) if (a < horizon) truckAt.set(Math.round(a), (truckAt.get(Math.round(a)) || 0) + c);

  // A tank already at its shutdown limit has tripped: its wells stay shut until the repair
  // ends whatever happens now (SPEC: automatic shut-in). Never report it as safe.
  if (input.tripped || vol >= limitBbl - 1e-6) {
    tripsAtMin = 0;
    vol = Math.min(vol, limitBbl);
  }

  for (let m = 0; m < horizon; m++) {
    // Trucks that arrive at minute m take their load first.
    if (tripsAtMin === null && truckAt.has(m)) {
      const want = truckAt.get(m) * trucks.capacityBbl;
      const take = Math.min(want, vol);
      vol -= take;
      removedBbl += take;
      tripsMade += truckAt.get(m);
      if (take < want - 1e-9) problems.push(`A trip at +${m} min finds less than a full load in the tank.`);
    }
    if (tripsAtMin === null) {
      const rate = inflowAt(m) / 1440; // bbl per minute
      if (vol + rate >= limitBbl && rate > 0) {
        const frac = Math.max(0, (limitBbl - vol) / rate);
        tripsAtMin = m + frac;
        vol = limitBbl;
      } else {
        vol += rate;
      }
    }
    peak = Math.max(peak, vol);
    points.push([m + 1, (vol / capacityBbl) * 100]);
  }

  // Trips after the tank trips still get paid for if the plan sends them.
  const plannedTrips = arrivalsBy.filter(([a]) => a < horizon).reduce((n, [, c]) => n + c, 0);
  const truckCost = plannedTrips * (trucks ? trucks.costPerTrip : 0);
  if (tripsAtMin !== null && arrivals.some((a) => a > tripsAtMin && a < horizon))
    problems.push("Some trips arrive after the tank has already reached shutdown, so they do not prevent it.");

  let shutInCost = 0;
  let autoCost = 0;
  for (const w of wells) {
    // Already off at plan start: not this plan's cost. A tripped tank's wells are off too,
    // but they count, because staying shut until the repair is what doing nothing costs.
    if (w.producing === false && !input.tripped) continue;
    const early = startOf.get(w.id);
    const autoFrom = tripsAtMin;
    const from = early !== undefined && (autoFrom === null || early <= autoFrom) ? early : autoFrom;
    if (from === null || from >= horizon) continue;
    const cost = w.revPerHour * (horizon - from) / 60;
    if (from === early) shutInCost += cost;
    else autoCost += cost;
  }

  return {
    points,
    tripsAtMin,
    peakPct: (peak / capacityBbl) * 100,
    removedBbl,
    tripsMade,
    cost: { trucks: truckCost, shutIn: shutInCost, auto: autoCost, total: truckCost + shutInCost + autoCost },
    problems,
    safe: tripsAtMin === null,
    tripped: tripsAtMin === 0,
  };
}

// The do-nothing baseline: same tank, no trucks, no early shut-ins.
export const doNothing = (input) => replay({ ...input, trucks: null, shutIns: [] });

// Builds a replay input from the loaded dataset at one moment. `latest(asset, metric)`
// returns the last reading at or before that moment, or null. Returns null with a reason
// when something it needs is missing, so the page never invents a number.
export function inputFor({ network, ops, facilityId, latest, rate, level: levelOf, horizonMin, plan }) {
  const sys = network.systems.find((s) => s.facilities.some((f) => f.id === facilityId));
  if (!sys) return { error: `${facilityId} is not in network.json.` };
  const fac = sys.facilities.find((f) => f.id === facilityId);
  const sOps = ops.systems?.[sys.id];
  const fOps = sOps?.facilities?.[facilityId];
  // `level` (optional) smooths the tank reading; the raw reading is the fallback.
  const level = (levelOf && levelOf(fac.tank.id, fac.pump.id)) ?? latest(fac.tank.id, "level_pct");
  if (level === null) return { error: `No ${fac.tank.id} level reading at this time.` };
  const shutdownPct = network.defaults?.tank_limits_pct?.shutdown;
  if (!Number.isFinite(shutdownPct)) return { error: "network.json has no shutdown limit." };
  const statuses = fac.wells.map((w) => latest(w.id, "status"));
  // Tripped: at the limit with every well shut in (SPEC: the level holds, wells stay off).
  const tripped = level >= shutdownPct - 0.5 && statuses.length > 0 && statuses.every((v) => v === 0);
  const wells = fac.wells.map((w) => {
    const status = latest(w.id, "status");
    // `rate` (optional) smooths the noisy rate, for example the last hour's mean.
    const water = (rate && rate(w.id)) ?? latest(w.id, "water_bpd");
    const producing = status === null ? true : status === 1;
    return {
      id: w.id,
      // A well that is shut in right now reads 0; it would come back at its baseline.
      waterBpd: producing ? (water ?? w.baseline_water_bpd) : 0,
      // For a tripped tank this is the revenue lost until the repair either way.
      revPerHour: revenuePerHour(w, ops.prices),
      producing,
    };
  });
  const trucks = (plan?.trucks || plan?.groups?.length) && fOps ? {
    count: plan.trucks, trips: plan.trips, groups: plan.groups,
    responseMin: fOps.response_min, cycleMin: fOps.cycle_min,
    capacityBbl: ops.emergency_trucking.truck_capacity_bbl,
    costPerTrip: ops.emergency_trucking.cost_per_trip,
    fleet: plan.fleet ?? sOps.emergency_fleet_size,
  } : null;
  return {
    input: {
      capacityBbl: fac.tank.capacity_bbl, levelPct: level, shutdownPct, horizonMin,
      wells, trucks, shutIns: plan?.shutIns || [], tripped,
    },
    system: sys, facility: fac, opsFacility: fOps, fleet: sOps?.emergency_fleet_size ?? null,
  };
}

// Reads a SPEC events.json `recommended_actions` list into the plan shape above.
export function planFromActions(actions = [], facilityId) {
  const plan = { trucks: 0, trips: 0, shutIns: [] };
  const groups = [];
  for (const a of actions) {
    if (a.type === "dispatch_truck" && (!facilityId || a.facility_id === facilityId)) {
      groups.push({ count: a.trucks | 0, trips: a.trips_per_truck | 0 });
      plan.trucks += a.trucks | 0;
      plan.trips = Math.max(plan.trips, a.trips_per_truck | 0);
    } else if (a.type === "shut_in_wells") {
      for (const w of a.wells || []) plan.shutIns.push({ well: w, startMin: a.start_after_min || 0 });
    }
  }
  // Two dispatches with different trip counts (1 truck x 2 trips + 1 truck x 1 trip) stay
  // apart; trucks x max trips would overprice them.
  if (groups.length > 1) plan.groups = groups;
  return plan;
}

export const money = (n) => (Number.isFinite(n) ? "$" + Math.round(n).toLocaleString("en-US") : "?");

// ---------- cheapest plan (an independent check on the backend's planner) ----------
//
// For one tank and a given number of trucks, tries every trip count that arrives before
// the repair, and every set of producing wells shut in together at the latest minute
// that still keeps the tank under shutdown (or, when nothing keeps it under, the
// cheapest start on a 5-minute grid). Doing nothing is always a candidate.
// truckSpec = { responseMin, cycleMin, capacityBbl, costPerTrip }.
// Planned peak margin under shutdown. 0 matches the sponsor reference plans (README EVENT_9
// shuts S1_WELL_C2 at 02:25 with the peak 0.3 bbl under the limit).
const MARGIN_PCT = 0;

export function bestForTrucks(input, truckSpec, count) {
  const horizon = input.horizonMin;
  const wells = input.wells.filter((w) => w.producing !== false);
  const trips = [];
  if (count === 0) trips.push(0);
  else for (let t = 1; truckSpec.responseMin + (t - 1) * truckSpec.cycleMin <= horizon; t++) trips.push(t);
  let best = null;
  const consider = (plan, r) => {
    if (r.problems.some((p) => /after the repair|only \d+ are free|not a well/.test(p))) return;
    const c = r.cost.total;
    if (!best || c < best.result.cost.total - 1e-6 || (Math.abs(c - best.result.cost.total) < 1e-6 && r.safe && !best.result.safe))
      best = { plan, result: r };
  };
  const run = (t, shutIns) => replay({ ...input,
    trucks: count && t ? { ...truckSpec, count, trips: t, fleet: count } : null, shutIns });
  for (const t of trips) {
    for (let mask = 0; mask < 1 << wells.length; mask++) {
      const set = wells.filter((_, i) => mask & (1 << i)).map((w) => w.id);
      const at = (s) => run(t, set.map((well) => ({ well, startMin: s })));
      if (!set.length) { consider({ trucks: t ? count : 0, trips: t, shutIns: [] }, at(0)); continue; }
      const r0 = at(0);
      if (r0.safe) {
        // Latest common start that stays safe with a small margin under the limit (sensor
        // noise), then push each well later on its own, the costliest per hour first.
        const ok = (r) => r.safe && r.peakPct <= input.shutdownPct - MARGIN_PCT + 1e-9;
        const latest = (fn) => { let lo = 0, hi = horizon; if (!ok(fn(0))) return null; while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (ok(fn(mid))) lo = mid; else hi = mid - 1; } return lo; };
        const common = latest((m) => at(m)) ?? 0;
        // Staggered: every well starts at 0 (safe), then the costliest per barrel it holds
        // back moves to its latest safe start first, so cheap barrels are given up early
        // and dear ones late. Two passes settle it.
        const costPerBbl = (id) => { const w = wells.find((x) => x.id === id); return w.waterBpd > 0 ? w.revPerHour / w.waterBpd : Infinity; };
        const starts = new Map(set.map((w) => [w, 0]));
        const order = [...set].sort((a, b) => costPerBbl(b) - costPerBbl(a));
        const runStarts = () => run(t, [...starts].map(([well, startMin]) => ({ well, startMin })));
        for (let pass = 0; pass < 2; pass++) for (const w of order) {
          const keep = starts.get(w);
          const m = latest((x) => { starts.set(w, x); return runStarts(); });
          starts.set(w, m === null ? keep : m);
        }
        consider({ trucks: t ? count : 0, trips: t, shutIns: [...starts].map(([well, startMin]) => ({ well, startMin })) }, runStarts());
        consider({ trucks: t ? count : 0, trips: t, shutIns: set.map((well) => ({ well, startMin: common })) }, at(common));
      } else {
        for (let s = 0; s < horizon; s += 5) consider({ trucks: t ? count : 0, trips: t, shutIns: set.map((well) => ({ well, startMin: s })) }, at(s));
      }
    }
  }
  return best;
}

// Several tanks in one system share its fleet. A truck works one tank for the whole
// plan (the conservative reading of the SPEC: no hand-offs between tanks), so this
// tries every split of the free trucks and keeps the cheapest total.
// tanks = [{ id, input, truckSpec }]. Returns { total, picks: [{ id, trucks, plan, result }] }.
export function cheapestJoint(tanks, fleet) {
  const table = tanks.map((tk) => {
    const row = [];
    for (let k = 0; k <= fleet; k++) row.push(bestForTrucks(tk.input, tk.truckSpec, k));
    return row;
  });
  let best = null;
  const walk = (i, left, picks, total) => {
    if (i === tanks.length) {
      const used = picks.reduce((n, p) => n + p.trucks, 0);
      if (!best || total < best.total - 1e-6 || (Math.abs(total - best.total) < 1e-6 && used < best.used)) best = { total, used, picks: [...picks] };
      return;
    }
    for (let k = 0; k <= left; k++) {
      const b = table[i][k];
      if (!b) continue;
      picks.push({ id: tanks[i].id, trucks: k, plan: b.plan, result: b.result });
      walk(i + 1, left - k, picks, total + b.result.cost.total);
      picks.pop();
    }
  };
  walk(0, fleet, [], 0);
  return best;
}

// One tank's best plan with 0 to maxK trucks: the expensive part of a fleet-size curve,
// and independent per tank, so it can run in parallel.
export function tankRow(tank, maxK) {
  const row = [];
  // A tank with no truck times in ops.json can only be planned with no trucks (shut-ins).
  for (let k = 0; k <= maxK; k++) row.push(k > 0 && !tank.truckSpec ? null : bestForTrucks(tank.input, tank.truckSpec, k));
  return row;
}

// Given each tank's row, the cheapest split of n trucks for every n from 0 to maxFleet.
export function walkCurve(tanks, table, maxFleet) {
  const out = [];
  for (let n = 0; n <= maxFleet; n++) {
    let best = null;
    const walk = (i, left, picks, total) => {
      if (i === tanks.length) {
        const used = picks.reduce((m, p) => m + p.trucks, 0);
        if (!best || total < best.total - 1e-6 || (Math.abs(total - best.total) < 1e-6 && used < best.used)) best = { total, used, picks: [...picks] };
        return;
      }
      for (let k = 0; k <= left; k++) {
        const b = table[i][k];
        if (!b) continue;
        picks.push({ id: tanks[i].id, trucks: k, plan: b.plan, result: b.result });
        walk(i + 1, left - k, picks, total + b.result.cost.total);
        picks.pop();
      }
    };
    walk(0, n, [], 0);
    out.push({ n, best });
  }
  return out;
}

// cheapestJoint for every fleet size from 0 to maxFleet, in one go.
export function jointCurve(tanks, maxFleet) {
  return walkCurve(tanks, tanks.map((tk) => tankRow(tk, maxFleet)), maxFleet);
}

// A plan in the SPEC events.json `recommended_actions` shape.
export function planToActions(plan, facilityId) {
  const out = [];
  if (plan.trucks && plan.trips) out.push({ type: "dispatch_truck", facility_id: facilityId, trucks: plan.trucks, trips_per_truck: plan.trips });
  const byStart = new Map();
  for (const s of plan.shutIns || []) {
    const k = Math.round(s.startMin);
    if (!byStart.has(k)) byStart.set(k, []);
    byStart.get(k).push(s.well);
  }
  for (const [m, wells] of byStart) out.push({ type: "shut_in_wells", wells, start_after_min: m });
  if (!out.length) out.push({ type: "none" });
  return out;
}
