"""Plant physics and the what-if forecast.

One `Plant` model drives both the live field (live.py steps it in real time) and
every forecast (we clone it and step into the future). That way the 3D scene, the
countdown, and the planner can never disagree.

Physics, per minute:
    level += (well inflow - pump outflow - crossover out + crossover in) * dt - truck loading
Starts from the current level (the bug BACKEND.md found in CauldronWatch), and
wells feeding a tank shut in when it reaches its shutdown level.
"""
from __future__ import annotations

import copy
from typing import Any

from .field import Field, nice_name

UNCERTAINTY = 0.15  # inflow +/- 15% gives t_min_low / t_min_high (BACKEND.md step 3)

ACTION_TYPES = {"SET_VALVE", "SET_PUMP", "CHOKE_WELL", "SEND_TRUCK"}
# Not operator actions: fault injection and recovery used by the live demo.
INTERNAL_TYPES = {"TRIP_PUMP", "RESET_PUMP"}


def _norm_open(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    v = str(value).strip().upper()
    if v in {"OPEN", "ON", "TRUE", "1"}:
        return True
    if v in {"CLOSED", "CLOSE", "OFF", "FALSE", "0"}:
        return False
    return None


def _norm_fraction(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if x > 1.0:  # accept 50 as 50%
        x = x / 100.0
    return max(0.0, min(1.0, x))


class Plant:
    def __init__(self, f: Field):
        self.f = f
        self.t = 0.0  # minutes since clock_start
        init = f.raw.get("initial", {}).get("tank_pct", {})
        self.level = {tid: init.get(tid, 50.0) / 100.0 * t["capacity_bbl"] for tid, t in f.tanks.items()}
        self.pump_on = {pid: True for pid in f.pumps}
        self.pump_tripped = {pid: False for pid in f.pumps}
        self.pump_trip_reason: dict[str, str] = {}
        self.pump_trip_t: dict[str, float] = {}
        self.valve_open = {vid: v.get("default", "OPEN") == "OPEN" for vid, v in f.valves.items()}
        self.well_factor = {wid: 1.0 for wid in f.wells}
        self.well_shut = {wid: False for wid in f.wells}
        self.tank_shutdown = {tid: False for tid in f.tanks}
        self.truck_free_at = {tid: 0.0 for tid in f.trucks}
        self.truck_jobs: list[dict] = []
        self.flows: dict[str, float] = {}  # last step's flows, bbl/hr (telemetry reads these)
        self.reset_costs()

    # ---------- bookkeeping ----------
    def reset_costs(self) -> None:
        self.cost = {"trucking_usd": 0.0, "disposal_usd": 0.0, "lost_production_usd": 0.0}
        self.hauled_bbl = 0.0

    def clone(self) -> "Plant":
        other = copy.copy(self)
        for name in ("level", "pump_on", "pump_tripped", "pump_trip_reason", "pump_trip_t",
                     "valve_open", "well_factor", "well_shut", "tank_shutdown",
                     "truck_free_at", "flows", "cost"):
            setattr(other, name, dict(getattr(self, name)))
        other.truck_jobs = [dict(j) for j in self.truck_jobs]
        return other

    def pct(self, tank_id: str) -> float:
        return 100.0 * self.level[tank_id] / self.f.tanks[tank_id]["capacity_bbl"]

    def wells_offline(self) -> list[str]:
        return [w for w, shut in self.well_shut.items() if shut or self.well_factor[w] == 0.0]

    def truck_phase(self, truck_id: str, t: float | None = None) -> dict:
        t = self.t if t is None else t
        for j in self.truck_jobs:
            if j["truck_id"] != truck_id or t >= j["unload_end"]:
                continue
            if t < j["depart"]:
                return {"phase": "QUEUED", "target_id": j["target_id"], "eta_t": j["arrive"]}
            if t < j["arrive"]:
                return {"phase": "EN_ROUTE", "target_id": j["target_id"], "eta_t": j["arrive"]}
            if t < j["load_end"]:
                return {"phase": "LOADING", "target_id": j["target_id"], "eta_t": j["load_end"]}
            return {"phase": "TO_DISPOSAL", "target_id": j["target_id"], "eta_t": j["unload_end"]}
        return {"phase": "IDLE", "target_id": None, "eta_t": None}

    # ---------- actions ----------
    def apply(self, action: dict) -> list[str]:
        """Apply one action now. Returns warnings (never raises on bad input)."""
        f = self.f
        typ = str(action.get("type", "")).upper()
        aid = action.get("asset_id")
        val = action.get("value")
        warn: list[str] = []

        if typ == "SET_VALVE":
            if aid not in f.valves:
                return [f"Unknown valve {aid}; action ignored."]
            pos = _norm_open(val)
            if pos is None:
                return [f"SET_VALVE {aid} needs OPEN or CLOSED, got {val!r}; action ignored."]
            self.valve_open[aid] = pos

        elif typ == "SET_PUMP":
            if aid not in f.pumps:
                return [f"Unknown pump {aid}; action ignored."]
            on = _norm_open(val)
            if on is None:
                return [f"SET_PUMP {aid} needs ON or OFF, got {val!r}; action ignored."]
            if on and self.pump_tripped[aid]:
                warn.append(f"{nice_name(aid)} is tripped and needs a field reset; turning it on has no effect.")
            self.pump_on[aid] = on

        elif typ == "CHOKE_WELL":
            if aid not in f.wells:
                return [f"Unknown well {aid}; action ignored."]
            frac = _norm_fraction(val)
            if frac is None:
                return [f"CHOKE_WELL {aid} needs a fraction 0-1, got {val!r}; action ignored."]
            self.well_factor[aid] = frac

        elif typ == "SEND_TRUCK":
            target = action.get("target_id")
            if aid not in f.trucks:
                return [f"Unknown truck {aid}; action ignored."]
            truck = f.trucks[aid]
            if target not in truck["minutes_to"]:
                return [f"{nice_name(aid)} has no route to {target}; action ignored."]
            depart = max(self.t, self.truck_free_at[aid])
            if depart > self.t:
                warn.append(f"{nice_name(aid)} is busy; this trip leaves at {f.clock_text(depart)}.")
            arrive = depart + truck["minutes_to"][target]
            load_end = arrive + truck["load_minutes"]
            unload_end = load_end + truck["minutes_to_disposal"] + truck["unload_minutes"]
            self.truck_jobs.append({
                "truck_id": aid, "target_id": target, "depart": depart, "arrive": arrive,
                "load_end": load_end, "unload_end": unload_end,
                "remaining": float(truck["capacity_bbl"]), "hauled": 0.0,
            })
            self.truck_free_at[aid] = unload_end
            self.cost["trucking_usd"] += (unload_end - depart) / 60.0 * truck["usd_hr"]

        elif typ == "TRIP_PUMP":
            if aid not in f.pumps:
                return [f"Unknown pump {aid}."]
            self.pump_tripped[aid] = True
            self.pump_trip_reason[aid] = str(val or "Pump tripped.")
            self.pump_trip_t[aid] = self.t

        elif typ == "RESET_PUMP":
            if aid not in f.pumps:
                return [f"Unknown pump {aid}."]
            self.pump_tripped[aid] = False
            self.pump_trip_reason.pop(aid, None)
            self.pump_trip_t.pop(aid, None)
            self.pump_on[aid] = True

        else:
            return [f"Unknown action type {typ!r}; expected one of {sorted(ACTION_TYPES)}."]
        return warn

    # ---------- physics ----------
    def step(self, dt: float = 1.0, inflow_scale: float = 1.0) -> list[dict]:
        """Advance dt minutes. Returns events that happened in this step."""
        f = self.f
        hours = dt / 60.0
        t0, t1 = self.t, self.t + dt
        events: list[dict] = []
        flows: dict[str, float] = {}

        inflow = {tid: 0.0 for tid in f.tanks}
        for wid, w in f.wells.items():
            if not self.well_shut[wid]:
                q = w["water_bbl_hr"] * self.well_factor[wid] * inflow_scale
                inflow[w["tank_id"]] += q
                flows[wid] = q
            else:
                flows[wid] = 0.0

        # Pumps on level control: full rate above the low setpoint, otherwise match inflow.
        want: dict[str, float] = {}
        for pid, p in f.pumps.items():
            tid = p["tank_id"]
            running = self.pump_on[pid] and not self.pump_tripped[pid] and self.valve_open.get(p["valve_id"], True)
            if not running:
                want[pid] = 0.0
            elif self.pct(tid) > f.tanks[tid]["pump_low_pct"]:
                want[pid] = p["rate_bbl_hr"]
            else:
                want[pid] = min(p["rate_bbl_hr"], inflow[tid])
        # Disposal wells cap what the pumps can push.
        for sid, s in f.swds.items():
            feeders = [pid for pid, p in f.pumps.items() if p["swd_id"] == sid]
            total = sum(want[pid] for pid in feeders)
            scale = 1.0 if total <= s["max_bbl_hr"] or total == 0 else s["max_bbl_hr"] / total
            for pid in feeders:
                want[pid] *= scale
            flows[sid] = min(total, s["max_bbl_hr"])
        flows.update(want)

        net = {tid: (inflow[tid] - want[f.pump_by_tank[tid]]) * hours for tid in f.tanks}

        for xo in f.crossovers:
            a, b = xo["connects"]
            q = 0.0
            if self.valve_open[xo["id"]]:
                dp = self.pct(a) - self.pct(b)
                q = max(-xo["max_bbl_hr"], min(xo["max_bbl_hr"], dp * xo["bbl_hr_per_pct"]))
            net[a] -= q * hours
            net[b] += q * hours
            flows[xo["id"]] = q  # positive: a -> b

        # Trucks load while parked at the tank.
        for j in self.truck_jobs:
            if j["remaining"] <= 0 or t1 <= j["arrive"] or t0 >= j["load_end"]:
                pass
            else:
                truck = f.trucks[j["truck_id"]]
                overlap = min(t1, j["load_end"]) - max(t0, j["arrive"])
                take = min(truck["capacity_bbl"] / truck["load_minutes"] * overlap, j["remaining"],
                           max(0.0, self.level[j["target_id"]] + net[j["target_id"]]))
                net[j["target_id"]] -= take
                j["remaining"] -= take
                j["hauled"] += take
                self.hauled_bbl += take
                self.cost["disposal_usd"] += take * f.raw.get("costs", {}).get("disposal_usd_bbl", 0.0)
            if t0 < j["arrive"] <= t1:
                events.append({"type": "TRUCK_ARRIVES", "asset_id": j["truck_id"], "target_id": j["target_id"], "t": j["arrive"]})
            if t0 < j["load_end"] <= t1:
                events.append({"type": "TRUCK_LOADED", "asset_id": j["truck_id"], "target_id": j["target_id"],
                               "t": j["load_end"], "bbl": round(j["hauled"], 1)})

        for tid, tank in f.tanks.items():
            before = self.pct(tid)
            cap = tank["capacity_bbl"]
            self.level[tid] = max(0.0, self.level[tid] + net[tid])
            if self.level[tid] > cap:
                self.level[tid] = cap
            after = self.pct(tid)
            if before < tank["warning_pct"] <= after:
                events.append({"type": "WARNING_LEVEL", "asset_id": tid, "t": self._cross(t0, dt, before, after, tank["warning_pct"])})
            if not self.tank_shutdown[tid] and after >= tank["shutdown_pct"]:
                t_cross = self._cross(t0, dt, before, after, tank["shutdown_pct"])
                self.tank_shutdown[tid] = True
                shut = []
                for wid in f.wells_by_tank[tid]:
                    if not self.well_shut[wid]:
                        self.well_shut[wid] = True
                        shut.append(wid)
                events.append({"type": "SHUTDOWN_LEVEL", "asset_id": tid, "t": t_cross})
                if shut:
                    events.append({"type": "WELLS_SHUT_IN", "asset_id": tid, "wells": shut, "t": t_cross})
            if before < 100.0 <= after:
                events.append({"type": "OVERFLOW", "asset_id": tid, "t": t1})
            flows[f"{tid}_IN"] = inflow[tid]

        for wid, w in f.wells.items():
            lost = 1.0 if self.well_shut[wid] else 1.0 - self.well_factor[wid]
            if lost > 0:
                self.cost["lost_production_usd"] += w["lost_usd_hr"] * lost * hours

        self.flows = flows
        self.t = t1
        return events

    @staticmethod
    def _cross(t0: float, dt: float, before: float, after: float, thr: float) -> float:
        if after == before:
            return t0 + dt
        return t0 + dt * max(0.0, min(1.0, (thr - before) / (after - before)))


# ---------------------------------------------------------------- forecast

def _run(plant: Plant, actions: list[dict], horizon: int, step: int, scale: float,
         record: bool) -> dict:
    p = plant.clone()
    p.reset_costs()
    start = p.t
    pending = sorted(actions, key=lambda a: float(a.get("at_min", 0) or 0))
    warnings: list[str] = []
    events: list[dict] = []
    timeline: list[dict] = []
    i = 0
    for m in range(horizon + 1):
        while i < len(pending) and float(pending[i].get("at_min", 0) or 0) <= m:
            warnings += p.apply(pending[i])
            i += 1
        if record and m % step == 0:
            timeline.append(_snapshot(p, m))
        if m == horizon:
            break
        for e in p.step(1.0, scale):
            e["t_min"] = round(e.pop("t") - start, 1)
            events.append(e)
    return {"plant": p, "events": events, "timeline": timeline, "warnings": warnings}


def _snapshot(p: Plant, m: int) -> dict:
    trucks = {}
    for tid in p.f.trucks:
        ph = p.truck_phase(tid)
        if ph["phase"] != "IDLE":
            trucks[tid] = {"phase": ph["phase"], "target_id": ph["target_id"]}
    offline = p.wells_offline()
    return {
        "t_min": m,
        "tanks": {tid: round(p.pct(tid), 1) for tid in p.f.tanks},
        "wells_online": len(p.f.wells) - len(offline),
        "wells_offline": offline,
        "trucks": trucks,
    }


def _event_text(f: Field, e: dict, now_t: float) -> str:
    when = f.clock_text(now_t + e["t_min"])
    a = nice_name(e["asset_id"])
    typ = e["type"]
    if typ == "SHUTDOWN_LEVEL":
        return f"{a} reaches shutdown at {when}"
    if typ == "SHUTDOWN_POSSIBLE":
        return f"{a} could reach shutdown by {when} if inflow runs 15% high"
    if typ == "WARNING_LEVEL":
        return f"{a} passes {f.tanks[e['asset_id']]['warning_pct']}% at {when}"
    if typ == "WELLS_SHUT_IN":
        return f"{len(e['wells'])} wells on {a} shut in at {when}"
    if typ == "OVERFLOW":
        return f"{a} overflows at {when}"
    if typ == "TRUCK_ARRIVES":
        return f"{a} arrives at {nice_name(e['target_id'])} at {when}"
    if typ == "TRUCK_LOADED":
        return f"{a} leaves {nice_name(e['target_id'])} with {e['bbl']:.0f} bbl at {when}"
    return f"{typ} {a} at {when}"


def forecast(plant: Plant, actions: list[dict] | None = None, horizon: int = 360,
             step: int = 5, data_warnings: list[str] | None = None,
             with_range: bool = True, record: bool = True, inflow_scale: float = 1.0) -> dict:
    """POST /api/forecast. Times in the result are minutes from plant.t (now).

    inflow_scale multiplies every well's water rate (Last Call uses 1.15 for a hot night).
    """
    f = plant.f
    actions = list(actions or [])
    horizon = max(5, min(int(horizon), 24 * 60))
    step = max(1, int(step))
    base = _run(plant, actions, horizon, step, inflow_scale, record)
    events = base["events"]

    if with_range:
        hi = _run(plant, actions, horizon, step, inflow_scale * (1.0 + UNCERTAINTY), False)
        lo = _run(plant, actions, horizon, step, inflow_scale * (1.0 - UNCERTAINTY), False)
        hi_sd = {e["asset_id"]: e["t_min"] for e in hi["events"] if e["type"] == "SHUTDOWN_LEVEL"}
        lo_sd = {e["asset_id"]: e["t_min"] for e in lo["events"] if e["type"] == "SHUTDOWN_LEVEL"}
        base_sd = set()
        for e in events:
            if e["type"] == "SHUTDOWN_LEVEL":
                base_sd.add(e["asset_id"])
                e["t_min_low"] = hi_sd.get(e["asset_id"], e["t_min"])
                e["t_min_high"] = lo_sd.get(e["asset_id"])  # None: beyond the horizon if inflow runs low
        for tid, t in hi_sd.items():
            if tid not in base_sd:
                events.append({"type": "SHUTDOWN_POSSIBLE", "asset_id": tid, "t_min": t})

    events.sort(key=lambda e: e["t_min"])
    for e in events:
        e["text"] = _event_text(f, e, plant.t)
        e["clock"] = f.clock_at(plant.t + e["t_min"])

    end: Plant = base["plant"]
    cost = {k: round(v, 2) for k, v in end.cost.items()}
    cost["total_usd"] = round(sum(cost.values()), 2)
    shutdowns = [e for e in events if e["type"] == "SHUTDOWN_LEVEL"]
    possible = [e for e in events if e["type"] == "SHUTDOWN_POSSIBLE"]
    wells_lost = sorted({w for e in events if e["type"] == "WELLS_SHUT_IN" for w in e["wells"]})

    return {
        "now": {"t": round(plant.t, 2), "clock": f.clock_at(plant.t)},
        "horizon_minutes": horizon,
        "step_minutes": step,
        "timeline": base["timeline"],
        "events": events,
        "cost": cost,
        "summary": {
            "keeps_running": not shutdowns,
            "robust": not shutdowns and not possible,
            "first_shutdown": shutdowns[0] if shutdowns else None,
            "tanks_lost": [e["asset_id"] for e in shutdowns],
            "wells_lost": wells_lost,
            "hauled_bbl": round(end.hauled_bbl, 1),
        },
        "warnings": list(data_warnings or []) + base["warnings"],
    }
