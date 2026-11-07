"""Site detail: power line, light towers, steel containment, disposal well and yard equipment, trucks.

Moving or lightable parts made here: PAD_n_lamps, SWD_n_lamps, SWD_n_gate, TRUCK_n_beacon,
TRUCK_n_headlights, and the TRUCK_n_axle_1..3 empties that carry the wheels.
"""
import math

import bmesh
from mathutils import Matrix, Vector

import field_parts as fp
from detail_equipment import bolts, flange, label, status_light
from field_parts import PI, Kit, ball, box, cyl, line, ring, tube

POLE_X = [-198 + 44 * i for i in range(10)]
POLE_Y = -6.5
ARM_Z = 11.4


def wire(bm, a, b, sag, r=0.015, segs=8):
    a, b = Vector(a), Vector(b)
    pts = [a.lerp(b, i / segs) - Vector((0, 0, sag * 4 * (i / segs) * (1 - i / segs))) for i in range(segs + 1)]
    for p, q in zip(pts, pts[1:]):
        tube(bm, p, q, r, 4)


def power(site, drops):
    """A three-wire line on wood poles along the road's south shoulder, with service drops."""
    k = Kit(site, "SITE_power", None)
    wood, steel, dark, wire_m = k["M_WOOD"], k["M_STEEL"], k["M_STEEL_DARK"], k["M_WIRE"]
    phases = [(-1.0, ARM_Z + 0.27), (1.0, ARM_Z + 0.27), (0.0, 12.25)]
    for x in POLE_X:
        cyl(wood, 0.16, 12.0, (x, POLE_Y, 6.0), segs=10, r2=0.12)
        box(wood, (0.12, 2.4, 0.14), (x, POLE_Y, ARM_Z))
        for dy in (-0.6, 0.6):
            tube(steel, (x, POLE_Y + dy, ARM_Z - 0.05), (x, POLE_Y, ARM_Z - 0.6), 0.02, 4)
        for dy, z in phases:
            cyl(steel, 0.05, 0.2, (x, POLE_Y + dy, z - 0.1), segs=6)
    for a, b in zip(POLE_X, POLE_X[1:]):
        for dy, z in phases:
            wire(wire_m, (a, POLE_Y + dy, z), (b, POLE_Y + dy, z), 0.9)
    for x, y in drops:
        px = min(POLE_X, key=lambda p: abs(p - x))
        cyl(wood, 0.13, 9.0, (x, y, 4.5), segs=8, r2=0.1)
        box(wood, (0.1, 1.2, 0.12), (x, y, 8.6))
        cyl(steel, 0.32, 0.9, (x + 0.35, y, 7.5), segs=12)
        box(dark, (0.4, 0.3, 0.5), (x + 0.3, y, 1.4))
        tube(dark, (x + 0.15, y, 7.0), (x + 0.15, y, 0.1), 0.03, 6)
        for dy in (-0.55, 0.55):
            wire(wire_m, (px, POLE_Y + dy * 1.8, ARM_Z + 0.27), (x, y + dy, 8.75), 0.6)
    k.done()


def light_tower(site, name, loc, aim_deg):
    """Mast with four lamp heads aimed toward aim_deg. The heads are NAME_lamps."""
    root = site.empty(f"{name}_tower", loc, math.radians(aim_deg), size=1)
    k = Kit(site, f"{name}_tower", root)
    box(k["M_CONCRETE"], (1.2, 1.2, 0.3), (0, 0, 0.15))
    tube(k["M_STEEL"], (0, 0, 0.3), (0, 0, 8.2), 0.1, 8)
    box(k["M_STEEL"], (0.12, 2.0, 0.12), (0.1, 0, 8.2))
    for z in range(1, 8):
        tube(k["M_STEEL"], (-0.12, -0.18, z), (-0.12, 0.18, z), 0.015, 4)
    box(k["M_STEEL_DARK"], (0.5, 0.4, 0.6), (0, 0.4, 1.0))
    k.done()
    lamps = bmesh.new()
    tilt = Matrix.Rotation(math.radians(-30), 4, "Y")
    for dy in (-0.75, -0.25, 0.25, 0.75):
        box(lamps, (0.12, 0.42, 0.32), (0.28, dy, 8.35), tilt)
    site.mesh(f"{name}_lamps", lamps, "M_LAMP", root)


def pad_extras(site, n, pad, tank_y, top):
    """Steel containment posts, liner, step-over, driveway sign."""
    k = Kit(site, f"PAD_{n}_detail", pad)
    for i in range(9):
        off = -6.0 + 1.5 * i
        for x, y in ((off, tank_y - 6.55), (off, tank_y + 6.55), (-6.55, tank_y + off), (6.55, tank_y + off)):
            box(k["M_STEEL_DARK"], (0.1, 0.1, 0.95), (x, y, top + 0.47))
    box(k["M_RUBBER"], (11.8, 11.8, 0.012), (0, tank_y, top + 0.006))
    for i, z in enumerate((0.25, 0.5, 0.75)):
        for side in (-1, 1):
            box(k["M_STEEL"], (0.9, 0.28, 0.05), (-4.0, tank_y - 6.2 + side * (0.95 - 0.28 * i), top + z))
    box(k["M_STEEL"], (0.9, 0.7, 0.05), (-4.0, tank_y - 6.2, top + 0.97))
    for dx in (-0.45, 0.45):
        line(k["M_STEEL"], [(-4.0 + dx, tank_y - 7.2, top + 1.3), (-4.0 + dx, tank_y - 6.2, top + 2.0),
                            (-4.0 + dx, tank_y - 5.2, top + 1.3)], 0.02)
    tube(k["M_STEEL"], (-11.3, 27.5, 0.0), (-11.3, 27.5, 2.0), 0.05, 6)
    box(k["M_STEEL"], (1.9, 0.05, 0.55), (-11.3, 27.5, 1.75))
    k.done()
    label(site, f"PAD_{n}_sign", f"TANK BATTERY {n}", 0.13, (-11.3, 27.47, 1.75), (PI / 2, 0, 0), pad)


def swd_extras(site, sid, root, fx, num):
    """Settling tank, inlet meter run, building trim, wellhead trim, gate, sign, lamps."""
    k = Kit(site, f"{sid}_detail", root)
    tank, st, dk, pp = k["M_TANK"], k["M_STEEL"], k["M_STEEL_DARK"], k["M_PIPE"]
    gx, gy = -8.0 * fx, 5.0
    cyl(tank, 1.8, 4.5, (gx, gy, 2.4), segs=28)
    cyl(tank, 1.85, 0.4, (gx, gy, 4.85), segs=28, r2=0.35)
    for side in (-0.2, 0.2):
        tube(st, (gx + 1.95 * fx, gy + side, 0.2), (gx + 1.95 * fx, gy + side, 5.2), 0.02, 5)
    for z in range(1, 6):
        tube(st, (gx + 1.95 * fx, gy - 0.2, z), (gx + 1.95 * fx, gy + 0.2, z), 0.015, 4)
    line(pp, [(gx, gy - 1.85, 0.6), (gx, -1.0, 0.6), (-4.5 * fx, -1.0, 0.6), (-4.5 * fx, -2.15, 0.6)], 0.09)
    line(pp, [(-4.5 * fx, -4.65, 0.6), (-4.5 * fx, -5.5, 0.6), (-2.2 * fx, -5.5, 0.6)], 0.09)
    for x in (8.4, 9.1):
        flange(st, (x * fx, -2, 0.6), (1, 0, 0), 0.26)
        bolts(st, (x * fx, -2, 0.6), "X", 0.22)
    cyl(dk, 0.22, 0.4, (8.75 * fx, -2, 0.6), axis="X", segs=14)
    box(dk, (0.2, 0.2, 0.2), (8.75 * fx, -2, 0.9))
    for vx in (0.5, 5.5):
        box(dk, (0.6, 0.6, 0.4), (vx * fx, -3, 4.55))
    box(st, (1.1, 0.8, 0.8), (8.7 * fx, -5.0, 0.55))
    for z in (1.8, 2.3, 2.8):
        box(dk, (0.05, 1.4, 0.06), ((-2.03 if fx > 0 else 2.03), -3, z))
    box(dk, (0.3, 0.25, 0.2), (5 * fx, -6.15, 3.4))
    ring(st, 0.16, 0.015, (0.36 * fx, 6, 0.7), axis="X", segs=12, rsegs=4)
    tube(dk, (0, 6.3, 1.6), (0, 6.3, 1.9), 0.012, 4)
    cyl(st, 0.07, 0.03, (0, 6.32, 1.95), axis="Y", segs=10)
    k.done()
    gate = bmesh.new()
    for z in (0.2, 1.7):
        tube(gate, (0, 0, z), (8, 0, z), 0.04, 6)
    for x in (0, 2, 4, 6, 8):
        tube(gate, (x, 0, 0.2), (x, 0, 1.7), 0.03, 6)
    tube(gate, (0, 0, 0.2), (8, 0, 1.7), 0.025, 6)
    g = site.mesh(f"{sid}_gate", gate, "M_STEEL", root, loc=(-4.0, -10.5, 0.1))
    g.rotation_euler.z = math.radians(75)
    label(site, f"{sid}_sign", f"SWD {num}", 0.9, (3 * fx, -6.06, 3.2), (PI / 2, 0, 0), root)
    light_tower(site, sid, root.matrix_basis @ Vector((10 * fx, 9, 0.15)), 222 if fx > 0 else 318)


def yard_extras(site, root):
    """Trailer stairs and trim, fuel tank, storage box, dumpster, curb stops, sign."""
    k = Kit(site, "YARD_1_detail", root)
    st, dk = k["M_STEEL"], k["M_STEEL_DARK"]
    for dx in (-0.65, 0.65):
        line(st, [(-9 + dx, -3.1, 0.6), (-9 + dx, -3.1, 1.5), (-9 + dx, -4.1, 1.5)], 0.025)
    box(dk, (12, 3.6, 0.42), (-12, -6, 0.33))
    box(st, (0.8, 0.7, 0.6), (-18.45, -6, 1.8))
    cyl(k["M_TANK"], 0.6, 2.4, (8, -9.5, 1.4), axis="X", segs=18)
    for x in (7.2, 8.8):
        box(dk, (0.12, 1.0, 0.8), (x, -9.5, 0.5))
    box(dk, (0.4, 0.3, 0.6), (9.4, -8.6, 0.42))
    box(st, (6.0, 2.4, 2.6), (-15, 6, 1.42))
    for x in (-17.9, -12.1):
        box(dk, (0.06, 2.3, 2.4), (x, 6, 1.4))
    box(dk, (2.0, 1.6, 1.3), (18, -6, 0.77))
    for x in (-2.0, 12.5):
        box(k["M_CONCRETE"], (1.8, 0.25, 0.15), (x + 6.8, 4.0, 0.2), Matrix.Rotation(PI / 2, 4, "Z"))
    tube(st, (5.5, 11.5, 0.0), (5.5, 11.5, 2.0), 0.05, 6)
    box(st, (1.4, 0.05, 0.5), (5.5, 11.5, 1.75))
    k.done()
    label(site, "YARD_1_sign", "YARD 1", 0.22, (5.5, 11.47, 1.75), (PI / 2, 0, 0), root)


def truck_extras(site, tid, root):
    """Axles with wheels, fenders, mirrors, steps, fuel tanks, catwalk, ladder, rings, lights."""
    k = Kit(site, f"{tid}_detail", root)
    st, dk = k["M_STEEL"], k["M_STEEL_DARK"]
    for y in (-1.1, 1.1):
        box(dk, (1.3, 0.48, 0.06), (4.3, y, 1.12))
        box(dk, (2.9, 0.48, 0.06), (-2.7, y, 1.12))
        box(dk, (0.04, 0.48, 0.6), (-4.2, y, 0.62))
        tube(st, (4.6, y * 1.09, 2.6), (4.75, y * 1.3, 2.6), 0.02, 4)
        box(dk, (0.06, 0.16, 0.4), (4.75, y * 1.36, 2.55))
        box(dk, (0.4, 0.3, 0.06), (3.0, y * 1.15, 0.65))
        cyl(st, 0.3, 1.2, (2.0, y * 0.95, 0.95), axis="X", segs=14)
    box(st, (4.5, 0.6, 0.04), (-1.4, 0, 3.55))
    for y in (-0.3, 0.3):
        for x in (-3.6, -1.4, 0.8):
            tube(st, (x, y, 3.55), (x, y, 4.4), 0.02, 4)
        tube(st, (-3.6, y, 4.4), (0.8, y, 4.4), 0.02, 4)
        tube(st, (-4.62, y * 0.8, 0.8), (-4.62, y * 0.8, 3.55), 0.02, 4)
    for z in (1.1, 1.5, 1.9, 2.3, 2.7, 3.1, 3.5):
        tube(st, (-4.62, -0.24, z), (-4.62, 0.24, z), 0.015, 4)
    for x in (-3.8, -1.35, 1.1):
        ring(st, 1.07, 0.04, (x, 0, 2.45), axis="X", segs=24, rsegs=4)
    k.done()
    status_light(site, f"{tid}_beacon", root, (3.8, 0, 3.27), r=0.1)
    lights = bmesh.new()
    for y in (-0.85, 0.85):
        box(lights, (0.06, 0.3, 0.2), (6.1, y, 1.65))
        box(lights, (0.06, 0.25, 0.12), (-4.6, y * 1.1, 1.0))
    site.mesh(f"{tid}_headlights", lights, "M_LAMP", root)
    for i, x in enumerate((4.3, -2.0, -3.4), start=1):
        axle = site.empty(f"{tid}_axle_{i}", (x, 0, 0.52), parent=root, size=0.5)
        tires, hubs = bmesh.new(), bmesh.new()
        for y in (-1.1, 1.1):
            cyl(tires, 0.52, 0.42, (0, y, 0), axis="Y", segs=20)
            cyl(hubs, 0.26, 0.44, (0, y, 0), axis="Y", segs=12)
            for j in range(6):
                a = 2 * PI * j / 6
                cyl(hubs, 0.03, 0.47, (0.16 * math.cos(a), y, 0.16 * math.sin(a)), axis="Y", segs=6)
        cyl(hubs, 0.06, 2.0, (0, 0, 0), axis="Y", segs=8)
        site.mesh(f"{tid}_axle_{i}_tire", tires, "M_RUBBER", axle)
        site.mesh(f"{tid}_axle_{i}_hub", hubs, "M_STEEL", axle)
