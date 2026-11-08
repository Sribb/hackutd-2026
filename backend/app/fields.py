"""The active field: which scenario or blueprint the whole backend runs on, plus
the actions already carried out on it.

Every endpoint reads through `snapshot()`, so switching fields is atomic and can
never mix IDs from two fields. Executed commands land in `applied`. /api/state
shows them, and every forecast and plan starts from them.
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field as dc_field

from .forecast import Plant, build_plant
from .scenario import state_payload

_lock = threading.RLock()


@dataclass(frozen=True)
class Snapshot:
    scenario: dict
    plant: Plant
    applied: tuple          # executed actions, oldest first, each with at_min 0
    blueprint_id: str | None
    version: int


@dataclass
class _Active:
    scenario: dict
    plant: Plant
    applied: list = dc_field(default_factory=list)
    blueprint_id: str | None = None
    version: int = 0


_active: _Active | None = None
_listeners: list = []


def on_change(fn) -> None:
    """fn(snapshot) runs after every activate or apply, e.g. to push to SpacetimeDB.
    A listener that raises is logged and skipped, never breaking the change."""
    _listeners.append(fn)


def _notify(snap) -> None:
    for fn in list(_listeners):
        try:
            fn(snap)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("freeboard.fields").exception("field listener failed")


def activate(scenario: dict, blueprint_id: str | None = None) -> Snapshot:
    """Makes `scenario` (already passed through scenario.prepare) the active field."""
    global _active
    sc = copy.deepcopy(scenario)
    with _lock:
        version = (_active.version + 1) if _active else 1
        _active = _Active(scenario=sc, plant=build_plant(sc), blueprint_id=blueprint_id, version=version)
        snap = snapshot()
    _notify(snap)
    return snap


def snapshot() -> Snapshot:
    with _lock:
        if _active is None:
            raise RuntimeError("No active field. Call fields.activate() at startup.")
        return Snapshot(_active.scenario, _active.plant, tuple(_active.applied),
                        _active.blueprint_id, _active.version)


def apply(action: dict, expected_version: int | None = None) -> Snapshot:
    """Records an executed action. RESET_PUMP repairs a tripped pump (simulated only)."""
    with _lock:
        if _active is None:
            raise RuntimeError("No active field.")
        if expected_version is not None and expected_version != _active.version:
            raise RuntimeError("The active field changed, so this action was not applied.")
        if action["type"] == "RESET_PUMP":
            pid = action["asset_id"]
            _active.scenario["failures"] = [f for f in _active.scenario.get("failures", [])
                                            if f.get("asset_id") != pid]
            _active.plant = build_plant(_active.scenario)
        else:
            _active.applied.append({**action, "at_min": 0.0})
        snap = snapshot()
    _notify(snap)
    return snap


def live_state(snap: Snapshot | None = None) -> dict:
    """/api/state with executed actions shown on the assets they changed."""
    snap = snap or snapshot()
    st = state_payload(snap.scenario)
    assets = st["assets"]
    for a in snap.applied:
        aid, typ = a["asset_id"], a["type"]
        if aid not in assets:
            continue
        if typ == "SET_VALVE":
            assets[aid]["position"] = a["value"]
        elif typ == "SET_PUMP":
            if a["value"] == "OFF":
                assets[aid].update(status="OFFLINE", reason="Turned off by an approved command.")
            elif assets[aid].get("status") == "OFFLINE":
                assets[aid] = {"status": "OK"}
        elif typ == "CHOKE_WELL":
            kept = float(a["value"])
            if kept == 0:
                assets[aid] = {"status": "OFFLINE", "reason": "Shut in by an approved command."}
            elif kept < 1:
                assets[aid] = {"status": "WARNING", "reason": f"Choked to {round(kept * 100)}% by an approved command."}
            else:
                assets[aid] = {"status": "OK"}
        elif typ == "SEND_TRUCK":
            assets[aid]["position"] = a.get("target_id", assets[aid].get("position"))
            assets[aid]["reason"] = f"Dispatched to {a.get('target_id')}."
    st["blueprint_id"] = snap.blueprint_id
    st["applied_actions"] = list(snap.applied)
    return st
