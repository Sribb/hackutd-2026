"""Tests for the no-Blender generator, blender/assemble_glb.py. Run from the repo root:

  python3 blender/test_assemble.py           kit builds, checked against layout.py and against Blender builds
  python3 blender/test_assemble.py --quick   kit builds only, no Blender

Three blueprints go through the assembler: the default field (scenario/field.json), the demo
kit's sample 1, and the ingest draft of sample 2's CSV (made live with ingest_bytes when an
interpreter with ingest's requirements is found, otherwise blender/samples/sample2_draft.json).
The assembler runs with python3 -S, so it can only use the standard library. For each build:

1. the glb parses: every buffer view, accessor, index, child, mesh, and material is in range;
2. every contract name (names.py, and the CONTRACT.md ID table for the default field) and
   every blueprint ID is present, with no .001 names, and asset nodes carry asset_id and kind;
3. every moving part, light, and sensor sits under its asset (lamp heads under their light
   tower), with no rotation where it animates by rotation, and each tank level ships at 0.55;
4. every asset, pad, load-out, waypoint, and crossover valve sits where layout.py says;
5. the default field takes under 1 s and 10 MB (each sample too). The time is the build's
   own clock and the process's CPU time, so a busy machine does not fail the test;
6. against build_field.py on the same blueprint: asset and moving-part positions within
   0.5 m, every pipe's bounding box within 5 cm, the same asset, moving-part, light, and
   sensor names, the same extras on every node both builds have, and the same report keys.

Then a broken blueprint must exit 1 with no glb, and the kit must stay under 15 MB.
"""
import importlib.machinery
import importlib.util
import json
import math
import os
import resource
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import blueprint  # noqa: E402
import layout  # noqa: E402
import names  # noqa: E402
from glb_read import Glb  # noqa: E402
from glb_write import WIDTH, accessor, read_glb  # noqa: E402

BLENDER = os.environ.get("BLENDER", "/Applications/Blender.app/Contents/MacOS/Blender")
ASSEMBLE = os.path.join(HERE, "assemble_glb.py")
SAMPLE1 = os.path.join(ROOT, "scenario", "samples", "sample1_pecos_8tank.json")
SAMPLE2_CSV = os.path.join(ROOT, "scenario", "samples", "sample2_delaware_4tank.csv")
SAMPLE2_DRAFT = os.path.join(HERE, "samples", "sample2_draft.json")
MAX_SECONDS, MAX_MB, KIT_MB, BLENDER_M, PIPE_M = 1.0, 10.0, 15.0, 0.5, 0.05
MOVING = ("_LEVEL", "_gauge", "_rotor", "_wheel", "_beam", "_crank", "_gate", "_axle_1", "_axle_2", "_axle_3")
LIGHTS = ("_light", "_beacon", "_headlights", "_lamps")
SENSORS = ("_lt", "_lshh")
STILL = ("_rotor", "_beam", "_crank", "_axle_1", "_axle_2", "_axle_3", "_wheel")   # rotate in the console
FAILS = []


def ok(cond, what):
    print(("PASS  " if cond else "FAIL  ") + what)
    if not cond:
        FAILS.append(what)
    return cond


def load(path):
    with open(path) as f:
        return json.load(f)


def ingest_pythons():
    """Interpreters that may have ingest's requirements, most specific first."""
    cands = [os.environ.get("INGEST_PYTHON"), sys.executable, os.path.join(ROOT, ".venv", "bin", "python")]
    try:   # in a git worktree, the main checkout holds the .venv
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=ROOT, capture_output=True,
                                text=True, timeout=10).stdout.strip()
        if common:
            cands.append(os.path.join(os.path.dirname(os.path.abspath(os.path.join(ROOT, common))), ".venv", "bin",
                                      "python"))
    except (OSError, subprocess.SubprocessError):
        pass
    return [c for c in dict.fromkeys(cands) if c and os.path.exists(c)]


def sample2_draft(tmp):
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


def assemble(args):
    """Runs the assembler with no site-packages. Returns (process, (wall seconds, CPU seconds))."""
    t0, c0 = time.time(), resource.getrusage(resource.RUSAGE_CHILDREN)
    p = subprocess.run([sys.executable, "-S", ASSEMBLE] + args, cwd=ROOT, capture_output=True, text=True,
                       timeout=120)
    c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    return p, (time.time() - t0, c1.ru_utime + c1.ru_stime - c0.ru_utime - c0.ru_stime)


def blender_build(bp_path, out):
    cmd = [BLENDER, "--background", "--factory-startup", "--python", os.path.join(HERE, "build_field.py"), "--",
           "--blueprint", bp_path, "--out", out]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600)


def contract_names():
    """The default field's IDs from the CONTRACT.md table, read the way tools/check reads them."""
    path = os.path.join(ROOT, "tools", "check")
    spec = importlib.util.spec_from_loader("tools_check", importlib.machinery.SourceFileLoader("tools_check", path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assets, meshes = mod.contract_ids()
    return assets | meshes


# ---------- checks on one glb ----------

def structure(path):
    """Problems that would stop a glTF loader: anything out of range."""
    g, binary = read_glb(path)
    out = []
    if g["buffers"][0]["byteLength"] > len(binary):
        out.append("buffer is longer than the BIN chunk")
    for i, bv in enumerate(g["bufferViews"]):
        if bv.get("byteOffset", 0) + bv["byteLength"] > len(binary):
            out.append(f"bufferView {i} runs past the buffer")
    for i, a in enumerate(g["accessors"]):
        bv = g["bufferViews"][a["bufferView"]]
        size = a["count"] * WIDTH[a["type"]] * {5126: 4, 5125: 4, 5123: 2, 5121: 1}[a["componentType"]]
        if a.get("byteOffset", 0) + size > bv["byteLength"]:
            out.append(f"accessor {i} runs past its view")
    nmat = len(g.get("materials", []))
    for i, m in enumerate(g["meshes"]):
        for p in m["primitives"]:
            count = g["accessors"][p["attributes"]["POSITION"]]["count"]
            if any(g["accessors"][a]["count"] != count for a in p["attributes"].values()):
                out.append(f"mesh {i} has attributes of different lengths")
            idx = accessor(g, binary, p["indices"])
            if len(idx) % 3 or (len(idx) and max(idx) >= count):
                out.append(f"mesh {i} has an index out of range")
            if not 0 <= p.get("material", 0) < max(1, nmat):
                out.append(f"mesh {i} names a missing material")
    parents = {}
    for i, nd in enumerate(g["nodes"]):
        if "mesh" in nd and not 0 <= nd["mesh"] < len(g["meshes"]):
            out.append(f"node {nd.get('name')} names a missing mesh")
        for c in nd.get("children", []):
            if c in parents or not 0 <= c < len(g["nodes"]):
                out.append(f"node {c} has two parents or does not exist")
            parents[c] = i
    roots = g["scenes"][g.get("scene", 0)]["nodes"]
    if any(r in parents for r in roots) or len(parents) + len(roots) != len(g["nodes"]):
        out.append("the scene roots and the children do not cover every node exactly once")
    return out


def backwards(path, first_generated):
    """Triangles in the assembler's own meshes that face away from their vertex normals.

    With double-sided materials, three.js lights a backwards triangle from behind, so it
    shows dark. Kit meshes come from Blender; meshes from first_generated on are generated.
    """
    g, binary = read_glb(path)
    bad = 0
    for m in g["meshes"][first_generated:]:
        for p in m["primitives"]:
            pos, nrm = accessor(g, binary, p["attributes"]["POSITION"]), accessor(g, binary, p["attributes"]["NORMAL"])
            idx = accessor(g, binary, p["indices"])
            for t in range(0, len(idx), 3):
                a, b, c = (3 * idx[t + k] for k in range(3))
                e1 = [pos[b + k] - pos[a + k] for k in range(3)]
                e2 = [pos[c + k] - pos[a + k] for k in range(3)]
                n = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
                h = [nrm[a + k] + nrm[b + k] + nrm[c + k] for k in range(3)]
                if n[0] * h[0] + n[1] * h[1] + n[2] * h[2] < -1e-12:
                    bad += 1
    return bad


def check_kit_build(label, bp_path, out, rep, glb, seconds):
    bp = blueprint.check(load(bp_path))[0]
    L = layout.plan(bp)
    mb = os.path.getsize(os.path.join(out, "field.glb")) / 1e6
    ok(rep["ok"] and rep.get("model") == "kit" and not rep["errors"],
       f"{label}: report ok, model kit ({rep['triangles']} triangles, {rep['glb_mb']} MB, {rep['seconds']} s, "
       f"{rep['detail']['level']} detail)")
    wall, cpu = seconds
    ok(rep["seconds"] < MAX_SECONDS and cpu < MAX_SECONDS and mb < MAX_MB,
       f"{label}: {rep['seconds']:.2f} s to build, {cpu:.2f} s of CPU end to end ({wall:.2f} s wall), and "
       f"{mb:.2f} MB (targets {MAX_SECONDS:g} s and {MAX_MB:g} MB)")
    problems = structure(os.path.join(out, "field.glb"))
    ok(not problems, f"{label}: the glb parses and every reference is in range {problems[:3]}")
    generated = next(i for i, m in enumerate(glb.g["meshes"]) if m.get("name") == "SITE_ground")
    flipped = backwards(os.path.join(out, "field.glb"), generated)
    ok(not flipped, f"{label}: every generated triangle faces its normals ({flipped} backwards)")
    ok(rep["detail"]["passes"][0]["estimated"] == rep["triangles"],
       f"{label}: the plan's triangle count matches the glb ({rep['detail']['passes'][0]['estimated']})")

    node_names = [nd.get("name", "") for nd in glb.nodes]
    have = set(node_names)
    ok(len(node_names) == len(have) and not any("." in n for n in node_names),
       f"{label}: {len(node_names)} node names, all unique, no .001 names")
    need = set(names.expected(bp))
    ok(need <= have, f"{label}: all {len(need)} contract names present {sorted(need - have)[:5]}")
    kinds = names.asset_kinds(bp)
    tagged = {nd["name"]: nd["extras"] for nd in glb.nodes if "asset_id" in nd.get("extras", {})}
    ok(set(tagged) == set(kinds) and all(tagged[a]["asset_id"] == a and tagged[a]["kind"] == k
                                         for a, k in kinds.items()),
       f"{label}: asset nodes are exactly the {len(kinds)} blueprint IDs, each with asset_id and kind")
    ok(rep["counts"] == blueprint.counts(bp), f"{label}: report counts match the blueprint")
    ok(glb.tris == rep["triangles"], f"{label}: report triangles match the glb")
    pipes = [nd for nd in glb.nodes if nd.get("name", "").startswith("PIPE_")]
    ok(pipes and all(nd.get("extras", {}).get("pipe_id") == nd["name"] for nd in pipes),
       f"{label}: {len(pipes)} pipes, each with pipe_id")

    loose = []
    for aid in kinds:
        for part in MOVING + LIGHTS + SENSORS:
            if aid + part in have:
                i = glb.idx(aid + part)
                nd = glb.nodes[i]
                owner = aid + "_tower" if part == "_lamps" else aid    # lamp heads sit on their light tower
                if glb.parent.get(i) != glb.idx(owner):
                    loose.append(aid + part)
                elif part in STILL and max(abs(q) for q in nd.get("rotation", (0, 0, 0, 1))[:3]) > 1e-6:
                    loose.append(aid + part + " (rotated)")
    owners = {f"SENSOR_{aid}": aid for aid in kinds}                                    # #46
    owners.update({f"RTU_PAD_{p['n']}": f"PAD_{p['n']}" for p in L["pads"]})
    loose += [n for n, owner in owners.items() if n in have and glb.parent.get(glb.idx(n)) != glb.idx(owner)]
    levels = [abs(glb.nodes[glb.idx(t["id"] + "_LEVEL")].get("scale", [1, 1, 1])[1] - 0.55) < 1e-4 for t in bp["tanks"]]
    ok(not loose and all(levels), f"{label}: moving parts, lights, and sensors sit under their asset {loose[:4]}")

    want = {}
    for p in L["pads"]:
        f = p["frame"]
        want[p["tank"]], want[f"PAD_{p['n']}"] = f.world(0, layout.TANK_Y), (f.x, f.y)
        want[f"LOADOUT_{p['n']}"] = f.world(-7.6, 35.0)
    for key in ("pumps", "wells", "trucks"):
        want.update({a["id"]: tuple(a["pos"]) for a in L[key]})
    want.update({s["id"]: (s["frame"].x, s["frame"].y) for s in L["swds"]})
    if L["yard"]["id"]:
        want[L["yard"]["id"]] = (L["yard"]["frame"].x, L["yard"]["frame"].y)
    want.update({x["id"]: tuple(x["valve"][:2]) for x in L["crossovers"]})
    want.update({n: loc[:2] for n, loc in L["waypoints"]})
    for key in ("tanks", "pumps", "wells", "swds", "trucks"):
        want.update({a["id"]: a["pos"] for a in bp[key] if a["pos"] and key != "tanks"})
    off = [n for n, xy in want.items() if math.dist(glb.plan_xy(n), xy) > 0.01]
    given = [t["id"] for t in bp["tanks"] if t["pos"] and math.dist(glb.plan_xy(t["id"]), t["pos"]) > 0.01]
    ok(not off and not given, f"{label}: {len(want)} assets, pads, load-outs, and waypoints sit where layout.py "
       f"says {(off + given)[:4]}")
    return bp


def compare(label, bp, kit, kit_rep, ref, ref_rep):
    kinds = names.asset_kinds(bp)
    sets = {}
    for what, parts in (("asset", ("",)), ("moving-part", MOVING), ("light", LIGHTS), ("sensor", SENSORS)):
        sets[what] = [{aid + p for aid in kinds for p in parts if aid + p in g.by_name} for g in (kit, ref)]
    iot = [{n for n in g.by_name if n.startswith(("SENSOR_", "RTU_")) and not n.endswith("_tag")} for g in (kit, ref)]
    if iot[1]:            # #46: sensor nodes and pad RTUs
        sets["sensor-node and RTU"] = iot
    for what, (a, b) in sets.items():
        ok(a == b, f"{label}: the same {len(b)} {what} names as the Blender build "
           f"(kit only {sorted(a - b)[:3]}, Blender only {sorted(b - a)[:3]})")
    both = set(kit.by_name) & set(ref.by_name)
    extras = sorted(n for n in both if kit.nodes[kit.idx(n)].get("extras") != ref.nodes[ref.idx(n)].get("extras"))
    ok(not extras, f"{label}: all {len(both)} nodes in both builds carry the Blender build's extras, sensors, "
       f"RTUs and valve actuation too {extras[:3]}")
    dev = []
    for n in sorted(sets["asset"][1] | sets["moving-part"][1]):
        if n in kit.by_name:
            pk, pr = kit.world[kit.idx(n)], ref.world[ref.idx(n)]
            dev.append((math.dist([pk[r][3] for r in range(3)], [pr[r][3] for r in range(3)]), n))
    worst = max(dev) if dev else (0.0, "")
    ok(worst[0] < BLENDER_M, f"{label}: {len(dev)} asset and moving-part positions within {BLENDER_M} m of the "
       f"Blender build (largest {worst[0]:.4f} m, {worst[1]})")
    pipes = [n for n in ref.by_name if n.startswith("PIPE_")]
    off = sorted(((max(abs(a - b) for a, b in zip(kit.box(n), ref.box(n))), n) for n in pipes if n in kit.by_name),
                 reverse=True)
    ok(set(pipes) <= set(kit.by_name) and (not off or off[0][0] < PIPE_M),
       f"{label}: all {len(pipes)} pipes run where the Blender build runs them, within {PIPE_M} m "
       f"(largest {off[0][0] if off else 0:.4f} m, {off[0][1] if off else ''})")
    keys_k, keys_r = set(kit_rep) - {"model"}, set(ref_rep)
    ok(keys_k == keys_r, f"{label}: build_report.json has the Blender report's keys plus model "
       f"(missing {sorted(keys_r - keys_k)}, extra {sorted(keys_k - keys_r)})")
    print(f"      {label}: kit {kit_rep['seconds']} s, {kit_rep['glb_mb']} MB, {kit_rep['triangles']} triangles | "
          f"Blender {ref_rep['seconds']} s, {ref_rep['glb_mb']} MB, {ref_rep['triangles']} triangles")


# ---------- runs ----------

def test_builds(tmp, with_blender):
    draft, source = sample2_draft(tmp)
    print(f"      sample 2 draft from {source}")
    cases = [("default", os.path.join(ROOT, "scenario", "field.json")), ("sample1", SAMPLE1),
             ("sample2_draft", draft)]
    if with_blender and not os.path.exists(BLENDER):
        print(f"SKIP  Blender comparison: no Blender at {BLENDER}")
        with_blender = False
    for label, path in cases:
        out = os.path.join(tmp, f"{label}-kit")
        p, seconds = assemble(["--blueprint", path, "--out", out])
        if not ok(p.returncode == 0, f"{label}: assemble_glb.py exited 0 under python3 -S"):
            print("\n".join((p.stdout + p.stderr).splitlines()[-15:]))
            continue
        rep, glb = load(os.path.join(out, "build_report.json")), Glb(os.path.join(out, "field.glb"))
        bp = check_kit_build(label, path, out, rep, glb, seconds)
        if label == "default":
            table = contract_names()
            ok(table <= set(glb.by_name), f"default: all {len(table)} CONTRACT.md table names present "
               f"{sorted(table - set(glb.by_name))[:5]}")
        if with_blender:
            ref_out = os.path.join(tmp, f"{label}-blender")
            q = blender_build(path, ref_out)
            if ok(q.returncode == 0, f"{label}: the Blender build exited 0"):
                compare(label, bp, glb, rep, Glb(os.path.join(ref_out, "field.glb")),
                        load(os.path.join(ref_out, "build_report.json")))


def test_failure(tmp):
    data = load(os.path.join(HERE, "samples", "small.json"))
    data["wells"].append({"id": "WELL_1", "tank_id": "TANK_1"})
    data["pumps"][1]["tank_id"] = "TANK_9"
    path, out = os.path.join(tmp, "broken.json"), os.path.join(tmp, "broken")
    with open(path, "w") as f:
        json.dump(data, f)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "field.glb"), "w") as f:
        f.write("an older glb")
    p, _ = assemble(["--blueprint", path, "--out", out])
    rep = load(os.path.join(out, "build_report.json"))
    ok(p.returncode == 1 and not rep["ok"] and "duplicate ID" in " ".join(rep["errors"])
       and "TANK_9" in " ".join(rep["errors"]), f"a broken blueprint exits 1 and says why ({p.returncode})")
    ok(not os.path.exists(os.path.join(out, "field.glb")), "a broken blueprint leaves no glb behind")
    p, _ = assemble(["--blueprint", os.path.join(tmp, "missing.json"), "--out", out])
    ok(p.returncode == 1 and "cannot read" in " ".join(load(os.path.join(out, "build_report.json"))["errors"]),
       "a missing blueprint file exits 1 with a clear message")
    p, _ = assemble(["--bogus", "1"])
    ok(p.returncode == 2, "an unknown option exits 2")


def test_kit():
    kit = os.path.join(HERE, "kit")
    m = load(os.path.join(kit, "manifest.json"))
    size = sum(os.path.getsize(os.path.join(kit, f)) for f in os.listdir(kit))
    ok(size < KIT_MB * 1e6, f"the kit is {size / 1e6:.2f} MB in {len(os.listdir(kit))} files (under {KIT_MB:g} MB)")
    print(f"      kit types: {', '.join(m['types'])}")
    import assemble_glb
    stale = assemble_glb.Kit(kit).stale()
    if stale:
        print(f"WARN  the kit is older than {', '.join(stale)}: rerun blender/export_kit.py in Blender")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_kit()
        test_builds(tmp, "--quick" not in sys.argv)
        test_failure(tmp)
    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
