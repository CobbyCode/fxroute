/* SPDX-License-Identifier: AGPL-3.0-only */
#include "upmix_fast.h"

#include <assert.h>
#include <fenv.h>
#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define RATE 48000U
#define PI 3.14159265358979323846
#define CH FX_UPMIX_FAST_CHANNELS

typedef struct { float *c[CH]; size_t frames; } bus;

static bus bus_new(size_t frames) {
    bus b = {.frames = frames};
    for (unsigned i = 0; i < CH; i++) { b.c[i] = calloc(frames, sizeof(float)); assert(b.c[i]); }
    return b;
}
static void bus_free(bus *b) { for (unsigned i = 0; i < CH; i++) free(b->c[i]); }

static void run(fx_upmix_fast *u, const bus *in, bus *out, size_t chunk) {
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
static double max_abs_diff(const float *a, const float *b, size_t count) {
    double worst = 0.0;
    for (size_t i = 0; i < count; i++) if (fabs((double)a[i] - b[i]) > worst) worst = fabs((double)a[i] - b[i]);
    return worst;
}
static double db(double ratio) { return 10.0 * log10(ratio); }

/* Settled window: the last second of a three second run. */
#define FRAMES (3U * RATE)
#define SETTLED (2U * RATE)

static void test_latency_is_reported_and_scales_with_rate(void) {
    unsigned rates[] = {44100U, 48000U, 96000U, 192000U};
    size_t expected[] = {58U, 64U, 128U, 256U};
    for (unsigned i = 0; i < 4U; i++) {
        fx_upmix_fast *u = fx_upmix_fast_create(rates[i], 1, 1, 1);
        assert(u && fx_upmix_fast_latency(u) == expected[i]);
        fx_upmix_fast_destroy(u);
    }
    assert(fx_upmix_fast_create(100U, 1, 1, 1) == NULL);
}

static void test_every_channel_of_the_matrix_path_is_delayed_by_exactly_the_latency(void) {
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
    bus in = bus_new(2000U), out = bus_new(2000U);
    size_t latency = fx_upmix_fast_latency(u);
    /* An impulse per channel on its own run, below the multichannel detection so the matrix
     * path stays selected: pass-through must be sample exact. */
    for (unsigned channel = 2U; channel < CH; channel++) {
        fx_upmix_fast_reset(u);
        memset(in.c[channel], 0, 2000U * sizeof(float));
        in.c[channel][100] = 0.001F;
        run(u, &in, &out, 509U);
        assert(fx_upmix_fast_latency(u) == latency);
        for (size_t n = 0; n < 2000U; n++) {
            float expected = n == 100U + latency ? 0.001F : 0.0F;
            assert(out.c[channel][n] == expected);
        }
        in.c[channel][100] = 0.0F;
    }
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
}

/* Channel layouts as masks over FL FR FC LFE SL SR BL BR: 2.0, 2.1, 3.0, 4.0, 5.0, 5.1, 7.1. */
static const unsigned LAYOUTS[] = {0x03U, 0x0BU, 0x07U, 0xC3U, 0x37U, 0x3FU, 0xFFU};

/* A stereo image in FL/FR and independent signals in the other channels of the layout. */
static void fill_layout(bus *b, unsigned layout) {
    for (size_t n = 0; n < b->frames; n++) {
        float common = noise(0.1F);
        b->c[0][n] = common + noise(0.06F); b->c[1][n] = common + noise(0.06F);
        for (unsigned channel = 2U; channel < CH; channel++) b->c[channel][n] = layout >> channel & 1U ? noise(0.05F) : 0.0F;
    }
}

/* Only a two-channel programme is upmixed. Anything that carries more channels passes straight
 * through: no delay, no matrix, every channel the input itself. */
static void test_only_a_two_channel_programme_is_upmixed_and_delayed(void) {
    for (unsigned layout = 0; layout < sizeof LAYOUTS / sizeof LAYOUTS[0]; layout++) {
        fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
        size_t frames = RATE, settled = RATE / 10U, tail = 3U * RATE / 4U;
        bus in = bus_new(frames), out = bus_new(frames);
        fx_upmix_fast_set_strength(u, 10.0F, 10.0F);
        fill_layout(&in, LAYOUTS[layout]);
        run(u, &in, &out, 509U);
        if (LAYOUTS[layout] == 0x03U) {
            assert(fx_upmix_fast_latency(u) == 64U);
            assert(fx_upmix_fast_engagement(u) == 1.0F);
            assert(mean_square(out.c[2], tail, frames - tail) > 0.5 * mean_square(in.c[0], tail, frames - tail));
        } else {
            assert(fx_upmix_fast_latency(u) == 0U);
            for (unsigned channel = 0; channel < CH; channel++)
                assert(memcmp(out.c[channel] + settled, in.c[channel] + settled, (frames - settled) * sizeof(float)) == 0);
        }
        fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
    }
}

/* The direct path fades in over the switch time (30 ms, at most the delay: here the delay), and
 * the delayed path is gone before it could play the same onset a second time: the Center level
 * drops once the fade is over, so any rest of the delayed path would show as the old level
 * coming back. */
static void test_a_multichannel_onset_fades_in_once_and_is_not_repeated(void) {
    unsigned rates[] = {44100U, 48000U, 96000U, 192000U};
    for (unsigned i = 0; i < 4U; i++) {
        fx_upmix_fast *u = fx_upmix_fast_create(rates[i], 1, 1, 1);
        size_t fade = fx_upmix_fast_latency(u), onset = 1000U, frames = onset + 8U * fade;
        bus in = bus_new(frames), out = bus_new(frames);
        for (size_t n = onset; n < frames; n++) in.c[2][n] = n < onset + fade ? 0.5F : 0.25F;
        run(u, &in, &out, 509U);
        assert(fx_upmix_fast_latency(u) == 0U);
        for (size_t n = 0; n < onset; n++) assert(out.c[2][n] == 0.0F);
        /* A raised cosine: no corner at the start, no jump on the way, half way at half the level. */
        assert(out.c[2][onset] < 2.0F / (float)(fade * fade));
        for (size_t n = onset; n < onset + fade; n++) {
            float step = out.c[2][n] - out.c[2][n - 1U];
            assert(step >= 0.0F && step < 1.0F / (float)fade);
        }
        assert(fabsf(out.c[2][onset + fade / 2U - 1U] - 0.25F) < 0.01F);
        for (size_t n = onset + fade; n < frames; n++) assert(out.c[2][n] == 0.25F);
        for (unsigned channel = 0; channel < CH; channel++)
            if (channel != 2U) assert(mean_square(out.c[channel], 0, frames) == 0.0);
        fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
    }
}

static void test_mono_goes_to_center_and_leaves_the_front(void) {
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
    bus in = bus_new(FRAMES), out = bus_new(FRAMES);
    double input_power;
    fx_upmix_fast_set_strength(u, 10.0F, 10.0F);
    for (size_t n = 0; n < FRAMES; n++) in.c[0][n] = in.c[1][n] = noise(0.1F);
    run(u, &in, &out, 509U);
    input_power = mean_square(in.c[0], SETTLED, RATE);
    /* The centre takes the whole mid component: |C|^2 = |L+R|^2 / 2 = 2 P. */
    assert(fabs(db(mean_square(out.c[2], SETTLED, RATE) / (2.0 * input_power))) < 1.0);
    assert(mean_square(out.c[0], SETTLED, RATE) < 0.01 * input_power);
    assert(mean_square(out.c[1], SETTLED, RATE) < 0.01 * input_power);
    for (unsigned channel = 4U; channel < CH; channel++) assert(mean_square(out.c[channel], SETTLED, RATE) < 1e-4 * input_power);
    assert(mean_square(out.c[3], 0, FRAMES) == 0.0);
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_without_a_center_output_the_centre_image_stays_in_front(void) {
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 0, 0, 1);
    bus in = bus_new(FRAMES), out = bus_new(FRAMES);
    size_t latency = fx_upmix_fast_latency(u);
    fx_upmix_fast_set_strength(u, 10.0F, 10.0F);
    for (size_t n = 0; n < FRAMES; n++) in.c[0][n] = in.c[1][n] = noise(0.1F);
    run(u, &in, &out, 509U);
    assert(mean_square(out.c[2], 0, FRAMES) == 0.0);
    /* Fully correlated material has no surround content either: L/R are untouched. */
    assert(max_abs_diff(out.c[0] + latency, in.c[0], FRAMES - latency) == 0.0);
    assert(max_abs_diff(out.c[1] + latency, in.c[1], FRAMES - latency) == 0.0);
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_surround_stays_active_without_a_center(void) {
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 0, 0, 1);
    bus in = bus_new(FRAMES), out = bus_new(FRAMES);
    double input_power;
    fx_upmix_fast_set_strength(u, 10.0F, 10.0F);
    for (size_t n = 0; n < FRAMES; n++) { in.c[0][n] = noise(0.1F); in.c[1][n] = noise(0.1F); }
    run(u, &in, &out, 509U);
    input_power = mean_square(in.c[0], SETTLED, RATE);
    assert(mean_square(out.c[2], 0, FRAMES) == 0.0);
    assert(mean_square(out.c[4], 0, FRAMES) == 0.0 && mean_square(out.c[5], 0, FRAMES) == 0.0);
    assert(mean_square(out.c[6], SETTLED, RATE) > 0.5 * input_power);
    assert(mean_square(out.c[7], SETTLED, RATE) > 0.5 * input_power);
    assert(fabs(correlation(out.c[6], out.c[7], SETTLED, RATE)) < 0.1);
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_side_only_and_rear_only_get_full_power_and_both_split_it(void) {
    double power[3];
    unsigned flags[3][2] = {{1, 0}, {0, 1}, {1, 1}};
    for (unsigned variant = 0; variant < 3U; variant++) {
        fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, flags[variant][0], flags[variant][1]);
        bus in = bus_new(FRAMES), out = bus_new(FRAMES);
        fx_upmix_fast_set_strength(u, 10.0F, 10.0F);
        for (size_t n = 0; n < FRAMES; n++) { float x = noise(0.1F); in.c[0][n] = x; in.c[1][n] = -x; }
        run(u, &in, &out, 509U);
        power[variant] = flags[variant][0] ? mean_square(out.c[4], SETTLED, RATE) : mean_square(out.c[6], SETTLED, RATE);
        /* Out-of-phase material is surround only: no centre. */
        assert(mean_square(out.c[2], SETTLED, RATE) < 1e-3 * power[variant] + 1e-9);
        fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
    }
    assert(fabs(db(power[0] / power[1])) < 0.5);
    assert(fabs(db(power[2] / power[0]) + 3.0) < 0.5);
}

static void test_anti_phase_surround_feeds_are_decorrelated_and_relieve_the_front(void) {
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 0);
    bus in = bus_new(FRAMES), out = bus_new(FRAMES);
    double input_power;
    fx_upmix_fast_set_strength(u, 10.0F, 10.0F);
    for (size_t n = 0; n < FRAMES; n++) { float x = noise(0.1F); in.c[0][n] = x; in.c[1][n] = -x; }
    run(u, &in, &out, 509U);
    input_power = mean_square(in.c[0], SETTLED, RATE);
    /* S = sqrt(2) x; at full strength each surround feed carries 0.8 of it. */
    assert(fabs(db(mean_square(out.c[4], SETTLED, RATE) / (2.0 * 0.64 * input_power))) < 0.5);
    assert(fabs(correlation(out.c[4], out.c[5], SETTLED, RATE)) < 0.1);
    /* Relief takes 0.5 * 0.8 of the difference component out of L/R: L = x * (1 - 0.4). */
    assert(fabs(db(mean_square(out.c[0], SETTLED, RATE) / (0.36 * input_power))) < 0.5);
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_hard_panned_material_stays_in_its_front_channel(void) {
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
    bus in = bus_new(FRAMES), out = bus_new(FRAMES);
    size_t latency = fx_upmix_fast_latency(u);
    fx_upmix_fast_set_strength(u, 10.0F, 10.0F);
    for (size_t n = 0; n < FRAMES; n++) in.c[0][n] = noise(0.1F);
    run(u, &in, &out, 509U);
    assert(max_abs_diff(out.c[0] + latency, in.c[0], FRAMES - latency) == 0.0);
    for (unsigned channel = 1U; channel < CH; channel++) assert(mean_square(out.c[channel], 0, FRAMES) == 0.0);
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_center_strength_controls_how_much_leaves_l_r(void) {
    double center[3], front[3];
    float strengths[3] = {1.0F, 5.0F, 10.0F};
    for (unsigned variant = 0; variant < 3U; variant++) {
        fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
        bus in = bus_new(FRAMES), out = bus_new(FRAMES);
        fx_upmix_fast_set_strength(u, strengths[variant], 5.0F);
        rng = 7U;
        for (size_t n = 0; n < FRAMES; n++) {
            float common = noise(0.1F);
            in.c[0][n] = common + noise(0.06F); in.c[1][n] = common + noise(0.06F);
        }
        run(u, &in, &out, 509U);
        center[variant] = mean_square(out.c[2], SETTLED, RATE);
        front[variant] = mean_square(out.c[0], SETTLED, RATE);
        fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
    }
    assert(center[0] < center[1] && center[1] < center[2]);
    assert(front[0] > front[1] && front[1] > front[2]);
    assert(center[2] > 4.0 * center[0]);
}

static void test_surround_strength_controls_the_surround_level(void) {
    double level[3];
    float strengths[3] = {1.0F, 5.0F, 10.0F};
    for (unsigned variant = 0; variant < 3U; variant++) {
        fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
        bus in = bus_new(FRAMES), out = bus_new(FRAMES);
        fx_upmix_fast_set_strength(u, 5.0F, strengths[variant]);
        rng = 11U;
        for (size_t n = 0; n < FRAMES; n++) { in.c[0][n] = noise(0.1F); in.c[1][n] = noise(0.1F); }
        run(u, &in, &out, 509U);
        level[variant] = mean_square(out.c[6], SETTLED, RATE);
        fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
    }
    assert(level[0] < level[1] && level[1] < level[2]);
}

static void test_zero_strength_switches_one_share_off_and_leaves_the_other_working(void) {
    /* {center, surround, expect centre, expect surround} */
    float cases[3][2] = {{0.0F, 0.0F}, {0.0F, 10.0F}, {10.0F, 0.0F}};
    for (unsigned variant = 0; variant < 3U; variant++) {
        fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
        bus in = bus_new(FRAMES), out = bus_new(FRAMES);
        size_t latency = fx_upmix_fast_latency(u);
        double input_power;
        int center_on = cases[variant][0] > 0.0F, surround_on = cases[variant][1] > 0.0F;
        fx_upmix_fast_set_strength(u, cases[variant][0], cases[variant][1]);
        rng = 21U;
        /* A common part for the centre and two independent parts for the surround feeds. */
        for (size_t n = 0; n < FRAMES; n++) {
            float common = noise(0.1F);
            in.c[0][n] = common + noise(0.1F); in.c[1][n] = common + noise(0.1F);
        }
        run(u, &in, &out, 509U);
        input_power = mean_square(in.c[0], SETTLED, RATE);
        if (center_on) assert(mean_square(out.c[2], SETTLED, RATE) > 0.3 * input_power);
        else assert(mean_square(out.c[2], 0, FRAMES) == 0.0);
        for (unsigned channel = 4U; channel < CH; channel++) {
            if (surround_on) assert(mean_square(out.c[channel], SETTLED, RATE) > 0.05 * input_power);
            else assert(mean_square(out.c[channel], 0, FRAMES) == 0.0);
        }
        if (!center_on && !surround_on) {
            /* Both shares off: exactly the delayed input, nothing taken out of L/R. */
            assert(max_abs_diff(out.c[0] + latency, in.c[0], FRAMES - latency) == 0.0);
            assert(max_abs_diff(out.c[1] + latency, in.c[1], FRAMES - latency) == 0.0);
        }
        if (center_on && !surround_on) {
            /* Only the centre rotation leaves L/R; the side component stays. */
            assert(mean_square(out.c[0], SETTLED, RATE) < 0.9 * input_power);
        }
        fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
    }
}

static void test_zero_strength_is_below_the_weakest_steering(void) {
    double center[2], surround[2];
    float strengths[2] = {0.0F, 1.0F};
    for (unsigned variant = 0; variant < 2U; variant++) {
        fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
        bus in = bus_new(FRAMES), out = bus_new(FRAMES);
        fx_upmix_fast_set_strength(u, strengths[variant], strengths[variant]);
        rng = 31U;
        for (size_t n = 0; n < FRAMES; n++) {
            float common = noise(0.1F);
            in.c[0][n] = common + noise(0.1F); in.c[1][n] = common + noise(0.1F);
        }
        run(u, &in, &out, 509U);
        center[variant] = mean_square(out.c[2], SETTLED, RATE);
        surround[variant] = mean_square(out.c[6], SETTLED, RATE);
        fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
    }
    assert(center[0] == 0.0 && surround[0] == 0.0);
    assert(center[1] > 0.0 && surround[1] > 0.0);
}

static void test_strength_change_to_zero_while_running_is_smooth_and_settles_to_silence(void) {
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
    /* The release takes about 17 s to fall below the flush threshold. */
    size_t frames = 24U * RATE;
    bus in = bus_new(frames), out = bus_new(frames);
    double worst = 0.0;
    for (size_t n = 0; n < frames; n++) in.c[0][n] = in.c[1][n] = 0.3F * sinf((float)(2.0 * PI * 1000.0 * n / RATE));
    fx_upmix_fast_set_strength(u, 10.0F, 10.0F);
    {
        bus first = in, second = in; first.frames = 2U * RATE; second.frames = frames - 2U * RATE;
        bus out_first = out, out_second = out; out_first.frames = 2U * RATE; out_second.frames = frames - 2U * RATE;
        for (unsigned i = 0; i < CH; i++) { second.c[i] = in.c[i] + 2U * RATE; out_second.c[i] = out.c[i] + 2U * RATE; }
        run(u, &first, &out_first, 509U);
        fx_upmix_fast_set_strength(u, 0.0F, 0.0F);
        run(u, &second, &out_second, 509U);
    }
    for (size_t n = 1; n < frames; n++) {
        double step = fabs((double)out.c[2][n] - out.c[2][n - 1U]);
        if (step > worst) worst = step;
    }
    assert(worst < 0.07);
    /* The share released to nothing: the centre is silent and L/R are the input again. */
    assert(mean_square(out.c[2], frames - RATE, RATE) == 0.0);
    assert(max_abs_diff(out.c[0] + frames - RATE, in.c[0] + frames - RATE - fx_upmix_fast_latency(u), RATE) < 1e-6);
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_real_multichannel_content_disengages_and_re_engages_late(void) {
    size_t frames = 25U * RATE;
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
    bus in = bus_new(frames), out = bus_new(frames);
    size_t latency = fx_upmix_fast_latency(u);
    fx_upmix_fast_set_strength(u, 10.0F, 10.0F);
    assert(fx_upmix_fast_engagement(u) == 1.0F);
    for (size_t n = 0; n < frames; n++) {
        float common = noise(0.1F);
        in.c[0][n] = common + noise(0.06F); in.c[1][n] = common + noise(0.06F);
        /* Real centre and surround content during the first five seconds. */
        if (n < 5U * RATE) { in.c[2][n] = noise(0.01F); in.c[6][n] = noise(0.01F); }
    }
    run(u, &in, &out, 509U);
    /* Disengaged: every channel is its input, without the delay of the matrix. */
    assert(max_abs_diff(out.c[0] + RATE, in.c[0] + RATE, 3U * RATE) == 0.0);
    assert(max_abs_diff(out.c[6] + RATE, in.c[6] + RATE, 3U * RATE) == 0.0);
    /* Still disengaged inside the 10 s hold after the content stopped at 5 s. */
    assert(max_abs_diff(out.c[2] + 6U * RATE, in.c[2] + 6U * RATE, 8U * RATE) == 0.0);
    /* Engaged again once the extra channels stayed silent for the hold time. */
    assert(mean_square(out.c[2], 24U * RATE, RATE) > 1e-4);
    assert(fx_upmix_fast_engagement(u) > 0.95F && fx_upmix_fast_latency(u) == latency);
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
}

/* Detector stability. These run for minutes of programme, so they stream in
 * small blocks at a low rate (the detector times are in seconds) instead of
 * holding the whole programme in memory. */
#define LONG_RATE 16000U
#define SECONDS(count) ((size_t)((count) * LONG_RATE))

typedef struct {
    unsigned rate;
    float rear_noise;                       /* white noise RMS in SL */
    float rear_sine;                        /* 1 kHz sine peak in BL */
    float ping_level;                       /* 1 kHz sine peak for the bursts below ... */
    unsigned ping_channel;                  /* ... in this channel (0 = SR) */
    size_t ping_period, ping_length;        /* periodic bursts (0 = none) */
    size_t burst_from, burst_to;            /* one more burst */
    size_t spike_at; float spike_value;     /* single-sample glitch in BR */
} scene;

typedef struct { float lowest, highest; unsigned off_toggles, on_toggles; } trace;

/* A stereo programme in FL/FR plus whatever the scene puts into the other channels. */
static void scene_frame(const scene *s, size_t n, float *frame) {
    float common = noise(0.1F);
    double phase = 2.0 * PI * 1000.0 * (double)n / s->rate;
    memset(frame, 0, CH * sizeof *frame);
    frame[0] = common + noise(0.06F); frame[1] = common + noise(0.06F);
    if (s->rear_noise > 0.0F) frame[4] = noise(s->rear_noise);
    if (s->rear_sine > 0.0F) frame[6] = s->rear_sine * (float)sin(phase);
    if ((s->ping_period && n % s->ping_period < s->ping_length) || (n >= s->burst_from && n < s->burst_to))
        frame[s->ping_channel ? s->ping_channel : 5U] = s->ping_level * (float)sin(phase);
    if (s->spike_value != 0.0F && n == s->spike_at) frame[7] = s->spike_value;
}

/* Runs frames [begin, begin + frames) of the scene and follows the engagement. */
static trace stream(fx_upmix_fast *u, const scene *s, size_t begin, size_t frames) {
    enum { BLOCK = 400 };
    float input[CH][BLOCK], output[CH][BLOCK];
    const float *ip[CH]; float *op[CH];
    trace t = {1.0F, 0.0F, 0, 0};
    int engaged = fx_upmix_fast_engagement(u) > 0.5F;
    for (unsigned c = 0; c < CH; c++) { ip[c] = input[c]; op[c] = output[c]; }
    for (size_t done = 0; done + BLOCK <= frames; done += BLOCK) {
        float level;
        for (size_t k = 0; k < BLOCK; k++) {
            float frame[CH];
            scene_frame(s, begin + done + k, frame);
            for (unsigned c = 0; c < CH; c++) input[c][k] = frame[c];
        }
        fx_upmix_fast_process(u, ip, op, BLOCK);
        level = fx_upmix_fast_engagement(u);
        if (level < t.lowest) t.lowest = level;
        if (level > t.highest) t.highest = level;
        if ((level > 0.5F) != engaged) { engaged = level > 0.5F; if (engaged) t.on_toggles++; else t.off_toggles++; }
    }
    return t;
}

static void render_scene(const scene *s, bus *in) {
    for (size_t n = 0; n < in->frames; n++) {
        float frame[CH];
        scene_frame(s, n, frame);
        for (unsigned channel = 0; channel < CH; channel++) in->c[channel][n] = frame[channel];
    }
}

/* Processes frames [begin, end) of whole buses. */
static void run_span(fx_upmix_fast *u, const bus *in, bus *out, size_t begin, size_t end) {
    bus in_part = *in, out_part = *out;
    in_part.frames = out_part.frames = end - begin;
    for (unsigned channel = 0; channel < CH; channel++) { in_part.c[channel] = in->c[channel] + begin; out_part.c[channel] = out->c[channel] + begin; }
    run(u, &in_part, &out_part, 400U);
}

/* Stereo, a Center signal from 2 s to 5 s, stereo again: the path and the reported latency follow
 * the programme by themselves, back after the 10 s hold. With both strengths at 0 the matrix path
 * is an exact delay, so every sample is the direct input, the delayed input or, during a
 * crossfade, something between the two. */
static void test_stereo_to_multichannel_and_back_switches_the_path_and_the_latency(void) {
    scene s = {.rate = LONG_RATE, .ping_channel = 2U, .ping_level = 0.1F, .burst_from = SECONDS(2), .burst_to = SECONDS(5)};
    size_t frames = SECONDS(20);
    for (unsigned variant = 0; variant < 2U; variant++) {
        fx_upmix_fast *u = fx_upmix_fast_create(LONG_RATE, 1, 1, 1);
        bus in = bus_new(frames), out = bus_new(frames);
        size_t delay = fx_upmix_fast_latency(u), fade = delay;
        size_t direct_from = SECONDS(2) + 2U + fade, direct_to = SECONDS(15), matrix_again = SECONDS(15.6);
        float strength = variant ? 10.0F : 0.0F;
        assert(delay == 22U);
        fx_upmix_fast_set_strength(u, strength, strength);
        render_scene(&s, &in);
        run_span(u, &in, &out, 0, SECONDS(2));
        assert(fx_upmix_fast_latency(u) == delay && fx_upmix_fast_engagement(u) == 1.0F);
        run_span(u, &in, &out, SECONDS(2), SECONDS(2.05));
        assert(fx_upmix_fast_latency(u) == 0U && fx_upmix_fast_engagement(u) < 0.05F);
        run_span(u, &in, &out, SECONDS(2.05), SECONDS(15));           /* about 9.8 s after the Center fell silent */
        assert(fx_upmix_fast_latency(u) == 0U && fx_upmix_fast_engagement(u) == 0.0F);
        run_span(u, &in, &out, SECONDS(15), SECONDS(15.5));
        assert(fx_upmix_fast_latency(u) == delay);
        run_span(u, &in, &out, SECONDS(15.5), frames);
        assert(fx_upmix_fast_latency(u) == delay && fx_upmix_fast_engagement(u) > 0.9F);
        for (unsigned channel = 0; channel < CH; channel++)
            assert(memcmp(out.c[channel] + direct_from, in.c[channel] + direct_from, (direct_to - direct_from) * sizeof(float)) == 0);
        if (variant) {
            /* The upmix works before the switch and resumes after the return. */
            assert(mean_square(out.c[2], SECONDS(1), SECONDS(1)) > 0.5 * mean_square(in.c[0], SECONDS(1), SECONDS(1)));
            assert(mean_square(out.c[2], SECONDS(18.5), SECONDS(1.5)) > 0.5 * mean_square(in.c[0], SECONDS(18.5), SECONDS(1.5)));
        } else {
            for (unsigned channel = 0; channel < CH; channel++) {
                for (size_t n = delay; n < SECONDS(2); n++) assert(out.c[channel][n] == in.c[channel][n - delay]);
                for (size_t n = matrix_again; n < frames; n++) assert(out.c[channel][n] == in.c[channel][n - delay]);
                for (size_t n = delay; n < frames; n++) {
                    float now = in.c[channel][n], before = in.c[channel][n - delay];
                    assert(out.c[channel][n] >= (now < before ? now : before) && out.c[channel][n] <= (now > before ? now : before));
                }
            }
        }
        fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
    }
}

/* The return to the matrix path is the same raised cosine backwards. A slow ramp in FL/FR
 * makes the two paths differ by a constant, so the share of the direct path can be read off the
 * output. The Center signal ends after 1 s; the hold runs out about 10.2 s later. */
static void test_the_return_fades_back_to_the_matrix_path(void) {
    size_t frames = SECONDS(13);
    fx_upmix_fast *u = fx_upmix_fast_create(LONG_RATE, 1, 1, 1);
    bus in = bus_new(frames), out = bus_new(frames);
    size_t delay = fx_upmix_fast_latency(u), fade = delay, middle = SECONDS(11), start;
    double threshold;
    fx_upmix_fast_set_strength(u, 0.0F, 0.0F);
    for (size_t n = 0; n < frames; n++) {
        in.c[0][n] = in.c[1][n] = (float)(-0.4 + 0.8 * (double)n / (double)frames);
        if (n < SECONDS(1)) in.c[2][n] = 0.1F * (float)sin(2.0 * PI * 1000.0 * (double)n / LONG_RATE);
    }
    run_span(u, &in, &out, 0, frames);
    assert(fx_upmix_fast_latency(u) == delay);
    /* The first frame past the middle of the fade (between the two blend steps around it) locates its start. */
    assert(fade % 2U == 0U);
    threshold = 0.5 - 0.25 * sin(PI / (double)fade);
    while (middle < frames && ((double)out.c[0][middle] - in.c[0][middle - delay]) >= threshold * ((double)in.c[0][middle] - in.c[0][middle - delay])) middle++;
    assert(middle > SECONDS(11.1) && middle < SECONDS(11.4));
    start = middle - fade / 2U;
    for (size_t n = SECONDS(2); n < start; n++) assert(out.c[0][n] == in.c[0][n]);
    for (size_t k = 0; k + 1U < fade; k++) {
        double direct = in.c[0][start + k], delayed = in.c[0][start + k - delay];
        double share = ((double)out.c[0][start + k] - delayed) / (direct - delayed);
        assert(fabs(share - (0.5 - 0.5 * cos(PI * (double)(fade - 1U - k) / (double)fade))) < 0.01);
    }
    for (size_t n = start + fade - 1U; n < frames; n++) assert(out.c[0][n] == in.c[0][n - delay]);
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
}

/* The matrix starts over when the stereo programme returns: the Center steering it held when the
 * multichannel content took over does not come back 10 s later. Mono before the Center signal
 * (2 s to 5 s), hard left from then on, which has no centre at all. */
static void test_the_return_to_the_matrix_path_keeps_nothing_from_before_the_switch(void) {
    size_t frames = SECONDS(20);
    fx_upmix_fast *u = fx_upmix_fast_create(LONG_RATE, 1, 1, 1);
    bus in = bus_new(frames), out = bus_new(frames);
    fx_upmix_fast_set_strength(u, 10.0F, 10.0F);
    for (size_t n = 0; n < frames; n++) {
        in.c[0][n] = noise(0.1F);
        if (n < SECONDS(2)) in.c[1][n] = in.c[0][n];
        else if (n < SECONDS(5)) in.c[2][n] = 0.1F * (float)sin(2.0 * PI * 1000.0 * (double)n / LONG_RATE);
    }
    run_span(u, &in, &out, 0, frames);
    assert(fx_upmix_fast_latency(u) == 22U && fx_upmix_fast_engagement(u) > 0.9F);
    assert(mean_square(out.c[2], SECONDS(1), SECONDS(1)) > 0.5 * mean_square(in.c[0], SECONDS(1), SECONDS(1)));
    assert(mean_square(out.c[2], SECONDS(6), frames - SECONDS(6)) == 0.0);
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
}

/* CPU seconds for 60 s of a stereo or a 5.1 programme. */
static double process_seconds(int multichannel) {
    enum { BLOCK = 512 };
    static float input[CH][BLOCK], output[CH][BLOCK];
    const float *ip[CH]; float *op[CH];
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
    clock_t begin;
    double spent;
    for (unsigned channel = 0; channel < CH; channel++) { ip[channel] = input[channel]; op[channel] = output[channel]; }
    for (size_t k = 0; k < BLOCK; k++) {
        float common = noise(0.1F);
        input[0][k] = common + noise(0.06F); input[1][k] = common + noise(0.06F);
        for (unsigned channel = 2U; channel < 6U; channel++) input[channel][k] = multichannel ? noise(0.05F) : 0.0F;
    }
    for (size_t done = 0; done < RATE; done += BLOCK) fx_upmix_fast_process(u, ip, op, BLOCK);
    begin = clock();
    for (size_t done = 0; done < 60U * RATE; done += BLOCK) fx_upmix_fast_process(u, ip, op, BLOCK);
    spent = (double)(clock() - begin) / CLOCKS_PER_SEC;
    assert(fx_upmix_fast_latency(u) == (multichannel ? 0U : 64U));
    fx_upmix_fast_destroy(u);
    return spent;
}

/* A bypassed upmixer copies its input; side chain, statistics and the phase filter are not run. */
static void test_the_direct_path_runs_no_matrix(void) {
    double matrix = process_seconds(0), direct = process_seconds(1);
    assert(direct * 1.5 < matrix);
}

/* Rear levels in the band between the thresholds do not flip the detector. */
static void test_levels_around_the_threshold_do_not_flutter(void) {
    scene s = {.rate = LONG_RATE};
    fx_upmix_fast *u = fx_upmix_fast_create(LONG_RATE, 1, 1, 1);
    trace t;
    /* Noise whose peaks reach -66 dBFS but whose level is below the threshold stays a stereo source. */
    s.rear_noise = 0.00025F;     /* -72 dBFS RMS */
    t = stream(u, &s, 0, SECONDS(120));
    assert(t.off_toggles == 0 && t.lowest == 1.0F);
    /* Clearly above the threshold it disengages once and stays so. */
    s.rear_noise = 0.0007F;      /* -63 dBFS RMS */
    t = stream(u, &s, SECONDS(120), SECONDS(120));
    assert(t.off_toggles <= 1 && t.on_toggles == 0 && fx_upmix_fast_engagement(u) < 0.01F);
    fx_upmix_fast_destroy(u);

    /* Hysteresis: a sine between the off level (-72 dBFS) and the on level (-66 dBFS) ... */
    u = fx_upmix_fast_create(LONG_RATE, 1, 1, 1);
    memset(&s, 0, sizeof s); s.rate = LONG_RATE;
    s.rear_sine = 0.0004F;       /* -68 dBFS */
    t = stream(u, &s, 0, SECONDS(120));
    assert(t.off_toggles == 0 && t.lowest == 1.0F);                      /* ... does not disengage a stereo source ... */
    s.rear_sine = 0.001F;        /* -60 dBFS: real content */
    t = stream(u, &s, SECONDS(120), SECONDS(5));
    assert(fx_upmix_fast_engagement(u) < 0.01F);
    s.rear_sine = 0.0004F;
    t = stream(u, &s, SECONDS(125), SECONDS(120));
    assert(t.highest < 0.01F && t.on_toggles == 0);                      /* ... but keeps a disengaged one disengaged. */
    /* Once it falls below the off level the normal hold applies. */
    s.rear_sine = 0.0002F;       /* -74 dBFS */
    t = stream(u, &s, SECONDS(245), SECONDS(8));
    assert(t.highest < 0.01F);
    t = stream(u, &s, SECONDS(253), SECONDS(7));
    assert(fx_upmix_fast_engagement(u) > 0.9F);
    fx_upmix_fast_destroy(u);
}

static void test_short_glitches_do_not_disengage_but_real_content_does(void) {
    scene s = {.rate = LONG_RATE, .spike_at = SECONDS(5)};
    fx_upmix_fast *u = fx_upmix_fast_create(LONG_RATE, 1, 1, 1);
    trace t;
    s.spike_value = 0.005F;      /* a single sample at -46 dBFS */
    t = stream(u, &s, 0, SECONDS(20));
    assert(t.off_toggles == 0 && t.lowest == 1.0F);
    fx_upmix_fast_destroy(u);

    u = fx_upmix_fast_create(LONG_RATE, 1, 1, 1);
    s.spike_value = 0.5F;        /* a loud click is real signal */
    t = stream(u, &s, 0, SECONDS(10));
    assert(t.off_toggles == 1);
    fx_upmix_fast_destroy(u);
}

static void test_multichannel_onset_disengages_within_milliseconds(void) {
    scene s = {.rate = RATE};
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
    s.burst_from = 2U * RATE; s.burst_to = 10U * RATE; s.ping_level = 0.01F;       /* -40 dBFS */
    stream(u, &s, 0, 2U * RATE);
    assert(fx_upmix_fast_engagement(u) == 1.0F);
    stream(u, &s, 2U * RATE, RATE / 50U);                                           /* 20 ms later */
    assert(fx_upmix_fast_engagement(u) < 0.05F);
    fx_upmix_fast_destroy(u);
}

/* A disengaged matrix must not leave a denormal in its state: every sample would then run slow on x86. */
static void test_a_disengaged_matrix_runs_without_denormals(void) {
    scene s = {.rate = RATE, .burst_to = 12U * RATE, .ping_level = 0.01F};
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
    stream(u, &s, 0, 10U * RATE);                                  /* disengaged from the first millisecond on */
    feclearexcept(FE_UNDERFLOW);
    stream(u, &s, 10U * RATE, RATE);
    assert(fx_upmix_fast_engagement(u) == 0.0F);
    assert(!fetestexcept(FE_UNDERFLOW));
    fx_upmix_fast_destroy(u);
}

/* Any single extra channel counts as multichannel content, and the matrix returns after a fixed 10 s. */
static void test_every_extra_channel_holds_the_upmixer_off_for_ten_seconds(void) {
    for (unsigned channel = 2U; channel < CH; channel++) {
        scene s = {.rate = LONG_RATE, .ping_level = 0.01F, .ping_channel = channel, .burst_to = SECONDS(3)};
        fx_upmix_fast *u = fx_upmix_fast_create(LONG_RATE, 1, 1, 1);
        trace t = stream(u, &s, 0, SECONDS(3));
        assert(fx_upmix_fast_engagement(u) < 0.01F);
        t = stream(u, &s, SECONDS(3), SECONDS(9.9));               /* 9.9 s after the content stopped */
        assert(t.highest < 0.01F);
        stream(u, &s, SECONDS(12.9), SECONDS(3.1));                /* 3 s after the hold was over */
        assert(fx_upmix_fast_engagement(u) > 0.9F);
        fx_upmix_fast_destroy(u);
    }
}

/* Repeated interruptions do not lengthen the wait: it is 10 s after the last content, whatever came before. */
static void test_the_hold_stays_ten_seconds_after_repeated_interruptions(void) {
    scene s = {.rate = LONG_RATE, .ping_level = 0.003F, .ping_period = SECONDS(15), .ping_length = SECONDS(0.05)};
    fx_upmix_fast *u = fx_upmix_fast_create(LONG_RATE, 1, 1, 1);
    trace t = stream(u, &s, 0, SECONDS(95));                       /* bursts at 0, 15, ... 90 s: engaged in between */
    assert(t.off_toggles >= 6 && t.on_toggles >= 5);
    s.ping_period = 0;
    t = stream(u, &s, SECONDS(95), SECONDS(4));                    /* 9 s after the last burst */
    assert(t.highest < 0.01F);
    stream(u, &s, SECONDS(99), SECONDS(7));                        /* 16 s after it */
    assert(fx_upmix_fast_engagement(u) > 0.9F);
    fx_upmix_fast_destroy(u);
}

static void test_lfe_is_never_synthesised(void) {
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
    bus in = bus_new(FRAMES), out = bus_new(FRAMES);
    fx_upmix_fast_set_strength(u, 10.0F, 10.0F);
    for (size_t n = 0; n < FRAMES; n++) { in.c[0][n] = noise(0.1F); in.c[1][n] = noise(0.1F); }
    run(u, &in, &out, 509U);
    assert(mean_square(out.c[3], 0, FRAMES) == 0.0);
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_no_destination_means_pure_delayed_pass_through(void) {
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 0, 0, 0);
    bus in = bus_new(FRAMES), out = bus_new(FRAMES);
    size_t latency = fx_upmix_fast_latency(u);
    for (size_t n = 0; n < FRAMES; n++) { in.c[0][n] = noise(0.1F); in.c[1][n] = noise(0.1F); }
    run(u, &in, &out, 509U);
    assert(max_abs_diff(out.c[0] + latency, in.c[0], FRAMES - latency) == 0.0);
    assert(max_abs_diff(out.c[1] + latency, in.c[1], FRAMES - latency) == 0.0);
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_result_does_not_depend_on_the_block_size(void) {
    size_t sizes[] = {1U, 7U, 509U, 1024U, 4096U};
    bus in = bus_new(FRAMES), reference = bus_new(FRAMES), out = bus_new(FRAMES);
    for (size_t n = 0; n < FRAMES; n++) { float common = noise(0.1F); in.c[0][n] = common + noise(0.05F); in.c[1][n] = common + noise(0.05F); }
    /* A Center signal in the last second: the switch to the direct path is part of the result. */
    for (size_t n = SETTLED; n < FRAMES; n++) in.c[2][n] = noise(0.05F);
    for (unsigned i = 0; i < 5U; i++) {
        fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
        fx_upmix_fast_set_strength(u, 8.0F, 7.0F);
        run(u, &in, i ? &out : &reference, sizes[i]);
        if (i) for (unsigned channel = 0; channel < CH; channel++) assert(memcmp(out.c[channel], reference.c[channel], FRAMES * sizeof(float)) == 0);
        fx_upmix_fast_destroy(u);
    }
    bus_free(&in); bus_free(&reference); bus_free(&out);
}

static void test_strength_change_while_running_is_smooth(void) {
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
    bus in = bus_new(2U * RATE), out = bus_new(2U * RATE);
    double worst = 0.0;
    for (size_t n = 0; n < 2U * RATE; n++) in.c[0][n] = in.c[1][n] = 0.3F * sinf((float)(2.0 * PI * 1000.0 * n / RATE));
    fx_upmix_fast_set_strength(u, 1.0F, 1.0F);
    {
        bus first = in, second = in; first.frames = RATE; second.frames = RATE;
        bus out_first = out, out_second = out; out_first.frames = RATE; out_second.frames = RATE;
        for (unsigned i = 0; i < CH; i++) { second.c[i] = in.c[i] + RATE; out_second.c[i] = out.c[i] + RATE; }
        run(u, &first, &out_first, 509U);
        fx_upmix_fast_set_strength(u, 10.0F, 10.0F);
        run(u, &second, &out_second, 509U);
    }
    for (size_t n = 1; n < 2U * RATE; n++) {
        double step = fabs((double)out.c[2][n] - out.c[2][n - 1U]);
        if (step > worst) worst = step;
    }
    /* A 0.42 amplitude 1 kHz tone moves at most 0.056 per frame. */
    assert(worst < 0.07);
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_non_finite_input_never_reaches_the_output(void) {
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
    bus in = bus_new(8192U), out = bus_new(8192U);
    for (size_t n = 0; n < 8192U; n++) { in.c[0][n] = noise(0.1F); in.c[1][n] = noise(0.1F); }
    in.c[0][1000] = NAN; in.c[1][1200] = INFINITY; in.c[2][1400] = -INFINITY;
    /* The same on the direct path a Center signal selects from frame 4000 on. */
    for (size_t n = 4000U; n < 8192U; n++) in.c[2][n] = noise(0.05F);
    in.c[0][6000] = NAN; in.c[4][6500] = INFINITY; in.c[1][7000] = -INFINITY;
    run(u, &in, &out, 509U);
    assert(fx_upmix_fast_latency(u) == 0U);
    assert(out.c[0][6000] == 0.0F && out.c[4][6500] == 0.0F && out.c[1][7000] == 0.0F);
    for (unsigned channel = 0; channel < CH; channel++)
        for (size_t n = 0; n < 8192U; n++) assert(isfinite(out.c[channel][n]));
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_silence_stays_silent_and_reset_clears_the_state(void) {
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
    bus loud = bus_new(RATE), quiet = bus_new(RATE), out = bus_new(RATE), surround = bus_new(RATE);
    for (size_t n = 0; n < RATE; n++) { loud.c[0][n] = noise(0.1F); loud.c[1][n] = noise(0.1F); }
    fill_layout(&surround, 0x3FU);
    run(u, &loud, &out, 509U);
    /* A reset also leaves the direct path a multichannel programme selected. */
    run(u, &surround, &out, 509U);
    assert(fx_upmix_fast_latency(u) == 0U);
    fx_upmix_fast_reset(u);
    assert(fx_upmix_fast_latency(u) == 64U);
    run(u, &quiet, &out, 509U);
    for (unsigned channel = 0; channel < CH; channel++) assert(mean_square(out.c[channel], 0, RATE) == 0.0);
    fx_upmix_fast_destroy(u); bus_free(&loud); bus_free(&quiet); bus_free(&out); bus_free(&surround);
}

static void test_strength_validation(void) {
    assert(fx_upmix_fast_strength_valid(1.0F) && fx_upmix_fast_strength_valid(10.0F) && fx_upmix_fast_strength_valid(5.5F));
    assert(fx_upmix_fast_strength_valid(0.0F));
    assert(!fx_upmix_fast_strength_valid(-0.5F) && !fx_upmix_fast_strength_valid(10.5F) &&
           !fx_upmix_fast_strength_valid(NAN) && !fx_upmix_fast_strength_valid(INFINITY));
}

int main(void) {
    test_latency_is_reported_and_scales_with_rate();
    test_every_channel_of_the_matrix_path_is_delayed_by_exactly_the_latency();
    test_only_a_two_channel_programme_is_upmixed_and_delayed();
    test_a_multichannel_onset_fades_in_once_and_is_not_repeated();
    test_mono_goes_to_center_and_leaves_the_front();
    test_without_a_center_output_the_centre_image_stays_in_front();
    test_surround_stays_active_without_a_center();
    test_side_only_and_rear_only_get_full_power_and_both_split_it();
    test_anti_phase_surround_feeds_are_decorrelated_and_relieve_the_front();
    test_hard_panned_material_stays_in_its_front_channel();
    test_center_strength_controls_how_much_leaves_l_r();
    test_surround_strength_controls_the_surround_level();
    test_zero_strength_switches_one_share_off_and_leaves_the_other_working();
    test_zero_strength_is_below_the_weakest_steering();
    test_strength_change_to_zero_while_running_is_smooth_and_settles_to_silence();
    test_real_multichannel_content_disengages_and_re_engages_late();
    test_stereo_to_multichannel_and_back_switches_the_path_and_the_latency();
    test_the_return_fades_back_to_the_matrix_path();
    test_the_return_to_the_matrix_path_keeps_nothing_from_before_the_switch();
    test_the_direct_path_runs_no_matrix();
    test_levels_around_the_threshold_do_not_flutter();
    test_short_glitches_do_not_disengage_but_real_content_does();
    test_multichannel_onset_disengages_within_milliseconds();
    test_a_disengaged_matrix_runs_without_denormals();
    test_every_extra_channel_holds_the_upmixer_off_for_ten_seconds();
    test_the_hold_stays_ten_seconds_after_repeated_interruptions();
    test_lfe_is_never_synthesised();
    test_no_destination_means_pure_delayed_pass_through();
    test_result_does_not_depend_on_the_block_size();
    test_strength_change_while_running_is_smooth();
    test_non_finite_input_never_reaches_the_output();
    test_silence_stays_silent_and_reset_clears_the_state();
    test_strength_validation();
    return 0;
}
