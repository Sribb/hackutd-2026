"""Build every generated model in a row, for a look and for the tests.

  /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
      --python blender/parts_preview.py -- --out DIR [--render]

Row 1 is each equipment model (#47) and the labeled placeholder. Row 2 is the generated
scenery: yucca, prickly pear, a barbed wire fence with a corner, and a cattle guard.
Writes DIR/parts.glb (open it with blender/preview/index.html) and DIR/parts_report.json,
which gives the triangles under each root. --render also saves DIR/parts.png, a Workbench
render. Exits 1 if anything fails to build.
"""
import math
import importlib
import json
import os
import random
import sys
import traceback

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import field_parts as fp  # noqa: E402
import detail_equipment as de  # noqa: E402
import equipment_parts as eq  # noqa: E402
import scenery_parts as sp  # noqa: E402

for _mod in (fp, de, eq, sp):
    importlib.reload(_mod)

EQUIPMENT = (("SEP_1", "SEPARATOR"), ("HT_1", "HEATER_TREATER"), ("FLARE_1", "FLARE"), ("COMP_1", "COMPRESSOR"),
             ("LACT_1", "LACT"), ("METER_1", "METER_RUN"), ("EQ_1", "OTHER"))


def tris(obj):
    out = 0
    for o in [obj] + list(obj.children_recursive):
        if o.type == "MESH":
            out += sum(len(p.vertices) - 2 for p in o.data.polygons)
    return out


def build(col):
    site = fp.Site(col)
    roots = []
    for i, (eid, etype) in enumerate(EQUIPMENT):
        model = etype if etype in eq.MODELS else None
        item = {"id": eid, "type": etype, "model": model, "label": None, "pos": (14.0 * i, 0.0), "rot": 0.0}
        roots.append(eq.build(site, item))
    rng = random.Random(47)
    row = -16.0

    def scenery(name, x):
        root = site.empty(f"SCENERY_{name}", (x, row, 0.0), size=1)
        return root, fp.Kit(site, f"SCENERY_{name}", root)

    root, k = scenery("yucca", 0.0)
    for j in range(3):
        sp.yucca(k, (2.5 * j, 0.0, 0.0), rng)
    k.done()
    roots.append(root)
    root, k = scenery("prickly_pear", 12.0)
    for j in range(3):
        sp.prickly_pear(k, (2.5 * j, 0.0, 0.0), rng)
    k.done()
    roots.append(root)
    root, k = scenery("barbed_wire_fence", 24.0)
    sp.barbed_wire_fence(k, [(0.0, 0.0), (20.0, 0.0), (20.0, -10.0)], rng)
    k.done()
    roots.append(root)
    root, k = scenery("cattle_guard", 54.0)
    sp.cattle_guard(k, (0.0, 0.0, 0.0), 0.0)
    k.done()
    roots.append(root)
    return roots


def render(scene, path):
    from mathutils import Vector
    data = bpy.data.cameras.new("CAM_PARTS")
    data.lens = 30
    cam = bpy.data.objects.new("CAM_PARTS", data)
    scene.collection.objects.link(cam)
    cam.location = (42.0, -58.0, 26.0)
    cam.rotation_euler = (Vector((42.0, -8.0, 1.0)) - cam.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam
    sun = bpy.data.objects.new("SUN", bpy.data.lights.new("SUN", "SUN"))
    sun.rotation_euler = (math.radians(50), 0, math.radians(35))
    scene.collection.objects.link(sun)
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.light, scene.display.shading.color_type = "STUDIO", "MATERIAL"
    scene.display.shading.show_cavity = scene.display.shading.show_shadows = True
    scene.render.resolution_x, scene.render.resolution_y = 1600, 900
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    out = os.path.abspath(argv[argv.index("--out") + 1]) if "--out" in argv else os.path.join(HERE, "out", "parts")
    os.makedirs(out, exist_ok=True)
    report = {"ok": False, "parts": {}, "errors": []}
    try:
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        roots = build(bpy.context.scene.collection)
        report["parts"] = {r.name: tris(r) for r in roots}
        bpy.ops.export_scene.gltf(filepath=os.path.join(out, "parts.glb"), export_format="GLB",
                                  export_apply=True, export_extras=True, export_yup=True)
        if "--render" in argv:
            render(bpy.context.scene, os.path.join(out, "parts.png"))
        report["ok"] = True
    except Exception:  # noqa: BLE001
        report["errors"].append(traceback.format_exc())
    with open(os.path.join(out, "parts_report.json"), "w") as f:
        json.dump(report, f, indent=2)
    print("parts_preview:", "OK" if report["ok"] else "FAILED", report["parts"], *report["errors"])
    if bpy.app.background:
        sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
