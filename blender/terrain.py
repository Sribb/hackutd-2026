"""The land around the lease (#98): ground to the horizon, its color, and the distant mesas.

Pure Python (no bpy), so the tests can check it without Blender. Plan coordinates are the
blueprint's: x east along the lease road, y north, meters.

- lease_height() is the lease ground (SITE_ground), field_build.ground() samples it.
- ring() plans the terrain from the lease ground's edge out to the horizon. Its first loop is
  exactly the lease ground's edge vertices, so the two meshes meet with no crack.
- height() is the terrain: the lease ground at the edge, easing into a very gentle roll farther
  out, flat along the lease road's line so the road can run out to the horizon.
- tint() is the macro color painted into vertex colors (COLOR_0), so the soil texture's tiles
  don't read as a grid from the overview: broad paler and redder patches, and caliche dust
  near roads and pads.
- mesas() plans a cheap ring of caprock mesas and buttes on the horizon.
"""
import math
import random

HORIZON = 5400.0          # the terrain runs this far past the lease ground's edge
# (distance from the lease edge in m, keep every nth vertex of the loop inside it)
RINGS = ((3, 1), (8, 1), (15, 1), (25, 1), (40, 2), (60, 1), (90, 1), (130, 2), (190, 1), (270, 1),
         (380, 1), (540, 1), (760, 1), (1050, 1), (1450, 1), (2000, 1), (2800, 1), (3900, 1), (HORIZON, 1))
RINGS_LITE = ((6, 2), (30, 2), (120, 2), (400, 1), (1200, 1), (2800, 1), (HORIZON, 1))   # a field near budget
CORNER_FAN = 6            # extra directions that round each corner of the lease
ROAD_FLAT = (8.0, 60.0)   # the roll is flat within the first |y| and fully back by the second
SOIL_TILE = 8.0           # meters per tile of the soil texture
UV_TURN = 0.41            # radians: the tile lattice is turned off the road's axis
CALICHE_TILE = 6.0


def smooth(t):
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


def lease_height(x, y):
    """The lease ground: a few centimeters of wobble, always below pads and roads."""
    return (-0.2 + 0.07 * math.sin(x / 23) + 0.05 * math.sin(y / 17 + x / 41)
            + 0.04 * math.sin((x + y) / 9.3))


def ground_grid(bounds):
    """(nx, ny): cells of the lease ground mesh along x and y, about 6000 of at least 5 m."""
    x0, x1, y0, y1 = bounds
    cell = max(5.0, math.sqrt((x1 - x0) * (y1 - y0) / 6000.0))
    return max(2, round((x1 - x0) / cell)), max(2, round((y1 - y0) / cell))


def outside(x, y, bounds):
    """Meters from (x, y) to the lease ground's rectangle, 0 inside it."""
    x0, x1, y0, y1 = bounds
    return math.hypot(max(x0 - x, 0.0, x - x1), max(y0 - y, 0.0, y - y1))


# ---------- noise ----------

def _hash(ix, iy, seed):
    h = (ix * 374761393 + iy * 668265263 + seed * 1442695041) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    return ((h ^ (h >> 16)) & 0xFFFF) / 65535.0


def noise(x, y, seed=0):
    """Smooth value noise in 0..1 with features about 1 unit across."""
    ix, iy = math.floor(x), math.floor(y)
    fx, fy = x - ix, y - iy
    sx, sy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
    a, b = _hash(ix, iy, seed), _hash(ix + 1, iy, seed)
    c, d = _hash(ix, iy + 1, seed), _hash(ix + 1, iy + 1, seed)
    return a + (b - a) * sx + (c - a) * sy + (a - b - c + d) * sx * sy


def fbm(x, y, seed, octaves=3):
    total, amp, norm = 0.0, 1.0, 0.0
    for o in range(octaves):
        total += amp * noise(x, y, seed + 7 * o)
        norm += amp
        x, y, amp = x * 2.03 + 17.1, y * 2.03 - 9.7, amp * 0.5
    return total / norm


# ---------- height ----------

def _roll(x, y):
    """Long, low swells: about +-2 m over a few hundred meters to a few kilometers."""
    return (1.1 * math.sin(x / 310 + 0.4) * math.cos(y / 270 - 0.3) + 0.6 * math.sin((x - 2 * y) / 530 + 1.1)
            + 2.2 * math.sin(x / 1400 + 0.2) * math.sin(y / 1150 + 0.9) + 2.0 * (fbm(x / 900, y / 900, 5, 2) - 0.5))


def height(x, y, bounds):
    """Terrain height: the lease ground at its edge, easing into the roll by 250 m out, with a
    slow rise toward the horizon. Along the lease road's line (y near 0) the roll only depends
    on x, so the road out of the lease runs on the terrain without cutting through it."""
    d = outside(x, y, bounds)
    base = lease_height(x, y)
    if d <= 0.0:
        return base
    flat = smooth((abs(y) - ROAD_FLAT[0]) / (ROAD_FLAT[1] - ROAD_FLAT[0]))
    roll = _roll(x, 0.0) * (1 - flat) + _roll(x, y) * flat
    return base + smooth(d / 250.0) * (roll + 0.4) + 7.0 * smooth((d - 1800.0) / 3600.0)


def road_height(x, bounds):
    """The lease road's surface where it runs out of the lease, above any terrain along it."""
    return height(x, 0.0, bounds) + 0.3 * smooth(outside(x, 0.0, bounds) / 40.0)


# ---------- color ----------

DUST = 9.0                # meters of caliche dust beside roads and pads


def tint(x, y, near=None):
    """Vertex color (r, g, b) multiplying the soil texture: patches 40 m to 1 km across, paler
    with caliche dust where near(x, y) says a road or pad is close (meters, or None)."""
    n1 = fbm(x / 170.0, y / 170.0, 11)
    n2 = fbm(x / 41.0, y / 41.0, 23, 2)
    n3 = noise(x / 950.0, y / 950.0, 37)
    v = 0.80 + 0.55 * (n1 - 0.5) + 0.28 * (n2 - 0.5) + 0.2 * (n3 - 0.5)
    red = smooth((n3 - 0.35) * 2.2) * 0.6 + (n1 - 0.5) * 0.5
    r, g, b = v * (1.0 + 0.05 * red), v * (1.0 - 0.05 * red), v * (1.0 - 0.11 * red)
    if near is not None:
        dist = near(x, y)
        if dist is not None:
            k = 0.55 * math.exp(-max(0.0, dist) / DUST)
            r, g, b = r + (0.98 - r) * k, g + (0.95 - g) * k, b + (0.90 - b) * k
    return (min(1.0, r), min(1.0, g), min(1.0, b))


def soil_uv(x, y):
    """Soil texture coordinates: tiles turned off the road's axis and gently warped, so the
    repeat runs along wavy lines instead of a straight lattice."""
    c, s = math.cos(UV_TURN), math.sin(UV_TURN)
    u, v = (c * x + s * y) / SOIL_TILE, (-s * x + c * y) / SOIL_TILE
    return (u + 0.45 * math.sin(y / 37.0 + 0.7) + 0.3 * math.sin((x + y) / 83.0),
            v + 0.45 * math.sin(x / 43.0 - 0.4) + 0.3 * math.sin((x - y) / 71.0))


# ---------- the ring out to the horizon ----------

def boundary(bounds, grid=None):
    """The lease ground's edge vertices, counterclockwise from the southwest corner, computed
    exactly as field_build.ground() makes them (or for a grid of (nx, ny) cells). Returns
    (points, corner indices)."""
    x0, x1, y0, y1 = bounds
    nx, ny = grid or ground_grid(bounds)
    X = lambda i: x0 + (x1 - x0) * i / nx
    Y = lambda j: y0 + (y1 - y0) * j / ny
    pts = [(X(i), Y(0)) for i in range(nx)] + [(X(nx), Y(j)) for j in range(ny)]
    pts += [(X(nx - i), Y(ny)) for i in range(nx)] + [(X(0), Y(ny - j)) for j in range(ny)]
    return pts, (0, nx, nx + ny, 2 * nx + ny)


def ring(bounds, rings=RINGS, fan=CORNER_FAN, grid=None):
    """The terrain around the lease ground as plan points and triangles.

    Returns (points, tris, first): points[:first] are the lease ground's edge vertices, so they
    must get lease_height(); every triangle is counterclockwise seen from above. Near the lease
    the loops follow its rounded rectangle; far out they ease into an evenly spaced circle.
    """
    pts, corners = boundary(bounds, grid)
    x0, x1, y0, y1 = bounds
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half = math.hypot(x1 - x0, y1 - y0) / 2
    edge_of = []                      # outward normal angle of each edge vertex
    for i in range(len(pts)):
        side = sum(1 for c in corners if i >= c) - 1
        edge_of.append(math.radians((270, 0, 90, 180)[side]))
    entries = []                      # (boundary index, direction)
    for i in range(len(pts)):
        if i in corners:
            a_out = edge_of[i]
            a_in = a_out - math.pi / 2
            entries += [(i, a_in + (a_out - a_in) * f / fan) for f in range(fan + 1)]
        else:
            entries.append((i, edge_of[i]))
    n = len(entries)
    south_mid = next(e for e, (i, _) in enumerate(entries) if i == corners[1] // 2)
    theta0 = 1.5 * math.pi - 2 * math.pi * south_mid / n
    points = list(pts)
    first = len(points)
    loop_ids = [i for i, _ in entries]           # vertex id of each live entry on the current loop
    live = list(range(n))                        # which entries are still on the loop
    tris = []
    for d, keep in rings:
        kept = live[::keep] if keep > 1 else list(live)
        w = smooth((d - 60.0) / 900.0)
        new_ids = []
        for e in kept:
            bi, a = entries[e]
            bx, by = pts[bi]
            ox, oy = bx + d * math.cos(a), by + d * math.sin(a)
            th = theta0 + 2 * math.pi * e / n
            r = half + d
            px, py = ox + (cx + r * math.cos(th) - ox) * w, oy + (cy + r * math.sin(th) - oy) * w
            new_ids.append(len(points))
            points.append((px, py))
        pos = {e: k for k, e in enumerate(live)}
        m = len(kept)
        for j in range(m):
            i0, i1 = pos[kept[j]], pos[kept[(j + 1) % m]]
            if i1 <= i0:
                i1 += len(live)
            b0, b1 = new_ids[j], new_ids[(j + 1) % m]
            s = i0 + (i1 - i0) // 2          # the first half of the gap fans to b0, the rest to b1
            for i in range(i0, i1):
                if i == s:
                    tris.append((loop_ids[s % len(live)], b0, b1))
                a0, a1 = loop_ids[i % len(live)], loop_ids[(i + 1) % len(live)]
                if a0 != a1:
                    tris.append((a1, a0, b0 if i < s else b1))
        live, loop_ids = kept, new_ids
    return points, tris, first


def signed_area(p, q, r):
    return ((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])) / 2


# ---------- mesas on the horizon ----------

CAPROCK, LEDGE, TALUS, FOOT = (0.76, 0.68, 0.56), (0.60, 0.50, 0.40), (0.62, 0.43, 0.32), (0.60, 0.47, 0.36)
HAZE, HAZE_K = (0.74, 0.77, 0.82), 0.2


def _hazed(c, k=HAZE_K):
    return tuple(a + (h - a) * k for a, h in zip(c, HAZE))


def mesas(bounds, seed, count=24, clusters=7):
    """A ring of caprock mesas and buttes 2.8 to 4.8 km out, in clusters spread around the
    horizon so every view has some.

    Returns [(verts [(x, y, z)], faces [(i, j, k, ...)], colors [(r, g, b)] per vertex)]. Each is a
    flat top, a caprock cliff, and a talus slope that runs below the terrain.
    """
    rng = random.Random(f"freeboard-mesas:{seed}")
    x0, x1, y0, y1 = bounds
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    turn = rng.uniform(0, 2 * math.pi)
    centers = [turn + 2 * math.pi * (i + rng.uniform(-0.25, 0.25)) / clusters for i in range(clusters)]
    out = []
    for k in range(count):
        th = centers[k % clusters] + rng.gauss(0, 0.13)
        dist = rng.uniform(2800, 4800)
        mx, my = cx + dist * math.cos(th), cy + dist * math.sin(th)
        wide = rng.uniform(0.2, 1.0) ** 1.5
        w = 180 + 1000 * wide                     # along the horizon
        dpt = w * rng.uniform(0.35, 0.7)          # toward the field
        h = rng.uniform(30, 50) + 45 * wide * rng.random()
        cap = rng.uniform(4.0, 9.0)
        m = 12
        top = []
        for i in range(m):
            a = 2 * math.pi * i / m
            r = 1.0 + 0.16 * (noise(i * 0.9, k * 3.1, 61) - 0.5) * 2
            lx, ly = 0.5 * w * math.cos(a) * r, 0.5 * dpt * math.sin(a) * r
            top.append((lx, ly))
        t = th + math.pi / 2                      # long axis along the horizon
        ct, st = math.cos(t), math.sin(t)
        z_ground = height(mx, my, bounds)
        z_top = z_ground + h
        levels = [(0.0, z_top, CAPROCK), (1.2, z_top - cap, LEDGE),
                  (0.55 * (h - cap) + 1.2, z_top - cap - 0.5 * (h - cap), TALUS),
                  (1.7 * (h - cap) + 1.2, z_ground - 8.0, FOOT)]
        verts, cols = [], []
        for grow, z, col in levels:
            for lx, ly in top:
                L = math.hypot(lx, ly) or 1.0
                gx, gy = lx + grow * lx / L, ly + grow * ly / L
                verts.append((mx + gx * ct - gy * st, my + gx * st + gy * ct, z))
                cols.append(_hazed(col))
        faces = [tuple(range(m))]
        for lv in range(len(levels) - 1):
            a, b = lv * m, (lv + 1) * m
            faces += [(a + i, b + i, b + (i + 1) % m, a + (i + 1) % m) for i in range(m)]
        out.append((verts, faces, cols))
    return out


# ---------- other operators' pads around the lease ----------

def neighbors(bounds, seed, count=10):
    """Other leases' well pads past the ends of this one, each with a spur road to the lease
    road's line, the way a Permian section fills in. Pumpjacks and small tanks stand on them as
    far silhouettes. Deterministic for a seed.

    Returns [{"rect": (x0, x1, y0, y1), "jacks": [(x, y, yaw)], "tanks": [(x, y)],
    "spur": (x, y_road, y_pad)}]. Pads sit 300 m to 2.4 km past the lease's ends and 120 to
    800 m off the road, so none is on the lease, the road, or another pad.
    """
    rng = random.Random(f"freeboard-neighbors:{seed}")
    x0, x1, y0, y1 = bounds
    out = []
    for k in range(count):
        east, north = k % 2 == 0, (k // 2) % 2 == 0
        for _ in range(20):
            along = rng.uniform(300, 2400)
            x = x1 + along if east else x0 - along
            y = (1 if north else -1) * rng.uniform(120, 800)
            w, d = rng.uniform(30, 48), rng.uniform(34, 52)
            rect = (x - w / 2, x + w / 2, y - d / 2, y + d / 2)
            if all(abs(x - (o["rect"][0] + o["rect"][1]) / 2) > 90 for o in out):
                break
        s = 1 if north else -1
        jacks = [(x + (i - 0.5 * (n - 1)) * 12.0, y + s * d * 0.18, rng.uniform(-0.2, 0.2) + (0 if north else math.pi))
                 for n in [rng.choice((1, 2, 2, 3))] for i in range(n)]
        tanks = [(x + (i - 0.5) * 5.0 - w * 0.25, y - s * d * 0.22) for i in range(2)] if rng.random() < 0.6 else []
        out.append({"rect": rect, "jacks": jacks, "tanks": tanks, "spur": (x + rng.uniform(-0.3, 0.3) * w, 0.0,
                                                                             y - s * d / 2)})
    return out
