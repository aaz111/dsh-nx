"""Report rendering for the dsh-nx FE pipeline (PNG via Pillow).

Kept separate from cae_run.py so the solver path stays dependency-free and the
picture can be re-rendered from a stored result JSON.
"""

import os

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\arial.ttf",
)

COLOR_STOPS = [(0.0, (43, 92, 196)), (0.2, (38, 166, 205)), (0.4, (46, 196, 128)),
               (0.6, (154, 205, 60)), (0.8, (247, 190, 52)), (1.0, (212, 42, 42))]


def colormap(t):
    t = max(0.0, min(1.0, float(t)))
    for index in range(len(COLOR_STOPS) - 1):
        t0, c0 = COLOR_STOPS[index]
        t1, c1 = COLOR_STOPS[index + 1]
        if t0 <= t <= t1:
            ratio = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return tuple(int(round(c0[i] + (c1[i] - c0[i]) * ratio)) for i in range(3))
    return COLOR_STOPS[-1][1]


def load_font(size, bold=False):
    from PIL import ImageFont
    candidates = FONT_CANDIDATES if not bold else (r"C:\Windows\Fonts\msyhbd.ttc",) + FONT_CANDIDATES
    for candidate in candidates:
        if os.path.exists(candidate):
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _lerp(values, position):
    """Linear interpolation over a uniformly sampled list."""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    span = len(values) - 1
    pos = max(0.0, min(1.0, position)) * span
    low = int(pos)
    high = min(low + 1, span)
    ratio = pos - low
    return values[low] * (1.0 - ratio) + values[high] * ratio


def write_png(spec, analytic, info, path, title=None):
    """Two-panel report: stress-coloured deformed beam + deflection comparison."""
    from PIL import Image, ImageDraw

    width_px, height_px = 1280, 860
    image = Image.new("RGB", (width_px, height_px), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    font_title = load_font(26, bold=True)
    font_sub = load_font(16)
    font_small = load_font(13)
    font_tiny = load_font(12)

    length = float(spec["length"])
    height = float(spec["height"])
    width = float(spec["width"])
    load = float(spec.get("load", 200.0))
    max_disp = info["maxDisp"] or 1e-9
    curve = info["curve"] or [0.0]
    slice_stress = info["sliceStress"] or [0.0]
    peak = max(slice_stress) or 1.0

    draw.text((96, 24), title or ("NX Nastran 静力分析结果 · 悬臂梁 %.0f×%.0f×%.0f mm" % (length, width, height)),
              font=font_title, fill=(18, 22, 28))
    draw.text((96, 62),
              "网格 %d×%d×%d 六面体（%d 单元 / %d 节点） · 载荷 %.0f N · 求解 %.1f s"
              % (info["grid"][0], info["grid"][1], info["grid"][2], info["nelem"],
                 info["nodes"], load, info["seconds"]),
              font=font_sub, fill=(96, 104, 114))
    draw.text((96, 88),
              "自由端最大位移 %.4f mm（解析 %.4f mm，偏差 %+.1f%%） · 最大等效应力 %.1f MPa（解析 %.1f MPa，偏差 %+.1f%%）"
              % (info["maxDisp"], analytic["disp"], 100.0 * (info["maxDisp"] / analytic["disp"] - 1.0),
                 info["maxStress"], analytic["stress"], 100.0 * (info["maxStress"] / analytic["stress"] - 1.0)),
              font=font_sub, fill=(60, 66, 76))

    # ---------------------------------------------------------------- panel 1
    beam_cy, beam_h = 300, 84
    x_left, x_right = 110, width_px - 210
    plot_w = x_right - x_left
    px_per_mm = plot_w / length
    px_per_z_mm = beam_h / height
    disp_px_per_mm = 0.55 * beam_h / max_disp
    exaggeration = disp_px_per_mm / px_per_z_mm

    def px(x_mm):
        return x_left + x_mm * px_per_mm

    def py(z_mm, dz):
        # dz is a real deflection (mm); scale it by the exaggeration factor so it
        # reads on the same axis as the beam thickness.
        return beam_cy - (z_mm + dz * exaggeration) * px_per_z_mm

    # undeformed reference (dashed)
    ref_top, ref_bottom = py(height / 2.0, 0.0), py(-height / 2.0, 0.0)
    for x in range(x_left, x_right, 16):
        draw.line([(x, ref_top), (min(x + 8, x_right), ref_top)], fill=(198, 205, 214), width=1)
        draw.line([(x, ref_bottom), (min(x + 8, x_right), ref_bottom)], fill=(198, 205, 214), width=1)

    # stress-coloured strips along the beam
    samples = 96
    strips = []
    for index in range(samples):
        t0, t1 = index / float(samples), (index + 1) / float(samples)
        dz0, dz1 = _lerp(curve, t0), _lerp(curve, t1)
        colour = colormap(_lerp(slice_stress, (t0 + t1) / 2.0) / peak)
        quad = [(px(length * t0), py(height / 2.0, dz0)), (px(length * t1), py(height / 2.0, dz1)),
                (px(length * t1), py(-height / 2.0, dz1)), (px(length * t0), py(-height / 2.0, dz0))]
        draw.polygon(quad, fill=colour)
        strips.append((t0, quad))

    # outline of the deformed beam
    top_edge = [(px(length * (i / 48.0)), py(height / 2.0, _lerp(curve, i / 48.0))) for i in range(49)]
    bottom_edge = [(px(length * (i / 48.0)), py(-height / 2.0, _lerp(curve, i / 48.0))) for i in range(49)]
    draw.line(top_edge, fill=(32, 38, 48), width=2)
    draw.line(bottom_edge, fill=(32, 38, 48), width=2)
    draw.line([top_edge[0], bottom_edge[0]], fill=(32, 38, 48), width=2)
    draw.line([top_edge[-1], bottom_edge[-1]], fill=(32, 38, 48), width=2)

    # fixed wall with hatching
    wall_top, wall_bottom = beam_cy - beam_h * 1.05, beam_cy + beam_h * 1.05
    draw.line([(x_left, wall_top), (x_left, wall_bottom)], fill=(36, 40, 50), width=5)
    for y in range(int(wall_top), int(wall_bottom), 13):
        draw.line([(x_left - 26, y + 13), (x_left, y)], fill=(140, 148, 158), width=1)
    draw.text((x_left - 30, wall_top - 30), "固定端", font=font_small, fill=(70, 78, 88))

    # tip load arrow (downward, matching -Z)
    tip_x = px(length)
    tip_y = py(-height / 2.0, _lerp(curve, 1.0))
    arrow_top = tip_y - 96
    draw.line([(tip_x + 26, arrow_top), (tip_x + 26, tip_y - 12)], fill=(198, 46, 46), width=3)
    draw.polygon([(tip_x + 19, tip_y - 12), (tip_x + 33, tip_y - 12), (tip_x + 26, tip_y + 4)], fill=(198, 46, 46))
    draw.text((tip_x + 16, arrow_top - 20), "F = %.0f N" % load, font=font_small,
              fill=(198, 46, 46), anchor="ra")

    # deflection annotation with exaggeration factor
    draw.text((tip_x - 150, tip_y + 30), "变形放大 %.0f×" % exaggeration, font=font_tiny, fill=(120, 128, 138))
    draw.text((tip_x - 150, tip_y + 46), "自由端 %.4f mm" % info["maxDisp"], font=font_tiny, fill=(120, 128, 138))

    # peak stress marker at the fixed end (top fibre)
    sigma_x = px(0) + 6
    sigma_y = py(height / 2.0, _lerp(curve, 0.0)) - 16
    draw.ellipse([sigma_x - 5, sigma_y - 5, sigma_x + 5, sigma_y + 5], fill=(212, 42, 42))
    draw.text((sigma_x + 12, sigma_y - 8), "σmax %.1f MPa" % info["maxStress"], font=font_small, fill=(180, 40, 40))

    # colour bar
    bar_x, bar_w, bar_top, bar_h = width_px - 118, 20, beam_cy - beam_h, beam_h * 2
    for step in range(bar_h):
        draw.line([(bar_x, bar_top + step), (bar_x + bar_w, bar_top + step)],
                  fill=colormap(1.0 - step / float(bar_h)))
    draw.rectangle([bar_x, bar_top, bar_x + bar_w, bar_top + bar_h], outline=(150, 158, 168))
    draw.text((bar_x - 4, bar_top - 26), "von Mises (MPa)", font=font_tiny, fill=(60, 66, 76))
    draw.text((bar_x + bar_w + 8, bar_top + bar_h - 8), "0", font=font_tiny, fill=(60, 66, 76))
    draw.text((bar_x + bar_w + 8, bar_top - 2), "%.0f" % peak, font=font_tiny, fill=(60, 66, 76))

    # ---------------------------------------------------------------- panel 2
    chart_left, chart_top, chart_w, chart_h = 110, 520, width_px - 260, 230
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = chart_top + chart_h * frac
        draw.line([(chart_left, y), (chart_left + chart_w, y)], fill=(238, 241, 246) if frac else (210, 216, 224))
    draw.line([(chart_left, chart_top), (chart_left, chart_top + chart_h)], fill=(210, 216, 224))
    draw.line([(chart_left + chart_w, chart_top), (chart_left + chart_w, chart_top + chart_h)], fill=(210, 216, 224))

    analytic_points = []
    fe_points = []
    for index in range(81):
        fraction = index / 80.0
        fe_value = _lerp(curve, fraction) / max_disp
        x = chart_left + chart_w * fraction
        fe_points.append((x, chart_top + chart_h * (0.04 + 0.9 * fe_value)))
        shape = fraction ** 2 * (3.0 - fraction) / 2.0  # x²(3L−x)/(2L³)
        analytic_points.append((x, chart_top + chart_h * (0.04 + 0.9 * shape)))

    draw.line(fe_points, fill=(31, 111, 208), width=3)
    for index in range(0, len(analytic_points) - 1, 4):
        draw.line([analytic_points[index], analytic_points[index + 1]], fill=(96, 104, 116), width=2)

    draw.text((chart_left, chart_top - 40),
              "挠度曲线 x–δ：FE %.4f mm（蓝实线） vs 解析 %.4f mm（灰虚线 δ=Fx²(3L−x)/6EI）"
              % (info["maxDisp"], analytic["disp"]), font=font_sub, fill=(60, 66, 76))
    for frac, label in ((0.04, "0"), (0.49, "%.3f" % (info["maxDisp"] / 2.0)),
                        (0.94, "%.3f mm" % info["maxDisp"])):
        draw.text((chart_left - 8, chart_top + chart_h * frac - 7), label,
                  font=font_tiny, fill=(130, 138, 148), anchor="ra")
    draw.text((chart_left, chart_top + chart_h + 10), "x = 0（固定端）", font=font_tiny, fill=(120, 128, 138))
    draw.text((chart_left + chart_w - 130, chart_top + chart_h + 10), "x = %.0f mm（自由端）" % length,
              font=font_tiny, fill=(120, 128, 138))

    # ---------------------------------------------------------------- footer
    draw.line([(96, height_px - 62), (width_px - 96, height_px - 62)], fill=(230, 234, 240))
    material = spec.get("material", {})
    draw.text((96, height_px - 48),
              "材料 E=%.0f MPa ν=%.2f · SOL 101 · 固定端 SPC1 123456 · 自由端 %d 节点均分载荷 %.2f N/节点"
              % (float(material.get("E", 210000.0)), float(material.get("nu", 0.3)),
                 info.get("loadedNodes", 0), load / max(1, info.get("loadedNodes", 1))),
              font=font_tiny, fill=(140, 148, 158))
    draw.text((96, height_px - 30),
              "求解器 NX Nastran（%s） · BDF %d 卡 · 结果来自 .f06 位移/应力块"
              % (os.path.basename(info.get("solverExe", "nastran.exe")), info.get("deckLines", 0)),
              font=font_tiny, fill=(160, 168, 178))

    image.save(path)
    return {"path": path, "width": width_px, "height": height_px, "exaggeration": round(exaggeration, 1)}
