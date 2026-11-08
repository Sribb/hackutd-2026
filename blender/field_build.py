"""Build a field in Blender from a checked blueprint (blueprint.py) and its layout (layout.py).

build() creates every object in one collection. Assets are Empty nodes named with their
blueprint IDs; everything else follows the names in docs/CONTRACT.md.

The civil works are shaped like a Permian lease (#98): a caliche road with a low crown, two
lanes of darker wheel tracks and shoulders that slope into the ground; driveways with their own
tracks; caliche pads with soft edges and a low berm along the sides and back; and black HDPE
flowlines on timber sleepers. Their shading is a vertex color layer, Col, that scenery.py
multiplies into the kit's textures; the plain field (--no-scenery) ignores it.
"""
import math

import bmesh
from mathutils import Vector

import detail_equipment as de
import detail_site as ds
import equipment_parts as eq
import field_parts as fp
import iot_parts as iot
import layout as lay
import piping
import siting
import terrain

PAD_TOP, TANK_Y, HEADER_Y = lay.PAD_TOP, lay.TANK_Y, 56.5
ground_height = terrain.lease_height

# Lease road cross-section: (meters from the centerline, height, shade). Trucks run down the
# middle, so a pair of wheel tracks sits a little low and dark there; the shoulders slope off
# below the ground.
_HALF = ((-5.4, -0.45, "toe"), (-4.2, -0.04, "shoulder"), (-3.4, 0.065, "edge"), (-1.3, 0.085, "road"),
         (-0.95, 0.066, "track"), (-0.6, 0.083, "road"))
ROAD_PROFILE = _HALF + ((0.0, 0.09, "crown"),) + tuple((-y, z, s) for y, z, s in reversed(_HALF))
SHADES = {"toe": (0.78, 0.62, 0.46), "shoulder": (0.84, 0.71, 0.56), "edge": (0.90, 0.79, 0.64),
          "road": (0.94, 0.83, 0.68), "crown": (0.96, 0.85, 0.70), "track": (0.58, 0.49, 0.39)}


def ground(site, bounds):
    """SITE_ground: the lease's ground. terrain.ring() continues it to the horizon from its edge."""
    x0, x1, y0, y1 = bounds
    nx, ny = terrain.ground_grid(bounds)
    bm = bmesh.new()
    grid = [[bm.verts.new((x0 + (x1 - x0) * i / nx, y0 + (y1 - y0) * j / ny,
                           ground_height(x0 + (x1 - x0) * i / nx, y0 + (y1 - y0) * j / ny)))
             for i in range(nx + 1)] for j in range(ny + 1)]
    for j in range(ny):
        for i in range(nx):
            bm.faces.new((grid[j][i], grid[j][i + 1], grid[j + 1][i + 1], grid[j + 1][i]))
    site.mesh("SITE_ground", bm, "M_GROUND", recalc=False)


def shade(key, wear=1.0):
    """A road shade; wear darkens or fades the wheel tracks along the road."""
    r, g, b = SHADES[key]
    if key == "track":
        k = 1.0 - wear
        r, g, b = r + (0.92 - r) * k, g + (0.81 - g) * k, b + (0.66 - b) * k
    return (r, g, b)


def strip(bm, col, rows):
    """Quads between consecutive rows of (x, y, z, rgb) points, facing up. Rows have equal length."""
    vs = []
    for row in rows:
        out = []
        for x, y, z, rgb in row:
            v = bm.verts.new((x, y, z))
            v[col] = (*rgb, 1.0)
            out.append(v)
        vs.append(out)
    (ax, ay), (bx, by), (cx, cy) = (vs[0][0].co.xy, vs[0][1].co.xy, vs[1][0].co.xy)
    up = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax) < 0
    for a, b in zip(vs, vs[1:]):
        for i in range(len(a) - 1):
            bm.faces.new((a[i], b[i], b[i + 1], a[i + 1]) if up else (a[i], a[i + 1], b[i + 1], b[i]))


def color_layer(bm):
    return bm.verts.layers.float_color.get("Col") or bm.verts.layers.float_color.new("Col")


def road_rows(x0, x1, step, z_at=None, wear_seed=71):
    """Rows of the lease road profile from x0 to x1 (x0 < x1), every step meters."""
    n = max(1, round((x1 - x0) / step))
    rows = []
    for i in range(n + 1):
        x = x0 + (x1 - x0) * i / n
        wear = 0.75 + 0.25 * terrain.noise(x / 23.0, 3.7, wear_seed)
        dz = z_at(x) if z_at else 0.0
        rows.append([(x, y, z + dz, shade(s, wear)) for y, z, s in ROAD_PROFILE])
    return rows


def driveway(bm, col, d):
    """A driveway from the lease road's edge to a pad: tracks, soft sides, a ramp up at the pad."""
    x0, x1, y0, y1 = d
    road_end, pad_end = (y0, y1) if abs(y0) < abs(y1) else (y1, y0)
    cx, w = (x0 + x1) / 2, x1 - x0
    across = [(x0 - 1.1, -0.4, "toe"), (x0, 0.068, "edge"), (cx - 1.15, 0.072, "road"), (cx - 0.9, 0.058, "track"),
              (cx - 0.65, 0.072, "road"), (cx, 0.078, "crown"), (cx + 0.65, 0.072, "road"),
              (cx + 0.9, 0.058, "track"), (cx + 1.15, 0.072, "road"), (x1, 0.068, "edge"), (x1 + 1.1, -0.4, "toe")]
    if w < 3.0:
        across = [across[0], across[1], across[5], across[-2], across[-1]]
    length = abs(pad_end - road_end)
    n = max(1, round(length / 8.0))
    s = 1.0 if pad_end > road_end else -1.0
    rows = []
    for i in range(n + 1):
        t = length * i / n
        y = road_end + s * t
        ramp = 0.07 * terrain.smooth((t - (length - 1.6)) / 1.6)
        wear = 0.8 + 0.2 * terrain.noise(cx / 7.0, y / 9.0, 73)
        rows.append([(x, y, z + (ramp if z > 0 else 0.0), shade(k, wear)) for x, z, k in across])
    strip(bm, col, rows)


def road(site, L):
    """SITE_road: the lease road and its driveways, with the shading in Col."""
    bm = bmesh.new()
    col = color_layer(bm)
    r0, r1 = L["road"]
    strip(bm, col, road_rows(r0, r1, 14.0))
    for d in L["driveways"]:
        driveway(bm, col, d)
    site.mesh("SITE_road", bm, "M_ROAD", recalc=False)


def pad_slab(site, n, root):
    """PAD_n_gravel: a flat caliche top with soft edges that slope into the ground."""
    rect = lay.PAD_RECT
    bm = bmesh.new()
    col = color_layer(bm)
    nx, ny = max(2, round((rect[1] - rect[0]) / 8.0)), max(2, round((rect[3] - rect[2]) / 8.0))
    x0, x1, y0, y1 = rect

    def caliche(x, y, k=1.0):
        v = 0.9 + 0.1 * terrain.noise(x / 6.0, y / 6.0, 90 + len(n))
        return (v * 0.95 * k, v * 0.85 * k, v * 0.70 * k, 1.0)

    grid, edge = [], {}
    for j in range(ny + 1):
        row = []
        for i in range(nx + 1):
            x, y = x0 + (x1 - x0) * i / nx, y0 + (y1 - y0) * j / ny
            v = bm.verts.new((x, y, PAD_TOP))
            v[col] = caliche(x, y)
            row.append(v)
            edge[(round(x, 4), round(y, 4))] = v
        grid.append(row)
    for j in range(ny):
        for i in range(nx):
            bm.faces.new((grid[j][i], grid[j][i + 1], grid[j + 1][i + 1], grid[j + 1][i]))
    pts, tris, first = terrain.ring(rect, rings=((1.4, 1),), fan=3, grid=(nx, ny))
    verts = []
    for k, (x, y) in enumerate(pts):
        if k < first:
            verts.append(edge[(round(x, 4), round(y, 4))])
        else:
            v = bm.verts.new((x, y, -0.45))
            v[col] = caliche(x, y, 0.82)
            verts.append(v)
    for a, b, c in tris:
        bm.faces.new((verts[a], verts[b], verts[c]))
    site.mesh(f"PAD_{n}_gravel", bm, "M_GRAVEL", root, recalc=False)


def berm(site, n, root, runs):
    """PAD_n_berm: a rounded ridge along each run, its ends sloping into the ground."""
    if not runs:
        return
    bm = bmesh.new()
    col = color_layer(bm)
    h, top = siting.BERM["half"], siting.BERM["top"]
    profile = ((-h, -0.45, 0.82), (-0.45 * h, 0.02, 0.92), (0.0, top, 1.0), (0.45 * h, 0.02, 0.92), (h, -0.45, 0.82))
    for run in runs:
        pts = [Vector((*p, 0)) for p in run]
        ends = [pts[0] - (pts[1] - pts[0]).normalized() * 1.1, pts[-1] + (pts[-1] - pts[-2]).normalized() * 1.1]
        path = [ends[0]] + pts + [ends[1]]
        rows = []
        for i, p in enumerate(path):
            a = path[max(0, i - 1)]
            c = path[min(len(path) - 1, i + 1)]
            d = (c - a).normalized()
            side = Vector((-d.y, d.x, 0))
            if 0 < i < len(path) - 1:
                d0, d1 = (p - a).normalized(), (c - p).normalized()
                miter = (Vector((-d0.y, d0.x, 0)) + Vector((-d1.y, d1.x, 0))).normalized()
                side = miter / max(0.5, miter.dot(Vector((-d0.y, d0.x, 0))))
            low = i in (0, len(path) - 1)
            rows.append([(p.x + side.x * o, p.y + side.y * o, -0.45 if low else z, (0.86 * k, 0.74 * k, 0.58 * k))
                         for o, z, k in profile])
        strip(bm, col, rows)
    site.mesh(f"PAD_{n}_berm", bm, "M_BERM", root, recalc=False)


def flowlines(site, supports, f, tid, wells):
    """Each well's black HDPE flowline to its tank's inlet header, laid so no two lines cross,
    on timber sleepers off the pad. Returns the pad-local polylines, for the berm's gaps."""
    lines = []
    for wid, w in wells:
        lx, ly = f.local(w.x, w.y)
        lines.append((lx + 0.5, wid, w, ly))
    lines.sort(key=lambda l: (l[0], l[1]))
    c = len(lines)
    spacing = 2.0 if c <= 4 else 1.0
    hxs = [(j - (c - 1) / 2) * spacing for j in range(c)]
    local = []
    for j, (lx, wid, w, ly) in enumerate(lines):
        hx, eastward = hxs[j], lx < hxs[j]
        # a line that runs past this one's drop must sit nearer the header, so it never crosses
        outer = sum(1 for k, l in enumerate(lines)
                    if (l[0] < hxs[k]) == eastward and (l[0] < lx if eastward else l[0] > lx))
        cy = 59.2 + 0.3 * outer
        run = ((lx, ly, 0.12), (lx, cy, 0.12), (hx, cy, 0.12), (hx, HEADER_Y + 0.2, 0.12), (hx, HEADER_Y, 0.5))
        obj = site.pipe(f"PIPE_{wid}_{tid}", [w] + [f.world3(*p) for p in run], 0.06, segs=8)
        obj.data.materials[0] = fp.material("M_HDPE")
        local.append([(p[0], p[1]) for p in run[:3]])
        sleepers(supports, f, run[:3])
    return local


def sleepers(supports, f, run):
    """Timber blocks under a flowline lying on the ground, every 6 m, off the pad."""
    x0, x1, y0, y1 = lay.PAD_RECT
    for (ax, ay, _), (bx, by, _) in zip(run, run[1:]):
        length = math.hypot(bx - ax, by - ay)
        k = int((length - 3.0) / 9.0)
        for i in range(k):
            t = (1.5 + 9.0 * (i + 0.5) + (length - 3.0 - 9.0 * k) / 2) / length
            x, y = ax + (bx - ax) * t, ay + (by - ay) * t
            if x0 - 1.5 < x < x1 + 1.5 and y0 - 1.5 < y < y1 + 1.5:
                continue
            wx, wy = f.world(x, y)
            along_y = abs(by - ay) > abs(bx - ax)
            size = (0.5, 0.14, 0.4) if along_y else (0.14, 0.5, 0.4)
            fp.box(supports["M_WOOD"], size, (wx, wy, -0.14))


def build_pad(site, supports, pad, pumps, wells, xo_angles, blockers):
    """Pad, tank, pumps and valves, load-out, inlet header, wells. Returns the trunk tie-in xs."""
    f, tid, n = pad["frame"], pad["tank"], pad["n"]
    root = site.empty(f"PAD_{n}", (f.x, f.y, 0), f.rot, size=1)
    pad_slab(site, n, root)
    kit = fp.Kit(site, f"PAD_{n}", root)
    ds.containment(kit["M_STEEL"], TANK_Y, PAD_TOP)
    kit.done()
    ds.pad_extras(site, n, root, TANK_Y, PAD_TOP)

    outlets = [lay.OUTLET_ANGLES[p["slot"]] for p in pumps]
    tank_root, tank = fp.build_tank(site, tid, f.world3(0, TANK_Y, PAD_TOP), f.rot, outlets, xo_angles)
    lo = de.tank_extras(site, tid, tank_root, n, pad["capacity"], outlets, xo_angles)
    ties = []
    for p, outlet in zip(pumps, tank["outlets"]):
        pump_root, pump = fp.build_pump(site, p["id"], (*p["pos"], PAD_TOP), p["rot"])
        de.pump_extras(site, p["id"], pump_root)
        ties.append(piping.pump_lines(site, supports, tid, p, pump_root, pump, outlet))

    stand = Vector(f.world3(-7.6, 35.0, PAD_TOP))
    de.build_loadout(site, f"LOADOUT_{n}", stand, f.rot)
    site.pipe(f"PIPE_{tid}_LOADOUT_{n}", [lo, (lo.x, lo.y, 1.75), (lo.x, stand.y, 1.75),
                                          (stand.x, stand.y, 1.75)], 0.1)
    inlet = tank["inlet"]
    site.pipe(f"PIPE_INLET_{n}", [f.world3(-4, HEADER_Y, 0.5), f.world3(4, HEADER_Y, 0.5)], 0.08)
    c = Vector(f.world3(0, HEADER_Y, 0.5))
    site.pipe(f"PIPE_INLET_{n}_RISER", [c, (c.x, c.y, inlet.z), (inlet.x, c.y, inlet.z), inlet], 0.08)

    outs = []
    for w in wells:
        well_root, well = fp.build_pumpjack(site, w["id"], (*w["pos"], 0.0), math.degrees(w["rot"]))
        de.well_extras(site, w["id"], well_root)
        outs.append((w["id"], well["outlet"]))
    lines = flowlines(site, supports, f, tid, outs)
    berm(site, n, root, siting.berm_runs(lines, blockers, f))
    return ties


def build(collection, bp, L):
    site = fp.Site(collection)
    ground(site, L["ground"])
    road(site, L)
    supports = fp.Kit(site, "SITE_supports", None)

    ties = []
    for pad in L["pads"]:
        tid = pad["tank"]
        ties += build_pad(site, supports, pad, [p for p in L["pumps"] if p["tank"] == tid],
                          [w for w in L["wells"] if w["tank"] == tid], L["xo_nozzles"][tid],
                          siting.berm_blockers(L, pad))
    piping.crossovers(site, supports, L)

    inlets = []
    for s in L["swds"]:
        f = s["frame"]
        root, swd = fp.build_swd(site, s["id"], (f.x, f.y, 0.0), fx=s["fx"], rot_z=f.rot)
        ds.swd_extras(site, s["id"], root, s["fx"])
        inlets.append((s["id"], swd["inlet"]))
    trunk_pipes = piping.trunk(site, supports, L, ties, inlets)

    yard = L["yard"]
    auto_trucks = [t for t in L["trucks"] if not t["given"]]
    if yard["id"]:
        f = yard["frame"]
        root = fp.build_yard(site, yard["id"], (f.x, f.y, 0.0), f.rot)
        ds.yard_extras(site, root, lay.YARD_SLOTS[:len(auto_trucks)])
    for t in L["trucks"]:
        ds.truck_extras(site, t["id"], fp.build_truck(site, t["id"], (*t["pos"], 0.12), math.degrees(t["rot"])))
    for item in L.get("equipment", []):
        eq.build(site, item)
    iot.build(site, bp, L)
    ds.power(site, L["drops"], L["poles"])
    for name, loc, aim in L["towers"]:
        ds.light_tower(site, name, loc, aim)
    for name, loc in L["waypoints"]:
        site.empty(name, loc, size=1)["kind"] = "waypoint"
    supports.done()
    return {"trunk_pipes": trunk_pipes}
