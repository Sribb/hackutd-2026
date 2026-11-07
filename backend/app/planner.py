"""Planner: tries combinations of valves, trucks, and choked wells, runs each through
the same forecast, and ranks them by dollars (BACKEND.md step 6).

Replaces SnitchTheWitch's OR-Tools planner (it crashed in our tests) with a small
enumeration that always returns something, even when not every tank can be saved.
Choke order borrows the 2021 EOG optimizer idea: cut water where it loses the least
revenue per barrel.
"""
from __future__ import annotations

import itertools
from collections import Counter

from .field import Field, nice_name
from .sim import Plant, forecast

MAX_TRIPS = 3
DEFAULT_LEAD = {"SET_VALVE_manual": 20, "SET_VALVE_remote": 1, "CHOKE_WELL": 2, "SEND_TRUCK": 5, "SET_PUMP": 1}


def lead(f: Field, action_type: str, asset_id: str | None = None) -> int:
    """Minutes until an action takes effect (scenario lead_min, shared with lastcall/)."""
    table = {**DEFAULT_LEAD, **{k: v for k, v in f.raw.get("lead_min", {}).items() if not k.startswith("_")}}
    if action_type == "SET_VALVE":
        manual = bool(f.valves.get(asset_id or "", {}).get("manual", False))
        return int(table["SET_VALVE_manual" if manual else "SET_VALVE_remote"])
    return int(table.get(action_type, 0))


def _at_risk(plant: Plant, horizon: int) -> list[str]:
    base = forecast(plant, [], horizon=horizon, record=False)
    order: list[str] = []
    for e in base["events"]:
        if e["type"] in ("SHUTDOWN_LEVEL", "SHUTDOWN_POSSIBLE") and e["asset_id"] not in order:
            order.append(e["asset_id"])
    return order


def _candidates(f: Field, risk: list[str]) -> list[list[dict]]:
    if not risk:
        return [[]]
    target = risk[0]
    xo_opts: list[list[dict]] = [[]]
    neighbors: list[str] = []
    for xo in f.crossovers:
        if any(t in xo["connects"] for t in risk):
            xo_opts.append([{"type": "SET_VALVE", "asset_id": xo["id"], "value": "OPEN", "at_min": lead(f, "SET_VALVE", xo["id"])}])
            neighbors += [t for t in xo["connects"] if t not in risk]

    trucks = sorted(f.trucks, key=lambda tid: f.trucks[tid]["minutes_to"].get(target, 1e9))
    trip_sets: list[tuple[str, ...]] = [()]
    for n in range(1, MAX_TRIPS + 1):
        trip_sets += list(itertools.combinations_with_replacement(trucks, n))

    def choke_opts(tanks: list[str]) -> list[list[dict]]:
        wells = [w for t in tanks for w in f.wells_by_tank[t]]
        wells.sort(key=lambda w: f.wells[w]["lost_usd_hr"] / f.wells[w]["water_bbl_hr"])
        opts: list[list[dict]] = [[]]
        for k in (1, 2):
            for frac in (0.5, 0.0):
                if len(wells) >= k:
                    opts.append([{"type": "CHOKE_WELL", "asset_id": w, "value": frac,
                                  "at_min": lead(f, "CHOKE_WELL")} for w in wells[:k]])
        return opts

    out: list[list[dict]] = []
    for xo in xo_opts:
        chokes = choke_opts(risk + (neighbors if xo else []))
        for trips in trip_sets:
            truck_actions = [{"type": "SEND_TRUCK", "asset_id": tid, "target_id": target, "at_min": lead(f, "SEND_TRUCK")} for tid in trips]
            for ch in chokes:
                out.append(xo + truck_actions + ch)
    return out


def describe(f: Field, actions: list[dict]) -> str:
    if not actions:
        return "No action needed. Keep watching."
    parts: list[str] = []
    for a in actions:
        if a["type"] == "SET_VALVE":
            verb = "Open" if str(a["value"]).upper() == "OPEN" else "Close"
            parts.append(f"{verb} {nice_name(a['asset_id'])}")
    trips = Counter((a["asset_id"], a["target_id"]) for a in actions if a["type"] == "SEND_TRUCK")
    if trips:
        by_target: dict[str, list[str]] = {}
        for (tid, target), n in trips.items():
            by_target.setdefault(target, []).append(nice_name(tid) + (" twice" if n == 2 else f" {n} times" if n > 2 else ""))
        for target, names in by_target.items():
            parts.append(f"send {' and '.join(names)} to {nice_name(target)}")
    for a in actions:
        if a["type"] == "CHOKE_WELL":
            v = float(a["value"])
            parts.append(f"shut in {nice_name(a['asset_id'])}" if v == 0 else f"choke {nice_name(a['asset_id'])} to {v:.0%}")
        if a["type"] == "SET_PUMP":
            parts.append(f"turn {nice_name(a['asset_id'])} {str(a['value']).lower()}")
    text = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
    return text[0].upper() + text[1:] + "."


def make_plans(plant: Plant, horizon: int = 360, limit: int = 3) -> dict:
    f = plant.f
    risk = _at_risk(plant, horizon)
    scored = []
    seen = set()
    for acts in _candidates(f, risk):
        r = forecast(plant, acts, horizon=horizon, with_range=False, record=False)
        key = (round(r["cost"]["total_usd"], 0), tuple(r["summary"]["tanks_lost"]))
        if key in seen:
            continue
        seen.add(key)
        first = r["summary"]["first_shutdown"]
        scored.append((acts, r, first["t_min"] if first else horizon + 1))

    def rank(item):
        acts, r, first_t = item
        return (len(r["summary"]["tanks_lost"]), -first_t if r["summary"]["tanks_lost"] else 0,
                r["cost"]["total_usd"], len(acts))

    scored.sort(key=rank)
    top = scored[:limit]
    # Make sure one returned plan holds even if inflow runs 15% high.
    for item in scored:
        full = forecast(plant, item[0], horizon=horizon, record=False)
        if full["summary"]["robust"]:
            if all(item is not t for t in top):
                top = top[: limit - 1] + [item]
            break

    plans = []
    for i, (acts, _, _) in enumerate(top, start=1):
        full = forecast(plant, acts, horizon=horizon, record=False)
        s = full["summary"]
        notes = []
        if s["keeps_running"] and not s["robust"]:
            notes.append("Holds at forecast inflow, but not if inflow runs 15% high.")
        if s["robust"]:
            notes.append("Holds even if inflow runs 15% high.")
        plans.append({
            "id": f"plan_{i}",
            "summary": describe(f, acts),
            "actions": acts,
            "cost": full["cost"],
            "keeps_running": s["keeps_running"],
            "robust": s["robust"],
            "cannot_save": s["tanks_lost"],
            "wells_lost": s["wells_lost"],
            "first_shutdown": s["first_shutdown"],
            "events": [e for e in full["events"] if e["type"] in ("SHUTDOWN_LEVEL", "SHUTDOWN_POSSIBLE", "TRUCK_ARRIVES")],
            "notes": notes,
        })
    if plans:
        plans[0]["recommended"] = True
    return {
        "made_at": {"t": round(plant.t, 2), "clock": f.clock_at(plant.t)},
        "at_risk": risk,
        "searched": len(scored),
        "plans": plans,
    }


def approve_steps(f: Field, plan: dict, made_at_t: float, incident: dict | None) -> dict:
    """POST /api/plans/{id}/approve: numbered steps the 3D scene walks through."""
    arrivals: dict[str, list[float]] = {}
    for e in plan.get("events", []):
        if e["type"] == "TRUCK_ARRIVES":
            arrivals.setdefault(e["asset_id"], []).append(e["t_min"])
    steps = []
    prio = {"SET_VALVE": 0, "CHOKE_WELL": 1, "SET_PUMP": 2, "SEND_TRUCK": 3}
    ordered = sorted(plan["actions"], key=lambda a: (a.get("at_min", 0), prio.get(a["type"], 9)))
    for n, a in enumerate(ordered, start=1):
        when = f.clock_text(made_at_t + a.get("at_min", 0))
        aid = a["asset_id"]
        if a["type"] == "SET_VALVE":
            verb = "Open" if str(a["value"]).upper() == "OPEN" else "Close"
            text = f"{verb} {nice_name(aid)} by {when}."
        elif a["type"] == "SEND_TRUCK":
            eta = arrivals.get(aid, [])
            arr = eta.pop(0) if eta else None
            text = f"Send {nice_name(aid)} to {nice_name(a['target_id'])}."
            if arr is not None:
                text += f" Arrives in {arr:.0f} minutes ({f.clock_text(made_at_t + arr)})."
        elif a["type"] == "CHOKE_WELL":
            v = float(a["value"])
            text = (f"Shut in {nice_name(aid)} by {when}." if v == 0
                    else f"Choke {nice_name(aid)} to {v:.0%} by {when}.")
        else:
            text = f"Turn {nice_name(aid)} {str(a['value']).lower()} by {when}."
        steps.append({"n": n, "asset_id": aid, "text": text, "action": a,
                      "due_t": made_at_t + a.get("at_min", 0), "status": "PENDING"})

    lead = incident["summary"] if incident else "We have a developing problem."
    sd = plan.get("first_shutdown")
    risk_line = f" Without action, {sd['text'].lower()}." if sd and not plan["keeps_running"] else ""
    lines = [f"Field, this is control. {lead}{risk_line}", "Here's the plan."]
    for s in steps:
        lines.append(f"Step {s['n']}: {s['text']}")
    lines.append("Call back when each step is done. Control out.")
    return {"plan_id": plan["id"], "steps": steps, "radio_script": " ".join(lines)}
