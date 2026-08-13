#define _POSIX_C_SOURCE 200809L
#include "autogain.h"

#include <ebur128.h>
#include <math.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef struct {
    _Atomic uint64_t sequence;
    _Atomic uint64_t gain;
    _Atomic uint64_t reference_lufs;
    _Atomic uint64_t momentary_lufs;
    _Atomic uint64_t shortterm_lufs;
    _Atomic uint64_t integrated_lufs;
    _Atomic uint64_t relative_threshold_lufs;
    _Atomic uint64_t loudness_range_lu;
} atomic_measurement;

struct fx_autogain {
    ebur128_state *state;
    float *ring;
    size_t ring_frames;
    size_t maximum_block_frames;
    _Atomic size_t ring_head;
    _Atomic size_t ring_tail;
    _Atomic int stop;
    pthread_t worker;
    int worker_started;
    uint64_t frames_until_lra;
    unsigned sample_rate;
    fx_autogain_config config;
    atomic_measurement published;
    fx_autogain_measurement rt_measurement;
    double applied_gain;
};

static uint64_t double_bits(double value) {
    uint64_t bits;
    memcpy(&bits, &value, sizeof(bits));
    return bits;
}

static double bits_double(uint64_t bits) {
    double value;
    memcpy(&value, &bits, sizeof(value));
    return value;
}

static void publish_measurement(fx_autogain *autogain,
                                const fx_autogain_measurement *measurement) {
    uint64_t sequence = atomic_load_explicit(&autogain->published.sequence,
                                             memory_order_relaxed);
    atomic_store_explicit(&autogain->published.sequence, sequence + 1U,
                          memory_order_release);
    atomic_store_explicit(&autogain->published.gain, double_bits(measurement->gain),
                          memory_order_relaxed);
    atomic_store_explicit(&autogain->published.reference_lufs,
                          double_bits(measurement->reference_lufs), memory_order_relaxed);
    atomic_store_explicit(&autogain->published.momentary_lufs,
                          double_bits(measurement->momentary_lufs), memory_order_relaxed);
    atomic_store_explicit(&autogain->published.shortterm_lufs,
                          double_bits(measurement->shortterm_lufs), memory_order_relaxed);
    atomic_store_explicit(&autogain->published.integrated_lufs,
                          double_bits(measurement->integrated_lufs), memory_order_relaxed);
    atomic_store_explicit(&autogain->published.relative_threshold_lufs,
                          double_bits(measurement->relative_threshold_lufs),
                          memory_order_relaxed);
    atomic_store_explicit(&autogain->published.loudness_range_lu,
                          double_bits(measurement->loudness_range_lu), memory_order_relaxed);
    atomic_store_explicit(&autogain->published.sequence, sequence + 2U,
                          memory_order_release);
}

static int load_measurement(const fx_autogain *autogain,
                            fx_autogain_measurement *measurement) {
    uint64_t before = atomic_load_explicit(&autogain->published.sequence,
                                           memory_order_acquire);
    if (before & 1U) return 0;
    measurement->gain = bits_double(atomic_load_explicit(&autogain->published.gain,
                                                         memory_order_relaxed));
    measurement->reference_lufs = bits_double(atomic_load_explicit(
        &autogain->published.reference_lufs, memory_order_relaxed));
    measurement->momentary_lufs = bits_double(atomic_load_explicit(
        &autogain->published.momentary_lufs, memory_order_relaxed));
    measurement->shortterm_lufs = bits_double(atomic_load_explicit(
        &autogain->published.shortterm_lufs, memory_order_relaxed));
    measurement->integrated_lufs = bits_double(atomic_load_explicit(
        &autogain->published.integrated_lufs, memory_order_relaxed));
    measurement->relative_threshold_lufs = bits_double(atomic_load_explicit(
        &autogain->published.relative_threshold_lufs, memory_order_relaxed));
    measurement->loudness_range_lu = bits_double(atomic_load_explicit(
        &autogain->published.loudness_range_lu, memory_order_relaxed));
    return before == atomic_load_explicit(&autogain->published.sequence,
                                          memory_order_acquire);
}

fx_autogain_config fx_autogain_default_config(void) {
    fx_autogain_config config = {
        FX_AUTOGAIN_GEOMETRIC_MEAN_MSI,
        -23.0,
        -70.0,
        15U
    };
    return config;
}

static double geometric_pair(double first, double second) {
    double result = sqrt(fabs(first * second));
    return first < 0.0 && second < 0.0 ? -result : result;
}

static double reference_loudness(const fx_autogain *autogain,
                                 const fx_autogain_measurement *measurement) {
    switch (autogain->config.reference) {
        case FX_AUTOGAIN_MOMENTARY: return measurement->momentary_lufs;
        case FX_AUTOGAIN_SHORTTERM: return measurement->shortterm_lufs;
        case FX_AUTOGAIN_INTEGRATED: return measurement->integrated_lufs;
        case FX_AUTOGAIN_GEOMETRIC_MEAN_MSI:
            return cbrt(measurement->momentary_lufs * measurement->shortterm_lufs *
                        measurement->integrated_lufs);
        case FX_AUTOGAIN_GEOMETRIC_MEAN_MS:
            return geometric_pair(measurement->momentary_lufs, measurement->shortterm_lufs);
        case FX_AUTOGAIN_GEOMETRIC_MEAN_MI:
            return geometric_pair(measurement->momentary_lufs, measurement->integrated_lufs);
        case FX_AUTOGAIN_GEOMETRIC_MEAN_SI:
            return geometric_pair(measurement->shortterm_lufs, measurement->integrated_lufs);
    }
    return 0.0;
}

static void measure_frames(fx_autogain *autogain, const float *frames, size_t count,
                           fx_autogain_measurement *measurement) {
    int failed = ebur128_add_frames_float(autogain->state, frames, count) != EBUR128_SUCCESS;
    if (ebur128_loudness_momentary(autogain->state, &measurement->momentary_lufs) !=
        EBUR128_SUCCESS) failed = 1;
    if (ebur128_loudness_shortterm(autogain->state, &measurement->shortterm_lufs) !=
        EBUR128_SUCCESS) failed = 1;
    if (ebur128_loudness_global(autogain->state, &measurement->integrated_lufs) !=
        EBUR128_SUCCESS) failed = 1;

    if (!isfinite(measurement->momentary_lufs)) measurement->momentary_lufs = 0.0;
    if (measurement->shortterm_lufs > 10.0 || !isfinite(measurement->shortterm_lufs))
        measurement->shortterm_lufs = measurement->momentary_lufs;
    if (measurement->integrated_lufs > 10.0 || !isfinite(measurement->integrated_lufs))
        measurement->integrated_lufs = measurement->momentary_lufs;
    if (ebur128_relative_threshold(autogain->state,
                                   &measurement->relative_threshold_lufs) !=
        EBUR128_SUCCESS) failed = 1;

    if ((uint64_t)count >= autogain->frames_until_lra) {
        uint64_t excess = (uint64_t)count - autogain->frames_until_lra;
        autogain->frames_until_lra = autogain->sample_rate - excess % autogain->sample_rate;
        if (ebur128_loudness_range(autogain->state, &measurement->loudness_range_lu) !=
            EBUR128_SUCCESS) failed = 1;
    } else {
        autogain->frames_until_lra -= count;
    }

    if (measurement->momentary_lufs > autogain->config.silence_threshold_lufs && !failed) {
        double peak_left = 0.0, peak_right = 0.0;
        if (ebur128_prev_sample_peak(autogain->state, 0U, &peak_left) == EBUR128_SUCCESS &&
            ebur128_prev_sample_peak(autogain->state, 1U, &peak_right) == EBUR128_SUCCESS) {
            double loudness = reference_loudness(autogain, measurement);
            double gain = exp(((autogain->config.target_lufs - loudness) / 20.0) * log(10.0));
            double peak = peak_left > peak_right ? peak_left : peak_right;
            measurement->reference_lufs = loudness;
            if (peak > 0.00001 && gain * peak < 1.0) measurement->gain = gain;
        }
    }
    publish_measurement(autogain, measurement);
}

static void *measurement_worker(void *argument) {
    fx_autogain *autogain = argument;
    fx_autogain_measurement measurement = {0};
    const struct timespec idle = {0, 1000000L};
    measurement.gain = 1.0;

    for (;;) {
        size_t tail = atomic_load_explicit(&autogain->ring_tail, memory_order_relaxed);
        size_t head = atomic_load_explicit(&autogain->ring_head, memory_order_acquire);
        if (tail == head) {
            if (atomic_load_explicit(&autogain->stop, memory_order_acquire)) break;
            nanosleep(&idle, NULL);
            continue;
        }
        size_t index = tail % autogain->ring_frames;
        size_t count = head - tail;
        size_t contiguous = autogain->ring_frames - index;
        if (count > autogain->maximum_block_frames) count = autogain->maximum_block_frames;
        if (count > contiguous) count = contiguous;
        measure_frames(autogain, &autogain->ring[index * 2U], count, &measurement);
        atomic_store_explicit(&autogain->ring_tail, tail + count, memory_order_release);
    }
    return NULL;
}

fx_autogain *fx_autogain_init(unsigned sample_rate, size_t maximum_block_frames,
                              const fx_autogain_config *config) {
    fx_autogain_config selected = config ? *config : fx_autogain_default_config();
    if (!sample_rate || !maximum_block_frames ||
        selected.reference < FX_AUTOGAIN_MOMENTARY ||
        selected.reference > FX_AUTOGAIN_GEOMETRIC_MEAN_SI ||
        !isfinite(selected.target_lufs) || selected.target_lufs < -100.0 ||
        selected.target_lufs > 0.0 || !isfinite(selected.silence_threshold_lufs) ||
        selected.silence_threshold_lufs < -100.0 || selected.silence_threshold_lufs > 0.0 ||
        selected.maximum_history_seconds < 6U || selected.maximum_history_seconds > 3600U ||
        maximum_block_frames > SIZE_MAX / 2U ||
        (size_t)sample_rate > SIZE_MAX - maximum_block_frames * 2U) return NULL;

    fx_autogain *autogain = calloc(1, sizeof(*autogain));
    if (!autogain) return NULL;
    autogain->ring_frames = sample_rate;
    if (autogain->ring_frames < maximum_block_frames * 2U)
        autogain->ring_frames = maximum_block_frames * 2U;
    if (autogain->ring_frames > SIZE_MAX / (2U * sizeof(*autogain->ring))) {
        free(autogain);
        return NULL;
    }
    autogain->ring = malloc(autogain->ring_frames * 2U * sizeof(*autogain->ring));
    autogain->state = ebur128_init(2U, sample_rate,
                                   EBUR128_MODE_S | EBUR128_MODE_I | EBUR128_MODE_LRA |
                                   EBUR128_MODE_SAMPLE_PEAK);
    if (!autogain->ring || !autogain->state ||
        ebur128_set_channel(autogain->state, 0U, EBUR128_LEFT) != EBUR128_SUCCESS ||
        ebur128_set_channel(autogain->state, 1U, EBUR128_RIGHT) != EBUR128_SUCCESS ||
        ebur128_set_max_history(autogain->state,
                               (unsigned long)selected.maximum_history_seconds * 1000UL) !=
            EBUR128_SUCCESS ||
        !atomic_is_lock_free(&autogain->ring_head) ||
        !atomic_is_lock_free(&autogain->published.gain)) {
        fx_autogain_free(autogain);
        return NULL;
    }
    autogain->maximum_block_frames = maximum_block_frames;
    autogain->frames_until_lra = (uint64_t)sample_rate * 3U;
    autogain->sample_rate = sample_rate;
    autogain->config = selected;
    autogain->rt_measurement.gain = 1.0;
    autogain->applied_gain = 1.0;
    publish_measurement(autogain, &autogain->rt_measurement);
    if (pthread_create(&autogain->worker, NULL, measurement_worker, autogain)) {
        fx_autogain_free(autogain);
        return NULL;
    }
    autogain->worker_started = 1;
    return autogain;
}

void fx_autogain_free(fx_autogain *autogain) {
    if (!autogain) return;
    if (autogain->worker_started) {
        atomic_store_explicit(&autogain->stop, 1, memory_order_release);
        pthread_join(autogain->worker, NULL);
    }
    if (autogain->state) ebur128_destroy(&autogain->state);
    free(autogain->ring);
    free(autogain);
}

int fx_autogain_process(fx_autogain *autogain, const float *left, const float *right,
                        float *out_left, float *out_right, size_t frames) {
    if (!autogain || !left || !right || !out_left || !out_right ||
        frames > autogain->maximum_block_frames) return -1;

    size_t head = atomic_load_explicit(&autogain->ring_head, memory_order_relaxed);
    size_t tail = atomic_load_explicit(&autogain->ring_tail, memory_order_acquire);
    size_t available = autogain->ring_frames - (head - tail);
    /* Preserve queued history on overrun; only newest measurement frames are dropped. */
    size_t accepted = frames < available ? frames : available;
    for (size_t i = 0; i < accepted; i++) {
        size_t index = (head + i) % autogain->ring_frames;
        autogain->ring[index * 2U] = left[i];
        autogain->ring[index * 2U + 1U] = right[i];
    }
    atomic_store_explicit(&autogain->ring_head, head + accepted, memory_order_release);

    fx_autogain_measurement latest;
    for (unsigned attempt = 0; attempt < 3U; attempt++) {
        if (load_measurement(autogain, &latest)) {
            autogain->rt_measurement = latest;
            break;
        }
    }
    double peak = 0.0;
    for (size_t i = 0; i < frames; i++) {
        double left_peak = fabs((double)left[i]);
        double right_peak = fabs((double)right[i]);
        if (left_peak > peak) peak = left_peak;
        if (right_peak > peak) peak = right_peak;
    }
    double target_gain = autogain->rt_measurement.gain;
    double seconds = (double)frames / autogain->sample_rate;
    double time_constant = target_gain < autogain->applied_gain ? 0.1 : 0.4;
    double alpha = exp(-seconds / time_constant);
    autogain->applied_gain = alpha * autogain->applied_gain + (1.0 - alpha) * target_gain;
    double applied_gain = autogain->applied_gain;
    if (peak > 0.0 && applied_gain * peak >= 1.0)
        applied_gain = 0.9999999403953552 / peak;
    float gain = (float)applied_gain;
    for (size_t i = 0; i < frames; i++) {
        out_left[i] = left[i] * gain;
        out_right[i] = right[i] * gain;
    }
    return 0;
}

fx_autogain_measurement fx_autogain_get_measurement(const fx_autogain *autogain) {
    fx_autogain_measurement measurement = {0};
    if (!autogain) return measurement;
    while (!load_measurement(autogain, &measurement)) {}
    return measurement;
}
