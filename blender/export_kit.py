"""Export the model kit: one glb per asset type, so a field can be built without Blender.

Run once from the repo root, headless (and again after a part builder changes):
  /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
      --python blender/export_kit.py

Each asset type is built at the origin by the same part builders build_field.py uses
(field_parts.py, detail_equipment.py, detail_site.py, field_build.build_pad, and, when they
are on main, equipment_parts.py from #47 and iot_parts.py from #46), then exported to
blender/kit/<type>.glb. A type whose geometry changes with the detail level (bolts, posts,
wellhead trim) also gets <type>_lite.glb and <type>_bare.glb. blender/assemble_glb.py reads
the kit in plain Python and places a copy of each type per blueprint ID.

Every kit glb has one root node at the origin named with the type's token (KIT, PAD_KIT,
LOADOUT_KIT, SENSOR_KIT or RTU_PAD_KIT) and extras {kind, part: "root"}. Its children keep
the names the full generator gives them with the token in place of the ID, so KIT_LEVEL
becomes TANK_3_LEVEL and KIT_axle_1 becomes TRUCK_1_axle_1. Each child has extras
{part: <suffix>}. Small painted tags and signs are left out, and the tank number is left to
the assembler, since text depends on the ID. glyphs.glb holds one mesh per character for
that, and palette.glb one small mesh per material.

manifest.json lists every type: kind, token, file per detail level, parts and their roles,
bounds, triangles, and the anchor points the assembler joins pipes to (in the type's own
frame, Blender axes: x east, y north, z up). It also carries build_field.py's detail policy
and a hash of each builder source, so a stale kit is easy to spot.
"""
import hashlib
import json
import math
import os
import sys
import time

import bmesh
import bpy
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import build_field as bf  # noqa: E402  (reloads the part builders; its main() does not run)
import detail_equipment as de  # noqa: E402
import detail_site as ds  # noqa: E402
import field_build  # noqa: E402
import field_parts as fp  # noqa: E402
import layout as lay  # noqa: E402

try:
    import equipment_parts as eq  # noqa: E402  (#47, once it is on main)
except ImportError:
    eq = None
try:
    import iot_parts as iot  # noqa: E402  (#46, once it is on main)
except ImportError:
    iot = None

KIT_DIR = os.path.join(HERE, "kit")
TOKEN = "KIT"
GLYPHS = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ" "abcdefghijklmnopqrstuvwxyz" "0123456789" "-.+/#&()%:,'")
SOURCES = ("field_parts.py", "detail_equipment.py", "detail_site.py", "field_build.py", "piping.py",
           "layout.py", "build_field.py", "equipment_parts.py", "iot_parts.py", "well_parts.py", "swd_parts.py",
           "truck_parts.py", "terrain.py")
MOVING = {"LEVEL", "gauge", "rotor", "wheel", "beam", "crank", "gate", "axle_1", "axle_2", "axle_3"}
LIGHTS = {"light", "beacon", "headlights", "lamps"}
SENSORS = {"lt", "lshh"}


def role(part):
    if part in MOVING:
        return "moving"
    if part in LIGHTS:
        return "light"
    if part in SENSORS:
        return "sensor"
    return "static"


def reset():
    bf.clear_scene()
    for blocks in (bpy.data.meshes, bpy.data.curves):
        for block in list(blocks):
            blocks.remove(block)


def site():
    return fp.Site(bpy.context.scene.collection)


def remove(name):
    obj = bpy.data.objects.get(name)
    if obj is not None:
        bpy.data.objects.remove(obj, do_unlink=True)


def v3(v):
    return [round(float(c), 5) for c in v]


# ---------- the types ----------

def build_tank(s):
    """The tank with its inlet and load-out nozzles. The outlet and crossover nozzles depend on
    the tank's pumps and crossovers, so the assembler adds those to each tank."""
    root, pts = fp.build_tank(s, TOKEN, (0, 0, 0), 0.0, outlets=(), xo_angles=())
    lo = de.tank_extras(s, TOKEN, root, TOKEN, capacity=None, outlets=(), xo_angles=())
    remove(f"{TOKEN}_label")      # the painted number depends on the ID: the assembler writes it
    anchors = {"outlets": {str(a): v3(fp.polar(fp.NOZZLE_R, a, fp.OUTLET[1])) for a in lay.OUTLET_ANGLES},
               "inlet": v3(pts["inlet"]), "loadout": v3(lo)}
    # nozzle stub and flanges as build_tank and tank_extras make them, for the assembler's nozzles
    consts = {"R": fp.TANK_R, "nozzle_r": fp.NOZZLE_R, "outlet_z": fp.OUTLET[1], "xo_z": 0.85,
              "nozzle_pipe_r": 0.11, "flange_r": 0.19, "flange_thick": 0.05,
              "flange_at": [fp.TANK_R + 0.15, fp.NOZZLE_R - 0.03], "label_angle": de.LABEL_ANGLE, "level": 0.55}
    return root, {"anchors": anchors, "tank": consts}


def build_pump(s):
    root, pts = fp.build_pump(s, TOKEN, (0, 0, 0), 0.0)
    de.pump_extras(s, TOKEN, root)
    return root, {"anchors": {"suction": v3(pts["suction"]), "discharge": v3(pts["discharge"])}}


def build_valve(manual):
    """fp.build_valve with a handwheel (manual) or an electric actuator (remote): the blueprint's
    manual/remote picks one per valve ID (fp.valve_modes), so the kit has both."""
    def make(s):
        fp.VALVE_MANUAL.clear()
        fp.VALVE_MANUAL[TOKEN] = manual
        try:
            root, pts = fp.build_valve(s, TOKEN, (0, 0, 0), 0.0)
            de.valve_extras(s, TOKEN, root, TOKEN)
        finally:
            fp.VALVE_MANUAL.clear()
        return root, {"anchors": {"north": v3(pts["north"]), "south": v3(pts["south"])},
                      "actuation": "manual" if manual else "remote"}
    return make


def build_well(s):
    root, pts = fp.build_pumpjack(s, TOKEN, (0, 0, 0), 0.0)
    de.well_extras(s, TOKEN, root)
    return root, {"anchors": {"outlet": v3(pts["outlet"])}}


def build_swd(fx):
    """The disposal well. Its light tower is the tower type, placed at the scene root the way
    build_field.py does it, so this records where swd_extras put it and takes it off."""
    def make(s):
        root, pts = fp.build_swd(s, TOKEN, (0, 0, 0), fx=fx, rot_z=0.0)
        ds.swd_extras(s, TOKEN, root, fx)
        tower = bpy.data.objects[f"{TOKEN}_tower"]
        spot = {"loc": v3(tower.location), "aim_deg": round(math.degrees(tower.rotation_euler.z), 4)}
        for obj in [tower, *tower.children_recursive]:
            bpy.data.objects.remove(obj, do_unlink=True)
        return root, {"anchors": {"inlet": v3(pts["inlet"]), "tower": spot}, "fx": fx}
    return make


def build_truck(s):
    root = fp.build_truck(s, TOKEN, (0, 0, 0), 0.0)
    ds.truck_extras(s, TOKEN, root)
    return root, {}


def build_yard(s):
    root = fp.build_yard(s, TOKEN, (0, 0, 0), 0.0)
    ds.yard_extras(s, root, [])           # curb stops depend on the truck count: the assembler adds them
    return root, {"curb": {"size": [1.8, 0.25, 0.15], "offset": [6.8, 0.0, 0.2], "rot_deg": 90.0}}


def build_loadout(s):
    return de.build_loadout(s, "LOADOUT_KIT", (0, 0, 0), 0.0), {}


def build_pad(s):
    """field_build.build_pad on an empty pad at the origin, keeping only PAD_KIT and its children.
    The berm is left out: its gaps follow each pad's flowlines and neighbors (#98, #122), so the
    assembler builds PAD_n_berm per pad (kit_routes.berm)."""
    pad = {"frame": lay.Frame(0.0, 0.0), "tank": "TANK_PADKIT", "n": "KIT", "capacity": None}
    supports = fp.Kit(s, "SITE_supports", None)
    field_build.build_pad(s, supports, pad, [], [], [], [])
    supports.done()
    remove("PAD_KIT_berm")
    root = bpy.data.objects["PAD_KIT"]
    keep = {root, *root.children_recursive}
    for obj in [o for o in bpy.data.objects if o not in keep]:
        bpy.data.objects.remove(obj, do_unlink=True)
    return root, {}


def build_tower(s):
    ds.light_tower(s, TOKEN, (0, 0, 0), 0.0)
    return bpy.data.objects[f"{TOKEN}_tower"], {}


def build_equipment(model):
    def make(s):
        item = {"id": TOKEN, "pos": (0.0, 0.0), "rot": 0.0, "type": model or "OTHER", "model": model,
                "label": None}
        root = eq.build(s, item)
        remove(f"{TOKEN}_label")          # a placeholder's painted type and ID: the assembler writes it
        return root, {}
    return make


def sensor_kinds():
    """Every asset kind iot_parts.build() can put a sensor on: MOUNTS, plus equipment on a post."""
    return list(iot.MOUNTS) + ["equipment"]


def sensor_type(kind):
    """The kit type for a sensor on this kind of asset: sensor_<device>_<mount>, because the
    hardware follows the asset (#100: a radar on a tank roof, a puck on a truck)."""
    mount = iot.MOUNTS[kind][3] if kind in iot.MOUNTS else "post"
    return f"sensor_{iot.DEVICE.get(kind, 'pressure')}_{mount}"


def build_sensor(kind):
    """iot_parts.sensor as it builds on this kind of asset, at the origin."""
    def make(s):
        mount = iot.MOUNTS[kind][3] if kind in iot.MOUNTS else "post"
        iot.sensor(s, TOKEN, None, (0, 0, 0), mount, [], [], kind)
        root = bpy.data.objects[f"SENSOR_{TOKEN}"]
        return root, {"device": root["device"], "mount": mount}
    return make


def build_rtu(s):
    """iot_parts.rtu, moved to the origin. The manifest keeps where it sits on a pad."""
    iot.rtu(s, TOKEN, None, lay.PAD_TOP)
    root = bpy.data.objects[f"RTU_PAD_{TOKEN}"]
    spot = v3(root.location)
    root.location = (0.0, 0.0, 0.0)
    return root, {"spot": spot}


TYPES = {   # type: (kind, token, how the assembler names the root, builder)
    "tank": ("tank", TOKEN, "{id}", build_tank),
    "pump": ("pump", TOKEN, "{id}", build_pump),
    "valve_discharge": ("valve", TOKEN, "{id}", build_valve(True)),
    "valve_crossover": ("valve", TOKEN, "{id}", build_valve(True)),
    "valve_discharge_remote": ("valve", TOKEN, "{id}", build_valve(False)),
    "valve_crossover_remote": ("valve", TOKEN, "{id}", build_valve(False)),
    "well": ("well", TOKEN, "{id}", build_well),
    "disposal_well": ("disposal_well", TOKEN, "{id}", build_swd(1)),
    "disposal_well_mirror": ("disposal_well", TOKEN, "{id}", build_swd(-1)),
    "truck": ("truck", TOKEN, "{id}", build_truck),
    "yard": ("yard", TOKEN, "{id}", build_yard),
    "loadout": ("loadout", "LOADOUT_KIT", "LOADOUT_{n}", build_loadout),
    "pad": ("pad", "PAD_KIT", "PAD_{n}", build_pad),
    "tower": ("tower", TOKEN, "{name}", build_tower),
}
if eq is not None:
    for _model in list(getattr(eq, "MODELS", {})) + [None]:
        TYPES[f"equipment_{(_model or 'placeholder').lower()}"] = ("equipment", TOKEN, "{id}", build_equipment(_model))
if iot is not None:
    for _kind in sensor_kinds():
        TYPES.setdefault(sensor_type(_kind), ("sensor", f"SENSOR_{TOKEN}", "SENSOR_{id}", build_sensor(_kind)))
    TYPES["rtu"] = ("rtu", f"RTU_PAD_{TOKEN}", "RTU_PAD_{n}", build_rtu)


def iot_info():
    """What kit_extras.iot() needs to pick and place a sensor: iot_parts.MOUNTS, and each asset
    kind's device and kit type."""
    if iot is None:
        return None
    return {"mounts": {k: [float(v[0]), float(v[1]), float(v[2]), v[3]] for k, v in iot.MOUNTS.items()},
            "devices": {k: iot.DEVICE.get(k, "pressure") for k in sensor_kinds()},
            "types": {k: sensor_type(k) for k in sensor_kinds()}}


# ---------- export ----------

def suffix(obj, token):
    if obj.name != token and not obj.name.startswith(token + "_"):
        raise RuntimeError(f"kit node {obj.name} does not start with {token}")
    return obj.name[len(token):]


def tag(root, kind, token):
    """Extras kind and part on the kit nodes. Returns the parts list for the manifest."""
    root["kind"] = kind
    root["part"] = "root"
    parts = []
    for obj in root.children_recursive:
        name = suffix(obj, token)
        obj["part"] = name[1:]
        parts.append({"name": name, "role": role(name[1:]), "type": obj.type.lower(),
                      "parent": "" if obj.parent is root else suffix(obj.parent, token)})
    return parts


def bounds(objs):
    pts = [o.matrix_world @ Vector(c) for o in objs if o.type == "MESH" for c in o.bound_box]
    return [round(f(p[i] for p in pts), 3) for i in range(3) for f in (min, max)] if pts else None


def export_type(name, level):
    kind, token, naming, make = TYPES[name]
    reset()
    fp.LOD.update(bf.LEVELS[level], small_labels=False)
    root, info = make(site())
    parts = tag(root, kind, token)
    keep = {root, *root.children_recursive}
    dotted = sorted(o.name for o in bpy.data.objects if "." in o.name)
    stray = [o.name for o in bpy.data.objects if o not in keep]
    if dotted or stray:
        raise RuntimeError(f"{name}: clashing names {dotted[:5]} or nodes outside the root {stray[:5]}")
    info = dict(info, root=suffix(root, token))
    bpy.context.view_layer.update()       # world matrices are lazy: refresh them before measuring
    return info, parts, bf.triangles(bpy.context.scene), bounds(list(bpy.data.objects))


def write_glb(path):
    bf.export_glb(path)
    nodes = bf.glb_nodes(path)
    if any("." in n.get("name", "") for n in nodes):
        raise RuntimeError(f"{path}: the exporter renamed a node")


def export_types(manifest):
    for name, (kind, token, naming, _) in TYPES.items():
        entry = {"kind": kind, "token": token, "name": naming, "levels": {}, "triangles": {}}
        seen = {}
        for level in bf.LEVELS:
            info, parts, tris, box = export_type(name, level)
            if tris in seen:          # the level dropped nothing on this type: share the file
                entry["levels"][level], entry["triangles"][level] = seen[tris], tris
                continue
            fname = f"{name}.glb" if level == "full" else f"{name}_{level}.glb"
            write_glb(os.path.join(KIT_DIR, fname))
            seen[tris] = fname
            entry["levels"][level], entry["triangles"][level] = fname, tris
            if level == "full":
                entry.update(parts=parts, bounds=box, **info)
        manifest["types"][name] = entry
        print(f"kit: {name:22} {entry['triangles']}")


def text_width(text):
    """Ink right edge of left-aligned text at size 1, to measure advances."""
    cu = bpy.data.curves.new("measure", "FONT")
    cu.body, cu.size, cu.extrude, cu.align_x, cu.align_y, cu.resolution_u = text, 1.0, 0.0, "LEFT", "CENTER", 6
    obj = bpy.data.objects.new("measure", cu)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.update()
    me = bpy.data.meshes.new_from_object(obj.evaluated_get(bpy.context.evaluated_depsgraph_get()))
    bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.curves.remove(cu)
    xs = [v.co.x for v in me.vertices]
    bpy.data.meshes.remove(me)
    return max(xs) if xs else 0.0


def export_glyphs(manifest):
    """One mesh per character at size 1, centered on its advance like detail_equipment.label()."""
    reset()
    fp.LOD.update(bf.LEVELS["full"])
    s = site()
    advance = {" ": round(text_width("H H") - text_width("HH"), 5)}
    for ch in GLYPHS:
        advance[ch] = round(text_width(ch + ch) - text_width(ch), 5)
        de.label(s, f"GLYPH_{ord(ch)}", ch, 1.0, (0, 0, 0), (0, 0, 0), None, "M_STEEL_DARK", small=False)
    write_glb(os.path.join(KIT_DIR, "glyphs.glb"))
    manifest["glyphs"] = {"file": "glyphs.glb", "node": "GLYPH_<codepoint>", "advance": advance, "line": 1.0,
                          "align": "each glyph is centered on its advance, vertically centered on the line",
                          "material": "M_STEEL_DARK", "triangles": bf.triangles(bpy.context.scene)}


def export_palette(manifest):
    """A small mesh per material, so the assembler has every material the generator uses."""
    reset()
    s = site()
    for i, mat in enumerate(fp.PALETTE):
        bm = bmesh.new()
        fp.box(bm, (0.1, 0.1, 0.1), (i * 0.2, 0, 0))
        s.mesh(f"PALETTE_{mat}", bm, mat)
    write_glb(os.path.join(KIT_DIR, "palette.glb"))
    manifest["palette"] = {"file": "palette.glb", "materials": list(fp.PALETTE)}


def sources():
    out = {}
    for name in SOURCES:
        path = os.path.join(HERE, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                out[name] = hashlib.sha256(f.read()).hexdigest()[:16]
    return out


def main():
    t0 = time.time()
    os.makedirs(KIT_DIR, exist_ok=True)
    for old in os.listdir(KIT_DIR):
        if old.endswith(".glb"):
            os.remove(os.path.join(KIT_DIR, old))
    manifest = {
        "format": 1,
        "blender": bpy.app.version_string,
        "sources": sources(),
        "token": TOKEN,
        "axes": "anchors and bounds are in the type's own frame with Blender axes: x east, y north, z up",
        "detail": {"levels": bf.LEVELS, "dropped": bf.DROPPED, "budget_triangles": bf.BUDGET_TRIS,
                   "budget_mb": bf.BUDGET_MB, "budget_draw_calls": bf.BUDGET_DRAWS,
                   "full_detail_max_tanks": bf.FULL_DETAIL_MAX_TANKS},
        "layout": {"pad_top": lay.PAD_TOP, "tank_y": lay.TANK_Y, "yard_slots": [list(s) for s in lay.YARD_SLOTS]},
        "types": {},
    }
    if iot_info():
        manifest["iot"] = iot_info()
    export_types(manifest)
    export_glyphs(manifest)
    export_palette(manifest)
    files = sorted(f for f in os.listdir(KIT_DIR) if f.endswith(".glb"))
    size = sum(os.path.getsize(os.path.join(KIT_DIR, f)) for f in files)
    manifest["files"] = {f: os.path.getsize(os.path.join(KIT_DIR, f)) for f in files}
    manifest["kit_mb"] = round(size / 1e6, 2)
    manifest["seconds"] = round(time.time() - t0, 1)
    with open(os.path.join(KIT_DIR, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
        f.write("\n")
    print(f"kit: {len(manifest['types'])} types, {len(files)} files, {size / 1e6:.2f} MB "
          f"in {manifest['seconds']} s -> {os.path.relpath(KIT_DIR, os.path.dirname(HERE))}")
    if size > 15e6:
        raise RuntimeError(f"the kit is {size / 1e6:.1f} MB, over its 15 MB budget")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        sys.exit(2)
