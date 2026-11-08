"""Blueprints API (#41): upload, validate, build (or fall back), serve the glb, activate."""
import copy
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import blueprints
from backend.app.main import app

ROOT = Path(__file__).resolve().parents[2]
FIELD = json.loads((ROOT / "scenario" / "field.json").read_text())
SAMPLE1 = json.loads((ROOT / "scenario" / "samples" / "sample1_pecos_8tank.json").read_text())
REAL_FIND_BLENDER = blueprints.find_blender
client = TestClient(app)

pytestmark = pytest.mark.skipif(blueprints._schema_validator() is None, reason="pip install jsonschema")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Each test stores into its own folder, never runs a real Blender, and ends on the default scenario."""
    monkeypatch.setattr(blueprints, "DATA_DIR", tmp_path / "blueprints")
    monkeypatch.setattr(blueprints, "find_blender", lambda: None)
    blueprints._records.clear()
    yield
    client.post("/api/blueprints/default/activate")
    blueprints._records.clear()


def wait_done(bid, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        body = client.get(f"/api/blueprints/{bid}").json()
        if body["status"] != "building":
            return body
        time.sleep(0.05)
    raise AssertionError(f"{bid} still building after {timeout} s")


def shutdowns():
    fc = client.post("/api/forecast", json={}).json()
    return {e["asset_id"]: e["text"] for e in fc["events"] if e["type"] == "SHUTDOWN_LEVEL"}


def test_field_json_is_ok_and_its_glb_downloads():
    r = client.post("/api/blueprints", json={"blueprint": FIELD})
    assert r.status_code == 200, r.text
    body = r.json()
    # The shape web/src/upload.js (#43) reads.
    for key in ("id", "status", "errors", "warnings", "counts", "glb_url", "note"):
        assert key in body
    assert body["ok"] and body["errors"] == []
    assert body["status"] in ("ready", "fallback")
    assert body["counts"]["tanks"] == len(FIELD["tanks"])
    assert body["glb_url"] == f"/api/blueprints/{body['id']}/field.glb"
    glb = client.get(body["glb_url"])
    assert glb.status_code == 200
    assert glb.content[:4] == b"glTF"
    assert glb.headers["content-type"] == "model/gltf-binary"
    assert client.get(f"/api/blueprints/{body['id']}").json()["status"] == body["status"]


def test_multipart_json_file_and_same_file_same_id():
    files = {"file": ("field.json", json.dumps(FIELD).encode(), "application/json")}
    a = client.post("/api/blueprints", files=files)
    assert a.status_code == 200, a.text
    b = client.post("/api/blueprints", json=FIELD)  # a raw blueprint body works too
    assert b.status_code == 200
    assert a.json()["id"] == b.json()["id"]


def test_drawings_and_csv_are_sent_to_ingest():
    r = client.post("/api/blueprints", files={"file": ("plan.csv", b"id,kind\n", "text/csv")})
    assert r.status_code == 415
    assert "/api/ingest" in r.json()["detail"]


def test_broken_blueprint_returns_problems_and_is_not_built():
    bad = copy.deepcopy(SAMPLE1)
    del bad["tanks"][0]["capacity_bbl"]
    bad["pumps"][0]["tank_id"] = "TANK_99"
    r = client.post("/api/blueprints", json={"blueprint": bad})
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "failed" and not body["ok"]
    assert body["glb_url"] is None
    text = " ".join(body["errors"])
    assert "capacity_bbl" in text and "TANK_99" in text
    assert "problem" in body["detail"]
    bid = body["id"]
    assert client.get(f"/api/blueprints/{bid}").json()["status"] == "failed"
    assert client.get(f"/api/blueprints/{bid}/field.glb").status_code == 409
    assert client.post(f"/api/blueprints/{bid}/activate").status_code == 409
    assert len(client.get("/api/field").json()["tanks"]) == 6  # still the default


def test_sample1_activate_reports_tank_6_shutdown():
    before = shutdowns()
    r = client.post("/api/blueprints", json={"blueprint": SAMPLE1, "activate": False})
    assert r.status_code == 200, r.text
    bid = r.json()["id"]
    assert r.json()["active"] is False
    assert shutdowns() == before  # not active yet

    act = client.post(f"/api/blueprints/{bid}/activate")
    assert act.status_code == 200 and act.json()["active"] is True
    after = shutdowns()
    assert "TANK_6" in after and "2:03 AM" in after["TANK_6"]
    field = client.get("/api/field").json()
    assert len(field["tanks"]) == 8
    assert client.get("/api/state").json()["assets"]["PUMP_6"]["status"] == "ALARM"
    assert client.post("/api/plans", json={}).json()["plans"]
    assert client.get("/api/blueprints").json()["active"] == bid

    back = client.post("/api/blueprints/default/activate")
    assert back.status_code == 200
    assert len(client.get("/api/field").json()["tanks"]) == 6
    assert shutdowns() == before


def test_upload_activates_by_default():
    r = client.post("/api/blueprints", json={"blueprint": SAMPLE1})
    assert r.json()["active"] is True
    assert len(client.get("/api/field").json()["tanks"]) == 8


def test_fallback_with_no_blender(monkeypatch, tmp_path):
    gen = tmp_path / "build_field.py"
    gen.write_text("# takes --blueprint and --out\n")
    monkeypatch.setattr(blueprints, "GENERATOR", gen)
    monkeypatch.setattr(blueprints, "find_blender", REAL_FIND_BLENDER)
    monkeypatch.setenv("BLENDER", str(tmp_path / "no-such-blender"))
    monkeypatch.setattr(blueprints.shutil, "which", lambda _name: None)
    monkeypatch.setattr(blueprints, "MAC_BLENDER", str(tmp_path / "no-such-app"))
    body = client.post("/api/blueprints", json={"blueprint": SAMPLE1}).json()
    assert body["status"] == "fallback" and body["model"] == "fallback"
    assert "Blender isn't installed" in body["note"]
    assert "TANK_7" in body["note"]  # the standard model has 6 tanks
    glb = client.get(body["glb_url"])
    assert glb.status_code == 200 and glb.headers["x-freeboard-model"] == "fallback"
    assert glb.content == blueprints.FALLBACK_GLB.read_bytes()


def fake_blender(tmp_path, ok=True):
    """A stand-in for Blender that runs the generator's CLI contract: --blueprint X --out DIR."""
    script = tmp_path / "fake_blender.py"
    script.write_text(f"""#!{sys.executable}
import json, os, sys
args = sys.argv[sys.argv.index("--") + 1:]
bp = json.load(open(args[args.index("--blueprint") + 1]))
out = args[args.index("--out") + 1]
report = {{"ok": {ok}, "errors": [] if {ok} else ["boom"], "warnings": ["built at low detail"],
          "counts": {{"tanks": len(bp["tanks"]), "wells": len(bp["wells"])}}, "placed": {{"TANK_1": [0, 0]}}}}
if {ok}:
    open(os.path.join(out, "field.glb"), "wb").write(b"glTF-fake-build")
json.dump(report, open(os.path.join(out, "build_report.json"), "w"))
sys.exit(0 if {ok} else 2)
""")
    script.chmod(0o755)
    if sys.platform == "win32":  # Windows can't run a shebang script, so wrap it
        wrapper = tmp_path / "fake_blender.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\n')
        script = wrapper
    gen = tmp_path / "build_field.py"
    gen.write_text("# takes --blueprint and --out\n")
    return script, gen


def test_build_with_blender_serves_the_built_glb(monkeypatch, tmp_path):
    script, gen = fake_blender(tmp_path, ok=True)
    monkeypatch.setattr(blueprints, "GENERATOR", gen)
    monkeypatch.setattr(blueprints, "find_blender", REAL_FIND_BLENDER)
    monkeypatch.setenv("BLENDER", str(script))
    first = client.post("/api/blueprints", json={"blueprint": SAMPLE1}).json()
    assert first["status"] in ("building", "ready")
    body = wait_done(first["id"])
    assert body["status"] == "ready", body
    assert body["model"] == "built"
    assert body["counts"] == {"tanks": 8, "wells": len(SAMPLE1["wells"])}
    assert body["placed"] == {"TANK_1": [0, 0]}
    assert "Generator: built at low detail" in body["warnings"]
    glb = client.get(body["glb_url"])
    assert glb.content == b"glTF-fake-build" and glb.headers["x-freeboard-model"] == "built"


def test_failed_build_falls_back(monkeypatch, tmp_path):
    script, gen = fake_blender(tmp_path, ok=False)
    monkeypatch.setattr(blueprints, "GENERATOR", gen)
    monkeypatch.setattr(blueprints, "find_blender", REAL_FIND_BLENDER)
    monkeypatch.setenv("BLENDER", str(script))
    first = client.post("/api/blueprints", json={"blueprint": FIELD}).json()
    body = wait_done(first["id"])
    assert body["status"] == "fallback" and body["ok"] and body["errors"] == []
    assert "Generator error: boom" in body["warnings"]
    assert client.get(body["glb_url"]).content == blueprints.FALLBACK_GLB.read_bytes()


def test_main_generator_without_blueprint_flag_is_never_run(monkeypatch, tmp_path):
    """main's build_field.py rebuilds blender/field.glb in place, so it must not run from here."""
    gen = tmp_path / "build_field.py"
    gen.write_text("# rebuilds the fixed field\n")
    monkeypatch.setattr(blueprints, "GENERATOR", gen)
    monkeypatch.setattr(blueprints, "find_blender", lambda: "/bin/false")
    body = client.post("/api/blueprints", json={"blueprint": FIELD}).json()
    assert body["status"] == "fallback" and "#33" in body["note"]


def test_unknown_id_is_404():
    assert client.get("/api/blueprints/bp_000000000000").status_code == 404
    assert client.get("/api/blueprints/../../etc/passwd").status_code == 404
    assert client.post("/api/blueprints/nope/activate").status_code == 404
