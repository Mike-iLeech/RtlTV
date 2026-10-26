#define SDR4N_SHIM_IMPL
#include "usb_fd_shim.h"

#include <stdarg.h>
#include <stdint.h>
#include <stdlib.h>
#include <android/log.h>

static __thread int pending_fd = -1;
static __thread libusb_device_handle *pending_handle = NULL;
static __thread int pending_taken = 0;

void sdr4n_shim_begin(int fd)
{
	pending_fd = fd;
	pending_handle = NULL;
	pending_taken = 0;
}

int sdr4n_shim_end(void)
{
	int ok = pending_taken ? 0 : -1;
	pending_fd = -1;
	pending_handle = NULL;
	pending_taken = 0;
	return ok;
}

int sdr4n_libusb_init(libusb_context **ctx)
{
	/* Без перебора /dev/bus/usb: на Android он запрещён (SELinux) и бесполезен. */
	struct libusb_init_option opt = { .option = LIBUSB_OPTION_NO_DEVICE_DISCOVERY };
	return libusb_init_context(ctx, &opt, 1);
}

ssize_t sdr4n_get_device_list(libusb_context *ctx, libusb_device ***list)
{
	libusb_device **out = calloc(2, sizeof(*out));
	if (!out)
		return LIBUSB_ERROR_NO_MEM;
	*list = out;
	if (pending_fd < 0)
		return 0;
	if (!pending_handle) {
		int r = libusb_wrap_sys_device(ctx, (intptr_t)pending_fd, &pending_handle);
		if (r < 0) {
			__android_log_print(ANDROID_LOG_ERROR, "sdr4noob", "libusb_wrap_sys_device(%d): %s",
					    pending_fd, libusb_error_name(r));
			pending_handle = NULL;
			return 0;
		}
	}
	out[0] = libusb_get_device(pending_handle);
	return 1;
}

void sdr4n_free_device_list(libusb_device **list, int unref_devices)
{
	/* Устройство принадлежит обёрнутому дескриптору — ссылки не отпускаем. */
	(void)unref_devices;
	free(list);
}

int sdr4n_open(libusb_device *dev, libusb_device_handle **handle)
{
	if (pending_handle && libusb_get_device(pending_handle) == dev) {
		*handle = pending_handle;
		pending_taken = 1;
		return 0;
	}
	return LIBUSB_ERROR_NOT_FOUND;
}

libusb_device_handle *sdr4n_open_device_with_vid_pid(libusb_context *ctx, uint16_t vid, uint16_t pid)
{
	libusb_device **list = NULL;
	libusb_device_handle *h = NULL;
	ssize_t n = sdr4n_get_device_list(ctx, &list);
	for (ssize_t i = 0; i < n; i++) {
		struct libusb_device_descriptor d;
		if (libusb_get_device_descriptor(list[i], &d) == 0 && d.idVendor == vid && d.idProduct == pid &&
		    sdr4n_open(list[i], &h) == 0)
			break;
		h = NULL;
	}
	sdr4n_free_device_list(list, 1);
	return h;
}

void sdr4n_exit(libusb_context *ctx)
{
	/* драйвер не взял обёрнутое устройство (например, неизвестный VID/PID) — закрыть до libusb_exit */
	if (pending_handle && !pending_taken) {
		libusb_close(pending_handle);
		pending_handle = NULL;
	}
	libusb_exit(ctx);
}

void sdr4n_log(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	__android_log_vprint(ANDROID_LOG_INFO, "librtlsdr", fmt, ap);
	va_end(ap);
}
