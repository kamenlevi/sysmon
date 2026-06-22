"""Live per-core utilization window — like the Task Manager 'logical
processors' grid. Each CPU core gets its own small scrolling graph of its
utilization over time, plus a GPU utilization graph when a GPU is present.

Note: Linux exposes per-core CPU usage, but not per-shader-core GPU usage,
so the GPU is shown as one overall-utilisation graph + VRAM.
"""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

import cairo

from .monitor import SystemStats
from .panel_base import CaretPanel

_COLS = 2          # cores laid out in this many columns
_HIST = 90         # samples kept per graph


class _CoreGraph(Gtk.DrawingArea):
    """A small scrolling area-graph of one value's history (0-100%)."""

    def __init__(self, width=150, height=40):
        super().__init__()
        self._vals = []
        self.set_size_request(width, height)
        self.connect("draw", self._draw)

    def push(self, v):
        self._vals.append(max(0.0, min(float(v), 100.0)))
        if len(self._vals) > _HIST:
            del self._vals[0]
        self.queue_draw()

    def _draw(self, _w, cr):
        a = self.get_allocation()
        w, h = a.width, a.height

        # Background + border
        cr.set_source_rgba(0.97, 0.97, 0.97, 1.0)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        cr.set_source_rgba(0.86, 0.86, 0.86, 1.0)
        cr.set_line_width(1.0)
        cr.rectangle(0.5, 0.5, w - 1, h - 1)
        cr.stroke()

        n = len(self._vals)
        if n < 2:
            return

        def pt(i, v):
            return (i / (n - 1) * w, (h - 1) - (v / 100.0) * (h - 2))

        # Filled area
        cr.move_to(0, h)
        for i, v in enumerate(self._vals):
            cr.line_to(*pt(i, v))
        cr.line_to(w, h)
        cr.close_path()
        cr.set_source_rgba(0.30, 0.45, 0.72, 0.20)
        cr.fill()

        # Line on top
        cr.move_to(*pt(0, self._vals[0]))
        for i, v in enumerate(self._vals):
            cr.line_to(*pt(i, v))
        cr.set_source_rgba(0.24, 0.36, 0.60, 0.95)
        cr.set_line_width(1.3)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.stroke()


class _GraphCell(Gtk.Box):
    """A label ('Core 0   45%') above a scrolling graph."""

    def __init__(self, title):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._title = title
        self.label = Gtk.Label(xalign=0.0)
        self.label.set_markup(f"<small><b>{title}</b></small>")
        self.graph = _CoreGraph()
        self.graph.set_hexpand(True)
        self.pack_start(self.label, False, False, 0)
        self.pack_start(self.graph, True, True, 0)

    def update(self, pct, suffix=""):
        self.graph.push(pct)
        text = f"{self._title}   {pct:.0f}%"
        if suffix:
            text += f"   {suffix}"
        self.label.set_markup(f"<small><b>{text}</b></small>")


class CoresPanel(CaretPanel):
    def __init__(self):
        super().__init__("CPU / GPU cores", show_back=True)
        self.autohide = False
        root = self.body

        self._cpu_header = Gtk.Label(xalign=0)
        self._cpu_header.set_markup("<b>CPU cores</b>")
        root.pack_start(self._cpu_header, False, False, 2)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(320)
        scroll.set_max_content_height(380)
        root.pack_start(scroll, True, True, 0)

        self._cpu_grid = Gtk.Grid()
        self._cpu_grid.set_column_spacing(12)
        self._cpu_grid.set_row_spacing(8)
        self._cpu_grid.set_column_homogeneous(True)
        scroll.add(self._cpu_grid)
        self._core_cells = []

        # GPU
        self._gpu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._gpu_box.set_no_show_all(True)
        root.pack_start(self._gpu_box, False, False, 0)
        gpu_header = Gtk.Label(xalign=0)
        gpu_header.set_markup("<b>GPU</b>")
        self._gpu_box.pack_start(gpu_header, False, False, 0)
        self._gpu_cell = _GraphCell("Usage")
        self._gpu_box.pack_start(self._gpu_cell, False, False, 0)

    def _ensure_cores(self, n):
        if len(self._core_cells) == n:
            return
        for c in self._cpu_grid.get_children():
            self._cpu_grid.remove(c)
        self._core_cells = []
        for i in range(n):
            cell = _GraphCell(f"Core {i}")
            self._cpu_grid.attach(cell, i % _COLS, i // _COLS, 1, 1)
            self._core_cells.append(cell)
        self._cpu_grid.show_all()

    def update(self, s: SystemStats):
        cores = s.cpu_per_core or []
        self._ensure_cores(len(cores))
        self._cpu_header.set_markup(f"<b>CPU — {len(cores)} cores</b>")
        for cell, v in zip(self._core_cells, cores):
            cell.update(v)

        if s.gpu_available:
            self._gpu_box.set_visible(True)
            suffix = ""
            if s.gpu_mem_total_mb > 0:
                suffix = (f"VRAM {s.gpu_mem_used_mb/1024:.1f}/"
                          f"{s.gpu_mem_total_mb/1024:.1f}G")
            if s.gpu_temp > 0:
                suffix += (f"  {s.gpu_temp:.0f}°C" if suffix else f"{s.gpu_temp:.0f}°C")
            self._gpu_cell.update(s.gpu_percent, suffix)
        else:
            self._gpu_box.set_visible(False)

