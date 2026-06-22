"""Process collection and grouping logic."""
import os
import signal
import psutil
from dataclasses import dataclass, field
from typing import List, Optional

# Maps process executable name → human-friendly app name
FRIENDLY_NAMES = {
    # Browsers
    "chrome": "Google Chrome",
    "chromium": "Chromium",
    "chromium-browser": "Chromium",
    "google-chrome": "Google Chrome",
    "google-chrome-stable": "Google Chrome",
    "brave": "Brave Browser",
    "brave-browser": "Brave Browser",
    "firefox": "Firefox",
    "firefox-esr": "Firefox",
    "opera": "Opera",
    "vivaldi-bin": "Vivaldi",
    "microsoft-edge": "Microsoft Edge",
    "msedge": "Microsoft Edge",
    # Dev tools
    "code": "Visual Studio Code",
    "code-oss": "VS Code (OSS)",
    "codium": "VSCodium",
    "cursor": "Cursor Editor",
    "sublime_text": "Sublime Text",
    "atom": "Atom",
    "gedit": "Text Editor",
    "vim": "Vim",
    "nvim": "Neovim",
    "emacs": "Emacs",
    "idea.sh": "IntelliJ IDEA",
    "pycharm.sh": "PyCharm",
    "webstorm.sh": "WebStorm",
    "clion.sh": "CLion",
    # Terminals
    "gnome-terminal-server": "GNOME Terminal",
    "konsole": "Konsole",
    "xterm": "XTerm",
    "alacritty": "Alacritty",
    "kitty": "Kitty",
    "wezterm-gui": "WezTerm",
    "tilix": "Tilix",
    "terminator": "Terminator",
    # Communication
    "slack": "Slack",
    "discord": "Discord",
    "signal-desktop": "Signal",
    "telegram-desktop": "Telegram",
    "thunderbird": "Thunderbird",
    "evolution": "Evolution Mail",
    "zoom": "Zoom",
    "teams": "Microsoft Teams",
    # Media
    "spotify": "Spotify",
    "vlc": "VLC",
    "mpv": "mpv",
    "rhythmbox": "Rhythmbox",
    "totem": "GNOME Videos",
    "celluloid": "Celluloid",
    # Creative
    "gimp-2.10": "GIMP",
    "gimp": "GIMP",
    "inkscape": "Inkscape",
    "blender": "Blender",
    "krita": "Krita",
    "darktable": "Darktable",
    "obs": "OBS Studio",
    "obs-studio": "OBS Studio",
    # System
    "gnome-shell": "GNOME Shell",
    "Xorg": "X Server",
    "Xwayland": "XWayland",
    "kwin_x11": "KWin",
    "kwin_wayland": "KWin (Wayland)",
    "mutter": "Mutter (Compositor)",
    "pulseaudio": "PulseAudio",
    "pipewire": "PipeWire",
    "pipewire-pulse": "PipeWire (PulseAudio)",
    "wireplumber": "WirePlumber",
    "nautilus": "Files (Nautilus)",
    "thunar": "Thunar File Manager",
    "dolphin": "Dolphin",
    "systemd": "systemd",
    "dockerd": "Docker Daemon",
    "containerd": "containerd",
    "mongod": "MongoDB",
    "postgres": "PostgreSQL",
    "mysqld": "MySQL",
    "redis-server": "Redis",
    "nginx": "Nginx",
    "apache2": "Apache",
    # Runtimes
    "python3": "Python 3",
    "python": "Python",
    "python3.10": "Python 3.10",
    "python3.11": "Python 3.11",
    "python3.12": "Python 3.12",
    "node": "Node.js",
    "java": "Java",
    "ruby": "Ruby",
    "php": "PHP",
    "go": "Go",
    "rust": "Rust (cargo)",
}

# Process names that are renderer/helper subprocesses (grouped under parent)
SUBPROCESS_HINTS = {
    "chrome", "chromium", "chromium-browser", "google-chrome",
    "google-chrome-stable", "brave-browser", "brave", "firefox",
    "opera", "vivaldi-bin", "microsoft-edge", "msedge",
    "code", "code-oss", "electron", "slack", "discord",
    "spotify", "signal-desktop", "telegram-desktop",
}

# Known subprocess type args
_SUBPROCESS_ARGS = {
    "--type=renderer", "--type=gpu-process", "--type=utility",
    "--type=zygote", "--type=sandbox", "tab",
}


@dataclass
class ProcessGroup:
    name: str           # friendly display name
    raw_name: str       # executable name
    description: str    # what it's doing / script name / tab count etc.
    pids: List[int] = field(default_factory=list)
    cpu_percent: float = 0.0
    ram_mb: float = 0.0
    ram_percent: float = 0.0
    process_count: int = 1
    is_system: bool = False

    @property
    def kill_safe(self) -> bool:
        """True if this is a user process (safer to offer kill)."""
        return not self.is_system


def _friendly_name(proc_name: str, cmdline: List[str]) -> str:
    # Direct match
    if proc_name in FRIENDLY_NAMES:
        return FRIENDLY_NAMES[proc_name]
    # Check cmdline[0] basename
    if cmdline:
        base = os.path.basename(cmdline[0])
        if base in FRIENDLY_NAMES:
            return FRIENDLY_NAMES[base]
        # Strip version suffix: python3.11 → python3
        stripped = base.rstrip("0123456789.")
        if stripped in FRIENDLY_NAMES:
            return FRIENDLY_NAMES[stripped]
    # Capitalise as fallback
    return proc_name.replace("-", " ").replace("_", " ").title()


def _describe(proc_name: str, cmdline: List[str], count: int) -> str:
    """Build a human-readable description of what the process is doing."""
    parts = []

    if count > 1:
        if proc_name in {"chrome", "chromium", "chromium-browser", "google-chrome",
                         "google-chrome-stable", "brave-browser", "brave",
                         "microsoft-edge", "msedge", "opera", "vivaldi-bin"}:
            parts.append(f"~{count - 1} tab(s)/processes")
        elif proc_name in {"code", "code-oss", "electron", "slack", "discord"}:
            parts.append(f"{count} processes (Electron)")
        else:
            parts.append(f"{count} processes")

    # For scripts: show script name
    if proc_name in {"python3", "python", "python3.10", "python3.11", "python3.12"}:
        for arg in cmdline[1:]:
            if arg.endswith(".py") and not arg.startswith("-"):
                parts.append(os.path.basename(arg))
                break
        else:
            # Show first meaningful arg
            for arg in cmdline[1:]:
                if not arg.startswith("-") and arg not in {"-c", "-m"}:
                    parts.append(arg[:40])
                    break

    if proc_name == "node":
        for arg in cmdline[1:]:
            if not arg.startswith("-"):
                parts.append(os.path.basename(arg)[:40])
                break

    if proc_name == "java":
        for arg in cmdline:
            if not arg.startswith("-") and "." in arg:
                parts.append(arg.split(".")[-1][:40])
                break

    return "  ·  ".join(parts) if parts else ""


def _is_system(proc: psutil.Process) -> bool:
    try:
        return proc.uids().real == 0 or proc.username() in ("root", "daemon", "nobody")
    except Exception:
        return False


def collect_top_processes(n: int = 20, sort_by: str = "cpu") -> List[ProcessGroup]:
    """Return top N process groups, sorted by cpu or ram.

    Non-blocking: psutil.process_iter caches Process objects, so cpu_percent
    is the delta since the previous call (~one poll interval). The very first
    call reads ~0% CPU; every call after is accurate. No sleeping.
    """
    raw: dict = {}  # key=proc_name → aggregated data

    attrs = ["pid", "name", "cmdline", "cpu_percent", "memory_info",
             "memory_percent", "username", "status"]

    try:
        proc_iter = psutil.process_iter(attrs)
    except Exception:
        return []

    for p in proc_iter:
        try:
            info = p.info
            name = info.get("name") or ""
            if not name or info.get("status") == psutil.STATUS_ZOMBIE:
                continue
            cpu = info.get("cpu_percent") or 0.0
            mi = info.get("memory_info")
            mem_mb = mi.rss / 1e6 if mi else 0.0
            mem_pct = info.get("memory_percent") or 0.0
            cmdline = info.get("cmdline") or []
            pid = p.pid
            uid_is_system = _is_system(p)

            key = name
            if key not in raw:
                raw[key] = {
                    "name": name,
                    "cmdline": cmdline,
                    "cpu": 0.0,
                    "ram_mb": 0.0,
                    "ram_pct": 0.0,
                    "pids": [],
                    "is_system": uid_is_system,
                }
            raw[key]["cpu"] += cpu
            raw[key]["ram_mb"] += mem_mb
            raw[key]["ram_pct"] += mem_pct
            raw[key]["pids"].append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    groups = []
    for name, data in raw.items():
        count = len(data["pids"])
        friendly = _friendly_name(name, data["cmdline"])
        desc = _describe(name, data["cmdline"], count)
        groups.append(ProcessGroup(
            name=friendly,
            raw_name=name,
            description=desc,
            pids=data["pids"],
            cpu_percent=min(data["cpu"], 100.0 * psutil.cpu_count()),
            ram_mb=data["ram_mb"],
            ram_percent=data["ram_pct"],
            process_count=count,
            is_system=data["is_system"],
        ))

    key_fn = (lambda g: g.cpu_percent) if sort_by == "cpu" else (lambda g: g.ram_mb)
    groups.sort(key=key_fn, reverse=True)
    return groups[:n]


def terminate_group(pids: List[int]) -> int:
    """Send SIGTERM to all PIDs. Returns count of successes."""
    ok = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            ok += 1
        except (ProcessLookupError, PermissionError):
            pass
    return ok


def kill_group(pids: List[int]) -> int:
    """Send SIGKILL to all PIDs. Returns count of successes."""
    ok = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            ok += 1
        except (ProcessLookupError, PermissionError):
            pass
    return ok
