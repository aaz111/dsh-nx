"""Body-fitted ("draped") hexahedral mesh.

A voxel mesh represents the part with a staircase: every curved face, every hole and every
chamfer becomes a pile of axis-aligned steps, and the boundary nodes sit half a voxel away
from the real surface. This module pushes those boundary nodes onto the STL surface, so the
outside of the mesh follows the geometry that was modelled -- the elements stay hexahedra
(no change to the deck or to the solver), but the skin is no longer stepped.

What it does, in order:
  1. find the outer skin of the voxel mesh (faces used by one element only) and the nodes on it;
  2. project every skin node onto the closest point of the STL surface, but only by up to
     `reach` * element size, so a thin wall is never collapsed;
  3. revert any single-node move that would invert an adjacent element;
  4. optionally relax the skin (Laplacian, projected back onto the surface) so the elements
     next to the boundary do not become needles.

The result is checked by volume (against the CAD volume the caller passes in) and by the
signed volume of every element: an inverted element would make Nastran complain about a
non-positive Jacobian, so the mesh is refused here instead.
"""

import math

import numpy as np

HEX_FACES = ((0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7))

# Six tetrahedra per hexahedron, around the 0-6 body diagonal. A five-tet split needs a face
# diagonal in this corner order and covers only 5/6 of the volume, silently losing a sixth of
# the mesh -- measured on the unit cube before this was wired in.
HEX_TETS = ((0, 1, 2, 6), (0, 2, 3, 6), (0, 3, 7, 6), (0, 7, 4, 6), (0, 4, 5, 6), (0, 5, 1, 6))


def _skin_nodes(mesh):
    """Node ids on the outer skin, plus the node -> cells map."""
    seen = {}
    for index, cell in enumerate(mesh["cells"]):
        for face in HEX_FACES:
            quad = [cell[i] for i in face]
            key = tuple(sorted(quad))
            entry = seen.get(key)
            if entry is None:
                seen[key] = [1, index, quad]
            else:
                entry[0] += 1
    skin = set()
    for count, _index, quad in seen.values():
        if count == 1:
            skin.update(quad)
    by_node = {}
    for index, cell in enumerate(mesh["cells"]):
        for nid in cell:
            by_node.setdefault(nid, []).append(index)
    return sorted(skin), by_node


def _closest_point(point, a, b, c):
    """Closest point on triangle (a, b, c) to `point` -- the standard clamped barycentric."""
    ab = b - a
    ac = c - a
    ap = point - a
    d1 = float(np.dot(ab, ap))
    d2 = float(np.dot(ac, ap))
    if d1 <= 0.0 and d2 <= 0.0:
        return a
    bp = point - b
    d3 = float(np.dot(ab, bp))
    d4 = float(np.dot(ac, bp))
    if d3 >= 0.0 and d4 <= d3:
        return b
    vc = d1 * d4 - d3 * d2
    if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
        v = d1 / (d1 - d3) if (d1 - d3) else 0.0
        return a + ab * v
    cp = point - c
    d5 = float(np.dot(ab, cp))
    d6 = float(np.dot(ac, cp))
    if d6 >= 0.0 and d5 <= d6:
        return c
    vb = d5 * d2 - d1 * d6
    if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
        w = d2 / (d2 - d6) if (d2 - d6) else 0.0
        return a + ac * w
    va = d3 * d6 - d5 * d4
    if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
        denominator = (d4 - d3) + (d5 - d6)
        w = (d4 - d3) / denominator if denominator else 0.0
        return b + (c - b) * w
    denominator = va + vb + vc
    if not denominator:
        return a
    v = vb / denominator
    w = vc / denominator
    return a + ab * v + ac * w


def _triangle_grid(triangles, cell):
    """Bucket the triangles so a node only tests the ones near it."""
    buckets = {}
    for index, tri in enumerate(triangles):
        low = np.floor(tri.min(axis=0) / cell).astype(int)
        high = np.floor(tri.max(axis=0) / cell).astype(int)
        for i in range(low[0], high[0] + 1):
            for j in range(low[1], high[1] + 1):
                for k in range(low[2], high[2] + 1):
                    buckets.setdefault((i, j, k), []).append(index)
    return buckets


def _nearest_surface(point, buckets, triangles, cell, reach):
    """Closest surface point within `reach`, or None."""
    key = tuple(np.floor(point / cell).astype(int))
    best = None
    best_distance = reach * reach
    for i in (key[0] - 1, key[0], key[0] + 1):
        for j in (key[1] - 1, key[1], key[1] + 1):
            for k in (key[2] - 1, key[2], key[2] + 1):
                for index in buckets.get((i, j, k), ()):
                    tri = triangles[index]
                    candidate = _closest_point(point, tri[0], tri[1], tri[2])
                    delta = candidate - point
                    distance = float(np.dot(delta, delta))
                    if distance < best_distance:
                        best_distance = distance
                        best = candidate
    return best


def hex_volume(points):
    """Signed volume of one hexahedron from its eight corner points."""
    total = 0.0
    for a, b, c, d in HEX_TETS:
        pa, pb, pc, pd = points[a], points[b], points[c], points[d]
        total += float(np.dot(np.cross(pb - pa, pc - pa), pd - pa)) / 6.0
    return total


def mesh_volume(mesh):
    coords = {nid: np.array((x, y, z), dtype=np.float64) for nid, x, y, z in mesh["nodes"]}
    total = 0.0
    for cell in mesh["cells"]:
        total += abs(hex_volume([coords[nid] for nid in cell]))
    return total


def _would_invert(mesh, coords, nid, target, by_node):
    """True when moving `nid` to `target` flips the sign of any adjacent element."""
    trial = dict(coords)
    trial[nid] = target
    for index in by_node.get(nid, ()):
        cell = mesh["cells"][index]
        volume = hex_volume([trial[key] for key in cell])
        original = hex_volume([coords[key] for key in cell])
        if volume * original <= 0.0 or abs(volume) < 0.05 * abs(original):
            return True
    return False


def drape(mesh, triangles, reach=0.85, relax=1, target_volume=None):
    """Project the skin of a voxel mesh onto the surface; returns (mesh, report)."""
    triangles = np.asarray(triangles, dtype=np.float64)
    if triangles.ndim != 3 or triangles.shape[1:] != (3, 3):
        triangles = triangles.reshape((-1, 3, 3))
    coords = {nid: np.array((x, y, z), dtype=np.float64) for nid, x, y, z in mesh["nodes"]}
    skin, by_node = _skin_nodes(mesh)
    if not skin or len(triangles) == 0:
        return mesh, {"draped": False, "reason": "no skin or no triangles"}
    # Sides, load faces and the fixed set stay keyed to the CAD-aligned box: after a drape the
    # boundary nodes sit a fraction off their nominal plane, and a tight side test would then
    # silently drop nodes (measured: the fixed set went 224 -> 156 without this).
    mesh["original"] = [(nid, x, y, z) for nid, x, y, z in mesh["nodes"]]
    spacing = float(mesh.get("elementSize") or 1.0)
    cell = spacing
    buckets = _triangle_grid(triangles, cell)
    before = mesh_volume(mesh)

    moved = 0
    for nid in skin:
        point = coords[nid]
        candidate = _nearest_surface(point, buckets, triangles, cell, spacing * reach)
        if candidate is None:
            continue
        if float(np.linalg.norm(candidate - point)) < 1e-9:
            continue
        if _would_invert(mesh, coords, nid, candidate, by_node):
            continue
        coords[nid] = candidate
        moved += 1

    for _round in range(int(relax)):
        updated = dict(coords)
        for nid in skin:
            neighbours = set()
            for index in by_node.get(nid, ()):
                neighbours.update(mesh["cells"][index])
            neighbours.discard(nid)
            if not neighbours:
                continue
            average = np.mean([coords[key] for key in neighbours], axis=0)
            blend = coords[nid] * 0.5 + average * 0.5
            candidate = _nearest_surface(blend, buckets, triangles, cell, spacing * reach)
            if candidate is None:
                continue
            if _would_invert(mesh, coords, nid, candidate, by_node):
                continue
            updated[nid] = candidate
        coords = updated

    position = {entry[0]: index for index, entry in enumerate(mesh["nodes"])}
    for nid, point in coords.items():
        mesh["nodes"][position[nid]] = (nid, float(point[0]), float(point[1]), float(point[2]))

    after = mesh_volume(mesh)
    report = {"draped": True, "skinNodes": len(skin), "moved": moved,
              "volumeBefore": round(before, 3), "volumeAfter": round(after, 3),
              "relaxRounds": int(relax)}
    if target_volume:
        report["volumeTarget"] = round(float(target_volume), 3)
        report["errorBefore"] = round(100.0 * (before - float(target_volume)) / float(target_volume), 2)
        report["errorAfter"] = round(100.0 * (after - float(target_volume)) / float(target_volume), 2)
    points = {nid: np.array((x, y, z), dtype=np.float64) for nid, x, y, z in mesh["nodes"]}
    volumes = [hex_volume([points[key] for key in cell]) for cell in mesh["cells"]]
    reference = volumes[0] if volumes else 1.0
    inverted = sum(1 for value in volumes if value * reference <= 0.0)
    report["invertedElements"] = inverted
    return mesh, report
