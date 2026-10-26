# Нативная часть SDR4NOOB для Android:
#   usb-1.0   — libusb (без поиска устройств: на Android их открывает приложение через UsbManager);
#   rtlsdr    — librtlsdr сборки rtl-sdr-blog, открывается по файловому дескриптору (usb_fd_shim);
#   sdr4noob  — JNI-обёртка для Kotlin (sdr/RtlSdrNative.kt).
# Исходники libusb и librtlsdr скачивает задача Gradle fetchNativeDeps в SDR4N_DEPS.
cmake_minimum_required(VERSION 3.22)
project(sdr4noob C)

if(NOT SDR4N_DEPS OR NOT EXISTS "${SDR4N_DEPS}/libusb-1.0.30/libusb/core.c")
  message(FATAL_ERROR "Нет исходников драйвера в '${SDR4N_DEPS}': запустите сборку через Gradle (задача fetchNativeDeps)")
endif()
set(LIBUSB "${SDR4N_DEPS}/libusb-1.0.30")
set(RTLSDR "${SDR4N_DEPS}/rtl-sdr-blog-1.4.0")

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

# ---------- JNI ----------
add_library(sdr4noob SHARED sdr4noob_jni.c usb_fd_shim.c)
target_link_libraries(sdr4noob rtlsdr usb-1.0 log)
target_link_options(sdr4noob PRIVATE "-Wl,-z,max-page-size=16384")
