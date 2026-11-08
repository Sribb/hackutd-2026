"""Tests for the blueprint generator. Run from the repo root:

  python3 blender/test_generator.py           validation tests, every build, and tools/check
  python3 blender/test_generator.py --quick   validation and layout tests only (no Blender)

1. Validation and layout run in plain Python against blueprint.py and layout.py, including
   the blueprint format v1 rules in docs/BLUEPRINT.md.
2. Builds: small, the default (scenario/field.json into blender/), large, the demo kit's
   sample 1 and the ingest draft of sample 2, and three variants made here (given positions,
   extra crossovers, a 20-tank maximum). Each glb's JSON chunk is read to check names, kinds,
   hierarchy, positions, bounding boxes, pipes, and budget.
3. A broken blueprint must exit 1, explain itself in build_report.json, and write no glb.
4. tools/check must PASS its blender line on the default build.

The sample 2 draft comes from ingest live (ingest_bytes on the CSV) when an interpreter with
ingest's requirements is found: this one, $INGEST_PYTHON, or a .venv in the repo or the main
checkout. Otherwise the test uses blender/samples/sample2_draft.json and says so.
"""
import copy
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import blueprint  # noqa: E402
from glb_read import Glb, touch  # noqa: E402
import layout  # noqa: E402
import names  # noqa: E402
import test_parts  # noqa: E402
import test_scenery  # noqa: E402

BLENDER = os.environ.get("BLENDER", "/Applications/Blender.app/Contents/MacOS/Blender")
SAMPLE1 = os.path.join(ROOT, "scenario", "samples", "sample1_pecos_8tank.json")
SAMPLE2_CSV = os.path.join(ROOT, "scenario", "samples", "sample2_delaware_4tank.csv")
SAMPLE2_DRAFT = os.path.join(HERE, "samples", "sample2_draft.json")
FAILS = []


def ok(cond, what):
    print(("PASS  " if cond else "FAIL  ") + what)
    if not cond:
        FAILS.append(what)
    return cond


def load(path):
    with open(path) as f:
        return json.load(f)


def errors_of(data):
    return blueprint.check(data)[1]


def ingest_pythons():
    """Interpreters that may have ingest's requirements, most specific first."""
    cands = [sys.executable, os.environ.get("INGEST_PYTHON"), os.path.join(ROOT, ".venv", "bin", "python")]
    try:   # in a git worktree, the main checkout holds the .venv
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=ROOT, capture_output=True,
                                text=True, timeout=10).stdout.strip()
        if common:
            main = os.path.dirname(os.path.abspath(os.path.join(ROOT, common)))
            cands.append(os.path.join(main, ".venv", "bin", "python"))
    except (OSError, subprocess.SubprocessError):
        pass
    return [c for c in dict.fromkeys(cands) if c and os.path.exists(c)]


def sample2_draft(tmp):
    """The ingest draft of sample 2's CSV: (path, where it came from)."""
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); from ingest import ingest_bytes; "
            "r = ingest_bytes(open(sys.argv[2], 'rb').read(), 'sample2_delaware_4tank.csv'); "
            "json.dump(r['blueprint'], sys.stdout)")
    for py in ingest_pythons():
        p = subprocess.run([py, "-c", code, ROOT, SAMPLE2_CSV], capture_output=True, text=True, timeout=120)
        if p.returncode == 0:
            try:
                draft = json.loads(p.stdout)
            except json.JSONDecodeError:
                continue
            path = os.path.join(tmp, "sample2_draft.json")
            with open(path, "w") as f:
                json.dump(draft, f, indent=1)
            return path, f"live ingest ({py})"
    return SAMPLE2_DRAFT, "blender/samples/sample2_draft.json (ingest is not importable here)"


# ---------- 1. validation and layout ----------

def test_format_v1():
    """docs/BLUEPRINT.md format v1: optional site and connectors, _ keys, drafts with gaps."""
    s1 = load(SAMPLE1)
    bp, errs, warns = blueprint.check(s1)
    ok(not errs, f"sample 1 is a valid blueprint {errs[:2]}")
    ok(bp["site"] and bp["site"]["name"] == "Pecos Ridge lease" and [c["id"] for c in bp["connectors"]],
       "sample 1: site and connectors are read")
    draft = load(SAMPLE2_DRAFT)
    bp, errs, _ = blueprint.check(draft)
    ok(not errs and not any("shutdown_pct" in t for t in draft["tanks"]),
       f"the sample 2 draft builds although it misses numbers {errs[:2]}")

    base = load(os.path.join(ROOT, "scenario", "field.json"))

    def case(change):
        data = copy.deepcopy(base)
        change(data)
        return blueprint.check(data)

    _, errs, warns = case(lambda d: d.update(site="Avon", connectors={"id": "CONN_1"}))
    ok(not errs and any('"site"' in w for w in warns) and any('"connectors"' in w for w in warns),
       "a malformed site or connectors list is a warning, not an error")
    bad_conn = [{"id": "conn x"}, "text", {"id": "TANK_1"}, {"_note": "comment"},
                {"id": "CONN_A", "kind": "mqtt", "signals": [{"asset_id": "TANK_99", "signal": "level_pct"},
                                                              {"asset_id": "PUMP_3", "signal": "running"}]}]
    bp, errs, warns = case(lambda d: d.update(connectors=bad_conn))
    ok(not errs and [c["id"] for c in bp["connectors"]] == ["CONN_A"]
       and bp["connectors"][0]["signals"] == [("PUMP_3", "running")],
       f"bad connectors and signals are skipped with warnings ({len(warns)} warnings)")
    _, errs, _ = case(lambda d: (d.update(connectors=[{"id": "CONN_A", "kind": "sim", "signals": []}],
                                          connections=[{"from": "CONN_A", "to": "TANK_3", "kind": "SIGNAL"}])))
    ok(not errs, f"a connection may end at a connector {errs[:1]}")

    def comments(d):
        d["_note"] = "top-level comment"
        d["tanks"].append({"_note": "a comment entry"})
        d["tanks"][0]["_ingest"] = {"label": "T1", "confidence": 0.5}
        d["trucks"][0]["minutes_to"]["_source"] = "dispatcher"
        d["initial"]["tank_pct"]["_note"] = "levels at 01:47"
    bp, errs, warns = case(comments)
    ok(not errs and len(bp["tanks"]) == 6 and not any("_note" in w for w in warns),
       f"keys starting with _ are ignored everywhere {errs[:1]}")

    def nulls(d):
        d["tanks"][0].update(x=None, y=None, capacity_bbl=None)
        d["valves"][0].update(kind=None)
        d["pumps"][0].update(valve_id=None)
    bp, errs, _ = case(nulls)
    ok(not errs and bp["tanks"][0]["pos"] is None and bp["pumps"][0]["valve_id"] == "VALVE_D_1",
       f"null numbers, positions, and kinds count as missing {errs[:1]}")
    bp, errs, _ = case(lambda d: d["valves"][0].update(pump_id=None, connects=["SWD_1", "PUMP_1"]))
    ok(not errs and bp["pumps"][0]["valve_id"] == "VALVE_D_1", "a discharge valve finds its pump in either end")
    _, errs, warns = case(lambda d: d.update(version=2, extra_list=[]))
    ok(not errs and any("version 2" in w for w in warns) and any("extra_list" in w for w in warns),
       "another format version and unknown keys are warnings")


def test_validation():
    base = load(os.path.join(ROOT, "scenario", "field.json"))
    for path in ("scenario/field.json", "blender/samples/small.json", "blender/samples/large.json"):
        ok(not errors_of(load(os.path.join(ROOT, path))), f"{path} is a valid blueprint")

    def broken(change, expect, what):
        data = copy.deepcopy(base)
        change(data)
        errs = errors_of(data)
        ok(any(expect in e for e in errs), f"rejects {what}: {errs[:1]}")

    broken(lambda d: d["wells"].append(dict(d["wells"][0])), "duplicate ID", "a duplicate ID")
    broken(lambda d: d["pumps"][0].update(tank_id="TANK_99"), "not an asset", "a pump on an unknown tank")
    broken(lambda d: d["wells"][3].update(tank_id="TANK_X"), "not an asset", "a well on an unknown tank")
    broken(lambda d: d["valves"][6].update(connects=["TANK_3", "TANK_77"]), "not an asset",
           "a crossover to an unknown tank")
    broken(lambda d: d["valves"][6].update(connects=["TANK_3", "PUMP_4"]), "exactly two tanks",
           "a crossover to a pump")
    broken(lambda d: d["tanks"][0].update(id="tank 1"), "must match", "a lowercase ID with a space")
    broken(lambda d: d["trucks"][0].update(id="1TRUCK"), "must match", "an ID starting with a digit")
    broken(lambda d: d["trucks"][0].update(home_id="YARD_9"), "not an asset", "a truck with an unknown home")
    broken(lambda d: d["tanks"][2].update(x=10), "only one of x and y", "x without y")
    broken(lambda d: d["tanks"][2].update(x="ten", y=50), "must be numbers", "a text position")
    broken(lambda d: d.update(disposal_wells=[]), "disposal wells", "zero disposal wells")
    broken(lambda d: d["wells"].extend({"id": f"WELL_X{i}", "tank_id": "TANK_1"} for i in range(5)),
           "0 to 8 per tank", "nine wells on one tank")
    broken(lambda d: d["tanks"].extend({"id": f"TANK_{i}"} for i in range(7, 22)), "1 to 20 tanks", "21 tanks")
    broken(lambda d: d["trucks"].extend({"id": f"TRUCK_{i}"} for i in range(3, 8)), "0 to 6 trucks", "7 trucks")
    broken(lambda d: d["valves"].append({"id": "VALVE_D_9", "kind": "DISCHARGE", "pump_id": "PUMP_1"}),
           "belongs to", "two discharge valves on one pump")
    broken(lambda d: d["valves"][0].update(kind="GLOBE"), "not supported", "an unknown valve kind")
    with tempfile.TemporaryDirectory() as tmp:
        bad = os.path.join(tmp, "bad.json")
        with open(bad, "w") as f:
            f.write('{"tanks": [')
        ok(blueprint.load(bad)[1] and "not valid JSON" in blueprint.load(bad)[1][0], "rejects broken JSON")


def test_layout():
    bp = blueprint.check(load(os.path.join(ROOT, "scenario", "field.json")))[0]
    L = layout.plan(bp)
    xs = [p["frame"].x for p in L["pads"]]
    ok(xs == [-137.5, -82.5, -27.5, 27.5, 82.5, 137.5] and L["road"] == (-225.0, 225.0),
       "default layout keeps today's pad positions and 450 m road")
    big = blueprint.check(load(os.path.join(HERE, "samples", "large.json")))[0]
    LB = layout.plan(big)
    ok(LB["rows"] == 2 and sum(p["frame"].flip for p in LB["pads"]) == 5, "12 tanks lay out in two rows (7 + 5)")
    ok(LB["road"][1] - LB["road"][0] > L["road"][1] - L["road"][0], "the road grows with the tank count")
    ok({x["id"]: x["style"] for x in LB["crossovers"]} == {"VALVE_XO_34": "straight", "VALVE_XO_2_9": "rack"},
       "neighbors get a straight crossover, tanks across the road get a rack")
    yard_x = LB["yard"]["frame"].x
    ok(all(abs(p["frame"].x - yard_x) > 30 for p in LB["pads"] if p["frame"].flip), "the yard has its own slot")


# ---------- 2. builds ----------

def blender(args, script=os.path.join(HERE, "build_field.py")):
    cmd = [BLENDER, "--background", "--factory-startup", "--python", script, "--"]
    return subprocess.run(cmd + args, cwd=ROOT, capture_output=True, text=True, timeout=600)


def check_rigs(label, glb, bp, data):
    """Hero equipment (#100): pumpjack pivots unchanged, pitman and bridle hang from the beam,
    and every valve says whether it is manual or remote, as the blueprint does."""
    def local(name):
        nd = glb.nodes[glb.idx(name)]
        return nd.get("translation", (0, 0, 0)), nd.get("rotation", (0, 0, 0, 1))
    pivots, rigs = True, True
    for w in bp["wells"]:
        (bx, by, bz), _ = local(w["id"] + "_beam")
        (cx, cy, cz), _ = local(w["id"] + "_crank")
        pivots &= max(abs(bx), abs(by - 4.1), abs(bz)) < 1e-4            # three.js (x, height, -y)
        pivots &= max(abs(cx), abs(cy - 1.6), abs(cz + 2.7)) < 1e-4
        for part in ("_pitman", "_bridle"):
            i = glb.by_name.get(w["id"] + part, [None])[0]
            rigs &= i is not None and glb.parent.get(i) == glb.idx(w["id"] + "_beam")
            rigs &= i is not None and max(abs(q) for q in glb.nodes[i].get("rotation", (0, 0, 0, 1))[:3]) < 1e-6
    ok(pivots, f"{label}: pumpjack beam and crank pivots are where the console expects them")
    ok(rigs, f"{label}: every pitman and bridle hangs from its beam with no rotation")
    acts = {nd["name"]: nd.get("extras", {}).get("actuation") for nd in glb.nodes
            if nd.get("extras", {}).get("kind") == "valve"}
    want = {v["id"]: "manual" if v.get("manual") is True or v.get("remote") is False else "remote"
            for v in data.get("valves", []) if isinstance(v, dict) and v.get("id") in acts}
    ok(all(acts.get(v) == m for v, m in want.items()) and all(a in ("manual", "remote") for a in acts.values()),
       f"{label}: {len(acts)} valves carry actuation, matching manual/remote "
       f"({sum(1 for a in acts.values() if a == 'manual')} manual)")
    kinds = names.asset_kinds(bp)
    want_dev = {"tank": "radar_level", "valve": "position", "truck": "telematics", "yard": "gateway"}
    devs = {nd["extras"]["for_asset"]: nd["extras"].get("device") for nd in glb.nodes
            if nd.get("extras", {}).get("kind") == "sensor"}
    ok(all(d == want_dev.get(kinds.get(a), "pressure") for a, d in devs.items()),
       f"{label}: {len(devs)} sensors say which instrument they are, by asset kind")


def check_build(label, bp_path, out_dir, expect_level=None, max_seconds=30):
    data = load(bp_path)
    bp = blueprint.check(data)[0]
    L = layout.plan(bp)
    rep = load(os.path.join(out_dir, "build_report.json"))
    ok(rep["ok"] and not rep["errors"], f"{label}: build_report ok ({rep['triangles']} triangles, "
       f"{rep['glb_mb']} MB, {rep['seconds']} s, {rep['detail']['level']} detail)")
    ok(rep["seconds"] < max_seconds, f"{label}: built in {rep['seconds']} s (limit {max_seconds} s)")
    if expect_level:
        ok(rep["detail"]["level"] == expect_level, f"{label}: detail level is {expect_level}")
    glb = Glb(os.path.join(out_dir, "field.glb"))
    node_names = [nd.get("name", "") for nd in glb.nodes]
    ok(len(node_names) == len(set(node_names)) and not any("." in n for n in node_names),
       f"{label}: {len(node_names)} node names, all unique, no .001 suffixes")
    missing = sorted(set(names.expected(bp)) - set(node_names))
    ok(not missing, f"{label}: all {len(names.expected(bp))} expected names present {missing[:5]}")
    kinds = names.asset_kinds(bp)
    tagged = {nd["name"]: nd.get("extras", {}) for nd in glb.nodes if "asset_id" in nd.get("extras", {})}
    ok(set(tagged) == set(kinds), f"{label}: asset nodes are exactly the {len(kinds)} blueprint IDs")
    ok(all(tagged[a].get("asset_id") == a and tagged[a].get("kind") == k for a, k in kinds.items() if a in tagged),
       f"{label}: every asset carries asset_id and kind in extras")
    ok(rep["counts"] == blueprint.counts(bp), f"{label}: report counts match the blueprint")
    if len(bp["tanks"]) <= 12:
        ok(glb.tris < 300_000 and rep["glb_mb"] < 30, f"{label}: inside budget ({glb.tris} triangles)")
    ok(glb.tris == rep["triangles"], f"{label}: report triangles match the glb")

    parents_ok = True
    for t in bp["tanks"]:
        lv = glb.nodes[glb.idx(f"{t['id']}_LEVEL")]
        parents_ok &= glb.parent.get(glb.idx(f"{t['id']}_LEVEL")) == glb.idx(t["id"])
        parents_ok &= abs(lv.get("scale", [1, 1, 1])[1] - 0.55) < 1e-4
    for part, owners in (("_rotor", bp["pumps"]), ("_beam", bp["wells"]), ("_crank", bp["wells"]),
                         ("_axle_1", bp["trucks"])):
        for a in owners:
            nd = glb.nodes[glb.idx(a["id"] + part)]
            parents_ok &= glb.parent.get(glb.idx(a["id"] + part)) == glb.idx(a["id"])
            parents_ok &= max(abs(q) for q in nd.get("rotation", (0, 0, 0, 1))[:3]) < 1e-6
    ok(parents_ok, f"{label}: moving parts sit under their asset with no rotation; levels ship at 0.55")
    check_rigs(label, glb, bp, data)

    pos_ok = True
    for p in L["pads"]:
        x, y = glb.plan_xy(p["tank"])
        ex, ey = p["frame"].world(0, layout.TANK_Y)
        pos_ok &= abs(x - ex) < 0.01 and abs(y - ey) < 0.01
    for key in ("tanks", "pumps", "wells", "swds", "trucks"):
        for a in bp[key]:
            if a["pos"]:
                x, y = glb.plan_xy(a["id"])
                pos_ok &= abs(x - a["pos"][0]) < 0.01 and abs(y - a["pos"][1]) < 0.01
    ok(pos_ok, f"{label}: tanks sit where the layout says, given x and y are honored")

    boxes = {a: glb.box(a) for a, k in kinds.items() if k != "valve"}
    boxes.update({x["id"]: glb.box(x["id"]) for x in bp["crossovers"]})
    ground = glb.box("SITE_ground")
    ids = sorted(boxes)
    clashes = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if {kinds[a], kinds[b]} == {"truck", "yard"}:
                continue
            if touch(boxes[a], boxes[b], -0.05):
                clashes.append(f"{a}/{b}")
    ok(not clashes, f"{label}: no two assets overlap {clashes[:6]}")
    outside = [a for a, b in boxes.items() if not (ground[0] <= b[0] and b[1] <= ground[1]
                                                    and ground[2] <= b[2] and b[3] <= ground[3])]
    ok(not outside, f"{label}: every asset is on the ground {outside[:5]}")
    on_road = [a for a, b in boxes.items() if kinds[a] != "truck" and b[2] < 3.4 and b[3] > -3.4]
    ok(not on_road, f"{label}: nothing but trucks sits on the lease road {on_road[:5]}")
    pads = [f"PAD_{layout.suffix(t['id'])}" for t in bp["tanks"]]
    pad_hits = [f"{a}/{b}" for i, a in enumerate(pads) for b in pads[i + 1:] if touch(glb.box(a), glb.box(b), -0.05)]
    ok(not pad_hits, f"{label}: pads do not overlap {pad_hits[:4]}")

    joins = []
    for w in bp["wells"]:
        joins.append((f"PIPE_{w['id']}_{w['tank_id']}", w["id"], f"PIPE_INLET_{layout.suffix(w['tank_id'])}"))
    for p in bp["pumps"]:
        joins.append((f"PIPE_{p['tank_id']}_{p['id']}", p["tank_id"], p["id"]))
        if p["valve_id"]:
            joins.append((f"PIPE_{p['id']}_{p['valve_id']}", p["id"], p["valve_id"]))
    for x in L["crossovers"]:
        west, east = (x["a"], x["b"]) if x["west"] == x["a"] else (x["b"], x["a"])
        base = names.valve_base(x["id"])
        joins += [(f"PIPE_{base}_W", west, x["id"]), (f"PIPE_{base}_E", x["id"], east)]
    loose = [p for p, a, b in joins if not (touch(glb.box(p), glb.box(a), 0.3) and touch(glb.box(p), glb.box(b), 0.3))]
    ok(not loose, f"{label}: {len(joins)} pipes reach both ends {loose[:5]}")
    pipe_tags = all(nd.get("extras", {}).get("pipe_id") == nd["name"] for nd in glb.nodes
                    if nd.get("name", "").startswith("PIPE_"))
    ok(pipe_tags, f"{label}: every pipe carries pipe_id")
    w_end, e_end = glb.plan_xy("ROAD_END_W")[0], glb.plan_xy("ROAD_END_E")[0]
    jct = [n for n in node_names if n.startswith("ROAD_JCT_")]
    ok(all(abs(glb.plan_xy(n)[1]) < 1e-6 and w_end < glb.plan_xy(n)[0] < e_end for n in jct),
       f"{label}: {len(jct)} road junctions sit on the road between its ends")
    test_parts.check_iot(ok, label, glb, bp)
    test_scenery.check_build(ok, label, rep, glb)
    return rep, glb


def variants(tmp):
    """Extra blueprints: given positions, more crossovers, and the largest field."""
    small = load(os.path.join(HERE, "samples", "small.json"))
    given = copy.deepcopy(small)
    given["name"] = "Variant: given positions"
    given["tanks"][1].update(x=140.0, y=-60.0)          # south of the road, far east
    given["wells"][2].update(x=150.0, y=-95.0)
    given["trucks"][0].update(x=60.0, y=-14.0)
    given["valves"].append({"id": "VALVE_XO_12", "kind": "CROSSOVER", "connects": ["TANK_1", "TANK_2"]})
    large = load(os.path.join(HERE, "samples", "large.json"))
    xo = copy.deepcopy(large)
    xo["name"] = "Variant: crossovers"
    xo["valves"] += [{"id": "VALVE_XO_15", "kind": "CROSSOVER", "connects": ["TANK_1", "TANK_5"]},
                     {"id": "VALVE_XO_26", "kind": "CROSSOVER", "connects": ["TANK_6", "TANK_2"]},
                     {"id": "VALVE_XO_8_12", "kind": "CROSSOVER", "connects": ["TANK_8", "TANK_12"]},
                     {"id": "VALVE_XO_45", "kind": "CROSSOVER", "connects": ["TANK_4", "TANK_5"]}]
    big = {"name": "Variant: 20 tanks", "tanks": [], "pumps": [], "valves": [], "wells": [],
           "disposal_wells": [{"id": f"SWD_{i}"} for i in range(1, 5)],
           "trucks": [{"id": f"TRUCK_{i}", "home_id": "YARD_1"} for i in range(1, 7)], "yards": [{"id": "YARD_1"}]}
    for n in range(1, 21):
        big["tanks"].append({"id": f"TANK_{n}", "capacity_bbl": 500})
        big["pumps"].append({"id": f"PUMP_{n}", "tank_id": f"TANK_{n}", "valve_id": f"VALVE_D_{n}"})
        big["valves"].append({"id": f"VALVE_D_{n}", "kind": "DISCHARGE", "pump_id": f"PUMP_{n}"})
        big["wells"] += [{"id": f"WELL_{n}_{j}", "tank_id": f"TANK_{n}"} for j in range(1, 4)]
    big["valves"].append({"id": "VALVE_XO_10_11", "kind": "CROSSOVER", "connects": ["TANK_10", "TANK_11"]})
    out = {}
    for name, data in (("given", given), ("crossovers", xo), ("max20", big),
                       ("equipment", test_parts.equipment_blueprint(small)), ("iot", test_parts.iot_blueprint(small))):
        out[name] = os.path.join(tmp, f"{name}.json")
        with open(out[name], "w") as f:
            json.dump(data, f, indent=1)
    return out


def test_builds(tmp):
    draft, source = sample2_draft(tmp)
    print(f"      sample 2 draft from {source}")
    cases = [("small", os.path.join(HERE, "samples", "small.json"), "full"),
             ("default", None, "full"),
             ("large", os.path.join(HERE, "samples", "large.json"), None),     # full or lite, see below
             ("sample1", SAMPLE1, "full"),
             ("sample2_draft", draft, "full")]
    paths = variants(tmp)
    cases += [(k, v, None) for k, v in paths.items()]
    reports, glbs = {}, {}
    for label, path, level in cases:
        out = HERE if path is None else os.path.join(tmp, label)
        p = blender([] if path is None else ["--blueprint", path, "--out", out])
        if not ok(p.returncode == 0, f"{label}: Blender exited 0"):
            print("\n".join((p.stdout + p.stderr).splitlines()[-15:]))
            continue
        reports[label], glbs[label] = check_build(label, path or os.path.join(ROOT, "scenario", "field.json"),
                                                  out, level)
    if "sample1" in reports:
        r = reports["sample1"]
        ok(r.get("site", {}).get("name") == "Pecos Ridge lease" and r.get("connectors") == ["CONN_SIM"],
           "sample1: the report carries the site and the connector IDs")
    if "sample2_draft" in glbs:
        g, data = glbs["sample2_draft"], load(draft)
        raw = []   # three.js gets plan (x, y) as (x, height, -y): check the glb's own numbers
        for t in data["tanks"]:
            m = g.world[g.idx(t["id"])]
            raw.append(abs(m[0][3] - t["x"]) < 0.01 and abs(m[2][3] + t["y"]) < 0.01 and 0 <= m[1][3] < 1)
        ok(raw and all(raw), "sample2_draft: a tank at plan (x, y) sits at three.js (x, height, -y)")
        given = sorted(a["id"] for k in ("tanks", "pumps", "wells", "disposal_wells", "yards")
                       for a in data.get(k, []) if a.get("x") is not None)
        ok(reports["sample2_draft"]["placed"]["given"] == given,
           f"sample2_draft: every position from the CSV is used ({len(given)} assets)")
    if "large" in reports:   # 47 wells: hero full fits only under a budget above about 450k triangles
        ok(reports["large"]["detail"]["level"] in ("full", "lite"),
           f"large: builds at full or lite, never bare ({reports['large']['detail']['level']})")
    if "max20" in reports:
        r = reports["max20"]
        ok(r["detail"]["level"] != "full" and any("dropped" in w for w in r["warnings"]),
           f"max20: more than 12 tanks drops small detail and says so ({r['detail']['level']})")
    if "given" in reports:
        ok(reports["given"]["placed"]["given"] == sorted(["TANK_2", "WELL_3", "TRUCK_1"]),
           "given: the report lists which assets had positions")
    if "crossovers" in reports:
        styles = reports["crossovers"]["layout"]["crossovers"]
        ok(list(styles.values()).count("rack") == 4, f"crossovers: far pairs route on the rack {styles}")
    if "equipment" in reports:
        test_parts.check_build(ok, reports["equipment"], glbs["equipment"], load(paths["equipment"]))
    if "iot" in reports:
        test_parts.check_iot_variant(ok, reports["iot"], glbs["iot"], load(paths["iot"]))
    return reports


# ---------- 3. a broken blueprint ----------

def test_failure(tmp):
    data = load(os.path.join(HERE, "samples", "small.json"))
    data["wells"].append({"id": "WELL_1", "tank_id": "TANK_1"})
    data["pumps"][1]["tank_id"] = "TANK_9"
    data["trucks"][0]["id"] = "truck one"
    path, out = os.path.join(tmp, "broken.json"), os.path.join(tmp, "broken")
    with open(path, "w") as f:
        json.dump(data, f)
    p = blender(["--blueprint", path, "--out", out])
    ok(p.returncode == 1, f"broken blueprint exits 1 (got {p.returncode})")
    rep = load(os.path.join(out, "build_report.json"))
    errs = " | ".join(rep["errors"])
    ok(not rep["ok"] and "duplicate ID" in errs and "TANK_9" in errs and "must match" in errs,
       f"broken blueprint: report names every problem ({len(rep['errors'])} errors)")
    ok(not os.path.exists(os.path.join(out, "field.glb")), "broken blueprint writes no glb")
    p = blender(["--blueprint", os.path.join(tmp, "missing.json"), "--out", out])
    ok(p.returncode == 1 and "cannot read" in " ".join(load(os.path.join(out, "build_report.json"))["errors"]),
       "a missing blueprint file exits 1 with a clear message")


def test_tools_check():
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "check")], cwd=ROOT, capture_output=True,
                       text=True, timeout=600)
    lines = p.stdout.splitlines()
    blender_line = next((l for l in lines if l[6:].startswith("blender")), "")
    print("      " + blender_line)
    print("      " + (lines[-1] if lines else ""))
    ok(blender_line.startswith("PASS"), "tools/check: blender line PASS on the default build")


def main():
    test_format_v1()
    test_validation()
    test_parts.test_validation(ok, load(os.path.join(HERE, "samples", "small.json")))
    test_scenery.test_plan(ok, ROOT)
    test_scenery.test_terrain(ok, ROOT)
    test_layout()
    if "--quick" not in sys.argv:
        with tempfile.TemporaryDirectory() as tmp:
            reports = test_builds(tmp)
            test_parts.test_preview(ok, blender, tmp)
            if "small" in reports:
                test_scenery.test_plain_and_repeat(ok, blender, tmp, os.path.join(HERE, "samples", "small.json"),
                                                   reports["small"])
            test_failure(tmp)
        test_tools_check()
    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
