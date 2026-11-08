"""Plan runs (#63): Jarvis executes a whole plan, and a person confirms each step.

A run turns a plan's actions (planner.py: {type, asset_id, value, target_id?, at_min})
into ordered steps. Each step is released at `at_min` live minutes after the run starts,
as an ordinary live.py proposal (PENDING, source "run", with `run_id` and `step`). It
goes to the asset's connector only when a person confirms it with
POST /api/actions/{id}/confirm. The run never confirms anything itself; JARVIS_CONFIRM=0
auto-confirms only steps that go to the simulator (live.propose decides that).

After a step is confirmed, the run checks its effect in the live readings:
  SET_VALVE   the valve's `position` reads the commanded value
  SET_PUMP    the pump's `running` matches ON / OFF
  CHOKE_WELL  the well's `flow_bbl_hr` is within 10% of design x the choke
  SEND_TRUCK  the truck is no longer IDLE
within VERIFY_MIN live minutes. It also replays what was actually confirmed, at the
times it was confirmed, and watches every tank: one that runs TANK_TOL_PCT points
above that replay for TANK_SUSTAIN_MIN is "rising faster than forecast".

Any of those, or a step that is rejected or fails at its connector, pauses the run,
raises a RUN_PAUSED alert, and asks Jarvis for a re-plan from the current state (the
rules pick with no API key; Claude may pick among the planner's plans with one). The
re-plan is offered as a new run, PAUSED and not started: resuming it starts it and
cancels the run it replaces. A tank reaching shutdown during a run fails the run.

Statuses. Run: RUNNING, PAUSED, DONE, FAILED, CANCELLED. Step: WAITING (not released),
PENDING (waiting for a person), VERIFYING (confirmed, effect not seen yet), DONE,
MISMATCH (effect not seen in time), REJECTED, FAILED (connector refused), CANCELLED.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import math
from typing import Callable, Optional

from .connectors.sim import _on
from .detect import now_iso
from .fieldmap import name
from .forecast import RANGE, build_plant, clean_actions, simulate
from .live import ActionError, LiveField, describe
from .planner import _plan as planner_plan, make_plans

log = logging.getLogger("freeboard.runs")

PLAN_HORIZON = 360       # same as POST /api/plans

ACTIVE = ("RUNNING", "PAUSED")
FINISHED = {"DONE", "MISMATCH", "REJECTED", "FAILED", "CANCELLED"}
VERIFY_MIN = {"SET_VALVE": 2.0, "SET_PUMP": 2.0, "CHOKE_WELL": 3.0, "SEND_TRUCK": 1.0}
TANK_TOL_PCT = 3.0       # points above the replay of what was confirmed
TANK_SUSTAIN_MIN = 1.0   # for this long, so one noisy reading never pauses a run
MAX_PLANS = 10           # candidates the re-plan chooses from


def touches(plan: dict, assets: set) -> bool:
    return any(a.get("asset_id") in assets or a.get("target_id") in assets for a in plan.get("actions", []))


def cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def pick_plan(plans: list[dict], exclude: set = frozenset()) -> Optional[dict]:
    """The rules pick, from plans cheapest first: the first that keeps every tank running
    and holds with inflow 15% high, else the first that keeps them running, else the
    first. Never one that uses an excluded asset. None when the pick has no actions
    (doing nothing more is enough)."""
    usable = [p for p in plans if not touches(p, exclude)]
    for ok in (lambda p: p.get("keeps_running") and p.get("robust"), lambda p: p.get("keeps_running"),
               lambda p: bool(p.get("actions"))):
        for p in usable:
            if ok(p):
                return p if p.get("actions") else None
    return None


class RunManager:
    def __init__(self, live: LiveField, jarvis=None, plans: Optional[dict] = None,
                 plans_fn: Optional[Callable[[dict], dict]] = None) -> None:
        self.live = live
        self.jarvis = jarvis
        self.static_plans = plans if plans is not None else {}   # main.py's POST /api/plans results
        self.plans: dict[str, dict] = {}                         # plans computed from live state
        self.plans_fn = plans_fn or (lambda sc: make_plans(sc, top_n=MAX_PLANS))
        self.runs: dict[str, dict] = {}
        self._ctx: dict[str, dict] = {}                          # run id -> replay cache (not JSON)
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task] = set()
        live.listeners.append(self._on_event)

    # ---------- plans ----------
    def remember(self, plans: list[dict]) -> None:
        for p in plans:
            self.plans[p["id"]] = p

    def find_plan(self, plan_id: str) -> Optional[dict]:
        return self.plans.get(plan_id) or self.static_plans.get(plan_id)

    def planning_scenario(self, exclude: set = frozenset()) -> dict:
        """The live scenario, adjusted for what's already happened: wells at their live
        flow (so a choke counts), busy or excluded trucks gone, and an excluded crossover
        that reads CLOSED gone (it's stuck shut, so a plan can't use it)."""
        live = self.live
        sc = live.live_scenario()
        f = sc["field"]
        for w in f["wells"]:
            r = live.latest.get((w["id"], "flow_bbl_hr"))
            if r is not None and not live.detector.is_stale(w["id"], "flow_bbl_hr"):
                try:
                    w["water_bbl_hr"] = max(0.0, float(r["value"]))
                except (TypeError, ValueError):
                    pass
        f["trucks"] = [t for t in f["trucks"] if t["id"] not in exclude
                       and live.registry.sim.truck_phase(t["id"])["phase"] == "IDLE"]
        xo = {x["id"] for x in live.fmap.crossovers}
        f["valves"] = [v for v in f["valves"] if not (
            v["id"] in exclude and v["id"] in xo and str(live.value(v["id"], "position")).upper() != "OPEN")]
        return sc

    def exclusions(self) -> set:
        """Assets a new plan shouldn't lean on: valves with an active VALVE_MISMATCH."""
        return {aid for (aid, kind) in self.live.detector.active if kind == "VALVE_MISMATCH"}

    def inflight(self) -> list[dict]:
        """Truck jobs already under way (dispatched, not done loading), from the simulator,
        which models every truck."""
        sim = self.live.registry.sim
        return [j for j in sim.truck_jobs if j["remaining"] > 0 and sim.t < j["load_end"]]

    def rescore(self, sc: dict, plans: list[dict]) -> list[dict]:
        """The planner can't see trucks already on the road (planning_scenario drops them so
        it won't send them twice). Replay its candidates, plus doing nothing more, with those
        jobs added at no extra cost, and rank them again: feasible cheapest first."""
        jobs = self.inflight()
        if not jobs:
            return plans
        plant, now = build_plant(sc), self.live.registry.sim.t
        pre = []
        for i, j in enumerate(jobs, start=1):
            sid = f"INFLIGHT_{i}"
            plant.trucks[sid] = {"id": sid, "capacity_bbl": j["remaining"], "usd_hr": 0, "haul_to_swd_min": 0,
                                 "minutes_to": {j["target_id"]: max(0.0, j["arrive"] - now)},
                                 "load_min": max(1.0, j["load_end"] - max(now, j["arrive"]))}
            pre.append({"type": "SEND_TRUCK", "asset_id": sid, "target_id": j["target_id"], "at_min": 0.0})
        names = sorted({name(j["truck_id"]) for j in jobs})
        note = f" Counts {' and '.join(names)}, already on the way."
        out, seen = [], set()
        for acts in [p["actions"] for p in plans] + [[]]:
            key = json.dumps(sorted(json.dumps(a, sort_keys=True) for a in acts))
            if key in seen:
                continue
            seen.add(key)
            clean, _ = clean_actions(plant, pre + acts)
            run = simulate(plant, clean, PLAN_HORIZON)
            robust = not simulate(plant, clean, PLAN_HORIZON, 1 + RANGE).shutdowns
            p = planner_plan(plant, sc["field"], acts, run, robust, PLAN_HORIZON)
            p["summary"] += note
            out.append(p)
        ok = sorted((p for p in out if p["keeps_running"]), key=lambda p: (p["cost"]["total_usd"], len(p["actions"])))
        bad = sorted((p for p in out if not p["keeps_running"]),
                     key=lambda p: (len(p["cannot_save"]), p["cost"]["total_usd"]))
        return ok + bad

    async def candidates(self, exclude: set) -> list[dict]:
        sc = self.planning_scenario(exclude)
        res = await asyncio.to_thread(self.plans_fn, sc)
        plans = await asyncio.to_thread(self.rescore, sc, res.get("plans", []))
        self.remember([p for p in plans if p.get("actions")])
        return plans

    async def choose(self, plans: list[dict], exclude: set, why: str) -> dict:
        """Ask Jarvis (rules when there's no key). Returns {plan, source, reason}."""
        if self.jarvis is not None:
            return await self.jarvis.choose_plan(plans, exclude, why)
        plan = pick_plan(plans, exclude)
        return {"plan": plan, "source": "rules", "reason": ""}

    # ---------- views ----------
    def list(self) -> list[dict]:
        return list(self.runs.values())

    def get(self, run_id: str) -> dict:
        run = self.runs.get(run_id)
        if run is None:
            raise ActionError(404, f"No run {run_id}.")
        return run

    def running(self) -> Optional[dict]:
        return next((r for r in self.runs.values() if r["status"] == "RUNNING"), None)

    async def _broadcast(self, run: dict) -> None:
        await self.live.hub.broadcast("run", {"run": run})

    # ---------- start a run ----------
    async def execute(self, plan: dict, source: str = "console", by: str = "", start: bool = True,
                      replaces: Optional[str] = None, excluded: set = frozenset(), note: str = "") -> dict:
        """POST /api/plans/{id}/execute. 409 if another run is RUNNING; 422 if a step can't
        apply to this field. A run with start=False is an offered re-plan (PAUSED)."""
        if start and self.running():
            r = self.running()
            raise ActionError(409, f"{r['id']} is already running. Pause or cancel it first.")
        steps = []
        order = sorted(enumerate(plan.get("actions", [])), key=lambda x: (float(x[1].get("at_min", 0) or 0), x[0]))
        for n, (_, a) in enumerate(order, start=1):
            action = self.live.clean_action(a)
            steps.append({"n": n, "action": action, "at_min": float(a.get("at_min", 0) or 0),
                          "text": describe(action), "status": "WAITING", "proposal_id": None,
                          "release_clock": None, "confirmed_t": None, "result": None, "verify": None})
        if not steps:
            raise ActionError(422, f"Plan {plan.get('id')} has no actions to run.")
        run = {"id": f"run_{next(self._ids)}", "plan_id": plan.get("id"), "summary": plan.get("summary", ""),
               "cost_usd": plan.get("cost", {}).get("total_usd"), "robust": plan.get("robust"),
               "keeps_running": plan.get("keeps_running"), "status": "PAUSED", "source": source,
               "created_t": now_iso(), "created_by": by or None, "started": False, "start_t": None,
               "start_clock": None, "steps": steps, "reason": None, "note": note or None,
               "replaces": replaces, "replan_run_id": None, "replan_source": None,
               "excluded": sorted(excluded), "alert_id": None, "ended_t": None}
        self.runs[run["id"]] = run
        self._ctx[run["id"]] = {"plant": None, "key": None, "levels": {}, "horizon": 0, "over": {}, "alert": None}
        if start:
            await self._start(run)
        else:
            run["reason"] = (f"Re-plan offered for {replaces}. Resume it to start; that cancels {replaces}."
                             if replaces else "Not started. Resume it to start.")
            await self._broadcast(run)
        return run

    async def execute_id(self, plan_id: str, source: str = "console", by: str = "") -> dict:
        plan = self.find_plan(plan_id)
        if plan is None:  # plan ids are deterministic, so recompute from the live state
            await self.candidates(set())
            plan = self.find_plan(plan_id)
        if plan is None:
            raise ActionError(404, f"No plan {plan_id}. Call POST /api/plans (or ask Jarvis for plans) first.")
        return await self.execute(plan, source=source, by=by)

    async def execute_best(self, source: str = "jarvis", by: str = "") -> tuple[Optional[dict], dict]:
        """Jarvis's "fix it": the cheapest robust plan from the live state. Returns
        (run or None when nothing needs doing, the choice)."""
        if self.running():
            r = self.running()
            raise ActionError(409, f"{r['id']} is already running. Pause or cancel it first.")
        exclude = self.exclusions()
        plans = await self.candidates(exclude)
        choice = {"plan": pick_plan(plans, exclude), "source": "rules", "reason": ""}
        if choice["plan"] is None:
            idle = next((p for p in plans if not p.get("actions")), plans[0] if plans else None)
            return None, {"plan": idle, "source": "rules", "reason": ""}
        return await self.execute(choice["plan"], source=source, by=by, excluded=exclude), choice

    async def _start(self, run: dict) -> None:
        live = self.live
        for other in list(self.runs.values()):  # one active run at a time
            if other is not run and other["status"] == "PAUSED":
                why = (f"Replaced by {run['id']}." if other["id"] == run.get("replaces")
                       else f"Superseded by {run['id']}.")
                await self._cancel(other, why)
        run.update(status="RUNNING", started=True, start_t=live.t, start_clock=live.fmap.clock(live.t), reason=None)
        for s in run["steps"]:
            s["release_clock"] = live.fmap.clock_text(live.t + s["at_min"])
        try:
            self._ctx[run["id"]]["plant"] = build_plant(live.live_scenario())
        except Exception:  # noqa: BLE001 - no replay means no tank check, never a broken run
            log.exception("run %s: could not build the replay plant", run["id"])
        await self._release_due(run)
        await self._broadcast(run)

    # ---------- person controls ----------
    async def pause(self, run_id: str, by: str = "") -> dict:
        run = self.get(run_id)
        if run["status"] != "RUNNING":
            raise ActionError(409, f"{run_id} is {run['status']}, not RUNNING.")
        run.update(status="PAUSED", reason=f"Paused by {by or 'a person'}. Nothing new is released until it resumes.")
        await self._broadcast(run)
        return run

    async def resume(self, run_id: str, by: str = "") -> dict:
        run = self.get(run_id)
        if run["status"] != "PAUSED":
            raise ActionError(409, f"{run_id} is {run['status']}, not PAUSED.")
        other = self.running()
        if other:
            raise ActionError(409, f"{other['id']} is already running. Pause or cancel it first.")
        if not run["started"]:
            await self._start(run)
            return run
        run.update(status="RUNNING", reason=None)
        await self._clear_alert(run)
        offered = self.runs.get(run.get("replan_run_id") or "")
        if offered and not offered["started"] and offered["status"] == "PAUSED":
            await self._cancel(offered, f"{run_id} was resumed instead.")
        self._ctx[run["id"]]["over"] = {}
        await self._release_due(run)
        await self._finish_if_done(run)
        await self._broadcast(run)
        return run

    async def cancel(self, run_id: str, by: str = "") -> dict:
        run = self.get(run_id)
        if run["status"] not in ACTIVE:
            raise ActionError(409, f"{run_id} is already {run['status']}.")
        await self._cancel(run, f"Cancelled by {by or 'a person'}.")
        return run

    async def _cancel(self, run: dict, why: str) -> None:
        await self._end(run, "CANCELLED", why)

    async def _end(self, run: dict, status: str, why: str) -> None:
        """Stop a run: waiting steps are cancelled and pending proposals rejected, so
        nothing more from it can reach equipment."""
        run.update(status=status, reason=why, ended_t=now_iso())
        for s in run["steps"]:
            if s["status"] == "WAITING":
                s["status"] = "CANCELLED"
            elif s["status"] == "PENDING":
                s["status"] = "CANCELLED"
                p = self.live.proposals.get(s["proposal_id"] or "")
                if p and p["status"] == "PENDING":
                    await self.live.reject(p["id"], by=f"{run['id']} {status.lower()}")
        if status == "CANCELLED":
            await self._clear_alert(run)
        await self._broadcast(run)

    async def _clear_alert(self, run: dict) -> None:
        ctx = self._ctx[run["id"]]
        a = ctx["alert"]
        if a and a["kind"] == "RUN_PAUSED":
            await self.live.resolve_alert(a["asset_id"], a["kind"])
            ctx["alert"] = None

    async def _alert(self, run: dict, asset_id: str, kind: str, message: str) -> None:
        a = await self.live.raise_alert(asset_id, kind, "ALARM", message)
        self._ctx[run["id"]]["alert"] = a
        run["alert_id"] = a["id"]

    # ---------- events from the live field ----------
    async def _on_event(self, event: str, payload) -> None:
        if event == "decision":
            run = self.runs.get(payload.get("run_id") or "")
            n = payload.get("step")
            if run and isinstance(n, int) and 1 <= n <= len(run["steps"]):
                before = self._sig(run)
                await self._sync(run, run["steps"][n - 1], payload)
                if self._sig(run) != before:
                    await self._broadcast(run)
        elif event == "tick":
            for run in list(self.runs.values()):
                if run["status"] in ACTIVE or any(s["status"] == "VERIFYING" for s in run["steps"]):
                    await self.update(run)

    @staticmethod
    def _sig(run: dict) -> tuple:
        return (run["status"], run.get("replan_run_id"), tuple(s["status"] for s in run["steps"]))

    async def update(self, run: dict) -> None:
        """One live tick for one run: verify, watch tanks, release due steps, finish."""
        if not run["started"]:
            return  # an offered re-plan: nothing happens until a person starts it
        before = self._sig(run)
        live = self.live
        for s in run["steps"]:
            if s["status"] != "VERIFYING":
                continue
            ok, text = self._check(s)
            text = cap(text)
            if ok:
                s.update(status="DONE", verify=text)
            elif live.t - s["confirmed_t"] >= VERIFY_MIN.get(s["action"]["type"], 2.0) - 1e-9:
                s.update(status="MISMATCH", verify=text)
                if run["status"] in ACTIVE:
                    await self._problem(run, s["action"]["asset_id"], text, exclude={s["action"]["asset_id"]})
        if run["status"] in ACTIVE:
            await self._check_shutdown(run)
        if run["status"] == "RUNNING":
            await self._check_tanks(run)
        if run["status"] == "RUNNING":
            await self._release_due(run)
            await self._finish_if_done(run)
        if self._sig(run) != before:
            await self._broadcast(run)

    async def _release_due(self, run: dict) -> None:
        live = self.live
        for s in run["steps"]:
            if run["status"] != "RUNNING":
                return
            if s["status"] != "WAITING" or live.t - run["start_t"] < s["at_min"] - 1e-6:
                continue
            s["status"] = "PENDING"  # claimed before the await, so it's released once
            try:
                p = await live.propose(s["action"], source="run", extra={"run_id": run["id"], "step": s["n"]},
                                       reason=f"Step {s['n']} of {len(run['steps'])} in {run['id']} "
                                              f"({run['plan_id']}): {s['text']}")
            except ActionError as e:
                s.update(status="FAILED", result=e.detail)
                await self._problem(run, s["action"]["asset_id"], f"Step {s['n']} can't be proposed: {e.detail}",
                                    exclude={s["action"]["asset_id"]})
                continue
            await self._sync(run, s, p)

    async def _sync(self, run: dict, s: dict, p: dict) -> None:
        """Bring a step up to date with its proposal. Safe to call twice."""
        s["proposal_id"] = p["id"]
        if s["status"] not in ("WAITING", "PENDING"):
            return
        st = p["status"]
        if st == "PENDING":
            s["status"] = "PENDING"
        elif st == "CONFIRMED":
            s.update(status="VERIFYING", confirmed_t=self.live.t, result=p.get("result"))
        elif st == "REJECTED":
            if run["status"] not in ACTIVE:
                s["status"] = "CANCELLED"
                return
            s.update(status="REJECTED", result=f"Rejected by {p.get('decided_by') or 'a person'}.")
            if run["status"] == "RUNNING":
                await self._problem(run, s["action"]["asset_id"],
                                    f"Step {s['n']} ({s['text'][:-1]}) was rejected.", exclude=set())
        elif st == "FAILED":
            s.update(status="FAILED", result=p.get("result"))
            if run["status"] in ACTIVE:
                await self._problem(run, s["action"]["asset_id"],
                                    f"Step {s['n']} ({s['text'][:-1]}) failed at {p.get('connector_id')}: "
                                    f"{p.get('result')}", exclude={s["action"]["asset_id"]})

    async def _finish_if_done(self, run: dict) -> None:
        if run["status"] == "RUNNING" and all(s["status"] in FINISHED for s in run["steps"]):
            run.update(status="DONE", ended_t=now_iso(),
                       reason="Every step is confirmed and its effect seen in the live readings."
                       if all(s["status"] == "DONE" for s in run["steps"]) else
                       "Every step is finished; some were skipped when a person resumed the run.")
            await self._clear_alert(run)

    # ---------- checks ----------
    def _check(self, s: dict) -> tuple[bool, str]:
        """(effect seen, what the readings say) for a confirmed step."""
        live, a, since = self.live, s["action"], s["confirmed_t"]
        aid, typ = a["asset_id"], a["type"]
        mins = f"{VERIFY_MIN.get(typ, 2.0):g} minutes"

        def fresh(signal: str):
            r = live.latest.get((aid, signal))
            ok = r is not None and r["t"] > since + 1e-9 and not live.detector.is_stale(aid, signal)
            return r["value"] if ok else None

        if typ == "SEND_TRUCK":
            ph = live.registry.sim.truck_phase(aid)
            if ph["phase"] != "IDLE":
                return True, f"{name(aid)} is {ph['phase'].lower().replace('_', ' ')}."
            return False, f"{name(aid)} is still idle {mins} after it was dispatched to {name(a['target_id'])}."
        signal = {"SET_VALVE": "position", "SET_PUMP": "running", "CHOKE_WELL": "flow_bbl_hr"}[typ]
        v = fresh(signal)
        if v is None:
            return False, f"No fresh {signal} reading from {name(aid)} since it was commanded, so its effect is unconfirmed."
        if typ == "SET_VALVE":
            if str(v).upper() == a["value"]:
                return True, f"{name(aid)} reads {a['value']}."
            return False, f"{name(aid)} still reads {str(v).upper()} {mins} after it was commanded {a['value']}."
        if typ == "SET_PUMP":
            want = a["value"] == "ON"
            if _on(v) == want:
                return True, f"{name(aid)} reads {'running' if want else 'stopped'}."
            return False, f"{name(aid)} still reads {'stopped' if want else 'running'} {mins} after it was turned {a['value']}."
        design = float(live.fmap.wells[aid]["water_bbl_hr"])
        target = design * float(a["value"])
        try:
            flow = float(v)
        except (TypeError, ValueError):
            return False, f"{name(aid)} flow reading {v!r} isn't a number."
        if abs(flow - target) <= max(2.0, 0.1 * design):
            return True, f"{name(aid)} flows {flow:.0f} bbl/hr (target {target:.0f})."
        return False, f"{name(aid)} still flows {flow:.0f} bbl/hr {mins} after it was choked to {target:.0f}."

    async def _check_shutdown(self, run: dict) -> None:
        live = self.live
        for tid, tank in live.fmap.tanks.items():
            lvl = live.value(tid, "level_pct")
            if lvl is None or live.detector.is_stale(tid, "level_pct"):
                continue
            if float(lvl) >= float(tank["shutdown_pct"]):
                why = (f"{name(tid)} reached shutdown ({float(lvl):.1f}%, limit {tank['shutdown_pct']}%) "
                       f"during {run['id']}.")
                await self._clear_alert(run)
                await self._end(run, "FAILED", why)
                await self._alert(run, tid, "RUN_FAILED", f"{why} The run is stopped.")
                return

    def _replay(self, run: dict, ctx: dict, elapsed: float) -> dict:
        """Tank levels by minute if exactly what was confirmed had happened when it was confirmed."""
        done = [s for s in run["steps"] if s["confirmed_t"] is not None and s["status"] != "FAILED"]
        key = tuple((s["n"], s["confirmed_t"]) for s in done)
        if key != ctx["key"] or elapsed + 2 > ctx["horizon"]:
            acts = [{**s["action"], "at_min": max(0.0, s["confirmed_t"] - run["start_t"])} for s in done]
            clean, _ = clean_actions(ctx["plant"], acts)
            horizon = int(max(360, elapsed + 120))
            ctx.update(key=key, horizon=horizon, levels=simulate(ctx["plant"], clean, horizon, record_every=1).levels)
        return ctx["levels"]

    async def _check_tanks(self, run: dict) -> None:
        ctx, live = self._ctx[run["id"]], self.live
        if ctx["plant"] is None or not any(s["confirmed_t"] is not None for s in run["steps"]):
            return
        elapsed = live.t - run["start_t"]
        levels = self._replay(run, ctx, elapsed)
        lo, hi = int(math.floor(elapsed)), int(math.ceil(elapsed))
        for tid in live.fmap.tanks:
            a, b = levels.get(lo, {}).get(tid), levels.get(hi, {}).get(tid)
            lvl = live.value(tid, "level_pct")
            if a is None or b is None or lvl is None or live.detector.is_stale(tid, "level_pct"):
                ctx["over"].pop(tid, None)
                continue
            expected = a + (b - a) * (elapsed - lo)
            if float(lvl) - expected <= TANK_TOL_PCT:
                ctx["over"].pop(tid, None)
                continue
            since = ctx["over"].setdefault(tid, live.t)
            if live.t - since >= TANK_SUSTAIN_MIN - 1e-9:
                ctx["over"] = {}
                why = (f"{name(tid)} is at {float(lvl):.1f}%, {float(lvl) - expected:.1f} points above where "
                       f"the confirmed steps should have it ({expected:.1f}%). It's rising faster than forecast.")
                await self._problem(run, tid, why, exclude=set())
                return

    # ---------- pause and re-plan ----------
    async def _problem(self, run: dict, asset_id: str, why: str, exclude: set) -> None:
        """Pause the run, raise an alert, and offer Jarvis's re-plan from the current state."""
        why = cap(why)
        run["excluded"] = sorted(set(run["excluded"]) | set(exclude))
        if run["status"] == "RUNNING":
            run["status"] = "PAUSED"
        run["reason"] = why
        await self._clear_alert(run)
        await self._alert(run, asset_id, "RUN_PAUSED",
                          f"{why} {run['id']} is paused. Jarvis is re-planning from the current state.")
        await self._broadcast(run)
        if self.jarvis is not None and getattr(self.jarvis, "llm_ready", lambda: False)():
            task = asyncio.get_running_loop().create_task(self._replan(run, asset_id, why))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            await self._replan(run, asset_id, why)

    async def _replan(self, run: dict, asset_id: str, why: str) -> None:
        try:
            old = self.runs.get(run.get("replan_run_id") or "")
            if old and not old["started"] and old["status"] == "PAUSED":
                await self._cancel(old, f"Superseded by a newer re-plan for {run['id']}.")
            exclude = set(run["excluded"]) | self.exclusions()
            plans = await self.candidates(exclude)
            choice = await self.choose(plans, exclude, why)
        except Exception as e:  # noqa: BLE001 - the pause and alert stand even if planning fails
            log.exception("re-plan for %s failed", run["id"])
            choice, plans = {"plan": None, "source": "rules", "reason": f"Re-plan failed: {e}"}, []
        if run["status"] != "PAUSED":
            return  # resumed or cancelled while Jarvis was thinking
        if choice["plan"] is None:
            idle = next((p for p in plans if not p.get("actions")), None)
            offer = (idle["summary"] if idle else
                     choice.get("reason") or "Jarvis found no plan that avoids the failed equipment.")
            run.update(replan_run_id=None, replan_source=choice["source"])
            msg = f"{why} {run['id']} is paused. Re-plan: {offer}"
        else:
            new = await self.execute(choice["plan"], source="replan", start=False, replaces=run["id"],
                                     excluded=exclude, note=choice.get("reason") or "")
            run.update(replan_run_id=new["id"], replan_source=choice["source"])
            msg = (f"{why} {run['id']} is paused. Jarvis offers {new['id']}: {new['summary']} "
                   f"Resume {new['id']} to start it (each step still waits for a person), or resume {run['id']}.")
        a = self._ctx[run["id"]]["alert"]
        if a and a.get("active"):
            await self._alert(run, a["asset_id"], "RUN_PAUSED", msg)
        await self._broadcast(run)

    async def settle(self) -> None:
        """Wait for background re-plans (Claude path). Tests and shutdown use it."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
