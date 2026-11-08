"""Materials, mesh helpers, and asset builders for the Freeboard field.

Units are meters. Blender is Z-up; the glTF export turns Blender Z into web Y.
Every asset is an Empty named with its contract ID. Static parts are merged into
one child mesh per material. Moving parts are separate children whose origin is
their pivot, so the web console can animate them with a single rotation or scale.
"""
import math

import bmesh
import bpy
from mathutils import Matrix, Vector

PI = math.pi
SHARP = math.radians(35)

# Small detail the builder can drop to stay inside the triangle budget (see build_field.py).
LOD = {"bolts": True, "small_labels": True, "posts": True, "trim": True}

# name: (base color, metallic, roughness). Neutral on purpose: the web console adds status color.
PALETTE = {
    "M_TANK": ((0.70, 0.70, 0.68), 0.10, 0.55),
    "M_STEEL": ((0.56, 0.57, 0.58), 0.35, 0.45),
    "M_STEEL_DARK": ((0.20, 0.21, 0.22), 0.30, 0.55),
    "M_PIPE": ((0.44, 0.45, 0.46), 0.30, 0.45),
    "M_CONCRETE": ((0.55, 0.54, 0.51), 0.0, 0.85),
    "M_GRAVEL": ((0.47, 0.44, 0.40), 0.0, 0.95),
    "M_GROUND": ((0.60, 0.53, 0.42), 0.0, 1.0),
    "M_ROAD": ((0.34, 0.32, 0.29), 0.0, 0.95),
    "M_BERM": ((0.52, 0.47, 0.39), 0.0, 1.0),
    "M_BRINE": ((0.16, 0.26, 0.29), 0.0, 0.2),
    "M_RUBBER": ((0.04, 0.04, 0.04), 0.0, 0.8),
    "M_GLASS": ((0.08, 0.11, 0.13), 0.0, 0.1),
    "M_LAMP": ((0.86, 0.86, 0.82), 0.0, 0.4),     # status lights and lamps: the console lights these
    "M_WOOD": ((0.32, 0.26, 0.20), 0.0, 0.9),
    "M_WIRE": ((0.10, 0.10, 0.10), 0.5, 0.5),
}

AXES = {
    "Z": Matrix.Identity(4),
    "X": Matrix.Rotation(PI / 2, 4, "Y"),
    "Y": Matrix.Rotation(PI / 2, 4, "X"),
}


def material(name):
    mat = bpy.data.materials.get(name)
    if mat:
        return mat
    color, metallic, roughness = PALETTE[name]
    mat = bpy.data.materials.new(name)
    if mat.node_tree is None:
        mat.use_nodes = True
    bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = roughness
    mat.diffuse_color = (*color, 1.0)
    mat.metallic = metallic
    mat.roughness = roughness
    return mat


def finish(bm, recalc=True):
    """Outward normals, smooth curved faces, sharp creases (stands in for auto smooth)."""
    if recalc:
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    for f in bm.faces:
        f.smooth = True
    for e in bm.edges:
        if not e.is_manifold or e.calc_face_angle(PI) > SHARP:
            e.smooth = False


class Site:
    """Creates objects in one collection."""

    def __init__(self, collection):
        self.col = collection

    def empty(self, name, loc=(0, 0, 0), rot_z=0.0, parent=None, kind=None, size=1.0):
        obj = bpy.data.objects.new(name, None)
        obj.empty_display_type = "PLAIN_AXES"
        obj.empty_display_size = size
        obj.location = loc
        obj.rotation_euler = (0.0, 0.0, rot_z)
        if parent:
            obj.parent = parent
        if kind:
            obj["asset_id"] = name
            obj["kind"] = kind
        self.col.objects.link(obj)
        return obj

    def mesh(self, name, bm, mat, parent=None, loc=(0, 0, 0), recalc=True):
        finish(bm, recalc)
        me = bpy.data.meshes.new(name)
        bm.to_mesh(me)
        bm.free()
        me.materials.append(material(mat))
        obj = bpy.data.objects.new(name, me)
        obj.location = loc
        if parent:
            obj.parent = parent
        self.col.objects.link(obj)
        return obj

    def pipe(self, name, points, radius, segs=10):
        pts = []
        for p in points:
            if not pts or (Vector(p) - pts[-1]).length > 1e-3:
                pts.append(Vector(p))
        bm = bmesh.new()
        line(bm, pts, radius, segs)
        for end, nxt in ((pts[0], pts[1]), (pts[-1], pts[-2])):
            d = (nxt - end).normalized() * 0.03
            tube(bm, end - d, end + d, radius * 1.7, segs)
        obj = self.mesh(name, bm, "M_PIPE")
        obj["pipe_id"] = name
        return obj


class Kit:
    """Static parts of one asset, merged into one mesh per material."""

    def __init__(self, site, owner, parent):
        self.site, self.owner, self.parent, self.parts = site, owner, parent, {}

    def __getitem__(self, mat):
        return self.parts.setdefault(mat, bmesh.new())

    def done(self):
        for mat, bm in self.parts.items():
            if bm.faces:
                self.site.mesh(f"{self.owner}_{mat[2:].lower()}", bm, mat, self.parent)
            else:
                bm.free()


# ---------- primitives (all write into a bmesh) ----------

def box(bm, size, center, rot=None):
    rot = rot if rot is not None else Matrix.Identity(4)
    m = Matrix.Translation(Vector(center)) @ rot @ Matrix.Diagonal((*size, 1.0))
    bmesh.ops.create_cube(bm, size=1.0, matrix=m)


def cyl(bm, r, depth, center, axis="Z", segs=16, r2=None, caps=True):
    """A cylinder or cone frustum. caps=False leaves the ends open, for ends nobody can see."""
    m = Matrix.Translation(Vector(center)) @ AXES[axis]
    bmesh.ops.create_cone(bm, cap_ends=caps, cap_tris=False, segments=segs,
                          radius1=r, radius2=r if r2 is None else r2, depth=depth, matrix=m)


def ball(bm, r, center, segs=10):
    bmesh.ops.create_uvsphere(bm, u_segments=segs, v_segments=max(4, segs // 2), radius=r,
                              matrix=Matrix.Translation(Vector(center)))


def tube(bm, a, b, r, segs=10):
    a, b = Vector(a), Vector(b)
    d = b - a
    if d.length < 1e-4:
        return
    rot = Vector((0, 0, 1)).rotation_difference(d.normalized()).to_matrix().to_4x4()
    bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=segs, radius1=r, radius2=r,
                          depth=d.length, matrix=Matrix.Translation((a + b) / 2) @ rot)


def line(bm, points, r, segs=10):
    for p, q in zip(points, points[1:]):
        tube(bm, p, q, r, segs)
    for p in points[1:-1]:
        ball(bm, r * 1.12, p, segs=6 if r < 0.07 else 8)


def ring(bm, big_r, small_r, center, axis="Z", segs=24, rsegs=6):
    m = Matrix.Translation(Vector(center)) @ AXES[axis]
    grid = []
    for i in range(segs):
        a = 2 * PI * i / segs
        row = []
        for j in range(rsegs):
            b = 2 * PI * j / rsegs
            rr = big_r + small_r * math.cos(b)
            row.append(bm.verts.new(m @ Vector((rr * math.cos(a), rr * math.sin(a), small_r * math.sin(b)))))
        grid.append(row)
    for i in range(segs):
        for j in range(rsegs):
            bm.faces.new((grid[i][j], grid[(i + 1) % segs][j],
                          grid[(i + 1) % segs][(j + 1) % rsegs], grid[i][(j + 1) % rsegs]))


def arc_wall(bm, r_out, r_in, z0, z1, a0, a1, segs):
    """A cylinder wall from angle a0 to a1, with thickness. Used for the tank cutaway."""
    cols = []
    for i in range(segs + 1):
        a = a0 + (a1 - a0) * i / segs
        c, s = math.cos(a), math.sin(a)
        cols.append([bm.verts.new((r_out * c, r_out * s, z0)), bm.verts.new((r_out * c, r_out * s, z1)),
                     bm.verts.new((r_in * c, r_in * s, z1)), bm.verts.new((r_in * c, r_in * s, z0))])
    for i in range(segs):
        a, b = cols[i], cols[i + 1]
        for k in range(4):
            bm.faces.new((a[k], a[(k + 1) % 4], b[(k + 1) % 4], b[k]))
    bm.faces.new(cols[0])
    bm.faces.new(list(reversed(cols[-1])))


def arc_roof(bm, r, z0, z_rim, z_apex, a0, a1, segs):
    """A pie-slice cone roof from angle a0 to a1."""
    top = bm.verts.new((0, 0, z_apex))
    bot = bm.verts.new((0, 0, z0))
    rim = []
    for i in range(segs + 1):
        a = a0 + (a1 - a0) * i / segs
        c, s = math.cos(a), math.sin(a)
        rim.append((bm.verts.new((r * c, r * s, z0)), bm.verts.new((r * c, r * s, z_rim))))
    for i in range(segs):
        (b0, t0), (b1, t1) = rim[i], rim[i + 1]
        bm.faces.new((b0, b1, t1, t0))
        bm.faces.new((top, t0, t1))
        bm.faces.new((bot, b1, b0))
    bm.faces.new((bot, rim[0][0], rim[0][1], top))
    bm.faces.new((top, rim[-1][1], rim[-1][0], bot))


def polar(r, deg, z=0.0):
    a = math.radians(deg)
    return Vector((r * math.cos(a), r * math.sin(a), z))


def fine():
    """True at full detail: chamfers, rungs, and small hardware. Lite and bare skip them."""
    return LOD["bolts"]


def strut(bm, a, b, w, d=None):
    """A rectangular bar from a to b, w by d in section: structural tube, flat bar, or rail."""
    a, b = Vector(a), Vector(b)
    v = b - a
    if v.length < 1e-4:
        return
    rot = Vector((0, 0, 1)).rotation_difference(v.normalized()).to_matrix().to_4x4()
    box(bm, (w, d or w, v.length), (a + b) / 2, rot)


def rbox(bm, size, center, rot=None, bevel=0.02):
    """A box with chamfered edges, so it catches a highlight. A plain box below full detail."""
    before = set(bm.verts)
    box(bm, size, center, rot)
    if not fine() or bevel <= 0:
        return
    verts = [v for v in bm.verts if v not in before]
    edges = list({e for v in verts for e in v.link_edges})
    bmesh.ops.bevel(bm, geom=verts + edges, offset=min(bevel, min(size) * 0.3), segments=1,
                    affect="EDGES", clamp_overlap=True)


def prism(bm, pts, d0, d1, plane="YZ"):
    """Extrude a 2D outline [(u, v)] from depth d0 to d1. plane YZ: outline in (y, z), depth along x."""
    place = {"YZ": lambda u, v, w: (w, u, v), "XZ": lambda u, v, w: (u, w, v),
             "XY": lambda u, v, w: (u, v, w)}[plane]
    a = [bm.verts.new(place(u, v, d0)) for u, v in pts]
    b = [bm.verts.new(place(u, v, d1)) for u, v in pts]
    bm.faces.new(a)
    bm.faces.new(list(reversed(b)))
    for i in range(len(pts)):
        j = (i + 1) % len(pts)
        bm.faces.new((a[i], a[j], b[j], b[i]))


def hull(points):
    """2D convex hull (monotone chain), counterclockwise."""
    pts = sorted(set((round(x, 5), round(y, 5)) for x, y in points))

    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2 and ((out[-1][0] - out[-2][0]) * (p[1] - out[-2][1])
                                     - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(p)
        return out
    lower, upper = half(pts), half(reversed(pts))
    return lower[:-1] + upper[:-1]


def circle_pts(c, r, n, a0=0.0, a1=2 * PI):
    """n points on a circle (or arc a0..a1, inclusive) around c in 2D."""
    full = abs(a1 - a0 - 2 * PI) < 1e-6
    k = n if full else n - 1
    return [(c[0] + r * math.cos(a0 + (a1 - a0) * i / k), c[1] + r * math.sin(a0 + (a1 - a0) * i / k))
            for i in range(n)]


def handwheel(bm, r, center=(0, 0, 0), spokes=3):
    """A valve handwheel in the XY plane: rim, spokes, hub. Turns about local Z."""
    c = Vector(center)
    small = r < 0.15
    ring(bm, r, max(0.01, r * 0.1), c, segs=8 if small else min(16, round(r * 56)), rsegs=3 if small else 4)
    for i in range(2 if small else spokes):
        a = i * PI / (2 if small else spokes)
        strut(bm, c + Vector((r * math.cos(a), r * math.sin(a), 0)),
              c - Vector((r * math.cos(a), r * math.sin(a), 0)), r * 0.08, r * 0.06)
    cyl(bm, r * 0.2, r * 0.25, c, segs=6 if small else 8)


# ---------- assets ----------

TANK_R, TANK_H, WALL, FLOOR = 3.25, 4.9, 0.08, 0.15
CUT = (-130.0, -50.0)            # the opening faces -Y, toward the road
NOZZLE_R = TANK_R + 0.45
OUTLET = (-40.0, 0.85)           # angle, height: suction to the pump
INLET = (140.0, 4.3)             # angle, height: produced water in from the header


def build_tank(site, tid, loc, rot_z=0.0, outlets=(OUTLET[0],), xo_angles=()):
    """Cutaway tank. outlets are nozzle angles for its pumps, xo_angles for its crossovers.

    The stair, roof walkway, hatches and vents are in detail_equipment.tank_extras.
    """
    root = site.empty(tid, loc, rot_z, kind="tank", size=4)
    k = Kit(site, tid, root)
    a0, a1 = math.radians(CUT[1]), math.radians(CUT[0] + 360)
    arc_wall(k["M_TANK"], TANK_R, TANK_R - WALL, 0, TANK_H, a0, a1, 44)
    for z in (0.12, 1.6, 3.25):                                       # shell courses and the bottom chime
        arc_wall(k["M_TANK"], TANK_R + 0.03, TANK_R - 0.01, z, z + 0.07, a0, a1, 44)
    arc_wall(k["M_STEEL"], TANK_R + 0.06, TANK_R - 0.01, TANK_H - 0.1, TANK_H, a0, a1, 44)   # top angle
    if fine():                                                        # vertical seams between the shell sheets
        for deg in range(-35, 230, 30):
            box(k["M_TANK"], (0.05, 0.09, TANK_H - 0.3), polar(TANK_R + 0.02, deg, TANK_H / 2),
                Matrix.Rotation(math.radians(deg), 4, "Z"))
    arc_roof(k["M_TANK"], TANK_R + 0.05, TANK_H, TANK_H + 0.08, TANK_H + 0.55, a0, a1, 44)
    cyl(k["M_TANK"], TANK_R, FLOOR, (0, 0, FLOOR / 2), segs=44)
    cyl(k["M_CONCRETE"], TANK_R + 0.4, 0.16, (0, 0, -0.06), segs=44)
    nozzles = [(a, OUTLET[1]) for a in outlets] + [INLET] + [(a, 0.85) for a in xo_angles]
    for deg, z in nozzles:
        tube(k["M_PIPE"], polar(TANK_R - 0.05, deg, z), polar(NOZZLE_R, deg, z), 0.11, 12)
    tube(k["M_PIPE"], (0, 0, TANK_H + 0.4), (0, 0, TANK_H + 1.15), 0.07, 10)   # center vent, gooseneck
    ball(k["M_PIPE"], 0.1, (0, 0, TANK_H + 1.15), segs=8)
    tube(k["M_PIPE"], (0, 0, TANK_H + 1.15), (0.3, 0, TANK_H + 1.0), 0.07, 10)
    k.done()
    lvl = bmesh.new()
    depth = TANK_H - FLOOR - 0.15
    cyl(lvl, TANK_R - WALL - 0.03, depth, (0, 0, depth / 2), segs=44)
    level = site.mesh(f"{tid}_LEVEL", lvl, "M_BRINE", root, loc=(0, 0, FLOOR))
    level.scale.z = 0.55   # preview only: the web console sets this from the forecast
    m = root.matrix_basis.copy()
    pts = {"outlets": [m @ polar(NOZZLE_R, a, OUTLET[1]) for a in outlets],
           "inlet": m @ polar(NOZZLE_R, INLET[0], INLET[1])}
    return root, pts


def build_pump(site, pid, loc, rot_z=0.0):
    """Centrifugal transfer pump on a skid. Suction faces -X, motor at +X.

    Suction strainer, check valve, gauges and the coupling guard are in detail_equipment.pump_extras.
    """
    root = site.empty(pid, loc, rot_z, kind="pump", size=1.5)
    k = Kit(site, pid, root)
    st, dk = k["M_STEEL"], k["M_STEEL_DARK"]
    box(k["M_CONCRETE"], (3.8, 1.8, 0.2), (0, 0, 0.1))
    box(dk, (3.3, 1.2, 0.18), (0.1, 0, 0.29))                          # skid
    for y in (-0.55, 0.55):
        box(dk, (3.3, 0.08, 0.06), (0.1, y, 0.41))                     # drip lip
    cyl(st, 0.42, 0.34, (-0.8, 0, 0.85), axis="X", segs=20)            # volute
    cyl(st, 0.3, 0.08, (-1.01, 0, 0.85), axis="X", segs=16)            # suction cover
    box(st, (0.3, 0.5, 0.45), (-0.8, 0, 0.6))                          # volute foot
    tube(k["M_PIPE"], (-1.75, 0, 0.85), (-0.99, 0, 0.85), 0.12, 12)
    tube(k["M_PIPE"], (-0.8, 0, 1.2), (-0.8, 0, 1.9), 0.1, 12)
    cyl(st, 0.16, 0.6, (-0.3, 0, 0.85), axis="X", segs=14)            # bearing frame
    box(st, (0.4, 0.5, 0.45), (-0.25, 0, 0.6))
    cyl(st, 0.36, 1.0, (1.1, 0, 0.82), axis="X", segs=20)              # motor
    for x in (0.62, 1.58):
        cyl(dk, 0.37, 0.05, (x, 0, 0.82), axis="X", segs=20)           # end bells
    box(dk, (0.8, 0.6, 0.08), (1.1, 0, 0.42))
    box(dk, (0.25, 0.2, 0.25), (1.1, 0.38, 0.95))                      # terminal box
    k.done()
    fan = bmesh.new()
    cyl(fan, 0.34, 0.1, (0, 0, 0), axis="X", segs=20)
    for i in range(4):
        box(fan, (0.03, 0.62, 0.07), (0.06, 0, 0), Matrix.Rotation(i * PI / 4, 4, "X"))
    site.mesh(f"{pid}_rotor", fan, "M_STEEL_DARK", root, loc=(1.66, 0, 0.82))
    m = root.matrix_basis.copy()
    return root, {"suction": m @ Vector((-1.75, 0, 0.85)), "discharge": m @ Vector((-0.8, 0, 1.9))}


VALVE_MANUAL = {}   # valve ID -> True for a handwheel valve, False for one with a remote actuator


def valve_modes(data):
    """{valve ID: manual?} from a raw blueprint. A valve is manual when it says manual: true or
    remote: false, the way the backend reads it; otherwise it has an actuator."""
    out = {}
    for v in (data or {}).get("valves") or []:
        if isinstance(v, dict) and isinstance(v.get("id"), str):
            out[v["id"]] = v.get("manual") is True or v.get("remote") is False
    return out


def build_valve(site, vid, loc, rot_z=0.0):
    """Rising-stem gate valve. The pipe runs along local Y; the handwheel turns about local Z.

    A manual valve (VALVE_MANUAL, default manual) has a big handwheel and a rising stem. A
    remote one has an electric actuator on the yoke with a smaller override handwheel. Either
    way VID_wheel keeps its pivot at (0, 0, 0.88).
    """
    manual = VALVE_MANUAL.get(vid, True)
    root = site.empty(vid, loc, rot_z, kind="valve", size=0.8)
    root["actuation"] = "manual" if manual else "remote"
    k = Kit(site, vid, root)
    st, dk = k["M_STEEL"], k["M_STEEL_DARK"]
    cyl(dk, 0.13, 0.44, (0, 0, 0), axis="Y", segs=14)                   # ends
    rbox(dk, (0.24, 0.2, 0.44), (0, 0, 0.02), bevel=0.04)               # gate body
    for y in (-0.235, 0.235):
        cyl(dk, 0.21, 0.05, (0, y, 0), axis="Y", segs=16)               # end flanges
    cyl(dk, 0.16, 0.05, (0, 0, 0.26), segs=14)                          # bonnet flange
    cyl(dk, 0.1, 0.2, (0, 0, 0.38), segs=12, r2=0.075)                  # bonnet
    for sx in (-0.075, 0.075):
        strut(st, (sx, 0, 0.47), (sx * 0.9, 0, 0.83), 0.035, 0.05)      # yoke
    box(st, (0.24, 0.08, 0.06), (0, 0, 0.83))
    if manual:
        cyl(st, 0.018, 0.75, (0, 0, 0.86), segs=8)                      # rising stem, through the wheel
        cyl(dk, 0.03, 0.05, (0, 0, 1.23), segs=8)
    else:
        rbox(dk, (0.34, 0.3, 0.3), (0, 0, 1.07), bevel=0.03)            # actuator gearcase
        cyl(st, 0.11, 0.3, (0.3, 0, 1.03), axis="X", segs=14)           # motor
        cyl(dk, 0.115, 0.03, (0.44, 0, 1.03), axis="X", segs=14)
        rbox(dk, (0.16, 0.13, 0.14), (-0.05, 0, 1.29), bevel=0.015)     # terminal box and controls
        cyl(k["M_GLASS"], 0.045, 0.01, (-0.05, -0.07, 1.29), axis="Y", segs=10)   # local display
        line(dk, [(-0.13, 0, 1.29), (-0.25, 0, 1.29), (-0.25, 0, 0.3), (-0.25, 0.3, 0.0)], 0.018, segs=6)
    k.done()
    wheel = bmesh.new()
    handwheel(wheel, 0.26 if manual else 0.17, (0, 0, 0))
    site.mesh(f"{vid}_wheel", wheel, "M_STEEL", root, loc=(0, 0, 0.88))
    m = root.matrix_basis.copy()
    return root, {"north": m @ Vector((0, 0.27, 0)), "south": m @ Vector((0, -0.27, 0))}


def build_pumpjack(site, wid, loc, rot_deg):
    """Beam pumping unit. The horse head points to local -Y, over the wellhead. See well_parts.py."""
    import well_parts
    return well_parts.build(site, wid, loc, rot_deg)


def build_swd(site, sid, loc, fx=1, rot_z=0.0):
    """Disposal facility: pump building, filters, injection wellhead, fence.

    fx=1 puts the trunk inlet on local +X, fx=-1 mirrors it to -X. The gate faces local -Y.
    """
    root = site.empty(sid, loc, rot_z, kind="disposal_well", size=4)
    k = Kit(site, sid, root)
    box(k["M_GRAVEL"], (26, 22, 0.15), (0, 0, 0.075))
    box(k["M_STEEL"], (10, 6, 4), (3 * fx, -3, 2.15))
    box(k["M_STEEL_DARK"], (10.6, 6.6, 0.2), (3 * fx, -3, 4.25))
    box(k["M_STEEL_DARK"], (3.0, 0.05, 3.0), (5 * fx, -6.03, 1.65))
    box(k["M_STEEL_DARK"], (1.0, 0.05, 2.1), (0, -6.03, 1.2))
    for y in (-4.2, -2.6):
        cyl(k["M_STEEL"], 0.45, 2.0, (-4.5 * fx, y, 1.15), segs=16)
        ball(k["M_STEEL"], 0.45, (-4.5 * fx, y, 2.15), segs=14)
    cyl(k["M_CONCRETE"], 0.6, 0.3, (0, 6, 0.3), segs=14)
    cyl(k["M_STEEL_DARK"], 0.24, 1.9, (0, 6, 1.4), segs=14)
    box(k["M_STEEL_DARK"], (0.55, 0.55, 0.45), (0, 6, 1.1))
    tube(k["M_STEEL_DARK"], (0, 6, 1.9), (0.7 * fx, 6, 1.9), 0.09, 10)
    ring(k["M_STEEL"], 0.25, 0.025, (0.42 * fx, 6, 1.1), axis="X")
    line(k["M_PIPE"], [(3 * fx, 0.0, 0.6), (3 * fx, 4.5, 0.6), (0.3 * fx, 4.5, 0.6), (0.3 * fx, 6, 0.6)], 0.09)
    tube(k["M_PIPE"], (8.0 * fx, -2, 0.6), (9.5 * fx, -2, 0.6), 0.16, 12)
    x0, x1, y0, y1 = -12.5, 12.5, -10.5, 10.5
    runs = [((x0, y1), (x1, y1)), ((x0, y0), (x0, y1)), ((x1, y0), (x1, y1)),
            ((x0, y0), (-4, y0)), ((4, y0), (x1, y0))]
    gap = 3 if LOD["posts"] else 6
    for (ax, ay), (bx, by) in runs:
        length = math.hypot(bx - ax, by - ay)
        for i in range(int(length // gap) + 1):
            t = min(1.0, i * gap / length)
            px, py = ax + (bx - ax) * t, ay + (by - ay) * t
            tube(k["M_STEEL"], (px, py, 0.1), (px, py, 1.8), 0.04, 6)
        for z in (0.9, 1.7):
            tube(k["M_STEEL"], (ax, ay, z), (bx, by, z), 0.025, 6)
    k.done()
    return root, {"inlet": root.matrix_basis @ Vector((9.5 * fx, -2, 0.6))}


def build_yard(site, yid, loc, rot_z=0.0):
    root = site.empty(yid, loc, rot_z, kind="yard", size=3)
    k = Kit(site, yid, root)
    box(k["M_GRAVEL"], (44, 24, 0.12), (0, 0, 0.06))
    for x in (-17, -12, -7):
        for y in (-7.2, -4.8):
            box(k["M_CONCRETE"], (0.5, 0.5, 0.4), (x, y, 0.32))
    box(k["M_STEEL"], (12, 3.6, 2.8), (-12, -6, 1.92))
    box(k["M_STEEL_DARK"], (12.3, 3.9, 0.12), (-12, -6, 3.38))
    box(k["M_STEEL_DARK"], (0.9, 0.05, 2.0), (-9, -4.18, 1.55))
    for x in (-16, -13, -6.5):
        box(k["M_GLASS"], (1.4, 0.05, 0.9), (x, -4.18, 2.1))
    box(k["M_CONCRETE"], (1.4, 1.0, 0.5), (-9, -3.6, 0.37))
    tube(k["M_STEEL"], (16, -8, 0.1), (16, -8, 9.0), 0.1, 8)
    box(k["M_STEEL_DARK"], (1.2, 0.5, 0.3), (15.6, -8, 9.0))
    k.done()
    return root


def build_truck(site, tid, loc, rot_deg=0.0):
    """Vacuum truck. Faces local +X, origin on the ground at its center."""
    root = site.empty(tid, loc, math.radians(rot_deg), kind="truck", size=2)
    k = Kit(site, tid, root)
    box(k["M_STEEL_DARK"], (9.6, 0.9, 0.3), (0, 0, 1.0))
    box(k["M_STEEL"], (2.3, 2.4, 2.0), (3.65, 0, 2.15))
    box(k["M_STEEL"], (1.3, 2.2, 1.0), (5.4, 0, 1.65))
    box(k["M_GLASS"], (0.06, 2.1, 0.8), (4.82, 0, 2.6))
    for y in (-1.21, 1.21):
        box(k["M_GLASS"], (1.1, 0.05, 0.7), (3.9, y, 2.6))
    box(k["M_STEEL_DARK"], (0.25, 2.4, 0.35), (6.15, 0, 1.0))
    box(k["M_STEEL_DARK"], (0.05, 1.6, 0.8), (6.06, 0, 1.6))
    tube(k["M_STEEL_DARK"], (2.5, 1.05, 1.2), (2.5, 1.05, 3.6), 0.08, 8)
    cyl(k["M_TANK"], 1.05, 6.2, (-1.35, 0, 2.45), axis="X", segs=24)
    cyl(k["M_STEEL_DARK"], 0.35, 0.2, (-1.3, 0, 3.55), segs=14)
    box(k["M_STEEL_DARK"], (0.9, 1.6, 1.0), (2.0, 0, 1.85))
    for y in (-1.2, 1.2):
        box(k["M_STEEL_DARK"], (5.0, 0.25, 0.25), (-1.35, y, 1.5))
    for x in (-3.5, -1.3, 0.9):
        box(k["M_STEEL_DARK"], (0.3, 1.8, 0.5), (x, 0, 1.4))
    tube(k["M_PIPE"], (-4.45, 0, 1.7), (-4.95, 0, 1.7), 0.12, 10)
    k.done()
    return root   # wheels are axle objects added in detail_site.truck_extras
