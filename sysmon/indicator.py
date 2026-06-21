"""AppIndicator3 tray icon with live icon generation and popup panel."""
import time
import threading

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Notify", "0.7")
from gi.repository import Gtk, GLib, Notify

from .icon_gen import generate_tray_icon
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


class SysMonIndicator:
    def __init__(self, monitor, history, settings):
        self.monitor = monitor
        self.history = history
        self.settings = settings
        self._main_window = None
        self._last_stats = SystemStats()
        self._cpu_history = []          # rolling CPU% for the live tray graph
        self._CPU_HISTORY_LEN = 30

        # Fan controller
        from .fans import detect_fans, FanCurveController
        self._fans = detect_fans()
        # label → controllable, computed once. Used to enrich every stats
        # tick without an O(n*m) scan of detected fans.
        self._fan_controllable_by_label = {
            f.label: f.controllable for f in self._fans.values()
        }
        self._fan_controller = FanCurveController(
            self._fans, lambda: self._last_stats.cpu_temp
        )
        self._fan_controller.start()

        self._popup = PopupWindow(
            on_open_app=self._show_main_window,
            settings=settings,
            on_settings=lambda: self._on_settings(),
            on_quit=Gtk.main_quit,
        )
        self._popup._fan_controller = self._fan_controller

        if _HAS_INDICATOR:
            self._indicator = AppIndicator.Indicator.new(
                "sysmon",
                "utilities-system-monitor",
                AppIndicator.IndicatorCategory.HARDWARE,
            )
            self._indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            # AppIndicator forces a menu on left-click; we auto-activate its
            # single item so the click opens the stats panel directly. The
            # label is a fallback in case auto-activate is suppressed.
            menu = Gtk.Menu()
            item = Gtk.MenuItem(label="Show / hide stats")
            item.connect("activate", lambda *_: self._toggle_popup())
            menu.append(item)
            menu.show_all()
            menu.connect("show", self._on_menu_show)
            self._indicator.set_menu(menu)
        else:
            self._status_icon = Gtk.StatusIcon()
            self._status_icon.set_from_icon_name("utilities-system-monitor")
            self._status_icon.connect("activate", self._on_tray_click)

        monitor.add_callback(self._on_stats)
        self._update_icon()
        GLib.timeout_add(1500, self._update_icon)

    def _on_stats(self, s: SystemStats):
        self._last_stats = s
        # Feed the rolling CPU history for the live tray graph.
        self._cpu_history.append(s.cpu_percent)
        if len(self._cpu_history) > self._CPU_HISTORY_LEN:
            del self._cpu_history[: -self._CPU_HISTORY_LEN]
        # Enrich fan data with controllable flag from detected fans (O(n)).
        if s.fans:
            ctrl_by_label = self._fan_controllable_by_label
            s.fans = [
                (label, rpm, ctrl_by_label.get(label, False))
                for label, rpm, _ in s.fans
            ]
        # Don't bother updating the popup when it's hidden — saves a marshalled
        # idle callback plus a full GTK widget tree update every tick.
        if self._popup.get_visible():
            try:
                from .processes import collect_top_processes
                procs = collect_top_processes(5, sort_by="cpu")
            except Exception:
                procs = []
            GLib.idle_add(self._popup.update, s, procs)
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

    def _update_icon(self) -> bool:
        """Redraw the live menu-bar graph from the rolling CPU history."""
        s = self._last_stats
        icon_path = generate_tray_icon(
            cpu_history=self._cpu_history,
            ram_pct=s.ram_percent,
        )
        if _HAS_INDICATOR:
            import os
            icon_dir = os.path.dirname(icon_path)
            icon_name = os.path.splitext(os.path.basename(icon_path))[0]
            self._indicator.set_icon_theme_path(icon_dir)
            self._indicator.set_icon_full(icon_name, "system monitor")
            self._indicator.set_label("", "")
        else:
            try:
                from gi.repository import GdkPixbuf
                pb = GdkPixbuf.Pixbuf.new_from_file(icon_path)
                self._status_icon.set_from_pixbuf(pb)
            except Exception:
                pass
        return True  # keep the timer running

    def _on_menu_show(self, menu):
        # Left-click opened the (one-item) menu. Close it immediately and open
        # the stats panel instead, so a single click goes straight to stats.
        def go():
            menu.popdown()
            self._toggle_popup()
            return False
        GLib.idle_add(go)

    def _toggle_popup(self):
        # Pre-fill with current data so the panel never flashes empty.
        if not self._popup.get_visible():
            try:
                from .processes import collect_top_processes
                procs = collect_top_processes(5, sort_by="cpu")
            except Exception:
                procs = []
            self._popup.update(self._last_stats, procs)
        self._popup.show_near_top_right()   # handles toggle internally

    def _on_tray_click(self, *_):
        self._toggle_popup()

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


class _DummyApp(Gtk.Application):
    """Minimal Gtk.Application shim so MainWindow can call super().__init__(application=app)."""
    def __init__(self):
        super().__init__(application_id="com.sysmon.app")
        self.register()
