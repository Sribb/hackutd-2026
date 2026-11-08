"""Lab proof: bake Cycles ambient occlusion for one tank pad onto a second UV set and export it as the
glTF occlusion texture (three.js loads it as material.aoMap on uv channel 1). Headless only:

  /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
    --python blender/lab/bake_ao.py -- --blend blender/field.blend --out blender/out/lab

Writes field_base.glb (same blend, no AO), field_ao.glb, ao_atlas.jpg and bake_ao.json with the timings.
It never writes into the committed build: the blend is opened read-only and saved nowhere.
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
ap.add_argument("--center", default="TANK_1", help="bake everything within --radius m of this node")
ap.add_argument("--radius", type=float, default=30.0)
ap.add_argument("--size", type=int, default=2048, help="atlas size in px")
ap.add_argument("--samples", type=int, default=64)
ap.add_argument("--distance", type=float, default=1.5, help="AO ray length in m")
ap.add_argument("--device", default="GPU", choices=["GPU", "CPU"])
o = ap.parse_args(argv)
os.makedirs(o.out, exist_ok=True)
report = {"args": vars(o)}
T = time.time()

bpy.ops.wm.open_mainfile(filepath=os.path.abspath(o.blend))
scene = bpy.context.scene


def export(path):
    t = time.time()
    bpy.ops.export_scene.gltf(filepath=os.path.abspath(path), export_format="GLB", use_active_scene=True,
                              export_apply=True, export_extras=True, export_cameras=True, export_lights=False,
                              export_yup=True, export_animations=False)
    return round(time.time() - t, 2), round(os.path.getsize(path) / 1e6, 2)


report["base_export_s"], report["base_mb"] = export(os.path.join(o.out, "field_base.glb"))

# ---- pick the region: static meshes near the center node, never site, scenery, or moving parts -------
MOVING = re.compile(r"_(beam|crank|rotor|wheel|gauge|axle_\d|LEVEL|light|beacon|headlights|lamps|gate)$")
center = bpy.data.objects[o.center].matrix_world.translation.copy()


def under(ob, prefix):
    while ob:
        if ob.name.startswith(prefix):
            return True
        ob = ob.parent
    return False


region, skipped_uv = [], 0
for ob in scene.objects:
    if ob.type != "MESH" or not ob.visible_get():
        continue
    if under(ob, "SITE_") or under(ob, "SCENERY") or MOVING.search(ob.name):
        continue
    c = sum((ob.matrix_world @ Vector(v) for v in ob.bound_box), Vector()) / 8
    if (Vector((c.x, c.y)) - Vector((center.x, center.y))).length > o.radius:
        continue
    if len(ob.data.uv_layers):
        skipped_uv += 1          # already textured: lookdev owns its UV0, leave it alone here
        continue
    region.append(ob)
report["region_objects"] = len(region)
report["region_skipped_textured"] = skipped_uv
report["region_triangles"] = sum(sum(len(p.vertices) - 2 for p in ob.data.polygons) for ob in region)

# ---- own data per object, UV0 placeholder + AO_UV second set, one shared atlas ------------------------
for ob in region:
    if ob.data.users > 1:
        ob.data = ob.data.copy()
    ob.data.uv_layers.new(name="UVMap")          # TEXCOORD_0: the slot lookdev's tiling UVs will fill
    ao = ob.data.uv_layers.new(name="AO_UV")     # TEXCOORD_1: unique, non-overlapping, for the bake
    ob.data.uv_layers.active = ao

t = time.time()
bpy.ops.object.select_all(action="DESELECT")
for ob in region:
    ob.select_set(True)
bpy.context.view_layer.objects.active = region[0]
scene.tool_settings.use_uv_select_sync = True
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=0.0, scale_to_bounds=False)
bpy.ops.uv.average_islands_scale()
bpy.ops.uv.pack_islands(rotate=True, margin=0.003)
bpy.ops.object.mode_set(mode="OBJECT")
report["unwrap_s"] = round(time.time() - t, 2)

# ---- material copies for the region with an AO image node wired to glTF occlusion --------------------
img = bpy.data.images.new("ao_atlas", o.size, o.size, alpha=False, float_buffer=False)
img.colorspace_settings.name = "Non-Color"
img.generated_color = (1, 1, 1, 1)


def gltf_output_group():
    g = bpy.data.node_groups.get("glTF Material Output")
    if g is None:
        g = bpy.data.node_groups.new("glTF Material Output", "ShaderNodeTree")
        g.interface.new_socket("Occlusion", in_out="INPUT", socket_type="NodeSocketFloat")
    return g


copies = {}
for ob in region:
    for slot in ob.material_slots:
        m = slot.material
        if m is None:
            continue
        if m.name not in copies:
            c = m.copy()
            c.name = m.name + "_AO"
            c.use_nodes = True
            nt = c.node_tree
            uv = nt.nodes.new("ShaderNodeUVMap"); uv.uv_map = "AO_UV"
            tex = nt.nodes.new("ShaderNodeTexImage"); tex.image = img
            sep = nt.nodes.new("ShaderNodeSeparateColor")
            grp = nt.nodes.new("ShaderNodeGroup"); grp.node_tree = gltf_output_group()
            nt.links.new(uv.outputs["UV"], tex.inputs["Vector"])
            nt.links.new(tex.outputs["Color"], sep.inputs[0])
            nt.links.new(sep.outputs[0], grp.inputs["Occlusion"])
            nt.nodes.active = tex                    # the bake writes into the active image node
            copies[m.name] = c
        slot.material = copies[m.name]
report["material_copies"] = len(copies)

# ---- Cycles AO bake --------------------------------------------------------------------------------
scene.render.engine = "CYCLES"
if o.device == "GPU":
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = "METAL"
    prefs.get_devices()
    for d in prefs.devices:
        d.use = True
    scene.cycles.device = "GPU"
report["devices"] = [d.name for d in bpy.context.preferences.addons["cycles"].preferences.devices if d.use]
scene.cycles.samples = o.samples
if scene.world is None:
    scene.world = bpy.data.worlds.new("World")
scene.world.light_settings.distance = o.distance
scene.render.bake.margin = 4
t = time.time()
bpy.ops.object.bake(type="AO", margin=4, use_clear=True)
report["bake_s"] = round(time.time() - t, 2)

jpg = os.path.abspath(os.path.join(o.out, "ao_atlas.jpg"))
scene.render.image_settings.file_format = "JPEG"
scene.render.image_settings.quality = 90
scene.render.image_settings.color_mode = "BW"
img.save_render(jpg, scene=scene)
img.filepath = jpg
img.source = "FILE"
img.reload()
report["atlas_kb"] = round(os.path.getsize(jpg) / 1e3)

report["ao_export_s"], report["ao_mb"] = export(os.path.join(o.out, "field_ao.glb"))
report["total_s"] = round(time.time() - T, 2)
with open(os.path.join(o.out, "bake_ao.json"), "w") as f:
    json.dump(report, f, indent=2)
print("BAKE_AO", json.dumps(report))
