"""Field definition: loads the scenario file and offers lookups and clock helpers.

Called by backend/app/main.py at startup. Swap the scenario file (or this loader)
for sponsor data later without changing the API.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCENARIO = REPO_ROOT / "scenario" / "field.json"


@dataclass
class Field:
    raw: dict
    tanks: dict[str, dict] = field(default_factory=dict)
    pumps: dict[str, dict] = field(default_factory=dict)
    valves: dict[str, dict] = field(default_factory=dict)
    wells: dict[str, dict] = field(default_factory=dict)
    swds: dict[str, dict] = field(default_factory=dict)
    trucks: dict[str, dict] = field(default_factory=dict)
    wells_by_tank: dict[str, list[str]] = field(default_factory=dict)
    pump_by_tank: dict[str, str] = field(default_factory=dict)
    crossovers: list[dict] = field(default_factory=list)
    clock_start_min: int = 0

    @classmethod
    def from_dict(cls, raw: dict) -> "Field":
        f = cls(raw=raw)
        f.tanks = {t["id"]: t for t in raw["tanks"]}
        f.pumps = {p["id"]: p for p in raw["pumps"]}
        f.valves = {v["id"]: v for v in raw["valves"]}
        f.wells = {w["id"]: w for w in raw["wells"]}
        f.swds = {s["id"]: s for s in raw["disposal_wells"]}
        f.trucks = {t["id"]: t for t in raw["trucks"]}
        f.wells_by_tank = {tid: [] for tid in f.tanks}
        for w in raw["wells"]:
            f.wells_by_tank[w["tank_id"]].append(w["id"])
        f.pump_by_tank = {p["tank_id"]: p["id"] for p in raw["pumps"]}
        f.crossovers = [v for v in raw["valves"] if v.get("kind") == "CROSSOVER"]
        hh, mm = raw.get("clock_start", "00:00").split(":")
        f.clock_start_min = int(hh) * 60 + int(mm)
        return f

    # ---------- lookups ----------
    def all_ids(self) -> set[str]:
        ids = set(self.tanks) | set(self.pumps) | set(self.valves) | set(self.wells)
        ids |= set(self.swds) | set(self.trucks)
        ids |= {y["id"] for y in self.raw.get("yards", [])}
        ids |= {f"{t}_LEVEL" for t in self.tanks}
        return ids

    def kind_of(self, asset_id: str) -> str | None:
        for kind, table in (
            ("tank", self.tanks), ("pump", self.pumps), ("valve", self.valves),
            ("well", self.wells), ("swd", self.swds), ("truck", self.trucks),
        ):
            if asset_id in table:
                return kind
        if asset_id.endswith("_LEVEL") and asset_id[:-6] in self.tanks:
            return "tank"
        return None

    def public(self) -> dict:
        """The shape served by GET /api/field (contract fields plus extras)."""
        r = self.raw
        return {
            "name": r.get("name"),
            "clock_start": r.get("clock_start"),
            "tanks": r["tanks"],
            "pumps": r["pumps"],
            "valves": r["valves"],
            "wells": r["wells"],
            "disposal_wells": r["disposal_wells"],
            "trucks": r["trucks"],
            "yards": r.get("yards", []),
            "costs": r.get("costs", {}),
        }

    # ---------- clock ----------
    def clock_at(self, t_min: float) -> str:
        """24h 'HH:MM' for a sim time (minutes since clock_start)."""
        total = int(round(self.clock_start_min + t_min)) % (24 * 60)
        return f"{total // 60:02d}:{total % 60:02d}"

    def clock_text(self, t_min: float) -> str:
        """'2:47 AM' style for operator-facing text."""
        total = int(round(self.clock_start_min + t_min)) % (24 * 60)
        h, m = divmod(total, 60)
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {suffix}"


def nice_name(asset_id: str) -> str:
    """TANK_3 -> 'Tank 3', VALVE_XO_34 -> 'the Tank 3 to 4 crossover'."""
    if asset_id.startswith("VALVE_XO_"):
        a, b = asset_id[-2], asset_id[-1]
        return f"the Tank {a} to {b} crossover"
    if asset_id.startswith("VALVE_D_"):
        return f"Pump {asset_id.split('_')[-1]} discharge valve"
    if asset_id.startswith("SWD_"):
        return f"Disposal well {asset_id.split('_')[-1]}"
    parts = asset_id.split("_")
    if len(parts) == 2:
        return f"{parts[0].capitalize()} {parts[1]}"
    return asset_id


def load_field(path: str | os.PathLike | None = None) -> Field:
    p = Path(path or os.environ.get("FREEBOARD_SCENARIO") or DEFAULT_SCENARIO)
    with open(p) as fh:
        return Field.from_dict(json.load(fh))
