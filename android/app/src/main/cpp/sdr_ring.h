/* Кольцевой буфер комплексных отсчётов float (I, Q) между потоком драйвера и Kotlin — для приёмников с
 * собственным потоком приёма (HackRF, Airspy). Драйвер кладёт блоки (ring_push), Kotlin забирает (ring_read);
 * если Kotlin не успевает, новые блоки отбрасываются целиком (счётчик dropped). */
#ifndef SDR4N_SDR_RING_H
#define SDR4N_SDR_RING_H

#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef struct {
	pthread_mutex_t lock;
	pthread_cond_t cond;
	float *buf;            /* пары I, Q */
	size_t cap;            /* ёмкость в комплексных отсчётах */
	size_t head, fill;
	int64_t dropped;
	int running;           /* приём идёт (stop сбрасывает) */
	int lost;              /* драйвер сообщил об ошибке/отключении */
} sdr_ring;

static int ring_init(sdr_ring *r, size_t cap)
{
	memset(r, 0, sizeof(*r));
	r->buf = malloc(cap * 2 * sizeof(float));
	if (!r->buf)
		return -ENOMEM;
	r->cap = cap;
	pthread_mutex_init(&r->lock, NULL);
	pthread_cond_init(&r->cond, NULL);
	return 0;
}

static void ring_free(sdr_ring *r)
{
	pthread_cond_destroy(&r->cond);
	pthread_mutex_destroy(&r->lock);
	free(r->buf);
	r->buf = NULL;
}

/* Положить n отсчётов: conv(i, &I, &Q) отдаёт i-й отсчёт источника. */
#define RING_PUSH(r, n, CONV)                                                       \
	do {                                                                            \
		pthread_mutex_lock(&(r)->lock);                                             \
		if ((r)->fill + (n) > (r)->cap) {                                           \
			(r)->dropped += (n);                                                    \
		} else {                                                                    \
			size_t _t = ((r)->head + (r)->fill) % (r)->cap;                         \
			for (size_t _i = 0; _i < (size_t)(n); _i++) {                           \
				float _I, _Q;                                                       \
				CONV(_i, &_I, &_Q);                                                 \
				(r)->buf[2 * _t] = _I;                                              \
				(r)->buf[2 * _t + 1] = _Q;                                          \
				if (++_t == (r)->cap)                                               \
					_t = 0;                                                         \
			}                                                                       \
			(r)->fill += (n);                                                       \
			pthread_cond_signal(&(r)->cond);                                        \
		}                                                                           \
		pthread_mutex_unlock(&(r)->lock);                                           \
	} while (0)

/* Забрать до max отсчётов в out (I, Q подряд); ждёт не дольше timeout_ms. 0 — тайм-аут, −1 — приём остановлен. */
static int ring_read(sdr_ring *r, float *out, size_t max, int timeout_ms)
{
	struct timespec ts;
	clock_gettime(CLOCK_REALTIME, &ts);
	ts.tv_sec += timeout_ms / 1000;
	ts.tv_nsec += (long)(timeout_ms % 1000) * 1000000L;
	if (ts.tv_nsec >= 1000000000L) {
		ts.tv_sec++;
		ts.tv_nsec -= 1000000000L;
	}
	pthread_mutex_lock(&r->lock);
	while (r->fill < max && r->running) {
		if (pthread_cond_timedwait(&r->cond, &r->lock, &ts) == ETIMEDOUT)
			break;
	}
	size_t n = r->fill < max ? r->fill : max;
	if (n == 0) {
		int alive = r->running;
		pthread_mutex_unlock(&r->lock);
		return alive ? 0 : -1;
	}
	size_t first = r->cap - r->head < n ? r->cap - r->head : n;
	memcpy(out, r->buf + 2 * r->head, first * 2 * sizeof(float));
	if (n > first)
		memcpy(out + 2 * first, r->buf, (n - first) * 2 * sizeof(float));
	r->head = (r->head + n) % r->cap;
	r->fill -= n;
	pthread_mutex_unlock(&r->lock);
	return (int)n;
}

static void ring_flush(sdr_ring *r)
{
	pthread_mutex_lock(&r->lock);
	r->head = r->fill = 0;
	pthread_mutex_unlock(&r->lock);
}

static void ring_set_running(sdr_ring *r, int on)
{
	pthread_mutex_lock(&r->lock);
	r->running = on;
	if (on) {
		r->head = r->fill = 0;
		r->lost = 0;
	}
	pthread_cond_broadcast(&r->cond);
	pthread_mutex_unlock(&r->lock);
}

static void ring_lost(sdr_ring *r)
{
	pthread_mutex_lock(&r->lock);
	r->lost = 1;
	r->running = 0;
	pthread_cond_broadcast(&r->cond);
	pthread_mutex_unlock(&r->lock);
}

#endif
