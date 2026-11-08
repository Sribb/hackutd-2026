"""Tests for backend/app/eog/blueprint.py: a dataset's network as a blueprint and as the console's
asset list.

The synthetic tests always run: they write small networks with other system names (S6 and up) to a
temporary folder. The sponsor tests read FREEBOARD_EOG_DIR and skip when it is unset or missing.
Every blueprint is checked by four readers: docs/blueprint.schema.json with jsonschema, the team's
cross checks (scenario/validate_blueprint.py), the 3D generator's own check and layout
(blender/blueprint.py and blender/layout.py, plain Python, loaded read-only), and the backend's
scenario reader.
"""
from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import math
import os
import re
import sys
import time
import types
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.app.eog import blueprint as B
from backend.app.eog.dataset import Dataset, discover, load

REPO = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO / "docs" / "blueprint.schema.json"
BLENDER_DIR = REPO / "blender"
GENERATOR_MODULES = ("blueprint", "layout", "names", "siting", "terrain")
EARTH_R = 6_371_008.8
T0 = 1_830_000_000          # a synthetic week, far from the sponsor's
BLOCKED = ("tank", "pump", "well", "pipeline", "disposal")   # kinds whose dataset ids the schema rejects

# Three systems with other names and shapes: a center exactly on a facility's latitude, a disposal
# site exactly at the center, two pads nearly in line north-south (the layout must move one), ids
# that already fit the schema (kept), an id the schema makes collide with a fitting id listed after
# it (the fitting one is kept, the other made unique), an id with lowercase and punctuation, 1 to 8
# wells per tank, a pipeline with no limits, a fleet over the schema's 6 trucks, a fleet of 0, and a
# system with no center or truck_yard (dataset.py falls back).
SYNTH = [
    {"id": "S6", "center": (35.0, -100.0), "yard": (35.4, -99.5), "disposal": ("S6_DISPOSAL", 35.0, -100.0),
     "fleet": 8, "facilities": [
         {"id": "S6_FACILITY_A", "lat": 35.05, "lon": -100.06, "tank": "S6_TANK_A", "pump": "S6_PUMP_A",
          "pipeline": "S6_PIPELINE_A", "wells": ["S6_WELL_A1", "S6_WELL_A2"]},
         {"id": "S6_FACILITY_B", "lat": 35.0, "lon": -100.02, "tank": "S6_TANK_B", "pump": "S6_PUMP_B",
          "pipeline": "S6_PIPELINE_B", "wells": [f"S6_WELL_B{k}" for k in range(1, 6)], "limits": False}]},
    {"id": "S7", "center": (36.0, -101.0), "yard": (35.6, -101.3), "disposal": ("SWD_7", 36.01, -101.0),
     "fleet": 0, "facilities": [
         {"id": "S7_FACILITY_A", "lat": 35.95, "lon": -101.0, "tank": "S7_TANK_A", "pump": "S7_PUMP_A",
          "pipeline": "S7_PIPELINE_A", "wells": [f"S7_WELL_A{k}" for k in range(1, 9)]},
         {"id": "S7_FACILITY_B", "lat": 35.90, "lon": -101.0005, "tank": "TANK_7B", "pump": "S7_PUMP_B",
          "pipeline": "S7_PIPELINE_B", "wells": ["S7_WELL_B1"]}]},
    {"id": "S8", "disposal": ("S8_DISPOSAL", 34.0, -102.0), "fleet": 1, "facilities": [
        {"id": "S8_FACILITY_A", "lat": 34.03, "lon": -102.03, "tank": "X", "pump": "S8_PUMP_A",
         "pipeline": "S8_PIPELINE_A", "wells": ["s8-well.1", "S8_WELL_A2", "S8_WELL_A3"]},
        {"id": "S8_FACILITY_B", "lat": 33.96, "lon": -101.95, "tank": "TANK_X", "pump": "S8_PUMP_B",
         "pipeline": "S8_PIPELINE_B", "wells": ["S8_WELL_B1", "S8_WELL_B2", "S8_WELL_B3", "S8_WELL_B4"]}]},
]
GHOST = ("S9_GHOST", "level_pct")   # in the telemetry, not in network.json


# ---------- writing synthetic datasets ----------
def write_dataset(folder: Path, systems: list[dict], limits=(80, 95), ghost=None) -> Dataset:
    folder.mkdir(parents=True, exist_ok=True)
    net = {"meta": {"timezone": "UTC"},
           "defaults": {"tank_limits_pct": {"warning": limits[0], "shutdown": limits[1]}}, "systems": []}
    ops = {"dataset_id": "synthetic", "prices": {"oil_per_bbl": 70, "gas_per_mcf": 2.5},
           "expected_repair_hours": {"pump_failure": 5},
           "emergency_trucking": {"truck_capacity_bbl": 120, "cost_per_trip": 800}, "systems": {}}
    series = []
    for s in systems:
        d_id, d_lat, d_lon = s["disposal"]
        out = {"id": s["id"], "disposal_site": {"id": d_id, "lat": d_lat, "lon": d_lon, "pressure_psi": 900},
               "facilities": []}
        if "center" in s:
            out["center"] = list(s["center"])
        if "yard" in s:
            out["truck_yard"] = list(s["yard"])
        fops = {}
        for i, f in enumerate(s["facilities"]):
            pipe = {"id": f["pipeline"]}
            if f.get("limits", True):
                pipe.update({"max_bpd": 2300 + 100 * i, "max_pressure_psi": 1500})
            out["facilities"].append({
                "id": f["id"], "lat": f["lat"], "lon": f["lon"],
                "wells": [{"id": w, "baseline_water_bpd": 300 + 50 * k, "oil_bpd": 80 + 10 * k, "gas_mcfd": 250 + 25 * k}
                          for k, w in enumerate(f["wells"])],
                "tank": {"id": f["tank"], "capacity_bbl": 700 + 100 * i},
                "pump": {"id": f["pump"], "max_flow_bpd": 2000 + 100 * i, "start_level_pct": 75, "stop_level_pct": 25},
                "pipeline": pipe})
            fops[f["id"]] = {"response_min": 40 + 10 * i, "cycle_min": 180 + 15 * i}
            series += [(f["id"], "inflow_bpd"), (f["tank"], "level_pct"), (f["pump"], "status"),
                       (f["pump"], "flow_bpd"), (f["pump"], "discharge_psi"), (f["pipeline"], "receive_flow_bpd"),
                       (f["pipeline"], "pressure_psi")]
            series += [(w, m) for w in f["wells"] for m in ("water_bpd", "status")]
        net["systems"].append(out)
        ops["systems"][s["id"]] = {"emergency_fleet_size": s.get("fleet", 2), "facilities": fops}
    if ghost:
        series += [ghost] if isinstance(ghost[0], str) else list(ghost)
    (folder / "network.json").write_text(json.dumps(net, indent=1), encoding="utf-8")
    (folder / "ops.json").write_text(json.dumps(ops, indent=1), encoding="utf-8")
    lines = ["timestamp,ts_unix,asset_id,metric,value"]
    for k in range(3):
        t = T0 + 300 * k
        stamp = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines += [f"{stamp},{t},{a},{m},{50.0 + k}" for a, m in series]
    (folder / "telemetry.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return load(folder)


def tiny_systems(n: int, facilities: int = 1, first: int = 6) -> list[dict]:
    """n systems S<first>.. with `facilities` facilities each, spread around (33, -103)."""
    out = []
    for i in range(n):
        sid, lat, lon = f"S{first + i}", 33.0 + 0.3 * i, -103.0 + 0.3 * i
        out.append({"id": sid, "center": (lat, lon), "yard": (lat - 0.2, lon - 0.2), "fleet": 2,
                    "disposal": (f"{sid}_DISPOSAL", lat + 0.01, lon), "facilities": [
                        {"id": f"{sid}_FACILITY_{k}", "lat": lat + 0.02 * math.cos(k), "lon": lon + 0.03 * math.sin(k),
                         "tank": f"{sid}_TANK_{k}", "pump": f"{sid}_PUMP_{k}", "pipeline": f"{sid}_PIPELINE_{k}",
                         "wells": [f"{sid}_WELL_{k}_1", f"{sid}_WELL_{k}_2"]} for k in range(facilities)]})
    return out


# ---------- fixtures ----------
@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def jsonschema_mod():
    return pytest.importorskip("jsonschema")


@pytest.fixture(scope="module")
def validator(schema, jsonschema_mod):
    jsonschema_mod.Draft202012Validator.check_schema(schema)
    return jsonschema_mod.Draft202012Validator(schema)


@pytest.fixture(scope="module")
def no_bytecode():
    """Import other lanes' modules without writing __pycache__ into their folders."""
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    yield
    sys.dont_write_bytecode = saved


@pytest.fixture(scope="module")
def team(no_bytecode):
    """scenario/validate_blueprint.py: the team's schema-plus-cross-reference check."""
    spec = importlib.util.spec_from_file_location("_team_validate_blueprint", REPO / "scenario" / "validate_blueprint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _from_blender(mod) -> bool:
    f = getattr(mod, "__file__", None)
    try:
        return bool(f) and Path(f).resolve().parent == BLENDER_DIR.resolve()
    except (OSError, ValueError):
        return False


@pytest.fixture(scope="module")
def gen(no_bytecode):
    """The generator's plain-Python modules (blender/blueprint.py, layout.py, names.py, siting.py,
    terrain.py), imported read-only. blender/ stays first on sys.path while this file runs, so the
    generator's own lazy imports (layout.plan imports siting) keep working; afterwards every module
    loaded from blender/ is dropped and anything it shadowed is put back, so no other test sees them."""
    if not (BLENDER_DIR / "layout.py").is_file():
        pytest.skip("blender/ is not in this checkout")
    before, path = dict(sys.modules), list(sys.path)
    for m in GENERATOR_MODULES:
        sys.modules.pop(m, None)
    sys.path.insert(0, str(BLENDER_DIR))
    try:
        yield types.SimpleNamespace(**{m: importlib.import_module(m) for m in GENERATOR_MODULES})
    finally:
        sys.path[:] = path
        for name, mod in list(sys.modules.items()):
            if _from_blender(mod):
                del sys.modules[name]
        sys.modules.update({k: v for k, v in before.items() if k not in sys.modules})


@pytest.fixture(scope="module")
def synth(tmp_path_factory) -> Dataset:
    return write_dataset(tmp_path_factory.mktemp("synthetic") / "net", SYNTH, ghost=GHOST)


def sponsor_datasets(history: bool) -> list[Dataset]:
    """The datasets under FREEBOARD_EOG_DIR inside history/ (history=True) or outside it, such as
    the test/ folder the organizers add before judging. Skips when there are none."""
    raw = os.environ.get("FREEBOARD_EOG_DIR", "").strip()
    if not raw or not Path(raw).is_dir():
        pytest.skip("FREEBOARD_EOG_DIR is not set to the sponsor data")
    found = [d for d in discover(Path(raw)) if d["id"].startswith("history/") == history]
    if not found:
        pytest.skip("no history/ datasets under FREEBOARD_EOG_DIR" if history else
                    "no dataset outside history/ yet (the organizers add test/ before judging)")
    return [load(d["path"], d["id"]) for d in found]


@pytest.fixture(scope="module")
def history() -> list[Dataset]:
    return sponsor_datasets(history=True)


@pytest.fixture(scope="module")
def others() -> list[Dataset]:
    return sponsor_datasets(history=False)


# ---------- helpers ----------
def all_blueprints(ds: Dataset) -> list[tuple[str, dict]]:
    """Every per-system blueprint, plus the whole network when blueprint v1 can hold it."""
    out = [(sid, B.to_blueprint(ds, sid)) for sid in ds.systems]
    try:
        out.append(("all", B.to_blueprint(ds)))
    except B.BlueprintError:
        pass
    return out


def check_valid(bp: dict, validator, team, gen, allowed_warnings=()) -> None:
    """Valid by the schema, the team's cross checks, the generator's check and the generator's layout."""
    text = json.dumps(bp, allow_nan=False)                      # plain JSON, no NaN or infinity
    errors = sorted(validator.iter_errors(bp), key=lambda e: list(e.absolute_path))
    assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors[:5]]
    assert team.check(bp, validator) == []
    g, errs, warns = gen.blueprint.check(json.loads(text))
    assert errs == []
    assert [w for w in warns if w not in allowed_warnings] == []
    plan = gen.layout.plan(g)
    assert plan["errors"] == [] and plan["warnings"] == []
    longest = max(gen.names.expected(g), key=len)
    assert len(longest) <= B.BLENDER_NAME_MAX, longest


def project(lat, lon, origin):
    k = math.pi / 180 * EARTH_R
    return (lon - origin[1]) * k * math.cos(math.radians(origin[0])), (lat - origin[0]) * k


def sites(ds: Dataset, bp: dict) -> list[dict]:
    """The positioned sites of a blueprint with their real coordinates."""
    out = []
    for t in bp["tanks"]:
        f = ds.facilities[t["_facility"]]
        out.append({"key": t["_facility"], "kind": "facility", "x": t["x"], "y": t["y"], "lat": f.lat, "lon": f.lon})
    for s in bp["disposal_wells"]:
        sysm = next(v for v in ds.systems.values() if v.disposal_id == s["_dataset_id"])
        out.append({"key": s["_dataset_id"], "kind": "disposal", "x": s["x"], "y": s["y"],
                    "lat": sysm.disposal_lat, "lon": sysm.disposal_lon})
    for y in bp.get("yards", []):
        sysm = ds.systems[bp["_source"]["systems"][0]]
        out.append({"key": y["id"], "kind": "truck_yard", "x": y["x"], "y": y["y"],
                    "lat": sysm.truck_yard[0], "lon": sysm.truck_yard[1]})
    return out


def rect(gen, kind, x, y):
    """The generator's own footprint for a site (blender/layout.py plan() and _site())."""
    lay = gen.layout
    if kind == "facility":
        return lay.tank_frame(x, y).rect(*lay.FOOTPRINT)
    if kind == "disposal":
        return lay.Frame(x, y, y < 0).rect(-13, 13, -11, 11)
    return lay.Frame(x, y, y > 0).rect(-22, 22, -12, 12)


def check_layout(ds: Dataset, bp: dict, gen) -> None:
    """Real geography kept, one scale, nothing on the road, no overlaps, a clear frontage for every site."""
    lay = bp["_layout"]
    origin, scale = tuple(lay["origin"]), lay["scale"]
    moved = {n.split(" ")[0] for n in lay["notes"]}
    pts = sites(ds, bp)
    for p in pts:
        p["e"], p["n"] = project(p["lat"], p["lon"], origin)
        half, depth, default = B.SITES[p["kind"]]
        p["half"], p["side"] = half, (1 if p["n"] > 0 else -1 if p["n"] < 0 else default)
        assert (p["y"] > 0) == (p["side"] > 0), p                      # its side of the road, by latitude
        assert abs(p["y"] - (scale * p["n"] + p["side"] * depth)) < 0.02, p
        assert abs(p["y"]) >= depth - 0.01
        assert max(abs(p["x"]), abs(p["y"])) <= B.MAX_COORD
        assert scale * max(abs(p["e"]), abs(p["n"])) <= B.FIELD_HALF_M + 0.01   # _layout.scale is rounded
        if p["key"] not in moved:
            assert abs(p["x"] - scale * p["e"]) < 0.02, p              # exactly the scaled projection
    for i, a in enumerate(pts):
        for b in pts[i + 1:]:
            assert not gen.layout.overlaps(rect(gen, a["kind"], a["x"], a["y"]), rect(gen, b["kind"], b["x"], b["y"]))
            if a["side"] != b["side"]:
                continue
            lo, hi = (a, b) if a["x"] <= b["x"] else (b, a)
            assert (hi["x"] - hi["half"]) - (lo["x"] + lo["half"]) >= B.GAP_M - 0.05, (a["key"], b["key"])
            if abs(a["e"] - b["e"]) > 1e-6:
                assert (a["x"] - b["x"]) * (a["e"] - b["e"]) > 0, (a["key"], b["key"])   # east-west order kept


def with_real_ids(bp: dict) -> dict:
    """The blueprint with its dataset ids put back, references included: every id that is capital
    letters, digits and underscores (an id like s8-well.1 can't be a blueprint id under any schema)."""
    back = {v: k for k, v in bp["_id_map"].items() if re.fullmatch(B.ANY_ID[1:-1], k)}
    out = copy.deepcopy(bp)
    for key in ("tanks", "pumps", "valves", "wells", "disposal_wells", "trucks", "yards"):
        for a in out.get(key, []):
            a["id"] = back.get(a["id"], a["id"])
            for ref in ("tank_id", "pump_id", "swd_id", "valve_id", "home_id"):
                if ref in a:
                    a[ref] = back.get(a[ref], a[ref])
            if "connects" in a:
                a["connects"] = [back.get(c, c) for c in a["connects"]]
            for k in ("minutes_to", "_cycle_min"):
                if k in a:
                    a[k] = {back.get(t, t): v for t, v in a[k].items()}
    return out


def pointer(doc, ptr: str):
    for part in ptr.lstrip("/").split("/"):
        doc = doc[part.replace("~1", "/").replace("~0", "~")]
    return doc


def leaf_errors(errors) -> list:
    """Schema errors down to the failing keyword. A DISCHARGE valve's oneOf counts its own branch only."""
    out = []
    for e in errors:
        if e.validator == "oneOf" and isinstance(e.instance, dict) and e.instance.get("kind") == "DISCHARGE":
            out += leaf_errors([c for c in e.context if c.relative_schema_path and c.relative_schema_path[0] == 0])
        elif e.validator in ("oneOf", "anyOf", "allOf") and e.context:
            out += leaf_errors(e.context)
        else:
            out.append(e)
    return out


def id_targets(kinds=BLOCKED) -> dict[str, str]:
    """Schema pointer -> pattern for every rule ID_RULES names for these kinds, refs included."""
    out = {}
    for kind in kinds:
        rule = B.ID_RULES[kind]
        out[rule["rule"]] = rule["pattern"]
        out.update({r["rule"]: r["pattern"] for r in rule.get("refs", [])})
    return out


def patched_schema(schema: dict, pointers) -> dict:
    """SCHEMA_CHANGE["ids"] applied at the given pattern pointers."""
    out = copy.deepcopy(schema)
    for ptr in pointers:
        pointer(out, ptr.rsplit("/", 1)[0])["pattern"] = B.ANY_ID
    return out


def check_real_ids(bp: dict, schema: dict, jsonschema_mod, gen) -> set[str]:
    """The blueprint with its dataset ids put back fails only ID_RULES patterns, passes once all of them
    are changed as SCHEMA_CHANGE["ids"] says, fails again on any one left out, and already passes the
    generator's check. Returns the rule pointers the real ids hit."""
    v, targets = jsonschema_mod.Draft202012Validator, id_targets()
    real = with_real_ids(bp)
    leaves = leaf_errors(v(schema).iter_errors(real))
    assert leaves and all(e.validator == "pattern" for e in leaves)
    seen = {e.validator_value for e in leaves}
    assert seen <= set(targets.values())
    assert list(v(patched_schema(schema, targets)).iter_errors(real)) == []
    assert gen.blueprint.check(real)[1] == []
    hit = {p for p, pattern in targets.items() if pattern in seen}
    for ptr in hit:
        rest = leaf_errors(v(patched_schema(schema, [p for p in targets if p != ptr])).iter_errors(real))
        assert {e.validator_value for e in rest} == {targets[ptr]}, ptr
    return hit


def check_assets(ds: Dataset, assets: list[dict]) -> None:
    ids = [a["id"] for a in assets]
    counts = Counter(ids)
    assert [i for i, c in counts.items() if c > 1] == []
    telemetry = {a for a, _ in ds.series_keys()}
    assert all(counts[a] == 1 for a in telemetry)                  # every telemetry asset id exactly once
    assert all(counts[a] == 1 for a in ds.assets)                  # and every network asset
    by_id = {a["id"]: a for a in assets}
    want = {"system": len(ds.systems), "facility": len(ds.facilities), "well": len(ds.wells),
            "tank": len(ds.facilities), "pump": len(ds.facilities), "pipeline": len(ds.facilities),
            "disposal": len(ds.systems), "truck_yard": len(ds.systems),
            "truck": sum(s.fleet_size for s in ds.systems.values()),
            "unknown": len(telemetry - set(ds.assets))}
    assert Counter(a["kind"] for a in assets) == Counter({k: v for k, v in want.items() if v})
    keys = {"id", "kind", "system", "facility", "label", "lat", "lon", "latlon_from", "x_m", "y_m", "links", "fed_by",
            "blueprint_id", "node", "metrics", "synthetic"}
    for a in assets:
        assert keys <= set(a), a["id"]
        assert a["metrics"] == sorted(m for x, m in ds.series_keys() if x == a["id"])
        for t in a["links"]:
            assert a["id"] in by_id[t]["fed_by"]
        for s in a["fed_by"]:
            assert a["id"] in by_id[s]["links"]
        if a["kind"] == "unknown":
            continue
        assert isinstance(a["lat"], float) and isinstance(a["lon"], float)
        assert isinstance(a["x_m"], float) and isinstance(a["y_m"], float)
        assert a["synthetic"] == (a["kind"] in ("truck_yard", "truck"))
    for f in ds.facilities.values():
        assert by_id[f.tank_id]["links"] == [f.pump_id]
        assert by_id[f.pump_id]["links"] == [f.pipeline_id]
        assert by_id[f.pipeline_id]["links"] == [ds.systems[f.system].disposal_id]
        assert by_id[f.tank_id]["fed_by"] == f.well_ids
        for w in f.wells:
            assert by_id[w.id]["links"] == [f.tank_id] and by_id[w.id]["facility"] == f.id


def check_positions_match_generator(ds: Dataset, gen) -> None:
    """Where field_assets says each asset is, the generator builds it (blender/layout.py plan())."""
    for sid in ds.systems:
        bp = B.to_blueprint(ds, sid)
        plan = gen.layout.plan(gen.blueprint.check(bp)[0])
        at = {a["blueprint_id"]: (a["x_m"], a["y_m"]) for a in B.field_assets(ds, sid) if a["blueprint_id"]}
        built = {p["tank"]: p["frame"].world(0, gen.layout.TANK_Y) for p in plan["pads"]}
        built.update({x["id"]: x["pos"] for key in ("pumps", "wells", "trucks") for x in plan[key]})
        built.update({s["id"]: (s["frame"].x, s["frame"].y) for s in plan["swds"]})
        built[plan["yard"]["id"]] = (plan["yard"]["frame"].x, plan["yard"]["frame"].y)
        valves = {v["id"] for v in bp["valves"]}
        assert set(at) - valves == set(built)
        for bid, (x, y) in built.items():
            assert abs(at[bid][0] - x) < 0.011 and abs(at[bid][1] - y) < 0.011, (bid, at[bid], (x, y))


# ---------- synthetic: always run ----------
def test_synthetic_blueprints_validate(synth, validator, team, gen):
    got = all_blueprints(synth)
    assert [s for s, _ in got] == ["S6", "S7", "S8", "all"]
    for sid, bp in got:
        check_valid(bp, validator, team, gen, allowed_warnings=(
            () if sid != "all" else ("no yard in the blueprint; trucks park south of the road with no YARD asset",)))
        check_layout(synth, bp, gen)
    whole = got[-1][1]
    assert [o["rule"] for o in whole["_omitted"]] == ["/properties/yards/maxItems", "/properties/trucks/maxItems"]
    assert [o["count"] for o in whole["_omitted"]] == [3, 9]
    assert "trucks" not in whole and "yards" not in whole


def test_synthetic_numbers_come_from_the_files(synth):
    bp = B.to_blueprint(synth, "S6")
    a, b = synth.facilities["S6_FACILITY_A"], synth.facilities["S6_FACILITY_B"]
    tank = bp["tanks"][0]
    assert (tank["capacity_bbl"], tank["shutdown_pct"], tank["warning_pct"], tank["pump_low_pct"]) == (700, 95, 80, 25)
    assert bp["pumps"][1]["rate_bbl_hr"] == pytest.approx(b.max_flow_bpd / 24, abs=1e-6)
    w = bp["wells"][1]
    assert w["_dataset_id"] == "S6_WELL_A2"
    assert w["water_bbl_hr"] == pytest.approx(350 / 24, abs=1e-6)
    assert w["lost_usd_hr"] == pytest.approx((90 * 70 + 275 * 2.5) / 24, abs=1e-6)
    # B's pipeline has no max_bpd, so its pump's flow counts toward the disposal well's capacity
    assert bp["disposal_wells"][0]["max_bbl_hr"] == pytest.approx((a.pipeline_max_bpd + b.max_flow_bpd) / 24, abs=1e-6)
    assert "_max_bpd" in bp["valves"][0] and "_max_bpd" not in bp["valves"][1]
    assert len(bp["trucks"]) == 6                      # fleet of 8, schema maximum 6
    assert bp["_omitted"][0]["ids"] == ["TRUCK_S6_7", "TRUCK_S6_8"]
    truck = bp["trucks"][0]
    assert (truck["capacity_bbl"], truck["home_id"]) == (120, "YARD_S6")
    assert truck["minutes_to"] == {"TANK_S6_TANK_A": 40, "TANK_S6_TANK_B": 50}
    assert truck["_cycle_min"] == {"TANK_S6_TANK_A": 180, "TANK_S6_TANK_B": 195}
    assert bp["_trucking"] == {"system": "S6", "fleet_size": 8, "truck_capacity_bbl": 120, "cost_per_trip": 800}
    assert "trucks" not in B.to_blueprint(synth, "S7")  # a fleet of 0
    assert all(v["default"] == "OPEN" and v["kind"] == "DISCHARGE" for v in bp["valves"])
    assert bp["site"] == {"name": "System S6", "lat": 35.0, "lon": -100.0, "width_m": bp["site"]["width_m"],
                          "timezone": "UTC"}


def test_synthetic_ids_kept_prefixed_or_made_unique(synth):
    ids = B.id_map(synth)
    assert ids["TANK_7B"] == "TANK_7B" and ids["SWD_7"] == "SWD_7"          # already fit: kept
    assert ids["S6_TANK_A"] == "TANK_S6_TANK_A" and ids["S6_PIPELINE_A"] == "VALVE_D_S6_PIPELINE_A"
    assert ids["S6_DISPOSAL"] == "SWD_S6_DISPOSAL" and ids["S6_WELL_A1"] == "WELL_S6_WELL_A1"
    assert ids["s8-well.1"] == "WELL_S8_WELL_1"                                # upper-cased, punctuation to _
    assert ids["TANK_X"] == "TANK_X" and ids["X"] == "TANK_X_2"                # X's prefixed id was taken
    s8 = B.to_blueprint(synth, "S8")
    tank_rule = next(r for r in s8["_id_rules"] if r["kind"] == "tank")
    assert tank_rule["made_unique"] == ["X"] and (tank_rule["renamed"], tank_rule["kept"]) == (1, 1)
    for sid, bp in all_blueprints(synth):
        listed = [a["id"] for key in ("tanks", "pumps", "valves", "wells", "disposal_wells", "trucks", "yards")
                  for a in bp.get(key, [])]
        assert len(listed) == len(set(listed))
        # every blueprint id is a dataset id's (in _id_map) or synthesized (in _synthesized), never both
        mapped, made = set(bp["_id_map"].values()), {s["id"] for s in bp["_synthesized"]}
        assert set(listed) == mapped | made and not mapped & made
        assert bp["_id_map"] == {k: ids[k] for k in bp["_id_map"]}             # the same id in every scope
        for kind, key in (("tank", "tanks"), ("pump", "pumps"), ("pipeline", "valves"), ("well", "wells"),
                          ("disposal", "disposal_wells")):
            rule = B.ID_RULES[kind]
            for a in bp[key]:
                real = a["_dataset_id"]
                assert bp["_id_map"][real] == a["id"]
                want = real if re.fullmatch(rule["pattern"][1:-1], real) else rule["prefix"] + B._sanitize(real)
                assert a["id"] == want or (real == "X" and a["id"] == want + "_2"), (real, a["id"])
        assert {r["kind"] for r in bp["_id_rules"]} >= {"facility", "system"}


def test_synthesized_ids_avoid_every_dataset_id(tmp_path, validator, team, gen):
    # the telemetry names an asset network.json doesn't, with the id the yard (and a truck) would get
    ds = write_dataset(tmp_path / "taken", tiny_systems(1), ghost=[("YARD_S6", "level_pct"), ("TRUCK_S6_1", "status")])
    bp = B.to_blueprint(ds, "S6")
    check_valid(bp, validator, team, gen)
    assert [y["id"] for y in bp["yards"]] == ["YARD_S6_2"]
    assert [t["id"] for t in bp["trucks"]] == ["TRUCK_S6_1_2", "TRUCK_S6_2"]
    assets = B.field_assets(ds)
    check_assets(ds, assets)
    assert [a["kind"] for a in assets if a["id"] in ("YARD_S6", "TRUCK_S6_1")] == ["unknown", "unknown"]


def test_an_id_used_twice_raises(tmp_path):
    systems = tiny_systems(1)
    fac = systems[0]["facilities"][0]
    fac["wells"] = [fac["tank"]] + fac["wells"][1:]          # a well with its tank's id
    ds = write_dataset(tmp_path / "twice", systems)
    for fn in (B.to_blueprint, B.field_assets, B.id_map):
        with pytest.raises(B.BlueprintError, match="S6_TANK_0 is used twice in network.json"):
            fn(ds)


def test_synthetic_field_assets(synth, gen):
    assets = B.field_assets(synth)
    check_assets(synth, assets)
    ghost = [a for a in assets if a["id"] == GHOST[0]]
    assert len(ghost) == 1 and ghost[0]["kind"] == "unknown" and ghost[0]["metrics"] == [GHOST[1]]
    trucks = [a for a in assets if a["kind"] == "truck" and a["system"] == "S6"]
    assert len(trucks) == 8 and [t["blueprint_id"] is not None for t in trucks] == [True] * 6 + [False] * 2
    assert sum(len(B.field_assets(synth, sid)) for sid in synth.systems) == len(assets) - 1   # all but the ghost
    s8 = {a["id"]: a for a in B.field_assets(synth, "S8")}
    yard = s8["YARD_S8"]                       # S8 has no truck_yard or center: both fall back to its disposal site
    assert (yard["lat"], yard["lon"]) == (34.0, -102.0) and yard["y_m"] < 0 < s8["S8_DISPOSAL"]["y_m"]
    pipe = s8["S8_PIPELINE_A"]
    assert pipe["node"] == "VALVE_D_S8_PIPELINE_A"
    assert pipe["pipes"] == ["PIPE_PUMP_S8_PUMP_A_VALVE_D_S8_PIPELINE_A", "PIPE_VALVE_D_S8_PIPELINE_A_TRUNK"]
    assert pipe["path_m"][0] == [s8["S8_PUMP_A"]["x_m"], s8["S8_PUMP_A"]["y_m"]]
    assert pipe["path_m"][1] == [s8["S8_DISPOSAL"]["x_m"], s8["S8_DISPOSAL"]["y_m"]]
    assert s8["S8_FACILITY_A"]["node"] == "PAD_X_2" and s8["S8_FACILITY_B"]["node"] == "PAD_X"
    check_positions_match_generator(synth, gen)


def test_moves_are_reported(synth):
    notes = {sid: B.to_blueprint(synth, sid)["_layout"]["notes"] for sid in synth.systems}
    # S7's two pads are 45 m apart east-west, both south of the road: the capped scale can't part them
    assert {n.split(" ")[0] for n in notes["S7"]} == {"S7_FACILITY_A", "S7_FACILITY_B"}
    assert notes["S8"] == []
    s7 = {t["_facility"]: t for t in B.to_blueprint(synth, "S7")["tanks"]}
    assert s7["S7_FACILITY_B"]["x"] < s7["S7_FACILITY_A"]["x"]           # B is the western one, as in the file


def test_whole_network_past_the_schema_raises(tmp_path, validator, team, gen):
    ds = write_dataset(tmp_path / "five", tiny_systems(5))
    with pytest.raises(B.BlueprintError) as err:
        B.to_blueprint(ds)
    assert err.value.rules == [{"rule": "/properties/disposal_wells/maxItems", "max": 4, "count": 5,
                                "what": "disposal sites (one per system)"}]
    assert "to_blueprint(ds, system)" in str(err.value)
    for sid in ds.systems:
        check_valid(B.to_blueprint(ds, sid), validator, team, gen)
    assert B.to_blueprint(write_dataset(tmp_path / "four", tiny_systems(4)))["_source"]["systems"] == [
        "S6", "S7", "S8", "S9"]


def test_one_system_network_whole_equals_system(tmp_path, validator, team, gen):
    ds = write_dataset(tmp_path / "one", tiny_systems(1, facilities=3))
    whole, one = B.to_blueprint(ds), B.to_blueprint(ds, "S6")
    check_valid(whole, validator, team, gen)
    skip = {"name", "site", "_source"}
    assert {k: v for k, v in whole.items() if k not in skip} == {k: v for k, v in one.items() if k not in skip}


def test_too_many_facilities_in_one_system(tmp_path):
    ds = write_dataset(tmp_path / "big", tiny_systems(1, facilities=21))
    with pytest.raises(B.BlueprintError) as err:
        B.to_blueprint(ds, "S6")
    assert err.value.rules[0]["rule"] == "/properties/tanks/maxItems"
    assert sum(a["kind"] == "facility" for a in B.field_assets(ds)) == 21


@pytest.mark.parametrize("limits, ok, warned", [((80, 100), True, False), ((120, 95), True, True),
                                                ((80, 0), False, False), ((80, 101), False, False)])
def test_tank_limits_outside_the_schema(tmp_path, validator, limits, ok, warned):
    ds = write_dataset(tmp_path / "limits", tiny_systems(1), limits=limits)
    if not ok:
        with pytest.raises(B.BlueprintError, match="shutdown"):
            B.to_blueprint(ds, "S6")
        return
    bp = B.to_blueprint(ds, "S6")
    assert list(validator.iter_errors(bp)) == []
    assert ("warning_pct" not in bp["tanks"][0]) == warned
    assert any("warning_pct" in w for w in bp.get("_warnings", [])) == warned


def rewrite_ops(folder: Path, change) -> Dataset:
    """The dataset at folder, loaded again after change(ops) edits its ops.json."""
    path = folder / "ops.json"
    ops = json.loads(path.read_text(encoding="utf-8"))
    change(ops)
    path.write_text(json.dumps(ops), encoding="utf-8")
    return load(folder)


@pytest.mark.parametrize("field, value", [("truck_capacity_bbl", 0), ("response_min", -5)])
def test_trucks_outside_the_schema_are_left_out(tmp_path, validator, team, gen, field, value):
    folder = tmp_path / "trucks"
    write_dataset(folder, tiny_systems(1, facilities=2))

    def change(ops):
        if field == "truck_capacity_bbl":
            ops["emergency_trucking"][field] = value
        else:
            ops["systems"]["S6"]["facilities"]["S6_FACILITY_1"][field] = value

    ds = rewrite_ops(folder, change)
    bp = B.to_blueprint(ds, "S6")
    check_valid(bp, validator, team, gen)
    assert "trucks" not in bp and [y["id"] for y in bp["yards"]] == ["YARD_S6"]
    assert [w for w in bp["_warnings"] if "trucks are left out" in w] != []
    trucks = [a for a in B.field_assets(ds, "S6") if a["kind"] == "truck"]      # field_assets agrees
    assert len(trucks) == 2 and all(t["blueprint_id"] is None and t["node"] is None for t in trucks)
    check_positions_match_generator(ds, gen)


@pytest.mark.parametrize("where, value, rule", [("center", (95.0, -100.0), "/$defs/site/properties/lat"),
                                                ("center", (float("nan"), -100.0), "/$defs/site/properties/lat"),
                                                ("yard", (33.0, float("inf")), "/$defs/coord"),
                                                ("disposal", (-91.0, -103.0), "/$defs/coord"),
                                                ("facility", (33.0, 181.0), "/$defs/coord")])
def test_points_off_the_map_raise(tmp_path, where, value, rule):
    systems = tiny_systems(1)
    s = systems[0]
    if where == "disposal":
        s["disposal"] = (s["disposal"][0], *value)
    elif where == "facility":
        s["facilities"][0]["lat"], s["facilities"][0]["lon"] = value
    else:
        s[where] = value
    ds = write_dataset(tmp_path / "off", systems)
    for fn in (B.to_blueprint, B.field_assets):           # never a NaN or far-off position, in either
        for scope in ("S6", None):
            with pytest.raises(B.BlueprintError, match="not a place on Earth") as err:
                fn(ds, scope)
            assert err.value.rules[0]["rule"] == rule


def test_unknown_system(synth):
    for fn in (B.to_blueprint, B.field_assets, B.id_map):
        with pytest.raises(B.BlueprintError, match="No system named 'S1'"):
            fn(synth, "S1")


def test_same_for_every_time_travel_view(synth):
    view = synth.at(T0)
    assert view.now == T0 and synth.now == T0 + 600
    assert B.field_assets(view) == B.field_assets(synth)
    for sid in synth.systems:
        assert B.to_blueprint(view, sid) == B.to_blueprint(synth, sid)


def test_schema_rules_are_the_files_rules(schema, gen):
    for kind, r in B.ID_RULES.items():
        assert pointer(schema, r["rule"]) == r["pattern"], kind
        assert r["pattern"] == f"^{r['prefix']}[A-Z0-9_]+$"
        for ref in r.get("refs", []):
            assert pointer(schema, ref["rule"]) == ref["pattern"], (kind, ref)
            assert re.fullmatch(ref["pattern"][1:-1], r["prefix"] + "N1_X")        # the mapped id passes it too
    assert pointer(schema, "/properties/pumps/items/$ref") == "#/$defs/pump"
    assert pointer(schema, "/$defs/pump/properties/valve_id/$ref") == "#/$defs/valve_id"
    assert len(id_targets()) == 6 and all(p.split("/")[-1] == "pattern" for p in id_targets())
    for key, r in B.COUNT_RULES.items():
        assert pointer(schema, r["rule"]) == r["max"], key
        assert gen.blueprint.COUNT_LIMITS[key][1] == r["max"]        # the generator caps them the same
    assert pointer(schema, B.NO_LIST_RULE) is False
    assert pointer(schema, "/$defs/asset_id/pattern") == B.ANY_ID == gen.blueprint.ID_RE.pattern
    assert pointer(schema, "/$defs/coord/maximum") == B.MAX_COORD == gen.blueprint.MAX_COORD


def test_generator_facts_match_blender(gen):
    lay = gen.layout
    assert B.PAD_TANK_Y == lay.TANK_Y and B.PAD_PITCH_M == lay.PAD_PITCH
    assert B.PUMP_SLOTS == tuple((x, lay.PUMP_Y) for x in lay.PUMP_SLOTS)
    assert B.WELLS_UP_TO_4 == {k: tuple((x, y) for x, y, _ in v) for k, v in lay.WELLS_UP_TO_4.items()}
    for n in range(0, 9):
        assert B.well_slots(n) == [(x, y) for x, y, _ in lay.well_slots(n)]
    assert B.YARD_SLOTS == lay.YARD_SLOTS
    assert B.SITES["facility"][:2] == (lay.FOOTPRINT[1], lay.TANK_Y) and lay.FOOTPRINT[0] == -lay.FOOTPRINT[1]
    assert B.SITES["disposal"][1] == lay.SWD_Y and B.SITES["truck_yard"][1] == -lay.YARD_Y
    src = Path(lay.__file__).read_text(encoding="utf-8")
    assert "rect(-13, 13, -11, 11)" in src and "rect(-22, 22, -12, 12)" in src   # the site extents mirrored
    assert B.WELLS_PER_TANK == gen.blueprint.WELLS_PER_TANK
    for tid in ("TANK_3", "TANK_N1_TANK_A", "TANK_", "N1_TANK_A"):
        assert B.pad_suffix(tid) == lay.suffix(tid)


def test_real_ids_are_blocked_only_by_the_named_rules(synth, schema, jsonschema_mod, gen):
    hits = {sid: check_real_ids(B.to_blueprint(synth, sid), schema, jsonschema_mod, gen) for sid in synth.systems}
    assert hits["S6"] == set(id_targets())                         # every one of the six blocks an S6 id
    assert "/$defs/swd_id/pattern" not in hits["S7"]               # S7's SWD_7 already fits and is kept


def test_backend_reader_accepts_blueprints(synth):
    from backend.app.scenario import prepare
    for sid, bp in all_blueprints(synth):
        data = prepare(copy.deepcopy(bp), f"blueprint {sid}")
        assert len(data["field"]["tanks"]) == len(bp["tanks"])


def test_module_reads_no_files_clock_or_sponsor_ids():
    src = Path(B.__file__).read_text(encoding="utf-8")
    for banned in ("open(", "read_text", "Path(", "import time", "datetime", "time.time", "os.environ"):
        assert banned not in src, banned
    assert re.search(r"\bS[1-5](?:_|\b)", src) is None


# ---------- the sponsor's data: skipped without FREEBOARD_EOG_DIR ----------
def test_history_blueprints_validate(history, validator, team, gen):
    assert len(history) >= 5
    for ds in history:
        assert len(ds.systems) >= 1
        for sid in ds.systems:
            bp = B.to_blueprint(ds, sid)
            check_valid(bp, validator, team, gen)
            check_layout(ds, bp, gen)
            assert bp["_source"]["dataset"] == ds.id and "_warnings" not in bp
            assert all(a["id"] == B.ID_RULES[k]["prefix"] + a["_dataset_id"]
                       for k, key in (("tank", "tanks"), ("pump", "pumps"), ("pipeline", "valves"),
                                      ("well", "wells"), ("disposal", "disposal_wells")) for a in bp[key])


def test_history_whole_network_is_past_the_schema(history):
    for ds in history:
        if len(ds.systems) <= B.COUNT_RULES["disposal_wells"]["max"]:
            continue
        with pytest.raises(B.BlueprintError) as err:
            B.to_blueprint(ds)
        assert err.value.rules[0]["rule"] == "/properties/disposal_wells/maxItems"
        assert err.value.rules[0]["count"] == len(ds.systems)


def test_history_field_assets(history, gen):
    for ds in history:
        check_assets(ds, B.field_assets(ds))
        check_positions_match_generator(ds, gen)


def test_history_real_ids_need_exactly_the_id_change(history, schema, jsonschema_mod, gen):
    for ds in history:
        for sid in ds.systems:
            assert check_real_ids(B.to_blueprint(ds, sid), schema, jsonschema_mod, gen) == set(id_targets())


def test_other_datasets(others, validator, team, gen):
    """Any dataset outside history/, such as test/ (an unseen network, S6 to S11) once it lands."""
    for ds in others:
        assert len(ds.systems) >= 1
        for sid in ds.systems:
            bp = B.to_blueprint(ds, sid)
            check_valid(bp, validator, team, gen)
            check_layout(ds, bp, gen)
        check_assets(ds, B.field_assets(ds))
        check_positions_match_generator(ds, gen)


def test_history_speed(history):
    ds = max(history, key=lambda d: d.end)
    t = time.perf_counter()
    for sid in ds.systems:
        B.to_blueprint(ds, sid)
    B.field_assets(ds)
    assert time.perf_counter() - t < 0.25
