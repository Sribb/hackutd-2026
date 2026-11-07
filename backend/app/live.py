"""The live field: the IoT side of the console.

Every tick it:
  1. advances the plant (scenario faults fire on schedule, approved plan steps execute),
  2. reads simulated sensors (level, flow, valve limit switches, truck GPS) with noise,
     or real readings pushed by devices through POST /api/telemetry,
  3. detects failures *from the readings only*: a pump with no flow when it should run,
     a tank rising faster than expected, stale or stuck sensors,
  4. re-forecasts from what the sensors say (not from hidden truth), raises alerts at
     60/30/15 minutes to shutdown, and broadcasts everything over /ws.

A stale sensor is never treated as safe: its tank is extrapolated as if its pump had
stopped (worst case) and its status is at least WARNING.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
import random
from collections import deque

from .alerts import Alerts
from .bus import Hub
from .field import Field, nice_name
from .planner import approve_steps, describe, make_plans
from .sim import ACTION_TYPES, Plant, forecast

log = logging.getLogger("freeboard.live")

TICK_S = 0.5
COUNTDOWN_ALERTS = (60, 30, 15)
RATE_WINDOW_MIN = 10.0


class LiveField:
    def __init__(self, f: Field, hub: Hub) -> None:
        self.f = f
        self.hub = hub
        self.alerts = Alerts(self.broadcast)
        self.lock = asyncio.Lock()
        self.speed = float(os.environ.get("FREEBOARD_SPEED", "0.25"))  # sim minutes per real second
        self.rng = random.Random(7)
        self._ids = itertools.count(1)
        self._task: asyncio.Task | None = None
        self.reset_state()

    # ------------------------------------------------------------------ lifecycle
    def reset_state(self) -> None:
        self.plant = Plant(self.f)
        self.paused = False
        self.faults = sorted((dict(x) for x in self.f.raw.get("faults", [])), key=lambda x: x["at_min"])
        self.sensor_faults: dict[str, dict] = {}       # asset_id -> {"mode": "DROPOUT"|"STUCK"}
        self.device_override: dict[str, dict] = {}     # "ASSET.metric" -> {value, until_t, source}
        self.readings: dict[str, dict] = {}            # "ASSET.metric" -> {value, t, source}
        self.level_hist = {tid: deque(maxlen=60) for tid in self.f.tanks}
        self.rates: dict[str, dict] = {}
        self.zero_flow_since: dict[str, float | None] = {pid: None for pid in self.f.pumps}
        self.last_flow_t: dict[str, float] = {}
        self.failed: dict[str, dict] = {}
        self.data_issues: dict[str, str] = {}
        self.incident: dict | None = None
        self.last_forecast: dict | None = None
        self.plans: dict[str, dict] = {}
        self.plans_made_t = 0.0
        self.plans_result: dict | None = None
        self.execution: dict | None = None
        self.pending: dict[str, dict] = {}
        self.command_log: deque = deque(maxlen=200)
        self._last_fc_t = -1e9
        self.alerts.reset()
        self._sense()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self.tick(self.speed * TICK_S)
            except Exception:
                log.exception("tick failed")

    async def broadcast(self, typ: str, data: dict, **kw) -> None:
        await self.hub.broadcast(typ, data, meta={"clock": self.f.clock_at(self.plant.t), "t": round(self.plant.t, 2)}, **kw)

    # ------------------------------------------------------------------ tick
    async def tick(self, dt: float, force: bool = False) -> None:
        if self.paused and not force:
            return
        events: list[dict] = []
        steps_done: list[dict] = []
        detections: list[dict] = []
        async with self.lock:
            remaining = dt
            while remaining > 1e-9:  # sense and detect every sim-minute, whatever the speed
                d = min(1.0, remaining)
                self._apply_due_faults()
                events += self.plant.step(d)
                remaining -= d
                steps_done += self._run_plan_steps()
                self._sense()
                detections += self._detect()

        await self.broadcast("telemetry", self.telemetry())
        for e in events:
            await self._on_live_event(e)
        for d in detections:
            await self._on_detection(d)
        for s in steps_done:
            await self.broadcast("plan_step", s)
            await self.broadcast("ui", {"action": "focus", "asset_id": s["asset_id"], "source": "plan",
                                        "reason": f"Step {s['n']}: {s['text']}"})
        if steps_done and self.execution and all(s["status"] == "DONE" for s in self.execution["steps"]):
            await self.alerts.raise_alert("INFO", "All plan steps dispatched",
                                          f"Every step of {self.execution['plan_id'].replace('_', ' ')} is underway. Watch the trucks on the map.",
                                          key=f"plan_done:{self.execution['plan_id']}:{self.execution['approved_t']}")
        if force or events or detections or steps_done or self.plant.t - self._last_fc_t >= 1.0:
            await self.refresh_forecast()

    def _apply_due_faults(self) -> None:
        while self.faults and self.faults[0]["at_min"] <= self.plant.t:
            self._inject(self.faults.pop(0))

    def _inject(self, fault: dict) -> list[str]:
        typ = fault.get("type", "TRIP").upper()
        aid = fault["asset_id"]
        if typ == "TRIP":
            return self.plant.apply({"type": "TRIP_PUMP", "asset_id": aid, "value": fault.get("reason", "Pump tripped.")})
        if typ in ("SENSOR_DROPOUT", "SENSOR_STUCK"):
            self.sensor_faults[aid] = {"mode": typ.split("_")[1]}
            return []
        if typ == "CLEAR_SENSOR":
            self.sensor_faults.pop(aid, None)
            return []
        if typ == "REPAIR":
            return self.plant.apply({"type": "RESET_PUMP", "asset_id": aid})
        return [f"Unknown fault type {typ}"]

    def _run_plan_steps(self) -> list[dict]:
        done = []
        if not self.execution:
            return done
        for s in self.execution["steps"]:
            if s["status"] == "PENDING" and s["due_t"] <= self.plant.t + 1e-6:
                warns = self.plant.apply(s["action"])
                s["status"] = "DONE"
                s["done_clock"] = self.f.clock_at(self.plant.t)
                s["warnings"] = warns
                self.command_log.append({"id": f"cmd{next(self._ids)}", "clock": s["done_clock"], "action": s["action"],
                                         "text": s["text"], "source": "plan", "by": self.execution.get("approved_by"),
                                         "status": "DONE", "warnings": warns})
                done.append(dict(s))
        return done

    # ------------------------------------------------------------------ sensors
    def _put(self, key: str, value, source: str = "sim") -> None:
        self.readings[key] = {"value": value, "t": self.plant.t, "source": source}

    def _sense(self) -> None:
        p, f = self.plant, self.f
        cfg = f.raw.get("sensors", {})
        ln, fn = cfg.get("level_noise_pct", 0.08), cfg.get("flow_noise_bbl_hr", 2.0)
        for k in [k for k, o in self.device_override.items() if o["until_t"] < p.t]:
            del self.device_override[k]

        for tid in f.tanks:
            key = f"{tid}.level_pct"
            fault = self.sensor_faults.get(tid)
            if key in self.device_override:
                self._put(key, self.device_override[key]["value"], self.device_override[key]["source"])
            elif fault and fault["mode"] == "DROPOUT":
                pass  # no new reading; it goes stale
            elif fault and fault["mode"] == "STUCK" and key in self.readings:
                self._put(key, self.readings[key]["value"], "sim")
            else:
                self._put(key, round(p.pct(tid) + self.rng.gauss(0, ln), 2))
            if key in self.readings and self.readings[key]["t"] == p.t:
                self.level_hist[tid].append((p.t, self.readings[key]["value"]))

        for pid, pump in f.pumps.items():
            key = f"{pid}.flow_bbl_hr"
            if key in self.device_override:
                self._put(key, self.device_override[key]["value"], self.device_override[key]["source"])
            else:
                q = p.flows.get(pid, 0.0)
                self._put(key, round(max(0.0, q + (self.rng.gauss(0, fn) if q > 0 else 0.0)), 1))
        for vid in f.valves:
            self._put(f"{vid}.position", "OPEN" if p.valve_open[vid] else "CLOSED")
        for wid in f.wells:
            self._put(f"{wid}.rate_bbl_hr", round(p.flows.get(wid, f.wells[wid]["water_bbl_hr"] * p.well_factor[wid]), 1))
        for sid in f.swds:
            self._put(f"{sid}.rate_bbl_hr", round(p.flows.get(sid, 0.0), 1))
        for tid in f.trucks:
            ph = p.truck_phase(tid)
            self._put(f"{tid}.gps", {**ph, "eta_clock": f.clock_at(ph["eta_t"]) if ph["eta_t"] is not None else None})

    def ingest(self, readings: list[dict], source: str) -> dict:
        """POST /api/telemetry: real devices override simulated sensors for a while."""
        accepted, rejected = [], []
        ids = self.f.all_ids()
        for r in readings:
            aid, metric = r.get("asset_id"), r.get("metric")
            if aid not in ids or not metric:
                rejected.append({**r, "error": "unknown asset_id or missing metric"})
                continue
            hold = float(r.get("hold_min", 10))
            key = f"{aid}.{metric}"
            self.device_override[key] = {"value": r.get("value"), "until_t": self.plant.t + hold, "source": source}
            accepted.append(key)
        self._sense()
        return {"accepted": accepted, "rejected": rejected}

    # ------------------------------------------------------------------ detection
    def _reading(self, key: str):
        r = self.readings.get(key)
        return r["value"] if r else None

    def _slope_bbl_hr(self, tid: str) -> float | None:
        pts = [(t, v) for t, v in self.level_hist[tid] if t >= self.plant.t - RATE_WINDOW_MIN]
        if len(pts) < 4 or pts[-1][0] - pts[0][0] < 2.0:
            return None
        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        mv = sum(v for _, v in pts) / n
        den = sum((t - mt) ** 2 for t, _ in pts)
        if den == 0:
            return None
        slope_pct_min = sum((t - mt) * (v - mv) for t, v in pts) / den
        return slope_pct_min / 100.0 * self.f.tanks[tid]["capacity_bbl"] * 60.0

    def _expected_bbl_hr(self, tid: str) -> float:
        """What the tank *should* do if every pump we commanded on is actually running."""
        f, p = self.f, self.plant
        inflow = sum(self._reading(f"{w}.rate_bbl_hr") or 0.0 for w in f.wells_by_tank[tid])
        pid = f.pump_by_tank[tid]
        pump = f.pumps[pid]
        level = self._reading(f"{tid}.level_pct") or 0.0
        commanded = p.pump_on[pid] and p.valve_open[pump["valve_id"]] and pid not in self.failed
        out = 0.0
        if commanded:
            out = pump["rate_bbl_hr"] if level > f.tanks[tid]["pump_low_pct"] else min(pump["rate_bbl_hr"], inflow)
        xo = 0.0
        for v in f.crossovers:
            if tid in v["connects"] and p.valve_open[v["id"]]:
                q = p.flows.get(v["id"], 0.0)
                xo += -q if v["connects"][0] == tid else q
        return inflow - out + xo

    def _detect(self) -> list[dict]:
        f, p = self.f, self.plant
        found: list[dict] = []
        stale_after = f.raw.get("sensors", {}).get("stale_after_min", 5)

        for pid, pump in f.pumps.items():
            tid = pump["tank_id"]
            flow = self._reading(f"{pid}.flow_bbl_hr") or 0.0
            level = self._reading(f"{tid}.level_pct") or 0.0
            should_run = p.pump_on[pid] and p.valve_open[pump["valve_id"]] and level > f.tanks[tid]["pump_low_pct"] + 0.5
            if should_run and flow < 0.1 * pump["rate_bbl_hr"]:
                if self.zero_flow_since[pid] is None:
                    self.zero_flow_since[pid] = p.t
                if pid not in self.failed and p.t - self.zero_flow_since[pid] >= 1.0:
                    since = self.last_flow_t.get(pid, self.zero_flow_since[pid])
                    self.failed[pid] = {"since_t": since, "detected_t": p.t,
                                        "reason": f"Pump tripped. No flow since {f.clock_text(since)}."}
                    found.append({"type": "PUMP_FAILURE", "asset_id": pid, "tank_id": tid})
            elif flow >= 0.5 * pump["rate_bbl_hr"]:
                self.zero_flow_since[pid] = None
                self.last_flow_t[pid] = p.t
                if pid in self.failed:
                    del self.failed[pid]
                    found.append({"type": "PUMP_RECOVERED", "asset_id": pid, "tank_id": tid})

        issues: dict[str, str] = {}
        for tid in f.tanks:
            r = self.readings.get(f"{tid}.level_pct")
            age = p.t - r["t"] if r else 1e9
            if age > stale_after:
                issues[tid] = (f"No level reading from {nice_name(tid)} for {age:.0f} min. "
                               f"Treating it as at risk, assuming its pump has stopped.")
            obs = self._slope_bbl_hr(tid)
            exp = self._expected_bbl_hr(tid)
            self.rates[tid] = {"observed_bbl_hr": None if obs is None else round(obs, 1), "expected_bbl_hr": round(exp, 1)}
            hist = [v for t, v in self.level_hist[tid] if t >= p.t - 8]
            if len(hist) >= 8 and len(set(hist)) == 1 and abs(exp) > 5:
                issues[tid] = (f"{nice_name(tid)} level reading hasn't changed in 8 min while flow says it should. "
                               f"Sensor may be stuck. Treating it as at risk.")
            if obs is not None and obs - exp > 40 and tid not in issues:
                pid = f.pump_by_tank[tid]
                if pid not in self.failed:
                    found.append({"type": "UNEXPLAINED_RISE", "asset_id": tid, "observed": obs, "expected": exp})
        new_issues = {k: v for k, v in issues.items() if k not in self.data_issues}
        self.data_issues = issues
        for tid, text in new_issues.items():
            found.append({"type": "DATA_ISSUE", "asset_id": tid, "text": text})
        return found

    async def _on_detection(self, d: dict) -> None:
        f = self.f
        clock = f.clock_at(self.plant.t)
        if d["type"] == "PUMP_FAILURE":
            pid, tid = d["asset_id"], d["tank_id"]
            fail = self.failed[pid]
            rate = self.rates.get(tid, {})
            evidence = [f"{nice_name(pid)} flow meter reads 0 bbl/hr; it should be moving {f.pumps[pid]['rate_bbl_hr']} bbl/hr."]
            if rate.get("observed_bbl_hr") is not None:
                evidence.append(f"{nice_name(tid)} is changing {rate['observed_bbl_hr']:+.0f} bbl/hr.")
            self.incident = {
                "id": f"inc{next(self._ids)}", "root_asset_id": pid, "tank_id": tid,
                "summary": f"{nice_name(pid)} failed. {nice_name(tid)} is filling.",
                "reason": fail["reason"], "evidence": evidence, "detected_clock": clock,
                "affected": [tid] + f.wells_by_tank[tid],
            }
            await self.broadcast("incident", self.incident)
            await self.broadcast("ui", {"action": "focus", "asset_id": pid, "source": "detector",
                                        "highlight": [pid, tid], "reason": self.incident["summary"]})
            await self.refresh_forecast()
            sd = (self.last_forecast or {}).get("summary", {}).get("first_shutdown")
            body = self.incident["summary"] + " " + fail["reason"]
            if sd:
                body += f" {sd['text']}."
            await self.alerts.raise_alert("CRITICAL", f"{nice_name(pid)} failed", body, key=f"fail:{pid}:{fail['since_t']:.0f}",
                                          asset_id=pid, clock=clock, ui={"action": "focus", "asset_id": pid})
        elif d["type"] == "PUMP_RECOVERED":
            if self.incident and self.incident["root_asset_id"] == d["asset_id"]:
                self.incident = {**self.incident, "resolved_clock": clock}
                await self.broadcast("incident", self.incident)
            await self.alerts.raise_alert("INFO", f"{nice_name(d['asset_id'])} is running again",
                                          "Flow is back.", key=f"recover:{d['asset_id']}:{self.plant.t:.0f}", asset_id=d["asset_id"], clock=clock)
        elif d["type"] == "UNEXPLAINED_RISE":
            await self.alerts.raise_alert(
                "WARNING", f"{nice_name(d['asset_id'])} rising faster than expected",
                f"Rising {d['observed']:+.0f} bbl/hr, expected {d['expected']:+.0f} bbl/hr. Check its pump and valves.",
                key=f"rise:{d['asset_id']}", asset_id=d["asset_id"], clock=clock,
                ui={"action": "focus", "asset_id": d["asset_id"]})
        elif d["type"] == "DATA_ISSUE":
            await self.alerts.raise_alert("WARNING", f"Sensor problem at {nice_name(d['asset_id'])}", d["text"],
                                          key=f"data:{d['asset_id']}:{self.plant.t:.0f}", asset_id=d["asset_id"], clock=clock,
                                          ui={"action": "focus", "asset_id": d["asset_id"]})

    async def _on_live_event(self, e: dict) -> None:
        clock = self.f.clock_at(e["t"])
        if e["type"] == "SHUTDOWN_LEVEL":
            await self.alerts.raise_alert("CRITICAL", f"{nice_name(e['asset_id'])} hit shutdown",
                                          f"{nice_name(e['asset_id'])} reached its shutdown level at {self.f.clock_text(e['t'])}. Its wells are shutting in.",
                                          key=f"sd:{e['asset_id']}", asset_id=e["asset_id"], clock=clock)
        elif e["type"] == "TRUCK_ARRIVES":
            await self.broadcast("field_event", {**e, "text": f"{nice_name(e['asset_id'])} arrived at {nice_name(e['target_id'])}."})
        elif e["type"] == "TRUCK_LOADED":
            await self.broadcast("field_event", {**e, "text": f"{nice_name(e['asset_id'])} loaded {e['bbl']:.0f} bbl from {nice_name(e['target_id'])}."})
        elif e["type"] == "OVERFLOW":
            await self.alerts.raise_alert("CRITICAL", f"{nice_name(e['asset_id'])} overflowing", "Spill risk. Dispatch spill response.",
                                          key=f"of:{e['asset_id']}", asset_id=e["asset_id"], clock=clock)

    # ------------------------------------------------------------------ forecasting
    def sensed_plant(self) -> Plant:
        """A plant built from what the sensors and detector say, for forecasting and planning."""
        f = self.f
        sp = self.plant.clone()
        for pid in f.pumps:
            sp.pump_tripped[pid] = pid in self.failed
        for tid, tank in f.tanks.items():
            r = self.readings.get(f"{tid}.level_pct")
            if r is None:
                continue
            pct = float(r["value"])
            if tid in self.data_issues:  # worst case: assume nothing has been leaving since the last good reading
                inflow = sum(f.wells[w]["water_bbl_hr"] * sp.well_factor[w] for w in f.wells_by_tank[tid] if not sp.well_shut[w])
                pct += inflow * max(0.0, sp.t - r["t"]) / 60.0 / tank["capacity_bbl"] * 100.0
                sp.pump_tripped[f.pump_by_tank[tid]] = True
            sp.level[tid] = min(tank["capacity_bbl"], max(0.0, pct / 100.0 * tank["capacity_bbl"]))
        return sp

    def data_warnings(self) -> list[str]:
        return list(self.data_issues.values())

    async def forecast(self, actions: list[dict], horizon: int = 360, step: int = 5) -> dict:
        async with self.lock:
            sp = self.sensed_plant()
            warns = self.data_warnings()
        return await asyncio.to_thread(forecast, sp, actions, horizon, step, warns)

    async def refresh_forecast(self) -> None:
        fc = await self.forecast([], 360, 5)
        self._last_fc_t = self.plant.t
        self.last_forecast = fc
        await self.broadcast("forecast", {**fc, "countdown": self.countdown()})
        await self._countdown_alerts()

    def countdown(self) -> dict | None:
        fc = self.last_forecast
        if not fc:
            return None
        sd = fc["summary"]["first_shutdown"]
        if not sd:
            poss = next((e for e in fc["events"] if e["type"] == "SHUTDOWN_POSSIBLE"), None)
            if poss:
                return {"asset_id": poss["asset_id"], "t_min": poss["t_min"], "clock": poss["clock"],
                        "text": poss["text"], "certain": False}
            return None
        wells = len(fc["summary"]["wells_lost"])
        rng = ""
        if sd.get("t_min_high") is not None:
            spread = max(sd["t_min"] - sd["t_min_low"], sd["t_min_high"] - sd["t_min"])
            rng = f", give or take {spread:.0f} minutes"
        return {"asset_id": sd["asset_id"], "t_min": sd["t_min"], "t_min_low": sd.get("t_min_low"),
                "t_min_high": sd.get("t_min_high"), "clock": sd["clock"], "certain": True,
                "wells_lost": wells, "text": f"{sd['text']}{rng}. {wells} wells go down with it."}

    async def _countdown_alerts(self) -> None:
        fc = self.last_forecast
        if not fc:
            return
        for e in fc["events"]:
            if e["type"] != "SHUTDOWN_LEVEL":
                continue
            for th in COUNTDOWN_ALERTS:
                if e["t_min"] <= th:
                    await self.alerts.raise_alert(
                        "CRITICAL" if th <= 30 else "WARNING",
                        f"{nice_name(e['asset_id'])}: {e['t_min']:.0f} min to shutdown",
                        f"{e['text']}. Open the console to pick a plan.",
                        key=f"cd:{e['asset_id']}:{th}:{self.incident['id'] if self.incident else ''}",
                        asset_id=e["asset_id"], clock=e["clock"],
                        ui={"action": "focus", "asset_id": e["asset_id"]})
                    break

    # ------------------------------------------------------------------ commands
    def _validate(self, a: dict) -> str | None:
        typ = str(a.get("type", "")).upper()
        if typ not in ACTION_TYPES:
            return f"Unknown action type {typ!r}."
        want = {"SET_VALVE": "valve", "SET_PUMP": "pump", "CHOKE_WELL": "well", "SEND_TRUCK": "truck"}[typ]
        if self.f.kind_of(a.get("asset_id", "")) != want:
            return f"{a.get('asset_id')} is not a {want}."
        if typ == "SEND_TRUCK" and a.get("target_id") not in self.f.tanks:
            return "SEND_TRUCK needs target_id set to a tank."
        return None

    async def command(self, action: dict, source: str = "web", by: str = "", confirm: bool = False) -> dict:
        a = {k: v for k, v in action.items() if k in ("type", "asset_id", "value", "target_id")}
        a["type"] = str(a.get("type", "")).upper()
        err = self._validate(a)
        if err:
            return {"status": "REJECTED", "error": err, "action": a}
        if confirm:
            pid = f"p{next(self._ids)}"
            preview = await self.forecast([{**a, "at_min": 0}])
            self.pending[pid] = {"id": pid, "action": a, "text": describe(self.f, [a]), "source": source, "by": by,
                                 "preview": {"summary": preview["summary"], "cost": preview["cost"]}}
            await self.broadcast("pending_command", self.pending[pid])
            await self.broadcast("ui", {"action": "focus", "asset_id": a["asset_id"], "source": source,
                                        "reason": f"Awaiting confirmation: {self.pending[pid]['text']}"})
            return {"status": "PENDING", **self.pending[pid]}
        return await self._execute(a, source, by)

    async def confirm(self, pending_id: str, by: str, approve: bool = True) -> dict | None:
        p = self.pending.pop(pending_id, None)
        if p is None:
            return None
        if not approve:
            await self.broadcast("pending_cleared", {"id": pending_id, "status": "CANCELLED", "by": by})
            return {"status": "CANCELLED", **p}
        await self.broadcast("pending_cleared", {"id": pending_id, "status": "CONFIRMED", "by": by})
        if p["action"]["type"] == "APPROVE_PLAN":
            res = await self.approve(p["action"]["plan_id"], by=by or p["by"])
            return {"status": "APPROVED" if res else "REJECTED", **(res or {"error": "Plan expired. Ask for plans again."})}
        return await self._execute(p["action"], p["source"], by or p["by"])

    async def propose_plan(self, plan_id: str, source: str, by: str) -> dict:
        plan = self.plans[plan_id]
        pid = f"p{next(self._ids)}"
        self.pending[pid] = {"id": pid, "action": {"type": "APPROVE_PLAN", "plan_id": plan_id},
                             "text": f"Approve {plan_id.replace('_', ' ')}: {plan['summary']}", "source": source, "by": by,
                             "preview": {"summary": {"keeps_running": plan["keeps_running"], "robust": plan["robust"],
                                                     "first_shutdown": plan["first_shutdown"]}, "cost": plan["cost"]}}
        await self.broadcast("pending_command", self.pending[pid])
        await self.broadcast("ui", {"action": "show_panel", "panel": "plans", "source": source,
                                    "reason": self.pending[pid]["text"]})
        return {"status": "PENDING", **self.pending[pid]}

    async def _execute(self, a: dict, source: str, by: str) -> dict:
        async with self.lock:
            warns = self.plant.apply(dict(a))
            self._sense()
        rejected = any("ignored" in w for w in warns)
        entry = {"id": f"cmd{next(self._ids)}", "clock": self.f.clock_at(self.plant.t), "action": a,
                 "text": describe(self.f, [a]), "source": source, "by": by,
                 "status": "REJECTED" if rejected else "DONE", "warnings": warns}
        self.command_log.append(entry)
        await self.broadcast("command", entry)
        await self.broadcast("telemetry", self.telemetry())
        await self.broadcast("ui", {"action": "focus", "asset_id": a["asset_id"], "source": source, "reason": entry["text"]})
        await self.refresh_forecast()
        if source == "phone":
            await self.alerts.raise_alert("INFO", "Command from phone", f"{by or 'A phone'}: {entry['text']}",
                                          key=entry["id"], asset_id=a["asset_id"], clock=entry["clock"], push=False)
        return entry

    # ------------------------------------------------------------------ plans
    async def make_plans(self, horizon: int = 360) -> dict:
        async with self.lock:
            sp = self.sensed_plant()
        result = await asyncio.to_thread(make_plans, sp, horizon)
        result["warnings"] = self.data_warnings()
        self.plans = {p["id"]: p for p in result["plans"]}
        self.plans_made_t = sp.t
        self.plans_result = result
        await self.broadcast("plans", result)
        return result

    async def approve(self, plan_id: str, by: str = "", execute: bool = True) -> dict | None:
        plan = self.plans.get(plan_id)
        if plan is None:
            return None
        # Re-run the plan from *now*: the operator may approve minutes after it was made.
        fresh = await self.forecast(plan["actions"])
        plan = {**plan, "events": [e for e in fresh["events"] if e["type"] == "TRUCK_ARRIVES"],
                "first_shutdown": fresh["summary"]["first_shutdown"], "keeps_running": fresh["summary"]["keeps_running"]}
        res = approve_steps(self.f, plan, self.plant.t, self.incident)
        res["summary"] = plan["summary"]
        res["cost"] = fresh["cost"]
        res["keeps_running"] = fresh["summary"]["keeps_running"]
        res["robust"] = fresh["summary"]["robust"]
        if not fresh["summary"]["keeps_running"]:
            res["warning"] = "This plan no longer keeps every tank running from now. Ask for new plans."
        res["approved_by"] = by
        res["approved_clock"] = self.f.clock_at(self.plant.t)
        if execute:
            self.execution = {"plan_id": plan_id, "steps": res["steps"], "approved_by": by, "approved_t": self.plant.t}
        await self.broadcast("plan_approved", res)
        await self.broadcast("ui", {"action": "walkthrough", "asset_ids": [s["asset_id"] for s in res["steps"]],
                                    "source": "plan", "reason": plan["summary"]})
        await self.alerts.raise_alert("INFO", "Plan approved", f"{by or 'Operator'} approved: {plan['summary']}",
                                      key=f"approve:{plan_id}:{self.plant.t:.1f}", clock=res["approved_clock"], push=False)
        return res

    # ------------------------------------------------------------------ demo controls
    async def set_sim(self, speed: float | None = None, paused: bool | None = None) -> dict:
        if speed is not None:
            self.speed = max(0.0, min(30.0, float(speed)))
        if paused is not None:
            self.paused = bool(paused)
        await self.broadcast("sim", self.sim_status())
        return self.sim_status()

    async def advance(self, minutes: float) -> dict:
        await self.tick(max(0.0, min(240.0, minutes)), force=True)
        return self.sim_status()

    async def reset(self) -> dict:
        async with self.lock:
            self.reset_state()
        await self.broadcast("reset", self.snapshot())
        await self.refresh_forecast()
        return self.sim_status()

    async def inject(self, fault: dict) -> dict:
        async with self.lock:
            warns = self._inject(fault)
        await self.broadcast("fault_injected", {**fault, "warnings": warns})
        return {"ok": not warns, "warnings": warns}

    def sim_status(self) -> dict:
        return {"t": round(self.plant.t, 2), "clock": self.f.clock_at(self.plant.t), "speed": self.speed,
                "paused": self.paused, "pending_faults": self.faults,
                "sensor_faults": self.sensor_faults, "push_enabled": self.alerts.push_enabled}

    # ------------------------------------------------------------------ views
    def telemetry(self) -> dict:
        f, p = self.f, self.plant
        r = self.readings
        tanks = {}
        for tid in f.tanks:
            rd = r.get(f"{tid}.level_pct")
            tanks[tid] = {"level_pct": rd["value"] if rd else None, "source": rd["source"] if rd else None,
                          "age_min": round(p.t - rd["t"], 1) if rd else None, **self.rates.get(tid, {})}
        return {
            "t": round(p.t, 2), "clock": f.clock_at(p.t), "speed": self.speed, "paused": self.paused,
            "tanks": tanks,
            "pumps": {pid: {"flow_bbl_hr": r[f"{pid}.flow_bbl_hr"]["value"],
                            "running": r[f"{pid}.flow_bbl_hr"]["value"] > 5,
                            "source": r[f"{pid}.flow_bbl_hr"]["source"]} for pid in f.pumps},
            "valves": {vid: r[f"{vid}.position"]["value"] for vid in f.valves},
            "crossover_flow_bbl_hr": {v["id"]: round(p.flows.get(v["id"], 0.0), 1) for v in f.crossovers},
            "wells": {wid: {"rate_bbl_hr": r[f"{wid}.rate_bbl_hr"]["value"], "online": not p.well_shut[wid],
                            "choke": p.well_factor[wid]} for wid in f.wells},
            "disposal_wells": {sid: {"rate_bbl_hr": r[f"{sid}.rate_bbl_hr"]["value"], "max_bbl_hr": f.swds[sid]["max_bbl_hr"]}
                               for sid in f.swds},
            "trucks": {tid: r[f"{tid}.gps"]["value"] for tid in f.trucks},
        }

    def state(self) -> dict:
        """GET /api/state: contract shape (clock, assets, incident) plus extras."""
        f, p = self.f, self.plant
        fc = self.last_forecast or {"events": []}
        sd_by_tank = {e["asset_id"]: e for e in fc["events"] if e["type"] == "SHUTDOWN_LEVEL"}
        poss = {e["asset_id"]: e for e in fc["events"] if e["type"] == "SHUTDOWN_POSSIBLE"}
        assets: dict[str, dict] = {}
        for tid, tank in f.tanks.items():
            lvl = self._reading(f"{tid}.level_pct")
            st, reason = "OK", None
            if tid in self.data_issues:
                st, reason = "WARNING", self.data_issues[tid]
            if tid in sd_by_tank:
                e = sd_by_tank[tid]
                st, reason = ("ALARM" if e["t_min"] <= 90 else "WARNING"), e["text"] + "."
            elif tid in poss:
                st, reason = "WARNING", poss[tid]["text"] + "."
            if p.tank_shutdown[tid]:
                st, reason = "ALARM", "At shutdown level. Wells are shut in."
            elif lvl is not None and lvl >= tank["warning_pct"] and st == "OK":
                st, reason = "WARNING", f"Above {tank['warning_pct']}%."
            assets[tid] = {"status": st, "level_pct": lvl, "reason": reason, **self.rates.get(tid, {})}
        for pid in f.pumps:
            flow = self._reading(f"{pid}.flow_bbl_hr")
            if pid in self.failed:
                assets[pid] = {"status": "ALARM", "reason": self.failed[pid]["reason"], "flow_bbl_hr": flow}
            elif not p.pump_on[pid]:
                assets[pid] = {"status": "OFFLINE", "reason": "Turned off by operator.", "flow_bbl_hr": flow}
            else:
                assets[pid] = {"status": "OK", "flow_bbl_hr": flow}
        for vid in f.valves:
            assets[vid] = {"status": "OK", "position": self._reading(f"{vid}.position")}
        for wid, w in f.wells.items():
            tid = w["tank_id"]
            if p.well_shut[wid] or p.well_factor[wid] == 0:
                assets[wid] = {"status": "OFFLINE", "reason": "Shut in."}
            elif p.well_factor[wid] < 1:
                assets[wid] = {"status": "WARNING", "reason": f"Choked to {p.well_factor[wid]:.0%}."}
            elif tid in sd_by_tank:
                assets[wid] = {"status": "WARNING", "reason": f"Shuts in at {f.clock_text(p.t + sd_by_tank[tid]['t_min'])} unless {nice_name(tid)} is relieved."}
            else:
                assets[wid] = {"status": "OK"}
            assets[wid]["rate_bbl_hr"] = self._reading(f"{wid}.rate_bbl_hr")
        for sid, s in f.swds.items():
            rate = self._reading(f"{sid}.rate_bbl_hr") or 0
            assets[sid] = {"status": "WARNING" if rate >= 0.95 * s["max_bbl_hr"] else "OK", "rate_bbl_hr": rate,
                           "max_bbl_hr": s["max_bbl_hr"]}
        for tid in f.trucks:
            assets[tid] = {"status": "OK", **(self._reading(f"{tid}.gps") or {})}
        incident = None
        if self.incident:
            incident = {k: self.incident[k] for k in ("root_asset_id", "summary")} | self.incident
        return {"clock": f.clock_at(p.t), "t": round(p.t, 2), "assets": assets, "incident": incident,
                "countdown": self.countdown(), "warnings": self.data_warnings()}

    def snapshot(self) -> dict:
        return {
            "sim": self.sim_status(),
            "state": self.state(),
            "telemetry": self.telemetry(),
            "forecast": {**self.last_forecast, "countdown": self.countdown()} if self.last_forecast else None,
            "alerts": self.alerts.items[-50:],
            "plans": self.plans_result,
            "execution": self.execution,
            "pending": list(self.pending.values()),
            "commands": list(self.command_log)[-30:],
            "presence": self.hub.presence(),
        }
