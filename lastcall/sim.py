"""Reference forecast that speaks the POST /api/forecast shape from docs/CONTRACT.md.

Last Call does not need this file if the real backend forecast exists. Anything
with the signature forecast(scenario, actions, horizon_minutes, step_minutes,
inflow_scale) -> dict in the contract shape can be passed to lastcall.core
instead. This one exists so Last Call can be built, tested, and demoed tonight
without waiting on the backend.

Physics, kept simple on purpose (see README "Not building": no ML forecast):
  level += (well inflow + crossover in - pump out - truck loads) * dt
  A running pump holds its tank at setpoint (outflow = min(rate, what's there)).
  A tank that reaches shutdown level shuts in its wells for the rest of the
  horizon. Shut-in wells are not restarted by the forecast.
  An open crossover moves water from the higher tank to the lower one,
  proportional to the level gap, capped by the line's max_bbl_hr.
  A sent truck drives to the tank, loads, hauls to disposal, and comes back
  until the horizon ends.

Action semantics (contract action list):
  SET_VALVE  value "OPEN" or "CLOSED", takes effect at at_min.
  SET_PUMP   value "ON" or "OFF", takes effect at at_min. A tripped pump stays
             off unless a SET_PUMP ON arrives.
  CHOKE_WELL value is the fraction of normal flow the well keeps
             (1.0 = untouched, 0.5 = half, 0.0 = shut in), from at_min on.
  SEND_TRUCK target_id is the tank, at_min is the dispatch time. Travel time
             comes from the truck's minutes_to.
"""

from __future__ import annotations

from typing import Dict, List, Optional

XO_GAIN_PER_HR = 1.0  # bbl/hr of crossover flow per bbl of level gap


def _clock_text(clock: str, t_min: float) -> str:
    h, m = (int(x) for x in clock.split(":"))
    total = (h * 60 + m + int(round(t_min))) % (24 * 60)
    hh, mm = divmod(total, 60)
    suffix = "AM" if hh < 12 else "PM"
    hh12 = hh % 12 or 12
    return f"{hh12}:{mm:02d} {suffix}"


def clock_text(scenario: dict, t_min: float) -> str:
    return _clock_text(scenario.get("clock", "00:00"), t_min)


def _truck_schedule(truck: dict, target: str, dispatch: float, horizon: float) -> List[tuple]:
    """Return (load_start, load_end) windows for a truck cycling on one tank."""
    drive = truck["minutes_to"].get(target)
    if drive is None:
        return []
    load = truck.get("load_min", 20)
    haul = truck.get("haul_to_swd_min", 30)
    windows = []
    t = dispatch + drive
    while t < horizon:
        windows.append((t, t + load))
        t += load + haul + drive
    return windows


def _run(scenario: dict, actions: List[dict], horizon: int, step: int, inflow_scale: float) -> dict:
    tanks = {t["id"]: dict(t) for t in scenario["tanks"]}
    level = {tid: t["capacity_bbl"] * t["level_pct"] / 100.0 for tid, t in tanks.items()}
    cap = {tid: float(t["capacity_bbl"]) for tid, t in tanks.items()}
    shut_bbl = {tid: cap[tid] * t["shutdown_pct"] / 100.0 for tid, t in tanks.items()}
    setpoint = {tid: cap[tid] * t.get("setpoint_pct", 50) / 100.0 for tid, t in tanks.items()}

    pumps = {p["id"]: p for p in scenario["pumps"]}
    pump_on = {pid: p.get("status", "OK") != "ALARM" for pid, p in pumps.items()}
    valves = {v["id"]: v for v in scenario["valves"]}
    valve_open = {vid: v.get("position", "CLOSED") == "OPEN" for vid, v in valves.items()}
    wells = scenario["wells"]
    well_keep = {w["id"]: 1.0 for w in wells}
    tank_shut: Dict[str, Optional[float]] = {tid: None for tid in tanks}
    trucks = {t["id"]: t for t in scenario["trucks"]}
    costs = scenario.get("costs", {})

    timed = sorted(actions, key=lambda a: a.get("at_min", 0))
    truck_windows: List[tuple] = []  # (start, end, tank, bbl_per_min, truck_id)
    truck_hours = 0.0
    for a in timed:
        if a["type"] == "SEND_TRUCK":
            tr = trucks[a["asset_id"]]
            for s, e in _truck_schedule(tr, a["target_id"], a.get("at_min", 0), horizon):
                truck_windows.append((s, e, a["target_id"], tr["capacity_bbl"] / (e - s), tr["id"]))
            truck_hours += max(0.0, horizon - a.get("at_min", 0)) / 60.0

    timeline = []
    events = []
    hauled_bbl = 0.0
    lost_usd = 0.0
    dt_hr = step / 60.0
    ai = 0
    t = 0
    while t <= horizon:
        # Apply actions that have taken effect.
        while ai < len(timed) and timed[ai].get("at_min", 0) <= t:
            a = timed[ai]
            if a["type"] == "SET_VALVE":
                valve_open[a["asset_id"]] = str(a["value"]).upper() == "OPEN"
            elif a["type"] == "SET_PUMP":
                pump_on[a["asset_id"]] = str(a["value"]).upper() == "ON"
            elif a["type"] == "CHOKE_WELL":
                well_keep[a["asset_id"]] = max(0.0, min(1.0, float(a["value"])))
            ai += 1

        online = sum(1 for w in wells if tank_shut[w["tank_id"]] is None and well_keep[w["id"]] > 0)
        timeline.append({
            "t_min": t,
            "tanks": {tid: round(100.0 * level[tid] / cap[tid], 2) for tid in tanks},
            "wells_online": online,
        })
        if t == horizon:
            break

        net = {tid: 0.0 for tid in tanks}
        # Wells.
        for w in wells:
            if tank_shut[w["tank_id"]] is not None:
                lost_usd += w["lost_usd_hr"] * dt_hr
                continue
            keep = well_keep[w["id"]]
            net[w["tank_id"]] += w["water_bbl_hr"] * keep * inflow_scale
            lost_usd += w["lost_usd_hr"] * (1.0 - keep) * dt_hr
        # Crossovers.
        for vid, v in valves.items():
            if not valve_open[vid]:
                continue
            a_id, b_id = v["connects"]
            gap = level[a_id] - level[b_id]
            q = max(-v["max_bbl_hr"], min(v["max_bbl_hr"], XO_GAIN_PER_HR * gap))
            net[a_id] -= q
            net[b_id] += q
        # Pumps hold their tank at setpoint.
        for pid, p in pumps.items():
            tid = p["tank_id"]
            if not pump_on[pid]:
                continue
            available = max(0.0, net[tid] + (level[tid] - setpoint[tid]) / dt_hr)
            net[tid] -= min(p["rate_bbl_hr"], available)
        # Trucks.
        for s, e, tid, per_min, _ in truck_windows:
            overlap = max(0.0, min(e, t + step) - max(s, t))
            if overlap > 0:
                take = min(per_min * overlap, max(0.0, level[tid] + net[tid] * dt_hr - 0.05 * cap[tid]))
                net[tid] -= take / dt_hr
                hauled_bbl += take

        for tid in tanks:
            level[tid] = max(0.0, min(cap[tid], level[tid] + net[tid] * dt_hr))
            if tank_shut[tid] is None and level[tid] >= shut_bbl[tid]:
                # Interpolate the crossing inside the step.
                prev = level[tid] - net[tid] * dt_hr
                frac = (shut_bbl[tid] - prev) / (level[tid] - prev) if level[tid] != prev else 1.0
                t_hit = t + step * max(0.0, min(1.0, frac))
                tank_shut[tid] = t_hit
                n_wells = sum(1 for w in wells if w["tank_id"] == tid)
                lost_usd += n_wells * costs.get("well_restart_usd", 0)
                events.append({
                    "type": "SHUTDOWN_LEVEL",
                    "asset_id": tid,
                    "t_min": round(t_hit, 1),
                    "wells_lost": n_wells,
                    "text": f"{tid.replace('_', ' ').title()} reaches shutdown at {clock_text(scenario, t_hit)}",
                })
        t += step

    trucking = truck_hours * costs.get("truck_usd_hr", 0)
    disposal = hauled_bbl * costs.get("disposal_usd_bbl", 0)
    return {
        "timeline": timeline,
        "events": events,
        "cost": {
            "total_usd": round(trucking + disposal + lost_usd),
            "trucking_usd": round(trucking),
            "disposal_usd": round(disposal),
            "lost_production_usd": round(lost_usd),
        },
        "warnings": [],
    }


def forecast(scenario: dict, actions: List[dict], horizon_minutes: int = 360,
             step_minutes: int = 5, inflow_scale: float = 1.0, with_range: bool = False) -> dict:
    """Contract-shaped forecast. with_range adds t_min_low / t_min_high at +/-15% inflow."""
    out = _run(scenario, actions, horizon_minutes, step_minutes, inflow_scale)
    if with_range:
        hi = {e["asset_id"]: e["t_min"] for e in _run(scenario, actions, horizon_minutes, step_minutes, inflow_scale * 1.15)["events"]}
        lo = {e["asset_id"]: e["t_min"] for e in _run(scenario, actions, horizon_minutes, step_minutes, inflow_scale * 0.85)["events"]}
        for e in out["events"]:
            e["t_min_low"] = hi.get(e["asset_id"], e["t_min"])
            e["t_min_high"] = lo.get(e["asset_id"])  # None means it never hits at the low inflow
    return out
