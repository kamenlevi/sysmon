"""Process monitor tab widget."""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

from .processes import ProcessGroup, collect_top_processes, terminate_group, kill_group

CSS = b"""
.proc-header {
    background-color: #181825;
    color: #7f849c;
    font-size: 10px;
    font-weight: bold;
    letter-spacing: 0.5px;
    padding: 4px 8px;
    border-bottom: 1px solid #313244;
}
.proc-row {
    background-color: #1e1e2e;
    border-bottom: 1px solid #2a2a3e;
    padding: 3px 0;
}
.proc-row:hover {
    background-color: #262637;
}
.proc-row.high-cpu {
    background-color: #2e1a1a;
}
.proc-row.high-ram {
    background-color: #1a1e2e;
}
.proc-name {
    color: #cdd6f4;
    font-size: 12px;
    font-weight: bold;
}
.proc-desc {
    color: #6c7086;
    font-size: 10px;
    font-style: italic;
}
.proc-cpu-high { color: #f38ba8; font-family: monospace; font-size: 11px; font-weight: bold; }
.proc-cpu-med  { color: #fab387; font-family: monospace; font-size: 11px; }
.proc-cpu-ok   { color: #a6e3a1; font-family: monospace; font-size: 11px; }
.proc-ram      { color: #cba6f7; font-family: monospace; font-size: 11px; }
.proc-pid      { color: #6c7086; font-family: monospace; font-size: 10px; }
button.btn-term {
    background-color: #313244;
    color: #fab387;
    border-radius: 4px;
    border: 1px solid #45475a;
    padding: 1px 6px;
    font-size: 10px;
    min-height: 0;
}
button.btn-term:hover { background-color: #45475a; }
button.btn-kill {
    background-color: #3a1a1a;
    color: #f38ba8;
    border-radius: 4px;
    border: 1px solid #5c2626;
    padding: 1px 6px;
    font-size: 10px;
    min-height: 0;
}
button.btn-kill:hover { background-color: #5c2626; }
.sort-btn {
    background-color: transparent;
    color: #89b4fa;
    border: none;
    font-size: 10px;
    padding: 2px 6px;
}
.sort-btn.active {
    background-color: #313244;
    border-radius: 4px;
}
.search-entry {
    background-color: #181825;
    color: #cdd6f4;
    border: 1px solid #313244;
    border-radius: 6px;
    padding: 3px 8px;
    font-size: 11px;
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


def _lbl(text, css_class="", ellipsize=False, xalign=0.0) -> Gtk.Label:
    l = Gtk.Label(label=text, xalign=xalign)
    if css_class:
        l.get_style_context().add_class(css_class)
    if ellipsize:
        l.set_ellipsize(Pango.EllipsizeMode.END)
        l.set_max_width_chars(30)
    return l


def _cpu_class(pct: float) -> str:
    if pct >= 50:
        return "proc-cpu-high"
    if pct >= 20:
        return "proc-cpu-med"
    return "proc-cpu-ok"


def _fmt_ram(mb: float) -> str:
    if mb >= 1024:
        return f"{mb/1024:.1f} GB"
    return f"{mb:.0f} MB"


class ProcessRow(Gtk.ListBoxRow):
    def __init__(self, group: ProcessGroup, on_action):
        super().__init__()
        self.group = group
        self.get_style_context().add_class("proc-row")
        if group.cpu_percent >= 30:
            self.get_style_context().add_class("high-cpu")
        elif group.ram_mb >= 500:
            self.get_style_context().add_class("high-ram")

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        outer.set_margin_start(8)
        outer.set_margin_end(6)
        outer.set_margin_top(5)
        outer.set_margin_bottom(5)
        self.add(outer)

        # Left: name + description
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        left.set_size_request(220, -1)
        name_lbl = _lbl(group.name, "proc-name", ellipsize=True)
        left.pack_start(name_lbl, False, False, 0)
        if group.description:
            desc_lbl = _lbl(group.description, "proc-desc", ellipsize=True)
            left.pack_start(desc_lbl, False, False, 0)
        outer.pack_start(left, False, False, 0)

        # Spacer
        outer.pack_start(Gtk.Box(), True, True, 0)

        # CPU bar + label
        cpu_vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        cpu_vb.set_size_request(90, -1)
        cpu_lbl = _lbl(f"{group.cpu_percent:5.1f}%", _cpu_class(group.cpu_percent), xalign=1.0)
        cpu_pb = Gtk.ProgressBar()
        cpu_pb.set_fraction(min(group.cpu_percent / (100.0 * _ncpu()), 1.0))
        cpu_pb.set_size_request(-1, 4)
        cpu_vb.pack_start(cpu_lbl, False, False, 0)
        cpu_vb.pack_start(cpu_pb, False, False, 0)
        outer.pack_start(cpu_vb, False, False, 0)

        # RAM
        outer.pack_start(_lbl("  ", ""), False, False, 0)
        ram_vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        ram_vb.set_size_request(80, -1)
        ram_lbl = _lbl(_fmt_ram(group.ram_mb), "proc-ram", xalign=1.0)
        ram_pb = Gtk.ProgressBar()
        ram_pb.set_fraction(min(group.ram_percent / 100.0, 1.0))
        ram_pb.set_size_request(-1, 4)
        ctx = ram_pb.get_style_context()
        ctx.remove_class("progressbar")
        ram_vb.pack_start(ram_lbl, False, False, 0)
        ram_vb.pack_start(ram_pb, False, False, 0)
        outer.pack_start(ram_vb, False, False, 0)

        # PIDs / count
        outer.pack_start(_lbl("  ", ""), False, False, 0)
        if group.process_count > 1:
            pid_text = f"{group.process_count} proc"
        else:
            pid_text = f"PID {group.pids[0]}" if group.pids else ""
        pid_lbl = _lbl(pid_text, "proc-pid")
        pid_lbl.set_size_request(64, -1)
        outer.pack_start(pid_lbl, False, False, 0)

        # Action buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_box.set_margin_start(8)

        term_btn = Gtk.Button(label="End")
        term_btn.get_style_context().add_class("btn-term")
        term_btn.set_tooltip_text(f"Terminate (SIGTERM) — politely ask to quit")
        term_btn.connect("clicked", lambda *_: on_action("term", group))

        kill_btn = Gtk.Button(label="Kill")
        kill_btn.get_style_context().add_class("btn-kill")
        kill_btn.set_tooltip_text(f"Force kill (SIGKILL) — immediate, no cleanup")
        kill_btn.connect("clicked", lambda *_: on_action("kill", group))

        btn_box.pack_start(term_btn, False, False, 0)
        btn_box.pack_start(kill_btn, False, False, 0)
        outer.pack_start(btn_box, False, False, 0)


_ncpu_cache = [None]


def _ncpu() -> int:
    if _ncpu_cache[0] is None:
        import psutil
        _ncpu_cache[0] = psutil.cpu_count() or 1
    return _ncpu_cache[0]


class ProcessTab(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        _apply_css()

        self._sort_by = "cpu"
        self._filter_text = ""
        self._groups: list[ProcessGroup] = []
        self._refresh_pending = False

        # Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_start(10)
        toolbar.set_margin_end(10)
        toolbar.set_margin_top(8)
        toolbar.set_margin_bottom(6)
        self.pack_start(toolbar, False, False, 0)

        sort_lbl = _lbl("Sort by:", "")
        sort_lbl.get_style_context().add_class("proc-desc")
        toolbar.pack_start(sort_lbl, False, False, 0)

        self._btn_cpu = Gtk.Button(label="CPU ▾")
        self._btn_cpu.get_style_context().add_class("sort-btn")
        self._btn_cpu.get_style_context().add_class("active")
        self._btn_cpu.connect("clicked", self._sort_cpu)
        toolbar.pack_start(self._btn_cpu, False, False, 0)

        self._btn_ram = Gtk.Button(label="RAM")
        self._btn_ram.get_style_context().add_class("sort-btn")
        self._btn_ram.connect("clicked", self._sort_ram)
        toolbar.pack_start(self._btn_ram, False, False, 0)

        toolbar.pack_start(Gtk.Box(), True, True, 0)

        # Search
        self._search = Gtk.Entry()
        self._search.set_placeholder_text("Filter apps…")
        self._search.get_style_context().add_class("search-entry")
        self._search.set_size_request(180, -1)
        self._search.connect("changed", self._on_search)
        toolbar.pack_start(self._search, False, False, 0)

        refresh_btn = Gtk.Button(label="↻")
        refresh_btn.set_tooltip_text("Refresh now")
        refresh_btn.connect("clicked", lambda *_: self._do_refresh())
        toolbar.pack_start(refresh_btn, False, False, 0)

        # Column header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        header.get_style_context().add_class("proc-header")
        header.set_margin_start(8)
        header.set_margin_end(6)

        def hcol(text, width=-1, expand=False):
            l = Gtk.Label(label=text, xalign=0.0)
            l.get_style_context().add_class("proc-header")
            if width > 0:
                l.set_size_request(width, -1)
            header.pack_start(l, expand, expand, 0)

        hcol("Application", 220)
        hcol("", expand=True)
        hcol("CPU %", 90)
        hcol("  RAM", 84)
        hcol("  PID(s)", 64)
        hcol("  Actions", 100)
        self.pack_start(header, False, False, 0)

        # Scrollable list
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._listbox.set_filter_func(self._filter_func)
        scroll.add(self._listbox)
        self.pack_start(scroll, True, True, 0)

        # Status bar
        self._status_lbl = Gtk.Label(label="", xalign=0.0)
        self._status_lbl.get_style_context().add_class("proc-desc")
        self._status_lbl.set_margin_start(10)
        self._status_lbl.set_margin_bottom(4)
        self.pack_start(self._status_lbl, False, False, 0)

        # Auto-refresh every 3s
        GLib.timeout_add(3000, self._auto_refresh)
        self._do_refresh()

    def _sort_cpu(self, *_):
        self._sort_by = "cpu"
        self._btn_cpu.get_style_context().add_class("active")
        self._btn_cpu.set_label("CPU ▾")
        self._btn_ram.get_style_context().remove_class("active")
        self._btn_ram.set_label("RAM")
        self._do_refresh()

    def _sort_ram(self, *_):
        self._sort_by = "ram"
        self._btn_ram.get_style_context().add_class("active")
        self._btn_ram.set_label("RAM ▾")
        self._btn_cpu.get_style_context().remove_class("active")
        self._btn_cpu.set_label("CPU")
        self._do_refresh()

    def _on_search(self, entry):
        self._filter_text = entry.get_text().lower()
        self._listbox.invalidate_filter()

    def _filter_func(self, row: Gtk.ListBoxRow) -> bool:
        if not self._filter_text:
            return True
        if not isinstance(row, ProcessRow):
            return True
        g = row.group
        return (
            self._filter_text in g.name.lower()
            or self._filter_text in g.raw_name.lower()
            or self._filter_text in g.description.lower()
        )

    def _auto_refresh(self) -> bool:
        # Don't fire off a 30-process scan thread while the tab is offscreen
        # (e.g. main window hidden, or a different tab is active).
        top = self.get_toplevel()
        if top is not None and not top.get_visible():
            return True
        if hasattr(self, "is_drawable") and not self.is_drawable():
            return True
        self._do_refresh()
        return True

    def _do_refresh(self):
        import threading
        t = threading.Thread(target=self._fetch_and_update, daemon=True)
        t.start()

    def _fetch_and_update(self):
        groups = collect_top_processes(n=30, sort_by=self._sort_by)
        GLib.idle_add(self._rebuild_list, groups)

    def _rebuild_list(self, groups: list):
        self._groups = groups
        for child in self._listbox.get_children():
            self._listbox.remove(child)
        for g in groups:
            row = ProcessRow(g, self._on_action)
            self._listbox.add(row)
        self._listbox.show_all()
        self._listbox.invalidate_filter()

        total_cpu = sum(g.cpu_percent for g in groups)
        total_ram = sum(g.ram_mb for g in groups)
        self._status_lbl.set_text(
            f"Showing {len(groups)} app groups · "
            f"Top total: CPU {total_cpu:.1f}%  RAM {_fmt_ram(total_ram)}"
        )

    def _on_action(self, action: str, group: ProcessGroup):
        if action == "term":
            self._confirm_and_act(
                group,
                f'End "{group.name}"?',
                f"This will send a polite quit signal to {group.process_count} process(es).\n"
                f"The app may save its work before closing.",
                "End Process",
                terminate_group,
            )
        elif action == "kill":
            self._confirm_and_act(
                group,
                f'Force-kill "{group.name}"?',
                f"This will immediately kill {group.process_count} process(es) with no cleanup.\n"
                f"Any unsaved data will be lost.",
                "Force Kill",
                kill_group,
                is_destructive=True,
            )

    def _confirm_and_act(self, group, title, msg, btn_label, fn, is_destructive=False):
        dlg = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            flags=0,
            message_type=Gtk.MessageType.WARNING if is_destructive else Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.CANCEL,
            text=title,
        )
        dlg.format_secondary_text(msg)
        action_btn = dlg.add_button(btn_label, Gtk.ResponseType.OK)
        if is_destructive:
            action_btn.get_style_context().add_class("destructive-action")

        resp = dlg.run()
        dlg.destroy()

        if resp == Gtk.ResponseType.OK:
            count = fn(group.pids)
            self._show_toast(f"Signal sent to {count} process(es).")
            GLib.timeout_add(1200, self._do_refresh)

    def _show_toast(self, msg: str):
        self._status_lbl.set_markup(f'<span color="#a6e3a1">{msg}</span>')
        GLib.timeout_add(4000, lambda: self._status_lbl.set_text("") or False)
