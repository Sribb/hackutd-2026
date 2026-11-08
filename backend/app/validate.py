"""Checks a loaded scenario (or blueprint) has every number the forecast needs.

`problems(scenario)` returns a list of plain-English problems, empty when the
field is usable. Nothing is defaulted silently: a missing capacity or rate is a
problem, because guessing it could make a tank look safe when it isn't.
Missing tank *levels* are not problems here: the forecast reports those tanks
as unknown, never safe.
"""
from __future__ import annotations

import re

ID = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _num(x, *, lo=None, hi=None, positive=False) -> bool:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    if positive and x <= 0:
        return False
    if lo is not None and x < lo:
        return False
    if hi is not None and x > hi:
        return False
    return True


def problems(scenario: dict) -> list[str]:
    out: list[str] = []
    f = scenario.get("field")
    if not isinstance(f, dict):
        return ["The scenario has no field definition."]

    def items(key):
        val = f.get(key)
        if not isinstance(val, list):
            out.append(f"`{key}` is missing or not a list.")
            return []
        return [x for x in val if isinstance(x, dict)]

    tanks, pumps, valves = items("tanks"), items("pumps"), items("valves")
    wells, swds, trucks = items("wells"), items("disposal_wells"), items("trucks")

    seen: set[str] = set()
    for group in (tanks, pumps, valves, wells, swds, trucks):
        for a in group:
            aid = a.get("id")
            if not isinstance(aid, str) or not ID.match(aid):
                out.append(f"Asset ID {aid!r} must be uppercase letters, digits, and underscores.")
            elif aid in seen:
                out.append(f"Asset ID {aid} is used twice.")
            else:
                seen.add(aid)

    tank_ids = {t.get("id") for t in tanks}
    pump_ids = {p.get("id") for p in pumps}
    swd_ids = {s.get("id") for s in swds}

    for t in tanks:
        tid = t.get("id")
        if not _num(t.get("capacity_bbl"), positive=True):
            out.append(f"{tid}: `capacity_bbl` must be a number above 0.")
        if not _num(t.get("shutdown_pct"), lo=0, hi=100) or t.get("shutdown_pct") == 0:
            out.append(f"{tid}: `shutdown_pct` must be a number from 1 to 100.")
        for opt in ("setpoint_pct", "pump_low_pct", "warning_pct"):
            if opt in t and not _num(t[opt], lo=0, hi=100):
                out.append(f"{tid}: `{opt}` must be a number from 0 to 100.")

    for p in pumps:
        pid = p.get("id")
        if p.get("tank_id") not in tank_ids:
            out.append(f"{pid}: `tank_id` {p.get('tank_id')!r} is not a tank in this field.")
        if not _num(p.get("rate_bbl_hr"), lo=0):
            out.append(f"{pid}: `rate_bbl_hr` must be a number, 0 or more.")

    for v in valves:
        vid, ends = v.get("id"), v.get("connects")
        if not isinstance(ends, list) or len(ends) < 2:
            out.append(f"{vid}: `connects` must list the two things it joins.")
            continue
        a, b = ends[0], ends[1]
        if a in tank_ids and b in tank_ids:
            if "max_bbl_hr" in v and not _num(v["max_bbl_hr"], positive=True):
                out.append(f"{vid}: `max_bbl_hr` must be a number above 0.")
        elif a in pump_ids and b in swd_ids:
            pass
        else:
            out.append(f"{vid}: `connects` {ends} must be two tanks (a crossover) "
                       "or a pump and a disposal well (a discharge valve).")
        if v.get("position", "CLOSED") not in ("OPEN", "CLOSED"):
            out.append(f"{vid}: `position` must be OPEN or CLOSED.")

    for w in wells:
        wid = w.get("id")
        if w.get("tank_id") not in tank_ids:
            out.append(f"{wid}: `tank_id` {w.get('tank_id')!r} is not a tank in this field.")
        if not _num(w.get("water_bbl_hr"), lo=0):
            out.append(f"{wid}: `water_bbl_hr` must be a number, 0 or more.")
        if not _num(w.get("lost_usd_hr"), lo=0):
            out.append(f"{wid}: `lost_usd_hr` must be a number, 0 or more.")

    for s in swds:
        if not _num(s.get("max_bbl_hr"), positive=True):
            out.append(f"{s.get('id')}: `max_bbl_hr` must be a number above 0.")

    for tr in trucks:
        trid = tr.get("id")
        if not _num(tr.get("capacity_bbl"), positive=True):
            out.append(f"{trid}: `capacity_bbl` must be a number above 0.")
        mt = tr.get("minutes_to")
        if not isinstance(mt, dict) or not mt:
            out.append(f"{trid}: `minutes_to` must map at least one tank to minutes.")
        else:
            for k, m in mt.items():
                if k not in tank_ids:
                    out.append(f"{trid}: `minutes_to` names {k!r}, which is not a tank in this field.")
                elif not _num(m, lo=0):
                    out.append(f"{trid}: minutes to {k} must be a number, 0 or more.")
        for opt in ("load_min", "haul_to_swd_min", "usd_hr"):
            if opt in tr and not _num(tr[opt], lo=0):
                out.append(f"{trid}: `{opt}` must be a number, 0 or more.")

    for k, lv in (scenario.get("initial_levels_pct") or {}).items():
        if k not in tank_ids:
            out.append(f"Starting level given for {k!r}, which is not a tank in this field.")
        elif lv is not None and not _num(lv, lo=0, hi=100):
            out.append(f"{k}: starting level must be 0-100, or left out if unknown.")

    for fl in scenario.get("failures") or []:
        if fl.get("asset_id") not in seen:
            out.append(f"Failure names {fl.get('asset_id')!r}, which is not an asset in this field.")

    for k, v in (f.get("costs") or {}).items():
        if not k.startswith("_") and not _num(v, lo=0):
            out.append(f"Cost `{k}` must be a number, 0 or more.")
    for k, v in (f.get("lead_min") or {}).items():
        if not k.startswith("_") and not _num(v, lo=0):
            out.append(f"Lead time `{k}` must be a number of minutes, 0 or more.")
    return out
