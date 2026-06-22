"""Drill-in panels (history, processes, disks) — each a caret panel that
looks exactly like the detailed panel and opens in the same spot, with a
← back arrow that returns to the detailed panel.
"""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, Pango

import cairo
import psutil

from .panel_base import CaretPanel, WIDTH
from .processes import collect_top_processes, terminate_group


# ── History ─────────────────────────────────────────────────────────────────

_WINDOWS = [("5 min", 300), ("30 min", 1800), ("1 hour", 3600),
            ("6 hours", 21600), ("24 hours", 86400)]
_SERIES = [("CPU", (0.23, 0.43, 0.65)),
           ("RAM", (0.62, 0.40, 0.66)),
           ("GPU", (0.30, 0.60, 0.38))]


class _MultiGraph(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self._rows = []
        self._has_gpu = False
        self.on_zoom = None
        self.set_size_request(-1, 220)
        self.connect("draw", self._draw)
        self.add_events(Gdk.EventMask.SCROLL_MASK)
        self.connect("scroll-event", self._on_scroll)

    def _on_scroll(self, _w, event):
        if self.on_zoom is None:
            return False
        d = getattr(event, "direction", None)
        if d == Gdk.ScrollDirection.UP:
            self.on_zoom(-1)        # zoom in (shorter window)
        elif d == Gdk.ScrollDirection.DOWN:
            self.on_zoom(+1)        # zoom out (longer window)
        elif d == Gdk.ScrollDirection.SMOOTH:
            ok, _dx, dy = event.get_scroll_deltas()
            if ok and dy:
                self.on_zoom(-1 if dy < 0 else +1)
        return True

    def set_data(self, rows, has_gpu):
        self._rows = rows
        self._has_gpu = has_gpu
        self.queue_draw()

    @staticmethod
    def _gap_threshold(rows):
        # A break (machine off) = a time gap far bigger than the normal spacing.
        if len(rows) < 3:
            return 60.0
        dts = sorted(rows[i][0] - rows[i - 1][0] for i in range(1, len(rows)))
        median = dts[len(dts) // 2] or 1.0
        return max(15.0, 6.0 * median)

    def _draw(self, _w, cr):
        a = self.get_allocation()
        w, h = a.width, a.height
        cr.set_source_rgba(0.99, 0.99, 0.99, 1.0)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        pad_l, pad_r, pad_t, pad_b = 30, 6, 8, 14
        gx, gy = pad_l, pad_t
        gw, gh = w - pad_l - pad_r, h - pad_t - pad_b
        cr.set_line_width(1.0)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(9)
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = gy + gh * frac
            cr.set_source_rgba(0.90, 0.90, 0.90, 1.0)
            cr.move_to(gx, y)
            cr.line_to(gx + gw, y)
            cr.stroke()
            cr.set_source_rgba(0.6, 0.6, 0.6, 1.0)
            cr.move_to(4, y + 3)
            cr.show_text(f"{int(100*(1-frac))}")
        rows = self._rows
        if len(rows) >= 2:
            t0, t1 = rows[0][0], rows[-1][0]
            span = max(1e-6, t1 - t0)
            gap = self._gap_threshold(rows)

            def series(idx, colour):
                cr.set_source_rgba(*colour, 0.95)
                cr.set_line_width(1.6)
                cr.set_line_join(cairo.LINE_JOIN_ROUND)
                started = False
                prev_t = None
                for r in rows:
                    x = gx + (r[0] - t0) / span * gw
                    y = gy + gh * (1.0 - max(0.0, min(r[idx], 100.0)) / 100.0)
                    # Break the line across gaps (machine was off → blank).
                    if not started or (prev_t is not None and r[0] - prev_t > gap):
                        cr.move_to(x, y)
                    else:
                        cr.line_to(x, y)
                    started = True
                    prev_t = r[0]
                cr.stroke()
            series(1, _SERIES[0][1])
            series(2, _SERIES[1][1])
            if self._has_gpu:
                series(3, _SERIES[2][1])
        else:
            cr.set_source_rgba(0.6, 0.6, 0.6, 1.0)
            cr.set_font_size(12)
            cr.move_to(gx + 10, gy + gh / 2)
            cr.show_text("Collecting history…")


class HistoryPanel(CaretPanel):
    def __init__(self, history_db, settings):
        super().__init__("Usage history", show_back=True)
        self.autohide = False
        self.history = history_db
        self.settings = settings
        box = self.body

        ctrl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ctrl.pack_start(Gtk.Label(label="Window:"), False, False, 0)
        self._combo = Gtk.ComboBoxText()
        default = getattr(settings, "history_default_window", 1800)
        active = 1
        for i, (label, secs) in enumerate(_WINDOWS):
            self._combo.append(str(secs), label)
            if secs == default:
                active = i
        self._combo.set_active(active)
        self._combo.connect("changed", lambda *_: self.refresh())
        ctrl.pack_start(self._combo, False, False, 0)
        setd = Gtk.Button(label="Set default")
        setd.connect("clicked", self._set_default)
        ctrl.pack_end(setd, False, False, 0)
        box.pack_start(ctrl, False, False, 4)

        legend = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        self._legend = {}
        for name, colour in _SERIES:
            it = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            sw = Gtk.DrawingArea()
            sw.set_size_request(14, 10)
            sw.connect("draw", self._swatch(colour))
            it.pack_start(sw, False, False, 0)
            lbl = Gtk.Label(label=f"{name} —")
            self._legend[name] = lbl
            it.pack_start(lbl, False, False, 0)
            legend.pack_start(it, False, False, 0)
        box.pack_start(legend, False, False, 2)

        self._graph = _MultiGraph()
        self._graph.on_zoom = self._zoom
        box.pack_start(self._graph, True, True, 0)
        hint = Gtk.Label(xalign=0.0)
        hint.set_markup("<small>Scroll on the graph to zoom the time window "
                        "in / out.</small>")
        box.pack_start(hint, False, False, 0)

    def _zoom(self, direction):
        i = self._combo.get_active()
        n = len(_WINDOWS)
        i = max(0, min(n - 1, i + direction))
        if i != self._combo.get_active():
            self._combo.set_active(i)   # triggers refresh

    @staticmethod
    def _swatch(colour):
        def draw(w, cr):
            a = w.get_allocation()
            cr.set_source_rgba(*colour, 0.95)
            cr.rectangle(0, a.height / 2 - 1.5, a.width, 3)
            cr.fill()
            return False
        return draw

    def _set_default(self, *_):
        self.settings.history_default_window = int(self._combo.get_active_id())
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
        rows = [(r[0], r[1], r[3], r[4]) for r in raw]
        has_gpu = any(r[3] > 0 for r in rows)
        self._graph.set_data(rows, has_gpu)
        if rows:
            last = rows[-1]
            self._legend["CPU"].set_text(f"CPU {last[1]:.0f}%")
            self._legend["RAM"].set_text(f"RAM {last[2]:.0f}%")
            self._legend["GPU"].set_text(f"GPU {last[3]:.0f}%" if has_gpu else "GPU —")


# ── Processes ────────────────────────────────────────────────────────────────

class ProcessesPanel(CaretPanel):
    def __init__(self):
        super().__init__("Processes", show_back=True)
        self.autohide = False
        self._sort = "cpu"
        box = self.body

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.pack_start(Gtk.Label(label="Sort:"), False, False, 0)
        self._sort_combo = Gtk.ComboBoxText()
        self._sort_combo.append("cpu", "CPU")
        self._sort_combo.append("ram", "Memory")
        self._sort_combo.set_active(0)
        self._sort_combo.connect("changed", self._on_sort)
        bar.pack_start(self._sort_combo, False, False, 0)
        box.pack_start(bar, False, False, 4)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(360)
        scroll.set_max_content_height(360)
        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        scroll.add(self._list)
        box.pack_start(scroll, True, True, 0)

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
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            name = Gtk.Label(label=p.name, xalign=0.0)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            cpu = Gtk.Label(label=f"{p.cpu_percent:.0f}%", xalign=1.0)
            cpu.set_size_request(54, -1)
            ram = Gtk.Label(label=f"{p.ram_mb:.0f}MB", xalign=1.0)
            ram.set_size_request(74, -1)
            kill = Gtk.Button(label="✕")
            kill.set_relief(Gtk.ReliefStyle.NONE)
            kill.set_tooltip_text(f"Terminate {p.name}")
            kill.connect("clicked", self._mk_kill(p))
            row.pack_start(name, True, True, 0)
            row.pack_start(cpu, False, False, 0)
            row.pack_start(ram, False, False, 0)
            row.pack_start(kill, False, False, 0)
            self._list.pack_start(row, False, False, 0)
        self._list.show_all()

    def _mk_kill(self, group):
        def on_click(_b):
            try:
                terminate_group(group.pids)
            except Exception:
                pass
            self.refresh()
        return on_click


# ── Disks ────────────────────────────────────────────────────────────────────

def _fmt_gb(n):
    return f"{n/(1024**3):.1f} GB"


class DisksPanel(CaretPanel):
    _SKIP = {"squashfs", "tmpfs", "devtmpfs", "overlay", "autofs", "ramfs", ""}

    def __init__(self):
        super().__init__("Disks", show_back=True)
        self.autohide = False
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(300)
        scroll.set_max_content_height(360)
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scroll.add(self._box)
        self.body.pack_start(scroll, True, True, 0)

    def refresh(self):
        for c in self._box.get_children():
            self._box.remove(c)
        seen = set()
        try:
            parts = psutil.disk_partitions(all=False)
        except Exception:
            parts = []
        for part in parts:
            if part.fstype in self._SKIP or part.device.startswith("/dev/loop"):
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
            nm = Gtk.Label(xalign=0.0)
            nm.set_markup(f"<b>{part.mountpoint}</b>  "
                          f"<small>{part.device} · {part.fstype}</small>")
            nm.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            pct = Gtk.Label(label=f"{u.percent:.0f}%", xalign=1.0)
            head.pack_start(nm, True, True, 0)
            head.pack_end(pct, False, False, 0)
            bar = Gtk.ProgressBar()
            bar.set_fraction(min(u.percent / 100.0, 1.0))
            sub = Gtk.Label(xalign=0.0)
            sub.set_markup(f"<small>{_fmt_gb(u.used)} used · {_fmt_gb(u.free)} "
                           f"free · {_fmt_gb(u.total)} total</small>")
            row.pack_start(head, False, False, 0)
            row.pack_start(bar, False, False, 0)
            row.pack_start(sub, False, False, 0)
            self._box.pack_start(row, False, False, 0)
        if not seen:
            self._box.pack_start(Gtk.Label(label="No disks found."), False, False, 0)
        self._box.show_all()
