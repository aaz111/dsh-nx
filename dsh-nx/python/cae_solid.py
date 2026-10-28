"""Solid FE pipeline for the dsh-nx plugin.

Geometry comes from the part's own mesh (the STL the exporter writes), not from a box:
the solid is voxelised at the requested element size, cells whose centre is inside the
closed mesh are kept, and those hexahedra go into an NX Nastran deck. That gives a real
body mesh of whatever shape the part has, with pressure / moment / gravity / centrifugal
loads on top and SOL 103 modes next to SOL 101 statics.

Standalone: python cae_solid.py <spec.json> <workdir> [result.json]
"""

import json
import math
import os
import re
import struct
import subprocess
import sys
import time

from cae_run import NASTRAN, card_lines, fmt, parse_f06

try:                      # numpy / Pillow live in the same runtime; plotting is optional
    import cae_plot
except Exception:         # pragma: no cover - a run without pictures is still a result
    cae_plot = None
try:                      # numpy is shared with the mesh draper
    import cae_drape
except Exception:         # pragma: no cover - a plain voxel mesh is still a valid answer
    cae_drape = None

RHO_STEEL = 7.85e-9   # tonne/mm^3, the consistent unit set for mm / N / MPa / s
GRAVITY = 9810.0      # mm/s^2
# Simcenter Nastran asks for ~14 GB of open core by default, which this machine cannot give
# while NX itself is resident, and then refuses to even read the deck. Cap it explicitly.
MEMORY = os.environ.get("NXC_NASTRAN_MEM") or "2000mb"


# ---------------------------------------------------------------- STL input
def read_stl(path):
    """Binary or ASCII STL -> list of triangles, each three (x, y, z) tuples."""
    with open(path, "rb") as handle:
        data = handle.read()
    triangles = []
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if 84 + count * 50 == len(data):
            for index in range(count):
                base = 84 + index * 50 + 12
                corners = [struct.unpack_from("<3f", data, base + 12 * c) for c in range(3)]
                triangles.append(tuple(tuple(float(v) for v in corner) for corner in corners))
            return triangles
    text = data.decode("ascii", "replace")
    current = []
    for match in re.finditer(r"vertex\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)", text):
        current.append(tuple(float(match.group(i)) for i in (1, 2, 3)))
        if len(current) == 3:
            triangles.append(tuple(current))
            current = []
    return triangles


def mesh_box(triangles):
    xs = [corner[0] for tri in triangles for corner in tri]
    ys = [corner[1] for tri in triangles for corner in tri]
    zs = [corner[2] for tri in triangles for corner in tri]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


# ---------------------------------------------------------------- voxel mesh
def build_columns(triangles, yc, zc):
    """x-crossings per (y, z) column of CELL CENTRES, keyed by cell index.

    Two traps live here and both were hit for real on a 100x20x20 bar:
    * sampling on grid corners runs the ray along face edges, which drop whole boundary
      layers of cells (280 of 2560 cells vanished);
    * an STL quad is two triangles and a sample that lands on their shared diagonal is
      counted twice, flipping the in/out parity for that column (the bar end face splits
      along y == z, and cell centres sit exactly there).
    So samples are jittered off the lattice and the crossings of each column are
    de-duplicated before the parity is counted.
    """
    columns = {}
    ny = len(yc)
    nz = len(zc)
    for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in triangles:
        if abs((by - ay) * (cz - az) - (bz - az) * (cy - ay)) < 1e-9:
            continue
        y0, y1 = min(ay, by, cy), max(ay, by, cy)
        z0, z1 = min(az, bz, cz), max(az, bz, cz)
        det = (by - ay) * (cz - az) - (bz - az) * (cy - ay)
        for j in range(ny):
            py = yc[j]
            if py < y0 - 1e-9 or py > y1 + 1e-9:
                continue
            for k in range(nz):
                pz = zc[k]
                if pz < z0 - 1e-9 or pz > z1 + 1e-9:
                    continue
                u = ((py - ay) * (cz - az) - (pz - az) * (cy - ay)) / det
                v = ((by - ay) * (pz - az) - (bz - az) * (py - ay)) / det
                w = 1.0 - u - v
                if u < -1e-9 or v < -1e-9 or w < -1e-9:
                    continue
                x = w * ax + u * bx + v * cx
                columns.setdefault((j, k), []).append(x)
    for key, values in columns.items():
        values.sort()
        cleaned = [values[0]]
        for value in values[1:]:
            if abs(value - cleaned[-1]) > 1e-6:
                cleaned.append(value)
        columns[key] = cleaned
    return columns


def mesh_solid(triangles, element_size, cap=60000):
    """Voxelise the closed mesh: keep the cells whose centre is inside it."""
    low, high = mesh_box(triangles)
    extent = [high[i] - low[i] for i in range(3)]
    spacing = float(element_size)
    counts = [max(1, int(round(extent[i] / spacing))) for i in range(3)]
    while counts[0] * counts[1] * counts[2] > cap:
        spacing *= 1.15
        counts = [max(1, int(round(extent[i] / spacing))) for i in range(3)]
    steps = [extent[i] / counts[i] for i in range(3)]
    coords = [[low[i] + steps[i] * index for index in range(counts[i] + 1)] for i in range(3)]

    columns = build_columns(triangles,
                            [low[1] + steps[1] * (index + 0.5 + 0.00037 * (index + 1))
                             for index in range(counts[1])],
                            [low[2] + steps[2] * (index + 0.5 + 0.00061 * (index + 1))
                             for index in range(counts[2])])
    keep = []
    for i in range(counts[0]):
        xc = low[0] + steps[0] * (i + 0.5)
        for j in range(counts[1]):
            for k in range(counts[2]):
                hits = columns.get((j, k))
                if not hits:
                    continue
                crossed = 0
                for value in hits:
                    if value < xc:
                        crossed += 1
                    else:
                        break
                if crossed % 2 == 1:
                    keep.append((i, j, k))
    if not keep:
        raise RuntimeError("no cell of the %.4g mm grid fell inside the solid -- the mesh is "
                           "open or the element size is too coarse" % spacing)

    node_index = {}
    nodes = []
    cells = []
    for (i, j, k) in keep:
        corners = []
        for di in (0, 1):
            for dj in (0, 1):
                for dk in (0, 1):
                    key = (i + di, j + dj, k + dk)
                    index = node_index.get(key)
                    if index is None:
                        index = len(nodes) + 1
                        node_index[key] = index
                        nodes.append((index, coords[0][key[0]], coords[1][key[1]], coords[2][key[2]]))
                    corners.append(index)
        # CHEXA wants the bottom quad, then the top quad, both counter-clockwise.
        order = [corners[0], corners[2], corners[6], corners[4],
                 corners[1], corners[3], corners[7], corners[5]]
        cells.append(order)

    return {
        "nodes": nodes,
        "cells": cells,
        "box": (low, high),
        "counts": counts,
        "spacing": [round(step, 6) for step in steps],
        "elementSize": round(spacing, 6),
    }


def surface_faces(mesh):
    """Outer quads of the hex mesh: faces shared by one cell only."""
    hex_faces = ((0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7))
    seen = {}
    for cell in mesh["cells"]:
        for face in hex_faces:
            quad = [cell[i] for i in face]
            key = tuple(sorted(quad))
            seen.setdefault(key, [0, quad])
            seen[key][0] += 1
    return [entry[1] for entry in seen.values() if entry[0] == 1]


# ---------------------------------------------------------------- selection
SIDES = {"-x": (0, 0), "+x": (0, 1), "-y": (1, 0), "+y": (1, 1), "-z": (2, 0), "+z": (2, 1)}
OUTWARD = {"-x": (-1, 0, 0), "+x": (1, 0, 0), "-y": (0, -1, 0),
           "+y": (0, 1, 0), "-z": (0, 0, -1), "+z": (0, 0, 1)}


def reference_nodes(mesh):
    """Positions before a drape: faces are picked on the CAD-aligned box, never on the wobble."""
    return mesh.get("original") or mesh["nodes"]


def nodes_on_side(mesh, side, tolerance=None):
    axis, which = SIDES[side]
    target = mesh["box"][1][axis] if which else mesh["box"][0][axis]
    tol = tolerance if tolerance is not None else 1e-6 + mesh["spacing"][axis] * 1e-3
    return [nid for nid, x, y, z in reference_nodes(mesh)
            if abs((x, y, z)[axis] - target) <= tol]


def quads_on_side(mesh, faces, side):
    axis, which = SIDES[side]
    target = mesh["box"][1][axis] if which else mesh["box"][0][axis]
    tol = 1e-6 + mesh["spacing"][axis] * 1e-3
    coords = {nid: (x, y, z) for nid, x, y, z in reference_nodes(mesh)}
    picked = []
    for quad in faces:
        if all(abs(coords[nid][axis] - target) <= tol for nid in quad):
            picked.append(quad)
    return picked


def quad_area(coords, quad):
    a, b, c, d = [coords[nid] for nid in quad]
    def tri(p, q, r):
        ux, uy, uz = q[0] - p[0], q[1] - p[1], q[2] - p[2]
        vx, vy, vz = r[0] - p[0], r[1] - p[1], r[2] - p[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        return 0.5 * math.sqrt(nx * nx + ny * ny + nz * nz)
    return tri(a, b, c) + tri(a, c, d)


# ---------------------------------------------------------------- deck
def build_loads(mesh, faces, spec):
    """Turn the spec's load list into (nodal forces, extra bulk cards, notes)."""
    coords = {nid: (x, y, z) for nid, x, y, z in reference_nodes(mesh)}
    forces = {}
    cards = []
    notes = []

    def add(nid, vector):
        previous = forces.get(nid, (0.0, 0.0, 0.0))
        forces[nid] = (previous[0] + vector[0], previous[1] + vector[1], previous[2] + vector[2])

    for load in spec.get("loads") or []:
        kind = str(load.get("type") or "force").lower()
        if kind == "force":
            side = str(load.get("face") or "+z").lower()
            direction = load.get("direction")
            if direction:
                vector = [float(v) for v in direction]
            else:
                vector = [float(v) for v in OUTWARD[side]]
                vector = [-v for v in vector]      # pressing on the face pushes inward
            length = math.sqrt(sum(v * v for v in vector)) or 1.0
            vector = [v / length for v in vector]
            targets = nodes_on_side(mesh, side)
            if not targets:
                raise RuntimeError("no node on face %s to apply the force to" % side)
            share = float(load.get("value", 0.0)) / len(targets)
            for nid in targets:
                add(nid, [v * share for v in vector])
            notes.append({"load": "force", "face": side, "value": float(load.get("value", 0.0)),
                          "direction": [round(v, 4) for v in vector], "nodes": len(targets)})
        elif kind == "pressure":
            side = str(load.get("face") or "+z").lower()
            quads = quads_on_side(mesh, faces, side)
            if not quads:
                raise RuntimeError("no outer face lies on %s for the pressure" % side)
            pressure = float(load.get("value", 0.0))
            outward = OUTWARD[side]
            total = 0.0
            for quad in quads:
                area = quad_area(coords, quad)
                total += area
                share = pressure * area / 4.0
                for nid in quad:
                    add(nid, [-outward[i] * share for i in range(3)])
            notes.append({"load": "pressure", "face": side, "value": pressure,
                          "area": round(total, 2), "quads": len(quads),
                          "totalForce": round(pressure * total, 2)})
        elif kind == "moment":
            side = str(load.get("face") or "+x").lower()
            axis = [float(v) for v in (load.get("axis") or [0.0, 0.0, 1.0])]
            length = math.sqrt(sum(v * v for v in axis)) or 1.0
            axis = [v / length for v in axis]
            targets = nodes_on_side(mesh, side)
            if len(targets) < 4:
                raise RuntimeError("a moment needs a face with nodes on both sides of its axis")
            centre = [sum(coords[nid][i] for nid in targets) / len(targets) for i in range(3)]
            # Split the face perpendicular to the moment axis; the arm axis must lie IN the
            # face, so choose between the two axes not parallel to the moment axis, taking
            # whichever actually spreads across these nodes.
            candidates = [i for i in range(3) if abs(axis[i]) < 0.9]
            if not candidates:
                raise RuntimeError("the moment axis %s leaves no arm direction" % (axis,))
            spread = {}
            for index in candidates:
                values = [coords[nid][index] for nid in targets]
                spread[index] = max(values) - min(values)
            arm_index = max(candidates, key=lambda i: spread[i])
            if spread[arm_index] < 1e-6:
                raise RuntimeError("the face %s is flat along every arm direction" % side)
            arm_axis = [0.0, 0.0, 0.0]
            arm_axis[arm_index] = 1.0
            positive, negative = [], []
            for nid in targets:
                offset = (coords[nid][0] - centre[0]) * arm_axis[0] + \
                         (coords[nid][1] - centre[1]) * arm_axis[1] + \
                         (coords[nid][2] - centre[2]) * arm_axis[2]
                (positive if offset >= 0 else negative).append(nid)
            if not positive or not negative:
                raise RuntimeError("could not split the face %s into two halves for the moment" % side)
            def centroid(group):
                return [sum(coords[nid][i] for nid in group) / len(group) for i in range(3)]
            cpos, cneg = centroid(positive), centroid(negative)
            arm = math.sqrt(sum((cpos[i] - cneg[i]) ** 2 for i in range(3)))
            if arm < 1e-6:
                raise RuntimeError("the moment's arm came out zero -- pick another face")
            direction = [axis[1] * arm_axis[2] - axis[2] * arm_axis[1],
                         axis[2] * arm_axis[0] - axis[0] * arm_axis[2],
                         axis[0] * arm_axis[1] - axis[1] * arm_axis[0]]
            magnitude = float(load.get("value", 0.0)) / arm
            for nid in positive:
                add(nid, [v * magnitude / len(positive) for v in direction])
            for nid in negative:
                add(nid, [-v * magnitude / len(negative) for v in direction])
            notes.append({"load": "moment", "face": side, "value": float(load.get("value", 0.0)),
                          "axis": [round(v, 4) for v in axis], "arm": round(arm, 3),
                          "coupleForce": round(magnitude, 3)})
        elif kind == "gravity":
            direction = [float(v) for v in (load.get("direction") or [0.0, 0.0, -1.0])]
            length = math.sqrt(sum(v * v for v in direction)) or 1.0
            direction = [v / length for v in direction]
            value = float(load.get("value") or GRAVITY)
            density = float((spec.get("material") or {}).get("rho", RHO_STEEL))
            spacing = mesh.get("spacing") or [1.0, 1.0, 1.0]
            volume = spacing[0] * spacing[1] * spacing[2]
            total = 0.0
            for cell in mesh["cells"]:
                share = density * value * volume / 8.0
                total += density * value * volume
                for nid in cell:
                    add(nid, [share * direction[i] for i in range(3)])
            notes.append({"load": "gravity", "value": value, "g": value,
                          "direction": [round(v, 4) for v in direction],
                          "totalForce": round(total, 3)})
        elif kind == "centrifugal":
            axis = [float(v) for v in (load.get("axis") or [0.0, 0.0, 1.0])]
            length = math.sqrt(sum(v * v for v in axis)) or 1.0
            axis = [v / length for v in axis]
            rpm = float(load.get("rpm") or load.get("omegaRpm") or load.get("value") or 0.0)
            if rpm <= 0:
                raise RuntimeError("a centrifugal load needs rpm (or omegaRpm / value) > 0")
            omega = 2.0 * math.pi * rpm / 60.0
            anchor = [float(v) for v in (load.get("origin") or [0.0, 0.0, 0.0])]
            density = float((spec.get("material") or {}).get("rho", RHO_STEEL))
            spacing = mesh.get("spacing") or [1.0, 1.0, 1.0]
            # Written as nodal forces on purpose: an RFORCE card asked for 5.6e4 mm of
            # deflection at 3000 rpm (a factor of ~2e7 out), and no amount of A-field
            # fiddling explained it, while rho * omega^2 * r * V is checkable by hand.
            volume = spacing[0] * spacing[1] * spacing[2]
            total = 0.0
            for cell in mesh["cells"]:
                corners = [coords[nid] for nid in cell]
                centre = [sum(c[i] for c in corners) / 8.0 for i in range(3)]
                rel = [centre[i] - anchor[i] for i in range(3)]
                along = sum(rel[i] * axis[i] for i in range(3))
                radial = [rel[i] - along * axis[i] for i in range(3)]
                radius = math.sqrt(sum(v * v for v in radial))
                if radius < 1e-9:
                    continue
                share = density * omega * omega * radius * volume / 8.0
                total += density * omega * omega * radius * volume
                vector = [share * radial[i] / radius for i in range(3)]
                for nid in cell:
                    add(nid, vector)
            notes.append({"load": "centrifugal", "rpm": rpm, "omega": round(omega, 4),
                          "axis": [round(v, 4) for v in axis],
                          "totalForce": round(total, 3)})
        else:
            raise RuntimeError("unknown load type %r" % kind)
    return forces, cards, notes


def write_deck(mesh, spec, path):
    nodes, cells = mesh["nodes"], mesh["cells"]
    material = spec.get("material") or {}
    young = float(material.get("E", 210000.0))
    poisson = float(material.get("nu", 0.3))
    density = float(material.get("rho", RHO_STEEL))
    faces = surface_faces(mesh)
    forces, extra, notes = build_loads(mesh, faces, spec)
    has_gravity = any(str(note.get("load")) == "gravity" for note in notes)

    fixed = []
    for side in spec.get("fix") or ["-x"]:
        fixed.extend(nodes_on_side(mesh, str(side).lower()))
    fixed = sorted(set(fixed))
    if not fixed:
        raise RuntimeError("no node is on the fixed face(s) %s" % (spec.get("fix"),))

    modal = str(spec.get("solver") or "statics").lower() == "modal"
    modes = int(spec.get("modes") or 6)
    lines = []
    if modal:
        lines += ["SOL 103", "CEND", "TITLE = NX NASTRAN NORMAL MODES (dsh-nx)", "SUBCASE 1",
                  "  SPC = 1", "  METHOD = 1", "  DISPLACEMENT = ALL",
                  "BEGIN BULK", "EIGRL, 1, , , %d" % modes]
    else:
        # Nastran rejects one load set holding both GRAV and FORCE ("USER FATAL MESSAGE 2134
        # (GP3) LOAD OR DEFORM SET 1 DEFINED FOR BOTH GRAVITY AND NON-GRAVITY LOADS"), so
        # gravity goes in set 2 and set 3 adds the two together.
        load_set = 1
        lines += ["SOL 101", "CEND", "TITLE = NX NASTRAN SOLID STATICS (dsh-nx)", "SUBCASE 1",
                  "  SPC = 1", "  LOAD = %d" % load_set, "  DISPLACEMENT = ALL", "  STRESS = ALL",
                  "BEGIN BULK"]

    for nid, x, y, z in nodes:
        lines.extend(card_lines("GRID", [nid, "", fmt(x), fmt(y), fmt(z)]))
    for index, cell in enumerate(cells):
        lines.extend(card_lines("CHEXA", [index + 1, 1] + list(cell)))
    lines.extend(card_lines("PSOLID", [1, 1]))
    lines.extend(card_lines("MAT1", [1, fmt(young), "", fmt(poisson), fmt(density)]))
    for nid in fixed:
        lines.extend(card_lines("SPC1", [1, "123456", nid]))
    if not modal:
        for count, (nid, vector) in enumerate(sorted(forces.items())):
            magnitude = math.sqrt(sum(v * v for v in vector))
            if magnitude < 1e-12:
                continue
            lines.extend(card_lines("FORCE", [1, nid, 0, fmt(magnitude),
                                              fmt(vector[0] / magnitude),
                                              fmt(vector[1] / magnitude),
                                              fmt(vector[2] / magnitude)]))
        lines.extend(extra)
    lines.append("ENDDATA")
    with open(path, "w", encoding="ascii", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    total_force = [sum(v[i] for v in forces.values()) for i in range(3)]
    return {
        "nodes": len(nodes),
        "elements": len(cells),
        "fixedNodes": len(fixed),
        "loadedNodes": len(forces),
        "deformed?": None,
        "loads": notes,
        "resultant": [round(v, 3) for v in total_force],
        "deckLines": len(lines),
    }


# ---------------------------------------------------------------- results
def parse_modes(path):
    """The REAL EIGENVALUES table: mode, extraction order, eigenvalue, radians, cycles.

    Rows are read as whitespace-separated tokens (never by column position), because the
    column widths shift with the eigenvalue magnitude. There is no eigenvector page to
    scan: without a DISPLACEMENT request in the case control Nastran prints the table only.
    """
    out = []
    inside = False
    header = 0
    with open(path, "r", encoding="latin-1") as handle:
        for raw in handle:
            squeezed = raw.replace(" ", "")
            if "REALEIGENVALUES" in squeezed:
                inside = True
                header = 0
                continue
            if not inside:
                continue
            tokens = raw.split()
            if not tokens:
                if out:
                    break
                continue
            if not tokens[0].isdigit():
                header += 1
                if header > 4 and out:
                    break
                continue
            if len(tokens) < 5:
                continue
            try:
                mode = int(tokens[0])
                eigenvalue = float(tokens[2].replace("D", "E"))
                cycles = float(tokens[4].replace("D", "E"))
            except ValueError:
                # page headers start with a page number too ("1   SIMCENTER NASTRAN ...")
                continue
            out.append({"mode": mode, "eigenvalue": round(eigenvalue, 3),
                        "frequency": round(cycles, 3)})
    return out


def make_plots(mesh, displacements, element_stress, workdir, warp, prefix="contour",
               title=None):
    """Colour-mapped pictures of the result; returns the files that were written."""
    if cae_plot is None or not displacements:
        return []
    plots = []
    for field in ("deflection", "vonmises"):
        if field == "vonmises" and not element_stress:
            continue
        target = os.path.join(workdir, "%s_%s.png" % (prefix, field))
        try:
            info = cae_plot.render(mesh, displacements, element_stress, target,
                                   field=field, warp=warp, title=title)
        except Exception as problem:      # a picture must never sink the solve
            print("plotting %s failed: %s" % (field, problem))
            continue
        if info:
            plots.append(info)
    return plots


def write_surface_stl(mesh, forces_scale, displacements, path):
    """Outer faces of the mesh, moved by the (scaled) displacement field."""
    coords = {nid: (x, y, z) for nid, x, y, z in mesh["nodes"]}
    moved = {}
    for nid, point in coords.items():
        delta = displacements.get(nid, (0.0, 0.0, 0.0))
        moved[nid] = (point[0] + delta[0] * forces_scale,
                      point[1] + delta[1] * forces_scale,
                      point[2] + delta[2] * forces_scale)
    faces = surface_faces(mesh)
    with open(path, "w", encoding="ascii", newline="\n") as handle:
        handle.write("solid nx_solid\n")
        for quad in faces:
            a, b, c, d = quad
            for tri in ((a, b, c), (a, c, d)):
                pa, pb, pc = moved[tri[0]], moved[tri[1]], moved[tri[2]]
                ux, uy, uz = pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]
                vx, vy, vz = pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]
                nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
                length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
                handle.write("  facet normal %.6e %.6e %.6e\n" % (nx / length, ny / length, nz / length))
                handle.write("    outer loop\n")
                for point in (pa, pb, pc):
                    handle.write("      vertex %.6e %.6e %.6e\n" % point)
                handle.write("    endloop\n  endfacet\n")
        handle.write("endsolid nx_solid\n")
    return {"faces": len(faces), "triangles": len(faces) * 2, "scale": round(forces_scale, 4)}


# ---------------------------------------------------------------- runner
def run(spec, workdir):
    os.makedirs(workdir, exist_ok=True)
    triangles = read_stl(spec["stl"])
    if not triangles:
        raise RuntimeError("could not read any triangle from %s" % spec["stl"])
    mesh = mesh_solid(triangles, spec.get("elementSize") or 5.0)
    mode = str(spec.get("mesh") or "draped").lower()
    drape = {"draped": False, "mode": mode}
    if mode == "draped" and cae_drape is not None:
        target = (spec.get("source") or {}).get("volume")
        mesh, drape = cae_drape.drape(mesh, triangles, target_volume=target)
        drape["mode"] = mode
        if drape.get("invertedElements"):
            raise RuntimeError("the draped mesh has %d inverted element(s); rerun with mesh=voxel "
                               "or a finer element size" % drape["invertedElements"])
    deck = os.path.join(workdir, "solid_job.bdf")
    summary = write_deck(mesh, spec, deck)

    for stale in ("solid_job.f06", "solid_job.log", "solid_job.f04", "solid_job.op2"):
        target = os.path.join(workdir, stale)
        if os.path.exists(target):
            os.remove(target)

    started = time.time()
    process = subprocess.run([NASTRAN, "solid_job.bdf", "mem=%s" % MEMORY], cwd=workdir,
                             capture_output=True, text=True, encoding="latin-1", errors="replace")
    elapsed = time.time() - started
    f06 = os.path.join(workdir, "solid_job.f06")
    if not os.path.exists(f06):
        raise RuntimeError("NX Nastran produced no f06 (exit %s): %s"
                           % (process.returncode, (process.stderr or process.stdout)[-800:]))

    result = {
        "mesh": {"nodes": summary["nodes"], "elements": summary["elements"],
                 "elementSize": mesh["elementSize"], "grid": mesh["counts"],
                 "spacing": mesh["spacing"], "fixedNodes": summary["fixedNodes"],
                 "loadedNodes": summary["loadedNodes"], "loads": summary["loads"],
                 "resultant": summary["resultant"], "deckLines": summary["deckLines"],
                 "drape": drape},
        "solver": {"exe": NASTRAN, "seconds": round(elapsed, 1), "returncode": process.returncode,
                   "f06": f06},
        "files": {"bdf": deck, "f06": f06},
    }

    if str(spec.get("solver") or "statics").lower() == "modal":
        modes = parse_modes(f06)
        if not modes:
            raise RuntimeError("the f06 carries no eigenvalues -- check %s" % f06)
        displacements, _stress = parse_f06(f06)
        result["modes"] = modes
        result["results"] = {"modes": len(modes),
                             "firstFrequency": modes[0]["frequency"],
                             "frequencies": [m["frequency"] for m in modes]}
        if displacements:
            largest = max(math.sqrt(sum(v * v for v in values)) for values in displacements.values())
            extent = max(mesh["box"][1][i] - mesh["box"][0][i] for i in range(3))
            warp = (0.18 * extent / largest) if largest > 0 else 1.0
            stl = write_surface_stl(mesh, warp, displacements,
                                    os.path.join(workdir, "solid_mode1.stl"))
            result["files"]["stl"] = stl
            plots = make_plots(mesh, displacements, None, workdir, warp, prefix="mode1",
                               title=spec.get("title"))
            if plots:
                result["files"]["plots"] = plots
                result["results"]["contours"] = {plot["field"]: plot["path"] for plot in plots}
        return result

    displacements, element_stress = parse_f06(f06)
    if not displacements:
        raise RuntimeError("no displacement in the f06 -- check %s" % f06)
    largest = max(math.sqrt(sum(v * v for v in values)) for values in displacements.values())
    extent = max(mesh["box"][1][i] - mesh["box"][0][i] for i in range(3))
    scale = (0.18 * extent / largest) if largest > 0 else 1.0
    stl = write_surface_stl(mesh, scale, displacements, os.path.join(workdir, "solid_deformed.stl"))
    plots = make_plots(mesh, displacements, element_stress, workdir, scale,
                       title=spec.get("title"))
    peak = max(element_stress.values()) if element_stress else 0.0
    loaded = []
    for load in summary["loads"]:
        if load.get("face"):
            loaded.extend(nodes_on_side(mesh, load["face"]))
    loaded = sorted(set(loaded))
    face_disp = max((math.sqrt(sum(v * v for v in displacements.get(nid, (0, 0, 0))))
                     for nid in loaded), default=0.0)
    result["results"] = {
        "maxDisplacement": round(largest, 6),
        "loadedFaceDisplacement": round(face_disp, 6),
        "maxVonMises": round(peak, 3),
        "nodesWithResult": len(displacements),
        "elementsWithStress": len(element_stress),
        "deformationScale": round(scale, 3),
        "reaction": None,
    }
    result["files"]["stl"] = stl
    if plots:
        result["files"]["plots"] = plots
        result["results"]["contours"] = {plot["field"]: plot["path"] for plot in plots}
    return result


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    spec_path = sys.argv[1]
    workdir = sys.argv[2] if len(sys.argv) > 2 else os.path.dirname(os.path.abspath(spec_path))
    result_path = sys.argv[3] if len(sys.argv) > 3 else None
    # utf-8-sig: a spec edited in Notepad carries a BOM, and json.load chokes on it.
    with open(spec_path, "r", encoding="utf-8-sig") as handle:
        spec = json.load(handle)
    result = run(spec, workdir)
    if result_path:
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        print("result written to %s" % result_path)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

