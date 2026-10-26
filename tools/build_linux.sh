#!/usr/bin/env bash
# Сборка SDR4NOOB для Linux: dist/SDR4NOOB (PyInstaller) → release/SDR4NOOB-<версия>-<arch>.AppImage (+ .zsync)
# и release/sdr4noob_<версия>_<amd64|arm64>.deb, у каждого файл .sha256.
# Сначала: tools/build_linux_libs.sh (встроенные .so в rtltv/driver) и зависимости из requirements-dev.txt.
# Нужны: appimagetool (переменная APPIMAGETOOL или в PATH), dpkg-deb, zsyncmake (для .zsync, по желанию).
# Использование: PYTHON=python3 tools/build_linux.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
VER="$($PY -c 'import rtltv; print(rtltv.__version__)')"
ARCH="$(uname -m)"                                   # x86_64 / aarch64
case "$ARCH" in
  x86_64) DEBARCH=amd64 ;;
  aarch64) DEBARCH=arm64 ;;
  *) echo "архитектура $ARCH не поддерживается" >&2; exit 1 ;;
esac
APPIMAGETOOL="${APPIMAGETOOL:-appimagetool}"
OUT="$ROOT/release"
mkdir -p "$OUT"

for f in librtlsdr.so libusb-1.0.so.0 libmbelib.so liblrpt_dsp.so libportaudio.so.2; do
  [ -f "rtltv/driver/$f" ] || { echo "нет rtltv/driver/$f — сначала tools/build_linux_libs.sh" >&2; exit 1; }
done

# ---- PyInstaller (DLL Windows в сборку Linux не нужны)
rm -rf build/linux-driver
mkdir -p build/linux-driver
cp rtltv/driver/*.so rtltv/driver/*.so.* rtltv/driver/*.txt build/linux-driver/
$PY -m PyInstaller --noconfirm --clean --windowed --name SDR4NOOB \
  --collect-submodules rtltv \
  --add-data "build/linux-driver:rtltv/driver" \
  --add-data "rtltv/guide:rtltv/guide" \
  --add-data "rtltv/assets:rtltv/assets" \
  --hidden-import PySide6.QtSvg \
  --hidden-import PySide6.QtWebEngineWidgets \
  --exclude-module tkinter \
  run.py
$PY tools/prune_webengine.py dist/SDR4NOOB
cp LICENSE THIRD_PARTY.md README.md CHANGELOG.md dist/SDR4NOOB/

write_udev() {   # write_udev <папка для rules> <папка для modprobe.d>
  mkdir -p "$1" "$2"
  $PY -c 'import sys, rtltv.linux_setup as l; open(sys.argv[1], "w").write(l.udev_rules_text()); open(sys.argv[2], "w").write(l.blacklist_text())' \
    "$1/60-sdr4noob.rules" "$2/blacklist-sdr4noob.conf"
}

install_desktop() {   # install_desktop <корень usr>
  install -Dm0644 packaging/linux/sdr4noob.desktop "$1/share/applications/sdr4noob.desktop"
  install -Dm0644 packaging/linux/io.github.mike_ileech.SDR4NOOB.metainfo.xml \
    "$1/share/metainfo/io.github.mike_ileech.SDR4NOOB.metainfo.xml"
  install -Dm0644 rtltv/assets/sdr4noob.svg "$1/share/icons/hicolor/scalable/apps/sdr4noob.svg"
  install -Dm0644 rtltv/assets/sdr4noob-256.png "$1/share/icons/hicolor/256x256/apps/sdr4noob.png"
}

# ---- AppImage
APPDIR=build/AppDir
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/bin"
cp -a dist/SDR4NOOB "$APPDIR/usr/lib/sdr4noob"
ln -s ../lib/sdr4noob/SDR4NOOB "$APPDIR/usr/bin/sdr4noob"
install_desktop "$APPDIR/usr"
cp packaging/linux/sdr4noob.desktop "$APPDIR/sdr4noob.desktop"
cp rtltv/assets/sdr4noob-256.png "$APPDIR/sdr4noob.png"
ln -s sdr4noob.png "$APPDIR/.DirIcon"
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/sdr4noob/SDR4NOOB" "$@"
EOF
chmod 0755 "$APPDIR/AppRun"
IMG="$OUT/SDR4NOOB-$VER-$ARCH.AppImage"
rm -f "$IMG" "$IMG.zsync"
UPD="gh-releases-zsync|Mike-iLeech|SDR4NOOB|latest|SDR4NOOB-*-$ARCH.AppImage.zsync"
( cd "$OUT" && ARCH="$ARCH" APPIMAGE_EXTRACT_AND_RUN=1 "$APPIMAGETOOL" --no-appstream -u "$UPD" \
    "$ROOT/$APPDIR" "$(basename "$IMG")" )
chmod 0755 "$IMG"

# ---- .deb: программа в /opt/sdr4noob, команда sdr4noob, правило udev и запрет драйвера DVB ядра
DEB=build/deb
rm -rf "$DEB"
mkdir -p "$DEB/DEBIAN" "$DEB/opt" "$DEB/usr/bin"
cp -a dist/SDR4NOOB "$DEB/opt/sdr4noob"
ln -s /opt/sdr4noob/SDR4NOOB "$DEB/usr/bin/sdr4noob"
install_desktop "$DEB/usr"
write_udev "$DEB/usr/lib/udev/rules.d" "$DEB/usr/lib/modprobe.d"
SIZE_KB=$(du -sk "$DEB" | cut -f1)
GLIBC=$(ldd --version | head -n1 | grep -oE '[0-9]+\.[0-9]+$')   # Qt из PyPI: x86_64 — от 2.34, aarch64 — от 2.39
cat > "$DEB/DEBIAN/control" <<EOF
Package: sdr4noob
Version: $VER
Architecture: $DEBARCH
Maintainer: Mike-iLeech <mike-ileech@users.noreply.github.com>
Installed-Size: $SIZE_KB
Depends: libc6 (>= $GLIBC), libasound2 | libasound2t64, libgl1, libegl1, libfontconfig1, libxkbcommon0, libnss3, libdbus-1-3
Recommends: libportaudio2, libxcb-cursor0, libhackrf0, libairspy0
Section: hamradio
Priority: optional
Homepage: https://mike-ileech.github.io/SDR4NOOB/
Description: All-in-one for the beginner radio amateur on any USB radio receiver (SDR)
 TV, FM radio with RDS, SSTV, weather and amateur satellites, ADS-B and APRS
 on any USB radio receiver (SDR). The RTL-SDR driver is built in; HackRF and
 Airspy use the system libraries (libhackrf0, libairspy0).
EOF
cat > "$DEB/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
  udevadm control --reload-rules 2>/dev/null || true
  udevadm trigger --subsystem-match=usb 2>/dev/null || true
  for m in dvb_usb_rtl28xxu rtl2832_sdr rtl2832 rtl2830; do modprobe -r "$m" 2>/dev/null || true; done
  command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
  command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
fi
exit 0
EOF
cat > "$DEB/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
udevadm control --reload-rules 2>/dev/null || true
exit 0
EOF
chmod 0755 "$DEB/DEBIAN/postinst" "$DEB/DEBIAN/postrm"
DEBFILE="$OUT/sdr4noob_${VER}_${DEBARCH}.deb"
dpkg-deb --root-owner-group -Zxz --build "$DEB" "$DEBFILE"

cd "$OUT"
for f in "$(basename "$IMG")" "$(basename "$DEBFILE")"; do
  sha256sum "$f" > "$f.sha256"
done
ls -l "$OUT"
