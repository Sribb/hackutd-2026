"""Where every asset goes. Pure Python (no bpy), so the tests can run it without Blender.

Plan coordinates are the blueprint's x and y in meters. The lease road runs along x on
y = 0, and y points north. Blender uses the same X and Y with Z up. The glTF export turns
Blender Z into three.js Y, so a blueprint point (x, y) lands at three.js (x, height, -y).

Each tank sits on a pad, and a pad is a Frame: an origin on the road centerline and a
facing. Pads north of the road keep the original orientation (the road at pad-local
y = 0, the tank at y = 46). Pads south of the road are the same pad turned 180 degrees,
so every pad-local offset below works for both rows.
"""
import math

PAD_PITCH = 55.0
ROW_MAX = 8                       # more tanks than this go in two rows, one each side of the road
ROAD_HALF, TRUNK_Y, TRUNK_Z = 3.5, 8.0, 0.6
PAD_TOP = 0.15
TANK_Y = 46.0                     # pad-local tank center
NOZZLE_R = 3.7                    # must match field_parts.NOZZLE_R
NOZZLE_Z = PAD_TOP + 0.85
PUMP_Y = 33.0
PUMP_SLOTS = (4.83, 9.83)         # pad-local x of a tank's first and second pump
OUTLET_ANGLES = (-40.0, -15.0)    # tank-local nozzle angle feeding each of those pumps
STUB_ANGLES = (85.0, 95.0, 105.0, 115.0)   # back-side nozzles for crossovers to far tanks
SWD_INSET, SWD_Y, ROAD_MARGIN = 67.5, 16.0, 20.0
YARD_Y = -31.0
YARD_SLOTS = ((-2.0, 4.0), (12.5, 4.0), (-2.0, 0.6), (12.5, 0.6), (-16.5, 0.6), (12.5, -2.8))
FOOTPRINT = (-25.0, 25.0, 3.0, 96.0)       # pad-local area a pad and its wells need
PAD_RECT = (-12.0, 12.0, 29.0, 58.0)
LANE_Y, LANE_Z0, LANE_DZ, LANES = 54.0, 2.05, 0.45, 5
WELLS_UP_TO_4 = {
    0: [], 1: [(0.0, 76.0, 0.0)], 2: [(-8.0, 76.0, -4.0), (8.0, 76.0, 4.0)],
    3: [(-14.0, 72.0, -8.0), (0.0, 82.0, 0.0), (14.0, 72.0, 8.0)],
    4: [(-16.0, 72.0, -8.0), (-6.0, 82.0, 3.0), (6.0, 82.0, -3.0), (16.0, 72.0, 8.0)],
}


class Frame:
    """A pad-local coordinate system: origin (x, y) in plan, turned 180 degrees if flip."""

    def __init__(self, x, y, flip=False):
        self.x, self.y, self.flip = float(x), float(y), bool(flip)
        self.s = -1.0 if flip else 1.0

    @property
    def rot(self):
        return math.pi if self.flip else 0.0

    def world(self, lx, ly):
        return (self.x + self.s * lx, self.y + self.s * ly)

    def world3(self, lx, ly, z):
        return (*self.world(lx, ly), z)

    def local(self, wx, wy):
        return (self.s * (wx - self.x), self.s * (wy - self.y))

    def rect(self, x0, x1, y0, y1):
        (ax, ay), (bx, by) = self.world(x0, y0), self.world(x1, y1)
        return (min(ax, bx), max(ax, bx), min(ay, by), max(ay, by))


def overlaps(r, q, margin=0.0):
    return r[0] < q[1] + margin and q[0] < r[1] + margin and r[2] < q[3] + margin and q[2] < r[3] + margin


def suffix(tank_id):
    """TANK_3 -> 3. Names PAD_n, LOADOUT_n, PIPE_INLET_n and the painted number."""
    return tank_id[5:] if tank_id.startswith("TANK_") and len(tank_id) > 5 else tank_id


def tank_frame(x, y):
    return Frame(x, y + TANK_Y, True) if y < 0 else Frame(x, y - TANK_Y)


def well_slots(count):
    """Pad-local (x, y, rotation in degrees) for count auto-placed wells, west to east."""
    if count <= 4:
        return list(WELLS_UP_TO_4[count])
    n1 = (count + 1) // 2
    row1 = [(i - (n1 - 1) / 2) * 11.0 for i in range(n1)]
    gaps = [row1[0] - 5.5] + [x + 5.5 for x in row1]          # row 2 sits between row 1's wells
    row2 = sorted(sorted(gaps, key=lambda x: (abs(x), x))[:count - n1])
    clamp = lambda v, lim: max(-lim, min(lim, v))
    return [(x, 70.0, clamp(0.45 * x, 8.0)) for x in row1] + [(x, 88.0, clamp(-0.3 * x, 6.0)) for x in row2]


def tank_slots(n):
    """Pad frames for n tanks laid out automatically, and the yard's x in two-row fields."""
    if n <= ROW_MAX:
        return [Frame((i - (n - 1) / 2) * PAD_PITCH, 0.0) for i in range(n)], None
    k = math.ceil((n + 1) / 2)
    xs = [(i - (k - 1) / 2) * PAD_PITCH for i in range(k)]
    yard_i = k // 2
    south = sorted(sorted((j for j in range(k) if j != yard_i), key=lambda j: (abs(j - (k - 1) / 2), j))[:n - k])
    return [Frame(x, 0.0) for x in xs] + [Frame(xs[j], 0.0, True) for j in south], xs[yard_i]


def plan(bp):
    """Every position the builder needs, as plain numbers. Returns a dict."""
    L = {"pads": [], "pumps": [], "wells": [], "swds": [], "trucks": [], "crossovers": [],
         "driveways": [], "waypoints": [], "drops": [], "towers": [],
         "warnings": [], "errors": [], "placed": {"given": [], "auto": []}}
    warn, placed = L["warnings"].append, L["placed"]

    def mark(aid, given):
        placed["given" if given else "auto"].append(aid)

    tanks = bp["tanks"]
    slots, yard_x = tank_slots(len(tanks))
    given = {t["id"]: tank_frame(*t["pos"]) for t in tanks if t["pos"]}
    given_feet = [f.rect(*FOOTPRINT) for f in given.values()]
    east = max(f.x for f in slots)
    cands = slots + [Frame(east + PAD_PITCH * j, 0.0) for j in range(1, len(tanks) + 1)]
    used, frames = set(), {}
    for t in tanks:
        tid = t["id"]
        if tid in given:
            frames[tid] = given[tid]
            if abs(t["pos"][1]) < 30:
                warn(f"{tid} at y={t['pos'][1]:g} is close to the lease road on y = 0; pads need |y| of 30 m or more")
        else:
            i = next(i for i, f in enumerate(cands)
                     if i not in used and not any(overlaps(f.rect(*FOOTPRINT), g) for g in given_feet))
            used.add(i)
            frames[tid] = cands[i]
        mark(tid, tid in given)
    names = {}
    for t in tanks:
        n = suffix(t["id"])
        if n in names:
            L["errors"].append(f"{names[n]} and {t['id']} would both be named PAD_{n}; rename one tank")
        names[n] = t["id"]
        L["pads"].append({"tank": t["id"], "n": n, "frame": frames[t["id"]], "given": t["id"] in given,
                          "capacity": t["capacity_bbl"]})
    ids = list(frames)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if overlaps(frames[a].rect(*PAD_RECT), frames[b].rect(*PAD_RECT)):
                warn(f"the pads of {a} and {b} overlap; move one of them")
    L["rows"] = 2 if len({f.flip for f in frames.values()}) > 1 else 1

    pump_index = {}
    for p in bp["pumps"]:
        f = frames[p["tank_id"]]
        i = pump_index.get(p["tank_id"], 0)
        pump_index[p["tank_id"]] = i + 1
        pos = p["pos"] or f.world(PUMP_SLOTS[i], PUMP_Y)
        L["pumps"].append({"id": p["id"], "tank": p["tank_id"], "slot": i, "pos": pos, "rot": f.rot,
                           "frame": f, "given": bool(p["pos"]), "valve": p["valve_id"]})
        mark(p["id"], bool(p["pos"]))

    auto_wells = {}
    for w in bp["wells"]:
        if not w["pos"]:
            auto_wells.setdefault(w["tank_id"], []).append(w["id"])
    slot_of = {wid: s for tid, wids in auto_wells.items() for wid, s in zip(wids, well_slots(len(wids)))}
    for w in bp["wells"]:
        f = frames[w["tank_id"]]
        if w["pos"]:
            pos, rot = w["pos"], f.rot
        else:
            dx, ly, deg = slot_of[w["id"]]
            pos, rot = f.world(dx, ly), f.rot + math.radians(deg)
        L["wells"].append({"id": w["id"], "tank": w["tank_id"], "pos": pos, "rot": rot, "frame": f,
                           "given": bool(w["pos"])})
        mark(w["id"], bool(w["pos"]))

    pad_xs = [f.x for f in frames.values()]
    feet = [f.rect(*FOOTPRINT) for f in frames.values()]
    west_x, east_x = min(pad_xs) - SWD_INSET, max(pad_xs) + SWD_INSET
    spots = [(west_x, False, 1, -1), (east_x, False, -1, 1), (west_x, True, -1, -1), (east_x, True, 1, 1)]
    center_x = (min(pad_xs) + max(pad_xs)) / 2
    auto_i = 0
    for s in bp["swds"]:
        if s["pos"]:
            x, y = s["pos"]
            flip = y < 0
            fx = (1 if x < center_x else -1) * (-1 if flip else 1)
            f = Frame(x, y, flip)
        else:
            x, flip, fx, out = spots[auto_i % 4]
            auto_i += 1
            f = Frame(x, -SWD_Y if flip else SWD_Y, flip)
            while any(overlaps(f.rect(-13, 13, -11, 11), r) for r in feet):
                f = Frame(f.x + 30.0 * out, f.y, flip)
        L["swds"].append({"id": s["id"], "frame": f, "fx": fx, "given": bool(s["pos"])})
        mark(s["id"], bool(s["pos"]))

    yard = bp["yard"]
    if yard and yard["pos"]:
        x, y = yard["pos"]
        yf = Frame(x, y, y > 0)
    else:
        yf = Frame(yard_x if yard_x is not None else center_x, YARD_Y)
        while any(overlaps(yf.rect(-22, 22, -12, 12), r) for r in feet):
            yf = Frame(yf.x + PAD_PITCH, YARD_Y)
    L["yard"] = {"id": yard["id"] if yard else None, "frame": yf, "given": bool(yard and yard["pos"])}
    if yard:
        mark(yard["id"], L["yard"]["given"])
    auto_i = 0
    for t in bp["trucks"]:
        if t["pos"]:
            pos, rot = t["pos"], 0.0
        else:
            pos, rot = yf.world(*YARD_SLOTS[auto_i]), yf.rot
            auto_i += 1
        L["trucks"].append({"id": t["id"], "pos": pos, "rot": rot, "given": bool(t["pos"])})
        mark(t["id"], bool(t["pos"]))
    for v in bp["discharge"] + bp["crossovers"]:
        mark(v["id"], False)

    _site(L, frames, pad_xs)
    plan_crossovers(L, bp, frames)
    import siting   # imported here: siting reads this module's constants
    siting.place_equipment(L, bp)
    return L


def _site(L, frames, pad_xs):
    """Road, ground, driveways, waypoints, power drops, and light towers."""
    xs = [min(pad_xs) - SWD_INSET - ROAD_MARGIN, max(pad_xs) + SWD_INSET + ROAD_MARGIN]
    xs += [s["frame"].x - ROAD_MARGIN for s in L["swds"]] + [s["frame"].x + ROAD_MARGIN for s in L["swds"]]
    xs += [L["yard"]["frame"].x - 30, L["yard"]["frame"].x + 30]
    xs += [a["pos"][0] + d for key in ("pumps", "wells", "trucks") for a in L[key] if a["given"] for d in (-20, 20)]
    L["road"] = (min(xs), max(xs))
    rects = [f.rect(*FOOTPRINT) for f in frames.values()]
    rects += [s["frame"].rect(-13, 13, -11, 11) for s in L["swds"]] + [L["yard"]["frame"].rect(-22, 22, -12, 12)]
    rects += [(a["pos"][0], a["pos"][0], a["pos"][1] - 8, a["pos"][1] + 8)
              for key in ("pumps", "wells", "trucks") for a in L[key]]
    L["ground"] = (L["road"][0] - 10, L["road"][1] + 10,
                   min(-55.0, min(r[2] for r in rects) - 4), max(100.0, max(r[3] for r in rects) + 4))

    def drive(f, x0, x1, edge_ly, toward_road):
        """A driveway slab from the road edge to the pad-local y edge_ly."""
        (ax, _), (bx, _) = f.world(x0, 0), f.world(x1, 0)
        ey = f.world(0, edge_ly)[1]
        y0, y1 = (ROAD_HALF, ey) if ey > 0 else (ey, -ROAD_HALF)
        if y1 - y0 > 0.5:
            L["driveways"].append((min(ax, bx), max(ax, bx), y0, y1))
        elif toward_road:
            L["warnings"].append(f"{toward_road} touches the lease road, so it gets no driveway")

    for p in L["pads"]:
        f, tid = p["frame"], p["tank"]
        drive(f, -10.0, -4.0, 29.0, tid)
        L["waypoints"].append((f"ROAD_JCT_{tid}", (f.world(-7.0, 0)[0], 0.0, 0.1)))
        L["waypoints"].append((f"STOP_{tid}", f.world3(-10.2, 35.0, PAD_TOP)))
        L["drops"].append(f.world(-12.8, 30.0))
        L["towers"].append((f"PAD_{p['n']}", f.world3(10.5, 56.8, PAD_TOP), 226 + (180 if f.flip else 0)))
    for s in L["swds"]:
        f = s["frame"]
        drive(f, -4.0, 4.0, -10.8, s["id"])
        L["waypoints"].append((f"ROAD_JCT_{s['id']}", (f.x, 0.0, 0.1)))
        L["waypoints"].append((f"STOP_{s['id']}", f.world3(0.0, -8.5, 0.15)))
        L["drops"].append(f.world(-11.0 * s["fx"], -9.0))
    yf = L["yard"]["frame"]
    drive(yf, -4.0, 4.0, 12.0, None)
    if L["yard"]["id"]:
        L["waypoints"].append((f"ROAD_JCT_{L['yard']['id']}", (yf.x, 0.0, 0.1)))
        L["waypoints"].append((f"STOP_{L['yard']['id']}", yf.world3(0.0, 10.0, 0.12)))
    L["drops"].append(yf.world(-20.0, 11.0))
    L["waypoints"].append(("ROAD_END_W", (L["road"][0], 0.0, 0.1)))
    L["waypoints"].append(("ROAD_END_E", (L["road"][1], 0.0, 0.1)))
    L["trunk_dips"] = sorted((d[0], d[1]) for d in L["driveways"] if d[2] < TRUNK_Y < d[3])
    x0, x1 = L["road"][0] + 27.0, L["road"][1] - 27.0
    n = max(2, round((x1 - x0) / 44.0) + 1)
    L["poles"] = [x0 + (x1 - x0) * i / (n - 1) for i in range(n)]


# ---------- crossovers ----------

def _local_angle(frame, world_deg):
    return (world_deg - (180.0 if frame.flip else 0.0)) % 360.0


def plan_crossovers(L, bp, frames):
    """Route every crossover. Adjacent tanks in a row get a straight run between them, as
    today's VALVE_XO_34 does. Any other pair runs on a pipe rack behind the tanks, crossing
    under the road in a gap between pads when the tanks are on opposite sides."""
    center = {t: f.world(0, TANK_Y) for t, f in frames.items()}
    sides = {t: set() for t in frames}
    stubs = {t: list(STUB_ANGLES) for t in frames}
    L["xo_nozzles"] = {t: [] for t in frames}
    taken = []
    for xo in bp["crossovers"]:
        plan_ = _straight(xo, frames, center, sides, L) or _corridor(xo, frames, stubs, taken, L)
        if plan_:
            L["crossovers"].append(plan_)


def _straight(xo, frames, center, sides, L):
    a, b = xo["a"], xo["b"]
    fa, fb = frames[a], frames[b]
    (ax, ay), (bx, by) = center[a], center[b]
    if fa.flip != fb.flip or abs(ay - by) > 0.5 or abs(ax - bx) < 2 * NOZZLE_R + 3:
        return None
    lo, hi = sorted((ax, bx))
    if any(t not in (a, b) and abs(ty - ay) < 15 and lo - 1 < tx < hi + 1 for t, (tx, ty) in center.items()):
        return None
    w, e = (a, b) if ax < bx else (b, a)
    if "E" in sides[w] or "W" in sides[e]:
        return None
    sides[w].add("E")
    sides[e].add("W")
    L["xo_nozzles"][w].append(_local_angle(frames[w], 0.0))
    L["xo_nozzles"][e].append(_local_angle(frames[e], 180.0))
    z = 1.6
    tw, te = (lo + NOZZLE_R, ay, NOZZLE_Z), (hi - NOZZLE_R, ay, NOZZLE_Z)
    # a_pts run from tank a's nozzle up to the valve, b_pts from the valve on to tank b
    if w == a:
        a_pts, b_pts = [tw, (tw[0], ay, z)], [(te[0], ay, z), te]
    else:
        a_pts, b_pts = [te, (te[0], ay, z)], [(tw[0], ay, z), tw]
    half, mid, supports, d = (hi - lo) / 2, (lo + hi) / 2, [], 8.0
    while d <= half - 9.5:
        supports += [(mid - d, ay, z, "x", 0.0), (mid + d, ay, z, "x", 0.0)]
        d += 7.0
    return {"id": xo["id"], "style": "straight", "a": a, "b": b, "west": w,
            "valve": (mid, ay, z), "valve_rot": math.pi / 2, "a_pts": a_pts, "b_pts": b_pts,
            "supports": supports, "valve_pad": True}


def _gap(L, x_lo, x_hi):
    xs = sorted({round(p["frame"].x, 3) for p in L["pads"]})
    cands = [(p + q) / 2 for p, q in zip(xs, xs[1:]) if q - p >= 40]
    cands += [xs[0] - PAD_PITCH / 2, xs[-1] + PAD_PITCH / 2]
    yx = L["yard"]["frame"].x
    ok = [c for c in cands if abs(c - yx) >= 26] or cands
    mid = (x_lo + x_hi) / 2
    return min(ok, key=lambda c: (not x_lo <= c <= x_hi, abs(c - mid)))


def _vertical(x, y0, y1, z):
    """A run along y at x. Where it crosses the lease road it dips under it."""
    pts = [(x, y0, z)]
    if y0 * y1 < 0:
        s = 1.0 if y0 > 0 else -1.0
        pts += [(x, s * 6.0, z), (x, s * 5.2, -0.9), (x, -s * 5.2, -0.9), (x, -s * 6.0, z)]
    return pts + [(x, y1, z)]


def _corridor(xo, frames, stubs, taken, L):
    a, b = xo["a"], xo["b"]
    fa, fb = frames[a], frames[b]
    if not stubs[a] or not stubs[b]:
        L["errors"].append(f"{xo['id']}: {a if not stubs[a] else b} has no free crossover nozzle left")
        return None
    sa, sb = stubs[a][0], stubs[b][0]
    na = (NOZZLE_R * math.cos(math.radians(sa)), TANK_Y + NOZZLE_R * math.sin(math.radians(sa)))
    nb = (NOZZLE_R * math.cos(math.radians(sb)), TANK_Y + NOZZLE_R * math.sin(math.radians(sb)))
    pa, pb = fa.world(na[0], LANE_Y), fb.world(nb[0], LANE_Y)
    same = fa.flip == fb.flip and abs(fa.y - fb.y) < 0.5
    key = lambda f: (f.flip, round(f.y, 1))
    if same:
        segs, gx = [(key(fa), min(pa[0], pb[0]), max(pa[0], pb[0]))], None
    else:
        gx = _gap(L, min(pa[0], pb[0]), max(pa[0], pb[0]))
        segs = [(key(fa), min(pa[0], gx), max(pa[0], gx)), (key(fb), min(pb[0], gx), max(pb[0], gx))]

    def clash(lane):
        for t_lane, t_segs, t_gx in taken:
            if t_lane != lane:
                continue
            if gx is not None and t_gx is not None and abs(gx - t_gx) < 2.0:
                return True
            if any(k == tk and x0 < tx1 + 1 and tx0 < x1 + 1 for k, x0, x1 in segs for tk, tx0, tx1 in t_segs):
                return True
        return False

    lane = next((n for n in range(LANES) if not clash(n)), None)
    if lane is None:
        L["errors"].append(f"{xo['id']}: more than {LANES} crossovers share the pipe rack here")
        return None
    taken.append((lane, segs, gx))
    stubs[a].pop(0)
    stubs[b].pop(0)
    L["xo_nozzles"][a].append(sa)
    L["xo_nozzles"][b].append(sb)
    z = LANE_Z0 + LANE_DZ * lane
    middle, supports = [], []
    if gx is not None:
        gxl = gx + 0.5 * (lane - 2)
        middle = _vertical(gxl, pa[1], pb[1], z)
        y, step = pa[1], (7.0 if pb[1] > pa[1] else -7.0)
        while (pb[1] - y) * step > 0:
            if abs(y) > 10 and abs(y - pa[1]) > 1.5 and abs(y - pb[1]) > 1.5:
                supports.append((gxl, y, z, "y", 0.35))
            y += step
        runs = [(pa, (gxl, pa[1]), fa), ((gxl, pb[1]), pb, fb)]
    else:
        runs = [(pa, pb, fa)]
    for p, q, f in runs:
        x0, x1 = sorted((p[0], q[0]))
        x = x0 + 3.5 + lane
        while x < x1 - 1.5:
            supports.append((x, p[1], z, "x", 0.35 * f.s))
            x += 7.0
    a_pts = [fa.world3(na[0], na[1], NOZZLE_Z)]
    b_pts = [fa.world3(na[0], 51.6, NOZZLE_Z), fa.world3(na[0], 51.6, z), (*pa, z)] + middle
    b_pts += [(*pb, z), fb.world3(nb[0], 51.6, z), fb.world3(nb[0], 51.6, NOZZLE_Z), fb.world3(nb[0], nb[1], NOZZLE_Z)]
    w = a if frames[a].world(0, TANK_Y)[0] <= frames[b].world(0, TANK_Y)[0] else b
    return {"id": xo["id"], "style": "rack", "a": a, "b": b, "west": w, "lane": lane, "gap_x": gx,
            "valve": fa.world3(na[0], 50.7, NOZZLE_Z), "valve_rot": fa.rot,
            "a_pts": a_pts, "b_pts": _dedupe(b_pts), "supports": supports, "valve_pad": False}


def _dedupe(pts):
    out = [pts[0]]
    for p in pts[1:]:
        if math.dist(p, out[-1]) > 1e-3:
            out.append(p)
    return out
