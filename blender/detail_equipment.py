"""Equipment detail bolted onto the core assets from field_parts.py.

Adds what an operator looks for up close: gauges, sensors, control panels with
status lights, valve trim, caged ladders, wellhead trees, labels, and the truck
load-out stands. Status lights use M_LAMP so the console can light them.
"""
import math

import bmesh
import bpy
from mathutils import Matrix, Vector

import field_parts as fp
from field_parts import PI, TANK_H, TANK_R, Kit, ball, box, cyl, line, polar, ring, tube

H, R = TANK_H, TANK_R
GAUGE_ANGLE = 200.0
LABEL_ANGLE = -25.0
LOADOUT_ANGLE = 215.0
GAUGE_Z0, GAUGE_SPAN = 0.3, 4.3   # float marker height (tank-local) for an empty and a full tank


def placed(bm, matrix, build, *args, **kwargs):
    """Run a primitive builder, then move only the vertices it created."""
    before = set(bm.verts)
    build(bm, *args, **kwargs)
    bmesh.ops.transform(bm, matrix=matrix, verts=[v for v in bm.verts if v not in before])


def facing(deg):
    """Euler rotation that stands flat text up, facing outward at angle deg."""
    return (PI / 2, 0.0, math.radians(deg) + PI / 2)


def label(site, name, text, size, loc, rot, parent=None, mat="M_STEEL_DARK", small=True):
    """Extruded text converted to a mesh, so it exports to glTF. Small labels can be dropped."""
    if small and not fp.LOD["small_labels"]:
        return None
    cu = bpy.data.curves.new(f"{name}_font", "FONT")
    cu.body, cu.size, cu.extrude = text, size, 0.0   # flat paint: a quarter of the triangles of extruded text
    cu.align_x, cu.align_y = "CENTER", "CENTER"
    cu.resolution_u = 3 if small else 6
    tmp = bpy.data.objects.new(f"{name}_tmp", cu)
    # convert in a private scene: updating the field's own view layer costs more with every object
    scratch = bpy.data.scenes.new("_LABELS")
    scratch.collection.objects.link(tmp)
    scratch.view_layers[0].update()
    me = bpy.data.meshes.new_from_object(tmp.evaluated_get(scratch.view_layers[0].depsgraph))
    bpy.data.objects.remove(tmp, do_unlink=True)
    bpy.data.scenes.remove(scratch)
    bpy.data.curves.remove(cu)
    me.name = name
    me.materials.clear()
    me.materials.append(fp.material(mat))
    obj = bpy.data.objects.new(name, me)
    obj.location, obj.rotation_euler = loc, rot
    if parent:
        obj.parent = parent
    site.col.objects.link(obj)
    return obj


def flange(bm, center, direction, r, thick=0.05, segs=12):
    d = Vector(direction).normalized() * thick / 2
    tube(bm, Vector(center) - d, Vector(center) + d, r, segs)


def bolts(bm, center, axis, r, n=8, size=0.018):
    """Hex bolt heads around a flange whose face is normal to axis X, Y or Z."""
    if not fp.LOD["bolts"]:
        return
    c = Vector(center)
    for i in range(n):
        a = 2 * PI * i / n
        off = {"X": (0, r * math.cos(a), r * math.sin(a)), "Y": (r * math.cos(a), 0, r * math.sin(a)),
               "Z": (r * math.cos(a), r * math.sin(a), 0)}[axis]
        cyl(bm, size * 1.1, size * 2.2, c + Vector(off), axis=axis, segs=6)


def id_text(aid):
    """Painted text for an asset ID: PUMP_3 -> PUMP 3."""
    return aid.replace("_", " ")


def status_light(site, name, parent, loc, r=0.06):
    bm = bmesh.new()
    cyl(bm, r * 1.3, 0.03, (0, 0, -0.06), segs=8)
    cyl(bm, r, 0.1, (0, 0, 0), segs=8)
    ball(bm, r, (0, 0, 0.05), segs=8)
    return site.mesh(name, bm, "M_LAMP", parent, loc=loc)


STAIR = (15.0, 108.0)      # circumferential stair, foot to landing (tank-local degrees)
WALK = 116.0               # roof walkway, from the landing to the center
DECK_Z = H + 0.62          # walkway deck height


def roof_z(r):
    """Height of the cone roof at radius r, tank-local."""
    return H + 0.08 + 0.47 * (1 - r / (R + 0.05))


def _rail(bm, pts, h=1.0, mid=True, w=0.045):
    """Handrail along a polyline of deck points: posts at each point, a top rail, a mid rail."""
    up = Vector((0, 0, h))
    for p in pts:
        fp.strut(bm, p, p + up, w)
    for p, q in zip(pts, pts[1:]):
        fp.strut(bm, p + up, q + up, w)
        if mid and fp.fine():
            fp.strut(bm, p + up / 2, q + up / 2, w * 0.8)


def _stair_and_walkway(k):
    st, dk = k["M_STEEL"], k["M_STEEL_DARK"]
    a0, a1 = STAIR
    r0, r1 = R + 0.12, R + 0.92
    zf = lambda a: 0.05 + (DECK_Z - 0.05) * (a - a0) / (a1 - a0)
    turn = lambda a: Matrix.Rotation(math.radians(a), 4, "Z")
    n = 18 if fp.fine() else 10
    for i in range(n):                                               # treads
        a = a0 + (a1 - a0) * (i + 0.5) / n
        box(dk, (r1 - r0 - 0.06, 0.26, 0.035), polar((r0 + r1) / 2, a, zf(a)), turn(a))
    marks = [a0 + (a1 - a0) * i / 6 for i in range(7)]
    for r in (r0, r1):                                               # stringers
        for p, q in zip(marks, marks[1:]):
            fp.strut(st, polar(r, p, zf(p) - 0.08), polar(r, q, zf(q) - 0.08), 0.07)
    for a in marks[1::2]:                                            # brackets back to the shell
        fp.strut(st, polar(R + 0.02, a, zf(a) - 0.12), polar(r0, a, zf(a) - 0.12), 0.06)
    _rail(st, [polar(r1, a, zf(a)) for a in marks[::2]] + [polar(r1, a1 + 12, DECK_Z)])
    lm = a1 + 6                                                      # landing at the rim
    box(dk, (r1 - r0 + 0.1, 0.95, 0.05), polar((r0 + r1) / 2, lm, DECK_Z - 0.025), turn(lm))
    fp.strut(st, polar(r1, lm, DECK_Z - 0.05), polar(r1, lm, 0.1), 0.08)             # landing leg
    # walkway from the landing across the roof to the center vent
    rin, rout = 0.75, R + 0.12
    box(dk, (rout - rin, 0.76, 0.045), polar((rin + rout) / 2, WALK, DECK_Z - 0.02), turn(WALK))
    t = polar(1, WALK + 90)
    for side in (-0.4, 0.4):
        if fp.fine():
            _rail(st, [polar(r, WALK, DECK_Z) + t * side for r in (rin, (rin + rout) / 2, rout - 0.3)])
        for r in (1.3, 2.7):                                         # deck legs down to the roof
            p = polar(r, WALK) + t * side * 0.8
            fp.strut(st, (p.x, p.y, roof_z(r)), (p.x, p.y, DECK_Z - 0.04), 0.06)


def _hatch(k, r, deg):
    """Thief hatch: flanged neck, hinged lid, latch."""
    p = polar(r, deg)
    z = roof_z(r)
    cyl(k["M_STEEL_DARK"], 0.21, 0.05, (p.x, p.y, z + 0.03), segs=12)
    cyl(k["M_STEEL_DARK"], 0.15, 0.2, (p.x, p.y, z + 0.14), segs=12, caps=False)
    cyl(k["M_STEEL"], 0.2, 0.05, (p.x, p.y, z + 0.265), segs=12)
    box(k["M_STEEL_DARK"], (0.1, 0.08, 0.08), Vector((p.x, p.y, z + 0.25)) + polar(0.2, deg))
    fp.strut(k["M_STEEL"], Vector((p.x, p.y, z + 0.3)) - polar(0.15, deg), Vector((p.x, p.y, z + 0.33)) - polar(0.3, deg),
             0.03)


def _pv_vent(k, r, deg):
    """Pressure-vacuum vent on a flame arrestor, with its weather hood."""
    p = polar(r, deg)
    z = roof_z(r)
    st, dk = k["M_STEEL"], k["M_STEEL_DARK"]
    cyl(k["M_PIPE"], 0.08, 0.3, (p.x, p.y, z + 0.15), segs=10, caps=False)
    for dz in (0.31, 0.57):
        cyl(st, 0.17, 0.035, (p.x, p.y, z + dz), segs=12)                 # arrestor flanges
    cyl(dk, 0.14, 0.24, (p.x, p.y, z + 0.44), segs=12, caps=False)        # arrestor element housing
    fp.rbox(dk, (0.32, 0.26, 0.22), (p.x, p.y, z + 0.7), bevel=0.02)       # PV valve body
    cyl(st, 0.22, 0.08, (p.x, p.y, z + 0.85), segs=12, r2=0.12)          # weather hood


def tank_extras(site, tid, root, number, capacity=None, outlets=(fp.OUTLET[0],), xo_angles=(), level=0.55):
    """Stair and roof walkway, thief hatches, PV vent on a flame arrestor, equalizer line,
    float gauge, level radar, high-level switch, flanges, drain, labels.

    number is the painted tank number (from the ID), capacity the barrels on the shell.
    """
    k = Kit(site, f"{tid}_detail", root)
    steel, dark, pipe = k["M_STEEL"], k["M_STEEL_DARK"], k["M_PIPE"]
    _stair_and_walkway(k)
    _hatch(k, 1.75, WALK + 16)
    if fp.fine():
        _hatch(k, 2.7, WALK - 12)
    _pv_vent(k, 1.55, WALK - 30)
    # equalizer line: a top nozzle down the shell to a valved, blinded tie-in at grade
    eq = 178.0
    line(pipe, [polar(R - 0.05, eq, H - 0.45), polar(R + 0.35, eq, H - 0.45), polar(R + 0.35, eq, 0.55),
                polar(R + 0.95, eq, 0.55)], 0.07, segs=8)
    flange(steel, polar(R + 0.15, eq, H - 0.45), polar(1, eq), 0.14, segs=10)
    box(dark, (0.2, 0.2, 0.22), polar(R + 0.7, eq, 0.55), Matrix.Rotation(math.radians(eq), 4, "Z"))
    flange(dark, polar(R + 0.97, eq, 0.55), polar(1, eq), 0.13, thick=0.04, segs=10)   # blind
    if fp.fine():
        fp.strut(steel, polar(R + 0.7, eq, 0.66), polar(R + 0.7, eq, 0.9), 0.025)
        fp.handwheel(steel, 0.12, polar(R + 0.7, eq, 0.9))
        for z in (1.6, 3.2):
            fp.strut(steel, polar(R + 0.02, eq, z), polar(R + 0.35, eq, z), 0.05)      # line clamps
    # float gauge board: the marker is a separate moving part, below
    rot = Matrix.Rotation(math.radians(GAUGE_ANGLE), 4, "Z")
    g = polar(R + 0.07, GAUGE_ANGLE)
    box(dark, (0.04, 0.16, H - 0.4), (g.x, g.y, (H - 0.4) / 2 + 0.2), rot)
    for i in range(10):
        t = polar(R + 0.1, GAUGE_ANGLE, GAUGE_Z0 + GAUGE_SPAN * i / 9)
        box(steel, (0.02, 0.13 if i % 3 == 0 else 0.07, 0.02), t, rot)
    top = polar(R + 0.12, GAUGE_ANGLE, H + 0.12)
    ball(steel, 0.08, top)
    tube(steel, top, polar(R - 0.3, GAUGE_ANGLE, H + 0.2), 0.008, 4)
    # flanges on every nozzle, and a load-out nozzle with its valve
    nozzles = [(a, fp.OUTLET[1]) for a in outlets] + [fp.INLET, (LOADOUT_ANGLE, 0.85)]
    nozzles += [(a, 0.85) for a in xo_angles]
    for deg, zz in nozzles:
        d = polar(1, deg)
        flange(steel, polar(R + 0.15, deg, zz), d, 0.19, segs=12 if fp.fine() else 8)
        flange(steel, polar(fp.NOZZLE_R - 0.03, deg, zz), d, 0.19, segs=12 if fp.fine() else 8)
    tube(pipe, polar(R - 0.05, LOADOUT_ANGLE, 0.85), polar(fp.NOZZLE_R, LOADOUT_ANGLE, 0.85), 0.1, 12)
    vb = polar(R + 0.3, LOADOUT_ANGLE, 0.85)
    box(dark, (0.22, 0.22, 0.24), vb, Matrix.Rotation(math.radians(LOADOUT_ANGLE), 4, "Z"))
    fp.strut(dark, (vb.x, vb.y, 0.97), (vb.x, vb.y, 1.12), 0.025)
    fp.handwheel(steel, 0.11, (vb.x, vb.y, 1.13))
    # drain valve, sensor conduit down the shell, junction box, grounding strap
    dv = polar(R + 0.25, 30, 0.3)
    tube(pipe, polar(R - 0.05, 30, 0.3), dv, 0.06, 8)
    box(dark, (0.16, 0.16, 0.16), dv)
    line(dark, [polar(R + 0.12, 165, H - 0.75), polar(R + 0.12, 165, 0.6), polar(R + 0.55, 165, 0.6)], 0.02)
    box(dark, (0.3, 0.2, 0.3), polar(R + 0.7, 165, 0.6), Matrix.Rotation(math.radians(165), 4, "Z"))
    tube(steel, polar(R + 0.02, 120, 0.2), polar(R + 0.3, 120, 0.05), 0.012, 4)
    k.done()
    marker = bmesh.new()
    box(marker, (0.06, 0.24, 0.12), (0, 0, 0), rot)
    m = polar(R + 0.14, GAUGE_ANGLE)
    site.mesh(f"{tid}_gauge", marker, "M_LAMP", root, loc=(m.x, m.y, GAUGE_Z0 + GAUGE_SPAN * level))
    # guided-wave radar level transmitter on a roof nozzle: flange, neck, housing, cable gland
    lt = bmesh.new()
    p = polar(2.2, 150)
    zr = roof_z(2.2)
    cyl(lt, 0.14, 0.05, (p.x, p.y, zr + 0.03), segs=12)
    cyl(lt, 0.055, 0.22, (p.x, p.y, zr + 0.16), segs=10, caps=False)
    cyl(lt, 0.12, 0.2, (p.x, p.y, zr + 0.37), segs=12)
    cyl(lt, 0.1, 0.06, (p.x, p.y, zr + 0.5), segs=12, r2=0.07)
    fp.strut(lt, (p.x, p.y, zr + 0.37), polar(2.4, 150, zr + 0.37), 0.04)                # cable gland
    site.mesh(f"{tid}_lt", lt, "M_STEEL_DARK", root)
    sw = bmesh.new()
    tube(sw, polar(R, 160, H - 0.6), polar(R + 0.25, 160, H - 0.6), 0.05, 8)
    box(sw, (0.18, 0.18, 0.22), polar(R + 0.34, 160, H - 0.6))
    site.mesh(f"{tid}_lshh", sw, "M_STEEL_DARK", root)
    text = number.replace("_", " ")
    size = min(1.7, 2.4 / (0.6 * len(text)))
    label(site, f"{tid}_label", text, size, polar(R + 0.05, LABEL_ANGLE, 3.0), facing(LABEL_ANGLE), root,
          small=False)
    if capacity:
        label(site, f"{tid}_label_cap", f"{capacity:g} BBL", 0.32, polar(R + 0.1, LABEL_ANGLE, 1.95),
              facing(LABEL_ANGLE), root)
    return root.matrix_basis @ polar(fp.NOZZLE_R, LOADOUT_ANGLE, 0.85)


def pump_extras(site, pid, root):
    """Suction strainer, discharge check valve, gauges, coupling guard, motor fins, anchor bolts,
    bollards, and the control panel with its status light."""
    k = Kit(site, f"{pid}_detail", root)
    steel, dark = k["M_STEEL"], k["M_STEEL_DARK"]
    if fp.fine():
        for x in (-1.55, -0.5, 0.6, 1.6):
            for y in (-0.5, 0.5):
                cyl(dark, 0.025, 0.08, (x, y, 0.42), segs=6)
    # basket strainer on the suction, with its cover, drain and a compound gauge
    flange(steel, (-1.72, 0, 0.85), (1, 0, 0), 0.21)
    bolts(steel, (-1.72, 0, 0.85), "X", 0.17)
    cyl(dark, 0.18, 0.58, (-1.36, 0, 0.8), segs=14)
    cyl(steel, 0.21, 0.05, (-1.36, 0, 1.11), segs=14)
    cyl(dark, 0.06, 0.08, (-1.36, 0, 1.17), segs=6)
    for x in (-1.6, -1.12):
        flange(steel, (x, 0, 0.85), (1, 0, 0), 0.19, thick=0.04)
    fp.strut(steel, (-1.36, 0, 0.51), (-1.36, -0.22, 0.48), 0.03)
    if fp.fine():
        fp.strut(steel, (-1.15, -0.1, 0.85), (-1.15, -0.1, 1.05), 0.03)
        gauge(k, (-1.15, -0.1, 1.14), r=0.055)
    # swing check valve on the discharge riser, then the discharge flange and its gauge
    fp.rbox(dark, (0.26, 0.24, 0.26), (-0.8, 0, 1.52), bevel=0.03)
    fp.rbox(steel, (0.18, 0.06, 0.18), (-0.8, -0.14, 1.52), bevel=0.015)       # bonnet cover
    for z in (1.37, 1.67):
        flange(steel, (-0.8, 0, z), (0, 0, 1), 0.17, thick=0.04)
    flange(steel, (-0.8, 0, 1.88), (0, 0, 1), 0.18)
    bolts(steel, (-0.8, 0, 1.88), "Z", 0.15)
    fp.strut(steel, (-0.8, -0.1, 1.76), (-0.8, -0.26, 1.76), 0.03)
    gauge(k, (-0.8, -0.27, 1.84))
    # coupling guard: a rounded shroud over the shaft between bearing frame and motor
    shroud = fp.hull(fp.circle_pts((0, 0.85), 0.21, 10, 0, PI) + [(-0.24, 0.42), (0.24, 0.42)])
    fp.prism(steel, shroud, 0.04, 0.6, "YZ")
    if fp.fine():
        for x in (0.1, 0.32, 0.54):
            box(dark, (0.02, 0.5, 0.02), (x, 0, 1.07))
    # motor: cooling fins, lifting eye
    for i in range(10 if fp.fine() else 6):
        a = 2 * PI * (i + 0.5) / (10 if fp.fine() else 6)
        if math.sin(a) < 0.9:
            box(steel, (0.85, 0.06, 0.025), (1.1, 0.37 * math.cos(a), 0.82 + 0.37 * math.sin(a)),
                Matrix.Rotation(a, 4, "X"))
    ring(steel, 0.06, 0.012, (1.1, 0, 1.22), axis="Y", segs=8, rsegs=3)
    for x, y in ((-2.0, -1.05), (2.0, -1.05), (-2.0, 1.05), (2.0, 1.05)):
        cyl(steel, 0.1, 1.0, (x, y, 0.5), segs=10 if fp.fine() else 6)
        if fp.fine():
            ball(steel, 0.1, (x, y, 1.0), segs=8)
    for x in (0.0, 0.8):
        fp.strut(steel, (x, 1.6, 0.0), (x, 1.6, 1.9), 0.07)
    fp.rbox(steel, (1.0, 0.3, 0.9), (0.4, 1.6, 1.35), bevel=0.02)
    box(dark, (1.1, 0.4, 0.06), (0.4, 1.6, 1.83))
    box(dark, (0.08, 0.05, 0.08), (0.75, 1.43, 1.35))
    box(dark, (0.12, 0.04, 0.12), (0.2, 1.44, 1.5))
    line(dark, [(1.1, 0.5, 0.95), (1.1, 1.2, 0.95), (0.4, 1.2, 0.95), (0.4, 1.45, 0.95)], 0.025, segs=6)
    k.done()
    status_light(site, f"{pid}_light", root, (0.1, 1.6, 1.95))
    label(site, f"{pid}_tag", id_text(pid), 0.11, (0.42, 1.44, 1.62), (PI / 2, 0, 0), root, "M_STEEL_DARK")


def valve_extras(site, vid, root, tag):
    """Flange bolts, a stem guard on manual valves, and the tag plate on the yoke."""
    k = Kit(site, f"{vid}_detail", root)
    for y in (-0.235, 0.235):
        bolts(k["M_STEEL"], (0, y, 0), "Y", 0.17)
    bolts(k["M_STEEL"], (0, 0, 0.26), "Z", 0.135, n=6)
    fp.strut(k["M_STEEL"], (0.075, 0, 0.66), (0.17, 0, 0.6), 0.008)
    box(k["M_STEEL"], (0.2, 0.01, 0.09), (0.2, 0, 0.56))
    k.done()
    label(site, f"{vid}_tag", tag, 0.05, (0.2, -0.008, 0.56), (PI / 2, 0, 0), root)


def gauge(k, at, facing="-Y", r=0.065):
    """A dial pressure gauge on a short stub: case and glass. facing is -Y or -X."""
    at = Vector(at)
    axis, d = ("Y", Vector((0, -1, 0))) if facing == "-Y" else ("X", Vector((-1, 0, 0)))
    cyl(k["M_STEEL_DARK"], r, 0.045, at, axis=axis, segs=10)
    cyl(k["M_GLASS"], r * 0.85, 0.01, at + d * 0.024, axis=axis, segs=10, caps=False)
    fp.strut(k["M_STEEL"], at - Vector((0, 0, r)), at - Vector((0, 0, r + 0.08)), 0.022)


def well_extras(site, wid, root):
    """Wellhead trim (master, wing and casing valves, check valve, gauge), crank guard,
    pump-off controller with its status light, and the well sign."""
    import well_parts as wp
    k = Kit(site, f"{wid}_detail", root)
    st, dk = k["M_STEEL"], k["M_STEEL_DARK"]
    fp.strut(st, (1.8, 3.0, 0.12), (1.8, 3.0, 1.9), 0.08)
    fp.rbox(st, (0.5, 0.25, 0.6), (1.8, 2.82, 1.45), bevel=0.02)
    if not fp.LOD["trim"]:   # keep only the controller that carries the status light
        k.done()
        status_light(site, f"{wid}_light", root, (1.95, 2.82, 1.88), r=0.045)
        return
    y, tz = wp.WELL_Y, wp.TEE_Z
    box(dk, (0.17, 0.2, 0.2), (0.3, y, tz))                                  # wing valve on the tie-in
    fp.strut(st, (-2.0, -5.0, 0.12), (-2.0, -5.0, 1.62), 0.07)               # well sign
    box(st, (0.8, 0.04, 0.36), (-2.0, -5.0, 1.45))
    if not fp.fine():
        k.done()
        status_light(site, f"{wid}_light", root, (1.95, 2.82, 1.88), r=0.045)
        return
    fp.strut(st, (-0.22, y, tz), (-0.22, y, tz + 0.2), 0.035)                # tubing pressure gauge
    gauge(k, (-0.22, y, tz + 0.29))
    gx, ys = 1.45, (1.75, 3.4, 5.05)                                         # crank guard
    for sx in (-gx, gx):
        for gy in ys:
            fp.strut(st, (sx, gy, 0.12), (sx, gy, 1.45), 0.05)
        fp.strut(st, (sx, ys[0], 1.4), (sx, ys[-1], 1.4), 0.04)
    fp.strut(st, (-gx, ys[-1], 1.4), (gx, ys[-1], 1.4), 0.04)
    fp.strut(st, (0, y - 0.1, 0.51), (0, y - 0.3, 0.51), 0.035)              # master valve stem and wheel
    placed(st, Matrix.Translation((0, y - 0.3, 0.51)) @ Matrix.Rotation(PI / 2, 4, "X"), fp.handwheel, 0.13)
    fp.strut(st, (0.3, y, tz + 0.1), (0.3, y, tz + 0.34), 0.025)
    fp.handwheel(st, 0.1, (0.3, y, tz + 0.34))
    cyl(dk, 0.085, 0.13, (0.47, y, tz), axis="X", segs=10)                  # check valve
    box(dk, (0.1, 0.12, 0.07), (0.47, y, tz + 0.1))
    flange(st, (0.54, y, tz), (1, 0, 0), 0.1, thick=0.035, segs=8)
    cyl(st, 0.04, 0.45, (-0.225, y, 0.18), axis="X", segs=8)                 # casing valve
    box(dk, (0.14, 0.16, 0.16), (-0.32, y, 0.18))
    fp.strut(st, (-0.32, y, 0.26), (-0.32, y, 0.38), 0.02)
    # crank guard: posts and rails around the counterweights' sweep and the belt drive
    for sx in (-gx, gx):
        fp.strut(st, (sx, ys[0], 0.8), (sx, ys[-1], 0.8), 0.04)
    fp.strut(st, (-gx, ys[-1], 0.8), (gx, ys[-1], 0.8), 0.04)
    for gz in (0.8, 1.4):
        fp.strut(st, (-gx, ys[0], gz), (-0.95, ys[0], gz), 0.04)
        fp.strut(st, (0.95, ys[0], gz), (gx, ys[0], gz), 0.04)
    # pump-off controller: rain hood, door seam, disconnect, conduit underground to the motor
    box(dk, (0.6, 0.35, 0.05), (1.8, 2.82, 1.78))
    box(dk, (0.01, 0.01, 0.5), (1.8, 2.69, 1.45))
    box(dk, (0.3, 0.2, 0.25), (1.8, 2.86, 0.95))
    for a, b in (((1.8, 2.95, 0.85), (1.8, 2.95, 0.12)), ((1.8, 2.95, 0.14), (1.8, 4.97, 0.14)),
                 ((1.8, 4.95, 0.14), (0.05, 4.95, 0.14))):
        fp.strut(dk, a, b, 0.045)
    k.done()
    status_light(site, f"{wid}_light", root, (1.95, 2.82, 1.88), r=0.045)
    label(site, f"{wid}_sign", id_text(wid), 0.12, (-2.0, -5.025, 1.45), (PI / 2, 0, 0), root)


def build_loadout(site, lid, loc, rot_z=0.0):
    """Truck load-out stand: riser, valve, quick-connect, drip bucket, hose rack."""
    root = site.empty(lid, loc, rot_z, size=0.8)
    root["kind"] = "loadout"
    k = Kit(site, lid, root)
    box(k["M_CONCRETE"], (1.8, 1.4, 0.15), (0, 0, 0.075))
    line(k["M_PIPE"], [(0, 0, 1.6), (0, 0, 0.95), (-0.6, 0, 0.95)], 0.09)
    box(k["M_STEEL_DARK"], (0.24, 0.22, 0.26), (-0.3, 0, 0.95))
    tube(k["M_STEEL_DARK"], (-0.3, 0, 1.08), (-0.3, 0, 1.22), 0.015, 4)
    ring(k["M_STEEL"], 0.1, 0.012, (-0.3, 0, 1.23), segs=12, rsegs=4)
    cyl(k["M_STEEL_DARK"], 0.12, 0.08, (-0.64, 0, 0.95), axis="X", segs=12)
    box(k["M_STEEL_DARK"], (0.45, 0.45, 0.3), (-0.7, 0, 0.3))
    for y in (0.35, 0.55):
        tube(k["M_STEEL"], (0.15, y, 0.15), (0.15, y, 1.4), 0.03, 6)
    ring(k["M_RUBBER"], 0.3, 0.05, (0.2, 0.45, 1.05), axis="X", segs=16, rsegs=6)
    k.done()
    return root
