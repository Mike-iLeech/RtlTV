# Сторонние компоненты SDR4NOOB

SDR4NOOB распространяется под лицензией **GPL-3.0-or-later** (файл `LICENSE`). В сборку для Windows
(`SDR4NOOB-<версия>-win64.zip`) входят перечисленные ниже компоненты; их лицензии совместимы с GPL-3.0.
Полные тексты лицензий — в исходных репозиториях компонентов по ссылкам ниже.

**Исходный код.** Исходники SDR4NOOB — в этом репозитории (тег версии `vX.Y.Z` соответствует релизу).
Для librtlsdr (GPL-2.0) и libusb (LGPL-2.1) в релизах используется неизменённая сборка по ссылкам ниже;
если ссылка перестанет работать, точные исходники этой сборки можно запросить через
[Issues](https://github.com/Mike-iLeech/SDR4NOOB/issues) — предложение действует три года с даты последнего релиза.

## Библиотеки в rtltv/driver

| Компонент | Лицензия | Исходники | Примечание |
|---|---|---|---|
| **librtlsdr** (`librtlsdr.dll`), сборка rtl-sdr-blog | GPL-2.0-or-later | https://github.com/rtlsdrblog/rtl-sdr-blog | Драйвер RTL2832U с поддержкой RTL-SDR Blog V4; libusb статически внутри DLL (libusb — LGPL-2.1-or-later, https://github.com/libusb/libusb). DLL взята без изменений из пакета [pyrtlsdrlib 0.0.7](https://pypi.org/project/pyrtlsdrlib/0.0.7/) (файл `librtlsdr_w64_static.dll`, сборка `rtlsdr-bin-w64_static` v0.9.0); исходники и сценарий сборки — https://github.com/pyrtlsdr/pyrtlsdrlib и https://github.com/rtlsdrblog/rtl-sdr-blog. |
| **mbelib** (`mbelib.dll`) | ISC (`rtltv/driver/mbelib-LICENSE.txt`) | https://github.com/szechyjs/mbelib | Вокодер IMBE/AMBE для голоса DMR. Собирается `tools/build_mbelib.bat`. См. оговорку о патентах ниже. |
| **lrpt_dsp** (`lrpt_dsp.dll`) | GPL-3.0-or-later (часть SDR4NOOB) | `tools/lrpt_dsp.c` | Свой код: демодулятор QPSK/OQPSK, Витерби, Рид — Соломон для «Метеор-М» LRPT. Собирается `tools/build_lrpt_dsp.bat`. |

### Сборки для Linux

В AppImage и пакете .deb вместо DLL — библиотеки `.so`, собранные `tools/build_linux_libs.sh` в CI из исходников:

| Компонент | Лицензия | Исходники |
|---|---|---|
| **librtlsdr** (`librtlsdr.so`), rtl-sdr-blog v1.3.6 | GPL-2.0-or-later | https://github.com/rtlsdrblog/rtl-sdr-blog/tree/v1.3.6 |
| **libusb** (`libusb-1.0.so.0`) — из пакета `libusb-1.0-0` Ubuntu сборочной машины, без изменений | LGPL-2.1-or-later | https://github.com/libusb/libusb , исходный пакет Ubuntu `libusb-1.0` |
| **mbelib** (`libmbelib.so`), v1.3.0 | ISC | https://github.com/szechyjs/mbelib/tree/v1.3.0 |
| **lrpt_dsp** (`liblrpt_dsp.so`) | GPL-3.0-or-later | `tools/lrpt_dsp.c` |
| **PortAudio** (`libportaudio.so.2`), v19.7.0, только ALSA; используется, если в системе нет своей | MIT | https://github.com/PortAudio/portaudio/tree/v19.7.0 |

Библиотеки Qt и прочие зависимости PySide6 в сборке для Linux — из колёс PyPI (как и в Windows) и подключаются
динамически: их можно заменить своей сборкой.

### Сборка для macOS

В `SDR4NOOB.app` (образы `SDR4NOOB-<версия>-macos-<arm64|x86_64>.dmg` и `.zip`) те же компоненты собраны для
macOS из исходников в CI скриптом `tools/build_macos_libs.sh` (лежат в `Contents/Frameworks/rtltv/driver`):

| Компонент | Лицензия | Исходники | Примечание |
|---|---|---|---|
| **librtlsdr** (`librtlsdr.dylib`) | GPL-2.0-or-later (`librtlsdr-LICENSE.txt`) | https://github.com/rtlsdrblog/rtl-sdr-blog, тег v1.3.6 | Без изменений исходников; libusb вшита статически. |
| **libusb** (внутри `librtlsdr.dylib`) | LGPL-2.1-or-later (`libusb-LICENSE.txt`) | https://github.com/libusb/libusb, выпуск 1.0.30 | Без изменений. Чтобы заменить libusb своей сборкой, пересоберите librtlsdr тем же скриптом (`LIBUSB_VER`). |
| **mbelib** (`libmbelib.dylib`) | ISC (`mbelib-LICENSE.txt`) | https://github.com/szechyjs/mbelib, тег v1.3.0 | Без изменений. |
| **lrpt_dsp** (`liblrpt_dsp.dylib`) | GPL-3.0-or-later (часть SDR4NOOB) | `tools/lrpt_dsp.c` | Свой код. |

### Оговорка о патентах AMBE

Алгоритмы вокодеров AMBE/AMBE+2, которые реализует mbelib, в некоторых странах могут быть защищены
патентами. Авторы mbelib и SDR4NOOB не дают никаких гарантий и не предоставляют патентных лицензий.
Функция декодирования голоса DMR предназначена для исследовательских и учебных целей;
**пользуйтесь ею на свой страх и риск** и с учётом законов вашей страны.

## Android-версия

В `SDR4NOOB-<версия>-android.apk` входят собранные из исходников при сборке APK (`android/app/build.gradle.kts`,
задача `fetchNativeDeps`, архивы сверяются по SHA-256) и статически скомпонованные в `libsdr4noob.so`:

| Компонент | Лицензия | Исходники | Примечание |
|---|---|---|---|
| **librtlsdr**, сборка rtl-sdr-blog 1.4.0 | GPL-2.0-or-later | https://github.com/rtlsdrblog/rtl-sdr-blog/archive/refs/tags/V1.4.0.tar.gz | Исходники не изменены; открытие по файловому дескриптору Android — прослойкой `android/app/src/main/cpp/usb_fd_shim.[ch]` (часть SDR4NOOB, GPL-3.0-or-later). |
| **libusb** 1.0.30 | LGPL-2.1-or-later | https://github.com/libusb/libusb/releases/download/v1.0.30/libusb-1.0.30.tar.bz2 | Без изменений. Вся программа открыта (GPL-3.0), поэтому требование LGPL о возможности пересборки с другой libusb выполняется: замените архив в `fetchNativeDeps` и соберите APK. |

Остальное в APK — код SDR4NOOB (Kotlin, GPL-3.0-or-later) и руководство из `rtltv/guide`. Сторонних
библиотек Java/Kotlin, кроме стандартной библиотеки Kotlin (Apache-2.0, https://kotlinlang.org/), нет.

## Python и библиотеки Python

| Компонент | Лицензия | Сайт |
|---|---|---|
| CPython 3.12 | PSF License 2.0 | https://www.python.org/ |
| Qt 6 / PySide6 (PySide6-Essentials, shiboken6) | LGPL-3.0 (Qt и PySide6 — также GPL; используются по LGPL) | https://www.qt.io/ , https://wiki.qt.io/Qt_for_Python |
| Qt WebEngine (PySide6-WebEngine, PySide6-Addons) — встроенный браузер справки на основе Chromium | LGPL-3.0; Chromium и его компоненты — BSD-3-Clause и др. (полный список — `qtwebengine_resources` и https://doc.qt.io/qt-6/qtwebengine-licensing.html) | https://doc.qt.io/qt-6/qtwebengine-index.html |
| NumPy | BSD-3-Clause | https://numpy.org/ |
| SciPy | BSD-3-Clause | https://scipy.org/ |
| sgp4 | MIT | https://github.com/brandon-rhodes/python-sgp4 |
| python-sounddevice | MIT | https://github.com/spatialaudio/python-sounddevice |
| PortAudio (внутри sounddevice) | MIT | https://www.portaudio.com/ |
| PyInstaller (только для сборки) | GPL-2.0-or-later с исключением для загрузчика: собранные программы могут распространяться под любой лицензией | https://pyinstaller.org/ |

Qt и PySide6 подключаются динамически (DLL и модули в `_internal`; на macOS — фреймворки в
`SDR4NOOB.app/Contents/Frameworks`), поэтому их можно заменить своей
совместимой сборкой, как того требует LGPL-3.0. Исходники Qt: https://download.qt.io/ ,
PySide6: https://code.qt.io/cgit/pyside/pyside-setup.git/ .

NumPy и SciPy в колёсах PyPI содержат OpenBLAS (BSD-3-Clause) и библиотеки времени выполнения GCC/gfortran
(GPL-3.0 с исключением GCC Runtime Library Exception) — см. файлы лицензий внутри пакетов.

## Данные и онлайн-сервисы

SDR4NOOB обращается к этим сервисам только при необходимости и кэширует ответы на диске
(`%LOCALAPPDATA%\SDR4NOOB`). Данные принадлежат их владельцам.

| Данные | Источник | Условия |
|---|---|---|
| Тайлы карты | OpenStreetMap, `tile.openstreetmap.org` | Данные © участники OpenStreetMap, лицензия ODbL 1.0 (https://www.openstreetmap.org/copyright). Подпись «© участники OpenStreetMap» выводится на карте. Использование тайлов — по политике OSMF (https://operations.osmfoundation.org/policies/tiles/): кэш на диске, умеренная загрузка, своя строка User-Agent. |
| Орбиты (TLE / GP) | CelesTrak, `celestrak.org` | Публичные данные CelesTrak (https://celestrak.org/); обновляются не чаще, чем нужно. |
| Сведения о бортах и рейсах | adsbdb (`api.adsbdb.com`), hexdb.io (`hexdb.io`) | Бесплатные публичные API; см. условия на https://www.adsbdb.com/ и https://hexdb.io/ . |
| Флаги стран | flagcdn (`flagcdn.com`) | Изображения флагов flagcdn (https://flagcdn.com/); флаги государств — общественное достояние. |
| Проверка обновлений | GitHub API (`api.github.com`) | Только чтение публичной информации о выпусках репозитория SDR4NOOB. |

## Инструменты

- **Zadig** (https://zadig.akeo.ie/, GPL-3.0) — не входит в SDR4NOOB; пользователь скачивает его сам для
  установки драйвера WinUSB.
- **SatDump** (https://github.com/SatDump/SatDump, GPL-3.0) — не входит в SDR4NOOB; использовался как эталон
  параметров LRPT/LRIT и для сверки результатов декодирования.
