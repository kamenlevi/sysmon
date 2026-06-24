#!/usr/bin/env bash
# Build a Baro .deb. Usage: packaging/build-deb.sh [version]
set -e
VERSION="${1:-1.0.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="baro_${VERSION}_all"
STAGE="$ROOT/build/$PKG"

rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/usr/lib/python3/dist-packages/baro" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/doc/baro"

# App package (python3 -m baro finds it on dist-packages)
cp "$ROOT"/baro/*.py "$STAGE/usr/lib/python3/dist-packages/baro/"

# Launcher
cat > "$STAGE/usr/bin/baro" <<'EOF'
#!/usr/bin/env bash
exec python3 -m baro "$@"
EOF
chmod 755 "$STAGE/usr/bin/baro"

# Desktop launcher
cat > "$STAGE/usr/share/applications/baro.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Baro
Comment=Real-time CPU/GPU/RAM/disk monitor in your top bar
Exec=baro
Icon=utilities-system-monitor
Categories=System;Monitor;
Terminal=false
StartupNotify=false
EOF

# Docs
cp "$ROOT/README.md" "$STAGE/usr/share/doc/baro/README.md"
cp "$ROOT/LICENSE"   "$STAGE/usr/share/doc/baro/copyright"

# Debian changelog
cat > "$STAGE/usr/share/doc/baro/changelog" <<EOF
baro (${VERSION}) unstable; urgency=low

  * Release ${VERSION}.

 -- Kamen Levi <kamenlevi@gmail.com>  $(date -R)
EOF
gzip -9n "$STAGE/usr/share/doc/baro/changelog"

# Normalise permissions (dirs 755, files 644, launcher 755)
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE/usr" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/usr/bin/baro"
chmod 755 "$STAGE/DEBIAN"

# Control
INSTALLED_KB=$(du -sk "$STAGE/usr" | cut -f1)
cat > "$STAGE/DEBIAN/control" <<EOF
Package: baro
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: all
Installed-Size: ${INSTALLED_KB}
Depends: python3 (>= 3.8), python3-gi, python3-gi-cairo, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, gir1.2-notify-0.7, python3-psutil, python3-cairo, lm-sensors
Recommends: gnome-shell-extension-appindicator
Maintainer: Kamen Levi <kamenlevi@gmail.com>
Homepage: https://github.com/kamenlevi/baro
Description: Real-time system monitor for the GNOME top bar
 Baro shows live CPU, GPU, memory and disk usage in the menu bar with donut
 gauges, expandable details, per-core graphs, usage history and a process
 manager. Inspired by the macOS Stats app.
EOF

fakeroot dpkg-deb --build "$STAGE" "$ROOT/build/${PKG}.deb" >/dev/null
echo "Built build/${PKG}.deb"
