#define _POSIX_C_SOURCE 200809L
#include "dsp.h"

#include <math.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

/* Loudness Strength live-update regression.
 *
 * A Strength change alters the LSP loud_comp_stereo work point p (the
 * "volume" control) and the host applies the inverse compensation -p after
 * the plugin so the stage stays level-neutral.  The two values form one
 * level-neutral transaction and must become effective on the same audio
 * block boundary: the work-point curve changes through the plugin's FFT/OLA
 * path (latency 2^rank, Hann overlap), so the host trim is crossfaded with
 * the same sample timing instead of switching instantly.
 *
 * This test drives the exact live-update sequence the runtime uses
 * (fxdsp_live_begin -> live control volume -> live param compensation ->
 * fxdsp_live_commit) while a concurrent thread runs fxdsp_process, mirroring
 * the realtime process thread that applies committed live updates.  It steps
 * Strength through both directions (1->5, 5->1, 5->4, 4->5 and the full
 * adjacent sweep) and measures the maximum intermediate output peak, not just
 * the final state. Each transition is compared with the higher settled peak
 * of its old and new Strength so a coarse FFT's legitimate crest-factor
 * change is not treated as a gain excursion. It runs the same sequence at the
 * default
 * 4096 FFT, the smallest 256 FFT (boundaries inside one block), the largest
 * 16384 FFT (boundaries spanning many blocks) and at 44.1/48/96 kHz so the
 * matched-latency compensation is not tied to one block/rate.
 *
 * The work point follows the production formula
 *   p = volumeDb - calibration + (10 - strength) * 30/9 + AutoGain
 * with volumeDb=-40, no calibration and AutoGain off, which keeps every step
 * inside the -83..+7 dB LSP port range.
 */

#define BLOCK 1024U
#define AMPLITUDE 0.1f
#define TOLERANCE_DB 0.25
#define PI 3.14159265358979323846

static int failures;

typedef struct {
    fxdsp *dsp;
    float *left, *right, *out_l, *out_r;
    double rate;
    _Atomic size_t blocks;
    _Atomic float peak;
    _Atomic int stop;
} run_ctx;

static void check(int condition, const char *message) {
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        failures++;
    }
}

static double strength_db(int strength) {
    return (10.0 - strength) * (30.0 / 9.0);
}

static double work_point(int strength) {
    const double volume_db = -40.0;
    const double calibration_db = 0.0;
    double p = volume_db - calibration_db + strength_db(strength);
    if (p < -83.0) p = -83.0;
    if (p > 7.0) p = 7.0;
    return p;
}

static int write_config(const char *path, double rate, int fft_index, int strength) {
    FILE *file = fopen(path, "w");
    if (!file) return -1;
    double p = work_point(strength);
    fprintf(file,
        "rate %.0f\n"
        "inputs 2\n"
        "outputs 2\n"
        "matrix 0 0 1\n"
        "matrix 1 1 1\n"
        "stage_begin 0 global-loudness lv2 http://lsp-plug.in/plugins/lv2/loud_comp_stereo\n"
        "control input 1\n"
        "control std 4\n"
        "control fft %d\n"
        "control volume %.9f\n"
        "control hclip 0\n"
        "control hcrange 6\n"
        "param output_gain_db %.9f\n"
        "stage_end\n"
        "output 0 0 0 normal\n"
        "output 1 0 0 normal\n"
        "bypass 0\n",
        rate, fft_index, p, -p);
    fclose(file);
    return 0;
}

static void fill_sine(float *left, float *right, size_t frames, size_t phase, double rate) {
    for (size_t n = 0; n < frames; n++) {
        double t = (double)(phase + n) / rate;
        float value = (float)(AMPLITUDE * sin(2.0 * PI * 1000.0 * t));
        left[n] = value;
        right[n] = value;
    }
}

static float run_block(fxdsp *d, float *left, float *right,
                       float *out_l, float *out_r, size_t frames) {
    const float *input[2] = {left, right};
    float *output[2] = {out_l, out_r};
    fxdsp_process(d, input, output, frames);
    float peak = 0.0f;
    for (size_t n = 0; n < frames; n++) {
        float current = fmaxf(fabsf(out_l[n]), fabsf(out_r[n]));
        if (current > peak) peak = current;
    }
    return peak;
}

static void *process_loop(void *arg) {
    run_ctx *ctx = arg;
    size_t phase = 0;
    while (!atomic_load_explicit(&ctx->stop, memory_order_relaxed)) {
        fill_sine(ctx->left, ctx->right, BLOCK, phase, ctx->rate);
        phase += BLOCK;
        float peak = run_block(ctx->dsp, ctx->left, ctx->right, ctx->out_l, ctx->out_r,
                               BLOCK);
        float current = atomic_load_explicit(&ctx->peak, memory_order_relaxed);
        while (peak > current &&
               !atomic_compare_exchange_weak_explicit(&ctx->peak, &current, peak,
                                                      memory_order_relaxed,
                                                      memory_order_relaxed)) {}
        atomic_fetch_add_explicit(&ctx->blocks, 1U, memory_order_relaxed);
    }
    return NULL;
}

static void wait_blocks(run_ctx *ctx, size_t target) {
    struct timespec ts = {0, 500000L};
    while (atomic_load_explicit(&ctx->blocks, memory_order_acquire) < target)
        nanosleep(&ts, NULL);
}

static void run_config(double rate, int fft_index) {
    unsigned rank = 8U + (unsigned)fft_index;
    size_t frame_size = (size_t)1U << (rank - 1U);
    /* Enough blocks for the FFT buffer to fill and each transition to
     * complete: the curve change can begin up to one frame after commit and
     * crossfades over one more frame. */
    size_t settle_blocks = (2U * frame_size + BLOCK - 1U) / BLOCK + 4U;

    char config_path[] = "/tmp/fxroute-loudness-strength-XXXXXX";
    char error[256];
    int fd = mkstemp(config_path);
    if (fd < 0) { check(0, "cannot create config"); return; }
    close(fd);

    if (write_config(config_path, rate, fft_index, 1)) {
        check(0, "cannot write config");
        unlink(config_path);
        return;
    }
    run_ctx ctx;
    memset(&ctx, 0, sizeof ctx);
    ctx.rate = rate;
    ctx.dsp = fxdsp_load(config_path, error, sizeof error);
    unlink(config_path);
    if (!ctx.dsp) {
        fprintf(stderr, "FAIL: cannot load loudness config (rate %.0f fft %d): %s\n",
                rate, fft_index, error);
        failures++;
        return;
    }

    ctx.left = calloc(BLOCK, sizeof(float));
    ctx.right = calloc(BLOCK, sizeof(float));
    ctx.out_l = calloc(BLOCK, sizeof(float));
    ctx.out_r = calloc(BLOCK, sizeof(float));
    if (!ctx.left || !ctx.right || !ctx.out_l || !ctx.out_r) {
        check(0, "out of memory");
        fxdsp_free(ctx.dsp);
        free(ctx.left); free(ctx.right); free(ctx.out_l); free(ctx.out_r);
        return;
    }
    atomic_init(&ctx.blocks, 0U);
    atomic_init(&ctx.peak, 0.0f);
    atomic_init(&ctx.stop, 0);

    pthread_t thread;
    if (pthread_create(&thread, NULL, process_loop, &ctx)) {
        check(0, "cannot start process thread");
        fxdsp_free(ctx.dsp);
        free(ctx.left); free(ctx.right); free(ctx.out_l); free(ctx.out_r);
        return;
    }

    wait_blocks(&ctx, settle_blocks);
    atomic_store_explicit(&ctx.peak, 0.0f, memory_order_relaxed);
    size_t reference_start = atomic_load_explicit(&ctx.blocks, memory_order_relaxed);
    wait_blocks(&ctx, reference_start + settle_blocks);
    float reference_peak = atomic_load_explicit(&ctx.peak, memory_order_acquire);
    check(reference_peak > 0.0f, "settled loudness output is non-zero");
    if (reference_peak <= 0.0f) {
        atomic_store_explicit(&ctx.stop, 1, memory_order_relaxed);
        pthread_join(thread, NULL);
        fxdsp_free(ctx.dsp);
        free(ctx.left); free(ctx.right); free(ctx.out_l); free(ctx.out_r);
        return;
    }
    /* Both directions and several steps: 1->5, 5->1, 5->4, 4->5 plus the
     * full adjacent sweep, ending back at Strength 1. */
    static const int steps[] = {5, 1, 5, 4, 5, 1, 2, 3, 4, 5, 4, 3, 2, 1};
    float previous_settled_peak = reference_peak;
    float worst_excursion_db = -1000.0f;
    int worst_strength = 0;

    for (size_t i = 0; i < sizeof(steps) / sizeof(steps[0]); i++) {
        int strength = steps[i];
        double p = work_point(strength);
        atomic_store_explicit(&ctx.peak, 0.0f, memory_order_relaxed);
        size_t start = atomic_load_explicit(&ctx.blocks, memory_order_relaxed);
        check(fxdsp_live_begin(ctx.dsp), "live begin accepted");
        check(fxdsp_live_control(ctx.dsp, "global-loudness", "volume", (float)p),
              "live volume control accepted");
        check(fxdsp_live_param(ctx.dsp, "global-loudness", "output_gain_db", (float)(-p)),
              "live compensation param accepted");
        check(fxdsp_live_commit(ctx.dsp), "live commit accepted");
        wait_blocks(&ctx, start + settle_blocks);
        float transition_peak = atomic_load_explicit(&ctx.peak, memory_order_acquire);

        atomic_store_explicit(&ctx.peak, 0.0f, memory_order_relaxed);
        size_t settled_start = atomic_load_explicit(&ctx.blocks, memory_order_relaxed);
        wait_blocks(&ctx, settled_start + settle_blocks);
        float settled_peak = atomic_load_explicit(&ctx.peak, memory_order_acquire);
        float allowed_peak = fmaxf(previous_settled_peak, settled_peak);
        float excursion_db = 20.0f * log10f(transition_peak / allowed_peak);
        fprintf(stderr, "  rate %.0f fft %d strength=%d transition_peak=%.6f settled_peak=%.6f excursion=%.3f dB\n",
                rate, fft_index, strength, transition_peak, settled_peak, excursion_db);
        if (excursion_db > worst_excursion_db) {
            worst_excursion_db = excursion_db;
            worst_strength = strength;
        }
        previous_settled_peak = settled_peak;
    }

    fprintf(stderr, "rate %.0f fft %d reference_peak=%.6f worst_excursion=%.3f dB (strength=%d)\n",
            rate, fft_index, reference_peak, worst_excursion_db, worst_strength);
    check(worst_excursion_db <= TOLERANCE_DB, "no positive gain excursion across Strength steps");

    /* Final state must be back to level-neutral at Strength 1. */
    float final_db = 20.0f * log10f(previous_settled_peak / reference_peak);
    check(fabsf(final_db) <= 0.5f, "final state returns to level-neutral");

    atomic_store_explicit(&ctx.stop, 1, memory_order_relaxed);
    pthread_join(thread, NULL);
    fxdsp_free(ctx.dsp);
    free(ctx.left); free(ctx.right); free(ctx.out_l); free(ctx.out_r);
}

int main(void) {
    run_config(48000.0, 4);   /* default 4096 FFT */
    run_config(48000.0, 0);   /* smallest 256 FFT (boundaries inside a block) */
    run_config(48000.0, 6);   /* largest 16384 FFT (boundaries span many blocks) */
    run_config(44100.0, 4);
    run_config(96000.0, 4);
    if (failures) {
        fprintf(stderr, "loudness strength test failed: %d\n", failures);
        return 1;
    }
    printf("loudness strength transition tests passed\n");
    return 0;
}
