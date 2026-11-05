#!/usr/bin/env bash
# Build dist/sytrak-gen_<version>_<arch>.deb (Ubuntu / Debian). Must run ON Linux:
# PyInstaller cannot cross-build, so build on the oldest Ubuntu you install on.
#
#   sudo apt install python3-venv python3-tk dpkg-dev
#   ./build_deb.sh
#   sudo apt install ./dist/sytrak-gen_0.1.0_amd64.deb
#
# Installs to /opt/sytrak-gen, adds the "sytrak-gen" command and a menu entry.
# The Google key is NOT packaged: copy it to /opt/sytrak-gen/secrets/ or pick it in Settings.
set -euo pipefail
cd "$(dirname "$0")"

PKG=sytrak-gen
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/sytrak_gen/__init__.py)"
ARCH="$(dpkg --print-architecture)"

command -v dpkg-deb >/dev/null || { echo "dpkg-deb missing: sudo apt install dpkg-dev"; exit 1; }
python3 -c "import tkinter" 2>/dev/null || { echo "Tkinter missing: sudo apt install python3-tk"; exit 1; }

# A .venv copied from Windows has no bin/python - use a separate Linux one then.
VENV=.venv
[ -x "$VENV/bin/python" ] || VENV=.venv-linux
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
PY="$VENV/bin/python"

"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r requirements-dev.txt
"$PY" -m pytest -q tests
[ -f assets/icons/app.png ] || "$PY" tools/make_icon.py
"$PY" -m PyInstaller --noconfirm --clean sytrak_gen.spec     # -> dist/SyTrakGen

STAGE="build/deb/${PKG}_${VERSION}_${ARCH}"
rm -rf "$STAGE"
install -d "$STAGE/DEBIAN" "$STAGE/opt/$PKG/secrets" "$STAGE/usr/bin" \
           "$STAGE/usr/share/applications" "$STAGE/usr/share/pixmaps"
install -m 755 dist/SyTrakGen "$STAGE/opt/$PKG/SyTrakGen"
ln -s "/opt/$PKG/SyTrakGen" "$STAGE/usr/bin/$PKG"
install -m 644 assets/icons/app.png "$STAGE/usr/share/pixmaps/$PKG.png"

cat > "$STAGE/usr/share/applications/$PKG.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SyTrak Gen
Comment=Read device MAC over J-Link, generate and verify QR labels
Exec=/opt/$PKG/SyTrakGen
Icon=$PKG
Terminal=false
Categories=Utility;Development;
EOF

cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Architecture: $ARCH
Maintainer: KrishWorks <dev.user@krishworks.com>
Section: utils
Priority: optional
Installed-Size: $(du -sk "$STAGE" | cut -f1)
Depends: libx11-6, libxft2, libfontconfig1
Recommends: jlink
Description: SyTrak Gen station
 Reads the nRF52833 MAC over SEGGER J-Link, generates QR labels into a
 Google Sheet and verifies them with the SyTrak Mobile app.
 Needs the SEGGER J-Link Software (JLinkExe) installed separately.
EOF

mkdir -p dist
OUT="dist/${PKG}_${VERSION}_${ARCH}.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$OUT"
echo
echo "Build complete: $PWD/$OUT"
echo "Install:   sudo apt install ./$OUT"
echo "Uninstall: sudo apt remove $PKG"
