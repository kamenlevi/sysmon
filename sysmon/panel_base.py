"""Shared caret-dropdown panel base.

Every panel (the detailed overview and each drill-in: history, processes,
disks, cores) is one of these, so they all look identical — same white
rounded body, same caret, same fixed width — and the app positions them
all in the same spot under the icon.
"""
import math
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib

import cairo

WIDTH = 340
CARET_H = 9
CARET_W = 18

_CSS = b"""
window.sysmon-panel { background-color: transparent; }
.panel-title { color: #1a1a1a; font-size: 12px; font-weight: bold; }
.close-btn, .back-btn {
    background: transparent; border: none; color: #888888;
    font-size: 15px; padding: 0 6px; min-width: 0; min-height: 0;
}
.close-btn:hover { color: #b04a3a; }
.back-btn { color: #2a2a2a; font-size: 18px; min-width: 30px; min-height: 26px; padding: 0 8px; }
.back-btn:hover { color: #000000; background-color: #ececec; border-radius: 6px; }
"""

_CSS_APPLIED = [False]


def apply_css():
    if _CSS_APPLIED[0]:
        return
    p = Gtk.CssProvider()
    p.load_from_data(_CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _CSS_APPLIED[0] = True


class CaretPanel(Gtk.Window):
    def __init__(self, title, show_back=False):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.autohide = True
        self.on_back = None
        self._shown_at = 0.0
        self._caret_x = WIDTH / 2.0

        apply_css()
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.set_app_paintable(True)
        self.connect("draw", self._draw_bg)

        self.get_style_context().add_class("sysmon-panel")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        # UTILITY (not POPUP_MENU) so the WM gives the window input focus —
        # otherwise buttons need a focusing click first (felt unresponsive).
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_accept_focus(True)
        self.set_size_request(WIDTH, -1)

        self.connect("focus-out-event", self._on_focus_out)
        self.connect("key-press-event", self._on_key_press)
        self.connect("configure-event", self._pin_width)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.set_margin_start(16)
        root.set_margin_end(16)
        root.set_margin_top(CARET_H + 8)
        root.set_margin_bottom(12)
        self.add(root)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.back_btn = Gtk.Button(label="←")
        self.back_btn.get_style_context().add_class("back-btn")
        self.back_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.back_btn.set_tooltip_text("Back to panel")
        self.back_btn.set_no_show_all(True)
        self.back_btn.connect("clicked", lambda *_: self._do_back())
        self.back_btn.set_visible(show_back)
        header.pack_start(self.back_btn, False, False, 0)
        self.title_lbl = Gtk.Label(xalign=0.0)
        self.title_lbl.set_markup(f"<b>{title}</b>")
        self.title_lbl.get_style_context().add_class("panel-title")
        header.pack_start(self.title_lbl, True, True, 0)
        close = Gtk.Button(label="✕")
        close.get_style_context().add_class("close-btn")
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.connect("clicked", lambda *_: self.hide())
        header.pack_end(close, False, False, 0)
        root.pack_start(header, False, False, 0)
        root.pack_start(Gtk.Separator(), False, False, 4)

        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.pack_start(self.body, True, True, 0)

    # ── caret + body ────────────────────────────────────────────────────
    def _draw_bg(self, _w, cr):
        a = self.get_allocation()
        w, h = a.width, a.height
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        r = 12
        top = CARET_H
        cx = max(CARET_W, min(self._caret_x, w - CARET_W))
        cr.new_sub_path()
        cr.arc(w - r, top + r, r, -math.pi / 2, 0)
        cr.arc(w - r, h - r, r, 0, math.pi / 2)
        cr.arc(r, h - r, r, math.pi / 2, math.pi)
        cr.arc(r, top + r, r, math.pi, 1.5 * math.pi)
        cr.close_path()
        cr.move_to(cx - CARET_W / 2, top)
        cr.line_to(cx, 0)
        cr.line_to(cx + CARET_W / 2, top)
        cr.close_path()
        cr.set_source_rgba(1.0, 1.0, 1.0, 1.0)
        cr.fill_preserve()
        cr.set_source_rgba(0.82, 0.82, 0.82, 1.0)
        cr.set_line_width(1.0)
        cr.stroke()
        return False

    # ── behaviour ───────────────────────────────────────────────────────
    def _on_focus_out(self, *_):
        if not self.autohide:
            return False
        if time.monotonic() - self._shown_at < 0.6:
            return False
        self.hide()
        return False

    def _on_key_press(self, _w, event):
        if event.keyval == Gdk.KEY_Escape:
            self.hide()
        return False

    def _pin_width(self, _w, _e):
        width, height = self.get_size()
        if width != WIDTH:
            self.resize(WIDTH, height)
        return False

    def _do_back(self):
        self.hide()
        if self.on_back:
            self.on_back()

    def _settle(self):
        if self.get_visible():
            self.present()
            self.grab_focus()
        return False

    # ── show ────────────────────────────────────────────────────────────
    def show_at(self, x, caret_x):
        self._shown_at = time.monotonic()
        self.show_all()
        self._caret_x = caret_x
        self.move(x, 30)
        self.present()
        self.grab_focus()
        self.queue_draw()
        GLib.timeout_add(300, self._settle)

    @staticmethod
    def cursor_geometry():
        """Return (x, caret_x) placing a WIDTH-wide panel under the cursor."""
        display = Gdk.Display.get_default()
        seat = display.get_default_seat()
        _, cx, _cy = seat.get_pointer().get_position()
        screen_w = Gdk.Screen.get_default().get_width()
        x = max(4, min(cx - WIDTH // 2, screen_w - WIDTH - 4))
        caret_x = max(CARET_W, min(cx - x, WIDTH - CARET_W))
        return x, caret_x
