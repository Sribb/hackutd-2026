"""Loads the scenario file and builds the /api/field and /api/state payloads.

The scenario path defaults to backend/data/scenario.json. Set FREEBOARD_SCENARIO
to point at another file with the same shape.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIO = BACKEND_DIR / "data" / "scenario.json"

FIELD_KEYS = ("tanks", "pumps", "valves", "wells", "disposal_wells", "trucks")


def load_scenario(path: str | Path | None = None) -> dict:
    path = Path(path or os.environ.get("FREEBOARD_SCENARIO") or DEFAULT_SCENARIO)
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in FIELD_KEYS if k not in data.get("field", {})]
    if missing:
        raise ValueError(f"{path}: field is missing {missing}")
    return data


def field_payload(scenario: dict) -> dict:
    """GET /api/field: everything that doesn't change during the night."""
    return {k: scenario["field"][k] for k in FIELD_KEYS}


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
        if tid not in levels:
            assets[tid] = {"status": "WARNING", "level_pct": None,
                           "reason": "No level reading. Treat as unknown, not safe."}
            continue
        level = float(levels[tid])
        if tid in failed_tanks or level >= tank["shutdown_pct"]:
            status = "ALARM"
        elif level >= tank["shutdown_pct"] - 10:
            status = "WARNING"
        else:
            status = "OK"
        assets[tid] = {"status": status, "level_pct": level}

    for valve in field["valves"]:
        position = "CLOSED" if valve["id"].startswith("VALVE_XO") else "OPEN"
        assets[valve["id"]] = {"status": "OK", "position": position}

    for well in field["wells"]:
        assets[well["id"]] = {"status": "OK"}
    for swd in field["disposal_wells"]:
        assets[swd["id"]] = {"status": "OK"}
    for truck in field["trucks"]:
        assets[truck["id"]] = {"status": "OK", "position": "YARD_1"}

    incident = None
    if failures:
        root = next(iter(failures.values()))
        incident = {"root_asset_id": root["asset_id"], "summary": root["summary"]}

    return {"clock": scenario.get("start_clock", "00:00"), "assets": assets, "incident": incident}
