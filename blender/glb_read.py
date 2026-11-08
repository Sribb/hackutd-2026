"""Read a glb's JSON chunk for the tests: node names, world matrices, bounding boxes.

Pure Python, no Blender. Positions come back in plan coordinates: three.js (x, height, -y)
is plan (x, y) at that height.
"""
import hashlib
import json
import struct


def read_glb(path):
    with open(path, "rb") as f:
        raw = f.read()
    n, kind = struct.unpack_from("<II", raw, 12)
    assert raw[:4] == b"glTF" and kind == 0x4E4F534A, "not a glb"
    return json.loads(raw[20:20 + n])


def mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def local_matrix(node):
    if "matrix" in node:
        m = node["matrix"]
        return [[m[c * 4 + r] for c in range(4)] for r in range(4)]
    tx, ty, tz = node.get("translation", (0, 0, 0))
    qx, qy, qz, qw = node.get("rotation", (0, 0, 0, 1))
    sx, sy, sz = node.get("scale", (1, 1, 1))
    r = [[1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
         [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
         [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)]]
    s = (sx, sy, sz)
    return [[r[i][0] * s[0], r[i][1] * s[1], r[i][2] * s[2], (tx, ty, tz)[i]] for i in range(3)] + [[0, 0, 0, 1]]


class Glb:
    """Node names, world positions in plan coordinates, and world bounding boxes.

    A node with EXT_mesh_gpu_instancing (#97) draws its mesh once per instance: tris counts
    every instance, instances[i] holds each one's local matrix, and box() covers them all."""

    def __init__(self, path):
        self.g = g = read_glb(path)
        self.bin = None
        self._path = path
        self.nodes = g["nodes"]
        self.by_name = {}
        for i, nd in enumerate(self.nodes):
            self.by_name.setdefault(nd.get("name", ""), []).append(i)
        self.parent = {c: i for i, nd in enumerate(self.nodes) for c in nd.get("children", [])}
        self.instances = {i: self._instance_matrices(nd) for i, nd in enumerate(self.nodes)
                          if "EXT_mesh_gpu_instancing" in nd.get("extensions", {})}
        self.world = {}
        for root in g["scenes"][g.get("scene", 0)]["nodes"]:
            self._walk(root, [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        self.tris = self._tris()

    def _view(self, accessor_idx):
        """(binary chunk, byte offset) of an accessor's data."""
        if self.bin is None:
            with open(self._path, "rb") as f:
                raw = f.read()
            off = 20 + struct.unpack_from("<I", raw, 12)[0]
            self.bin = raw[off + 8: off + 8 + struct.unpack_from("<I", raw, off)[0]]
        a = self.g["accessors"][accessor_idx]
        v = self.g["bufferViews"][a["bufferView"]]
        return v, v.get("byteOffset", 0) + a.get("byteOffset", 0)

    def _floats(self, accessor_idx, width):
        """An accessor's float data as rows of width. Instancing data is plain float32."""
        a = self.g["accessors"][accessor_idx]
        _, start = self._view(accessor_idx)
        assert a["componentType"] == 5126, "instance data must be float32"
        vals = struct.unpack_from(f"<{a['count'] * width}f", self.bin, start)
        return [vals[k * width:(k + 1) * width] for k in range(a["count"])]

    def duplicate_meshes(self):
        """How many single-primitive meshes repeat another one byte for byte (same material too)."""
        seen, dupes = set(), 0
        for m in self.g["meshes"]:
            if len(m["primitives"]) != 1:
                continue
            p = m["primitives"][0]
            h = hashlib.sha1(repr((p.get("material"), sorted(p["attributes"]))).encode())
            for i in [p["attributes"][k] for k in sorted(p["attributes"])] + [p["indices"]]:
                v, start = self._view(i)
                end = v.get("byteOffset", 0) + v["byteLength"]
                h.update(self.bin[start:end])
            key = h.hexdigest()
            dupes += key in seen
            seen.add(key)
        return dupes

    def _instance_matrices(self, nd):
        at = nd["extensions"]["EXT_mesh_gpu_instancing"]["attributes"]
        n = self.g["accessors"][next(iter(at.values()))]["count"]
        t = self._floats(at["TRANSLATION"], 3) if "TRANSLATION" in at else [(0, 0, 0)] * n
        r = self._floats(at["ROTATION"], 4) if "ROTATION" in at else [(0, 0, 0, 1)] * n
        s = self._floats(at["SCALE"], 3) if "SCALE" in at else [(1, 1, 1)] * n
        return [local_matrix({"translation": t[k], "rotation": r[k], "scale": s[k]}) for k in range(n)]

    def _tris(self):
        acc = self.g["accessors"]
        per_mesh = [sum(acc[p["indices"]]["count"] // 3 for p in m["primitives"]) for m in self.g["meshes"]]
        return sum(per_mesh[nd["mesh"]] * len(self.instances.get(i, [None])) for i, nd in enumerate(self.nodes)
                   if "mesh" in nd)

    def instance_boxes(self, name):
        """World AABB of each instance of an instanced node, in plan coordinates."""
        i = self.idx(name)
        return [self._mesh_box(i, mat_mul(self.world[i], m)) for m in self.instances.get(i, [])]

    def _mesh_box(self, i, m):
        pts = []
        for p in self.g["meshes"][self.nodes[i]["mesh"]]["primitives"]:
            a = self.g["accessors"][p["attributes"]["POSITION"]]
            lo, hi = a["min"], a["max"]
            for c in range(8):
                v = [(hi if c >> k & 1 else lo)[k] for k in range(3)] + [1]
                w = [sum(m[r][k] * v[k] for k in range(4)) for r in range(3)]
                pts.append((w[0], -w[2], w[1]))
        return (min(p[0] for p in pts), max(p[0] for p in pts), min(p[1] for p in pts),
                max(p[1] for p in pts), min(p[2] for p in pts), max(p[2] for p in pts))

    def _walk(self, i, parent):
        self.world[i] = m = mat_mul(parent, local_matrix(self.nodes[i]))
        for c in self.nodes[i].get("children", []):
            self._walk(c, m)

    def idx(self, name):
        return self.by_name[name][0]

    def plan_xy(self, name):
        m = self.world[self.idx(name)]
        return (m[0][3], -m[2][3])

    def box(self, name):
        """World AABB of a node and its children, in plan coordinates (x0, x1, y0, y1, z0, z1)."""
        boxes = []
        stack = [self.idx(name)]
        while stack:
            i = stack.pop()
            stack += self.nodes[i].get("children", [])
            if "mesh" in self.nodes[i]:
                for m in self.instances.get(i, [None]):
                    boxes.append(self._mesh_box(i, self.world[i] if m is None else mat_mul(self.world[i], m)))
        if not boxes:
            return None
        return (min(b[0] for b in boxes), max(b[1] for b in boxes), min(b[2] for b in boxes),
                max(b[3] for b in boxes), min(b[4] for b in boxes), max(b[5] for b in boxes))


def touch(a, b, grow=0.0):
    return a[0] - grow < b[1] and b[0] - grow < a[1] and a[2] - grow < b[3] and b[2] - grow < a[3]
