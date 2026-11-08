"""Tests for the blueprint generator. Run from the repo root:

  python3 blender/test_generator.py           validation tests, every build, and tools/check
  python3 blender/test_generator.py --quick   validation and layout tests only (no Blender)

1. Validation and layout run in plain Python against blueprint.py and layout.py.
2. Builds: small, the default (scenario/field.json into blender/), large, and three variants
   made here (given positions, extra crossovers, a 20-tank maximum). Each glb's JSON chunk
   is read to check names, kinds, hierarchy, positions, bounding boxes, pipes, and budget.
3. A broken blueprint must exit 1, explain itself in build_report.json, and write no glb.
4. tools/check must PASS its blender line on the default build.
"""
import copy
import json
import math
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import blueprint  # noqa: E402
import layout  # noqa: E402
import names  # noqa: E402

BLENDER = os.environ.get("BLENDER", "/Applications/Blender.app/Contents/MacOS/Blender")
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


# ---------- 1. validation and layout ----------

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


# ---------- glb reading ----------

def read_glb(path):
    with open(path, "rb") as f:
        raw = f.read()
    n, kind = struct.unpack_from("<II", raw, 12)
    assert raw[:4] == b"glTF" and kind == 0x4E4F534A, "not a glb"
    return json.loads(raw[20:20 + n])


def mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def local_matrix(node):
    if "matrix" in node:
        m = node["matrix"]
        return [[m[c * 4 + r] for c in range(4)] for r in range(4)]
    tx, ty, tz = node.get("translation", (0, 0, 0))
    qx, qy, qz, qw = node.get("rotation", (0, 0, 0, 1))
    sx, sy, sz = node.get("scale", (1, 1, 1))
    r = [[1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
         [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
         [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)]]
    s = (sx, sy, sz)
    return [[r[i][0] * s[0], r[i][1] * s[1], r[i][2] * s[2], (tx, ty, tz)[i]] for i in range(3)] + [[0, 0, 0, 1]]


class Glb:
    """Node names, world positions in plan coordinates, and world bounding boxes."""

    def __init__(self, path):
        self.g = g = read_glb(path)
        self.nodes = g["nodes"]
        self.by_name = {}
        for i, nd in enumerate(self.nodes):
            self.by_name.setdefault(nd.get("name", ""), []).append(i)
        self.parent = {c: i for i, nd in enumerate(self.nodes) for c in nd.get("children", [])}
        self.world = {}
        for root in g["scenes"][g.get("scene", 0)]["nodes"]:
            self._walk(root, [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        self.tris = self._tris()

    def _tris(self):
        acc = self.g["accessors"]
        per_mesh = [sum(acc[p["indices"]]["count"] // 3 for p in m["primitives"]) for m in self.g["meshes"]]
        return sum(per_mesh[nd["mesh"]] for nd in self.nodes if "mesh" in nd)

    def _walk(self, i, parent):
        self.world[i] = m = mat_mul(parent, local_matrix(self.nodes[i]))
        for c in self.nodes[i].get("children", []):
            self._walk(c, m)

    def idx(self, name):
        return self.by_name[name][0]

    def plan_xy(self, name):
        m = self.world[self.idx(name)]
        return (m[0][3], -m[2][3])

    def box(self, name):
        """World AABB of a node and its children, in plan coordinates (x0, x1, y0, y1, z0, z1)."""
        pts = []
        stack = [self.idx(name)]
        while stack:
            i = stack.pop()
            stack += self.nodes[i].get("children", [])
            if "mesh" in self.nodes[i]:
                m = self.world[i]
                for p in self.g["meshes"][self.nodes[i]["mesh"]]["primitives"]:
                    a = self.g["accessors"][p["attributes"]["POSITION"]]
                    lo, hi = a["min"], a["max"]
                    for c in range(8):
                        v = [(hi if c >> k & 1 else lo)[k] for k in range(3)] + [1]
                        w = [sum(m[r][k] * v[k] for k in range(4)) for r in range(3)]
                        pts.append((w[0], -w[2], w[1]))
        if not pts:
            return None
        return (min(p[0] for p in pts), max(p[0] for p in pts), min(p[1] for p in pts),
                max(p[1] for p in pts), min(p[2] for p in pts), max(p[2] for p in pts))


def touch(a, b, grow=0.0):
    return a[0] - grow < b[1] and b[0] - grow < a[1] and a[2] - grow < b[3] and b[2] - grow < a[3]


# ---------- 2. builds ----------

def blender(args):
    cmd = [BLENDER, "--background", "--factory-startup", "--python", os.path.join(HERE, "build_field.py"), "--"]
    return subprocess.run(cmd + args, cwd=ROOT, capture_output=True, text=True, timeout=600)


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
    for name, data in (("given", given), ("crossovers", xo), ("max20", big)):
        out[name] = os.path.join(tmp, f"{name}.json")
        with open(out[name], "w") as f:
            json.dump(data, f, indent=1)
    return out


def test_builds(tmp):
    cases = [("small", os.path.join(HERE, "samples", "small.json"), "full"),
             ("default", None, "full"),
             ("large", os.path.join(HERE, "samples", "large.json"), "full")]
    cases += [(k, v, None) for k, v in variants(tmp).items()]
    reports = {}
    for label, path, level in cases:
        out = HERE if path is None else os.path.join(tmp, label)
        p = blender([] if path is None else ["--blueprint", path, "--out", out])
        if not ok(p.returncode == 0, f"{label}: Blender exited 0"):
            print("\n".join((p.stdout + p.stderr).splitlines()[-15:]))
            continue
        reports[label], _ = check_build(label, path or os.path.join(ROOT, "scenario", "field.json"), out, level)
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
    test_validation()
    test_layout()
    if "--quick" not in sys.argv:
        with tempfile.TemporaryDirectory() as tmp:
            test_builds(tmp)
            test_failure(tmp)
        test_tools_check()
    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
