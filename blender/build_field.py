"""Build an oil field from a blueprint: blueprint.json in, field.glb out.

Run from the repo root:
  /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
      --python blender/build_field.py -- [--blueprint PATH.json] [--out DIR] \
      [--detail full|lite|bare] [--render-dir DIR] [--render-assets ID,ID] [--no-scenery] \
      [--look fast|full|bake]

The blueprint follows docs/BLUEPRINT.md (format v1); ingest drafts with missing numbers
build too, since the generator only needs IDs and links.

After the field, scenery.py dresses it as desert under one hideable SCENERY root, with
whatever triangle budget is left. --no-scenery builds the plain field: no SCENERY layer,
and flat ground and road colors instead of the kit's textures.

Last, look.py gives every part its realistic material, UVs and weathering. --look full adds
baked ambient occlusion (the committed blender/field.glb); fast, the default, skips it;
bake is full plus the AO_UV map (TEXCOORD_1) an AO texture bake needs.

With no --blueprint it builds scenario/field.json into blender/, the field the team uses
today. With --blueprint and no --out it writes to blender/out/<blueprint name>/. Every run
writes build_report.json. A blueprint with errors exits with code 1 and writes no glb. A
build that fails exits with code 2.

blueprint.py checks the file, layout.py places every asset, and field_build.py and
piping.py build the scene from field_parts.py (core assets), detail_equipment.py, and
detail_site.py (detail). names.py lists the node names every build must contain.
"""
import importlib
import json
import math
import os
import struct
import sys
import time
import traceback
from collections import Counter

import bpy
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import blueprint  # noqa: E402
import layout  # noqa: E402
import names  # noqa: E402
import field_parts as fp  # noqa: E402
import detail_equipment as de  # noqa: E402
import detail_site as ds  # noqa: E402
import piping  # noqa: E402
import siting  # noqa: E402
import equipment_parts  # noqa: E402
import iot_parts  # noqa: E402
import field_build  # noqa: E402
import scenery_parts  # noqa: E402
import scenery  # noqa: E402
import look  # noqa: E402
import optimize  # noqa: E402

for _mod in (blueprint, layout, siting, names, fp, de, ds, piping, equipment_parts, iot_parts, field_build,
             scenery_parts, scenery, look, optimize):
    importlib.reload(_mod)

ROOT = os.path.dirname(HERE)
DEFAULT_BLUEPRINT = os.path.join(ROOT, "scenario", "field.json")
BUDGET_TRIS, BUDGET_MB, BUDGET_DRAWS = 300_000, 30.0, 1500   # for up to 12 tanks; see CONTRACT.md
SCENERY_SHARE = 0.95        # scenery fills the triangle budget up to 95%, leaving headroom
FULL_DETAIL_MAX_TANKS = 12
LEVELS = {
    "full": {"bolts": True, "small_labels": True, "posts": True, "trim": True},
    "lite": {"bolts": False, "small_labels": False, "posts": False, "trim": True},
    "bare": {"bolts": False, "small_labels": False, "posts": False, "trim": False},
}
DROPPED = {
    "full": [],
    "lite": ["bolts", "small labels, tags and signs", "containment and fence posts"],
    "bare": ["bolts", "small labels, tags and signs", "containment and fence posts",
             "wellhead trees, crank guards and well signs"],
}
# Triangles each asset adds at each level, measured from the sample builds. A tank includes
# its pad and load-out, a pump its valve and lines, a well its flowline. The site (ground,
# road, power, trunk, yard) costs one triangle per 12.5 m2 of ground plus 28 per road meter.
# A tank also pays for its pad's RTU, and every monitored asset for its sensor node.
COSTS = {
    "full": {"tank": 7570, "pump": 4150, "well": 1900, "swd": 3800, "truck": 3100, "xo": 1750, "equipment": 2400,
             "sensor": 150},
    "lite": {"tank": 6800, "pump": 3350, "well": 1820, "swd": 3120, "truck": 3100, "xo": 1350, "equipment": 2200,
             "sensor": 150},
    "bare": {"tank": 6800, "pump": 3350, "well": 1060, "swd": 3120, "truck": 3100, "xo": 1350, "equipment": 2200,
             "sensor": 150},
}


class UsageError(Exception):
    pass


def parse_args(argv):
    opts = {"blueprint": None, "out": None, "detail": None, "render_dir": None, "render_assets": None,
            "scenery": True, "look": "fast"}
    flags = {"--blueprint": "blueprint", "--out": "out", "--detail": "detail",
             "--render-dir": "render_dir", "--render-assets": "render_assets", "--look": "look"}
    i = 0
    while i < len(argv):
        if argv[i] == "--no-scenery":
            opts["scenery"] = False
            i += 1
            continue
        key = flags.get(argv[i])
        if key is None or i + 1 >= len(argv):
            raise UsageError(f"unknown or incomplete option {argv[i]!r}; options: {', '.join(flags)}, --no-scenery")
        opts[key] = argv[i + 1]
        i += 2
    if opts["detail"] not in (None, *LEVELS):
        raise UsageError(f"--detail must be one of {', '.join(LEVELS)}")
    if opts["look"] not in look.MODES:
        raise UsageError(f"--look must be one of {', '.join(look.MODES)}")
    return opts


def rel(path):
    path = os.path.abspath(path)
    return os.path.relpath(path, ROOT) if path.startswith(ROOT + os.sep) else path


def estimate(bp, L, level):
    c = COSTS[level]
    gx0, gx1, gy0, gy1 = L["ground"]
    site = (gx1 - gx0) * (gy1 - gy0) / 12.5 + 28 * (L["road"][1] - L["road"][0]) + 2000 * (L["rows"] - 1)
    return int(site + c["tank"] * len(bp["tanks"]) + c["pump"] * len(bp["pumps"]) + c["well"] * len(bp["wells"])
               + c["swd"] * len(bp["swds"]) + c["truck"] * len(bp["trucks"]) + c["xo"] * len(bp["crossovers"])
               + c["equipment"] * len(bp["equipment"]) + c["sensor"] * len(names.sensors(bp)))


def clear_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for col in list(bpy.data.collections):
        bpy.data.collections.remove(col)
    for blocks in (bpy.data.meshes, bpy.data.curves, bpy.data.cameras, bpy.data.lights):
        for block in list(blocks):
            if block.users == 0:
                blocks.remove(block)


def stage(collection, scene, L):
    """Overview camera framing the whole field, a sun, and a sky color."""
    x0, x1 = L["road"]
    gx0, gx1, gy0, gy1 = L["ground"]
    tank_ys = [p["frame"].world(0, layout.TANK_Y)[1] for p in L["pads"]]
    ty = sum(tank_ys) / len(tank_ys) - (14.0 if L["rows"] == 1 else 0.0)
    s = max(1.0, (x1 - x0) / 450.0, (gy1 - gy0) / 155.0)
    cam_data = bpy.data.cameras.new("CAM_OVERVIEW")
    cam_data.lens = 24
    cam_data.clip_end = 3000 * s
    cam = bpy.data.objects.new("CAM_OVERVIEW", cam_data)
    collection.objects.link(cam)
    cx = (x0 + x1) / 2
    cam.location = (cx, ty - 267 * s, 170 * s)
    cam.rotation_euler = (Vector((cx, ty, 0)) - cam.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam
    sun_data = bpy.data.lights.new("SUN", "SUN")
    sun_data.energy = 3.5
    sun = bpy.data.objects.new("SUN", sun_data)
    sun.rotation_euler = (math.radians(50), 0, math.radians(35))
    collection.objects.link(sun)
    world = bpy.data.worlds.get("SKY") or bpy.data.worlds.new("SKY")
    world.color = (0.55, 0.6, 0.66)
    scene.world = world


def triangles(scene):
    """Each n-gon is n - 2 triangles, so a mesh has (corners - 2 x faces) of them."""
    return sum(len(o.data.loops) - 2 * len(o.data.polygons) for o in scene.objects if o.type == "MESH")


def build_scene(scene, bp, L, level):
    fp.LOD.update(LEVELS[level])
    clear_scene()
    field_build.build(scene.collection, bp, L)
    stage(scene.collection, scene, L)
    return triangles(scene)


def export_glb(path):
    """Write the glb. GPU instancing turns each SCENERY_*_instances Empty from optimize.py into
    one EXT_mesh_gpu_instancing node; nothing else in the scene qualifies (see optimize.py)."""
    kwargs = dict(filepath=path, export_format="GLB", use_active_scene=True, export_apply=True,
                  export_extras=True, export_cameras=True, export_lights=False, export_yup=True,
                  export_animations=False, export_gpu_instances=True)
    try:
        bpy.ops.export_scene.gltf(**kwargs)
    except TypeError as exc:
        print("exporter rejected an option, retrying with defaults:", exc)
        for key in ("use_active_scene", "export_animations", "export_lights", "export_cameras", "export_gpu_instances"):
            kwargs.pop(key, None)
        bpy.ops.export_scene.gltf(**kwargs)


def glb_json(path):
    with open(path, "rb") as f:
        raw = f.read()
    n, kind = struct.unpack_from("<II", raw, 12)
    if raw[:4] != b"glTF" or kind != 0x4E4F534A:
        raise ValueError(f"{path} is not a glb with a JSON chunk")
    return json.loads(raw[20:20 + n])


def glb_summary(g):
    """What the console has to draw: draw calls (one per primitive of a mesh node, so one per
    instanced node too), instanced nodes and their copies, meshes, and MB of embedded images
    (the plain field has none, so these are the scenery's textures)."""
    views, acc = g.get("bufferViews", []), g.get("accessors", [])
    draws = copies = 0
    held = []
    for nd in g.get("nodes", []):
        if "mesh" not in nd:
            continue
        draws += len(g["meshes"][nd["mesh"]]["primitives"])
        inst = nd.get("extensions", {}).get("EXT_mesh_gpu_instancing")
        if inst:
            held.append(nd.get("name", ""))
            copies += acc[inst["attributes"]["TRANSLATION"]]["count"]
    images = sum(views[i["bufferView"]]["byteLength"] for i in g.get("images", []) if "bufferView" in i)
    return {"draw_calls": draws, "meshes": len(g.get("meshes", [])), "instanced_nodes": len(held),
            "instances": copies, "images_mb": round(images / 1e6, 2), "instanced": sorted(held)}


RENDER_VIEWS = {   # camera and target offsets in the asset's own frame
    "tank": ((15.5, -24.0, 8.85), (0.0, -6.0, 2.35)), "well": ((7.5, -11.0, 4.5), (0.0, -1.5, 2.0)),
    "truck": ((11.0, -11.0, 4.9), (1.0, 0.0, 1.7)), "disposal_well": ((0.0, -40.0, 22.0), (0.0, 0.0, 2.0)),
    "valve": ((14.0, -20.0, 16.0), (0.0, 0.0, 1.0)), "yard": ((0.0, -45.0, 30.0), (0.0, 0.0, 0.0)),
    "pump": ((6.0, -8.0, 4.0), (0.0, 0.0, 1.0)), "equipment": ((9.0, -13.0, 7.0), (0.0, 0.0, 2.0)),
}


def render_checks(scene, out_dir, assets):
    """Workbench renders of the overview and a few assets, for a quick look."""
    os.makedirs(out_dir, exist_ok=True)
    scene.render.engine = "BLENDER_WORKBENCH"
    shading = scene.display.shading
    shading.light, shading.color_type = "STUDIO", "MATERIAL"
    shading.show_cavity, shading.show_shadows = True, True
    scene.render.resolution_x, scene.render.resolution_y = 1600, 900
    overview = scene.camera
    for aid in [None] + assets:
        cam = overview
        if aid:
            obj = bpy.data.objects.get(aid)
            if obj is None or obj.get("kind") not in RENDER_VIEWS:
                print(f"render: skipping {aid}, not an asset")
                continue
            eye, look = RENDER_VIEWS[obj["kind"]]
            m = obj.matrix_world
            data = bpy.data.cameras.new(f"CHECK_{aid}")
            data.lens = 28
            cam = bpy.data.objects.new(f"CHECK_{aid}", data)
            scene.collection.objects.link(cam)
            cam.location = m @ Vector(eye)
            cam.rotation_euler = (m @ Vector(look) - cam.location).to_track_quat("-Z", "Y").to_euler()
        scene.camera = cam
        scene.render.filepath = os.path.join(out_dir, f"{aid or 'overview'}.png")
        bpy.ops.render.render(write_still=True)
        if aid:
            bpy.data.objects.remove(cam)
    scene.camera = overview


class Clock:
    """Wall seconds per build step, for the report's "timing"."""

    def __init__(self):
        self.t, self.laps = time.time(), {}

    def lap(self, step):
        now = time.time()
        self.laps[step] = round(self.laps.get(step, 0.0) + now - self.t, 2)
        self.t = now


def write_report(report, out_dir, t0):
    report["seconds"] = round(time.time() - t0, 2)
    report["cpu_seconds"] = round(time.process_time(), 2)
    if out_dir:
        with open(os.path.join(out_dir, "build_report.json"), "w") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
    status = "OK" if report["ok"] else "FAILED"
    print(f"build_report: {status} in {report['seconds']} s" + "".join(f"\n  error: {e}" for e in report["errors"]))


def paths(opts):
    src = os.path.abspath(opts["blueprint"]) if opts["blueprint"] else DEFAULT_BLUEPRINT
    stem = os.path.splitext(os.path.basename(src))[0]
    out_dir = os.path.abspath(opts["out"]) if opts["out"] else (
        os.path.join(HERE, "out", stem) if opts["blueprint"] else HERE)
    os.makedirs(out_dir, exist_ok=True)
    return src, out_dir


def run(opts, src, out_dir, report):
    """The build itself. Returns the exit code."""
    clock = Clock()
    report["timing"] = clock.laps
    report["blueprint"] = rel(src)
    data, errors = blueprint.load(src)
    bp, more, warnings = blueprint.check(data) if data is not None else (None, [], [])
    report["errors"] += errors + more
    report["warnings"] += warnings
    if report["errors"]:
        return 1
    L = layout.plan(bp)
    report["warnings"] += L["warnings"]
    report["errors"] += L["errors"]
    if report["errors"]:
        return 1
    report["name"] = bp["name"]
    if bp["site"]:
        report["site"] = bp["site"]
    if bp["connectors"]:
        report["connectors"] = [c["id"] for c in bp["connectors"]]
    report["counts"] = blueprint.counts(bp)
    clock.lap("plan")

    order = list(LEVELS)
    if opts["detail"]:
        level = opts["detail"]
    else:
        floor = 0 if len(bp["tanks"]) <= FULL_DETAIL_MAX_TANKS else 1
        level = next((lv for lv in order[floor:] if estimate(bp, L, lv) <= BUDGET_TRIS * 0.97), order[-1])
    scene = bpy.context.scene
    scene.name = "Field"
    passes = []
    while True:
        tris = build_scene(scene, bp, L, level)
        passes.append({"detail": level, "estimated": estimate(bp, L, level), "triangles": tris})
        if tris <= BUDGET_TRIS or opts["detail"] or level == order[-1]:
            break
        level = order[order.index(level) + 1]

    field_tris, want = tris, set(names.expected(bp))
    clock.lap("field")
    view = {"on": False}
    if opts["scenery"]:
        view = scenery.dress(scene.collection, bp, L, int(BUDGET_TRIS * SCENERY_SHARE) - tris)
        tris = triangles(scene)
        view.update({"triangles": tris - field_tris, "field_triangles": field_tris})
        want.add("SCENERY")
        clock.lap("scenery")

    objs = list(scene.objects)
    dotted = sorted(o.name for o in objs if "." in o.name)
    if dotted:
        report["errors"].append("names clash with each other (Blender added suffixes): " + ", ".join(dotted[:12]))
    built = {o.name for o in objs}
    missing = sorted(want - built)
    if missing:
        report["errors"].append("the build is missing contract names: " + ", ".join(missing[:20]))
    if report["errors"]:
        return 2

    report["look"] = look.apply(scene, opts["look"], textures=opts["scenery"])
    glb, blend = os.path.join(out_dir, "field.glb"), os.path.join(out_dir, "field.blend")
    kinds, n_objects = Counter(o["kind"] for o in objs if "asset_id" in o.keys()), len(objs)
    del objs                             # optimize.run() removes merged objects
    clock.lap("checks")
    opt = optimize.run(scene, want)      # last step before export: merges, shares, groups scenery copies
    clock.lap("optimize")
    export_glb(glb)
    clock.lap("export")
    bpy.ops.wm.save_as_mainfile(filepath=blend, compress=True)
    clock.lap("blend")
    g = glb_json(glb)
    nodes, summary = g.get("nodes", []), glb_summary(g)
    gone = sorted(want - {n.get("name") for n in nodes})
    if gone:
        report["errors"].append("field.glb is missing names after export: " + ", ".join(gone[:20]))
        return 2
    stray = [n for n in summary["instanced"] if not (n.startswith("SCENERY_") and n.endswith(optimize.GROUP_SUFFIX))]
    if stray:
        report["errors"].append("the exporter instanced nodes outside SCENERY: " + ", ".join(stray[:12]))
        return 2
    mb = os.path.getsize(glb) / 1e6
    report.update({
        "ok": True,
        "outputs": {"glb": rel(glb), "blend": rel(blend), "report": rel(os.path.join(out_dir, "build_report.json"))},
        "kinds": dict(sorted(kinds.items())),
        "placed": {k: sorted(v) for k, v in L["placed"].items()},
        "layout": {"rows": L["rows"], "pad_pitch_m": layout.PAD_PITCH, "road_m": round(L["road"][1] - L["road"][0], 1),
                   "road_x": [round(v, 1) for v in L["road"]], "ground": [round(v, 1) for v in L["ground"]],
                   "crossovers": {x["id"]: x["style"] for x in L["crossovers"]}},
        "detail": {"level": level, "dropped": DROPPED[level], "passes": passes},
        "iot": {"sensors": len(names.sensors(bp)), "rtus": len(bp["tanks"])},
        "scenery": dict(view, **({"images_mb": summary["images_mb"]} if view["on"] else {})),
        "triangles": tris, "glb_mb": round(mb, 2), "nodes": len(nodes), "objects": n_objects,
        "draw_calls": summary["draw_calls"],
        "optimize": dict(opt, glb_meshes=summary["meshes"], instanced_nodes=summary["instanced_nodes"],
                         instances=summary["instances"]),
        "budget": {"triangles": BUDGET_TRIS, "mb": BUDGET_MB, "draw_calls": BUDGET_DRAWS},
    })
    if level != "full":
        why = (f"more than {FULL_DETAIL_MAX_TANKS} tanks" if len(bp["tanks"]) > FULL_DETAIL_MAX_TANKS
               else "full detail would pass the triangle budget") if not opts["detail"] else "--detail was set"
        report["warnings"].append(f"built at {level} detail ({why}); dropped: {', '.join(DROPPED[level])}")
    if tris > BUDGET_TRIS or mb > BUDGET_MB or summary["draw_calls"] > BUDGET_DRAWS:
        report["warnings"].append(f"over budget: {tris} triangles, {mb:.1f} MB and {summary['draw_calls']} draw calls "
                                  f"(budget {BUDGET_TRIS}, {BUDGET_MB:.0f} MB and {BUDGET_DRAWS})")
    if opts["render_dir"]:
        assets = (opts["render_assets"] or "").split(",") if opts["render_assets"] else [
            a for a in (bp["tanks"][0]["id"], bp["wells"][0]["id"] if bp["wells"] else None,
                        bp["trucks"][0]["id"] if bp["trucks"] else None, bp["swds"][-1]["id"]) if a]
        render_checks(scene, opts["render_dir"], [a for a in assets if a])
    clock.lap("report")
    return 0


def main():
    t0 = time.time()
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    report = {"ok": False, "errors": [], "warnings": []}
    code, out_dir = 2, None
    try:
        opts = parse_args(argv)
        src, out_dir = paths(opts)
        code = run(opts, src, out_dir, report)
    except UsageError as e:
        report["errors"].append(str(e))
    except Exception:  # noqa: BLE001
        report["errors"].append("the build crashed:\n" + traceback.format_exc())
    if code:
        report["ok"] = False
        if not report["errors"]:
            report["errors"].append("the build failed")
    write_report(report, out_dir, t0)
    if bpy.app.background:
        sys.exit(code)


if __name__ == "__main__":
    main()
