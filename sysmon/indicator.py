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

        # Detailed showcase panel + drill-in windows.
        self._popup = PopupWindow(
            on_open_app=self._show_main_window,
            settings=settings,
            on_settings=lambda: self._on_settings(),
            on_quit=Gtk.main_quit,
            on_cores=self._open_cores,
            on_set_default=lambda *_: None,
        )
        self._popup._fan_controller = self._fan_controller
        self._popup.on_nav = self._open_view   # panel rows can drill in

        from .cores_window import CoresWindow
        from .detail_views import HistoryView, ProcessesView, DisksView
        self._cores = CoresWindow()
        self._history_view = HistoryView(history, settings)
        self._proc_view = ProcessesView()
        self._disks_view = DisksView()

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
            menu.append(it)
            return it

        self._mi_cpu = gauge_row()
        self._mi_gpu = gauge_row()
        self._mi_gpu.set_no_show_all(True)
        self._mi_ram = gauge_row()
        self._mi_disk = gauge_row()

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
                        f"CPU   {s.cpu_percent:.0f}%")
        if cfg.show_gpu and s.gpu_available:
            self._mi_gpu.set_visible(True)
            self._set_gauge(self._mi_gpu, "gpu", s.gpu_percent,
                            f"GPU   {s.gpu_percent:.0f}%")
        else:
            self._mi_gpu.set_visible(False)
        self._set_gauge(self._mi_ram, "ram", s.ram_percent,
                        f"Memory   {s.ram_percent:.0f}%")
        try:
            disk_pct = psutil.disk_usage("/").percent
        except Exception:
            disk_pct = 0.0
        self._set_gauge(self._mi_disk, "disk", disk_pct,
                        f"Disk   {disk_pct:.0f}%")
        down, up = self._net_rate
        self._mi_net.set_label(f"Network   ↓ {_rate(down)}   ↑ {_rate(up)}")

    # ── View opening ─────────────────────────────────────────────────────────

    def _open_view(self, view):
        if view == "panel":
            self._open_panel()
        elif view == "cores":
            self._open_cores()
        elif view == "history":
            self._history_view.refresh()
            self._history_view.present_window()
        elif view == "processes":
            self._proc_view.refresh()
            self._proc_view.present_window()
        elif view == "disks":
            self._disks_view.refresh()
            self._disks_view.present_window()

    def _open_panel(self):
        if not self._popup.get_visible():
            self._popup.update(self._last_stats, self._collect_procs(6))
        self._popup.show_near_top_right()

    def _open_cores(self):
        self._cores.update(self._last_stats)
        self._cores.present_window()

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
        if self._proc_view.get_visible():
            GLib.idle_add(self._proc_view.refresh)
        if self._disks_view.get_visible():
            GLib.idle_add(self._disks_view.refresh)
        if self._history_view.get_visible():
            GLib.idle_add(self._history_view.refresh)
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
        parts = [f"CPU {s.cpu_percent:3.0f}%"]
        if self.settings.show_gpu and s.gpu_available:
            parts.append(f"GPU {s.gpu_percent:3.0f}%")
        parts.append(f"RAM {s.ram_percent:3.0f}%")
        label = "  ".join(parts)
        self._indicator.set_label(label, "  ".join("CPU 100%" for _ in parts))
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


def _rate(bps: float) -> str:
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024:
            return f"{bps:.0f} {unit}"
        bps /= 1024
    return f"{bps:.0f} TB/s"


class _DummyApp(Gtk.Application):
    """Minimal Gtk.Application shim so MainWindow can call super().__init__(application=app)."""
    def __init__(self):
        super().__init__(application_id="com.sysmon.app")
        self.register()
