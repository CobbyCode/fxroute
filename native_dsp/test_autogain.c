#include "autogain.h"

#include <ebur128.h>
#include <math.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define RATE 48000U
#define BLOCK 4800U

static int failures;
static _Thread_local int track_allocations;
static _Thread_local size_t tracked_allocations;

#if defined(__GLIBC__)
extern void *__libc_malloc(size_t size);
extern void *__libc_calloc(size_t count, size_t size);
extern void *__libc_realloc(void *pointer, size_t size);
extern void __libc_free(void *pointer);

void *malloc(size_t size) {
    if (track_allocations) tracked_allocations++;
    return __libc_malloc(size);
}

void *calloc(size_t count, size_t size) {
    if (track_allocations) tracked_allocations++;
    return __libc_calloc(count, size);
}

void *realloc(void *pointer, size_t size) {
    if (track_allocations) tracked_allocations++;
    return __libc_realloc(pointer, size);
}

void free(void *pointer) {
    __libc_free(pointer);
}
#endif

static void check(int condition, const char *message) {
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        failures++;
    }
}

static void check_close(double actual, double expected, double tolerance, const char *message) {
    if (!isfinite(actual) || fabs(actual - expected) > tolerance) {
        fprintf(stderr, "FAIL: %s (got %.12g, expected %.12g)\n", message, actual, expected);
        failures++;
    }
}

static void fill_sine(float *left, float *right, size_t frames, double amplitude, double *phase) {
    for (size_t i = 0; i < frames; i++) {
        left[i] = (float)(amplitude * sin(*phase));
        right[i] = (float)(0.5 * amplitude * sin(*phase));
        *phase += 2.0 * 3.14159265358979323846 * 997.0 / RATE;
    }
}

static void run_seconds(fx_autogain *gain, double amplitude, unsigned seconds,
                        float *left, float *right, float *out_left, float *out_right,
                        double *phase) {
    for (unsigned n = 0; n < seconds * RATE / BLOCK; n++) {
        fill_sine(left, right, BLOCK, amplitude, phase);
        check(fx_autogain_process(gain, left, right, out_left, out_right, BLOCK) == 0,
              "valid block processes successfully");
    }
}

static int wait_for_gain(fx_autogain *gain, double minimum) {
    for (unsigned attempt = 0; attempt < 200000U; attempt++) {
        if (fx_autogain_get_measurement(gain).gain > minimum) return 1;
        sched_yield();
    }
    return 0;
}

static int wait_for_integrated(fx_autogain *gain, double expected, double tolerance) {
    for (unsigned attempt = 0; attempt < 500000U; attempt++) {
        double actual = fx_autogain_get_measurement(gain).integrated_lufs;
        if (isfinite(actual) && fabs(actual - expected) <= tolerance) return 1;
        sched_yield();
    }
    return 0;
}

static fx_autogain *make_gain(fx_autogain_reference reference, double target,
                              double silence, unsigned history) {
    fx_autogain_config config = fx_autogain_default_config();
    config.reference = reference;
    config.target_lufs = target;
    config.silence_threshold_lufs = silence;
    config.maximum_history_seconds = history;
    return fx_autogain_init(RATE, BLOCK, &config);
}

static void test_defaults_and_validation(void) {
    fx_autogain_config config = fx_autogain_default_config();
    check(config.reference == FX_AUTOGAIN_GEOMETRIC_MEAN_MSI,
          "default reference is Geometric Mean (MSI)");
    check_close(config.target_lufs, -23.0, 0.0, "default target matches EasyEffects");
    check_close(config.silence_threshold_lufs, -70.0, 0.0,
                "default silence threshold matches FXRoute settings");
    check(config.maximum_history_seconds == 15U, "default maximum history is 15 seconds");
    check(fx_autogain_init(0, BLOCK, &config) == NULL, "zero sample rate is rejected");
    check(fx_autogain_init(RATE, 0, &config) == NULL, "zero block size is rejected");
    config.reference = (fx_autogain_reference)99;
    check(fx_autogain_init(RATE, BLOCK, &config) == NULL, "unknown reference is rejected");
    config = fx_autogain_default_config();
    config.target_lufs = 1.0;
    check(fx_autogain_init(RATE, BLOCK, &config) == NULL, "target above zero is rejected");
    config = fx_autogain_default_config();
    config.maximum_history_seconds = 5U;
    check(fx_autogain_init(RATE, BLOCK, &config) == NULL,
          "history below the EasyEffects range is rejected");
}

static void test_common_stereo_gain_and_modes(void) {
    float *left = malloc(BLOCK * sizeof(*left));
    float *right = malloc(BLOCK * sizeof(*right));
    float *out_left = malloc(BLOCK * sizeof(*out_left));
    float *out_right = malloc(BLOCK * sizeof(*out_right));
    const fx_autogain_reference modes[] = {
        FX_AUTOGAIN_MOMENTARY, FX_AUTOGAIN_SHORTTERM, FX_AUTOGAIN_INTEGRATED,
        FX_AUTOGAIN_GEOMETRIC_MEAN_MSI, FX_AUTOGAIN_GEOMETRIC_MEAN_MS,
        FX_AUTOGAIN_GEOMETRIC_MEAN_MI, FX_AUTOGAIN_GEOMETRIC_MEAN_SI
    };

    for (size_t m = 0; m < sizeof(modes) / sizeof(modes[0]); m++) {
        fx_autogain *gain = make_gain(modes[m], -23.0, -70.0, 15U);
        double phase = 0.0;
        check(gain != NULL, "all reference modes initialize");
        run_seconds(gain, 0.04, 4U, left, right, out_left, out_right, &phase);
        check(wait_for_gain(gain, 1.0), "worker publishes a measured gain");
        fx_autogain_measurement measurement = fx_autogain_get_measurement(gain);
        double expected_loudness;
        switch (modes[m]) {
            case FX_AUTOGAIN_MOMENTARY: expected_loudness = measurement.momentary_lufs; break;
            case FX_AUTOGAIN_SHORTTERM: expected_loudness = measurement.shortterm_lufs; break;
            case FX_AUTOGAIN_INTEGRATED: expected_loudness = measurement.integrated_lufs; break;
            case FX_AUTOGAIN_GEOMETRIC_MEAN_MSI:
                expected_loudness = cbrt(measurement.momentary_lufs * measurement.shortterm_lufs *
                                         measurement.integrated_lufs);
                break;
            case FX_AUTOGAIN_GEOMETRIC_MEAN_MS:
                expected_loudness = -sqrt(fabs(measurement.momentary_lufs * measurement.shortterm_lufs));
                break;
            case FX_AUTOGAIN_GEOMETRIC_MEAN_MI:
                expected_loudness = -sqrt(fabs(measurement.momentary_lufs * measurement.integrated_lufs));
                break;
            case FX_AUTOGAIN_GEOMETRIC_MEAN_SI:
                expected_loudness = -sqrt(fabs(measurement.shortterm_lufs * measurement.integrated_lufs));
                break;
            default: expected_loudness = 0.0; break;
        }
        check_close(measurement.reference_lufs, expected_loudness, 1e-9,
                    "reference mode uses EasyEffects loudness formula");
        check_close(measurement.gain, pow(10.0, (-23.0 - expected_loudness) / 20.0), 1e-9,
                    "target determines linear gain");
        for (size_t i = 0; i < BLOCK; i++) {
            if (fabsf(right[i]) > 1e-5f) {
                check_close(out_left[i] / left[i], out_right[i] / right[i], 2e-5,
                            "left and right receive one common gain");
                break;
            }
        }
        fx_autogain_free(gain);
    }
    free(left);
    free(right);
    free(out_left);
    free(out_right);
}

static void test_silence_bad_measurements_and_block_limit(void) {
    float left[BLOCK] = {0};
    float right[BLOCK] = {0};
    float out_left[BLOCK];
    float out_right[BLOCK];
    fx_autogain *gain = fx_autogain_init(RATE, BLOCK, NULL);
    check(gain != NULL, "NULL config selects defaults");
    check(fx_autogain_process(gain, left, right, out_left, out_right, BLOCK) == 0,
          "startup silence is accepted");
    fx_autogain_measurement measurement = fx_autogain_get_measurement(gain);
    check_close(measurement.momentary_lufs, 0.0, 0.0,
                "non-finite momentary startup result becomes zero");
    check_close(measurement.shortterm_lufs, 0.0, 0.0,
                "non-finite short-term startup result falls back to momentary");
    check_close(measurement.integrated_lufs, 0.0, 0.0,
                "non-finite integrated startup result falls back to momentary");
    check_close(measurement.gain, 1.0, 0.0, "silence does not update gain");
    check(fx_autogain_process(gain, left, right, out_left, out_right, BLOCK + 1U) == -1,
          "blocks larger than the initialized capacity are rejected");
    fx_autogain_free(gain);
}

static void test_peak_safety_freezes_previous_gain(void) {
    float left[BLOCK], right[BLOCK], out_left[BLOCK], out_right[BLOCK];
    double phase = 0.0;
    fx_autogain *gain = make_gain(FX_AUTOGAIN_MOMENTARY, -12.0, -70.0, 15U);
    run_seconds(gain, 0.02, 1U, left, right, out_left, out_right, &phase);
    check(wait_for_gain(gain, 1.0), "quiet material is measured asynchronously");
    double safe_gain = fx_autogain_get_measurement(gain).gain;
    check(safe_gain > 1.0, "quiet material establishes positive gain");
    fill_sine(left, right, BLOCK, 0.02, &phase);
    left[0] = 0.95f;
    check(fx_autogain_process(gain, left, right, out_left, out_right, BLOCK) == 0,
          "unsafe candidate block still processes");
    check_close(fx_autogain_get_measurement(gain).gain, safe_gain, 0.0,
                 "sample-peak safety rejects a clipping gain update");
    check(fabsf(out_left[0]) < 1.0f,
          "RT block peak caps an asynchronously calculated gain");
    fx_autogain_free(gain);
}

static void test_gain_changes_are_smoothed(void) {
    float left[BLOCK], right[BLOCK], out_left[BLOCK], out_right[BLOCK];
    double phase = 0.0;
    fx_autogain *gain = make_gain(FX_AUTOGAIN_MOMENTARY, -12.0, -70.0, 15U);
    check(gain != NULL, "smoothed gain initializes");
    run_seconds(gain, 0.02, 1U, left, right, out_left, out_right, &phase);
    check(wait_for_gain(gain, 1.0), "worker publishes smoothing target");
    double target = fx_autogain_get_measurement(gain).gain;
    fill_sine(left, right, BLOCK, 0.02, &phase);
    check(fx_autogain_process(gain, left, right, out_left, out_right, BLOCK) == 0,
          "smoothed gain block processes");
    double applied = out_left[BLOCK / 2U] / left[BLOCK / 2U];
    check(applied > 1.0 && applied < target,
          "release smoothing approaches a rising target without a step");
    fx_autogain_free(gain);
}

static ebur128_state *make_oracle(unsigned history) {
    ebur128_state *state = ebur128_init(2U, RATE, EBUR128_MODE_S | EBUR128_MODE_I |
                                                   EBUR128_MODE_LRA |
                                                   EBUR128_MODE_SAMPLE_PEAK);
    if (!state || ebur128_set_channel(state, 0U, EBUR128_LEFT) != EBUR128_SUCCESS ||
        ebur128_set_channel(state, 1U, EBUR128_RIGHT) != EBUR128_SUCCESS ||
        ebur128_set_max_history(state, history * 1000UL) != EBUR128_SUCCESS) {
        if (state) ebur128_destroy(&state);
        return NULL;
    }
    return state;
}

static void feed_history(fx_autogain *short_history, fx_autogain *long_history,
                         ebur128_state *short_oracle, ebur128_state *long_oracle,
                         double amplitude, unsigned seconds, float *left, float *right,
                         float *out_left, float *out_right, float *interleaved,
                         double *phase) {
    for (unsigned n = 0; n < seconds * RATE / BLOCK; n++) {
        fill_sine(left, right, BLOCK, amplitude, phase);
        for (size_t i = 0; i < BLOCK; i++) {
            interleaved[2U * i] = left[i];
            interleaved[2U * i + 1U] = right[i];
        }
        check(ebur128_add_frames_float(short_oracle, interleaved, BLOCK) == EBUR128_SUCCESS,
              "short-history oracle accepts frames");
        check(ebur128_add_frames_float(long_oracle, interleaved, BLOCK) == EBUR128_SUCCESS,
              "long-history oracle accepts frames");
        check(fx_autogain_process(short_history, left, right, out_left, out_right, BLOCK) == 0,
              "short-history AutoGain accepts frames");
        check(fx_autogain_process(long_history, left, right, out_left, out_right, BLOCK) == 0,
              "long-history AutoGain accepts frames");
        if (n % 4U == 3U) {
            double expected_short = 0.0, expected_long = 0.0;
            check(ebur128_loudness_global(short_oracle, &expected_short) == EBUR128_SUCCESS,
                  "short-history oracle provides an intermediate result");
            check(ebur128_loudness_global(long_oracle, &expected_long) == EBUR128_SUCCESS,
                  "long-history oracle provides an intermediate result");
            check(wait_for_integrated(short_history, expected_short, 1e-9),
                  "short-history worker drains queued frames");
            check(wait_for_integrated(long_history, expected_long, 1e-9),
                  "long-history worker drains queued frames");
        }
    }
}

static void test_maximum_history_matches_non_histogram_ebur128(void) {
    float left[BLOCK], right[BLOCK], out_left[BLOCK], out_right[BLOCK];
    float interleaved[BLOCK * 2U];
    double phase = 0.0;
    fx_autogain *short_history = make_gain(FX_AUTOGAIN_INTEGRATED, -23.0, -70.0, 6U);
    fx_autogain *long_history = make_gain(FX_AUTOGAIN_INTEGRATED, -23.0, -70.0, 30U);
    ebur128_state *short_oracle = make_oracle(6U);
    ebur128_state *long_oracle = make_oracle(30U);
    check(short_history && long_history && short_oracle && long_oracle,
          "bounded-history instances initialize");
    if (!short_history || !long_history || !short_oracle || !long_oracle) goto cleanup;

    feed_history(short_history, long_history, short_oracle, long_oracle, 0.2, 8U,
                 left, right, out_left, out_right, interleaved, &phase);
    feed_history(short_history, long_history, short_oracle, long_oracle, 0.02, 8U,
                 left, right, out_left, out_right, interleaved, &phase);
    double expected_short = 0.0, expected_long = 0.0;
    check(ebur128_loudness_global(short_oracle, &expected_short) == EBUR128_SUCCESS,
          "short-history oracle produces integrated loudness");
    check(ebur128_loudness_global(long_oracle, &expected_long) == EBUR128_SUCCESS,
          "long-history oracle produces integrated loudness");
    check(wait_for_integrated(short_history, expected_short, 1e-9),
          "worker reaches exact short-history result");
    check(wait_for_integrated(long_history, expected_long, 1e-9),
          "worker reaches exact long-history result");
    double short_lufs = fx_autogain_get_measurement(short_history).integrated_lufs;
    double long_lufs = fx_autogain_get_measurement(long_history).integrated_lufs;
    check_close(short_lufs, expected_short, 1e-9,
                "six-second history matches non-histogram libebur128");
    check_close(long_lufs, expected_long, 1e-9,
                "thirty-second history matches non-histogram libebur128");
    check(fabs(short_lufs - long_lufs) > 1.0,
          "maximum history changes integrated loudness after old audio expires");
cleanup:
    if (short_oracle) ebur128_destroy(&short_oracle);
    if (long_oracle) ebur128_destroy(&long_oracle);
    fx_autogain_free(short_history);
    fx_autogain_free(long_history);
}

static void test_processing_does_not_allocate(void) {
#if defined(__GLIBC__)
    float left[BLOCK], right[BLOCK], out_left[BLOCK], out_right[BLOCK];
    double phase = 0.0;
    fx_autogain *gain = make_gain(FX_AUTOGAIN_INTEGRATED, -23.0, -70.0, 15U);
    check(gain != NULL, "allocation test initializes AutoGain");
    if (!gain) return;

    tracked_allocations = 0U;
    track_allocations = 1;
    for (unsigned second = 0U; second < 12U; second++) {
        run_seconds(gain, second % 2U ? 0.02 : 0.2, 1U,
                    left, right, out_left, out_right, &phase);
    }
    track_allocations = 0;

    check(tracked_allocations == 0U,
          "fx_autogain_process performs no allocation on the calling thread");
    fx_autogain_free(gain);
#endif
}

int main(void) {
    test_defaults_and_validation();
    test_common_stereo_gain_and_modes();
    test_silence_bad_measurements_and_block_limit();
    test_peak_safety_freezes_previous_gain();
    test_gain_changes_are_smoothed();
    test_maximum_history_matches_non_histogram_ebur128();
    test_processing_does_not_allocate();
    if (failures) return 1;
    printf("autogain tests passed\n");
    return 0;
}
