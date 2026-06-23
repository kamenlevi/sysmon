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


class BaroIndicator:
    def __init__(self, monitor, history, settings):
        self.monitor = monitor
        self.history = history
        self.settings = settings
        self._main_window = None
        self._last_stats = SystemStats()
        self._net_prev = _net_totals()
        self._net_t = time.monotonic()
        self._net_rate = (0.0, 0.0)
        # Rolling per-core (+ gpu) history so the Cores panel has the past,
        # not just realtime. ~2000 samples ≈ 50 min at the default poll.
        self._core_hist = []
        self._CORE_HIST_MAX = 2000

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

        # One panel window hosting every view (overview + drill-ins) in a
        # Stack — switching is instant, no window map/unmap lag.
        self._panel_geom = None
        self._popup = PopupWindow(
            settings=settings,
            history_db=history,
            on_open_app=self._show_main_window,
            on_settings=lambda: self._on_settings(),
            on_quit=Gtk.main_quit,
        )
        self._popup._fan_controller = self._fan_controller

        if _HAS_INDICATOR:
            self._indicator = AppIndicator.Indicator.new(
                "baro", "utilities-system-monitor",
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
        # (Network line omitted from the menu — its variable width was the
        # only thing that made the menu wobble. It's in the detailed panel.)

        menu.append(Gtk.SeparatorMenuItem())

        # (Disks omitted — the Disk row's submenu already lists every disk.)
        for label, view in (("Detailed panel…", "overview"),
                            ("Processes…", "processes"),
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
        # Only push items that actually changed — far less DBus churn, so the
        # native menu stays snappy while open.
        for i, it in enumerate(items):
            if i < len(lines):
                if getattr(it, "_ll", None) != lines[i]:
                    it.set_label(lines[i])
                    it._ll = lines[i]
                if not it.get_visible():
                    it.set_visible(True)
            elif it.get_visible():
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
            lines.append(f"{_short_mount(part.mountpoint)}:  {u.percent:.0f}%  "
                         f"({u.used/(1024**3):.0f}/{u.total/(1024**3):.0f} GB)")
        return lines

    def _set_gauge(self, item, key, pct, label):
        if getattr(item, "_ll", None) != label:
            _mono(item, label)
            item._ll = label
        img = item.get_image()
        if img is not None:
            rp = int(round(pct))
            if getattr(item, "_gpct", None) != rp:
                img.set_from_file(gen_donut_icon(pct, key, size=_GAUGE_PX))
                item._gpct = rp

    def _main_disk(self):
        return getattr(self.settings, "main_disk", "/") or "/"

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
            disk_pct = psutil.disk_usage(self._main_disk()).percent
        except Exception:
            disk_pct = 0.0
        self._set_gauge(self._mi_disk, "disk", disk_pct,
                        f"Disk   {_pct3(disk_pct)}%")
        self._fill_items(self._disk_items, self._disk_lines())

    # ── View opening ─────────────────────────────────────────────────────────

    def _open_view(self, page):
        from .panel_base import CaretPanel
        if not self._popup.get_visible():
            self._panel_geom = CaretPanel.cursor_geometry()
            self._popup.open_to(page)
            self._popup.update(self._last_stats, self._collect_procs(6),
                               self._core_hist)
            self._popup.show_at(*self._panel_geom)
        else:
            # Already open — just switch page (instant, no remap).
            self._popup.open_to(page)
            self._popup.update(self._last_stats, self._collect_procs(6),
                               self._core_hist)

    def _open_panel(self):
        self._open_view("overview")

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

        # Network rate (real interfaces only — loopback excluded)
        now = time.monotonic()
        dt = max(0.001, now - self._net_t)
        recv, sent = _net_totals()
        self._net_rate = ((recv - self._net_prev[0]) / dt,
                          (sent - self._net_prev[1]) / dt)
        self._net_prev = (recv, sent)
        self._net_t = now

        # Persist to history so the history view has data.
        try:
            self.history.record(s)
        except Exception:
            pass

        # Per-core history buffer (always recorded so it's there on open).
        self._core_hist.append((
            time.time(), list(s.cpu_per_core or []),
            s.gpu_percent if s.gpu_available else None))
        if len(self._core_hist) > self._CORE_HIST_MAX:
            del self._core_hist[: -self._CORE_HIST_MAX]

        procs = self._collect_procs(8)
        GLib.idle_add(self._refresh_menu, s, procs)
        if self._popup.get_visible():
            GLib.idle_add(self._popup.update, s, procs, self._core_hist)
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
        n = Notify.Notification.new("⚠ Baro Warning", body, "dialog-warning")
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


def _short_mount(mp, n=18):
    """Truncate a long mountpoint in the middle so the menu can't grow wide."""
    if len(mp) <= n:
        return mp
    keep = n - 1
    return mp[: keep // 2] + "…" + mp[-(keep - keep // 2):]


def _net_totals():
    """Total (recv, sent) bytes across real interfaces, excluding loopback."""
    try:
        per = psutil.net_io_counters(pernic=True)
        recv = sum(c.bytes_recv for n, c in per.items() if not n.startswith("lo"))
        sent = sum(c.bytes_sent for n, c in per.items() if not n.startswith("lo"))
        return recv, sent
    except Exception:
        try:
            c = psutil.net_io_counters()
            return c.bytes_recv, c.bytes_sent
        except Exception:
            return 0, 0


def _mono(item, text):
    """Render a menu item's label in monospace so its width never changes."""
    lbl = item.get_child()
    if isinstance(lbl, Gtk.Label):
        lbl.set_use_markup(True)
        lbl.set_markup("<tt>" + GLib.markup_escape_text(text) + "</tt>")
    else:
        item.set_label(text)


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
        super().__init__(application_id="com.baro.app")
        self.register()
