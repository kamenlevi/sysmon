"""
Stats-style detailed panel.

A white window that drops down under the menu-bar icon with a caret, and
shows a rich set of live system stats: CPU / GPU / Memory / Disk donut
gauges, network throughput, sensors (temps + fans), uptime, load and the
top processes. Hides on click-away or via its close button.

It can also be made the default click action (a toggle in the footer),
and has a Cores button for the per-core view plus Settings and Quit.

Modelled on the Stats macOS app.
"""
import math
import os
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

import cairo
import psutil

from .monitor import SystemStats

WIDTH = 330
CARET_H = 9
CARET_W = 18
N_PROC_ROWS = 6

CSS = b"""
window.sysmon-popup { background-color: transparent; }

.panel-title { color: #1a1a1a; font-size: 12px; font-weight: bold; }
.metric-name { color: #1a1a1a; font-size: 13px; font-weight: bold; }
.metric-sub  { color: #6e6e6e; font-size: 11px; }
.sec-title   { color: #9a9a9a; font-size: 9px; font-weight: bold;
               letter-spacing: 1px; }
.info-name   { color: #444444; font-size: 11px; font-weight: bold; }
.info-val    { color: #2a2a2a; font-size: 11px; }
.proc-name   { color: #2a2a2a; font-size: 11px; }
.proc-val    { color: #2a2a2a; font-size: 11px; font-weight: bold; }
.warn-text   { color: #b04a3a; font-size: 10px; }

.foot-btn {
    background-color: #f2f2f2;
    color: #333333;
    border: 1px solid #d9d9d9;
    border-radius: 6px;
    padding: 2px 9px;
    font-size: 10px;
}
.foot-btn:hover { background-color: #e7e7e7; }
.foot-btn:checked { background-color: #d6e4ff; border-color: #9bb8e6; }
.close-btn {
    background: transparent; border: none; color: #888888;
    font-size: 14px; padding: 0 4px; min-width: 0; min-height: 0;
}
.close-btn:hover { color: #b04a3a; }

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


def _click_wrap(widget, cb):
    """Wrap a row so clicking it drills into a detail view (with hand cursor)."""
    ev = Gtk.EventBox()
    ev.add(widget)

    def _press(_w, _e):
        cb()
        return True
    ev.connect("button-press-event", _press)
    ev.connect("enter-notify-event", lambda w, e: (
        w.get_window().set_cursor(
            Gdk.Cursor.new_from_name(w.get_display(), "pointer"))
        if w.get_window() else None))
    ev.connect("leave-notify-event", lambda w, e: (
        w.get_window().set_cursor(None) if w.get_window() else None))
    return ev


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_rate(bps: float) -> str:
    return _fmt_bytes(bps) + "/s"


def _fmt_uptime(sec: float) -> str:
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


class _Donut(Gtk.DrawingArea):
    """Circular gauge with the percentage drawn in the centre."""

    def __init__(self, size=58):
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
        cr.set_line_width(lw)
        cr.set_source_rgba(0.90, 0.90, 0.90, 1.0)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()
        start = -math.pi / 2
        end = start + 2 * math.pi * (self._pct / 100.0)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(0.20, 0.20, 0.20, 1.0)
        cr.arc(cx, cy, r, start, end)
        cr.stroke()
        txt = f"{self._pct:.0f}%"
        cr.set_source_rgba(0.10, 0.10, 0.10, 1.0)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(13)
        ext = cr.text_extents(txt)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing,
                   cy - ext.height / 2 - ext.y_bearing)
        cr.show_text(txt)


class _MetricRow(Gtk.Box):
    """A donut gauge on the left and up to two detail lines on the right."""

    def __init__(self, name: str):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.set_margin_top(5)
        self.set_margin_bottom(5)
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

    def set(self, pct, sub1="", sub2=""):
        self.donut.set_pct(pct)
        self.sub1.set_text(sub1)
        self.sub1.set_visible(bool(sub1))
        self.sub2.set_text(sub2)
        self.sub2.set_visible(bool(sub2))


class _InfoRow(Gtk.Box):
    """A bold name on the left, a value on the right — for non-% stats."""

    def __init__(self, name):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.name_lbl = _lbl(name, "info-name", xalign=0.0)
        self.val_lbl = _lbl("", "info-val", xalign=1.0, ellipsize=True)
        self.pack_start(self.name_lbl, False, False, 0)
        self.pack_end(self.val_lbl, False, False, 0)

    def set(self, value):
        self.val_lbl.set_text(value)


class PopupWindow(Gtk.Window):

    def __init__(self, on_open_app, settings, on_settings=None, on_quit=None,
                 on_cores=None, on_set_default=None, on_nav=None):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.on_open_app = on_open_app
        self.settings = settings
        self._on_settings = on_settings
        self._on_quit = on_quit
        self._on_cores = on_cores
        self._on_set_default = on_set_default
        self.on_nav = on_nav
        self._fan_controller = None
        self._shown_at = 0.0
        self._caret_x = WIDTH / 2.0

        # For throughput rates (computed on demand while open).
        self._prev_disk = psutil.disk_io_counters()
        self._prev_net = psutil.net_io_counters()
        self._prev_t = time.monotonic()

        _apply_css()

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
        root.set_margin_top(CARET_H + 8)
        root.set_margin_bottom(12)
        self.add(root)

        # ── Header with close button ───────────────────────────────────
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.pack_start(_lbl("System Monitor", "panel-title", xalign=0.0),
                          True, True, 0)
        close_btn = Gtk.Button(label="✕")
        close_btn.get_style_context().add_class("close-btn")
        close_btn.set_relief(Gtk.ReliefStyle.NONE)
        close_btn.set_tooltip_text("Close")
        close_btn.connect("clicked", lambda *_: self.hide())
        header.pack_end(close_btn, False, False, 0)
        root.pack_start(header, False, False, 0)
        root.pack_start(Gtk.Separator(), False, False, 4)

        # ── Metric gauges ──────────────────────────────────────────────
        self._cpu = _MetricRow("CPU")
        root.pack_start(self._cpu, False, False, 0)

        self._gpu = _MetricRow("GPU")
        root.pack_start(self._gpu, False, False, 0)
        self._gpu.show_all()
        self._gpu.set_no_show_all(True)
        self._gpu.hide()

        self._ram = _MetricRow("Memory")
        root.pack_start(self._ram, False, False, 0)

        self._disk = _MetricRow("Disk")
        root.pack_start(_click_wrap(self._disk, lambda: self._nav("disks")),
                        False, False, 0)

        # ── Info rows (network, sensors, uptime) ───────────────────────
        root.pack_start(Gtk.Separator(), False, False, 4)
        self._net = _InfoRow("Network")
        root.pack_start(self._net, False, False, 1)
        self._sensors = _InfoRow("Sensors")
        root.pack_start(self._sensors, False, False, 1)
        self._sensors.show_all()
        self._sensors.set_no_show_all(True)
        self._sensors.hide()
        self._load = _InfoRow("Load (1·5·15m)")
        self._load.set_tooltip_text(
            "Load average: avg processes using/waiting for CPU over the last "
            "1, 5 and 15 minutes. ≈ your core count means fully busy.")
        root.pack_start(self._load, False, False, 1)
        self._uptime = _InfoRow("Uptime")
        self._uptime.set_tooltip_text("Click for the full process list")
        root.pack_start(_click_wrap(self._uptime, lambda: self._nav("processes")),
                        False, False, 1)

        # ── Top processes ──────────────────────────────────────────────
        root.pack_start(Gtk.Separator(), False, False, 6)
        self._proc_title = _lbl("TOP PROCESSES", "sec-title", xalign=0.0)
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
        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)

        cores_btn = Gtk.Button(label="Cores")
        cores_btn.get_style_context().add_class("foot-btn")
        cores_btn.connect("clicked", self._on_cores_clicked)
        foot.pack_start(cores_btn, False, False, 0)

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
        cr.new_sub_path()
        cr.arc(w - r, top + r, r, -math.pi / 2, 0)
        cr.arc(w - r, h - r, r, 0, math.pi / 2)
        cr.arc(r, h - r, r, math.pi / 2, math.pi)
        cr.arc(r, top + r, r, math.pi, 1.5 * math.pi)
        cr.close_path()
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
        if time.monotonic() - self._shown_at < 0.6:
            return False
        self.hide()
        return False

    def _settle(self):
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

    def _on_cores_clicked(self, *_):
        self.hide()
        if self._on_cores:
            self._on_cores()

    def _nav(self, view):
        self.hide()
        if self.on_nav:
            self.on_nav(view)

    # ── Extra (on-demand) stats ─────────────────────────────────────────

    def _extras(self):
        now = time.monotonic()
        dt = max(0.001, now - self._prev_t)
        out = {}
        try:
            du = psutil.disk_usage("/")
            out["disk_pct"] = du.percent
            out["disk_used"] = du.used
            out["disk_total"] = du.total
        except Exception:
            pass
        try:
            dio = psutil.disk_io_counters()
            out["rd"] = (dio.read_bytes - self._prev_disk.read_bytes) / dt
            out["wr"] = (dio.write_bytes - self._prev_disk.write_bytes) / dt
            self._prev_disk = dio
        except Exception:
            pass
        try:
            nio = psutil.net_io_counters()
            out["down"] = (nio.bytes_recv - self._prev_net.bytes_recv) / dt
            out["up"] = (nio.bytes_sent - self._prev_net.bytes_sent) / dt
            self._prev_net = nio
        except Exception:
            pass
        self._prev_t = now
        try:
            out["load"] = os.getloadavg()
        except Exception:
            pass
        try:
            out["uptime"] = time.time() - psutil.boot_time()
        except Exception:
            pass
        try:
            out["nproc"] = len(psutil.pids())
        except Exception:
            pass
        return out

    # ── Data update ────────────────────────────────────────────────────

    def update(self, s: SystemStats, procs=None):
        cfg = self.settings
        ex = self._extras()

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
                line2 += ("  " if line2 else "") + f"{s.gpu_temp:.0f}°C"
            if s.gpu_power_w > 0:
                line2 += ("  " if line2 else "") + f"{s.gpu_power_w:.0f}W"
            self._gpu.set(s.gpu_percent, line1, line2)
        else:
            self._gpu.set_visible(False)

        # Memory
        line2 = ""
        if s.swap_total_gb > 0:
            line2 = f"swap {s.swap_used_gb:.1f} / {s.swap_total_gb:.1f} GB"
        self._ram.set(s.ram_percent,
                      f"{s.ram_used_gb:.1f} / {s.ram_total_gb:.1f} GB", line2)

        # Disk
        if "disk_pct" in ex:
            sub2 = ""
            if "rd" in ex:
                sub2 = f"↓{_fmt_rate(ex['rd'])}  ↑{_fmt_rate(ex['wr'])}"
            self._disk.set(
                ex["disk_pct"],
                f"{_fmt_bytes(ex['disk_used'])} / {_fmt_bytes(ex['disk_total'])}",
                sub2)
            self._disk.set_visible(True)
        else:
            self._disk.set_visible(False)

        # Network
        if "down" in ex:
            self._net.set(f"↓ {_fmt_rate(ex['down'])}   ↑ {_fmt_rate(ex['up'])}")
            self._net.set_visible(True)
        else:
            self._net.set_visible(False)

        # Sensors (temps + fans)
        sensor_parts = []
        if cfg.show_temp and s.cpu_temp > 0:
            sensor_parts.append(f"CPU {s.cpu_temp:.0f}°C")
        if cfg.show_temp and s.gpu_available and s.gpu_temp > 0:
            sensor_parts.append(f"GPU {s.gpu_temp:.0f}°C")
        for fan in (s.fans or []):
            label, rpm = fan[0], fan[1]
            sensor_parts.append(f"{label} {rpm}rpm")
        if sensor_parts:
            self._sensors.set("   ".join(sensor_parts))
            self._sensors.set_visible(True)
        else:
            self._sensors.set_visible(False)

        # Load + uptime
        if "load" in ex:
            self._load.set("  ".join(f"{x:.2f}" for x in ex["load"]))
        if "uptime" in ex:
            up = _fmt_uptime(ex["uptime"])
            if "nproc" in ex:
                up += f"   ·   {ex['nproc']} processes"
            self._uptime.set(up)

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
                lbl = _lbl(f"⚠  {w}", "warn-text", xalign=0.0, ellipsize=True)
                self._warn_box.pack_start(lbl, False, False, 0)
                lbl.show()
        else:
            self._warn_box.set_visible(False)

    # ── Show / position ────────────────────────────────────────────────

    def show_near_top_right(self):
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
        self._caret_x = max(CARET_W, min(cursor_x - x, w - CARET_W))
        self.queue_draw()
