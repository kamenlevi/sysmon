"""Dynamically generate the tray icon as a PNG using Cairo."""
import os
import tempfile

import cairo

_ICON_DIR = os.path.join(tempfile.gettempdir(), "sysmon_icons")
os.makedirs(_ICON_DIR, exist_ok=True)
_COUNTER = [0]
_LAST_KEY = [None]
_LAST_PATH = [None]


def generate_tray_icon(
    cpu_pct: float,
    ram_pct: float,
    gpu_pct: float = 0.0,
    has_gpu: bool = False,
    has_warning: bool = False,
    size: int = 22,
) -> str:
    """Draw bars for cpu/gpu/ram. Returns path to written PNG."""
    key = (
        int(round(cpu_pct)),
        int(round(ram_pct)),
        int(round(gpu_pct)) if has_gpu else -1,
        bool(has_warning),
        size,
    )
    if _LAST_KEY[0] == key and _LAST_PATH[0] is not None and os.path.exists(_LAST_PATH[0]):
        return _LAST_PATH[0]

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)

    ctx.set_source_rgba(0, 0, 0, 0)
    ctx.paint()

    bars = [cpu_pct, gpu_pct if has_gpu else None, ram_pct]

    n = len(bars)
    pad = 1
    gap = 1
    total_gap = gap * (n - 1) + pad * 2
    bar_w = max(2, (size - total_gap) // n)

    for i, pct in enumerate(bars):
        x = pad + i * (bar_w + gap)
        y_bg_top = pad
        bg_h = size - pad * 2

        ctx.set_source_rgba(0.35, 0.35, 0.35, 0.6)
        _rounded_rect(ctx, x, y_bg_top, bar_w, bg_h, 1)
        ctx.fill()

        if pct is None:
            continue

        bar_h = max(1, int(pct / 100.0 * bg_h))
        ctx.set_source_rgba(0.85, 0.85, 0.85, 0.9)
        y_fill = pad + bg_h - bar_h
        _rounded_rect(ctx, x, y_fill, bar_w, bar_h, 1)
        ctx.fill()

    _COUNTER[0] = (_COUNTER[0] + 1) % 2
    path = os.path.join(_ICON_DIR, f"sysmon_icon_{_COUNTER[0]}.png")
    surface.write_to_png(path)
    _LAST_KEY[0] = key
    _LAST_PATH[0] = path
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
