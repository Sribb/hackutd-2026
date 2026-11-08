"""The desert scenery pass (#88): dress the built field as West Texas at night.

dress() runs after the field is built. It textures the ground and the lease road with the
CC0 kit's dry soil and dirt road tracks, then puts everything else under one Empty named
SCENERY: a barbed wire ranch fence along the lease edge, cattle guards and road stubs where
the lease road leaves, a lease sign, and a seeded scatter of plants and rocks. Nothing under
SCENERY carries an asset ID, so it is never clickable, and the console can hide the whole
layer by that root. siting.plan_scenery() decides where everything goes and keeps it off
pads, roads, pipes, and truck routes.

Kit models (blender/assets/scenery) are imported once and shared: every copy is its own
node pointing at the same mesh, so the glb stores each model once. Rocks and branches are
decimated on import to fit the triangle budget. Generated plants (scenery_parts.py) merge
into one mesh per material.
"""
import math
import os
import random

import bpy

import field_build
import field_parts as fp
import layout as lay
import scenery_parts as sp
import siting

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
KIT = {"grass": ["plants/dry_grass_2"], "rock": ["rocks/rock_1", "rocks/rock_2"],
       "boulder": ["rocks/boulder_1", "rocks/boulder_2"], "branch": ["plants/dead_branch_1", "plants/dead_branch_2"],
       "creosote": ["plants/creosote_1"], "mesquite": ["plants/mesquite_1"], "sign": ["props/lease_sign_1"]}
DECIMATE = {"rocks/rock_1": 0.25, "rocks/rock_2": 0.25, "rocks/boulder_1": 0.2, "rocks/boulder_2": 0.2,
            "plants/dead_branch_1": 0.2, "plants/dead_branch_2": 0.2}
# Texture set and meters per tile. The soil scan covers 4 m, but at 4 m its light and dark
# patches repeat in a visible grid from the console's camera, so it is stretched to 8 m.
GROUND_TEX = ("ground_dry_soil", 8.0)
ROAD_TEX = ("dirt_road_tracks", 7.0)         # one tile across the 7 m road; the tracks run along the image's v


def kit_mesh(path, cache):
    """The mesh of a kit model, imported once (decimated if listed), with its imported objects removed."""
    if path in cache:
        return cache[path]
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=os.path.join(ASSETS, "scenery", path + ".glb"))
    new = [o for o in bpy.data.objects if o not in before]
    src = next(o for o in new if o.type == "MESH")
    me = src.data
    if path in DECIMATE:
        mod = src.modifiers.new("decimate", "DECIMATE")
        mod.ratio = DECIMATE[path]
        me = bpy.data.meshes.new_from_object(src.evaluated_get(bpy.context.evaluated_depsgraph_get()))
    me.name = "KIT_" + os.path.basename(path)
    for o in new:
        bpy.data.objects.remove(o, do_unlink=True)
    cache[path] = me
    return me


def textured(mat_name, tex, normal=True):
    """Give a palette material the kit's color and normal maps (Principled BSDF, so glTF keeps them)."""
    mat = fp.material(mat_name)
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = next(n for n in nodes if n.type == "BSDF_PRINCIPLED")
    folder = os.path.join(ASSETS, "textures", tex)
    img = nodes.new("ShaderNodeTexImage")
    img.image = bpy.data.images.load(os.path.join(folder, f"{tex}_color.jpg"), check_existing=True)
    links.new(img.outputs["Color"], bsdf.inputs["Base Color"])
    if normal:
        nimg = nodes.new("ShaderNodeTexImage")
        nimg.image = bpy.data.images.load(os.path.join(folder, f"{tex}_normal_gl.jpg"), check_existing=True)
        nimg.image.colorspace_settings.name = "Non-Color"
        nmap = nodes.new("ShaderNodeNormalMap")
        links.new(nimg.outputs["Color"], nmap.inputs["Color"])
        links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    bsdf.inputs["Roughness"].default_value = 0.95
    return mat


def planar_uvs(obj, tile, along_x=None):
    """World-planar UVs. along_x(face center) says a face belongs to a road running along X, so
    its texture v follows X; otherwise v follows Y, as on a driveway."""
    me = obj.data
    uv = me.uv_layers.new(name="UVMap")
    for poly in me.polygons:
        c = poly.center
        for li in poly.loop_indices:
            co = me.vertices[me.loops[li].vertex_index].co
            if along_x is None:
                uv.data[li].uv = (co.x / tile, co.y / tile)
            elif along_x(c):
                uv.data[li].uv = ((co.y + lay.ROAD_HALF) / tile, co.x / tile)
            else:
                uv.data[li].uv = (co.x / tile, co.y / tile)


def along_road(L):
    """True for a face on the lease road (texture runs along X), False for one on a driveway."""
    rects = L["driveways"]
    return lambda c: not any(r[0] - 0.01 <= c.x <= r[1] + 0.01 and r[2] - 0.01 <= c.y <= r[3] + 0.01 for r in rects)


def dress(collection, bp, L, budget):
    """Texture the ground and road, then build the SCENERY layer inside budget triangles. Returns a summary."""
    seed = bp["name"] or "field"
    plan = siting.plan_scenery(L, budget, seed)
    ground, road = bpy.data.objects.get("SITE_ground"), bpy.data.objects.get("SITE_road")
    textures = []
    if ground:
        planar_uvs(ground, GROUND_TEX[1])
        textured("M_GROUND", GROUND_TEX[0])
        textures.append(GROUND_TEX[0])
    if road:
        planar_uvs(road, ROAD_TEX[1], along_road(L))
        textured("M_ROAD", ROAD_TEX[0])
        textures.append(ROAD_TEX[0])

    site = fp.Site(collection)
    root = site.empty("SCENERY", (0.0, 0.0, 0.0), size=2)
    root["kind"] = "scenery"
    rng = random.Random(f"freeboard-scenery-parts:{seed}")
    height = field_build.ground_height
    if plan["fence"]:
        k = fp.Kit(site, "SCENERY_fence", root)
        for line in plan["fence"]:
            sp.barbed_wire_fence(k, line, rng, post_gap=5.0, ground=height)
        k.done()
        k = fp.Kit(site, "SCENERY_cattle_guard", root)
        for x, y in plan["guards"]:
            sp.cattle_guard(k, (x, y, 0.0), 0.0, width=2 * lay.ROAD_HALF + 1.0)
        k.done()
        k = fp.Kit(site, "SCENERY_road", root)
        for x0, x1, y0, y1 in plan["stubs"]:
            if x1 - x0 > 0.2:
                fp.box(k["M_ROAD"], (x1 - x0, y1 - y0, 0.07), ((x0 + x1) / 2, (y0 + y1) / 2, 0.035))
        k.done()
        stubs = bpy.data.objects.get("SCENERY_road_road")
        if stubs:
            planar_uvs(stubs, ROAD_TEX[1], lambda c: True)

    cache, counts = {}, {}

    def place(kind, path, x, y, rot, scale, sink=0.03):
        n = counts[kind] = counts.get(kind, 0) + 1
        obj = bpy.data.objects.new(f"SCENERY_{kind}_{n}", kit_mesh(path, cache))
        obj.parent = root
        obj.location = (x, y, height(x, y) - sink)
        obj.rotation_euler = (0.0, 0.0, rot)
        obj.scale = (scale, scale, scale)
        obj["scenery"] = os.path.basename(path)
        collection.objects.link(obj)

    if plan["sign"]:
        place("lease_sign", KIT["sign"][0], *plan["sign"], 0.0, 1.0, sink=0.1)
    gen = {"yucca": fp.Kit(site, "SCENERY_yucca", root), "prickly_pear": fp.Kit(site, "SCENERY_prickly_pear", root)}
    for i, (kind, x, y, rot, scale) in enumerate(plan["scatter"]):
        if kind in gen:
            prng = random.Random(f"{seed}:{i}")
            build = sp.yucca if kind == "yucca" else sp.prickly_pear
            build(gen[kind], (x, y, height(x, y) - 0.05), prng, scale)
            counts[kind] = counts.get(kind, 0) + 1
        else:
            variants = KIT[kind]
            place(kind, variants[counts.get(kind, 0) % len(variants)], x, y, rot, scale)
    for k in gen.values():
        k.done()
    fence_m = sum(math.dist(p, q) for line in plan["fence"] for p, q in zip(line, line[1:]))
    return {"on": True, "seed": seed, "textures": textures, "fence_m": round(fence_m, 1),
            "cattle_guards": len(plan["guards"]), "items": dict(sorted(counts.items())), "dropped": plan["dropped"]}
