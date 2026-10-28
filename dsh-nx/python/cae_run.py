"""NX Nastran FE pipeline for the dsh-nx plugin.

Pipeline: geometry spec -> structured hex mesh -> BDF deck -> NX Nastran solve
-> parsed results -> deformed STL + PNG/SVG report + JSON summary.

Standalone (no NXOpen needed): python cae_run.py <spec.json> <workdir>
"""

import json
import math
import os
import re
import subprocess
import sys
import time

# Mirrors lib/bridge.js NX_INSTALL_ROOTS: the Siemens folders first, then app drives.
NX_INSTALL_ROOTS = (
    r"C:\Program Files\Siemens",
    r"C:\Program Files\Siemens PLM Software",
    r"C:\Program Files (x86)\Siemens",
    r"C:\Siemens",
    r"C:\software",
    r"C:\apps",
    r"D:\Program Files\Siemens",
    r"D:\Program Files\Siemens PLM Software",
    r"D:\Siemens",
    r"D:\software",
    r"D:\apps",
)


def find_nastran():
    """Locate nastran.exe: DSH_NX_NASTRAN, then UGII_BASE_DIR, then the usual NX install roots."""
    explicit = os.environ.get("DSH_NX_NASTRAN", "")
    if explicit and os.path.isfile(explicit):
        return explicit
    bases = [os.environ.get("UGII_BASE_DIR", "")]
    for root in NX_INSTALL_ROOTS:
        if not os.path.isdir(root):
            continue
        try:
            bases.extend(os.path.join(root, name) for name in sorted(os.listdir(root)))
        except OSError:
            continue
    for base in bases:
        if not base:
            continue
        exe = os.path.join(base, "NXNASTRAN", "bin", "nastran.exe")
        if os.path.isfile(exe):
            return exe
    return explicit or "nastran.exe"


NASTRAN = find_nastran()
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\arial.ttf",
)
FIELD = 8


# ---------------------------------------------------------------- deck writing
def card_lines(name, fields):
    """Fixed-field (8 column) Nastran card, splitting into continuation lines."""
    groups = [fields[i:i + 8] for i in range(0, len(fields), 8)] or [[]]
    lines = []
    for index, group in enumerate(groups):
        cells = [name.ljust(FIELD) if index == 0 else "+".ljust(FIELD)]
        cells.extend(str(value).rjust(FIELD) for value in group)
        if index < len(groups) - 1:
            cells.append("+")
        lines.append("".join(cells))
    return lines


def fmt(value):
    """A real field of a fixed-field card: at most 8 columns, decimal point included.

    "%.6g" overflows for small loads (1.234567e-05 is 12 columns), and the old fallback
    to "%.4e" overflowed too -- Nastran then reports USER FATAL MESSAGE 300 (RCARD)
    "DATA ERROR IN FIELD UNDERLINED. POSSIBLE ERROR IN EXPONENT." So the exponent is
    written in Nastran's compact form, where the E is dropped: 1.2346-5.
    """
    if not isinstance(value, float):
        return str(value)
    text = "%.6g" % value
    if "." not in text and "e" not in text.lower():
        text += "."
    if len(text) > FIELD:
        text = "%.4e" % value
        mantissa, exponent = text.split("e")
        sign = "-" if exponent.startswith("-") else "+"
        digits = exponent.lstrip("+-0") or "0"
        text = mantissa + sign + digits
        if len(text) > FIELD:
            text = "%.3e" % value
            mantissa, exponent = text.split("e")
            sign = "-" if exponent.startswith("-") else "+"
            digits = exponent.lstrip("+-0") or "0"
            text = mantissa + sign + digits
    if len(text) > FIELD:
        raise RuntimeError("value %r does not fit an 8-column Nastran field" % (value,))
    return text


def build_mesh(spec):
    """Structured hex mesh of a box; the -X face is fixed, the +X face is loaded."""
    length = float(spec["length"])
    width = float(spec["width"])
    height = float(spec["height"])
    nx = int(spec.get("nx", 24))
    ny = int(spec.get("ny", 4))
    nz = int(spec.get("nz", 2))

    xs = [length * i / nx for i in range(nx + 1)]
    ys = [-width / 2.0 + width * j / ny for j in range(ny + 1)]
    zs = [-height / 2.0 + height * k / nz for k in range(nz + 1)]

    def node_id(i, j, k):
        return 1 + i * (ny + 1) * (nz + 1) + j * (nz + 1) + k

    nodes = []
    for i in range(nx + 1):
        for j in range(ny + 1):
            for k in range(nz + 1):
                nodes.append((node_id(i, j, k), xs[i], ys[j], zs[k]))

    elements = []
    eid = 1
    for i in range(nx):
        for j in range(ny):
            for k in range(nz):
                g = [
                    node_id(i, j, k), node_id(i + 1, j, k),
                    node_id(i + 1, j + 1, k), node_id(i, j + 1, k),
                    node_id(i, j, k + 1), node_id(i + 1, j, k + 1),
                    node_id(i + 1, j + 1, k + 1), node_id(i, j + 1, k + 1),
                ]
                elements.append((eid, (i, j, k), g))
                eid += 1

    fixed = [node_id(0, j, k) for j in range(ny + 1) for k in range(nz + 1)]
    tip = [node_id(nx, j, k) for j in range(ny + 1) for k in range(nz + 1)]
    return nodes, elements, fixed, tip, (nx, ny, nz), (xs, ys, zs)


def write_deck(spec, path):
    nodes, elements, fixed, tip, _grid, _axes = build_mesh(spec)
    material = spec.get("material", {})
    young = float(material.get("E", 210000.0))
    poisson = float(material.get("nu", 0.3))
    load = float(spec.get("load", 200.0))
    direction = spec.get("direction", "z")

    lines = ["SOL 101", "CEND",
             "TITLE = NX NASTRAN FE ANALYSIS (dsh-nx)", "SUBCASE 1",
             "  SPC = 1", "  LOAD = 1", "  DISPLACEMENT = ALL", "  STRESS = ALL",
             "BEGIN BULK"]
    for nid, x, y, z in nodes:
        lines.extend(card_lines("GRID", [nid, "", fmt(float(x)), fmt(float(y)), fmt(float(z))]))
    for eid, _ijk, g in elements:
        lines.extend(card_lines("CHEXA", [eid, 1] + list(g)))
    lines.extend(card_lines("PSOLID", [1, 1]))
    lines.extend(card_lines("MAT1", [1, fmt(young), "", fmt(poisson)]))
    for nid in fixed:
        lines.extend(card_lines("SPC1", [1, "123456", nid]))
    share = load / float(len(tip))
    unit = [0.0, 0.0, 0.0]
    unit[{"x": 0, "y": 1}.get(direction, 2)] = 1.0
    for nid in tip:
        lines.extend(card_lines("FORCE", [1, nid, "", fmt(share),
                                          fmt(unit[0]), fmt(unit[1]), fmt(unit[2])]))
    lines.append("ENDDATA")
    with open(path, "w", encoding="ascii", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    return {"nodes": len(nodes), "elements": len(elements), "fixedNodes": len(fixed),
            "loadedNodes": len(tip), "loadPerNode": share}


# ---------------------------------------------------------------- f06 parsing
NUMBER = re.compile(r"-?\d+\.\d+(?:[Ee][-+]?\d+)?")


def parse_f06(path):
    with open(path, "r", encoding="latin-1") as handle:
        lines = handle.readlines()

    displacements = {}
    element_stress = {}
    mode = None
    current_element = None
    mises_at = None
    for raw in lines:
        line = raw.rstrip("\n")
        compact = re.sub(r"\s+", "", line)
        if "DISPLACEMENTVECTOR" in compact:
            mode, current_element = "disp", None
            continue
        if "STRESSESIN" in compact:
            mode, current_element = "stress", None
            mises_at = None
            continue
        if "OLOAD" in compact or "PAGE" in line and mode == "disp" and displacements:
            mode = None
        if mode == "disp":
            match = re.match(r"^\s*(\d+)\s+G\s+(.+)$", line)
            if match:
                values = [float(v) for v in NUMBER.findall(match.group(2))]
                if len(values) >= 3:
                    displacements[int(match.group(1))] = values[:3]
        elif mode == "stress":
            if "VONMISES" in compact and "PRESSURE" in compact:
                mises_at = line.find("VON MISES")
                continue
            marker = re.match(r"^\s*0?\s*(\d+)\s+(\d+)GRID\s+CS", line)
            if marker:
                current_element = int(marker.group(1))
                continue
            if not current_element:
                continue
            # The last number on a stress line is NOT the von Mises value: the Y/Z lines
            # of every corner end with direction cosines, so reading the tail of the line
            # reports a cosine as a stress. Slice the column the header names instead.
            if mises_at is None or len(line) < mises_at + 6:
                continue
            # Take the tail and its FIRST number: a fixed 14-character window slices
            # exponents in half ("9.990894E-03" -> 9.9908) and reports 1000x the stress.
            numbers = NUMBER.findall(line[max(0, mises_at - 8):])
            if not numbers:
                continue
            value = abs(float(numbers[0]))
            element_stress[current_element] = max(element_stress.get(current_element, 0.0), value)
    return displacements, element_stress


# ---------------------------------------------------------------- writers
def write_stl(spec, displacements, path, factor=None):
    nodes, elements, _fixed, _tip, _grid, _axes = build_mesh(spec)
    max_disp = max((math.sqrt(sum(v * v for v in values)) for values in displacements.values()), default=0.0)
    if factor is None:
        longest = max(float(spec["length"]), float(spec["width"]), float(spec["height"]))
        factor = (0.25 * longest / max_disp) if max_disp > 0 else 1.0
    coords = {}
    for nid, x, y, z in nodes:
        d = displacements.get(nid, [0.0, 0.0, 0.0])
        coords[nid] = (x + d[0] * factor, y + d[1] * factor, z + d[2] * factor)

    faces = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    counts = {}
    for _eid, _ijk, g in elements:
        for face in faces:
            key = tuple(sorted(g[i] for i in face))
            counts[key] = counts.get(key, 0) + 1
    triangles = []
    for _eid, _ijk, g in elements:
        for face in faces:
            if counts[tuple(sorted(g[i] for i in face))] != 1:
                continue
            a, b, c, d = [g[i] for i in face]
            triangles.append((a, b, c))
            triangles.append((a, c, d))

    with open(path, "w", encoding="ascii", newline="\n") as handle:
        handle.write("solid nx_cae\n")
        for a, b, c in triangles:
            pa, pb, pc = coords[a], coords[b], coords[c]
            u = [pb[i] - pa[i] for i in range(3)]
            v = [pc[i] - pa[i] for i in range(3)]
            normal = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
            length = math.sqrt(sum(n * n for n in normal)) or 1.0
            handle.write("  facet normal %.6e %.6e %.6e\n" % tuple(n / length for n in normal))
            handle.write("    outer loop\n")
            for point in (pa, pb, pc):
                handle.write("      vertex %.6e %.6e %.6e\n" % point)
            handle.write("    endloop\n  endfacet\n")
        handle.write("endsolid nx_cae\n")
    return {"triangles": len(triangles), "factor": round(factor, 3), "maxDisp": round(max_disp, 6)}


def slice_stress(spec, element_stress):
    """Per x-slice peak von Mises (mm slices along the beam)."""
    _nodes, elements, _fixed, _tip, (nx, _ny, _nz), (xs, _ys, _zs) = build_mesh(spec)
    best = [0.0] * nx
    for eid, (i, _j, _k), _g in elements:
        value = element_stress.get(eid, 0.0)
        if value > best[i]:
            best[i] = value
    return best


def slice_deflection(spec, displacements):
    """Signed deflection sampled along the beam (keeps the -Z direction)."""
    nodes, _elements, _fixed, _tip, (nx, _ny, _nz), (xs, _ys, _zs) = build_mesh(spec)
    curve = []
    for i in range(nx + 1):
        target = xs[i]
        values = [displacements.get(nid, [0, 0, 0])[2] for nid, x, y, z in nodes if abs(x - target) < 1e-9]
        curve.append(max(values, key=abs) if values else 0.0)
    return curve


from cae_viz import colormap, write_png  # noqa: E402


def write_svg(spec, info, analytic, path):
    """Lightweight vector twin of the PNG (kept for cad_view / 2D preview)."""
    width_px, height_px = 1240, 780
    length = float(spec["length"])
    height = float(spec["height"])
    left, plot_w = 100, 990
    beam_cy, beam_h = 250, 96
    px_per_mm = plot_w / length

    def px(x_mm):
        return left + x_mm * px_per_mm

    factor = (0.26 * length / info["maxDisp"]) if info["maxDisp"] > 0 else 1.0

    def pz(z_mm, dz):
        return beam_cy - (z_mm + dz * factor) * (beam_h / height)

    stress = info["sliceStress"]
    peak = max(stress) if stress else 1.0
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">'
             % (width_px, height_px, width_px, height_px),
             '<rect width="100%" height="100%" fill="#ffffff"/>',
             '<text x="%d" y="40" font-family="Segoe UI, Arial" font-size="22" fill="#14181e">'
             'NX Nastran 静力分析 — 悬臂梁 %.0f×%.0f×%.0f mm</text>'
             % (left, length, float(spec["width"]), height),
             '<text x="%d" y="66" font-family="Segoe UI, Arial" font-size="14" fill="#5a626c">'
             'max disp %.4f mm (analytic %.4f mm) · peak von Mises %.1f MPa (analytic %.1f MPa)</text>'
             % (left, info["maxDisp"], analytic["disp"], info["maxStress"], analytic["stress"]),
             '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="none" stroke="#aab4c0" '
             'stroke-dasharray="5 4"/>' % (px(0), pz(height / 2, 0), plot_w,
                                           pz(-height / 2, 0) - pz(height / 2, 0))]
    nx = len(stress)
    for i in range(nx):
        x0 = length * i / nx
        x1 = length * (i + 1) / nx
        record = stress[i] / peak if peak else 0.0
        red, green, blue = colormap(record)
        dz = info["curve"][i] if i < len(info["curve"]) else 0.0
        parts.append('<polygon points="%.1f,%.1f %.1f,%.1f %.1f,%.1f %.1f,%.1f" fill="rgb(%d,%d,%d)"/>'
                     % (px(x0), pz(height / 2, dz), px(x1), pz(height / 2, dz),
                        px(x1), pz(-height / 2, dz), px(x0), pz(-height / 2, dz), red, green, blue))
    parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#1e2b40" stroke-width="2"/>'
                 % (px(0), beam_cy - beam_h * 0.95, px(0), beam_cy + beam_h * 0.95))
    parts.append('<text x="%.1f" y="%.1f" font-family="Segoe UI, Arial" font-size="13" fill="#c82828">'
                 'F = %.0f N</text>' % (px(length) + 10, beam_cy - beam_h * 1.6,
                                        float(spec.get("load", 200))))
    bar_x, bar_top, bar_h = width_px - 90, 150, beam_h * 2
    for step in range(0, bar_h, 4):
        red, green, blue = colormap(1.0 - step / float(bar_h))
        parts.append('<rect x="%d" y="%d" width="22" height="4" fill="rgb(%d,%d,%d)"/>'
                     % (bar_x, bar_top + step, red, green, blue))
    parts.append('<text x="%d" y="%d" font-family="Segoe UI, Arial" font-size="12" fill="#3c434c">'
                 '%.1f MPa</text>' % (bar_x + 28, bar_top + 4, peak))
    parts.append('<text x="%d" y="%d" font-family="Segoe UI, Arial" font-size="12" fill="#3c434c">0</text>'
                 % (bar_x + 28, bar_top + bar_h))
    parts.append("</svg>")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(parts))
    return {"path": path}


# ---------------------------------------------------------------- runner
def run(spec, workdir):
    os.makedirs(workdir, exist_ok=True)
    deck = os.path.join(workdir, "cae_job.bdf")
    mesh = write_deck(spec, deck)
    for stale in ("cae_job.f06", "cae_job.log", "cae_job.f04"):
        target = os.path.join(workdir, stale)
        if os.path.exists(target):
            os.remove(target)

    started = time.time()
    process = subprocess.run([NASTRAN, "cae_job.bdf"], cwd=workdir, capture_output=True, text=True,
                             encoding="latin-1", errors="replace")
    elapsed = time.time() - started

    f06 = os.path.join(workdir, "cae_job.f06")
    if not os.path.exists(f06):
        raise RuntimeError("NX Nastran 没有产出 f06（exit %s）: %s"
                           % (process.returncode, (process.stderr or process.stdout)[-800:]))
    displacements, element_stress = parse_f06(f06)
    if not displacements:
        raise RuntimeError("f06 里没有解析到位移结果，请检查 %s" % f06)

    length = float(spec["length"])
    height = float(spec["height"])
    width = float(spec["width"])
    young = float(spec.get("material", {}).get("E", 210000.0))
    load = float(spec.get("load", 200.0))
    inertia = width * height ** 3 / 12.0
    analytic = {"disp": load * length ** 3 / (3.0 * young * inertia),
                "stress": load * length * (height / 2.0) / inertia}

    _nodes, elements, _fixed, tip, grid, _axes = build_mesh(spec)
    curve = slice_deflection(spec, displacements)
    slice_peak = slice_stress(spec, element_stress)
    max_disp = max((abs(v) for v in curve), default=0.0)
    max_stress = max(slice_peak) if slice_peak else 0.0
    info = {"grid": grid, "maxDisp": max_disp, "maxStress": max_stress, "curve": curve,
            "sliceStress": slice_peak, "seconds": round(elapsed, 1), "nelem": len(elements),
            "nodes": len(displacements), "loadedNodes": len(tip),
            "solverExe": NASTRAN, "deckLines": sum(1 for _ in open(deck, encoding="ascii"))}

    stl = write_stl(spec, displacements, os.path.join(workdir, "cae_deformed.stl"))
    try:
        png = write_png(spec, analytic, info, os.path.join(workdir, "cae_result.png"))
    except Exception as error:  # Pillow missing / font problems must not lose the solve
        png = {"error": "%s: %s" % (type(error).__name__, error)}
    svg = write_svg(spec, info, analytic, os.path.join(workdir, "cae_result.svg"))

    tip_dz = max((abs(displacements.get(nid, [0, 0, 0])[2]) for nid in tip), default=0.0)
    return {
        "mesh": mesh,
        "solver": {"exe": NASTRAN, "seconds": round(elapsed, 1), "returncode": process.returncode,
                   "tail": (process.stdout or "").strip().splitlines()[-2:]},
        "results": {
            "tipDeflection": round(tip_dz, 6),
            "analyticDeflection": round(analytic["disp"], 6),
            "deflectionRatio": round(tip_dz / analytic["disp"], 4) if analytic["disp"] else None,
            "maxVonMises": round(max_stress, 3),
            "analyticStress": round(analytic["stress"], 3),
            "stressRatio": round(max_stress / analytic["stress"], 4) if analytic["stress"] else None,
            "nodes": len(displacements),
            "elementsWithStress": len(element_stress),
        },
        "files": {"bdf": deck, "f06": f06, "stl": stl, "png": png, "svg": svg},
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    spec_path = sys.argv[1]
    workdir = sys.argv[2] if len(sys.argv) > 2 else os.path.dirname(os.path.abspath(spec_path))
    result_path = sys.argv[3] if len(sys.argv) > 3 else None
    with open(spec_path, "r", encoding="utf-8") as handle:
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
