"""The desert scenery pass (#88, #98): dress the built field as a Permian Basin lease.

dress() runs after the field is built. It textures the ground with the CC0 kit's dry soil and
the road, pads, and berms with its caliche, and multiplies a vertex color layer (Col) into
each: broad paler and redder patches and caliche dust on the ground, so no tile grid shows
from the overview, and the wheel tracks and shoulders field_build.py painted on the road.

Everything else goes under one Empty named SCENERY: the terrain from the lease's edge to the
horizon with a ring of caprock mesas, the lease road and power line running out of the lease,
other operators' pads (beyond.py builds those), a barbed wire ranch fence along the lease edge
with cattle guards where the road leaves, an RRC style lease sign and H2S warnings, pipeline
markers where the trunk runs under a driveway, and a seeded scatter of plants and rocks,
thickest along the fence and thinning out past it. Nothing under SCENERY carries
an asset ID, so it is never clickable, and the console can hide the whole layer by that root.
siting.plan_scenery() keeps the scatter off pads, roads, pipes, and truck routes.

Kit models (blender/assets/scenery) are imported once and shared: every copy is its own node
pointing at the same mesh, so the glb stores each model once. Rocks and branches are
decimated on import to fit the triangle budget. Generated plants and signs (scenery_parts.py)
merge into one mesh per material.
"""
import math
import os
import random

import bmesh
import bpy
from mathutils import Matrix

import field_build
import field_parts as fp
import layout as lay
import scenery_parts as sp
import siting
import terrain
from beyond import POWER_SPANS, caliche_tint, caliche_uv, far_road, land, mesas, neighbor_leases, power_out, \
    surface, tri_count, uvs

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
KIT = {"grass": ["plants/dry_grass_2"], "rock": ["rocks/rock_1", "rocks/rock_2"],
       "boulder": ["rocks/boulder_1", "rocks/boulder_2"], "branch": ["plants/dead_branch_1", "plants/dead_branch_2"],
       "creosote": ["plants/creosote_1"], "mesquite": ["plants/mesquite_1"]}
DECIMATE = {"rocks/rock_1": 0.25, "rocks/rock_2": 0.25, "rocks/boulder_1": 0.2, "rocks/boulder_2": 0.2,
            "plants/dead_branch_1": 0.2, "plants/dead_branch_2": 0.2}
SOIL, CALICHE = "ground_dry_soil", "ground_caliche"
# Materials that multiply the vertex colors in Col into their texture, and the texture each gets.
TINTED = {"M_GROUND": SOIL, "M_ROAD": CALICHE, "M_GRAVEL": CALICHE, "M_BERM": CALICHE, "M_MESA": None}
OPERATOR = "HIGH DESERT OPERATING, LLC"      # made up: no real operator's name on the sign
# About what each optional piece costs in triangles (measured on the default field).
MESA_TRIS, ROAD_TRIS, SIGN_TRIS, PLATE_TRIS, POWER_TRIS, LEASE_TRIS, OUTSIDE_TRIS = 2000, 2200, 1600, 450, 4600, 5000, 14000
WHAT = {"road": "the road out of the lease", "signs": "signs", "power": "the power line out of the lease",
        "leases": "other leases", "brush": "brush past the fence"}


def kit_meshes(paths):
    """{path: mesh} for kit models, each imported once (decimated if listed).

    They are imported into a scratch scene: the importer deselects every object in the scene
    it imports into, and the decimation evaluates every object in it, so doing either in the
    built field would cost seconds per model."""
    cache = {}
    if not paths:
        return cache
    tmp = bpy.data.scenes.new("_kit_import")
    layer = tmp.view_layers[0]
    with bpy.context.temp_override(scene=tmp, view_layer=layer):
        for path in sorted(set(paths)):
            before = set(tmp.objects)
            bpy.ops.import_scene.gltf(filepath=os.path.join(ASSETS, "scenery", path + ".glb"))
            new = [o for o in tmp.objects if o not in before]
            src = next(o for o in new if o.type == "MESH")
            me = src.data
            if path in DECIMATE:
                mod = src.modifiers.new("decimate", "DECIMATE")
                mod.ratio = DECIMATE[path]
                layer.depsgraph.update()
                me = bpy.data.meshes.new_from_object(src.evaluated_get(layer.depsgraph))
            me.name = "KIT_" + os.path.basename(path)
            cache[path] = me
    for o in list(tmp.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    bpy.data.scenes.remove(tmp)
    return cache


def resized(path, size=1024, flatten=None):
    """The kit image at path, at size x size, as a packed JPEG (so the glb stores it small).

    flatten=b takes out the image's broad light and dark patches: a gaussian blur of b tiles is
    subtracted and the mean added back, leaving cracks, pebbles and grain. Those patches are what
    repeat as a grid from far away; the ground's vertex colors replace them with patches that
    never repeat."""
    import numpy as np
    name = os.path.splitext(os.path.basename(path))[0] + ("_detail" if flatten else f"_{size}")
    if name in bpy.data.images:
        return bpy.data.images[name]
    src = bpy.data.images.load(path, check_existing=True).copy()
    src.scale(size, size)
    if flatten:
        px = np.empty(size * size * 4, dtype=np.float32)
        src.pixels.foreach_get(px)
        px = px.reshape(size, size, 4)
        rgb = px[..., :3]
        f = np.fft.fftfreq(size)
        g = np.exp(-2 * (np.pi * flatten * size) ** 2 * (f[:, None] ** 2 + f[None, :] ** 2))
        low = np.stack([np.real(np.fft.ifft2(np.fft.fft2(rgb[..., c]) * g)) for c in range(3)], axis=-1)
        px[..., :3] = np.clip(rgb - low + rgb.mean(axis=(0, 1)), 0.0, 1.0)
        src.pixels.foreach_set(px.ravel())
    tmp = os.path.join(bpy.app.tempdir or "/tmp", name + ".jpg")
    src.file_format = "JPEG"
    src.save(filepath=tmp, quality=88)       # raw pixels: no view transform, unlike save_render
    bpy.data.images.remove(src)
    img = bpy.data.images.load(tmp)
    img.name = name
    img.pack()
    return img


def tinted(mat_name, tex):
    """A palette material whose base color is the kit's color map (if tex) times the Col vertex
    colors, with the kit's normal map. Principled BSDF, so glTF keeps the texture and COLOR_0.
    The soil's color map is its detail() version."""
    mat = fp.material(mat_name)
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = next(n for n in nodes if n.type == "BSDF_PRINCIPLED")
    vc = nodes.new("ShaderNodeVertexColor")
    vc.layer_name = "Col"
    if tex is None:
        links.new(vc.outputs["Color"], bsdf.inputs["Base Color"])
        return mat
    folder = os.path.join(ASSETS, "textures", tex)
    img = nodes.new("ShaderNodeTexImage")
    color = os.path.join(folder, f"{tex}_color.jpg")
    img.image = resized(color, flatten=0.04) if tex == SOIL else bpy.data.images.load(color, check_existing=True)
    mix = nodes.new("ShaderNodeMix")
    mix.data_type, mix.blend_type = "RGBA", "MULTIPLY"
    mix.inputs["Factor"].default_value = 1.0
    links.new(img.outputs["Color"], mix.inputs[6])
    links.new(vc.outputs["Color"], mix.inputs[7])
    links.new(mix.outputs[2], bsdf.inputs["Base Color"])
    nimg = nodes.new("ShaderNodeTexImage")
    normal = os.path.join(folder, f"{tex}_normal_gl.jpg")
    nimg.image = resized(normal) if tex == SOIL else bpy.data.images.load(normal, check_existing=True)
    nimg.image.colorspace_settings.name = "Non-Color"
    nmap = nodes.new("ShaderNodeNormalMap")
    links.new(nimg.outputs["Color"], nmap.inputs["Color"])
    links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    bsdf.inputs["Roughness"].default_value = 0.95
    return mat


def paint(me, color_at):
    """Give a mesh a Col layer from color_at(x, y) in its own coordinates, if it has none."""
    if "Col" in me.color_attributes:
        return
    attr = me.color_attributes.new("Col", "FLOAT_COLOR", "POINT")
    flat = []
    for v in me.vertices:
        flat += [*color_at(v.co.x, v.co.y), 1.0]
    attr.data.foreach_set("color", flat)


def lease_lines(bp, seed):
    """The lease sign's text: made-up operator, the lease, its wells, a made-up RRC number."""
    site = bp.get("site") or {}
    name = (site.get("name") or bp.get("name") or "Lease").split(" - ")[0].strip().upper()
    if not name.endswith("LEASE"):
        name += " LEASE"
    rrc = 40000 + random.Random(f"rrc:{seed}").randrange(10000)
    n = len(bp["wells"])
    wells = f"WELLS 1 - {n}" if n > 1 else ("WELL 1" if n else "SALT WATER DISPOSAL")
    return [str(site.get("operator") or OPERATOR).upper()[:30], name[:30], wells, f"RRC LEASE NO. {rrc}",
            "EMERGENCY 432-555-0147"]


def markers(L):
    """Plan (x, y) of the yellow pipeline markers beside each driveway the trunk runs under."""
    return [(x, lay.TRUNK_Y - 1.5) for x0, x1 in L.get("trunk_dips", []) for x in (x0 - 1.6, x1 + 1.6)]


def signs(root, bp, L, plan, seed):
    """SCENERY_signs: the lease sign at the west entrance, an H2S plate on every pad's sign post,
    and yellow markers where the trunk runs under a driveway."""
    k = fp.Kit(fp.Site(bpy.context.scene.collection), "SCENERY_signs", root)
    if plan["sign"]:
        x, y = plan["sign"]
        sp.lease_sign(k, (x, y, terrain.lease_height(x, y) - 0.1), math.radians(-135), lease_lines(bp, seed))
    for p in L["pads"]:
        f = p["frame"]
        sp.h2s_plate(k, Matrix.Translation(f.world3(-11.3, 27.43, 1.12)) @ Matrix.Rotation(f.rot, 4, "Z"))
    for x, y in markers(L):
        sp.marker(k, (x, y, terrain.lease_height(x, y) - 0.1))
    k.done()


def dress(collection, bp, L, budget):
    """Texture and tint the ground, roads, and pads, then build the SCENERY layer inside budget
    triangles. Returns a summary."""
    seed = bp["name"] or "field"
    L["seed"] = seed
    near = siting.dust(L)
    for mat, tex in TINTED.items():
        tinted(mat, tex)
    ground = bpy.data.objects.get("SITE_ground")
    if ground:
        paint(ground.data, lambda x, y: terrain.tint(x, y, near))
        uvs(ground.data, terrain.soil_uv)
    for obj in collection.all_objects:
        if obj.type == "MESH" and obj.data.materials and obj.data.materials[0] \
                and obj.data.materials[0].name in ("M_ROAD", "M_GRAVEL", "M_BERM"):
            paint(obj.data, caliche_tint)
            uvs(obj.data, caliche_uv)

    site = fp.Site(collection)
    root = site.empty("SCENERY", (0.0, 0.0, 0.0), size=2)
    root["kind"] = "scenery"
    # The terrain always comes (coarser when the field leaves little), then what fits, in
    # order: mesas, the road out, signs, other leases; the fence and the scatter get the rest.
    full = len(terrain.ring(L["ground"])[1])
    pts, zs, tris = land(root, L, near, terrain.RINGS if budget >= full + 30_000 else terrain.RINGS_LITE)
    left, dropped = budget - len(tris), []
    if left >= MESA_TRIS + 10_000:
        left -= tri_count(mesas(root, L))
    else:
        dropped.append("mesas")
    wants = [("road", ROAD_TRIS), ("signs", SIGN_TRIS + PLATE_TRIS * len(L["pads"])), ("power", POWER_TRIS),
             ("leases", LEASE_TRIS), ("brush", OUTSIDE_TRIS)]
    keep = set()
    for name, cost in wants:
        if left - cost >= 20_000:
            keep.add(name)
            left -= cost
        else:
            dropped.append(WHAT[name])
    plan = siting.plan_scenery(L, left, seed, [siting.around(x, y, 0.6, 0.6) for x, y in markers(L)])
    plan["dropped"] = dropped + plan["dropped"]
    at = surface(pts, zs, tris)
    if "road" in keep:
        far_road(root, L, plan["guards"], at)
    if "signs" in keep:
        signs(root, bp, L, plan, seed)
    if "power" in keep:
        power_out(root, L, at)
    if "leases" in keep:
        neighbor_leases(root, L, at, seed)
    outside = siting.plan_outside(L, OUTSIDE_TRIS, seed) if "brush" in keep else []
    rng = random.Random(f"freeboard-scenery-parts:{seed}")
    if plan["fence"]:
        k = fp.Kit(site, "SCENERY_fence", root)
        for line in plan["fence"]:
            sp.barbed_wire_fence(k, line, rng, post_gap=5.0, ground=field_build.ground_height)
        k.done()
        k = fp.Kit(site, "SCENERY_cattle_guard", root)
        for x, y in plan["guards"]:
            sp.cattle_guard(k, (x, y, 0.0), 0.0, width=2 * lay.ROAD_HALF + 1.0)
        k.done()

    cache = kit_meshes([p for kind in {s[0] for s in plan["scatter"] + outside} if kind in KIT for p in KIT[kind]])
    counts = {}
    b = L["ground"]

    def height(x, y):
        """The lease ground inside it, the terrain mesh's own surface outside."""
        if b[0] <= x <= b[1] and b[2] <= y <= b[3]:
            return terrain.lease_height(x, y)
        z = at(x, y)
        return terrain.height(x, y, b) if z is None else z

    def place(kind, path, x, y, rot, scale, sink=0.03):
        n = counts[kind] = counts.get(kind, 0) + 1
        obj = bpy.data.objects.new(f"SCENERY_{kind}_{n}", cache[path])
        obj.parent = root
        obj.location = (x, y, height(x, y) - sink)
        obj.rotation_euler = (0.0, 0.0, rot)
        obj.scale = (scale, scale, scale)
        obj["scenery"] = os.path.basename(path)
        collection.objects.link(obj)

    if plan["sign"] and "signs" in keep:
        counts["lease_sign"] = 1
    gen = {"yucca": fp.Kit(site, "SCENERY_yucca", root), "prickly_pear": fp.Kit(site, "SCENERY_prickly_pear", root)}
    for i, (kind, x, y, rot, scale) in enumerate(plan["scatter"] + outside):
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
    return {"on": True, "seed": seed, "textures": [SOIL, CALICHE], "fence_m": round(fence_m, 1),
            "cattle_guards": len(plan["guards"]), "terrain_m": terrain.HORIZON,
            "mesas": 0 if "mesas" in dropped else 24,
            "other_leases": len(terrain.neighbors(L["ground"], seed)) if "leases" in keep else 0,
            "power_spans": POWER_SPANS if "power" in keep else 0, "brush_past_fence": len(outside),
            "items": dict(sorted(counts.items())), "dropped": plan["dropped"]}
