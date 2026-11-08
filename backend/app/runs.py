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
  SEND_TRUCK  this step's own truck job (dispatched after the step was released, to the
              step's target) exists and leaves within TRUCK_ON_TIME_MIN of the confirm,
              not queued behind another job. The job is held once the confirm lands, so a
              live reload (a new simulator) doesn't lose it
within VERIFY_MIN live minutes. It also replays what was actually confirmed, at the
times it was confirmed, and watches every tank: one that runs TANK_TOL_PCT points
above that replay for TANK_SUSTAIN_MIN is "rising faster than forecast".

A live reload (a newly activated blueprint) closes a step's PENDING proposal as REJECTED
by "blueprint" when its asset is gone, it is no longer valid, or it would now go to
another connector. live.py sends no decision for that, so every tick re-reads each
PENDING step's proposal: a closed step becomes REJECTED with live.py's reason, and the
run pauses (nothing was sent) with "Plan paused: the field changed (...). Re-plan from
the current state." and a re-plan offered as below.

Any of those, or a step that is rejected or fails at its connector, pauses the run,
raises a RUN_PAUSED alert, and asks Jarvis for a re-plan from the current state (the
rules pick with no API key; Claude may pick among the planner's plans with one). The
re-plan is offered as a new run, PAUSED and not started: resuming it starts it and
cancels the run it replaces. Only one re-plan is in flight per run: a problem raised
while one is running is folded into the next one. A tank reaching shutdown during a run
fails the run.

The re-plan never leans on what went wrong: the asset of a failed or mismatched step, and
the asset (and exact action) of a step a person rejected, are excluded for the rest of the
run and every re-plan after it. When nothing left keeps every tank below shutdown, the
offer says so plainly ("Without Truck 1, no plan keeps Tank 3 below shutdown; the best
remaining plan delays it to 3:10 AM.") instead of offering the rejected asset again.

Before a run starts, its plan is scored again on the live state (clock, levels, what's
already been applied, trucks on the road). If its first step's lead time has passed, a
step can't apply any more, or it no longer keeps every tank below shutdown, the start is
refused with 409, offering the current cheapest robust plan. An offered re-plan is
instead re-timed when it is resumed: its steps keep the clock times they had when it was
offered (`offered_t`), overdue ones go out now, and it starts if that still keeps every
tank below shutdown; only otherwise is it refused with 409.

Cancel never cuts off a step whose proposal is SENDING (a person confirmed it and the
command is on its way): the run is CANCELLING, releases nothing more, and becomes
CANCELLED once those sends finish.

Statuses. Run: RUNNING, PAUSED, CANCELLING, DONE, FAILED, CANCELLED. Step: WAITING (not released),
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
STALE_GRACE_MIN = 1.0    # a plan may start this long after its first step's lead time
TRUCK_ON_TIME_MIN = 1.0  # a confirmed truck must leave within this of the confirm
SOURCES = ("console", "jarvis", "replan")   # who made a run (run["source"])


def touches(plan: dict, assets: set) -> bool:
    return any(a.get("asset_id") in assets or a.get("target_id") in assets for a in plan.get("actions", []))


def cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def same_action(a: dict, b: dict) -> bool:
    """The same command: type, asset, value, and target (timing aside)."""
    return all(str(a.get(k) or "").upper() == str(b.get(k) or "").upper()
               for k in ("type", "asset_id", "value", "target_id"))


def repeats(plan: dict, rejected: list[dict]) -> bool:
    """True when the plan contains an action a person already rejected."""
    return any(same_action(a, r) for a in plan.get("actions", []) for r in rejected)


def and_list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


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
        self._origin: dict[str, float] = {}                      # plan id -> live t it was planned at
        self._failed: list[tuple[dict, dict]] = []               # (run, active RUN_FAILED alert)
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        live.listeners.append(self._on_event)

    # ---------- plans ----------
    def remember(self, plans: list[dict], t: Optional[float] = None) -> None:
        """Keep plans computed from the live state at live minute `t` (default now), so
        their ids can be executed. POST /api/plans plans from the live clock (#42), so
        main.py remembers its results here too."""
        for p in plans:
            self.plans[p["id"]] = p
            self._origin[p["id"]] = self.live.t if t is None else float(t)

    def origin_of(self, plan_id: Optional[str]) -> float:
        """The live minute a plan was planned at: when it was remembered; minute 0 (the
        scenario's start) for POST /api/plans results; else now."""
        if plan_id in self.plans and plan_id in self._origin:
            return self._origin[plan_id]
        if plan_id in self.static_plans:
            return 0.0
        return self.live.t

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

    def _plant_now(self, sc: dict):
        """(plant, pre-actions, note): the plant for `sc` plus the truck jobs already under
        way, as free trucks sent at minute 0 (planning_scenario drops the busy trucks)."""
        jobs = self.inflight()
        plant, now = build_plant(sc), self.live.registry.sim.t
        pre = []
        for i, j in enumerate(jobs, start=1):
            sid = f"INFLIGHT_{i}"
            plant.trucks[sid] = {"id": sid, "capacity_bbl": j["remaining"], "usd_hr": 0, "haul_to_swd_min": 0,
                                 "minutes_to": {j["target_id"]: max(0.0, j["arrive"] - now)},
                                 "load_min": max(1.0, j["load_end"] - max(now, j["arrive"]))}
            pre.append({"type": "SEND_TRUCK", "asset_id": sid, "target_id": j["target_id"], "at_min": 0.0})
        names = sorted({name(j["truck_id"]) for j in jobs})
        note = f" Counts {' and '.join(names)}, already on the way." if names else ""
        return plant, pre, note

    @staticmethod
    def _score(plant, sc: dict, pre: list[dict], acts: list[dict], note: str) -> tuple[dict, list[str]]:
        """(the plan as the planner would describe it from here, warnings for steps that
        can't apply)."""
        clean, warns = clean_actions(plant, pre + acts)
        run = simulate(plant, clean, PLAN_HORIZON)
        robust = not simulate(plant, clean, PLAN_HORIZON, 1 + RANGE).shutdowns
        p = planner_plan(plant, sc["field"], acts, run, robust, PLAN_HORIZON)
        p["summary"] += note
        return p, warns

    def rescore(self, sc: dict, plans: list[dict]) -> list[dict]:
        """The planner can't see trucks already on the road (planning_scenario drops them so
        it won't send them twice). Replay its candidates, plus doing nothing more, with those
        jobs added at no extra cost, and rank them again: feasible cheapest first."""
        if not self.inflight():
            return plans
        plant, pre, note = self._plant_now(sc)
        out, seen = [], set()
        for acts in [p["actions"] for p in plans] + [[]]:
            key = json.dumps(sorted(json.dumps(a, sort_keys=True) for a in acts))
            if key in seen:
                continue
            seen.add(key)
            out.append(self._score(plant, sc, pre, acts, note)[0])
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

    def assess(self, plan: dict) -> tuple[dict, list[str]]:
        """Score `plan` on the live state as a run started now would play it: each action
        at its at_min from now, on the live levels, valves, and well flows, with the
        trucks already on the road."""
        sc = self.planning_scenario(set())
        plant, pre, note = self._plant_now(sc)
        acts = [dict(a) for a in plan.get("actions", [])]
        return self._score(plant, sc, pre, acts, note)

    async def check_fresh(self, plan: dict, origin_t: float, exclude: set = frozenset(),
                          rejected: list = ()) -> Optional[dict]:
        """Refuse (409) a plan the live field has moved past: its first step's lead time
        has passed, a step can't apply any more, or it no longer keeps every tank below
        shutdown. The 409 offers the current cheapest robust plan (`plan`, executable by
        its id) that avoids `exclude` and the `rejected` actions. Returns the plan as
        scored from the live state now (None when it wasn't scored)."""
        live, acts = self.live, plan.get("actions", [])
        if not acts:
            return None
        why, now = None, None
        first = min(float(a.get("at_min", 0) or 0) for a in acts)
        if live.t - origin_t > first + STALE_GRACE_MIN + 1e-9:
            why = (f"it was planned at {live.fmap.clock_text(origin_t)}, its first step was due by "
                   f"{live.fmap.clock_text(origin_t + first)}, and it's now {live.fmap.clock_text(live.t)}.")
        else:
            now, warns = await asyncio.to_thread(self.assess, plan)
            if warns:
                why = f"a step can't apply any more. {warns[0]}"
            elif plan.get("keeps_running", True) and not now["keeps_running"]:
                why = f"from the live state it no longer keeps every tank below shutdown. {now['summary']}"
        if why is None:
            return now
        exclude = set(exclude) | self.exclusions()
        plans = [p for p in await self.candidates(exclude) if not repeats(p, list(rejected))]
        offer = pick_plan(plans, exclude)
        msg = f"Plan {plan.get('id')} is out of date: {why}"
        if offer is None:
            idle = next((p for p in plans if not p.get("actions")), None)
            msg += " " + (idle["summary"] if idle else "No plan from the live state needs running.")
        else:
            kind = ("cheapest robust plan" if offer.get("robust") else
                    "cheapest plan that keeps every tank running" if offer.get("keeps_running") else
                    "best plan, though it can't save every tank,")
            msg += (f" From the live state, the {kind} is {offer['id']} "
                    f"(${offer.get('cost', {}).get('total_usd', 0):,.0f}): {offer['summary']} "
                    f"Execute {offer['id']} to run it.")
        raise ActionError(409, msg, extra={"stale_plan_id": plan.get("id"), "plan": offer})

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
                      replaces: Optional[str] = None, excluded: set = frozenset(), note: str = "",
                      origin_t: Optional[float] = None, rejected: list = ()) -> dict:
        """POST /api/plans/{id}/execute. 409 if another run is RUNNING or the plan is out of
        date (check_fresh); 422 if a step can't apply to this field. A run with start=False
        is an offered re-plan (PAUSED), stale only from when it was offered (`offered_t`).
        `rejected` are actions a person already said no to. `source` is one of SOURCES."""
        if start and self.running():
            r = self.running()
            raise ActionError(409, f"{r['id']} is already running. Pause or cancel it first.")
        origin = self.origin_of(plan.get("id")) if origin_t is None else origin_t
        if not start:
            origin = self.live.t  # an offer goes stale from when it was offered, not planned
        steps = []
        order = sorted(enumerate(plan.get("actions", [])), key=lambda x: (float(x[1].get("at_min", 0) or 0), x[0]))
        for n, (_, a) in enumerate(order, start=1):
            action = self.live.clean_action(a)
            steps.append({"n": n, "action": action, "at_min": float(a.get("at_min", 0) or 0),
                          "text": describe(action), "status": "WAITING", "proposal_id": None,
                          "release_clock": None, "confirmed_t": None, "result": None, "verify": None})
        if not steps:
            raise ActionError(422, f"Plan {plan.get('id')} has no actions to run.")
        if start:
            await self.check_fresh(plan, origin)
            if self.running():  # someone started one while the plan was being scored
                raise ActionError(409, f"{self.running()['id']} is already running. Pause or cancel it first.")
        run = {"id": f"run_{next(self._ids)}", "plan_id": plan.get("id"), "summary": plan.get("summary", ""),
               "cost_usd": plan.get("cost", {}).get("total_usd"), "robust": plan.get("robust"),
               "keeps_running": plan.get("keeps_running"), "status": "PAUSED", "source": source,
               "created_t": now_iso(), "created_by": by or None, "started": False, "start_t": None,
               "start_clock": None, "steps": steps, "reason": None, "note": note or None,
               "replaces": replaces, "replan_run_id": None, "replan_source": None,
               "excluded": sorted(excluded), "rejected": [dict(a) for a in rejected],
               "offered_t": None if start else self.live.t, "alert_id": None, "ended_t": None}
        self.runs[run["id"]] = run
        self._ctx[run["id"]] = {"plant": None, "key": None, "levels": {}, "horizon": 0, "over": {}, "alert": None,
                                "plan": plan, "origin_t": origin, "jobs": {}, "replanning": False,
                                "replan_want": None, "end_why": None}
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
        return await self.execute(plan, source=source, by=by, origin_t=self.origin_of(plan_id))

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
        for old, _ in list(self._failed):  # a person has taken the failure in hand
            await self._clear_failed(old)
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
            ctx = self._ctx[run["id"]]
            plan, origin = ctx["plan"], ctx["origin_t"]
            if run.get("offered_t") is not None:  # an offer: re-timed to now, then scored
                plan, origin = self._retimed(run), self.live.t
            now = await self.check_fresh(plan, origin, exclude=set(run["excluded"]), rejected=run["rejected"])
            if run["status"] != "PAUSED" or self.running():
                raise ActionError(409, f"{run_id} is {run['status']} and can't start now.")
            if run.get("offered_t") is not None:
                self._apply_retime(run, plan, now)
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

    def _retimed(self, run: dict) -> dict:
        """An offered re-plan moved to the live clock: each step keeps the clock time it
        had when the plan was offered, so its at_min (minutes from the start) shrinks by
        the live time since then, and an overdue step goes out now (at_min 0)."""
        elapsed = max(0.0, self.live.t - run["offered_t"])
        plan = self._ctx[run["id"]]["plan"]
        acts = [{**a, "at_min": round(max(0.0, float(a.get("at_min", 0) or 0) - elapsed), 3)}
                for a in plan.get("actions", [])]
        return {**plan, "actions": acts}

    def _apply_retime(self, run: dict, plan: dict, now: Optional[dict]) -> None:
        """Start an offer on its re-timed steps (check_fresh found they still work)."""
        elapsed = max(0.0, self.live.t - run["offered_t"])
        for s in run["steps"]:  # the shift keeps their order
            s["at_min"] = round(max(0.0, s["at_min"] - elapsed), 3)
        self._ctx[run["id"]].update(plan=plan, origin_t=self.live.t)
        if now is not None and elapsed > 1e-9:
            run.update(summary=now["summary"], robust=now["robust"], keeps_running=now["keeps_running"])

    async def cancel(self, run_id: str, by: str = "") -> dict:
        run = self.get(run_id)
        if run["status"] not in ACTIVE:
            raise ActionError(409, f"{run_id} is already {run['status']}.")
        await self._cancel(run, f"Cancelled by {by or 'a person'}.")
        return run

    async def _cancel(self, run: dict, why: str) -> None:
        await self._end(run, "CANCELLED", why)

    def _proposal(self, run: dict, s: dict) -> Optional[dict]:
        """A step's proposal, found by run and step too: live.propose stores it before its
        first await, so this sees a step that is being released right now."""
        p = self.live.proposals.get(s["proposal_id"] or "")
        if p is None:
            p = next((x for x in self.live.proposals.values()
                      if x.get("run_id") == run["id"] and x.get("step") == s["n"]), None)
        return p

    def _undecided(self, run: dict) -> list[dict]:
        """Steps whose proposal is still PENDING or SENDING (on its way to a connector)."""
        out = []
        for s in run["steps"]:
            p = self._proposal(run, s)
            if p is not None and p["status"] in ("PENDING", "SENDING"):
                out.append(s)
        return out

    async def _end(self, run: dict, status: str, why: str) -> None:
        """Stop a run: waiting steps are cancelled and pending proposals rejected, so
        nothing more from it can reach equipment. A step whose proposal is SENDING was
        confirmed by a person and is already on its way: it is never marked cancelled. It
        finishes like any step (VERIFYING, then DONE or MISMATCH), and a cancel waits for
        it as CANCELLING."""
        final = status
        if status == "CANCELLED":
            status = "CANCELLING"  # becomes CANCELLED in _settle_end once nothing is SENDING
        run.update(status=status, reason=why)
        self._ctx[run["id"]]["end_why"] = why
        if final != "CANCELLED":
            run["ended_t"] = now_iso()
        await self._clear_alert(run)
        for s in run["steps"]:
            if s["status"] == "WAITING":
                s["status"] = "CANCELLED"
                continue
            if s["status"] != "PENDING":
                continue
            p = self._proposal(run, s)
            if p is None:
                s["status"] = "CANCELLED"
            elif p["status"] == "PENDING":
                s.update(status="CANCELLED", proposal_id=p["id"])
                await self.live.reject(p["id"], by=f"{run['id']} {final.lower()}")
            elif p["status"] != "SENDING":
                await self._sync(run, s, p)  # decided, its decision event not here yet
        if run["status"] == "CANCELLING":
            in_flight = self._undecided(run)
            if in_flight:
                steps = ", ".join(str(s["n"]) for s in in_flight)
                run["reason"] = (f"{why} Waiting for step {steps}, already confirmed and on its way to "
                                 f"the connector, to finish. Nothing more is released.")
        await self._settle_end(run, broadcast=False)
        await self._broadcast(run)

    async def _settle_end(self, run: dict, broadcast: bool = True) -> None:
        """CANCELLING becomes CANCELLED once no step's proposal is PENDING or SENDING."""
        if run["status"] != "CANCELLING" or self._undecided(run):
            return
        run.update(status="CANCELLED", reason=self._ctx[run["id"]]["end_why"], ended_t=now_iso())
        if broadcast:
            await self._broadcast(run)

    async def _clear_alert(self, run: dict) -> None:
        ctx = self._ctx[run["id"]]
        a = ctx["alert"]
        if a and a["kind"] == "RUN_PAUSED":
            await self.live.resolve_alert(a["asset_id"], a["kind"])
            ctx["alert"] = None

    async def _alert(self, run: dict, asset_id: str, kind: str, message: str) -> None:
        a = await self.live.raise_alert(asset_id, kind, "ALARM", message)
        if kind == "RUN_FAILED":
            self._failed = [(r, x) for r, x in self._failed if r is not run] + [(run, a)]
        else:
            self._ctx[run["id"]]["alert"] = a
        run["alert_id"] = a["id"]

    async def _clear_failed(self, run: dict) -> None:
        """Resolve a failed run's RUN_FAILED alert (active: false)."""
        for r, a in list(self._failed):
            if r is run:
                self._failed.remove((r, a))
                if a.get("active"):
                    await self.live.resolve_alert(a["asset_id"], a["kind"])

    async def _check_failed(self) -> None:
        """A RUN_FAILED alert resolves once its tank reads below shutdown again."""
        live = self.live
        for run, a in list(self._failed):
            tank = live.fmap.tanks.get(a["asset_id"])
            lvl = live.value(a["asset_id"], "level_pct")
            if not a.get("active") or tank is None or (
                    lvl is not None and not live.detector.is_stale(a["asset_id"], "level_pct")
                    and float(lvl) < float(tank["shutdown_pct"])):
                await self._clear_failed(run)

    # ---------- events from the live field ----------
    async def _on_event(self, event: str, payload) -> None:
        if event == "decision":
            run = self.runs.get(payload.get("run_id") or "")
            n = payload.get("step")
            if run and isinstance(n, int) and 1 <= n <= len(run["steps"]):
                before = self._sig(run)
                await self._sync(run, run["steps"][n - 1], payload)
                await self._settle_end(run, broadcast=False)
                if self._sig(run) != before:
                    await self._broadcast(run)
        elif event == "tick":
            for run in list(self.runs.values()):
                if (run["status"] in ACTIVE or run["status"] == "CANCELLING"
                        or any(s["status"] == "VERIFYING" for s in run["steps"])):
                    await self.update(run)
            if self._failed:
                await self._check_failed()

    @staticmethod
    def _sig(run: dict) -> tuple:
        return (run["status"], run.get("replan_run_id"), tuple(s["status"] for s in run["steps"]))

    async def update(self, run: dict) -> None:
        """One live tick for one run: verify, watch tanks, release due steps, finish."""
        if not run["started"]:
            return  # an offered re-plan: nothing happens until a person starts it
        before = self._sig(run)
        live = self.live
        await self._resync(run)
        await self._settle_end(run, broadcast=False)
        for s in run["steps"]:
            if s["status"] != "VERIFYING":
                continue
            ok, text = self._check(run, s)
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
            sim = live.registry.sim   # so _check finds this step's own truck job
            self._ctx[run["id"]]["jobs"][s["n"]] = {"sim": sim, "start": len(sim.truck_jobs), "job": None}
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

    async def _resync(self, run: dict) -> None:
        """Catch up every PENDING step whose proposal was decided without a decision event:
        a live reload closes it as REJECTED by "blueprint" and tells no listener. Steps
        closed by the same reload pause the run once, with one re-plan."""
        closed = []
        for s in run["steps"]:
            if s["status"] != "PENDING":
                continue
            p = self._proposal(run, s)
            if p is None or p["status"] in ("PENDING", "SENDING"):
                continue
            await self._sync(run, s, p, closed=closed)
        await self._field_changed(run, closed)

    @staticmethod
    def _closed_why(p: dict) -> str:
        why = str(p.get("result") or "the step is no longer valid").strip()
        why = why[len("Closed:"):].strip() if why.startswith("Closed:") else why
        return why.split(" Propose it again")[0].rstrip(". ")

    async def _field_changed(self, run: dict, closed: list[dict]) -> None:
        """Steps closed by a live reload: the run pauses (nothing was sent) and re-plans."""
        if not closed or run["status"] not in ACTIVE:
            return
        whys = list(dict.fromkeys(s["result"] for s in closed))
        steps = ", ".join(str(s["n"]) for s in closed)
        why = (f"Plan paused: the field changed ({'; '.join(whys)}). Re-plan from the current state. "
               f"{'Steps' if len(closed) > 1 else 'Step'} {steps} {'were' if len(closed) > 1 else 'was'} "
               f"closed and nothing was sent.")
        present = set(self.live.fmap.all_ids())
        ids = [x for s in closed for x in (s["action"].get("asset_id"), s["action"].get("target_id")) if x]
        asset = next((x for x in ids if x in present), ids[0])   # focus on something still in the field
        await self._problem(run, asset, why, exclude=set())

    async def _sync(self, run: dict, s: dict, p: dict, closed: Optional[list] = None) -> None:
        """Bring a step up to date with its proposal. Safe to call twice. A step closed by
        a live reload goes into `closed` when given (the caller pauses the run once for
        all of them), else pauses the run here."""
        s["proposal_id"] = p["id"]
        if s["status"] not in ("WAITING", "PENDING"):
            return
        st = p["status"]
        if st == "PENDING":
            s["status"] = "PENDING"
        elif st == "CONFIRMED":
            s.update(status="VERIFYING", confirmed_t=self.live.t, result=p.get("result"))
            if s["action"]["type"] == "SEND_TRUCK":
                self._claim_job(run, s)  # the step's job was added before this
        elif st == "REJECTED" and p.get("decided_by") == "blueprint":
            if run["status"] not in ACTIVE:
                s["status"] = "CANCELLED"
                return
            s.update(status="REJECTED", result=self._closed_why(p))
            if closed is not None:
                closed.append(s)
            else:
                await self._field_changed(run, [s])
        elif st == "REJECTED":
            if run["status"] not in ACTIVE:
                s["status"] = "CANCELLED"
                return
            s.update(status="REJECTED", result=f"Rejected by {p.get('decided_by') or 'a person'}.")
            # A person said no: no re-plan of this run (or of its re-plans) uses that asset again.
            aid = s["action"]["asset_id"]
            if not any(same_action(s["action"], r) for r in run["rejected"]):
                run["rejected"].append(dict(s["action"]))
            offer = self.runs.get(run.get("replan_run_id") or "")
            stale_offer = (offer is not None and not offer["started"] and offer["status"] == "PAUSED"
                           and touches(self._ctx[offer["id"]]["plan"], {aid}))
            if run["status"] == "RUNNING" or stale_offer:
                await self._problem(run, aid, f"Step {s['n']} ({s['text'][:-1]}) was rejected.", exclude={aid})
            else:
                run["excluded"] = sorted(set(run["excluded"]) | {aid})
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
    def _claim_job(self, run: dict, s: dict) -> Optional[dict]:
        """Find and keep this step's own truck job: the latest job for this truck and
        target added after the step was released. Called when the confirm lands (the
        connector adds the job before that), so the job is then held by identity, with the
        simulator time it was claimed at. A live reload (a new simulator, with an empty job
        list and its own clock) can't lose it or flip a good step to MISMATCH.

        A reload between the release and the confirm means the job went to the new
        simulator, every one of whose jobs came after the release, so it is searched from
        its start. The release-time simulator is searched too, for a reload that landed
        between the send and this call."""
        mark = self._ctx[run["id"]]["jobs"].get(s["n"])
        if not mark:
            return None
        if mark["job"] is not None:
            return mark["job"]
        a, cur = s["action"], self.live.registry.sim
        places = [(cur, mark["start"] if mark["sim"] is cur else 0)]
        if mark["sim"] is not cur:
            places.append((mark["sim"], mark["start"]))
        for sim, start in places:
            mine = [j for j in sim.truck_jobs[start:]
                    if j["truck_id"] == a["asset_id"] and j["target_id"] == a["target_id"]]
            if mine:
                mark.update(job=mine[-1], sim_t=sim.t, live_t=self.live.t)
                return mark["job"]
        return None

    def _truck_job(self, run: dict, s: dict) -> tuple[Optional[dict], float, float]:
        """(this step's own job, its simulator's time at the confirm, that simulator's time
        now). Job times are on their simulator's clock, which restarts at 0 on a reload,
        so they are compared on that clock, carried forward by live time."""
        job = self._claim_job(run, s)
        if job is None:
            return None, 0.0, 0.0
        mark = self._ctx[run["id"]]["jobs"][s["n"]]
        at_confirm = mark["sim_t"] - (mark["live_t"] - s["confirmed_t"])
        return job, at_confirm, mark["sim_t"] + (self.live.t - mark["live_t"])

    def _check(self, run: dict, s: dict) -> tuple[bool, str]:
        """(effect seen, what the readings say) for a confirmed step."""
        live, a, since = self.live, s["action"], s["confirmed_t"]
        aid, typ = a["asset_id"], a["type"]
        mins = f"{VERIFY_MIN.get(typ, 2.0):g} minutes"

        def fresh(signal: str):
            r = live.latest.get((aid, signal))
            ok = r is not None and r["t"] > since + 1e-9 and not live.detector.is_stale(aid, signal)
            return r["value"] if ok else None

        if typ == "SEND_TRUCK":
            (job, at_confirm, t), tgt = self._truck_job(run, s), name(a["target_id"])
            if job is None:
                return False, f"No dispatch of {name(aid)} to {tgt} seen {mins} after it was confirmed."
            if job["depart"] > at_confirm + TRUCK_ON_TIME_MIN + 1e-9:
                leaves = since + (job["depart"] - at_confirm)   # on the live clock
                return False, (f"{name(aid)} is queued behind another job: it leaves for {tgt} at "
                               f"{live.fmap.clock_text(leaves)}, not when it was confirmed.")
            phase = ("queued" if t < job["depart"] else "en route" if t < job["arrive"] else
                     "loading" if t < job["load_end"] else "hauling to disposal" if t < job["done"] else "done")
            return True, f"{name(aid)} is {phase} for {tgt}."
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
        ctx = self._ctx[run["id"]]
        ctx["replan_want"] = (asset_id, why)
        if ctx["replanning"] or self._closed:
            return  # the re-plan in flight picks this up when it's done
        ctx["replanning"] = True
        if self.jarvis is not None and getattr(self.jarvis, "llm_ready", lambda: False)():
            task = asyncio.get_running_loop().create_task(self._replans(run))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            await self._replans(run)

    async def _replans(self, run: dict) -> None:
        """One re-plan at a time per run; a problem raised meanwhile gets one more."""
        ctx = self._ctx[run["id"]]
        try:
            while ctx["replan_want"] is not None and not self._closed:
                asset_id, why = ctx["replan_want"]
                ctx["replan_want"] = None
                await self._replan(run, asset_id, why)
        finally:
            ctx["replanning"] = False

    async def _replan(self, run: dict, asset_id: str, why: str) -> None:
        try:
            old = self.runs.get(run.get("replan_run_id") or "")
            if old and not old["started"] and old["status"] == "PAUSED":
                await self._cancel(old, f"Superseded by a newer re-plan for {run['id']}.")
            exclude = set(run["excluded"]) | self.exclusions()
            plans = [p for p in await self.candidates(exclude) if not repeats(p, run["rejected"])]
            choice = await self.choose(plans, exclude, why)
            short = await asyncio.to_thread(self._shortfall, run, exclude, choice["plan"])
        except Exception as e:  # noqa: BLE001 - the pause and alert stand even if planning fails
            log.exception("re-plan for %s failed", run["id"])
            choice, plans, short = {"plan": None, "source": "rules", "reason": f"Re-plan failed: {e}"}, [], ""
        if run["status"] != "PAUSED":
            return  # resumed or cancelled while Jarvis was thinking
        if choice["plan"] is None:
            idle = next((p for p in plans if not p.get("actions")), None)
            offer = (idle["summary"] if idle else
                     choice.get("reason") or "Jarvis found no plan that avoids the failed equipment.")
            run.update(replan_run_id=None, replan_source=choice["source"])
            msg = f"{why} {run['id']} is paused. Re-plan: {short or offer}"
        else:
            new = await self.execute(choice["plan"], source="replan", start=False, replaces=run["id"],
                                     excluded=exclude, note=choice.get("reason") or "", rejected=run["rejected"])
            run.update(replan_run_id=new["id"], replan_source=choice["source"])
            if short:
                new["reason"] = f"{short} {new['reason']}"
            msg = (f"{why} {run['id']} is paused. {short + ' ' if short else ''}Jarvis offers {new['id']}: "
                   f"{new['summary']} Resume {new['id']} to start it (each step still waits for a person), "
                   f"or resume {run['id']}.")
        a = self._ctx[run["id"]]["alert"]
        if a and a.get("active"):
            await self._alert(run, a["asset_id"], "RUN_PAUSED", msg)
        await self._broadcast(run)

    def _shortfall(self, run: dict, exclude: set, plan: Optional[dict]) -> str:
        """'' when the re-plan keeps every tank below shutdown (or nothing is needed). Else
        the plain sentence: "Without Truck 1, no plan keeps Tank 3 below shutdown; the best
        remaining plan delays it to 3:10 AM." It never points back at an excluded asset."""
        if plan is not None and plan.get("keeps_running"):
            return ""
        live = self.live
        sc = self.planning_scenario(exclude)
        plant, pre, _ = self._plant_now(sc)
        nothing = simulate(plant, clean_actions(plant, pre)[0], PLAN_HORIZON).shutdowns
        best = nothing if plan is None else simulate(
            plant, clean_actions(plant, pre + [dict(a) for a in plan["actions"]])[0], PLAN_HORIZON).shutdowns
        if not best:
            return ""
        tanks = sorted(best, key=lambda t: best[t])
        gone = [name(a) for a in sorted(exclude) if a in live.fmap.all_ids()]
        line = (f"Without {and_list(gone)}, no plan" if gone else "No plan") + \
            f" keeps {and_list([name(t) for t in tanks])} below shutdown"
        at = {t: live.fmap.clock_text(live.t + best[t]) for t in tanks}
        if plan is None:
            return f"{line}, and no remaining plan helps: " + and_list(
                [f"{name(t)} reaches shutdown at {at[t]}" for t in tanks]) + "."
        one = len(tanks) == 1
        parts = [f"delays {'it' if one else name(t)} to {at[t]}" if best[t] > nothing.get(t, best[t]) + 1e-6
                 else f"still reaches shutdown at {at[t]}" if one else f"leaves {name(t)} reaching shutdown at {at[t]}"
                 for t in tanks]
        return f"{line}; the best remaining plan {and_list(parts)}."

    async def settle(self) -> None:
        """Wait for background re-plans (Claude path). Tests use it."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        """Server shutdown (main.py's lifespan): cancel background re-plans, start no more."""
        self._closed = True
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
