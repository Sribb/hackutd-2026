"""Write a glb in plain Python: kit meshes copied in once and shared, plus generated meshes.

Standard library only, so blender/assemble_glb.py runs where there is no Blender. The
companion of glb_read.py.

Geometry is built in Blender axes (x east, y north, z up, meters) and turned into glTF axes
(x, z, -y) when it is written, the same way Blender's exporter does with +Y Up. Quaternions
follow the same rule: Blender (w, x, y, z) is glTF [x, z, -y, w].
"""
import json
import math
import struct
from array import array

ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER = 34962, 34963
COMPONENTS = {5126: "f", 5125: "I", 5123: "H", 5121: "B"}
WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


# ---------- reading ----------

def read_glb(path):
    """(json, bin bytes) of a glb."""
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:4] != b"glTF":
        raise ValueError(f"{path} is not a glb")
    n, kind = struct.unpack_from("<II", raw, 12)
    if kind != 0x4E4F534A:
        raise ValueError(f"{path} has no JSON chunk first")
    g = json.loads(raw[20:20 + n])
    rest = 20 + n
    binary = b""
    if rest + 8 <= len(raw):
        m, _ = struct.unpack_from("<II", raw, rest)
        binary = raw[rest + 8:rest + 8 + m]
    return g, binary


def accessor(g, binary, i):
    """An accessor's values as a flat array (tightly packed views only, as Blender writes them)."""
    a = g["accessors"][i]
    bv = g["bufferViews"][a["bufferView"]]
    code = COMPONENTS[a["componentType"]]
    out = array(code)
    size = a["count"] * WIDTH[a["type"]] * out.itemsize
    if bv.get("byteStride") not in (None, WIDTH[a["type"]] * out.itemsize):
        raise ValueError("interleaved buffer views are not supported")
    start = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    out.frombytes(binary[start:start + size])
    return out


# ---------- math ----------

def to_gltf(p):
    return (p[0], p[2], -p[1])


def from_gltf(p):
    return (p[0], -p[2], p[1])


def quat_mul(a, b):
    """Hamilton product of (w, x, y, z) quaternions."""
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (aw * bw - ax * bx - ay * by - az * bz, aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx, aw * bz + ax * by - ay * bx + az * bw)


def quat_axis(axis, angle):
    s = math.sin(angle / 2)
    return (math.cos(angle / 2), *(s * c for c in axis))


def euler_xyz(rx, ry, rz):
    """Blender's XYZ Euler as a (w, x, y, z) quaternion."""
    return quat_mul(quat_axis((0, 0, 1), rz), quat_mul(quat_axis((0, 1, 0), ry), quat_axis((1, 0, 0), rx)))


def gltf_quat(q):
    """A Blender-axes (w, x, y, z) quaternion as a glTF [x, y, z, w] rotation."""
    w, x, y, z = q
    return [x, z, -y, w]


def quat_from_matrix(m):
    """(w, x, y, z) from a 3x3 rotation matrix given as rows."""
    tr = m[0][0] + m[1][1] + m[2][2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        return (0.25 * s, (m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s)
    i = max(range(3), key=lambda k: m[k][k])
    j, k = (i + 1) % 3, (i + 2) % 3
    s = math.sqrt(1.0 + m[i][i] - m[j][j] - m[k][k]) * 2
    q = [0.0, 0.0, 0.0, 0.0]
    q[0] = (m[k][j] - m[j][k]) / s
    q[1 + i] = 0.25 * s
    q[1 + j] = (m[j][i] + m[i][j]) / s
    q[1 + k] = (m[k][i] + m[i][k]) / s
    return tuple(q)


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def unit(a):
    n = math.sqrt(dot(a, a))
    return scale(a, 1.0 / n) if n > 1e-12 else (0.0, 0.0, 1.0)


def look_at(eye, target):
    """glTF rotation [x, y, z, w] for a camera at eye (glTF axes) looking at target, +Y up."""
    f = unit(sub(target, eye))
    r = unit(cross(f, (0.0, 1.0, 0.0)))
    u = cross(r, f)
    rows = [[r[0], u[0], -f[0]], [r[1], u[1], -f[1]], [r[2], u[2], -f[2]]]
    w, x, y, z = quat_from_matrix(rows)
    return [x, y, z, w]


# ---------- meshes ----------

class Mesh:
    """Triangles in Blender axes with per-vertex normals, wound counterclockwise seen from outside.

    The primitives are wound right by construction. tri() and quad() check against the
    vertex normals instead, for anything built by hand.
    """

    def __init__(self):
        self.p, self.n, self.i = array("f"), array("f"), array("I")

    def __len__(self):
        return len(self.p) // 3

    @property
    def tris(self):
        return len(self.i) // 3

    def vert(self, p, n):
        self.p.extend(p)
        self.n.extend(n)
        return len(self) - 1

    def tri(self, a, b, c):
        p, n = self.p, self.n
        pa, pb, pc = (p[3 * a:3 * a + 3], p[3 * b:3 * b + 3], p[3 * c:3 * c + 3])
        g = cross(sub(pb, pa), sub(pc, pa))
        hint = (n[3 * a] + n[3 * b] + n[3 * c], n[3 * a + 1] + n[3 * b + 1] + n[3 * c + 1],
                n[3 * a + 2] + n[3 * b + 2] + n[3 * c + 2])
        self.i.extend((a, c, b) if dot(g, hint) < 0 else (a, b, c))

    def quad(self, a, b, c, d):
        self.tri(a, b, c)
        self.tri(a, c, d)

    def box(self, size, center, rot_z=0.0):
        hx, hy, hz = size[0] / 2, size[1] / 2, size[2] / 2
        cs, sn = math.cos(rot_z), math.sin(rot_z)

        def w(x, y, z):
            return (center[0] + cs * x - sn * y, center[1] + sn * x + cs * y, center[2] + z)

        def wn(x, y, z):
            return (cs * x - sn * y, sn * x + cs * y, z)

        half = (hx, hy, hz)
        for axis in range(3):
            u, v = [(1, 2), (0, 2), (0, 1)][axis]
            for s in (-1, 1):
                nrm = [0.0, 0.0, 0.0]
                nrm[axis] = float(s)
                corners = []
                for du, dv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                    c = [0.0, 0.0, 0.0]
                    c[axis], c[u], c[v] = s * half[axis], du * half[u], dv * half[v]
                    corners.append(self.vert(w(*c), wn(*nrm)))
                a, b, cc, d = corners
                # u x v points along +axis for x and z, and along -axis for y
                flip = (s > 0) if axis == 1 else (s < 0)
                self.i.extend((a, cc, b, a, d, cc) if flip else (a, b, cc, a, cc, d))

    def tube(self, a, b, r, segs=10, caps=False):
        d = sub(b, a)
        if dot(d, d) < 1e-8:
            return
        d = unit(d)
        helper = (1.0, 0.0, 0.0) if abs(d[2]) > 0.9 else (0.0, 0.0, 1.0)
        u = unit(cross(d, helper))
        v = cross(d, u)
        ring_a, ring_b = [], []
        for k in range(segs):
            t = 2 * math.pi * k / segs
            nrm = add(scale(u, math.cos(t)), scale(v, math.sin(t)))
            ring_a.append(self.vert(add(a, scale(nrm, r)), nrm))
            ring_b.append(self.vert(add(b, scale(nrm, r)), nrm))
        for k in range(segs):
            k2 = (k + 1) % segs
            self.i.extend((ring_a[k], ring_a[k2], ring_b[k2], ring_a[k], ring_b[k2], ring_b[k]))
        if caps:     # the ring runs counterclockwise about d, so the start cap is wound the other way
            for end, nrm, back in ((a, scale(d, -1), True), (b, d, False)):
                ring = [self.vert(add(end, scale(add(scale(u, math.cos(2 * math.pi * k / segs)),
                                                     scale(v, math.sin(2 * math.pi * k / segs))), r)), nrm)
                        for k in range(segs)]
                for k in range(1, segs - 1):
                    self.i.extend((ring[0], ring[k + 1], ring[k]) if back else (ring[0], ring[k], ring[k + 1]))

    def ball(self, c, r, segs=8):
        rows = max(4, segs // 2)
        grid = []
        for j in range(rows + 1):
            phi = math.pi * j / rows - math.pi / 2
            row = []
            for k in range(segs):
                t = 2 * math.pi * k / segs
                nrm = (math.cos(phi) * math.cos(t), math.cos(phi) * math.sin(t), math.sin(phi))
                row.append(self.vert(add(c, scale(nrm, r)), nrm))
            grid.append(row)
        for j in range(rows):
            for k in range(segs):
                k2 = (k + 1) % segs
                a, b, cc, d = grid[j][k], grid[j][k2], grid[j + 1][k2], grid[j + 1][k]
                if j > 0:
                    self.i.extend((a, b, cc))
                if j < rows - 1:
                    self.i.extend((a, cc, d))

    def pipe(self, points, radius, segs=10):
        """field_parts.Site.pipe: a tube along the points, balls at the bends, a flange at each end."""
        pts = []
        for p in points:
            if not pts or math.dist(p, pts[-1]) > 1e-3:
                pts.append(tuple(p))
        for p, q in zip(pts, pts[1:]):
            self.tube(p, q, radius, segs)
        for p in pts[1:-1]:
            self.ball(p, radius * 1.12, 6 if radius < 0.07 else 8)
        for end, nxt in ((pts[0], pts[1]), (pts[-1], pts[-2])):
            d = scale(unit(sub(nxt, end)), 0.03)
            self.tube(sub(end, d), add(end, d), radius * 1.7, segs, caps=True)

    def text(self, text, size, advance, glyph, line=1.0):
        """Flat text like detail_equipment.label(): each line centered, lines one unit apart.

        advance maps a character to its width at size 1, and glyph(ch) gives its triangles in
        glTF axes (centered on its advance), or None for a blank.
        """
        lines = text.split("\n")
        for k, row in enumerate(lines):
            adv = [advance.get(c, advance[" "]) for c in row]
            pen, y = -sum(adv) / 2, ((len(lines) - 1) / 2 - k) * line
            for c, a in zip(row, adv):
                g = glyph(c)
                if g:
                    self.merge(*g, size=size, offset=((pen + a / 2) * size, y * size, 0.0))
                pen += a

    def merge(self, positions, normals, indices, size=1.0, offset=(0.0, 0.0, 0.0)):
        """Append triangles given in glTF axes, scaled by size and moved by offset (Blender axes)."""
        base = len(self)
        for k in range(0, len(positions), 3):
            x, y, z = from_gltf(positions[k:k + 3])
            self.p.extend((x * size + offset[0], y * size + offset[1], z * size + offset[2]))
            self.n.extend(from_gltf(normals[k:k + 3]))
        self.i.extend(base + j for j in indices)


# ---------- writing ----------

class Writer:
    """One glb: shared kit meshes, generated meshes, nodes, materials, and one camera."""

    def __init__(self, generator):
        self.j = {"asset": {"generator": generator, "version": "2.0"}, "scene": 0,
                  "scenes": [{"name": "Field", "nodes": []}], "nodes": [], "cameras": [], "materials": [],
                  "meshes": [], "accessors": [], "bufferViews": [], "buffers": [{"byteLength": 0}]}
        self.bin = bytearray()
        self.materials = {}
        self.mesh_tris = []

    def _view(self, data, target):
        self.bin.extend(b"\0" * (-len(self.bin) % 4))
        self.j["bufferViews"].append({"buffer": 0, "byteLength": len(data), "byteOffset": len(self.bin),
                                      "target": target})
        self.bin.extend(data)
        return len(self.j["bufferViews"]) - 1

    def _accessor(self, view, comp, count, kind, lo=None, hi=None):
        acc = {"bufferView": view, "componentType": comp, "count": count, "type": kind}
        if lo is not None:
            acc["min"], acc["max"] = lo, hi
        self.j["accessors"].append(acc)
        return len(self.j["accessors"]) - 1

    def _append(self, key, item):
        self.j.setdefault(key, []).append(item)
        return len(self.j[key]) - 1

    def import_file(self, g, binary, meshes=True):
        """Copy a kit glb's materials, and its meshes unless meshes is False, sharing them from then on.

        Only the buffer views something uses are copied. A material is taken by name the first
        time it is seen, with its textures, samplers, and images, so a texture that every kit
        file carries lands in the output once. Returns {kit mesh index: output mesh index}.
        """
        views, accs, textures = {}, {}, {}

        def view(i):
            if i not in views:
                bv = g["bufferViews"][i]
                start = bv.get("byteOffset", 0)
                self.bin.extend(b"\0" * (-len(self.bin) % 4))
                spec = {k: v for k, v in bv.items() if k in ("byteStride", "target")}
                views[i] = self._append("bufferViews", dict(spec, buffer=0, byteOffset=len(self.bin),
                                                            byteLength=bv["byteLength"]))
                self.bin.extend(binary[start:start + bv["byteLength"]])
            return views[i]

        def texture(i):
            if i not in textures:
                t = dict(g["textures"][i])
                if "source" in t:
                    img = dict(g["images"][t["source"]])
                    if "bufferView" in img:
                        img["bufferView"] = view(img["bufferView"])
                    t["source"] = self._append("images", img)
                if "sampler" in t:
                    t["sampler"] = self._append("samplers", dict(g["samplers"][t["sampler"]]))
                textures[i] = self._append("textures", t)
            return textures[i]

        def remap(obj):
            """A material with its texture references moved to the output's textures."""
            if isinstance(obj, dict):
                return {k: (dict(remap(v), index=texture(v["index"])) if k.endswith("Texture") and
                            isinstance(v, dict) and "index" in v else remap(v)) for k, v in obj.items()}
            return [remap(v) for v in obj] if isinstance(obj, list) else obj

        mats = []
        for m in g.get("materials", []):
            if m.get("name") not in self.materials:
                self.materials[m.get("name")] = self._append("materials", remap(m))
            mats.append(self.materials[m.get("name")])
        for key in ("extensionsUsed", "extensionsRequired"):
            for ext in g.get(key, []):
                if ext not in self.j.setdefault(key, []):
                    self.j[key].append(ext)
        if not meshes:
            return {}

        def acc(i):
            if i not in accs:
                a = g["accessors"][i]
                accs[i] = self._append("accessors", dict(a, bufferView=view(a["bufferView"])))
            return accs[i]

        out = {}
        for i, m in enumerate(g.get("meshes", [])):
            prims = []
            for p in m["primitives"]:
                q = dict(p, attributes={k: acc(v) for k, v in p["attributes"].items()})
                if "indices" in p:
                    q["indices"] = acc(p["indices"])
                if "material" in p:
                    q["material"] = mats[p["material"]]
                prims.append(q)
            out[i] = self._append("meshes", dict(m, primitives=prims))
            self.mesh_tris.append(sum(g["accessors"][p["indices"]]["count"] // 3 for p in m["primitives"]))
        return out

    def mesh(self, name, mesh, material):
        """Add a generated Mesh with one material. Returns the mesh index, or None if it is empty."""
        if not mesh.tris:
            return None
        pos = array("f", (c for k in range(0, len(mesh.p), 3) for c in to_gltf(mesh.p[k:k + 3])))
        nrm = array("f", (c for k in range(0, len(mesh.n), 3) for c in to_gltf(mesh.n[k:k + 3])))
        lo = [min(pos[k::3]) for k in range(3)]
        hi = [max(pos[k::3]) for k in range(3)]
        count = len(mesh)
        a_pos = self._accessor(self._view(pos.tobytes(), ARRAY_BUFFER), 5126, count, "VEC3", lo, hi)
        a_nrm = self._accessor(self._view(nrm.tobytes(), ARRAY_BUFFER), 5126, count, "VEC3")
        idx = array("H" if count < 65536 else "I", mesh.i)
        a_idx = self._accessor(self._view(idx.tobytes(), ELEMENT_ARRAY_BUFFER), 5123 if idx.typecode == "H" else 5125,
                               len(idx), "SCALAR")
        prim = {"attributes": {"POSITION": a_pos, "NORMAL": a_nrm}, "indices": a_idx,
                "material": self.materials[material]}
        self.j["meshes"].append({"name": name, "primitives": [prim]})
        self.mesh_tris.append(mesh.tris)
        return len(self.j["meshes"]) - 1

    def node(self, name, parent=None, loc=None, rot=None, scl=None, mesh=None, extras=None, camera=None):
        """Add a node; loc is glTF axes, rot a glTF [x, y, z, w]. parent None makes it a scene root."""
        nd = {"name": name}
        if loc is not None and any(abs(c) > 1e-9 for c in loc):
            nd["translation"] = [float(c) for c in loc]
        if rot is not None and (abs(rot[3]) < 1 - 1e-9 or any(abs(c) > 1e-9 for c in rot[:3])):
            nd["rotation"] = [float(c) for c in rot]
        if scl is not None and any(abs(c - 1) > 1e-9 for c in scl):
            nd["scale"] = [float(c) for c in scl]
        if mesh is not None:
            nd["mesh"] = mesh
        if camera is not None:
            nd["camera"] = camera
        if extras:
            nd["extras"] = extras
        self.j["nodes"].append(nd)
        i = len(self.j["nodes"]) - 1
        if parent is None:
            self.j["scenes"][0]["nodes"].append(i)
        else:
            self.j["nodes"][parent].setdefault("children", []).append(i)
        return i

    def triangles(self):
        return sum(self.mesh_tris[nd["mesh"]] for nd in self.j["nodes"] if "mesh" in nd)

    def write(self, path):
        self.bin.extend(b"\0" * (-len(self.bin) % 4))
        self.j["buffers"][0]["byteLength"] = len(self.bin)
        for key in ("cameras", "materials"):
            if not self.j[key]:
                del self.j[key]
        text = json.dumps(self.j, separators=(",", ":")).encode()
        text += b" " * (-len(text) % 4)
        total = 12 + 8 + len(text) + 8 + len(self.bin)
        with open(path, "wb") as f:
            f.write(struct.pack("<III", 0x46546C67, 2, total))
            f.write(struct.pack("<II", len(text), 0x4E4F534A))
            f.write(text)
            f.write(struct.pack("<II", len(self.bin), 0x004E4942))
            f.write(self.bin)
        return total
