"""Contour images for the solid FE runs: a colour-mapped, shaded view of the outer surface.

Pure python on top of numpy (rasteriser) and Pillow (colour bar and labels) -- matplotlib is
not part of the runtime this plugin ships with. The renderer is a small software pipeline:
project the outer quads, z-buffer them, interpolate the nodal result across each triangle and
look the value up in a fixed colour table.

Nothing here talks to NX; cae_solid.py calls it once the f06 is parsed.
"""

import math
import os

import numpy as np
from PIL import Image, ImageDraw

# Blue -> cyan -> green -> yellow -> red, the usual FE rainbow.
_STOPS = [
    (0.00, (49, 62, 148)),
    (0.20, (48, 128, 214)),
    (0.40, (58, 187, 178)),
    (0.55, (141, 209, 88)),
    (0.72, (243, 205, 74)),
    (0.86, (240, 137, 54)),
    (1.00, (192, 50, 46)),
]

LIGHT = np.array([-0.35, 0.48, 0.80])
LIGHT = LIGHT / np.linalg.norm(LIGHT)


def _lut(size=256):
    table = np.zeros((size, 3), dtype=np.float64)
    positions = np.array([stop[0] for stop in _STOPS])
    colours = np.array([stop[1] for stop in _STOPS], dtype=np.float64)
    for channel in range(3):
        table[:, channel] = np.interp(np.linspace(0.0, 1.0, size), positions, colours[:, channel])
    return table


def _basis(azimuth, elevation):
    """Camera looking from (azimuth, elevation) towards the model, orthographic."""
    direction = np.array([math.cos(elevation) * math.cos(azimuth),
                          math.cos(elevation) * math.sin(azimuth),
                          math.sin(elevation)])
    right = np.cross(np.array([0.0, 0.0, 1.0]), direction)
    right = right / (np.linalg.norm(right) or 1.0)
    up = np.cross(direction, right)
    return right, up, direction


def _outer_quads(mesh):
    """The quads used by exactly one element -- i.e. the outside skin of the voxel mesh."""
    seen = {}
    for cell in mesh["cells"]:
        c = list(cell)
        for quad in ((c[0], c[3], c[2], c[1]), (c[4], c[5], c[6], c[7]),
                     (c[0], c[1], c[5], c[4]), (c[1], c[2], c[6], c[5]),
                     (c[2], c[3], c[7], c[6]), (c[3], c[0], c[4], c[7])):
            key = tuple(sorted(quad))
            seen[key] = seen.get(key, 0) + 1
    quads = []
    for cell in mesh["cells"]:
        c = list(cell)
        for quad in ((c[0], c[3], c[2], c[1]), (c[4], c[5], c[6], c[7]),
                     (c[0], c[1], c[5], c[4]), (c[1], c[2], c[6], c[5]),
                     (c[2], c[3], c[7], c[6]), (c[3], c[0], c[4], c[7])):
            if seen[tuple(sorted(quad))] == 1:
                quads.append(quad)
    return quads


def nodal_field(mesh, displacements, element_stress, field, scale=1.0):
    """Per-node value: displacement magnitude (mm) or averaged von Mises (MPa)."""
    values = {}
    if field == "vonmises":
        total = {}
        count = {}
        for index, cell in enumerate(mesh["cells"]):
            stress = element_stress.get(index + 1)
            if stress is None:
                continue
            for nid in cell:
                total[nid] = total.get(nid, 0.0) + stress
                count[nid] = count.get(nid, 0) + 1
        for nid, summed in total.items():
            values[nid] = summed / count[nid]
        if not values:
            for nid in displacements:
                values[nid] = 0.0
    else:
        for nid, delta in displacements.items():
            values[nid] = math.sqrt(sum(v * v for v in delta)) * scale
    return values


def _triangle_raster(points2d, depth, shade, value, zbuffer, colour, lut, value_max):
    """Scan-convert one triangle into the buffers (numpy, barycentric)."""
    (x0, y0), (x1, y1), (x2, y2) = points2d
    height, width = zbuffer.shape
    minx = max(int(math.floor(min(x0, x1, x2))), 0)
    maxx = min(int(math.ceil(max(x0, x1, x2))), width - 1)
    miny = max(int(math.floor(min(y0, y1, y2))), 0)
    maxy = min(int(math.ceil(max(y0, y1, y2))), height - 1)
    if minx > maxx or miny > maxy:
        return
    area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
    if abs(area) < 1e-12:
        return
    xs = np.arange(minx, maxx + 1) + 0.5
    ys = np.arange(miny, maxy + 1) + 0.5
    gridx, gridy = np.meshgrid(xs, ys)
    w0 = ((x1 - gridx) * (y2 - gridy) - (x2 - gridx) * (y1 - gridy)) / area
    w1 = ((x2 - gridx) * (y0 - gridy) - (x0 - gridx) * (y2 - gridy)) / area
    w2 = 1.0 - w0 - w1
    inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
    if not inside.any():
        return
    depth_at = w0 * depth[0] + w1 * depth[1] + w2 * depth[2]
    window = zbuffer[miny:maxy + 1, minx:maxx + 1]
    closer = inside & (depth_at > window)
    if not closer.any():
        return
    value_at = w0 * value[0] + w1 * value[1] + w2 * value[2]
    index = np.clip((value_at / (value_max or 1.0) * (len(lut) - 1)), 0, len(lut) - 1)
    tint = lut[index.astype(np.int32)] / 255.0
    tint = tint * shade
    window[closer] = depth_at[closer]
    block = colour[miny:maxy + 1, minx:maxx + 1, :3]
    block[closer] = np.clip(tint * 255.0, 0, 255)[closer].astype(np.uint8)


def render(mesh, displacements, element_stress, path, field="deflection", warp=1.0,
           value_scale=1.0, width=1100, height=760, azimuth=0.78, elevation=0.52,
           title=None):
    coords = {nid: np.array((x, y, z), dtype=np.float64) for nid, x, y, z in mesh["nodes"]}
    values = nodal_field(mesh, displacements, element_stress, field, value_scale)
    if not values:
        return None
    quads = _outer_quads(mesh)
    if not quads:
        return None

    moved = {}
    for nid, point in coords.items():
        delta = displacements.get(nid, (0.0, 0.0, 0.0))
        moved[nid] = point + np.array(delta, dtype=np.float64) * warp

    right, up, direction = _basis(azimuth, elevation)
    screen = {}
    for nid, point in moved.items():
        screen[nid] = (float(np.dot(point, right)), float(-np.dot(point, up)),
                       float(np.dot(point, direction)))
    xs = [p[0] for p in screen.values()]
    ys = [p[1] for p in screen.values()]
    span_x = (max(xs) - min(xs)) or 1.0
    span_y = (max(ys) - min(ys)) or 1.0
    margin = 55
    bar_room = 190                      # keep the colour bar off the model itself
    zoom = min((width - bar_room - 2 * margin) / span_x, (height - 2 * margin) / span_y)
    offset_x = (width - bar_room - span_x * zoom) / 2.0 - min(xs) * zoom
    offset_y = (height - span_y * zoom) / 2.0 - min(ys) * zoom
    for nid, (sx, sy, sz) in list(screen.items()):
        screen[nid] = (sx * zoom + offset_x, sy * zoom + offset_y, sz)

    peak = max(values.values()) or 1.0
    lut = _lut()
    zbuffer = np.full((height, width), -1e18, dtype=np.float64)
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    canvas[:, :] = (248, 249, 251)

    for quad in quads:
        ids = list(quad)
        for a, b, c in ((ids[0], ids[1], ids[2]), (ids[0], ids[2], ids[3])):
            pa, pb, pc = moved[a], moved[b], moved[c]
            normal = np.cross(pb - pa, pc - pa)
            length = np.linalg.norm(normal)
            if length < 1e-12:
                continue
            normal = normal / length
            shade = 0.72 + 0.28 * abs(float(np.dot(normal, LIGHT)))
            sa, sb, sc = screen[a], screen[b], screen[c]
            _triangle_raster([(sa[0], sa[1]), (sb[0], sb[1]), (sc[0], sc[1])],
                             (sa[2], sb[2], sc[2]), shade,
                             (values.get(a, 0.0), values.get(b, 0.0), values.get(c, 0.0)),
                             zbuffer, canvas, lut, peak)

    image = Image.fromarray(canvas)
    draw = ImageDraw.Draw(image)
    label = title or ("displacement" if field == "deflection" else "von Mises")
    unit = "mm" if field == "deflection" else "MPa"
    draw.text((24, 18), "%s  max %.4g %s" % (label, peak, unit), fill=(40, 44, 52))

    bar_x, bar_y, bar_w, bar_h = width - 150, 90, 26, height - 200
    for step in range(bar_h):
        ratio = 1.0 - step / float(bar_h - 1)
        colour = lut[min(int(ratio * (len(lut) - 1)), len(lut) - 1)]
        draw.line([(bar_x, bar_y + step), (bar_x + bar_w, bar_y + step)],
                  fill=tuple(int(v) for v in colour))
    draw.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], outline=(90, 96, 106))
    for tick in range(5):
        ratio = tick / 4.0
        y = bar_y + bar_h - int(ratio * bar_h)
        value = peak * ratio
        draw.line([(bar_x + bar_w, y), (bar_x + bar_w + 6, y)], fill=(90, 96, 106))
        draw.text((bar_x + bar_w + 10, y - 5), "%.3g" % value, fill=(60, 66, 76))
    draw.text((bar_x - 4, bar_y + bar_h + 12), unit, fill=(60, 66, 76))

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    image.save(path)
    return {"field": field, "path": path, "min": round(min(values.values()), 4),
            "max": round(peak, 4)}
