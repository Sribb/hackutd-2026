"""Read a glb's JSON chunk for the tests: node names, world matrices, bounding boxes.

Pure Python, no Blender. Positions come back in plan coordinates: three.js (x, height, -y)
is plan (x, y) at that height.
"""
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
    """Node names, world positions in plan coordinates, and world bounding boxes."""

    def __init__(self, path):
        self.g = g = read_glb(path)
        self.nodes = g["nodes"]
        self.by_name = {}
        for i, nd in enumerate(self.nodes):
            self.by_name.setdefault(nd.get("name", ""), []).append(i)
        self.parent = {c: i for i, nd in enumerate(self.nodes) for c in nd.get("children", [])}
        self.world = {}
        for root in g["scenes"][g.get("scene", 0)]["nodes"]:
            self._walk(root, [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        self.tris = self._tris()

    def _tris(self):
        acc = self.g["accessors"]
        per_mesh = [sum(acc[p["indices"]]["count"] // 3 for p in m["primitives"]) for m in self.g["meshes"]]
        return sum(per_mesh[nd["mesh"]] for nd in self.nodes if "mesh" in nd)

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
        pts = []
        stack = [self.idx(name)]
        while stack:
            i = stack.pop()
            stack += self.nodes[i].get("children", [])
            if "mesh" in self.nodes[i]:
                m = self.world[i]
                for p in self.g["meshes"][self.nodes[i]["mesh"]]["primitives"]:
                    a = self.g["accessors"][p["attributes"]["POSITION"]]
                    lo, hi = a["min"], a["max"]
                    for c in range(8):
                        v = [(hi if c >> k & 1 else lo)[k] for k in range(3)] + [1]
                        w = [sum(m[r][k] * v[k] for k in range(4)) for r in range(3)]
                        pts.append((w[0], -w[2], w[1]))
        if not pts:
            return None
        return (min(p[0] for p in pts), max(p[0] for p in pts), min(p[1] for p in pts),
                max(p[1] for p in pts), min(p[2] for p in pts), max(p[2] for p in pts))


def touch(a, b, grow=0.0):
    return a[0] - grow < b[1] and b[0] - grow < a[1] and a[2] - grow < b[3] and b[2] - grow < a[3]
