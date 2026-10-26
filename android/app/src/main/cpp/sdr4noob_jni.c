/* JNI-обёртка встроенного драйвера RTL-SDR (librtlsdr) для Kotlin: sdr/RtlSdrNative.kt.
 *
 * Поток: отдельный pthread крутит rtlsdr_read_async() и складывает блоки u8 IQ в кольцевой буфер;
 * Kotlin забирает их read() в своём потоке. Если Kotlin не успевает, новые блоки отбрасываются
 * целиком (чётность I/Q не сбивается), счётчик потерь — dropped().
 */
#include <errno.h>
#include <jni.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <android/log.h>

#include <rtl-sdr.h>
#include "usb_fd_shim.h"

#define TAG "sdr4noob"
#define RING_BYTES (8u << 20)       /* ~1,7 с при 2,4 MS/s */
#define ASYNC_BUFS 12
#define ASYNC_BUF_LEN (16 * 16384)  /* кратно 512 */

typedef struct {
	rtlsdr_dev_t *dev;
	pthread_t thread;
	int thread_started;
	pthread_mutex_t lock;
	pthread_cond_t cond;
	uint8_t *ring;
	size_t head, fill;        /* head — где читать; fill — сколько лежит */
	int64_t dropped;
	int running;              /* поток чтения должен работать (stop() сбрасывает) */
	int exited;               /* поток чтения завершился */
	int lost;                 /* поток завершился сам: устройство отключено */
} sdr_t;

static void on_samples(unsigned char *buf, uint32_t len, void *ctx)
{
	sdr_t *s = ctx;
	len &= ~1u;  /* пары I/Q не должны сдвигаться даже от нечётной короткой передачи */
	pthread_mutex_lock(&s->lock);
	if (s->fill + len > RING_BYTES) {
		s->dropped += len;
	} else {
		size_t tail = (s->head + s->fill) % RING_BYTES;
		size_t first = RING_BYTES - tail < len ? RING_BYTES - tail : len;
		memcpy(s->ring + tail, buf, first);
		memcpy(s->ring, buf + first, len - first);
		s->fill += len;
		pthread_cond_signal(&s->cond);
	}
	pthread_mutex_unlock(&s->lock);
}

static void *reader(void *arg)
{
	sdr_t *s = arg;
	int r = 0;
	pthread_mutex_lock(&s->lock);
	int go = s->running;
	pthread_mutex_unlock(&s->lock);
	/* stop() мог прийти раньше: тогда read_async не запускаем вовсе */
	if (go)
		r = rtlsdr_read_async(s->dev, on_samples, s, ASYNC_BUFS, ASYNC_BUF_LEN);
	pthread_mutex_lock(&s->lock);
	if (s->running)
		s->lost = 1;  /* не stop(): чтение оборвалось само */
	s->running = 0;
	s->exited = 1;
	pthread_cond_broadcast(&s->cond);
	pthread_mutex_unlock(&s->lock);
	__android_log_print(ANDROID_LOG_INFO, TAG, "rtlsdr_read_async завершился: %d", r);
	return NULL;
}

static sdr_t *H(jlong h) { return (sdr_t *)(uintptr_t)h; }

#define JNI(name) Java_io_github_mikeileech_sdr4noob_sdr_RtlSdrNative_##name

/* Возвращает хэндл или 0; код ошибки — в err[0]. Хэндл — указатель, а не число со знаком: на arm64
 * (Android 11+) у указателей кучи метка в старшем байте, и как jlong они отрицательные. */
JNIEXPORT jlong JNICALL JNI(open)(JNIEnv *env, jobject self, jint fd, jintArray err)
{
	jint code = -ENOMEM;
	sdr_t *s = calloc(1, sizeof(*s));
	if (!s) {
		(*env)->SetIntArrayRegion(env, err, 0, 1, &code);
		return 0;
	}
	s->ring = malloc(RING_BYTES);
	if (!s->ring) {
		free(s);
		(*env)->SetIntArrayRegion(env, err, 0, 1, &code);
		return 0;
	}
	pthread_mutex_init(&s->lock, NULL);
	pthread_cond_init(&s->cond, NULL);
	sdr4n_shim_begin(fd);
	int r = rtlsdr_open(&s->dev, 0);
	int taken = sdr4n_shim_end();
	if (r < 0 || !s->dev) {
		__android_log_print(ANDROID_LOG_ERROR, TAG, "rtlsdr_open(fd=%d): %d (устройство %s)", fd, r,
				    taken == 0 ? "открыто" : "не взято драйвером");
		pthread_cond_destroy(&s->cond);
		pthread_mutex_destroy(&s->lock);
		free(s->ring);
		free(s);
		code = r < 0 ? r : -1;
		(*env)->SetIntArrayRegion(env, err, 0, 1, &code);
		return 0;
	}
	return (jlong)(uintptr_t)s;
}

/* Остановить поток чтения. rtlsdr_cancel_async() действует, только пока read_async уже запущен, поэтому
 * отмена повторяется, пока поток не сообщит, что вышел (иначе stop сразу после start ждал бы вечно). */
static void stop_thread(sdr_t *s)
{
	if (!s->thread_started)
		return;
	pthread_mutex_lock(&s->lock);
	s->running = 0;
	while (!s->exited) {
		pthread_mutex_unlock(&s->lock);
		rtlsdr_cancel_async(s->dev);
		pthread_mutex_lock(&s->lock);
		if (s->exited)
			break;
		struct timespec ts;
		clock_gettime(CLOCK_REALTIME, &ts);
		ts.tv_nsec += 10 * 1000000L;
		if (ts.tv_nsec >= 1000000000L) {
			ts.tv_sec++;
			ts.tv_nsec -= 1000000000L;
		}
		pthread_cond_timedwait(&s->cond, &s->lock, &ts);
	}
	pthread_mutex_unlock(&s->lock);
	pthread_join(s->thread, NULL);
	s->thread_started = 0;
}

JNIEXPORT void JNICALL JNI(close)(JNIEnv *env, jobject self, jlong h)
{
	sdr_t *s = H(h);
	if (!s)
		return;
	stop_thread(s);
	rtlsdr_close(s->dev);
	pthread_cond_destroy(&s->cond);
	pthread_mutex_destroy(&s->lock);
	free(s->ring);
	free(s);
}

JNIEXPORT jint JNICALL JNI(start)(JNIEnv *env, jobject self, jlong h)
{
	sdr_t *s = H(h);
	if (s->thread_started)
		return 0;
	rtlsdr_reset_buffer(s->dev);  /* синхронно, до запуска потока */
	pthread_mutex_lock(&s->lock);
	s->head = s->fill = 0;
	s->running = 1;
	s->exited = 0;
	s->lost = 0;
	pthread_mutex_unlock(&s->lock);
	if (pthread_create(&s->thread, NULL, reader, s) != 0) {
		s->running = 0;
		s->exited = 1;
		return -1;
	}
	s->thread_started = 1;
	return 0;
}

JNIEXPORT void JNICALL JNI(stop)(JNIEnv *env, jobject self, jlong h)
{
	stop_thread(H(h));
}

/* Прочитать до len байт (чётное число). Возвращает число байт, 0 — тайм-аут, -1 — поток остановлен
 * или устройство потеряно (lost()). */
JNIEXPORT jint JNICALL JNI(read)(JNIEnv *env, jobject self, jlong h, jbyteArray out, jint len, jint timeout_ms)
{
	sdr_t *s = H(h);
	struct timespec ts;
	clock_gettime(CLOCK_REALTIME, &ts);
	ts.tv_sec += timeout_ms / 1000;
	ts.tv_nsec += (long)(timeout_ms % 1000) * 1000000L;
	if (ts.tv_nsec >= 1000000000L) {
		ts.tv_sec++;
		ts.tv_nsec -= 1000000000L;
	}
	len &= ~1;
	pthread_mutex_lock(&s->lock);
	while (s->fill < (size_t)len && s->running) {
		if (pthread_cond_timedwait(&s->cond, &s->lock, &ts) == ETIMEDOUT)
			break;
	}
	size_t n = s->fill < (size_t)len ? s->fill : (size_t)len;
	n &= ~(size_t)1;
	if (n == 0) {
		int alive = s->running;
		pthread_mutex_unlock(&s->lock);
		return alive ? 0 : -1;
	}
	size_t first = RING_BYTES - s->head < n ? RING_BYTES - s->head : n;
	(*env)->SetByteArrayRegion(env, out, 0, (jsize)first, (const jbyte *)(s->ring + s->head));
	if (n > first)
		(*env)->SetByteArrayRegion(env, out, (jsize)first, (jsize)(n - first), (const jbyte *)s->ring);
	s->head = (s->head + n) % RING_BYTES;
	s->fill -= n;
	pthread_mutex_unlock(&s->lock);
	return (jint)n;
}

/* Выбросить накопленное (после перестройки частоты — старые отсчёты не нужны). */
JNIEXPORT void JNICALL JNI(flush)(JNIEnv *env, jobject self, jlong h)
{
	sdr_t *s = H(h);
	pthread_mutex_lock(&s->lock);
	s->head = s->fill = 0;
	pthread_mutex_unlock(&s->lock);
}

JNIEXPORT jlong JNICALL JNI(dropped)(JNIEnv *env, jobject self, jlong h)
{
	sdr_t *s = H(h);
	pthread_mutex_lock(&s->lock);
	jlong d = s->dropped;
	pthread_mutex_unlock(&s->lock);
	return d;
}

JNIEXPORT jboolean JNICALL JNI(lost)(JNIEnv *env, jobject self, jlong h)
{
	sdr_t *s = H(h);
	pthread_mutex_lock(&s->lock);
	int l = s->lost;
	pthread_mutex_unlock(&s->lock);
	return l ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT jint JNICALL JNI(setSampleRate)(JNIEnv *env, jobject self, jlong h, jint rate)
{
	return rtlsdr_set_sample_rate(H(h)->dev, (uint32_t)rate);
}

JNIEXPORT jint JNICALL JNI(setCenterFreq)(JNIEnv *env, jobject self, jlong h, jlong hz)
{
	return rtlsdr_set_center_freq(H(h)->dev, (uint32_t)hz);
}

JNIEXPORT jint JNICALL JNI(setGain)(JNIEnv *env, jobject self, jlong h, jint tenths)
{
	rtlsdr_dev_t *d = H(h)->dev;
	if (tenths < 0)
		return rtlsdr_set_tuner_gain_mode(d, 0);
	int r = rtlsdr_set_tuner_gain_mode(d, 1);
	return r < 0 ? r : rtlsdr_set_tuner_gain(d, tenths);
}

JNIEXPORT jintArray JNICALL JNI(gains)(JNIEnv *env, jobject self, jlong h)
{
	rtlsdr_dev_t *d = H(h)->dev;
	int n = rtlsdr_get_tuner_gains(d, NULL);
	if (n < 0)
		n = 0;
	int *g = calloc(n > 0 ? n : 1, sizeof(int));
	if (n > 0)
		rtlsdr_get_tuner_gains(d, g);
	jintArray arr = (*env)->NewIntArray(env, n);
	if (arr && n > 0)
		(*env)->SetIntArrayRegion(env, arr, 0, n, g);
	free(g);
	return arr;
}

JNIEXPORT jint JNICALL JNI(setPpm)(JNIEnv *env, jobject self, jlong h, jint ppm)
{
	int r = rtlsdr_set_freq_correction(H(h)->dev, ppm);
	return r == -2 ? 0 : r;  /* -2 — та же поправка уже стоит */
}

JNIEXPORT jint JNICALL JNI(setBiasTee)(JNIEnv *env, jobject self, jlong h, jboolean on)
{
	return rtlsdr_set_bias_tee(H(h)->dev, on ? 1 : 0);
}

JNIEXPORT jint JNICALL JNI(setAgc)(JNIEnv *env, jobject self, jlong h, jboolean on)
{
	return rtlsdr_set_agc_mode(H(h)->dev, on ? 1 : 0);
}

JNIEXPORT jint JNICALL JNI(setDirectSampling)(JNIEnv *env, jobject self, jlong h, jint mode)
{
	return rtlsdr_set_direct_sampling(H(h)->dev, mode);
}

JNIEXPORT jint JNICALL JNI(tunerType)(JNIEnv *env, jobject self, jlong h)
{
	return (jint)rtlsdr_get_tuner_type(H(h)->dev);
}

JNIEXPORT jstring JNICALL JNI(usbStrings)(JNIEnv *env, jobject self, jlong h)
{
	char m[256] = {0}, p[256] = {0}, sn[256] = {0}, out[800];
	rtlsdr_get_usb_strings(H(h)->dev, m, p, sn);
	snprintf(out, sizeof(out), "%s|%s|%s", m, p, sn);
	for (char *c = out; *c; c++)  /* строки USB — не обязательно UTF-8: JNI требует ASCII-безопасное */
		if ((unsigned char)*c >= 0x80 || (unsigned char)*c < 0x20)
			*c = '?';
	return (*env)->NewStringUTF(env, out);
}
