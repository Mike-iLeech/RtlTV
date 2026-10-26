# Нативная часть SDR4NOOB для Android:
#   usb-1.0   — libusb (без поиска устройств: на Android их открывает приложение через UsbManager);
#   rtlsdr    — librtlsdr сборки rtl-sdr-blog, открывается по файловому дескриптору (usb_fd_shim);
#   sdr4noob  — JNI-обёртка для Kotlin (sdr/RtlSdrNative.kt, dsp/Ambe.kt, lrpt/LrptNative.kt).
# Исходники libusb и librtlsdr скачивает задача Gradle fetchNativeDeps в SDR4N_DEPS.
cmake_minimum_required(VERSION 3.22)
project(sdr4noob C)

if(NOT SDR4N_DEPS OR NOT EXISTS "${SDR4N_DEPS}/libusb-1.0.30/libusb/core.c")
  message(FATAL_ERROR "Нет исходников драйвера в '${SDR4N_DEPS}': запустите сборку через Gradle (задача fetchNativeDeps)")
endif()
set(LIBUSB "${SDR4N_DEPS}/libusb-1.0.30")
set(RTLSDR "${SDR4N_DEPS}/rtl-sdr-blog-1.4.0")
set(MBELIB "${SDR4N_DEPS}/mbelib-9a04ed5c78176a9965f3d43f7aa1b1f5330e771f")
set(HACKRF "${SDR4N_DEPS}/hackrf-2024.02.1/host/libhackrf/src")
set(AIRSPY "${SDR4N_DEPS}/airspyone_host-fc61ab6be57ed61f0e2bdd9c6dfae74cacef57d0/libairspy/src")

set(CMAKE_C_STANDARD 11)
add_compile_options(-O2 -Wno-unused-parameter -Wno-unused-function)

# ---------- libusb ----------
add_library(usb-1.0 STATIC
  ${LIBUSB}/libusb/core.c
  ${LIBUSB}/libusb/descriptor.c
  ${LIBUSB}/libusb/hotplug.c
  ${LIBUSB}/libusb/io.c
  ${LIBUSB}/libusb/sync.c
  ${LIBUSB}/libusb/strerror.c
  ${LIBUSB}/libusb/os/linux_usbfs.c
  ${LIBUSB}/libusb/os/events_posix.c
  ${LIBUSB}/libusb/os/threads_posix.c
  ${LIBUSB}/libusb/os/linux_netlink.c)
target_include_directories(usb-1.0 PRIVATE ${LIBUSB}/android ${LIBUSB}/libusb/os PUBLIC ${LIBUSB}/libusb)
target_compile_options(usb-1.0 PRIVATE -fvisibility=hidden -pthread -Wno-sign-compare)

# ---------- librtlsdr (rtl-sdr-blog) ----------
# usb_fd_shim.h подключается раньше всего (-include) и подменяет поиск/открытие устройства в librtlsdr.c
# на libusb_wrap_sys_device(fd) — сам librtlsdr.c не патчится.
add_library(rtlsdr STATIC
  ${RTLSDR}/src/librtlsdr.c
  ${RTLSDR}/src/tuner_e4k.c
  ${RTLSDR}/src/tuner_fc0012.c
  ${RTLSDR}/src/tuner_fc0013.c
  ${RTLSDR}/src/tuner_fc2580.c
  ${RTLSDR}/src/tuner_r82xx.c)
target_include_directories(rtlsdr PUBLIC ${RTLSDR}/include ${CMAKE_CURRENT_SOURCE_DIR})
target_compile_definitions(rtlsdr PRIVATE rtlsdr_STATIC DETACH_KERNEL_DRIVER)
target_compile_options(rtlsdr PRIVATE -include ${CMAKE_CURRENT_SOURCE_DIR}/usb_fd_shim.h
  -Wno-sign-compare -Wno-format -Wno-unused-variable -Wno-unused-but-set-variable)
target_link_libraries(rtlsdr PUBLIC usb-1.0)

# ---------- mbelib (голос DMR) ----------
add_library(mbe STATIC ${MBELIB}/ecc.c ${MBELIB}/mbelib.c ${MBELIB}/ambe3600x2400.c ${MBELIB}/ambe3600x2450.c
  ${MBELIB}/imbe7100x4400.c ${MBELIB}/imbe7200x4400.c)
target_include_directories(mbe PUBLIC ${MBELIB})
target_compile_options(mbe PRIVATE -Wno-unused-variable -Wno-unused-but-set-variable -Wno-sign-compare -Wno-format)

# ---------- libhackrf: открывается по fd той же прослойкой, что librtlsdr ----------
add_library(hackrf STATIC ${HACKRF}/hackrf.c)
target_include_directories(hackrf PUBLIC ${HACKRF} ${CMAKE_CURRENT_SOURCE_DIR})
target_compile_options(hackrf PRIVATE -include ${CMAKE_CURRENT_SOURCE_DIR}/usb_fd_shim.h
  -Wno-sign-compare -Wno-format -Wno-unused-variable -Wno-unused-but-set-variable)
target_link_libraries(hackrf PUBLIC usb-1.0)

# ---------- libairspy: своя функция airspy_open_fd ----------
add_library(airspy STATIC ${AIRSPY}/airspy.c ${AIRSPY}/iqconverter_float.c ${AIRSPY}/iqconverter_int16.c)
target_include_directories(airspy PUBLIC ${AIRSPY})
target_compile_options(airspy PRIVATE -Wno-sign-compare -Wno-format -Wno-unused-variable -Wno-unused-but-set-variable)
target_link_libraries(airspy PUBLIC usb-1.0)

# ---------- JNI ----------
# lrpt_dsp.c — копия tools/lrpt_dsp.c (демодулятор, Витерби, Рид — Соломон «Метеор-М» LRPT), обёртка lrpt_jni.c
add_library(sdr4noob SHARED sdr4noob_jni.c usb_fd_shim.c mbe_jni.c hackrf_airspy_jni.c lrpt_dsp.c lrpt_jni.c)
target_link_libraries(sdr4noob rtlsdr hackrf airspy usb-1.0 mbe m log)
target_link_options(sdr4noob PRIVATE "-Wl,-z,max-page-size=16384")
