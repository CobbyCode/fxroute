/* SPDX-License-Identifier: AGPL-3.0-only */
#include "dsp.h"
#include <float.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Linker wrappers catch allocations made below the public processing API. */
static int processing;
static unsigned allocations;
void *__real_malloc(size_t size);
void *__real_calloc(size_t count, size_t size);
void *__real_realloc(void *pointer, size_t size);
void __real_free(void *pointer);
void *__wrap_malloc(size_t size) {
    if (processing) allocations++;
    return __real_malloc(size);
}
void *__wrap_calloc(size_t count, size_t size) {
    if (processing) allocations++;
    return __real_calloc(count, size);
}
void *__wrap_realloc(void *pointer, size_t size) {
    if (processing) allocations++;
    return __real_realloc(pointer, size);
}
void __wrap_free(void *pointer) {
    if (processing) allocations++;
    __real_free(pointer);
}

static void process(fxdsp *d, const float *left, const float *right,
                    float *out_left, float *out_right, size_t frames) {
    const float *input[] = {left, right};
    float *output[] = {out_left, out_right};
    processing = 1;
    fxdsp_process(d, input, output, frames);
    processing = 0;
}

int main(int argc, char **argv) {
    char error[256];
    static float input[16384], output[16384], unaffected[16384];
    static float reference[16384], reference_right[16384];
    const float bad_samples[] = {NAN, INFINITY, -INFINITY, FLT_MAX, -FLT_MAX};
    int failures = 0;
    if (argc != 2 && argc != 3) return 2;
    for (size_t bad = 0; bad < sizeof bad_samples / sizeof bad_samples[0]; bad++) {
        fxdsp *d = fxdsp_load(argv[1], error, sizeof error);
        fxdsp *clean = fxdsp_load(argv[1], error, sizeof error);
        if (!d || !clean) { fprintf(stderr, "load: %s\n", error); return 3; }
        for (unsigned i = 0; i < 16384; i++)
            input[i] = 0.125f * sinf(6.28318530718f * 1000.0f * (float)i / 48000.0f);
        process(d, input, input, output, unaffected, 16384);
        process(clean, input, input, reference, reference_right, 16384);
        if (memcmp(output, reference, sizeof output)) return 4;
        /* The last sample of a callback must not poison later callbacks.
         * The other input/output pair must remain sample-exact throughout. */
        float saved = input[1023];
        input[1023] = bad_samples[bad];
        process(d, input, reference_right, output, unaffected, 1024);
        input[1023] = saved;
        process(clean, input, reference_right, reference, reference_right, 1024);
        for (unsigned i = 0; i < 1024; i++) {
            if (!isfinite(output[i]) || unaffected[i] != reference_right[i]) return 5;
        }
        /* These FIR-shaped SOS fixtures keep the current output finite while
         * only z1 or z2 overflows. Catch the bad history in the same sample,
         * not one or two samples later when it reaches the output. */
        if (argc == 3 && bad >= 3 && output[1023] != 0.0f) {
            fprintf(stderr, "history overflow not suppressed for finite sample[%zu]: %g\n",
                    bad, output[1023]);
            failures++;
        }
        process(d, input, input, output, unaffected, 16384);
        process(clean, input, input, reference, reference_right, 16384);
        double energy = 0.0;
        for (unsigned i = 8192; i < 16384; i++) {
            if (!isfinite(output[i]) || fabsf(output[i] - reference[i]) > 1e-6f ||
                unaffected[i] != reference_right[i]) {
                fprintf(stderr, "sample %u after nonfinite[%zu]: got=%g expected=%g\n",
                        i, bad, output[i], reference[i]);
                failures++;
                break;
            }
            energy += (double)output[i] * output[i];
        }
        if (energy < 0.01) {
            fprintf(stderr, "permanent silence after nonfinite[%zu]\n", bad);
            failures++;
        }
        fxdsp_free(d);
        fxdsp_free(clean);
    }
    if (allocations) { fprintf(stderr, "callback allocations/frees: %u\n", allocations); return 6; }
    if (failures) return 1;
    puts("NaN/Inf/finite-overflow recovery, finite parity, channel isolation, no callback allocations: PASS");
    return 0;
}
