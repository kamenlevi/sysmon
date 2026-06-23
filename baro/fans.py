"""Fan detection, RPM reading, and PWM curve control via hwmon sysfs."""
import glob
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


def _read(path: str, default: str = "") -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default


def _write(path: str, value: str) -> bool:
    try:
        with open(path, "w") as f:
            f.write(value)
        return True
    except PermissionError:
        # Try via pkexec/sudo
        try:
            r = subprocess.run(
                ["sudo", "-n", "tee", path],
                input=value.encode(),
                capture_output=True,
                timeout=2,
            )
            return r.returncode == 0
        except Exception:
            return False
    except Exception:
        return False


@dataclass
class FanChannel:
    key: str               # unique id e.g. "hwmon3/fan1"
    label: str             # display name e.g. "CPU Fan"
    hwmon_name: str        # e.g. "nct6798"
    rpm: int = 0
    rpm_min: int = 0
    rpm_max: int = 0       # estimated from history
    pwm_path: Optional[str] = None
    pwm_enable_path: Optional[str] = None
    pwm_max_path: Optional[str] = None
    controllable: bool = False   # can we write to pwm?
    pwm_writable: bool = False   # direct write without sudo?
    # Current applied curve for this fan
    curve: List[Tuple[float, float]] = field(default_factory=lambda: list(DEFAULT_CURVE))


# Default fan curve: (temp_celsius, speed_percent)
DEFAULT_CURVE: List[Tuple[float, float]] = [
    (30.0, 20.0),
    (50.0, 35.0),
    (65.0, 55.0),
    (75.0, 75.0),
    (85.0, 90.0),
    (95.0, 100.0),
]


def detect_fans() -> Dict[str, FanChannel]:
    """Scan hwmon sysfs and return all fan channels found."""
    fans: Dict[str, FanChannel] = {}

    for hwmon_dir in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        hwmon_name = _read(os.path.join(hwmon_dir, "name")) or os.path.basename(hwmon_dir)

        for fan_input in sorted(glob.glob(os.path.join(hwmon_dir, "fan*_input"))):
            m = re.search(r"fan(\d+)_input$", fan_input)
            if not m:
                continue
            idx = m.group(1)

            raw_rpm = _read(fan_input)
            try:
                rpm = int(raw_rpm)
            except ValueError:
                continue

            label_path = os.path.join(hwmon_dir, f"fan{idx}_label")
            label = _read(label_path) or f"{hwmon_name} fan {idx}"
            # Prettify: "fan1" → "Fan 1", keep custom labels
            if label.lower().startswith("fan") and len(label) <= 5:
                label = f"{hwmon_name.upper()} Fan {idx}"

            key = f"{os.path.basename(hwmon_dir)}/fan{idx}"

            pwm_path = os.path.join(hwmon_dir, f"pwm{idx}")
            pwm_enable_path = os.path.join(hwmon_dir, f"pwm{idx}_enable")
            pwm_max_path = os.path.join(hwmon_dir, f"pwm{idx}_max")

            has_pwm = os.path.exists(pwm_path)
            pwm_writable = has_pwm and os.access(pwm_path, os.W_OK)

            fans[key] = FanChannel(
                key=key,
                label=label,
                hwmon_name=hwmon_name,
                rpm=rpm,
                pwm_path=pwm_path if has_pwm else None,
                pwm_enable_path=pwm_enable_path if os.path.exists(pwm_enable_path) else None,
                pwm_max_path=pwm_max_path if os.path.exists(pwm_max_path) else None,
                controllable=has_pwm,
                pwm_writable=pwm_writable,
            )

    return fans


def refresh_rpms(fans: Dict[str, FanChannel]) -> None:
    """Update RPM values in-place for all known fans."""
    for key, fan in fans.items():
        hwmon, fan_n = key.split("/")
        idx = fan_n.replace("fan", "")
        path = f"/sys/class/hwmon/{hwmon}/fan{idx}_input"
        try:
            rpm = int(_read(path) or "0")
            fan.rpm = rpm
            if rpm > fan.rpm_max:
                fan.rpm_max = rpm
        except ValueError:
            pass


def curve_speed_at(curve: List[Tuple[float, float]], temp: float) -> float:
    """Interpolate fan speed (0-100%) from curve at given temperature."""
    if not curve:
        return 50.0
    if temp <= curve[0][0]:
        return curve[0][1]
    if temp >= curve[-1][0]:
        return curve[-1][1]
    for i in range(len(curve) - 1):
        t0, s0 = curve[i]
        t1, s1 = curve[i + 1]
        if t0 <= temp <= t1:
            frac = (temp - t0) / (t1 - t0)
            return s0 + frac * (s1 - s0)
    return curve[-1][1]


def speed_to_pwm(speed_pct: float, pwm_max: int = 255) -> int:
    return max(0, min(pwm_max, int(round(speed_pct / 100.0 * pwm_max))))


def set_fan_manual(fan: FanChannel) -> bool:
    """Switch fan to manual PWM control (pwm_enable=1)."""
    if fan.pwm_enable_path:
        return _write(fan.pwm_enable_path, "1")
    return False


def set_fan_auto(fan: FanChannel) -> bool:
    """Return fan to automatic BIOS control (pwm_enable=2)."""
    if fan.pwm_enable_path:
        return _write(fan.pwm_enable_path, "2")
    return False


def apply_pwm(fan: FanChannel, speed_pct: float) -> bool:
    """Write a PWM value (0-100%) to the fan channel."""
    if not fan.pwm_path:
        return False
    pwm_max = 255
    if fan.pwm_max_path:
        try:
            pwm_max = int(_read(fan.pwm_max_path) or "255")
        except ValueError:
            pass
    val = speed_to_pwm(speed_pct, pwm_max)
    return _write(fan.pwm_path, str(val))


class FanCurveController:
    """
    Background thread that continuously applies user-defined fan curves
    based on current CPU temperature. Reverts fans to auto on stop.
    """

    def __init__(self, fans: Dict[str, FanChannel], get_cpu_temp_fn):
        self._fans = fans
        self._get_temp = get_cpu_temp_fn
        self._enabled: Dict[str, bool] = {}   # key → curve active?
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def set_curve_active(self, fan_key: str, active: bool):
        with self._lock:
            self._enabled[fan_key] = active
        if not active:
            fan = self._fans.get(fan_key)
            if fan:
                set_fan_auto(fan)

    def is_active(self, fan_key: str) -> bool:
        return self._enabled.get(fan_key, False)

    def update_curve(self, fan_key: str, curve: List[Tuple[float, float]]):
        fan = self._fans.get(fan_key)
        if fan:
            fan.curve = list(curve)

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        # Revert all controlled fans to auto
        for key, active in list(self._enabled.items()):
            if active:
                fan = self._fans.get(key)
                if fan:
                    set_fan_auto(fan)
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self):
        while not self._stop.is_set():
            temp = self._get_temp()
            if temp > 0:
                with self._lock:
                    active_keys = [k for k, v in self._enabled.items() if v]
                for key in active_keys:
                    fan = self._fans.get(key)
                    if fan and fan.controllable:
                        speed = curve_speed_at(fan.curve, temp)
                        set_fan_manual(fan)
                        apply_pwm(fan, speed)
            self._stop.wait(2.0)


def check_pwm_writable() -> bool:
    """Return True if any hwmon PWM file is directly writable."""
    for path in glob.glob("/sys/class/hwmon/hwmon*/pwm[0-9]"):
        if os.access(path, os.W_OK):
            return True
    return False
