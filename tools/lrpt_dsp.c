/*
 * lrpt_dsp.c — тяжёлые циклы декодера «Метеор-М» LRPT для SDR4NOOB.
 *
 *   * демодулятор QPSK/OQPSK: АРУ, петля Костаса (2-го порядка), символьная синхронизация
 *     Гарднера с кубической интерполяцией, мягкие символы int8;
 *   * декодер Витерби k=7 r=1/2 (полиномы 0x4F/0x6D, мягкие решения);
 *   * свёрточный кодер (для синтетики и оценки BER);
 *   * Рида — Соломона (255,223) CCSDS: GF(2^8) по 0x187, fcr=112, prim=11, 32 проверочных символа,
 *     опционально — дуальный базис.
 *
 * Сборка: tools\build_lrpt_dsp.bat (cl /O2 /LD) -> rtltv\driver\lrpt_dsp.dll
 *
 * Библиотека не зависит от CRT и Windows SDK (/NODEFAULTLIB /NOENTRY): память под состояние и
 * буферы выделяет вызывающая сторона (Python/numpy), математика — свои функции ниже.
 */
#include <stdint.h>

int _fltused = 0;

#pragma function(memset, memcpy)
void *memset(void *d, int c, size_t n)
{
    unsigned char *p = (unsigned char *)d;
    while (n--)
        *p++ = (unsigned char)c;
    return d;
}

void *memcpy(void *d, const void *s, size_t n)
{
    unsigned char *a = (unsigned char *)d;
    const unsigned char *b = (const unsigned char *)s;
    while (n--)
        *a++ = *b++;
    return d;
}

static void *memmove_(void *d, const void *s, size_t n)
{
    unsigned char *a = (unsigned char *)d;
    const unsigned char *b = (const unsigned char *)s;
    if (a < b) {
        while (n--)
            *a++ = *b++;
    } else {
        a += n;
        b += n;
        while (n--)
            *--a = *--b;
    }
    return d;
}
#define memmove memmove_

float sqrtf(float);
#pragma intrinsic(sqrtf)

static inline double fabs_(double x) { return x < 0 ? -x : x; }
static inline float fabsf_(float x) { return x < 0 ? -x : x; }
#define fabs fabs_
#define fabsf fabsf_

/* sin двойной точности: приведение к [-pi, pi], отражение к [-pi/2, pi/2], ряд до x^13 */
static double sin_(double x)
{
    const double PI = 3.141592653589793, TP = 6.283185307179586;
    if (x > PI || x < -PI)
        x -= TP * (double)(long long)((x + (x > 0 ? PI : -PI)) / TP);
    if (x > PI / 2)
        x = PI - x;
    else if (x < -PI / 2)
        x = -PI - x;
    double x2 = x * x;
    return x * (1.0 - x2 / 6.0 * (1.0 - x2 / 20.0 * (1.0 - x2 / 42.0 * (1.0 - x2 / 72.0 *
               (1.0 - x2 / 110.0 * (1.0 - x2 / 156.0))))));
}

static double cos_(double x) { return sin_(x + 1.5707963267948966); }
#define sin sin_
#define cos cos_

static inline long lrintf_(float v) { return (long)(v >= 0 ? v + 0.5f : v - 0.5f); }
#define lrintf lrintf_

#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API
#endif

#define TWO_PI 6.283185307179586

/* ------------------------------------------------------------------ демодулятор */

typedef struct {
    int mode;          /* 0 — QPSK, 1 — OQPSK, 2 — BPSK */
    double sps;        /* отсчётов на символ на входе */
    double half;       /* номинальный полусимвол */
    double interval;   /* текущий полусимвол */
    double tfreq;      /* интегратор петли синхронизации */
    double t_next;     /* время следующего полуотсчёта относительно ring[1] */
    double ta, tb;     /* коэффициенты петли синхронизации */
    double ca, cb;     /* коэффициенты петли Костаса */
    double phase, freq, fmax;
    double agc, agc_rate;
    float rr[4], ri[4];        /* последние 4 повёрнутых отсчёта */
    int nfill;
    float hr[3], hi[3];        /* последние 3 полуотсчёта */
    int hidx;
    float pend_i;
    double amp;                /* средний |I|+|Q| в центрах символов */
    double lock;               /* сглаженная метрика захвата 0..1 */
} demod_t;

static void loop_gains(double bw, double *a, double *b)
{
    double zeta = 0.7071, theta = bw / (zeta + 0.25 / zeta);
    double d = 1.0 + 2.0 * zeta * theta + theta * theta;
    *a = 4.0 * zeta * theta / d;
    *b = 4.0 * theta * theta / d;
}

API int demod_size(void) { return (int)sizeof(demod_t); }

/* mem — буфер demod_size() байт от вызывающей стороны */
API void *demod_init(void *mem, double sps, int mode, double costas_bw, double timing_bw, double fmax_rad)
{
    demod_t *d = (demod_t *)mem;
    memset(d, 0, sizeof(demod_t));
    d->mode = mode;
    d->sps = sps;
    d->half = sps / 2.0;
    d->interval = d->half;
    loop_gains(costas_bw, &d->ca, &d->cb);
    loop_gains(timing_bw, &d->ta, &d->tb);
    d->fmax = fmax_rad;
    d->agc = 1.0;
    d->agc_rate = 1e-3;
    d->amp = 1.0;
    return d;
}

API void demod_set_costas(void *p, double bw)
{
    demod_t *d = (demod_t *)p;
    loop_gains(bw, &d->ca, &d->cb);
}

API void demod_set_freq(void *p, double freq_rad)
{
    demod_t *d = (demod_t *)p;
    d->freq = freq_rad;
}

/* state: [freq рад/отсчёт, phase, interval/half, agc, amp, lock] */
API void demod_state(void *p, double *out)
{
    demod_t *d = (demod_t *)p;
    out[0] = d->freq;
    out[1] = d->phase;
    out[2] = d->interval / d->half;
    out[3] = d->agc;
    out[4] = d->amp;
    out[5] = d->lock;
}

static inline float cubic(const float *y, float mu)
{
    /* Лагранж 3-го порядка по y[0..3], точка между y[1] и y[2] */
    float c0 = y[1];
    float c1 = -y[0] / 3.0f - y[1] / 2.0f + y[2] - y[3] / 6.0f;
    float c2 = (y[0] + y[2]) / 2.0f - y[1];
    float c3 = (y[3] - y[0]) / 6.0f + (y[1] - y[2]) / 2.0f;
    return ((c3 * mu + c2) * mu + c1) * mu + c0;
}

static inline int8_t clip8(float v)
{
    if (v > 127.0f)
        return 127;
    if (v < -127.0f)
        return -127;
    return (int8_t)lrintf(v);
}

static void costas(demod_t *d, float I, float Q)
{
    float sI = I >= 0 ? 1.0f : -1.0f, sQ = Q >= 0 ? 1.0f : -1.0f;
    double n = d->amp > 1e-9 ? d->amp : 1.0;
    double e = d->mode == 2 ? sI * Q / n : (sI * Q - sQ * I) / n; /* ≈ sin(фазовая ошибка) */
    if (e > 1.0)
        e = 1.0;
    if (e < -1.0)
        e = -1.0;
    d->freq += d->cb * e / d->sps;
    if (d->freq > d->fmax)
        d->freq = d->fmax;
    if (d->freq < -d->fmax)
        d->freq = -d->fmax;
    d->phase += d->ca * e;
    /* метрика захвата 0..1. QPSK: 2|I||Q|/(I²+Q²) = 1 на диагоналях, ≈0,64 при случайной фазе;
       BPSK: (I²-Q²)/(I²+Q²) = 1 на оси I, ≈0 при случайной фазе */
    {
        double p = (double)I * I + (double)Q * Q, m;
        if (d->mode == 2)
            m = p > 1e-12 ? ((double)I * I - (double)Q * Q) / p : 0.0;
        else
            m = p > 1e-12 ? (2.0 * fabs(I) * fabs(Q) / p - 0.64) / 0.36 : 0.0;
        d->lock += 0.002 * (m - d->lock);
    }
    if (d->mode == 2)
        d->amp += 0.002 * (fabsf(I) - d->amp);
    else
        d->amp += 0.002 * ((fabsf(I) + fabsf(Q)) * 0.5 - d->amp);
}

static void timing(demod_t *d, double e)
{
    double n = d->amp > 1e-9 ? d->amp * d->amp : 1.0;
    e /= n;
    if (e > 1.0)
        e = 1.0;
    if (e < -1.0)
        e = -1.0;
    d->tfreq += d->tb * e;
    if (d->tfreq > 0.01 * d->half)
        d->tfreq = 0.01 * d->half;
    if (d->tfreq < -0.01 * d->half)
        d->tfreq = -0.01 * d->half;
    d->interval = d->half - d->tfreq * d->half;
    d->t_next -= d->ta * e * d->half;
}

/*
 * iq — комплексные отсчёты (float re,im) с частотой sps*Rs, после согласованного фильтра RRC.
 * soft_out — мягкие биты (I,Q,I,Q,...) int8; cons — точки созвездия (float I,Q) нормированные.
 * Возвращает число символов: для QPSK/OQPSK — пар мягких бит, для BPSK — по одному мягкому биту (I).
 */
API int demod_process(void *p, const float *iq, int n, int8_t *soft_out, float *cons, int max_sym)
{
    demod_t *d = (demod_t *)p;
    int nsym = 0;
    for (int i = 0; i < n; i++) {
        float xr = iq[2 * i] * (float)d->agc, xi = iq[2 * i + 1] * (float)d->agc;
        float mag = sqrtf(xr * xr + xi * xi);
        d->agc += d->agc_rate * (1.0 - mag) * d->agc;
        if (d->agc < 1e-6)
            d->agc = 1e-6;
        if (d->agc > 1e6)
            d->agc = 1e6;
        float c = (float)cos(d->phase), s = (float)sin(d->phase);
        float yr = xr * c + xi * s, yi = xi * c - xr * s;
        d->phase += d->freq;
        if (d->phase > TWO_PI)
            d->phase -= TWO_PI;
        else if (d->phase < -TWO_PI)
            d->phase += TWO_PI;
        d->rr[0] = d->rr[1]; d->rr[1] = d->rr[2]; d->rr[2] = d->rr[3]; d->rr[3] = yr;
        d->ri[0] = d->ri[1]; d->ri[1] = d->ri[2]; d->ri[2] = d->ri[3]; d->ri[3] = yi;
        if (d->nfill < 4) {
            d->nfill++;
            continue;
        }
        d->t_next -= 1.0;
        while (d->t_next < 1.0) {
            float mu = (float)d->t_next;
            if (mu < 0.0f)
                mu = 0.0f;
            float hr = cubic(d->rr, mu), hi = cubic(d->ri, mu);
            d->hr[0] = d->hr[1]; d->hr[1] = d->hr[2]; d->hr[2] = hr;
            d->hi[0] = d->hi[1]; d->hi[1] = d->hi[2]; d->hi[2] = hi;
            d->hidx ^= 1;
            d->t_next += d->interval;
            if (d->mode == 2) {
                if (d->hidx == 0) {
                    double e = d->hr[1] * (d->hr[2] - d->hr[0]);
                    timing(d, e);
                    costas(d, hr, hi);
                    if (nsym < max_sym) {
                        float k = (float)(48.0 / (d->amp > 1e-9 ? d->amp : 1.0));
                        soft_out[nsym] = clip8(hr * k);
                        if (cons) {
                            cons[2 * nsym] = hr / (float)(d->amp + 1e-9);
                            cons[2 * nsym + 1] = hi / (float)(d->amp + 1e-9);
                        }
                        nsym++;
                    }
                }
            } else if (d->mode == 0) {
                if (d->hidx == 0) {
                    double e = d->hr[1] * (d->hr[2] - d->hr[0]) + d->hi[1] * (d->hi[2] - d->hi[0]);
                    timing(d, e);
                    costas(d, hr, hi);
                    if (nsym < max_sym) {
                        float k = (float)(48.0 / (d->amp > 1e-9 ? d->amp : 1.0));
                        soft_out[2 * nsym] = clip8(hr * k);
                        soft_out[2 * nsym + 1] = clip8(hi * k);
                        if (cons) {
                            cons[2 * nsym] = hr / (float)(d->amp * 1.4142 + 1e-9);
                            cons[2 * nsym + 1] = hi / (float)(d->amp * 1.4142 + 1e-9);
                        }
                        nsym++;
                    }
                }
            } else {
                if (d->hidx == 0) {
                    double e = d->hr[1] * (d->hr[2] - d->hr[0]);
                    timing(d, 2.0 * e);
                    d->pend_i = hr;
                } else {
                    double e = d->hi[1] * (d->hi[2] - d->hi[0]);
                    timing(d, 2.0 * e);
                    float I = d->pend_i, Q = hi;
                    costas(d, I, Q);
                    if (nsym < max_sym) {
                        float k = (float)(48.0 / (d->amp > 1e-9 ? d->amp : 1.0));
                        soft_out[2 * nsym] = clip8(I * k);
                        soft_out[2 * nsym + 1] = clip8(Q * k);
                        if (cons) {
                            cons[2 * nsym] = I / (float)(d->amp * 1.4142 + 1e-9);
                            cons[2 * nsym + 1] = Q / (float)(d->amp * 1.4142 + 1e-9);
                        }
                        nsym++;
                    }
                }
            }
        }
    }
    return nsym;
}

/* ------------------------------------------------------------------ свёрточный код */

#define POLYA 0x4F
#define POLYB 0x6D

static inline int parity(unsigned v)
{
    v ^= v >> 4;
    v &= 0xF;
    return (0x6996 >> v) & 1;
}

/* bits: n бит (0/1) -> out: 2n бит; state — 6 бит истории (вход/выход) */
API int conv_encode(const uint8_t *bits, int n, uint8_t *out, int state)
{
    unsigned reg = (unsigned)state & 0x3F;
    for (int i = 0; i < n; i++) {
        reg = ((reg << 1) | (bits[i] & 1)) & 0x7F;
        out[2 * i] = (uint8_t)parity(reg & POLYA);
        out[2 * i + 1] = (uint8_t)parity(reg & POLYB);
    }
    return (int)(reg & 0x3F);
}

/*
 * Мягкий Витерби: soft — 2*npairs значений int8 (>0 = бит 1), out — npairs бит (0/1).
 * dec — рабочий буфер npairs*8 байт. Трассировка от лучшего состояния в конце окна
 * (окна перекрываются в Python).
 * Возвращает нормированную метрику лучшего пути (для оценки качества), *100.
 */
API int viterbi27(const int8_t *soft, int npairs, uint8_t *out, uint64_t *dec)
{
    static int inited = 0;
    static int8_t exp0[128], exp1[128];
    if (!inited) {
        for (int r = 0; r < 128; r++) {
            exp0[r] = parity(r & POLYA) ? 1 : -1;
            exp1[r] = parity(r & POLYB) ? 1 : -1;
        }
        inited = 1;
    }
    if (npairs <= 0)
        return 0;
    int32_t m0[64], m1[64];
    int32_t *cur = m0, *nxt = m1;
    for (int s = 0; s < 64; s++)
        cur[s] = 0;
    long long total = 0;
    for (int t = 0; t < npairs; t++) {
        int a = soft[2 * t], b = soft[2 * t + 1];
        uint64_t dv = 0;
        for (int ns = 0; ns < 64; ns++) {
            int bit = ns & 1;
            int p0 = ns >> 1, p1 = (ns >> 1) | 32;
            int r0 = ((p0 << 1) | bit) & 0x7F, r1 = ((p1 << 1) | bit) & 0x7F;
            int32_t v0 = cur[p0] + exp0[r0] * a + exp1[r0] * b;
            int32_t v1 = cur[p1] + exp0[r1] * a + exp1[r1] * b;
            if (v1 > v0) {
                nxt[ns] = v1;
                dv |= (uint64_t)1 << ns;
            } else {
                nxt[ns] = v0;
            }
        }
        dec[t] = dv;
        int32_t mx = nxt[0];
        for (int s = 1; s < 64; s++)
            if (nxt[s] > mx)
                mx = nxt[s];
        if (mx > (1 << 28) || mx < -(1 << 28) || (t & 255) == 255) {
            for (int s = 0; s < 64; s++)
                nxt[s] -= mx;
            total += mx;
        }
        int32_t *tmp = cur;
        cur = nxt;
        nxt = tmp;
    }
    int best = 0;
    for (int s = 1; s < 64; s++)
        if (cur[s] > cur[best])
            best = s;
    total += cur[best];
    int st = best;
    for (int t = npairs - 1; t >= 0; t--) {
        out[t] = (uint8_t)(st & 1);
        int k = (int)((dec[t] >> st) & 1);
        st = (st >> 1) | (k << 5);
    }
    return (int)(total * 100 / npairs);
}

/* ------------------------------------------------------------------ Рид — Соломон */

#define NN 255
#define NROOTS 32
#define FCR 112
#define PRIM 11
#define IPRIM 116
#define A0 NN

static uint8_t alpha_to[256], index_of[256], genpoly[NROOTS + 1];
static uint8_t taltab[256], tal1tab[256];
static int rs_ready = 0;

static int modnn(int x)
{
    while (x >= NN) {
        x -= NN;
        x = (x >> 8) + (x & NN);
    }
    return x;
}

static void rs_init(void)
{
    int sr = 1;
    index_of[0] = A0;
    alpha_to[A0] = 0;
    for (int i = 0; i < NN; i++) {
        index_of[sr] = (uint8_t)i;
        alpha_to[i] = (uint8_t)sr;
        sr <<= 1;
        if (sr & 256)
            sr ^= 0x187;
        sr &= 255;
    }
    genpoly[0] = 1;
    for (int i = 0, root = FCR * PRIM; i < NROOTS; i++, root += PRIM) {
        genpoly[i + 1] = 1;
        for (int j = i; j > 0; j--) {
            if (genpoly[j] != 0)
                genpoly[j] = genpoly[j - 1] ^ alpha_to[modnn(index_of[genpoly[j]] + root)];
            else
                genpoly[j] = genpoly[j - 1];
        }
        genpoly[0] = alpha_to[modnn(index_of[genpoly[0]] + root)];
    }
    for (int i = 0; i <= NROOTS; i++)
        genpoly[i] = index_of[genpoly[i]];
    /* таблицы перевода в дуальный базис (CCSDS 131.0-B, приложение) */
    static const uint8_t tal[] = {0x8d, 0xef, 0xec, 0x86, 0xfa, 0x99, 0xaf, 0x7b};
    for (int i = 0; i < 256; i++) {
        taltab[i] = 0;
        for (int j = 0; j < 8; j++)
            if (i & (1 << j))
                taltab[i] ^= tal[7 - j];
        tal1tab[taltab[i]] = (uint8_t)i;
    }
    rs_ready = 1;
}

/* Кодирование 223 байт -> 32 проверочных (для синтетики). */
API void rs_encode(const uint8_t *data, uint8_t *parity_out, int dual)
{
    if (!rs_ready)
        rs_init();
    uint8_t bb[NROOTS];
    memset(bb, 0, sizeof(bb));
    for (int i = 0; i < NN - NROOTS; i++) {
        uint8_t dch = dual ? tal1tab[data[i]] : data[i];
        int feedback = index_of[dch ^ bb[0]];
        if (feedback != A0) {
            for (int j = 1; j < NROOTS; j++)
                bb[j] ^= alpha_to[modnn(feedback + genpoly[NROOTS - j])];
        }
        memmove(&bb[0], &bb[1], NROOTS - 1);
        bb[NROOTS - 1] = feedback != A0 ? alpha_to[modnn(feedback + genpoly[0])] : 0;
    }
    for (int i = 0; i < NROOTS; i++)
        parity_out[i] = dual ? taltab[bb[i]] : bb[i];
}

/* Декодирование кодового слова 255 байт на месте. Возвращает число исправленных байт или -1. */
API int rs_decode(uint8_t *data, int dual)
{
    if (!rs_ready)
        rs_init();
    uint8_t buf[NN];
    if (dual) {
        for (int i = 0; i < NN; i++)
            buf[i] = tal1tab[data[i]];
    } else {
        memcpy(buf, data, NN);
    }
    int deg_lambda, el, deg_omega, i, j, r, k;
    uint8_t u, q, tmp, num1, num2, den, discr_r;
    uint8_t lambda[NROOTS + 1], s[NROOTS], b[NROOTS + 1], t[NROOTS + 1], omega[NROOTS + 1];
    uint8_t root[NROOTS], reg[NROOTS + 1], loc[NROOTS];
    int syn_error, count;

    for (i = 0; i < NROOTS; i++)
        s[i] = buf[0];
    for (j = 1; j < NN; j++) {
        for (i = 0; i < NROOTS; i++) {
            if (s[i] == 0)
                s[i] = buf[j];
            else
                s[i] = buf[j] ^ alpha_to[modnn(index_of[s[i]] + (FCR + i) * PRIM)];
        }
    }
    syn_error = 0;
    for (i = 0; i < NROOTS; i++) {
        syn_error |= s[i];
        s[i] = index_of[s[i]];
    }
    if (!syn_error)
        return 0;

    memset(&lambda[1], 0, NROOTS);
    lambda[0] = 1;
    for (i = 0; i < NROOTS + 1; i++)
        b[i] = index_of[lambda[i]];
    r = 0;
    el = 0;
    while (++r <= NROOTS) {
        discr_r = 0;
        for (i = 0; i < r; i++) {
            if ((lambda[i] != 0) && (s[r - i - 1] != A0))
                discr_r ^= alpha_to[modnn(index_of[lambda[i]] + s[r - i - 1])];
        }
        discr_r = index_of[discr_r];
        if (discr_r == A0) {
            memmove(&b[1], b, NROOTS);
            b[0] = A0;
        } else {
            t[0] = lambda[0];
            for (i = 0; i < NROOTS; i++) {
                if (b[i] != A0)
                    t[i + 1] = lambda[i + 1] ^ alpha_to[modnn(discr_r + b[i])];
                else
                    t[i + 1] = lambda[i + 1];
            }
            if (2 * el <= r - 1) {
                el = r - el;
                for (i = 0; i <= NROOTS; i++)
                    b[i] = (lambda[i] == 0) ? A0 : (uint8_t)modnn(index_of[lambda[i]] - discr_r + NN);
            } else {
                memmove(&b[1], b, NROOTS);
                b[0] = A0;
            }
            memcpy(lambda, t, NROOTS + 1);
        }
    }
    deg_lambda = 0;
    for (i = 0; i < NROOTS + 1; i++) {
        lambda[i] = index_of[lambda[i]];
        if (lambda[i] != A0)
            deg_lambda = i;
    }
    memcpy(&reg[1], &lambda[1], NROOTS);
    count = 0;
    for (i = 1, k = IPRIM - 1; i <= NN; i++, k = modnn(k + IPRIM)) {
        q = 1;
        for (j = deg_lambda; j > 0; j--) {
            if (reg[j] != A0) {
                reg[j] = (uint8_t)modnn(reg[j] + j);
                q ^= alpha_to[reg[j]];
            }
        }
        if (q != 0)
            continue;
        root[count] = (uint8_t)i;
        loc[count] = (uint8_t)k;
        if (++count == deg_lambda)
            break;
    }
    if (deg_lambda != count)
        return -1;
    deg_omega = deg_lambda - 1;
    for (i = 0; i <= deg_omega; i++) {
        tmp = 0;
        for (j = i; j >= 0; j--) {
            if ((s[i - j] != A0) && (lambda[j] != A0))
                tmp ^= alpha_to[modnn(s[i - j] + lambda[j])];
        }
        omega[i] = index_of[tmp];
    }
    for (j = count - 1; j >= 0; j--) {
        num1 = 0;
        for (i = deg_omega; i >= 0; i--) {
            if (omega[i] != A0)
                num1 ^= alpha_to[modnn(omega[i] + i * root[j])];
        }
        num2 = alpha_to[modnn(root[j] * (FCR - 1) + NN)];
        den = 0;
        for (i = (deg_lambda < NROOTS - 1 ? deg_lambda : NROOTS - 1) & ~1; i >= 0; i -= 2) {
            if (lambda[i + 1] != A0)
                den ^= alpha_to[modnn(lambda[i + 1] + i * root[j])];
        }
        if (den == 0)
            return -1;
        if (num1 != 0) {
            u = alpha_to[modnn(index_of[num1] + index_of[num2] + NN - index_of[den])];
            buf[loc[j]] ^= u;
        }
    }
    if (dual) {
        for (i = 0; i < NN; i++)
            data[i] = taltab[buf[i]];
    } else {
        memcpy(data, buf, NN);
    }
    return count;
}
