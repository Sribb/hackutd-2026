"""Generated scenery for the gaps in the CC0 kit (blender/assets/manifest.json "gaps", #47):
a barbed wire ranch fence, soaptree yucca, prickly pear, and a cattle guard.

Every builder writes into a Kit (one bmesh per material), so many copies merge into a few
meshes. Builders take a random.Random for variation, so a seeded caller is deterministic.
Plants use muted desert colors. Scenery never carries an asset ID: it is not clickable.
"""
import math

import bmesh
import bpy
from mathutils import Matrix, Vector

import field_parts as fp
from detail_equipment import placed
from field_parts import box, cyl, tube

fp.PALETTE.update({
    "M_YUCCA": ((0.34, 0.38, 0.27), 0.0, 0.8),
    "M_CACTUS": ((0.30, 0.39, 0.25), 0.0, 0.75),
    "M_PLANT_DRY": ((0.50, 0.44, 0.33), 0.0, 0.95),
    "M_FRUIT": ((0.42, 0.15, 0.22), 0.0, 0.6),
})
GOLDEN = math.pi * (3 - math.sqrt(5))
_TEMPLATES = {}


def template(kind, *args):
    """(verts, faces) of a unit primitive, made once. Stamping copies of it into a big bmesh is
    much faster than running a bmesh operator there, which walks the whole mesh every call."""
    key = (kind, *args)
    if key not in _TEMPLATES:
        bm = bmesh.new()
        if kind == "sphere":
            bmesh.ops.create_uvsphere(bm, u_segments=args[0], v_segments=args[1], radius=1.0)
        elif kind == "cube":    # 1 m on a side, centered on the origin
            bmesh.ops.create_cube(bm, size=1.0)
        elif kind == "cyl":     # a capped cylinder of radius 1 and length 1 along Z, centered
            bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=args[0], radius1=1.0,
                                  radius2=1.0, depth=1.0)
        else:   # an open tube of radius 1 and length 1 along Z, centered on the origin
            bmesh.ops.create_cone(bm, cap_ends=False, cap_tris=False, segments=args[0], radius1=1.0,
                                  radius2=1.0, depth=1.0)
        bm.verts.index_update()
        _TEMPLATES[key] = ([tuple(v.co) for v in bm.verts], [tuple(v.index for v in f.verts) for f in bm.faces])
        bm.free()
    return _TEMPLATES[key]


def stamp(bm, tpl, m):
    """Add a copy of a template moved by matrix m."""
    verts, faces = tpl
    vs = [bm.verts.new(m @ Vector(c)) for c in verts]
    for f in faces:
        bm.faces.new([vs[i] for i in f])


def strand(bm, a, b, r=0.007):
    """A wire between two points: a 3-sided open tube, the cheapest thing that reads as wire."""
    a, b = Vector(a), Vector(b)
    d = b - a
    if d.length < 1e-4:
        return
    rot = Vector((0, 0, 1)).rotation_difference(d.normalized()).to_matrix().to_4x4()
    stamp(bm, template("tube", 3), Matrix.Translation((a + b) / 2) @ rot @ Matrix.Diagonal((r, r, d.length, 1.0)))


def blade(bm, base, direction, length, width):
    """A narrow leaf: a thin three-sided spike from base along direction."""
    d = Vector(direction).normalized()
    side = d.cross(Vector((0, 0, 1)))
    side = side.normalized() if side.length > 1e-3 else Vector((1, 0, 0))
    up = side.cross(d).normalized()
    b = Vector(base)
    v = [bm.verts.new(b + side * width), bm.verts.new(b - side * width * 0.5 + up * width * 0.8),
         bm.verts.new(b - side * width * 0.5 - up * width * 0.8), bm.verts.new(b + d * length)]
    for f in ((v[0], v[1], v[3]), (v[1], v[2], v[3]), (v[2], v[0], v[3]), (v[2], v[1], v[0])):
        bm.faces.new(f)


def yucca(k, loc, rng, scale=1.0):
    """Soaptree yucca: a shaggy trunk, a ball of stiff leaves, a skirt of dead ones, maybe a seed stalk."""
    x, y, z = loc
    h = rng.uniform(0.25, 1.4) * scale
    cyl(k["M_PLANT_DRY"], 0.13 * scale, h + 0.2, (x, y, z + h / 2 - 0.1), segs=6, r2=0.1 * scale)
    crown = Vector((x, y, z + h))
    n = rng.randint(30, 40)
    for i in range(n):
        az = i * GOLDEN + rng.uniform(-0.2, 0.2)
        el = math.radians(rng.uniform(5, 88))
        d = Vector((math.cos(az) * math.cos(el), math.sin(az) * math.cos(el), math.sin(el)))
        blade(k["M_YUCCA"], crown, d, rng.uniform(0.55, 0.85) * scale, 0.022 * scale)
    for i in range(max(8, n // 3)):
        az = i * GOLDEN * 1.7 + rng.uniform(-0.3, 0.3)
        el = math.radians(rng.uniform(-75, -45))
        d = Vector((math.cos(az) * math.cos(el), math.sin(az) * math.cos(el), math.sin(el)))
        blade(k["M_PLANT_DRY"], crown - Vector((0, 0, 0.08 * scale)), d, rng.uniform(0.35, 0.6) * scale, 0.02 * scale)
    if rng.random() < 0.4:
        top = crown + Vector((rng.uniform(-0.1, 0.1), rng.uniform(-0.1, 0.1), rng.uniform(1.1, 1.8) * scale))
        tube(k["M_PLANT_DRY"], crown, top, 0.022 * scale, 5)
        for j in range(5):
            p = crown.lerp(top, 0.6 + 0.08 * j)
            az = j * 2.1
            tip = p + Vector((math.cos(az) * 0.18, math.sin(az) * 0.18, 0.06)) * scale
            tube(k["M_PLANT_DRY"], p, tip, 0.012 * scale, 4)


def prickly_pear(k, loc, rng, scale=1.0):
    """Prickly pear: flat oval pads growing from each other's rims, with a few fruit on top."""
    x, y, z = loc
    pads = []

    def grow(base, yaw, lean, size, depth):
        w, t, hgt = 0.17 * size, 0.035 * size, 0.21 * size
        rot = Matrix.Rotation(yaw, 4, "Z") @ Matrix.Rotation(lean, 4, "Y")
        up = (rot @ Vector((0, 0, 1, 0))).to_3d()
        center = base + up * hgt * 0.9
        pads.append((center, rot, (w, t, hgt)))
        if depth == 0:
            return
        for j in range(rng.choice((1, 1, 2, 2))):
            side = rng.uniform(0.35, 0.8) * (1 if (j + depth) % 2 else -1)
            attach = center + (rot @ Vector((side * w, 0, hgt * 0.8, 0))).to_3d()
            grow(attach, yaw + rng.uniform(-0.8, 0.8), lean + side * 1.1, size * rng.uniform(0.75, 0.92), depth - 1)

    for i in range(rng.randint(3, 4)):     # low, wide clumps: base pads fan out from the center
        a = i * 1.9 + rng.uniform(0, 0.8)
        base = Vector((x + math.cos(a) * 0.2 * scale, y + math.sin(a) * 0.2 * scale, z - 0.05))
        lean = rng.uniform(0.45, 0.9) * (1 if i % 2 else -1)
        grow(base, a + rng.uniform(-0.4, 0.4), lean, scale * rng.uniform(0.9, 1.1), 2)
    for center, rot, (w, t, hgt) in pads[:10]:
        m = Matrix.Translation(center) @ rot @ Matrix.Diagonal((w, t, hgt, 1.0))
        stamp(k["M_CACTUS"], template("sphere", 7, 4), m)
        if rng.random() < 0.35:
            tip = center + (rot @ Vector((rng.uniform(-0.5, 0.5) * w, 0, hgt * 0.95, 0))).to_3d()
            stamp(k["M_FRUIT"], template("sphere", 5, 4), Matrix.Translation(tip) @ Matrix.Scale(0.035 * scale, 4))


def barbed_wire_fence(k, points, rng, post_gap=4.5, ground=lambda x, y: 0.0):
    """A ranch fence along a polyline: wood corner posts with H-braces, steel T-posts between,
    four wire strands, and barbs on the top strand. points is [(x, y), ...] in plan meters."""
    heights = (0.4, 0.65, 0.9, 1.15)
    wood, steel, wire = k["M_WOOD"], k["M_STEEL_DARK"], k["M_WIRE"]
    posts = []
    for i, (p, q) in enumerate(zip(points, points[1:])):
        p, q = Vector((*p, 0)), Vector((*q, 0))
        n = max(1, round((q - p).length / post_gap))
        posts += [p.lerp(q, j / n) for j in range(0 if i == 0 else 1, n + 1)]
    corners = {(round(c[0], 3), round(c[1], 3)) for c in points}
    for c in posts:
        g = ground(c.x, c.y)
        if (round(c.x, 3), round(c.y, 3)) in corners:
            cyl(wood, 0.1, 2.0, (c.x, c.y, g + 0.5), segs=6)
        else:
            box(steel, (0.05, 0.05, 1.45), (c.x, c.y, g + 0.52))
    for a, b in zip(posts, posts[1:]):
        ga, gb = ground(a.x, a.y), ground(b.x, b.y)
        for hz in heights:
            strand(wire, (a.x, a.y, ga + hz), (b.x, b.y, gb + hz))
        d = b - a
        steps = max(1, int(d.length / 2.0))
        for j in range(1, steps):
            p = a.lerp(b, j / steps)
            z = ga + (gb - ga) * j / steps + heights[-1]
            blade(wire, Vector((p.x, p.y, z - 0.03)), (rng.uniform(-1, 1), rng.uniform(-1, 1), 1.0), 0.07, 0.006)
    for c in points[:1] + points[-1:]:          # H-braces at the ends
        c = Vector((*c, 0))
        nxt = posts[1] if (posts[0] - c).length < 1e-3 else posts[-2]
        d = (nxt - c).normalized() * 2.4
        e = c + d
        g = ground(e.x, e.y)
        cyl(wood, 0.09, 1.9, (e.x, e.y, g + 0.45), segs=6)
        tube(wood, (c.x, c.y, g + 1.05), (e.x, e.y, g + 1.05), 0.06, 5)
        strand(wire, (c.x, c.y, g + 0.2), (e.x, e.y, g + 1.0), 0.01)


def cattle_guard(k, center, angle, width=8.0, length=2.4):
    """Steel pipe rails over a pit where a road crosses a fence, with wing rails.
    The road runs along local X turned by angle (radians); the rails run across it."""
    cx, cy, cz = center
    m = Matrix.Translation((cx, cy, cz)) @ Matrix.Rotation(angle, 4, "Z")
    st, dk, cc = k["M_STEEL"], k["M_STEEL_DARK"], k["M_CONCRETE"]
    placed(dk, m, box, (length, width, 0.05), (0, 0, -0.32))                 # pit floor in shadow
    for sx in (-1, 1):
        placed(cc, m, box, (0.3, width + 0.4, 0.5), (sx * (length / 2 + 0.15), 0, -0.12))
    for sy in (-1, 1):
        placed(cc, m, box, (length + 0.6, 0.3, 0.5), (0, sy * (width / 2 + 0.05), -0.12))
    n = int(length / 0.16)
    for i in range(n + 1):
        x = -length / 2 + length * i / n
        placed(st, m, cyl, 0.055, width, (x, 0, 0.06), axis="Y", segs=6)
    for sy in (-1, 1):                                                         # wing rails
        y = sy * (width / 2 + 0.2)
        for px in (-length / 2 - 0.2, length / 2 + 0.2):
            placed(st, m, tube, (px, y, -0.1), (px, y + sy * 1.6, 1.0), 0.05, 6)
        for z in (0.45, 0.9):
            placed(st, m, tube, (-length / 2 - 0.2, y + sy * 1.6 * z, z), (length / 2 + 0.2, y + sy * 1.6 * z, z), 0.035, 6)


def text(bm, body, size, m):
    """Flat painted text, centered on the origin and facing +Z, added to bm moved by matrix m."""
    cu = bpy.data.curves.new("_sign_text", "FONT")
    cu.body, cu.size, cu.extrude, cu.resolution_u = body, size, 0.0, 2
    cu.align_x, cu.align_y = "CENTER", "CENTER"
    tmp = bpy.data.objects.new("_sign_text", cu)
    me = tmp.to_mesh()
    vs = [bm.verts.new(m @ v.co) for v in me.vertices]
    for p in me.polygons:
        bm.faces.new([vs[i] for i in p.vertices])
    tmp.to_mesh_clear()
    bpy.data.objects.remove(tmp)
    bpy.data.curves.remove(cu)


FACE = Matrix.Rotation(math.pi / 2, 4, "X")      # turns flat text (facing +Z) to face -Y


def lease_sign(k, loc, yaw, lines):
    """A lease entrance sign in the Texas RRC style: two wood posts, a white board with the
    operator, lease, wells, and lease number, and a yellow H2S warning below. Faces local -Y."""
    m = Matrix.Translation(loc) @ Matrix.Rotation(yaw, 4, "Z")
    for x in (-0.75, 0.75):
        placed(k["M_WOOD"], m, box, (0.1, 0.1, 2.5), (x, 0.03, 1.05))
    placed(k["M_SIGN"], m, box, (1.9, 0.04, 1.15), (0, 0, 1.62))
    for i, line in enumerate(lines):
        size = 0.15 if i == 0 else 0.11
        text(k["M_STEEL_DARK"], line, size, m @ Matrix.Translation((0, -0.025, 2.03 - 0.2 * i - (0.02 if i else 0))) @ FACE)
    h2s_plate(k, m @ Matrix.Translation((0, 0, 0.66)))


def h2s_plate(k, m):
    """A generic hydrogen sulfide warning plate, centered on m, facing local -Y."""
    placed(k["M_HAZARD"], m, box, (0.86, 0.03, 0.56), (0, 0, 0))
    placed(k["M_STEEL_DARK"], m, box, (0.86, 0.032, 0.13), (0, 0, 0.21))
    text(k["M_HAZARD"], "DANGER", 0.09, m @ Matrix.Translation((0, -0.02, 0.205)) @ FACE)
    text(k["M_STEEL_DARK"], "POISON GAS", 0.075, m @ Matrix.Translation((0, -0.02, 0.06)) @ FACE)
    text(k["M_STEEL_DARK"], "H2S", 0.17, m @ Matrix.Translation((0, -0.02, -0.12)) @ FACE)


def far_pumpjack(k, loc, yaw):
    """A pumpjack seen from hundreds of meters away: skid, samson post, walking beam, horsehead,
    and cranks. About 70 triangles; faces +X (the horsehead end)."""
    m = Matrix.Translation(loc) @ Matrix.Rotation(yaw, 4, "Z")
    cube = template("cube")

    def part(mat, size, at, tilt=0.0):
        stamp(k[mat], cube, m @ Matrix.Translation(at) @ Matrix.Rotation(tilt, 4, "Y") @ Matrix.Diagonal((*size, 1.0)))

    part("M_STEEL_DARK", (7.5, 1.4, 0.4), (0.0, 0.0, 0.2))
    for s in (-0.55, 0.55):
        stamp(k["M_STEEL"], cube, m @ Matrix.Translation((0.3, s, 2.6)) @ Matrix.Rotation(s * 0.18, 4, "X")
              @ Matrix.Diagonal((0.25, 0.22, 5.0, 1.0)))
    part("M_STEEL", (7.2, 0.35, 0.55), (0.6, 0.0, 5.1))
    part("M_STEEL", (0.5, 0.45, 1.9), (4.3, 0.0, 4.6), 0.25)
    part("M_STEEL_DARK", (1.4, 0.3, 1.4), (-2.6, 0.0, 1.5))
    part("M_STEEL_DARK", (1.2, 1.1, 0.9), (-3.1, 0.0, 0.85))
    stamp(k["M_STEEL"], template("tube", 4), m @ Matrix.Translation((4.4, 0, 2.0)) @ Matrix.Diagonal((0.04, 0.04, 3.6, 1.0)))


PHASES = ((-1.0, 11.67), (1.0, 11.67), (0.0, 12.25))     # (y offset, height): detail_site.power's wires


def power_line(k, xs, y, z_at, sag_per_m=0.016):
    """A three-wire distribution line on wood poles at plan xs along y, each pole standing at
    z_at(x). The wires hang in a parabola that sags about 1.6% of the span, as real ones do."""
    for x in xs[1:]:
        z = z_at(x)
        stamp(k["M_WOOD"], template("cyl", 8), Matrix.Translation((x, y, z + 6.0)) @ Matrix.Diagonal((0.15, 0.15, 12.0, 1.0)))
        stamp(k["M_WOOD"], template("cube"), Matrix.Translation((x, y, z + 11.4)) @ Matrix.Diagonal((0.12, 2.4, 0.14, 1.0)))
        for dy, h in PHASES:
            stamp(k["M_STEEL"], template("cyl", 6), Matrix.Translation((x, y + dy, z + h - 0.1))
                  @ Matrix.Diagonal((0.05, 0.05, 0.2, 1.0)))
    for a, b in zip(xs, xs[1:]):
        za, zb, sag, n = z_at(a), z_at(b), sag_per_m * abs(b - a), 8
        for dy, h in PHASES:
            pts = [Vector((a + (b - a) * i / n, y + dy, za + h + (zb - za) * i / n - sag * 4 * (i / n) * (1 - i / n)))
                   for i in range(n + 1)]
            for p, q in zip(pts, pts[1:]):
                strand(k["M_WIRE"], p, q, 0.012)


def far_tank(k, loc, r=1.8, h=4.8):
    """A small stock tank seen from far away: a capped cylinder."""
    x, y, z = loc
    stamp(k["M_TANK"], template("cyl", 12), Matrix.Translation((x, y, z + h / 2)) @ Matrix.Diagonal((r, r, h, 1.0)))


def marker(k, loc):
    """A yellow pipeline marker post where a line runs under a road."""
    x, y, z = loc
    box(k["M_HAZARD"], (0.1, 0.1, 1.5), (x, y, z + 0.55))
    box(k["M_STEEL_DARK"], (0.14, 0.14, 0.1), (x, y, z + 1.33))


PARTS = ("yucca", "prickly_pear", "barbed_wire_fence", "cattle_guard", "lease_sign", "marker")
