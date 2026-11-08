"""gaia's Claude side in dataset mode: the prompt describes the field on screen, the week-history
tool answers from the dataset's own events and plans, and no flow is ever said as "-0".

The sponsor tests run when FREEBOARD_EOG_DIR points at the sponsor repo and skip otherwise; the
synthetic ones always run."""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import auth
from backend.app.eog import dataset_api, gaia, service
from backend.app.jarvis import Jarvis, bbl_hr_text
from backend.app.main import app
from backend.tests.eog_fixture import write_dataset
from backend.tests.test_gaia_agentic import make, tripped_field

REAL_ROOT = os.environ.get("FREEBOARD_EOG_DIR", "")
WEEK = "history/full_week"
MINUS_ZERO = re.compile(r"(?<![\d])-0\b")
client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(dataset_api, "PREBUILD", False)
    monkeypatch.setenv("JARVIS_MODE", "rules")
    for var in ("FREEBOARD_EOG_DATASET", "FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gaia, "_legacy_on", lambda: False)
    service.SERVICE.reset()
    yield
    service.SERVICE.reset()
    auth.THROTTLE.reset()


def sponsor(monkeypatch) -> None:
    base = Path(REAL_ROOT) if REAL_ROOT else None
    if base is None or not (base / "history" / "full_week").is_dir():
        pytest.skip("FREEBOARD_EOG_DIR does not point at the sponsor repo")
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    assert client.post("/api/eog/datasets/activate", json={"id": WEEK}).status_code == 200
    assert gaia.active()


def ask(j: Jarvis, text: str) -> dict:
    return asyncio.run(j.handle(text))


# ---------- the demo field keeps its prompt ----------

def test_demo_prompt_still_describes_the_demo_field():
    j = make(tripped_field())
    assert not gaia.active()
    p = j.field_prompt()
    assert p == j.system
    assert "TANK_3" in p and "disposal wells" in p and "Tank 3" in p
    assert "dataset" not in p.lower() and "UTC" not in p


def test_no_minus_zero_barrels():
    assert bbl_hr_text(-0.3) == "0" and bbl_hr_text(-12) == "0" and bbl_hr_text(None) == "0"
    assert bbl_hr_text(41.6) == "42"
    assert gaia._bbl_hr(-0.4) == 0.0 and str(gaia._bbl_hr(-0.4)) == "0.0" and gaia._bbl_hr(None) is None
    assert gaia._bbl_hr(2400) == 100.0


# ---------- a synthetic dataset (always runs) ----------

@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    base = tmp_path / "eog"
    write_dataset(base / "alpha", seed=3, hours=24, repair_hours=5,
                  problems=[{"type": "pump_failure", "facility": "Q7_FACILITY_A", "onset_h": 6.0, "level_pct": 60}])
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    assert client.post("/api/eog/datasets/activate", json={"id": "alpha"}).status_code == 200
    return base


def test_synthetic_prompt_and_history(synthetic):
    j = make(tripped_field())
    p = j.field_prompt()
    assert p != j.system and "alpha" in p and "Q7_TANK_A" in p and "UTC" in p
    assert "TANK_3" not in p and "six tanks" not in p
    h = gaia.week_history()
    assert h["dataset"] == "alpha" and h["pump_failures"] >= 1
    out = ask(j, "which failure cost the most this week?")
    assert out["source"] == "rules" and "alpha" in out["reply"] and not MINUS_ZERO.search(out["reply"])
    assert "Q7_PUMP_A" in out["reply"]


def test_history_tool_only_on_a_dataset(synthetic):
    res = asyncio.run(Jarvis.run_tool(make(tripped_field()), "get_week_history", {}, {"ui": [], "proposals": []}))
    assert res["dataset"] == "alpha" and isinstance(res["events"], list)
    assert gaia.history_answer("open the crossover") is None


# ---------- the sponsor's full week (FREEBOARD_EOG_DIR) ----------

def test_sponsor_prompt_names_the_real_field(monkeypatch):
    sponsor(monkeypatch)
    p = make(tripped_field()).field_prompt()
    assert WEEK in p and "2026-10-11 23:55 UTC" in p and "2026-10-05 00:00 UTC" in p
    for aid in ("S1_TANK_A", "S2_PUMP_A", "S4_WELL_A2", "S1_DISPOSAL"):
        assert aid in p
    assert "TANK_3" not in p and "disposal wells" not in p and "AM" not in p.replace("AM or PM", "")


def test_sponsor_costliest_failure_by_rules(monkeypatch):
    sponsor(monkeypatch)
    out = ask(make(tripped_field()), "which failure cost the most this week?")
    assert out["source"] == "rules"
    r = out["reply"]
    assert "S2_PUMP_A at S2_FACILITY_A" in r and "$3,384" in r and "Oct 08 10:00 UTC" in r and WEEK in r
    assert "13 pump failures came to $15,327" in r and not MINUS_ZERO.search(r)
    # #190 adds id/source/reason to every directive; compare what matters
    assert [{k: d.get(k) for k in ("type", "action", "asset_id")} for d in out["ui"]] == [
        {"type": "ui", "action": "focus", "asset_id": "S2_PUMP_A"}]


def test_sponsor_week_and_list_by_rules(monkeypatch):
    sponsor(monkeypatch)
    j = make(tripped_field())
    week = ask(j, "what happened this week")
    assert week["source"] == "rules" and "13 pump failures" in week["reply"] and "$15,327" in week["reply"]
    lst = ask(j, "list the failures")
    assert lst["source"] == "rules" and lst["reply"].startswith("13 pump failures")
    assert "S1_PUMP_B Oct 07 02:10 UTC ($0)" in lst["reply"]
