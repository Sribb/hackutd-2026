"""Pipes that join assets: each pump's lines, the trunk to the disposal wells, and crossovers.

Every pipe is one mesh named for what it connects (see docs/CONTRACT.md). Runs that cross
the lease road or a driveway dip under it.
"""
from mathutils import Vector

import detail_equipment as de
import field_parts as fp
import layout as lay
from names import valve_base   # VALVE_XO_34 -> XO_34, for pipe names and the painted tag

TRUNK_Y, TRUNK_Z = lay.TRUNK_Y, lay.TRUNK_Z


def y_run(x, y0, y1, z):
    """Points along y at x from y0 to y1 at height z, dipping under the lease road if it crosses."""
    pts = [Vector((x, y0, z))]
    if y0 * y1 < 0:
        s = 1.0 if y0 > 0 else -1.0
        e = lay.ROAD_HALF + 2.1
        pts += [Vector((x, s * e, z)), Vector((x, s * (e - 0.8), -0.7)),
                Vector((x, -s * (e - 0.8), -0.7)), Vector((x, -s * e, z))]
    return pts + [Vector((x, y1, z))]


def pump_lines(site, supports, tank_id, p, root, pump, outlet):
    """Tank outlet to pump, pump through its discharge valve, then down to the trunk.

    Returns the x where this line ties into the trunk.
    """
    pid, m = p["id"], root.matrix_basis.copy()
    o, s = outlet, pump["suction"]
    if p["slot"] == 0 and not p["given"]:
        pts = [o, (o.x, o.y, 1.6), (o.x, s.y, 1.6), (o.x, s.y, s.z), s]
    else:
        pts = [o, (o.x, o.y, 1.6), (s.x, o.y, 1.6), (s.x, s.y, 1.6), s]
    site.pipe(f"PIPE_{tank_id}_{pid}", pts, 0.11)
    d = pump["discharge"]
    high = m @ Vector((-0.8, -1.9, 2.05))
    if p["valve"]:
        vid = p["valve"]
        vroot, valve = fp.build_valve(site, vid, m @ Vector((-0.8, -0.9, 2.05)), rot_z=p["rot"])
        de.valve_extras(site, vid, vroot, valve_base(vid).replace("_", "-"))
        vz = vroot.location.z
        site.pipe(f"PIPE_{pid}_{vid}", [d, (d.x, d.y, vz), valve["north"]], 0.09)
        start, name = [valve["south"], high], f"PIPE_{vid}_TRUNK"
    else:
        start, name = [d, m @ Vector((-0.8, 0, 2.05)), high], f"PIPE_{pid}_TRUNK"
    site.pipe(name, start + y_run(high.x, high.y, TRUNK_Y, TRUNK_Z), 0.09)
    step = 6.0 if high.y > TRUNK_Y else -6.0
    y = TRUNK_Y + step
    while (high.y - y) * step > 0:
        if abs(y - high.y) >= 4 and abs(y) > 8:
            fp.box(supports["M_CONCRETE"], (0.8, 0.5, TRUNK_Z - 0.08), (high.x, y, (TRUNK_Z - 0.08) / 2))
        y += step
    return high.x


def trunk_run(x0, x1, dips):
    """Trunk points from x0 to x1 along the shoulder, dipping under each driveway."""
    lo, hi = sorted((x0, x1))
    pts = [Vector((x0, TRUNK_Y, TRUNK_Z))]
    cross = [(a, b) for a, b in dips if lo < a and b < hi]
    if x1 < x0:
        cross = [(b, a) for a, b in reversed(cross)]
    step = 1 if x1 >= x0 else -1
    for a, b in cross:
        pts += [Vector((a - step * 1.2, TRUNK_Y, TRUNK_Z)), Vector((a - step * 0.4, TRUNK_Y, -0.7)),
                Vector((b + step * 0.4, TRUNK_Y, -0.7)), Vector((b + step * 1.2, TRUNK_Y, TRUNK_Z))]
    pts.append(Vector((x1, TRUNK_Y, TRUNK_Z)))
    return pts


def trunk(site, supports, L, ties, inlets):
    """The trunk line west to east between the tie-ins and the disposal wells.

    ties are the tie-in x positions; inlets is [(swd_id, inlet point)]. The westmost and
    eastmost disposal wells north of the road feed in at the trunk's ends, as SWD_1 and SWD_2
    do today. Any other disposal well gets a branch, PIPE_TRUNK_<id>, from the trunk.
    """
    xs = sorted([x for x in ties] + [i.x for _, i in inlets])
    nodes = []
    for x in xs:
        if not nodes or x - nodes[-1] > 0.3:
            nodes.append(x)
    north = [(sid, i) for sid, i in inlets if i.y > 0]
    end_w = next(((sid, i) for sid, i in north if abs(i.x - nodes[0]) <= 0.3), None)
    end_e = next(((sid, i) for sid, i in north if abs(i.x - nodes[-1]) <= 0.3 and (sid, i) != end_w), None)
    for k, (a, b) in enumerate(zip(nodes, nodes[1:])):
        pts = trunk_run(a, b, L["trunk_dips"])
        if k == 0 and end_w:
            pts = [end_w[1]] + pts
        if k == len(nodes) - 2 and end_e:
            pts = pts + [end_e[1]]
        site.pipe(f"PIPE_TRUNK_{k}", pts, 0.16, segs=12)
    for sid, inlet in inlets:
        if (sid, inlet) in (end_w, end_e):
            continue
        site.pipe(f"PIPE_TRUNK_{sid}", y_run(inlet.x, TRUNK_Y, inlet.y, TRUNK_Z) + [inlet], 0.16, segs=12)
    x = nodes[0] + 4
    while x < nodes[-1] - 2:
        if all(not (a - 1.5 < x < b + 1.5) for a, b in L["trunk_dips"]):
            fp.box(supports["M_CONCRETE"], (0.5, 0.9, TRUNK_Z - 0.15), (x, TRUNK_Y, (TRUNK_Z - 0.15) / 2))
        x += 10.0
    return len(nodes) - 1


def support(kit, x, y, z, along, off):
    """A post under a pipe at height z. off moves the post to one side of a rack lane."""
    px, py = (x, y + off) if along == "x" else (x + off, y)
    fp.box(kit["M_CONCRETE"], (0.6, 0.6, 0.3), (px, py, 0.15))
    fp.tube(kit["M_STEEL"], (px, py, 0.3), (px, py, z - 0.12), 0.06, 8)
    if along == "x":
        fp.box(kit["M_STEEL"], (0.1, 0.7 + abs(off), 0.08), (x, y + off / 2, z - 0.15))
    else:
        fp.box(kit["M_STEEL"], (0.7 + abs(off), 0.1, 0.08), (x + off / 2, y, z - 0.15))


def crossovers(site, supports, L):
    """Each crossover valve and its two pipe halves, PIPE_<base>_W and PIPE_<base>_E.

    The _W half joins the more westerly of the two tanks.
    """
    for x in L["crossovers"]:
        vid = x["id"]
        vroot, valve = fp.build_valve(site, vid, x["valve"], rot_z=x["valve_rot"])
        de.valve_extras(site, vid, vroot, valve_base(vid).replace("_", "-"))
        ends = [valve["north"], valve["south"]]
        near = Vector(x["a_pts"][-1])
        a_end = min(ends, key=lambda e: (e - near).length)
        b_end = ends[1] if a_end is ends[0] else ends[0]
        a_pipe, b_pipe = list(x["a_pts"]) + [a_end], [b_end] + list(x["b_pts"])
        base = valve_base(vid)
        west, east = (a_pipe, b_pipe) if x["west"] == x["a"] else (b_pipe, a_pipe)
        site.pipe(f"PIPE_{base}_W", west, 0.11)
        site.pipe(f"PIPE_{base}_E", east, 0.11)
        if x["valve_pad"]:
            fp.box(supports["M_CONCRETE"], (1.6, 1.6, 0.16), (x["valve"][0], x["valve"][1], 0.08))
        for s in x["supports"]:
            support(supports, *s)
