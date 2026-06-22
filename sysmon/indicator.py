"""AppIndicator tray icon.

On GNOME a left-click can only open the system's native menu (the app
can't pop its own window from that click), so the menu IS the main view:
clicking the icon opens it directly with donut-gauge stats inline, plus
entries that drill into the detailed panel, processes (with kill), disks,
usage history and per-core graphs.
"""
import time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Notify", "0.7")
from gi.repository import Gtk, GLib, Notify

import psutil

from .icon_gen import generate_tray_icon, gen_donut_icon
from .monitor import SystemStats
from .popup import PopupWindow
from .settings import open_settings_dialog

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
    _HAS_INDICATOR = True
except Exception:
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator
        _HAS_INDICATOR = True
    except Exception:
        _HAS_INDICATOR = False


_last_warn_notify_time = [0.0]
_NOTIFY_COOLDOWN = 30.0
_GAUGE_PX = 24


class SysMonIndicator:
    def __init__(self, monitor, history, settings):
        self.monitor = monitor
        self.history = history
        self.settings = settings
        self._main_window = None
        self._last_stats = SystemStats()
        self._net_prev = psutil.net_io_counters()
        self._net_t = time.monotonic()
        self._net_rate = (0.0, 0.0)

        # Fan controller
        from .fans import detect_fans, FanCurveController
        self._fans = detect_fans()
        self._fan_controllable_by_label = {
            f.label: f.controllable for f in self._fans.values()
        }
        self._fan_controller = FanCurveController(
            self._fans, lambda: self._last_stats.cpu_temp
        )
        self._fan_controller.start()

        # Detailed panel + drill-in panels — all share the caret-panel style
        # and the same on-screen position.
        self._panel_geom = None
        self._popup = PopupWindow(
            on_open_app=self._show_main_window,
            settings=settings,
            on_settings=lambda: self._on_settings(),
            on_quit=Gtk.main_quit,
            on_cores=lambda: self._open_view("cores", fresh=False),
            on_nav=lambda v: self._open_view(v, fresh=False),
        )
        self._popup._fan_controller = self._fan_controller

        from .cores_window import CoresPanel
        from .detail_views import HistoryPanel, ProcessesPanel, DisksPanel
        self._cores = CoresPanel()
        self._history = HistoryPanel(history, settings)
        self._proc = ProcessesPanel()
        self._disks = DisksPanel()
        # The back arrow on any drill-in returns to the detailed panel.
        for p in (self._cores, self._history, self._proc, self._disks):
            p.on_back = lambda: self._open_view("panel", fresh=False)

        if _HAS_INDICATOR:
            self._indicator = AppIndicator.Indicator.new(
                "sysmon", "utilities-system-monitor",
                AppIndicator.IndicatorCategory.HARDWARE,
            )
            self._indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self._indicator.set_menu(self._build_menu())
        else:
            self._status_icon = Gtk.StatusIcon()
            self._status_icon.set_from_icon_name("utilities-system-monitor")
            self._status_icon.connect("activate", lambda *_: self._open_panel())

        monitor.add_callback(self._on_stats)
        self._set_static_icon()
        self._refresh_menu(self._last_stats, [])
        self._update_label()
        GLib.timeout_add(1500, self._update_label)

    # ── Main view: the native menu ───────────────────────────────────────────

    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()

        def gauge_row():
            it = Gtk.ImageMenuItem(label="")
            it.set_image(Gtk.Image())
            it.set_always_show_image(True)
            sub = Gtk.Menu()
            it.set_submenu(sub)
            menu.append(it)
            return it, self._make_sub_items(sub, 40)

        self._mi_cpu, self._cpu_items = gauge_row()
        self._mi_gpu, self._gpu_items = gauge_row()
        self._mi_gpu.set_no_show_all(True)
        self._mi_ram, self._ram_items = gauge_row()
        self._mi_disk, self._disk_items = gauge_row()

        self._mi_net = Gtk.MenuItem(label="")
        self._mi_net.set_sensitive(False)
        menu.append(self._mi_net)

        menu.append(Gtk.SeparatorMenuItem())

        for label, view in (("Detailed panel…", "panel"),
                            ("Processes…", "processes"),
                            ("Disks…", "disks"),
                            ("Usage history…", "history"),
                            ("CPU / GPU cores…", "cores")):
            it = Gtk.MenuItem(label=label)
            it.connect("activate", lambda _w, v=view: self._open_view(v))
            menu.append(it)

        menu.append(Gtk.SeparatorMenuItem())

        settings_item = Gtk.MenuItem(label="Settings…")
        settings_item.connect("activate", self._on_settings)
        menu.append(settings_item)
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        menu.append(quit_item)

        menu.show_all()
        return menu

    def _make_sub_items(self, submenu, n):
        items = []
        for _ in range(n):
            it = Gtk.MenuItem(label="")
            it.set_sensitive(False)
            it.set_no_show_all(True)
            submenu.append(it)
            items.append(it)
        submenu.show_all()
        return items

    @staticmethod
    def _fill_items(items, lines):
        for i, it in enumerate(items):
            if i < len(lines):
                it.set_label(lines[i])
                it.set_visible(True)
            else:
                it.set_visible(False)

    def _disk_lines(self):
        lines = []
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
            lines.append(f"{part.mountpoint}:  {u.percent:.0f}%  "
                         f"({u.used/(1024**3):.0f}/{u.total/(1024**3):.0f} GB)")
        return lines

    def _set_gauge(self, item, key, pct, label):
        item.set_label(label)
        img = item.get_image()
        if img is not None:
            img.set_from_file(gen_donut_icon(pct, key, size=_GAUGE_PX))

    def _refresh_menu(self, s: SystemStats, procs):
        if not _HAS_INDICATOR:
            return
        cfg = self.settings
        self._set_gauge(self._mi_cpu, "cpu", s.cpu_percent,
                        f"CPU   {_pct3(s.cpu_percent)}%")
        cpu_lines = []
        if s.cpu_freq_mhz > 0:
            cpu_lines.append(f"Frequency:  {s.cpu_freq_mhz/1000:.2f} / "
                             f"{s.cpu_freq_max_mhz/1000:.2f} GHz")
        if cfg.show_temp and s.cpu_temp > 0:
            cpu_lines.append(f"Temperature:  {s.cpu_temp:.0f}°C")
        for i, c in enumerate(s.cpu_per_core or []):
            cpu_lines.append(f"Core {i}:  {c:.0f}%")
        self._fill_items(self._cpu_items, cpu_lines)

        if cfg.show_gpu and s.gpu_available:
            self._mi_gpu.set_visible(True)
            self._set_gauge(self._mi_gpu, "gpu", s.gpu_percent,
                            f"GPU   {_pct3(s.gpu_percent)}%")
            gpu_lines = []
            if s.gpu_name:
                gpu_lines.append(s.gpu_name)
            if s.gpu_mem_total_mb > 0:
                gpu_lines.append(f"VRAM:  {s.gpu_mem_used_mb/1024:.1f} / "
                                 f"{s.gpu_mem_total_mb/1024:.1f} GB")
            if cfg.show_temp and s.gpu_temp > 0:
                gpu_lines.append(f"Temperature:  {s.gpu_temp:.0f}°C")
            if s.gpu_power_w > 0:
                gpu_lines.append(f"Power:  {s.gpu_power_w:.0f} W")
            self._fill_items(self._gpu_items, gpu_lines)
        else:
            self._mi_gpu.set_visible(False)

        self._set_gauge(self._mi_ram, "ram", s.ram_percent,
                        f"Memory   {_pct3(s.ram_percent)}%")
        ram_lines = [f"Used:  {s.ram_used_gb:.1f} / {s.ram_total_gb:.1f} GB"]
        if s.swap_total_gb > 0:
            ram_lines.append(f"Swap:  {s.swap_used_gb:.1f} / {s.swap_total_gb:.1f} GB")
        self._fill_items(self._ram_items, ram_lines)

        try:
            disk_pct = psutil.disk_usage("/").percent
        except Exception:
            disk_pct = 0.0
        self._set_gauge(self._mi_disk, "disk", disk_pct,
                        f"Disk   {_pct3(disk_pct)}%")
        self._fill_items(self._disk_items, self._disk_lines())

        down, up = self._net_rate
        self._mi_net.set_label(f"Network   ↓ {_rate(down)}   ↑ {_rate(up)}")

    # ── View opening ─────────────────────────────────────────────────────────

    def _panels(self):
        return (self._popup, self._cores, self._history, self._proc, self._disks)

    def _open_view(self, view, fresh=True):
        from .panel_base import CaretPanel
        # Fresh opens (from the menu) appear under the cursor; navigation
        # (panel rows / back arrow) reuses that spot so panels stay put.
        if fresh or self._panel_geom is None:
            self._panel_geom = CaretPanel.cursor_geometry()

        target = {"panel": self._popup, "cores": self._cores,
                  "history": self._history, "processes": self._proc,
                  "disks": self._disks}.get(view)
        if target is None:
            return

        if target is self._popup:
            target.update(self._last_stats, self._collect_procs(6))
        elif target is self._cores:
            target.update(self._last_stats)
        else:
            target.refresh()

        for p in self._panels():
            if p is not target:
                p.hide()
        target.show_at(*self._panel_geom)

    def _open_panel(self, fresh=True):
        self._open_view("panel", fresh=fresh)

    def _collect_procs(self, n):
        try:
            from .processes import collect_top_processes
            return collect_top_processes(n, sort_by="cpu")
        except Exception:
            return []

    # ── Stats callback ───────────────────────────────────────────────────────

    def _on_stats(self, s: SystemStats):
        self._last_stats = s
        if s.fans:
            ctrl_by_label = self._fan_controllable_by_label
            s.fans = [
                (label, rpm, ctrl_by_label.get(label, False))
                for label, rpm, _ in s.fans
            ]

        # Network rate
        now = time.monotonic()
        dt = max(0.001, now - self._net_t)
        try:
            nio = psutil.net_io_counters()
            self._net_rate = (
                (nio.bytes_recv - self._net_prev.bytes_recv) / dt,
                (nio.bytes_sent - self._net_prev.bytes_sent) / dt,
            )
            self._net_prev = nio
            self._net_t = now
        except Exception:
            pass

        # Persist to history so the history view has data.
        try:
            self.history.record(s)
        except Exception:
            pass

        procs = self._collect_procs(8)
        GLib.idle_add(self._refresh_menu, s, procs)
        if self._popup.get_visible():
            GLib.idle_add(self._popup.update, s, procs)
        if self._cores.get_visible():
            GLib.idle_add(self._cores.update, s)
        if self._proc.get_visible():
            GLib.idle_add(self._proc.refresh)
        if self._disks.get_visible():
            GLib.idle_add(self._disks.refresh)
        if self._history.get_visible():
            GLib.idle_add(self._history.refresh)
        self._maybe_notify(s)

    def _maybe_notify(self, s: SystemStats):
        if not s.warnings:
            return
        if not self.settings.notify_desktop:
            return
        now = time.time()
        if now - _last_warn_notify_time[0] < _NOTIFY_COOLDOWN:
            return
        _last_warn_notify_time[0] = now
        body = "\n".join(f"• {w}" for w in s.warnings)
        n = Notify.Notification.new("⚠ SysMon Warning", body, "dialog-warning")
        n.set_urgency(Notify.Urgency.CRITICAL)
        try:
            n.show()
        except Exception:
            pass

    # ── Menu-bar icon + label ────────────────────────────────────────────────

    def _set_static_icon(self):
        icon_path = generate_tray_icon()
        if _HAS_INDICATOR:
            import os
            icon_dir = os.path.dirname(icon_path)
            icon_name = os.path.splitext(os.path.basename(icon_path))[0]
            self._indicator.set_icon_theme_path(icon_dir)
            self._indicator.set_icon_full(icon_name, "system monitor")
        else:
            try:
                from gi.repository import GdkPixbuf
                pb = GdkPixbuf.Pixbuf.new_from_file(icon_path)
                self._status_icon.set_from_pixbuf(pb)
            except Exception:
                pass

    def _update_label(self) -> bool:
        if not _HAS_INDICATOR:
            return True
        s = self._last_stats
        parts = [f"CPU {_pct3(s.cpu_percent)}%"]
        if self.settings.show_gpu and s.gpu_available:
            parts.append(f"GPU {_pct3(s.gpu_percent)}%")
        parts.append(f"RAM {_pct3(s.ram_percent)}%")
        label = "  ".join(parts)
        self._indicator.set_label(label, label)
        return True

    # ── Other windows ────────────────────────────────────────────────────────

    def _show_main_window(self):
        if self._main_window is None:
            app = _DummyApp()
            from .main_window import MainWindow
            self._main_window = MainWindow(
                app, self.monitor, self.history, self.settings,
                fan_channels=self._fans,
                fan_controller=self._fan_controller,
            )
        self._main_window.present()

    def _on_settings(self, *_):
        open_settings_dialog(self.settings, parent=self._main_window)


_FIG = "\u2007"   # figure space - same width as a digit


def _pct3(v) -> str:
    """A percentage padded to 3 digit-widths so it never shifts."""
    return f"{int(round(max(0.0, min(v, 100.0))))}".rjust(3, _FIG)


def _rate(bps: float) -> str:
    # Fixed digit-width so the menu never changes width.
    val, unit = bps, "B/s "
    for u in ("KB/s", "MB/s", "GB/s"):
        if val < 1024:
            break
        val /= 1024
        unit = u
    return f"{int(round(val))}".rjust(4, _FIG) + f" {unit}"


class _DummyApp(Gtk.Application):
    """Minimal Gtk.Application shim so MainWindow can call super().__init__(application=app)."""
    def __init__(self):
        super().__init__(application_id="com.sysmon.app")
        self.register()
