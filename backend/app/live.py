"""The live field: readings from connectors, detection, alerts, and human-confirmed actions.

Every tick (TICK_S real seconds, FREEBOARD_SPEED live minutes per real second):
  1. the simulator advances (faults fire on schedule),
  2. every connector is read and the readings are stored with their time,
  3. detect.py looks for pump trips, stale sensors, and valve mismatches,
  4. `state`, plus any new or resolved `alert`, goes out over /ws.

Actions: Jarvis or the console *proposes* a command. It is stored PENDING and changes
nothing. Only POST /api/actions/{id}/confirm sends it through the asset's connector:
the proposal is claimed (SENDING) before anything awaits, so it is sent at most once.
JARVIS_CONFIRM=0 lets Jarvis skip the confirm, but only for commands that go to the
simulator: a command for real equipment always waits for a person.

A send whose answer never came back (a connector result with `"uncertain": True`, or a
real connector that raised) is UNCERTAIN, never FAILED: it may have moved the device, so
nothing sends it again and a person checks the field. A confirm whose command path (the
connector, its config, the command's address) is not the one it was proposed on is
refused with 409. A reload between the claim and the send ends the proposal FAILED with
nothing sent (`generation` counts reloads); a send already under way keeps its result, but
a reload in between never records it in `commanded`.

Every proposal, decision, send result, alert ack, shelve, and unshelve goes into `audit`
(audit.py, CF-10), with who did it: `role` (operator or phone, from the control token, see
auth.py) and `source` (console, phone, jarvis, blueprint, timer).

Alert priority (CF-7): after each evaluation that raises or resolves an alert, and at least
every REPRIORITIZE_MIN live minutes while any alert is active, the baseline forecast (no
actions) is rerun from the live readings and every active alert gets its priority, minutes
to consequence, and dollars at risk (detect.consequence_of). An alert whose priority, root,
or consequence moved goes out again over /ws as `{"type": "alert", "event": "updated"}`.
A shelved alert comes back by itself at `shelved_until_t` (`"event": "unshelved"`).

This layer is additive. GET /api/state and the forecast keep reading the scenario file.
reload() points it at a newly activated blueprint (live_api installs app.state.live_reload).
"""
from __future__ import annotations

import asyncio
import copy
import itertools
import logging
import os
import threading
from collections import defaultdict
from typing import Optional

from .audit import AuditLog
from .connectors import Registry
from .connectors.base import UNCERTAIN_DETAIL
from .connectors.sim import _fraction, _on
from .detect import Detector, consequence_of, new_history, now_iso
from .fieldmap import COMMANDS, FieldMap, name
from .forecast import DEFAULT_COSTS, forecast
from .hub import Hub
from .notify import Notifier

log = logging.getLogger("freeboard.live")

TICK_S = 1.0
HOLD = None   # dataset mode (#153): eog/legacy_api.py sets a callable; while it returns True the field stands still
REPRIORITIZE_MIN = 1.0       # rerun the baseline forecast for priorities at least this often (live min)
PRIORITY_HORIZON_MIN = 360   # the forecast's own horizon: past it, an alarm is P3
MOVED_MIN = 2.0              # a consequence that moves this much is broadcast again
SHELVE_MIN, SHELVE_MAX = 15, 240   # live minutes an alert may be shelved for (CF-7)
RELOADED_BEFORE_SEND = "The field was reloaded before it was sent; nothing moved. Propose it again."


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
        self.detector = Detector(self.fmap, float(sensors.get("stale_after_min", 5)), declared=self.registry.owner)
        self.latest: dict[tuple, dict] = {}
        self.history: dict[tuple, object] = defaultdict(new_history)
        self.commanded: dict[str, dict] = {}       # asset id -> {"value", "t"} from confirmed commands
        self.proposals: dict[str, dict] = {}
        self._paths: dict[str, tuple] = {}         # proposal id -> Registry.path() when it was proposed
        self.generation = 0                        # +1 on every reload(); a send checks it first
        self._gen_lock = threading.Lock()          # the generation check (worker thread) vs reload()
        self._ids = itertools.count(1)
        self.hub = hub or Hub()
        self.notifier = Notifier()
        self.speed = float(os.environ.get("FREEBOARD_SPEED", "0.25"))
        self.paused = False
        self.auto_confirm_sim = os.environ.get("JARVIS_CONFIRM", "1") == "0"
        self.audit = AuditLog(lambda: (self.fmap.clock(self.t), round(self.t, 2)))
        self.updates: list[tuple[str, dict]] = []    # (event, alert) from the last step, for publish
        self._prio_t: Optional[float] = None         # live time priorities were last computed
        self._told: dict[str, tuple] = {}            # alert id -> (priority, consequence asset, time) last broadcast
        self._task: Optional[asyncio.Task] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None   # the loop start() ran on
        self.brief_hook = None                       # jarvis_tools.BriefFeed.poke, set by live_api
        self.ingest(self.registry.read())

    # ---------- loop ----------
    def start(self) -> None:
        if self._task is None:
            self.loop = asyncio.get_running_loop()
            self._task = self.loop.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        self.registry.close()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            if self.paused or (HOLD is not None and HOLD()):
                continue
            try:
                await self.tick(self.speed * TICK_S)
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("live tick failed")

    def step(self, dt_min: float) -> list[dict]:
        """Advance, read, detect, then rank. Returns alerts raised or resolved; alerts that
        only changed (priority, root, shelf) are left in self.updates for publish. Synchronous."""
        self.registry.advance(dt_min)
        self.t += dt_min
        self.ingest(self.registry.read())
        changed = self.detector.evaluate(self.t, self.latest, self.history, self.commanded, self.fmap.clock(self.t))
        self.updates = self._follow_up(changed)
        return changed

    async def tick(self, dt_min: float) -> list[dict]:
        changed = self.step(dt_min)
        updates, self.updates = self.updates, []
        await self.publish(changed, updates)
        return changed

    async def publish(self, changed: list[dict], updates: list[tuple[str, dict]] = ()) -> None:
        await self.hub.broadcast("state", {"state": self.state()})
        for alert in changed:
            await self.hub.broadcast("alert", {"alert": alert, "event": "raised" if alert["active"] else "resolved"})
            self.notifier.push(alert)
            if alert["active"] and alert["severity"] == "ALARM":
                await self.focus(alert["asset_id"], alert["message"], source="detector")
        for event, alert in updates:
            await self.hub.broadcast("alert", {"alert": alert, "event": event})
            if event == "unshelved":
                self.notifier.push(alert)   # the shelf ran out and the alarm still holds: it is back
        self._poke_brief(changed)

    # ---------- priority, root cause, and shelving (CF-7) ----------
    def _follow_up(self, changed: list[dict]) -> list[tuple[str, dict]]:
        """After an evaluation: end the shelf of any alert that resolved, link roots that
        appeared, unshelve what ran out, and rerank. Returns (event, alert) to broadcast."""
        for a in changed:
            if not a["active"] and a.get("shelved_until_t") is not None:
                self._end_shelf(a, "resolved", source="detector")
        out: dict[str, tuple[str, dict]] = {}
        for a in self.detector.link_roots():
            out[a["id"]] = ("updated", a)
        for a in self.prioritize(force=bool(changed) or bool(out)):
            out.setdefault(a["id"], ("updated", a))
        for a in self._expire_shelves():
            out[a["id"]] = ("unshelved", a)
        raised = {a["id"] for a in changed}
        return [u for aid, u in out.items() if aid not in raised]

    def prioritize(self, force: bool = False) -> list[dict]:
        """Rank every active alert from the baseline forecast of the live field. Runs when
        forced, or when REPRIORITIZE_MIN live minutes have passed. Returns the alerts whose
        priority, root, or consequence moved enough to tell the screens again."""
        active = list(self.detector.active.values())
        if not active:
            self._prio_t = None
            return []
        if not force and self._prio_t is not None and self.t - self._prio_t < REPRIORITIZE_MIN - 1e-9:
            return []
        self._prio_t = self.t
        sc = self.live_scenario()
        try:
            fc = forecast(sc, [], horizon=PRIORITY_HORIZON_MIN, step=60)
        except Exception:  # noqa: BLE001 - a forecast problem must never stop detection
            log.exception("priority forecast failed")
            return []
        shutdowns: dict[str, dict] = {}
        for ev in fc["events"]:
            if ev["type"] == "SHUTDOWN_LEVEL":
                shutdowns.setdefault(ev["asset_id"], ev)
        costs = {**DEFAULT_COSTS, **(sc.get("field", {}).get("costs") or {})}
        restart = float(costs.get("well_restart_usd") or 0)
        moved = []
        self._told = {k: v for k, v in self._told.items() if k in {a["id"] for a in active}}
        # A tank with no fresh level has no forecast shutdown; that must never read as safe.
        unknown = {tid for tid in self.fmap.tanks
                   if self.value(tid, "level_pct") is None or self.detector.is_stale(tid, "level_pct")}
        for a in active:
            new = consequence_of(self.fmap, a, shutdowns, PRIORITY_HORIZON_MIN, restart,
                                 now_t=self.t, start_clock=sc["start_clock"], unknown_tanks=unknown)
            at, told = new["consequence_t"], self._told.get(a["id"])
            # Broadcast again only when the priority or the consequence changed, or its time
            # moved MOVED_MIN from what the screens were last told. A countdown is not news,
            # and neither is a shutdown that already happened being "now" again.
            moved_time = at is not None and told is not None and told[2] is not None and \
                abs(at - told[2]) >= MOVED_MIN and not (told[2] <= self.t and at <= self.t + 1.0)
            if (told is None or told[0] != new["priority"] or told[1] != new["consequence_asset_id"]
                    or (told[2] is None) != (at is None) or moved_time):
                self._told[a["id"]] = (new["priority"], new["consequence_asset_id"], at)
                moved.append(a)
            a.update(new)
        return moved

    def _alert(self, alert_id: str) -> dict:
        for a in self.detector.alerts:
            if a["id"] == alert_id:
                return a
        raise ActionError(404, f"No alert {alert_id}.")

    async def shelve(self, alert_id: str, minutes: float, reason: str, by: str = "",
                     role: Optional[str] = None, source: Optional[str] = None) -> dict:
        """Shelve an active alert for SHELVE_MIN to SHELVE_MAX live minutes, with a reason.
        It stays active (and keeps its priority current); it comes back at the end by
        itself. Shelving again replaces the timer. Logged as `shelved`."""
        a = self._alert(alert_id)
        why = (reason or "").strip()
        if not why:
            raise ActionError(422, "A reason is required to shelve an alert.")
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            minutes = -1.0
        if not SHELVE_MIN <= minutes <= SHELVE_MAX:
            raise ActionError(422, f"Shelve for {SHELVE_MIN} to {SHELVE_MAX} minutes.")
        if not a["active"]:
            raise ActionError(409, f"Alert {alert_id} has already cleared, so there is nothing to shelve.")
        until = self.t + minutes
        a.update(shelved_until_t=round(until, 2), shelved_until=self.fmap.clock_text(until), shelve_reason=why,
                 shelved_by=by or None, shelved_t=now_iso())
        self.audit.append("shelved", source=source or by or None, role=role, by=by or None,
                          asset_id=a["asset_id"], alert_id=a["id"], kind=a["kind"], priority=a.get("priority"),
                          minutes=minutes, reason=why, shelved_until=a["shelved_until"],
                          shelved_until_t=a["shelved_until_t"])
        await self.hub.broadcast("alert", {"alert": a, "event": "shelved"})
        return a

    async def unshelve(self, alert_id: str, by: str = "", role: Optional[str] = None,
                       source: Optional[str] = None) -> dict:
        """Bring a shelved alert back before its timer ends. 409 if it isn't shelved."""
        a = self._alert(alert_id)
        if a.get("shelved_until_t") is None:
            raise ActionError(409, f"Alert {alert_id} is not shelved.")
        self._end_shelf(a, "by hand", source=source or by or None, role=role, by=by or None)
        await self.hub.broadcast("alert", {"alert": a, "event": "unshelved"})
        return a

    def _end_shelf(self, a: dict, why: str, source: Optional[str], role: Optional[str] = None,
                   by: Optional[str] = None) -> dict:
        was = {"shelve_reason": a.get("shelve_reason"), "shelved_until": a.get("shelved_until")}
        a.update(shelved_until=None, shelved_until_t=None, shelve_reason=None, shelved_by=None, shelved_t=None)
        self.audit.append("unshelved", source=source, role=role, by=by, asset_id=a["asset_id"], alert_id=a["id"],
                          kind=a["kind"], priority=a.get("priority"), why=why, **was)
        return a

    def _expire_shelves(self) -> list[dict]:
        out = []
        for a in list(self.detector.active.values()):
            until = a.get("shelved_until_t")
            if until is not None and self.t >= until - 1e-9:
                out.append(self._end_shelf(a, "expired", source="timer"))
        return out

    # ---------- following the active blueprint ----------
    def reload(self, scenario: dict, raw: Optional[dict] = None) -> dict:
        """Swap in a newly activated field. Synchronous with no await, so no tick, confirm,
        or proposal can run between tearing the old field down and building the new one.

        Stops the old connectors and builds the new FieldMap, connector registry, and
        detector. Keeps the live clock (`t` and the start clock), speed, pause, the hub,
        and every proposal and alert record (alert ids keep counting). Then:
          - every active alert resolves, with `resolved_reason` (the new detector raises it
            again from fresh readings if it still holds);
          - a PENDING proposal closes as REJECTED by "blueprint", with `result` saying why,
            when its asset or target is gone, it is no longer valid, or it would now go to
            a different connector: a person never confirms a command routed somewhere
            other than where it was proposed;
          - readings start over from the new connectors.
        Returns {"assets", "resolved_alerts", "closed_proposals"} for publish_reload()."""
        start, initial, faults, sensors, specs = start_conditions(scenario, raw)
        old_det = self.detector
        with self._gen_lock:   # a claimed proposal that hasn't started sending now never will
            self.generation += 1
        self.registry.close()
        self.scenario = scenario
        self.fmap = FieldMap.from_scenario(scenario, self.fmap.start_clock)
        self.registry = Registry(specs, self.fmap, lambda: self.t, initial_pct=initial,
                                 faults=faults, sensors=sensors)
        self.detector = Detector(self.fmap, float(sensors.get("stale_after_min", 5)), declared=self.registry.owner)
        self.detector._ids, self.detector.alerts = old_det._ids, old_det.alerts
        present = set(self.fmap.all_ids())

        resolved = []
        for alert in old_det.active.values():
            aid = alert["asset_id"]
            alert.update(active=False, resolved_t=now_iso(), resolved_reason=(
                f"{name(aid)} is not in the activated blueprint." if aid not in present else
                "The field was reloaded from a new blueprint. It is checked again from fresh readings."))
            if alert.get("shelved_until_t") is not None:
                self._end_shelf(alert, "reloaded", source="blueprint")
            resolved.append(alert)
        self._prio_t = None
        self._told = {}
        self.updates = []

        closed = []
        for p in self.proposals.values():
            why = self._invalid_after_reload(p, present) if p["status"] == "PENDING" else None
            if why:
                p.update(status="REJECTED", decided_t=now_iso(), decided_by="blueprint", result=why)
                self._log(p, "rejected", source="blueprint", by="blueprint", result=why)
                closed.append(p)

        # A confirmed position only still holds for real equipment; the new simulator starts over.
        def still_holds(aid: str) -> bool:
            typ = {"valve": "SET_VALVE", "pump": "SET_PUMP"}.get(self.fmap.kind_of(aid) or "")
            return bool(typ) and self.registry.route({"type": typ, "asset_id": aid}) is not self.registry.sim
        self.commanded = {aid: c for aid, c in self.commanded.items() if still_holds(aid)}
        self.latest.clear()
        self.history.clear()
        self.ingest(self.registry.read())
        return {"assets": len(present), "resolved_alerts": resolved, "closed_proposals": closed}

    def _invalid_after_reload(self, p: dict, present: set) -> Optional[str]:
        a = p["action"]
        gone = [x for x in (a.get("asset_id"), a.get("target_id")) if x and x not in present]
        if gone:
            return f"Closed: {name(gone[0])} is not in the activated blueprint."
        try:
            self.clean_action(a)
        except ActionError as e:
            return f"Closed: it is no longer valid in the activated blueprint. {e.detail}"
        conn = self.registry.route(a)
        if conn.id != p.get("connector_id"):
            return (f"Closed: in the activated blueprint it would go to {conn.id}, not {p.get('connector_id')}. "
                    f"Propose it again to send it there.")
        if self.registry.path(a) != self._paths.get(p["id"]):
            return (f"Closed: {conn.id} is set up differently in the activated blueprint (its url, address, "
                    f"or settings changed). Propose it again to send it there.")
        return None

    async def publish_reload(self, changes: dict) -> None:
        for p in changes.get("closed_proposals", []):
            await self.hub.broadcast("proposal", {"proposal": p})
        for alert in changes.get("resolved_alerts", []):
            await self.hub.broadcast("alert", {"alert": alert, "event": "resolved"})
        await self.hub.broadcast("state", {"state": self.state()})
        self._poke_brief(changes.get("resolved_alerts") or [], force=True)

    async def reload_and_publish(self, scenario: dict, raw: Optional[dict] = None) -> dict:
        changes = self.reload(scenario, raw)
        await self.publish_reload(changes)
        return changes

    def ingest(self, readings: list[dict]) -> None:
        for r in readings:
            key = (r["asset_id"], r["signal"])
            self.latest[key] = {"value": r["value"], "t": r["t"], "source": r.get("source"), "wall": now_iso()}
            self.history[key].append((r["t"], r["value"]))

    async def focus(self, asset_id: str, reason: str = "", source: str = "jarvis") -> dict:
        directive = {"type": "ui", "action": "focus", "asset_id": asset_id}
        await self.hub.broadcast("ui", {"action": "focus", "asset_id": asset_id, "reason": reason, "source": source})
        return directive

    async def ui(self, directive: dict) -> dict:
        """Broadcast one checked screen directive (jarvis_tools.check_directive) to every screen."""
        await self.hub.broadcast("ui", {k: v for k, v in directive.items() if k != "type"})
        return directive

    def _poke_brief(self, changed=(), force: bool = False) -> None:
        """Tell the Jarvis brief feed (jarvis_tools.BriefFeed, set by live_api) something changed."""
        if self.brief_hook is not None:
            self.brief_hook(changed, force)

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
        # Missing data never reads as healthy: any asset with a stale or never-reported signal
        # (including one a real connector declares but hasn't sent) is UNKNOWN unless it's in ALARM.
        stale_by_asset: dict[str, list[str]] = {}
        for aid, sig in d.stale_keys:
            stale_by_asset.setdefault(aid, []).append(sig)
        for aid, sigs in stale_by_asset.items():
            entry = assets.get(aid)
            if entry is None or entry["status"] in ("ALARM", "UNKNOWN"):
                continue
            entry["status"], entry["stale"] = "UNKNOWN", True
            note = f"No current {', '.join(sorted(sigs))} reading. Treat as unknown, not safe."
            entry["reason"] = f"{entry['reason']} {note}".strip() if entry.get("reason") else note
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

    def _log(self, p: dict, event: str, source: Optional[str], role: Optional[str] = None,
             by: Optional[str] = None, **extra) -> None:
        a = p["action"]
        self.audit.append(event, source=source, role=role, by=by, asset_id=a["asset_id"], proposal_id=p["id"],
                          status=p["status"], text=p["text"], connector_id=p.get("connector_id"),
                          **({"target_id": a["target_id"]} if a.get("target_id") else {}), **extra)

    async def propose(self, action: dict, reason: str = "", source: str = "console",
                      role: Optional[str] = None) -> dict:
        """Store a PENDING proposal. Changes nothing in the field. `role` is the token role
        of whoever asked (the operator who typed to Jarvis, say), for the audit log."""
        a = self.clean_action(action)
        conn = self.registry.route(a)
        p = {"id": f"act_{next(self._ids)}", "action": a, "reason": (reason or describe(a)).strip(),
             "status": "PENDING", "created_t": now_iso(), "text": describe(a), "source": source,
             "connector_id": conn.id}
        self.proposals[p["id"]] = p
        self._paths[p["id"]] = self.registry.path(a)
        self._log(p, "proposed", source=source, role=role, reason=p["reason"])
        await self.hub.broadcast("proposal", {"proposal": p})
        await self.focus(a["asset_id"], f"Waiting for confirmation: {p['text']}", source=source)
        # The broadcasts above yield, so a person may already have decided it: only
        # auto-confirm while it is still PENDING (checked and claimed with no await between).
        if (self.auto_confirm_sim and source == "jarvis" and conn is self.registry.sim
                and p["status"] == "PENDING"):
            return await self.confirm(p["id"], by="jarvis", source="jarvis", auto="JARVIS_CONFIRM=0")
        return p

    async def confirm(self, action_id: str, by: str = "", role: Optional[str] = None,
                      source: Optional[str] = None, auto: Optional[str] = None) -> dict:
        """Send a PENDING proposal through its connector, exactly once.

        The proposal is claimed (status SENDING) before the first await, so a second
        confirm from another screen, or a reject racing this one, gets 409 instead of
        sending the command again or after a reject. `by` is stored as `decided_by`; the
        HTTP route sets it from the caller's token role (auth.py), never from the body.

        The command goes only over the path it was proposed on (409 otherwise), and only if
        no reload came between the claim and the send (else FAILED, nothing sent)."""
        p = self._pending(action_id)
        a = p["action"]
        conn = self.registry.route(a)
        if conn.id != p.get("connector_id") or self.registry.path(a) != self._paths.get(p["id"]):
            raise ActionError(409, f"Proposal {action_id}: the command path changed; propose again.")
        gen, is_sim = self.generation, conn is self.registry.sim
        p.update(status="SENDING", decided_t=now_iso(), decided_by=by or None, connector_id=conn.id)
        source = source or by or None
        self._log(p, "confirmed", source=source, role=role, by=by or None, **({"auto": auto} if auto else {}))
        # --- no await above this line: the PENDING check and the claim are atomic ---
        started: list = []   # gets an entry once conn.send() has been called
        try:
            await self.hub.broadcast("proposal", {"proposal": p})
            res = await self._send_if_current(conn, a, gen, is_sim, started)
        except Exception as e:  # noqa: BLE001 - never leave a proposal stuck in SENDING
            log.exception("sending %s failed", action_id)
            # A real connector that raised inside send() may already have sent it: UNCERTAIN.
            res = ({"ok": False, "uncertain": True, "detail": f"{conn.id}: sending raised {e}; {UNCERTAIN_DETAIL}."}
                   if started and not is_sim else {"ok": False, "detail": f"Send failed: {e}"})
        reloaded = self.generation != gen
        if res is None:   # the field reloaded before the send started: nothing left
            res = {"ok": False, "detail": RELOADED_BEFORE_SEND}
        ok = bool(res.get("ok"))
        # UNCERTAIN: it left for the device but no acknowledgement came back. It may have
        # moved, so it is not FAILED (which invites a retry); a person checks the field.
        status = "CONFIRMED" if ok else "UNCERTAIN" if res.get("uncertain") else "FAILED"
        p.update(status=status, result=res.get("detail", ""), decided_t=now_iso())
        self._log(p, "result", source=source, role=role, by=by or None, result=p["result"],
                  **({"reloaded": True} if reloaded else {}))
        # After a reload, `commanded` belongs to the new field: never write the old one's result.
        if ok and not reloaded and a["type"] in ("SET_VALVE", "SET_PUMP"):
            self.commanded[a["asset_id"]] = {"value": a["value"], "t": self.t}
        await self.hub.broadcast("proposal", {"proposal": p})
        await self.focus(a["asset_id"], p["result"], source="action")
        await self.hub.broadcast("state", {"state": self.state()})
        self._poke_brief(force=ok)
        return p

    async def _send_if_current(self, conn, a: dict, gen: int, is_sim: bool, started: list) -> Optional[dict]:
        """conn.send(a), unless a reload changed the generation first (then None: not sent).
        The simulator sends on the loop with no await after the check. A real connector sends
        on a worker thread, which checks under the lock reload() takes to bump the generation;
        a reload after that check finds the send under way and leaves it to finish."""
        if is_sim:
            if self.generation != gen:
                return None
            started.append(True)
            return conn.send(a)

        def go() -> Optional[dict]:
            with self._gen_lock:
                if self.generation != gen:
                    return None
                started.append(True)
            return conn.send(a)

        return await asyncio.to_thread(go)

    async def reject(self, action_id: str, by: str = "", role: Optional[str] = None,
                     source: Optional[str] = None) -> dict:
        p = self._pending(action_id)
        # Decided before the first await, so a confirm that comes in later gets 409.
        p.update(status="REJECTED", decided_t=now_iso(), decided_by=by or None)
        self._log(p, "rejected", source=source or by or None, role=role, by=by or None)
        await self.hub.broadcast("proposal", {"proposal": p})
        return p

    def ack(self, alert_id: str, by: str = "", role: Optional[str] = None,
            source: Optional[str] = None) -> Optional[dict]:
        """Acknowledge an alert (it stays active until its condition clears). None if unknown.
        A second ack changes nothing and is not logged again."""
        for a in self.detector.alerts:
            if a["id"] == alert_id:
                if not a.get("acked"):
                    a.update(acked=True, acked_t=now_iso(), acked_by=by or None)
                    self.audit.append("acked", source=source or by or None, role=role, by=by or None,
                                      asset_id=a["asset_id"], alert_id=a["id"], kind=a["kind"],
                                      severity=a["severity"], message=a["message"], priority=a.get("priority"))
                return a
        return None

    def _pending(self, action_id: str) -> dict:
        p = self.proposals.get(action_id)
        if p is None:
            raise ActionError(404, f"No proposal {action_id}.")
        if p["status"] != "PENDING":
            raise ActionError(409, f"Proposal {action_id} is already {p['status']}.")
        return p
