"""
Quick-stats popup panel.

Movable (drag header), resizable (drag grip or window edge),
position/size persisted in settings. Content scales with the window —
progress bars expand, labels ellipsise, no scrolling.
Minimum size: 220 × 200 px.
"""
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

from .monitor import SystemStats

# ── Minimum dimensions ────────────────────────────────────────────────────────
MIN_W = 220
MIN_H = 200

# ── CSS ───────────────────────────────────────────────────────────────────────
CSS = b"""
window.sysmon-popup {
    background-color: #1e1e2e;
    border: 1px solid #45475a;
}

/* ---- header / drag handle ---- */
.popup-header {
    background-color: #181825;
    border-bottom: 1px solid #313244;
    padding: 5px 8px;
}
.popup-title {
    color: #cdd6f4;
    font-size: 11px;
    font-weight: bold;
}
.popup-close {
    background: transparent;
    border: none;
    color: #6c7086;
    font-size: 13px;
    padding: 0 2px;
    min-width: 0;
    min-height: 0;
}
.popup-close:hover { color: #cdd6f4; }

/* ---- section titles ---- */
.sec-title {
    color: #a6adc8;
    font-size: 9px;
    font-weight: bold;
    letter-spacing: 0.8px;
}

/* ---- metric values ---- */
.val       { color: #cdd6f4; font-family: monospace; font-size: 11px; }
.val-warn  { color: #cdd6f4; font-family: monospace; font-size: 11px; }
.val-ok    { color: #cdd6f4; font-family: monospace; font-size: 11px; }
.sub       { color: #6c7086; font-size: 9px; }

/* ---- progress bars ---- */
progressbar trough {
    background-color: #313244;
    border-radius: 3px;
    min-height: 7px;
}
progressbar progress {
    border-radius: 3px;
    min-height: 7px;
}
progressbar.bar-cpu progress  { background-color: #7a8394; }
progressbar.bar-gpu progress  { background-color: #7a8394; }
progressbar.bar-ram progress  { background-color: #7a8394; }
progressbar.bar-fan progress  { background-color: #7a8394; }
progressbar.bar-warn progress { background-color: #7a8394; }
progressbar.bar-crit progress { background-color: #7a8394; }

/* ---- warning list ---- */
.warn-text { color: #a6adc8; font-size: 10px; }

/* ---- bottom buttons ---- */
.btn-action {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 5px;
    padding: 3px 10px;
    font-size: 10px;
}
.btn-action:hover { background-color: #45475a; }
.btn-curve {
    background-color: #1e1e2e;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 5px;
    padding: 3px 10px;
    font-size: 10px;
}
.btn-curve:hover { background-color: #313244; }

/* ---- resize grip ---- */
.resize-grip {
    color: #45475a;
    font-size: 13px;
    padding: 0 2px 1px 0;
}

/* ---- separator ---- */
separator { background-color: #313244; min-height: 1px; }

/* ---- fan curve toggle panel ---- */
.curve-panel {
    background-color: #181825;
    border-top: 1px solid #313244;
    padding: 6px;
}
"""

_CSS_APPLIED = [False]


def _apply_css():
    if _CSS_APPLIED[0]:
        return
    p = Gtk.CssProvider()
    p.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _CSS_APPLIED[0] = True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _lbl(text="", css="val", xalign=0.0, ellipsize=False) -> Gtk.Label:
    l = Gtk.Label(label=text, xalign=xalign)
    if css:
        l.get_style_context().add_class(css)
    if ellipsize:
        l.set_ellipsize(Pango.EllipsizeMode.END)
    return l


def _pbar(bar_class="bar-cpu") -> Gtk.ProgressBar:
    pb = Gtk.ProgressBar()
    pb.set_hexpand(True)          # fills available width
    pb.get_style_context().add_class(bar_class)
    return pb


def _update_pbar(pb: Gtk.ProgressBar, pct: float,
                 normal_cls: str, warn_pct=70, crit_pct=90):
    pb.set_fraction(min(pct / 100.0, 1.0))


def _section_row(title: str) -> Gtk.Label:
    lbl = _lbl(title.upper(), "sec-title")
    lbl.set_margin_top(7)
    lbl.set_margin_bottom(1)
    return lbl


# ── Main popup window ─────────────────────────────────────────────────────────

class PopupWindow(Gtk.Window):

    def __init__(self, on_open_app, settings, on_settings=None, on_quit=None):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.on_open_app = on_open_app
        self.settings = settings
        self._on_settings = on_settings
        self._on_quit = on_quit
        self._fan_controller = None
        self._active_fan_key = None
        self._curve_editor = None
        self._enable_switch = None
        self._first_show = True

        _apply_css()

        # Window chrome — behaves like a menu dropdown, not a window
        self.get_style_context().add_class("sysmon-popup")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.POPUP_MENU)
        self.set_size_request(MIN_W, -1)

        # Auto-hide when the user clicks elsewhere, like a real menu
        self.connect("focus-out-event", self._on_focus_out)
        self.connect("key-press-event", self._on_key_press)

        # Root layout
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(root)

        self._content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._content_box.set_margin_start(12)
        self._content_box.set_margin_end(12)
        self._content_box.set_margin_top(10)
        self._content_box.set_margin_bottom(8)
        root.pack_start(self._content_box, True, True, 0)

        self._build_metrics()

        # Fan curve panel (collapsible, sits outside scroll so it can grow)
        self._curve_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._curve_outer.get_style_context().add_class("curve-panel")
        self._curve_outer.set_no_show_all(True)
        root.pack_start(self._curve_outer, False, False, 0)

        root.pack_start(Gtk.Separator(), False, False, 0)
        root.pack_start(self._build_bottom_bar(), False, False, 0)

        self.show_all()
        self.hide()

    def _on_focus_out(self, *_):
        # Ignore the focus flicker caused by the indicator menu closing as the
        # popup opens; only auto-hide once the popup has settled.
        if time.monotonic() - getattr(self, "_shown_at", 0) < 0.4:
            return False
        self.hide()
        return False

    def _on_key_press(self, _w, event):
        if event.keyval == Gdk.KEY_Escape:
            self.hide()
        return False

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> Gtk.Widget:
        hdr_box = Gtk.EventBox()
        hdr_box.get_style_context().add_class("popup-header")

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        inner.set_margin_start(4)
        inner.set_margin_end(4)
        hdr_box.add(inner)

        # Drag handle (whole header area)
        hdr_box.connect("button-press-event", self._on_header_drag)
        hdr_box.connect("enter-notify-event", lambda w, e: (
            w.get_window().set_cursor(
                Gdk.Cursor.new_from_name(self.get_display(), "move")
            ) if w.get_window() else None
        ))
        hdr_box.connect("leave-notify-event", lambda w, e: (
            w.get_window().set_cursor(None) if w.get_window() else None
        ))

        # Warning icon
        self._hdr_warn = _lbl("⚠", "val-warn")
        self._hdr_warn.set_no_show_all(True)
        inner.pack_start(self._hdr_warn, False, False, 0)

        # Title
        title = _lbl("System Monitor", "popup-title")
        inner.pack_start(title, True, True, 0)

        # Close button
        close_btn = Gtk.Button(label="✕")
        close_btn.get_style_context().add_class("popup-close")
        close_btn.set_relief(Gtk.ReliefStyle.NONE)
        close_btn.connect("clicked", lambda *_: self.hide())
        inner.pack_end(close_btn, False, False, 0)

        return hdr_box

    def _on_header_drag(self, widget, event):
        if event.button == 1:
            self.begin_move_drag(
                event.button,
                int(event.x_root),
                int(event.y_root),
                event.time,
            )

    # ── Metrics grid ──────────────────────────────────────────────────────────

    def _build_metrics(self):
        box = self._content_box

        # ── CPU ────────────────────────────────────────────────────────────
        box.pack_start(_section_row("CPU"), False, False, 0)
        self._cpu_grid = self._make_metric_grid()
        box.pack_start(self._cpu_grid, False, False, 0)

        self._cpu_pbar = _pbar("bar-cpu")
        self._cpu_pct  = _lbl("", "val", xalign=1.0)
        self._cpu_temp = _lbl("", "val-ok", xalign=1.0)
        self._cpu_sub  = _lbl("", "sub")
        self._cpu_sub.set_ellipsize(Pango.EllipsizeMode.END)

        self._attach_metric(self._cpu_grid,
                            icon="", bar=self._cpu_pbar,
                            val=self._cpu_pct, extra=self._cpu_temp)
        self._cpu_grid.attach(self._cpu_sub, 0, 1, 4, 1)

        # ── GPU ────────────────────────────────────────────────────────────
        self._gpu_section = _section_row("GPU")
        self._gpu_section.set_no_show_all(True)
        box.pack_start(self._gpu_section, False, False, 0)

        self._gpu_grid = self._make_metric_grid()
        self._gpu_grid.set_no_show_all(True)
        box.pack_start(self._gpu_grid, False, False, 0)

        self._gpu_pbar = _pbar("bar-gpu")
        self._gpu_pct  = _lbl("", "val", xalign=1.0)
        self._gpu_temp = _lbl("", "val-ok", xalign=1.0)
        self._gpu_sub  = _lbl("", "sub")
        self._gpu_sub.set_ellipsize(Pango.EllipsizeMode.END)

        self._attach_metric(self._gpu_grid,
                            icon="", bar=self._gpu_pbar,
                            val=self._gpu_pct, extra=self._gpu_temp)
        self._gpu_grid.attach(self._gpu_sub, 0, 1, 4, 1)

        # ── RAM ────────────────────────────────────────────────────────────
        box.pack_start(_section_row("Memory"), False, False, 0)
        self._ram_grid = self._make_metric_grid()
        box.pack_start(self._ram_grid, False, False, 0)

        self._ram_pbar = _pbar("bar-ram")
        self._ram_pct  = _lbl("", "val", xalign=1.0)
        self._ram_sub  = _lbl("", "sub")
        self._ram_sub.set_ellipsize(Pango.EllipsizeMode.END)

        self._attach_metric(self._ram_grid,
                            icon="", bar=self._ram_pbar, val=self._ram_pct)
        self._ram_grid.attach(self._ram_sub, 0, 1, 4, 1)

        # ── Fans ───────────────────────────────────────────────────────────
        self._fan_section = _section_row("Fans")
        self._fan_section.set_no_show_all(True)
        box.pack_start(self._fan_section, False, False, 0)

        self._fan_rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self._fan_rows_box.set_no_show_all(True)
        box.pack_start(self._fan_rows_box, False, False, 0)

        # ── Warnings ───────────────────────────────────────────────────────
        self._warn_sep = Gtk.Separator()
        self._warn_sep.set_margin_top(6)
        self._warn_sep.set_no_show_all(True)
        box.pack_start(self._warn_sep, False, False, 0)

        self._warn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._warn_box.set_margin_top(3)
        self._warn_box.set_no_show_all(True)
        box.pack_start(self._warn_box, False, False, 0)

    def _make_metric_grid(self) -> Gtk.Grid:
        g = Gtk.Grid()
        g.set_column_spacing(6)
        g.set_row_spacing(2)
        g.set_margin_bottom(2)
        return g

    def _attach_metric(self, grid, icon, bar, val, extra=None):
        """
        Row 0: [progress bar (hexpand)] [val lbl] [extra lbl]
        """
        grid.attach(bar, 1, 0, 1, 1)
        val.set_size_request(50, -1)
        grid.attach(val, 2, 0, 1, 1)
        if extra:
            extra.set_size_request(46, -1)
            grid.attach(extra, 3, 0, 1, 1)

    # ── Bottom bar ────────────────────────────────────────────────────────────

    def _build_bottom_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_margin_start(8)
        bar.set_margin_end(8)
        bar.set_margin_top(5)
        bar.set_margin_bottom(5)

        self._curve_toggle = Gtk.ToggleButton(label="Fan Curve ▾")
        self._curve_toggle.get_style_context().add_class("btn-curve")
        self._curve_toggle.set_no_show_all(True)
        self._curve_toggle.connect("toggled", self._on_curve_toggle)
        bar.pack_start(self._curve_toggle, False, False, 0)

        if self._on_quit:
            quit_btn = Gtk.Button(label="Quit")
            quit_btn.get_style_context().add_class("btn-action")
            quit_btn.connect("clicked", lambda *_: self._on_quit())
            bar.pack_end(quit_btn, False, False, 0)

        open_btn = Gtk.Button(label="Full Monitor")
        open_btn.get_style_context().add_class("btn-action")
        open_btn.connect("clicked", lambda *_: (self.on_open_app(), None)[1])
        bar.pack_end(open_btn, False, False, 0)

        if self._on_settings:
            settings_btn = Gtk.Button(label="Settings")
            settings_btn.get_style_context().add_class("btn-action")
            settings_btn.connect("clicked", lambda *_: self._on_settings())
            bar.pack_end(settings_btn, False, False, 0)

        return bar

    # ── Resize grip ───────────────────────────────────────────────────────────

    def _build_grip_row(self) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row.pack_start(Gtk.Box(), True, True, 0)   # spacer

        grip = Gtk.EventBox()
        grip_lbl = _lbl("⊡", "resize-grip", xalign=1.0)
        grip.add(grip_lbl)
        grip.set_size_request(22, 14)

        grip.connect("enter-notify-event", lambda w, e: (
            w.get_window().set_cursor(
                Gdk.Cursor.new_from_name(self.get_display(), "se-resize")
            ) if w.get_window() else None
        ))
        grip.connect("leave-notify-event", lambda w, e: (
            w.get_window().set_cursor(None) if w.get_window() else None
        ))
        grip.connect("button-press-event", self._on_grip_drag)
        grip.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )

        row.pack_end(grip, False, False, 0)
        return row

    def _on_grip_drag(self, widget, event):
        if event.button == 1:
            self.begin_resize_drag(
                Gdk.WindowEdge.SOUTH_EAST,
                event.button,
                int(event.x_root),
                int(event.y_root),
                event.time,
            )

    # ── Position / size persistence ───────────────────────────────────────────

    def _on_first_map(self, *_):
        if self._first_show:
            self._first_show = False
            x, y = self.settings.popup_x, self.settings.popup_y
            if x >= 0 and y >= 0:
                self.move(x, y)
            else:
                self._default_position()

    def _default_position(self):
        screen = Gdk.Screen.get_default()
        w, _ = self.get_size()
        self.move(screen.get_width() - w - 8, 40)

    def _on_configure(self, win, event):
        x, y = self.get_position()
        w, h = self.get_size()
        # Clamp to minimum before saving
        if w < MIN_W or h < MIN_H:
            return
        s = self.settings
        s.popup_x = x
        s.popup_y = y
        s.popup_w = w
        s.popup_h = h
        # Debounce: configure-event fires for every pixel of a drag/resize.
        # Coalesce into one disk write 400ms after the gesture stops.
        if getattr(self, "_save_handle", 0):
            GLib.source_remove(self._save_handle)
        self._save_handle = GLib.timeout_add(400, self._flush_settings)

    def _flush_settings(self):
        self._save_handle = 0
        try:
            self.settings.save()
        except Exception:
            pass
        return False  # one-shot

    # ── Data update ───────────────────────────────────────────────────────────

    def update(self, s: SystemStats):
        cfg = self.settings

        # CPU
        if cfg.show_cpu:
            _update_pbar(self._cpu_pbar, s.cpu_percent, "bar-cpu")
            self._cpu_pct.set_text(f"{s.cpu_percent:5.1f}%")

            if cfg.show_temp and s.cpu_temp > 0:
                cls = "val-warn" if s.cpu_temp > cfg.warn_cpu_temp else "val-ok"
                _set_css_classes(self._cpu_temp, ["val-warn", "val-ok"], cls)
                self._cpu_temp.set_text(f"{s.cpu_temp:.0f}°C")
            else:
                self._cpu_temp.set_text("")

            throttle = " (throttling)" if s.thermal_throttling else ""
            freq = f"{s.cpu_freq_mhz:.0f}/{s.cpu_freq_max_mhz:.0f}MHz{throttle}" \
                   if s.cpu_freq_mhz > 0 else ""
            self._cpu_sub.set_text(freq)

        # GPU
        if cfg.show_gpu and s.gpu_available:
            self._gpu_section.set_visible(True)
            self._gpu_grid.set_visible(True)
            _update_pbar(self._gpu_pbar, s.gpu_percent, "bar-gpu")
            self._gpu_pct.set_text(f"{s.gpu_percent:5.1f}%")
            if cfg.show_temp and s.gpu_temp > 0:
                cls = "val-warn" if s.gpu_temp > cfg.warn_gpu_temp else "val-ok"
                _set_css_classes(self._gpu_temp, ["val-warn", "val-ok"], cls)
                self._gpu_temp.set_text(f"{s.gpu_temp:.0f}°C")
            vram = ""
            if s.gpu_mem_total_mb > 0:
                vram = (f"VRAM {s.gpu_mem_used_mb/1024:.1f}/"
                        f"{s.gpu_mem_total_mb/1024:.1f}GB")
            if s.gpu_power_w > 0:
                vram += f"  {s.gpu_power_w:.0f}W"
            self._gpu_sub.set_text(vram)
        else:
            self._gpu_section.set_visible(False)
            self._gpu_grid.set_visible(False)

        # RAM
        if cfg.show_ram:
            _update_pbar(self._ram_pbar, s.ram_percent, "bar-ram")
            self._ram_pct.set_text(f"{s.ram_percent:5.1f}%")
            detail = f"{s.ram_used_gb:.1f}/{s.ram_total_gb:.1f}GB"
            if s.swap_total_gb > 0:
                detail += f"  swap {s.swap_used_gb:.1f}/{s.swap_total_gb:.1f}GB"
            self._ram_sub.set_text(detail)

        # Fans
        self._update_fan_rpms(s.fans)

        # Warnings
        for c in self._warn_box.get_children():
            self._warn_box.remove(c)
        if s.warnings:
            self._warn_sep.set_visible(True)
            self._warn_box.set_visible(True)
            for w in s.warnings:
                lbl = Gtk.Label(label=f"!  {w}", xalign=0.0)
                lbl.get_style_context().add_class("warn-text")
                lbl.set_ellipsize(Pango.EllipsizeMode.END)
                self._warn_box.pack_start(lbl, False, False, 0)
                lbl.show()
        else:
            self._warn_sep.set_visible(False)
            self._warn_box.set_visible(False)

    def _update_fan_rpms(self, fans):
        if not fans:
            self._fan_section.set_visible(False)
            self._fan_rows_box.set_visible(False)
            self._curve_toggle.set_visible(False)
            return

        self._fan_section.set_visible(True)
        self._fan_rows_box.set_visible(True)

        existing = self._fan_rows_box.get_children()
        if len(existing) != len(fans):
            for c in existing:
                self._fan_rows_box.remove(c)
            for label, rpm, controllable in fans:
                row = _FanRpmRow(label, rpm)
                self._fan_rows_box.pack_start(row, False, False, 0)
                row.show_all()
        else:
            for row, (label, rpm, _) in zip(existing, fans):
                if isinstance(row, _FanRpmRow):
                    row.set_rpm(rpm)

        if self._fan_controller is not None:
            self._curve_toggle.set_visible(True)

    # ── Fan curve toggle ──────────────────────────────────────────────────────

    def _on_curve_toggle(self, btn):
        if btn.get_active():
            btn.set_label("Fan Curve ▴")
            self._build_curve_panel()
            self._curve_outer.set_visible(True)
            self._curve_outer.show_all()
        else:
            btn.set_label("Fan Curve ▾")
            self._curve_outer.set_visible(False)

    def _build_curve_panel(self):
        for c in self._curve_outer.get_children():
            self._curve_outer.remove(c)

        from .fan_curve_widget import FanCurveEditor
        from .fans import DEFAULT_CURVE, check_pwm_writable

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        inner.set_margin_start(6)
        inner.set_margin_end(6)
        inner.set_margin_top(4)
        inner.set_margin_bottom(4)
        self._curve_outer.pack_start(inner, True, True, 0)

        if not self._fan_controller:
            inner.pack_start(_lbl("Fan controller not available.", "sub"), False, False, 0)
            return

        fan_keys = list(self._fan_controller._fans.keys())

        # Fan selector if more than one
        if len(fan_keys) > 1:
            sel_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            sel_row.pack_start(_lbl("Fan:", "sub"), False, False, 0)
            fan_sel = Gtk.ComboBoxText()
            for k in fan_keys:
                fan_sel.append(k, self._fan_controller._fans[k].label)
            fan_sel.set_active(0)
            fan_sel.connect("changed", self._on_fan_select)
            sel_row.pack_start(fan_sel, True, True, 0)
            inner.pack_start(sel_row, False, False, 0)

        if not self._active_fan_key and fan_keys:
            self._active_fan_key = fan_keys[0]

        # Curve editor
        initial = DEFAULT_CURVE
        if self._active_fan_key:
            fan = self._fan_controller._fans.get(self._active_fan_key)
            if fan:
                initial = fan.curve

        editor = FanCurveEditor(points=list(initial), compact=True)
        editor.set_hexpand(True)
        editor.connect("curve-changed", self._on_curve_changed)
        self._curve_editor = editor
        inner.pack_start(editor, True, True, 0)

        # Controls
        ctrl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ctrl.set_margin_top(2)

        sw_lbl = _lbl("Apply curve", "sub")
        en_sw = Gtk.Switch()
        en_sw.set_valign(Gtk.Align.CENTER)
        if self._active_fan_key:
            en_sw.set_active(self._fan_controller.is_active(self._active_fan_key))
        en_sw.connect("notify::active", self._on_enable_toggle)
        self._enable_switch = en_sw

        reset_btn = Gtk.Button(label="Auto")
        reset_btn.get_style_context().add_class("btn-curve")
        reset_btn.set_tooltip_text("Return fan to BIOS automatic control")
        reset_btn.connect("clicked", self._on_reset_auto)

        ctrl.pack_start(sw_lbl, False, False, 0)
        ctrl.pack_start(en_sw, False, False, 0)
        ctrl.pack_end(reset_btn, False, False, 0)
        inner.pack_start(ctrl, False, False, 0)

        if not check_pwm_writable():
            w = _lbl("⚠ PWM not writable — run install.sh for udev rule", "warn-text")
            w.set_ellipsize(Pango.EllipsizeMode.END)
            inner.pack_start(w, False, False, 0)

    def _on_fan_select(self, combo):
        self._active_fan_key = combo.get_active_id()
        if self._curve_editor and self._fan_controller and self._active_fan_key:
            fan = self._fan_controller._fans.get(self._active_fan_key)
            if fan:
                self._curve_editor.set_points(list(fan.curve))

    def _on_curve_changed(self, editor, points):
        if self._fan_controller and self._active_fan_key:
            self._fan_controller.update_curve(self._active_fan_key, points)

    def _on_enable_toggle(self, sw, _):
        if self._fan_controller and self._active_fan_key:
            self._fan_controller.set_curve_active(self._active_fan_key, sw.get_active())

    def _on_reset_auto(self, _):
        if self._fan_controller and self._active_fan_key:
            self._fan_controller.set_curve_active(self._active_fan_key, False)
            if self._enable_switch:
                self._enable_switch.set_active(False)

    # ── Show ──────────────────────────────────────────────────────────────────

    def show_near_top_right(self):
        """Toggle: show right under the panel icon, or hide if already visible."""
        if self.get_visible():
            self.hide()
            return
        self._shown_at = time.monotonic()
        self._position_under_cursor()
        self.show_all()
        self.present()
        self._position_under_cursor()
        self.grab_focus()

    def _position_under_cursor(self):
        display = Gdk.Display.get_default()
        seat = display.get_default_seat()
        pointer = seat.get_pointer()
        _, cursor_x, cursor_y = pointer.get_position()
        w, _h = self.get_size()
        screen = Gdk.Screen.get_default()
        screen_w = screen.get_width()
        # Drop straight down from the icon, kept on-screen horizontally.
        x = max(4, min(cursor_x - w // 2, screen_w - w - 4))
        y = 32
        self.move(x, y)


# ── Fan RPM row widget ────────────────────────────────────────────────────────

class _FanRpmRow(Gtk.Box):
    _MAX_RPM = 5000

    def __init__(self, label: str, rpm: int):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._label_str = label

        self._name_lbl = _lbl(label, "sub")
        self._name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self._name_lbl.set_size_request(60, -1)
        self.pack_start(self._name_lbl, False, False, 0)

        self._pb = Gtk.ProgressBar()
        self._pb.set_hexpand(True)
        self._pb.get_style_context().add_class("bar-fan")
        self.pack_start(self._pb, True, True, 0)

        self._rpm_lbl = _lbl(f"{rpm} RPM", "val", xalign=1.0)
        self._rpm_lbl.set_size_request(72, -1)
        self.pack_start(self._rpm_lbl, False, False, 0)

        self.set_rpm(rpm)

    def set_rpm(self, rpm: int):
        self._rpm_lbl.set_text(f"{rpm} RPM")
        self._pb.set_fraction(min(rpm / self._MAX_RPM, 1.0))


# ── Utility ───────────────────────────────────────────────────────────────────

def _set_css_classes(widget, remove: list, add: str):
    ctx = widget.get_style_context()
    for c in remove:
        ctx.remove_class(c)
    ctx.add_class(add)
