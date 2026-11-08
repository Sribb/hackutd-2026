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


def item(kind, label, feeds=None, connects=(), cap=None, rate=None, x=None, y=None, conf=0.95,
         shutdown=None, lost=None, manual=None):
    return {"kind": kind, "label": label, "feeds_label": feeds, "connects_labels": list(connects), "capacity_bbl": cap,
            "rate_bbl_hr": rate, "shutdown_pct": shutdown, "lost_usd_hr": lost, "manual": manual,
            "x": x, "y": y, "confidence": conf, "evidence": "drawn on plan"}


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
    assert "Missing max_bbl_hr, manual." in issues   # the plan's crossover has neither


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


def test_missing_credentials_is_a_clear_503_not_a_crash():
    def no_auth(**_kwargs):
        raise TypeError("Could not resolve authentication method. Expected one of api_key, auth_token, or credentials to be set.")

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=no_auth)))
    with pytest.raises(IngestError) as err:
        ingest_bytes(PNG_HEADER, "plan.png", client=client)
    assert err.value.status == 503 and "ANTHROPIC_API_KEY" in err.value.message


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


def test_crossover_with_a_known_flow_asks_only_for_manual():
    # Ronak's report: sample 2 gives XO 2-3 150 bbl/hr, but review asked for its flow again.
    result = ingest_bytes((ROOT / "scenario" / "samples" / "sample2_delaware_4tank.csv").read_bytes(), "s2.csv")
    xo = next(v for v in result["blueprint"]["valves"] if v["id"] == "VALVE_XO_23")
    assert xo["max_bbl_hr"] == 150
    issues = [r["issue"] for r in result["review"] if r["id"] == "VALVE_XO_23"]
    assert len(issues) == 1 and issues[0].startswith("Missing manual.") and "max_bbl_hr" not in issues[0]


def test_truck_minutes_to_goes_on_review():
    # BLUEPRINT.md gap 1: without this, a truck passes review and then fails on load.
    result = ingest_bytes((ROOT / "scenario" / "samples" / "sample2_delaware_4tank.csv").read_bytes(), "s2.csv")
    truck = [r["issue"] for r in result["review"] if r["id"] == "TRUCK_1"]
    assert len(truck) == 1 and truck[0].startswith("Missing minutes_to.")


def test_csv_with_every_number_needs_no_review():
    csv_text = ("kind,label,tank,capacity,rate,shutdown_pct,lost_usd_hr,manual,minutes_to,connects\n"
                "tank,Tank 1,,500,,90,,,,\n"
                "tank,Tank 2,,500,,85,,,,\n"
                "pump,P-1,Tank 1,,150,,,,,\n"
                "well,Well 1,Tank 1,,20,,260,,,\n"
                "crossover,XO 1-2,,,150,,,manual,,Tank 1;Tank 2\n"
                "swd,SWD 1,,,450,,,,,\n"
                "discharge,VD-1,,,,,,,,P-1;SWD 1\n"
                "vacuum_truck,Truck 1,,130,,,,,Tank 1:20; Tank 2:25,\n")
    result = ingest_bytes(csv_text.encode(), "full.csv")
    bp = result["blueprint"]
    assert result["review"] == [] and result["ok"], (result["review"], result["errors"])
    assert [t["shutdown_pct"] for t in bp["tanks"]] == [90, 85]
    assert bp["wells"][0]["lost_usd_hr"] == 260
    assert next(v for v in bp["valves"] if v["kind"] == "CROSSOVER")["manual"] is True
    assert bp["trucks"][0]["minutes_to"] == {"TANK_1": 20, "TANK_2": 25}


def test_csv_remote_crossover_and_unknown_drive_time_label():
    csv_text = ("kind,label,connects,mode,minutes_to,capacity\n"
                "tank,Tank 1,,,,500\n"
                "tank,Tank 2,,,,500\n"
                "xo,XO,Tank 1;Tank 2,remote,,\n"
                "truck,Truck 1,,,Tank 9:15,130\n")
    result = ingest_bytes(csv_text.encode(), "remote.csv")
    assert next(v for v in result["blueprint"]["valves"])["manual"] is False
    issues = " ".join(r["issue"] for r in result["review"] if r["id"] == "TRUCK_1")
    assert "'Tank 9'" in issues and "Missing minutes_to." in issues


def test_review_matches_the_blueprint_validator():
    """Every gap docs/BLUEPRINT.md's validator finds in a draft is on ingest's review list, and only those."""
    jsonschema = pytest.importorskip("jsonschema")
    import importlib.util
    spec = importlib.util.spec_from_file_location("validate_blueprint", ROOT / "scenario" / "validate_blueprint.py")
    vb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vb)
    validator = jsonschema.Draft202012Validator(json.loads(vb.SCHEMA.read_text()))
    result = ingest_bytes((ROOT / "scenario" / "samples" / "sample2_delaware_4tank.csv").read_bytes(), "s2.csv")
    gaps = vb.check(result["blueprint"], validator)
    assert len(gaps) == len(result["review"]) == 14, gaps
    asked = sorted(f for r in result["review"] for f in r["issue"].split(".")[0].removeprefix("Missing ").split(", "))
    found = sorted(g.split("'")[1] for g in gaps)
    assert asked == found


def test_numbers_claude_reads_reach_the_blueprint():
    plan = {**PLAN, "items": [item("tank", "Tank 3", cap=500, shutdown=90), item("tank", "T-4", cap=500, shutdown=90),
                              item("well", "W-9", feeds="Tank 3", rate=25, lost=310),
                              item("crossover_valve", "XO", connects=["Tank 3", "T-4"], rate=150, manual=True)]}
    result = ingest_bytes(PNG_HEADER, "plan.png", client=FakeClient(plan))
    bp = result["blueprint"]
    assert {t["shutdown_pct"] for t in bp["tanks"]} == {90}
    assert bp["wells"][0]["lost_usd_hr"] == 310
    xo = bp["valves"][0]
    assert xo["manual"] is True and xo["max_bbl_hr"] == 150
    assert not [r for r in result["review"] if r["id"] == "VALVE_XO_34"]


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
