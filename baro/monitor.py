"""System data collection — runs in a background thread."""
import glob
import os
import threading
import time
from dataclasses import dataclass, field, replace
from typing import List, Optional, Callable

import psutil


@dataclass
class SystemStats:
    timestamp: float = 0.0

    cpu_percent: float = 0.0
    cpu_per_core: List[float] = field(default_factory=list)
    cpu_freq_mhz: float = 0.0
    cpu_freq_max_mhz: float = 0.0
    cpu_temp: float = 0.0

    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    ram_percent: float = 0.0
    swap_used_gb: float = 0.0
    swap_total_gb: float = 0.0
    swap_percent: float = 0.0

    gpu_available: bool = False
    gpu_name: str = ""
    gpu_percent: float = 0.0
    gpu_temp: float = 0.0
    gpu_mem_used_mb: float = 0.0
    gpu_mem_total_mb: float = 0.0
    gpu_mem_percent: float = 0.0
    gpu_power_w: float = 0.0

    thermal_throttling: bool = False
    warnings: List[str] = field(default_factory=list)

    # Fan data: list of (label, rpm, controllable) tuples — lightweight for UI
    fans: List[tuple] = field(default_factory=list)

    def clone(self):
        return replace(
            self,
            cpu_per_core=list(self.cpu_per_core),
            warnings=list(self.warnings),
            fans=list(self.fans),
        )


def _read_file(path: str, default="") -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default


def _detect_nvidia():
    try:
        import pynvml
        pynvml.nvmlInit()
        return pynvml
    except Exception:
        return None


def _detect_amd_gpu():
    """Return sysfs paths for AMD GPU if present."""
    paths = glob.glob("/sys/class/drm/card*/device/gpu_busy_percent")
    if paths:
        card_path = os.path.dirname(paths[0])
        # find hwmon for temp
        hwmon_dirs = glob.glob(os.path.join(card_path, "hwmon/hwmon*"))
        temp_path = None
        power_path = None
        for hw in hwmon_dirs:
            t = os.path.join(hw, "temp1_input")
            if os.path.exists(t):
                temp_path = t
            p = os.path.join(hw, "power1_average")
            if os.path.exists(p):
                power_path = p
        mem_info = glob.glob(os.path.join(card_path, "mem_info_vram_used"))
        mem_total = glob.glob(os.path.join(card_path, "mem_info_vram_total"))
        return {
            "busy": paths[0],
            "temp": temp_path,
            "power": power_path,
            "mem_used": mem_info[0] if mem_info else None,
            "mem_total": mem_total[0] if mem_total else None,
        }
    return None


def _get_cpu_temp() -> float:
    try:
        temps = psutil.sensors_temperatures()
        for key in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
            if key in temps:
                entries = temps[key]
                vals = [e.current for e in entries if e.current and e.current > 0]
                if vals:
                    return max(vals)
        # try all keys, pick max plausible temp
        for entries in temps.values():
            for e in entries:
                if e.current and 20 < e.current < 120:
                    return e.current
    except Exception:
        pass
    # fallback: thermal zones
    for path in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
        try:
            val = int(_read_file(path)) / 1000.0
            if 20 < val < 120:
                return val
        except Exception:
            pass
    return 0.0


_THROTTLE_COUNT_GLOB = "/sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count"


def _read_throttle_total() -> Optional[int]:
    """Sum of per-core thermal_throttle counters, or None if unavailable."""
    paths = glob.glob(_THROTTLE_COUNT_GLOB)
    if not paths:
        return None
    total = 0
    any_read = False
    for p in paths:
        raw = _read_file(p)
        if raw:
            try:
                total += int(raw)
                any_read = True
            except ValueError:
                pass
    return total if any_read else None


class SystemMonitor:
    def __init__(self, interval: float = 1.5):
        self.interval = interval
        self._stats = SystemStats()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._callbacks: List[Callable] = []

        self._nvml = _detect_nvidia()
        self._nvml_handle = None
        self._nvml_name = ""
        if self._nvml:
            try:
                self._nvml_handle = self._nvml.nvmlDeviceGetHandleByIndex(0)
                self._nvml_name = self._nvml.nvmlDeviceGetName(self._nvml_handle)
                if isinstance(self._nvml_name, bytes):
                    self._nvml_name = self._nvml_name.decode()
            except Exception:
                self._nvml = None

        self._amd = None if self._nvml else _detect_amd_gpu()
        if self._amd:
            self._gpu_name = _read_file(
                glob.glob("/sys/class/drm/card*/device/product_name")[0]
                if glob.glob("/sys/class/drm/card*/device/product_name") else ""
            ) or "AMD GPU"
        elif self._nvml:
            self._gpu_name = self._nvml_name
        else:
            self._gpu_name = ""

        # Warm up CPU percent measurement
        psutil.cpu_percent(interval=None)

        # Track thermal throttle counter to detect changes (not absolute presence)
        self._last_throttle_count = _read_throttle_total()
        self._last_throttle_time = 0.0

    def add_callback(self, cb: Callable):
        self._callbacks.append(cb)

    def get_stats(self) -> SystemStats:
        with self._lock:
            return self._stats.clone()

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _collect(self) -> SystemStats:
        s = SystemStats()
        s.timestamp = time.time()

        # CPU
        s.cpu_percent = psutil.cpu_percent(interval=None)
        s.cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
        try:
            freq = psutil.cpu_freq()
            if freq:
                s.cpu_freq_mhz = freq.current
                s.cpu_freq_max_mhz = freq.max if freq.max else freq.current
        except Exception:
            pass
        s.cpu_temp = _get_cpu_temp()

        # RAM
        vm = psutil.virtual_memory()
        s.ram_used_gb = vm.used / 1e9
        s.ram_total_gb = vm.total / 1e9
        s.ram_percent = vm.percent
        sw = psutil.swap_memory()
        s.swap_used_gb = sw.used / 1e9
        s.swap_total_gb = sw.total / 1e9
        s.swap_percent = sw.percent

        # GPU
        if self._nvml and self._nvml_handle:
            try:
                util = self._nvml.nvmlDeviceGetUtilizationRates(self._nvml_handle)
                s.gpu_percent = float(util.gpu)
                temp = self._nvml.nvmlDeviceGetTemperature(
                    self._nvml_handle, self._nvml.NVML_TEMPERATURE_GPU
                )
                s.gpu_temp = float(temp)
                mem = self._nvml.nvmlDeviceGetMemoryInfo(self._nvml_handle)
                s.gpu_mem_used_mb = mem.used / 1e6
                s.gpu_mem_total_mb = mem.total / 1e6
                s.gpu_mem_percent = 100.0 * mem.used / mem.total if mem.total else 0
                try:
                    pwr = self._nvml.nvmlDeviceGetPowerUsage(self._nvml_handle)
                    s.gpu_power_w = pwr / 1000.0
                except Exception:
                    pass
                s.gpu_available = True
                s.gpu_name = self._gpu_name
            except Exception:
                pass
        elif self._amd:
            try:
                s.gpu_percent = float(_read_file(self._amd["busy"]) or 0)
                if self._amd["temp"]:
                    raw = _read_file(self._amd["temp"])
                    s.gpu_temp = int(raw) / 1000.0 if raw else 0
                if self._amd["mem_used"] and self._amd["mem_total"]:
                    used = int(_read_file(self._amd["mem_used"]) or 0)
                    total = int(_read_file(self._amd["mem_total"]) or 0)
                    s.gpu_mem_used_mb = used / 1e6
                    s.gpu_mem_total_mb = total / 1e6
                    s.gpu_mem_percent = 100.0 * used / total if total else 0
                if self._amd["power"]:
                    raw = _read_file(self._amd["power"])
                    s.gpu_power_w = int(raw) / 1e6 if raw else 0
                s.gpu_available = True
                s.gpu_name = self._gpu_name
            except Exception:
                pass

        # Thermal throttling: only flag when the kernel counter actually
        # increased recently (within ~10s of the last bump).
        cur_throttle = _read_throttle_total()
        if cur_throttle is not None and self._last_throttle_count is not None:
            if cur_throttle > self._last_throttle_count:
                self._last_throttle_time = s.timestamp
            self._last_throttle_count = cur_throttle
        elif cur_throttle is not None:
            self._last_throttle_count = cur_throttle
        s.thermal_throttling = (
            self._last_throttle_time > 0
            and (s.timestamp - self._last_throttle_time) < 10.0
        )

        # Warnings
        if s.thermal_throttling:
            s.warnings.append("CPU thermal throttling")
        if s.cpu_temp > 90:
            s.warnings.append(f"CPU temp critical: {s.cpu_temp:.0f}°C")
        if s.cpu_percent >= 99:
            s.warnings.append("CPU at 100% load")
        if s.ram_percent > 90:
            s.warnings.append(f"RAM usage critical: {s.ram_percent:.0f}%")
        if s.gpu_available:
            if s.gpu_temp > 85:
                s.warnings.append(f"GPU temp critical: {s.gpu_temp:.0f}°C")
            if s.gpu_mem_percent > 90:
                s.warnings.append(f"GPU VRAM critical: {s.gpu_mem_percent:.0f}%")

        # Fans
        try:
            fan_data = psutil.sensors_fans()
            for hw_label, entries in fan_data.items():
                for e in entries:
                    label = e.label or hw_label
                    s.fans.append((label, int(e.current), False))
        except Exception:
            pass

        return s

    def _loop(self):
        while not self._stop_event.is_set():
            s = self._collect()
            with self._lock:
                self._stats = s
            for cb in list(self._callbacks):
                try:
                    cb(s)
                except Exception:
                    pass
            self._stop_event.wait(self.interval)
