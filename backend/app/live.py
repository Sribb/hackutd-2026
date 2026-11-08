"""The live field: readings from connectors, detection, alerts, and human-confirmed actions.

Every tick (TICK_S real seconds, FREEBOARD_SPEED live minutes per real second):
  1. the simulator advances (faults fire on schedule),
  2. every connector is read and the readings are stored with their time,
  3. detect.py looks for pump trips, stale sensors, and valve mismatches,
  4. `state`, plus any new or resolved `alert`, goes out over /ws.

Actions: Jarvis or the console *proposes* a command. It is stored PENDING and changes
nothing. Only POST /api/actions/{id}/confirm sends it through the asset's connector.
JARVIS_CONFIRM=0 lets Jarvis skip the confirm, but only for commands that go to the
simulator: a command for real equipment always waits for a person.

This layer is additive. GET /api/state and the forecast keep reading the scenario file.
"""
from __future__ import annotations

import asyncio
import copy
import itertools
import logging
import os
from collections import defaultdict
from typing import Optional

from .connectors import Registry
from .connectors.sim import _fraction, _on
from .detect import Detector, new_history, now_iso
from .fieldmap import COMMANDS, FieldMap, name
from .hub import Hub
from .notify import Notifier

log = logging.getLogger("freeboard.live")

TICK_S = 1.0


class ActionError(Exception):
    """A proposal or confirmation that can't go ahead. `status` is the HTTP code."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def describe(a: dict) -> str:
    typ, aid = a["type"], a["asset_id"]
    if typ == "SET_VALVE":
        return f"{'Open' if a['value'] == 'OPEN' else 'Close'} {name(aid)}."
    if typ == "SET_PUMP":
        return f"Turn {'on' if a['value'] == 'ON' else 'off'} {name(aid)}."
    if typ == "CHOKE_WELL":
        return f"Shut in {name(aid)}." if a["value"] == 0 else f"Choke {name(aid)} to {a['value']:.0%} of its flow."
    return f"Send {name(aid)} to {name(a['target_id'])}."


def start_conditions(scenario: dict, raw: Optional[dict]) -> tuple[str, dict, list, dict, list]:
    """(start clock, initial tank %, faults, sensors, connectors) from the raw file when it
    is a blueprint (so faults fire at their at_min), else from the backend shape."""
    raw = raw or {}
    if "tanks" in raw:
        return (raw.get("clock_start", "00:00"), dict(raw.get("initial", {}).get("tank_pct", {})),
                list(raw.get("faults", [])), dict(raw.get("sensors", {})), list(raw.get("connectors") or []))
    faults = [{"asset_id": f["asset_id"], "type": "TRIP", "at_min": 0, "reason": f.get("reason")}
              for f in scenario.get("failures", [])]
    return (scenario.get("start_clock", "00:00"), dict(scenario.get("initial_levels_pct", {})), faults,
            dict(raw.get("sensors") or scenario.get("sensors") or {}),
            list(raw.get("connectors") or scenario.get("connectors") or []))


class LiveField:
    def __init__(self, scenario: dict, raw: Optional[dict] = None, hub: Optional[Hub] = None) -> None:
        start, initial, faults, sensors, specs = start_conditions(scenario, raw)
        self.scenario = scenario
        self.fmap = FieldMap.from_scenario(scenario, start)
        self.t = 0.0
        self.registry = Registry(specs, self.fmap, lambda: self.t, initial_pct=initial,
                                 faults=faults, sensors=sensors)
        self.detector = Detector(self.fmap, float(sensors.get("stale_after_min", 5)))
        self.latest: dict[tuple, dict] = {}
        self.history: dict[tuple, object] = defaultdict(new_history)
        self.commanded: dict[str, dict] = {}       # asset id -> {"value", "t"} from confirmed commands
        self.proposals: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self.hub = hub or Hub()
        self.notifier = Notifier()
        self.speed = float(os.environ.get("FREEBOARD_SPEED", "0.25"))
        self.paused = False
        self.auto_confirm_sim = os.environ.get("JARVIS_CONFIRM", "1") == "0"
        self._task: Optional[asyncio.Task] = None
        self.ingest(self.registry.read())

    # ---------- loop ----------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        self.registry.close()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            if self.paused:
                continue
            try:
                await self.tick(self.speed * TICK_S)
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("live tick failed")

    def step(self, dt_min: float) -> list[dict]:
        """Advance, read, detect. Returns alerts raised or resolved. Synchronous."""
        self.registry.advance(dt_min)
        self.t += dt_min
        self.ingest(self.registry.read())
        return self.detector.evaluate(self.t, self.latest, self.history, self.commanded, self.fmap.clock(self.t))

    async def tick(self, dt_min: float) -> list[dict]:
        changed = self.step(dt_min)
        await self.publish(changed)
        return changed

    async def publish(self, changed: list[dict]) -> None:
        await self.hub.broadcast("state", {"state": self.state()})
        for alert in changed:
            await self.hub.broadcast("alert", {"alert": alert})
            self.notifier.push(alert)
            if alert["active"] and alert["severity"] == "ALARM":
                await self.focus(alert["asset_id"], alert["message"], source="detector")

    def ingest(self, readings: list[dict]) -> None:
        for r in readings:
            key = (r["asset_id"], r["signal"])
            self.latest[key] = {"value": r["value"], "t": r["t"], "source": r.get("source"), "wall": now_iso()}
            self.history[key].append((r["t"], r["value"]))

    async def focus(self, asset_id: str, reason: str = "", source: str = "jarvis") -> dict:
        directive = {"type": "ui", "action": "focus", "asset_id": asset_id}
        await self.hub.broadcast("ui", {"action": "focus", "asset_id": asset_id, "reason": reason, "source": source})
        return directive

    # ---------- views ----------
    def value(self, asset_id: str, signal: str):
        r = self.latest.get((asset_id, signal))
        return None if r is None else r["value"]

    def state(self) -> dict:
        f, d = self.fmap, self.detector
        assets: dict[str, dict] = {}
        alarm_assets = {a["asset_id"] for a in d.active.values() if a["severity"] == "ALARM"}

        def reason(aid: str) -> Optional[str]:
            msgs = [a["message"] for a in d.active_for(aid)]
            return " ".join(msgs) or None

        for tid, tank in f.tanks.items():
            lvl = self.value(tid, "level_pct")
            stale = lvl is None or d.is_stale(tid, "level_pct")
            pumps_down = any(p in alarm_assets for p in f.pumps_by_tank.get(tid, []))
            if stale:
                st = "UNKNOWN"
            elif lvl >= tank["shutdown_pct"] or pumps_down or tid in alarm_assets:
                st = "ALARM"
            elif lvl >= f.warning_pct(tid) or d.active_for(tid):
                st = "WARNING"
            else:
                st = "OK"
            assets[tid] = {"status": st, "level_pct": lvl, "stale": stale, "reason": reason(tid),
                           **d.rates.get(tid, {})}
            if stale and not assets[tid]["reason"]:
                assets[tid]["reason"] = "No level reading. Treat as unknown, not safe."
        for pid in f.pumps:
            stale = not any(k[0] == pid and k not in d.stale_keys for k in self.latest)
            off = str(self.commanded.get(pid, {}).get("value", "ON")) == "OFF"
            st = "ALARM" if pid in alarm_assets else "UNKNOWN" if stale else "OFFLINE" if off else "OK"
            assets[pid] = {"status": st, "running": self.value(pid, "running"),
                           "flow_bbl_hr": self.value(pid, "flow_bbl_hr"), "stale": stale, "reason": reason(pid)}
        for vid in f.valves:
            stale = d.is_stale(vid, "position") or self.value(vid, "position") is None
            st = "UNKNOWN" if stale else "WARNING" if d.active_for(vid) else "OK"
            assets[vid] = {"status": st, "position": self.value(vid, "position"), "stale": stale,
                           "reason": reason(vid)}
        for wid in f.wells:
            flow = self.value(wid, "flow_bbl_hr")
            stale = flow is None or d.is_stale(wid)
            st = "UNKNOWN" if stale else "OFFLINE" if flow <= 0.05 else "OK"
            assets[wid] = {"status": st, "flow_bbl_hr": flow, "stale": stale, "reason": reason(wid)}
        for sid, s in f.swds.items():
            flow = self.value(sid, "flow_bbl_hr")
            stale = flow is None or d.is_stale(sid)
            st = "UNKNOWN" if stale else "WARNING" if flow >= 0.95 * float(s["max_bbl_hr"]) else "OK"
            assets[sid] = {"status": st, "flow_bbl_hr": flow, "stale": stale}
        for trid in f.trucks:
            assets[trid] = {"status": "OK", "stale": False, **self.registry.sim.truck_phase(trid)}
        last: dict[str, dict] = {}
        for (aid, _), r in self.latest.items():
            if aid not in last or r["t"] > last[aid]["t"]:
                last[aid] = r
        for aid, entry in assets.items():
            r = last.get(aid)
            entry["age_min"] = round(self.t - r["t"], 2) if r else None
            entry["last_reading_t"] = r["wall"] if r else None
        return {"clock": f.clock(self.t), "t": round(self.t, 2), "assets": assets,
                "connectors": self.registry.status(), "speed": self.speed, "paused": self.paused}

    def alerts(self) -> list[dict]:
        return list(self.detector.alerts)

    def live_scenario(self) -> dict:
        """A copy of the scenario with the live readings, for the forecast and planner.
        A stale tank has no level, so the forecast warns instead of calling it safe."""
        sc = copy.deepcopy(self.scenario)
        sc["start_clock"] = self.fmap.clock(self.t)
        sc["initial_levels_pct"] = {
            tid: (None if self.detector.is_stale(tid, "level_pct") or self.value(tid, "level_pct") is None
                  else float(self.value(tid, "level_pct"))) for tid in self.fmap.tanks}
        sc["failures"] = [
            {"asset_id": a["asset_id"], "at_clock": a["clock"], "reason": a["message"],
             "summary": f"{name(a['asset_id'])} failed. {name(self.fmap.pumps[a['asset_id']]['tank_id'])} is filling."}
            for a in self.detector.active.values() if a["kind"] == "PUMP_TRIP" and a["asset_id"] in self.fmap.pumps]
        for v in sc["field"]["valves"]:
            pos = self.value(v["id"], "position")
            if pos in ("OPEN", "CLOSED"):
                v["position"] = pos
        return sc

    # ---------- actions ----------
    def clean_action(self, action: dict) -> dict:
        f = self.fmap
        typ = str(action.get("type", "")).upper()
        aid = str(action.get("asset_id", "")).upper()
        val = action.get("value")
        if typ not in COMMANDS:
            raise ActionError(422, f"Unknown action type {typ!r}. Use one of {', '.join(COMMANDS)}.")
        if typ == "SET_VALVE":
            on = _on(val)
            if aid not in f.valves or on is None:
                raise ActionError(422, f"SET_VALVE needs a valve ID and OPEN or CLOSED, got {aid} {val!r}.")
            return {"type": typ, "asset_id": aid, "value": "OPEN" if on else "CLOSED"}
        if typ == "SET_PUMP":
            on = _on(val)
            if aid not in f.pumps or on is None:
                raise ActionError(422, f"SET_PUMP needs a pump ID and ON or OFF, got {aid} {val!r}.")
            return {"type": typ, "asset_id": aid, "value": "ON" if on else "OFF"}
        if typ == "CHOKE_WELL":
            frac = _fraction(val)
            if aid not in f.wells or frac is None:
                raise ActionError(422, f"CHOKE_WELL needs a well ID and a value from 0 (shut in) to 1, got {aid} {val!r}.")
            return {"type": typ, "asset_id": aid, "value": frac}
        target = str(action.get("target_id") or "").upper()
        if aid not in f.trucks or target not in f.trucks[aid].get("minutes_to", {}):
            raise ActionError(422, f"SEND_TRUCK needs a truck ID and a target_id tank it can reach, got {aid} {target!r}.")
        return {"type": typ, "asset_id": aid, "value": None, "target_id": target}

    async def propose(self, action: dict, reason: str = "", source: str = "console") -> dict:
        """Store a PENDING proposal. Changes nothing in the field."""
        a = self.clean_action(action)
        conn = self.registry.route(a)
        p = {"id": f"act_{next(self._ids)}", "action": a, "reason": (reason or describe(a)).strip(),
             "status": "PENDING", "created_t": now_iso(), "text": describe(a), "source": source,
             "connector_id": conn.id}
        self.proposals[p["id"]] = p
        await self.hub.broadcast("proposal", {"proposal": p})
        await self.focus(a["asset_id"], f"Waiting for confirmation: {p['text']}", source=source)
        if self.auto_confirm_sim and source == "jarvis" and conn is self.registry.sim:
            return await self.confirm(p["id"], by="JARVIS_CONFIRM=0")
        return p

    async def confirm(self, action_id: str, by: str = "") -> dict:
        p = self._pending(action_id)
        a = p["action"]
        conn = self.registry.route(a)
        if conn is self.registry.sim:
            res = conn.send(a)
        else:
            res = await asyncio.to_thread(conn.send, a)
        ok = bool(res.get("ok"))
        p.update(status="CONFIRMED" if ok else "FAILED", result=res.get("detail", ""),
                 decided_t=now_iso(), decided_by=by or None, connector_id=conn.id)
        if ok and a["type"] in ("SET_VALVE", "SET_PUMP"):
            self.commanded[a["asset_id"]] = {"value": a["value"], "t": self.t}
        await self.hub.broadcast("proposal", {"proposal": p})
        await self.focus(a["asset_id"], p["result"], source="action")
        await self.hub.broadcast("state", {"state": self.state()})
        return p

    async def reject(self, action_id: str, by: str = "") -> dict:
        p = self._pending(action_id)
        p.update(status="REJECTED", decided_t=now_iso(), decided_by=by or None)
        await self.hub.broadcast("proposal", {"proposal": p})
        return p

    def _pending(self, action_id: str) -> dict:
        p = self.proposals.get(action_id)
        if p is None:
            raise ActionError(404, f"No proposal {action_id}.")
        if p["status"] != "PENDING":
            raise ActionError(409, f"Proposal {action_id} is already {p['status']}.")
        return p
