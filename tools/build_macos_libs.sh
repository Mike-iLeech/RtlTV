#!/bin/bash
# Сборка нативных библиотек SDR4NOOB для macOS в rtltv/driver/ (аналог tools/build_*.bat для Windows):
#   librtlsdr.dylib  — драйвер RTL-SDR (rtl-sdr-blog, поддержка V4), libusb вшит статически: Homebrew не нужен;
#   libmbelib.dylib  — голосовой кодек DMR (mbelib);
#   liblrpt_dsp.dylib — Витерби, Рид — Соломон и демодулятор QPSK/OQPSK для «Метеора» (tools/lrpt_dsp.c).
# Библиотеки собираются под архитектуру машины (arm64 на Apple Silicon, x86_64 на Intel) или под ARCH=...;
# ARCH="arm64;x86_64" — универсальные (нужен Xcode с обоими SDK, он есть на раннерах GitHub).
#
#   tools/build_macos_libs.sh            нужны Xcode Command Line Tools, cmake, curl, git
#
# Каждая библиотека получает install name @loader_path/<имя>, поэтому её можно грузить из любого места
# (ctypes, PyInstaller кладёт их в SDR4NOOB.app/Contents/Frameworks/rtltv/driver).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/rtltv/driver"
WORK="${WORK:-${TMPDIR:-/tmp}/sdr4noob-macos-libs}"
ARCH="${ARCH:-$(uname -m)}"
LIBUSB_VER="1.0.30"
RTLSDR_TAG="v1.3.6"
MBELIB_TAG="v1.3.0"
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-14.0}"   # колёса PySide6 6.12 — macosx_14_0

ARCH_FLAGS=""
IFS=';' read -ra _A <<< "$ARCH"
for a in "${_A[@]}"; do ARCH_FLAGS="$ARCH_FLAGS -arch $a"; done
CFLAGS_COMMON="-O2 $ARCH_FLAGS -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"

rm -rf "$WORK"
mkdir -p "$WORK" "$OUT"
cd "$WORK"

# ---------------------------------------------------------------- libusb (статически)
echo "== libusb $LIBUSB_VER"
curl -fsSL -o libusb.tar.bz2 \
  "https://github.com/libusb/libusb/releases/download/v$LIBUSB_VER/libusb-$LIBUSB_VER.tar.bz2"
tar xjf libusb.tar.bz2
PREFIX="$WORK/prefix"
(cd "libusb-$LIBUSB_VER" &&
  ./configure --quiet --prefix="$PREFIX" --disable-shared --enable-static --with-pic --disable-dependency-tracking \
    CFLAGS="$CFLAGS_COMMON" LDFLAGS="$ARCH_FLAGS" &&
  make -s -j"$(sysctl -n hw.ncpu)" && make -s install)

# ---------------------------------------------------------------- librtlsdr (rtl-sdr-blog)
echo "== librtlsdr $RTLSDR_TAG"
git clone -q --depth 1 --branch "$RTLSDR_TAG" https://github.com/rtlsdrblog/rtl-sdr-blog rtl-sdr
# frameworks — зависимости статической libusb (IOKit, CoreFoundation, Security)
FW="-framework IOKit -framework CoreFoundation -framework Security -lobjc"
# pkg-config отключён: иначе CMake подставит «-lusb-1.0» без пути, и линкер может взять libusb из Homebrew
cmake -S rtl-sdr -B rtl-build -DCMAKE_BUILD_TYPE=Release -DCMAKE_DISABLE_FIND_PACKAGE_PkgConfig=TRUE \
  -DCMAKE_OSX_ARCHITECTURES="$ARCH" -DCMAKE_OSX_DEPLOYMENT_TARGET="$MACOSX_DEPLOYMENT_TARGET" \
  -DINSTALL_UDEV_RULES=OFF -DDETACH_KERNEL_DRIVER=OFF \
  -DLIBUSB_INCLUDE_DIRS="$PREFIX/include/libusb-1.0" -DLIBUSB_LIBRARIES="$PREFIX/lib/libusb-1.0.a" \
  -DCMAKE_SHARED_LINKER_FLAGS="$FW" -DCMAKE_EXE_LINKER_FLAGS="$FW" >/dev/null
cmake --build rtl-build --target rtlsdr -j"$(sysctl -n hw.ncpu)" >/dev/null
SRC_LIB="$(find rtl-build -name 'librtlsdr*.dylib' -type f | head -1)"
[ -n "$SRC_LIB" ] || { echo "librtlsdr.dylib не собралась"; exit 1; }
cp "$SRC_LIB" "$OUT/librtlsdr.dylib"
install_name_tool -id "@loader_path/librtlsdr.dylib" "$OUT/librtlsdr.dylib"
if otool -L "$OUT/librtlsdr.dylib" | grep -q "libusb"; then
  echo "librtlsdr.dylib ссылается на внешнюю libusb — должна быть вшита статически"; otool -L "$OUT/librtlsdr.dylib"; exit 1
fi
cp rtl-sdr/COPYING "$OUT/librtlsdr-LICENSE.txt"
cp "libusb-$LIBUSB_VER/COPYING" "$OUT/libusb-LICENSE.txt"   # LGPL-2.1: libusb вшита в librtlsdr.dylib

# ---------------------------------------------------------------- mbelib
echo "== mbelib $MBELIB_TAG"
git clone -q --depth 1 --branch "$MBELIB_TAG" https://github.com/szechyjs/mbelib mbelib
(cd mbelib && cc $CFLAGS_COMMON -dynamiclib -fvisibility=default -install_name "@loader_path/libmbelib.dylib" \
  -o "$OUT/libmbelib.dylib" ecc.c mbelib.c ambe3600x2400.c ambe3600x2450.c imbe7100x4400.c imbe7200x4400.c -lm)
cp mbelib/COPYRIGHT "$OUT/mbelib-LICENSE.txt"

# ---------------------------------------------------------------- lrpt_dsp
echo "== lrpt_dsp"
cc $CFLAGS_COMMON -ffast-math -dynamiclib -install_name "@loader_path/liblrpt_dsp.dylib" \
  -o "$OUT/liblrpt_dsp.dylib" "$ROOT/tools/lrpt_dsp.c"

# Подпись ad-hoc: на Apple Silicon неподписанный код не запускается. Настоящую подпись ставит сборка .app.
for f in librtlsdr libmbelib liblrpt_dsp; do
  codesign --force --sign - "$OUT/$f.dylib"
  lipo -info "$OUT/$f.dylib"
done
echo "OK: $OUT"
