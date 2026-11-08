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
    site.col.objects.link(tmp)
    bpy.context.view_layer.update()
    me = bpy.data.meshes.new_from_object(tmp.evaluated_get(bpy.context.evaluated_depsgraph_get()))
    bpy.data.objects.remove(tmp, do_unlink=True)
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


def tank_extras(site, tid, root, number, capacity=None, outlets=(fp.OUTLET[0],), xo_angles=(), level=0.55):
    """Caged ladder, float gauge, level radar, high-level switch, vents, flanges, labels.

    number is the painted tank number (from the ID), capacity the barrels on the shell.
    """
    k = Kit(site, f"{tid}_detail", root)
    steel, dark, pipe = k["M_STEEL"], k["M_STEEL_DARK"], k["M_PIPE"]
    lad = fp.LADDER
    base = polar(R + 0.22, lad)
    turn = Matrix.Translation(base) @ Matrix.Rotation(math.radians(lad), 4, "Z")
    z = 2.3
    while z < H + 0.9:
        placed(steel, turn, fp.arc_wall, 0.42, 0.39, z, z + 0.05, -PI / 2, PI / 2, 10)
        z += 0.75
    for deg in (-60, 0, 60):
        p = base + polar(0.405, lad + deg)
        tube(steel, (p.x, p.y, 2.3), (p.x, p.y, H + 0.95), 0.015, 5)
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
    # pressure-vacuum vent with flame arrestor, and a gauge hatch
    v = polar(1.6, 40)
    cyl(pipe, 0.12, 0.4, (v.x, v.y, H + 0.5), segs=12)
    box(dark, (0.32, 0.32, 0.26), (v.x, v.y, H + 0.8))
    cyl(dark, 0.24, 0.06, (v.x, v.y, H + 0.97), segs=14)
    gh = polar(2.4, 125)
    cyl(dark, 0.16, 0.12, (gh.x, gh.y, H + 0.25), segs=12)
    box(steel, (0.06, 0.2, 0.05), (gh.x + 0.17, gh.y, H + 0.33))
    # flanges on every nozzle, and a load-out nozzle with its valve
    nozzles = [(a, fp.OUTLET[1]) for a in outlets] + [fp.INLET, (LOADOUT_ANGLE, 0.85)]
    nozzles += [(a, 0.85) for a in xo_angles]
    for deg, zz in nozzles:
        d = polar(1, deg)
        flange(steel, polar(R + 0.15, deg, zz), d, 0.19)
        flange(steel, polar(fp.NOZZLE_R - 0.03, deg, zz), d, 0.19)
    tube(pipe, polar(R - 0.05, LOADOUT_ANGLE, 0.85), polar(fp.NOZZLE_R, LOADOUT_ANGLE, 0.85), 0.1, 12)
    vb = polar(R + 0.3, LOADOUT_ANGLE, 0.85)
    box(dark, (0.22, 0.22, 0.24), vb, Matrix.Rotation(math.radians(LOADOUT_ANGLE), 4, "Z"))
    ring(steel, 0.11, 0.012, (vb.x, vb.y, 1.12), segs=12, rsegs=4)
    tube(dark, (vb.x, vb.y, 0.97), (vb.x, vb.y, 1.12), 0.015, 4)
    # drain valve, sensor conduit down the shell, junction box
    dv = polar(R + 0.25, 30, 0.3)
    tube(pipe, polar(R - 0.05, 30, 0.3), dv, 0.06, 8)
    box(dark, (0.16, 0.16, 0.16), dv)
    line(dark, [polar(R + 0.12, 165, H - 0.75), polar(R + 0.12, 165, 0.6), polar(R + 0.55, 165, 0.6)], 0.02)
    box(dark, (0.3, 0.2, 0.3), polar(R + 0.7, 165, 0.6), Matrix.Rotation(math.radians(165), 4, "Z"))
    tube(steel, polar(R + 0.02, 120, 0.2), polar(R + 0.3, 120, 0.05), 0.012, 4)   # grounding strap
    k.done()
    marker = bmesh.new()
    box(marker, (0.06, 0.24, 0.12), (0, 0, 0), rot)
    m = polar(R + 0.14, GAUGE_ANGLE)
    site.mesh(f"{tid}_gauge", marker, "M_LAMP", root, loc=(m.x, m.y, GAUGE_Z0 + GAUGE_SPAN * level))
    lt = bmesh.new()
    p = polar(2.2, 150)
    zr = H + 0.08 + 0.47 * (1 - 2.2 / (R + 0.05))
    cyl(lt, 0.13, 0.05, (p.x, p.y, zr + 0.02), segs=14)
    cyl(lt, 0.06, 0.3, (p.x, p.y, zr + 0.2), segs=10)
    box(lt, (0.2, 0.16, 0.16), (p.x, p.y, zr + 0.42))
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
    """Anchor bolts, flanges, check valve, motor fins, gauges, bollards, control panel and light."""
    k = Kit(site, f"{pid}_detail", root)
    steel, dark = k["M_STEEL"], k["M_STEEL_DARK"]
    for x in (-1.55, -0.5, 0.6, 1.6):
        for y in (-0.5, 0.5):
            cyl(dark, 0.025, 0.08, (x, y, 0.42), segs=6)
    flange(steel, (-1.72, 0, 0.85), (1, 0, 0), 0.21)
    bolts(steel, (-1.72, 0, 0.85), "X", 0.17)
    flange(steel, (-0.8, 0, 1.88), (0, 0, 1), 0.18)
    bolts(steel, (-0.8, 0, 1.88), "Z", 0.15)
    cyl(dark, 0.16, 0.12, (-0.8, 0, 1.45), segs=14)
    for i in range(14):
        a = 2 * PI * i / 14
        box(steel, (0.85, 0.06, 0.025), (1.1, 0.37 * math.cos(a), 0.82 + 0.37 * math.sin(a)),
            Matrix.Rotation(a, 4, "X"))
    ring(steel, 0.06, 0.012, (1.1, 0, 1.22), axis="Y", segs=10, rsegs=4)
    tube(dark, (-1.3, 0, 0.97), (-1.3, 0, 1.12), 0.015, 6)
    cyl(steel, 0.08, 0.04, (-1.3, 0.02, 1.16), axis="Y", segs=12)
    for x, y in ((-2.0, -1.05), (2.0, -1.05), (-2.0, 1.05), (2.0, 1.05)):
        cyl(steel, 0.1, 1.0, (x, y, 0.5), segs=10)
        ball(steel, 0.1, (x, y, 1.0), segs=8)
    for x in (0.0, 0.8):
        tube(steel, (x, 1.6, 0.0), (x, 1.6, 1.9), 0.04, 6)
    box(steel, (1.0, 0.3, 0.9), (0.4, 1.6, 1.35))
    box(dark, (1.1, 0.4, 0.06), (0.4, 1.6, 1.83))
    box(dark, (0.08, 0.05, 0.08), (0.75, 1.43, 1.35))
    box(dark, (0.12, 0.04, 0.12), (0.2, 1.44, 1.5))
    line(dark, [(1.1, 0.5, 0.95), (1.1, 1.2, 0.95), (0.4, 1.2, 0.95), (0.4, 1.45, 0.95)], 0.025)
    k.done()
    status_light(site, f"{pid}_light", root, (0.1, 1.6, 1.95))
    label(site, f"{pid}_tag", id_text(pid), 0.11, (0.42, 1.44, 1.62), (PI / 2, 0, 0), root, "M_STEEL_DARK")


def valve_extras(site, vid, root, tag):
    k = Kit(site, f"{vid}_detail", root)
    for y in (-0.24, 0.24):
        bolts(k["M_STEEL"], (0, y, 0), "Y", 0.23)
    box(k["M_STEEL_DARK"], (0.24, 0.24, 0.04), (0, 0, 0.53))
    tube(k["M_STEEL"], (0.1, 0, 0.72), (0.18, 0, 0.62), 0.006, 4)
    box(k["M_STEEL"], (0.2, 0.01, 0.09), (0.2, 0, 0.56))
    k.done()
    label(site, f"{vid}_tag", tag, 0.05, (0.2, -0.008, 0.56), (PI / 2, 0, 0), root)


def well_extras(site, wid, root):
    """Wellhead tree, wing valve and gauge, crank guard, brake, controller with light, sign."""
    k = Kit(site, f"{wid}_detail", root)
    st, dk = k["M_STEEL"], k["M_STEEL_DARK"]
    if not fp.LOD["trim"]:   # keep only the controller that carries the status light
        tube(st, (1.8, 3.0, 0.12), (1.8, 3.0, 1.9), 0.05, 6)
        box(st, (0.5, 0.25, 0.6), (1.8, 2.82, 1.45))
        k.done()
        status_light(site, f"{wid}_light", root, (1.95, 2.82, 1.88), r=0.045)
        return
    y = -3.45
    cyl(dk, 0.32, 0.3, (0, y, 0.3), segs=14)
    cyl(dk, 0.26, 0.15, (0, y, 0.52), segs=14)
    box(dk, (0.3, 0.3, 0.22), (0, y, 0.7))
    ring(st, 0.12, 0.015, (0.24, y, 0.7), axis="X", segs=14, rsegs=4)
    box(dk, (0.18, 0.2, 0.2), (0.38, y, 0.85))
    tube(dk, (0.38, y, 0.95), (0.38, y, 1.01), 0.012, 4)
    ring(st, 0.09, 0.012, (0.38, y, 1.02), segs=12, rsegs=4)
    tube(dk, (0.15, y, 0.92), (0.15, y, 1.05), 0.012, 4)
    cyl(st, 0.06, 0.03, (0.15, y, 1.08), axis="Y", segs=10)
    for gx in (-1.3, 1.3):
        for gy in (1.8, 3.2, 4.6):
            tube(st, (gx, gy, 0.12), (gx, gy, 1.4), 0.03, 4)
        for gz in (0.8, 1.35):
            tube(st, (gx, 1.8, gz), (gx, 4.6, gz), 0.022, 4)
    for gz in (0.8, 1.35):
        tube(st, (-1.3, 4.6, gz), (1.3, 4.6, gz), 0.022, 4)
    tube(st, (0.55, 2.0, 0.5), (0.7, 1.7, 1.6), 0.03, 6)
    ball(dk, 0.05, (0.7, 1.7, 1.62), segs=8)
    tube(st, (1.8, 3.0, 0.12), (1.8, 3.0, 1.9), 0.05, 6)
    box(st, (0.5, 0.25, 0.6), (1.8, 2.82, 1.45))
    box(dk, (0.6, 0.35, 0.05), (1.8, 2.82, 1.78))
    line(dk, [(1.8, 2.95, 1.2), (1.8, 3.9, 1.2), (0.36, 3.9, 1.0)], 0.02)
    tube(st, (-2.0, -5.0, 0.12), (-2.0, -5.0, 1.62), 0.04, 6)
    box(st, (0.8, 0.04, 0.36), (-2.0, -5.0, 1.45))
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
