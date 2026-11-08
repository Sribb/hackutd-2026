"""Where extra things go: what the field already occupies, and free spots for new things.

Pure Python (no bpy), so the tests can run it without Blender. Rectangles are plan
(x0, x1, y0, y1) in meters, x east along the lease road and y north.

occupied(L) lists everything layout.plan() put down that new things must stay off: the
road and trunk, driveways, pads with their pump lines, wells and flowlines, disposal
wells, the yard, crossovers, power drops, and the truck route waypoints. place_equipment()
puts blueprint equipment (#47) on the first free spot near where it belongs, and
plan_scenery() lays out the desert scenery (#88) inside a triangle budget.
"""
import math
import random

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


# ---------- scenery (#88) ----------

# name: (triangles each, share of the scatter budget, most allowed, clearance radius in m).
# grass, rock, boulder, branch, creosote, and mesquite come from the CC0 kit; yucca and
# prickly_pear are generated (scenery_parts.py). The kit's foliage plants cost thousands of
# triangles each, so they are few, and the cheap ones carry the look.
SPECIES = {
    "grass": (224, 0.22, 400, 0.6), "yucca": (240, 0.14, 120, 1.0), "prickly_pear": (520, 0.13, 70, 1.1),
    "rock": (800, 0.11, 50, 0.7), "boulder": (1000, 0.06, 16, 1.0), "branch": (500, 0.03, 10, 0.8),
    "creosote": (7600, 0.14, 3, 1.3), "mesquite": (13400, 0.12, 1, 1.6),
}
FENCE_INSET, FENCE_TRIS_M, GUARD_TRIS = 2.0, 11.0, 600
SCATTER_MAX = 115_000       # most triangles the scatter may use, however much budget is left
SCATTER_M2 = 1.6            # and at most this many per square meter inside the fence (today's density)
CELL = 5.0                  # scatter grid
NEAR = 45.0                 # plants are thicker within this many meters of a pad or well, where close-ups look


def plan_scenery(L, budget, seed):
    """Everything the scenery pass builds, inside budget triangles, deterministic for a seed.

    Returns {"fence": [polylines], "guards": [(x, y)], "stubs": [rects], "sign": (x, y) or None,
    "scatter": [(species, x, y, rot, scale)], "dropped": [...]}. The fence runs FENCE_INSET
    inside the ground's edge, with a cattle guard where the lease road leaves at each end.
    Nothing lands on anything in occupied().
    """
    rng = random.Random(f"freeboard-scenery:{seed}")
    gx0, gx1, gy0, gy1 = L["ground"]
    x0, x1, y0, y1 = gx0 + FENCE_INSET, gx1 - FENCE_INSET, gy0 + FENCE_INSET, gy1 - FENCE_INSET
    out = {"fence": [], "guards": [], "stubs": [], "sign": None, "scatter": [], "dropped": []}
    gap = lay.ROAD_HALF + 1.0
    fence = [[(x0, gap), (x0, y1), (x1, y1), (x1, gap)], [(x0, -gap), (x0, y0), (x1, y0), (x1, -gap)]]
    cost = FENCE_TRIS_M * (2 * (x1 - x0) + 2 * (y1 - y0)) + 2 * GUARD_TRIS
    if budget >= cost + 5000:
        out["fence"], out["guards"] = fence, [(x0, 0.0), (x1, 0.0)]
        r0, r1 = L["road"]
        h = lay.ROAD_HALF
        out["stubs"] = [(gx0, x0 - 1.4, -h, h), (x0 + 1.4, r0, -h, h), (r1, x1 - 1.4, -h, h), (x1 + 1.4, gx1, -h, h)]
        budget -= cost
    else:
        out["dropped"].append("fence and cattle guards")
    taken = occupied(L) + [(grow(r, 1.0), "a road stub") for r in out["stubs"]]
    sign = (x0 + 4.0, -lay.ROAD_HALF - 4.0)
    if out["fence"] and not clash(around(*sign, 0.8, 0.8), taken):
        out["sign"] = sign
        taken.append((around(*sign, 1.0, 1.0), "the lease sign"))
    budget = min(budget, SCATTER_MAX, SCATTER_M2 * (x1 - x0) * (y1 - y0))
    if budget < 2000:
        out["dropped"].append("plants and rocks")
        return out
    counts = {k: min(cap, int(share * budget / tris)) for k, (tris, share, cap, _) in SPECIES.items()}
    tokens = [k for k in SPECIES for _ in range(counts[k])]
    rng.shuffle(tokens)
    # A smooth density field gives bare patches and thicker ones, instead of an even sprinkle.
    waves = [(rng.uniform(0.008, 0.03), rng.uniform(0.008, 0.03), rng.uniform(0, 6.3)) for _ in range(3)]
    cells = [(x0 + CELL * (i + 0.5), y0 + CELL * (j + 0.5)) for i in range(int((x1 - x0) / CELL))
             for j in range(int((y1 - y0) / CELL))]
    rng.shuffle(cells)
    spots = [p["frame"].world(0.0, lay.TANK_Y) for p in L["pads"]] + [w["pos"] for w in L["wells"]]
    for cx, cy in cells:
        if not tokens:
            break
        density = 0.5 + sum(math.sin(cx * a + cy * b + p) for a, b, p in waves) / 6
        if any(abs(cx - sx) < NEAR and abs(cy - sy) < NEAR for sx, sy in spots):
            density += 0.35
        if rng.random() > 0.25 + 0.75 * density:
            continue
        x, y = cx + rng.uniform(-0.35, 0.35) * CELL, cy + rng.uniform(-0.35, 0.35) * CELL
        kind = tokens[-1]
        r = SPECIES[kind][3]
        if not (x0 + r + 1 < x < x1 - r - 1 and y0 + r + 1 < y < y1 - r - 1) or clash(around(x, y, r, r), taken):
            continue
        if any(math.hypot(x - px, y - py) < r + SPECIES[k][3] for k, px, py, *_ in out["scatter"]
               if abs(x - px) < 4 and abs(y - py) < 4):
            continue
        tokens.pop()
        out["scatter"].append((kind, round(x, 3), round(y, 3), round(rng.uniform(0, 2 * math.pi), 4),
                               round(rng.uniform(0.8, 1.2), 3)))
    if tokens:
        out["dropped"].append(f"{len(tokens)} plants and rocks with no free spot")
    return out
