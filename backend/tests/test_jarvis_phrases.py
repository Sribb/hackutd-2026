"""Every row of scenario/jarvis_phrases.json (docs/JARVIS_CONSOLE.md section 7, Track A check 1).

Each row runs on a fresh field (the placeholder scenario with no scripted trip), after
its setup's API calls, with no API key: it must be answered by the rules (200, source
"rules", never a 503), with the fixture's directives, proposal, offer, and reply text.
The web's Playwright suite reads the same file.
"""
import copy
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.live_api import install
from backend.app.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "scenario" / "jarvis_phrases.json").read_text())


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "JARVIS_MODE", "JARVIS_CONFIRM", "NTFY_TOPIC",
                "FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN", "FREEBOARD_AUDIT_FILE"):
        monkeypatch.delenv(var, raising=False)


def fresh(setup: str) -> TestClient:
    sc = copy.deepcopy(load_scenario())
    sc["failures"] = []
    app = FastAPI()
    install(app, sc)
    c = TestClient(app)
    for step in FIXTURE["setups"][setup]:
        r = c.post(step["post"], json=step["body"])
        assert r.status_code == 200, (step, r.text)
    return c


def check_row(row: dict, body: dict) -> None:
    label = f"{row['text']!r} -> {body.get('reply')!r}"
    assert body["source"] == "rules", label
    got = body["ui"]
    want = row["ui"]
    assert [d["action"] for d in got] == [w["action"] for w in want], (label, got)
    for d, w in zip(got, want):
        assert d["type"] == "ui" and d["id"].startswith("ui_"), d
        if "asset_id" in w:
            assert d.get("asset_id") == w["asset_id"], (label, d)
        for k, v in (w.get("args") or {}).items():
            assert d["args"].get(k) == v, (label, d)
    if row["proposal"] is None:
        assert body["proposals"] == [], label
    else:
        assert len(body["proposals"]) == 1, (label, body["proposals"])
        p = body["proposals"][0]
        assert p["status"] == "PENDING", label
        assert row["proposal"] == "*" or p["action"]["type"] == row["proposal"], label
    offers = body.get("offers", [])
    if row.get("offer"):
        assert [o["type"] for o in offers] == [row["offer"]], (label, offers)
    else:
        assert offers == [], label
    reply = body["reply"]
    if row.get("reply_starts"):
        assert reply.startswith(row["reply_starts"]), label
    for part in row.get("reply_has", []):
        assert part in reply, (label, part)
    for bad in FIXTURE["never"]:
        assert bad not in reply, (label, bad)


@pytest.mark.parametrize("row", FIXTURE["phrases"], ids=lambda r: f"{r['setup']}:{r['text']}")
def test_phrase(row):
    c = fresh(row["setup"])
    r = c.post("/api/jarvis", json={"message": row["text"], "selected_asset_ids": row.get("selected", [])})
    assert r.status_code == 200, (row["text"], r.status_code, r.text)
    check_row(row, r.json())


def test_fixture_covers_every_route_and_directive():
    rows = FIXTURE["phrases"]
    assert {r["route"] for r in rows} == {"L", "R"}
    seen = {d["action"] for r in rows for d in r["ui"]}
    assert seen >= {"focus", "explode", "collapse", "isolate", "show_sensors", "trace_pipes", "set_time",
                    "reset_view", "try_whatif"}
    assert all(r["setup"] in FIXTURE["setups"] for r in rows)
