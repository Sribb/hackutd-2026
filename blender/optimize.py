"""The post-pass right before export (#97): share identical meshes, set up GPU instancing for
repeated scenery, and drop unused data.

build_field.py calls run() once the scene is built and has passed its name checks, then
exports with export_gpu_instances on. Blender writes EXT_mesh_gpu_instancing, and three.js
0.170's GLTFLoader turns each instanced node into one InstancedMesh: one draw call for 110
grass clumps instead of 110.

1. merge(): outside SCENERY, an asset's <ID>_detail_<m> Kit part joins its sibling <ID>_<m>
   for the LABEL_SAFE kit materials (steel, steel_dark, concrete, gravel, rubber), like
   WELL_1_detail_steel into WELL_1_steel: one draw call instead of two. The console's
   web/src/parts.js already gives both the same label ("pumpjack frame"), so no label changes.
   Never pipe or tank parts (their detail_ meshes have their own labels), contract names, or
   anything with custom properties, so the functional parts the Jarvis console explodes
   (docs/JARVIS_CONSOLE.md, userData.part) always stay separate.
2. share(): objects whose meshes hold the same geometry, attributes, and material point at
   one mesh, so the glb stores it once. 24 pumpjacks carry one copy of each part, not 24.
   Each object keeps its own node, name, parent, and transform.
3. group(): under SCENERY only, leaf nodes that share a mesh and carry no contract name move
   under one Empty per mesh, named SCENERY_<model>_instances. The exporter turns that Empty
   into the instanced node and drops the copies' own nodes. Nothing a console reads by name
   is ever under one of these Empties.
4. unshare(): the exporter instances any set of leaf siblings that share a mesh. Outside the
   groups made here, no two leaf siblings may share one, so they get their own copy back.
5. purge(): meshes, materials, and images nothing uses any more are removed.
6. shadows(): small scenery gets extras cast_shadow false, for the console to skip in its
   shadow pass (see shadows()).

export_options() holds the Draco + WebP export, off by default (COMPRESS).

Only single-material meshes are shared. three.js names the child meshes of a multi-material
mesh after the glTF mesh, and the console resolves a click on a child by that name, so a
shared one would point at the wrong asset.
"""
import hashlib
import re
import time

import bmesh
import bpy
import numpy as np

MIN_COPIES = 2           # leaf siblings sharing a mesh before they become one instanced node
MERGE_PARTS = True       # see merge()
# Kit materials whose <ID>_<m> and <ID>_detail_<m> parts web/src/parts.js already names alike
# ("steel" and "detail_steel" are both the pumpjack frame), so joining them changes no label.
# Not pipe ("pipe" is a well's flowline, "detail_pipe" is piping) or tank (the settling tank).
LABEL_SAFE = ("steel", "steel_dark", "concrete", "gravel", "rubber")
SHADOW_MIN_M = 1.2       # scenery shorter than this gets cast_shadow false (see shadows())
COMPRESS = False         # Draco + WebP export; off until the console has a DRACOLoader (see export_options())
GROUP_SUFFIX = "_instances"
SCENERY_ROOT = "SCENERY"
EDITOR_ONLY = (".select_", ".uv_select_", ".hide_")   # attributes the exporter never writes

# attribute data type: (foreach_get property, values per element, numpy dtype)
ATTR = {
    "FLOAT": ("value", 1, np.float32), "INT": ("value", 1, np.int32), "BOOLEAN": ("value", 1, np.bool_),
    "INT8": ("value", 1, np.int8), "FLOAT_VECTOR": ("vector", 3, np.float32), "FLOAT2": ("vector", 2, np.float32),
    "FLOAT_COLOR": ("color", 4, np.float32), "BYTE_COLOR": ("color", 4, np.float32),
    "INT32_2D": ("value", 2, np.int32), "INT16_2D": ("value", 2, np.int16), "QUATERNION": ("value", 4, np.float32),
}


def _grab(coll, prop, width, dtype):
    arr = np.empty(len(coll) * width, dtype=dtype)
    coll.foreach_get(prop, arr)
    return arr.tobytes()


def mesh_key(obj):
    """A hash of everything the exporter writes for this object's mesh, or None to never share it."""
    me = obj.data
    if obj.type != "MESH" or obj.modifiers or me.shape_keys or len(obj.material_slots) > 1 or not me.polygons:
        return None
    h = hashlib.sha1()
    try:
        h.update(np.array([len(me.vertices), len(me.edges), len(me.loops), len(me.polygons)], np.int64).tobytes())
        h.update(_grab(me.vertices, "co", 3, np.float32))
        h.update(_grab(me.edges, "vertices", 2, np.int32))
        h.update(_grab(me.loops, "vertex_index", 1, np.int32))
        h.update(_grab(me.polygons, "loop_start", 1, np.int32))
        for attr in sorted(me.attributes, key=lambda a: a.name):
            if attr.name.startswith(EDITOR_ONLY):
                continue
            spec = ATTR.get(attr.data_type)
            if spec is None:
                return None
            h.update(f"{attr.name}|{attr.domain}|{attr.data_type}".encode())
            h.update(_grab(attr.data, *spec))
        for uv in me.uv_layers:
            h.update(uv.name.encode() + _grab(uv.data, "uv", 2, np.float32))
    except (TypeError, RuntimeError, AttributeError):
        return None
    mats = [s.material.name if s.material else "" for s in obj.material_slots]
    props = sorted((k, repr(me[k])) for k in me.keys())
    h.update(repr((mats, props)).encode())
    return h.hexdigest()


def under(obj, root):
    p = obj.parent
    while p is not None:
        if p == root:
            return True
        p = p.parent
    return False


def plain(obj, keep):
    """A static mesh with one material and nothing attached that the console could read."""
    return (obj.type == "MESH" and obj.name not in keep and len(obj.material_slots) == 1 and not obj.modifiers
            and not obj.data.shape_keys and not obj.keys() and not obj.data.keys()
            and not obj.data.has_custom_normals and obj.material_slots[0].material is not None)


def layers(me):
    """The attribute and UV names a mesh carries: parts only join when these match, so no part
    gets a zero-filled color or UV layer from its neighbor."""
    return tuple(sorted(a.name for a in me.attributes if not a.name.startswith(EDITOR_ONLY)))


def merge(objs, root, keep):
    """Join <owner>_detail_<m> into its sibling <owner>_<m> for the LABEL_SAFE kit materials, when
    both are plain leaves with the same material and layers. Returns how many objects went."""
    parents = {o.parent for o in objs if o.parent is not None}
    by_name = {o.name: o for o in objs}
    sets = {}
    for obj in objs:
        m = re.match(r"^(.+)_detail_([a-z_]+)$", obj.name)
        head = by_name.get(f"{m.group(1)}_{m.group(2)}") if m and m.group(2) in LABEL_SAFE else None
        if (head is None or obj.parent is None or head.parent != obj.parent or obj in parents or head in parents
                or not plain(obj, keep) or not plain(head, keep)
                or root is not None and (obj.parent == root or under(obj, root))
                or head.material_slots[0].material != obj.material_slots[0].material
                or layers(head.data) != layers(obj.data)):
            continue
        sets.setdefault(head.name, [head]).append(obj)
    gone = []
    for members in sets.values():
        head = members[0]
        to_head = (head.matrix_parent_inverse @ head.matrix_basis).inverted()
        bm = bmesh.new()
        bm.from_mesh(head.data)
        for obj in members[1:]:
            tmp = obj.data.copy()
            tmp.transform(to_head @ obj.matrix_parent_inverse @ obj.matrix_basis)
            bm.from_mesh(tmp)
            bpy.data.meshes.remove(tmp)
            gone.append(obj)
        old = head.data
        name, old.name = old.name, old.name + "_premerge"     # purge() drops it; the name stays dot-free
        me = bpy.data.meshes.new(name)
        bm.to_mesh(me)
        bm.free()
        me.materials.append(head.material_slots[0].material)
        head.data = me
    if gone:
        bpy.data.batch_remove(gone)
    return len(gone)


def share(objs):
    """Point objects with identical meshes at one mesh. Returns (meshes before, meshes after)."""
    groups, before = {}, set()
    for obj in sorted(objs, key=lambda o: o.name):
        if obj.type != "MESH":
            continue
        before.add(obj.data)
        key = mesh_key(obj)
        if key is not None:
            groups.setdefault(key, []).append(obj)
    for members in groups.values():
        keep = members[0].data
        for obj in members[1:]:
            obj.data = keep
    after = {o.data for o in objs if o.type == "MESH"}
    return len(before), len(after)


def group(collection, objs, root, keep):
    """Move SCENERY leaves that share a mesh under one Empty per mesh. Returns {Empty name: copies}."""
    sets, parents = {}, {o.parent for o in objs if o.parent is not None}   # Object.children scans every object
    for obj in sorted(objs, key=lambda o: o.name):
        if (obj.type == "MESH" and obj not in parents and obj.name not in keep and obj.parent is not None
                and under(obj, root) and len(obj.material_slots) <= 1):
            sets.setdefault((obj.parent.name, obj.data.name), []).append(obj)
    made, taken = {}, {o.name for o in bpy.data.objects}
    for (parent_name, mesh_name), members in sorted(sets.items()):
        if len(members) < MIN_COPIES:
            continue
        model = members[0].get("scenery") or re.sub(r"^KIT_", "", mesh_name)
        base = "SCENERY_" + re.sub(r"[^A-Za-z0-9_]", "_", str(model)) + GROUP_SUFFIX
        name, n = base, 2
        while name in taken:
            name, n = f"{base}_{chr(ord('a') + n - 2)}", n + 1
        taken.add(name)
        holder = bpy.data.objects.new(name, None)
        holder.empty_display_size = 0.5
        collection.objects.link(holder)
        holder.parent = bpy.data.objects[parent_name]     # identity local transform: copies keep their world spot
        holder["instances"] = len(members)
        for obj in members:
            obj.parent = holder
        made[name] = len(members)
    return made


def unshare(objs, groups):
    """Give leaf siblings outside the instance groups their own mesh again. Returns how many got one."""
    by_parent, parents = {}, {o.parent for o in objs if o.parent is not None}
    for obj in objs:
        if obj.type == "MESH" and obj not in parents and obj.parent is not None and obj.parent.name not in groups:
            by_parent.setdefault((obj.parent.name, obj.data.name), []).append(obj)
    n = 0
    for members in by_parent.values():
        for obj in sorted(members, key=lambda o: o.name)[1:]:
            obj.data = obj.data.copy()
            n += 1
    return n


def purge():
    """Remove meshes, materials, and images nothing uses, in one batch (removing IDs one at a
    time rescans the whole file each time). Returns how many blocks went."""
    gone = [b for blocks in (bpy.data.meshes, bpy.data.materials, bpy.data.images) for b in blocks if b.users == 0]
    if gone:
        bpy.data.batch_remove(gone)
    return len(gone)


def shadows(root):
    """Mark small scenery cast_shadow false (glTF extras, so userData.cast_shadow in three.js).
    The console casts a shadow from every mesh, which draws it a second time; grass, rocks,
    branches, cactus, yucca, wire and road stubs add draw calls and triangles there for shadows
    nobody sees at night. A kit model taller than SHADOW_MIN_M (mesquite, creosote, the sign)
    keeps its shadow. Returns the names marked."""
    if root is None:
        return []
    under_root = root.children_recursive          # computed in Python over every object: once
    kids, marked = {}, []
    for o in under_root:
        kids.setdefault(o.parent, []).append(o)
    for o in under_root:
        holder = o.name.endswith(GROUP_SUFFIX)
        if o.parent is not None and o.parent.name.endswith(GROUP_SUFFIX) or not (holder or o.type == "MESH"):
            continue                           # a copy (its holder carries the flag) or an Empty
        tall = max((c.dimensions.z for c in kids.get(o, []) if c.type == "MESH"), default=0.0) if holder \
            else o.dimensions.z
        generated = not holder and not o.get("scenery")    # fence, cattle guards, road stubs, yucca, cactus
        if generated or tall < SHADOW_MIN_M:
            o["cast_shadow"] = False
            marked.append(o.name)
    return sorted(marked)


def export_options(compress=COMPRESS):
    """Extra glTF exporter options. compress turns on Draco (level 6) and WebP textures: the
    default field goes from about 16 MB to about 5 MB (#117), but the console can only open it
    with a DRACOLoader (docs/CONTRACT.md "What's in field.glb"), and WebP encoding adds seconds
    to every export. So builds stay uncompressed unless build_field.py gets --compress."""
    if not compress:
        return {}
    return {"export_draco_mesh_compression_enable": True, "export_draco_mesh_compression_level": 6,
            "export_image_format": "WEBP", "export_image_quality": 80}


def run(scene, keep):
    """The whole pass on scene. keep is the set of node names that must stay real nodes."""
    t0 = time.time()
    root = bpy.data.objects.get(SCENERY_ROOT)
    merged = merge(list(scene.objects), root, keep) if MERGE_PARTS else 0
    objs = list(scene.objects)
    before, after = share(objs)
    groups = group(scene.collection, objs, root, keep) if root is not None else {}
    unshared = unshare(list(scene.objects), groups)
    purged = purge()
    bpy.context.view_layer.update()
    no_shadow = shadows(root)
    return {"merged": merged, "meshes_before": before, "meshes_after": after + unshared, "unshared": unshared,
            "instanced": dict(sorted(groups.items())), "instances": sum(groups.values()),
            "no_shadow": len(no_shadow), "purged": purged, "seconds": round(time.time() - t0, 2)}
