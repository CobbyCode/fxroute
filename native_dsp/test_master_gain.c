#define _POSIX_C_SOURCE 200809L
#include "dsp.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* Master gain stage contract:
 *
 * The canonical listening volume is split off the old loudness wrapper gain:
 * the loudness stage keeps the LSP work point p with the inverse host
 * compensation (-p), and the canonical volume (volumeDb) is applied by a
 * dedicated master_gain stage.  The post_effect meter tap must be written
 * BEFORE the master gain (pre-master tap), so Peak/VU stays independent of
 * the listening volume; the master gain then feeds the protection limiter
 * with exactly the pre-migration level.  Without a master_gain stage the tap
 * falls back to the post-all-stages position.
 */

#define BLOCK 512U

static int failures;

static void check(int condition, const char *message) {
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        failures++;
    }
}

static int nearly(float a, float b) {
    return fabsf(a - b) <= 1e-6f;
}

static fxdsp *load_config(const char *text, char *error, size_t size) {
    char path[] = "/tmp/fxroute-master-gain-XXXXXX";
    int fd = mkstemp(path);
    if (fd < 0) return NULL;
    FILE *file = fdopen(fd, "w");
    if (!file) {
        close(fd);
        unlink(path);
        return NULL;
    }
    fwrite(text, 1, strlen(text), file);
    fclose(file);
    fxdsp *d = fxdsp_load(path, error, size);
    unlink(path);
    return d;
}

static const char *config_with_master =
    "rate 48000\n"
    "inputs 2\n"
    "outputs 2\n"
    "matrix 0 0 1\n"
    "matrix 1 1 1\n"
    "stage_begin 0 pre native headroom\n"
    "param gain_db -6\n"
    "stage_end\n"
    "stage_begin 1 master native master_gain\n"
    "param gain_db -20\n"
    "stage_end\n"
    "stage_begin 2 post native headroom\n"
    "param gain_db -3\n"
    "stage_end\n"
    "output 0 0 0 normal\n"
    "output 1 0 0 normal\n"
    "bypass 0\n";

static const char *config_without_master =
    "rate 48000\n"
    "inputs 2\n"
    "outputs 2\n"
    "matrix 0 0 1\n"
    "matrix 1 1 1\n"
    "stage_begin 0 pre native headroom\n"
    "param gain_db -6\n"
    "stage_end\n"
    "stage_begin 1 post native headroom\n"
    "param gain_db -3\n"
    "stage_end\n"
    "output 0 0 0 normal\n"
    "output 1 0 0 normal\n"
    "bypass 0\n";

static void run_tap_case(const char *config, float expected_tap,
                         float expected_out, const char *label) {
    char error[256];
    fxdsp *d = load_config(config, error, sizeof error);
    check(d != NULL, label);
    if (!d) return;
    float input[2][BLOCK], output[2][BLOCK], taps[2][BLOCK];
    for (unsigned channel = 0; channel < 2; channel++)
        for (size_t n = 0; n < BLOCK; n++) {
            input[channel][n] = 0.25f;
            output[channel][n] = taps[channel][n] = -1.0f;
        }
    const float *in[2] = {input[0], input[1]};
    float *out[2] = {output[0], output[1]};
    float *tap[2] = {taps[0], taps[1]};
    fxdsp_process_tapped(d, in, out, tap, BLOCK);
    for (unsigned channel = 0; channel < 2; channel++)
        for (size_t n = 0; n < BLOCK; n++) {
            check(nearly(taps[channel][n], expected_tap), "tap level");
            check(nearly(output[channel][n], expected_out), "output level");
        }
    if (!nearly(output[0][0], expected_out))
        fprintf(stderr, "%s: output[0]=%.9g expected=%.9g\n",
                label, output[0][0], expected_out);
    fxdsp_free(d);
}

int main(void) {
    float pre = 0.25f * powf(10.0f, -6.0f / 20.0f);
    float post = pre * powf(10.0f, -20.0f / 20.0f) * powf(10.0f, -3.0f / 20.0f);
    float no_master_out = 0.25f * powf(10.0f, -6.0f / 20.0f) * powf(10.0f, -3.0f / 20.0f);
    run_tap_case(config_with_master, pre, post, "master gain tap");
    run_tap_case(config_without_master, no_master_out, no_master_out, "fallback tap position");
    if (failures) {
        fprintf(stderr, "master gain test failed: %d\n", failures);
        return 1;
    }
    printf("master gain tap tests passed\n");
    return 0;
}
