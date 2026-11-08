"""Build the CC0 scenery kit from already-downloaded source files.

Run headless (never against the live Blender session):
  /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
      --python blender/assets/tools/build_kit.py -- --src <download_dir> [--only ID ...]

<download_dir> holds the raw downloads laid out as the "src" paths in recipes.json
(ph/..., acg/..., kenney_*/...). It must live outside the repo. This script only reads
glTF/GLB, JPG/PNG, and HDR files from it. It never opens a downloaded .blend or runs
downloaded code.

Writes, under blender/assets/: scenery/<category>/<name>.glb, textures/<name>/*.jpg,
hdri/*.hdr, manifest.json, and CREDITS.md.
"""
import json
import math
import os
import random
import re
import shutil
import sys

import bmesh
import bpy
from mathutils import Matrix, Vector

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.dirname(HERE)
MAX_TRIS = 15000
MAX_GLB_BYTES = 3 * 1024 * 1024


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    src, only = None, []
    i = 0
    while i < len(argv):
        if argv[i] == "--src":
            src = argv[i + 1]
            i += 2
        elif argv[i] == "--only":
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                only.append(argv[i])
                i += 1
        else:
            raise SystemExit(f"unknown argument {argv[i]}")
    if not src or not os.path.isdir(src):
        raise SystemExit("pass --src <download_dir>")
    if os.path.realpath(src).startswith(os.path.realpath(os.path.dirname(os.path.dirname(ASSETS)))):
        raise SystemExit("the download dir must be outside the repo")
    return os.path.realpath(src), set(only)


def src_path(root, rel):
    p = os.path.realpath(os.path.join(root, rel))
    if not p.startswith(root + os.sep) or not os.path.isfile(p):
        raise SystemExit(f"missing or unsafe source file: {rel}")
    return p


def tri_count(me):
    return sum(len(p.vertices) - 2 for p in me.polygons)


def select_only(objs, active):
    vl = bpy.context.scene.view_layers[0]
    for o in bpy.context.scene.objects:
        o.select_set(o in objs, view_layer=vl)
    vl.objects.active = active


# ---------- mesh reduction ----------

def islands_by_material(bm):
    """Face islands connected through shared vertices, grouped by material index."""
    seen, groups = set(), {}
    for f in bm.faces:
        if f.index in seen:
            continue
        stack, isl = [f], []
        seen.add(f.index)
        while stack:
            g = stack.pop()
            isl.append(g)
            for v in g.verts:
                for h in v.link_faces:
                    if h.index not in seen:
                        seen.add(h.index)
                        stack.append(h)
        groups.setdefault(f.material_index, []).append(isl)
    return groups


def thin_cards(ob, keep, seed=7):
    """Delete a random share of small islands (leaf cards) per material and enlarge the rest.
    Returns the material indices that are not card-like, for normal decimation."""
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bm.faces.ensure_lookup_table()
    rng = random.Random(seed)
    groups = islands_by_material(bm)
    doomed, solid = [], []
    grow = min(1.5, 1.0 / math.sqrt(keep))
    for mi, isl in sorted(groups.items()):
        tris = sum(len(f.verts) - 2 for i in isl for f in i)
        if len(isl) < 50 or tris / len(isl) > 10:
            solid.append(mi)
            continue
        rng.shuffle(isl)
        n_keep = max(1, round(len(isl) * keep))
        for faces in isl[n_keep:]:
            doomed.extend(faces)
        for faces in isl[:n_keep]:
            verts = {v for f in faces for v in f.verts}
            c = sum((v.co for v in verts), Vector()) / len(verts)
            for v in verts:
                v.co = c + (v.co - c) * grow
    bmesh.ops.delete(bm, geom=list(set(doomed)), context="FACES")
    bm.to_mesh(ob.data)
    bm.free()
    return solid


def decimate(ob, ratio, mat_indices=None):
    """Collapse-decimate the whole mesh, or only faces with the given material indices."""
    if ratio >= 0.999:
        return
    target = ob
    if mat_indices is not None:
        all_mats = {p.material_index for p in ob.data.polygons}
        if not mat_indices:
            return
        if set(mat_indices) != all_mats:
            select_only([ob], ob)
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.context.scene.tool_settings.mesh_select_mode = (False, False, True)
            bm = bmesh.from_edit_mesh(ob.data)
            bm.select_mode = {"FACE"}
            for f in bm.faces:
                f.select_set(False)
            for f in bm.faces:
                if f.material_index in mat_indices:
                    f.select_set(True)
            bmesh.update_edit_mesh(ob.data)
            bpy.ops.mesh.separate(type="SELECTED")
            bpy.ops.object.mode_set(mode="OBJECT")
            target = [o for o in bpy.context.selected_objects if o is not ob][0]
    mod = target.modifiers.new("decimate", "DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.ratio = ratio
    mod.use_collapse_triangulate = True
    select_only([target], target)
    bpy.ops.object.modifier_apply(modifier=mod.name)
    if target is not ob:
        select_only([ob, target], ob)
        bpy.ops.object.join()


def reduce_to(ob, limit):
    start = tri_count(ob.data)
    if start <= limit:
        return
    original = ob.data.copy()
    ratio = limit / start
    for _ in range(8):
        solid = thin_cards(ob, ratio)
        thinned = tri_count(ob.data)
        decimate(ob, ratio, solid)
        got = tri_count(ob.data)
        print(f"  reduce {ob.name}: {start} -> {thinned} after card thinning -> {got} (ratio {ratio:.3f}, solid mats {solid})")
        if got <= limit:
            bpy.data.meshes.remove(original)
            return
        fresh = original.copy()
        old = ob.data
        ob.data = fresh
        bpy.data.meshes.remove(old)
        ratio *= 0.95 * limit / got
    raise SystemExit(f"could not reduce {ob.name} under {limit} triangles")


# ---------- materials and images ----------

def add_alpha_map(alpha_path, ob):
    """Poly Haven ships the leaf alpha as its own map. Merge it into the color map's
    alpha channel and make every material that reads alpha use a 0.5 MASK cutoff."""
    alpha = bpy.data.images.load(alpha_path)
    for mat in ob.data.materials:
        nt = mat.node_tree
        bsdf = next(n for n in nt.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
        base = bsdf.inputs["Base Color"].links
        if not base:
            continue
        tex = base[0].from_node
        color = tex.image
        merged = bpy.data.images.get(color.name + "_rgba")
        if merged is None:
            w, h = color.size
            if tuple(alpha.size) != (w, h):
                alpha.scale(w, h)
            px = list(color.pixels[:])
            ap = alpha.pixels[:]
            px[3::4] = ap[0::4]
            merged = bpy.data.images.new(color.name + "_rgba", w, h, alpha=True)
            merged.pixels = px
            merged.file_format = "PNG"
            merged.alpha_mode = "STRAIGHT"
            merged.pack()
        tex.image = merged
        al = bsdf.inputs["Alpha"]
        if al.links:
            for link in list(al.links):
                nt.links.remove(link)
            lt = nt.nodes.new("ShaderNodeMath")
            lt.operation = "LESS_THAN"
            lt.inputs[1].default_value = 0.5
            sub = nt.nodes.new("ShaderNodeMath")
            sub.operation = "SUBTRACT"
            sub.inputs[0].default_value = 1.0
            nt.links.new(tex.outputs["Alpha"], lt.inputs[0])
            nt.links.new(lt.outputs[0], sub.inputs[1])
            nt.links.new(sub.outputs[0], al)


def tint(ob, colors):
    for mat in ob.data.materials:
        if mat and mat.name.split(".")[0] in colors:
            bsdf = next(n for n in mat.node_tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
            bsdf.inputs["Base Color"].default_value = (*colors[mat.name.split(".")[0]], 1.0)


def used_images(ob):
    imgs = []
    for mat in ob.data.materials:
        if mat and mat.node_tree:
            for n in mat.node_tree.nodes:
                if n.bl_idname == "ShaderNodeTexImage" and n.image and n.image not in imgs:
                    imgs.append(n.image)
    return imgs


def rename_and_size(ob, rid, max_px):
    for mat in ob.data.materials:
        if mat:
            mat.name = f"{rid}_{mat.name.split('.')[0]}"
    for img in used_images(ob):
        w, h = img.size
        if max(w, h) > max_px:
            s = max_px / max(w, h)
            img.scale(max(1, int(w * s)), max(1, int(h * s)))
        img.name = f"{rid}_{img.name.split('.')[0]}"


# ---------- one model ----------

def build_model(r, src_root):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=src_path(src_root, r["src"]))
    keep = [o for o in bpy.data.objects if o.type == "MESH" and o.name in r["objects"]]
    if len(keep) != len(r["objects"]):
        raise SystemExit(f"{r['id']}: wanted {r['objects']}, found {[o.name for o in keep]}")
    for o in keep:
        mw = o.matrix_world.copy()
        o.parent = None
        o.matrix_world = mw
    for o in list(bpy.data.objects):
        if o not in keep:
            bpy.data.objects.remove(o, do_unlink=True)
    select_only(keep, keep[0])
    if len(keep) > 1:
        bpy.ops.object.join()
    ob = bpy.context.view_layer.objects.active
    bpy.ops.object.make_single_user(object=True, obdata=True)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    if r.get("alpha_map"):
        add_alpha_map(src_path(src_root, r["alpha_map"]), ob)
    if r.get("tint"):
        tint(ob, r["tint"])
    reduce_to(ob, min(r.get("max_tris", MAX_TRIS), MAX_TRIS))

    me = ob.data
    dims = Vector([max(v.co[i] for v in me.vertices) - min(v.co[i] for v in me.vertices) for i in range(3)])
    scale = r.get("scale", 1.0)
    if "fit" in r:
        axis, size = r["fit"]
        cur = max(dims) if axis == "max" else dims["xyz".index(axis)]
        scale = size / cur
    if abs(scale - 1.0) > 1e-6:
        me.transform(Matrix.Scale(scale, 4))

    # Rest on Z = 0, centered on the origin in X and Y.
    co = [v.co for v in me.vertices]
    lo = Vector([min(c[i] for c in co) for i in range(3)])
    hi = Vector([max(c[i] for c in co) for i in range(3)])
    me.transform(Matrix.Translation(Vector(((lo.x + hi.x) / -2, (lo.y + hi.y) / -2, -lo.z))))
    ob.location = (0, 0, 0)

    rid = r["id"]
    ob.name = rid
    me.name = rid
    rename_and_size(ob, rid, r.get("tex", 1024))
    ob["scenery"] = True
    ob["license"] = "CC0 1.0"
    ob["source_url"] = r["source_url"]

    out = os.path.join(ASSETS, r["out"])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    select_only([ob], ob)
    bpy.ops.export_scene.gltf(
        filepath=out, export_format="GLB", use_selection=True, export_apply=True,
        export_yup=True, export_extras=True, export_image_format="AUTO",
        export_jpeg_quality=85, export_image_quality=85, export_animations=False,
        export_cameras=False, export_lights=False,
        export_draco_mesh_compression_enable=False)
    tris = tri_count(me)
    size = os.path.getsize(out)
    d = hi - lo
    print(f"BUILT {rid}: {tris} tris, {size / 1e6:.2f} MB, {d.x:.2f} x {d.y:.2f} x {d.z:.2f} m (Blender X,Y,Z)")
    if tris > MAX_TRIS or size > MAX_GLB_BYTES:
        raise SystemExit(f"{rid} is over budget: {tris} tris, {size} bytes")
    return {"triangles": tris, "bytes": size,
            "size_m": [round(d.x, 3), round(d.y, 3), round(d.z, 3)]}


# ---------- textures and HDRIs ----------

def build_texture(t, src_root):
    files = []
    for role, rel in t["maps"].items():
        img = bpy.data.images.load(src_path(src_root, rel))
        w, h = img.size
        if max(w, h) > t["px"]:
            s = t["px"] / max(w, h)
            img.scale(int(w * s), int(h * s))
        out_rel = f"textures/{t['name']}/{t['name']}_{role}.jpg"
        out = os.path.join(ASSETS, out_rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        img.file_format = "JPEG"
        img.save(filepath=out, quality=92 if role.startswith("normal") else 85)
        bpy.data.images.remove(img)
        files.append(out_rel)
        print(f"BUILT {out_rel}: {os.path.getsize(out) / 1e6:.2f} MB")
    return files


# ---------- manifest and credits ----------

def entry(base, src, extra):
    e = {
        "id": base["id"], "category": base["category"],
        "source": src["name"], "source_url": base["source_url"],
        "license": src["license"], "license_url": src["license_url"],
        "author": base.get("author"), "notes": base.get("notes", ""),
    }
    e.update(extra)
    return e


def dump_compact(obj):
    """JSON with two-space indents, but short lists of plain values kept on one line."""
    text = json.dumps(obj, indent=2)
    return re.sub(r"\[\s+([^\[\]{}]*?)\s+\]",
                  lambda m: "[" + ", ".join(s.strip() for s in m.group(1).split(",\n")) + "]", text)


def write_credits(manifest):
    lines = [
        "# Scenery kit credits",
        "",
        "Every asset in `blender/assets/` is CC0 1.0 (public domain dedication), checked against",
        "each source's own license page or the License.txt inside its download.",
        "Credit is not required under CC0, but we credit every author anyway.",
        "`manifest.json` has the full record, and `python3 blender/assets/check_assets.py` checks it.",
        "",
    ]
    titles = {"plants": "Plants", "rocks": "Rocks", "fence": "Fence", "props": "Props",
              "textures": "Ground textures", "hdri": "Sky (HDRI)"}
    for cat, title in titles.items():
        rows = [a for a in manifest["assets"] if a["category"] == cat]
        if not rows:
            continue
        lines += [f"## {title}", "", "| Asset | File | Source | Author | License |", "|---|---|---|---|---|"]
        for a in rows:
            lines.append(f"| {a['id']} | `{a['file']}` | [{a['source']}]({a['source_url']}) | "
                         f"{a['author']} | [{a['license']}]({a['license_url']}) |")
        lines.append("")
    lines += ["## How the licenses were checked", ""]
    for name, s in manifest["sources"].items():
        lines.append(f"- **{name}:** {s['license_evidence']}")
    lines += ["", "## Changes we made", "",
              "Models were converted to GLB in Blender, joined into one mesh each, scaled to meters,",
              "set to rest on Z = 0, and renamed `SCENERY_*`. Some were decimated or had leaf cards thinned",
              "to stay under 15,000 triangles. Leaf alpha maps were merged into the color maps, and the",
              "Kenney grass was recolored to dry straw. Textures were re-encoded as JPEG at 1K (2K for the",
              "main ground). The notes in `manifest.json` list the changes for each asset.",
              "", "## Gaps", ""]
    for g in manifest["gaps"]:
        lines.append(f"- **{g['item']}.** {g['reason']}")
    lines.append("")
    with open(os.path.join(ASSETS, "CREDITS.md"), "w") as f:
        f.write("\n".join(lines))


def main():
    src_root, only = parse_args()
    recipes = json.load(open(os.path.join(HERE, "recipes.json")))
    sources = recipes["sources"]
    man_path = os.path.join(ASSETS, "manifest.json")
    old = {a["id"]: a for a in json.load(open(man_path))["assets"]} if os.path.exists(man_path) else {}
    assets = []
    for r in recipes["models"]:
        if only and r["id"] not in only:
            if r["id"] in old:
                assets.append(old[r["id"]])
            continue
        m = build_model(r, src_root)
        assets.append(entry(r, sources[r["source"]], {
            "file": r["out"], "triangles": m["triangles"], "bytes": m["bytes"],
            "size_m": m["size_m"], "max_texture_px": r.get("tex", 1024)}))
    for t in recipes["textures"]:
        if only and t["id"] not in only:
            if t["id"] in old:
                assets.append(old[t["id"]])
            continue
        bpy.ops.wm.read_factory_settings(use_empty=True)
        files = build_texture(t, src_root)
        assets.append(entry(t, sources[t["source"]], {
            "file": files[0], "files": files, "triangles": 0,
            "bytes": sum(os.path.getsize(os.path.join(ASSETS, f)) for f in files),
            "max_texture_px": t["px"], "main_ground": bool(t.get("main_ground"))}))
    for h in recipes["hdris"]:
        if only and h["id"] not in only:
            if h["id"] in old:
                assets.append(old[h["id"]])
            continue
        out = os.path.join(ASSETS, h["out"])
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copyfile(src_path(src_root, h["src"]), out)
        print(f"BUILT {h['out']}: {os.path.getsize(out) / 1e6:.2f} MB")
        assets.append(entry(h, sources[h["source"]], {
            "file": h["out"], "triangles": 0, "bytes": os.path.getsize(out)}))
    manifest = {
        "kit": recipes["kit"],
        "conventions": "Models are GLB in meters, one root node named by id, resting on Z = 0 and centered "
                       "in X and Y. size_m is [X, Y, Z] in Blender axes (Z up). The GLB itself is Y-up, "
                       "so three.js sees Blender Z as Y. Root nodes carry extras scenery, license, source_url.",
        "budget": {"total_mb": 60, "model_triangles": MAX_TRIS, "model_mb": 3,
                   "texture_px": 1024, "main_ground_px": 2048},
        "sources": {v["name"]: {k: v[k] for k in ("license", "license_url", "license_evidence")}
                    for v in sources.values()},
        "assets": assets,
        "gaps": recipes["gaps"],
    }
    with open(man_path, "w") as f:
        f.write(dump_compact(manifest) + "\n")
    write_credits(manifest)
    print(f"WROTE manifest.json with {len(assets)} assets")


main()
