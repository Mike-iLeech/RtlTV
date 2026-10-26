/* JNI к драйверам HackRF (libhackrf) и Airspy (libairspy) для Kotlin: sdr/UsbSources.kt.
 * HackRF открывается по файловому дескриптору через usb_fd_shim (libhackrf собран с -include usb_fd_shim.h),
 * Airspy — своей функцией airspy_open_fd. Отсчёты из потока драйвера идут в кольцевой буфер float (sdr_ring.h);
 * Kotlin забирает их readIq(). Хэндл — указатель (0 — ошибка, код в err[0]). */
#include <jni.h>
#include <stdint.h>
#include <stdlib.h>
#include <android/log.h>

#include <hackrf.h>
#include <airspy.h>
#include "sdr_ring.h"
#include "usb_fd_shim.h"

#define TAG "sdr4noob"
#define RING_SAMPLES (4u << 20)    /* 4 М отсчётов: 0,2 с при 20 МГц (HackRF), ~1,7 с при 2,5 МГц */

/* ------------------------------------------------------------------ HackRF */
typedef struct {
	hackrf_device *dev;
	sdr_ring ring;
} hrf_t;

static int hackrf_inited = 0;

static int hrf_rx(hackrf_transfer *t)
{
	hrf_t *s = t->rx_ctx;
	size_t n = (size_t)t->valid_length / 2;
	const int8_t *b = (const int8_t *)t->buffer;
#define HRF_CONV(i, pi, pq) do { *(pi) = b[2 * (i)] / 128.0f; *(pq) = b[2 * (i) + 1] / 128.0f; } while (0)
	RING_PUSH(&s->ring, n, HRF_CONV);
	return 0;
}

#define HJ(name) Java_io_github_mikeileech_sdr4noob_sdr_HackRfNative_##name

static void set_err(JNIEnv *env, jintArray err, jint code)
{
	(*env)->SetIntArrayRegion(env, err, 0, 1, &code);
}

JNIEXPORT jlong JNICALL HJ(open)(JNIEnv *env, jobject self, jint fd, jintArray err)
{
	if (!hackrf_inited) {
		int r = hackrf_init();
		if (r != HACKRF_SUCCESS) {
			set_err(env, err, r);
			return 0;
		}
		hackrf_inited = 1;
	}
	hrf_t *s = calloc(1, sizeof(*s));
	if (!s || ring_init(&s->ring, RING_SAMPLES) != 0) {
		free(s);
		set_err(env, err, HACKRF_ERROR_NO_MEM);
		return 0;
	}
	sdr4n_shim_begin(fd);
	int r = hackrf_open_by_serial(NULL, &s->dev);
	sdr4n_shim_end();
	if (r != HACKRF_SUCCESS || !s->dev) {
		__android_log_print(ANDROID_LOG_ERROR, TAG, "hackrf_open_by_serial(fd=%d): %s", fd, hackrf_error_name(r));
		ring_free(&s->ring);
		free(s);
		set_err(env, err, r != HACKRF_SUCCESS ? r : -1);
		return 0;
	}
	return (jlong)(uintptr_t)s;
}

static hrf_t *HH(jlong h) { return (hrf_t *)(uintptr_t)h; }
/* нулевой хэндл (устройство не открыто или уже закрыто) — ошибка, а не SIGSEGV */
#define HGET(s, h, ret) hrf_t *s = HH(h); if (!s) return ret

JNIEXPORT jint JNICALL HJ(start)(JNIEnv *env, jobject self, jlong h)
{
	HGET(s, h, -1);
	ring_set_running(&s->ring, 1);
	int r = hackrf_start_rx(s->dev, hrf_rx, s);
	if (r != HACKRF_SUCCESS)
		ring_set_running(&s->ring, 0);
	return r;
}

/* Остановить приём (для смены частоты дискретизации, как _rx_stop на ПК); start — запустить снова. */
JNIEXPORT void JNICALL HJ(stop)(JNIEnv *env, jobject self, jlong h)
{
	HGET(s, h, );
	ring_set_running(&s->ring, 0);
	hackrf_stop_rx(s->dev);
}

JNIEXPORT void JNICALL HJ(close)(JNIEnv *env, jobject self, jlong h)
{
	hrf_t *s = HH(h);
	if (!s)
		return;
	ring_set_running(&s->ring, 0);
	hackrf_stop_rx(s->dev);
	hackrf_close(s->dev);
	ring_free(&s->ring);
	free(s);
}

/* Прочитать до max отсчётов (I, Q подряд). 0 — тайм-аут, −1 — приём остановлен или устройство пропало. */
JNIEXPORT jint JNICALL HJ(readIq)(JNIEnv *env, jobject self, jlong h, jfloatArray out, jint max, jint timeout_ms)
{
	HGET(s, h, -1);
	if (max <= 0 || (*env)->GetArrayLength(env, out) < 2 * max)
		return 0;
	float *tmp = malloc((size_t)max * 2 * sizeof(float));
	if (!tmp)
		return 0;
	int n = ring_read(&s->ring, tmp, (size_t)max, timeout_ms);
	if (n == 0 && hackrf_is_streaming(s->dev) != HACKRF_TRUE)
		n = -1;  /* поток драйвера остановился сам: устройство отключено */
	if (n > 0)
		(*env)->SetFloatArrayRegion(env, out, 0, n * 2, tmp);
	free(tmp);
	return n;
}

JNIEXPORT void JNICALL HJ(flush)(JNIEnv *env, jobject self, jlong h) { HGET(s, h, ); ring_flush(&s->ring); }
JNIEXPORT jlong JNICALL HJ(dropped)(JNIEnv *env, jobject self, jlong h) { HGET(s, h, 0); return s->ring.dropped; }

JNIEXPORT jint JNICALL HJ(setSampleRate)(JNIEnv *env, jobject self, jlong h, jdouble rate)
{
	HGET(s, h, -1);
	int r = hackrf_set_sample_rate(s->dev, rate);
	if (r == HACKRF_SUCCESS)
		hackrf_set_baseband_filter_bandwidth(s->dev, hackrf_compute_baseband_filter_bw((uint32_t)(rate * 0.75)));
	return r;
}

JNIEXPORT jint JNICALL HJ(setFreq)(JNIEnv *env, jobject self, jlong h, jlong hz)
{
	HGET(s, h, -1);
	return hackrf_set_freq(s->dev, (uint64_t)hz);
}

JNIEXPORT jint JNICALL HJ(setGains)(JNIEnv *env, jobject self, jlong h, jboolean amp, jint lna, jint vga)
{
	HGET(s, h, -1);
	int r = hackrf_set_amp_enable(s->dev, amp ? 1 : 0);
	if (r == HACKRF_SUCCESS)
		r = hackrf_set_lna_gain(s->dev, (uint32_t)lna);
	if (r == HACKRF_SUCCESS)
		r = hackrf_set_vga_gain(s->dev, (uint32_t)vga);
	return r;
}

JNIEXPORT jint JNICALL HJ(setBias)(JNIEnv *env, jobject self, jlong h, jboolean on)
{
	HGET(s, h, -1);
	return hackrf_set_antenna_enable(s->dev, on ? 1 : 0);
}

JNIEXPORT jstring JNICALL HJ(boardName)(JNIEnv *env, jobject self, jlong h)
{
	uint8_t id = 0;
	const char *name = "HackRF";
	hrf_t *s = HH(h);
	if (s && hackrf_board_id_read(s->dev, &id) == HACKRF_SUCCESS) {
		const char *n = hackrf_board_id_name((enum hackrf_board_id)id);
		if (n)
			name = n;
	}
	return (*env)->NewStringUTF(env, name);
}

/* ------------------------------------------------------------------ Airspy */
typedef struct {
	struct airspy_device *dev;
	sdr_ring ring;
} asp_t;

static int asp_rx(airspy_transfer *t)
{
	asp_t *s = t->ctx;
	const float *b = (const float *)t->samples;
	size_t n = (size_t)t->sample_count;
#define ASP_CONV(i, pi, pq) do { *(pi) = b[2 * (i)]; *(pq) = b[2 * (i) + 1]; } while (0)
	RING_PUSH(&s->ring, n, ASP_CONV);
	return 0;
}

#define AJ(name) Java_io_github_mikeileech_sdr4noob_sdr_AirspyNative_##name

static asp_t *AH(jlong h) { return (asp_t *)(uintptr_t)h; }
#define AGET(s, h, ret) asp_t *s = AH(h); if (!s) return ret

JNIEXPORT jlong JNICALL AJ(open)(JNIEnv *env, jobject self, jint fd, jintArray err)
{
	asp_t *s = calloc(1, sizeof(*s));
	if (!s || ring_init(&s->ring, RING_SAMPLES) != 0) {
		free(s);
		set_err(env, err, AIRSPY_ERROR_NO_MEM);
		return 0;
	}
	int r = airspy_open_fd(&s->dev, fd);
	if (r != AIRSPY_SUCCESS || !s->dev) {
		__android_log_print(ANDROID_LOG_ERROR, TAG, "airspy_open_fd(%d): %s", fd, airspy_error_name((enum airspy_error)r));
		ring_free(&s->ring);
		free(s);
		set_err(env, err, r != AIRSPY_SUCCESS ? r : -1);
		return 0;
	}
	airspy_set_sample_type(s->dev, AIRSPY_SAMPLE_FLOAT32_IQ);
	return (jlong)(uintptr_t)s;
}

JNIEXPORT jintArray JNICALL AJ(sampleRates)(JNIEnv *env, jobject self, jlong h)
{
	uint32_t cnt = 0;
	asp_t *s = AH(h);
	if (!s)
		return (*env)->NewIntArray(env, 0);
	airspy_get_samplerates(s->dev, &cnt, 0);
	uint32_t *buf = calloc(cnt ? cnt : 1, sizeof(uint32_t));
	if (cnt)
		airspy_get_samplerates(s->dev, buf, cnt);
	jintArray arr = (*env)->NewIntArray(env, (jsize)cnt);
	if (arr && cnt)
		(*env)->SetIntArrayRegion(env, arr, 0, (jsize)cnt, (const jint *)buf);
	free(buf);
	return arr;
}

JNIEXPORT jint JNICALL AJ(start)(JNIEnv *env, jobject self, jlong h)
{
	AGET(s, h, -1);
	ring_set_running(&s->ring, 1);
	int r = airspy_start_rx(s->dev, asp_rx, s);
	if (r != AIRSPY_SUCCESS)
		ring_set_running(&s->ring, 0);
	return r;
}

JNIEXPORT void JNICALL AJ(stop)(JNIEnv *env, jobject self, jlong h)
{
	AGET(s, h, );
	ring_set_running(&s->ring, 0);
	airspy_stop_rx(s->dev);
}

JNIEXPORT void JNICALL AJ(close)(JNIEnv *env, jobject self, jlong h)
{
	asp_t *s = AH(h);
	if (!s)
		return;
	ring_set_running(&s->ring, 0);
	airspy_stop_rx(s->dev);
	airspy_close(s->dev);
	ring_free(&s->ring);
	free(s);
}

JNIEXPORT jint JNICALL AJ(readIq)(JNIEnv *env, jobject self, jlong h, jfloatArray out, jint max, jint timeout_ms)
{
	AGET(s, h, -1);
	if (max <= 0 || (*env)->GetArrayLength(env, out) < 2 * max)
		return 0;
	float *tmp = malloc((size_t)max * 2 * sizeof(float));
	if (!tmp)
		return 0;
	int n = ring_read(&s->ring, tmp, (size_t)max, timeout_ms);
	if (n == 0 && airspy_is_streaming(s->dev) != AIRSPY_TRUE)
		n = -1;
	if (n > 0)
		(*env)->SetFloatArrayRegion(env, out, 0, n * 2, tmp);
	free(tmp);
	return n;
}

JNIEXPORT void JNICALL AJ(flush)(JNIEnv *env, jobject self, jlong h) { AGET(s, h, ); ring_flush(&s->ring); }
JNIEXPORT jlong JNICALL AJ(dropped)(JNIEnv *env, jobject self, jlong h) { AGET(s, h, 0); return s->ring.dropped; }

JNIEXPORT jint JNICALL AJ(setSampleRate)(JNIEnv *env, jobject self, jlong h, jint rate)
{
	AGET(s, h, -1);
	return airspy_set_samplerate(s->dev, (uint32_t)rate);
}

JNIEXPORT jint JNICALL AJ(setFreq)(JNIEnv *env, jobject self, jlong h, jlong hz)
{
	AGET(s, h, -1);
	return airspy_set_freq(s->dev, (uint32_t)hz);
}

/* step < 0 — АРУ (МШУ и смеситель), иначе ступень linearity 0–21. */
JNIEXPORT jint JNICALL AJ(setGain)(JNIEnv *env, jobject self, jlong h, jint step)
{
	AGET(s, h, -1);
	if (step < 0) {
		int r = airspy_set_lna_agc(s->dev, 1);
		return r == AIRSPY_SUCCESS ? airspy_set_mixer_agc(s->dev, 1) : r;
	}
	airspy_set_lna_agc(s->dev, 0);
	airspy_set_mixer_agc(s->dev, 0);
	return airspy_set_linearity_gain(s->dev, (uint8_t)step);
}

JNIEXPORT jint JNICALL AJ(setBias)(JNIEnv *env, jobject self, jlong h, jboolean on)
{
	AGET(s, h, -1);
	return airspy_set_rf_bias(s->dev, on ? 1 : 0);
}
