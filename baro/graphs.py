"""Matplotlib graph widgets embedded in GTK3."""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
import matplotlib
matplotlib.use("GTK3Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_gtk3agg import FigureCanvasGTK3Agg as FigureCanvas
from matplotlib.figure import Figure
import numpy as np
from collections import deque
import time

DARK_BG = "#1e1e2e"
DARK_AX = "#181825"
GRID_COLOR = "#313244"
COLORS = {
    "cpu": "#89b4fa",
    "cpu_temp": "#f38ba8",
    "gpu": "#a6e3a1",
    "gpu_temp": "#fab387",
    "ram": "#cba6f7",
    "swap": "#89dceb",
}

plt.rcParams.update({
    "figure.facecolor": DARK_BG,
    "axes.facecolor": DARK_AX,
    "axes.edgecolor": GRID_COLOR,
    "axes.labelcolor": "#cdd6f4",
    "xtick.color": "#7f849c",
    "ytick.color": "#7f849c",
    "grid.color": GRID_COLOR,
    "text.color": "#cdd6f4",
    "figure.autolayout": True,
})


class RollingGraph(Gtk.Box):
    """A single graph panel showing one or more rolling time series."""

    def __init__(self, title: str, series_defs: list, y_label: str = "%",
                 y_max: float = 100.0, window_sec: int = 300, height_px: int = 160):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._window_sec = window_sec
        self._y_max = y_max
        self._series_defs = series_defs  # list of (key, label, color)
        self._data = {k: deque(maxlen=3600) for k, *_ in series_defs}
        self._times = deque(maxlen=3600)

        self._fig = Figure(figsize=(5, height_px / 96), dpi=96)
        self._ax = self._fig.add_subplot(111)
        self._ax.set_ylim(0, y_max)
        self._ax.set_ylabel(y_label, fontsize=8)
        self._ax.set_title(title, fontsize=9, pad=3)
        self._ax.grid(True, alpha=0.3)
        self._ax.tick_params(labelsize=7)

        self._lines = {}
        for key, label, color in series_defs:
            (line,) = self._ax.plot([], [], color=color, linewidth=1.2, label=label)
            self._lines[key] = line
        if len(series_defs) > 1:
            self._ax.legend(loc="upper left", fontsize=7, framealpha=0.5)

        canvas = FigureCanvas(self._fig)
        canvas.set_size_request(-1, height_px)
        self.pack_start(canvas, True, True, 0)
        self._canvas = canvas

    def push(self, t: float, values: dict):
        self._times.append(t)
        for key in self._data:
            self._data[key].append(values.get(key, 0.0))

    def redraw(self):
        if len(self._times) < 2:
            return
        times = np.array(self._times)
        now = times[-1]
        cutoff = now - self._window_sec
        mask = times >= cutoff
        t_rel = times[mask] - now  # negative seconds relative to now

        for key, line in self._lines.items():
            vals = np.array(self._data[key])[mask]
            line.set_xdata(t_rel)
            line.set_ydata(vals)

        self._ax.set_xlim(t_rel[0] if len(t_rel) else -self._window_sec, 0)
        self._canvas.draw_idle()

    def set_window(self, sec: int):
        self._window_sec = sec
