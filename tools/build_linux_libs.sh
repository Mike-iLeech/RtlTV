#!/usr/bin/env bash
# Сборка встроенных библиотек SDR4NOOB для Linux (x86_64 или aarch64 — по текущей машине) в rtltv/driver:
#   librtlsdr.so      драйвер RTL2832U, сборка rtl-sdr-blog (с RTL-SDR Blog V4), отцепляет драйвер ядра DVB сам;
#   libusb-1.0.so.0   его зависимость — кладётся рядом (RUNPATH=$ORIGIN), системная не нужна;
#   libmbelib.so      вокодер IMBE/AMBE (голос DMR);
#   liblrpt_dsp.so    демодулятор и декодеры «Метеор-М» LRPT (tools/lrpt_dsp.c).
#   libportaudio.so.2 звук (запасной, если в системе нет libportaudio2).
# Нужны: gcc, cmake, pkg-config, git, patchelf, libusb-1.0-0-dev, libasound2-dev
#   (Ubuntu/Debian: sudo apt install build-essential cmake pkg-config git patchelf libusb-1.0-0-dev libasound2-dev).
# Использование: tools/build_linux_libs.sh [рабочая_папка]   (по умолчанию ${TMPDIR:-/tmp}/sdr4noob-libs)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/rtltv/driver"
WORK="${1:-${TMPDIR:-/tmp}/sdr4noob-libs}"
RTLSDR_REPO=https://github.com/rtlsdrblog/rtl-sdr-blog
RTLSDR_TAG=v1.3.6
MBELIB_REPO=https://github.com/szechyjs/mbelib
MBELIB_TAG=v1.3.0
PORTAUDIO_REPO=https://github.com/PortAudio/portaudio
PORTAUDIO_TAG=v19.7.0
CFLAGS_COMMON="-O2 -fPIC -fvisibility=default"

mkdir -p "$WORK" "$OUT"

fetch() {   # fetch <repo> <tag> <папка>
  if [ ! -d "$3/.git" ]; then
    git clone -q --depth 1 --branch "$2" "$1" "$3"
  fi
}

# ---- librtlsdr (rtl-sdr-blog)
fetch "$RTLSDR_REPO" "$RTLSDR_TAG" "$WORK/rtl-sdr-blog"
cmake -S "$WORK/rtl-sdr-blog" -B "$WORK/rtl-build" -DCMAKE_BUILD_TYPE=Release \
  -DINSTALL_UDEV_RULES=OFF -DDETACH_KERNEL_DRIVER=ON -DCMAKE_SKIP_RPATH=ON >/dev/null
cmake --build "$WORK/rtl-build" --target rtlsdr -j"$(nproc)" >/dev/null
so=$(find "$WORK/rtl-build/src" -maxdepth 1 -name 'librtlsdr.so.*' -type f | sort | tail -n1)
[ -n "$so" ] || { echo "librtlsdr.so не собралась" >&2; exit 1; }
install -m 0755 "$so" "$OUT/librtlsdr.so"
patchelf --set-soname librtlsdr.so --set-rpath '$ORIGIN' "$OUT/librtlsdr.so"

# libusb рядом: файл с soname libusb-1.0.so.0 из системы сборки
usb=$(ldd "$OUT/librtlsdr.so" | awk '/libusb-1.0.so/ {print $3; exit}')
[ -n "$usb" ] && [ -f "$usb" ] || { echo "не найдена libusb-1.0.so.0 (пакет libusb-1.0-0-dev)" >&2; exit 1; }
install -m 0644 "$(readlink -f "$usb")" "$OUT/libusb-1.0.so.0"

# ---- PortAudio для звука (sounddevice): только ALSA — через неё работают и PulseAudio, и PipeWire;
#      берётся, только если в системе пользователя нет своей libportaudio2 (osplat.use_bundled_portaudio)
fetch "$PORTAUDIO_REPO" "$PORTAUDIO_TAG" "$WORK/portaudio"
cmake -S "$WORK/portaudio" -B "$WORK/pa-build" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \
  -DPA_USE_ALSA=ON -DPA_USE_JACK=OFF -DPA_USE_OSS=OFF -DPA_BUILD_SHARED=ON -DPA_BUILD_STATIC=OFF >/dev/null
cmake --build "$WORK/pa-build" -j"$(nproc)" >/dev/null
pa=$(find "$WORK/pa-build" -maxdepth 1 -name 'libportaudio.so*' -type f | sort | tail -n1)
[ -n "$pa" ] || { echo "libportaudio.so не собралась" >&2; exit 1; }
install -m 0755 "$pa" "$OUT/libportaudio.so.2"
patchelf --set-soname libportaudio.so.2 "$OUT/libportaudio.so.2"

# ---- mbelib: только *.c, как tools/build_mbelib.bat
fetch "$MBELIB_REPO" "$MBELIB_TAG" "$WORK/mbelib"
M="$WORK/mbelib"
gcc $CFLAGS_COMMON -shared -Wl,-soname,libmbelib.so -o "$OUT/libmbelib.so" \
  "$M/ecc.c" "$M/mbelib.c" "$M/ambe3600x2400.c" "$M/ambe3600x2450.c" "$M/imbe7100x4400.c" "$M/imbe7200x4400.c" -lm
install -m 0644 "$M/COPYRIGHT" "$OUT/mbelib-LICENSE.txt"

# ---- lrpt_dsp
gcc $CFLAGS_COMMON -ffast-math -shared -Wl,-soname,liblrpt_dsp.so -o "$OUT/liblrpt_dsp.so" "$ROOT/tools/lrpt_dsp.c" -lm

strip --strip-unneeded "$OUT/librtlsdr.so" "$OUT/libmbelib.so" "$OUT/liblrpt_dsp.so" "$OUT/libportaudio.so.2"
echo "OK ($(uname -m)):"
ls -l "$OUT"/*.so*
ldd "$OUT/librtlsdr.so"
