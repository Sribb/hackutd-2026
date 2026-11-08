"""The land past the lease (#98): the terrain to the horizon, mesas, the lease road and power
line running out of the lease, and other operators' pads. scenery.dress() calls these; each
builds under the SCENERY root and sits on the terrain mesh's own surface.

Also the small mesh helpers scenery.py shares: world-planar UVs, the caliche tint, and a mesh
object from plan vertices and faces.
"""
import math

import bmesh
import bpy

import detail_site as ds
import field_build
import field_parts as fp
import scenery_parts as sp
import terrain

POWER_SPANS, POWER_SPAN = 18, 90.0      # the power line runs this many spans west out of the lease


def uvs(me, uv_at):
    """World-planar texture coordinates from uv_at(x, y), if the mesh has none."""
    if me.uv_layers:
        return
    layer = me.uv_layers.new(name="UVMap")
    co = [me.vertices[i].co for i in range(len(me.vertices))]
    flat = []
    for lp in me.loops:
        c = co[lp.vertex_index]
        flat += uv_at(c.x, c.y)
    layer.data.foreach_set("uv", flat)


def caliche_uv(x, y):
    return (x / terrain.CALICHE_TILE, y / terrain.CALICHE_TILE)


def caliche_tint(x, y):
    v = 0.9 + 0.1 * terrain.noise(x / 6.0, y / 6.0, 97)
    return (v * 0.95, v * 0.85, v * 0.70)


def mesh_object(name, verts, faces, mat, root, colors=None, uv_at=None, smooth=True):
    """A mesh object under root from plan verts and faces, with optional Col and UVs."""
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    if colors is not None:
        attr = me.color_attributes.new("Col", "FLOAT_COLOR", "POINT")
        attr.data.foreach_set("color", [c for rgb in colors for c in (*rgb, 1.0)])
    if uv_at:
        uvs(me, uv_at)
    me.polygons.foreach_set("use_smooth", [smooth] * len(me.polygons))
    me.materials.append(fp.material(mat))
    obj = bpy.data.objects.new(name, me)
    obj.parent = root
    bpy.context.scene.collection.objects.link(obj)
    return obj


def tri_count(obj):
    me = obj.data
    return len(me.loops) - 2 * len(me.polygons)


def land(root, L, near, rings):
    """SCENERY_terrain out to the horizon. Returns the terrain's points, heights, and triangles,
    so what stands on it can sit on its surface."""
    b = L["ground"]
    pts, tris, first = terrain.ring(b, rings)
    zs = [terrain.lease_height(x, y) if i < first else terrain.height(x, y, b) for i, (x, y) in enumerate(pts)]
    mesh_object("SCENERY_terrain", [(x, y, z) for (x, y), z in zip(pts, zs)], tris, "M_GROUND", root,
                [terrain.tint(x, y, near) for x, y in pts], terrain.soil_uv)
    return pts, zs, tris


def mesas(root, L):
    """SCENERY_mesas: the caprock mesas on the horizon, one mesh."""
    verts, faces, cols = [], [], []
    for v, f, c in terrain.mesas(L["ground"], L.get("seed", "field")):
        base = len(verts)
        verts += v
        cols += c
        faces += [tuple(base + i for i in face) for face in f]
    return mesh_object("SCENERY_mesas", verts, faces, "M_MESA", root, cols, smooth=False)


def surface(pts, zs, tris, cell=100.0):
    """at(x, y): the terrain mesh's own height there (it is flat inside each triangle), or None
    off the terrain. Triangles are bucketed on a cell grid, so each lookup tries a few."""
    buckets = {}
    for t in tris:
        xs, ys = [pts[i][0] for i in t], [pts[i][1] for i in t]
        box = (min(xs), max(xs), min(ys), max(ys), t)
        for i in range(math.floor(box[0] / cell), math.floor(box[1] / cell) + 1):
            for j in range(math.floor(box[2] / cell), math.floor(box[3] / cell) + 1):
                buckets.setdefault((i, j), []).append(box)

    def at(x, y):
        for x0, x1, y0, y1, (a, b, c) in buckets.get((math.floor(x / cell), math.floor(y / cell)), ()):
            if not (x0 - 1e-6 <= x <= x1 + 1e-6 and y0 - 1e-6 <= y <= y1 + 1e-6):
                continue
            (ax, ay), (bx, by), (cx, cy) = pts[a], pts[b], pts[c]
            det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if abs(det) < 1e-9:
                continue
            u = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / det
            v = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / det
            if u >= -1e-6 and v >= -1e-6 and u + v <= 1 + 1e-6:
                return u * zs[a] + v * zs[b] + (1 - u - v) * zs[c]
        return None
    return at


def far_road(root, L, guards, at):
    """SCENERY_road: the lease road from its ends past the cattle guards out to the horizon,
    on the terrain. Full profile near the lease, a plain crowned strip beyond."""
    b = L["ground"]
    r0, r1 = L["road"]
    gw = min((g[0] for g in guards), default=b[0]) if guards else b[0]
    ge = max((g[0] for g in guards), default=b[1]) if guards else b[1]
    reach = terrain.HORIZON - 300.0

    def lift(x):
        if b[0] <= x <= b[1]:
            return 0.0
        tops = [at(x, y) for y in (-5.4, -3.5, 0.0, 3.5, 5.4)]
        tops = [t for t in tops if t is not None]
        return max([0.0, terrain.road_height(x, b)] + [t + 0.05 for t in tops]) if tops else 0.0

    simple = ((-5.4, -0.45, "toe"), (-3.6, 0.05, "edge"), (-1.0, 0.08, "road"), (0.0, 0.09, "crown"),
              (1.0, 0.08, "road"), (3.6, 0.05, "edge"), (5.4, -0.45, "toe"))
    bm = bmesh.new()
    col = field_build.color_layer(bm)
    gap = 1.4 if guards else 0.0
    for x0, x1 in ((gw + gap, r0), (r1, ge - gap)):
        if x1 - x0 > 0.5:
            field_build.strip(bm, col, field_build.road_rows(x0, x1, 4.0))
    for s, start in ((-1.0, gw - gap), (1.0, ge + gap)):
        xs, step, x = [], 4.0, start
        while abs(x - start) < reach:
            xs.append(x)
            x += s * step
            step = min(step * 1.12, 260.0)
        xs.append(start + s * reach)
        near = [x for x in xs if abs(x - start) <= 120.0]
        far = xs[len(near) - 1:]
        rows = []
        for x in near:
            dz = lift(x)
            rows.append([(x, y, z + dz, field_build.shade(k, 0.85)) for y, z, k in field_build.ROAD_PROFILE])
        if s < 0:
            rows.reverse()
        field_build.strip(bm, col, rows)
        rows = []
        for x in far:
            dz = lift(x)
            rows.append([(x, y, z + dz, field_build.shade(k, 0.85)) for y, z, k in simple])
        if s < 0:
            rows.reverse()
        field_build.strip(bm, col, rows)
    me = bpy.data.meshes.new("SCENERY_road")
    bm.to_mesh(me)
    bm.free()
    uvs(me, caliche_uv)
    me.polygons.foreach_set("use_smooth", [True] * len(me.polygons))
    me.materials.append(fp.material("M_ROAD"))
    obj = bpy.data.objects.new("SCENERY_road", me)
    obj.parent = root
    bpy.context.scene.collection.objects.link(obj)


def neighbor_leases(root, L, at, seed):
    """SCENERY_neighbors_*: other operators' caliche pads past the ends of the lease with far
    pumpjacks and small tanks on them, each with a spur road to the lease road's line."""
    b = L["ground"]
    simple = ((-3.4, -0.4, "toe"), (-2.2, 0.06, "edge"), (-0.7, 0.05, "track"), (0.0, 0.08, "crown"),
              (0.7, 0.05, "track"), (2.2, 0.06, "edge"), (3.4, -0.4, "toe"))

    def z_at(x, y):
        z = at(x, y)
        return terrain.height(x, y, b) if z is None else z

    pads, roads = bmesh.new(), bmesh.new()
    pcol, rcol = field_build.color_layer(pads), field_build.color_layer(roads)
    k = fp.Kit(fp.Site(bpy.context.scene.collection), "SCENERY_neighbors", root)
    for n in terrain.neighbors(b, seed):
        x0, x1, y0, y1 = n["rect"]
        top = max(z_at(x, y) for x in (x0, x1, (x0 + x1) / 2) for y in (y0, y1, (y0 + y1) / 2)) + 0.25
        ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        inner = [pads.verts.new((x, y, top)) for x, y in ring]
        outer = [pads.verts.new((x + (2.0 if x > (x0 + x1) / 2 else -2.0), y + (2.0 if y > (y0 + y1) / 2 else -2.0),
                                 z_at(x, y) - 0.5)) for x, y in ring]
        for v in inner:
            v[pcol] = (0.92, 0.82, 0.67, 1.0)
        for v in outer:
            v[pcol] = (0.8, 0.66, 0.5, 1.0)
        pads.faces.new(inner)
        for i in range(4):
            j = (i + 1) % 4
            pads.faces.new((inner[i], outer[i], outer[j], inner[j]))
        for jx, jy, yaw in n["jacks"]:
            sp.far_pumpjack(k, (jx, jy, top), yaw)
        for tx, ty in n["tanks"]:
            sp.far_tank(k, (tx, ty, top))
        sx, y_road, y_pad = n["spur"]
        s = 1.0 if y_pad > 0 else -1.0
        ys = [s * 5.0]
        while abs(ys[-1]) < abs(y_pad):
            ys.append(ys[-1] + s * 25.0)
        ys[-1] = y_pad
        rows = []
        for y in ys:
            dz = max(z_at(sx + dx, y) for dx in (-3.4, 0.0, 3.4)) + 0.08
            rows.append([(sx + dx, y, z + dz, field_build.shade(key, 0.8)) for dx, z, key in simple])
        field_build.strip(roads, rcol, rows)
    k.done()
    for bm, name, mat in ((pads, "SCENERY_neighbors_pads", "M_GRAVEL"), (roads, "SCENERY_neighbors_roads", "M_ROAD")):
        me = bpy.data.meshes.new(name)
        bm.to_mesh(me)
        bm.free()
        uvs(me, caliche_uv)
        me.materials.append(fp.material(mat))
        obj = bpy.data.objects.new(name, me)
        obj.parent = root
        bpy.context.scene.collection.objects.link(obj)


def power_out(root, L, at):
    """SCENERY_power: the lease's power line continued west out of the lease for about 1.6 km,
    so the line visibly comes from somewhere. Wires sag about 1.6% of each span."""
    poles = L.get("poles") or []
    if not poles:
        return None
    b = L["ground"]
    first = min(poles)
    xs = [first - POWER_SPAN * i for i in range(POWER_SPANS + 1)]

    def z_at(x):
        if x == first:
            return 0.0
        if b[0] <= x:
            return terrain.lease_height(x, ds.POLE_Y)
        z = at(x, ds.POLE_Y)
        return (terrain.height(x, ds.POLE_Y, b) if z is None else z) - 0.3

    k = fp.Kit(fp.Site(bpy.context.scene.collection), "SCENERY_power", root)
    sp.power_line(k, xs, ds.POLE_Y, z_at)
    k.done()
    return k
