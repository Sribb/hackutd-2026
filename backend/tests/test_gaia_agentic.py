"""gaia acts on the screen (#171): the ui_action tool turns one answer into several on-screen
steps from the console's whitelist, the persona is gaia, and the selected asset is what
"this", "it" and "that" mean. No network: a fake Anthropic client plays a scripted turn."""
import asyncio
import copy
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app import jarvis as jarvis_mod
from backend.app.forecast import forecast
from backend.app.jarvis import TOOLS, UI_ACTIONS, Jarvis
from backend.app.live import LiveField
from backend.app.planner import make_plans
from backend.app.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "JARVIS_MODE", "JARVIS_CONFIRM", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FREEBOARD_AUDIT_FILE", str(tmp_path / "audit.jsonl"))


def run(coro):
    return asyncio.run(coro)


def tripped_field() -> LiveField:
    sc = copy.deepcopy(load_scenario())
    sc["failures"] = []
    live = LiveField(sc)
    for _ in range(8):
        live.step(0.25)
    live.registry.sim.inject({"asset_id": "PUMP_3", "type": "TRIP", "reason": "test"})
    for _ in range(8):
        live.step(0.25)
    return live


def make(live: LiveField) -> Jarvis:
    return Jarvis(live, forecast_fn=lambda sc, a: forecast(sc, a), plans_fn=lambda sc: make_plans(sc))


def use(i: int, name: str, **inp) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=f"tu{i}", name=name, input=inp)


def ui(i: int, action: str, step: str, **kw) -> SimpleNamespace:
    base = {"action": action, "step": step, "asset_id": None, "ids": None, "direction": None, "clock": None,
            "t_min": None, "panel": None, "actions": None}
    return use(i, "ui_action", **{**base, **kw})


def turn(*blocks, stop="tool_use") -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop, content=list(blocks))


class FakeClient:
    """client.beta.messages.create(...) answers from a script, and keeps every request."""

    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0)


def with_claude(monkeypatch, j: Jarvis, script) -> FakeClient:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    monkeypatch.setenv("JARVIS_MODE", "llm")
    fake = FakeClient(script)
    j._client = fake
    return fake


def test_ui_action_enum_is_the_console_whitelist():
    js = (ROOT / "web/src/console/directives.js").read_text()
    m = re.search(r"const WHITELIST = new Set\(\[(.*?)\]\)", js, re.S)
    assert m, "directives.js WHITELIST not found"
    whitelist = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    assert set(UI_ACTIONS) == whitelist | {"chart"}  # chart: a card the console draws (agent/index.js)
    tool = next(t for t in TOOLS if t["name"] == "ui_action")
    assert tool["input_schema"]["properties"]["action"]["enum"] == UI_ACTIONS
    # Strict tools: every property is required.
    assert set(tool["input_schema"]["required"]) == set(tool["input_schema"]["properties"])


def test_walk_me_through_plays_several_steps_then_a_pending_proposal(monkeypatch):
    live = tripped_field()
    j = make(live)
    before = live.value("TRUCK_1", "phase") if hasattr(live, "value") else None
    fake = with_claude(monkeypatch, j, [
        turn(use(1, "list_alerts")),
        turn(ui(2, "focus", "flying to pump 3", asset_id="PUMP_3"),
             ui(3, "trace_pipes", "tracing pipes to tank 3", asset_id="PUMP_3", direction="downstream"),
             use(4, "get_forecast", actions=[])),
        turn(ui(5, "set_time", "jumping to 2:47 am", clock="2:47 AM"),
             use(6, "get_plans"),
             ui(7, "open_panel", "opening the plans", panel="plans")),
        turn(use(8, "propose_action", type="SEND_TRUCK", asset_id="TRUCK_1", value=None, target_id="TANK_3",
                 reason="Cheapest plan from get_plans.")),
        turn(SimpleNamespace(type="text", text="Pump 3 tripped and tank 3 is filling. The cheapest fix is on "
                                               "the card; hold to confirm."), stop="end_turn"),
    ])
    out = run(j.handle("walk me through what's wrong"))
    assert out["source"] == "claude"
    steps = [d["action"] for d in out["ui"]]
    assert steps[:4] == ["focus", "trace_pipes", "set_time", "open_panel"], steps
    assert out["ui"][0]["step"] == "flying to pump 3" and out["ui"][0]["asset_id"] == "PUMP_3"
    assert out["ui"][1]["args"] == {"direction": "downstream"}
    assert out["ui"][2]["args"] == {"clock": "2:47 AM"}
    assert out["ui"][3]["args"] == {"panel": "plans"}
    assert all(d["type"] == "ui" and d["action"] in UI_ACTIONS for d in out["ui"])
    # The fix is a proposal a person must confirm; nothing moved.
    assert len(out["proposals"]) == 1 and out["proposals"][0]["status"] == "PENDING"
    if before is not None:
        assert live.value("TRUCK_1", "phase") == before
    # Claude was offered the view tool on every round, and every tool result went back.
    assert all(any(t["name"] == "ui_action" for t in c["tools"]) for c in fake.calls)
    assert len(fake.calls) == 5


def test_ui_action_refuses_bad_steps():
    j = make(tripped_field())
    ctx = {"ui": [], "proposals": []}
    assert "error" in run(j.run_tool("ui_action", {"action": "set_valve", "asset_id": "VALVE_XO_34"}, ctx))
    assert "error" in run(j.run_tool("ui_action", {"action": "focus", "asset_id": None}, ctx))
    assert "error" in run(j.run_tool("ui_action", {"action": "focus", "asset_id": "TANK_3; drop"}, ctx))
    assert "error" in run(j.run_tool("ui_action", {"action": "set_time"}, ctx))
    assert "error" in run(j.run_tool("ui_action", {"action": "open_panel", "panel": "settings"}, ctx))
    assert "error" in run(j.run_tool("ui_action", {"action": "try_whatif", "actions": []}, ctx))
    assert ctx["ui"] == []
    for _ in range(jarvis_mod.MAX_UI_STEPS):
        assert run(j.run_tool("ui_action", {"action": "highlight", "ids": ["TANK_3", "PUMP_3"]}, ctx))["ok"]
    assert "error" in run(j.run_tool("ui_action", {"action": "reset_view"}, ctx))
    assert len(ctx["ui"]) == jarvis_mod.MAX_UI_STEPS
    assert ctx["ui"][0]["args"] == {"ids": ["TANK_3", "PUMP_3"], "seconds": 20}


def test_ui_action_takes_sponsor_ids_and_works_in_dataset_mode(monkeypatch):
    """ui_action is not one of eog/gaia.py's tools, so dataset mode leaves it alone, and a
    sponsor id passes through for the browser to check against what's on screen."""
    from backend.app.eog import gaia as eog_gaia
    monkeypatch.setattr(eog_gaia, "active", lambda: True)
    j = make(tripped_field())
    ctx = {"ui": [], "proposals": []}
    out = run(j.run_tool("ui_action", {"action": "focus", "asset_id": "s4_tank_c", "step": "Flying to S4 tank c"}, ctx))
    assert out["ok"] and out["known_asset"] is False
    assert ctx["ui"] == [{"type": "ui", "action": "focus", "source": "gaia", "asset_id": "S4_TANK_C",
                          "step": "flying to s4 tank c"}]


def test_persona_is_gaia_and_shows_rather_than_tells():
    p = make(tripped_field()).system
    assert "gaia" in p
    assert not re.search(r"jarvis|freeboard", p, re.I), "the persona never calls itself Jarvis or Freeboard"
    assert "ui_action" in p and "show, don't tell" in p.lower()
    assert "two short" in p                                    # spoken answers stay short
    assert "never move equipment" in p.lower()
    assert "hold to confirm" in p
    assert "where it came from and how old it is" in p         # every number has a source and age
    assert "\"this\", \"it\" and \"that\" mean the asset selected" in p.lower()
    assert chr(0x2014) not in p and chr(0x2013) not in p


def test_selection_is_what_this_means_for_claude(monkeypatch):
    j = make(tripped_field())
    fake = with_claude(monkeypatch, j, [turn(SimpleNamespace(type="text", text="ok"), stop="end_turn")])
    run(j.handle("what's wrong with this", selected=["s4_tank_c", "S4_TANK_C"]))
    text = fake.calls[0]["messages"][0]["content"][-1]["text"]
    assert "Selected in the 3D view: S4_TANK_C (what 'this', 'it' and 'that' mean)." in text
    assert text.endswith("what's wrong with this")


def test_rules_bind_this_to_the_selection():
    out = run(make(tripped_field()).handle("what's wrong with this?", selected=["TANK_3"]))
    assert out["source"] == "rules"
    assert out["reply"].startswith("Tank 3 is at "), out["reply"]
    assert out["ui"][0] == {"type": "ui", "action": "focus", "asset_id": "TANK_3"}


def test_route_accepts_selected():
    from backend.app.main import app
    client = TestClient(app)
    r = client.post("/api/jarvis", json={"message": "what's wrong with it", "selected": "TANK_3"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "Tank 3" in body["reply"] and body["ui"][0]["asset_id"] == "TANK_3"


def test_chart_step_names_its_asset():
    j = make(tripped_field())
    ctx = {"ui": [], "proposals": []}
    assert "error" in run(j.run_tool("ui_action", {"action": "chart", "asset_id": None}, ctx))
    out = run(j.run_tool("ui_action", {"action": "chart", "asset_id": "TANK_3", "step": "drawing the level"}, ctx))
    assert out["ok"]
    assert ctx["ui"] == [{"type": "ui", "action": "chart", "source": "gaia", "asset_id": "TANK_3", "step": "drawing the level"}]
