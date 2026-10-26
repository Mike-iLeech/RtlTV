/* JNI к mbelib (вокодер AMBE+2 для голоса DMR): dsp/Ambe.kt MbeNative.
 * Одно состояние синтезатора — три mbe_parms (текущие, предыдущие, предыдущие «улучшенные»), как в DSD. */
#include <jni.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include <mbelib.h>

typedef struct {
	mbe_parms cur, prev, prev_enh;
} mbe_state;

#define JNI(name) Java_io_github_mikeileech_sdr4noob_dsp_MbeNative_##name

JNIEXPORT jlong JNICALL JNI(create)(JNIEnv *env, jobject self)
{
	mbe_state *s = calloc(1, sizeof(*s));
	if (!s)
		return 0;
	mbe_initMbeParms(&s->cur, &s->prev, &s->prev_enh);
	return (jlong)(uintptr_t)s;
}

JNIEXPORT void JNICALL JNI(reset)(JNIEnv *env, jobject self, jlong h)
{
	mbe_state *s = (mbe_state *)(uintptr_t)h;
	if (s)
		mbe_initMbeParms(&s->cur, &s->prev, &s->prev_enh);
}

JNIEXPORT void JNICALL JNI(free)(JNIEnv *env, jobject self, jlong h)
{
	free((void *)(uintptr_t)h);
}

/* fr — 96 байт ambe_fr[4][24] (0/1), out — 160 float (8 кГц, масштаб mbelib ≈ ±8000 → делится на 8000).
 * Возвращает число исправленных/неисправленных ошибок кадра (errs2). */
JNIEXPORT jint JNICALL JNI(decode)(JNIEnv *env, jobject self, jlong h, jbyteArray fr, jfloatArray out)
{
	mbe_state *s = (mbe_state *)(uintptr_t)h;
	char ambe_fr[4][24];
	char ambe_d[49];
	char err_str[64];
	float buf[160];
	int errs = 0, errs2 = 0;
	if (!s)
		return -1;
	(*env)->GetByteArrayRegion(env, fr, 0, 96, (jbyte *)ambe_fr);
	memset(ambe_d, 0, sizeof(ambe_d));
	memset(err_str, 0, sizeof(err_str));
	mbe_processAmbe3600x2450Framef(buf, &errs, &errs2, err_str, ambe_fr, ambe_d, &s->cur, &s->prev, &s->prev_enh, 3);
	for (int i = 0; i < 160; i++) {
		float v = buf[i] / 8000.0f;
		buf[i] = v > 1.0f ? 1.0f : (v < -1.0f ? -1.0f : v);
	}
	(*env)->SetFloatArrayRegion(env, out, 0, 160, buf);
	return errs2;
}
