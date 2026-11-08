"""Beam pumping unit (pumpjack) and wellhead, built around the console's two pivots.

WELL_k_beam rocks about local X at (0, 0, 4.1) and WELL_k_crank spins about local X at
(0, 2.7, 1.6); both keep those origins and ship with no rotation. The horse head faces
local -Y over the wellhead at (0, -3.5).

- The beam mesh holds what rides on the walking beam: the I-beam, the horse head with
  its wireline, the saddle and the equalizer.
- The crank mesh holds the crankshaft, two crank arms, their counterweights and the
  wrist pins. At rest the pins point to +Y, straight under the equalizer.
- WELL_k_pitman (child of the beam, pivot on the equalizer's axis) holds both pitman
  arms, and WELL_k_bridle (child of the beam, pivot at the horse head's tangent point)
  holds the wireline bridle, carrier bar and polished rod. Both rock with the beam as
  shipped; docs/CONTRACT.md says how a console can keep them plumb and on the pins.
- Everything else (base, Samson post, gear reducer, motor, belt guard, brake, ladder,
  wellhead) is static, merged one mesh per material like every other asset.
"""
import math

import bmesh
from mathutils import Matrix, Vector

from field_parts import LOD, Kit, box, circle_pts, cyl, fine, hull, prism, rbox, strut, tube

PIVOT_Z = 4.1                  # saddle bearing
CY, CZ = 2.7, 1.6              # crankshaft (y, z)
HEAD_R = 3.47                  # horse head face radius about the pivot
LINE_R = HEAD_R + 0.03         # the wireline rides on the face, so it hangs at y = -LINE_R
WELL_Y = -LINE_R               # wellhead center, under the polished rod
PIN_R = 0.85                   # wrist pin radius: a 1.7 m stroke, about +-0.25 rad of beam
PIT_X = 0.93                   # pitman arms, either side
EQ_Y, EQ_Z = CY + PIN_R, -0.24  # equalizer (beam-local), straight over the wrist pins at rest
BASE_Z = 0.56                  # top of the steel base
STUFFING_Z = 1.42              # top of the stuffing box
CARRIER_Z = 2.6                # carrier bar at rest
TEE_Z = 0.85                   # flow tee, and the flowline tie-in
IN_Y, IN_Z = 3.68, 1.02        # gear reducer input shaft, outside the crank's sweep
MOTOR_Y, MOTOR_Z = 4.45, 1.0
BELT_X = 1.13                  # belt drive, outboard of the pitman arms
TOP = PIVOT_Z - 0.24           # top of the Samson post


def tier():
    """2 at full detail (the hero unit), 1 at lite (about the old full cost), 0 at bare."""
    return 2 if fine() else (1 if LOD["trim"] else 0)


def sg(full, lite, bare):
    """Pick a segment count (or any value) for the current tier."""
    return (bare, lite, full)[tier()]


def _base(k):
    dk = k["M_STEEL_DARK"]
    box(k["M_GRAVEL"], (5.0, 10.5, 0.12), (0, 0.2, 0.06))
    box(k["M_CONCRETE"], (2.5, 6.7, 0.3), (0, 1.6, 0.15))
    box(k["M_CONCRETE"], (1.6, 1.4, 0.16), (0, WELL_Y, 0.06))       # wellhead cellar slab
    y0, y1 = -1.45, 4.85
    for sx in (-0.42, 0.42):                                         # two wide-flange skid beams
        box(dk, (0.2, y1 - y0, 0.025), (sx, (y0 + y1) / 2, BASE_Z - 0.0125))
        if tier() == 2:
            box(dk, (0.025, y1 - y0, 0.23), (sx, (y0 + y1) / 2, 0.42))
    for y, w in ((-1.05, 1.95), (1.45, 1.35), (4.7, 0.84)):
        box(dk, (w, 0.16, 0.2), (0, y, 0.43))                        # cross members and outriggers


def _samson(k):
    st, dk = k["M_STEEL"], k["M_STEEL_DARK"]
    feet = [(-0.85, -1.05), (0.85, -1.05), (-0.62, 1.45), (0.62, 1.45)]
    heads = [(-0.2, -0.07), (0.2, -0.07), (-0.2, 0.1), (0.2, 0.1)]

    def leg(i, z):
        (fx, fy), (hx, hy) = feet[i], heads[i]
        t = (z - BASE_Z) / (TOP - BASE_Z)
        return Vector((fx + (hx - fx) * t, fy + (hy - fy) * t, z))

    for i in range(4):
        strut(st, leg(i, BASE_Z), leg(i, TOP), 0.15)
    for z in (1.55, 2.75) if tier() else (2.2,):                     # cross braces
        strut(st, leg(0, z), leg(1, z), 0.07)
        strut(st, leg(2, z), leg(3, z), 0.07)
    for a, b in ((0, 2), (1, 3)) if tier() else ():                   # side braces
        strut(st, leg(a, 0.9), leg(b, 2.4), 0.06)
        if tier() == 2:
            strut(st, leg(a, 3.3), leg(b, 3.3), 0.06)
    box(dk, (0.66, 0.5, 0.08), (0, 0.02, TOP + 0.04))                 # cap plate
    for sx in (-0.27, 0.27):                                         # pillow blocks under the saddle
        box(dk, (0.13, 0.36, 0.18), (sx, 0, TOP + 0.17))
        if tier() == 2:
            cyl(st, 0.105, 0.14, (sx, 0, PIVOT_Z), axis="X", segs=10)
    if tier() < 2:
        return
    # ladder up the front, to a small platform under the saddle
    for sx in (-0.2, 0.2):
        strut(st, (sx, -1.2, BASE_Z), (sx, -0.42, TOP - 0.25), 0.045, 0.02)
    for i in range(1, 9):
        p = Vector((0, -1.2 + 0.78 * i / 9, BASE_Z + (TOP - 0.25 - BASE_Z) * i / 9))
        strut(st, p + Vector((-0.2, 0, 0)), p + Vector((0.2, 0, 0)), 0.025)
    box(dk, (0.9, 0.5, 0.04), (0, -0.42, TOP - 0.27))
    for sx in (-0.45, 0.45):
        strut(st, (sx, -0.65, TOP - 0.27), (sx, -0.65, TOP + 0.62), 0.035)
        strut(st, (sx, -0.65, TOP + 0.62), (sx, -0.2, TOP + 0.62), 0.035)


def _reducer(k):
    st, dk = k["M_STEEL"], k["M_STEEL_DARK"]
    box(dk, (0.95, 1.95, 0.2), (0, CY + 0.32, BASE_Z + 0.1))          # sub-base
    rbox(st, (0.84, 1.85, 0.66), (0, CY + 0.32, 1.09), bevel=0.05)     # lower housing, back to the input shaft
    cyl(st, 0.56, 0.84, (0, CY, CZ - 0.02), axis="X", segs=sg(20, 16, 12))   # bull gear case around the crankshaft
    if not tier():
        return
    box(dk, (0.9, 1.3, 0.045), (0, CY, CZ))                           # split line
    box(dk, (0.9, 0.9, 0.045), (0, CY + 0.6, 1.42))
    box(dk, (0.06, 0.32, 0.22), (-1.12, 4.15, BASE_Z + 0.11))         # brake ratchet quadrant
    strut(st, (-1.12, 4.12, BASE_Z + 0.15), (-1.12, 3.95, 2.1), 0.05)  # brake lever
    box(dk, (0.08, 0.08, 0.14), (-1.12, 3.95, 2.13))
    if tier() < 2:
        return
    for s in (-1, 1):
        cyl(dk, 0.22, 0.08, (s * 0.46, CY, CZ), axis="X", segs=12)    # crankshaft bearing caps
        cyl(dk, 0.14, 0.08, (s * 0.46, IN_Y, IN_Z), axis="X", segs=8)
    cyl(st, 0.05, 2.18, (BELT_X / 2 - 0.47, IN_Y, IN_Z), axis="X", segs=6, caps=False)   # input shaft
    box(dk, (0.3, 0.22, 0.06), (0, CY - 0.1, CZ + 0.56))              # inspection cover
    # brake: drum on the input shaft's far end, its band, and a tall lever by the motor
    cyl(dk, 0.21, 0.13, (-1.0, IN_Y, IN_Z), axis="X", segs=12)
    cyl(st, 0.225, 0.05, (-1.0, IN_Y, IN_Z), axis="X", segs=12, caps=False)
    strut(st, (-1.12, 4.05, 1.1), (-1.06, IN_Y + 0.2, IN_Z - 0.1), 0.025)


def _motor_and_belt(k):
    st, dk = k["M_STEEL"], k["M_STEEL_DARK"]
    my, mz = MOTOR_Y, MOTOR_Z
    box(dk, (0.85, 0.7, 0.08), (0.05, my, BASE_Z + 0.04))             # slide base
    for sx in (-0.22, 0.32):
        box(dk, (0.1, 0.5, 0.2), (sx, my, BASE_Z + 0.18))              # feet
    cyl(st, 0.3, 0.74, (0.05, my, mz), axis="X", segs=sg(14, 12, 10))  # frame
    pts = hull(circle_pts((IN_Y, IN_Z), 0.47, sg(12, 10, 8)) + circle_pts((my, mz), 0.2, sg(8, 6, 5)))
    prism(k["M_STEEL"], pts, BELT_X - 0.1, BELT_X + 0.1, "YZ")         # V-belt guard, outboard of the crank
    if not tier():
        return
    cyl(dk, 0.27, 0.16, (-0.4, my, mz), axis="X", segs=sg(14, 12, 10), r2=0.22)    # fan cover
    box(dk, (0.22, 0.2, 0.18), (0.05, my, mz + 0.38))                 # conduit box
    if tier() < 2:
        return
    for i in range(6):                                               # cooling fins
        a = 2 * math.pi * (i + 0.5) / 6
        box(st, (0.62, 0.035, 0.05), (0.05, my + 0.31 * math.cos(a), mz + 0.31 * math.sin(a)),
            Matrix.Rotation(a, 4, "X"))
    strut(dk, (0.05, my + 0.1, mz + 0.38), (0.05, 4.95, mz + 0.38), 0.045)
    strut(dk, (0.05, 4.95, mz + 0.4), (0.05, 4.95, 0.1), 0.045)
    cyl(st, 0.04, BELT_X - 0.38, ((BELT_X + 0.47) / 2, my, mz), axis="X", segs=6, caps=False)   # motor shaft
    for y in (IN_Y, my):
        strut(dk, (0.5, y - 0.06, BASE_Z + 0.2), (BELT_X - 0.1, y - 0.06, 0.62), 0.04)   # guard brackets


def _wellhead(k):
    """Casing head, tubing head, flow tee, stuffing box, and the static stub of polished rod."""
    st, dk = k["M_STEEL"], k["M_STEEL_DARK"]
    y = WELL_Y
    n = sg(12, 10, 8)
    cyl(dk, 0.2, 0.62, (0, y, 0.31), segs=n, caps=False)               # casing and tubing heads
    cyl(dk, 0.29, 0.07, (0, y, 0.3), segs=n)
    cyl(st, 0.12, 0.3, (0, y, 0.8), segs=sg(10, 8, 8))                 # flow tee body
    cyl(st, 0.055, 0.5, (0.35, y, TEE_Z), axis="X", segs=sg(8, 6, 6), caps=False)   # tie-in nipple
    cyl(st, 0.075, 0.42, (0, y, 1.21), segs=sg(10, 8, 6), caps=False)  # stuffing box
    cyl(st, 0.032, 2.05 - STUFFING_Z, (0, y, (2.05 + STUFFING_Z) / 2), segs=sg(8, 6, 6))   # polished rod
    if tier() < 2:
        return
    cyl(dk, 0.24, 0.06, (0, y, 0.37), segs=n)                          # tubing head flange
    cyl(dk, 0.2, 0.05, (0, y, 0.64), segs=n)
    cyl(dk, 0.17, 0.05, (0, y, 0.97), segs=n)
    cyl(dk, 0.1, 0.12, (0, y, STUFFING_Z - 0.06), segs=6)               # packing gland nut
    cyl(st, 0.03, 0.22, (-0.11, y, TEE_Z), axis="X", segs=6, caps=False)  # gauge nipple


def _beam(site, wid, root):
    bm = bmesh.new()
    lo, hi, w = 0.12, 0.67, 0.34
    y0, y1 = -2.45, EQ_Y + 0.35
    box(bm, (w, y1 - y0, 0.04), (0, (y0 + y1) / 2, hi - 0.02))         # I-beam flanges and web
    box(bm, (w, y1 - y0, 0.04), (0, (y0 + y1) / 2, lo + 0.02))
    box(bm, (0.03, y1 - y0, hi - lo), (0, (y0 + y1) / 2, (lo + hi) / 2))
    if fine():
        y = y0 + 0.45
        while y < y1 - 0.2:
            if abs(y) > 0.35:
                box(bm, (w - 0.04, 0.02, hi - lo - 0.08), (0, y, (lo + hi) / 2))   # web stiffeners
            y += 0.9
    box(bm, (0.46, 0.66, 0.06), (0, 0, lo - 0.03))                    # saddle
    cyl(bm, 0.08, 0.68, (0, 0, 0), axis="X", segs=sg(10, 8, 6))        # center bearing shaft
    for sy in (-0.3, 0.3) if tier() == 2 else ():
        strut(bm, (0, sy, lo - 0.06), (0, 0, -0.06), 0.26, 0.04)
    # horse head: side plates with a curved face centered on the pivot, so the line stays plumb
    a0, a1, n = -0.43, 0.31, sg(8, 6, 4)
    face = [(-HEAD_R * math.cos(a), HEAD_R * math.sin(a)) for a in (a1 - (a1 - a0) * i / n for i in range(n + 1))]
    back = [(-2.88, -1.2), (-2.42, -0.3), (-2.3, lo), (-2.3, hi), (-2.62, 0.98)]
    prism(bm, face + back, -0.21, 0.21, "YZ")

    def arc(r0, r1, x0, x1):
        out = [(-r1 * math.cos(a), r1 * math.sin(a)) for a in (a0 + (a1 - a0) * i / n for i in range(n + 1))]
        prism(bm, out + [(-r0 * math.cos(a), r0 * math.sin(a)) for a in
                         (a1 - (a1 - a0) * i / n for i in range(n + 1))], x0, x1, "YZ")
    arc(HEAD_R - 0.03, HEAD_R + 0.012, -0.27, 0.27)                    # face plate, wider than the sides
    top = (-HEAD_R * math.cos(a1), HEAD_R * math.sin(a1))
    box(bm, (0.36, 0.22, 0.14), (0, top[0] + 0.18, top[1] + 0.02))    # wireline hanger
    for sx in (-0.1, 0.1) if tier() == 2 else ():                     # wireline over the face
        pts = [Vector((sx, -LINE_R * math.cos(a), LINE_R * math.sin(a))) for a in (a1 * (1 - i / 2) for i in range(3))]
        for p, q in zip(pts, pts[1:]):
            strut(bm, p, q, 0.028)
    # equalizer across the beam's tail; the pitman arms hang from it (WELL_k_pitman)
    box(bm, (0.36, 0.42, 0.14), (0, EQ_Y, lo - 0.07))                 # tail bearing
    cyl(bm, 0.1, 0.4, (0, EQ_Y, -0.08), axis="X", segs=sg(10, 8, 6))
    rbox(bm, (2.06, 0.24, 0.22), (0, EQ_Y, EQ_Z), bevel=0.03)          # equalizer
    return site.mesh(f"{wid}_beam", bm, "M_STEEL", root, loc=(0, 0, PIVOT_Z))


def _pitman(site, wid, beam):
    """Both pitman arms, from the equalizer down to the wrist pins. Pivot: the equalizer's axis."""
    bm = bmesh.new()
    pz = CZ - PIVOT_Z - EQ_Z                                           # down to the wrist pins at rest
    for s in (-1, 1):
        x = s * PIT_X
        cyl(bm, 0.12, 0.13, (x, 0, 0), axis="X", segs=sg(10, 6, 6))    # top bearing
        if tier() == 2:
            box(bm, (0.07, 0.04, -pz - 0.22), (x, 0, pz / 2))          # I-section arm
            for dy in (-0.065, 0.065):
                box(bm, (0.12, 0.04, -pz - 0.22), (x, dy, pz / 2))
        else:
            box(bm, (0.1, 0.15, -pz - 0.22), (x, 0, pz / 2))
        cyl(bm, 0.13, 0.13, (x, 0, pz), axis="X", segs=sg(10, 6, 6))   # wrist pin bearing
    return site.mesh(f"{wid}_pitman", bm, "M_STEEL", beam, loc=(0, EQ_Y, EQ_Z))


def _crank(site, wid, root):
    bm = bmesh.new()
    cyl(bm, 0.11, 1.5, (0, 0, 0), axis="X", segs=sg(10, 8, 6))         # crankshaft
    arm = circle_pts((0, 0), 0.27, 7, math.pi / 2, 3 * math.pi / 2) + [(1.02, -0.17), (1.02, 0.17)]
    cw = [(0.3, -0.36), (0.78, -0.5), (0.78, 0.5), (0.3, 0.36)]       # counterweights, toward the pin
    for s in (-1, 1):
        x = s * 0.66
        prism(bm, arm, x - 0.05, x + 0.05, "YZ")
        for d0, d1 in ((x - 0.14, x - 0.05), (x + 0.05, x + 0.14)) if tier() == 2 else ((x - 0.14, x + 0.14),):
            prism(bm, cw, d0, d1, "YZ")
        cyl(bm, 0.07, PIT_X - 0.66 + 0.1, (s * (0.66 + PIT_X) / 2, PIN_R, 0), axis="X", segs=sg(8, 6, 6))   # wrist pin
        if tier():
            cyl(bm, 0.2, 0.12, (s * 0.52, 0, 0), axis="X", segs=sg(10, 8, 6))   # crank hub
    return site.mesh(f"{wid}_crank", bm, "M_STEEL_DARK", root, loc=(0, CY, CZ))


def _bridle(site, wid, beam):
    """Wireline, carrier bar, rod clamp and the upper polished rod, hanging from the tangent point."""
    bm = bmesh.new()
    dz = CARRIER_Z - PIVOT_Z
    for sx in (-0.1, 0.1):
        strut(bm, (sx, 0, 0), (sx, 0, dz + 0.1), 0.028)
        if tier() == 2:
            cyl(bm, 0.03, 0.12, (sx, 0, dz + 0.1), segs=6)             # rope sockets
    rbox(bm, (0.44, 0.12, 0.09), (0, 0, dz), bevel=0.015)              # carrier bar
    box(bm, (0.13, 0.13, 0.12), (0, 0, dz + 0.11))                     # rod clamp
    cyl(bm, 0.032, 1.26, (0, 0, dz - 0.27), segs=sg(8, 6, 6))          # polished rod
    return site.mesh(f"{wid}_bridle", bm, "M_STEEL", beam, loc=(0, -LINE_R, 0))


def build(site, wid, loc, rot_deg):
    """The whole unit. Returns (root, {"outlet": world point where the flowline starts})."""
    root = site.empty(wid, loc, math.radians(rot_deg), kind="well", size=2)
    k = Kit(site, wid, root)
    _base(k)
    _samson(k)
    _reducer(k)
    _motor_and_belt(k)
    _wellhead(k)
    k.done()
    beam = _beam(site, wid, root)
    _pitman(site, wid, beam)
    _bridle(site, wid, beam)
    _crank(site, wid, root)
    return root, {"outlet": root.matrix_basis @ Vector((0.6, WELL_Y, TEE_Z))}
