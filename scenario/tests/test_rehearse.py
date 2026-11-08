"""Tests for scenario/rehearse.py against a fake backend served by uvicorn on a free port.

    python -m pytest -q scenario/tests

The fake speaks the docs/CONTRACT.md shapes with fixed numbers. Each test turns
parts of it on or off to drive the PASS, FAIL, and SKIP paths.
"""
from __future__ import annotations

import asyncio
import importlib.util
import socket
import threading
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("websockets")

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("rehearse", HERE.parent / "rehearse.py")
rehearse = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rehearse)

BASE_EVENT = {"type": "SHUTDOWN_LEVEL", "asset_id": "TANK_3", "t_min": 60, "t_min_low": 52, "t_min_high": 71,
              "text": "Tank 3 reaches shutdown at 2:47 AM, give or take 11 minutes. 4 wells go down with it."}
XO_EVENT = {"type": "SHUTDOWN_LEVEL", "asset_id": "TANK_3", "t_min": 215, "t_min_low": 144, "t_min_high": None,
            "text": "Tank 3 reaches shutdown at 5:23 AM, give or take up to 72 minutes earlier or much later."}
TANK6_EVENT = {"type": "SHUTDOWN_LEVEL", "asset_id": "TANK_6", "t_min": 63, "t_min_low": 55, "t_min_high": 74,
               "text": "Tank 6 reaches shutdown at 2:03 AM, give or take 11 minutes."}
ROBUST = {"id": "plan_b", "robust": True, "keeps_running": True, "cannot_save": [],
          "summary": "Open the Tank 3 to 4 crossover and shut in Well 11. Every tank stays below shutdown through 7:47 AM.",
          "actions": [{"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN", "at_min": 20},
                      {"type": "CHOKE_WELL", "asset_id": "WELL_11", "value": 0, "at_min": 2}],
          "cost": {"total_usd": 1798}}
CHEAP = {"id": "plan_a", "robust": False, "keeps_running": True, "cannot_save": [],
         "summary": "Open the Tank 3 to 4 crossover. Every tank stays below shutdown. Not enough if inflow runs 15% high.",
         "actions": [{"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN", "at_min": 20}],
         "cost": {"total_usd": 664}}


def flood_alerts(mode: str) -> list[dict]:
    """The alarms the Pump 3 trip sets off once Tank 3 has shut in, CF-7 shaped.
    cf7: grouped correctly (the high-high points at the high, a chain). ungrouped: Tank 3
    high has no root_id. two_p1: Tank 3 high-high is its own P1 root. thin: only the trip
    and the stale meter, as PR #57's detector rings today."""
    def a(n, asset, kind, prio, root):
        return {"id": f"alert_{n}", "asset_id": asset, "kind": kind, "severity": "ALARM", "active": True,
                "priority": prio, "root_id": root, "message": f"{asset} {kind}"}
    out = [a(2, "WELL_22", "SENSOR_STALE", "P3", None)]
    if mode == "thin":
        return out
    out += [a(3, "TANK_3", "TANK_HIGH", "P2", None) if mode == "ungrouped" else
            a(3, "TANK_3", "TANK_HIGH", "P1", "alert_1"),
            a(4, "TANK_3", "TANK_HIGH_HIGH", "P1", None if mode == "two_p1" else "alert_3")]
    out += [a(5 + i, f"WELL_{9 + i}", "WELL_SHUT_IN", "P2", "alert_1") for i in range(4)]
    return out


def make_app(live=True, jarvis="good", lastcall=True, ingest=True, blueprints=True,
             robust=True, live_follows=False, flood=None) -> FastAPI:
    """flood None: /api/alerts as PR #57 has it, with no priority or root_id (before CF-7).
    Otherwise one of flood_alerts' modes, rung once the fake's live clock passes 60 min."""
    app = FastAPI()
    trip = {"id": "alert_1", "asset_id": "PUMP_3", "kind": "PUMP_TRIP", "active": True,
            "message": "Pump 3 tripped: it reports stopped."}
    if flood:
        trip.update(priority="P1", root_id=None, minutes_to_consequence=60, usd_at_risk=7250)
    st = {"active": "default", "xo": "CLOSED", "tripped": True, "proposals": {}, "n": 0, "ws": [],
          "t": 6.0, "advances": 0, "alerts": [trip]}
    app.state.fake = st

    def tanks():
        n = 8 if st["active"] != "default" else 6
        return [{"id": f"TANK_{i}", "capacity_bbl": 500, "shutdown_pct": 90} for i in range(1, n + 1)]

    def tank3_pct() -> float:
        return min(90.0, 71.2 + 0.3 * st["t"])

    @app.get("/api/state")
    def state():
        return {"clock": "01:47", "assets": {"PUMP_3": {"status": "ALARM"}, "TANK_3": {"status": "ALARM"}},
                "incident": {"root_asset_id": "PUMP_3", "summary": "Pump 3 failed. Tank 3 is filling."}}

    @app.get("/api/field")
    def field():
        return {"tanks": tanks(), "pumps": [{"id": "PUMP_3", "tank_id": "TANK_3"}],
                "valves": [{"id": "VALVE_XO_34", "kind": "CROSSOVER", "connects": ["TANK_3", "TANK_4"], "manual": True}],
                "lead_min": {"SET_VALVE_manual": 20, "SET_VALVE_remote": 1}}

    @app.get("/api/health")
    def health():
        return {"ok": True, "lastcall": "mounted" if lastcall else "not installed"}

    @app.post("/api/forecast")
    async def forecast(request: Request):
        body = await request.json()
        if st["active"] != "default":
            return {"events": [TANK6_EVENT], "cost": {"total_usd": 3000}, "warnings": []}
        xo = any(a.get("asset_id") == "VALVE_XO_34" for a in body.get("actions", []))
        timeline = [{"t_min": 0, "wells_online": 24}, {"t_min": 360, "wells_online": 24 if xo else 20}]
        return {"events": [XO_EVENT if xo else BASE_EVENT], "cost": {"total_usd": 4520 if xo else 7250},
                "timeline": timeline, "warnings": []}

    @app.post("/api/plans")
    def plans():
        if st["active"] != "default":
            return {"plans": [{**ROBUST, "summary": "Open the Tank 5 to 6 crossover.", "cost": {"total_usd": 287}}]}
        return {"plans": [CHEAP, ROBUST] if robust else [CHEAP]}

    if lastcall:
        @app.post("/api/lastcall")
        def lc():
            def opt(p, exp, hot):
                return {"plan_id": p["id"], "summary": p["summary"], "cost": p["cost"], "robust": p["robust"],
                        "expires_clock": exp, "expires_clock_hot": hot,
                        "steps": [{"type": a["type"], "asset_id": a["asset_id"]} for a in p["actions"]]}
            return {"headline": "Your cheapest plan expires at 2:25 AM.",
                    "point_of_no_return": {"clock": "2:43 AM"},
                    "options": [opt(CHEAP, "2:25 AM", None), opt(ROBUST, "2:30 AM", "2:23 AM")]}

    if live:
        @app.get("/api/live/state")
        def live_state():
            ids = [t["id"] for t in tanks()] if live_follows else [f"TANK_{i}" for i in range(1, 7)]
            assets = {i: {"status": "OK", "level_pct": 50} for i in ids}
            assets["TANK_3"] = {"status": "ALARM", "level_pct": tank3_pct()}
            assets["VALVE_XO_34"] = {"status": "OK", "position": st["xo"]}
            return {"clock": "01:50", "t": 6, "paused": False, "assets": assets}

        @app.get("/api/alerts")
        def alerts():
            return {"alerts": st["alerts"]}

        @app.post("/api/live/sim")
        async def sim(request: Request):
            body = await request.json()
            if body.get("advance_min"):
                st["t"] += float(body["advance_min"])
                st["advances"] += 1
                if flood and st["t"] >= 60 and len(st["alerts"]) == 1:
                    st["alerts"] += flood_alerts(flood)
            h, m = divmod(int(107 + st["t"]), 60)
            return {"clock": f"{h:02d}:{m:02d}"}

        async def broadcast(msg: dict) -> None:
            for ws in list(st["ws"]):
                try:
                    await ws.send_json(msg)
                except Exception:  # noqa: BLE001
                    st["ws"].remove(ws)

        @app.post("/api/live/faults")
        async def faults(request: Request):
            body = await request.json()
            if body["type"] == "REPAIR":
                st["alerts"][0]["active"] = False
            elif body["type"] == "TRIP":
                async def later():
                    await asyncio.sleep(0.2)
                    st["alerts"][0]["active"] = True
                    await broadcast({"type": "alert", "seq": 9, "alert": st["alerts"][0]})
                asyncio.get_running_loop().create_task(later())
            return {"ok": True}

        def new_prop(action: dict) -> dict:
            st["n"] += 1
            p = {"id": f"act_{st['n']}", "action": action, "status": "PENDING",
                 "text": f"{action['value'].title()} the Tank 3 to 4 crossover."}
            st["proposals"][p["id"]] = p
            return p

        @app.post("/api/jarvis")
        async def ask_jarvis(request: Request):
            msg = (await request.json())["message"]
            if jarvis == "503":
                raise HTTPException(503, "Jarvis needs ANTHROPIC_API_KEY on the server.")
            if msg.startswith("open"):
                p = new_prop({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"})
                return {"reply": "Proposed: Open the Tank 3 to 4 crossover.", "source": "rules", "ui": [],
                        "proposals": [p]}
            if msg.startswith("Why"):
                tail = " Cheapest plan, $664: open the crossover." if jarvis == "good" else ""
                return {"reply": "Tank 3 reaches shutdown at 2:47 AM." + tail, "source": "rules", "ui": [],
                        "proposals": []}
            return {"reply": "Pump 3 tripped: it reports stopped.", "source": "rules", "ui": [], "proposals": []}

        @app.post("/api/actions")
        async def actions(request: Request):
            b = await request.json()
            return new_prop({"type": b["type"], "asset_id": b["asset_id"], "value": b["value"]})

        @app.post("/api/actions/{pid}/confirm")
        def confirm(pid: str):
            p = st["proposals"][pid]
            p["status"] = "CONFIRMED"
            st["xo"] = p["action"]["value"]
            return p

        @app.post("/api/actions/{pid}/reject")
        def reject(pid: str):
            st["proposals"][pid]["status"] = "REJECTED"
            return st["proposals"][pid]

        @app.websocket("/ws")
        async def ws(websocket: WebSocket):
            await websocket.accept()
            st["ws"].append(websocket)
            await websocket.send_json({"type": "state", "seq": 1, "state": {}})
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                if websocket in st["ws"]:
                    st["ws"].remove(websocket)

    if ingest:
        @app.post("/api/ingest")
        def ing():
            return {"ok": True, "errors": [], "review": [{"id": f"X{i}"} for i in range(14)],
                    "counts": {"tanks": 4, "wells": 20}, "source": {"read_by": "parser"}}

    if blueprints:
        @app.get("/api/blueprints")
        def bps():
            return {"active": st["active"], "blueprints": []}

        @app.post("/api/blueprints")
        def post_bp():
            st["active"] = "bp_1"
            return {"id": "bp_1", "status": "fallback", "ok": True, "glb_url": "/api/blueprints/bp_1/field.glb"}

        @app.post("/api/blueprints/{bid}/activate")
        def activate(bid: str):
            st["active"] = bid
            return {"id": bid, "active": True}

    return app


class Server:
    def __init__(self, app: FastAPI) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.app = app
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning",
                                                    ws="websockets"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "Server":
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def run(app: FastAPI, **kw) -> dict:
    with Server(app) as srv:
        return rehearse.Rehearsal(srv.url, **kw).run()


def by_beat(out: dict, prefix: str) -> dict:
    hits = [r for r in out["results"] if r["beat"].startswith(prefix)]
    assert hits, f"no row starting {prefix!r} in {[r['beat'] for r in out['results']]}"
    return hits[0]


def test_everything_passes_and_fills_every_live_value():
    app = make_app(live_follows=True, flood="cf7")
    out = run(app)
    bad = [r for r in out["results"] if r["result"] != "PASS"]
    assert not bad, bad
    assert out["ok"]
    assert out["live"] == {
        rehearse.LIVE_EVENT: BASE_EVENT["text"],
        rehearse.LIVE_BEFORE_AFTER: "Tank 3 shutdown moves from 2:47 AM to 5:23 AM, and the bill from $7,250 to $4,520",
        rehearse.LIVE_ROBUST: "Open the Tank 3 to 4 crossover and shut in Well 11, for $1,798",
        rehearse.LIVE_NOTHING: "$7,250",
        rehearse.LIVE_EXPIRY: "2:30 AM (2:23 AM if inflow runs 15% high)",
        rehearse.LIVE_TWO_NIGHTS: "$2,730 and 4 wells apart",
        rehearse.LIVE_FLOOD: "8",
    }
    flood = by_beat(out, "Beat 2b The flood")["detail"]
    assert "P1 root alert_1 PUMP_3 PUMP_TRIP P1, 6 grouped under it" in flood
    assert "stale sensor on its own: WELL_22" in flood
    assert any("whole flood rang" in c for c in out["server_changes"])
    assert "60 min to consequence" in out["extra"]["P1 before the flood"]
    assert out["unfilled"] == []
    st = app.state.fake
    assert st["active"] == "default"            # the blueprint was put back
    assert st["xo"] == "CLOSED"                 # the crossover was closed again
    assert st["alerts"][0]["active"]            # PUMP_3 is left tripped, as the demo starts
    assert all(p["status"] != "PENDING" for p in st["proposals"].values())
    assert any("Tripped PUMP_3" in c for c in out["server_changes"])
    assert "s from the trip to the alert" in by_beat(out, "Beat 2 What's failing: trip")["detail"]


def test_bare_backend_skips_live_and_blueprints_without_crashing():
    out = run(make_app(live=False, ingest=False, blueprints=False, lastcall=False))
    assert by_beat(out, "Live layer")["result"] == "SKIP"
    assert by_beat(out, "Beat 1 Upload")["result"] == "SKIP"
    assert by_beat(out, "Beat 1 Build")["result"] == "SKIP"
    assert by_beat(out, "Beat 3")["result"] == "PASS"
    lc = by_beat(out, "Beat 6 Last Call")
    assert lc["result"] == "FAIL" and "not mounted" in lc["detail"]
    assert rehearse.LIVE_EXPIRY in out["unfilled"]
    assert not any(r["detail"].startswith("rehearse.py crashed") for r in out["results"])


def test_skip_live_flag():
    app = make_app()
    out = run(app, skip_live=True)
    assert by_beat(out, "Live layer")["result"] == "SKIP"
    assert app.state.fake["proposals"] == {}    # nothing proposed, nothing faulted


def test_jarvis_503_is_a_fail_with_the_reply_quoted_and_confirm_still_checked():
    app = make_app(jarvis="503")
    out = run(app)
    for prefix in ("Beat 2 Jarvis", "Beat 4 Jarvis", "Beat 6 Jarvis"):
        row = by_beat(out, prefix)
        assert row["result"] == "FAIL"
        assert "HTTP 503" in row["detail"] and "ANTHROPIC_API_KEY" in row["detail"]
    confirm = by_beat(out, "Beat 6 Confirm")
    assert confirm["result"] == "PASS" and "console proposal" in confirm["detail"]
    assert app.state.fake["xo"] == "CLOSED"
    assert not out["ok"]


def test_jarvis_answer_without_the_plan_cost_fails():
    out = run(make_app(jarvis="no-cost"))
    row = by_beat(out, "Beat 4 Jarvis")
    assert row["result"] == "FAIL"
    assert "the plan cost" in row["detail"] and "2:47 AM" in row["detail"]


def test_no_robust_plan_fails_and_live_layer_not_following_the_upload_fails():
    out = run(make_app(robust=False))
    assert by_beat(out, "Beat 6 The plan")["result"] == "FAIL"
    assert rehearse.LIVE_ROBUST not in out["live"]
    follow = by_beat(out, "Beat 1 to 2")
    assert follow["result"] == "FAIL" and "TANK_7" in follow["detail"]


def test_flood_skips_before_cf7_and_leaves_the_clock_alone():
    app = make_app()                                # PR #57 alerts: no priority, no root_id
    out = run(app)
    row = by_beat(out, "Beat 2b The flood")
    assert row["result"] == "SKIP" and "CF-7" in row["detail"]
    assert not any("whole flood" in c for c in out["server_changes"])
    assert app.state.fake["t"] < 60                 # only the trip timing's 12-minute advance
    assert rehearse.LIVE_FLOOD in out["unfilled"]
    assert any("cut that beat" in n for n in out["notes"])


@pytest.mark.parametrize("mode, want", [
    ("ungrouped", "don't point at alert_1 by root_id: alert_3 TANK_3 TANK_HIGH"),
    ("two_p1", "2 P1 root causes, expected exactly one"),
    ("thin", "only 2 active alarms; the beat needs 8 or more"),
])
def test_flood_that_does_not_collapse_to_one_cause_fails(mode, want):
    out = run(make_app(flood=mode))
    row = by_beat(out, "Beat 2b The flood")
    assert row["result"] == "FAIL", row
    assert want in row["detail"], row["detail"]
    assert not out["ok"]


def test_fill_replaces_only_known_placeholders():
    text = "Say {LIVE: do-nothing cost}. Keep {LIVE: /api/lastcall expiry}. Prose: fill each `{LIVE: ...}`."
    assert rehearse.placeholders(text) == ["do-nothing cost", "/api/lastcall expiry"]
    out = rehearse.fill(text, {"do-nothing cost": "$7,250"})
    assert out == "Say $7,250. Keep {LIVE: /api/lastcall expiry}. Prose: fill each `{LIVE: ...}`."


def test_every_placeholder_in_demo_md_is_one_rehearse_fills():
    keys = rehearse.placeholders(rehearse.DEMO.read_text(encoding="utf-8"))
    known = {rehearse.LIVE_EVENT, rehearse.LIVE_BEFORE_AFTER, rehearse.LIVE_ROBUST, rehearse.LIVE_NOTHING,
             rehearse.LIVE_EXPIRY, rehearse.LIVE_FLOOD, rehearse.LIVE_TWO_NIGHTS}
    assert set(keys) <= known, set(keys) - known


def test_main_writes_the_filled_file_and_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rehearse, "FILLED", tmp_path / "DEMO.filled.md")
    monkeypatch.setattr(rehearse, "ROOT", tmp_path.parent)
    with Server(make_app(live_follows=True)) as srv:
        assert rehearse.main(["--api", srv.url, "--fill", "--json"]) == 0
    filled = (tmp_path / "DEMO.filled.md").read_text(encoding="utf-8")
    assert "$1,798" in filled and "{LIVE: /api/plans" not in filled
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    assert rehearse.main(["--api", f"http://127.0.0.1:{dead}", "--json"]) == 2
