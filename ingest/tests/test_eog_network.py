"""ingest/eog_network.py: an EOG sponsor network becomes one complete blueprint and one 3D field per system.

fixtures/eog/ holds network.json and ops.json from the sponsor repo's history/ folders (the
two files are identical in every one of them). Set EOG_DATA to a checkout of
nihaal419/HackUTD-ZeroDay to run every folder in it too, test/ included once it lands.
unseen() reshapes that network into S6 to S11 with other counts and ID styles, so nothing
here can lean on S1 to S5.
"""
import copy
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

from ingest import eog_field, eog_network as eog

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "eog"
sys.path.insert(0, str(ROOT / "blender"))
sys.path.insert(0, str(ROOT / "scenario"))
import blueprint as gen_blueprint  # noqa: E402
import names as gen_names  # noqa: E402
from glb_read import Glb  # noqa: E402

PREFIX = {"tanks": "TANK_", "pumps": "PUMP_", "valves": "VALVE_D_", "wells": "WELL_", "disposal_wells": "SWD_",
          "trucks": "TRUCK_", "yards": "YARD_"}


def folders():
    out = [FIXTURE]
    base = os.environ.get("EOG_DATA")
    if base:
        out += sorted({p.parent for p in Path(base).rglob("network.json") if ".git" not in p.parts})
    return out


def fixture():
    network, ops, _ = eog.load_folder(FIXTURE)
    return network, ops


def unseen():
    """S1..S5 renamed S6..S10 and reshaped, plus S11: one facility with 8 wells and its own ID style."""
    network, ops = fixture()
    shift = lambda text: re.sub(r'(?<![A-Z0-9])S([1-5])(?=_|")', lambda m: f"S{int(m.group(1)) + 5}", text)  # noqa: E731
    network, ops = json.loads(shift(json.dumps(network))), json.loads(shift(json.dumps(ops)))
    for s in network["systems"]:                      # somewhere else on the map
        s["center"] = [s["center"][0] + 1.2, s["center"][1] + 2.5]
        s["truck_yard"] = [s["truck_yard"][0] + 1.2, s["truck_yard"][1] + 2.5]
        s["disposal_site"]["lat"] += 1.2
        s["disposal_site"]["lon"] += 2.5
        for f in s["facilities"]:
            f["lat"], f["lon"] = f["lat"] + 1.2, f["lon"] + 2.5
    s7 = network["systems"][1]                        # another ID style
    for k, f in enumerate(s7["facilities"]):
        old = f["id"]
        f["id"] = f"S7_PAD_{['NORTH', 'EAST', 'SOUTH', 'WEST'][k]}"
        ops["systems"]["S7"]["facilities"][f["id"]] = ops["systems"]["S7"]["facilities"].pop(old)
    ops["systems"]["S8"]["emergency_fleet_size"] = 4
    s11 = copy.deepcopy(network["systems"][0])
    s11["id"], s11["disposal_site"]["id"] = "S11", "S11_SWD_MAIN"
    fac = s11["facilities"][0]
    s11["facilities"] = [fac]
    fac["id"], fac["tank"]["id"], fac["pump"]["id"], fac["pipeline"]["id"] = "S11_FACILITY_Q", "S11_TANK_Q", "S11_PUMP_Q", "S11_PIPELINE_Q"
    fac["wells"] = [dict(fac["wells"][k % 3], id=f"S11_WELL_Q{k + 1}") for k in range(8)]
    network["systems"].append(s11)
    ops["systems"]["S11"] = {"emergency_fleet_size": 1, "facilities": {"S11_FACILITY_Q": {"response_min": 40, "cycle_min": 150}}}
    return network, ops


def sponsor_ids(system):
    out = [system["disposal_site"]["id"]]
    for f in system["facilities"]:
        out += [f["id"], f["tank"]["id"], f["pump"]["id"], f["pipeline"]["id"]] + [w["id"] for w in f["wells"]]
    return out


@pytest.fixture(scope="module")
def validator():
    js = pytest.importorskip("jsonschema")
    return js.Draft202012Validator(json.loads((ROOT / "docs" / "blueprint.schema.json").read_text()))


def complete(bp, validator):
    from validate_blueprint import check
    return check(json.loads(json.dumps(bp)), validator)


@pytest.mark.parametrize("folder", folders(), ids=lambda p: p.name)
def test_every_folder_gives_complete_blueprints(folder, validator):
    network, ops, _ = eog.load_folder(folder)
    results = eog.convert(network, ops)
    assert list(results) == [s["id"] for s in network["systems"]]
    for sid, r in results.items():
        assert complete(r["blueprint"], validator) == [], sid
        assert r["review"] == [], sid          # network.json plus ops.json give every number
        _, errors, _ = gen_blueprint.check(r["blueprint"])
        assert errors == [], sid


@pytest.mark.parametrize("data", [fixture, unseen], ids=["history", "unseen"])
def test_every_sponsor_id_maps_to_a_typed_name(data, validator):
    network, ops = data()
    results = eog.convert(network, ops)
    assert results == eog.convert(network, ops)          # deterministic
    for system in network["systems"]:
        r = results[system["id"]]
        bp, ids = r["blueprint"], r["ids"]
        assert set(sponsor_ids(system)) <= set(ids), system["id"]
        assert len(set(ids.values())) == len(ids)          # one node per sponsor asset
        for key, prefix in PREFIX.items():
            for a in bp[key]:
                assert a["id"].startswith(prefix) and eog.ID_RE.match(a["id"]), a["id"]
                if a.get("_source_id"):
                    assert ids[a["_source_id"]] == a["id"]
        assert complete(bp, validator) == [], system["id"]


def test_numbers_come_from_the_files():
    network, ops = fixture()
    limits = network["defaults"]["tank_limits_pct"]
    po, pg = ops["prices"]["oil_per_bbl"], ops["prices"]["gas_per_mcf"]
    for system in network["systems"]:
        bp = eog.network_to_blueprints(network, ops)[system["id"]]
        by_src = {a.get("_source_id"): a for key in PREFIX for a in bp[key]}
        sys_ops = ops["systems"][system["id"]]
        for f in system["facilities"]:
            tank, pump = by_src[f["tank"]["id"]], by_src[f["pump"]["id"]]
            assert (tank["capacity_bbl"], tank["shutdown_pct"], tank["warning_pct"]) == \
                (f["tank"]["capacity_bbl"], limits["shutdown"], limits["warning"])
            assert pump["rate_bbl_hr"] == pytest.approx(f["pump"]["max_flow_bpd"] / 24, abs=1e-3)
            valve = by_src[f["pipeline"]["id"]]
            assert valve["connects"] == [pump["id"], by_src[system["disposal_site"]["id"]]["id"]]
            for w in f["wells"]:
                well = by_src[w["id"]]
                assert well["tank_id"] == tank["id"]
                assert well["water_bbl_hr"] == pytest.approx(w["baseline_water_bpd"] / 24, abs=1e-3)
                assert well["lost_usd_hr"] == pytest.approx((w["oil_bpd"] * po + w["gas_mcfd"] * pg) / 24, abs=0.01)
            for t in bp["trucks"]:
                assert t["minutes_to"][tank["id"]] == sys_ops["facilities"][f["id"]]["response_min"]
                assert t["capacity_bbl"] == ops["emergency_trucking"]["truck_capacity_bbl"]
        assert len(bp["trucks"]) == min(6, sys_ops["emergency_fleet_size"])


def test_without_ops_only_the_missing_numbers_are_gaps(validator):
    network, _ = fixture()
    for sid, r in eog.convert(network, None).items():
        problems = complete(r["blueprint"], validator)
        assert problems and all(re.match(r"wells/\d+: 'lost_usd_hr' is a required property", p) for p in problems), problems
        issues = " ".join(i["issue"] for i in r["review"])
        assert "lost_usd_hr" in issues and "ops.json" in issues
        _, errors, _ = gen_blueprint.check(r["blueprint"])     # still builds: the generator needs IDs only
        assert errors == []


@pytest.mark.parametrize("data", [fixture, unseen], ids=["history", "unseen"])
def test_layout_keeps_true_order_and_never_overlaps(data):
    network, ops = data()
    for system in network["systems"]:
        bp = eog.network_to_blueprints(network, ops)[system["id"]]
        placed = [a for key in ("tanks", "disposal_wells", "yards") for a in bp[key] if "x" in a]
        assert len(placed) == len(bp["tanks"]) + 2
        xs = sorted(a["x"] for a in placed)
        assert all(b - a >= eog.GAP_MIN - 0.2 for a, b in zip(xs, xs[1:]))
        assert all(abs(a["x"]) <= 5000 and abs(a["y"]) <= 5000 for a in placed)
        assert all(abs(t["y"]) >= eog.TANK_Y for t in bp["tanks"])
        src = {t["_source_id"]: t for t in bp["tanks"]}
        by_lon = sorted(system["facilities"], key=lambda f: (f["lon"], f["lat"]))    # same longitude: south first
        assert [src[f["tank"]["id"]]["x"] for f in by_lon] == sorted(src[f["tank"]["id"]]["x"] for f in by_lon)


def boxes_overlap(a, b):
    return a[0] < b[1] and b[0] < a[1] and a[2] < b[3] and b[2] < a[3]


@pytest.mark.parametrize("data", [fixture, unseen], ids=["history", "unseen"])
def test_kit_glb_has_every_id_and_builds_in_under_2_s(data, tmp_path):
    network, ops = data()
    eog_field._assembler()                                   # load the model kit once, as a server does
    for sid, r in eog.convert(network, ops).items():
        t0 = time.perf_counter()
        s = eog.write_system(r, tmp_path / sid, "kit")
        took = time.perf_counter() - t0
        assert s["ok"], (sid, s["errors"])
        assert took < 2.0, f"{sid} took {took:.2f} s"
        glb = Glb(s["glb"])
        names = set(glb.by_name)
        bp, _, _ = gen_blueprint.check(r["blueprint"])
        assert set(gen_names.expected(bp)) <= names, sid
        doc = json.loads((tmp_path / sid / "ids.json").read_text())
        assert doc["glb"] == "field.glb" and set(doc["assets"].values()) <= names
        for src, node in doc["assets"].items():
            if not node.startswith("PAD_"):
                assert glb.nodes[glb.idx(node)]["extras"]["source_id"] == src
        for src, pipes in doc["pipes"].items():
            assert pipes and set(pipes) <= names and len(pipes) >= 3, (src, pipes)   # own run + trunk to the SWD
        roots = [f"PAD_{t['id'][5:]}" for t in r["blueprint"]["tanks"]] + \
            [a["id"] for a in r["blueprint"]["disposal_wells"] + r["blueprint"]["yards"]]
        found = {n: glb.box(n) for n in roots}
        for i, a in enumerate(roots):
            for b in roots[i + 1:]:
                assert not boxes_overlap(found[a], found[b]), (sid, a, b)


def test_field_glb_builds_once_then_caches(tmp_path):
    t0 = time.perf_counter()
    path = eog.field_glb(FIXTURE, "S2", cache_dir=tmp_path)
    first = time.perf_counter() - t0
    assert path.exists() and (path.parent / "ids.json").exists() and first < 2.0
    t0 = time.perf_counter()
    assert eog.field_glb(FIXTURE, "S2", cache_dir=tmp_path) == path
    assert time.perf_counter() - t0 < 0.2
    with pytest.raises(eog.DatasetError):
        eog.field_glb(FIXTURE, "S99", cache_dir=tmp_path)


def test_cli_writes_one_folder_per_system(tmp_path, capsys):
    assert eog.main([str(FIXTURE), "--out", str(tmp_path), "--system", "S3", "--glb"]) == 0
    assert sorted(p.name for p in (tmp_path / "S3").iterdir()) == ["blueprint.json", "build_report.json", "field.glb",
                                                                   "ids.json"]
    assert "S3: 2 tanks" in capsys.readouterr().out
    assert eog.main([str(FIXTURE), "--out", str(tmp_path), "--system", "S42"]) == 1
    assert eog.main([str(tmp_path / "nowhere"), "--out", str(tmp_path)]) == 1
