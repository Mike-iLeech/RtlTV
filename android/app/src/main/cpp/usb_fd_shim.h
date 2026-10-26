/* Открытие USB-приёмника по файловому дескриптору (Android).
 *
 * На Android приложение не может само перебрать /dev/bus/usb: устройство открывает система после
 * разрешения пользователя (UsbManager.openDevice), а в драйвер передаётся только дескриптор.
 * libusb умеет принять его через libusb_wrap_sys_device(), но библиотеки драйверов (librtlsdr,
 * libhackrf …) ищут устройство сами: libusb_get_device_list() → libusb_open().
 *
 * Этот заголовок подключается к исходникам такой библиотеки раньше всего (-include usb_fd_shim.h)
 * и подменяет вызовы libusb: список устройств состоит из одного «обёрнутого» устройства,
 * libusb_open() отдаёт уже открытый дескриптор, а libusb_exit() закрывает его, если драйвер его
 * не взял. Так любой драйвер на libusb открывается по fd без правки его исходников. Перед вызовом *_open() драйвера поток задаёт fd: sdr4n_shim_begin(fd).
 */
#ifndef SDR4N_USB_FD_SHIM_H
#define SDR4N_USB_FD_SHIM_H

#include <stdio.h>
#include <sys/types.h>
#include <libusb.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Задать fd для ближайшего открытия устройства в текущем потоке (-1 — сбросить). */
void sdr4n_shim_begin(int fd);
/* Закончить открытие: вернуть 0, если обёрнутое устройство было взято драйвером. */
int sdr4n_shim_end(void);

int sdr4n_libusb_init(libusb_context **ctx);
ssize_t sdr4n_get_device_list(libusb_context *ctx, libusb_device ***list);
void sdr4n_free_device_list(libusb_device **list, int unref_devices);
int sdr4n_open(libusb_device *dev, libusb_device_handle **handle);
void sdr4n_exit(libusb_context *ctx);
void sdr4n_log(const char *fmt, ...);

#ifdef __cplusplus
}
#endif

#ifndef SDR4N_SHIM_IMPL
#define libusb_init(ctx) sdr4n_libusb_init(ctx)
#define libusb_get_device_list(ctx, list) sdr4n_get_device_list(ctx, list)
#define libusb_free_device_list(list, unref) sdr4n_free_device_list(list, unref)
#define libusb_open(dev, handle) sdr4n_open(dev, handle)
#define libusb_exit(ctx) sdr4n_exit(ctx)
/* диагностика драйвера (fprintf(stderr, …)) — в logcat */
#define fprintf(stream, ...) sdr4n_log(__VA_ARGS__)
#endif

#endif
