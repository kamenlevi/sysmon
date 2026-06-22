"""Entry point."""
import os
import signal
import sys

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Notify", "0.7")
from gi.repository import Gtk, GLib, Notify


def main():
    Notify.init("SysMon")

    from sysmon.settings import Settings
    from sysmon.history import HistoryDB
    from sysmon.monitor import SystemMonitor
    from sysmon.indicator import SysMonIndicator

    settings = Settings()
    history = HistoryDB(max_age_hours=settings.history_hours)
    monitor = SystemMonitor(interval=settings.poll_interval, settings=settings)

    indicator = SysMonIndicator(monitor, history, settings)

    monitor.start()

    signal.signal(signal.SIGINT, signal.SIG_DFL)

    try:
        Gtk.main()
    finally:
        monitor.stop()
        history.close()
        Notify.uninit()


if __name__ == "__main__":
    # Allow running as: python -m sysmon
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    main()
