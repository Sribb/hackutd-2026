"""The forecast: replays the next few hours minute by minute from the current levels.

Physics, per tank, each minute:
    volume += well inflow + crossover flow in - pump outflow - truck loading

- Wells feed their tank at water_bbl_hr, times the choke fraction they keep.
- A running pump holds its tank near setpoint_pct (proportional control), up to
  rate_bbl_hr. A tripped pump, a pump turned OFF, or a closed discharge valve moves
  nothing.
- Pumps share their disposal well's max_bbl_hr. When the sum is over the limit,
  every pump on that well slows by the same factor.
- An open crossover moves water from the fuller tank to the emptier one, up to its
  max_bbl_hr, never past level.
- A truck drives minutes_to the tank, loads capacity_bbl over load_min, then hauls
  to disposal. It is billed for the whole trip.
- At shutdown_pct the system shuts in every well feeding that tank (wells go
  offline for the rest of the horizon, and each costs well_restart_usd).

A tank with no level reading is never simulated as safe: its timeline value is
null, and the response says why in `warnings`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field as dc_field

from .scenario import state_payload

RANGE = 0.15          # inflow uncertainty used for t_min_low / t_min_high
TAU_MIN = 30.0        # pump level-control time constant
DEFAULT_XO_BBL_HR = 300.0
DEFAULT_COSTS = {"truck_usd_hr": 150.0, "disposal_usd_bbl": 1.0, "well_restart_usd": 500.0}
ACTION_TYPES = ("SET_VALVE", "SET_PUMP", "CHOKE_WELL", "SEND_TRUCK")


# ---------- names and clock ----------

def clock_at(start_clock: str, t_min: float) -> str:
    """'01:47' + 60 -> '2:47 AM'."""
    try:
        h, m = (int(x) for x in start_clock.split(":"))
    except (ValueError, AttributeError):
        h, m = 0, 0
    total = h * 60 + m + int(math.floor(t_min + 0.5))
    hh, mm = (total // 60) % 24, total % 60
    return f"{hh % 12 or 12}:{mm:02d} {'AM' if hh < 12 else 'PM'}"


def nice(asset_id: str) -> str:
    """TANK_3 -> Tank 3, VALVE_XO_34 -> the Tank 3 to 4 crossover."""
    parts = asset_id.split("_")
    if asset_id.startswith("VALVE_XO_") and len(parts) == 3 and len(parts[2]) == 2:
        return f"the Tank {parts[2][0]} to {parts[2][1]} crossover"
    if asset_id.startswith("VALVE_D_"):
        return f"the Pump {parts[-1]} discharge valve"
    if asset_id.startswith("SWD_"):
        return f"disposal well {parts[-1]}"
    return " ".join(p.capitalize() if not p.isdigit() else p for p in parts)


# ---------- inputs ----------

@dataclass
class Plant:
    """Everything the simulation needs, built once per request from the scenario."""
    start_clock: str
    tanks: dict            # id -> tank dict
    pumps: dict            # id -> pump dict
    wells: dict            # id -> well dict
    valves: dict           # id -> valve dict
    swds: dict             # id -> disposal well dict
    trucks: dict           # id -> truck dict
    costs: dict
    levels: dict           # tank id -> pct, or None when unknown
    tripped: set           # pump ids that have failed
    positions: dict        # valve id -> OPEN / CLOSED
    pump_swd: dict         # pump id -> (discharge valve id, swd id)
    warnings: list = dc_field(default_factory=list)


def build_plant(scenario: dict) -> Plant:
    f = scenario["field"]
    state = state_payload(scenario)
    assets = state["assets"]
    tanks = {t["id"]: t for t in f["tanks"]}
    levels = {tid: assets.get(tid, {}).get("level_pct") for tid in tanks}
    warnings = [f"{tid} has no level reading, so its future is unknown. Treat it as at risk."
                for tid, lv in levels.items() if lv is None]
    # now.py's worst-case assumptions for live readings that aren't fresh (#42)
    warnings += [str(w) for w in scenario.get("live_assumptions") or []]
    pumps = {p["id"]: p for p in f["pumps"]}
    valves = {v["id"]: v for v in f["valves"]}
    pump_swd = {}
    for v in f["valves"]:
        a, b = (v.get("connects") or [None, None])[:2]
        if a in pumps and isinstance(b, str) and b.startswith("SWD_"):
            pump_swd[a] = (v["id"], b)
    return Plant(
        start_clock=state["clock"],
        tanks=tanks, pumps=pumps, wells={w["id"]: w for w in f["wells"]},
        valves=valves, swds={s["id"]: s for s in f["disposal_wells"]},
        trucks={t["id"]: t for t in f["trucks"]},
        costs={**DEFAULT_COSTS, **{k: v for k, v in f.get("costs", {}).items() if not k.startswith("_")}},
        levels=levels,
        tripped={pid for pid in pumps if assets.get(pid, {}).get("status") == "ALARM"},
        positions={vid: assets.get(vid, {}).get("position", "CLOSED") for vid in valves},
        pump_swd=pump_swd,
        warnings=warnings,
    )


def clean_actions(plant: Plant, actions: list[dict]) -> tuple[list[dict], list[str]]:
    """Drops actions that can't apply, with a warning for each. Never raises."""
    ok, warns = [], []
    for a in actions:
        typ, aid, val = a.get("type"), a.get("asset_id"), a.get("value")
        at = a.get("at_min", 0) or 0
        bad = None
        if typ not in ACTION_TYPES:
            bad = f"unknown action type {typ!r}"
        elif typ == "SET_VALVE" and (aid not in plant.valves or val not in ("OPEN", "CLOSED")):
            bad = "SET_VALVE needs a valve ID and value OPEN or CLOSED"
        elif typ == "SET_PUMP" and (aid not in plant.pumps or val not in ("ON", "OFF")):
            bad = "SET_PUMP needs a pump ID and value ON or OFF"
        elif typ == "CHOKE_WELL" and (aid not in plant.wells or not isinstance(val, (int, float))
                                      or isinstance(val, bool) or not 0 <= val <= 1):
            bad = "CHOKE_WELL needs a well ID and a value from 0 (shut in) to 1 (untouched)"
        elif typ == "SEND_TRUCK" and (aid not in plant.trucks
                                      or a.get("target_id") not in plant.trucks.get(aid, {}).get("minutes_to", {})):
            bad = "SEND_TRUCK needs a truck ID and a target_id the truck can reach"
        elif typ == "SET_PUMP" and val == "ON" and aid in plant.tripped:
            bad = f"{aid} is tripped and can't be turned on until it's repaired"
        if bad:
            warns.append(f"Ignored {typ} on {aid}: {bad}.")
            continue
        ok.append({**a, "at_min": float(at)})
    ok.sort(key=lambda x: x["at_min"])
    return ok, warns


# ---------- one run ----------

@dataclass
class Run:
    levels: dict           # recorded minute -> {tank id: pct or None}
    wells_online: dict     # recorded minute -> wells online
    shutdowns: dict        # tank id -> t_min
    events: list
    cost: dict
    warnings: list


def simulate(plant: Plant, actions: list[dict], horizon: int, inflow_scale: float = 1.0,
             record_every: int = 1) -> Run:
    """One run. Levels are recorded every `record_every` minutes and at the horizon."""
    tanks, wells, costs = plant.tanks, plant.wells, plant.costs
    cap = {tid: float(t["capacity_bbl"]) for tid, t in tanks.items()}
    known = {tid for tid, lv in plant.levels.items() if lv is not None}
    vol = {tid: plant.levels[tid] / 100.0 * cap[tid] for tid in known}
    shut_vol = {tid: tanks[tid]["shutdown_pct"] / 100.0 * cap[tid] for tid in tanks}
    set_vol = {tid: tanks[tid].get("setpoint_pct", 50) / 100.0 * cap[tid] for tid in tanks}

    positions = dict(plant.positions)
    pump_on = {pid: pid not in plant.tripped for pid in plant.pumps}
    kept = {wid: 1.0 for wid in wells}
    online = {wid: True for wid in wells}
    wells_by_tank: dict[str, list[str]] = {}
    for wid, w in wells.items():
        wells_by_tank.setdefault(w["tank_id"], []).append(wid)

    pending = list(actions)
    loading: list[dict] = []          # active truck jobs
    truck_free = {tid: 0.0 for tid in plant.trucks}
    shutdowns: dict[str, float] = {}
    events: list[dict] = []
    warnings: list[str] = []
    swd_flagged: set[str] = set()
    xo_warned: set[str] = set()
    trucking_usd = disposal_usd = lost_usd = 0.0
    restarts = 0

    # Fixed lookups, built once per run instead of every minute.
    pumps_by_tank: dict[str, list[str]] = {}
    for pid, p in plant.pumps.items():
        pumps_by_tank.setdefault(p["tank_id"], []).append(pid)
    by_swd: dict[str, list[str]] = {}
    for pid, (_, sid) in plant.pump_swd.items():
        by_swd.setdefault(sid, []).append(pid)
    swd_limit = {sid: plant.swds.get(sid, {}).get("max_bbl_hr", float("inf")) / 60.0 for sid in by_swd}
    crossovers = [(vid, v["connects"][0], v["connects"][1], v.get("max_bbl_hr", DEFAULT_XO_BBL_HR) / 60.0,
                   v.get("bbl_hr_per_pct"))
                  for vid, v in plant.valves.items()
                  if len(v.get("connects") or []) >= 2 and all(c in tanks for c in v["connects"][:2])]
    well_list = [(wid, w["tank_id"], w["water_bbl_hr"] / 60.0 * inflow_scale, w["lost_usd_hr"] / 60.0)
                 for wid, w in wells.items()]
    pump_list = [(pid, p["tank_id"], plant.pump_swd.get(pid, (None, None))[0], p["rate_bbl_hr"] / 60.0)
                 for pid, p in plant.pumps.items()]

    levels_out: dict[int, dict] = {}
    online_out: dict[int, int] = {}
    record_every = max(1, record_every)

    def snapshot(minute: int):
        levels_out[minute] = {tid: (round(100.0 * vol[tid] / cap[tid], 2) if tid in known else None)
                              for tid in tanks}
        online_out[minute] = sum(1 for wid in wells if online[wid] and kept[wid] > 0)

    # Inflow and lost production only change on a choke or a shutdown, so cache them.
    inflow: dict[str, float] = {}
    lost_rate = 0.0
    dirty = True

    snapshot(0)
    for t in range(horizon):
        # 1. Actions that take effect this minute.
        while pending and pending[0]["at_min"] <= t:
            a = pending.pop(0)
            typ, aid = a["type"], a["asset_id"]
            if typ == "SET_VALVE":
                positions[aid] = a["value"]
            elif typ == "SET_PUMP":
                pump_on[aid] = a["value"] == "ON"
            elif typ == "CHOKE_WELL":
                kept[aid] = float(a["value"])
                dirty = True
            elif typ == "SEND_TRUCK":
                truck = plant.trucks[aid]
                if "busy_min" in a:
                    # A trip already under way that's past loading (one clock, #42): the truck
                    # is hauling to disposal, so it takes no more water and is busy until then.
                    truck_free[aid] = max(truck_free[aid], t + float(a["busy_min"]))
                    continue
                dispatch = max(float(t), truck_free[aid])
                if dispatch > t:
                    warnings.append(f"{nice(aid)} is busy until {clock_at(plant.start_clock, dispatch)}, "
                                    "so that trip starts late.")
                target = a["target_id"]
                # A trip already under way (one clock, #42) carries what's left of it: minutes
                # to the tank, minutes of loading, barrels it can still take, minutes of haul.
                to_tank = float(a.get("arrive_in", truck["minutes_to"][target]))
                load_min = float(a.get("load_min", truck.get("load_min", 20)))
                haul_min = float(a.get("haul_min", truck.get("haul_to_swd_min", 30)))
                truck_cap = float(a.get("capacity_bbl", truck["capacity_bbl"]))
                arrive = dispatch + to_tank
                trip = to_tank + load_min + haul_min
                truck_free[aid] = dispatch + trip
                trucking_usd += truck.get("usd_hr", costs["truck_usd_hr"]) * trip / 60.0
                loading.append({"truck": aid, "tank": target, "start": arrive, "end": arrive + load_min,
                                "rate": truck_cap / max(load_min, 1.0), "loaded": 0.0, "cap": truck_cap})
                events.append({"type": "TRUCK_ARRIVES", "asset_id": aid, "target_id": target,
                               "t_min": round(arrive, 1),
                               "text": f"{nice(aid)} reaches {nice(target)} at "
                                       f"{clock_at(plant.start_clock, arrive)}"})

        # 2. Flows this minute (bbl/min).
        if dirty:
            inflow, lost_rate = dict.fromkeys(tanks, 0.0), 0.0
            for wid, tid, water, lost in well_list:
                k = kept[wid] if online[wid] else 0.0
                inflow[tid] += water * k
                lost_rate += lost * (1.0 - k)
            dirty = False
        lost_usd += lost_rate

        xo = dict.fromkeys(tanks, 0.0)
        for vid, a, b, qmax, per_pct in crossovers:
            if positions.get(vid) != "OPEN":
                continue
            if a not in known or b not in known:
                if vid not in xo_warned:
                    xo_warned.add(vid)
                    warnings.append(f"{nice(vid)} connects a tank with no level reading, "
                                    "so its flow is left out.")
                continue
            pa, pb = vol[a] / cap[a], vol[b] / cap[b]
            q = min(qmax, abs(pa - pb) * cap[a] * cap[b] / (cap[a] + cap[b]))
            if per_pct:  # gravity flow: proportional to the level difference
                q = min(q, per_pct * abs(pa - pb) * 100.0 / 60.0)
            src, dst = (a, b) if pa > pb else (b, a)
            xo[src] -= q
            xo[dst] += q

        truck_out = dict.fromkeys(tanks, 0.0)
        loading = [j for j in loading if j["end"] > t]
        for job in loading:
            if job["start"] <= t < job["end"] and job["tank"] in known:
                truck_out[job["tank"]] += max(min(job["rate"], job["cap"] - job["loaded"]), 0.0)

        pump_out = {}
        for pid, tid, valve, rate in pump_list:
            if tid not in known or not pump_on[pid] or (valve and positions.get(valve) != "OPEN"):
                pump_out[pid] = 0.0
                continue
            want = inflow[tid] + xo[tid] - truck_out[tid] + (vol[tid] - set_vol[tid]) / TAU_MIN
            pump_out[pid] = min(max(want, 0.0), rate)

        for sid, pids in by_swd.items():
            total = sum(pump_out[p] for p in pids)
            if total > swd_limit[sid] + 1e-9:
                scale = swd_limit[sid] / total
                for p in pids:
                    pump_out[p] *= scale
                if sid not in swd_flagged:
                    swd_flagged.add(sid)
                    events.append({"type": "SWD_AT_CAPACITY", "asset_id": sid, "t_min": float(t),
                                   "text": f"{nice(sid).capitalize()} hits its limit of "
                                           f"{plant.swds[sid]['max_bbl_hr']:g} bbl/hr at "
                                           f"{clock_at(plant.start_clock, t)}. Pumps feeding it slow down."})

        # 3. Integrate.
        for tid in known:
            out = sum(pump_out[pid] for pid in pumps_by_tank.get(tid, ()))
            before = vol[tid]
            take = min(truck_out[tid], max(before + inflow[tid] + xo[tid] - out, 0.0))
            for job in loading:
                if job["tank"] == tid and job["start"] <= t < job["end"] and truck_out[tid] > 0:
                    share = take * min(job["rate"], job["cap"] - job["loaded"]) / truck_out[tid]
                    job["loaded"] += share
                    disposal_usd += share * costs["disposal_usd_bbl"]
            vol[tid] = min(max(before + inflow[tid] + xo[tid] - out - take, 0.0), cap[tid])

            if tid not in shutdowns and vol[tid] >= shut_vol[tid] - 1e-9:
                frac = (shut_vol[tid] - before) / (vol[tid] - before) if vol[tid] > before else 1.0
                t_hit = round(t + min(max(frac, 0.0), 1.0), 1)
                shutdowns[tid] = t_hit
                lost = [wid for wid in wells_by_tank.get(tid, []) if online[wid] and kept[wid] > 0]
                for wid in wells_by_tank.get(tid, []):
                    online[wid] = False
                restarts += len(lost)
                dirty = True
                events.append({"type": "SHUTDOWN_LEVEL", "asset_id": tid, "t_min": t_hit,
                               "wells_lost": lost,
                               "text": f"{nice(tid)} reaches shutdown at {clock_at(plant.start_clock, t_hit)}. "
                                       f"{len(lost)} wells go down with it."})
        if (t + 1) % record_every == 0 or t + 1 == horizon:
            snapshot(t + 1)

    lost_total = lost_usd + restarts * costs["well_restart_usd"]
    cost = {
        "total_usd": int(round(trucking_usd + disposal_usd + lost_total)),
        "trucking_usd": int(round(trucking_usd)),
        "disposal_usd": int(round(disposal_usd)),
        "lost_production_usd": int(round(lost_total)),
    }
    events.sort(key=lambda e: e["t_min"])
    return Run(levels_out, online_out, shutdowns, events, cost, warnings)


# ---------- the endpoint ----------

def forecast(scenario: dict, actions: list[dict], horizon: int = 360, step: int = 5,
             inflow_scale: float = 1.0, with_range: bool = True, plant: Plant | None = None) -> dict:
    """POST /api/forecast in the docs/CONTRACT.md shape."""
    plant = plant or build_plant(scenario)
    acts, warns = clean_actions(plant, actions)
    step = max(1, step)
    run = simulate(plant, acts, horizon, inflow_scale, record_every=step)
    warnings = plant.warnings + warns + run.warnings

    if with_range:
        hot = simulate(plant, acts, horizon, inflow_scale * (1 + RANGE))
        cool = simulate(plant, acts, horizon, inflow_scale * (1 - RANGE))
        for ev in run.events:
            if ev["type"] == "SHUTDOWN_LEVEL":
                tid = ev["asset_id"]
                ev["t_min_low"] = hot.shutdowns.get(tid, ev["t_min"])
                ev["t_min_high"] = cool.shutdowns.get(tid)  # None: later than the horizon
                ev["text"] = (ev["text"].split(".")[0] + ", give or take "
                              f"{_spread(ev)}." + ev["text"].split(".", 1)[1])
        for tid, t_hot in sorted(hot.shutdowns.items()):
            if tid not in run.shutdowns:
                warnings.append(f"{nice(tid)} stays below shutdown, but reaches it at "
                                f"{clock_at(plant.start_clock, t_hot)} if inflow runs "
                                f"{int(RANGE * 100)}% high.")

    timeline = [{"t_min": t, "tanks": run.levels[t], "wells_online": run.wells_online[t]}
                for t in range(0, horizon + 1, step)]
    if timeline[-1]["t_min"] != horizon:
        timeline.append({"t_min": horizon, "tanks": run.levels[horizon],
                         "wells_online": run.wells_online[horizon]})
    for row in timeline:
        row["tanks"] = {k: (None if v is None else round(v, 1)) for k, v in row["tanks"].items()}
    warnings = [w[:1].upper() + w[1:] for w in dict.fromkeys(warnings)]
    return {"timeline": timeline, "events": run.events, "cost": run.cost, "warnings": warnings}


def _spread(ev: dict) -> str:
    lo, hi = ev["t_min_low"], ev["t_min_high"]
    if hi is None:
        return f"up to {int(round(ev['t_min'] - lo))} minutes earlier or much later"
    return f"{int(round(max(ev['t_min'] - lo, hi - ev['t_min'])))} minutes"
