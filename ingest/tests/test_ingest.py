"""Offline tests: no network. Claude is replaced by a fake client that returns a fixed reply."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ingest import IngestError, ingest_bytes
from ingest.assemble import validate

ROOT = Path(__file__).resolve().parents[2]


class FakeClient:
    """Stands in for anthropic.Anthropic(): records the request, returns a canned reply."""

    def __init__(self, extraction, stop_reason="end_turn"):
        self.calls = []
        reply = SimpleNamespace(type="text", text=json.dumps(extraction))
        response = SimpleNamespace(content=[reply], stop_reason=stop_reason, model="claude-opus-5-5",
                                   stop_details=SimpleNamespace(category="test"), _request_id="req_test")

        def create(**kwargs):
            self.calls.append(kwargs)
            return response

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def item(kind, label, feeds=None, connects=(), cap=None, rate=None, x=None, y=None, conf=0.95):
    return {"kind": kind, "label": label, "feeds_label": feeds, "connects_labels": list(connects), "capacity_bbl": cap,
            "rate_bbl_hr": rate, "x": x, "y": y, "confidence": conf, "evidence": "drawn on plan"}


PLAN = {
    "site_name": "North Lease",
    "meters_per_drawing_width": 400,
    "items": [
        item("tank", "Tank 3", cap=500, x=0.4, y=0.5),
        item("tank", "T-4", cap=500, x=0.6, y=0.5),
        item("pump", "P3", feeds="Tank 3", rate=150, x=0.4, y=0.6),
        item("well", "W-9", feeds="Tank 3", rate=25, x=0.38, y=0.3),
        item("well", "unlabeled pumpjack", x=0.62, y=0.3, conf=0.6),
        item("crossover_valve", "XO", connects=["Tank 3", "T-4"], x=0.5, y=0.5),
        item("disposal_well", "SWD 1", rate=450, x=0.1, y=0.7),
    ],
    "unclear": ["a small box near the road"],
}
PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (800).to_bytes(4, "big") + (400).to_bytes(4, "big") + b"\x00" * 16


def test_scenario_json_is_a_valid_blueprint():
    result = ingest_bytes((ROOT / "scenario" / "field.json").read_bytes(), "field.json")
    assert result["ok"], result["errors"]
    assert result["source"]["read_by"] == "parser"
    assert result["counts"]["tanks"] == 6 and result["counts"]["wells"] == 24
    assert result["review"] == []


def test_image_goes_to_claude_with_structured_output_and_fallbacks():
    client = FakeClient(PLAN)
    result = ingest_bytes(PNG_HEADER, "plan.png", client=client)
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in call["betas"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["messages"][0]["content"][0]["type"] == "image"
    bp = result["blueprint"]
    ids = {a["id"] for k in ("tanks", "pumps", "valves", "wells", "disposal_wells") for a in bp[k]}
    assert {"TANK_3", "TANK_4", "PUMP_3", "WELL_9", "VALVE_XO_34", "SWD_1"} <= ids
    assert result["ok"], result["errors"]


def test_numbers_are_never_invented():
    bp = ingest_bytes(PNG_HEADER, "plan.png", client=FakeClient(PLAN))["blueprint"]
    tank = next(t for t in bp["tanks"] if t["id"] == "TANK_3")
    assert "shutdown_pct" not in tank
    unlabeled = next(w for w in bp["wells"] if w["_ingest"]["label"] == "unlabeled pumpjack")
    assert "water_bbl_hr" not in unlabeled


def test_review_lists_gaps_guesses_and_low_confidence():
    result = ingest_bytes(PNG_HEADER, "plan.png", client=FakeClient(PLAN))
    issues = " ".join(r["issue"] for r in result["review"])
    assert "shutdown_pct" in issues
    assert "nearest tank" in issues
    assert "60% sure" in issues
    assert "manual or remote" in issues


def test_positions_keep_the_drawing_shape():
    bp = ingest_bytes(PNG_HEADER, "plan.png", client=FakeClient(PLAN))["blueprint"]
    tank3 = next(t for t in bp["tanks"] if t["id"] == "TANK_3")
    assert tank3["x"] == pytest.approx(-40.0) and tank3["y"] == pytest.approx(0.0)
    swd = bp["disposal_wells"][0]
    assert swd["y"] == pytest.approx(-40.0)   # 0.2 below center x 400 m wide x aspect 0.5


def test_refusal_becomes_a_clear_error():
    with pytest.raises(IngestError) as err:
        ingest_bytes(PNG_HEADER, "plan.png", client=FakeClient(PLAN, stop_reason="refusal"))
    assert err.value.status == 422 and "declined" in err.value.message


def test_csv_is_parsed_without_claude():
    csv_text = ("kind,id,tank,capacity,rate,x,y,connects\n"
                "tank,TANK_1,,500,,0,40,\n"
                "tank,TANK_2,,500,,30,40,\n"
                "pump,PUMP_1,TANK_1,,150,3,30,\n"
                "well,WELL_1,TANK_1,,20,0,70,\n"
                "crossover,XO,,,,15,40,TANK_1;TANK_2\n"
                "swd,SWD_1,,,400,-60,10,\n")
    result = ingest_bytes(csv_text.encode(), "equipment.csv")
    assert result["source"]["read_by"] == "parser"
    bp = result["blueprint"]
    assert {v["id"] for v in bp["valves"]} == {"VALVE_XO_12"}
    assert bp["pumps"][0]["tank_id"] == "TANK_1" and bp["tanks"][0]["x"] == 0.0
    assert result["ok"], result["errors"]


def test_validate_catches_duplicates_and_bad_references():
    bp = {"tanks": [{"id": "TANK_1"}, {"id": "TANK_1"}], "pumps": [{"id": "PUMP_1", "tank_id": "TANK_9"}],
          "valves": [{"id": "bad id", "connects": ["TANK_7"]}]}
    errors = " ".join(validate(bp))
    assert "Duplicate ID TANK_1" in errors and "TANK_9" in errors and "bad ID" in errors and "TANK_7" in errors


def test_unsupported_binary_is_rejected():
    with pytest.raises(IngestError) as err:
        ingest_bytes(b"\xff\xfe\x00\x81binary", "plan.dwg")
    assert err.value.status == 415


def test_router_returns_blueprint():
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ingest.router import router

    app = FastAPI()
    app.include_router(router)
    res = TestClient(app).post("/api/ingest", files={"file": ("field.json", (ROOT / "scenario" / "field.json").read_bytes(), "application/json")})
    assert res.status_code == 200 and res.json()["counts"]["tanks"] == 6
