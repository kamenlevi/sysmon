"""
The detailed panel — a caret dropdown (shared CaretPanel style) showing
CPU/GPU/Memory/Disk donut gauges that expand IN PLACE (cores, temps,
specs), plus network, sensors, load, uptime and top processes. Fixed
width; only the height changes. Clicking uptime drills into Processes.
"""
import os
import math
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

import cairo
import psutil

from .monitor import SystemStats
from .panel_base import CaretPanel, WIDTH

N_PROC_ROWS = 6

CSS = b"""
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
    background-color: #f2f2f2; color: #333333; border: 1px solid #d9d9d9;
    border-radius: 6px; padding: 2px 12px; font-size: 10px;
}
.foot-btn:hover { background-color: #e7e7e7; }
.foot-btn:backdrop { color: #333333; background-color: #f2f2f2; }
.set-main-btn { background: transparent; border: none; color: #9a9a9a;
                padding: 0 4px; min-width: 0; min-height: 0; font-size: 11px; }
.set-main-btn:hover { color: #c79a2a; }
.is-main { color: #c79a2a; }
progressbar trough { min-height: 6px; }
progressbar progress { min-height: 6px; }
"""
_CSS_APPLIED = [False]


def _apply_css():
    if _CSS_APPLIED[0]:
        return
    p = Gtk.CssProvider()
    p.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _CSS_APPLIED[0] = True


def _lbl(text="", css="metric-sub", xalign=0.0, ellipsize=False):
    l = Gtk.Label(label=text, xalign=xalign)
    if css:
        l.get_style_context().add_class(css)
    if ellipsize:
        l.set_ellipsize(Pango.EllipsizeMode.END)
    return l


def _click_wrap(widget, cb):
    ev = Gtk.EventBox()
    ev.add(widget)

    def _press(_w, _e):
        cb()
        return True
    ev.connect("button-press-event", _press)
    ev.connect("enter-notify-event", lambda w, e: (
        w.get_window().set_cursor(Gdk.Cursor.new_from_name(w.get_display(), "pointer"))
        if w.get_window() else None))
    ev.connect("leave-notify-event", lambda w, e: (
        w.get_window().set_cursor(None) if w.get_window() else None))
    return ev


def _fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_rate(bps):
    return _fmt_bytes(bps) + "/s"


def _fmt_uptime(sec):
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
    def __init__(self, size=56):
        super().__init__()
        self._pct = 0.0
        self.set_size_request(size, size)
        self.connect("draw", self._draw)

    def set_pct(self, pct):
        self._pct = max(0.0, min(pct, 100.0))
        self.queue_draw()

    def _draw(self, _w, cr):
        a = self.get_allocation()
        size = min(a.width, a.height)
        cx, cy = a.width / 2.0, a.height / 2.0
        r = size / 2.0 - 4
        cr.set_line_width(6.0)
        cr.set_source_rgba(0.90, 0.90, 0.90, 1.0)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(0.20, 0.20, 0.20, 1.0)
        cr.arc(cx, cy, r, -math.pi / 2, -math.pi / 2 + 2 * math.pi * (self._pct / 100.0))
        cr.stroke()
        txt = f"{self._pct:.0f}%"
        cr.set_source_rgba(0.10, 0.10, 0.10, 1.0)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(13)
        ext = cr.text_extents(txt)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy - ext.height / 2 - ext.y_bearing)
        cr.show_text(txt)


class _MetricRow(Gtk.Box):
    """Expandable metric: clickable header (donut + name + subs + chevron)
    revealing per-component detail below, in place."""

    def __init__(self, name):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_margin_top(3)
        self.set_margin_bottom(3)
        header = Gtk.EventBox()
        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.add(hb)
        self.donut = _Donut()
        hb.pack_start(self.donut, False, False, 0)
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        details.set_valign(Gtk.Align.CENTER)
        self.name_lbl = _lbl(name, "metric-name", xalign=0.0)
        self.sub1 = _lbl("", "metric-sub", xalign=0.0, ellipsize=True)
        self.sub2 = _lbl("", "metric-sub", xalign=0.0, ellipsize=True)
        details.pack_start(self.name_lbl, False, False, 0)
        details.pack_start(self.sub1, False, False, 0)
        details.pack_start(self.sub2, False, False, 0)
        hb.pack_start(details, True, True, 0)
        self.chevron = _lbl("⌄", "metric-sub", xalign=1.0)
        self.chevron.set_valign(Gtk.Align.CENTER)
        hb.pack_end(self.chevron, False, False, 0)
        self.pack_start(header, False, False, 0)

        self.revealer = Gtk.Revealer()
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.NONE)
        self.detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.detail_box.set_margin_start(4)
        self.detail_box.set_margin_top(4)
        self.detail_box.set_margin_bottom(4)
        self.revealer.add(self.detail_box)
        self.pack_start(self.revealer, False, False, 0)

        self._expanded = False
        self.on_expand = None
        header.connect("button-press-event", self._toggle)
        header.connect("enter-notify-event", lambda w, e: (
            w.get_window().set_cursor(Gdk.Cursor.new_from_name(w.get_display(), "pointer"))
            if w.get_window() else None))
        header.connect("leave-notify-event", lambda w, e: (
            w.get_window().set_cursor(None) if w.get_window() else None))

    def _toggle(self, *_):
        self._expanded = not self._expanded
        self.revealer.set_reveal_child(self._expanded)
        self.chevron.set_text("⌃" if self._expanded else "⌄")
        if self._expanded and self.on_expand:
            self.on_expand()
        return True

    @property
    def expanded(self):
        return self._expanded

    def set(self, pct, sub1="", sub2=""):
        self.donut.set_pct(pct)
        self.sub1.set_text(sub1)
        self.sub1.set_visible(bool(sub1))
        self.sub2.set_text(sub2)
        self.sub2.set_visible(bool(sub2))


class _InfoRow(Gtk.Box):
    def __init__(self, name):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.name_lbl = _lbl(name, "info-name", xalign=0.0)
        self.val_lbl = _lbl("", "info-val", xalign=1.0, ellipsize=True)
        self.pack_start(self.name_lbl, False, False, 0)
        self.pack_end(self.val_lbl, False, False, 0)

    def set(self, value):
        self.val_lbl.set_text(value)


class PopupWindow(CaretPanel):

    def __init__(self, settings, history_db, on_open_app=None,
                 on_settings=None, on_quit=None):
        super().__init__("System Monitor", show_back=False)
        _apply_css()
        self.on_open_app = on_open_app
        self.settings = settings
        self._history_db = history_db
        self._on_settings = on_settings
        self._on_quit = on_quit
        self._fan_controller = None
        self._last = None
        self._last_procs = []
        self._core_hist = []
        self._nav_stack = []
        self._prev_disk = psutil.disk_io_counters()
        try:
            _per = psutil.net_io_counters(pernic=True)
            self._prev_net = (
                sum(c.bytes_recv for n, c in _per.items() if not n.startswith("lo")),
                sum(c.bytes_sent for n, c in _per.items() if not n.startswith("lo")))
        except Exception:
            self._prev_net = (0, 0)
        self._prev_t = time.monotonic()

        # All views live in one Stack so switching between them is instant
        # (no window map/unmap, no WM animation).
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.body.pack_start(self.stack, True, True, 0)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.stack.add_named(root, "overview")

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
        root.pack_start(self._disk, False, False, 0)

        self._build_cpu_detail()
        self._build_gpu_detail()
        self._build_ram_detail()
        self._build_disk_detail()
        # Fill detail on idle so a click toggles instantly (snappy).
        self._cpu.on_expand = lambda: GLib.idle_add(self._deferred, self._update_cpu_detail)
        self._gpu.on_expand = lambda: GLib.idle_add(self._deferred, self._update_gpu_detail)
        self._ram.on_expand = lambda: GLib.idle_add(self._deferred, self._update_ram_detail)
        self._disk.on_expand = lambda: GLib.idle_add(self._update_disk_detail) and False

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
            "Load average: avg processes using/waiting for CPU over 1, 5 and "
            "15 minutes. ≈ your core count means fully busy.")
        root.pack_start(self._load, False, False, 1)
        self._uptime = _InfoRow("Uptime")
        self._uptime.set_tooltip_text("Click for the full process list")
        root.pack_start(_click_wrap(self._uptime, lambda: self._navigate("processes")),
                        False, False, 1)

        root.pack_start(Gtk.Separator(), False, False, 6)
        self._proc_title = _lbl("TOP PROCESSES", "sec-title", xalign=0.0)
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

        self._warn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self._warn_box.set_margin_top(4)
        self._warn_box.set_no_show_all(True)
        root.pack_start(self._warn_box, False, False, 0)

        root.pack_start(Gtk.Separator(), False, False, 6)
        # Row 1: drill-ins (same things the simple menu offers).
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        nav.set_homogeneous(True)
        for label, view in (("History", "history"), ("Cores", "cores"),
                            ("Processes", "processes")):
            b = Gtk.Button(label=label)
            b.get_style_context().add_class("foot-btn")
            b.connect("clicked", lambda _w, v=view: self._navigate(v))
            nav.pack_start(b, True, True, 0)
        root.pack_start(nav, False, False, 0)
        # Row 2: settings / quit.
        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        foot.set_margin_top(4)
        settings_btn = Gtk.Button(label="Settings")
        settings_btn.get_style_context().add_class("foot-btn")
        settings_btn.connect("clicked", self._on_settings_clicked)
        foot.pack_start(settings_btn, False, False, 0)
        quit_btn = Gtk.Button(label="Quit")
        quit_btn.get_style_context().add_class("foot-btn")
        quit_btn.connect("clicked", lambda *_: self._on_quit() if self._on_quit else None)
        foot.pack_end(quit_btn, False, False, 0)
        root.pack_start(foot, False, False, 0)

        # ── Drill-in pages (embedded views) ─────────────────────────────
        from .cores_window import CoresView
        from .detail_views import HistoryView, ProcessesView, DisksView
        self._cores_view = CoresView()
        self._hist_view = HistoryView(history_db, settings)
        self._proc_view = ProcessesView()
        self._disks_view = DisksView()
        for name, view in (("cores", self._cores_view),
                           ("history", self._hist_view),
                           ("processes", self._proc_view),
                           ("disks", self._disks_view)):
            self.stack.add_named(view, name)

        self.show_all()
        self.hide()

    # ── Stack navigation ────────────────────────────────────────────────
    _TITLES = {"overview": "System Monitor", "cores": "CPU / GPU cores",
               "history": "Usage history", "processes": "Processes",
               "disks": "Disks"}

    def show_page(self, name):
        self.stack.set_visible_child_name(name)
        self.title_lbl.set_markup(f"<b>{self._TITLES.get(name, name)}</b>")
        self.back_btn.set_visible(name != "overview")
        self._refresh_page(name)

    def open_to(self, name):
        """Open straight to a page (from the menu) — back will just close."""
        self._nav_stack = []
        self.show_page(name)

    def _navigate(self, name):
        """Drill into a page from the overview — back returns to it."""
        self._nav_stack.append(self.stack.get_visible_child_name() or "overview")
        self.show_page(name)

    def _do_back(self):
        if self._nav_stack:
            self.show_page(self._nav_stack.pop())
        else:
            self.hide()

    def _refresh_page(self, name):
        if name == "overview":
            if self._last is not None:
                self._update_overview(self._last, self._last_procs)
        elif name == "cores":
            if self._last is not None:
                self._cores_view.update(self._last, self._core_hist)
        elif name == "history":
            self._hist_view.refresh()
        elif name == "processes":
            self._proc_view.refresh()
        elif name == "disks":
            self._disks_view.refresh()

    def update(self, s: SystemStats, procs=None, core_hist=None):
        """Called every tick while visible — refresh only the visible page."""
        self._last = s
        if procs is not None:
            self._last_procs = procs
        if core_hist is not None:
            self._core_hist = core_hist
        self._refresh_page(self.stack.get_visible_child_name() or "overview")

    def _deferred(self, fn):
        if self._last is not None:
            fn(self._last)
        return False

    # ── buttons ──────────────────────────────────────────────────────────
    def _on_settings_clicked(self, *_):
        if self._on_settings:
            self._on_settings()

    # ── per-component detail ────────────────────────────────────────────
    def _detail_line(self, box, name):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row.pack_start(_lbl(name, "info-name", xalign=0.0), False, False, 0)
        val = _lbl("", "info-val", xalign=1.0, ellipsize=True)
        row.pack_end(val, False, False, 0)
        box.pack_start(row, False, False, 0)
        return val

    def _build_cpu_detail(self):
        box = self._cpu.detail_box
        self._cpu_freq = self._detail_line(box, "Frequency")
        self._cpu_temp_d = self._detail_line(box, "Temperature")
        self._cpu_load_d = self._detail_line(box, "Load (1·5·15m)")
        box.pack_start(_lbl("Per-core usage", "sec-title", xalign=0.0), False, False, 2)
        self._cpu_cores_grid = Gtk.Grid()
        self._cpu_cores_grid.set_column_spacing(10)
        self._cpu_cores_grid.set_column_homogeneous(True)
        box.pack_start(self._cpu_cores_grid, False, False, 0)
        self._cpu_core_lbls = []

    def _build_gpu_detail(self):
        box = self._gpu.detail_box
        self._gpu_name_d = self._detail_line(box, "Model")
        self._gpu_vram_d = self._detail_line(box, "VRAM")
        self._gpu_temp_d = self._detail_line(box, "Temperature")
        self._gpu_power_d = self._detail_line(box, "Power")

    def _build_ram_detail(self):
        box = self._ram.detail_box
        self._ram_used_d = self._detail_line(box, "Used")
        self._ram_avail_d = self._detail_line(box, "Available")
        self._ram_cached_d = self._detail_line(box, "Cached")
        self._ram_swap_d = self._detail_line(box, "Swap")

    def _build_disk_detail(self):
        self._disk_detail_box = self._disk.detail_box

    def _main_disk(self):
        return getattr(self.settings, "main_disk", "/") or "/"

    def _set_main_disk(self, mountpoint):
        self.settings.main_disk = mountpoint
        try:
            self.settings.save()
        except Exception:
            pass
        self._update_disk_detail()
        if self._last is not None:
            try:
                import psutil as _ps
                self._disk.set(_ps.disk_usage(mountpoint).percent,
                               self._disk.sub1.get_text(), self._disk.sub2.get_text())
            except Exception:
                pass

    def _update_cpu_detail(self, s):
        if s.cpu_freq_mhz > 0:
            self._cpu_freq.set_text(
                f"{s.cpu_freq_mhz/1000:.2f} / {s.cpu_freq_max_mhz/1000:.2f} GHz")
        self._cpu_temp_d.set_text(f"{s.cpu_temp:.0f}°C" if s.cpu_temp > 0 else "—")
        try:
            self._cpu_load_d.set_text("  ".join(f"{x:.2f}" for x in os.getloadavg()))
        except Exception:
            pass
        cores = s.cpu_per_core or []
        if len(self._cpu_core_lbls) != len(cores):
            for c in self._cpu_cores_grid.get_children():
                self._cpu_cores_grid.remove(c)
            self._cpu_core_lbls = []
            cols = 4
            for i in range(len(cores)):
                lbl = _lbl("", "info-val", xalign=0.0)
                self._cpu_cores_grid.attach(lbl, i % cols, i // cols, 1, 1)
                self._cpu_core_lbls.append(lbl)
            self._cpu_cores_grid.show_all()
        for i, (lbl, v) in enumerate(zip(self._cpu_core_lbls, cores)):
            lbl.set_text(f"C{i} {v:.0f}%")

    def _update_gpu_detail(self, s):
        self._gpu_name_d.set_text(s.gpu_name or "—")
        if s.gpu_mem_total_mb > 0:
            self._gpu_vram_d.set_text(
                f"{s.gpu_mem_used_mb/1024:.1f} / {s.gpu_mem_total_mb/1024:.1f} GB")
        self._gpu_temp_d.set_text(f"{s.gpu_temp:.0f}°C" if s.gpu_temp > 0 else "—")
        self._gpu_power_d.set_text(f"{s.gpu_power_w:.0f} W" if s.gpu_power_w > 0 else "—")

    def _update_ram_detail(self, s):
        try:
            vm = psutil.virtual_memory()
            self._ram_used_d.set_text(f"{vm.used/(1024**3):.1f} GB")
            self._ram_avail_d.set_text(f"{vm.available/(1024**3):.1f} GB")
            self._ram_cached_d.set_text(f"{getattr(vm,'cached',0)/(1024**3):.1f} GB")
        except Exception:
            pass
        self._ram_swap_d.set_text(
            f"{s.swap_used_gb:.1f} / {s.swap_total_gb:.1f} GB"
            if s.swap_total_gb > 0 else "none")

    def _update_disk_detail(self):
        for c in self._disk_detail_box.get_children():
            self._disk_detail_box.remove(c)
        seen = set()
        skip = {"squashfs", "tmpfs", "devtmpfs", "overlay", "autofs", "ramfs", ""}
        try:
            parts = psutil.disk_partitions(all=False)
        except Exception:
            parts = []
        for part in parts:
            if part.fstype in skip or part.device.startswith("/dev/loop"):
                continue
            if part.mountpoint in seen:
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            seen.add(part.mountpoint)
            is_main = (part.mountpoint == self._main_disk())
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            star = Gtk.Button(label="★" if is_main else "☆")
            star.get_style_context().add_class("set-main-btn")
            if is_main:
                star.get_style_context().add_class("is-main")
            star.set_relief(Gtk.ReliefStyle.NONE)
            star.set_tooltip_text("Show this disk on the main gauge")
            star.connect("clicked", lambda _w, mp=part.mountpoint: self._set_main_disk(mp))
            head.pack_start(star, False, False, 0)
            head.pack_start(_lbl(part.mountpoint, "info-name", xalign=0.0, ellipsize=True),
                            True, True, 0)
            head.pack_end(_lbl(f"{u.percent:.0f}%  {u.used/(1024**3):.0f}/"
                               f"{u.total/(1024**3):.0f}GB", "info-val", xalign=1.0),
                          False, False, 0)
            bar = Gtk.ProgressBar()
            bar.set_fraction(min(u.percent / 100.0, 1.0))
            self._disk_detail_box.pack_start(head, False, False, 0)
            self._disk_detail_box.pack_start(bar, False, False, 0)
        self._disk_detail_box.show_all()

    # ── extras ──────────────────────────────────────────────────────────
    def _extras(self):
        now = time.monotonic()
        dt = max(0.001, now - self._prev_t)
        out = {}
        try:
            du = psutil.disk_usage(self._main_disk())
            out.update(disk_pct=du.percent, disk_used=du.used, disk_total=du.total)
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
            per = psutil.net_io_counters(pernic=True)
            recv = sum(c.bytes_recv for n, c in per.items() if not n.startswith("lo"))
            sent = sum(c.bytes_sent for n, c in per.items() if not n.startswith("lo"))
            out["down"] = (recv - self._prev_net[0]) / dt
            out["up"] = (sent - self._prev_net[1]) / dt
            self._prev_net = (recv, sent)
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

    # ── update ──────────────────────────────────────────────────────────
    def _update_overview(self, s: SystemStats, procs=None):
        cfg = self.settings
        ex = self._extras()

        freq = f"{s.cpu_freq_mhz/1000:.1f} GHz" if s.cpu_freq_mhz > 0 else ""
        temp = ""
        if cfg.show_temp and s.cpu_temp > 0:
            temp = f"{s.cpu_temp:.0f}°C"
            if s.thermal_throttling:
                temp += "  throttling"
        self._cpu.set(s.cpu_percent, freq, temp)

        if cfg.show_gpu and s.gpu_available:
            self._gpu.set_visible(True)
            l2 = ""
            if s.gpu_mem_total_mb > 0:
                l2 = f"{s.gpu_mem_used_mb/1024:.1f} / {s.gpu_mem_total_mb/1024:.1f} GB"
            if cfg.show_temp and s.gpu_temp > 0:
                l2 += ("  " if l2 else "") + f"{s.gpu_temp:.0f}°C"
            self._gpu.set(s.gpu_percent, s.gpu_name or "", l2)
        else:
            self._gpu.set_visible(False)

        l2 = f"swap {s.swap_used_gb:.1f} / {s.swap_total_gb:.1f} GB" if s.swap_total_gb > 0 else ""
        self._ram.set(s.ram_percent, f"{s.ram_used_gb:.1f} / {s.ram_total_gb:.1f} GB", l2)

        if "disk_pct" in ex:
            sub2 = f"↓{_fmt_rate(ex['rd'])}  ↑{_fmt_rate(ex['wr'])}" if "rd" in ex else ""
            self._disk.set(ex["disk_pct"],
                           f"{_fmt_bytes(ex['disk_used'])} / {_fmt_bytes(ex['disk_total'])}",
                           sub2)

        if self._cpu.expanded:
            self._update_cpu_detail(s)
        if self._gpu.expanded and s.gpu_available:
            self._update_gpu_detail(s)
        if self._ram.expanded:
            self._update_ram_detail(s)
        if self._disk.expanded:
            self._update_disk_detail()

        if "down" in ex:
            self._net.set(f"↓ {_fmt_rate(ex['down'])}   ↑ {_fmt_rate(ex['up'])}")

        sensor_parts = []
        if cfg.show_temp and s.cpu_temp > 0:
            sensor_parts.append(f"CPU {s.cpu_temp:.0f}°C")
        if cfg.show_temp and s.gpu_available and s.gpu_temp > 0:
            sensor_parts.append(f"GPU {s.gpu_temp:.0f}°C")
        for fan in (s.fans or []):
            sensor_parts.append(f"{fan[0]} {fan[1]}rpm")
        if sensor_parts:
            self._sensors.set("   ".join(sensor_parts))
            self._sensors.set_visible(True)
        else:
            self._sensors.set_visible(False)

        if "load" in ex:
            self._load.set("  ".join(f"{x:.2f}" for x in ex["load"]))
        if "uptime" in ex:
            up = _fmt_uptime(ex["uptime"])
            if "nproc" in ex:
                up += f"   ·   {ex['nproc']} processes"
            self._uptime.set(up)

        procs = procs or []
        for i, (row, name, val) in enumerate(self._proc_rows):
            if i < len(procs):
                row.set_visible(True)
                name.set_text(procs[i].name)
                val.set_text(f"{procs[i].cpu_percent:.0f}%")
            else:
                row.set_visible(False)
        self._proc_title.set_visible(bool(procs))

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
