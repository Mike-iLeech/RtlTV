# SDR4NOOB для Android

Android-версия SDR4NOOB для **любого USB радиоприёмника (SDR)**, подключённого к телефону через OTG.
Для приёмников на RTL2832U (например, RTL-SDR Blog V4/V3 и DVB-T донглы) драйвер встроен; другие USB
радиоприёмники программа узнаёт, драйверы для них — в плане (см. ниже). Без приёмника работают сетевой
`rtl_tcp` и тестовый сигнал.

Что есть сейчас: выбор приёмника с запросом разрешения USB, спектр и водопад, радио WFM (стерео, RDS) /
NFM / AM / USB / LSB / CW с АРУ и шумодавом, полоса канала, усиление, bias-T, ppm, полоса обзора 0,96–2,88 МГц,
менеджер частот (CSV/XML SDR#, как на ПК), запись звука и IQ в WAV, приём в фоне (сервис переднего плана),
DMR (журнал вызовов, голос через mbelib) и поиск DMR, воспроизведение записей IQ,
руководство для начинающих (то же, что в Windows-версии, вместе с интерактивной схемой), проверка обновлений.

## Архитектура и почему она такая

Рассмотренные варианты:

| Вариант | Плюсы | Минусы |
|---|---|---|
| PySide6 for Android (`pyside6-android-deploy`) | общий Python-код с Windows | статус технического превью; колёса NumPy/SciPy под Android и сборка через python-for-android — ручные и хрупкие; доступа к `UsbManager` из Python нет (нужен свой JNI-мост); APK > 100 МБ; QtWebEngine под Android нет |
| Kivy + python-for-android (+ pyjnius) | Python, рецепты NumPy есть | весь интерфейс переписывать всё равно (Kivy ≠ Qt); SciPy под p4a практически недоступен (а DSP на нём); сборка buildozer в CI долгая и ломкая |
| Python в Kotlin-приложении (Chaquopy) | переиспользование части `rtltv` | интерфейс всё равно нативный; SciPy нет; поток 2,4 MS/s через JNI → Python → обратно; размер и скорость |
| **Нативное Kotlin + NDK (выбран)** | штатный инструментарий Android, быстрая надёжная сборка Gradle в CI, APK ≈ 2 МБ, прямой доступ к USB Host API; драйвер — тот же librtlsdr (rtl-sdr-blog), что в Windows-версии | DSP переносится с Python на Kotlin (по образцу `rtltv/radio.py`) |

Так же устроены зрелые SDR-приложения под Android (SDR++ Android, rtl_tcp_andro, RF Analyzer):
нативный код для драйвера, свой интерфейс. Python-пакет `rtltv` на телефоне не запускается, а Windows-
сборка от Android-части не зависит.

Что общего с настольной версией:
- **версия** — одна, из `rtltv/__init__.py` (versionName; versionCode = X·10000 + Y·100 + Z);
- **справка** — `rtltv/guide` копируется в APK при сборке (цвета SVG-схем подставляются из темы «Тёмная»
  `rtltv/themes.py`, как это делает окно справки на ПК);
- **логотип** — `rtltv/assets/sdr4noob-256.png`;
- **драйвер RTL2832U** — librtlsdr из rtl-sdr-blog (на Windows — та же сборка в DLL);
- **алгоритмы** — перенос `rtltv/radio.py` (дециматоры, WFM-стерео с пилотом «фазором», NFM, AM, спектр dBFS),
  `rtltv/devices.py` (протокол rtl_tcp, список известных USB-приёмников).

```
android/app/src/main/
  cpp/                      нативная часть (CMake)
    usb_fd_shim.[ch]        открытие любого драйвера на libusb по файловому дескриптору
    sdr4noob_jni.c          JNI к librtlsdr: поток rtlsdr_read_async → кольцевой буфер → Kotlin
    mbe_jni.c               JNI к mbelib (вокодер AMBE+2 для голоса DMR)
    hackrf_airspy_jni.c     JNI к libhackrf и libairspy (sdr_ring.h — кольцевой буфер отсчётов float)
  java/io/github/mikeileech/sdr4noob/
    sdr/                    приёмники: SdrSource, RtlUsbSource, UsbSources (HackRF, Airspy), RtlTcpSource,
                            DemoSource, IqFile (записи IQ),
                            UsbReceivers, Receivers
    dsp/                    Filters (КИХ-дециматоры, биквады, деэмфазис, смеситель), Spectrum (БПФ), RadioDemod,
                            Rds (декодер и кодер RDS), Agc (АРУ как в SDR#, комплексный КИХ),
                            DmrCodes/Dmr (декодер и генератор DMR), DmrScan (поиск DMR), Ambe (mbelib)
    radio/                  RadioEngine (поток чтения → спектр → демодулятор → звук), AudioOut (AudioTrack),
                            RadioService (приём в фоне), FreqMan (менеджер частот), Recorder (WAV, «Загрузки»)
    ui/                     MainActivity (лаунчер), RadioActivity, SpectrumView, GuideActivity (WebView)
    update/                 UpdateCheck (GitHub releases/latest → ссылка на APK)
  assets/android/           страница справки об Android-версии
```

### USB: разрешение пользователя и дескриптор

На Android приложение не может само перебрать `/dev/bus/usb`. Порядок такой:

1. `UsbManager.deviceList` → известные VID/PID (`sdr/UsbReceivers.kt`, `res/xml/usb_receivers.xml` — для
   автозапуска при подключении приёмника со встроенным драйвером);
2. `UsbManager.requestPermission` — пользователь разрешает доступ (или уже разрешил, выбрав SDR4NOOB при подключении);
3. `UsbManager.openDevice(device).fileDescriptor` → JNI;
4. в нативной части libusb собрана с `LIBUSB_OPTION_NO_DEVICE_DISCOVERY` и принимает дескриптор через
   `libusb_wrap_sys_device()`.

Библиотеки драйверов (librtlsdr, libhackrf, …) ищут устройство сами (`libusb_get_device_list` →
`libusb_open`). Чтобы не патчить их исходники, `usb_fd_shim.h` подключается к ним раньше всего
(`-include`) и подменяет эти вызовы: список состоит из одного «обёрнутого» устройства, `libusb_open` отдаёт уже
открытый дескриптор. librtlsdr собирается из неизменённых исходников rtl-sdr-blog 1.4.0.

### Как добавить драйвер другого USB-приёмника

1. Скачать исходники в `fetchNativeDeps` (`app/build.gradle.kts`, URL + SHA-256).
2. Собрать библиотеку в `CMakeLists.txt` с `-include usb_fd_shim.h` (для libhackrf) или вызвать её
   функцию открытия по fd (у libairspy есть `airspy_open_fd`).
3. JNI-обёртку по образцу `sdr4noob_jni.c` (поток чтения → кольцевой буфер; формат отсчётов → float ±1).
4. Класс `SdrSource` и ветку в `Receivers.sourceFor`; в `UsbReceivers` отметить драйвер встроенным и добавить
   VID/PID в `usb_receivers.xml` (тест `attachFilterMatchesBuiltInDrivers` проверяет согласованность).

## План переноса режимов

| Очередь | Что | Откуда переносить | Состояние |
|---|---|---|---|
| 1 | RDS, SSB/CW, АРУ, менеджер частот, запись IQ/звука | `rds.py`, `radio.py`, `freqman.py`, `iqfile.py` | готово (3.6.0) |
| 1 | Приём в фоне при выключенном экране (foreground-сервис mediaPlayback) | — | готово (3.6.0) |
| 1б | Поиск DMR и голос DMR (mbelib через NDK), воспроизведение IQ-файлов | `dmr.py`, `dmrscan.py`, `iqfile.py` | готово (3.7.0) |
| 2 | Драйверы HackRF (libhackrf через fd-прослойку) и Airspy (`airspy_open_fd`) | `devices.py` | готово (3.7.0) |
| 2 | APRS (AFSK 1200, карта OSM) | `aprs.py`, `mapview.py` | в планах |
| 3 | ADS-B на карте (нужна полоса 2 MS/s и демодулятор PPM) | `adsb.py`, `aircraft_info.py` | в планах |
| 3 | SSTV | `sstv.py` | в планах |
| 4 | Спутники: пролёты (SGP4), приём с доплером, «Метеор» LRPT (тяжёлый DSP — в C через NDK, как `lrpt_dsp.c`) | `satpass.py`, `satrx.py`, `lrpt.py` | в планах |
| 4 | ТВ-измеритель | `digital.py`, `analog.py` | в планах |
| — | Английский интерфейс (как i18n настольной версии), настройки и папка данных | `i18n.py`, `paths.py` | в планах |

## Сборка

Нужны JDK 17 и Android SDK (platform 35, build-tools 35.0.0, NDK 27.2.12479018, CMake 3.22.1):

```bash
cd android
./gradlew testDebugUnitTest assembleDebug     # APK: app/build/outputs/apk/debug/app-debug.apk
```

Исходники libusb 1.0.30, rtl-sdr-blog 1.4.0, mbelib (коммит 9a04ed5), hackrf 2024.02.1 и airspyone_host
(коммит fc61ab6) сборка скачивает сама в `android/deps` и сверяет SHA-256.

**Релиз.** Тег `vX.Y.Z` запускает `.github/workflows/android.yml`: тесты, сборка, подпись ключом из секретов
репозитория `ANDROID_KEYSTORE_B64` / `ANDROID_KEYSTORE_PASS` (алиас `sdr4noob`) и публикация
`SDR4NOOB-X.Y.Z-android.apk` + `.sha256` в тот же релиз, что и Windows-архивы. Релиз создаёт `release.yml`
после проверки Windows-сборки (иначе обновлятор Windows увидел бы новую версию без архива), Android-job ждёт его
и догружает APK. Без секретов APK не публикуется (обновить APK, подписанный другим ключом, поверх нельзя). На PR APK лежит в артефактах прогона
(в PR из форков секретов нет — там он подписан отладочным ключом).
