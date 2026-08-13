#define _POSIX_C_SOURCE 200809L
#include "crystalizer.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

#define CHANNELS 4U
#define RATE 48000U
#define SECONDS 10U
#define QUANTUM 128U
#define PI 3.14159265358979323846

static double elapsed_seconds(const struct timespec *begin, const struct timespec *end) {
    return (double)(end->tv_sec - begin->tv_sec) + 1e-9 * (double)(end->tv_nsec - begin->tv_nsec);
}

int main(int argc, char **argv) {
    const double limit = argc > 1 ? strtod(argv[1], NULL) : 5.0;
    const size_t frames = RATE * SECONDS;
    fx_crystalizer *crystalizer[CHANNELS] = {0};
    float input[QUANTUM], output[QUANTUM];
    struct timespec begin, end;
    double checksum = 0.0;

    for (unsigned channel = 0; channel < CHANNELS; channel++) {
        crystalizer[channel] = fx_crystalizer_create(RATE);
        if (!crystalizer[channel]) return 2;
    }
    if (clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &begin)) return 2;
    for (size_t offset = 0; offset < frames; offset += QUANTUM) {
        for (size_t i = 0; i < QUANTUM; i++) {
            size_t n = offset + i;
            input[i] = 0.12F * sinf((float)(2.0 * PI * 997.0 * n / RATE)) +
                       ((n % 4001U) == 0U ? 0.4F : 0.0F);
        }
        for (unsigned channel = 0; channel < CHANNELS; channel++) {
            fx_crystalizer_process(crystalizer[channel], input, output, QUANTUM);
            checksum += output[(offset / QUANTUM + channel) % QUANTUM];
        }
    }
    if (clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &end)) return 2;
    for (unsigned channel = 0; channel < CHANNELS; channel++) fx_crystalizer_destroy(crystalizer[channel]);

    double elapsed = elapsed_seconds(&begin, &end);
    printf("crystalizer: 4 outputs, 48000 Hz, quantum 128, 10 s audio: %.3f s CPU (checksum %.9g)\n",
           elapsed, checksum);
    if (!isfinite(checksum) || elapsed >= limit) {
        fprintf(stderr, "crystalizer benchmark exceeded %.3f s CPU limit\n", limit);
        return 1;
    }
    return 0;
}
