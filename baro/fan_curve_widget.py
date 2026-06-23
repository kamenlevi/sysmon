"""
Interactive fan curve editor widget built with Cairo + Gtk.DrawingArea.

Drag control points to adjust the curve.
Left-click on empty space to add a point.
Right-click a point to remove it.
"""
import math
from typing import Callable, List, Optional, Tuple

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GObject


Point = Tuple[float, float]   # (temp_celsius, speed_percent)

TEMP_MIN = 20.0
TEMP_MAX = 100.0
SPEED_MIN = 0.0
SPEED_MAX = 100.0

# Colours (RGBA 0-1)
C_BG       = (0.11, 0.11, 0.18, 1.0)
C_GRID     = (0.22, 0.22, 0.33, 1.0)
C_AXIS     = (0.45, 0.45, 0.60, 1.0)
C_CURVE    = (0.34, 0.71, 0.98, 1.0)
C_FILL     = (0.34, 0.71, 0.98, 0.18)
C_POINT    = (0.34, 0.71, 0.98, 1.0)
C_POINT_HL = (0.99, 0.84, 0.22, 1.0)   # highlighted / dragging
C_POINT_RIM= (1.00, 1.00, 1.00, 0.8)
C_TEXT     = (0.55, 0.55, 0.72, 1.0)
C_LABEL    = (0.75, 0.75, 0.90, 1.0)
C_WARN     = (0.95, 0.24, 0.24, 0.6)   # danger zone strip (>85°C)

POINT_R     = 7.0   # normal point radius
POINT_R_HL  = 9.0   # highlighted
HIT_RADIUS  = 14.0  # click detection radius


class FanCurveEditor(Gtk.DrawingArea):
    """
    Interactive fan curve editor.

    Signals:
        curve-changed(points)  — emitted after any edit; points is List[Point]
    """

    __gsignals__ = {
        "curve-changed": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    def __init__(
        self,
        points: Optional[List[Point]] = None,
        on_change: Optional[Callable[[List[Point]], None]] = None,
        compact: bool = False,
    ):
        super().__init__()
        self._compact = compact
        self._pad = (28, 8, 10, 28) if not compact else (24, 6, 8, 22)
        # (left, right, top, bottom)

        self._points: List[Point] = sorted(
            points or [(30, 20), (50, 35), (65, 55), (75, 75), (85, 90), (95, 100)],
            key=lambda p: p[0],
        )
        self._hover_idx: Optional[int] = None
        self._drag_idx: Optional[int] = None
        self._on_change = on_change

        min_w, min_h = (260, 140) if not compact else (220, 110)
        self.set_size_request(min_w, min_h)

        mask = (
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.add_events(mask)

        self.connect("draw", self._draw)
        self.connect("button-press-event", self._press)
        self.connect("button-release-event", self._release)
        self.connect("motion-notify-event", self._motion)
        self.connect("leave-notify-event", self._leave)

    # ── Public API ───────────────────────────────────────────────────────────

    def get_points(self) -> List[Point]:
        return list(self._points)

    def set_points(self, points: List[Point]):
        self._points = sorted(points, key=lambda p: p[0])
        self.queue_draw()

    # ── Coordinate helpers ───────────────────────────────────────────────────

    def _draw_area(self):
        """Returns (x0, y0, draw_w, draw_h) of the plot area in pixels."""
        pl, pr, pt, pb = self._pad
        w = self.get_allocated_width()
        h = self.get_allocated_height()
        return pl, pt, w - pl - pr, h - pt - pb

    def _to_px(self, temp: float, speed: float) -> Tuple[float, float]:
        x0, y0, dw, dh = self._draw_area()
        px = x0 + (temp - TEMP_MIN) / (TEMP_MAX - TEMP_MIN) * dw
        py = y0 + (1.0 - (speed - SPEED_MIN) / (SPEED_MAX - SPEED_MIN)) * dh
        return px, py

    def _from_px(self, px: float, py: float) -> Tuple[float, float]:
        x0, y0, dw, dh = self._draw_area()
        temp = TEMP_MIN + (px - x0) / dw * (TEMP_MAX - TEMP_MIN)
        speed = SPEED_MIN + (1.0 - (py - y0) / dh) * (SPEED_MAX - SPEED_MIN)
        return (
            max(TEMP_MIN, min(TEMP_MAX, temp)),
            max(SPEED_MIN, min(SPEED_MAX, speed)),
        )

    def _nearest_point(self, px: float, py: float) -> Optional[int]:
        best_i, best_d = None, HIT_RADIUS ** 2
        for i, (t, s) in enumerate(self._points):
            cx, cy = self._to_px(t, s)
            d = (px - cx) ** 2 + (py - cy) ** 2
            if d < best_d:
                best_d = d
                best_i = i
        return best_i

    # ── Events ───────────────────────────────────────────────────────────────

    def _press(self, _, ev):
        idx = self._nearest_point(ev.x, ev.y)
        if ev.button == 1:
            if idx is not None:
                self._drag_idx = idx
            else:
                # Add new point
                t, s = self._from_px(ev.x, ev.y)
                # Don't add if too close to an existing point temp
                for pt, ps in self._points:
                    if abs(pt - t) < 3:
                        return
                self._points.append((round(t, 1), round(s, 1)))
                self._points.sort(key=lambda p: p[0])
                self._drag_idx = next(
                    i for i, (pt, _) in enumerate(self._points) if abs(pt - t) < 1
                )
                self._notify()
        elif ev.button == 3 and idx is not None:
            # Remove point (keep at least 2)
            if len(self._points) > 2:
                del self._points[idx]
                self._hover_idx = None
                self._notify()
        self.queue_draw()

    def _release(self, _, ev):
        if self._drag_idx is not None:
            self._notify()
        self._drag_idx = None
        self.queue_draw()

    def _motion(self, _, ev):
        if self._drag_idx is not None:
            t, s = self._from_px(ev.x, ev.y)
            t = round(t, 1)
            s = round(s, 1)
            idx = self._drag_idx
            # Clamp between neighbours
            if idx > 0:
                t = max(self._points[idx - 1][0] + 2, t)
            if idx < len(self._points) - 1:
                t = min(self._points[idx + 1][0] - 2, t)
            self._points[idx] = (t, s)
            self.queue_draw()
        else:
            old = self._hover_idx
            self._hover_idx = self._nearest_point(ev.x, ev.y)
            if self._hover_idx != old:
                cursor = Gdk.Cursor.new_from_name(
                    self.get_display(),
                    "grab" if self._hover_idx is not None else "default",
                )
                self.get_window().set_cursor(cursor)
                self.queue_draw()

    def _leave(self, *_):
        self._hover_idx = None
        self.queue_draw()

    def _notify(self):
        pts = list(self._points)
        self.emit("curve-changed", pts)
        if self._on_change:
            self._on_change(pts)

    # ── Drawing ──────────────────────────────────────────────────────────────

    def _draw(self, _, ctx):
        w = self.get_allocated_width()
        h = self.get_allocated_height()
        x0, y0, dw, dh = self._draw_area()
        pl, pr, pt, pb = self._pad
        compact = self._compact

        # Background
        ctx.set_source_rgba(*C_BG)
        ctx.rectangle(0, 0, w, h)
        ctx.fill()

        # Danger zone: > 85°C strip
        dx, _ = self._to_px(85, 0)
        ctx.set_source_rgba(*C_WARN)
        ctx.rectangle(dx, y0, x0 + dw - dx, dh)
        ctx.fill()

        # Grid lines
        ctx.set_line_width(0.5)
        ctx.set_source_rgba(*C_GRID)
        if not compact:
            for temp in range(30, 101, 10):
                gx, _ = self._to_px(temp, 0)
                ctx.move_to(gx, y0)
                ctx.line_to(gx, y0 + dh)
                ctx.stroke()
            for spd in range(0, 101, 20):
                _, gy = self._to_px(0, spd)
                ctx.move_to(x0, gy)
                ctx.line_to(x0 + dw, gy)
                ctx.stroke()
        else:
            for temp in range(40, 101, 20):
                gx, _ = self._to_px(temp, 0)
                ctx.move_to(gx, y0)
                ctx.line_to(gx, y0 + dh)
                ctx.stroke()
            for spd in range(0, 101, 25):
                _, gy = self._to_px(0, spd)
                ctx.move_to(x0, gy)
                ctx.line_to(x0 + dw, gy)
                ctx.stroke()

        # Axes border
        ctx.set_line_width(1.0)
        ctx.set_source_rgba(*C_AXIS)
        ctx.rectangle(x0, y0, dw, dh)
        ctx.stroke()

        # Axis labels
        ctx.set_source_rgba(*C_TEXT)
        ctx.select_font_face("Sans", 0, 0)
        ctx.set_font_size(8 if not compact else 7)

        if not compact:
            for temp in range(20, 101, 20):
                gx, _ = self._to_px(temp, 0)
                ctx.move_to(gx - 8, y0 + dh + 11)
                ctx.show_text(f"{temp}°")
            for spd in range(0, 101, 20):
                _, gy = self._to_px(20, spd)
                lbl = f"{spd}%"
                ctx.move_to(2, gy + 4)
                ctx.show_text(lbl)
        else:
            for temp in (30, 50, 70, 90):
                gx, _ = self._to_px(temp, 0)
                ctx.move_to(gx - 6, y0 + dh + 10)
                ctx.show_text(f"{temp}°")
            for spd in (0, 50, 100):
                _, gy = self._to_px(20, spd)
                ctx.move_to(1, gy + 3)
                ctx.show_text(f"{spd}%")

        # Curve fill
        pts = self._points
        if pts:
            ctx.new_path()
            px0, py0 = self._to_px(pts[0][0], pts[0][1])
            ctx.move_to(x0, y0 + dh)        # bottom-left
            ctx.line_to(px0, y0 + dh)       # along bottom to first point x
            ctx.line_to(px0, py0)
            for t, s in pts[1:]:
                ctx.line_to(*self._to_px(t, s))
            # close to bottom-right
            last_px, _ = self._to_px(pts[-1][0], pts[-1][1])
            ctx.line_to(last_px, y0 + dh)
            ctx.close_path()
            ctx.set_source_rgba(*C_FILL)
            ctx.fill()

        # Curve line
        if pts:
            ctx.set_line_width(2.0)
            ctx.set_source_rgba(*C_CURVE)
            ctx.set_line_cap(0)  # BUTT
            px, py = self._to_px(pts[0][0], pts[0][1])
            # Extend line from left edge
            ctx.move_to(x0, py)
            ctx.line_to(px, py)
            for t, s in pts[1:]:
                ctx.line_to(*self._to_px(t, s))
            # Extend to right edge
            last_px, last_py = self._to_px(pts[-1][0], pts[-1][1])
            ctx.line_to(x0 + dw, last_py)
            ctx.stroke()

        # Control points
        for i, (t, s) in enumerate(pts):
            cx, cy = self._to_px(t, s)
            is_hl = (i == self._hover_idx or i == self._drag_idx)
            r = POINT_R_HL if is_hl else POINT_R

            # Rim
            ctx.arc(cx, cy, r + 1.5, 0, 2 * math.pi)
            ctx.set_source_rgba(*C_POINT_RIM)
            ctx.fill()

            # Fill
            ctx.arc(cx, cy, r, 0, 2 * math.pi)
            ctx.set_source_rgba(*(C_POINT_HL if is_hl else C_POINT))
            ctx.fill()

            # Tooltip on hover
            if is_hl and not compact:
                tip = f"{t:.0f}°C → {s:.0f}%"
                ctx.set_font_size(9)
                te = ctx.text_extents(tip)
                tx = min(cx + 10, x0 + dw - te[2] - 4)
                ty = max(cy - 8, y0 + te[3] + 4)
                ctx.set_source_rgba(0.1, 0.1, 0.18, 0.85)
                ctx.rectangle(tx - 3, ty - te[3] - 2, te[2] + 6, te[3] + 4)
                ctx.fill()
                ctx.set_source_rgba(*C_LABEL)
                ctx.move_to(tx, ty)
                ctx.show_text(tip)

        # Hint text
        if not compact:
            ctx.set_source_rgba(*C_TEXT)
            ctx.set_font_size(7.5)
            ctx.move_to(x0 + 3, y0 + 10)
            ctx.show_text("drag points · left-click to add · right-click to remove")
