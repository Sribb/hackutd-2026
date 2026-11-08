"""The look pass (#99): realistic, muted PBR materials, UVs and weathering on a built field.

build_field.py calls apply() once, right before the glb export. It never adds, removes,
renames or moves an object, and it never changes a mesh's triangles: it only swaps material
slots, adds a UV map and adds one color attribute. Every contract name, pivot, moving part
and triangle count stays exactly as built.

1. Roles. Each mesh gets a material by role, worked out from its asset (the nearest parent
   with a kind), its own name, and the kit material it was built with. M_STEEL under a well
   becomes pumpjack paint, under a pump the pump casing's slate blue, on a tank's ladder
   galvanized steel; flowlines from wells become black HDPE. New parts that use the kit
   names get a role automatically, and anything unknown keeps its kit material, restyled.
   M_LAMP, M_BRINE, M_GROUND, M_ROAD, M_WIRE and every material that already carries an
   image (the scenery kit, scenery.py's ground, road and pads) are left alone, and so are
   their UVs and color layers; a mesh on one of those that has none gets box UVs and a
   white layer, so it never renders black.
2. UVs: a box projection in each mesh's own frame, in meters, so a tiling texture keeps the
   same real-world size on every part, with an offset per kind of part.
3. Weathering into the color attribute "Col" (glTF COLOR_0, which three.js multiplies into
   the base color): dust and grime near the ground, a darker horsehead, a little paint fade
   per kind of part, and in "full" mode ambient occlusion from ray casts against the field.
   Colors only ever darken and stay near gray, so the console's status colors stay clean.

Identical copies (the same mesh, role and base height, like each pumpjack's parts) get the
same UVs and colors, worked out once on the first copy by name. optimize.py (#97) can then
still share their meshes, and the AO costs one cast per kind of part, not per copy.

Textures are shared CC0 detail maps (blender/assets/materials, made by make_materials.py):
each material's own color is the glTF baseColorFactor, and the map adds wear, chips, rust
spots and grain around a bright mean, so a status color lerped into the factor stays bright.
With textures=False (build_field.py --no-scenery) no image is attached: the field gets the
role colors and weathering only.

Modes: "fast" (the default, for uploads through the API) skips ambient occlusion; "full"
(the committed blender/field.glb) adds it. Run apply() before optimize.run().
"""
import hashlib
import math
import os
import re
import time

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

MODES = ("fast", "full")
HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
DETAIL_SRGB = 0.88          # sRGB mean of every detail map (make_materials.DETAIL_MEAN)
DETAIL = ((DETAIL_SRGB + 0.055) / 1.055) ** 2.4     # the same, linear: each factor is divided by it
ATTR = "Col"

# Texture sets: folder under blender/assets, file stem, meters per tile, is it a detail map.
SETS = {
    "paint_worn": ("materials/paint_worn", "paint_worn", 1.6, True),
    "paint_light": ("materials/paint_light", "paint_light", 2.4, True),
    "galvanized": ("materials/galvanized", "galvanized", 1.0, True),
    "concrete": ("materials/concrete", "concrete", 2.0, True),
    "plastic_black": ("materials/plastic_black", "plastic_black", 1.2, True),
    "wood_weathered": ("materials/wood_weathered", "wood_weathered", 0.9, True),
    "caliche": ("materials/caliche", "caliche", 2.25, True),
}

# Role materials: texture set (or None), sRGB color, metallic, roughness, normal strength,
# dust (how much ground dust and grime the weathering adds, 0 to 1).
ROLES = {
    # restyled kit materials: what a part gets when no rule below is more specific
    "M_STEEL": ("paint_worn", "#7c7f81", 0.0, 0.65, 0.6, 0.8),          # painted steel, mid gray
    "M_STEEL_DARK": ("paint_worn", "#3c3f42", 0.0, 0.65, 0.6, 0.8),     # charcoal paint: skids, valve bodies
    "M_PIPE": ("paint_worn", "#2f3133", 0.0, 0.5, 0.4, 0.7),            # black epoxy-coated steel pipe
    "M_TANK": ("paint_light", "#ab9d82", 0.0, 0.65, 0.5, 1.0),          # desert tan coating, kept grayish
    "M_CONCRETE": ("concrete", "#a6a196", 0.0, 0.9, 0.8, 0.5),
    "M_GRAVEL": ("caliche", "#b9aa90", 0.0, 0.95, 0.8, 0.0),            # caliche pads, paler than the soil
    "M_BERM": ("caliche", "#a3937a", 0.0, 1.0, 0.8, 0.0),
    "M_WOOD": ("wood_weathered", "#7a6c5c", 0.0, 0.85, 0.8, 0.5),       # weathered poles and posts
    "M_RUBBER": ("plastic_black", "#202122", 0.0, 0.85, 0.6, 0.6),
    "M_GLASS": (None, "#1b2327", 0.0, 0.08, 0.0, 0.2),
    # roles
    "M_PJ_PAINT": ("paint_worn", "#4e5a65", 0.0, 0.68, 0.6, 1.0),       # pumpjack: slate blue-gray
    "M_PJ_DARK": ("paint_worn", "#30353b", 0.0, 0.7, 0.6, 1.0),         # its cranks, counterweights, base
    "M_PUMP_PAINT": ("paint_worn", "#4f5f70", 0.0, 0.55, 0.6, 1.0),     # pump casing: muted slate blue
    "M_MOTOR": ("paint_worn", "#6c7073", 0.0, 0.6, 0.6, 0.8),           # motors and their panels: gray
    "M_GALV": ("galvanized", "#a2a5a2", 0.35, 0.5, 0.5, 0.8),           # rails, ladders, gratings, posts
    "M_HDPE": ("plastic_black", "#232425", 0.0, 0.6, 0.5, 1.0),         # black HDPE flowlines
    "M_LINER": ("plastic_black", "#1f2021", 0.0, 0.75, 0.7, 1.0),       # containment liner
    "M_CONTAINMENT": ("paint_light", "#8e8574", 0.0, 0.65, 0.5, 1.0),   # steel containment ring wall
    "M_BUILDING": ("paint_light", "#b1aa9c", 0.0, 0.6, 0.5, 0.8),       # metal buildings and enclosures
    "M_TRUCK_CAB": ("paint_light", "#bcbcb8", 0.0, 0.35, 0.4, 0.8),     # off-white cab
    "M_TRUCK_TANK": ("paint_worn", "#2e3032", 0.0, 0.4, 0.5, 0.9),      # black vacuum barrel
    "M_STENCIL": (None, "#1c1d1e", 0.0, 0.7, 0.0, 0.3),                 # painted labels and tags
}
KEEP = {"M_LAMP", "M_BRINE", "M_GROUND", "M_ROAD", "M_WIRE", "M_YUCCA", "M_CACTUS", "M_PLANT_DRY", "M_FRUIT"}

# (asset kind, kit material) -> role. "+detail" keys apply to the asset's detail meshes.
BY_KIND = {
    ("well", "M_STEEL"): "M_PJ_PAINT", ("well", "M_STEEL_DARK"): "M_PJ_DARK",
    ("well+detail", "M_STEEL"): "M_GALV", ("well+detail", "M_STEEL_DARK"): "M_STEEL_DARK",
    ("pump", "M_STEEL"): "M_PUMP_PAINT", ("pump+detail", "M_STEEL"): "M_MOTOR",
    ("tank", "M_STEEL"): "M_GALV",
    ("valve", "M_STEEL"): "M_GALV",
    ("truck", "M_STEEL"): "M_TRUCK_CAB", ("truck", "M_TANK"): "M_TRUCK_TANK", ("truck+detail", "M_STEEL"): "M_GALV",
    ("disposal_well", "M_STEEL"): "M_BUILDING", ("disposal_well+detail", "M_STEEL"): "M_GALV",
    ("yard", "M_STEEL"): "M_BUILDING", ("yard+detail", "M_STEEL"): "M_BUILDING",
    ("rtu", "M_STEEL"): "M_BUILDING",
    ("loadout", "M_STEEL"): "M_GALV",
}
# (object name pattern, kit material) -> role, checked before BY_KIND.
BY_NAME = [
    (r"_(label(_cap|_front)?|tag|sign)$", "M_STEEL_DARK", "M_STENCIL"),
    (r"^PAD_\d+_steel$", "M_STEEL", "M_CONTAINMENT"),
    (r"^PAD_\d+_detail_steel_dark$", "M_STEEL_DARK", "M_CONTAINMENT"),
    (r"^PAD_\d+_detail_steel$", "M_STEEL", "M_GALV"),
    (r"_detail_rubber$", "M_RUBBER", "M_LINER"),
    (r"_(antenna|hub|wheel|gate)$", "M_STEEL", "M_GALV"),
    (r"_crank$", "M_STEEL_DARK", "M_PJ_DARK"),
    (r"_rotor$", "M_STEEL_DARK", "M_MOTOR"),
    (r"_tower_steel$", "M_STEEL", "M_GALV"),
    (r"^SITE_(supports|power)_steel$", "M_STEEL", "M_GALV"),
    (r"^SCENERY_cattle_guard_steel$", "M_STEEL", "M_GALV"),
]
BY_NAME = [(re.compile(p), m, r) for p, m, r in BY_NAME]

DUST = np.array((0.72, 0.64, 0.54), dtype=np.float32)   # what ground dust multiplies a color by, at most
DUST_HEIGHT = 1.6           # meters above grade where dust fades out
GRIME_HEIGHT = 0.25         # the dark band right at grade
HORSEHEAD = 0.5             # how dark the pumpjack's horsehead reads against its beam
AO_RAYS, AO_DIST, AO_STRENGTH, AO_FLOOR = 10, 1.2, 0.45, 0.62
AO_MAX_SIZE = 9.0           # meters: bigger meshes have too few vertices for vertex AO (pads, liners, trunk)


def srgb(hexstr):
    c = [int(hexstr[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return tuple(x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c)


def seed(name):
    return int(hashlib.md5(name.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


# ---------- materials ----------

def image(set_name, kind):
    folder, stem, _, _ = SETS[set_name]
    path = os.path.join(ASSETS, folder, f"{stem}_{kind}.jpg")
    img = bpy.data.images.load(path, check_existing=True)
    if kind == "normal_gl":
        img.colorspace_settings.name = "Non-Color"
    return img


def sock(sockets, ident):
    """A Mix node socket by identifier: its names repeat for the float, vector and color variants."""
    return next(s for s in sockets if s.identifier == ident)


def multiply(nt):
    node = nt.nodes.new("ShaderNodeMix")
    node.data_type, node.blend_type = "RGBA", "MULTIPLY"
    sock(node.inputs, "Factor_Float").default_value = 1.0
    return node, sock(node.inputs, "A_Color"), sock(node.inputs, "B_Color"), sock(node.outputs, "Result_Color")


def build_material(name, textures):
    """A Principled BSDF the glTF exporter maps one to one: base color = map x factor x COLOR_0."""
    tex, color, metallic, rough, nstrength, _ = ROLES[name]
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    lin = srgb(color)
    factor = lin
    if tex and textures and SETS[tex][3]:
        factor = tuple(min(1.0, c / DETAIL) for c in lin)
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = rough
    vcol = nt.nodes.new("ShaderNodeVertexColor")
    vcol.layer_name = ATTR
    _, tint_a, tint_b, tint_out = multiply(nt)
    tint_b.default_value = (*factor, 1.0)
    if tex and textures:
        img = nt.nodes.new("ShaderNodeTexImage")
        img.image = image(tex, "color")
        nt.links.new(img.outputs["Color"], tint_a)
        if nstrength > 0:
            nimg = nt.nodes.new("ShaderNodeTexImage")
            nimg.image = image(tex, "normal_gl")
            nmap = nt.nodes.new("ShaderNodeNormalMap")
            nmap.inputs["Strength"].default_value = nstrength
            nt.links.new(nimg.outputs["Color"], nmap.inputs["Color"])
            nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    else:
        tint_a.default_value = (1.0, 1.0, 1.0, 1.0)
    _, w_a, w_b, w_out = multiply(nt)
    nt.links.new(tint_out, w_a)
    nt.links.new(vcol.outputs["Color"], w_b)
    nt.links.new(w_out, bsdf.inputs["Base Color"])
    mat.diffuse_color = (*lin, 1.0)
    mat.metallic, mat.roughness = metallic, rough
    return mat


def has_image(mat):
    return mat is not None and mat.node_tree is not None and any(
        n.type == "TEX_IMAGE" and n.image for n in mat.node_tree.nodes)


# ---------- roles ----------

def asset_of(obj):
    p = obj
    while p is not None:
        if "kind" in p.keys():
            return p
        p = p.parent
    return None


def role_for(obj, mat_name, kind, hdpe_prefixes):
    name = obj.name
    for pat, kit, role in BY_NAME:
        if kit == mat_name and pat.search(name):
            return role
    if mat_name in ROLES and mat_name not in ("M_STEEL", "M_STEEL_DARK", "M_PIPE", "M_TANK"):
        return mat_name                  # concrete, gravel, wood, the kit role names: already a role
    if mat_name == "M_PIPE" and name.startswith(hdpe_prefixes):
        return "M_HDPE"
    if kind:
        detail = "_detail_" in name
        hit = (detail and BY_KIND.get((kind + "+detail", mat_name))) or BY_KIND.get((kind, mat_name))
        if hit:
            return hit
    return mat_name if mat_name in ROLES else None


# ---------- UVs ----------

def box_uvs(obj, tile, offset):
    """Box projection in the mesh's own frame, in meters (scaled by the object's world scale)."""
    me = obj.data
    nv, nl, npoly = len(me.vertices), len(me.loops), len(me.polygons)
    if not nl:
        return
    co = np.empty(nv * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3) * np.array(obj.matrix_world.to_scale(), dtype=np.float32)
    normals = np.empty(npoly * 3, dtype=np.float32)
    me.polygons.foreach_get("normal", normals)
    totals = np.empty(npoly, dtype=np.int32)
    me.polygons.foreach_get("loop_total", totals)
    lv = np.empty(nl, dtype=np.int32)
    me.loops.foreach_get("vertex_index", lv)
    axis = np.repeat(np.abs(normals.reshape(-1, 3)).argmax(axis=1), totals)
    p = co[lv]
    u = np.where(axis == 0, p[:, 1], p[:, 0])
    v = np.where(axis == 2, p[:, 1], p[:, 2])
    uv = np.stack([u / tile + offset[0], v / tile + offset[1]], axis=1).astype(np.float32)
    while me.uv_layers:                  # text labels arrive with their own UVs
        me.uv_layers.remove(me.uv_layers[0])
    layer = me.uv_layers.new(name="UVMap")
    layer.data.foreach_set("uv", uv.ravel())
    me.uv_layers.active = layer
    layer.active_render = True


# ---------- weathering ----------

def world_points(obj):
    me = obj.data
    co = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    m = np.array(obj.matrix_world, dtype=np.float32)
    return co, co @ m[:3, :3].T + m[:3, 3]


def pieces(me):
    """Connected pieces of a mesh: (piece label per vertex, number of pieces)."""
    parent = list(range(len(me.vertices)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    ev = np.empty(len(me.edges) * 2, dtype=np.int32)
    me.edges.foreach_get("vertices", ev)
    for a, b in ev.reshape(-1, 2).tolist():
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    _, label = np.unique([find(i) for i in range(len(parent))], return_inverse=True)
    return label, int(label.max()) + 1 if len(label) else 0


def weather_colors(obj, dust, ground_z, fade):
    """Per-vertex multiplier: dust and grime by height above grade, horsehead, paint fade."""
    co, wp = world_points(obj)
    h = wp[:, 2] - ground_z
    d = np.clip(1.0 - h / DUST_HEIGHT, 0.0, 1.0) ** 1.6 * dust
    col = 1.0 - d[:, None] * (1.0 - DUST)
    grime = np.clip(1.0 - h / GRIME_HEIGHT, 0.0, 1.0) * 0.25 * dust
    col *= (1.0 - grime)[:, None]
    if obj.name.endswith("_beam") and asset_of(obj) is not None and asset_of(obj).get("kind") == "well":
        # the horsehead and its bridle are the separate pieces at the beam's -Y end
        # (field_parts.build_pumpjack); the beam itself is one piece that reaches past them
        label, n = pieces(obj.data)
        ends = np.bincount(label, weights=co[:, 1], minlength=n) / np.maximum(np.bincount(label, minlength=n), 1)
        col[ends[label] < -2.6] *= HORSEHEAD
    col *= 0.93 + 0.07 * fade
    return col


def geometry_key(obj, role):
    """Objects with the same mesh, role and base height get the same UVs and colors, so
    optimize.py (#97) can still share their mesh: 24 pumpjacks store each part once."""
    me = obj.data
    h = hashlib.sha1(role.encode())
    for coll, prop, width, dtype in ((me.vertices, "co", 3, np.float32), (me.loops, "vertex_index", 1, np.int32),
                                     (me.polygons, "loop_total", 1, np.int32)):
        arr = np.empty(len(coll) * width, dtype=dtype)
        coll.foreach_get(prop, arr)
        h.update(arr.tobytes())
    h.update(repr(tuple(round(s, 4) for s in obj.matrix_world.to_scale())).encode())
    h.update(str(round(obj.matrix_world.translation.z, 2)).encode())
    return h.hexdigest()


def ambient_occlusion(targets, bvh):
    """Fraction of a short hemisphere left open above each vertex, from ray casts."""
    golden = math.pi * (3 - math.sqrt(5))
    dirs = []
    for i in range(AO_RAYS):           # a fixed cosine-weighted spiral, so builds repeat exactly
        r = math.sqrt((i + 0.5) / AO_RAYS)
        a = i * golden
        dirs.append((r * math.cos(a), r * math.sin(a), math.sqrt(max(0.0, 1 - r * r))))
    out = {}
    for key, obj, wp in targets:
        me = obj.data
        nrm = np.empty(len(me.vertices) * 3, dtype=np.float32)
        me.vertices.foreach_get("normal", nrm)
        m3 = obj.matrix_world.to_3x3()
        ao = np.ones(len(wp), dtype=np.float32)
        nrm = nrm.reshape(-1, 3)
        for i, (p, n) in enumerate(zip(wp, nrm)):
            n = (m3 @ Vector(n)).normalized()
            t = n.orthogonal().normalized()
            b = n.cross(t)
            origin = Vector(p) + n * 0.02
            hits = 0
            for dx, dy, dz in dirs:
                if bvh.ray_cast(origin, t * dx + b * dy + n * dz, AO_DIST)[0] is not None:
                    hits += 1
            ao[i] = max(AO_FLOOR, 1.0 - AO_STRENGTH * hits / AO_RAYS)
        out[key] = ao
    return out


def field_bvh(objs):
    verts, polys, base = [], [], 0
    for obj in objs:
        _, wp = world_points(obj)
        me = obj.data
        verts.append(wp)
        polys += [tuple(base + i for i in p.vertices) for p in me.polygons]
        base += len(wp)
    if not verts:
        return None
    return BVHTree.FromPolygons(np.concatenate(verts).tolist(), polys, all_triangles=False)


def set_colors(obj, col):
    me = obj.data
    attr = me.color_attributes.get(ATTR) or me.color_attributes.new(ATTR, "FLOAT_COLOR", "POINT")
    rgba = np.ones((len(col), 4), dtype=np.float32)
    rgba[:, :3] = np.clip(col, 0.0, 1.0)
    attr.data.foreach_set("color", rgba.ravel())
    me.color_attributes.active_color = attr
    me.color_attributes.render_color_index = me.color_attributes.find(ATTR)


def fill_gaps(obj):
    """A mesh on someone else's textured material keeps its UVs and color layers. If it has
    none, it gets box UVs and a white layer for each color attribute the material reads, so
    the texture tiles and the layer multiplies by 1 instead of reading as black."""
    me, mat = obj.data, obj.data.materials[0]
    added = 0
    if not me.uv_layers:
        box_uvs(obj, 2.0, (0.0, 0.0))
        added = 1
    for node in mat.node_tree.nodes:
        name = getattr(node, "layer_name", "") if node.type == "VERTEX_COLOR" else (
            node.attribute_name if node.type == "ATTRIBUTE" and node.attribute_type == "GEOMETRY" else "")
        if name and name not in me.attributes:
            attr = me.color_attributes.new(name, "FLOAT_COLOR", "POINT")
            attr.data.foreach_set("color", np.ones(len(me.vertices) * 4, dtype=np.float32))
            added = 1
    return added


# ---------- the pass ----------

def apply(scene, mode="fast", textures=True):
    """Restyle every mesh in scene. Returns a summary for build_report.json."""
    if mode not in MODES:
        raise ValueError(f"look mode must be one of {', '.join(MODES)}")
    t0 = time.time()
    meshes = [o for o in scene.objects if o.type == "MESH" and o.data is not None]
    hdpe = tuple(f"PIPE_{o.name}_" for o in scene.objects if o.get("kind") == "well")
    made, counts, styled, foreign = {}, {}, [], []
    for obj in meshes:
        me = obj.data
        if me.users > 1 or len(me.materials) != 1:
            continue                     # shared and multi-material kit models keep their own look
        mat = me.materials[0]
        if mat is not None and has_image(mat) and mat.name not in made:
            foreign.append(obj)          # textured by someone else (scenery.py's ground, road, pads)
            continue
        if mat is None or mat.name in KEEP:
            continue                     # the lamps, the tank level, wire and plants
        base = mat.name.split(".")[0]
        asset = asset_of(obj)
        role = role_for(obj, base, asset.get("kind") if asset else None, hdpe)
        if role is None:
            continue
        if role not in made:
            theirs = bpy.data.materials.get(role)
            if theirs is not None and has_image(theirs):
                me.materials[0] = theirs  # a role someone else already textured: theirs wins
                foreign.append(obj)
                continue
            made[role] = build_material(role, textures)
        me.materials[0] = made[role]
        counts[role] = counts.get(role, 0) + 1
        styled.append((obj, role))
    filled = sum(fill_gaps(obj) for obj in foreign)
    t_roles = time.time()

    # one representative per geometry key: it gets the UVs and colors, its copies get the same
    groups = {}
    for obj, role in sorted(styled, key=lambda s: s[0].name):
        groups.setdefault(geometry_key(obj, role), []).append((obj, role))
    for key, members in groups.items():
        tex = ROLES[members[0][1]][0]
        for obj, _ in members:
            if tex and textures:
                box_uvs(obj, SETS[tex][2], (seed(key), seed(key + "v")))
            else:
                while obj.data.uv_layers:    # no texture reads them: keep the glb small
                    obj.data.uv_layers.remove(obj.data.uv_layers[0])
    t_uv = time.time()

    ground_z = -0.2                     # field_build.ground_height's mean
    colors = {key: weather_colors(m[0][0], ROLES[m[0][1]][5], ground_z, seed(key + "f")) for key, m in groups.items()}
    ao_s = 0.0
    if mode == "full":
        t = time.time()
        solid = [o for o in meshes if o.data.materials and o.data.materials[0] is not None
                 and o.data.materials[0].name not in ("M_LAMP", "M_BRINE", "M_WIRE")
                 and not o.name.startswith("SCENERY_")]
        bvh = field_bvh(solid)
        targets = []
        for key, members in groups.items():
            obj = members[0][0]
            wp = world_points(obj)[1]
            if not obj.name.startswith("SCENERY_") and len(wp) and np.ptp(wp, axis=0).max() <= AO_MAX_SIZE:
                targets.append((key, obj, wp))
        for key, ao in ambient_occlusion(targets, bvh).items():
            colors[key] = colors[key] * ao[:, None]
        ao_s = time.time() - t
    for key, members in groups.items():
        for obj, _ in members:
            set_colors(obj, colors[key])
    t_end = time.time()
    return {"mode": mode, "textures": bool(textures), "materials": dict(sorted(counts.items())),
            "styled": len(styled), "looks": len(groups), "theirs": len(foreign), "gaps_filled": filled,
            "seconds": {"roles": round(t_roles - t0, 2), "uvs": round(t_uv - t_roles, 2),
                        "weather": round(t_end - t_uv - ao_s, 2), "ao": round(ao_s, 2),
                        "total": round(t_end - t0, 2)}}

