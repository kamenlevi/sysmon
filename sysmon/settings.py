"""Settings management and dialog."""
import json
import os

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

SETTINGS_PATH = os.path.expanduser("~/.config/sysmon/settings.json")

DEFAULTS = {
    "show_cpu": True,
    "show_gpu": True,
    "show_ram": True,
    "show_temp": True,
    "show_label": True,
    "warn_cpu_temp": 90,
    "warn_gpu_temp": 85,
    "warn_ram_pct": 90,
    "warn_cpu_pct": 99,
    "notify_desktop": True,
    "poll_interval": 1.5,
    "history_hours": 24,
    "graph_window_sec": 300,
    "dark_popup": True,
    # Which view a left-click opens: "menu" (native menu) or "panel".
    "default_view": "menu",
    # Default time window (seconds) for the usage-history view.
    "history_default_window": 1800,
    # Mountpoint shown on the main Disk gauge.
    "main_disk": "/",
    # Expand-animation duration in ms (0 = instant).
    "anim_ms": 170,
    # Quick-stats popup geometry (persisted across sessions)
    "popup_x": -1,
    "popup_y": -1,
    "popup_w": 340,
    "popup_h": 420,
}


class Settings:
    def __init__(self):
        self._data = dict(DEFAULTS)
        self._load()

    def _load(self):
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH) as f:
                    self._data.update(json.load(f))
            except Exception:
                pass

    def save(self):
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w") as f:
            json.dump(self._data, f, indent=2)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._data.get(name, DEFAULTS.get(name))

    def __setattr__(self, name, value):
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self._data[name] = value


def open_settings_dialog(settings: Settings, parent=None):
    dlg = Gtk.Dialog(
        title="SysMon Settings",
        transient_for=parent,
        flags=0,
    )
    dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                    Gtk.STOCK_OK, Gtk.ResponseType.OK)
    dlg.set_default_size(380, 460)

    box = dlg.get_content_area()
    box.set_margin_start(16)
    box.set_margin_end(16)
    box.set_margin_top(12)
    box.set_margin_bottom(12)
    box.set_spacing(6)

    def section(title):
        lbl = Gtk.Label(xalign=0)
        lbl.set_markup(f"<b>{title}</b>")
        lbl.set_margin_top(10)
        box.pack_start(lbl, False, False, 0)

    def toggle(label, attr):
        cb = Gtk.CheckButton(label=label)
        cb.set_active(getattr(settings, attr))
        box.pack_start(cb, False, False, 0)
        return cb

    def spin(label, attr, min_v, max_v, step=1, digits=0):
        row = Gtk.Box(spacing=8)
        lbl = Gtk.Label(label=label, xalign=0)
        lbl.set_size_request(200, -1)
        adj = Gtk.Adjustment(value=getattr(settings, attr),
                             lower=min_v, upper=max_v,
                             step_increment=step, page_increment=step * 10)
        sp = Gtk.SpinButton(adjustment=adj, digits=digits)
        row.pack_start(lbl, True, True, 0)
        row.pack_start(sp, False, False, 0)
        box.pack_start(row, False, False, 0)
        return sp

    def scale(label, attr, min_v, max_v, step):
        row = Gtk.Box(spacing=8)
        lbl = Gtk.Label(label=label, xalign=0)
        lbl.set_size_request(200, -1)
        adj = Gtk.Adjustment(value=getattr(settings, attr), lower=min_v,
                             upper=max_v, step_increment=step)
        sc = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj)
        sc.set_digits(0)
        sc.set_value_pos(Gtk.PositionType.RIGHT)
        sc.set_size_request(150, -1)
        row.pack_start(lbl, True, True, 0)
        row.pack_start(sc, False, False, 0)
        box.pack_start(row, False, False, 0)
        return sc

    # Most-used settings first.
    section("Display")
    t_gpu = toggle("Show GPU section", "show_gpu")
    t_temp = toggle("Show temperatures", "show_temp")

    section("Behaviour")
    t_notify = toggle("Desktop notifications for warnings", "notify_desktop")
    sp_poll = spin("Poll interval (seconds)", "poll_interval", 0.5, 10.0, 0.5, 1)
    sp_hist = spin("History retention (hours)", "history_hours", 1, 168)
    sp_graph = spin("Graph window (seconds)", "graph_window_sec", 30, 3600, 30)
    sc_anim = scale("Animation speed (ms, 0 = instant)", "anim_ms", 0, 600, 10)

    section("Warning Thresholds")
    sp_cpu_temp = spin("CPU temp warning (°C)", "warn_cpu_temp", 60, 110)
    sp_gpu_temp = spin("GPU temp warning (°C)", "warn_gpu_temp", 60, 110)
    sp_ram = spin("RAM warning (%)", "warn_ram_pct", 50, 100)
    sp_cpu_pct = spin("CPU load warning (%)", "warn_cpu_pct", 50, 100)

    box.show_all()

    result = dlg.run()
    if result == Gtk.ResponseType.OK:
        settings.warn_cpu_temp = sp_cpu_temp.get_value_as_int()
        settings.warn_gpu_temp = sp_gpu_temp.get_value_as_int()
        settings.warn_ram_pct = sp_ram.get_value_as_int()
        settings.warn_cpu_pct = sp_cpu_pct.get_value_as_int()
        settings.show_gpu = t_gpu.get_active()
        settings.show_temp = t_temp.get_active()
        settings.notify_desktop = t_notify.get_active()
        settings.poll_interval = sp_poll.get_value()
        settings.history_hours = sp_hist.get_value_as_int()
        settings.graph_window_sec = sp_graph.get_value_as_int()
        settings.anim_ms = int(sc_anim.get_value())
        settings.save()
    dlg.destroy()
