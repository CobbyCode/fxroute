/* SPDX-License-Identifier: AGPL-3.0-only */
#include "upmix_fast.h"
#include "upmix_hq.h"

#include <assert.h>
#include <fenv.h>
#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define RATE 48000U
#define PI 3.14159265358979323846
#define CH FX_UPMIX_HQ_CHANNELS

typedef struct { float *c[CH]; size_t frames; } bus;

static bus bus_new(size_t frames) {
    bus b = {.frames = frames};
    for (unsigned i = 0; i < CH; i++) { b.c[i] = calloc(frames, sizeof(float)); assert(b.c[i]); }
    return b;
}
static void bus_free(bus *b) { for (unsigned i = 0; i < CH; i++) free(b->c[i]); }

static void run(fx_upmix_hq *u, const bus *in, bus *out, size_t chunk) {
    for (size_t offset = 0; offset < in->frames; offset += chunk) {
        const float *ip[CH]; float *op[CH];
        size_t count = in->frames - offset < chunk ? in->frames - offset : chunk;
        for (unsigned i = 0; i < CH; i++) { ip[i] = in->c[i] + offset; op[i] = out->c[i] + offset; }
        fx_upmix_hq_process(u, ip, op, count);
    }
}

static void run_fast(fx_upmix_fast *u, const bus *in, bus *out, size_t chunk) {
    for (size_t offset = 0; offset < in->frames; offset += chunk) {
        const float *ip[CH]; float *op[CH];
        size_t count = in->frames - offset < chunk ? in->frames - offset : chunk;
        for (unsigned i = 0; i < CH; i++) { ip[i] = in->c[i] + offset; op[i] = out->c[i] + offset; }
        fx_upmix_fast_process(u, ip, op, count);
    }
}

static uint32_t rng = 2463534242U;
static float uniform(void) {
    rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5;
    return (float)rng / 2147483648.0F - 1.0F;
}
/* Gaussian-like noise with the given RMS. */
static float noise(float rms) {
    float sum = 0.0F;
    for (unsigned i = 0; i < 6U; i++) sum += uniform();
    return sum * rms / 1.41421356F;
}

static double mean_square(const float *x, size_t begin, size_t count) {
    double sum = 0.0;
    for (size_t i = begin; i < begin + count; i++) sum += (double)x[i] * x[i];
    return sum / (double)count;
}
static double correlation(const float *a, const float *b, size_t begin, size_t count) {
    double ab = 0.0, aa = 0.0, bb = 0.0;
    for (size_t i = begin; i < begin + count; i++) { ab += (double)a[i] * b[i]; aa += (double)a[i] * a[i]; bb += (double)b[i] * b[i]; }
    return aa > 0.0 && bb > 0.0 ? ab / sqrt(aa * bb) : 0.0;
}
/* Correlation of a[i] with reference[i + shift]. */
static double shifted_correlation(const float *a, const float *reference, size_t begin, size_t count, ptrdiff_t shift) {
    double ab = 0.0, aa = 0.0, bb = 0.0;
    for (size_t i = begin; i < begin + count; i++) {
        double x = a[i], y = reference[(ptrdiff_t)i + shift];
        ab += x * y; aa += x * x; bb += y * y;
    }
    return ab / sqrt(aa * bb);
}
static double max_abs_diff(const float *a, const float *b, size_t count) {
    double worst = 0.0;
    for (size_t i = 0; i < count; i++) if (fabs((double)a[i] - b[i]) > worst) worst = fabs((double)a[i] - b[i]);
    return worst;
}
static double db(double ratio) { return 10.0 * log10(ratio); }

#define FRAMES (4U * RATE)
#define SETTLED (2U * RATE)
#define SETTLED_FRAMES (2U * RATE)
static const unsigned FFT_SIZES[3] = {1024U, 2048U, 4096U};

/* Band-limited noise: random-phase spectrum inside [low, high] Hz, synthesised
 * with an inverse FFT, one period of NOISE_FRAMES samples repeated. */
#define NOISE_FRAMES 65536U

static void fft_in_place(double *re, double *im, unsigned size, int inverse) {
    for (unsigned i = 1, j = 0; i < size; i++) {
        unsigned bit = size >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) { double t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; }
    }
    for (unsigned length = 2U; length <= size; length <<= 1) {
        double angle = (inverse ? 2.0 : -2.0) * PI / length;
        for (unsigned start = 0; start < size; start += length)
            for (unsigned k = 0; k < length / 2U; k++) {
                double wr = cos(angle * k), wi = sin(angle * k);
                unsigned a = start + k, b = a + length / 2U;
                double tr = re[b] * wr - im[b] * wi, ti = re[b] * wi + im[b] * wr;
                re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti;
            }
    }
}

/* Adds `rms` worth of band noise to target[0..frames) (repeating the period). */
static void add_band_noise(float *target, size_t frames, double low, double high, double rms) {
    static double re[NOISE_FRAMES], im[NOISE_FRAMES];
    double power = 0.0;
    memset(re, 0, sizeof re); memset(im, 0, sizeof im);
    for (unsigned k = 1; k < NOISE_FRAMES / 2U; k++) {
        double hz = (double)k * RATE / NOISE_FRAMES, phase;
        if (hz < low || hz > high) continue;
        phase = PI * uniform();
        re[k] = cos(phase); im[k] = sin(phase);
        re[NOISE_FRAMES - k] = re[k]; im[NOISE_FRAMES - k] = -im[k];
    }
    fft_in_place(re, im, NOISE_FRAMES, 1);
    for (unsigned n = 0; n < NOISE_FRAMES; n++) power += re[n] * re[n];
    power = sqrt(power / NOISE_FRAMES);
    for (size_t n = 0; n < frames; n++) target[n] += (float)(re[n % NOISE_FRAMES] * rms / power);
}

/* Power of x inside [low, high] Hz over `count` samples (a power of two). */
static double band_power(const float *x, size_t begin, unsigned count, double low, double high) {
    static double re[NOISE_FRAMES], im[NOISE_FRAMES];
    double sum = 0.0;
    assert(count <= NOISE_FRAMES);
    for (unsigned n = 0; n < count; n++) { re[n] = (double)x[begin + n] * (0.5 - 0.5 * cos(2.0 * PI * n / count)); im[n] = 0.0; }
    fft_in_place(re, im, count, 0);
    for (unsigned k = 1; k < count / 2U; k++) {
        double hz = (double)k * RATE / count;
        if (hz >= low && hz <= high) sum += re[k] * re[k] + im[k] * im[k];
    }
    return sum;
}

/* Energy-vector azimuth of FL (-30), FR (+30) and FC (0) from channel powers. */
static double front_azimuth(double left, double right, double center) {
    double sine = (right - left) * 0.5, cosine = (left + right) * 0.866025403784 + center;
    return atan2(sine, cosine) * 180.0 / PI;
}

static void test_fft_sizes_and_rates_are_validated_and_latency_is_fft_size_minus_one(void) {
    unsigned rates[] = {44100U, 48000U, 96000U, 192000U};
    assert(fx_upmix_hq_fft_size_valid(1024U) && fx_upmix_hq_fft_size_valid(2048U) && fx_upmix_hq_fft_size_valid(4096U));
    assert(!fx_upmix_hq_fft_size_valid(0U) && !fx_upmix_hq_fft_size_valid(512U) && !fx_upmix_hq_fft_size_valid(3000U) &&
           !fx_upmix_hq_fft_size_valid(8192U));
    for (unsigned size = 0; size < 3U; size++)
        for (unsigned i = 0; i < 4U; i++) {
            fx_upmix_hq *u = fx_upmix_hq_create(rates[i], FFT_SIZES[size], 1, 1, 1);
            assert(u && fx_upmix_hq_latency(u) == FFT_SIZES[size] - 1U);
            fx_upmix_hq_destroy(u);
        }
    assert(fx_upmix_hq_create(100U, 2048U, 1, 1, 1) == NULL);
    assert(fx_upmix_hq_create(RATE, 1000U, 1, 1, 1) == NULL);
    assert(fx_upmix_hq_latency(NULL) == 0 && fx_upmix_hq_engagement(NULL) == 0.0F);
    fx_upmix_hq_destroy(NULL);
}

/* A hard-panned impulse carries no steering, so the engaged upmixer must hand it back sample exact. */
static void test_every_channel_is_delayed_by_exactly_the_latency(void) {
    for (unsigned size = 0; size < 3U; size++) {
        fx_upmix_hq *u = fx_upmix_hq_create(RATE, FFT_SIZES[size], 1, 1, 1);
        size_t latency = fx_upmix_hq_latency(u), frames = 3U * FFT_SIZES[size];
        bus in = bus_new(frames), out = bus_new(frames);
        for (unsigned channel = 0; channel < CH; channel++) {
            fx_upmix_hq_reset(u);
            in.c[channel][100] = 0.5F;
            run(u, &in, &out, 509U);
            for (unsigned other = 0; other < CH; other++)
                for (size_t n = 0; n < frames; n++) {
                    float expected = other == channel && n == 100U + latency ? 0.5F : 0.0F;
                    assert(out.c[other][n] == expected);
                }
            in.c[channel][100] = 0.0F;
        }
        fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
    }
}

/* The derived Center is the same signal as the delayed front, at the same sample position. */
static void test_derived_channels_are_aligned_with_the_delayed_front(void) {
    for (unsigned size = 0; size < 3U; size++) {
        fx_upmix_hq *u = fx_upmix_hq_create(RATE, FFT_SIZES[size], 1, 1, 1);
        bus in = bus_new(FRAMES), out = bus_new(FRAMES);
        size_t latency = fx_upmix_hq_latency(u), begin = SETTLED;
        double best = -1.0; int best_lag = 99;
        fx_upmix_hq_set_strength(u, 10.0F, 10.0F);
        for (size_t n = 0; n < FRAMES; n++) in.c[0][n] = in.c[1][n] = noise(0.1F);
        run(u, &in, &out, 509U);
        for (int lag = -3; lag <= 3; lag++) {
            double c = shifted_correlation(out.c[2], in.c[0], begin, SETTLED_FRAMES, lag - (ptrdiff_t)latency);
            if (c > best) { best = c; best_lag = lag; }
        }
        assert(best_lag == 0 && best > 0.998);
        fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
    }
}

static void test_mono_goes_to_center_and_leaves_the_front(void) {
    for (unsigned size = 0; size < 3U; size++) {
        fx_upmix_hq *u = fx_upmix_hq_create(RATE, FFT_SIZES[size], 1, 1, 1);
        bus in = bus_new(FRAMES), out = bus_new(FRAMES);
        double input_power;
        fx_upmix_hq_set_strength(u, 10.0F, 10.0F);
        for (size_t n = 0; n < FRAMES; n++) in.c[0][n] = in.c[1][n] = noise(0.1F);
        run(u, &in, &out, 509U);
        input_power = mean_square(in.c[0], SETTLED, SETTLED_FRAMES);
        /* The centre takes the whole mid component: |C|^2 = |L+R|^2 / 2 = 2 P. */
        assert(fabs(db(mean_square(out.c[2], SETTLED, SETTLED_FRAMES) / (2.0 * input_power))) < 1.0);
        assert(mean_square(out.c[0], SETTLED, SETTLED_FRAMES) < 0.01 * input_power);
        assert(mean_square(out.c[1], SETTLED, SETTLED_FRAMES) < 0.01 * input_power);
        for (unsigned channel = 4U; channel < CH; channel++) assert(mean_square(out.c[channel], SETTLED, SETTLED_FRAMES) < 1e-4 * input_power);
        assert(mean_square(out.c[3], 0, FRAMES) == 0.0);
        fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
    }
}

static void test_without_a_center_output_the_centre_image_stays_in_front(void) {
    fx_upmix_hq *u = fx_upmix_hq_create(RATE, 2048U, 0, 0, 1);
    bus in = bus_new(FRAMES), out = bus_new(FRAMES);
    size_t latency = fx_upmix_hq_latency(u);
    fx_upmix_hq_set_strength(u, 10.0F, 10.0F);
    for (size_t n = 0; n < FRAMES; n++) in.c[0][n] = in.c[1][n] = noise(0.1F);
    run(u, &in, &out, 509U);
    assert(mean_square(out.c[2], 0, FRAMES) == 0.0);
    /* Fully correlated material has no surround content either: L/R are untouched. */
    assert(max_abs_diff(out.c[0] + latency, in.c[0], FRAMES - latency) < 1e-6);
    assert(max_abs_diff(out.c[1] + latency, in.c[1], FRAMES - latency) < 1e-6);
    fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_surround_stays_active_without_a_center(void) {
    fx_upmix_hq *u = fx_upmix_hq_create(RATE, 2048U, 0, 0, 1);
    bus in = bus_new(FRAMES), out = bus_new(FRAMES);
    double input_power;
    fx_upmix_hq_set_strength(u, 10.0F, 10.0F);
    for (size_t n = 0; n < FRAMES; n++) { in.c[0][n] = noise(0.1F); in.c[1][n] = noise(0.1F); }
    run(u, &in, &out, 509U);
    input_power = mean_square(in.c[0], SETTLED, SETTLED_FRAMES);
    assert(mean_square(out.c[2], 0, FRAMES) == 0.0);
    assert(mean_square(out.c[4], 0, FRAMES) == 0.0 && mean_square(out.c[5], 0, FRAMES) == 0.0);
    assert(mean_square(out.c[6], SETTLED, SETTLED_FRAMES) > 0.5 * input_power);
    assert(mean_square(out.c[7], SETTLED, SETTLED_FRAMES) > 0.5 * input_power);
    assert(fabs(correlation(out.c[6], out.c[7], SETTLED, SETTLED_FRAMES)) < 0.1);
    fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
}

/* With one pair present it gets the full surround power; with both the more diffuse bin goes to the rears. */
static void test_side_only_and_rear_only_get_full_power_and_both_split_it_by_diffuseness(void) {
    double power[3], side_in_both = 0.0, rear_in_both = 0.0;
    unsigned flags[3][2] = {{1, 0}, {0, 1}, {1, 1}};
    for (unsigned variant = 0; variant < 3U; variant++) {
        fx_upmix_hq *u = fx_upmix_hq_create(RATE, 2048U, 1, flags[variant][0], flags[variant][1]);
        bus in = bus_new(FRAMES), out = bus_new(FRAMES);
        fx_upmix_hq_set_strength(u, 10.0F, 10.0F);
        for (size_t n = 0; n < FRAMES; n++) { float x = noise(0.1F); in.c[0][n] = x; in.c[1][n] = -x; }
        run(u, &in, &out, 509U);
        power[variant] = flags[variant][0] ? mean_square(out.c[4], SETTLED, SETTLED_FRAMES) : mean_square(out.c[6], SETTLED, SETTLED_FRAMES);
        if (variant == 2U) { side_in_both = mean_square(out.c[4], SETTLED, SETTLED_FRAMES); rear_in_both = mean_square(out.c[6], SETTLED, SETTLED_FRAMES); }
        /* Out-of-phase material is surround only: no centre. */
        assert(mean_square(out.c[2], SETTLED, SETTLED_FRAMES) < 1e-3 * power[variant] + 1e-9);
        fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
    }
    assert(fabs(db(power[0] / power[1])) < 0.5);
    /* The pairs share the power of one pair ... */
    assert(fabs(db((side_in_both + rear_in_both) / power[0])) < 0.5);
    /* ... and an out-of-phase, fully diffuse bin goes 1:3 to the rears (sin 60 deg vs cos 60 deg). */
    assert(fabs(db(rear_in_both / side_in_both) - db(3.0)) < 0.7);
}

static void test_anti_phase_surround_feeds_are_decorrelated_and_relieve_the_front(void) {
    fx_upmix_hq *u = fx_upmix_hq_create(RATE, 2048U, 1, 1, 0);
    bus in = bus_new(FRAMES), out = bus_new(FRAMES);
    double input_power;
    fx_upmix_hq_set_strength(u, 10.0F, 10.0F);
    for (size_t n = 0; n < FRAMES; n++) { float x = noise(0.1F); in.c[0][n] = x; in.c[1][n] = -x; }
    run(u, &in, &out, 509U);
    input_power = mean_square(in.c[0], SETTLED, SETTLED_FRAMES);
    /* Same scale as Fast: S = sqrt(2) x; at full strength each surround feed carries 0.8 of it. */
    assert(fabs(db(mean_square(out.c[4], SETTLED, SETTLED_FRAMES) / (2.0 * 0.64 * input_power))) < 0.5);
    assert(fabs(correlation(out.c[4], out.c[5], SETTLED, SETTLED_FRAMES)) < 0.1);
    /* The left feed follows the left front, the right feed the right front. */
    assert(correlation(out.c[4], out.c[0], SETTLED, SETTLED_FRAMES) > 0.5);
    assert(correlation(out.c[5], out.c[1], SETTLED, SETTLED_FRAMES) > 0.5);
    /* Relief takes 0.5 * 0.8 of the difference component out of L/R: L = x * (1 - 0.4). */
    assert(fabs(db(mean_square(out.c[0], SETTLED, SETTLED_FRAMES) / (0.36 * input_power))) < 0.5);
    /* The centre gets nothing from out-of-phase material. */
    assert(mean_square(out.c[2], SETTLED, SETTLED_FRAMES) < 1e-3 * input_power);
    fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_hard_panned_material_stays_in_its_front_channel(void) {
    for (unsigned side = 0; side < 2U; side++) {
        fx_upmix_hq *u = fx_upmix_hq_create(RATE, 2048U, 1, 1, 1);
        bus in = bus_new(FRAMES), out = bus_new(FRAMES);
        size_t latency = fx_upmix_hq_latency(u);
        fx_upmix_hq_set_strength(u, 10.0F, 10.0F);
        for (size_t n = 0; n < FRAMES; n++) in.c[side][n] = noise(0.1F);
        run(u, &in, &out, 509U);
        assert(max_abs_diff(out.c[side] + latency, in.c[side], FRAMES - latency) == 0.0);
        for (unsigned channel = 0; channel < CH; channel++) if (channel != side) assert(mean_square(out.c[channel], 0, FRAMES) == 0.0);
        fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
    }
}

static void test_center_strength_controls_how_much_leaves_l_r(void) {
    double center[3], front[3];
    float strengths[3] = {1.0F, 5.0F, 10.0F};
    for (unsigned variant = 0; variant < 3U; variant++) {
        fx_upmix_hq *u = fx_upmix_hq_create(RATE, 2048U, 1, 1, 1);
        bus in = bus_new(FRAMES), out = bus_new(FRAMES);
        fx_upmix_hq_set_strength(u, strengths[variant], 5.0F);
        rng = 7U;
        for (size_t n = 0; n < FRAMES; n++) {
            float common = noise(0.1F);
            in.c[0][n] = common + noise(0.06F); in.c[1][n] = common + noise(0.06F);
        }
        run(u, &in, &out, 509U);
        center[variant] = mean_square(out.c[2], SETTLED, SETTLED_FRAMES);
        front[variant] = mean_square(out.c[0], SETTLED, SETTLED_FRAMES);
        fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
    }
    assert(center[0] < center[1] && center[1] < center[2]);
    assert(front[0] > front[1] && front[1] > front[2]);
    assert(center[2] > 4.0 * center[0]);
}

static void test_surround_strength_controls_the_surround_level(void) {
    double level[3];
    float strengths[3] = {1.0F, 5.0F, 10.0F};
    for (unsigned variant = 0; variant < 3U; variant++) {
        fx_upmix_hq *u = fx_upmix_hq_create(RATE, 2048U, 1, 1, 1);
        bus in = bus_new(FRAMES), out = bus_new(FRAMES);
        fx_upmix_hq_set_strength(u, 5.0F, strengths[variant]);
        rng = 11U;
        for (size_t n = 0; n < FRAMES; n++) { in.c[0][n] = noise(0.1F); in.c[1][n] = noise(0.1F); }
        run(u, &in, &out, 509U);
        level[variant] = mean_square(out.c[6], SETTLED, SETTLED_FRAMES);
        fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
    }
    assert(level[0] < level[1] && level[1] < level[2]);
}

static void test_zero_strength_switches_one_share_off_and_leaves_the_other_working(void) {
    float cases[3][2] = {{0.0F, 0.0F}, {0.0F, 10.0F}, {10.0F, 0.0F}};
    for (unsigned variant = 0; variant < 3U; variant++) {
        fx_upmix_hq *u = fx_upmix_hq_create(RATE, 2048U, 1, 1, 1);
        bus in = bus_new(FRAMES), out = bus_new(FRAMES);
        size_t latency = fx_upmix_hq_latency(u);
        double input_power;
        int center_on = cases[variant][0] > 0.0F, surround_on = cases[variant][1] > 0.0F;
        fx_upmix_hq_set_strength(u, cases[variant][0], cases[variant][1]);
        rng = 21U;
        /* A common part for the centre and two independent parts for the surround feeds. */
        for (size_t n = 0; n < FRAMES; n++) {
            float common = noise(0.1F);
            in.c[0][n] = common + noise(0.1F); in.c[1][n] = common + noise(0.1F);
        }
        run(u, &in, &out, 509U);
        input_power = mean_square(in.c[0], SETTLED, SETTLED_FRAMES);
        if (center_on) assert(mean_square(out.c[2], SETTLED, SETTLED_FRAMES) > 0.3 * input_power);
        else assert(mean_square(out.c[2], 0, FRAMES) == 0.0);
        for (unsigned channel = 4U; channel < CH; channel++) {
            if (surround_on) assert(mean_square(out.c[channel], SETTLED, SETTLED_FRAMES) > 0.05 * input_power);
            else assert(mean_square(out.c[channel], 0, FRAMES) == 0.0);
        }
        if (!center_on && !surround_on) {
            /* Both shares off: exactly the delayed input, nothing taken out of L/R. */
            assert(max_abs_diff(out.c[0] + latency, in.c[0], FRAMES - latency) == 0.0);
            assert(max_abs_diff(out.c[1] + latency, in.c[1], FRAMES - latency) == 0.0);
        }
        if (center_on && !surround_on) assert(mean_square(out.c[0], SETTLED, SETTLED_FRAMES) < 0.9 * input_power);
        fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
    }
}

/* Simultaneous bands: five sources in different bands, each with its own character. */
typedef struct { double low, high; } band;
static const band BAND_CENTER = {700.0, 1400.0}, BAND_DIFFUSE = {2000.0, 4000.0}, BAND_LEFT = {300.0, 600.0},
                  BAND_OUT_OF_PHASE = {5000.0, 8000.0}, BAND_PANNED = {160.0, 240.0};

static void mixed_band_scene(bus *in, size_t frames) {
    rng = 99U;
    add_band_noise(in->c[0], frames, BAND_CENTER.low, BAND_CENTER.high, 0.1); memcpy(in->c[1], in->c[0], frames * sizeof(float));
    add_band_noise(in->c[0], frames, BAND_DIFFUSE.low, BAND_DIFFUSE.high, 0.1);
    add_band_noise(in->c[1], frames, BAND_DIFFUSE.low, BAND_DIFFUSE.high, 0.1);
    add_band_noise(in->c[0], frames, BAND_LEFT.low, BAND_LEFT.high, 0.1);
    {
        float *bed = calloc(frames, sizeof(float));
        assert(bed);
        add_band_noise(bed, frames, BAND_OUT_OF_PHASE.low, BAND_OUT_OF_PHASE.high, 0.1);
        for (size_t n = 0; n < frames; n++) { in->c[0][n] += bed[n]; in->c[1][n] -= bed[n]; }
        memset(bed, 0, frames * sizeof(float));
        add_band_noise(bed, frames, BAND_PANNED.low, BAND_PANNED.high, 0.1);
        for (size_t n = 0; n < frames; n++) { in->c[0][n] += 0.894F * bed[n]; in->c[1][n] += 0.447F * bed[n]; }
        free(bed);
    }
}

/* Share of the stereo input power of a band that a set of output channels carries;
 * the output window is shifted by the latency so both see the same signal. */
static double band_share(const bus *in, const bus *out, size_t latency, const unsigned *channels, unsigned count, band b, size_t begin) {
    double input = band_power(in->c[0], begin, 32768U, b.low, b.high) + band_power(in->c[1], begin, 32768U, b.low, b.high), sum = 0.0;
    for (unsigned i = 0; i < count; i++) sum += band_power(out->c[channels[i]], begin + latency, 32768U, b.low, b.high);
    return sum / input;
}

static void test_simultaneous_bands_are_routed_by_their_own_character(void) {
    static const unsigned centre[] = {2}, surround[] = {4, 5, 6, 7}, left[] = {0}, front[] = {0, 1}, not_left[] = {1, 2, 4, 5, 6, 7};
    for (unsigned size = 0; size < 3U; size++) {
        fx_upmix_hq *u = fx_upmix_hq_create(RATE, FFT_SIZES[size], 1, 1, 1);
        fx_upmix_fast *fast = fx_upmix_fast_create(RATE, 1, 1, 1);
        size_t frames = 8U * RATE, begin = 5U * RATE, hq_latency = fx_upmix_hq_latency(u), fast_latency = fx_upmix_fast_latency(fast);
        bus in = bus_new(frames), out = bus_new(frames), fast_out = bus_new(frames);
        double hq_centre, fast_centre;
        mixed_band_scene(&in, frames);
        /* The default strength: every band is judged by its own statistics. */
        fx_upmix_hq_set_strength(u, 6.0F, 6.0F);
        fx_upmix_fast_set_strength(fast, 6.0F, 6.0F);
        run(u, &in, &out, 509U);
        run_fast(fast, &in, &fast_out, 509U);
        /* Mono band: the centre takes most of it, the surround gets nothing. */
        hq_centre = band_share(&in, &out, hq_latency, centre, 1, BAND_CENTER, begin);
        fast_centre = band_share(&in, &fast_out, fast_latency, centre, 1, BAND_CENTER, begin);
        assert(hq_centre > 0.55);
        assert(band_share(&in, &out, hq_latency, surround, 4, BAND_CENTER, begin) < 0.01);
        /* The broadband matrix sees one mixed statistic and cannot separate it. */
        assert(fast_centre < 0.1 * hq_centre);
        /* Uncorrelated band: surround, not the centre. */
        assert(band_share(&in, &out, hq_latency, surround, 4, BAND_DIFFUSE, begin) > 0.2);
        assert(band_share(&in, &out, hq_latency, centre, 1, BAND_DIFFUSE, begin) < 0.01);
        /* Hard-left band stays in FL and nowhere else. */
        assert(band_share(&in, &out, hq_latency, left, 1, BAND_LEFT, begin) > 0.95);
        assert(band_share(&in, &out, hq_latency, not_left, 6, BAND_LEFT, begin) < 0.01);
        /* Out-of-phase band: surround, no centre. */
        assert(band_share(&in, &out, hq_latency, surround, 4, BAND_OUT_OF_PHASE, begin) > 0.4);
        assert(band_share(&in, &out, hq_latency, centre, 1, BAND_OUT_OF_PHASE, begin) < 0.01);
        /* Panned band keeps its image: front only, nothing in the centre or surround. */
        assert(band_share(&in, &out, hq_latency, front, 2, BAND_PANNED, begin) > 0.97);
        assert(band_share(&in, &out, hq_latency, surround, 4, BAND_PANNED, begin) < 0.01);
        assert(band_share(&in, &out, hq_latency, centre, 1, BAND_PANNED, begin) < 0.01);
        /* At full strength the mono band moves into the centre entirely. */
        fx_upmix_hq_reset(u);
        fx_upmix_hq_set_strength(u, 10.0F, 10.0F);
        run(u, &in, &out, 509U);
        assert(band_share(&in, &out, hq_latency, centre, 1, BAND_CENTER, begin) > 0.97);
        assert(band_share(&in, &out, hq_latency, front, 2, BAND_CENTER, begin) < 0.02);
        fx_upmix_hq_destroy(u); fx_upmix_fast_destroy(fast); bus_free(&in); bus_free(&out); bus_free(&fast_out);
    }
}

/* A panned source with an uncorrelated bed: the front image holds. */
static void test_a_panned_source_keeps_its_front_image(void) {
    float pans[] = {0.0F, 0.1F, 0.2F, 0.3F, 0.4F, 0.5F, 0.6F, 0.7F, 0.8F, 0.9F, 1.0F};
    for (unsigned size = 1; size < 3U; size++)
        for (unsigned i = 0; i < sizeof pans / sizeof *pans; i++) {
            fx_upmix_hq *u = fx_upmix_hq_create(RATE, FFT_SIZES[size], 1, 1, 1);
            fx_upmix_fast *fast = fx_upmix_fast_create(RATE, 1, 1, 1);
            bus in = bus_new(FRAMES), out = bus_new(FRAMES), fast_out = bus_new(FRAMES);
            double gl = cos(pans[i] * PI / 2.0), gr = sin(pans[i] * PI / 2.0);
            double input_az, hq_az, fast_az;
            fx_upmix_hq_set_strength(u, 6.0F, 6.0F);
            fx_upmix_fast_set_strength(fast, 6.0F, 6.0F);
            rng = 5U;
            for (size_t n = 0; n < FRAMES; n++) {
                float s = noise(0.1F);
                in.c[0][n] = (float)(gl * s) + noise(0.02F); in.c[1][n] = (float)(gr * s) + noise(0.02F);
            }
            run(u, &in, &out, 509U);
            run_fast(fast, &in, &fast_out, 509U);
            input_az = front_azimuth(mean_square(in.c[0], SETTLED, SETTLED_FRAMES), mean_square(in.c[1], SETTLED, SETTLED_FRAMES), 0.0);
            hq_az = front_azimuth(mean_square(out.c[0], SETTLED, SETTLED_FRAMES), mean_square(out.c[1], SETTLED, SETTLED_FRAMES),
                                  mean_square(out.c[2], SETTLED, SETTLED_FRAMES));
            fast_az = front_azimuth(mean_square(fast_out.c[0], SETTLED, SETTLED_FRAMES), mean_square(fast_out.c[1], SETTLED, SETTLED_FRAMES),
                                    mean_square(fast_out.c[2], SETTLED, SETTLED_FRAMES));
            /* The image stays within 4 degrees (extraction pulls slightly off-centre sources towards the
             * centre; the ambience taken from the weaker channel pushes the rest out by under a degree)
             * and is never meaningfully further off than the matrix. */
            assert(fabs(hq_az - input_az) < 4.0);
            assert(fabs(hq_az - input_az) <= fabs(fast_az - input_az) + 1.0);
            fx_upmix_hq_destroy(u); fx_upmix_fast_destroy(fast); bus_free(&in); bus_free(&out); bus_free(&fast_out);
        }
}

/* The null follows the dominant direction: a panned source leaks into the surround far less than with the matrix. */
static void test_a_panned_source_does_not_leak_into_the_surround_like_it_does_in_the_matrix(void) {
    fx_upmix_hq *u = fx_upmix_hq_create(RATE, 2048U, 1, 1, 1);
    fx_upmix_fast *fast = fx_upmix_fast_create(RATE, 1, 1, 1);
    bus in = bus_new(FRAMES), out = bus_new(FRAMES), fast_out = bus_new(FRAMES);
    double hq_leak, fast_leak;
    fx_upmix_hq_set_strength(u, 6.0F, 10.0F);
    fx_upmix_fast_set_strength(fast, 6.0F, 10.0F);
    rng = 17U;
    for (size_t n = 0; n < FRAMES; n++) {
        float s = noise(0.1F);
        in.c[0][n] = 0.894F * s + noise(0.03F); in.c[1][n] = 0.447F * s + noise(0.03F);
    }
    run(u, &in, &out, 509U);
    run_fast(fast, &in, &fast_out, 509U);
    hq_leak = fabs(shifted_correlation(out.c[4], in.c[0], SETTLED, SETTLED_FRAMES, -(ptrdiff_t)fx_upmix_hq_latency(u)));
    fast_leak = fabs(shifted_correlation(fast_out.c[4], in.c[0], SETTLED, SETTLED_FRAMES, -(ptrdiff_t)fx_upmix_fast_latency(fast)));
    assert(hq_leak < 0.25);
    assert(fast_leak > 2.0 * hq_leak);
    fx_upmix_hq_destroy(u); fx_upmix_fast_destroy(fast); bus_free(&in); bus_free(&out); bus_free(&fast_out);
}

/* Bass is never extracted: it stays in FL/FR where the existing bass management takes it. */
static void test_bass_stays_in_the_fronts_while_the_mids_go_to_the_center(void) {
    fx_upmix_hq *u = fx_upmix_hq_create(RATE, 2048U, 1, 1, 1);
    bus in = bus_new(8U * RATE), out = bus_new(8U * RATE);
    size_t frames = 8U * RATE, begin = 5U * RATE;
    double bass_in, bass_front, bass_center, mid_in, mid_center;
    rng = 41U;
    add_band_noise(in.c[0], frames, 20.0, 50.0, 0.1);
    memcpy(in.c[1], in.c[0], frames * sizeof(float));
    {
        float *mid = calloc(frames, sizeof(float));
        assert(mid);
        add_band_noise(mid, frames, 400.0, 1200.0, 0.1);
        for (size_t n = 0; n < frames; n++) { in.c[0][n] += mid[n]; in.c[1][n] += mid[n]; }
        free(mid);
    }
    fx_upmix_hq_set_strength(u, 10.0F, 10.0F);
    run(u, &in, &out, 509U);
    bass_in = band_power(in.c[0], begin, 32768U, 20.0, 50.0);
    bass_front = band_power(out.c[0], begin + fx_upmix_hq_latency(u), 32768U, 20.0, 50.0);
    bass_center = band_power(out.c[2], begin + fx_upmix_hq_latency(u), 32768U, 20.0, 50.0);
    mid_in = band_power(in.c[0], begin, 32768U, 400.0, 1200.0);
    mid_center = band_power(out.c[2], begin + fx_upmix_hq_latency(u), 32768U, 400.0, 1200.0);
    assert(fabs(db(bass_front / bass_in)) < 0.2);
    assert(bass_center < 0.02 * bass_in);          /* the smallest FFT leaks the most through its window */
    assert(mid_center > 1.5 * mid_in);
    fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_strength_change_to_zero_while_running_is_smooth_and_settles_to_silence(void) {
    fx_upmix_hq *u = fx_upmix_hq_create(RATE, 2048U, 1, 1, 1);
    /* The release takes about 17 s to fall below the flush threshold. */
    size_t frames = 24U * RATE;
    bus in = bus_new(frames), out = bus_new(frames);
    double worst = 0.0;
    for (size_t n = 0; n < frames; n++) in.c[0][n] = in.c[1][n] = 0.3F * sinf((float)(2.0 * PI * 1000.0 * n / RATE));
    fx_upmix_hq_set_strength(u, 10.0F, 10.0F);
    {
        bus first = in, second = in; first.frames = 2U * RATE; second.frames = frames - 2U * RATE;
        bus out_first = out, out_second = out; out_first.frames = 2U * RATE; out_second.frames = frames - 2U * RATE;
        for (unsigned i = 0; i < CH; i++) { second.c[i] = in.c[i] + 2U * RATE; out_second.c[i] = out.c[i] + 2U * RATE; }
        run(u, &first, &out_first, 509U);
        fx_upmix_hq_set_strength(u, 0.0F, 0.0F);
        run(u, &second, &out_second, 509U);
    }
    for (size_t n = 1; n < frames; n++) {
        double step = fabs((double)out.c[2][n] - out.c[2][n - 1U]);
        if (step > worst) worst = step;
    }
    assert(worst < 0.07);
    /* The share released to nothing: the centre is silent and L/R are the input again. */
    assert(mean_square(out.c[2], frames - RATE, RATE) == 0.0);
    assert(max_abs_diff(out.c[0] + frames - RATE, in.c[0] + frames - RATE - fx_upmix_hq_latency(u), RATE) < 1e-6);
    fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_strength_change_while_running_is_smooth(void) {
    fx_upmix_hq *u = fx_upmix_hq_create(RATE, 2048U, 1, 1, 1);
    bus in = bus_new(2U * RATE), out = bus_new(2U * RATE);
    double worst = 0.0;
    for (size_t n = 0; n < 2U * RATE; n++) in.c[0][n] = in.c[1][n] = 0.3F * sinf((float)(2.0 * PI * 1000.0 * n / RATE));
    fx_upmix_hq_set_strength(u, 1.0F, 1.0F);
    {
        bus first = in, second = in; first.frames = RATE; second.frames = RATE;
        bus out_first = out, out_second = out; out_first.frames = RATE; out_second.frames = RATE;
        for (unsigned i = 0; i < CH; i++) { second.c[i] = in.c[i] + RATE; out_second.c[i] = out.c[i] + RATE; }
        run(u, &first, &out_first, 509U);
        fx_upmix_hq_set_strength(u, 10.0F, 10.0F);
        run(u, &second, &out_second, 509U);
    }
    for (size_t n = 1; n < 2U * RATE; n++) {
        double step = fabs((double)out.c[2][n] - out.c[2][n - 1U]);
        if (step > worst) worst = step;
    }
    /* A 0.42 amplitude 1 kHz tone moves at most 0.056 per frame. */
    assert(worst < 0.07);
    fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
}

/* Detector integration. The detection itself is shared with the matrix and tested there;
 * these runs stream at a low rate because its times are in seconds. */
#define LONG_RATE 16000U
#define SECONDS(count) ((size_t)((count) * LONG_RATE))

/* A stereo programme (silent from stereo_to on, when that is set) and a sine in one extra channel. */
typedef struct { unsigned channel; float level; size_t from, to, stereo_to; } content;

static void content_frame(const content *c, size_t n, float *frame) {
    memset(frame, 0, CH * sizeof *frame);
    if (!c->stereo_to || n < c->stereo_to) {
        float common = noise(0.1F);
        frame[0] = common + noise(0.06F); frame[1] = common + noise(0.06F);
    }
    if (c->channel && n >= c->from && n < c->to) frame[c->channel] = c->level * (float)sin(2.0 * PI * 1000.0 * (double)n / LONG_RATE);
}

/* Streams frames [begin, begin + frames); returns the highest engagement seen. */
static float stream(fx_upmix_hq *u, const content *c, size_t begin, size_t frames, float *lowest) {
    enum { BLOCK = 400 };
    float input[CH][BLOCK], output[CH][BLOCK], highest = 0.0F;
    const float *ip[CH]; float *op[CH];
    for (unsigned k = 0; k < CH; k++) { ip[k] = input[k]; op[k] = output[k]; }
    if (lowest) *lowest = 1.0F;
    for (size_t done = 0; done + BLOCK <= frames; done += BLOCK) {
        float level;
        for (size_t k = 0; k < BLOCK; k++) {
            float frame[CH];
            content_frame(c, begin + done + k, frame);
            for (unsigned channel = 0; channel < CH; channel++) input[channel][k] = frame[channel];
        }
        fx_upmix_hq_process(u, ip, op, BLOCK);
        level = fx_upmix_hq_engagement(u);
        if (level > highest) highest = level;
        if (lowest && level < *lowest) *lowest = level;
    }
    return highest;
}

static void test_any_extra_channel_disengages_within_milliseconds_and_the_hold_is_ten_seconds(void) {
    for (unsigned channel = 2U; channel < CH; channel++) {
        content c = {.channel = channel, .level = 0.01F, .from = SECONDS(2), .to = SECONDS(5)};
        fx_upmix_hq *u = fx_upmix_hq_create(LONG_RATE, 1024U, 1, 1, 1);
        float lowest;
        stream(u, &c, 0, SECONDS(2), &lowest);
        assert(fx_upmix_hq_engagement(u) == 1.0F);
        stream(u, &c, SECONDS(2), SECONDS(0.05), &lowest);               /* 50 ms into the content */
        assert(fx_upmix_hq_engagement(u) < 0.05F);
        stream(u, &c, SECONDS(2.05), SECONDS(12.85), &lowest);          /* up to 9.9 s after it stopped */
        assert(fx_upmix_hq_engagement(u) < 0.01F);
        stream(u, &c, SECONDS(14.9), SECONDS(3.1), &lowest);            /* 3 s after the hold was over */
        assert(fx_upmix_hq_engagement(u) > 0.9F);
        fx_upmix_hq_destroy(u);
    }
}

/* While disengaged every channel is its delayed input: the spectral path keeps running but adds nothing. */
static void test_a_disengaged_upmixer_is_a_pure_delay(void) {
    size_t frames = 6U * RATE;
    fx_upmix_hq *u = fx_upmix_hq_create(RATE, 2048U, 1, 1, 1);
    bus in = bus_new(frames), out = bus_new(frames);
    size_t latency = fx_upmix_hq_latency(u);
    fx_upmix_hq_set_strength(u, 10.0F, 10.0F);
    for (size_t n = 0; n < frames; n++) {
        float common = noise(0.1F);
        in.c[0][n] = common + noise(0.06F); in.c[1][n] = common + noise(0.06F);
        in.c[2][n] = noise(0.01F); in.c[6][n] = noise(0.01F);
    }
    run(u, &in, &out, 509U);
    assert(fx_upmix_hq_engagement(u) == 0.0F);
    for (unsigned channel = 0; channel < CH; channel++)
        assert(max_abs_diff(out.c[channel] + latency + 2U * RATE, in.c[channel] + 2U * RATE, 3U * RATE) < 1e-6);
    fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
}

/* Recursive states decay into denormals while idle, and every sample then runs slow on x86. The
 * slowest decay (the 250 ms release of the per-bin gains) reaches the denormal range after about
 * three minutes of silence; a disengaged upmixer reaches it far sooner through its engagement. */
static void test_long_idle_and_disengaged_states_run_without_denormals(void) {
    content idle = {.stereo_to = SECONDS(4)};
    fx_upmix_hq *u = fx_upmix_hq_create(LONG_RATE, 1024U, 1, 1, 1);
    stream(u, &idle, 0, SECONDS(4), NULL);
    assert(fx_upmix_hq_engagement(u) == 1.0F);
    feclearexcept(FE_UNDERFLOW);
    stream(u, &idle, SECONDS(4), SECONDS(240), NULL);
    assert(!fetestexcept(FE_UNDERFLOW));
    fx_upmix_hq_destroy(u);

    {
        content disengaged = {.channel = 2U, .level = 0.01F, .from = 0, .to = SECONDS(40)};
        u = fx_upmix_hq_create(LONG_RATE, 1024U, 1, 1, 1);
        stream(u, &disengaged, 0, SECONDS(2), NULL);
        feclearexcept(FE_UNDERFLOW);
        stream(u, &disengaged, SECONDS(2), SECONDS(38), NULL);
        assert(fx_upmix_hq_engagement(u) == 0.0F);
        assert(!fetestexcept(FE_UNDERFLOW));
        fx_upmix_hq_destroy(u);
    }
}

static void test_lfe_is_never_synthesised(void) {
    fx_upmix_hq *u = fx_upmix_hq_create(RATE, 2048U, 1, 1, 1);
    bus in = bus_new(FRAMES), out = bus_new(FRAMES);
    fx_upmix_hq_set_strength(u, 10.0F, 10.0F);
    for (size_t n = 0; n < FRAMES; n++) { in.c[0][n] = noise(0.1F); in.c[1][n] = noise(0.1F); }
    run(u, &in, &out, 509U);
    assert(mean_square(out.c[3], 0, FRAMES) == 0.0);
    fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_no_destination_means_pure_delayed_pass_through(void) {
    fx_upmix_hq *u = fx_upmix_hq_create(RATE, 2048U, 0, 0, 0);
    bus in = bus_new(FRAMES), out = bus_new(FRAMES);
    size_t latency = fx_upmix_hq_latency(u);
    for (size_t n = 0; n < FRAMES; n++) { in.c[0][n] = noise(0.1F); in.c[1][n] = noise(0.1F); }
    run(u, &in, &out, 509U);
    assert(max_abs_diff(out.c[0] + latency, in.c[0], FRAMES - latency) == 0.0);
    assert(max_abs_diff(out.c[1] + latency, in.c[1], FRAMES - latency) == 0.0);
    fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_result_does_not_depend_on_the_block_size(void) {
    size_t sizes[] = {1U, 7U, 509U, 1024U, 4096U};
    bus in = bus_new(2U * RATE), reference = bus_new(2U * RATE), out = bus_new(2U * RATE);
    for (size_t n = 0; n < 2U * RATE; n++) { float common = noise(0.1F); in.c[0][n] = common + noise(0.05F); in.c[1][n] = common + noise(0.05F); }
    for (unsigned i = 0; i < 5U; i++) {
        fx_upmix_hq *u = fx_upmix_hq_create(RATE, 1024U, 1, 1, 1);
        fx_upmix_hq_set_strength(u, 8.0F, 7.0F);
        run(u, &in, i ? &out : &reference, sizes[i]);
        if (i) for (unsigned channel = 0; channel < CH; channel++) assert(memcmp(out.c[channel], reference.c[channel], 2U * RATE * sizeof(float)) == 0);
        fx_upmix_hq_destroy(u);
    }
    bus_free(&in); bus_free(&reference); bus_free(&out);
}

static void test_non_finite_input_never_reaches_the_output(void) {
    fx_upmix_hq *u = fx_upmix_hq_create(RATE, 1024U, 1, 1, 1);
    bus in = bus_new(16384U), out = bus_new(16384U);
    const float *ip[CH]; float *op[CH];
    for (size_t n = 0; n < 16384U; n++) { in.c[0][n] = noise(0.1F); in.c[1][n] = noise(0.1F); }
    in.c[0][1000] = NAN; in.c[1][1200] = INFINITY; in.c[2][1400] = -INFINITY;
    run(u, &in, &out, 509U);
    for (unsigned channel = 0; channel < CH; channel++)
        for (size_t n = 0; n < 16384U; n++) assert(isfinite(out.c[channel][n]));
    /* Missing input buffers read as silence. */
    for (unsigned channel = 0; channel < CH; channel++) { ip[channel] = channel < 2U ? in.c[channel] : NULL; op[channel] = out.c[channel]; }
    fx_upmix_hq_process(u, ip, op, 4096U);
    for (unsigned channel = 0; channel < CH; channel++)
        for (size_t n = 0; n < 4096U; n++) assert(isfinite(out.c[channel][n]));
    fx_upmix_hq_process(u, ip, op, 0);
    fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_loud_and_very_quiet_programmes_stay_finite_and_bounded(void) {
    float levels[] = {1.0F, 1e-6F, 1e-12F};
    for (unsigned i = 0; i < 3U; i++) {
        fx_upmix_hq *u = fx_upmix_hq_create(RATE, 2048U, 1, 1, 1);
        bus in = bus_new(2U * RATE), out = bus_new(2U * RATE);
        fx_upmix_hq_set_strength(u, 10.0F, 10.0F);
        for (size_t n = 0; n < 2U * RATE; n++) { float common = noise(levels[i]); in.c[0][n] = common + noise(0.5F * levels[i]); in.c[1][n] = common + noise(0.5F * levels[i]); }
        run(u, &in, &out, 509U);
        for (unsigned channel = 0; channel < CH; channel++)
            for (size_t n = 0; n < 2U * RATE; n++) assert(isfinite(out.c[channel][n]) && fabsf(out.c[channel][n]) < 8.0F * levels[i] + 1e-30F);
        fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
    }
}

static void test_silence_stays_silent_and_reset_restores_a_fresh_instance(void) {
    fx_upmix_hq *used = fx_upmix_hq_create(RATE, 1024U, 1, 1, 1), *fresh = fx_upmix_hq_create(RATE, 1024U, 1, 1, 1);
    bus loud = bus_new(RATE), quiet = bus_new(RATE), out = bus_new(RATE), second = bus_new(RATE), fresh_out = bus_new(RATE);
    for (size_t n = 0; n < RATE; n++) { float common = noise(0.1F); loud.c[0][n] = common + noise(0.06F); loud.c[1][n] = common + noise(0.06F); }
    run(used, &loud, &out, 509U);
    fx_upmix_hq_reset(used);
    run(used, &quiet, &out, 509U);
    for (unsigned channel = 0; channel < CH; channel++) assert(mean_square(out.c[channel], 0, RATE) == 0.0);
    fx_upmix_hq_reset(used);
    run(used, &loud, &second, 509U);
    run(fresh, &loud, &fresh_out, 509U);
    for (unsigned channel = 0; channel < CH; channel++) assert(memcmp(second.c[channel], fresh_out.c[channel], RATE * sizeof(float)) == 0);
    fx_upmix_hq_destroy(used); fx_upmix_hq_destroy(fresh);
    bus_free(&loud); bus_free(&quiet); bus_free(&out); bus_free(&second); bus_free(&fresh_out);
}

static void test_other_rates_keep_the_contract(void) {
    unsigned rates[] = {44100U, 96000U, 192000U};
    for (unsigned i = 0; i < 3U; i++) {
        fx_upmix_hq *u = fx_upmix_hq_create(rates[i], 2048U, 1, 1, 1);
        size_t frames = 3U * rates[i], begin = 2U * rates[i];
        bus in = bus_new(frames), out = bus_new(frames);
        double input_power;
        fx_upmix_hq_set_strength(u, 10.0F, 10.0F);
        for (size_t n = 0; n < frames; n++) in.c[0][n] = in.c[1][n] = noise(0.1F);
        run(u, &in, &out, 1000U);
        input_power = mean_square(in.c[0], begin, rates[i]);
        assert(fabs(db(mean_square(out.c[2], begin, rates[i]) / (2.0 * input_power))) < 1.0);
        assert(mean_square(out.c[0], begin, rates[i]) < 0.01 * input_power);
        fx_upmix_hq_destroy(u); bus_free(&in); bus_free(&out);
    }
}

static void test_strength_validation_is_shared_with_the_matrix(void) {
    assert(fx_upmix_strength_valid(0.0F) && fx_upmix_strength_valid(10.0F) && fx_upmix_strength_valid(5.5F));
    assert(!fx_upmix_strength_valid(-0.5F) && !fx_upmix_strength_valid(10.5F) && !fx_upmix_strength_valid(NAN) && !fx_upmix_strength_valid(INFINITY));
    assert(fx_upmix_fast_strength_valid(7.0F) == fx_upmix_strength_valid(7.0F));
}

int main(void) {
    test_fft_sizes_and_rates_are_validated_and_latency_is_fft_size_minus_one();
    test_every_channel_is_delayed_by_exactly_the_latency();
    test_derived_channels_are_aligned_with_the_delayed_front();
    test_mono_goes_to_center_and_leaves_the_front();
    test_without_a_center_output_the_centre_image_stays_in_front();
    test_surround_stays_active_without_a_center();
    test_side_only_and_rear_only_get_full_power_and_both_split_it_by_diffuseness();
    test_anti_phase_surround_feeds_are_decorrelated_and_relieve_the_front();
    test_hard_panned_material_stays_in_its_front_channel();
    test_center_strength_controls_how_much_leaves_l_r();
    test_surround_strength_controls_the_surround_level();
    test_zero_strength_switches_one_share_off_and_leaves_the_other_working();
    test_simultaneous_bands_are_routed_by_their_own_character();
    test_a_panned_source_keeps_its_front_image();
    test_a_panned_source_does_not_leak_into_the_surround_like_it_does_in_the_matrix();
    test_bass_stays_in_the_fronts_while_the_mids_go_to_the_center();
    test_strength_change_to_zero_while_running_is_smooth_and_settles_to_silence();
    test_strength_change_while_running_is_smooth();
    test_any_extra_channel_disengages_within_milliseconds_and_the_hold_is_ten_seconds();
    test_a_disengaged_upmixer_is_a_pure_delay();
    test_long_idle_and_disengaged_states_run_without_denormals();
    test_lfe_is_never_synthesised();
    test_no_destination_means_pure_delayed_pass_through();
    test_result_does_not_depend_on_the_block_size();
    test_non_finite_input_never_reaches_the_output();
    test_loud_and_very_quiet_programmes_stay_finite_and_bounded();
    test_silence_stays_silent_and_reset_restores_a_fresh_instance();
    test_other_rates_keep_the_contract();
    test_strength_validation_is_shared_with_the_matrix();
    return 0;
}
