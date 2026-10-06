/* SPDX-License-Identifier: AGPL-3.0-only */
#include "upmix_fast.h"

#include <assert.h>
#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

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

static void test_every_channel_is_delayed_by_exactly_the_latency(void) {
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
    bus in = bus_new(2000U), out = bus_new(2000U);
    size_t latency = fx_upmix_fast_latency(u);
    /* An impulse per channel on its own run: pass-through must be sample exact. */
    for (unsigned channel = 2U; channel < CH; channel++) {
        fx_upmix_fast_reset(u);
        memset(in.c[channel], 0, 2000U * sizeof(float));
        in.c[channel][100] = 0.5F;
        run(u, &in, &out, 509U);
        for (size_t n = 0; n < 2000U; n++) {
            float expected = n == 100U + latency ? 0.5F : 0.0F;
            assert(out.c[channel][n] == expected);
        }
        in.c[channel][100] = 0.0F;
    }
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
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
    size_t frames = 40U * RATE;
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
    /* Disengaged: every channel equals its delayed input. */
    assert(max_abs_diff(out.c[0] + latency + RATE, in.c[0] + RATE, 3U * RATE) < 1e-6);
    assert(max_abs_diff(out.c[6] + latency + RATE, in.c[6] + RATE, 3U * RATE) < 1e-6);
    /* Still disengaged well inside the hold time after the content stopped. */
    assert(max_abs_diff(out.c[2] + latency + 20U * RATE, in.c[2] + 20U * RATE, 5U * RATE) < 1e-6);
    /* Engaged again once the extra channels stayed silent for the hold time. */
    assert(mean_square(out.c[2], 38U * RATE, RATE) > 1e-4);
    assert(fx_upmix_fast_engagement(u) > 0.95F);
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
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
    run(u, &in, &out, 509U);
    for (unsigned channel = 0; channel < CH; channel++)
        for (size_t n = 0; n < 8192U; n++) assert(isfinite(out.c[channel][n]));
    fx_upmix_fast_destroy(u); bus_free(&in); bus_free(&out);
}

static void test_silence_stays_silent_and_reset_clears_the_state(void) {
    fx_upmix_fast *u = fx_upmix_fast_create(RATE, 1, 1, 1);
    bus loud = bus_new(RATE), quiet = bus_new(RATE), out = bus_new(RATE);
    for (size_t n = 0; n < RATE; n++) { loud.c[0][n] = noise(0.1F); loud.c[1][n] = noise(0.1F); }
    run(u, &loud, &out, 509U);
    fx_upmix_fast_reset(u);
    run(u, &quiet, &out, 509U);
    for (unsigned channel = 0; channel < CH; channel++) assert(mean_square(out.c[channel], 0, RATE) == 0.0);
    fx_upmix_fast_destroy(u); bus_free(&loud); bus_free(&quiet); bus_free(&out);
}

static void test_strength_validation(void) {
    assert(fx_upmix_fast_strength_valid(1.0F) && fx_upmix_fast_strength_valid(10.0F) && fx_upmix_fast_strength_valid(5.5F));
    assert(fx_upmix_fast_strength_valid(0.0F));
    assert(!fx_upmix_fast_strength_valid(-0.5F) && !fx_upmix_fast_strength_valid(10.5F) &&
           !fx_upmix_fast_strength_valid(NAN) && !fx_upmix_fast_strength_valid(INFINITY));
}

int main(void) {
    test_latency_is_reported_and_scales_with_rate();
    test_every_channel_is_delayed_by_exactly_the_latency();
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
    test_lfe_is_never_synthesised();
    test_no_destination_means_pure_delayed_pass_through();
    test_result_does_not_depend_on_the_block_size();
    test_strength_change_while_running_is_smooth();
    test_non_finite_input_never_reaches_the_output();
    test_silence_stays_silent_and_reset_clears_the_state();
    test_strength_validation();
    return 0;
}
