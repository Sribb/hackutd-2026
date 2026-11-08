"""Saltwater disposal facility: pump house, filters, two horizontal multistage injection
pumps (H-pumps) on skids, the injection header, the wellhead, and the fence.

fx=1 puts the trunk inlet on local +X, fx=-1 mirrors it to -X. The gate faces local -Y.
The settling tank, inlet meter run, trim, gate, sign and light tower come from
detail_site.swd_extras, so the building, filters and wellhead keep their old places.
"""
import math

from mathutils import Matrix, Vector

import field_parts as fp
from field_parts import Kit, ball, box, circle_pts, cyl, fine, hull, line, prism, rbox, ring, tube

HPUMPS_X = (9.6, 11.2)         # skid centerlines (times fx): the inlet side, between meter run and light tower
HP_Y0 = 7.4                    # discharge end; each pump runs south from here toward its motor
HP_X0, HP_X1 = 0.0, 7.4        # pump-local: discharge end to motor end along +X
SUCTION_X = HP_X1 - 0.85 - 1.5 - 0.55
SUCTION_Z, DISCHARGE_Z = 2.3, 1.55   # header heights: suction passes over the walkway


def placed_pump(k, matrix):
    """Build one H-pump along +X at the origin, then move only its vertices into place."""
    import bmesh
    mats = ("M_STEEL", "M_STEEL_DARK", "M_PIPE", "M_CONCRETE", "M_GLASS", "M_MOTOR")
    before = {m: set(k[m].verts) for m in mats}
    hpump(k)
    for m in mats:
        bmesh.ops.transform(k[m], matrix=matrix, verts=[v for v in k[m].verts if v not in before[m]])


def hpump(k, fx=1, y=0.0):
    """One H-pump skid along local X: motor, coupling guard, thrust chamber, suction, a long
    multistage barrel with stage clamps, and the discharge head with its gauge."""
    st, dk, pp = k["M_STEEL"], k["M_STEEL_DARK"], k["M_PIPE"]
    hi = fine()
    sg = lambda n: n if hi else max(8, n * 5 // 8)
    x = lambda v: v * fx
    z = 0.95                                                         # shaft height
    box(k["M_CONCRETE"], (HP_X1 - HP_X0 + 0.6, 1.3, 0.2), (x((HP_X0 + HP_X1) / 2), y, 0.25))
    box(dk, (HP_X1 - HP_X0 + 0.3, 0.95, 0.18), (x((HP_X0 + HP_X1) / 2), y, 0.44))
    # motor and its terminal box, at the motor end
    mx = HP_X1 - 0.85
    cyl(k["M_MOTOR"], 0.42, 1.3, (x(mx), y, z), axis="X", segs=sg(18))
    cyl(k["M_MOTOR"], 0.36, 0.2, (x(HP_X1 - 0.1), y, z), axis="X", segs=sg(18), r2=0.3)
    rbox(dk, (0.36, 0.3, 0.3), (x(mx), y + 0.45, z + 0.2), bevel=0.02)
    box(dk, (1.1, 0.8, 0.12), (x(mx), y, 0.59))
    if fine():
        for i in range(8):
            a = 2 * math.pi * (i + 0.5) / 8
            box(k["M_MOTOR"], (1.1, 0.06, 0.03), (x(mx), y + 0.43 * math.cos(a), z + 0.43 * math.sin(a)),
                Matrix.Rotation(a, 4, "X"))
    # coupling guard and thrust chamber
    guard = hull(circle_pts((y, z), 0.2, 8, 0, math.pi) + [(y - 0.22, 0.53), (y + 0.22, 0.53)])
    prism(st, guard, x(mx - 0.65), x(mx - 1.15), "YZ")
    tx = mx - 1.5
    cyl(dk, 0.27, 0.55, (x(tx), y, z), axis="X", segs=sg(14))
    box(dk, (0.5, 0.55, 0.38), (x(tx), y, 0.72))
    # suction case with its nozzle up to the suction header
    sx = tx - 0.55
    cyl(st, 0.3, 0.4, (x(sx), y, z), axis="X", segs=sg(16))
    tube(pp, (x(sx), y, z + 0.25), (x(sx), y, SUCTION_Z), 0.1, 10)
    fp.box(st, (0.3, 0.3, 0.04), (x(sx), y, 1.3))
    # the multistage barrel: a long casing with a clamp ring every stage
    bx0, bx1 = HP_X0 + 0.45, sx - 0.2
    cyl(st, 0.21, bx1 - bx0, (x((bx0 + bx1) / 2), y, z), axis="X", segs=sg(16), caps=False)
    n = 9 if fine() else 5
    for i in range(n):
        cyl(dk, 0.235, 0.05, (x(bx0 + (bx1 - bx0) * (i + 0.5) / n), y, z), axis="X", segs=sg(16))
    for xs in (bx0 + 0.6, (bx0 + bx1) / 2, bx1 - 0.6) if hi else (bx0 + 0.6, bx1 - 0.6):   # barrel supports
        prism(dk, [(y - 0.3, 0.53), (y + 0.3, 0.53), (y + 0.16, z - 0.12), (y - 0.16, z - 0.12)],
              x(xs - 0.06), x(xs + 0.06), "YZ")
    # discharge head and nozzle, check valve, gauge
    cyl(st, 0.3, 0.45, (x(HP_X0 + 0.22), y, z), axis="X", segs=sg(16))
    tube(pp, (x(HP_X0 + 0.22), y, z + 0.28), (x(HP_X0 + 0.22), y, 1.55), 0.08, 10)
    if not hi:
        return
    rbox(dk, (0.22, 0.24, 0.24), (x(HP_X0 + 0.22), y, 1.42), bevel=0.02)
    tube(st, (x(HP_X0 + 0.22), y - 0.12, 1.42), (x(HP_X0 + 0.22), y - 0.3, 1.42), 0.015, 6)
    cyl(dk, 0.065, 0.045, (x(HP_X0 + 0.22), y - 0.33, 1.5), axis="Y", segs=sg(10))
    cyl(k["M_GLASS"], 0.055, 0.01, (x(HP_X0 + 0.22), y - 0.36, 1.5), axis="Y", segs=sg(10), caps=False)


def build(site, sid, loc, fx=1, rot_z=0.0):
    root = site.empty(sid, loc, rot_z, kind="disposal_well", size=4)
    k = Kit(site, sid, root)
    st, dk, pp = k["M_STEEL"], k["M_STEEL_DARK"], k["M_PIPE"]
    box(k["M_GRAVEL"], (26, 22, 0.15), (0, 0, 0.075))
    # pump house: walls, roof with an overhang, roll-up door, man door, louvers
    rbox(st, (10, 6, 4), (3 * fx, -3, 2.15), bevel=0.04)
    rbox(dk, (10.6, 6.6, 0.2), (3 * fx, -3, 4.25), bevel=0.03)
    box(dk, (3.0, 0.05, 3.0), (5 * fx, -6.03, 1.65))
    if fine():
        for z in (0.6, 1.2, 1.8, 2.4, 3.0):
            box(st, (3.0, 0.07, 0.03), (5 * fx, -6.05, z))               # roll-up door slats
    box(dk, (1.0, 0.05, 2.1), (0, -6.03, 1.2))
    box(dk, (1.6, 0.05, 0.8), (3 * fx, -0.03, 2.9))                       # north louver
    # filter pods and their skid
    box(dk, (1.4, 3.0, 0.15), (-4.5 * fx, -3.4, 0.22))
    for y in (-4.2, -2.6):
        cyl(st, 0.45, 2.0, (-4.5 * fx, y, 1.15), segs=16)
        ball(st, 0.45, (-4.5 * fx, y, 2.15), segs=14)
        if fine():
            cyl(dk, 0.5, 0.06, (-4.5 * fx, y, 1.9), segs=16)
    # injection wellhead
    cyl(k["M_CONCRETE"], 0.6, 0.3, (0, 6, 0.3), segs=14)
    cyl(dk, 0.24, 1.9, (0, 6, 1.4), segs=14)
    box(dk, (0.55, 0.55, 0.45), (0, 6, 1.1))
    tube(dk, (0, 6, 1.9), (0.7 * fx, 6, 1.9), 0.09, 10)
    ring(st, 0.25, 0.025, (0.42 * fx, 6, 1.1), axis="X", segs=14, rsegs=4)
    # H-pumps on the inlet side, north of the meter run, running south from their discharge ends;
    # the suction header leaves the pump house's north wall, the injection header runs west to
    # the wellhead. The other side holds detail_site's skim tank and truck offload header.
    turn = Matrix.Rotation(-math.pi / 2, 4, "Z")
    for px in HPUMPS_X:
        placed_pump(k, Matrix.Translation((px * fx, HP_Y0, 0)) @ turn)
    sy = HP_Y0 - SUCTION_X
    line(pp, [(7.5 * fx, 0.05, SUCTION_Z), (7.5 * fx, sy, SUCTION_Z), (HPUMPS_X[-1] * fx, sy, SUCTION_Z)], 0.1)
    dy = HP_Y0 - 0.22
    line(pp, [(HPUMPS_X[-1] * fx, dy, DISCHARGE_Z), (0.3 * fx, dy, DISCHARGE_Z), (0.3 * fx, 6.5, DISCHARGE_Z),
              (0.3 * fx, 6.5, 0.6), (0.3 * fx, 6, 0.6)], 0.08)
    tube(pp, (8.0 * fx, -2, 0.6), (9.5 * fx, -2, 0.6), 0.16, 12)                            # trunk inlet
    # fence
    x0, x1, y0, y1 = -12.5, 12.5, -10.5, 10.5
    runs = [((x0, y1), (x1, y1)), ((x0, y0), (x0, y1)), ((x1, y0), (x1, y1)),
            ((x0, y0), (-4, y0)), ((4, y0), (x1, y0))]
    gap = 3 if fp.LOD["posts"] else 6
    for (ax, ay), (bx, by) in runs:
        length = math.hypot(bx - ax, by - ay)
        for i in range(int(length // gap) + 1):
            t = min(1.0, i * gap / length)
            px, py = ax + (bx - ax) * t, ay + (by - ay) * t
            tube(st, (px, py, 0.1), (px, py, 1.8), 0.04, 6)
        for z in (0.9, 1.7):
            tube(st, (ax, ay, z), (bx, by, z), 0.025, 6)
    k.done()
    return root, {"inlet": root.matrix_basis @ Vector((9.5 * fx, -2, 0.6))}
