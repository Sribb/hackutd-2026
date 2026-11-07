"""Loads the scenario file and builds the /api/field and /api/state payloads.

Which file:
1. FREEBOARD_SCENARIO, if set.
2. scenario/field.json (owned by @vinnycodes67), once it's in the repo.
3. backend/data/scenario.json, the backend's own placeholder.

Two shapes are accepted. The backend shape has `field`, `initial_levels_pct`, and
`failures`. The scenario/ shape has top-level `tanks`, `clock_start`, `initial`,
and `faults`, and is converted on load. Nothing in scenario/ is modified.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
TEAM_SCENARIO = REPO_DIR / "scenario" / "field.json"
DEFAULT_SCENARIO = BACKEND_DIR / "data" / "scenario.json"

FIELD_KEYS = ("tanks", "pumps", "valves", "wells", "disposal_wells", "trucks")


def scenario_path() -> Path:
    env = os.environ.get("FREEBOARD_SCENARIO")
    if env:
        return Path(env)
    return TEAM_SCENARIO if TEAM_SCENARIO.exists() else DEFAULT_SCENARIO


def load_scenario(path: str | Path | None = None) -> dict:
    path = Path(path) if path else scenario_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    if "field" not in data and "tanks" in data:
        data = from_team_shape(data)
    missing = [k for k in FIELD_KEYS if k not in data.get("field", {})]
    if missing:
        raise ValueError(f"{path}: field is missing {missing}")
    data["_source"] = str(path)
    return data


def _add_minutes(clock: str, minutes: float) -> str:
    h, m = (int(x) for x in clock.split(":"))
    total = h * 60 + m + int(round(minutes))
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def from_team_shape(d: dict) -> dict:
    """Converts scenario/field.json into the backend shape.

    The forecast starts at the first fault, using the readings in `initial`.
    Tanks hold their starting level until then, so the levels carry over.
    """
    faults = d.get("faults", [])
    start_offset = min((f.get("at_min", 0) for f in faults), default=0)
    start = _add_minutes(d.get("clock_start", "00:00"), start_offset)
    levels = dict(d.get("initial", {}).get("tank_pct", {}))
    pumps_by_id = {p["id"]: p for p in d["pumps"]}

    tanks = []
    for t in d["tanks"]:
        t = dict(t)
        # With no setpoint given, a running pump holds the tank where it started.
        t.setdefault("setpoint_pct", levels.get(t["id"], 50))
        tanks.append(t)
    valves = [{**v, "position": v.get("default", v.get("position", "CLOSED"))} for v in d["valves"]]
    trucks = []
    for tr in d["trucks"]:
        tr = dict(tr)
        tr.setdefault("load_min", tr.get("load_minutes", 20))
        tr.setdefault("haul_to_swd_min", tr.get("minutes_to_disposal", 30))
        trucks.append(tr)

    failures = []
    for f in faults:
        pump = pumps_by_id.get(f["asset_id"])
        tank = pump["tank_id"].replace("TANK_", "Tank ") if pump else None
        name = f["asset_id"].replace("PUMP_", "Pump ")
        failures.append({
            "asset_id": f["asset_id"],
            "at_clock": _add_minutes(d.get("clock_start", "00:00"), f.get("at_min", 0)),
            "reason": f.get("reason", "Failed."),
            "summary": f"{name} failed. {tank} is filling." if tank else f"{name} failed.",
        })

    field = {"tanks": tanks, "pumps": d["pumps"], "valves": valves, "wells": d["wells"],
             "disposal_wells": d["disposal_wells"], "trucks": trucks}
    for extra in ("lead_min", "costs", "yards"):
        if extra in d:
            field[extra] = d[extra]
    return {"start_clock": start, "field": field, "initial_levels_pct": levels, "failures": failures}


def field_payload(scenario: dict) -> dict:
    """GET /api/field: everything that doesn't change during the night.

    Also passes through `lead_min`, `costs`, and `yards` when the scenario has them.
    """
    out = {k: scenario["field"][k] for k in FIELD_KEYS}
    for extra in ("lead_min", "costs", "yards"):
        if extra in scenario["field"]:
            out[extra] = scenario["field"][extra]
    return out


def state_payload(scenario: dict) -> dict:
    """GET /api/state: current readings and the active incident.

    A tank with no reading is reported as WARNING with a reason, never as OK.
    """
    field = scenario["field"]
    levels = scenario.get("initial_levels_pct", {})
    failures = {f["asset_id"]: f for f in scenario.get("failures", [])}
    assets: dict[str, dict] = {}

    for pump in field["pumps"]:
        f = failures.get(pump["id"])
        assets[pump["id"]] = (
            {"status": "ALARM", "reason": f["reason"]} if f else {"status": "OK"}
        )

    failed_tanks = {
        p["tank_id"] for p in field["pumps"] if p["id"] in failures
    }
    for tank in field["tanks"]:
        tid = tank["id"]
        if levels.get(tid) is None:
            assets[tid] = {"status": "WARNING", "level_pct": None,
                           "reason": "No level reading. Treat as unknown, not safe."}
            continue
        level = float(levels[tid])
        warn_at = tank.get("warning_pct", tank["shutdown_pct"] - 10)
        if tid in failed_tanks or level >= tank["shutdown_pct"]:
            status = "ALARM"
        elif level >= warn_at:
            status = "WARNING"
        else:
            status = "OK"
        assets[tid] = {"status": status, "level_pct": level}

    for valve in field["valves"]:
        position = valve.get("position") or ("CLOSED" if valve["id"].startswith("VALVE_XO") else "OPEN")
        assets[valve["id"]] = {"status": "OK", "position": position}

    for well in field["wells"]:
        assets[well["id"]] = {"status": "OK"}
    for swd in field["disposal_wells"]:
        assets[swd["id"]] = {"status": "OK"}
    for truck in field["trucks"]:
        assets[truck["id"]] = {"status": "OK", "position": truck.get("home_id", "YARD_1")}

    incident = None
    if failures:
        root = next(iter(failures.values()))
        incident = {"root_asset_id": root["asset_id"], "summary": root["summary"]}

    return {"clock": scenario.get("start_clock", "00:00"), "assets": assets, "incident": incident}
