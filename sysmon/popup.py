"""
Stats-style dropdown panel.

A single white window that drops down directly under the menu-bar icon,
with a small caret pointing up at the icon. It shows the current system
stats as circular donut gauges plus a top-processes list, and hides as
soon as the user clicks elsewhere (or presses Escape). No title bar, no
other window.

Modelled on the Stats macOS app.
"""
import math
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

import cairo

from .monitor import SystemStats

WIDTH = 300
CARET_H = 9
CARET_W = 18
N_PROC_ROWS = 5

CSS = b"""
window.sysmon-popup { background-color: transparent; }

.metric-name { color: #1a1a1a; font-size: 13px; font-weight: bold; }
.metric-sub  { color: #6e6e6e; font-size: 11px; }
.sec-title   { color: #9a9a9a; font-size: 9px; font-weight: bold;
               letter-spacing: 1px; }
.proc-name   { color: #2a2a2a; font-size: 11px; }
.proc-val    { color: #2a2a2a; font-size: 11px; font-weight: bold; }
.warn-text   { color: #5a5a5a; font-size: 10px; }

.foot-btn {
    background-color: #f2f2f2;
    color: #333333;
    border: 1px solid #d9d9d9;
    border-radius: 6px;
    padding: 3px 12px;
    font-size: 11px;
}
.foot-btn:hover { background-color: #e7e7e7; }

separator { background-color: #ececec; min-height: 1px; }
"""

_CSS_APPLIED = [False]


def _apply_css():
    if _CSS_APPLIED[0]:
        return
    p = Gtk.CssProvider()
    p.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _CSS_APPLIED[0] = True


def _lbl(text="", css="metric-sub", xalign=0.0, ellipsize=False) -> Gtk.Label:
    l = Gtk.Label(label=text, xalign=xalign)
    if css:
        l.get_style_context().add_class(css)
    if ellipsize:
        l.set_ellipsize(Pango.EllipsizeMode.END)
    return l


class _Donut(Gtk.DrawingArea):
    """Circular gauge with the percentage drawn in the centre."""

    def __init__(self, size=62):
        super().__init__()
        self._pct = 0.0
        self.set_size_request(size, size)
        self.connect("draw", self._draw)

    def set_pct(self, pct: float):
        self._pct = max(0.0, min(pct, 100.0))
        self.queue_draw()

    def _draw(self, _w, cr):
        a = self.get_allocation()
        size = min(a.width, a.height)
        cx, cy = a.width / 2.0, a.height / 2.0
        r = size / 2.0 - 4
        lw = 6.0

        # Track ring
        cr.set_line_width(lw)
        cr.set_source_rgba(0.90, 0.90, 0.90, 1.0)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()

        # Value arc (clockwise from top)
        start = -math.pi / 2
        end = start + 2 * math.pi * (self._pct / 100.0)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(0.20, 0.20, 0.20, 1.0)
        cr.arc(cx, cy, r, start, end)
        cr.stroke()

        # Centre percentage
        txt = f"{self._pct:.0f}%"
        cr.set_source_rgba(0.10, 0.10, 0.10, 1.0)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(14)
        ext = cr.text_extents(txt)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing,
                   cy - ext.height / 2 - ext.y_bearing)
        cr.show_text(txt)


class _MetricRow(Gtk.Box):
    """A donut gauge on the left and up to two detail lines on the right."""

    def __init__(self, name: str):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.set_margin_top(6)
        self.set_margin_bottom(6)

        self.donut = _Donut()
        self.pack_start(self.donut, False, False, 0)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        details.set_valign(Gtk.Align.CENTER)
        self.name_lbl = _lbl(name, "metric-name", xalign=0.0)
        self.sub1 = _lbl("", "metric-sub", xalign=0.0, ellipsize=True)
        self.sub2 = _lbl("", "metric-sub", xalign=0.0, ellipsize=True)
        details.pack_start(self.name_lbl, False, False, 0)
        details.pack_start(self.sub1, False, False, 0)
        details.pack_start(self.sub2, False, False, 0)
        self.pack_start(details, True, True, 0)

    def set(self, pct: float, sub1: str = "", sub2: str = ""):
        self.donut.set_pct(pct)
        self.sub1.set_text(sub1)
        self.sub1.set_visible(bool(sub1))
        self.sub2.set_text(sub2)
        self.sub2.set_visible(bool(sub2))


class PopupWindow(Gtk.Window):

    def __init__(self, on_open_app, settings, on_settings=None, on_quit=None):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.on_open_app = on_open_app
        self.settings = settings
        self._on_settings = on_settings
        self._on_quit = on_quit
        self._fan_controller = None
        self._shown_at = 0.0
        self._caret_x = WIDTH / 2.0

        _apply_css()

        # Transparent window so we can paint a rounded body + caret ourselves.
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.set_app_paintable(True)
        self.connect("draw", self._draw_bg)

        self.get_style_context().add_class("sysmon-popup")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.POPUP_MENU)
        self.set_size_request(WIDTH, -1)

        self.connect("focus-out-event", self._on_focus_out)
        self.connect("key-press-event", self._on_key_press)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.set_margin_start(16)
        root.set_margin_end(16)
        root.set_margin_top(CARET_H + 12)
        root.set_margin_bottom(12)
        self.add(root)

        # ── Metric gauges ──────────────────────────────────────────────
        self._cpu = _MetricRow("CPU")
        root.pack_start(self._cpu, False, False, 0)

        self._gpu = _MetricRow("GPU")
        root.pack_start(self._gpu, False, False, 0)
        self._gpu.show_all()              # mark children shown...
        self._gpu.set_no_show_all(True)   # ...but keep the row itself toggleable
        self._gpu.hide()

        self._ram = _MetricRow("Memory")
        root.pack_start(self._ram, False, False, 0)

        # ── Top processes ──────────────────────────────────────────────
        root.pack_start(Gtk.Separator(), False, False, 6)
        self._proc_title = _lbl("TOP PROCESSES", "sec-title", xalign=0.0)
        self._proc_title.set_margin_top(2)
        self._proc_title.set_margin_bottom(2)
        root.pack_start(self._proc_title, False, False, 0)

        self._proc_rows = []
        for _ in range(N_PROC_ROWS):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            name = _lbl("", "proc-name", xalign=0.0, ellipsize=True)
            val = _lbl("", "proc-val", xalign=1.0)
            row.pack_start(name, True, True, 0)
            row.pack_end(val, False, False, 0)
            root.pack_start(row, False, False, 1)
            row.show_all()
            row.set_no_show_all(True)
            row.hide()
            self._proc_rows.append((row, name, val))

        # ── Warnings ───────────────────────────────────────────────────
        self._warn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self._warn_box.set_margin_top(4)
        self._warn_box.set_no_show_all(True)
        root.pack_start(self._warn_box, False, False, 0)

        # ── Footer ─────────────────────────────────────────────────────
        root.pack_start(Gtk.Separator(), False, False, 6)
        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        settings_btn = Gtk.Button(label="Settings")
        settings_btn.get_style_context().add_class("foot-btn")
        settings_btn.connect("clicked", self._on_settings_clicked)
        foot.pack_start(settings_btn, False, False, 0)
        quit_btn = Gtk.Button(label="Quit")
        quit_btn.get_style_context().add_class("foot-btn")
        quit_btn.connect("clicked", lambda *_: self._on_quit() if self._on_quit else None)
        foot.pack_end(quit_btn, False, False, 0)
        root.pack_start(foot, False, False, 0)

        self.show_all()
        self.hide()

    # ── Caret + body painting ──────────────────────────────────────────

    def _draw_bg(self, _w, cr):
        a = self.get_allocation()
        w, h = a.width, a.height

        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        r = 12
        top = CARET_H
        cx = max(CARET_W, min(self._caret_x, w - CARET_W))

        # Rounded body
        cr.new_sub_path()
        cr.arc(w - r, top + r, r, -math.pi / 2, 0)
        cr.arc(w - r, h - r, r, 0, math.pi / 2)
        cr.arc(r, h - r, r, math.pi / 2, math.pi)
        cr.arc(r, top + r, r, math.pi, 1.5 * math.pi)
        cr.close_path()
        # Caret
        cr.move_to(cx - CARET_W / 2, top)
        cr.line_to(cx, 0)
        cr.line_to(cx + CARET_W / 2, top)
        cr.close_path()

        cr.set_source_rgba(1.0, 1.0, 1.0, 1.0)
        cr.fill_preserve()
        cr.set_source_rgba(0.82, 0.82, 0.82, 1.0)
        cr.set_line_width(1.0)
        cr.stroke()
        return False

    # ── Auto-hide ──────────────────────────────────────────────────────

    def _on_focus_out(self, *_):
        # Ignore the focus flicker while the indicator menu closes as the
        # panel opens; only auto-hide once the panel has settled.
        if time.monotonic() - self._shown_at < 0.6:
            return False
        self.hide()
        return False

    def _settle(self):
        # Re-assert focus once the indicator menu has fully closed, so a real
        # click elsewhere reliably closes the panel.
        if self.get_visible():
            self.present()
            self.grab_focus()
        return False

    def _on_key_press(self, _w, event):
        if event.keyval == Gdk.KEY_Escape:
            self.hide()
        return False

    def _on_settings_clicked(self, *_):
        self.hide()
        if self._on_settings:
            self._on_settings()

    # ── Data update ────────────────────────────────────────────────────

    def update(self, s: SystemStats, procs=None):
        cfg = self.settings

        # CPU
        freq = f"{s.cpu_freq_mhz/1000:.1f} GHz" if s.cpu_freq_mhz > 0 else ""
        temp = ""
        if cfg.show_temp and s.cpu_temp > 0:
            temp = f"{s.cpu_temp:.0f}°C"
            if s.thermal_throttling:
                temp += "  throttling"
        self._cpu.set(s.cpu_percent, freq, temp)

        # GPU
        if cfg.show_gpu and s.gpu_available:
            self._gpu.set_visible(True)
            line1 = s.gpu_name or ""
            line2 = ""
            if s.gpu_mem_total_mb > 0:
                line2 = f"{s.gpu_mem_used_mb/1024:.1f} / {s.gpu_mem_total_mb/1024:.1f} GB"
            if cfg.show_temp and s.gpu_temp > 0:
                line2 += (("  ") if line2 else "") + f"{s.gpu_temp:.0f}°C"
            self._gpu.set(s.gpu_percent, line1, line2)
        else:
            self._gpu.set_visible(False)

        # Memory
        line1 = f"{s.ram_used_gb:.1f} / {s.ram_total_gb:.1f} GB"
        line2 = ""
        if s.swap_total_gb > 0:
            line2 = f"swap {s.swap_used_gb:.1f} / {s.swap_total_gb:.1f} GB"
        self._ram.set(s.ram_percent, line1, line2)

        # Top processes
        procs = procs or []
        for i, (row, name, val) in enumerate(self._proc_rows):
            if i < len(procs):
                p = procs[i]
                row.set_visible(True)
                name.set_text(p.name)
                val.set_text(f"{p.cpu_percent:.0f}%")
            else:
                row.set_visible(False)
        self._proc_title.set_visible(bool(procs))

        # Warnings
        for c in self._warn_box.get_children():
            self._warn_box.remove(c)
        if s.warnings:
            self._warn_box.set_visible(True)
            for w in s.warnings:
                lbl = _lbl(f"!  {w}", "warn-text", xalign=0.0, ellipsize=True)
                self._warn_box.pack_start(lbl, False, False, 0)
                lbl.show()
        else:
            self._warn_box.set_visible(False)

    # ── Show / position ────────────────────────────────────────────────

    def show_near_top_right(self):
        """Toggle: drop down under the icon, or hide if already visible."""
        if self.get_visible():
            self.hide()
            return
        self._shown_at = time.monotonic()
        self.show_all()
        self._position_under_cursor()
        self.present()
        self.grab_focus()
        GLib.timeout_add(300, self._settle)

    def _position_under_cursor(self):
        display = Gdk.Display.get_default()
        seat = display.get_default_seat()
        _, cursor_x, _cursor_y = seat.get_pointer().get_position()
        w, _h = self.get_size()
        screen_w = Gdk.Screen.get_default().get_width()
        x = max(4, min(cursor_x - w // 2, screen_w - w - 4))
        self.move(x, 30)
        # Point the caret at the icon (the click position).
        self._caret_x = max(CARET_W, min(cursor_x - x, w - CARET_W))
        self.queue_draw()
