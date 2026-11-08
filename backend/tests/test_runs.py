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
