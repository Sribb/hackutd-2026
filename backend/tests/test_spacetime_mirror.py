"""SpacetimeDB v2 (#126): the module's new tables and apply_batch, push_batch, and the mirror.

The module tests read backend/spacetime/src/index.ts as text. hackutd-freeboard-live is
published from it, so v2 may only add: a changed table needs --delete-data, and a changed
reducer breaks the consoles that call it. The mirror tests drive a live field by hand
(live.step) with spacetime_sync.call_reducer captured, so nothing touches the network
except one test that runs a fake SpacetimeDB on 127.0.0.1.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app import now, spacetime_sync
from backend.app import spacetime_mirror as sm
from backend.app.live import LiveField
from backend.app.live_api import _raw
from backend.app.scenario import load_scenario, state_payload

SRC = (Path(__file__).resolve().parents[1] / "spacetime" / "src" / "index.ts").read_text(encoding="utf-8")


# ---------- reading the module ----------

def squash(text: str) -> str:
    """Whitespace and trailing commas removed, so formatting never counts as a change."""
    return re.sub(r"\s+", "", text).replace(",}", "}")


def ts_tables() -> dict:
    """{variable: (options, columns)} for every `const x = table(...)` in the module."""
    return {m.group(1): (squash(m.group(2)), squash(m.group(3)))
            for m in re.finditer(r"const (\w+) = table\(\s*(\{.*?\}),\s*(\{.*?\})\s*\);", SRC, re.S)}


def ts_reducers() -> dict:
    """{name: params} for every exported reducer and init; "" when it takes no arguments."""
    out = {}
    for m in re.finditer(r"export const (\w+) = spacetimedb\.(?:reducer|init)\(", SRC):
        rest = SRC[m.end():].lstrip()
        if not rest.startswith("{"):
            out[m.group(1)] = ""
            continue
        depth = 0
        for i, ch in enumerate(rest):
            depth += {"{": 1, "}": -1}.get(ch, 0)
            if depth == 0:
                break
        out[m.group(1)] = squash(rest[:i + 1])
    return out


def ts_schema() -> list:
    """The tables registered with schema({...})."""
    return re.findall(r"\w+", re.search(r"schema\(\{(.*?)\}\)", SRC, re.S).group(1))


def ts_list(name: str) -> tuple:
    m = re.search(rf"const {name} = \[([^\]]*)\];", SRC)
    assert m, f"no const {name} list in index.ts"
    return tuple(re.findall(r"'([^']*)'", m.group(1)))


def ts_regex(name: str) -> str:
    m = re.search(rf"const {name} = /\^(.*?)\$/;", SRC)
    assert m, f"no const {name} pattern in index.ts"
    return m.group(1)


def ts_columns(table: str) -> tuple:
    return tuple(re.findall(r"(\w+):t\.", ts_tables()[table][1]))


# hackutd-freeboard-live runs v1 (c6894b4). None of this may change.
V1_TABLES = {
    "asset_state": ("{name:'asset_state',public:true}",
                    "{asset_id:t.string().primaryKey(),status:t.string(),level_pct:t.f64(),position:t.string(),"
                    "reason:t.string(),updated_at:t.timestamp()}"),
    "field_clock": ("{name:'field_clock',public:true}",
                    "{id:t.u32().primaryKey(),clock:t.string(),incident_root:t.string(),incident_summary:t.string()}"),
    "operator_action": ("{name:'operator_action',public:true}",
                        "{id:t.u64().primaryKey().autoInc(),action_type:t.string(),asset_id:t.string(),"
                        "value:t.string(),target_id:t.string(),at_min:t.u32(),operator:t.identity(),"
                        "created_at:t.timestamp()}"),
    "plan_step": ("{name:'plan_step',public:true}",
                  "{n:t.u32().primaryKey(),plan_id:t.string(),asset_id:t.string(),text:t.string(),done:t.bool()}"),
    "writer": ("{name:'writer'}", "{identity:t.identity().primaryKey()}"),
}
V1_REDUCERS = {
    "init": "",
    "claim_writer": "",
    "add_writer": "{identity:t.identity()}",
    "upsert_asset": "{asset_id:t.string(),status:t.string(),level_pct:t.f64(),position:t.string(),reason:t.string()}",
    "set_clock": "{clock:t.string(),incident_root:t.string(),incident_summary:t.string()}",
    "propose_action": "{action_type:t.string(),asset_id:t.string(),value:t.string(),target_id:t.string(),"
                      "at_min:t.u32()}",
    "remove_action": "{id:t.u64()}",
    "clear_actions": "",
    "set_plan": "{plan_id:t.string(),steps_json:t.string()}",
    "mark_step_done": "{n:t.u32(),done:t.bool()}",
}
V2_TABLES = {
    "alert": ("{name:'alert',public:true}",
              "{id:t.string().primaryKey(),asset_id:t.string(),kind:t.string(),severity:t.string(),"
              "message:t.string(),active:t.bool(),raised_clock:t.string(),resolved_clock:t.string()}"),
    "proposal": ("{name:'proposal',public:true}",
                 "{id:t.string().primaryKey(),type:t.string(),asset_id:t.string(),value:t.string(),"
                 "target_id:t.string(),status:t.string(),source:t.string(),reason:t.string(),"
                 "decided_by:t.string(),result:t.string()}"),
    "connector_status": ("{name:'connector_status',public:true}",
                         "{id:t.string().primaryKey(),kind:t.string(),status:t.string(),detail:t.string(),"
                         "updated_clock:t.string()}"),
}


def test_module_keeps_every_v1_table_column_and_reducer():
    tables, reducers = ts_tables(), ts_reducers()
    for name, want in V1_TABLES.items():
        assert tables.get(name) == want, f"v1 table {name} changed: the live database would need --delete-data"
    for name, want in V1_REDUCERS.items():
        assert reducers.get(name) == want, f"v1 reducer {name} changed or is gone: consoles calling it break"
    assert set(V1_TABLES) <= set(ts_schema())
    assert "const STATUSES = ['OK', 'WARNING', 'ALARM', 'OFFLINE', 'UNKNOWN'];" in SRC


def test_module_adds_the_v2_tables_and_a_writer_only_apply_batch():
    tables = ts_tables()
    for name, want in V2_TABLES.items():
        assert tables.get(name) == want, name
    assert set(V2_TABLES) <= set(ts_schema())
    assert ts_reducers().get("apply_batch") == "{json:t.string()}"
    body = SRC.split("export const apply_batch =", 1)[1].split("=>", 1)[1]
    assert body.lstrip(" {\n").startswith("requireWriter(ctx);")


def test_python_rules_match_the_module():
    assert sm.STATUSES == ts_list("STATUSES")
    assert sm.ACTION_TYPES == ts_list("ACTION_TYPES")
    assert sm.SEVERITIES == ts_list("SEVERITIES")
    assert sm.PROPOSAL_STATUSES == ts_list("PROPOSAL_STATUSES")
    assert sm.CONNECTOR_STATUSES == ts_list("CONNECTOR_STATUSES")
    assert sm.ID.pattern == ts_regex("ID") and sm.LOWER_ID.pattern == ts_regex("LOWER_ID")
    assert sm.TABLES == ts_list("LISTS")
    assert tuple(f"removed_{t}" for t in sm.TABLES) == ts_list("REMOVALS")
    # Every row the mirror builds has exactly the module's columns.
    assert tuple(sm.asset_row("TANK_3", {"status": "OK"})) == ts_columns("asset_state")[:-1]  # updated_at: the module's
    assert tuple(sm.clock_row({})) == ts_columns("field_clock")[1:]                          # id: always 0
    assert tuple(sm.alert_row(ALERT)) == ts_columns("alert")
    assert tuple(sm.proposal_row(PROPOSAL)) == ts_columns("proposal")
    assert tuple(sm.connector_row(CONNECTOR)) == ts_columns("connector_status")


# ---------- the module's checks, ported, so a test can tell what it takes ----------

TS_ID, TS_LOWER = re.compile(ts_regex("ID")), re.compile(ts_regex("LOWER_ID"))


def module_problems(batch) -> list:
    """What apply_batch in index.ts would refuse in `batch`. A port of its checks that uses
    the module's own lists and patterns (REFUSED below is what the real module refused)."""
    def is_id(v, rx=TS_ID) -> bool:
        return isinstance(v, str) and rx.fullmatch(v) is not None

    def texts(r: dict, *keys) -> bool:
        return all(r.get(k) is None or isinstance(r.get(k), str) for k in keys)

    if not isinstance(batch, dict):
        return ["json must be an object"]
    lists, removals = ts_list("LISTS"), ts_list("REMOVALS")
    out = [f"unknown batch key {k}" for k in batch if k not in ("full", "clock", *lists, *removals)]
    full = batch.get("full")
    if full is not None and not isinstance(full, bool):
        out.append("full must be true or false")
    elif full:
        out += [f"a full batch must carry {k}" for k in ("clock", *lists) if batch.get(k) is None]
    for key, rx in zip(removals, (TS_ID, TS_LOWER, TS_LOWER, TS_ID)):
        ids = batch.get(key)
        if ids is not None and not (isinstance(ids, list) and all(is_id(i, rx) for i in ids)):
            out.append(f"bad {key}")
    clock = batch.get("clock")
    if clock is not None and not (isinstance(clock, dict) and texts(clock, "clock", "incident_root", "incident_summary")
                                  and (not clock.get("incident_root") or is_id(clock["incident_root"]))):
        out.append("bad clock")
    checks = {
        "assets": lambda r: (is_id(r.get("asset_id")) and r.get("status") in ts_list("STATUSES")
                             and isinstance(r.get("level_pct"), (int, float))
                             and not isinstance(r.get("level_pct"), bool)
                             and (r["level_pct"] == -1 or 0 <= r["level_pct"] <= 100)
                             and texts(r, "position", "reason")),
        "alerts": lambda r: (is_id(r.get("id"), TS_LOWER) and is_id(r.get("asset_id")) and is_id(r.get("kind"))
                             and r.get("severity") in ts_list("SEVERITIES") and isinstance(r.get("active"), bool)
                             and texts(r, "message", "raised_clock", "resolved_clock")),
        "proposals": lambda r: (is_id(r.get("id"), TS_LOWER) and r.get("type") in ts_list("ACTION_TYPES")
                                and is_id(r.get("asset_id")) and (not r.get("target_id") or is_id(r.get("target_id")))
                                and r.get("status") in ts_list("PROPOSAL_STATUSES")
                                and (not r.get("source") or is_id(r.get("source"), TS_LOWER))
                                and texts(r, "value", "target_id", "source", "reason", "decided_by", "result")),
        "connectors": lambda r: (is_id(r.get("id")) and is_id(r.get("kind"), TS_LOWER)
                                 and r.get("status") in ts_list("CONNECTOR_STATUSES")
                                 and texts(r, "detail", "updated_clock")),
    }
    for key, ok in checks.items():
        rows = batch.get(key)
        if rows is None:
            continue
        if not isinstance(rows, list):
            out.append(f"{key} must be a list")
            continue
        out += [f"bad {key} row {r!r}" for r in rows if not (isinstance(r, dict) and ok(r))]
    return out


GOOD_ASSET = {"asset_id": "TANK_3", "status": "WARNING", "level_pct": 10.0, "position": "", "reason": ""}
ALERT = {"id": "alert_1", "asset_id": "PUMP_3", "kind": "PUMP_TRIP", "severity": "ALARM",
         "message": "Pump 3 tripped: it reports stopped.", "t": "2026-11-07T07:12:03+00:00", "clock": "01:48",
         "active": True}
PROPOSAL = {"id": "act_1", "action": {"type": "CHOKE_WELL", "asset_id": "WELL_9", "value": 0.5},
            "reason": "Tank 3 reaches shutdown at 2:47 AM.", "status": "PENDING",
            "created_t": "2026-11-07T07:12:10+00:00", "text": "Choke Well 9 to 50% of its flow.",
            "source": "jarvis", "connector_id": "CONN_SIM"}
CONNECTOR = {"id": "CONN_SIM", "kind": "sim", "status": "connected", "detail": "simulating 47 assets"}
ALERT_ROW = {"id": "alert_2", "asset_id": "TANK_3", "kind": "TANK_HIGH", "severity": "WARNING", "message": "",
             "active": True, "raised_clock": "01:55", "resolved_clock": ""}
PROPOSAL_ROW = {"id": "act_1", "type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN", "target_id": "",
                "status": "UNCERTAIN", "source": "plan", "reason": "", "decided_by": "phone", "result": ""}

# The real module (published to the scratch database hackutd-freeboard-v2check) refused each
# of these with the message shown, and wrote none of the batch, not even GOOD_ASSET.
REFUSED = [
    ({"assets": [GOOD_ASSET, {"asset_id": "TANK_4", "status": "FINE", "level_pct": 10}]}, "unknown status 'FINE'"),
    ({"assets": [GOOD_ASSET, {"asset_id": "TANK_4", "status": "OK", "level_pct": 150}]},
     "level_pct 150 must be 0-100, or -1 for none"),
    ({"assets": [GOOD_ASSET, {"asset_id": "tank_4", "status": "OK", "level_pct": 1}]},
     "asset_id 'tank_4' is not a contract ID"),
    ({"assets": [GOOD_ASSET], "removed_trucks": ["TRUCK_1"]}, "unknown batch key 'removed_trucks'"),
    ({"alerts": [{**ALERT_ROW, "severity": "INFO"}]}, "unknown severity 'INFO'"),
    ({"alerts": [{**ALERT_ROW, "kind": "tank-high"}]}, "alert kind 'tank-high' is not a contract ID"),
    ({"alerts": [{**ALERT_ROW, "active": "yes"}]}, "alert alert_2 needs active as true or false"),
    ({"proposals": [{**PROPOSAL_ROW, "status": "DONE"}]}, "unknown proposal status 'DONE'"),
    ({"proposals": [{**PROPOSAL_ROW, "type": "OPEN_VALVE"}]}, "unknown action type 'OPEN_VALVE'"),
    ({"proposals": [{**PROPOSAL_ROW, "target_id": "tank 3"}]}, "target_id 'tank 3' is not a contract ID"),
    ({"proposals": [{**PROPOSAL_ROW, "id": "ACT 1"}]}, "proposal id 'ACT 1' must be lowercase letters, digits, and _"),
    ({"connectors": [{"id": "CONN_SIM", "kind": "sim", "status": "up"}]}, "unknown connector status 'up'"),
    ({"connectors": [{"id": "CONN_SIM", "kind": "OPC-UA", "status": "connected"}]},
     "connector kind 'OPC-UA' must be lowercase letters, digits, and _"),
    ({"full": True, "assets": [GOOD_ASSET]}, "a full batch must carry clock, even if empty"),
    ({"full": "yes", "assets": [GOOD_ASSET]}, "full must be true or false"),
    ({"assets": GOOD_ASSET}, "assets must be a list"),
    ({"assets": [GOOD_ASSET], "removed_proposals": ["ACT_1"]},
     "removed_proposals 'ACT_1' must be lowercase letters, digits, and _"),
]


@pytest.mark.parametrize("batch, message", REFUSED, ids=[m for _, m in REFUSED])
def test_the_port_refuses_what_the_module_refused(batch, message):
    assert module_problems(batch), message


def test_the_port_takes_what_the_module_took():
    assert module_problems({"clock": {"clock": "01:55", "incident_root": "PUMP_3", "incident_summary": "x"},
                            "alerts": [ALERT_ROW], "proposals": [PROPOSAL_ROW],
                            "assets": [{**GOOD_ASSET, "status": "ALARM", "level_pct": 86.0}]}) == []
    assert module_problems({"removed_proposals": ["act_2"]}) == []
    assert module_problems({"full": True, "clock": {}, "assets": [], "alerts": [], "proposals": [],
                            "connectors": []}) == []


# ---------- rows ----------

def test_asset_rows_are_never_falsely_safe():
    row = sm.asset_row
    assert row("TANK_3", {"status": "WARNING", "level_pct": 81.234}) == {
        "asset_id": "TANK_3", "status": "WARNING", "level_pct": 81.2, "position": "", "reason": ""}
    # No reading: -1, and the live layer's status (UNKNOWN) stays.
    stale = row("TANK_5", {"status": "UNKNOWN", "level_pct": None, "reason": "No level reading."})
    assert (stale["status"], stale["level_pct"]) == ("UNKNOWN", -1)
    # A level the module can't take is no reading, and the tank is UNKNOWN unless it is in ALARM.
    for bad in (float("nan"), float("inf"), -5, 150, "high", True, [50]):
        r = row("TANK_1", {"status": "OK", "level_pct": bad})
        assert (r["status"], r["level_pct"]) == ("UNKNOWN", -1), bad
        assert "not safe" in r["reason"]
    assert row("TANK_3", {"status": "ALARM", "level_pct": 150})["status"] == "ALARM"
    # Sensor noise just past either end is clamped, not dropped.
    assert row("TANK_1", {"status": "ALARM", "level_pct": 100.3})["level_pct"] == 100.0
    assert row("TANK_1", {"status": "OK", "level_pct": -0.2})["level_pct"] == 0.0
    # A status the module doesn't know is UNKNOWN, never OK.
    for bad in ("FINE", "ok", None, 3):
        assert row("PUMP_3", {"status": bad})["status"] == "UNKNOWN", bad
    assert row("VALVE_XO_34", {"status": "OK", "position": True})["position"] == "OPEN"
    assert row("VALVE_XO_34", {"status": "OK", "position": False})["position"] == "CLOSED"
    assert row("TRUCK_1", {"status": "OK", "position": "YARD_1"})["position"] == "YARD_1"
    assert row("tank_3", {"status": "OK"}) is None and row(7, {"status": "OK"}) is None


def test_alert_proposal_and_connector_rows_follow_the_module_rules():
    assert sm.alert_row(ALERT) == {"id": "alert_1", "asset_id": "PUMP_3", "kind": "PUMP_TRIP", "severity": "ALARM",
                                   "message": "Pump 3 tripped: it reports stopped.", "active": True,
                                   "raised_clock": "01:48", "resolved_clock": ""}
    resolved = sm.alert_row({**ALERT, "active": False}, resolved_clock="01:52")
    assert resolved["active"] is False and resolved["resolved_clock"] == "01:52"
    # Never falsely safe: no `active` is still active, and an unknown severity is ALARM.
    assert sm.alert_row({k: v for k, v in ALERT.items() if k != "active"})["active"] is True
    assert sm.alert_row({**ALERT, "severity": "CRITICAL"})["severity"] == "ALARM"
    assert sm.alert_row({**ALERT, "severity": "warning"})["severity"] == "WARNING"
    # A new detector kind needs no republish; one that can't be made a word is OTHER.
    assert sm.alert_row({**ALERT, "kind": "TANK_HIGH_HIGH"})["kind"] == "TANK_HIGH_HIGH"
    assert sm.alert_row({**ALERT, "kind": "tank-high"})["kind"] == "TANK_HIGH"
    assert sm.alert_row({**ALERT, "kind": None})["kind"] == "OTHER"
    assert sm.alert_row({**ALERT, "id": "ALERT 1"}) is None and sm.alert_row({**ALERT, "asset_id": "pump 3"}) is None

    assert sm.proposal_row(PROPOSAL) == {"id": "act_1", "type": "CHOKE_WELL", "asset_id": "WELL_9", "value": "0.5",
                                         "target_id": "", "status": "PENDING", "source": "jarvis",
                                         "reason": "Tank 3 reaches shutdown at 2:47 AM.", "decided_by": "",
                                         "result": ""}
    truck = {**PROPOSAL, "action": {"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "value": None, "target_id": "TANK_3"}}
    assert (sm.proposal_row(truck)["value"], sm.proposal_row(truck)["target_id"]) == ("", "TANK_3")
    uncertain = sm.proposal_row({**PROPOSAL, "status": "UNCERTAIN", "decided_by": "phone",
                                 "result": "sent; delivery not confirmed - check the field before retrying"})
    assert uncertain["status"] == "UNCERTAIN" and uncertain["decided_by"] == "phone"
    assert sm.proposal_row({**PROPOSAL, "action": {**PROPOSAL["action"], "value": 0.0}})["value"] == "0"
    assert sm.proposal_row({**PROPOSAL, "source": "Some Script"})["source"] == ""
    for bad in ({"status": "EXPIRED"}, {"id": "ACT_1"}, {"action": {"type": "OPEN_VALVE", "asset_id": "VALVE_XO_34"}},
                {"action": {**truck["action"], "target_id": "tank 3"}}, {"action": None}):
        assert sm.proposal_row({**PROPOSAL, **bad}) is None, bad

    assert sm.connector_row(CONNECTOR, "01:47") == {**CONNECTOR, "updated_clock": "01:47"}
    assert sm.connector_row({**CONNECTOR, "status": "up"})["status"] == "unavailable"
    assert sm.connector_row({**CONNECTOR, "kind": "OPC-UA"})["kind"] == "unknown"
    assert sm.connector_row({**CONNECTOR, "kind": "MQTT"})["kind"] == "mqtt"
    assert sm.connector_row({**CONNECTOR, "id": "conn-x"}) is None

    cut = sm.alert_row({**ALERT, "message": "x" * 5000 + "\ud800"})["message"]
    assert len(cut) == sm.MAX_TEXT and cut.endswith("...")
    assert sm.alert_row({**ALERT, "message": "odd \ud800 text"})["message"] == "odd ? text"


# ---------- the mirror ----------

@pytest.fixture
def captured(monkeypatch):
    """SpacetimeDB on, with call_reducer captured: the (reducer, args) calls it got."""
    calls: list = []
    monkeypatch.setattr(spacetime_sync, "_config", lambda: ("http://spacetime.test", "freeboard", "tok"))
    monkeypatch.setattr(spacetime_sync, "_claimed", False)
    monkeypatch.setattr(spacetime_sync, "status", {"enabled": False, "last_push_ok": None, "last_error": None})
    monkeypatch.setattr(spacetime_sync, "call_reducer",
                        lambda reducer, args, timeout=3.0: calls.append((reducer, args)))
    return calls


def batches(calls: list) -> list:
    return [json.loads(args[0]) for reducer, args in calls if reducer == "apply_batch"]


def tick(mirror, app):
    return asyncio.run(mirror.tick(app))


@pytest.fixture
def live_app(monkeypatch):
    """A live field of its own (not main.py's), stepped by hand, behind a stand-in app."""
    for var in ("NTFY_TOPIC", "JARVIS_CONFIRM", "FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FREEBOARD_ONE_CLOCK", "1")
    sc = load_scenario()
    lv = LiveField(sc, _raw(sc))
    return SimpleNamespace(state=SimpleNamespace(live=lv)), lv


class FakeLive:
    """Only what the mirror reads from the live layer."""

    def __init__(self) -> None:
        self.alert_list: list = []
        self.proposals: dict = {}
        self.conns: list = []
        self.registry = SimpleNamespace(status=lambda: [dict(c) for c in self.conns])

    def alerts(self) -> list:
        return [dict(a) for a in self.alert_list]


@pytest.fixture
def fake_app(monkeypatch):
    """A stand-in app: now.state() returns a copy of `state`, and the live layer is a FakeLive."""
    state = {"clock": "01:50", "incident": None,
             "assets": {"TANK_3": {"status": "OK", "level_pct": 50.0}, "PUMP_3": {"status": "OK"}}}
    live = FakeLive()
    monkeypatch.setattr(now, "state", lambda app: copy.deepcopy(state))
    return SimpleNamespace(state=SimpleNamespace(live=live)), state, live


def test_mirror_sends_everything_first_then_only_what_changed(live_app, captured):
    app, lv = live_app
    for _ in range(5):
        lv.step(1.0)  # PUMP_3's scripted trip is confirmed: an ALARM alert
    m = sm.Mirror()
    assert tick(m, app) is True
    assert captured[0] == ("claim_writer", [])
    first, = batches(captured)
    assert first["full"] is True and module_problems(first) == []
    assert {r["asset_id"] for r in first["assets"]} == set(lv.state()["assets"])
    assert any(a["asset_id"] == "PUMP_3" and a["kind"] == "PUMP_TRIP" and a["active"] for a in first["alerts"])
    assert first["connectors"] == [{"id": "CONN_SIM", "kind": "sim", "status": "connected",
                                    "detail": lv.registry.sim.status()["detail"],
                                    "updated_clock": first["clock"]["clock"]}]
    assert first["proposals"] == [] and first["clock"]["incident_root"] == "PUMP_3"
    tank3 = next(r for r in first["assets"] if r["asset_id"] == "TANK_3")
    assert tank3["status"] == "ALARM" and 0 <= tank3["level_pct"] <= 100

    assert tick(m, app) is True and len(batches(captured)) == 1  # nothing changed, nothing sent

    p = asyncio.run(lv.propose({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}, source="jarvis"))
    assert tick(m, app) is True
    assert batches(captured)[-1] == {"proposals": [{
        "id": p["id"], "type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN", "target_id": "",
        "status": "PENDING", "source": "jarvis", "reason": p["reason"], "decided_by": "", "result": ""}]}

    asyncio.run(lv.confirm(p["id"], by="console"))
    lv.step(1.0)
    assert tick(m, app) is True
    third = batches(captured)[-1]
    assert "full" not in third and module_problems(third) == []
    assert (third["proposals"][0]["status"], third["proposals"][0]["decided_by"]) == ("CONFIRMED", "console")
    assert any(r["asset_id"] == "VALVE_XO_34" and r["position"] == "OPEN" for r in third["assets"])
    assert "clock" in third and 0 < len(third["assets"]) < len(first["assets"])

    lv.proposals.pop(p["id"])
    assert tick(m, app) is True
    assert batches(captured)[-1] == {"removed_proposals": [p["id"]]}


def test_gone_rows_are_removed_and_their_clocks_stay_put(fake_app, captured):
    app, state, live = fake_app
    live.alert_list.append(dict(ALERT))  # raised at 01:48
    live.conns.append(dict(CONNECTOR))
    m = sm.Mirror()
    assert tick(m, app) is True and batches(captured)[0]["connectors"][0]["updated_clock"] == "01:50"

    state["clock"] = "01:51"
    live.alert_list[0].update(active=False, resolved_t="2026-11-07T07:20:00+00:00")
    assert tick(m, app) is True
    assert batches(captured)[-1] == {"clock": {"clock": "01:51", "incident_root": "", "incident_summary": ""},
                                     "alerts": [{**sm.alert_row(ALERT), "active": False, "resolved_clock": "01:51"}]}

    state["clock"] = "01:52"  # the resolved clock and the connector's clock don't follow the live clock
    assert tick(m, app) is True and set(batches(captured)[-1]) == {"clock"}

    state["clock"] = "01:53"
    live.conns[0].update(status="unavailable", detail="lost the gateway")
    assert tick(m, app) is True
    assert batches(captured)[-1]["connectors"] == [{"id": "CONN_SIM", "kind": "sim", "status": "unavailable",
                                                     "detail": "lost the gateway", "updated_clock": "01:53"}]

    live.conns.clear()
    live.alert_list.clear()
    del state["assets"]["PUMP_3"]
    assert tick(m, app) is True
    assert batches(captured)[-1] == {"removed_assets": ["PUMP_3"], "removed_alerts": ["alert_1"],
                                     "removed_connectors": ["CONN_SIM"]}


def test_odd_values_never_get_a_batch_refused(fake_app, captured):
    app, state, live = fake_app
    state["incident"] = {"root_asset_id": "pump 3", "summary": None}
    state["assets"].update({
        "TANK_1": {"status": "FINE", "level_pct": float("nan"), "reason": "odd \ud800"},
        "TANK_2": {"status": "WARNING", "level_pct": 101.7},
        "VALVE_D_1": {"status": "OK", "position": False},
        "tank_9": {"status": "OK"},  # an ID the module refuses: left out
    })
    live.alert_list += [{**ALERT, "severity": "INFO", "kind": "odd kind!"}, {**ALERT, "id": "ALERT 2"}]
    live.proposals.update({"act_1": PROPOSAL, "act_2": {**PROPOSAL, "id": "act_2", "status": "EXPIRED"}})
    live.conns += [{**CONNECTOR, "kind": "OPC-UA", "status": None, "detail": 42}, {**CONNECTOR, "id": None}]
    assert tick(sm.Mirror(), app) is True
    batch, = batches(captured)
    assert module_problems(batch) == []
    assets = {r["asset_id"]: r for r in batch["assets"]}
    assert "tank_9" not in assets
    assert (assets["TANK_1"]["status"], assets["TANK_1"]["level_pct"]) == ("UNKNOWN", -1)
    assert (assets["TANK_2"]["status"], assets["TANK_2"]["level_pct"]) == ("UNKNOWN", -1)
    assert assets["VALVE_D_1"]["position"] == "CLOSED"
    assert [(a["id"], a["severity"]) for a in batch["alerts"]] == [("alert_1", "ALARM")]
    assert [p["id"] for p in batch["proposals"]] == ["act_1"]
    assert batch["connectors"] == [{"id": "CONN_SIM", "kind": "unknown", "status": "unavailable", "detail": "42",
                                    "updated_clock": "01:50"}]
    assert batch["clock"] == {"clock": "01:50", "incident_root": "", "incident_summary": ""}


def test_without_a_live_layer_its_tables_are_emptied(fake_app, captured):
    app, _, _ = fake_app
    app.state.live = None
    assert tick(sm.Mirror(), app) is True
    batch, = batches(captured)
    assert batch["full"] is True and (batch["alerts"], batch["proposals"], batch["connectors"]) == ([], [], [])
    assert module_problems(batch) == []


def test_a_failed_push_keeps_its_changes_for_the_next_try(fake_app, captured, monkeypatch):
    app, state, _ = fake_app
    down = {"on": True}

    def call(reducer, args, timeout=3.0):
        if reducer == "apply_batch" and down["on"]:
            raise urllib.error.URLError("SpacetimeDB is down")
        captured.append((reducer, args))

    monkeypatch.setattr(spacetime_sync, "call_reducer", call)
    m = sm.Mirror()
    assert tick(m, app) is False and m.sent is None
    assert spacetime_sync.status["last_push_ok"] is False and "down" in spacetime_sync.status["last_error"]
    assert tick(m, app) is None  # waiting out the failure instead of retrying at once
    down["on"], m.retry_at = False, 0.0
    assert tick(m, app) is True and batches(captured)[-1]["full"] is True

    state["assets"]["TANK_3"]["level_pct"] = 55.0
    down["on"] = True
    assert tick(m, app) is False
    state["assets"]["PUMP_3"]["status"] = "ALARM"
    down["on"], m.retry_at = False, 0.0
    assert tick(m, app) is True
    last = batches(captured)[-1]
    assert "full" not in last and {r["asset_id"] for r in last["assets"]} == {"TANK_3", "PUMP_3"}


def test_a_push_state_from_elsewhere_makes_the_next_batch_full(fake_app, captured):
    app, _, _ = fake_app
    m = sm.Mirror()
    assert tick(m, app) is True and tick(m, app) is True and len(batches(captured)) == 1
    # Startup, a blueprint activation, or POST /api/spacetime/sync wrote asset rows on its own.
    assert spacetime_sync.push_state(state_payload(load_scenario()))
    assert tick(m, app) is True and batches(captured)[-1]["full"] is True
    assert tick(m, app) is True and len(batches(captured)) == 2  # and only once


def test_push_state_counts_even_when_it_fails(monkeypatch):
    monkeypatch.setattr(spacetime_sync, "_config", lambda: ("http://spacetime.test", "freeboard", None))
    monkeypatch.setattr(spacetime_sync, "_claimed", True)
    monkeypatch.setattr(spacetime_sync, "status", {"enabled": False, "last_push_ok": None, "last_error": None})

    def down(reducer, args, timeout=3.0):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(spacetime_sync, "call_reducer", down)
    before = spacetime_sync.state_pushes
    assert spacetime_sync.push_state(state_payload(load_scenario())) is False
    assert spacetime_sync.state_pushes == before + 1


def test_a_full_batch_goes_every_resync_s(fake_app, captured):
    app, _, _ = fake_app
    m = sm.Mirror()
    assert tick(m, app) is True and tick(m, app) is True and len(batches(captured)) == 1
    m.last_full -= m.resync_s
    assert tick(m, app) is True and batches(captured)[-1]["full"] is True


def test_the_mirror_is_off_without_spacetime_db(monkeypatch):
    monkeypatch.delenv("SPACETIME_DB", raising=False)
    calls: list = []
    monkeypatch.setattr(spacetime_sync, "call_reducer", lambda *a, **k: calls.append(a))

    def never(app):
        raise AssertionError("read the field while SpacetimeDB is off")

    monkeypatch.setattr(now, "state", never)
    assert tick(sm.Mirror(), SimpleNamespace(state=SimpleNamespace())) is None and calls == []


def test_run_keeps_going_through_failures_and_stops_when_cancelled(fake_app, captured, monkeypatch):
    app, _, _ = fake_app
    reads = {"n": 0}
    fake_state = now.state

    def flaky_state(a):
        reads["n"] += 1
        if reads["n"] == 1:
            raise RuntimeError("half-built field")
        return fake_state(a)

    attempts: list = []

    def flaky_call(reducer, args, timeout=3.0):
        attempts.append(reducer)
        if reducer == "apply_batch" and attempts.count("apply_batch") <= 2:
            raise urllib.error.URLError("SpacetimeDB is down")
        captured.append((reducer, args))

    monkeypatch.setattr(now, "state", flaky_state)
    monkeypatch.setattr(spacetime_sync, "call_reducer", flaky_call)

    async def main():
        task = asyncio.create_task(sm.run(app, 0.01))
        deadline = time.monotonic() + 10
        while not batches(captured) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert reads["n"] >= 4 and attempts.count("apply_batch") == 3
    assert batches(captured)[0]["full"] is True


# ---------- push_batch ----------

def test_push_batch_claims_the_writer_once_and_sends_one_compact_apply_batch(captured):
    payload = {"clock": {"clock": "01:52", "incident_root": "PUMP_3", "incident_summary": ""},
               "removed_alerts": ["alert_3"]}
    assert spacetime_sync.push_batch(payload) is True
    assert spacetime_sync.push_batch(payload) is True
    assert [r for r, _ in captured] == ["claim_writer", "apply_batch", "apply_batch"]
    args = captured[1][1]
    assert len(args) == 1 and json.loads(args[0]) == payload and " " not in args[0]
    assert spacetime_sync.status == {"enabled": True, "last_push_ok": True, "last_error": None}


def test_push_batch_never_sends_nan_and_is_off_without_spacetime_db(captured, monkeypatch):
    nan = {"assets": [{"asset_id": "TANK_1", "status": "OK", "level_pct": float("nan")}]}
    assert spacetime_sync.push_batch(nan) is False
    assert "not valid JSON" in spacetime_sync.status["last_error"] and captured == []
    monkeypatch.setattr(spacetime_sync, "_config", lambda: None)
    assert spacetime_sync.push_batch({}) is False and spacetime_sync.status["enabled"] is False


def test_push_batch_over_http_sends_the_token_and_never_shows_it(monkeypatch, caplog):
    token = "sentinel-token-not-a-real-secret"
    seen: list = []
    replies = [(200, b""), (200, b""), (530, b"unknown status 'FINE'")]

    class FakeSpacetime(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append((self.path, self.headers.get("Authorization"), body))
            code, text = replies.pop(0) if replies else (500, b"unexpected call")
            self.send_response(code)
            self.send_header("Content-Length", str(len(text)))
            self.end_headers()
            self.wfile.write(text)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSpacetime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("SPACETIME_URL", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setenv("SPACETIME_DB", "freeboard-test")
        monkeypatch.setenv("SPACETIME_TOKEN", token)
        monkeypatch.setattr(spacetime_sync, "_claimed", False)
        monkeypatch.setattr(spacetime_sync, "status", {"enabled": False, "last_push_ok": None, "last_error": None})
        caplog.set_level(logging.DEBUG, logger="freeboard.spacetime")
        payload = {"clock": {"clock": "01:52", "incident_root": "PUMP_3", "incident_summary": "Pump 3 failed."}}
        assert spacetime_sync.push_batch(payload) is True
        assert [path for path, _, _ in seen] == ["/v1/database/freeboard-test/call/claim_writer",
                                                 "/v1/database/freeboard-test/call/apply_batch"]
        assert all(auth == f"Bearer {token}" for _, auth, _ in seen)
        args = json.loads(seen[1][2])
        assert len(args) == 1 and json.loads(args[0]) == payload
        assert spacetime_sync.push_batch(payload) is False  # the module refused this one
        assert spacetime_sync.status["last_error"] == "HTTP 530: unknown status 'FINE'"
        assert token not in json.dumps(spacetime_sync.status) and token not in caplog.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------- round 1 review fixes ----------

def test_a_stale_asset_never_shows_its_last_level_or_position(live_app, captured):
    app, lv = live_app
    lv.step(1.0)
    # VALVE_D_6 and TANK_1 stop reporting: the live layer calls them stale (UNKNOWN), and
    # /api/state carries the valve's last position only as last_position.
    lv.detector.stale_keys.update(k for k in lv.latest if k[0] in ("VALVE_D_6", "TANK_1"))
    st = now.state(app)
    v6 = st["assets"]["VALVE_D_6"]
    assert v6["stale"] is True and v6["position"] is None and v6["last_position"] in ("OPEN", "CLOSED")
    assert tick(sm.Mirror(), app) is True
    rows = {r["asset_id"]: r for r in batches(captured)[0]["assets"]}
    assert (rows["VALVE_D_6"]["status"], rows["VALVE_D_6"]["position"]) == ("UNKNOWN", "")
    assert "not safe" in rows["VALVE_D_6"]["reason"]
    assert (rows["TANK_1"]["status"], rows["TANK_1"]["level_pct"]) == ("UNKNOWN", -1)
    # Even when a stale entry still says OK and carries numbers, it goes as unknown.
    row = sm.asset_row("TANK_2", {"status": "OK", "level_pct": 40.0, "position": "OPEN", "stale": True})
    assert (row["status"], row["level_pct"], row["position"]) == ("UNKNOWN", -1, "")
    assert sm.asset_row("PUMP_3", {"status": "ALARM", "stale": True})["status"] == "ALARM"
    assert module_problems({"assets": [row]}) == []


def test_a_reused_alert_id_gets_its_own_resolved_clock(fake_app, captured):
    app, state, live = fake_app
    live.alert_list.append({**ALERT, "active": False})
    m = sm.Mirror()
    assert tick(m, app) is True and batches(captured)[-1]["alerts"][0]["resolved_clock"] == "01:50"
    # A new live field (blueprint activation) counts from alert_1 again: a new, active alert.
    state["clock"] = "02:10"
    live.alert_list[:] = [{**ALERT, "kind": "TANK_HIGH", "asset_id": "TANK_3", "clock": "02:10"}]
    assert tick(m, app) is True and batches(captured)[-1]["alerts"][0]["active"] is True
    state["clock"] = "02:20"
    live.alert_list[0]["active"] = False
    assert tick(m, app) is True
    assert batches(captured)[-1]["alerts"][0]["resolved_clock"] == "02:20"


def test_a_claim_that_got_no_answer_is_tried_again(captured, monkeypatch):
    up = {"on": False}

    def call(reducer, args, timeout=3.0):
        if not up["on"]:
            raise urllib.error.URLError("SpacetimeDB is down")
        captured.append((reducer, args))

    monkeypatch.setattr(spacetime_sync, "call_reducer", call)
    assert spacetime_sync.push_batch({"removed_alerts": ["alert_1"]}) is False
    up["on"] = True  # SpacetimeDB comes up (a fresh publish: nobody holds the writer role yet)
    assert spacetime_sync.push_batch({"removed_alerts": ["alert_1"]}) is True
    assert [r for r, _ in captured] == ["claim_writer", "apply_batch"]

    # The module answering "taken" counts as claimed: the push itself tells the rest.
    def taken(reducer, args, timeout=3.0):
        captured.append((reducer, args))
        if reducer == "claim_writer":
            raise urllib.error.HTTPError("http://spacetime.test", 530, "x", {}, None)

    monkeypatch.setattr(spacetime_sync, "_claimed", False)
    monkeypatch.setattr(spacetime_sync, "call_reducer", taken)
    captured.clear()
    assert spacetime_sync.push_batch({}) is True and spacetime_sync.push_batch({}) is True
    assert [r for r, _ in captured] == ["claim_writer", "apply_batch", "apply_batch"]
