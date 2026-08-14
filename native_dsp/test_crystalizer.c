#include "crystalizer.h"

#include <assert.h>
#include <math.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

#define RATE 48000U
#define FRAMES 8192U
#define PI 3.14159265358979323846

static double rms(const float *values, size_t begin, size_t count) {
    double square = 0.0;
    for (size_t i = begin; i < begin + count; i++) square += values[i] * values[i];
    return sqrt(square / (double)count);
}

static void test_uses_8_2_8_band_configuration(void) {
    static const float expected_edges[14] = {
        20.0F, 219.331F, 485.102F, 867.511F, 1395.82F, 2102.93F, 3026.51F,
        4209.57F, 5701.10F, 7556.64F, 9839.05F, 12619.1F, 15976.4F, 20000.0F
    };
    fx_crystalizer *crystalizer = fx_crystalizer_create(RATE);
    assert(crystalizer != NULL);
    assert(fx_crystalizer_band_count() == 13U);
    for (size_t i = 0; i < 14U; i++)
        assert(fabsf(fx_crystalizer_band_edge(i) - expected_edges[i]) < expected_edges[i] * 2e-4F);
    for (size_t i = 0; i < 13U; i++) {
        float expected = i == 2U ? powf(10.0F, -2.0F / 20.0F) : 1.0F;
        assert(fabsf(fx_crystalizer_base_intensity(crystalizer, i) - expected) < 1e-6F);
        assert(fabsf(fx_crystalizer_adaptive_intensity(crystalizer, i) - expected) < 1e-6F);
    }
    assert(fx_crystalizer_oversampling_quality(crystalizer) == 5U);
    assert(fabsf(fx_crystalizer_transition_band(crystalizer) - 120.0F) < 1e-6F);
    fx_crystalizer_set_band_intensity_db(crystalizer, 2U, -6.0F);
    assert(fabsf(fx_crystalizer_base_intensity(crystalizer, 2U) -
                 powf(10.0F, -6.0F / 20.0F)) < 1e-6F);
    fx_crystalizer_destroy(crystalizer);
}

static void test_impulse_has_oversampled_fir_latency(void) {
    fx_crystalizer *crystalizer = fx_crystalizer_create(RATE);
    float input[FRAMES] = {0}, output[FRAMES] = {0};
    size_t peak_at = 0;
    assert(crystalizer != NULL);
    input[0] = 1.0F;
    fx_crystalizer_process(crystalizer, input, output, FRAMES);
    for (size_t i = 1; i < FRAMES; i++) if (fabsf(output[i]) > fabsf(output[peak_at])) peak_at = i;
    assert(peak_at == fx_crystalizer_latency(crystalizer));
    assert(fabsf(output[peak_at]) > 1.0F);
    assert(output[peak_at] < 0.0F);
    fx_crystalizer_destroy(crystalizer);
}

static void test_adaptive_intensity_tracks_program_material_and_resets(void) {
    fx_crystalizer *crystalizer = fx_crystalizer_create(RATE);
    float *input = calloc(FRAMES, sizeof *input), *output = calloc(FRAMES, sizeof *output);
    assert(crystalizer != NULL && input != NULL && output != NULL);
    for (size_t i = 0; i < FRAMES; i++) input[i] = 0.1F * sinf((float)(2.0 * PI * 1500.0 * i / RATE));
    fx_crystalizer_process(crystalizer, input, output, FRAMES);
    float initial = fx_crystalizer_adaptive_intensity(crystalizer, 6U);
    double low_ratio = rms(output, 4096, 4096) / rms(input, 4096, 4096);
    for (size_t i = 0; i < FRAMES; i++)
        input[i] = (i % 251U == 0U) ? 0.8F : 0.02F * sinf((float)(2.0 * PI * 8000.0 * i / RATE));
    fx_crystalizer_process(crystalizer, input, output, FRAMES);
    float adapted = fx_crystalizer_adaptive_intensity(crystalizer, 6U);
    double high_ratio = rms(output, 4096, 4096) / rms(input, 4096, 4096);
    assert(isfinite(low_ratio) && isfinite(high_ratio));
    assert(fabsf(adapted - initial) > 1e-4F);
    fx_crystalizer_reset(crystalizer);
    assert(fabsf(fx_crystalizer_adaptive_intensity(crystalizer, 6U) - 1.0F) < 1e-6F);
    free(input); free(output); fx_crystalizer_destroy(crystalizer);
}

static void test_processing_is_quantum_independent_and_silence_stays_silent(void) {
    fx_crystalizer *whole = fx_crystalizer_create(RATE), *split = fx_crystalizer_create(RATE);
    float *input = calloc(FRAMES, sizeof *input), *a = calloc(FRAMES, sizeof *a), *b = calloc(FRAMES, sizeof *b);
    assert(whole != NULL && split != NULL && input != NULL && a != NULL && b != NULL);
    for (size_t i = 0; i < FRAMES; i++) input[i] = (i % 997U == 0U) ? 0.5F : 0.05F * sinf((float)(2.0 * PI * 4300.0 * i / RATE));
    fx_crystalizer_process(whole, input, a, FRAMES);
    static const size_t quanta[] = {1U, 128U, 17U, 511U, 63U, 256U, 3U};
    size_t quantum = 0U;
    for (size_t offset = 0; offset < FRAMES;) {
        size_t count = quanta[quantum++ % (sizeof quanta / sizeof quanta[0])];
        if (count > FRAMES - offset) count = FRAMES - offset;
        fx_crystalizer_process(split, input + offset, b + offset, count);
        offset += count;
    }
    for (size_t i = 0; i < FRAMES; i++) assert(fabsf(a[i] - b[i]) < 1e-6F);
    fx_crystalizer_reset(split);
    memset(input, 0, FRAMES * sizeof *input);
    fx_crystalizer_process(split, input, b, FRAMES);
    for (size_t i = 0; i < FRAMES; i++) assert(b[i] == 0.0F);
    free(input); free(a); free(b); fx_crystalizer_destroy(whole); fx_crystalizer_destroy(split);
}

int main(void) {
    test_uses_8_2_8_band_configuration();
    test_impulse_has_oversampled_fir_latency();
    test_adaptive_intensity_tracks_program_material_and_resets();
    test_processing_is_quantum_independent_and_silence_stays_silent();
    return 0;
}
