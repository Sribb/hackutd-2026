"""Pipe routes and pipe supports for the no-Blender build, the same as piping.py and field_build.py.

Pure Python: every function returns plain points in Blender axes (x east, y north, z up,
meters), and blender/assemble_glb.py turns them into meshes. piping.py and field_build.py
need Blender's mathutils, so their routes are repeated here, function for function. When
one of them changes, change its twin here: blender/test_assemble.py compares every pipe's
bounding box with a Blender build of the same blueprint.

A pipe is (name, points, radius, sides). A box is (size, center). A post is (bottom, top,
radius) for a steel tube.
"""
import math

import layout as lay

HEADER_Y = 56.5                        # field_build.HEADER_Y: the inlet header, pad-local
PUMP_VALVE = (-0.8, -0.9, 2.05)        # piping.pump_lines, in the pump's frame: the discharge valve
PUMP_HIGH = (-0.8, -1.9, 2.05)         # where the discharge turns down toward the trunk
PUMP_RISE = (-0.8, 0.0, 2.05)          # the riser of a pump with no valve


def at(loc, rot, p):
    """A point p in a frame at loc turned rot about z, in world."""
    c, s = math.cos(rot), math.sin(rot)
    return (loc[0] + c * p[0] - s * p[1], loc[1] + s * p[0] + c * p[1], loc[2] + p[2])


def polar(r, deg, z=0.0):
    """field_parts.polar."""
    a = math.radians(deg)
    return (r * math.cos(a), r * math.sin(a), z)


def nozzles(tank, angles):
    """build_tank's nozzle stubs and tank_extras' two flanges on each, for the nozzles a tank's
    pumps and crossovers need. tank is the kit's tank constants, angles [(degrees, height)].
    Returns (stubs, flanges) as [(a, b, radius)] tubes in the tank's frame."""
    stubs, flanges = [], []
    for deg, z in angles:
        stubs.append((polar(tank["R"] - 0.05, deg, z), polar(tank["nozzle_r"], deg, z), tank["nozzle_pipe_r"]))
        dx, dy, _ = polar(tank["flange_thick"] / 2, deg)
        for r in tank["flange_at"]:
            cx, cy, cz = polar(r, deg, z)
            flanges.append(((cx - dx, cy - dy, cz), (cx + dx, cy + dy, cz), tank["flange_r"]))
    return stubs, flanges


def y_run(x, y0, y1, z):
    """piping.y_run: along y at x, dipping under the lease road if the run crosses it."""
    pts = [(x, y0, z)]
    if y0 * y1 < 0:
        s = 1.0 if y0 > 0 else -1.0
        e = lay.ROAD_HALF + 2.1
        pts += [(x, s * e, z), (x, s * (e - 0.8), -0.7), (x, -s * (e - 0.8), -0.7), (x, -s * e, z)]
    return pts + [(x, y1, z)]


def trunk_run(x0, x1, dips):
    """piping.trunk_run: along the shoulder from x0 to x1, dipping under each driveway."""
    lo, hi = sorted((x0, x1))
    y, z = lay.TRUNK_Y, lay.TRUNK_Z
    pts = [(x0, y, z)]
    cross = [(a, b) for a, b in dips if lo < a and b < hi]
    if x1 < x0:
        cross = [(b, a) for a, b in reversed(cross)]
    step = 1 if x1 >= x0 else -1
    for a, b in cross:
        pts += [(a - step * 1.2, y, z), (a - step * 0.4, y, -0.7), (b + step * 0.4, y, -0.7), (b + step * 1.2, y, z)]
    return pts + [(x1, y, z)]


def pump_lines(tank_id, p, outlet, pump, valve):
    """piping.pump_lines: tank outlet to pump, pump through its valve, then down to the trunk.

    pump and valve are the kit anchors (suction, discharge; north, south) in their own
    frames. Returns (pipes, concrete boxes, trunk tie-in x, valve location or None).
    """
    pid, loc, rot = p["id"], (*p["pos"], lay.PAD_TOP), p["rot"]
    o, s = outlet, at(loc, rot, pump["suction"])
    if p["slot"] == 0 and not p["given"]:
        pts = [o, (o[0], o[1], 1.6), (o[0], s[1], 1.6), (o[0], s[1], s[2]), s]
    else:
        pts = [o, (o[0], o[1], 1.6), (s[0], o[1], 1.6), (s[0], s[1], 1.6), s]
    pipes = [(f"PIPE_{tank_id}_{pid}", pts, 0.11, 10)]
    d, high = at(loc, rot, pump["discharge"]), at(loc, rot, PUMP_HIGH)
    vloc = None
    if p["valve"]:
        vid, vloc = p["valve"], at(loc, rot, PUMP_VALVE)
        pipes.append((f"PIPE_{pid}_{vid}", [d, (d[0], d[1], vloc[2]), at(vloc, rot, valve["north"])], 0.09, 10))
        start, name = [at(vloc, rot, valve["south"]), high], f"PIPE_{vid}_TRUNK"
    else:
        start, name = [d, at(loc, rot, PUMP_RISE), high], f"PIPE_{pid}_TRUNK"
    pipes.append((name, start + y_run(high[0], high[1], lay.TRUNK_Y, lay.TRUNK_Z), 0.09, 10))
    boxes = []
    step = 6.0 if high[1] > lay.TRUNK_Y else -6.0
    y = lay.TRUNK_Y + step
    while (high[1] - y) * step > 0:
        if abs(y - high[1]) >= 4 and abs(y) > 8:
            boxes.append(((0.8, 0.5, lay.TRUNK_Z - 0.08), (high[0], y, (lay.TRUNK_Z - 0.08) / 2)))
        y += step
    return pipes, boxes, high[0], vloc


def pad_lines(f, tid, n, loadout, inlet):
    """field_build.build_pad: the load-out line, the inlet header, and the riser into the tank."""
    stand = f.world3(-7.6, 35.0, lay.PAD_TOP)
    lo, c = loadout, f.world3(0, HEADER_Y, 0.5)
    return [(f"PIPE_{tid}_LOADOUT_{n}", [lo, (lo[0], lo[1], 1.75), (lo[0], stand[1], 1.75),
                                         (stand[0], stand[1], 1.75)], 0.1, 10),
            (f"PIPE_INLET_{n}", [f.world3(-4, HEADER_Y, 0.5), f.world3(4, HEADER_Y, 0.5)], 0.08, 10),
            (f"PIPE_INLET_{n}_RISER", [c, (c[0], c[1], inlet[2]), (inlet[0], c[1], inlet[2]), inlet], 0.08, 10)]


def flowlines(f, tid, wells):
    """field_build.flowlines: each well's line to its tank's inlet header, laid so no two cross.

    wells is [(well ID, outlet point)].
    """
    lines = []
    for wid, w in wells:
        lx, ly = f.local(w[0], w[1])
        lines.append((lx + 0.5, wid, w, ly))
    lines.sort(key=lambda l: (l[0], l[1]))
    c = len(lines)
    spacing = 2.0 if c <= 4 else 1.0
    hxs = [(j - (c - 1) / 2) * spacing for j in range(c)]
    pipes = []
    for j, (lx, wid, w, ly) in enumerate(lines):
        hx, eastward = hxs[j], lx < hxs[j]
        outer = sum(1 for k, l in enumerate(lines)
                    if (l[0] < hxs[k]) == eastward and (l[0] < lx if eastward else l[0] > lx))
        cy = 59.2 + 0.3 * outer
        pts = [w] + [f.world3(*p) for p in ((lx, ly, 0.12), (lx, cy, 0.12), (hx, cy, 0.12),
                                            (hx, HEADER_Y + 0.2, 0.12), (hx, HEADER_Y, 0.5))]
        pipes.append((f"PIPE_{wid}_{tid}", pts, 0.06, 8))
    return pipes


def support(x, y, z, along, off):
    """piping.support: a post under a pipe at height z. Returns (concrete boxes, steel boxes, posts)."""
    px, py = (x, y + off) if along == "x" else (x + off, y)
    bar = ((0.1, 0.7 + abs(off), 0.08), (x, y + off / 2, z - 0.15)) if along == "x" else \
        ((0.7 + abs(off), 0.1, 0.08), (x + off / 2, y, z - 0.15))
    return [((0.6, 0.6, 0.3), (px, py, 0.15))], [bar], [((px, py, 0.3), (px, py, z - 0.12), 0.06)]


def crossover_lines(x, base, ends):
    """piping.crossovers for one crossover: its halves PIPE_<base>_W and _E, valve pad, supports.

    ends are the valve's north and south points in world. Returns (pipes, concrete, steel, posts).
    """
    near = tuple(x["a_pts"][-1])
    k = min((0, 1), key=lambda i: math.dist(ends[i], near))
    a_pipe, b_pipe = list(x["a_pts"]) + [ends[k]], [ends[1 - k]] + list(x["b_pts"])
    west, east = (a_pipe, b_pipe) if x["west"] == x["a"] else (b_pipe, a_pipe)
    pipes = [(f"PIPE_{base}_W", west, 0.11, 10), (f"PIPE_{base}_E", east, 0.11, 10)]
    concrete, steel, posts = [], [], []
    if x["valve_pad"]:
        concrete.append(((1.6, 1.6, 0.16), (x["valve"][0], x["valve"][1], 0.08)))
    for s in x["supports"]:
        c, st, po = support(*s)
        concrete, steel, posts = concrete + c, steel + st, posts + po
    return pipes, concrete, steel, posts


def trunk(ties, inlets, dips):
    """piping.trunk: west to east between the tie-ins and the disposal wells.

    ties are the tie-in xs, inlets [(SWD ID, inlet point)]. The westmost and eastmost disposal
    wells north of the road feed in at the trunk's ends; any other gets a branch,
    PIPE_TRUNK_<id>. Returns (pipes, concrete boxes).
    """
    xs = sorted(list(ties) + [i[0] for _, i in inlets])
    nodes = []
    for x in xs:
        if not nodes or x - nodes[-1] > 0.3:
            nodes.append(x)
    north = [(sid, i) for sid, i in inlets if i[1] > 0]
    end_w = next(((sid, i) for sid, i in north if abs(i[0] - nodes[0]) <= 0.3), None)
    end_e = next(((sid, i) for sid, i in north if abs(i[0] - nodes[-1]) <= 0.3 and (sid, i) != end_w), None)
    pipes = []
    for k, (a, b) in enumerate(zip(nodes, nodes[1:])):
        pts = trunk_run(a, b, dips)
        if k == 0 and end_w:
            pts = [end_w[1]] + pts
        if k == len(nodes) - 2 and end_e:
            pts = pts + [end_e[1]]
        pipes.append((f"PIPE_TRUNK_{k}", pts, 0.16, 12))
    for sid, inlet in inlets:
        if (sid, inlet) not in (end_w, end_e):
            pipes.append((f"PIPE_TRUNK_{sid}", y_run(inlet[0], lay.TRUNK_Y, inlet[1], lay.TRUNK_Z) + [inlet], 0.16, 12))
    boxes = []
    x = nodes[0] + 4
    while x < nodes[-1] - 2:
        if all(not (a - 1.5 < x < b + 1.5) for a, b in dips):
            boxes.append(((0.5, 0.9, lay.TRUNK_Z - 0.15), (x, lay.TRUNK_Y, (lay.TRUNK_Z - 0.15) / 2)))
        x += 10.0
    return pipes, boxes
