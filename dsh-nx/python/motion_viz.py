"""Render NX Motion result frames (JSON) into a report PNG and an animated GIF.

Input JSON (written by nxworker.motion_report):
{
  "title": str, "subtitle": [str, ...], "footer": [str, ...],
  "steps": [0, 5, ...],
  "links": [{"name": "L004", "frames": [[step, [r11..r33], [x, y, z]], ...]}, ...],
  "summary": [{"link": "L004", "start": [..], "end": [..], "deltaMm": 0.0,
                "rotDeg": 0.0, "excursionMm": 119.7, "swingDeg": 37.2}, ...]
}
"""

import json
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)

INK = (32, 36, 44)
MUTED = (112, 120, 132)
GRID = (228, 232, 238)
PAPER = (255, 255, 255)
PANEL = (247, 249, 252)
BORDER = (215, 222, 232)
BLUE = (38, 108, 198)
ORANGE = (232, 132, 30)
GREEN = (34, 148, 96)
RED = (198, 58, 58)
PURPLE = (126, 78, 178)
TEAL = (26, 158, 186)
PALETTE = (BLUE, ORANGE, GREEN, RED, PURPLE, TEAL, (168, 118, 36), (118, 60, 138))


def load_font(size, bold=False):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                index = 1 if (bold and path.endswith("msyh.ttc")) else 0
                return ImageFont.truetype(path, size, index=index)
            except Exception:
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    continue
    return ImageFont.load_default()


def rotation_angle(matrix):
    trace = matrix[0] + matrix[4] + matrix[8]
    return math.degrees(math.acos(max(-1.0, min(1.0, (trace - 1.0) / 2.0))))


def nice_span(low, high, pad=0.10):
    if abs(high - low) < 1e-9:
        return low - 1.0, high + 1.0
    span = high - low
    return low - span * pad, high + span * pad


def robust_range(values, low_pct=0.02, high_pct=0.98):
    """Trim the few far-away outliers so the bulk of the motion fills the frame."""
    if not values:
        return 0.0, 1.0
    ordered = sorted(values)
    if len(ordered) < 8:
        return ordered[0], ordered[-1]
    low = ordered[int(len(ordered) * low_pct)]
    high = ordered[min(len(ordered) - 1, int(len(ordered) * high_pct))]
    if high - low < 1e-9:
        return ordered[0], ordered[-1]
    return low, high


def polyline(draw, box, points, colour, width=2, xspan=None, yspan=None):
    left, top, right, bottom = box
    if not points:
        return []
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    x0, x1 = xspan if xspan else nice_span(min(xs), max(xs))
    y0, y1 = yspan if yspan else nice_span(min(ys), max(ys))
    if abs(x1 - x0) < 1e-12 or abs(y1 - y0) < 1e-12:
        return []
    screen = []
    for x, y in points:
        screen.append((left + (x - x0) / (x1 - x0) * (right - left),
                       bottom - (y - y0) / (y1 - y0) * (bottom - top)))
    if len(screen) > 1:
        draw.line(screen, fill=colour, width=width, joint="curve")
    return screen


def plot_box(draw, box, xlabel, ylabel, font, ticks_x=6, ticks_y=4):
    left, top, right, bottom = box
    draw.rectangle([left, top, right, bottom], outline=BORDER)
    for index in range(ticks_x + 1):
        x = left + (right - left) * index / ticks_x
        draw.line([(x, top), (x, bottom)], fill=GRID)
    for index in range(ticks_y + 1):
        y = bottom - (bottom - top) * index / ticks_y
        draw.line([(left, y), (right, y)], fill=GRID)
    if xlabel:
        draw.text((right, bottom + 6), xlabel, font=font, fill=MUTED, anchor="ra")
    if ylabel:
        draw.text((left - 10, top), ylabel, font=font, fill=MUTED, anchor="ra")


def panel(draw, box, title, font, hint=None, small=None):
    left, top, right, bottom = box
    draw.rectangle([left - 14, top - 34, right + 14, bottom + 34], fill=PANEL, outline=BORDER)
    draw.text((left, top - 30), title, font=font, fill=INK, anchor="la")
    if hint and small is not None:
        draw.text((right, top - 28), hint, font=small, fill=MUTED, anchor="ra")


def span_join(parts, widths):
    cells = []
    for value, width_chars in zip(parts, widths):
        value = str(value)
        cells.append(value + " " * max(0, width_chars - len(value)))
    return "".join(cells)


def moving_links(links):
    rows = []
    for link in links:
        frames = link.get("frames") or []
        if len(frames) < 2:
            continue
        start = frames[0][2]
        base = rotation_angle(frames[0][1])
        excursion = max(math.dist(start, frame[2]) for frame in frames)
        swing = max(abs(rotation_angle(frame[1]) - base) for frame in frames)
        if excursion > 0.05 or swing > 0.2:
            rows.append((link, excursion, swing))
    rows.sort(key=lambda item: -item[1])
    return rows


def render_report(data, out_png):
    width, height = 1500, 1040
    image = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(image)

    f_title = load_font(34, bold=True)
    f_sub = load_font(19)
    f_panel = load_font(21, bold=True)
    f_body = load_font(15)
    f_small = load_font(14)

    draw.text((44, 26), data.get("title", "NX Motion 运动仿真结果"), font=f_title, fill=INK, anchor="la")
    for index, line in enumerate((data.get("subtitle") or [])[:3]):
        draw.text((46, 76 + index * 24), line, font=f_sub, fill=MUTED, anchor="la")

    links = data.get("links", [])
    steps = data.get("steps", [])
    summary = data.get("summary", [])
    moving = moving_links(links)

    # ---------------------------------------------------------------- trajectories
    box_a = (70, 206, 700, 596)
    panel(draw, box_a, "运动轨迹（X–Y 平面）", f_panel, "○ 起点 / □ 终点", f_small)
    xs = [frame[2][0] for link in links for frame in (link.get("frames") or [])]
    ys = [frame[2][1] for link in links for frame in (link.get("frames") or [])]
    if xs:
        lowx, highx = robust_range(xs)
        lowy, highy = robust_range(ys)
        pad = max(6.0, 0.10 * max(highx - lowx, highy - lowy, 1.0))
        xspan = (lowx - pad, highx + pad)
        yspan = (lowy - pad, highy + pad)
        clen = max(xspan[1] - xspan[0], yspan[1] - yspan[0])
        cx = (xspan[0] + xspan[1]) / 2
        cy = (yspan[0] + yspan[1]) / 2
        xspan = (cx - clen / 2, cx + clen / 2)
        yspan = (cy - clen / 2, cy + clen / 2)
        plot_box(draw, box_a, "X (mm)", "Y (mm)", f_small)
        for index, (link, excursion, swing) in enumerate(moving[:6]):
            colour = PALETTE[index % len(PALETTE)]
            screen = polyline(draw, box_a, [(frame[2][0], frame[2][1]) for frame in link["frames"]],
                              colour, 2, xspan, yspan)
            if screen:
                draw.ellipse([screen[0][0] - 4, screen[0][1] - 4, screen[0][0] + 4, screen[0][1] + 4],
                             outline=colour, width=2)
                draw.rectangle([screen[-1][0] - 4, screen[-1][1] - 4, screen[-1][0] + 4, screen[-1][1] + 4],
                               outline=colour, width=2)
                mid = screen[len(screen) // 2]
                draw.text((mid[0] + 6, mid[1] - 16), link["name"], font=f_small, fill=colour)
        for link in links:
            if any(link is item[0] for item in moving):
                continue
            frames = link.get("frames") or []
            if not frames:
                continue
            x, y = frames[0][2][0], frames[0][2][1]
            if not (xspan[0] <= x <= xspan[1] and yspan[0] <= y <= yspan[1]):
                continue
            sx = box_a[0] + (x - xspan[0]) / (xspan[1] - xspan[0]) * (box_a[2] - box_a[0])
            sy = box_a[3] - (y - yspan[0]) / (yspan[1] - yspan[0]) * (box_a[3] - box_a[1])
            draw.line([(sx - 4, sy), (sx + 4, sy)], fill=MUTED)
            draw.line([(sx, sy - 4), (sx, sy + 4)], fill=MUTED)

    # ---------------------------------------------------------------- displacement
    box_b = (754, 206, 1456, 452)
    panel(draw, box_b, "关键构件行程（相对起始位姿，mm）", f_panel,
          " / ".join(link["name"] for link, _e, _s in moving[:5]), f_small)
    plot_box(draw, box_b, "时间步", "", f_small)
    if steps:
        step_span = (min(steps), max(steps))
        series = []
        for link, excursion, swing in moving[:6]:
            start = link["frames"][0][2]
            series.append([(frame[0], math.dist(start, frame[2])) for frame in link["frames"]])
        ymax = max([max(value for _x, value in item) for item in series], default=1.0) or 1.0
        for index, item in enumerate(series):
            colour = PALETTE[index % len(PALETTE)]
            polyline(draw, box_b, item, colour, 2, step_span, (0.0, ymax * 1.12))
            draw.text((box_b[0] + 12 + index * 64, box_b[3] - 22), moving[index][0]["name"],
                      font=f_small, fill=colour)
        draw.text((box_b[0] + 8, box_b[1] + 4), "%.1f mm" % (ymax * 1.12), font=f_small,
                  fill=MUTED, anchor="la")

    # ---------------------------------------------------------------- rotation
    box_c = (754, 522, 1456, 768)
    panel(draw, box_c, "关键构件姿态角（相对起始，deg）", f_panel)
    plot_box(draw, box_c, "时间步", "", f_small)
    if steps:
        step_span = (min(steps), max(steps))
        series = []
        for link, excursion, swing in moving[:6]:
            base = rotation_angle(link["frames"][0][1])
            series.append([(frame[0], rotation_angle(frame[1]) - base) for frame in link["frames"]])
        rmax = max([max(abs(value) for _x, value in item) for item in series], default=1.0) or 1.0
        for index, item in enumerate(series):
            polyline(draw, box_c, item, PALETTE[index % len(PALETTE)], 2, step_span, (-rmax * 1.12, rmax * 1.12))
        draw.text((box_c[0] + 8, box_c[1] + 4), "%.1f deg" % (rmax * 1.12), font=f_small,
                  fill=MUTED, anchor="la")

    # ---------------------------------------------------------------- table
    box_d = (70, 676, 700, 946)
    panel(draw, box_d, "构件运动汇总", f_panel, "行程 / 摆角 = 全程最大行程与最大转角", f_small)
    columns = ((0, "构件"), (72, "行程(mm)"), (186, "摆角(deg)"), (302, "运动类型"))
    for offset, label in columns:
        draw.text((box_d[0] + offset, box_d[1] - 2), label, font=f_small, fill=MUTED, anchor="la")
    ranked = sorted(summary, key=lambda row: -float(row.get("excursionMm", row.get("deltaMm", 0.0))))
    for index, row in enumerate(ranked[:12]):
        excursion = float(row.get("excursionMm", row.get("deltaMm", 0.0)))
        swing = float(row.get("swingDeg", row.get("rotDeg", 0.0)))
        state = "固定" if (excursion <= 0.05 and swing <= 0.2) else ("转动" if swing > 5.0 else "移动")
        cells = (str(row.get("link", ""))[:8], "%.2f" % excursion, "%.2f" % swing, state)
        for (offset, _label), value in zip(columns, cells):
            draw.text((box_d[0] + offset, box_d[1] + 20 + index * 20), value, font=f_body,
                      fill=INK, anchor="la")

    for index, line in enumerate((data.get("footer") or [])[:2]):
        draw.text((46, height - 50 + index * 20), line, font=f_small, fill=MUTED, anchor="la")

    image.save(out_png, "PNG")
    return out_png


def render_animation(data, out_gif, frame_limit=72, size=(960, 660)):
    links = data.get("links", [])
    steps = data.get("steps", [])
    moving = moving_links(links)
    if not moving or not steps:
        return None

    width, height = size
    f_head = load_font(24, bold=True)
    f_tick = load_font(16)
    f_label = load_font(15)
    base = Image.new("RGB", size, PAPER)

    xs = [frame[2][0] for link in links for frame in (link.get("frames") or [])]
    ys = [frame[2][1] for link in links for frame in (link.get("frames") or [])]
    lowx, highx = robust_range(xs)
    lowy, highy = robust_range(ys)
    pad = max(8.0, 0.12 * max(highx - lowx, highy - lowy, 1.0))
    span_x = (highx - lowx) + 2 * pad
    span_y = (highy - lowy) + 2 * pad
    cx = (highx + lowx) / 2
    cy = (highy + lowy) / 2
    avail_w, avail_h = width - 150, height - 190
    if span_x / span_y > avail_w / avail_h:
        span_x = max(span_x, span_y * avail_w / avail_h)
    else:
        span_y = max(span_y, span_x * avail_h / avail_w)
    plot_w = (width - 150) * 0.60
    avail_w, avail_h = plot_w, height - 190
    scale = min(avail_w / span_x, avail_h / span_y)
    view_w = span_x * scale
    view_h = span_y * scale
    left = 80 + (plot_w - view_w) / 2
    top = 110 + (avail_h - view_h) / 2
    right = left + view_w
    bottom = top + view_h

    def project(point):
        return (left + (point[0] - cx) * scale + view_w / 2,
                bottom - (point[1] - cy) * scale - view_h / 2)

    images = []
    total = len(steps)
    for index in range(total):
        canvas = base.copy()
        draw = ImageDraw.Draw(canvas)
        draw.text((40, 24), data.get("title", "NX Motion"), font=f_head, fill=INK)
        draw.text((42, 62), "时间步 %d / %d   ·   运动构件 %d 个" % (steps[index], steps[-1], len(moving)),
                  font=f_tick, fill=MUTED)
        draw.rectangle([left, top, right, bottom], outline=BORDER)
        for tick in range(7):
            x = left + (right - left) * tick / 6
            draw.line([(x, top), (x, bottom)], fill=GRID)
        for tick in range(5):
            y = top + (bottom - top) * tick / 4
            draw.line([(left, y), (right, y)], fill=GRID)

        for colour_index, (link, excursion, swing) in enumerate(moving[:6]):
            colour = PALETTE[colour_index % len(PALETTE)]
            trail = [project(frame[2][:2]) for frame in link["frames"][: index + 1]]
            for dot in range(0, max(0, len(trail) - 1)):
                shade = tuple(int(255 - (255 - channel) * (0.30 + 0.70 * dot / max(1, len(trail) - 1)))
                              for channel in colour)
                if dot + 1 < len(trail):
                    draw.line([trail[dot], trail[dot + 1]], fill=shade, width=2)
            frame = link["frames"][index]
            origin = project(frame[2][:2])
            matrix = frame[1]
            for axis_index, tint in enumerate((RED, GREEN, BLUE)):
                direction = (matrix[axis_index], matrix[3 + axis_index])
                tip = (origin[0] + direction[0] * 30.0 * scale,
                       origin[1] - direction[1] * 30.0 * scale)
                draw.line([origin, tip], fill=tint, width=2)
            draw.ellipse([origin[0] - 5, origin[1] - 5, origin[0] + 5, origin[1] + 5], fill=colour)
            draw.text((origin[0] + 8, origin[1] - 8), link["name"], font=f_label, fill=colour)

        for link in links:
            if any(link is item[0] for item in moving):
                continue
            frames = link.get("frames") or []
            if not frames:
                continue
            point = project(frames[0][2][:2])
            if not (left <= point[0] <= right and top <= point[1] <= bottom):
                continue
            draw.line([(point[0] - 4, point[1]), (point[0] + 4, point[1])], fill=MUTED)
            draw.line([(point[0], point[1] - 4), (point[0], point[1] + 4)], fill=MUTED)

        draw.text((left, height - 46), "彩色轨迹 = 构件位移路径；红/绿/蓝短线 = 构件局部 X/Y/Z 轴；灰色十字 = 固定构件",
                  font=f_label, fill=MUTED)

        # live numbers on the right so the wide canvas carries real information
        info_x = left + view_w + 46
        draw.text((info_x, top), "当前时刻读数", font=f_label, fill=INK)
        draw.text((info_x, top + 24), "构件    行程(mm)    摆角(deg)", font=f_label, fill=MUTED)
        for colour_index, (link, excursion, swing) in enumerate(moving[:6]):
            frame = link["frames"][index]
            start = link["frames"][0][2]
            distance = math.dist(start, frame[2])
            angle = rotation_angle(frame[1]) - rotation_angle(link["frames"][0][1])
            draw.text((info_x, top + 48 + colour_index * 22),
                      "%-7s %8.2f %9.1f" % (link["name"][:7], distance, angle),
                      font=f_label, fill=PALETTE[colour_index % len(PALETTE)])
        draw.text((info_x, top + 48 + min(6, len(moving)) * 22 + 16),
                  "全程最大：%.1f mm" % (moving[0][1] if moving else 0.0), font=f_label, fill=MUTED)
        images.append(canvas)

    keep = images[:: max(1, len(images) // frame_limit)]
    keep[0].save(out_gif, save_all=True, append_images=keep[1:], duration=90, loop=0, optimize=True)
    return out_gif


def main(argv):
    if len(argv) < 3:
        print("usage: motion_viz.py <frames.json> <outdir>", file=sys.stderr)
        return 2
    source, outdir = argv[1], argv[2]
    with open(source, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    os.makedirs(outdir, exist_ok=True)
    png = render_report(data, os.path.join(outdir, "motion_report.png"))
    gif = render_animation(data, os.path.join(outdir, "motion_animation.gif"))
    print(json.dumps({"report": png, "animation": gif,
                      "reportBytes": os.path.getsize(png),
                      "animationBytes": os.path.getsize(gif) if gif else 0}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
