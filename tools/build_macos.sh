#!/bin/bash
# Сборка SDR4NOOB для macOS: dist/SDR4NOOB.app → release/SDR4NOOB-<версия>-macos-<arch>.dmg (первая установка)
# и .zip (для автообновления) + файлы .sha256. Аналог build.bat для Windows.
#
#   python3 -m pip install -r requirements-dev.txt
#   tools/build_macos_libs.sh          # librtlsdr/libmbelib/liblrpt_dsp.dylib в rtltv/driver
#   tools/build_macos.sh
#
# Подпись. Без переменных окружения пакет подписывается ad-hoc (запускается на этом Mac; на другом Gatekeeper
# попросит «Открыть» через правую кнопку / «Системные настройки → Конфиденциальность и безопасность»).
# Для раздачи без предупреждений нужен сертификат Developer ID (Apple Developer Program):
#   MACOS_SIGN_IDENTITY="Developer ID Application: Имя (TEAMID)"  — подпись с hardened runtime
#                                                                 и installer/macos/entitlements.plist;
#   MACOS_NOTARY_PROFILE=<профиль xcrun notarytool store-credentials> — ещё и нотаризация dmg + staple.
# В CI сертификат импортируется из секретов (см. .github/workflows/release.yml, шаг «Подпись macOS»).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
VER="$("$PY" -c 'import rtltv; print(rtltv.__version__)')"
ARCH="$("$PY" -c 'import platform; m = platform.machine().lower(); print("arm64" if m in ("arm64", "aarch64") else "x86_64")')"
APP="dist/SDR4NOOB.app"
ENT="installer/macos/entitlements.plist"
NAME="SDR4NOOB-$VER-macos-$ARCH"

for f in librtlsdr libmbelib liblrpt_dsp; do
  [ -f "rtltv/driver/$f.dylib" ] || { echo "Нет rtltv/driver/$f.dylib — сначала tools/build_macos_libs.sh"; exit 1; }
done

# Библиотеки — как двоичные файлы (PyInstaller кладёт их в Contents/Frameworks и подписывает), Windows-DLL не берём.
"$PY" -m PyInstaller --noconfirm --clean --windowed --name SDR4NOOB \
  --osx-bundle-identifier io.github.mike-ileech.sdr4noob \
  --collect-submodules rtltv \
  --add-binary "rtltv/driver/*.dylib:rtltv/driver" \
  --add-data "rtltv/driver/*.txt:rtltv/driver" \
  --add-data "rtltv/guide:rtltv/guide" \
  --add-data "rtltv/assets:rtltv/assets" \
  --icon "rtltv/assets/sdr4noob.icns" \
  --hidden-import PySide6.QtSvg \
  --hidden-import PySide6.QtWebEngineWidgets \
  --exclude-module tkinter \
  run.py

PL="$APP/Contents/Info.plist"
plutil -replace CFBundleDisplayName -string "SDR4NOOB" "$PL"
plutil -replace CFBundleShortVersionString -string "$VER" "$PL"
plutil -replace CFBundleVersion -string "$VER" "$PL"
plutil -replace LSMinimumSystemVersion -string "12.0" "$PL"
plutil -replace NSHighResolutionCapable -bool true "$PL"
plutil -replace LSApplicationCategoryType -string "public.app-category.utilities" "$PL"
plutil -replace NSHumanReadableCopyright -string "SDR4NOOB, GPL-3.0-or-later" "$PL"
# тексты запросов доступа macOS (папка данных по умолчанию — ~/Documents/SDR4NOOB; rtl_tcp — по локальной сети)
plutil -replace NSDocumentsFolderUsageDescription -string \
  "SDR4NOOB хранит записи, снимки, звук и журналы в папке «Документы/SDR4NOOB» (её можно сменить в настройках)." "$PL"
plutil -replace NSLocalNetworkUsageDescription -string \
  "Подключение к сетевому приёмнику SDR (rtl_tcp) в вашей локальной сети." "$PL"
for f in LICENSE THIRD_PARTY.md README.md CHANGELOG.md; do cp "$f" "$APP/Contents/Resources/"; done

# Info.plist и Resources изменены после PyInstaller — подпись пакета ставится заново
if [ -n "${MACOS_SIGN_IDENTITY:-}" ]; then
  echo "== подпись: $MACOS_SIGN_IDENTITY"
  codesign --force --deep --timestamp --options runtime --entitlements "$ENT" --sign "$MACOS_SIGN_IDENTITY" "$APP"
else
  echo "== подпись ad-hoc (без сертификата Developer ID)"
  codesign --force --deep --sign - "$APP"
fi
codesign --verify --deep --strict "$APP"

mkdir -p release
rm -f "release/$NAME.zip" "release/$NAME.dmg"
ditto -c -k --sequesterRsrc --keepParent "$APP" "release/$NAME.zip"

STAGE="$(mktemp -d)"
ditto "$APP" "$STAGE/SDR4NOOB.app"
ln -s /Applications "$STAGE/Программы"
for i in 1 2 3 4 5; do   # hdiutil на раннерах иногда отвечает «Resource busy»
  if hdiutil create -quiet -volname "SDR4NOOB $VER" -srcfolder "$STAGE" -ov -format UDZO "release/$NAME.dmg"; then
    break
  fi
  [ "$i" = 5 ] && exit 1
  sleep 5
done
rm -rf "$STAGE"
if [ -n "${MACOS_SIGN_IDENTITY:-}" ]; then
  codesign --force --timestamp --sign "$MACOS_SIGN_IDENTITY" "release/$NAME.dmg"
  if [ -n "${MACOS_NOTARY_PROFILE:-}" ]; then
    echo "== нотаризация"
    xcrun notarytool submit "release/$NAME.dmg" --keychain-profile "$MACOS_NOTARY_PROFILE" --wait
    xcrun stapler staple "release/$NAME.dmg"
  fi
fi

for f in "$NAME.zip" "$NAME.dmg"; do
  (cd release && shasum -a 256 "$f" > "$f.sha256")
done
ls -la release
echo "OK: release/$NAME.dmg"
