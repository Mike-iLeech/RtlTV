/* JNI к декодеру «Метеор-М» LRPT (lrpt_dsp.c — копия tools/lrpt_dsp.c) для Kotlin: lrpt/LrptNative.kt.
 * Хэндл демодулятора — указатель на память demod_size() байт (0 — ошибка). Массивы копируются через
 * Get/Set…ArrayRegion в свои буферы; длины проверяются до вызова C (иначе — код -1/0 без записи). */
#include <jni.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* lrpt_dsp.c */
int demod_size(void);
void *demod_init(void *mem, double sps, int mode, double costas_bw, double timing_bw, double fmax_rad);
void demod_set_costas(void *p, double bw);
void demod_set_freq(void *p, double freq_rad);
void demod_state(void *p, double *out);
int demod_process(void *p, const float *iq, int n, int8_t *soft_out, float *cons, int max_sym);
int conv_encode(const uint8_t *bits, int n, uint8_t *out, int state);
int viterbi27(const int8_t *soft, int npairs, uint8_t *out, uint64_t *dec);
void rs_encode(const uint8_t *data, uint8_t *parity_out, int dual);
int rs_decode(uint8_t *data, int dual);

#define JNI(name) Java_io_github_mikeileech_sdr4noob_lrpt_LrptNative_##name
#define LEN(a) ((a) ? (*env)->GetArrayLength(env, (a)) : 0)

JNIEXPORT jint JNICALL JNI(demodSize)(JNIEnv *env, jobject self)
{
	return demod_size();
}

JNIEXPORT jlong JNICALL JNI(demodCreate)(JNIEnv *env, jobject self, jdouble sps, jint mode, jdouble costas_bw,
                                         jdouble timing_bw, jdouble fmax_rad)
{
	if (!(sps >= 2.0) || mode < 0 || mode > 2)
		return 0;
	void *mem = calloc(1, (size_t)demod_size());
	if (!mem)
		return 0;
	return (jlong)(uintptr_t)demod_init(mem, sps, mode, costas_bw, timing_bw, fmax_rad);
}

JNIEXPORT void JNICALL JNI(demodFree)(JNIEnv *env, jobject self, jlong h)
{
	free((void *)(uintptr_t)h);
}

JNIEXPORT void JNICALL JNI(demodSetCostas)(JNIEnv *env, jobject self, jlong h, jdouble bw)
{
	if (h)
		demod_set_costas((void *)(uintptr_t)h, bw);
}

JNIEXPORT void JNICALL JNI(demodSetFreq)(JNIEnv *env, jobject self, jlong h, jdouble f)
{
	if (h)
		demod_set_freq((void *)(uintptr_t)h, f);
}

/* out — 6 double: [freq рад/отсчёт, phase, interval/half, agc, amp, lock] */
JNIEXPORT void JNICALL JNI(demodState)(JNIEnv *env, jobject self, jlong h, jdoubleArray out)
{
	double st[6];
	if (!h || LEN(out) < 6)
		return;
	demod_state((void *)(uintptr_t)h, st);
	(*env)->SetDoubleArrayRegion(env, out, 0, 6, st);
}

/* iq — 2n float (re, im), soft — не меньше 2·maxSym байт, cons — null или не меньше 2·maxSym float.
 * Возвращает число символов (как demod_process) или -1 при неверных аргументах. */
JNIEXPORT jint JNICALL JNI(demodProcess)(JNIEnv *env, jobject self, jlong h, jfloatArray iq, jint n,
                                         jbyteArray soft, jfloatArray cons, jint max_sym)
{
	if (!h || n < 0 || max_sym < 0 || LEN(iq) / 2 < n || LEN(soft) / 2 < max_sym || (cons && LEN(cons) / 2 < max_sym))
		return -1;
	float *x = malloc(((size_t)n * 2 + 1) * sizeof(float));
	int8_t *s = calloc((size_t)max_sym * 2 + 1, 1);
	float *c = cons ? malloc(((size_t)max_sym * 2 + 1) * sizeof(float)) : NULL;
	if (!x || !s || (cons && !c)) {
		free(x);
		free(s);
		free(c);
		return -1;
	}
	(*env)->GetFloatArrayRegion(env, iq, 0, n * 2, x);
	int r = demod_process((void *)(uintptr_t)h, x, n, s, c, max_sym);
	/* QPSK/OQPSK — пары мягких бит, BPSK — по одному; копируем с запасом всю занятую часть */
	if (r > 0) {
		(*env)->SetByteArrayRegion(env, soft, 0, r * 2, (const jbyte *)s);
		if (c)
			(*env)->SetFloatArrayRegion(env, cons, 0, r * 2, c);
	}
	free(x);
	free(s);
	free(c);
	return r;
}

JNIEXPORT jint JNICALL JNI(convEncode)(JNIEnv *env, jobject self, jbyteArray bits, jint n, jbyteArray out, jint state)
{
	if (n < 0 || LEN(bits) < n || LEN(out) / 2 < n)
		return -1;
	uint8_t *b = malloc((size_t)n + 1), *o = malloc((size_t)n * 2 + 1);
	if (!b || !o) {
		free(b);
		free(o);
		return -1;
	}
	(*env)->GetByteArrayRegion(env, bits, 0, n, (jbyte *)b);
	int st = conv_encode(b, n, o, state);
	(*env)->SetByteArrayRegion(env, out, 0, n * 2, (const jbyte *)o);
	free(b);
	free(o);
	return st;
}

/* soft — 2·npairs int8, out — npairs бит. Возвращает метрику лучшего пути ×100 (как viterbi27). */
JNIEXPORT jint JNICALL JNI(viterbi27)(JNIEnv *env, jobject self, jbyteArray soft, jint npairs, jbyteArray out)
{
	if (npairs <= 0)
		return 0;
	if (LEN(soft) / 2 < npairs || LEN(out) < npairs)
		return 0;
	int8_t *s = malloc((size_t)npairs * 2);
	uint8_t *o = malloc((size_t)npairs);
	uint64_t *dec = malloc((size_t)npairs * sizeof(uint64_t));
	if (!s || !o || !dec) {
		free(s);
		free(o);
		free(dec);
		return 0;
	}
	(*env)->GetByteArrayRegion(env, soft, 0, npairs * 2, (jbyte *)s);
	int m = viterbi27(s, npairs, o, dec);
	(*env)->SetByteArrayRegion(env, out, 0, npairs, (const jbyte *)o);
	free(s);
	free(o);
	free(dec);
	return m;
}

JNIEXPORT void JNICALL JNI(rsEncode)(JNIEnv *env, jobject self, jbyteArray data, jbyteArray parity, jboolean dual)
{
	uint8_t d[223], p[32];
	if (LEN(data) < 223 || LEN(parity) < 32)
		return;
	(*env)->GetByteArrayRegion(env, data, 0, 223, (jbyte *)d);
	rs_encode(d, p, dual ? 1 : 0);
	(*env)->SetByteArrayRegion(env, parity, 0, 32, (const jbyte *)p);
}

/* cw — 255 байт, исправляется на месте. Возвращает число исправленных байт или -1. */
JNIEXPORT jint JNICALL JNI(rsDecode)(JNIEnv *env, jobject self, jbyteArray cw, jboolean dual)
{
	uint8_t d[255];
	if (LEN(cw) < 255)
		return -1;
	(*env)->GetByteArrayRegion(env, cw, 0, 255, (jbyte *)d);
	int r = rs_decode(d, dual ? 1 : 0);
	if (r > 0)
		(*env)->SetByteArrayRegion(env, cw, 0, 255, (const jbyte *)d);
	return r;
}
