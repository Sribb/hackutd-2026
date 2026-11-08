"""Where extra things go: what the field already occupies, and free spots for new things.

Pure Python (no bpy), so the tests can run it without Blender. Rectangles are plan
(x0, x1, y0, y1) in meters, x east along the lease road and y north.

occupied(L) lists everything layout.plan() put down that new things must stay off: the
road and trunk, driveways, pads with their pump lines, wells and flowlines, disposal
wells, the yard, crossovers, power drops, and the truck route waypoints. place_equipment()
puts blueprint equipment (#47) on the first free spot near where it belongs.
"""
import math

import layout as lay

POWER_Y = -6.5            # must match detail_site.POLE_Y: the power line on the road's south shoulder
# Footprints (along local X, along local Y) of each equipment model, guy wires and pads included.
FOOTPRINTS = {"SEPARATOR": (6.6, 3.0), "HEATER_TREATER": (3.8, 3.8), "FLARE": (8.0, 8.0),
              "COMPRESSOR": (9.2, 3.8), "LACT": (6.8, 3.2), "METER_RUN": (7.6, 1.8), None: (3.4, 3.4)}
# Pad-local spots tried first for equipment that belongs to a tank: the gaps beside the pad.
PAD_SPOTS = ((22.0, 20.0), (22.0, 36.0), (-24.0, 20.0), (-24.0, 36.0), (31.0, 20.0), (31.0, 36.0))
CLEAR = 1.5               # meters kept between a new thing and anything already there
STEP = 3.0                # search grid


def grow(r, m):
    return (r[0] - m, r[1] + m, r[2] - m, r[3] + m)


def around(x, y, hw, hd):
    return (x - hw, x + hw, y - hd, y + hd)


def span(p, q, m=0.0):
    """The rectangle covering a straight run from p to q, grown by m."""
    return (min(p[0], q[0]) - m, max(p[0], q[0]) + m, min(p[1], q[1]) - m, max(p[1], q[1]) + m)


def inside(r, bounds):
    return bounds[0] <= r[0] and r[1] <= bounds[1] and bounds[2] <= r[2] and r[3] <= bounds[3]


def occupied(L):
    """[(rect, what)] for everything already in the field."""
    out = []

    def add(r, what):
        out.append((r, what))

    r0, r1 = L["road"]
    add((r0, r1, -lay.ROAD_HALF - 0.5, lay.ROAD_HALF + 0.5), "the lease road")
    add((r0, r1, lay.TRUNK_Y - 1.0, lay.TRUNK_Y + 1.0), "the trunk line")
    add((r0, r1, POWER_Y - 3.0, POWER_Y + 1.5), "the power line")
    for d in L["driveways"]:
        add(grow(d, 0.5), "a driveway")
    for p in L["pads"]:
        f, name = p["frame"], f"PAD_{p['n']}"
        add(f.rect(*lay.PAD_RECT), name)
        add(f.rect(-13.5, 13.5, lay.ROAD_HALF, lay.PAD_RECT[2]), f"{name}'s pump lines")
        add(f.rect(-6.0, 6.0, 54.0, 63.0), f"{name}'s inlet header")
    for w in L["wells"]:
        x, y = w["pos"]
        add(around(x, y, 6.0, 6.0), w["id"])
        hx, hy = w["frame"].world(0.0, 57.0)
        add(span((x, y), (hx, hy), 1.0), f"{w['id']}'s flowline")
    for p in L["pumps"]:
        if p["given"]:
            x, y = p["pos"]
            add(around(x, y, 3.0, 3.0), p["id"])
            add(span((x, y), (x, lay.TRUNK_Y), 1.5), f"{p['id']}'s line to the trunk")
    for s in L["swds"]:
        f = s["frame"]
        add(grow(f.rect(-13.0, 13.0, -11.0, 11.0), 1.0), s["id"])
        ix, iy = f.world(9.5 * s["fx"], -2.0)
        add(span((ix, iy), (ix, lay.TRUNK_Y), 1.5), f"{s['id']}'s line to the trunk")
    yf = L["yard"]["frame"]
    add(grow(yf.rect(-22.0, 22.0, -12.0, 12.0), 1.0), L["yard"]["id"] or "the truck yard")
    for t in L["trucks"]:
        if t["given"]:
            add(around(*t["pos"], 6.0, 6.0), t["id"])
    for x in L["crossovers"]:
        pts = list(x["a_pts"]) + [x["valve"]] + list(x["b_pts"])
        for p, q in zip(pts, pts[1:]):
            add(span(p, q, 1.0), x["id"])
    poles = L.get("poles") or [0.0]
    for dx, dy in L["drops"]:
        add(around(dx, dy, 1.5, 1.5), "a power drop")
        px = min(poles, key=lambda p: abs(p - dx))
        add(span((px, POWER_Y), (dx, dy), 1.5), "a service wire")
    for name, loc in L["waypoints"]:
        add(around(loc[0], loc[1], 3.0, 3.0), name)
    for e in L.get("equipment", []):
        add(e["rect"], e["id"])
    return out


def clash(r, taken, margin=CLEAR):
    """What r overlaps, or None."""
    return next((what for q, what in taken if lay.overlaps(r, q, margin)), None)


def find_spot(size, anchor, taken, bounds, first=()):
    """Nearest (x, y) to anchor where a size (w, d) footprint clears taken, inside bounds.

    first lists spots to try before the search. Ties break on x, then y, so it is deterministic.
    """
    hw, hd = size[0] / 2, size[1] / 2
    for x, y in first:
        r = around(x, y, hw, hd)
        if inside(r, bounds) and not clash(r, taken):
            return (x, y)
    ax, ay = anchor
    nx = int((bounds[1] - bounds[0]) / STEP) + 1
    ny = int((bounds[3] - bounds[2]) / STEP) + 1
    grid = [(bounds[0] + i * STEP, bounds[2] + j * STEP) for i in range(nx) for j in range(ny)]
    grid.sort(key=lambda p: (round(math.hypot(p[0] - ax, p[1] - ay), 3), p[0], p[1]))
    for x, y in grid:
        r = around(x, y, hw, hd)
        if inside(r, bounds) and not clash(r, taken):
            return (x, y)
    return None


def anchor_of(aid, L):
    """Plan (x, y) of an asset the layout has placed, or None."""
    for p in L["pads"]:
        if p["tank"] == aid:
            return p["frame"].world(0.0, lay.TANK_Y)
    for key in ("pumps", "wells", "trucks"):
        for a in L[key]:
            if a["id"] == aid:
                return a["pos"]
    for s in L["swds"]:
        if s["id"] == aid:
            return (s["frame"].x, s["frame"].y)
    if L["yard"]["id"] == aid:
        return (L["yard"]["frame"].x, L["yard"]["frame"].y)
    for e in L.get("equipment", []):
        if e["id"] == aid:
            return e["pos"]
    return None


def place_equipment(L, bp):
    """L["equipment"]: where each blueprint equipment item goes, facing the road.

    A given x and y is used as is, with a warning if it lands on something. Otherwise an
    item with a tank_id goes in a gap beside that tank's pad, an item that connects to an
    asset goes near it, and anything else goes near the middle of the field. The ground
    grows to cover everything placed.
    """
    L["equipment"] = []
    taken = occupied(L)
    gx0, gx1, gy0, gy1 = L["ground"]
    bounds = (gx0 - 60.0, gx1 + 60.0, gy0 - 60.0, gy1 + 60.0)
    mid = ((L["road"][0] + L["road"][1]) / 2, 22.0)
    for e in bp["equipment"]:
        size = FOOTPRINTS.get(e["model"], FOOTPRINTS[None])
        if e["pos"]:
            pos, given = e["pos"], True
            hit = clash(around(*pos, size[0] / 2, size[1] / 2), taken, 0.0)
            if hit:
                L["warnings"].append(f"{e['id']} at ({pos[0]:g}, {pos[1]:g}) overlaps {hit}")
        else:
            given, first = False, ()
            pad = next((p for p in L["pads"] if p["tank"] == e["tank_id"]), None)
            if pad:
                anchor, first = pad["frame"].world(*PAD_SPOTS[0]), [pad["frame"].world(*s) for s in PAD_SPOTS]
            else:
                anchor = next((a for a in map(lambda c: anchor_of(c, L), e["connects"]) if a), mid)
            pos = find_spot(size, anchor, taken, bounds, first)
            if pos is None:
                L["errors"].append(f"{e['id']}: no free spot for a {e['type']} within 60 m of the field")
                continue
        rect = around(*pos, size[0] / 2, size[1] / 2)
        item = {"id": e["id"], "type": e["type"], "model": e["model"], "label": e["label"], "pos": pos,
                "rot": math.pi if pos[1] < 0 else 0.0, "size": size, "rect": rect, "given": given}
        L["equipment"].append(item)
        taken.append((rect, e["id"]))
        L["placed"]["given" if given else "auto"].append(e["id"])
    if L["equipment"]:
        rects = [e["rect"] for e in L["equipment"]]
        L["ground"] = (min(gx0, min(r[0] for r in rects) - 4), max(gx1, max(r[1] for r in rects) + 4),
                       min(gy0, min(r[2] for r in rects) - 4), max(gy1, max(r[3] for r in rects) + 4))
