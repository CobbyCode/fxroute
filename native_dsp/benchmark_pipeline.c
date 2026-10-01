#define _POSIX_C_SOURCE 200809L
#include "dsp.h"

#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static double seconds(clockid_t clock) {
    struct timespec time;
    if (clock_gettime(clock, &time)) exit(2);
    return time.tv_sec + time.tv_nsec * 1e-9;
}

static int compare_time(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return (x > y) - (x < y);
}

int main(int argc, char **argv) {
    if (argc != 4 && argc != 5) {
        fprintf(stderr, "usage: %s CONFIG SECONDS QUANTUM [OUTPUT.f32]\n", argv[0]);
        return 2;
    }
    char error[256];
    fxdsp *dsp = fxdsp_load(argv[1], error, sizeof error);
    if (!dsp) { fprintf(stderr, "%s\n", error); return 2; }
    fxdsp_prepare_workers(dsp, NULL);
    const char *timing = getenv("FXROUTE_DSP_TIMING");
    fxdsp_enable_timing(dsp, !timing || strcmp(timing, "0"));
    double duration = strtod(argv[2], NULL);
    size_t quantum = strtoul(argv[3], NULL, 10);
    if (!isfinite(duration) || duration <= 0 || duration > 3600 || !quantum || quantum > 8192) return 2;
    unsigned inputs = fxdsp_inputs(dsp), outputs = fxdsp_outputs(dsp), rate = fxdsp_rate(dsp);
    size_t frames = (size_t)(duration * rate);
    if (!frames) return 2;
    size_t calls = (frames + quantum - 1) / quantum;
    float *input[FXDSP_MAX_CHANNELS], *output[FXDSP_MAX_CHANNELS];
    const float *source[FXDSP_MAX_CHANNELS];
    double *times = calloc(calls, sizeof *times);
    FILE *file = argc == 5 ? fopen(argv[4], "wb") : NULL;
    if (!times || (argc == 5 && !file)) return 2;
    for (unsigned c = 0; c < inputs; c++) {
        input[c] = calloc(frames, sizeof(float));
        if (!input[c]) return 2;
        uint32_t random = 0x12345678U + 0x9e3779b9U * c;
        for (size_t n = 0; n < frames; n++) {
            random = 1664525U * random + 1013904223U;
            input[c][n] = ((float)(random >> 8) / 16777216.0f - 0.5f) * 0.2f;
        }
    }
    for (unsigned c = 0; c < outputs; c++) {
        output[c] = calloc(quantum, sizeof(float));
        if (!output[c]) return 2;
    }
    uint64_t hash = UINT64_C(14695981039346656037);
    double wall = 0, thread = 0, cpu = 0, deadline = (double)quantum / rate;
    size_t misses = 0;
    for (size_t call = 0, offset = 0; offset < frames; call++, offset += quantum) {
        size_t count = frames - offset < quantum ? frames - offset : quantum;
        for (unsigned c = 0; c < inputs; c++) source[c] = input[c] + offset;
        fxdsp_begin_callback(dsp);
        double c0 = seconds(CLOCK_PROCESS_CPUTIME_ID), t0 = seconds(CLOCK_THREAD_CPUTIME_ID);
        double w0 = seconds(CLOCK_MONOTONIC);
        fxdsp_process(dsp, source, output, count);
        times[call] = seconds(CLOCK_MONOTONIC) - w0;
        fxdsp_record_callback(dsp, (uint64_t)(times[call] * 1e9));
        thread += seconds(CLOCK_THREAD_CPUTIME_ID) - t0;
        cpu += seconds(CLOCK_PROCESS_CPUTIME_ID) - c0;
        wall += times[call];
        if (times[call] > (double)count / rate) misses++;
        for (size_t n = 0; n < count; n++) for (unsigned c = 0; c < outputs; c++) {
            if (!isfinite(output[c][n])) { fprintf(stderr, "non-finite output\n"); return 1; }
            unsigned char bytes[sizeof(float)];
            memcpy(bytes, &output[c][n], sizeof bytes);
            for (size_t b = 0; b < sizeof bytes; b++) { hash ^= bytes[b]; hash *= UINT64_C(1099511628211); }
            if (file && fwrite(bytes, 1, sizeof bytes, file) != sizeof bytes) return 2;
        }
    }
    qsort(times, calls, sizeof *times, compare_time);
    printf("{\"rate\":%u,\"inputs\":%u,\"outputs\":%u,\"quantum\":%zu,\"audio_s\":%.6f,"
           "\"wall_s\":%.6f,\"thread_s\":%.6f,\"cpu_s\":%.6f,\"p50_ms\":%.6f,"
           "\"p95_ms\":%.6f,\"p99_ms\":%.6f,\"max_ms\":%.6f,\"deadline_ms\":%.6f,"
            "\"deadline_misses\":%zu,\"hash\":\"%016" PRIx64 "\",\"workers\":%u",
           rate, inputs, outputs, quantum, (double)frames / rate, wall, thread, cpu,
           times[(calls - 1) / 2] * 1000, times[(calls - 1) * 95 / 100] * 1000,
            times[(calls - 1) * 99 / 100] * 1000, times[calls - 1] * 1000, deadline * 1000, misses, hash, fxdsp_worker_count(dsp));
    for (unsigned i = 0; i < FXDSP_TIMING_COUNT; i++) {
        fx_timing_summary t = fxdsp_read_timing(dsp, i);
        printf(",\"%s\":{\"samples\":%" PRIu64 ",\"p99_upper_ms\":%.6f,\"max_ms\":%.6f}",
               fxdsp_timing_name(i), t.samples, t.p99_ns * 1e-6, t.max_ns * 1e-6);
    }
    puts("}");
    if (file && fclose(file)) return 2;
    for (unsigned c = 0; c < inputs; c++) free(input[c]);
    for (unsigned c = 0; c < outputs; c++) free(output[c]);
    free(times); fxdsp_free(dsp);
    return 0;
}
