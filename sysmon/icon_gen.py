"""A single STATIC menu-bar icon (a small bar-chart glyph).

The icon is drawn once and never changes, so the tray never blinks. Live
CPU / RAM figures are shown as a text label next to it (see indicator.py),
which updates without any flicker.
"""
import math
import os
import tempfile

import cairo

_ICON_DIR = os.path.join(tempfile.gettempdir(), "sysmon_icons")
os.makedirs(_ICON_DIR, exist_ok=True)
_PATH = [None]


def generate_tray_icon(*_args, size: int = 22, **_kwargs) -> str:
    """Return the path to the static icon, drawing it once and caching it."""
    if _PATH[0] is not None and os.path.exists(_PATH[0]):
        return _PATH[0]

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)
    ctx.set_source_rgba(0, 0, 0, 0)
    ctx.paint()

    # Three static bars of fixed heights — a clean "stats" glyph, white so
    # it stays visible on the dark Ubuntu top bar.
    heights = [0.45, 0.85, 0.65]
    n = len(heights)
    pad = 3
    gap = 2
    bar_w = (size - pad * 2 - gap * (n - 1)) / n
    base_y = size - pad

    ctx.set_source_rgba(0.93, 0.93, 0.93, 1.0)
    for i, h in enumerate(heights):
        x = pad + i * (bar_w + gap)
        bar_h = h * (size - pad * 2)
        _rounded_rect(ctx, x, base_y - bar_h, bar_w, bar_h, 1.2)
        ctx.fill()

    path = os.path.join(_ICON_DIR, "sysmon_static.png")
    surface.write_to_png(path)
    _PATH[0] = path
    return path


_DONUT_CACHE = {}  # (key, size) -> last rounded pct written


def gen_donut_icon(pct: float, key: str, size: int = 20) -> str:
    """Draw a small circular gauge for a menu-item icon. Returns a PNG path.

    Skips redrawing when the rounded percentage hasn't changed, so the menu
    does no per-tick disk work while usage is steady.
    """
    pct = max(0.0, min(pct, 100.0))
    path = os.path.join(_ICON_DIR, f"donut_{key}.png")
    rp = int(round(pct))
    if _DONUT_CACHE.get((key, size)) == rp and os.path.exists(path):
        return path

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)
    ctx.set_source_rgba(0, 0, 0, 0)
    ctx.paint()

    cx = cy = size / 2.0
    r = size / 2.0 - 2.5
    lw = 3.5

    # Track ring
    ctx.set_line_width(lw)
    ctx.set_source_rgba(0.74, 0.74, 0.74, 1.0)
    ctx.arc(cx, cy, r, 0, 2 * math.pi)
    ctx.stroke()

    # Value arc (clockwise from top)
    start = -math.pi / 2
    end = start + 2 * math.pi * (pct / 100.0)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_source_rgba(0.12, 0.12, 0.12, 1.0)
    ctx.arc(cx, cy, r, start, end)
    ctx.stroke()

    surface.write_to_png(path)
    _DONUT_CACHE[(key, size)] = rp
    return path


def _rounded_rect(ctx, x, y, w, h, r):
    if h <= 0 or w <= 0:
        return
    r = min(r, w / 2, h / 2)
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -1.5708, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, 1.5708)
    ctx.arc(x + r, y + h - r, r, 1.5708, 3.1416)
    ctx.arc(x + r, y + r, r, 3.1416, 4.7124)
    ctx.close_path()
