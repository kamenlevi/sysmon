"""Drill-in detail windows reached from the menu / panel.

Each is the same size, with a back arrow (←) in the top-left that returns
you to the desktop (the menu is one click away). Views:

  HistoryView   — CPU/RAM/GPU usage over a selectable time window
  ProcessesView — full process list with the ability to kill
  DisksView     — usage of every mounted disk
"""
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango

import cairo
import psutil

from .processes import collect_top_processes, terminate_group


# ── Base window: back arrow + title + content ───────────────────────────────

class _DetailBase(Gtk.Window):
    def __init__(self, title):
        super().__init__(title=f"SysMon — {title}")
        self.set_default_size(480, 440)
        self.connect("delete-event", self._on_close)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(root)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_start(8)
        header.set_margin_end(12)
        header.set_margin_top(8)
        header.set_margin_bottom(6)
        back = Gtk.Button(label="←")
        back.set_relief(Gtk.ReliefStyle.NONE)
        back.set_tooltip_text("Back")
        back.connect("clicked", lambda *_: self.hide())
        header.pack_start(back, False, False, 0)
        tlbl = Gtk.Label(xalign=0.0)
        tlbl.set_markup(f"<b>{title}</b>")
        header.pack_start(tlbl, True, True, 0)
        root.pack_start(header, False, False, 0)
        root.pack_start(Gtk.Separator(), False, False, 0)

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.content.set_margin_start(14)
        self.content.set_margin_end(14)
        self.content.set_margin_top(10)
        self.content.set_margin_bottom(12)
        root.pack_start(self.content, True, True, 0)

    def present_window(self):
        self.show_all()
        self.present()

    def _on_close(self, *_):
        self.hide()
        return True


# ── History ─────────────────────────────────────────────────────────────────

_WINDOWS = [("5 min", 300), ("30 min", 1800), ("1 hour", 3600),
            ("6 hours", 21600), ("24 hours", 86400)]
# (label, colour) per series
_SERIES = [("CPU", (0.23, 0.43, 0.65)),
           ("RAM", (0.62, 0.40, 0.66)),
           ("GPU", (0.30, 0.60, 0.38))]


class _MultiGraph(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self._rows = []       # (ts, cpu, ram, gpu)
        self._has_gpu = False
        self.set_size_request(-1, 240)
        self.connect("draw", self._draw)

    def set_data(self, rows, has_gpu):
        self._rows = rows
        self._has_gpu = has_gpu
        self.queue_draw()

    def _draw(self, _w, cr):
        a = self.get_allocation()
        w, h = a.width, a.height
        cr.set_source_rgba(0.99, 0.99, 0.99, 1.0)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        pad_l, pad_r, pad_t, pad_b = 32, 8, 10, 18
        gx, gy = pad_l, pad_t
        gw, gh = w - pad_l - pad_r, h - pad_t - pad_b

        # Gridlines + y labels (0/50/100)
        cr.set_source_rgba(0.88, 0.88, 0.88, 1.0)
        cr.set_line_width(1.0)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(9)
        for frac in (0.0, 0.5, 1.0):
            y = gy + gh * frac
            cr.move_to(gx, y)
            cr.line_to(gx + gw, y)
            cr.stroke()
            cr.set_source_rgba(0.6, 0.6, 0.6, 1.0)
            cr.move_to(4, y + 3)
            cr.show_text(f"{int(100*(1-frac))}")
            cr.set_source_rgba(0.88, 0.88, 0.88, 1.0)

        rows = self._rows
        if len(rows) >= 2:
            t0 = rows[0][0]
            t1 = rows[-1][0]
            span = max(1e-6, t1 - t0)

            def draw_series(idx, colour):
                cr.set_source_rgba(*colour, 0.95)
                cr.set_line_width(1.6)
                cr.set_line_join(cairo.LINE_JOIN_ROUND)
                started = False
                for r in rows:
                    x = gx + (r[0] - t0) / span * gw
                    y = gy + gh * (1.0 - max(0.0, min(r[idx], 100.0)) / 100.0)
                    if not started:
                        cr.move_to(x, y)
                        started = True
                    else:
                        cr.line_to(x, y)
                cr.stroke()

            draw_series(1, _SERIES[0][1])   # cpu
            draw_series(2, _SERIES[1][1])   # ram
            if self._has_gpu:
                draw_series(3, _SERIES[2][1])  # gpu
        else:
            cr.set_source_rgba(0.6, 0.6, 0.6, 1.0)
            cr.set_font_size(12)
            cr.move_to(gx + 10, gy + gh / 2)
            cr.show_text("Collecting history…")


class HistoryView(_DetailBase):
    def __init__(self, history_db, settings):
        super().__init__("Usage history")
        self.history = history_db
        self.settings = settings

        ctrl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ctrl.pack_start(Gtk.Label(label="Time window:"), False, False, 0)
        self._combo = Gtk.ComboBoxText()
        default = getattr(settings, "history_default_window", 1800)
        active = 1
        for i, (label, secs) in enumerate(_WINDOWS):
            self._combo.append(str(secs), label)
            if secs == default:
                active = i
        self._combo.set_active(active)
        self._combo.connect("changed", self._on_window_changed)
        ctrl.pack_start(self._combo, False, False, 0)

        save_default = Gtk.Button(label="Set as default")
        save_default.connect("clicked", self._on_set_default)
        ctrl.pack_end(save_default, False, False, 0)
        self.content.pack_start(ctrl, False, False, 0)

        # Legend
        legend = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        for name, colour in _SERIES:
            item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            swatch = Gtk.DrawingArea()
            swatch.set_size_request(14, 10)
            swatch.connect("draw", self._mk_swatch(colour))
            item.pack_start(swatch, False, False, 0)
            item.pack_start(Gtk.Label(label=name), False, False, 0)
            legend.pack_start(item, False, False, 0)
        self.content.pack_start(legend, False, False, 0)

        self._graph = _MultiGraph()
        self.content.pack_start(self._graph, True, True, 0)

    @staticmethod
    def _mk_swatch(colour):
        def draw(widget, cr):
            a = widget.get_allocation()
            cr.set_source_rgba(*colour, 0.95)
            cr.rectangle(0, a.height / 2 - 1.5, a.width, 3)
            cr.fill()
            return False
        return draw

    def _on_window_changed(self, *_):
        self.refresh()

    def _on_set_default(self, *_):
        secs = int(self._combo.get_active_id())
        self.settings.history_default_window = secs
        try:
            self.settings.save()
        except Exception:
            pass

    def refresh(self):
        secs = int(self._combo.get_active_id() or 1800)
        try:
            raw = self.history.fetch(secs)
        except Exception:
            raw = []
        # raw rows: (ts, cpu, cpu_temp, ram, gpu, gpu_temp)
        rows = [(r[0], r[1], r[3], r[4]) for r in raw]
        has_gpu = any(r[3] > 0 for r in rows)
        self._graph.set_data(rows, has_gpu)


# ── Processes ────────────────────────────────────────────────────────────────

class ProcessesView(_DetailBase):
    def __init__(self):
        super().__init__("Processes")
        self._sort = "cpu"

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.pack_start(Gtk.Label(label="Sort by:"), False, False, 0)
        self._sort_combo = Gtk.ComboBoxText()
        self._sort_combo.append("cpu", "CPU")
        self._sort_combo.append("ram", "Memory")
        self._sort_combo.set_active(0)
        self._sort_combo.connect("changed", self._on_sort)
        bar.pack_start(self._sort_combo, False, False, 0)
        self.content.pack_start(bar, False, False, 0)

        # Header row
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        for text, w, x in (("Process", -1, 0.0), ("CPU", 70, 1.0),
                           ("Memory", 90, 1.0), ("", 60, 0.5)):
            l = Gtk.Label(label=text, xalign=x)
            l.set_markup(f"<small><b>{text}</b></small>")
            if w > 0:
                l.set_size_request(w, -1)
            hdr.pack_start(l, text == "Process", text == "Process", 0)
        self.content.pack_start(hdr, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        scroll.add(self._list)
        self.content.pack_start(scroll, True, True, 0)

    def _on_sort(self, *_):
        self._sort = self._sort_combo.get_active_id()
        self.refresh()

    def refresh(self):
        try:
            procs = collect_top_processes(25, sort_by=self._sort)
        except Exception:
            procs = []
        for c in self._list.get_children():
            self._list.remove(c)
        for p in procs:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            name = Gtk.Label(label=p.name, xalign=0.0)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            cpu = Gtk.Label(label=f"{p.cpu_percent:.0f}%", xalign=1.0)
            cpu.set_size_request(70, -1)
            ram = Gtk.Label(label=f"{p.ram_mb:.0f} MB", xalign=1.0)
            ram.set_size_request(90, -1)
            kill = Gtk.Button(label="Kill")
            kill.set_size_request(60, -1)
            kill.set_tooltip_text(f"Terminate {p.name} ({p.process_count} proc)")
            kill.connect("clicked", self._mk_kill(p))
            row.pack_start(name, True, True, 0)
            row.pack_start(cpu, False, False, 0)
            row.pack_start(ram, False, False, 0)
            row.pack_start(kill, False, False, 0)
            self._list.pack_start(row, False, False, 0)
        self._list.show_all()

    def _mk_kill(self, group):
        def on_click(_btn):
            try:
                terminate_group(group.pids)
            except Exception:
                pass
            self.refresh()
        return on_click


# ── Disks ────────────────────────────────────────────────────────────────────

def _fmt_gb(n_bytes):
    return f"{n_bytes/(1024**3):.1f} GB"


class DisksView(_DetailBase):
    def __init__(self):
        super().__init__("Disks")
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self._box)
        self.content.pack_start(scroll, True, True, 0)

    _SKIP_FS = {"squashfs", "tmpfs", "devtmpfs", "overlay", "autofs", "ramfs", ""}

    def refresh(self):
        for c in self._box.get_children():
            self._box.remove(c)
        seen = set()
        try:
            parts = psutil.disk_partitions(all=False)
        except Exception:
            parts = []
        for part in parts:
            # Skip snap/loop pseudo-mounts and virtual filesystems.
            if part.fstype in self._SKIP_FS or part.device.startswith("/dev/loop"):
                continue
            if part.mountpoint in seen:
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            seen.add(part.mountpoint)
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            name = Gtk.Label(xalign=0.0)
            name.set_markup(f"<b>{part.mountpoint}</b>  "
                            f"<small>{part.device} · {part.fstype}</small>")
            name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            pct = Gtk.Label(label=f"{u.percent:.0f}%", xalign=1.0)
            head.pack_start(name, True, True, 0)
            head.pack_end(pct, False, False, 0)
            bar = Gtk.ProgressBar()
            bar.set_fraction(min(u.percent / 100.0, 1.0))
            sub = Gtk.Label(xalign=0.0)
            sub.set_markup(f"<small>{_fmt_gb(u.used)} used · "
                           f"{_fmt_gb(u.free)} free · {_fmt_gb(u.total)} total</small>")
            row.pack_start(head, False, False, 0)
            row.pack_start(bar, False, False, 0)
            row.pack_start(sub, False, False, 0)
            self._box.pack_start(row, False, False, 0)
        if not seen:
            self._box.pack_start(Gtk.Label(label="No disks found."), False, False, 0)
        self._box.show_all()
