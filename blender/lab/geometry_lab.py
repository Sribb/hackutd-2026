"""Lab proofs for geometry: bevels + weighted normals on one pad, and Draco + WebP compression of the
whole field. Headless only, and it never saves the blend:

  Blender --background --factory-startup --python blender/lab/geometry_lab.py -- \
    --blend blender/field.blend --out blender/out/lab --mode bevel|compress

bevel:    on the hard-surface parts within --radius m of --center, adds a Bevel (--segments, 30 degree
          angle limit, hardened normals) and a Weighted Normal modifier, then exports field_bevel_s<N>.glb.
          --segments 0 skips the bevel: smooth by --angle degrees plus Weighted Normal, zero new triangles.
compress: exports field_draco.glb (KHR_draco_mesh_compression) and field_draco_webp.glb (plus
          EXT_texture_webp) from the unchanged blend.
Writes geometry_<mode>.json with sizes, triangle counts and timings.
"""
import argparse
import json
import math
import os
import re
import sys
import time

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--blend", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--mode", required=True, choices=["bevel", "compress"])
ap.add_argument("--center", default="TANK_1")
ap.add_argument("--radius", type=float, default=30.0)
ap.add_argument("--width", type=float, default=0.025, help="bevel width in m")
ap.add_argument("--segments", type=int, default=2)
ap.add_argument("--angle", type=float, default=30.0, help="segments 0: edges sharper than this stay hard")
ap.add_argument("--min-size", type=float, default=0.0, help="bevel only parts whose largest side is at least this, in m")
o = ap.parse_args(argv)
os.makedirs(o.out, exist_ok=True)
bpy.ops.wm.open_mainfile(filepath=os.path.abspath(o.blend))
scene = bpy.context.scene
report = {"args": vars(o)}
BASE = dict(export_format="GLB", use_active_scene=True, export_apply=True, export_extras=True,
            export_cameras=True, export_lights=False, export_yup=True, export_animations=False)


def export(name, **extra):
    path = os.path.abspath(os.path.join(o.out, name))
    t = time.time()
    bpy.ops.export_scene.gltf(filepath=path, **BASE, **extra)
    return {"file": name, "export_s": round(time.time() - t, 2), "mb": round(os.path.getsize(path) / 1e6, 2)}


def tris(objs):
    dg = bpy.context.evaluated_depsgraph_get()
    n = 0
    for ob in objs:
        me = ob.evaluated_get(dg).to_mesh()
        n += sum(len(p.vertices) - 2 for p in me.polygons)
        ob.evaluated_get(dg).to_mesh_clear()
    return n


def under(ob, prefix):
    while ob:
        if ob.name.startswith(prefix):
            return True
        ob = ob.parent
    return False


if o.mode == "bevel":
    # Hard-surface parts only: steel, concrete, cabinets. Not plants, cables, the liner, or moving parts.
    SKIP = re.compile(r"_(LEVEL|light|beacon|headlights|lamps|label|sign|tag|wire|cable|liner|brine)")
    center = bpy.data.objects[o.center].matrix_world.translation.copy()
    region = []
    for ob in scene.objects:
        if ob.type != "MESH" or not ob.visible_get() or under(ob, "SITE_") or under(ob, "SCENERY") or SKIP.search(ob.name):
            continue
        c = sum((ob.matrix_world @ Vector(v) for v in ob.bound_box), Vector()) / 8
        if (Vector((c.x, c.y)) - Vector((center.x, center.y))).length <= o.radius and max(ob.dimensions) >= o.min_size:
            region.append(ob)
    report["region_objects"] = len(region)
    report["region_tris_before"] = tris(region)
    t = time.time()
    for ob in region:
        if ob.data.users > 1:
            ob.data = ob.data.copy()
        for p in ob.data.polygons:
            p.use_smooth = True
        if o.segments == 0:
            # No bevel: smooth by angle, then weighted normals. Zero extra triangles.
            ob.data.set_sharp_from_angle(angle=math.radians(o.angle))
            wn = ob.modifiers.new("LabWeightedNormal", "WEIGHTED_NORMAL")
            wn.keep_sharp = True
            wn.weight = 50
            continue
        bev = ob.modifiers.new("LabBevel", "BEVEL")
        bev.width = o.width
        bev.segments = o.segments
        bev.limit_method = "ANGLE"
        bev.angle_limit = math.radians(30)
        bev.harden_normals = True
        bev.use_clamp_overlap = True
        wn = ob.modifiers.new("LabWeightedNormal", "WEIGHTED_NORMAL")
        wn.keep_sharp = True
        wn.weight = 50
    report["region_tris_after"] = tris(region)
    report["modifier_s"] = round(time.time() - t, 2)
    report["export"] = export(f"field_bevel_s{o.segments}.glb")
else:
    report["plain"] = export("field_plain.glb")
    report["draco"] = export("field_draco.glb", export_draco_mesh_compression_enable=True,
                             export_draco_mesh_compression_level=6, export_draco_position_quantization=14,
                             export_draco_normal_quantization=10, export_draco_texcoord_quantization=12)
    report["draco_webp"] = export("field_draco_webp.glb", export_draco_mesh_compression_enable=True,
                                  export_draco_mesh_compression_level=6, export_draco_position_quantization=14,
                                  export_draco_normal_quantization=10, export_draco_texcoord_quantization=12,
                                  export_image_format="WEBP")
    report["webp_only"] = export("field_webp.glb", export_image_format="WEBP")

with open(os.path.join(o.out, f"geometry_{o.mode}{'_s%d' % o.segments if o.mode == 'bevel' else ''}.json"), "w") as f:
    json.dump(report, f, indent=2)
print("GEOMETRY_LAB", json.dumps(report))
