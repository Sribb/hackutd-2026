"""The post-pass right before export (#97): share identical meshes, set up GPU instancing for
repeated scenery, and drop unused data.

build_field.py calls run() once the scene is built and has passed its name checks, then
exports with export_gpu_instances on. Blender writes EXT_mesh_gpu_instancing, and three.js
0.170's GLTFLoader turns each instanced node into one InstancedMesh: one draw call for 110
grass clumps instead of 110.

1. merge(): outside SCENERY, static Kit parts with the same parent and material join into one
   mesh, like WELL_1_steel and WELL_1_detail_steel. Each one was its own draw call, and the
   console's frame time follows draw calls (#97). Only parts named the way Kit.done() names
   them, <owner>_<material>, with no contract name and no custom properties, ever join; the
   one with the shortest name keeps its name.
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
GROUP_SUFFIX = "_instances"
SCENERY_ROOT = "SCENERY"
EDITOR_ONLY = (".select_", ".uv_select_", ".hide_")   # attributes the exporter never writes

# attribute data type: (foreach_get property, values per element, numpy dtype)
ATTR = {
    "FLOAT": ("value", 1, np.float32), "INT": ("value", 1, np.int32), "BOOLEAN": ("value", 1, np.bool_),
    "INT8": ("value", 1, np.int8), "FLOAT_VECTOR": ("vector", 3, np.float32), "FLOAT2": ("vector", 2, np.float32),
    "FLOAT_COLOR": ("color", 4, np.float32), "BYTE_COLOR": ("color", 4, np.float32),
    "INT32_2D": ("value", 2, np.int32), "QUATERNION": ("value", 4, np.float32),
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


def kit_part(obj, keep):
    """A static part as Kit.done() makes it: <owner>_<material>, one material, nothing attached."""
    if (obj.type != "MESH" or obj.name in keep or len(obj.material_slots) != 1 or obj.modifiers
            or obj.data.shape_keys or obj.keys() or obj.data.keys() or obj.data.has_custom_normals):
        return False
    mat = obj.material_slots[0].material
    return mat is not None and mat.name.startswith("M_") and obj.name.endswith("_" + mat.name[2:].lower())


def merge(objs, root, keep):
    """Join same-material Kit parts under one parent into one mesh. Returns how many objects went."""
    parents = {o.parent for o in objs if o.parent is not None}
    sets = {}
    for obj in objs:
        if (obj.parent is not None and obj not in parents and kit_part(obj, keep)
                and not (root is not None and (obj.parent == root or under(obj, root)))):
            sets.setdefault((obj.parent.name, obj.material_slots[0].material.name), []).append(obj)
    gone = []
    for members in sets.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda o: (len(o.name), o.name))
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


def run(scene, keep):
    """The whole pass on scene. keep is the set of node names that must stay real nodes."""
    t0 = time.time()
    root = bpy.data.objects.get(SCENERY_ROOT)
    merged = merge(list(scene.objects), root, keep)
    objs = list(scene.objects)
    before, after = share(objs)
    groups = group(scene.collection, objs, root, keep) if root is not None else {}
    unshared = unshare(list(scene.objects), groups)
    purged = purge()
    bpy.context.view_layer.update()
    return {"merged": merged, "meshes_before": before, "meshes_after": after + unshared, "unshared": unshared,
            "instanced": dict(sorted(groups.items())), "instances": sum(groups.values()),
            "purged": purged, "seconds": round(time.time() - t0, 2)}
