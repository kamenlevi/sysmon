"""Dynamic menu-bar icon: a live CPU sparkline + a RAM bar.

The icon is dynamic (it updates with usage) but has a CONSTANT width and
height — it never resizes or jumps. Modelled on the live menu-bar graphs
of the Stats macOS app.
"""
import os
import tempfile

import cairo

_ICON_DIR = os.path.join(tempfile.gettempdir(), "sysmon_icons")
os.makedirs(_ICON_DIR, exist_ok=True)
_COUNTER = [0]

# Fixed icon geometry — never changes. Square, because the Ubuntu
# AppIndicator only reliably renders square icons.
WIDTH = 22
HEIGHT = 22


def generate_tray_icon(cpu_history=None, ram_pct=0.0, **_kwargs) -> str:
    """Draw a live CPU area-sparkline with a RAM bar on the right.

    cpu_history: list of recent CPU percentages (0-100), oldest first.
    Returns the path to the written PNG.
    """
    cpu_history = list(cpu_history or [])

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, HEIGHT)
    ctx = cairo.Context(surface)
    ctx.set_source_rgba(0, 0, 0, 0)
    ctx.paint()

    pad = 2
    bar_w = 3
    gap = 2
    graph_x0 = pad
    graph_x1 = WIDTH - pad - bar_w - gap
    graph_w = graph_x1 - graph_x0
    graph_top = pad
    graph_bot = HEIGHT - pad
    graph_h = graph_bot - graph_top

    # ── CPU area sparkline ─────────────────────────────────────────────
    n = len(cpu_history)
    if n >= 2:
        def pt(i, v):
            x = graph_x0 + (i / (n - 1)) * graph_w
            y = graph_bot - (max(0.0, min(v, 100.0)) / 100.0) * graph_h
            return x, y

        # Filled area
        ctx.move_to(graph_x0, graph_bot)
        for i, v in enumerate(cpu_history):
            ctx.line_to(*pt(i, v))
        ctx.line_to(graph_x1, graph_bot)
        ctx.close_path()
        ctx.set_source_rgba(0.92, 0.92, 0.92, 0.30)
        ctx.fill()

        # Line on top
        ctx.move_to(*pt(0, cpu_history[0]))
        for i, v in enumerate(cpu_history):
            ctx.line_to(*pt(i, v))
        ctx.set_source_rgba(0.96, 0.96, 0.96, 0.95)
        ctx.set_line_width(1.3)
        ctx.set_line_join(cairo.LINE_JOIN_ROUND)
        ctx.stroke()
    else:
        # Baseline before history fills in
        ctx.move_to(graph_x0, graph_bot)
        ctx.line_to(graph_x1, graph_bot)
        ctx.set_source_rgba(0.96, 0.96, 0.96, 0.6)
        ctx.set_line_width(1.0)
        ctx.stroke()

    # ── RAM vertical bar ───────────────────────────────────────────────
    bx = WIDTH - pad - bar_w
    # Track
    ctx.set_source_rgba(1.0, 1.0, 1.0, 0.22)
    _rounded_rect(ctx, bx, graph_top, bar_w, graph_h, 1.5)
    ctx.fill()
    # Fill from bottom
    rh = max(1.0, (max(0.0, min(ram_pct, 100.0)) / 100.0) * graph_h)
    ctx.set_source_rgba(0.96, 0.96, 0.96, 0.95)
    _rounded_rect(ctx, bx, graph_bot - rh, bar_w, rh, 1.5)
    ctx.fill()

    _COUNTER[0] = (_COUNTER[0] + 1) % 2
    path = os.path.join(_ICON_DIR, f"sysmon_icon_{_COUNTER[0]}.png")
    surface.write_to_png(path)
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
