"""Generic vacuum truck: a day cab with a long hood on a straight frame, a vacuum tank body,
the vacuum pump and moisture trap behind the cab, hose trays and a hose reel, and the rear
outlet with its valve and camlock. No real maker's grille, badge, or shape.

The truck faces local +X with its origin on the ground at its center. Wheels, fenders,
mirrors, steps, fuel tanks, the top catwalk, the rear ladder, the beacon and the lights
come from detail_site.truck_extras, so this body keeps the sizes those parts sit on: cab
roof at 3.18 m around x 3.8, tank radius 1.05 m centered 2.45 m up from x -4.45 to 1.75,
front face at x 6.1, axles at x 4.3, -2.0 and -3.4.
"""
import math

from mathutils import Matrix, Vector

from field_parts import Kit, ball, box, cyl, fine, prism, rbox, strut, tube

TANK_Z, TANK_R = 2.45, 1.05
TANK_X0, TANK_X1 = -4.15, 1.45          # straight shell; the dished heads reach -4.45 and 1.75


def _dished(bm, x, sign, r=TANK_R, depth=0.3, segs=20):
    """A dished tank head at x, bulging toward sign (+1 front, -1 rear)."""
    import bmesh
    m = (Matrix.Translation((x, 0, TANK_Z)) @ Matrix.Rotation(math.pi / 2, 4, "Y")
         @ Matrix.Diagonal((1.0, 1.0, depth / r, 1.0)))
    before = set(bm.verts)
    bmesh.ops.create_uvsphere(bm, u_segments=segs, v_segments=8, radius=r, matrix=m)
    gone = [v for v in bm.verts if v not in before and (v.co.x - x) * sign < -1e-4]
    bmesh.ops.delete(bm, geom=gone, context="VERTS")


def _cab(k):
    st, dk, gl = k["M_STEEL"], k["M_STEEL_DARK"], k["M_GLASS"]
    rbox(st, (2.2, 2.36, 1.88), (3.65, 0, 2.21), bevel=0.08)                 # cab
    rbox(st, (2.0, 2.24, 0.08), (3.6, 0, 3.16), bevel=0.03)                  # roof cap
    box(gl, (0.05, 2.06, 0.82), (4.77, 0, 2.62), Matrix.Rotation(-0.16, 4, "Y"))   # windshield
    box(dk, (0.04, 0.06, 0.82), (4.79, 0, 2.62), Matrix.Rotation(-0.16, 4, "Y"))   # center post
    for s in (-1, 1):
        box(gl, (1.05, 0.04, 0.68), (3.98, s * 1.185, 2.62))                 # door glass
        box(gl, (0.45, 0.04, 0.6), (2.98, s * 1.185, 2.66))                  # rear quarter glass
        box(dk, (0.02, 0.03, 1.55), (3.42, s * 1.19, 2.15))                  # door seam
        box(dk, (0.18, 0.04, 0.04), (3.6, s * 1.2, 2.15))                    # handle
        for z in (0.62, 0.95):
            box(dk, (0.42, 0.3, 0.05), (3.3, s * 1.18, z))                   # cab steps
    # long hood: a sloped profile, a plain grille with bars, bumper
    prism(st, [(4.72, 1.2), (6.04, 1.2), (6.1, 1.78), (5.98, 2.12), (4.72, 2.3)], -1.0, 1.0, "XZ")
    box(dk, (0.06, 1.3, 0.72), (6.11, 0, 1.66))
    if fine():
        for z in (1.4, 1.55, 1.7, 1.85):
            box(st, (0.04, 1.26, 0.03), (6.14, 0, z))
    rbox(st, (0.32, 2.46, 0.32), (6.2, 0, 0.95), bevel=0.05)                 # bumper
    # exhaust stack and air cleaner behind the cab
    tube(st, (2.45, 1.0, 1.25), (2.45, 1.0, 3.75), 0.07, 10 if fine() else 6)
    if fine():
        cyl(dk, 0.1, 1.0, (2.45, 1.0, 2.6), segs=10, caps=False)             # heat shield
        cyl(dk, 0.24, 0.7, (2.5, -1.0, 2.1), segs=14)                        # air cleaner
        rbox(dk, (0.5, 0.3, 0.06), (5.4, 0, 2.2), bevel=0.02)                # hood scoop, generic


def _frame(k):
    dk, st = k["M_STEEL_DARK"], k["M_STEEL"]
    for s in (-1, 1):                                                        # frame rails
        box(dk, (10.3, 0.08, 0.3), (0.85, s * 0.45, 1.0))
        box(dk, (10.3, 0.2, 0.04), (0.85, s * 0.39, 1.13))
    for x in (-3.5, -1.3, 0.9):                                              # tank cradles
        box(dk, (0.3, 1.8, 0.45), (x, 0, 1.38))
    box(dk, (5.6, 0.2, 0.2), (-1.35, 0, 1.2))                                # subframe
    box(dk, (0.12, 2.2, 0.15), (-4.6, 0, 0.62))                              # rear underride bar
    for s in (-0.9, 0.9):
        box(dk, (0.1, 0.1, 0.5), (-4.5, s, 0.85))


def _tank(k):
    tk, st, dk = k["M_TANK"], k["M_STEEL"], k["M_STEEL_DARK"]
    n = 24 if fine() else 16
    cyl(tk, TANK_R, TANK_X1 - TANK_X0, ((TANK_X0 + TANK_X1) / 2, 0, TANK_Z), axis="X", segs=n, caps=False)
    _dished(tk, TANK_X1, 1, segs=n)
    _dished(tk, TANK_X0, -1, segs=n)
    cyl(dk, TANK_R + 0.04, 0.12, (TANK_X0 - 0.02, 0, TANK_Z), axis="X", segs=n, caps=False)   # rear door ring
    if fine():
        for a in range(0, 360, 45):                                          # door clamps
            p = Vector((TANK_X0 - 0.06, (TANK_R + 0.06) * math.cos(math.radians(a)),
                        TANK_Z + (TANK_R + 0.06) * math.sin(math.radians(a))))
            box(dk, (0.12, 0.08, 0.08), p)
    box(dk, (0.2, 0.25, 0.3), (TANK_X0 - 0.12, 0, TANK_Z + TANK_R + 0.05))  # door hinge
    for x in (-2.6, 0.3):                                                    # manways
        cyl(dk, 0.3, 0.16, (x, 0, TANK_Z + TANK_R + 0.04), segs=14)
        cyl(st, 0.33, 0.05, (x, 0, TANK_Z + TANK_R + 0.14), segs=14)
    if not fine():
        return
    # sight glass on the rear head, and the level float rod
    tube(st, (TANK_X0 - 0.2, -0.62, 1.75), (TANK_X0 - 0.2, -0.62, 3.1), 0.035, 6)
    cyl(k["M_GLASS"], 0.045, 1.1, (TANK_X0 - 0.2, -0.62, 2.42), segs=6, caps=False)


def _vacuum(k):
    st, dk, pp = k["M_STEEL"], k["M_STEEL_DARK"], k["M_PIPE"]
    rbox(dk, (0.85, 1.0, 0.7), (2.05, -0.15, 1.6), bevel=0.04)               # vacuum pump
    cyl(st, 0.25, 0.12, (2.05, 0.42, 1.6), axis="Y", segs=14)                # drive sheave
    cyl(st, 0.24, 1.15, (2.05, 0.62, 2.45), segs=14)                         # moisture trap
    cyl(dk, 0.27, 0.06, (2.05, 0.62, 3.05), segs=14)
    tube(pp, (2.05, 0.62, 3.08), (2.05, 0.62, 3.3), 0.06, 8)
    tube(pp, (2.05, 0.62, 3.3), (1.25, 0.3, 3.42), 0.06, 8)                  # vacuum line to the tank top
    ball(pp, 0.07, (2.05, 0.62, 3.3), segs=6)
    if not fine():
        return
    tube(pp, (2.05, -0.15, 1.95), (2.05, -0.15, 2.4), 0.05, 8)               # pump exhaust and silencer
    cyl(st, 0.12, 0.5, (2.05, -0.15, 2.6), segs=10)


def _hoses(k):
    st, dk, rb = k["M_STEEL"], k["M_STEEL_DARK"], k["M_RUBBER"]
    for s in (-1, 1):                                                        # hose trays down both sides
        box(dk, (5.1, 0.32, 0.03), (-1.35, s * 1.2, 1.36))
        box(dk, (5.1, 0.03, 0.2), (-1.35, s * 1.35, 1.46))
        for dy in (-0.07, 0.07) if fine() else (0.0,):
            cyl(rb, 0.065, 4.9, (-1.35, s * 1.2 + dy, 1.45), axis="X", segs=8 if fine() else 6, caps=False)
        for x in (-3.4, -1.35, 0.7):
            strut(st, (x, s * 1.35, 1.36), (x, s * 0.5, 1.15), 0.05)        # tray brackets
    if not fine():
        return
    # hose reel under the rear head, curb side
    rx, ry, rz = -4.3, 0.5, 0.98
    cyl(rb, 0.2, 0.36, (rx, ry, rz), axis="Y", segs=14)
    for dy in (-0.19, 0.19):
        cyl(st, 0.38, 0.025, (rx, ry + dy, rz), axis="Y", segs=16 if fine() else 10)
    strut(dk, (rx, ry - 0.24, rz), (rx + 0.2, ry - 0.24, 1.38), 0.05)
    strut(dk, (rx, ry + 0.24, rz), (rx + 0.2, ry + 0.24, 1.38), 0.05)


def _rear_valves(k):
    """The 4-inch rear outlet: tank nozzle, gate valve with a lever, camlock and dust cap; and a
    side drain valve with a handwheel."""
    import field_parts as fp
    st, dk, pp = k["M_STEEL"], k["M_STEEL_DARK"], k["M_PIPE"]
    z = 1.7
    tube(pp, (TANK_X0, 0, z), (-4.95, 0, z), 0.08, 10)
    rbox(dk, (0.2, 0.26, 0.26), (-4.6, 0, z), bevel=0.02)                   # valve body
    strut(st, (-4.6, 0.0, z + 0.13), (-4.6, 0.32, z + 0.45), 0.04)           # lever
    cyl(st, 0.1, 0.08, (-4.86, 0, z), axis="X", segs=12)                     # camlock
    for s in (-1, 1):
        strut(st, (-4.86, s * 0.1, z), (-4.8, s * 0.17, z + 0.12), 0.025)   # cam arms
    cyl(dk, 0.1, 0.06, (-4.97, 0, z), axis="X", segs=12)                     # dust cap
    if not fine():
        return
    tube(pp, (-4.0, -0.7, 1.55), (-4.0, -0.7, 1.2), 0.05, 8)                 # side drain
    box(dk, (0.16, 0.16, 0.16), (-4.0, -0.7, 1.2))
    strut(st, (-4.0, -0.78, 1.2), (-4.0, -0.95, 1.2), 0.02)
    placed_wheel = Matrix.Translation((-4.0, -0.95, 1.2)) @ Matrix.Rotation(math.pi / 2, 4, "X")
    import bmesh
    before = set(st.verts)
    fp.handwheel(st, 0.1)
    bmesh.ops.transform(st, matrix=placed_wheel, verts=[v for v in st.verts if v not in before])


def build(site, tid, loc, rot_deg=0.0):
    root = site.empty(tid, loc, math.radians(rot_deg), kind="truck", size=2)
    k = Kit(site, tid, root)
    _frame(k)
    _cab(k)
    _tank(k)
    _vacuum(k)
    _hoses(k)
    _rear_valves(k)
    k.done()
    return root   # wheels are axle objects added in detail_site.truck_extras
