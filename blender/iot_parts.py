"""IoT hardware in the field (#46): a wireless sensor node on each monitored asset, and an
RTU with a radio antenna on each tank pad.

SENSOR_<asset> is an Empty parented to its asset, so a click on it walks up to the asset.
It carries kind "sensor", for_asset, signals, and connectors in its extras, and its LED is
SENSOR_<asset>_light. RTU_PAD_n is parented to PAD_n, with RTU_PAD_n_light (status light)
and RTU_PAD_n_antenna (mast and whip). Lights use M_LAMP, like every other status light,
so the console lights them by setting the emissive color. Neither carries an asset ID:
the generator never invents one.

names.sensors(bp) decides which assets get a sensor.
"""
import math

import bmesh
import bpy
from mathutils import Matrix

import detail_equipment as de
import field_parts as fp
import layout as lay
import names
from field_parts import Kit, box, cyl, tube

RTU_SPOT = (-9.5, 50.5)          # pad-local: the free corner west of the tank, clear of crossovers
ROOF_R, ROOF_DEG = 2.4, 175.0     # tank roof mount, outside the cutaway


def roof_z(r):
    """Height of the tank's cone roof at radius r, in tank-local space (see detail_equipment's radar)."""
    return fp.TANK_H + 0.08 + 0.47 * (1 - r / (fp.TANK_R + 0.05))


# Where a sensor sits on each kind of asset: (local x, y, z, mount), in the asset's own frame.
# "surface" mounts sit on what is there; "post" mounts stand on a short post from the ground.
MOUNTS = {
    "tank": (*fp.polar(ROOF_R, ROOF_DEG)[:2], roof_z(ROOF_R), "surface"),
    "pump": (0.35, -0.42, 0.95, "bracket"),
    "valve": (-0.38, 0.0, 0.25, "bracket"),       # outboard of the handwheel's sweep (radius 0.29)
    "well": (-0.9, -3.0, 0.12, "post"),
    "disposal_well": (0.9, 6.7, 0.15, "post"),
    "truck": (3.1, 0.75, 3.15, "surface"),
    "yard": (-14.5, -6.0, 3.44, "surface"),
}


def sensor(site, aid, owner, loc, mount, signals, connectors):
    """A sensor node: an enclosure with a stub antenna and an LED, on a post or bracket if needed."""
    root = site.empty(f"SENSOR_{aid}", loc, 0.0, parent=owner, size=0.3)
    root["kind"] = "sensor"
    root["for_asset"] = aid
    root["signals"] = ",".join(signals)
    root["connectors"] = ",".join(connectors)
    k = Kit(site, f"SENSOR_{aid}", root)
    z0 = 0.0
    if mount == "post":
        tube(k["M_STEEL"], (0, 0.08, 0.0), (0, 0.08, 1.25), 0.03, 6)
        z0 = 1.0
    elif mount == "bracket":
        box(k["M_STEEL"], (0.06, 0.16, 0.04), (0, 0.1, z0 + 0.08))
    box(k["M_STEEL_DARK"], (0.2, 0.12, 0.26), (0, 0, z0 + 0.13))
    tube(k["M_STEEL_DARK"], (0.06, 0, z0 + 0.26), (0.06, 0, z0 + 0.62), 0.01, 4)
    k.done()
    return de.status_light(site, f"SENSOR_{aid}_light", root, (-0.05, 0, z0 + 0.31), r=0.05)


def rtu(site, n, pad_root, top):
    """RTU cabinet on a stand with a solar panel and battery box, a radio mast, and a status light."""
    name = f"RTU_PAD_{n}"
    root = site.empty(name, (*RTU_SPOT, top), 0.0, parent=pad_root, size=0.6)
    root["kind"] = "rtu"
    root["pad"] = f"PAD_{n}"
    k = Kit(site, name, root)
    st, dk = k["M_STEEL"], k["M_STEEL_DARK"]
    box(k["M_CONCRETE"], (1.4, 0.8, 0.2), (0, 0, 0.1))
    for x in (-0.4, 0.4):
        tube(st, (x, 0.12, 0.2), (x, 0.12, 2.6), 0.035, 6)
    box(st, (0.9, 0.36, 1.0), (0, 0, 1.3))                                  # cabinet
    box(dk, (0.92, 0.38, 0.05), (0, 0, 1.82))
    box(dk, (0.02, 0.01, 0.8), (0, -0.185, 1.3))                            # door seam
    box(dk, (0.6, 0.4, 0.4), (0, 0.05, 0.42))                               # battery box
    box(dk, (1.2, 0.8, 0.04), (0, 0.25, 2.65), Matrix.Rotation(-0.55, 4, "X"))   # solar panel
    tube(dk, (0.3, 0.12, 0.8), (0.3, 0.12, 0.2), 0.02, 4)                   # conduit
    k.done()
    de.status_light(site, f"{name}_light", root, (-0.3, -0.05, 1.93), r=0.07)
    de.label(site, f"{name}_tag", f"RTU {n.replace('_', ' ')}", 0.09, (0, -0.195, 1.6), (math.pi / 2, 0, 0), root)
    mast = bmesh.new()
    tube(mast, (0.75, 0.3, 0.2), (0.75, 0.3, 6.6), 0.045, 6)
    cyl(mast, 0.025, 1.3, (0.75, 0.3, 7.25), segs=6)                       # omni whip
    tube(mast, (0.75, 0.3, 5.9), (0.75, -0.9, 5.9), 0.02, 4)               # yagi boom toward the road
    for j in range(5):
        y = 0.1 - 0.24 * j
        tube(mast, (0.45, y, 5.9), (1.05, y, 5.9), 0.01, 4)
    tube(mast, (0.4, 0.12, 1.5), (0.75, 0.3, 1.5), 0.015, 4)              # coax to the cabinet
    return site.mesh(f"{name}_antenna", mast, "M_STEEL", root)


def build(site, bp, L):
    """Every sensor node and pad RTU. Run after the assets and pads exist."""
    kinds = names.asset_kinds(bp)
    sizes = {e["id"]: e["size"] for e in L.get("equipment", [])}
    for aid, signals, connectors in names.sensors(bp):
        owner = bpy.data.objects.get(aid)
        if owner is None:
            continue
        kind = kinds[aid]
        if kind == "equipment":
            w, d = sizes.get(aid, (3.4, 3.4))
            x, y, z, mount = -w / 2 + 0.3, -d / 2 + 0.3, 0.15, "post"
        else:
            x, y, z, mount = MOUNTS[kind]
        sensor(site, aid, owner, (x, y, z), mount, signals, connectors)
    for p in L["pads"]:
        rtu(site, p["n"], bpy.data.objects.get(f"PAD_{p['n']}"), lay.PAD_TOP)
