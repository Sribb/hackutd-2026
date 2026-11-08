"""IoT hardware in the field (#46): a sensor on each monitored asset, and an RTU with a solar
panel and a radio mast on each tank pad.

SENSOR_<asset> is an Empty parented to its asset, so a click on it walks up to the asset.
It carries kind "sensor", for_asset, signals, connectors and device in its extras, and its
LED is SENSOR_<asset>_light. What it looks like follows the asset (#100): a radar level
transmitter on a tank roof, a pressure transmitter on pumps, wellheads and equipment, a
position monitor on a valve, a telematics puck on a truck, a gateway on the yard office.
Each has a wireless adapter with a stub antenna.

RTU_PAD_n is parented to PAD_n, with RTU_PAD_n_light (status light) and RTU_PAD_n_antenna
(mast, omni whip and Yagi). Lights use M_LAMP, like every other status light, so the
console lights them by setting the emissive color. Neither carries an asset ID: the
generator never invents one.

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
from field_parts import Kit, box, cyl, fine, rbox, strut, tube


def sg(n):
    """Segments: full detail as written, fewer below it."""
    return n if fine() else max(6, n * 2 // 3)

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
DEVICE = {"tank": "radar_level", "valve": "position", "truck": "telematics", "yard": "gateway"}


def _wireless(k, x, z, h=0.32):
    """Wireless adapter: a short canister with a stub antenna, on the device's side."""
    cyl(k["M_STEEL_DARK"], 0.035, 0.12, (x, 0, z), segs=sg(8))
    cyl(k["M_STEEL_DARK"], 0.012, h, (x, 0, z + 0.06 + h / 2), segs=sg(6))
    cyl(k["M_STEEL_DARK"], 0.02, 0.04, (x, 0, z + 0.07 + h), segs=sg(6))


def _radar(k, z0):
    """Radar level transmitter on a roof nozzle: flange, neck, housing with a domed lid."""
    st, dk = k["M_STEEL"], k["M_STEEL_DARK"]
    cyl(dk, 0.13, 0.04, (0, 0, z0 + 0.02), segs=sg(12))
    cyl(st, 0.05, 0.12, (0, 0, z0 + 0.1), segs=sg(8), caps=False)
    cyl(st, 0.1, 0.16, (0, 0, z0 + 0.24), segs=sg(12))
    cyl(st, 0.1, 0.06, (0, 0, z0 + 0.35), segs=sg(12), r2=0.06)
    strut(dk, (0, -0.06, z0 + 0.24), (0, -0.16, z0 + 0.24), 0.03)        # cable gland
    _wireless(k, 0.15, z0 + 0.22)
    return (-0.02, 0.05, z0 + 0.42)


def _pressure(k, z0):
    """Pressure transmitter on a two-valve manifold: body, two round caps, display window."""
    st, dk = k["M_STEEL"], k["M_STEEL_DARK"]
    rbox(dk, (0.14, 0.12, 0.08), (0, 0, z0 + 0.04), bevel=0.01)           # manifold
    cyl(st, 0.035, 0.12, (0, 0, z0 + 0.14), segs=sg(8))                       # sensor module
    box(st, (0.1, 0.1, 0.06), (0, 0, z0 + 0.22))
    for dy in (-0.06, 0.06):
        cyl(st, 0.065, 0.06, (0, dy, z0 + 0.3), axis="Y", segs=sg(12))       # housing caps
    cyl(st, 0.055, 0.12, (0, 0, z0 + 0.3), axis="Y", segs=sg(12), caps=False)
    cyl(k["M_GLASS"], 0.045, 0.01, (0, -0.095, z0 + 0.3), axis="Y", segs=sg(10), caps=False)
    _wireless(k, 0.1, z0 + 0.26, h=0.26)
    return (-0.06, 0.0, z0 + 0.38)


def _position(k, z0):
    """Valve position monitor: switch box with a dome indicator."""
    rbox(k["M_STEEL_DARK"], (0.16, 0.12, 0.12), (0, 0, z0 + 0.1), bevel=0.01)
    cyl(k["M_STEEL"], 0.05, 0.05, (0, 0, z0 + 0.18), segs=sg(10), r2=0.03)
    _wireless(k, 0.1, z0 + 0.12, h=0.22)
    return (-0.05, 0.0, z0 + 0.22)


def _puck(k, z0, mast=False):
    """Telematics puck (truck) or gateway (yard): a low dome, with a whip on the gateway."""
    cyl(k["M_STEEL_DARK"], 0.11, 0.05, (0, 0, z0 + 0.025), segs=sg(12), r2=0.07)
    if mast:
        rbox(k["M_STEEL_DARK"], (0.24, 0.14, 0.2), (0, 0, z0 + 0.15), bevel=0.015)
        _wireless(k, 0.08, z0 + 0.3, h=0.45)
    else:
        _wireless(k, 0.0, z0 + 0.05, h=0.18)
    return (-0.06, 0.0, z0 + (0.28 if mast else 0.1))


def sensor(site, aid, owner, loc, mount, signals, connectors, kind="pump"):
    """A sensor that looks like the hardware for its asset, on a post or bracket if needed."""
    root = site.empty(f"SENSOR_{aid}", loc, 0.0, parent=owner, size=0.3)
    root["kind"] = "sensor"
    root["for_asset"] = aid
    root["signals"] = ",".join(signals)
    root["connectors"] = ",".join(connectors)
    device = DEVICE.get(kind, "pressure")
    root["device"] = device
    k = Kit(site, f"SENSOR_{aid}", root)
    z0 = 0.0
    if mount == "post":
        strut(k["M_STEEL"], (0, 0.08, 0.0), (0, 0.08, 1.05), 0.05)          # post and mounting plate
        box(k["M_STEEL"], (0.22, 0.03, 0.26), (0, 0.06, 0.95))
        box(k["M_STEEL"], (0.18, 0.12, 0.03), (0, 0.0, 1.0))
        z0 = 1.015
    elif mount == "bracket":
        box(k["M_STEEL"], (0.06, 0.16, 0.04), (0, 0.1, 0.02))
        z0 = 0.04
    build = {"radar_level": _radar, "pressure": _pressure, "position": _position}.get(device)
    led = build(k, z0) if build else _puck(k, z0, mast=device == "gateway")
    k.done()
    return de.status_light(site, f"SENSOR_{aid}_light", root, led, r=0.04)


def rtu(site, n, pad_root, top):
    """RTU: cabinet on a strut stand with a rain hood, battery box, solar panel on a tilt
    bracket, a radio mast with an omni whip and a Yagi toward the road, and a status light."""
    name = f"RTU_PAD_{n}"
    root = site.empty(name, (*RTU_SPOT, top), 0.0, parent=pad_root, size=0.6)
    root["kind"] = "rtu"
    root["pad"] = f"PAD_{n}"
    k = Kit(site, name, root)
    st, dk = k["M_STEEL"], k["M_STEEL_DARK"]
    box(k["M_CONCRETE"], (1.4, 0.8, 0.2), (0, 0, 0.1))
    for x in (-0.4, 0.4):
        strut(st, (x, 0.12, 0.2), (x, 0.12, 2.75), 0.06, 0.04)            # strut channel uprights
    for z in (0.95, 1.75):
        strut(st, (-0.46, 0.12, z), (0.46, 0.12, z), 0.05, 0.04)
    rbox(st, (0.9, 0.36, 1.0), (0, 0, 1.3), bevel=0.025)                  # cabinet
    box(dk, (0.98, 0.44, 0.04), (0, 0.0, 1.83))                           # rain hood
    box(dk, (0.02, 0.01, 0.8), (0.0, -0.185, 1.3))                        # door seam
    box(dk, (0.04, 0.04, 0.14), (0.3, -0.2, 1.3))                         # latch handle
    if fp.fine():
        for z in (0.95, 1.05, 1.15):
            box(dk, (0.2, 0.01, 0.02), (-0.28, -0.186, z))               # vent louvers
    rbox(dk, (0.6, 0.4, 0.4), (0, 0.05, 0.42), bevel=0.02)                # battery box
    tube(dk, (0.3, 0.12, 0.8), (0.3, 0.12, 0.2), 0.02, 4)                 # conduit
    # solar panel: aluminum frame, dark cells, tilted toward the road
    tilt = Matrix.Rotation(-0.6, 4, "X")
    box(st, (1.24, 0.84, 0.035), (0, 0.27, 2.78), tilt)
    box(k["M_GLASS"], (1.16, 0.76, 0.04), (0, 0.27, 2.785), tilt)
    if fp.fine():
        for x in (-0.29, 0.0, 0.29):
            box(st, (0.012, 0.77, 0.045), (x, 0.27, 2.785), tilt)        # cell rows
    strut(st, (0, 0.12, 2.5), (0, 0.42, 2.66), 0.04)                      # tilt brace
    k.done()
    de.status_light(site, f"{name}_light", root, (-0.3, -0.05, 1.93), r=0.07)
    de.label(site, f"{name}_tag", f"RTU {n.replace('_', ' ')}", 0.09, (0, -0.195, 1.6), (math.pi / 2, 0, 0), root)
    mast = bmesh.new()
    tube(mast, (0.75, 0.3, 0.2), (0.75, 0.3, 6.6), 0.045, 6)
    box(mast, (0.3, 0.3, 0.04), (0.75, 0.3, 0.22))                        # base plate
    cyl(mast, 0.025, 1.3, (0.75, 0.3, 7.25), segs=6)                      # omni whip
    cyl(mast, 0.04, 0.12, (0.75, 0.3, 6.62), segs=6)
    tube(mast, (0.75, 0.3, 5.9), (0.75, -0.9, 5.9), 0.02, 4)              # yagi boom toward the road
    for j in range(5):
        y = 0.1 - 0.24 * j
        tube(mast, (0.45 + 0.02 * j, y, 5.9), (1.05 - 0.02 * j, y, 5.9), 0.01, 4)
    tube(mast, (0.4, 0.12, 1.5), (0.75, 0.3, 1.5), 0.015, 4)              # coax to the cabinet
    tube(mast, (0.77, 0.32, 1.5), (0.77, 0.32, 5.85), 0.012, 4)
    return site.mesh(f"{name}_antenna", mast, "M_STEEL", root)


def build(site, bp, L):
    """Every sensor and pad RTU. Run after the assets and pads exist."""
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
        sensor(site, aid, owner, (x, y, z), mount, signals, connectors, kind)
    for p in L["pads"]:
        rtu(site, p["n"], bpy.data.objects.get(f"PAD_{p['n']}"), lay.PAD_TOP)
