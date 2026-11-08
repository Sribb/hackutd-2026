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


class ScenarioError(ValueError):
    """The field file can't be used. `problems` lists every reason, in plain English."""

    def __init__(self, source: str, problems: list[str]):
        self.source, self.problems = source, problems
        super().__init__(f"{source}: " + " ".join(problems))


def load_scenario(path: str | Path | None = None) -> dict:
    path = Path(path) if path else scenario_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ScenarioError(str(path), [f"Not valid JSON: {e}."]) from e
    return prepare(data, str(path))


def prepare(data: dict, source: str = "upload") -> dict:
    """Converts (if needed) and checks a field definition. Raises ScenarioError."""
    from .validate import problems  # local import keeps this module free of cycles

    if not isinstance(data, dict):
        raise ScenarioError(source, ["The file must be a JSON object."])
    team_shape = "field" not in data and "tanks" in data
    if team_shape:
        try:
            data = from_team_shape(data)
        except (TypeError, ValueError, AttributeError, KeyError) as e:
            raise ScenarioError(source, [f"Couldn't read the field file ({type(e).__name__}: {e}). "
                                         "Check clock_start is HH:MM and every list holds objects."]) from e
    found = problems(data)
    if found:
        raise ScenarioError(source, found)
    if team_shape:
        _preroll(data)
    data["_source"] = source
    return data


def _add_minutes(clock: str, minutes: float) -> str:
    h, m = (int(x) for x in clock.split(":"))
    total = h * 60 + m + int(round(minutes))
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def _preroll(data: dict) -> None:
    """Replays clock_start to the first fault with every pump healthy, so the
    forecast starts at the fault with the levels the field has by then."""
    minutes = int(round(data.pop("_preroll_min", 0)))
    if minutes <= 0:
        return
    from .forecast import build_plant, simulate  # local import: forecast imports this module

    calm = {**data, "failures": [], "start_clock": data.pop("_clock_start")}
    run = simulate(build_plant(calm), [], minutes, record_every=minutes)
    for tid, lv in run.levels[minutes].items():
        if lv is not None:
            data["initial_levels_pct"][tid] = round(lv, 2)


def from_team_shape(d: dict) -> dict:
    """Converts scenario/field.json into the backend shape.

    The forecast starts at the first fault. `_preroll` replays the minutes
    between clock_start and the fault so the starting levels are right.
    A pump with no setpoint pumps down toward `pump_low_pct`.
    """
    faults = d.get("faults", [])
    start_offset = min((f.get("at_min", 0) for f in faults), default=0)
    start = _add_minutes(d.get("clock_start", "00:00"), start_offset)
    levels = dict(d.get("initial", {}).get("tank_pct", {}))
    pumps_by_id = {p.get("id"): p for p in d.get("pumps", []) if isinstance(p, dict)}

    tanks = []
    for t in d.get("tanks", []):
        t = dict(t)
        # A running pump works the tank down toward pump_low_pct, as in scenario/.
        t.setdefault("setpoint_pct", t.get("pump_low_pct", levels.get(t["id"], 50)))
        tanks.append(t)
    valves = [{**v, "position": v.get("default", v.get("position", "CLOSED"))}
              for v in d.get("valves", []) if isinstance(v, dict)]
    trucks = []
    for tr in d.get("trucks", []):
        tr = dict(tr)
        tr.setdefault("load_min", tr.get("load_minutes", 20))
        tr.setdefault("haul_to_swd_min", tr.get("minutes_to_disposal", 30))
        trucks.append(tr)

    failures = []
    for f in faults:
        aid = str(f.get("asset_id", ""))
        pump = pumps_by_id.get(aid)
        tank = str(pump.get("tank_id", "")).replace("TANK_", "Tank ") if pump else None
        name = aid.replace("PUMP_", "Pump ")
        failures.append({
            "asset_id": aid,
            "at_clock": _add_minutes(d.get("clock_start", "00:00"), f.get("at_min", 0)),
            "reason": f.get("reason", "Failed."),
            "summary": f"{name} failed. {tank} is filling." if tank else f"{name} failed.",
        })

    field = {"tanks": tanks, "pumps": d.get("pumps"), "valves": valves, "wells": d.get("wells"),
             "disposal_wells": d.get("disposal_wells"), "trucks": trucks}
    for extra in ("lead_min", "costs", "yards"):
        if extra in d:
            field[extra] = d[extra]
    return {"start_clock": start, "field": field, "initial_levels_pct": levels, "failures": failures,
            "_preroll_min": start_offset, "_clock_start": d.get("clock_start", "00:00")}


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
