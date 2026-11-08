"""Plan runs (#63): Jarvis executes a plan, a person confirms each step."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.hub import Hub
from backend.app.jarvis import Jarvis
from backend.app.live import ActionError, LiveField
from backend.app.main import app
from backend.app.planner import make_plans
from backend.app.runs import RunManager, pick_plan
from backend.app.scenario import TEAM_SCENARIO, load_scenario

needs_field = pytest.mark.skipif(not TEAM_SCENARIO.exists(), reason="scenario/field.json not in this checkout")


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "JARVIS_MODE", "JARVIS_CONFIRM", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)


class RecHub(Hub):
    """Records every /ws message instead of sending it."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, dict]] = []

    async def broadcast(self, typ: str, payload: dict) -> None:
        self.sent.append((typ, json.loads(json.dumps(payload, default=str))))


def field(raw_extra: dict | None = None) -> tuple[LiveField, RunManager, Jarvis]:
    """The Avon field from scenario/field.json, run past the Pump 3 trip until it's detected."""
    sc = load_scenario(TEAM_SCENARIO)
    raw = json.loads(TEAM_SCENARIO.read_text())
    raw.update(raw_extra or {})
    live = LiveField(sc, raw, hub=RecHub())
    jarvis = Jarvis(live, forecast_fn=lambda s, a: {}, plans_fn=lambda s: make_plans(s))
    runs = RunManager(live, jarvis)
    jarvis.runs = runs
    for _ in range(16):
        live.step(0.25)
    assert any(k == ("PUMP_3", "PUMP_TRIP") for k in live.detector.active)
    return live, runs, jarvis


def cheapest(runs: RunManager) -> dict:
    plans = make_plans(runs.live.live_scenario(), top_n=10)["plans"]
    runs.remember(plans)
    p = plans[0]
    assert p["keeps_running"] and p["actions"]
    return p


async def advance(live: LiveField, minutes: float, confirm: bool = True, dt: float = 0.5,
                  until=None) -> None:
    """Tick the live field; a person confirms each run step as it arrives."""
    end = live.t + minutes
    while live.t < end - 1e-9:
        await live.tick(dt)
        if confirm:
            for p in list(live.proposals.values()):
                if p["status"] == "PENDING" and p.get("run_id"):
                    await live.confirm(p["id"], by="operator")
        if until and until():
            return


def statuses(run: dict) -> list[str]:
    return [s["status"] for s in run["steps"]]


# ---------- the happy path ----------

@needs_field
def test_cheapest_plan_confirmed_step_by_step_ends_with_no_shutdown():
    live, runs, _ = field()
    plan = cheapest(runs)

    async def go():
        run = await runs.execute(plan, by="test")
        assert run["status"] == "RUNNING" and run["plan_id"] == plan["id"]
        assert [s["at_min"] for s in run["steps"]] == sorted(s["at_min"] for s in run["steps"])
        assert all(s["status"] == "WAITING" for s in run["steps"])  # every at_min is > 0
        await advance(live, 360)
        return run

    run = asyncio.run(go())
    assert run["status"] == "DONE", run["reason"]
    assert statuses(run) == ["DONE"] * len(run["steps"])
    sim = live.registry.sim
    assert not any(sim.tank_shutdown.values()), sim.events
    assert not any(e["type"] == "SHUTDOWN_LEVEL" for e in sim.events)
    # Each step went out as an ordinary proposal, confirmed by a person, through the simulator.
    for s in run["steps"]:
        p = live.proposals[s["proposal_id"]]
        assert p["status"] == "CONFIRMED" and p["decided_by"] == "operator"
        assert p["source"] == "run" and p["run_id"] == run["id"] and p["step"] == s["n"]
        assert p["connector_id"] == "CONN_SIM"
    # The crossover step was released at its at_min, not before.
    xo = next(s for s in run["steps"] if s["action"]["type"] == "SET_VALVE")
    assert sim.valve_open[xo["action"]["asset_id"]]
    # /ws carried run messages with the contract shape.
    msgs = [m["run"] for t, m in live.hub.sent if t == "run"]
    assert msgs and msgs[-1]["status"] == "DONE"
    assert {"id", "plan_id", "steps", "status"} <= set(msgs[-1])
    assert {"proposal_id", "action", "status"} <= set(msgs[-1]["steps"][0])


@needs_field
def test_nothing_moves_while_steps_are_pending():
    live, runs, _ = field()
    plan = cheapest(runs)
    sim = live.registry.sim
    valves, chokes = dict(sim.valve_open), dict(sim.choke)

    async def go():
        run = await runs.execute(plan)
        await advance(live, 45, confirm=False)
        return run

    run = asyncio.run(go())
    assert run["status"] == "RUNNING"
    assert statuses(run) == ["PENDING"] * len(run["steps"])  # all released, none confirmed
    assert all(live.proposals[s["proposal_id"]]["status"] == "PENDING" for s in run["steps"])
    assert sim.valve_open == valves and sim.choke == chokes and sim.truck_jobs == []


# ---------- a stuck valve ----------

@needs_field
def test_stuck_valve_pauses_the_run_and_offers_a_replan():
    live, runs, _ = field()
    plan = cheapest(runs)
    xo = next(a for a in plan["actions"] if a["type"] == "SET_VALVE")["asset_id"]
    live.registry.sim.inject({"asset_id": xo, "type": "STUCK_VALVE"})

    async def go():
        run = await runs.execute(plan)
        await advance(live, 60, until=lambda: run["status"] != "RUNNING")
        return run

    run = asyncio.run(go())
    assert run["status"] == "PAUSED"
    step = next(s for s in run["steps"] if s["action"]["asset_id"] == xo)
    assert step["status"] == "MISMATCH" and "CLOSED" in step["verify"]
    assert xo in run["excluded"]
    # An alert went up for the valve.
    alert = next(a for a in live.alerts() if a["kind"] == "RUN_PAUSED")
    assert alert["active"] and alert["asset_id"] == xo and alert["severity"] == "ALARM"
    assert alert["id"] == run["alert_id"]
    # Jarvis (rules, no key) re-planned from the current state, offered as a new run.
    assert run["replan_source"] == "rules"
    new = runs.get(run["replan_run_id"])
    assert new["status"] == "PAUSED" and not new["started"] and new["replaces"] == run["id"]
    assert all(s["status"] == "WAITING" and s["proposal_id"] is None for s in new["steps"])
    used = {s["action"]["asset_id"] for s in new["steps"]}
    assert xo not in used                                   # not the stuck valve
    busy = {s["action"]["asset_id"] for s in run["steps"] if s["action"]["type"] == "SEND_TRUCK"}
    assert not used & busy                                  # not trucks already out
    assert new["id"] in alert["message"]

    # Starting the re-plan cancels the stuck run; its steps still each need a person.
    async def go2():
        await runs.resume(new["id"], by="operator")
        assert run["status"] == "CANCELLED" and new["status"] == "RUNNING"
        await advance(live, 300)

    asyncio.run(go2())
    assert not alert["active"]
    assert new["status"] == "DONE", new["reason"]
    assert not any(live.registry.sim.tank_shutdown.values())


@needs_field
def test_tank_rising_faster_than_forecast_pauses_the_run():
    live, runs, _ = field()
    plan = cheapest(runs)

    async def go():
        run = await runs.execute(plan)
        await advance(live, 8)  # the trucks are confirmed at 5 min
        assert any(s["status"] == "DONE" for s in run["steps"])
        live.registry.sim.inject({"asset_id": "PUMP_4", "type": "TRIP"})  # Tank 4 starts filling
        live.registry.sim.inject({"asset_id": "PUMP_1", "type": "TRIP"})
        live.registry.sim.inject({"asset_id": "PUMP_2", "type": "TRIP"})
        await advance(live, 60, until=lambda: run["status"] != "RUNNING")
        return run

    run = asyncio.run(go())
    assert run["status"] == "PAUSED"
    assert "rising faster than forecast" in run["reason"]
    assert any(a["kind"] == "RUN_PAUSED" and a["active"] for a in live.alerts())


# ---------- person controls ----------

@needs_field
def test_cancel_stops_further_steps():
    live, runs, _ = field()
    plan = cheapest(runs)

    async def go():
        run = await runs.execute(plan)
        await advance(live, 6)            # the 5-minute steps are confirmed
        done = [s for s in run["steps"] if s["status"] in ("VERIFYING", "DONE")]
        assert done and any(s["status"] == "WAITING" for s in run["steps"])
        await runs.cancel(run["id"], by="operator")
        n = len(live.proposals)
        await advance(live, 60)
        return run, done, n

    run, done, n = asyncio.run(go())
    assert run["status"] == "CANCELLED"
    assert len(live.proposals) == n       # nothing released after cancel
    later = [s for s in run["steps"] if s not in done]
    assert later and all(s["status"] == "CANCELLED" for s in later)
    xo = next(s for s in later if s["action"]["type"] == "SET_VALVE")
    assert live.registry.sim.valve_open[xo["action"]["asset_id"]] is False
    with pytest.raises(ActionError) as e:
        asyncio.run(runs.resume(run["id"]))
    assert e.value.status == 409


@needs_field
def test_cancel_rejects_a_pending_step_and_pause_holds_releases():
    live, runs, _ = field()
    plan = cheapest(runs)

    async def go():
        run = await runs.execute(plan)
        await advance(live, 6, confirm=False)
        pending = [s for s in run["steps"] if s["status"] == "PENDING"]
        await runs.pause(run["id"])
        n = len(live.proposals)
        await advance(live, 30, confirm=False)
        assert len(live.proposals) == n   # paused: nothing new released
        await runs.resume(run["id"])      # overdue steps go out now
        assert len(live.proposals) > n
        await runs.cancel(run["id"])
        return run, pending

    run, pending = asyncio.run(go())
    for s in pending:
        assert live.proposals[s["proposal_id"]]["status"] == "REJECTED"
    assert all(s["status"] == "CANCELLED" for s in run["steps"])
    with pytest.raises(ActionError):
        asyncio.run(live.confirm(pending[0]["proposal_id"]))


@needs_field
def test_rehearsal_auto_confirm_never_reaches_real_equipment(monkeypatch):
    """JARVIS_CONFIRM=0 confirms simulator steps by itself, never a real connector's."""
    monkeypatch.setenv("JARVIS_CONFIRM", "0")
    seen = []
    live, runs, _ = field({"connectors": [{
        "id": "CONN_HOOK", "kind": "webhook", "config": {"url": "http://gateway.local/cmd"},
        "commands": [{"asset_id": "VALVE_XO_34", "command": "SET_VALVE", "address": "xo34/cmd"}]}]})
    live.registry.get("CONN_HOOK").transport = httpx.MockTransport(
        lambda req: seen.append(req) or httpx.Response(200, json={"ok": True}))
    plan = cheapest(runs)
    assert any(a["asset_id"] == "VALVE_XO_34" for a in plan["actions"])

    async def go():
        run = await runs.execute(plan)
        await advance(live, 40, confirm=False)
        return run

    run = asyncio.run(go())
    xo = next(s for s in run["steps"] if s["action"]["asset_id"] == "VALVE_XO_34")
    assert xo["status"] == "PENDING" and seen == []
    assert live.proposals[xo["proposal_id"]]["connector_id"] == "CONN_HOOK"
    trucks = [s for s in run["steps"] if s["action"]["type"] == "SEND_TRUCK"]
    assert trucks and all(s["status"] == "DONE" for s in trucks)  # simulator-only, auto-confirmed


# ---------- Jarvis ----------

@needs_field
def test_jarvis_fix_it_proposes_the_cheapest_robust_plan():
    live, runs, jarvis = field()
    body = asyncio.run(jarvis.handle("Jarvis, fix it"))
    assert body["source"] == "rules"
    (run,) = body["runs"]
    plans = make_plans(runs.planning_scenario(set()), top_n=10)["plans"]
    assert run["plan_id"] == pick_plan(plans)["id"]
    assert run["robust"] and run["keeps_running"]
    assert run["status"] == "RUNNING" and run["source"] == "jarvis"
    assert "waits for your confirmation" in body["reply"]
    assert body["ui"] and body["ui"][0]["asset_id"] == run["steps"][0]["action"]["asset_id"]
    # Asking again while it runs is refused, not doubled.
    again = asyncio.run(jarvis.handle("fix it"))
    assert again["runs"] == [] and "already running" in again["reply"]
    assert any(t["name"] == "execute_plan" for t in __import__("backend.app.jarvis", fromlist=["TOOLS"]).TOOLS)


def test_pick_plan_prefers_robust_and_skips_excluded():
    a = {"id": "a", "actions": [{"asset_id": "VALVE_XO_34"}], "keeps_running": True, "robust": False}
    b = {"id": "b", "actions": [{"asset_id": "WELL_9"}], "keeps_running": True, "robust": True}
    c = {"id": "c", "actions": [{"asset_id": "TRUCK_1", "target_id": "TANK_3"}], "keeps_running": True, "robust": True}
    assert pick_plan([a, b, c])["id"] == "b"
    assert pick_plan([a, b, c], {"WELL_9"})["id"] == "c"
    assert pick_plan([a], {"TANK_3"})["id"] == "a"
    assert pick_plan([{"id": "n", "actions": [], "keeps_running": True, "robust": True}]) is None


# ---------- HTTP ----------

client = TestClient(app)


def test_runs_over_http():
    plans = client.post("/api/plans").json()["plans"]
    pid = next(p["id"] for p in plans if p["actions"])
    assert client.post("/api/plans/plan_nope/execute").status_code == 404
    r = client.post(f"/api/plans/{pid}/execute", json={"by": "test"})
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["status"] == "RUNNING" and run["plan_id"] == pid
    assert {"id", "plan_id", "steps", "status"} <= set(run)
    assert client.post(f"/api/plans/{pid}/execute").status_code == 409  # one running at a time
    assert run["id"] in [x["id"] for x in client.get("/api/runs").json()["runs"]]
    assert client.get(f"/api/runs/{run['id']}").json()["id"] == run["id"]
    assert client.get("/api/runs/run_nope").status_code == 404
    assert client.post(f"/api/runs/{run['id']}/pause").json()["status"] == "PAUSED"
    assert client.post(f"/api/runs/{run['id']}/pause").status_code == 409
    assert client.post(f"/api/runs/{run['id']}/resume").json()["status"] == "RUNNING"
    assert client.post(f"/api/runs/{run['id']}/explode").status_code == 404
    assert client.post(f"/api/runs/{run['id']}/cancel", json={"by": "test"}).json()["status"] == "CANCELLED"
    assert client.post(f"/api/runs/{run['id']}/cancel").status_code == 409


def test_jarvis_fix_it_over_http():
    r = client.post("/api/jarvis", json={"message": "fix it"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "runs" in body
    for run in body["runs"]:
        assert all(s["status"] in ("WAITING", "PENDING") for s in run["steps"])
        client.post(f"/api/runs/{run['id']}/cancel")


# ---------- cancel while a step is being sent ----------

def hook_field():
    """The Avon field with the Tank 3 to 4 crossover on a webhook whose send blocks until
    the test lets it go (a slow real connector). Returns (live, runs, gate, requests)."""
    import threading

    gate, seen = threading.Event(), []
    live, runs, _ = field({"connectors": [{
        "id": "CONN_HOOK", "kind": "webhook", "config": {"url": "http://gateway.local/cmd"},
        "commands": [{"asset_id": "VALVE_XO_34", "command": "SET_VALVE", "address": "xo34/cmd"}]}]})

    def handler(req):
        seen.append(req)
        assert gate.wait(10)
        return httpx.Response(200, json={"ok": True})

    live.registry.get("CONN_HOOK").transport = httpx.MockTransport(handler)
    return live, runs, gate, seen


@needs_field
def test_cancel_during_a_confirm_waits_for_the_send_and_releases_nothing_more():
    live, runs, gate, seen = hook_field()
    plan = cheapest(runs)

    async def go():
        run = await runs.execute(plan)
        await advance(live, 21, confirm=False)  # every step is out, none confirmed
        xo = next(s for s in run["steps"] if s["action"]["asset_id"] == "VALVE_XO_34")
        others = [s for s in run["steps"] if s is not xo]
        assert xo["status"] == "PENDING"
        sending = asyncio.create_task(live.confirm(xo["proposal_id"], by="operator"))
        await asyncio.sleep(0.05)
        assert live.proposals[xo["proposal_id"]]["status"] == "SENDING"

        out = await runs.cancel(run["id"], by="operator")
        assert out["status"] == "CANCELLING"          # not CANCELLED while a send is in flight
        assert xo["status"] == "PENDING"              # never marked cancelled while SENDING
        assert "on its way" in out["reason"]
        assert all(s["status"] == "CANCELLED" for s in others)
        assert all(live.proposals[s["proposal_id"]]["status"] == "REJECTED" for s in others)
        n = len(live.proposals)
        await live.tick(0.5)                          # nothing released, still waiting
        assert len(live.proposals) == n and run["status"] == "CANCELLING"
        with pytest.raises(ActionError):
            await runs.cancel(run["id"])
        with pytest.raises(ActionError):
            await runs.resume(run["id"])

        gate.set()
        p = await sending
        assert p["status"] == "CONFIRMED"
        assert run["status"] == "CANCELLED" and run["ended_t"]
        assert run["reason"] == "Cancelled by operator."
        assert xo["status"] in ("VERIFYING", "DONE", "MISMATCH")  # it was sent, so it's checked
        await advance(live, 10, confirm=False)
        assert len(live.proposals) == n
        return run

    run = asyncio.run(go())
    assert len(seen) == 1                             # sent exactly once
    msgs = [m["run"]["status"] for t, m in live.hub.sent if t == "run" and m["run"]["id"] == run["id"]]
    assert "CANCELLING" in msgs and msgs[-1] == "CANCELLED"


@needs_field
def test_cancel_during_an_auto_confirm_release_never_sends(monkeypatch):
    """A cancel that lands while a step is being released (its proposal broadcast) rejects
    that proposal, so JARVIS_CONFIRM=0 can't auto-confirm it afterwards."""
    monkeypatch.setenv("JARVIS_CONFIRM", "0")
    live, runs, _ = field()
    plan = cheapest(runs)

    class YieldHub(RecHub):
        async def broadcast(self, typ, payload):
            await super().broadcast(typ, payload)
            await asyncio.sleep(0)

    live.hub = YieldHub()
    sim = live.registry.sim

    async def go():
        run = await runs.execute(plan)
        await advance(live, 4, confirm=False)
        release = asyncio.create_task(live.tick(1.0))   # the 5-minute truck steps go out now
        for _ in range(20):
            await asyncio.sleep(0)
            if any(p.get("run_id") == run["id"] for p in live.proposals.values()):
                break
        assert any(p.get("run_id") == run["id"] for p in live.proposals.values())
        await runs.cancel(run["id"], by="operator")
        await release
        return run

    run = asyncio.run(go())
    assert run["status"] == "CANCELLED"
    assert sim.truck_jobs == []
    for p in live.proposals.values():
        if p.get("run_id") == run["id"]:
            assert p["status"] == "REJECTED"


# ---------- SEND_TRUCK verifies this step's own job ----------

@needs_field
def test_truck_step_verifies_its_own_job_not_just_a_busy_truck():
    live, runs, _ = field()
    plan = cheapest(runs)
    step = next(a for a in plan["actions"] if a["type"] == "SEND_TRUCK")
    truck, target = step["asset_id"], step["target_id"]
    elsewhere = next(t for t in live.fmap.trucks[truck]["minutes_to"] if t != target)

    async def go():
        run = await runs.execute(plan)
        # Before the step comes out, someone sends the same truck to another tank.
        p = await live.propose({"type": "SEND_TRUCK", "asset_id": truck, "target_id": elsewhere})
        await live.confirm(p["id"], by="someone")
        assert live.registry.sim.truck_phase(truck)["phase"] != "IDLE"
        await advance(live, 60, until=lambda: run["status"] != "RUNNING")
        return run

    run = asyncio.run(go())
    s = next(s for s in run["steps"] if s["action"]["asset_id"] == truck)
    # The old check ("not IDLE") would pass: the truck is busy, but with another job.
    assert s["status"] == "MISMATCH", s
    assert "queued behind another job" in s["verify"]
    assert run["status"] == "PAUSED" and truck in run["excluded"]


@needs_field
def test_truck_step_with_no_job_of_its_own_is_a_mismatch():
    live, runs, _ = field()
    plan = cheapest(runs)
    step = next(a for a in plan["actions"] if a["type"] == "SEND_TRUCK")
    sim = live.registry.sim
    real_send = sim.send
    # A connector that says ok but dispatches nothing (the sim's own job list stays empty).
    sim.send = lambda c: {"ok": True, "detail": "sent"} if c.get("type") == "SEND_TRUCK" else real_send(c)

    async def go():
        run = await runs.execute(plan)
        await advance(live, 30, until=lambda: run["status"] != "RUNNING")
        return run

    run = asyncio.run(go())
    s = next(s for s in run["steps"] if s["action"]["asset_id"] == step["asset_id"])
    assert s["status"] == "MISMATCH" and "No dispatch" in s["verify"]


@needs_field
def test_truck_step_verify_names_its_target():
    live, runs, _ = field()
    plan = cheapest(runs)

    async def go():
        run = await runs.execute(plan)
        await advance(live, 8)
        return run

    run = asyncio.run(go())
    s = next(s for s in run["steps"] if s["action"]["type"] == "SEND_TRUCK")
    assert s["status"] == "DONE" and "en route for Tank 3" in s["verify"]


# ---------- stale plans ----------

@needs_field
def test_execute_refuses_a_plan_whose_first_lead_time_has_passed():
    live, runs, _ = field()
    plan = cheapest(runs)

    async def go():
        for _ in range(16):          # 8 live minutes: the 5-minute truck steps are overdue
            await live.tick(0.5)
        with pytest.raises(ActionError) as e:
            await runs.execute_id(plan["id"])
        return e.value

    err = asyncio.run(go())
    assert err.status == 409 and "out of date" in err.detail and "first step was due" in err.detail
    offer = err.extra["plan"]
    assert err.extra["stale_plan_id"] == plan["id"] and offer and offer["actions"]
    assert offer["id"] in err.detail
    assert runs.list() == []
    # The offer is planned from now, so it can be executed by its id at once.
    run = asyncio.run(runs.execute_id(offer["id"]))
    assert run["status"] == "RUNNING" and run["plan_id"] == offer["id"]


@needs_field
def test_execute_refuses_a_plan_that_no_longer_keeps_tanks_below_shutdown():
    live, runs, _ = field()
    plan = cheapest(runs)
    for pid in ("PUMP_4", "PUMP_1", "PUMP_2"):
        live.registry.sim.inject({"asset_id": pid, "type": "TRIP"})

    async def go():
        for _ in range(4):           # 1 live minute: within the lead time, but three more trips
            await live.tick(0.25)
        with pytest.raises(ActionError) as e:
            await runs.execute(plan)
        return e.value

    err = asyncio.run(go())
    assert err.status == 409 and "no longer keeps every tank below shutdown" in err.detail
    assert err.extra["plan"]["id"] != plan["id"]
    assert runs.list() == []


@needs_field
def test_execute_refuses_a_plan_whose_truck_is_already_out():
    live, runs, _ = field()
    plan = cheapest(runs)
    truck = next(a for a in plan["actions"] if a["type"] == "SEND_TRUCK")

    async def go():
        p = await live.propose({"type": "SEND_TRUCK", "asset_id": truck["asset_id"], "target_id": "TANK_1"})
        await live.confirm(p["id"])
        with pytest.raises(ActionError) as e:
            await runs.execute(plan)
        return e.value

    err = asyncio.run(go())
    assert err.status == 409 and "can't apply any more" in err.detail


@needs_field
def test_stale_offered_replan_is_refused_on_resume():
    live, runs, _ = field()
    plan = cheapest(runs)
    xo = next(a for a in plan["actions"] if a["type"] == "SET_VALVE")["asset_id"]
    live.registry.sim.inject({"asset_id": xo, "type": "STUCK_VALVE"})

    async def go():
        run = await runs.execute(plan)
        await advance(live, 60, until=lambda: run["status"] != "RUNNING")
        new = runs.get(run["replan_run_id"])
        await advance(live, 30, confirm=False)  # nobody looks at the offer for 30 minutes
        with pytest.raises(ActionError) as e:
            await runs.resume(new["id"])
        return new, e.value

    new, err = asyncio.run(go())
    assert err.status == 409 and "out of date" in err.detail and err.extra["plan"]
    assert not new["started"] and new["status"] == "PAUSED"


def test_stale_plan_409_over_http_offers_the_current_plan():
    from fastapi import FastAPI

    from backend.app.live_api import install

    sc = load_scenario()
    static = {p["id"]: p for p in make_plans(sc)["plans"]}
    pid = next(i for i, p in static.items() if p["actions"])
    app2 = FastAPI()
    live = install(app2, sc, plans=static)
    c = TestClient(app2)
    for _ in range(60):
        live.step(0.5)               # 30 live minutes past the plan's start
    r = c.post(f"/api/plans/{pid}/execute")
    assert r.status_code == 409, r.text
    body = r.json()
    assert "out of date" in body["detail"] and body["stale_plan_id"] == pid
    assert body["plan"] is None or body["plan"]["id"]
    if body["plan"]:
        r2 = c.post(f"/api/plans/{body['plan']['id']}/execute")
        assert r2.status_code == 200, r2.text


# ---------- Jarvis "fix it" ----------

def test_fix_it_request_reads_negations_and_questions():
    from backend.app.jarvis import fix_it_request

    for t in ("fix it", "Jarvis, fix it.", "make it so", "Please run the plan", "Can you fix it?",
              "Could you please run the cheapest plan?", "hey jarvis, execute the plan", "ok, fix it now"):
        assert fix_it_request(t) == "run", t
    for t in ("Don't fix it", "do not run the plan yet", "never run the plan", "Not yet, don't make it so",
              "Hold off, wait. I said don't fix it", "I don't want you to fix it"):
        assert fix_it_request(t) == "negated", t
    for t in ("Should I fix it?", "How would you fix it?", "What would fix it", "fix it?",
              "Is it safe to run the plan?", "can I run the plan", "why not run the plan?"):
        assert fix_it_request(t) in ("question", "negated"), t
    assert fix_it_request("Should I fix it?") == "question"
    assert fix_it_request("what's failing") is None


@needs_field
def test_jarvis_does_not_start_a_run_on_a_negation_or_a_question():
    live, runs, jarvis = field()
    for msg in ("Don't fix it yet", "Should I run the plan?", "how would you fix it?"):
        body = asyncio.run(jarvis.handle(msg))
        assert body["runs"] == [] and runs.list() == [], msg
    body = asyncio.run(jarvis.handle("Should I run the plan?"))
    assert "Cheapest plan" in body["reply"]
    body = asyncio.run(jarvis.handle("Can you fix it?"))
    assert len(body["runs"]) == 1


# ---------- one re-plan in flight per run; shutdown ----------

class SlowJarvis:
    """Claude's re-plan, held until the test opens the gate."""

    def __init__(self) -> None:
        self.gate: asyncio.Event | None = None
        self.calls: list[str] = []
        self.inflight = self.most = 0

    def llm_ready(self) -> bool:
        return True

    async def choose_plan(self, plans, exclude, why):
        self.calls.append(why)
        self.inflight += 1
        self.most = max(self.most, self.inflight)
        try:
            await self.gate.wait()
        finally:
            self.inflight -= 1
        return {"plan": pick_plan(plans, exclude), "source": "claude", "reason": "held"}


async def until_called(slow: SlowJarvis) -> None:
    for _ in range(400):
        if slow.calls:
            return
        await asyncio.sleep(0.025)
    raise AssertionError("the re-plan never reached Jarvis")


def slow_field():
    live, _, _ = field()
    live.listeners.clear()
    slow = SlowJarvis()
    runs = RunManager(live, slow)
    return live, runs, slow


@needs_field
def test_only_one_replan_in_flight_per_run():
    live, runs, slow = slow_field()
    plan = cheapest(runs)

    async def go():
        slow.gate = asyncio.Event()
        run = await runs.execute(plan)
        await runs._problem(run, "TANK_3", "first", exclude=set())
        await until_called(slow)
        await runs._problem(run, "TANK_3", "second", exclude=set())
        await runs._problem(run, "TANK_3", "third", exclude=set())
        assert len(runs._tasks) == 1 and slow.calls == ["First"]
        slow.gate.set()
        await runs.settle()
        return run

    run = asyncio.run(go())
    assert slow.most == 1
    assert slow.calls == ["First", "Third"]          # the newest problem, once
    offers = [r for r in runs.list() if r.get("replaces") == run["id"]]
    assert [o["status"] for o in offers] == ["CANCELLED", "PAUSED"]   # the older offer superseded
    assert run["replan_run_id"] == offers[-1]["id"]


@needs_field
def test_close_cancels_background_replans():
    live, runs, slow = slow_field()
    plan = cheapest(runs)

    async def go():
        slow.gate = asyncio.Event()
        run = await runs.execute(plan)
        await runs._problem(run, "TANK_3", "stuck", exclude=set())
        await until_called(slow)
        (task,) = runs._tasks
        await runs.close()
        assert task.cancelled() and not runs._tasks
        await runs._problem(run, "TANK_3", "again", exclude=set())
        assert not runs._tasks                      # nothing new starts after shutdown
        return run

    run = asyncio.run(go())
    assert run["status"] == "PAUSED" and run["replan_run_id"] is None
    assert slow.calls == ["Stuck"]


# ---------- run alerts resolve when the run ends ----------

def alerts_of(live: LiveField, kind: str) -> list[dict]:
    return [a for a in live.alerts() if a["kind"] == kind]


@needs_field
def test_run_alerts_resolve_when_the_run_ends():
    live, runs, _ = field()
    plan = cheapest(runs)
    sim = live.registry.sim

    async def go():
        run = await runs.execute(plan)
        await advance(live, 6, confirm=False)
        pending = next(s for s in run["steps"] if s["status"] == "PENDING")
        await live.reject(pending["proposal_id"], by="operator")   # pauses the run
        assert run["status"] == "PAUSED"
        (paused,) = alerts_of(live, "RUN_PAUSED")
        assert paused["active"]
        # Tank 3 reaches shutdown while the run is paused: the run fails.
        tank = live.fmap.tanks["TANK_3"]
        sim.level["TANK_3"] = float(tank["capacity_bbl"]) * (float(tank["shutdown_pct"]) + 1) / 100
        await live.tick(0.5)
        assert run["status"] == "FAILED"
        assert not paused["active"] and paused.get("resolved_t")   # RUN_PAUSED went with the run
        (failed,) = alerts_of(live, "RUN_FAILED")
        assert failed["active"] and failed["id"] == run["alert_id"]
        # Back below shutdown: RUN_FAILED resolves too.
        sim.level["TANK_3"] = float(tank["capacity_bbl"]) * 0.5
        await live.tick(0.5)
        return failed

    failed = asyncio.run(go())
    assert not failed["active"] and failed.get("resolved_t")
    sent = [m["alert"] for t, m in live.hub.sent if t == "alert" and m["alert"]["kind"] == "RUN_FAILED"]
    assert sent[-1]["active"] is False


@needs_field
def test_run_failed_alert_resolves_when_the_next_run_starts():
    live, runs, _ = field()
    plan = cheapest(runs)
    sim = live.registry.sim

    async def go():
        run = await runs.execute(plan)
        tank = live.fmap.tanks["TANK_3"]
        sim.level["TANK_3"] = float(tank["capacity_bbl"]) * (float(tank["shutdown_pct"]) + 1) / 100
        await live.tick(0.5)
        assert run["status"] == "FAILED"
        (failed,) = alerts_of(live, "RUN_FAILED")
        assert failed["active"]
        await live.tick(0.5)
        assert failed["active"]                    # still at shutdown
        new = await runs.execute({"id": "plan_manual", "summary": "Truck 2 to Tank 4.", "keeps_running": False,
                                  "actions": [{"type": "SEND_TRUCK", "asset_id": "TRUCK_2",
                                               "target_id": "TANK_4", "at_min": 0}]})
        assert new["status"] == "RUNNING"
        return failed

    failed = asyncio.run(go())
    assert not failed["active"]


@needs_field
def test_run_sources_are_documented_values():
    from backend.app.runs import SOURCES

    live, runs, jarvis = field()
    plan = cheapest(runs)
    xo = next(a for a in plan["actions"] if a["type"] == "SET_VALVE")["asset_id"]
    live.registry.sim.inject({"asset_id": xo, "type": "STUCK_VALVE"})

    async def go():
        run = await runs.execute(plan)
        await advance(live, 60, until=lambda: run["status"] != "RUNNING")

    asyncio.run(go())
    assert {r["source"] for r in runs.list()} == {"console", "replan"}
    assert {r["source"] for r in runs.list()} <= set(SOURCES)
