"""Cheapest plans: try combinations of levers, keep the ones that keep every tank safe.

Levers around each tank at risk (one that reaches shutdown, or would at +15% inflow):
- each closed crossover on that tank: leave it, or open it
- trucks that can reach it: none, the fastest one, or the two fastest
- its wells, cheapest dollars lost per barrel of water first: none, choke the
  cheapest to 50%, shut in the cheapest one, two, or three

Each action starts after its lead time (lead_min), so a manual valve opens later
than a remote one. A plan keeps running only if no tank reaches shutdown in the
horizon. `robust` says whether it still holds with inflow 15% high.
"""
from __future__ import annotations

import hashlib
import itertools
import json

from .forecast import RANGE, build_plant, clock_at, forecast, nice, simulate, clean_actions

MAX_CANDIDATES = 600


def _lead(field: dict, plant, a: dict) -> float:
    leads = field.get("lead_min", {})
    if a["type"] == "SET_VALVE":
        manual = plant.valves[a["asset_id"]].get("manual", False)
        return float(leads.get("SET_VALVE_manual" if manual else "SET_VALVE_remote", 0))
    return float(leads.get(a["type"], 0))


def plan_id(actions: list[dict]) -> str:
    key = json.dumps(sorted((a["type"], a["asset_id"], str(a.get("value")), a.get("target_id") or "")
                            for a in actions))
    return "plan_" + hashlib.sha1(key.encode()).hexdigest()[:8]


def describe(actions: list[dict]) -> str:
    if not actions:
        return "Do nothing."
    parts = []
    for a in actions:
        if a["type"] == "SET_VALVE":
            parts.append(f"{'open' if a['value'] == 'OPEN' else 'close'} {nice(a['asset_id'])}")
        elif a["type"] == "CHOKE_WELL":
            parts.append(f"shut in {nice(a['asset_id'])}" if a["value"] == 0
                         else f"choke {nice(a['asset_id'])} to {int(round(a['value'] * 100))}%")
        elif a["type"] == "SEND_TRUCK":
            parts.append(f"send {nice(a['asset_id'])} to {nice(a['target_id'])}")
        elif a["type"] == "SET_PUMP":
            parts.append(f"turn {nice(a['asset_id'])} {a['value']}")
    text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + (", and " if len(parts) > 2 else " and ") + parts[-1]
    return text[0].upper() + text[1:] + "."


def _levers(plant, field: dict, tid: str) -> list[list[list[dict]]]:
    """Option lists for one tank. Each option is a list of actions (possibly empty)."""
    xo = [[]]
    for vid, v in plant.valves.items():
        ends = (v.get("connects") or [])[:2]
        if tid in ends and all(plant.levels.get(c) is not None for c in ends) \
                and plant.positions.get(vid) != "OPEN":
            xo.append([{"type": "SET_VALVE", "asset_id": vid, "value": "OPEN"}])
    trucks = sorted((t for t in plant.trucks.values() if tid in t.get("minutes_to", {})),
                    key=lambda t: t["minutes_to"][tid])
    truck_opts = [[]] + [[{"type": "SEND_TRUCK", "asset_id": t["id"], "target_id": tid}
                          for t in trucks[:k]] for k in (1, 2) if len(trucks) >= k]
    wells = sorted((w for w in plant.wells.values() if w["tank_id"] == tid and w["water_bbl_hr"] > 0),
                   key=lambda w: w["lost_usd_hr"] / w["water_bbl_hr"])
    choke_opts = [[]]
    if wells:
        choke_opts.append([{"type": "CHOKE_WELL", "asset_id": wells[0]["id"], "value": 0.5}])
    for k in (1, 2, 3):
        if len(wells) >= k:
            choke_opts.append([{"type": "CHOKE_WELL", "asset_id": w["id"], "value": 0} for w in wells[:k]])
    return [xo, truck_opts, choke_opts]


def make_plans(scenario: dict, horizon: int = 360, top_n: int = 3) -> dict:
    """POST /api/plans: cheapest first, always at least one plan."""
    field = scenario["field"]
    plant = build_plant(scenario)
    base = simulate(plant, [], horizon)
    hot = simulate(plant, [], horizon, 1 + RANGE)
    at_risk = sorted(set(base.shutdowns) | set(hot.shutdowns))
    unknown = sorted(tid for tid, lv in plant.levels.items() if lv is None)

    if not at_risk:
        p = _plan(plant, field, [], base, robust=True, horizon=horizon)
        p["summary"] = "No tank reaches shutdown in this horizon. No action needed."
        if unknown:
            p["summary"] = ("No tank with a reading reaches shutdown in this horizon. "
                            f"No reading for {', '.join(nice(t) for t in unknown)}, so check it in person.")
        return {"plans": [p]}

    per_tank = [_levers(plant, field, tid) for tid in at_risk]
    lever_lists = [opts for tank in per_tank for opts in tank]
    candidates = []
    for combo in itertools.product(*lever_lists):
        acts = [a for opt in combo for a in opt]
        trucks = [a["asset_id"] for a in acts if a["type"] == "SEND_TRUCK"]
        if len(trucks) != len(set(trucks)):
            continue  # one truck can't be sent to two tanks at once
        uniq = {json.dumps(a, sort_keys=True): a for a in acts}
        candidates.append(list(uniq.values()))
        if len(candidates) >= MAX_CANDIDATES:
            break

    scored = []
    for acts in candidates:
        timed = [{**a, "at_min": _lead(field, plant, a)} for a in acts]
        clean, _ = clean_actions(plant, timed)
        run = simulate(plant, clean, horizon)
        scored.append((not run.shutdowns, run.cost["total_usd"], len(timed), timed, run))

    feasible = sorted((s for s in scored if s[0]), key=lambda s: (s[1], s[2]))
    picked, picked_keys = [], []
    for ok, cost, n, timed, run in feasible:
        keys = {plan_id([a]) for a in timed}
        if any(prev <= keys for prev in picked_keys):
            continue  # a cheaper picked plan already does this with fewer steps
        robust = not simulate(plant, clean_actions(plant, timed)[0], horizon, 1 + RANGE).shutdowns
        picked.append(_plan(plant, field, timed, run, robust, horizon))
        picked_keys.append(keys)
        if len(picked) >= top_n:
            break

    if len(picked) < top_n:
        failing = sorted((s for s in scored if not s[0]),
                         key=lambda s: (len(s[4].shutdowns), s[1], s[2]))
        for ok, cost, n, timed, run in failing[: top_n - len(picked)]:
            picked.append(_plan(plant, field, timed, run, robust=False, horizon=horizon))
    return {"plans": picked}


def _plan(plant, field, timed: list[dict], run, robust: bool, horizon: int) -> dict:
    cannot = sorted(run.shutdowns)
    unknown = sorted(t for t, lv in plant.levels.items() if lv is None)
    summary = describe(timed)
    if not cannot:
        which = "Every tank" if not unknown else "Every tank with a reading"
        summary += f" {which} stays below shutdown through {clock_at(plant.start_clock, horizon)}."
        if not robust:
            summary += f" Not enough if inflow runs {int(RANGE * 100)}% high."
    else:
        summary += " Can't save " + ", ".join(
            f"{nice(t)} (shutdown at {clock_at(plant.start_clock, run.shutdowns[t])})" for t in cannot) + "."
    if unknown:
        summary += f" No reading for {', '.join(nice(t) for t in unknown)}, so check it in person."
    return {
        "id": plan_id(timed),
        "summary": summary,
        "actions": timed,
        "cost": run.cost,
        "keeps_running": not cannot,
        "cannot_save": cannot,
        "robust": robust and not cannot and not unknown,
        "unknown_tanks": unknown,
    }


# ---------- approve ----------

def approve(scenario: dict, plan: dict) -> dict:
    """POST /api/plans/{id}/approve: numbered steps with asset IDs, and a radio script."""
    field = scenario["field"]
    plant = build_plant(scenario)
    clock = plant.start_clock
    order = sorted(plan["actions"], key=lambda a: -a.get("at_min", 0))  # longest lead goes first
    steps = []
    for n, a in enumerate(order, start=1):
        aid = a["asset_id"]
        if a["type"] == "SET_VALVE":
            verb = "open" if a["value"] == "OPEN" else "close"
            if plant.valves[aid].get("manual"):
                text = (f"Send the rover to {verb} {nice(aid)}. It's a manual valve, about "
                        f"{int(a.get('at_min', 0))} minutes out, so it takes effect around "
                        f"{clock_at(clock, a.get('at_min', 0))}.")
            else:
                text = f"{verb.capitalize()} {nice(aid)} from the console."
        elif a["type"] == "CHOKE_WELL":
            w = plant.wells[aid]
            if a["value"] == 0:
                text = (f"Shut in {nice(aid)} from the console. That removes {w['water_bbl_hr']:g} bbl/hr "
                        f"of water and costs about ${w['lost_usd_hr']:g}/hr in production.")
            else:
                pct = int(round(a["value"] * 100))
                text = (f"Choke {nice(aid)} to {pct}% from the console. That removes "
                        f"{w['water_bbl_hr'] * (1 - a['value']):g} bbl/hr of water.")
        elif a["type"] == "SEND_TRUCK":
            tr = plant.trucks[aid]
            arrive = a.get("at_min", 0) + tr["minutes_to"][a["target_id"]]
            text = (f"Send {nice(aid)} to {nice(a['target_id'])}. It arrives around "
                    f"{clock_at(clock, arrive)} and hauls up to {tr['capacity_bbl']:g} bbl.")
        else:
            text = f"Turn {nice(aid)} {a['value']}."
        steps.append({"n": n, "asset_id": aid, "text": text})

    base = forecast(scenario, [], with_range=False, plant=plant)
    first = next((e for e in base["events"] if e["type"] == "SHUTDOWN_LEVEL"), None)
    state_line = ""
    incident = scenario.get("failures", [])
    if incident:
        state_line = f"{incident[0]['summary']} "
    if first:
        state_line += f"Without action, {nice(first['asset_id'])} reaches shutdown at {clock_at(clock, first['t_min'])}. "
    numbered = " ".join(f"Step {s['n']}: {s['text']}" for s in steps) or "No steps."
    outcome = ("With this plan every tank stays below shutdown."
               if plan.get("keeps_running") else
               "This plan can't save " + ", ".join(nice(t) for t in plan.get("cannot_save", [])) + ".")
    radio = (f"Field, this is control. {state_line}We're running this plan. {numbered} {outcome} "
             "Call back when each step is done. Control out.")
    return {"plan_id": plan["id"], "steps": steps, "radio_script": radio}
