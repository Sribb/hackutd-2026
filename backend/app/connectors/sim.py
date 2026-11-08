"""The simulator connector: a live digital twin that drives the demo with no hardware.

Ported from the plant physics in draft PR #21 (backend/app/sim.py). Per minute:

    tank volume += well inflow - pump outflow +/- crossover flow - truck loading - leak

- Wells feed their tank at water_bbl_hr times their choke (1 untouched, 0 shut in).
- A running pump empties its tank at rate_bbl_hr while the tank is above its low
  level (pump_low_pct, else setpoint_pct), then just keeps up with inflow. A tripped
  pump, a pump turned OFF, or a closed discharge valve moves nothing.
- Pumps on one disposal well share its max_bbl_hr.
- An open crossover moves water from the fuller tank to the emptier one.
- At shutdown_pct the tank's wells shut in.

Faults come from the blueprint's `faults` list and fire at `at_min`:
TRIP (pump stops), STUCK_VALVE (valve ignores commands), SENSOR_STALE (the asset's
sensors go quiet), LEAK (the tank loses `rate_bbl_hr`, default 20).

Readings carry noise from the blueprint's `sensors` block. A tank whose starting
level is unknown has a dead level sensor, so it is never reported as safe.
"""
from __future__ import annotations

import random
from typing import Callable, Optional

from ..fieldmap import FieldMap
from .base import Connector, Reading

DEFAULT_SENSORS = {"level_noise_pct": 0.08, "flow_noise_bbl_hr": 2.0, "stale_after_min": 5}
FAULT_TYPES = {"TRIP", "STUCK_VALVE", "SENSOR_STALE", "LEAK"}


def _on(value) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    v = str(value).strip().upper()
    if v in {"OPEN", "ON", "TRUE", "1", "RUNNING"}:
        return True
    if v in {"CLOSED", "CLOSE", "OFF", "FALSE", "0", "STOPPED"}:
        return False
    return None


def _fraction(value) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if x > 1.0:  # accept 50 as 50%
        x /= 100.0
    return max(0.0, min(1.0, x))


class SimConnector(Connector):
    kind = "sim"

    def __init__(self, spec: dict, fmap: FieldMap, now: Callable[[], float], *,
                 initial_pct: Optional[dict] = None, faults: Optional[list] = None,
                 sensors: Optional[dict] = None, skip: Optional[set] = None) -> None:
        super().__init__({"id": "CONN_SIM", **spec}, fmap, now)
        f = fmap
        self.sensors = {**DEFAULT_SENSORS, **(sensors or {})}
        self.skip = set(skip or ())                 # (asset_id, signal) another connector owns
        self.rng = random.Random(self.config.get("seed", 7))
        self.t = 0.0
        initial_pct = initial_pct or {}
        self.level = {}
        self.dead_sensors: set[str] = set()          # assets whose sensors report nothing
        for tid, tank in f.tanks.items():
            pct = initial_pct.get(tid)
            if pct is None:
                self.dead_sensors.add(tid)
                pct = 50.0
            self.level[tid] = float(pct) / 100.0 * float(tank["capacity_bbl"])
        self.pump_on = {pid: True for pid in f.pumps}
        self.tripped: dict[str, str] = {}            # pump id -> reason
        xo_ids = {xo["id"] for xo in f.crossovers}
        # Same default as /api/state: crossovers start closed, discharge valves open.
        self.valve_open = {vid: str(v.get("position") or v.get("default")
                                    or ("CLOSED" if vid in xo_ids else "OPEN")).upper() == "OPEN"
                           for vid, v in f.valves.items()}
        self.stuck_valves: set[str] = set()
        self.choke = {wid: 1.0 for wid in f.wells}
        self.well_shut = {wid: False for wid in f.wells}
        self.tank_shutdown = {tid: False for tid in f.tanks}
        self.leaks: dict[str, float] = {}
        self.truck_jobs: list[dict] = []
        self.truck_free_at = {tid: 0.0 for tid in f.trucks}
        self.flows: dict[str, float] = {}
        self.events: list[dict] = []
        self.faults = sorted((dict(x) for x in faults or []), key=lambda x: float(x.get("at_min", 0) or 0))
        self._step(0.0)  # fill self.flows so the first read is sensible
        self._apply_due_faults()

    # ---------- interface ----------
    def advance(self, dt_min: float) -> None:
        remaining = max(0.0, float(dt_min))
        while remaining > 1e-9:
            d = min(1.0, remaining)
            self._apply_due_faults()
            self._step(d)
            remaining -= d
        self._apply_due_faults()

    def read(self) -> list[Reading]:
        f, out = self.fmap, []
        ln, fn = float(self.sensors["level_noise_pct"]), float(self.sensors["flow_noise_bbl_hr"])

        def put(aid: str, signal: str, value) -> None:
            if aid not in self.dead_sensors and (aid, signal) not in self.skip:
                out.append(self.reading(aid, signal, value))

        for tid in f.tanks:
            put(tid, "level_pct", round(self.pct(tid) + self.rng.gauss(0, ln), 2))
        for pid in f.pumps:
            q = self.flows.get(pid, 0.0)
            put(pid, "flow_bbl_hr", round(max(0.0, q + (self.rng.gauss(0, fn) if q > 0 else 0.0)), 1))
            put(pid, "running", self.is_running(pid))
        for vid in f.valves:
            put(vid, "position", "OPEN" if self.valve_open[vid] else "CLOSED")
        for wid in f.wells:
            put(wid, "flow_bbl_hr", round(self.flows.get(wid, 0.0), 1))
        for sid in f.swds:
            put(sid, "flow_bbl_hr", round(self.flows.get(sid, 0.0), 1))
        return out

    def send(self, command: dict) -> dict:
        f = self.fmap
        typ = str(command.get("type", "")).upper()
        aid = command.get("asset_id")
        val = command.get("value")
        if typ == "SET_VALVE":
            pos = _on(val)
            if aid not in f.valves or pos is None:
                return {"ok": False, "detail": f"SET_VALVE needs a valve and OPEN or CLOSED, got {aid} {val!r}."}
            if aid in self.stuck_valves:
                return {"ok": True, "detail": f"Sent to {aid}. (Simulated fault: the valve is stuck and won't move.)"}
            self.valve_open[aid] = pos
            return {"ok": True, "detail": f"{aid} {'opened' if pos else 'closed'}."}
        if typ == "SET_PUMP":
            on = _on(val)
            if aid not in f.pumps or on is None:
                return {"ok": False, "detail": f"SET_PUMP needs a pump and ON or OFF, got {aid} {val!r}."}
            if on and aid in self.tripped:
                return {"ok": False, "detail": f"{aid} is tripped and needs a field reset before it can run."}
            self.pump_on[aid] = on
            return {"ok": True, "detail": f"{aid} turned {'on' if on else 'off'}."}
        if typ == "CHOKE_WELL":
            frac = _fraction(val)
            if aid not in f.wells or frac is None:
                return {"ok": False, "detail": f"CHOKE_WELL needs a well and a fraction 0 to 1, got {aid} {val!r}."}
            self.choke[aid] = frac
            return {"ok": True, "detail": f"{aid} set to {frac:.0%} of its flow."}
        if typ == "SEND_TRUCK":
            target = command.get("target_id")
            truck = f.trucks.get(aid)
            if truck is None or target not in truck.get("minutes_to", {}):
                return {"ok": False, "detail": f"{aid} has no route to {target}."}
            depart = max(self.t, self.truck_free_at[aid])
            arrive = depart + float(truck["minutes_to"][target])
            load_end = arrive + float(truck.get("load_min", truck.get("load_minutes", 20)))
            done = load_end + float(truck.get("haul_to_swd_min", truck.get("minutes_to_disposal", 30)))
            self.truck_jobs.append({"truck_id": aid, "target_id": target, "depart": depart, "arrive": arrive,
                                    "load_end": load_end, "done": done,
                                    "remaining": float(truck["capacity_bbl"])})
            self.truck_free_at[aid] = done
            return {"ok": True, "detail": f"{aid} dispatched to {target}, arriving in {arrive - self.t:.0f} min."}
        return {"ok": False, "detail": f"Unknown command {typ!r}."}

    def status(self) -> dict:
        return {"id": self.id, "kind": self.kind, "available": True,
                "detail": f"simulating {len(self.fmap.all_ids())} assets"}

    # ---------- faults ----------
    def _apply_due_faults(self) -> None:
        while self.faults and float(self.faults[0].get("at_min", 0) or 0) <= self.t + 1e-9:
            self.inject(self.faults.pop(0))

    def inject(self, fault: dict) -> str:
        """Apply a fault now. Returns '' on success, else why it was ignored."""
        typ = str(fault.get("type", "TRIP")).upper()
        aid = fault.get("asset_id")
        if typ == "TRIP" and aid in self.fmap.pumps:
            self.tripped[aid] = str(fault.get("reason") or "Pump tripped.")
        elif typ == "STUCK_VALVE" and aid in self.fmap.valves:
            self.stuck_valves.add(aid)
        elif typ == "SENSOR_STALE" and self.fmap.kind_of(aid or ""):
            self.dead_sensors.add(aid)
        elif typ == "LEAK" and aid in self.fmap.tanks:
            self.leaks[aid] = float(fault.get("rate_bbl_hr", 20.0))
        elif typ == "REPAIR" and self.fmap.kind_of(aid or ""):
            self.tripped.pop(aid, None)
            self.stuck_valves.discard(aid)
            self.dead_sensors.discard(aid)
            self.leaks.pop(aid, None)
        else:
            return f"Can't apply fault {typ} to {aid}."
        self.events.append({"type": f"FAULT_{typ}", "asset_id": aid, "t": self.t})
        return ""

    # ---------- physics ----------
    def pct(self, tid: str) -> float:
        return 100.0 * self.level[tid] / float(self.fmap.tanks[tid]["capacity_bbl"])

    def is_running(self, pid: str) -> bool:
        vid = self.fmap.pump_valve.get(pid)
        return self.pump_on[pid] and pid not in self.tripped and (vid is None or self.valve_open.get(vid, True))

    def truck_phase(self, truck_id: str) -> dict:
        for j in self.truck_jobs:
            if j["truck_id"] != truck_id or self.t >= j["done"]:
                continue
            if self.t < j["depart"]:
                return {"phase": "QUEUED", "target_id": j["target_id"]}
            if self.t < j["arrive"]:
                return {"phase": "EN_ROUTE", "target_id": j["target_id"]}
            if self.t < j["load_end"]:
                return {"phase": "LOADING", "target_id": j["target_id"]}
            return {"phase": "TO_DISPOSAL", "target_id": j["target_id"]}
        return {"phase": "IDLE", "target_id": None}

    def _step(self, dt: float) -> None:
        f = self.fmap
        hours = dt / 60.0
        t0, t1 = self.t, self.t + dt
        flows: dict[str, float] = {}

        inflow = {tid: 0.0 for tid in f.tanks}
        for wid, w in f.wells.items():
            q = 0.0 if self.well_shut[wid] else float(w["water_bbl_hr"]) * self.choke[wid]
            flows[wid] = q
            if w["tank_id"] in inflow:
                inflow[w["tank_id"]] += q

        want: dict[str, float] = {}
        for pid, p in f.pumps.items():
            tid = p["tank_id"]
            if not self.is_running(pid):
                want[pid] = 0.0
            elif self.pct(tid) > f.low_pct(tid):
                want[pid] = float(p["rate_bbl_hr"])
            else:
                want[pid] = min(float(p["rate_bbl_hr"]), inflow[tid])
        for sid, s in f.swds.items():
            feeders = [pid for pid, s_id in f.pump_swd.items() if s_id == sid]
            total = sum(want[pid] for pid in feeders)
            if total > float(s["max_bbl_hr"]) > 0:
                for pid in feeders:
                    want[pid] *= float(s["max_bbl_hr"]) / total
            flows[sid] = min(total, float(s["max_bbl_hr"]))
        flows.update(want)

        net = {tid: inflow[tid] * hours for tid in f.tanks}
        for pid, p in f.pumps.items():
            net[p["tank_id"]] -= want[pid] * hours
        for tid, rate in self.leaks.items():
            net[tid] -= rate * hours
        for xo in f.crossovers:
            a, b = xo["connects"][:2]
            q = 0.0
            if self.valve_open.get(xo["id"]):
                mx = float(xo.get("max_bbl_hr", 300.0))
                per = float(xo.get("bbl_hr_per_pct", mx / 10.0))
                q = max(-mx, min(mx, (self.pct(a) - self.pct(b)) * per))
            net[a] -= q * hours
            net[b] += q * hours
            flows[xo["id"]] = q  # positive: a -> b
        for j in self.truck_jobs:
            if j["remaining"] <= 0 or t1 <= j["arrive"] or t0 >= j["load_end"]:
                continue
            truck = f.trucks[j["truck_id"]]
            load_min = float(truck.get("load_min", truck.get("load_minutes", 20))) or 1.0
            overlap = min(t1, j["load_end"]) - max(t0, j["arrive"])
            take = min(float(truck["capacity_bbl"]) / load_min * overlap, j["remaining"],
                       max(0.0, self.level[j["target_id"]] + net[j["target_id"]]))
            net[j["target_id"]] -= take
            j["remaining"] -= take

        for tid, tank in f.tanks.items():
            cap = float(tank["capacity_bbl"])
            self.level[tid] = min(cap, max(0.0, self.level[tid] + net[tid]))
            if not self.tank_shutdown[tid] and self.pct(tid) >= float(tank["shutdown_pct"]):
                self.tank_shutdown[tid] = True
                for wid in f.wells_by_tank.get(tid, []):
                    self.well_shut[wid] = True
                self.events.append({"type": "SHUTDOWN_LEVEL", "asset_id": tid, "t": t1})
        self.flows = flows
        self.t = t1
