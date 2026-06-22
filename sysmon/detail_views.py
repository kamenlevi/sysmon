"""Drill-in views as embeddable Gtk.Box widgets (shown as Stack pages in the
panel, so switching to them is instant — no window map/unmap).

  HistoryView   — CPU/RAM/GPU over a selectable window (hover readout, scroll zoom)
  ProcessesView — full process list (updates in place) with kill
  DisksView     — usage of every mounted disk
"""
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, Pango

import cairo
import psutil

from .processes import collect_top_processes, terminate_group


_WINDOWS = [("5 min", 300), ("30 min", 1800), ("1 hour", 3600),
            ("6 hours", 21600), ("24 hours", 86400)]


def _segmented(box, options, active_value, on_select):
    """A row of grouped toggle buttons (no popup → safe on the panel).

    options: list of (label, value). Returns the list of buttons; each has a
    `_value` attr. on_select(value) is called when the selection changes.
    """
    btns = []
    group = None
    for label, value in options:
        rb = Gtk.RadioButton.new_with_label_from_widget(group, label)
        if group is None:
            group = rb
        rb.set_mode(False)            # render as a button, not a radio dot
        rb._value = value
        rb.connect("toggled", lambda b: b.get_active() and on_select(b._value))
        box.pack_start(rb, False, False, 0)
        btns.append(rb)
    for rb in btns:
        if rb._value == active_value:
            rb.set_active(True)
            break
    return btns
_SERIES = [("CPU", (0.23, 0.43, 0.65)),
           ("RAM", (0.62, 0.40, 0.66)),
           ("GPU", (0.30, 0.60, 0.38))]


class _MultiGraph(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self._rows = []
        self._has_gpu = False
        self.on_zoom = None
        self._mx = None
        self.set_size_request(-1, 220)
        self.connect("draw", self._draw)
        self.add_events(Gdk.EventMask.SCROLL_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK
                        | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("scroll-event", self._on_scroll)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("leave-notify-event", self._on_leave)

    def _on_scroll(self, _w, event):
        if self.on_zoom is None:
            return False
        d = getattr(event, "direction", None)
        if d == Gdk.ScrollDirection.UP:
            self.on_zoom(-1)
        elif d == Gdk.ScrollDirection.DOWN:
            self.on_zoom(+1)
        elif d == Gdk.ScrollDirection.SMOOTH:
            ok, _dx, dy = event.get_scroll_deltas()
            if ok and dy:
                self.on_zoom(-1 if dy < 0 else +1)
        return True

    def _on_motion(self, _w, event):
        self._mx = event.x
        self.queue_draw()
        return False

    def _on_leave(self, *_):
        self._mx = None
        self.queue_draw()
        return False

    def set_data(self, rows, has_gpu):
        self._rows = rows
        self._has_gpu = has_gpu
        self.queue_draw()

    @staticmethod
    def _gap_threshold(rows):
        if len(rows) < 3:
            return 60.0
        dts = sorted(rows[i][0] - rows[i - 1][0] for i in range(1, len(rows)))
        return max(15.0, 6.0 * (dts[len(dts) // 2] or 1.0))

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
        if len(rows) < 2:
            cr.set_source_rgba(0.6, 0.6, 0.6, 1.0)
            cr.set_font_size(12)
            cr.move_to(gx + 10, gy + gh / 2)
            cr.show_text("Collecting history…")
            return

        t0, t1 = rows[0][0], rows[-1][0]
        span = max(1e-6, t1 - t0)
        gap = self._gap_threshold(rows)

        def series(idx, colour):
            cr.set_source_rgba(*colour, 0.95)
            cr.set_line_width(1.6)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            prev_t = None
            for r in rows:
                x = gx + (r[0] - t0) / span * gw
                y = gy + gh * (1.0 - max(0.0, min(r[idx], 100.0)) / 100.0)
                if prev_t is None or r[0] - prev_t > gap:
                    cr.move_to(x, y)
                else:
                    cr.line_to(x, y)
                prev_t = r[0]
            cr.stroke()
        series(1, _SERIES[0][1])
        series(2, _SERIES[1][1])
        if self._has_gpu:
            series(3, _SERIES[2][1])

        if self._mx is not None and gx <= self._mx <= gx + gw:
            tt = t0 + (self._mx - gx) / gw * span
            row = min(rows, key=lambda r: abs(r[0] - tt))
            hx = gx + (row[0] - t0) / span * gw
            cr.set_source_rgba(0.4, 0.4, 0.4, 0.7)
            cr.set_line_width(1.0)
            cr.move_to(hx, gy)
            cr.line_to(hx, gy + gh)
            cr.stroke()
            vals = [("CPU", row[1]), ("RAM", row[2])]
            if self._has_gpu:
                vals.append(("GPU", row[3]))
            for (name, v), (_n, colour) in zip(vals, _SERIES):
                yy = gy + gh * (1.0 - max(0.0, min(v, 100.0)) / 100.0)
                cr.set_source_rgba(*colour, 1.0)
                cr.arc(hx, yy, 2.5, 0, 6.2832)
                cr.fill()
            when = time.strftime("%H:%M:%S", time.localtime(row[0]))
            lines = [when] + [f"{n}: {v:.0f}%" for n, v in vals]
            cr.set_font_size(10)
            bw = max(cr.text_extents(t).width for t in lines) + 10
            bh = 13 * len(lines) + 6
            bx = min(hx + 8, gx + gw - bw)
            by = gy + 4
            cr.set_source_rgba(1, 1, 1, 0.92)
            cr.rectangle(bx, by, bw, bh)
            cr.fill()
            cr.set_source_rgba(0.8, 0.8, 0.8, 1)
            cr.rectangle(bx + 0.5, by + 0.5, bw - 1, bh - 1)
            cr.stroke()
            cr.set_source_rgba(0.15, 0.15, 0.15, 1)
            for i, t in enumerate(lines):
                cr.move_to(bx + 5, by + 12 + i * 13)
                cr.show_text(t)


class HistoryView(Gtk.Box):
    title = "Usage history"

    def __init__(self, history_db, settings):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.history = history_db
        self.settings = settings

        ctrl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        default = getattr(settings, "history_default_window", 1800)
        self._win_btns = _segmented(
            ctrl, [(l, s) for (l, s) in _WINDOWS], default, self._on_win)
        setd = Gtk.Button(label="Set default")
        setd.connect("clicked", self._set_default)
        ctrl.pack_end(setd, False, False, 0)
        self.pack_start(ctrl, False, False, 4)

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
        self.pack_start(legend, False, False, 2)

        self._graph = _MultiGraph()
        self._graph.on_zoom = self._zoom
        self.pack_start(self._graph, True, True, 0)
        hint = Gtk.Label(xalign=0.0)
        hint.set_markup("<small>Scroll to zoom · hover for values.</small>")
        self.pack_start(hint, False, False, 0)

    @staticmethod
    def _swatch(colour):
        def draw(w, cr):
            a = w.get_allocation()
            cr.set_source_rgba(*colour, 0.95)
            cr.rectangle(0, a.height / 2 - 1.5, a.width, 3)
            cr.fill()
            return False
        return draw

    def _on_win(self, _secs):
        self.refresh()

    def _active_idx(self):
        for i, b in enumerate(self._win_btns):
            if b.get_active():
                return i
        return 0

    def _win_secs(self):
        return self._win_btns[self._active_idx()]._value

    def _zoom(self, direction):
        i = max(0, min(len(self._win_btns) - 1, self._active_idx() + direction))
        self._win_btns[i].set_active(True)

    def _set_default(self, *_):
        self.settings.history_default_window = int(self._win_secs())
        try:
            self.settings.save()
        except Exception:
            pass

    def refresh(self):
        secs = int(self._win_secs() or 1800)
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


class ProcessesView(Gtk.Box):
    title = "Processes"
    _ROWS = 30

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._sort = "cpu"

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        bar.pack_start(Gtk.Label(label="Sort: "), False, False, 0)
        _segmented(bar, [("CPU", "cpu"), ("Memory", "ram")], "cpu", self._on_sort)
        self.pack_start(bar, False, False, 4)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(360)
        scroll.set_max_content_height(360)
        lst = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        scroll.add(lst)
        self.pack_start(scroll, True, True, 0)

        # Fixed pool of rows, updated in place (never rebuilt → no scroll crash).
        self._rows = []
        for _ in range(self._ROWS):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            name = Gtk.Label(xalign=0.0)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            cpu = Gtk.Label(xalign=1.0)
            cpu.set_size_request(54, -1)
            ram = Gtk.Label(xalign=1.0)
            ram.set_size_request(74, -1)
            kill = Gtk.Button(label="✕")
            kill.set_relief(Gtk.ReliefStyle.NONE)
            row.pack_start(name, True, True, 0)
            row.pack_start(cpu, False, False, 0)
            row.pack_start(ram, False, False, 0)
            row.pack_start(kill, False, False, 0)
            row._pids = []
            kill.connect("clicked", self._mk_kill(row))
            lst.pack_start(row, False, False, 0)
            row.show_all()
            row.set_no_show_all(True)
            row.hide()
            self._rows.append((row, name, cpu, ram))

    def _on_sort(self, value):
        self._sort = value
        self.refresh()

    def refresh(self):
        try:
            procs = collect_top_processes(self._ROWS, sort_by=self._sort)
        except Exception:
            procs = []
        for i, (row, name, cpu, ram) in enumerate(self._rows):
            if i < len(procs):
                p = procs[i]
                row._pids = list(p.pids)
                name.set_text(p.name)
                cpu.set_text(f"{p.cpu_percent:.0f}%")
                ram.set_text(f"{p.ram_mb:.0f}MB")
                row.set_visible(True)
            else:
                row.set_visible(False)

    def _mk_kill(self, row):
        def on_click(_b):
            try:
                terminate_group(row._pids)
            except Exception:
                pass
            self.refresh()
        return on_click


def _fmt_gb(n):
    return f"{n/(1024**3):.1f} GB"


class DisksView(Gtk.Box):
    title = "Disks"
    _SKIP = {"squashfs", "tmpfs", "devtmpfs", "overlay", "autofs", "ramfs", ""}

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(300)
        scroll.set_max_content_height(360)
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scroll.add(self._box)
        self.pack_start(scroll, True, True, 0)

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
