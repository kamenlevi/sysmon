#!/usr/bin/env bash
set -e

echo "=== Baro Installer ==="
echo ""

# System packages
echo "[1/4] Installing system dependencies..."
sudo apt-get update -q
sudo apt-get install -y \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gtk-3.0 \
    gir1.2-appindicator3-0.1 \
    gir1.2-ayatanaappindicator3-0.1 \
    gir1.2-notify-0.7 \
    python3-cairo \
    python3-dev \
    libgirepository1.0-dev \
    gcc \
    libcairo2-dev \
    pkg-config \
    lm-sensors \
    gnome-shell-extension-appindicator \
    python3-pip \
    python3-matplotlib \
    python3-numpy

# Python packages
echo "[2/4] Installing Python packages..."
pip3 install --user psutil pynvml pycairo

# Detect lm-sensors
echo "[2b] Configuring lm-sensors (answer yes to all prompts)..."
sudo sensors-detect --auto 2>/dev/null || true

# Fan PWM udev rule — grants current user write access to hwmon PWM files
echo "[2c] Installing fan PWM udev rule for user: $USER..."
UDEV_RULE=/etc/udev/rules.d/60-baro-fancontrol.rules
sudo tee "$UDEV_RULE" > /dev/null << UDEV
# Baro: allow $USER to control fan PWM without root
SUBSYSTEM=="hwmon", ACTION=="add", RUN+="/bin/sh -c 'chown $USER /sys/%p/pwm* /sys/%p/pwm*_enable 2>/dev/null; chmod 664 /sys/%p/pwm* /sys/%p/pwm*_enable 2>/dev/null'"
UDEV
sudo udevadm control --reload-rules 2>/dev/null || true
sudo udevadm trigger 2>/dev/null || true
echo "    udev rule written to $UDEV_RULE"
echo "    Fan control will be active after your next login (or reboot)."

# Install app
echo "[3/4] Installing Baro..."
INSTALL_DIR="$HOME/.local/lib/baro"
mkdir -p "$INSTALL_DIR"
cp -r "$(dirname "$0")/baro" "$INSTALL_DIR/"

mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/baro" <<'LAUNCHER'
#!/usr/bin/env bash
exec python3 -m baro "$@"
LAUNCHER
chmod +x "$HOME/.local/bin/baro"

# Add to PATH if needed
if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.profile"
fi

# Fix the PYTHONPATH in the launcher
sed -i "1a export PYTHONPATH=\"$INSTALL_DIR:\$PYTHONPATH\"" "$HOME/.local/bin/baro"

# Autostart desktop entry
echo "[4/4] Setting up autostart..."
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/baro.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Baro
Comment=System Monitor
Exec=$HOME/.local/bin/baro
Icon=utilities-system-monitor
X-GNOME-Autostart-enabled=true
NoDisplay=false
Hidden=false
DESKTOP

# Application launcher
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/baro.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Baro
Comment=Real-time CPU/GPU/RAM Monitor
Exec=$HOME/.local/bin/baro
Icon=utilities-system-monitor
Categories=System;Monitor;
Terminal=false
StartupNotify=false
DESKTOP

echo ""
echo "=== Installation complete! ==="
echo ""
echo "Start now:  baro"
echo "  (you may need to open a new terminal first for PATH to update)"
echo ""
echo "Note: If the tray icon doesn't appear, enable the AppIndicator extension:"
echo "  gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com"
echo "  or install: sudo apt install gnome-shell-extension-appindicator"
echo "  then log out and back in."
echo ""
