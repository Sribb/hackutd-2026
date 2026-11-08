"""Last Call: count down to the operator's options, not to the failure.

The forecast answers "when does Tank 3 shut down?". Last Call answers the
question a night-shift operator actually has to decide on: "how long can I
think before each fix stops working, and what does each minute of waiting
cost?"

Every fix has a lead time. A manual crossover valve needs a rover to drive to
the pad. A truck needs to be called, then drive. A choke is remote and almost
instant but burns production. As the clock runs, the cheap slow fixes expire
first and only the fast expensive ones are left. Last Call finds those
expiry times by replaying the forecast with each plan started later and later.

Inputs: a scenario (the /api/field + /api/state data) and, optionally, any
forecast function in the docs/CONTRACT.md shape. Output: the /api/lastcall
response described in docs/CONTRACT.md.
"""

from __future__ import annotations

import itertools
import json
import time
from typing import Callable, Dict, List, Optional

from . import sim

ForecastFn = Callable[[List[dict], float], dict]  # (actions, inflow_scale) -> contract forecast

HOT_INFLOW = 1.15  # same +15% the backend uses for t_min_low


# ---------------------------------------------------------------- plans ----

def _lead(scenario: dict, action_type: str, manual: bool = False) -> int:
    leads = scenario.get("lead_min", {})
    if action_type == "SET_VALVE":
        return int(leads.get("SET_VALVE_manual" if manual else "SET_VALVE_remote", 0))
    return int(leads.get(action_type, 0))


def _subset(scenario: dict, tank_ids: set) -> dict:
    """Keep only the tanks that can trade water with the at-risk tanks. Speeds up replays."""
    reach = set(tank_ids)
    changed = True
    while changed:
        changed = False
        for v in scenario["valves"]:
            a, b = v["connects"]
            if (a in reach) != (b in reach):
                reach |= {a, b}
                changed = True
    out = dict(scenario)
    out["tanks"] = [t for t in scenario["tanks"] if t["id"] in reach]
    out["pumps"] = [p for p in scenario["pumps"] if p["tank_id"] in reach]
    out["wells"] = [w for w in scenario["wells"] if w["tank_id"] in reach]
    out["valves"] = [v for v in scenario["valves"] if set(v["connects"]) <= reach]
    return out


def _known(scenario: dict, unknown: set) -> dict:
    """Drop tanks with no level reading, plus their pumps, wells and crossovers.
    The reference sim can't move a level it doesn't have."""
    out = dict(scenario)
    out["tanks"] = [t for t in scenario["tanks"] if t["id"] not in unknown]
    out["pumps"] = [p for p in scenario["pumps"] if p["tank_id"] not in unknown]
    out["wells"] = [w for w in scenario["wells"] if w["tank_id"] not in unknown]
    out["valves"] = [v for v in scenario["valves"] if not (set(v.get("connects", [])) & unknown)]
    return out


def _unknown_text(unknown: List[str]) -> str:
    return (f"No level reading for {_sentence([_name(t) for t in unknown])[:-1]}, "
            "so Last Call can't vouch for them. Check them in person.")


def candidate_plans(scenario: dict, at_risk: List[str]) -> List[dict]:
    """Build plan templates. Each action carries lead_min instead of at_min.

    Levers per at-risk tank: open each crossover touching it, send 0/1/2 trucks
    (fastest first), and choke its wells, cheapest dollars-per-barrel first.
    """
    plans = []
    for tid in at_risk:
        valves = [v for v in scenario["valves"] if tid in v["connects"] and v.get("position") != "OPEN"]
        trucks = sorted((t for t in scenario["trucks"] if tid in t["minutes_to"]),
                        key=lambda t: t["minutes_to"][tid])
        wells = sorted((w for w in scenario["wells"] if w["tank_id"] == tid),
                       key=lambda w: w["lost_usd_hr"] / max(w["water_bbl_hr"], 1e-9))

        valve_opts = [[]] + [[v] for v in valves]
        truck_opts = [trucks[:k] for k in range(len(trucks) + 1)]
        choke_opts = [(0, 1.0)] + [(k, keep) for k in range(1, len(wells) + 1) for keep in (0.5, 0.0)]

        for vs, ts, (nk, keep) in itertools.product(valve_opts, truck_opts, choke_opts):
            actions, words = [], []
            for v in vs:
                actions.append({"type": "SET_VALVE", "asset_id": v["id"], "value": "OPEN",
                                "lead_min": _lead(scenario, "SET_VALVE", v.get("manual", False))})
                words.append(f"open the {_name(v['id'])}")
            for tr in ts:
                actions.append({"type": "SEND_TRUCK", "asset_id": tr["id"], "target_id": tid,
                                "lead_min": _lead(scenario, "SEND_TRUCK")})
            if ts:
                words.append(f"send {_count(len(ts), 'truck')} to {_name(tid)}")
            for w in wells[:nk]:
                actions.append({"type": "CHOKE_WELL", "asset_id": w["id"], "value": keep,
                                "lead_min": _lead(scenario, "CHOKE_WELL")})
            if nk:
                how = "shut in" if keep == 0.0 else "choke to half"
                words.append(f"{how} {_count(nk, 'well')} on {_name(tid)}")
            summary = ("Do nothing" if not words else _sentence(words))
            plans.append({"actions": actions, "summary": summary, "tank": tid})
    for i, p in enumerate(plans):
        p["id"] = f"lc_{i}"
    return plans


def _name(asset_id: str) -> str:
    if asset_id.startswith("VALVE_XO_"):
        a, b = asset_id[len("VALVE_XO_"):]
        return f"Tank {a} to {b} crossover"
    return asset_id.replace("_", " ").title()


def _count(n: int, noun: str) -> str:
    words = {1: "one", 2: "two", 3: "three", 4: "four"}
    return f"{words.get(n, n)} {noun}{'' if n == 1 else 's'}"


def _sentence(parts: List[str]) -> str:
    s = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return s[0].upper() + s[1:] + "."


def schedule(plan: dict, start_min: float, delay: Optional[Dict[int, float]] = None) -> List[dict]:
    """Turn a template into contract actions, as if the operator decides at start_min."""
    out = []
    for i, a in enumerate(plan["actions"]):
        b = {k: v for k, v in a.items() if k != "lead_min"}
        b["at_min"] = start_min + a["lead_min"] + (delay or {}).get(i, 0)
        out.append(b)
    return out


# --------------------------------------------------------------- search ----

def _keeps_running(fc: dict) -> bool:
    return not any(e["type"] == "SHUTDOWN_LEVEL" for e in fc["events"])


def _last_ok(test: Callable[[float], bool], hi: float, res: float = 1.0) -> Optional[float]:
    """Largest t in [0, hi] where test(t) holds, assuming it holds then stops. None if never."""
    if not test(0):
        return None
    if test(hi):
        return hi
    lo = 0.0
    while hi - lo > res:
        mid = (lo + hi) / 2
        if test(mid):
            lo = mid
        else:
            hi = mid
    return round(lo, 1)


def last_call(scenario: dict, forecast_fn: Optional[ForecastFn] = None,
              horizon_minutes: int = 360, step_minutes: int = 5, top_n: int = 3) -> dict:
    t_start = time.perf_counter()

    # A tank with no reading is never counted as safe. The reference sim drops it;
    # a backend forecast handles it itself. Either way it is named in the answer.
    unknown = sorted(t["id"] for t in scenario["tanks"] if t.get("level_pct") is None)
    if unknown and forecast_fn is None:
        scenario = _known(scenario, set(unknown))
    if unknown and not scenario["tanks"]:
        return {"status": "UNKNOWN", "clock": scenario.get("clock"), "text": _unknown_text(unknown),
                "unknown_tanks": unknown, "options": [], "curve": [], "cliffs": [], "assets": {},
                "do_nothing": None, "warnings": [_unknown_text(unknown)]}

    full_fc = (lambda acts, scale=1.0: sim.forecast(scenario, acts, horizon_minutes, step_minutes, scale))
    baseline = (forecast_fn or full_fc)([], 1.0)
    shutdowns = [e for e in baseline["events"] if e["type"] == "SHUTDOWN_LEVEL"
                 and e.get("asset_id") not in unknown]
    if not shutdowns:
        hours = horizon_minutes // 60
        if unknown:
            return {"status": "UNKNOWN", "clock": scenario.get("clock"),
                    "text": f"No tank with a reading reaches shutdown in the next {hours} hours with no action. "
                            + _unknown_text(unknown),
                    "unknown_tanks": unknown, "options": [], "curve": [], "cliffs": [], "assets": {},
                    "do_nothing": _plan_out_cost(baseline), "warnings": [_unknown_text(unknown)]}
        return {"status": "CALM", "text": "No tank reaches shutdown in the next "
                f"{hours} hours with no action.", "options": [], "curve": [], "cliffs": [],
                "assets": {}, "unknown_tanks": [], "do_nothing": _plan_out_cost(baseline)}

    at_risk = sorted({e["asset_id"] for e in shutdowns})
    first_hit = min(e["t_min"] for e in shutdowns)

    if forecast_fn is None:
        small = _subset(scenario, set(at_risk))
        fc = lambda acts, scale=1.0: sim.forecast(small, acts, horizon_minutes, step_minutes, scale)
    else:
        fc = forecast_fn

    plans = candidate_plans(scenario, at_risk)

    # 1. When does each plan stop keeping every tank running?
    for p in plans:
        p["expires_min"] = _last_ok(lambda t: _keeps_running(fc(schedule(p, t), 1.0)), first_hit)
        p["expires_min_hot"] = _last_ok(lambda t: _keeps_running(fc(schedule(p, t), HOT_INFLOW)), first_hit)
        p["cost_now"] = fc(schedule(p, 0), 1.0)["cost"]

    alive = [p for p in plans if p["expires_min"] is not None]
    point_of_no_return = max((p["expires_min"] for p in alive), default=None)

    # 2. Cheapest plan for each decision minute, and the cliffs where it jumps.
    def best_at(t: float) -> Optional[dict]:
        live = [p for p in alive if p["expires_min"] >= t]
        if not live:
            return None
        # Cost moves a little with the start time (fewer truck hours, later chokes), so re-price.
        priced = [(fc(schedule(p, t), 1.0)["cost"]["total_usd"], p) for p in live]
        return min(priced, key=lambda x: x[0])

    # Do nothing from here on: the cost of letting it shut down.
    nothing_cost = baseline["cost"]["total_usd"]

    probe = sorted({0.0} | {p["expires_min"] for p in alive} | {p["expires_min"] + 1 for p in alive}
                   | set(range(0, int(first_hit) + 1, 5)))
    probe = sorted({round(t, 1) for t in probe if t <= first_hit})
    curve, prev_id, cliffs = [], None, []
    for t in probe:
        b = best_at(t)
        if b is None:
            point = {"start_min": t, "clock": sim.clock_text(scenario, t), "total_usd": nothing_cost,
                     "plan_id": None, "keeps_running": False}
        else:
            point = {"start_min": t, "clock": sim.clock_text(scenario, t), "total_usd": b[0],
                     "plan_id": b[1]["id"], "keeps_running": True}
        if curve and point["plan_id"] != prev_id:
            before = curve[-1]
            jump = point["total_usd"] - before["total_usd"]
            if point["plan_id"] is None:
                text = (f"After {sim.clock_text(scenario, before['start_min'])} nothing keeps every well "
                        f"online. Shutdown costs ${point['total_usd']:,}.")
            else:
                gone = next(p for p in plans if p["id"] == before["plan_id"])
                text = (f"After {sim.clock_text(scenario, gone['expires_min'])}, \"{gone['summary']}\" stops working. "
                        f"Cheapest plan goes from ${before['total_usd']:,} to ${point['total_usd']:,}.")
            cliffs.append({"t_min": before["start_min"] if point["plan_id"] is None else gone["expires_min"],
                           "clock": sim.clock_text(scenario, before["start_min"] if point["plan_id"] is None else gone["expires_min"]),
                           "from_usd": before["total_usd"], "to_usd": point["total_usd"],
                           "jump_usd": jump, "text": text})
        prev_id = point["plan_id"]
        curve.append(point)

    # 3. Ranked options right now, with an expiry and a do-by time for every step.
    ranked = sorted(alive, key=lambda p: p["cost_now"]["total_usd"])
    options = []
    seen = set()
    for p in ranked:
        if p["summary"] in seen:
            continue
        seen.add(p["summary"])
        steps = _steps(p, scenario)
        options.append({
            "plan_id": p["id"], "summary": p["summary"],
            "cost": p["cost_now"],
            "expires_min": p["expires_min"], "expires_clock": sim.clock_text(scenario, p["expires_min"]),
            "expires_min_hot": p["expires_min_hot"],
            "robust": p["expires_min_hot"] is not None,
            "expires_clock_hot": None if p["expires_min_hot"] is None else sim.clock_text(scenario, p["expires_min_hot"]),
            "steps": steps,
        })
        if len(options) >= top_n:
            break

    # 4. Per asset: the last minute that asset is still part of any plan that works. Drives the 3D fuses.
    assets: Dict[str, dict] = {}
    for p in alive:
        for a in p["actions"]:
            cur = assets.setdefault(a["asset_id"], {"useful_until_min": -1})
            cur["useful_until_min"] = max(cur["useful_until_min"], p["expires_min"])
    by_id = {p["id"]: p for p in plans}
    for pt in curve:
        if pt["plan_id"] is None:
            continue
        for a in by_id[pt["plan_id"]]["actions"]:
            cur = assets[a["asset_id"]]
            cur.setdefault("in_cheapest_from_min", pt["start_min"])
            cur["in_cheapest_until_min"] = pt["start_min"]
    for aid, v in assets.items():
        v["useful_until_clock"] = sim.clock_text(scenario, v["useful_until_min"])
    for p in plans:
        for a in p["actions"]:
            assets.setdefault(a["asset_id"], {"useful_until_min": None, "useful_until_clock": None})

    # The ladder: each plan that is the cheapest at some minute, in the order they die.
    ladder = []
    for pt in curve:
        if pt["plan_id"] and (not ladder or ladder[-1]["plan_id"] != pt["plan_id"]):
            lp = by_id[pt["plan_id"]]
            ladder.append({"plan_id": lp["id"], "summary": lp["summary"],
                           "from_min": pt["start_min"], "expires_min": lp["expires_min"],
                           "expires_clock": sim.clock_text(scenario, lp["expires_min"]),
                           "expires_min_hot": lp["expires_min_hot"],
                           "usd_at_start": pt["total_usd"],
                           "asset_ids": [a["asset_id"] for a in lp["actions"]],
                           "steps": _steps(lp, scenario)})

    best = options[0] if options else None
    next_cliff = cliffs[0] if cliffs else None
    if best:
        headline = f"Your cheapest plan expires at {best['expires_clock']}."
        if next_cliff:
            headline += (f" Wait past it and the bill goes from ${best['cost']['total_usd']:,} "
                         f"to ${next_cliff['to_usd']:,}.")
        if not best["robust"]:
            backup = next((o for o in options if o["robust"]), None)
            headline += " It fails now if inflow runs 15% hot."
            if backup:
                headline += (f" \"{backup['summary']}\" holds either way until "
                             f"{backup['expires_clock_hot']}.")
    else:
        headline = "No plan keeps every well online. Pick the least damage."

    warnings = ["Costs and lead times are placeholders until the mentors confirm them."]
    if unknown:
        headline += " " + _unknown_text(unknown)
        warnings.insert(0, _unknown_text(unknown))

    return {
        "status": "INCIDENT",
        "clock": scenario.get("clock"),
        "headline": headline,
        "shutdown": {"asset_id": shutdowns[0]["asset_id"], "t_min": first_hit,
                     "clock": sim.clock_text(scenario, first_hit)},
        "point_of_no_return": None if point_of_no_return is None else {
            "t_min": point_of_no_return, "clock": sim.clock_text(scenario, point_of_no_return),
            "text": f"Last moment any plan keeps every well online: {sim.clock_text(scenario, point_of_no_return)}."},
        "do_nothing_usd": nothing_cost,
        "options": options,
        "curve": curve,
        "cliffs": cliffs,
        "ladder": ladder,
        "assets": assets,
        "plans_tested": len(plans),
        "compute_ms": round(1000 * (time.perf_counter() - t_start)),
        "unknown_tanks": unknown,
        "warnings": warnings,
    }


def _steps(p: dict, scenario: dict) -> List[dict]:
    """Every step shifts together, so each do-by time is the plan's expiry.
    Longest lead goes first: the rover has to leave before the truck call matters."""
    out = []
    for a in sorted(p["actions"], key=lambda a: -a["lead_min"]):
        do_by = p["expires_min"]
        out.append({
            "n": len(out) + 1,
            "asset_id": a["asset_id"], "type": a["type"],
            "lead_min": a["lead_min"],
            "do_by_min": do_by,
            "do_by_clock": sim.clock_text(scenario, do_by),
            "text": _step_text(a, do_by, scenario),
        })
    return out


def _plan_out_cost(fc: dict) -> int:
    return fc["cost"]["total_usd"]


def _step_text(a: dict, do_by: float, scenario: dict) -> str:
    when = sim.clock_text(scenario, do_by)
    if a["type"] == "SET_VALVE":
        return f"Send the rover to open the {_name(a['asset_id'])} by {when}. Drive and open takes {a['lead_min']} min."
    if a["type"] == "SEND_TRUCK":
        return f"Dispatch {_name(a['asset_id'])} to {_name(a['target_id'])} by {when}."
    if a["type"] == "CHOKE_WELL":
        how = "Shut in" if a["value"] == 0 else f"Choke to {int(a['value'] * 100)}%"
        return f"{how} {_name(a['asset_id'])} by {when}."
    return f"{a['type']} {a['asset_id']} by {when}."


def load_scenario(path: str) -> dict:
    """Load lastcall/scenario.json, or scenario/field.json (Vinny's lane) adapted to the same shape."""
    with open(path) as f:
        data = json.load(f)
    return from_field_json(data) if "initial" in data else data


def from_field_json(fj: dict) -> dict:
    """Adapt scenario/field.json to the shape sim.py reads, at the moment of the first fault.

    field.json starts its clock before the failure (clock_start) and lists faults with
    at_min. Every pump runs normally until then, draining its tank toward pump_low_pct,
    so Last Call replays those minutes first and starts at the fault: tanks at the
    replayed levels, faulted pumps tripped, valves at their default positions.
    """
    faults = fj.get("faults", [])
    at = min((f.get("at_min", 0) for f in faults), default=0)
    h, m = (int(x) for x in fj.get("clock_start", "00:00").split(":"))
    clock = "{:02d}:{:02d}".format(*divmod((h * 60 + m + int(at)) % 1440, 60))
    levels = fj.get("initial", {}).get("tank_pct", {})
    tripped = {f["asset_id"]: f.get("reason", "Tripped.") for f in faults if f.get("type") == "TRIP"}
    tanks = []
    for t in fj["tanks"]:
        lvl = levels.get(t["id"])
        if lvl is None:
            raise ValueError(f"{t['id']} has no initial level. Last Call never assumes a tank is safe.")
        low = t.get("setpoint_pct", t.get("pump_low_pct", lvl))
        tanks.append({**t, "level_pct": lvl, "setpoint_pct": low})
    valves = [{**v, "position": v.get("position", v.get("default", "CLOSED")),
               "max_bbl_hr": v.get("max_bbl_hr", 0)} for v in fj["valves"]]
    trucks = [{**t, "load_min": t.get("load_min", t.get("load_minutes", 20)),
               "haul_to_swd_min": t.get("haul_to_swd_min", t.get("minutes_to_disposal", 30) + t.get("unload_minutes", 0))}
              for t in fj["trucks"]]
    healthy = [{**p, "status": "OK"} for p in fj["pumps"]]
    out = {**fj, "clock": clock, "tanks": tanks, "pumps": healthy, "valves": valves, "trucks": trucks}
    if at > 0:
        # Replay the minutes before the fault with every pump running.
        pre = sim.forecast({**out, "clock": fj.get("clock_start", "00:00")}, [], int(at), 1)
        last = pre["timeline"][-1]["tanks"]
        out["tanks"] = [{**t, "level_pct": last[t["id"]]} for t in tanks]
    out["pumps"] = [{**p, "status": "ALARM" if p["id"] in tripped else "OK",
                     **({"reason": tripped[p["id"]]} if p["id"] in tripped else {})} for p in fj["pumps"]]
    return out
