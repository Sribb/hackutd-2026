"""Build a field in Blender from a checked blueprint (blueprint.py) and its layout (layout.py).

build() creates every object in one collection. Assets are Empty nodes named with their
blueprint IDs; everything else follows the names in docs/CONTRACT.md.
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

PAD_TOP, TANK_Y, HEADER_Y = lay.PAD_TOP, lay.TANK_Y, 56.5


def ground_height(x, y):
    return (-0.2 + 0.07 * math.sin(x / 23) + 0.05 * math.sin(y / 17 + x / 41)
            + 0.04 * math.sin((x + y) / 9.3))


def ground(site, bounds):
    x0, x1, y0, y1 = bounds
    cell = max(5.0, math.sqrt((x1 - x0) * (y1 - y0) / 6000.0))
    nx, ny = max(2, round((x1 - x0) / cell)), max(2, round((y1 - y0) / cell))
    bm = bmesh.new()
    grid = [[bm.verts.new((x0 + (x1 - x0) * i / nx, y0 + (y1 - y0) * j / ny,
                           ground_height(x0 + (x1 - x0) * i / nx, y0 + (y1 - y0) * j / ny)))
             for i in range(nx + 1)] for j in range(ny + 1)]
    for j in range(ny):
        for i in range(nx):
            bm.faces.new((grid[j][i], grid[j][i + 1], grid[j + 1][i + 1], grid[j + 1][i]))
    site.mesh("SITE_ground", bm, "M_GROUND", recalc=False)


def slab(kit, mat, x0, x1, y0, y1, height):
    fp.box(kit[mat], (x1 - x0, y1 - y0, height), ((x0 + x1) / 2, (y0 + y1) / 2, height / 2))


def flowlines(site, f, tid, wells):
    """Each well's flowline to its tank's inlet header, laid so no two lines cross."""
    lines = []
    for wid, w in wells:
        lx, ly = f.local(w.x, w.y)
        lines.append((lx + 0.5, wid, w, ly))
    lines.sort(key=lambda l: (l[0], l[1]))
    c = len(lines)
    spacing = 2.0 if c <= 4 else 1.0
    hxs = [(j - (c - 1) / 2) * spacing for j in range(c)]
    for j, (lx, wid, w, ly) in enumerate(lines):
        hx, eastward = hxs[j], lx < hxs[j]
        # a line that runs past this one's drop must sit nearer the header, so it never crosses
        outer = sum(1 for k, l in enumerate(lines)
                    if (l[0] < hxs[k]) == eastward and (l[0] < lx if eastward else l[0] > lx))
        cy = 59.2 + 0.3 * outer
        pts = [w] + [f.world3(*p) for p in ((lx, ly, 0.12), (lx, cy, 0.12), (hx, cy, 0.12),
                                            (hx, HEADER_Y + 0.2, 0.12), (hx, HEADER_Y, 0.5))]
        site.pipe(f"PIPE_{wid}_{tid}", pts, 0.06, segs=8)


def build_pad(site, supports, pad, pumps, wells, xo_angles):
    """Pad, tank, pumps and valves, load-out, inlet header, wells. Returns the trunk tie-in xs."""
    f, tid, n = pad["frame"], pad["tank"], pad["n"]
    root = site.empty(f"PAD_{n}", (f.x, f.y, 0), f.rot, size=1)
    kit = fp.Kit(site, f"PAD_{n}", root)
    fp.box(kit["M_GRAVEL"], (24, 29, PAD_TOP), (0, 43.5, PAD_TOP / 2))
    for dy in (-6.2, 6.2):
        fp.box(kit["M_STEEL"], (13, 0.3, 0.8), (0, TANK_Y + dy, PAD_TOP + 0.4))
    for dx in (-6.2, 6.2):
        fp.box(kit["M_STEEL"], (0.3, 12.1, 0.8), (dx, TANK_Y, PAD_TOP + 0.4))
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
    flowlines(site, f, tid, outs)
    return ties


def build(collection, bp, L):
    site = fp.Site(collection)
    ground(site, L["ground"])
    civil = fp.Kit(site, "SITE", None)
    supports = fp.Kit(site, "SITE_supports", None)
    r0, r1 = L["road"]
    slab(civil, "M_ROAD", r0, r1, -lay.ROAD_HALF, lay.ROAD_HALF, 0.08)
    for d in L["driveways"]:
        slab(civil, "M_ROAD", *d, 0.07)

    ties = []
    for pad in L["pads"]:
        tid = pad["tank"]
        ties += build_pad(site, supports, pad, [p for p in L["pumps"] if p["tank"] == tid],
                          [w for w in L["wells"] if w["tank"] == tid], L["xo_nozzles"][tid])
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
    civil.done()
    supports.done()
    return {"trunk_pipes": trunk_pipes}
