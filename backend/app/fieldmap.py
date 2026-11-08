"""Lookups over the loaded field for the live layer (connectors, detection, Jarvis).

Built from the backend-shaped scenario (`scenario["field"]`), so it works for both
backend/data/scenario.json and a v1 blueprint. Discharge valves and crossovers are
found from `connects`, since older files have no `kind`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .forecast import clock_at, nice

COMMANDS = ("SET_VALVE", "SET_PUMP", "CHOKE_WELL", "SEND_TRUCK")
SIGNALS = ("level_pct", "flow_bbl_hr", "running", "position", "pressure_psi", "motor_amps")


@dataclass
class FieldMap:
    start_clock: str
    tanks: dict
    pumps: dict
    valves: dict
    wells: dict
    swds: dict
    trucks: dict
    wells_by_tank: dict = field(default_factory=dict)
    pumps_by_tank: dict = field(default_factory=dict)
    pump_valve: dict = field(default_factory=dict)   # pump id -> discharge valve id
    pump_swd: dict = field(default_factory=dict)     # pump id -> disposal well id
    crossovers: list = field(default_factory=list)   # valves that connect two tanks

    @classmethod
    def from_scenario(cls, scenario: dict, start_clock: Optional[str] = None) -> "FieldMap":
        f = scenario["field"]
        m = cls(
            start_clock=start_clock or scenario.get("start_clock", "00:00"),
            tanks={t["id"]: t for t in f["tanks"]},
            pumps={p["id"]: p for p in f["pumps"]},
            valves={v["id"]: v for v in f["valves"]},
            wells={w["id"]: w for w in f["wells"]},
            swds={s["id"]: s for s in f["disposal_wells"]},
            trucks={t["id"]: t for t in f["trucks"]},
        )
        for tid in m.tanks:
            m.wells_by_tank[tid] = []
            m.pumps_by_tank[tid] = []
        for wid, w in m.wells.items():
            m.wells_by_tank.setdefault(w["tank_id"], []).append(wid)
        for pid, p in m.pumps.items():
            m.pumps_by_tank.setdefault(p["tank_id"], []).append(pid)
            if p.get("valve_id"):
                m.pump_valve[pid] = p["valve_id"]
            if p.get("swd_id"):
                m.pump_swd[pid] = p["swd_id"]
        for vid, v in m.valves.items():
            ends = list(v.get("connects") or [])[:2]
            if len(ends) == 2 and all(e in m.tanks for e in ends):
                m.crossovers.append(v)
            elif len(ends) == 2 and ends[0] in m.pumps:
                m.pump_valve.setdefault(ends[0], vid)
                if ends[1] in m.swds:
                    m.pump_swd.setdefault(ends[0], ends[1])
            elif v.get("pump_id") in m.pumps:
                m.pump_valve.setdefault(v["pump_id"], vid)
        return m

    # ---------- lookups ----------
    def kind_of(self, asset_id: str) -> Optional[str]:
        for kind, table in (("tank", self.tanks), ("pump", self.pumps), ("valve", self.valves),
                            ("well", self.wells), ("swd", self.swds), ("truck", self.trucks)):
            if asset_id in table:
                return kind
        return None

    def all_ids(self) -> list[str]:
        return [*self.tanks, *self.pumps, *self.valves, *self.wells, *self.swds, *self.trucks]

    def warning_pct(self, tid: str) -> float:
        t = self.tanks[tid]
        return float(t.get("warning_pct", t["shutdown_pct"] - 10))

    def high_high_pct(self, tid: str) -> float:
        """TANK_HIGH_HIGH threshold (CF-7): high_high_pct, else 5 points under shutdown."""
        t = self.tanks[tid]
        return float(t.get("high_high_pct", t["shutdown_pct"] - 5))

    def low_pct(self, tid: str) -> float:
        """Level a running pump works its tank down to."""
        t = self.tanks[tid]
        return float(t.get("pump_low_pct", t.get("setpoint_pct", 50)))

    # ---------- clock ----------
    def clock(self, t_min: float) -> str:
        """'HH:MM' for live minutes since the start clock."""
        try:
            h, m = (int(x) for x in self.start_clock.split(":"))
        except (ValueError, AttributeError):
            h, m = 0, 0
        total = int(round(h * 60 + m + t_min)) % (24 * 60)
        return f"{total // 60:02d}:{total % 60:02d}"

    def clock_text(self, t_min: float) -> str:
        """'2:47 AM' style, for messages."""
        return clock_at(self.start_clock, t_min)


def name(asset_id: str) -> str:
    """TANK_3 -> 'Tank 3'; VALVE_XO_34 -> 'the Tank 3 to 4 crossover'."""
    return nice(asset_id)
