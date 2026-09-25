#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Regression tests for native DSP safety bounds (PEQ gain, stage delay,
output delay, crystalizer intensity).

Extreme finite values must be rejected before state mutation on both the
config and the live path, and a rejected value must leave the previous
valid state fully functional. Stage/output delays must share the same
limits on the config and live paths, matching the real buffer capacity.
"""
import shlex
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native_dsp"


def test_native_dsp_safety_bounds_hold_and_state_survives_rejection(tmp_path):
    harness = tmp_path / "safety_test.c"
    binary = tmp_path / "safety_test"
    harness.write_text(
        r'''
#define _POSIX_C_SOURCE 200809L
#include "crystalizer.h"
#include "dsp.h"
#include <math.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static fxdsp *d;
static float silence_in[2][1024], silence_out[4][1024];

static void *commit_helper(void *unused) {
    struct timespec delay = {.tv_sec = 0, .tv_nsec = 10000000L};
    const float *input[] = {silence_in[0], silence_in[1]};
    float *output[] = {silence_out[0], silence_out[1], silence_out[2], silence_out[3]};
    (void)unused;
    nanosleep(&delay, NULL);
    fxdsp_process(d, input, output, 1024);
    return NULL;
}

static int commit_live(void) {
    pthread_t thread;
    if (pthread_create(&thread, NULL, commit_helper, NULL)) return -1;
    int ok = fxdsp_live_commit(d);
    pthread_join(thread, NULL);
    return ok;
}

static void write_config(const char *dir, const char *name, const char *text) {
    char path[1024];
    snprintf(path, sizeof path, "%s/%s", dir, name);
    FILE *file = fopen(path, "w");
    if (!file) { fprintf(stderr, "cannot write %s\n", path); exit(90); }
    fputs(text, file);
    fclose(file);
}

static fxdsp *expect_load(const char *dir, const char *name, int code) {
    char path[1024], error[256];
    snprintf(path, sizeof path, "%s/%s", dir, name);
    fxdsp *loaded = fxdsp_load(path, error, sizeof error);
    if (code && !loaded) { fprintf(stderr, "config %s rejected: %s\n", name, error); exit(code); }
    if (!code && loaded) { fprintf(stderr, "config %s unexpectedly accepted\n", name); fxdsp_free(loaded); exit(91); }
    return loaded;
}

static double sine_gain(float frequency, float gain_db_unused, int use_live_peq, float live_gain) {
    /* Steady-state RMS ratio of a 1kHz sine through output 0. When
     * use_live_peq is set, a live PEQ update is committed first. */
    static float in[48000], out[48000];
    const float *input[] = {in};
    float *output[] = {out};
    (void)gain_db_unused;
    if (use_live_peq) {
        if (!fxdsp_live_begin(d)) return -1.0;
        if (!fxdsp_live_peq(d, 0, 0, "bell", 1000.0f, 1.0f, live_gain)) return -2.0;
        if (!commit_live()) return -3.0;
    }
    for (unsigned i = 0; i < 48000; i++) in[i] = 0.5f * sinf(2.0f * 3.14159265358979323846f * frequency * (float)i / 48000.0f);
    fxdsp_process(d, input, output, 48000);
    double square = 0;
    for (unsigned i = 24000; i < 48000; i++) {
        if (!isfinite(out[i])) return -4.0;
        square += (double)out[i] * out[i];
    }
    return sqrt(square / 24000) / (0.5 / sqrt(2.0));
}

static int impulse_peak(float *out, size_t frames, size_t *peak_at) {
    static float in[26000];
    const float *input[] = {in, in};
    float *output[] = {out, out + 26000};
    double best = -1.0;
    size_t best_at = 0;
    if (frames > 26000) return -1;
    memset(in, 0, sizeof in);
    memset(out, 0, 2 * 26000 * sizeof *out);
    in[0] = 1.0f;
    fxdsp_process(d, input, output, frames);
    for (size_t i = 0; i < frames; i++) {
        if (!isfinite(out[i])) return -2;
        if (fabs(out[i]) > best) { best = fabs(out[i]); best_at = i; }
    }
    *peak_at = best_at;
    return best > 0.5 ? 0 : -3;
}

int main(int argc, char **argv) {
    const char *dir = argv[1];
    double gain;
    size_t peak;
    static float delay_out[52000];
    (void)argc;

    /* 1. PEQ gain: extreme config values are rejected. */
    write_config(dir, "peq-ok.conf",
        "rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\npeq 0 bell 1000 1 6\n");
    write_config(dir, "peq-huge.conf",
        "rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\npeq 0 bell 1000 1 1e30\n");
    write_config(dir, "peq-inf.conf",
        "rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\npeq 0 bell 1000 1 inf\n");
    d = expect_load(dir, "peq-ok.conf", 11);
    fxdsp_free(d); d = NULL;
    expect_load(dir, "peq-huge.conf", 0);
    expect_load(dir, "peq-inf.conf", 0);

    /* 1b. PEQ gain: rejected live values leave the valid state working. */
    d = expect_load(dir, "peq-ok.conf", 12);
    gain = sine_gain(1000.0f, 0, 0, 0);
    if (fabs(gain - 2.0) > 0.1) { fprintf(stderr, "peq +6dB gain=%g\n", gain); return 13; }
    if (!fxdsp_live_begin(d)) return 14;
    if (fxdsp_live_peq(d, 0, 0, "bell", 1000.0f, 1.0f, 1e30f)) return 15;
    if (fxdsp_live_peq(d, 0, 0, "bell", 1000.0f, 1.0f, INFINITY)) return 16;
    if (!fxdsp_live_abort(d)) return 17;
    gain = sine_gain(1000.0f, 0, 0, 0);
    if (fabs(gain - 2.0) > 0.1) { fprintf(stderr, "peq state poisoned, gain=%g\n", gain); return 18; }
    gain = sine_gain(1000.0f, 0, 1, 0.0f);
    if (fabs(gain - 1.0) > 0.05) { fprintf(stderr, "peq live update dead, gain=%g\n", gain); return 19; }
    fxdsp_free(d); d = NULL;

    /* 2. Stage delay: boundary accepted, negative/over-capacity rejected. */
    write_config(dir, "delay-ok.conf",
        "rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n"
        "stage_begin 0 dly native delay\nparam left_ms 500\nparam right_ms 500\nstage_end\n");
    write_config(dir, "delay-over.conf",
        "rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n"
        "stage_begin 0 dly native delay\nparam left_ms 600\nparam right_ms 0\nstage_end\n");
    write_config(dir, "delay-neg.conf",
        "rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n"
        "stage_begin 0 dly native delay\nparam left_ms -1\nparam right_ms 0\nstage_end\n");
    write_config(dir, "pdelay-ok.conf",
        "rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n"
        "stage_begin 0 g-delay native delay\nparam left_ms 10000\nparam right_ms 10000\nstage_end\n");
    write_config(dir, "pdelay-over.conf",
        "rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n"
        "stage_begin 0 g-delay native delay\nparam left_ms 10001\nparam right_ms 0\nstage_end\n");
    d = expect_load(dir, "delay-ok.conf", 21);
    fxdsp_free(d); d = NULL;
    expect_load(dir, "delay-over.conf", 0);
    expect_load(dir, "delay-neg.conf", 0);
    d = expect_load(dir, "pdelay-ok.conf", 22);
    fxdsp_free(d); d = NULL;
    expect_load(dir, "pdelay-over.conf", 0);

    /* 2b. Stage delay: live path shares the config limits and stays live. */
    write_config(dir, "delay-live.conf",
        "rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n"
        "stage_begin 0 dly native delay\nparam left_ms 0\nparam right_ms 0\nstage_end\n");
    d = expect_load(dir, "delay-live.conf", 23);
    if (!fxdsp_live_begin(d) || !fxdsp_live_param(d, "dly", "left_ms", 500.0f) ||
        !fxdsp_live_param(d, "dly", "right_ms", 500.0f) || !commit_live()) return 24;
    if (impulse_peak(delay_out, 25000, &peak) || peak != 24000) { fprintf(stderr, "delay 500ms peak=%zu\n", peak); return 25; }
    if (!fxdsp_live_begin(d)) return 26;
    if (fxdsp_live_param(d, "dly", "left_ms", -5.0f)) return 27;
    if (fxdsp_live_param(d, "dly", "left_ms", 600.0f)) return 28;
    if (!fxdsp_live_abort(d)) return 29;
    if (impulse_peak(delay_out, 25000, &peak) || peak != 24000) { fprintf(stderr, "delay state moved, peak=%zu\n", peak); return 30; }
    if (!fxdsp_live_begin(d) || !fxdsp_live_param(d, "dly", "left_ms", 10.0f) ||
        !fxdsp_live_param(d, "dly", "right_ms", 10.0f) || !commit_live()) return 31;
    if (impulse_peak(delay_out, 25000, &peak) || peak != 480) { fprintf(stderr, "delay 10ms peak=%zu\n", peak); return 32; }
    fxdsp_free(d); d = NULL;
    write_config(dir, "pdelay-live.conf",
        "rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n"
        "stage_begin 0 g-delay native delay\nparam left_ms 0\nparam right_ms 0\nstage_end\n");
    d = expect_load(dir, "pdelay-live.conf", 33);
    if (!fxdsp_live_begin(d) || !fxdsp_live_param(d, "g-delay", "left_ms", 10000.0f) ||
        !fxdsp_live_abort(d)) return 34;
    if (!fxdsp_live_begin(d)) return 35;
    if (fxdsp_live_param(d, "g-delay", "left_ms", 10001.0f)) return 36;
    if (!fxdsp_live_abort(d)) return 37;
    fxdsp_free(d); d = NULL;

    /* 3. Output delay: boundary accepted, over-capacity rejected, live parity. */
    write_config(dir, "odelay-ok.conf",
        "rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\noutput 0 0 500 normal\n");
    write_config(dir, "odelay-over.conf",
        "rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\noutput 0 0 600 normal\n");
    write_config(dir, "odelay-10.conf",
        "rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\noutput 0 0 10 normal\n");
    d = expect_load(dir, "odelay-ok.conf", 41);
    fxdsp_free(d); d = NULL;
    expect_load(dir, "odelay-over.conf", 0);
    d = expect_load(dir, "odelay-10.conf", 42);
    if (impulse_peak(delay_out, 2000, &peak) || peak != 480) { fprintf(stderr, "output delay 10ms peak=%zu\n", peak); return 43; }
    if (!fxdsp_live_begin(d)) return 44;
    if (!fxdsp_live_output(d, 0, 0.0f, 500.0f, 0)) return 45;
    if (!fxdsp_live_abort(d)) return 46;
    if (!fxdsp_live_begin(d)) return 47;
    if (fxdsp_live_output(d, 0, 0.0f, 501.0f, 0)) return 48;
    if (fxdsp_live_output(d, 0, 0.0f, -1.0f, 0)) return 49;
    if (!fxdsp_live_abort(d)) return 50;
    fxdsp_free(d); d = NULL;

    /* 4. Crystalizer: extreme values rejected without NaN/Inf state. */
    {
        fx_crystalizer *c = fx_crystalizer_create(48000);
        float before;
        static float cin[8192], cout[8192];
        if (!c) return 61;
        fx_crystalizer_set_band_intensity_db(c, 2U, -2.0f);
        before = fx_crystalizer_base_intensity(c, 2U);
        if (!isfinite(before)) return 62;
        fx_crystalizer_set_band_intensity_db(c, 2U, 1e30f);
        fx_crystalizer_set_band_intensity_db(c, 2U, INFINITY);
        if (fx_crystalizer_base_intensity(c, 2U) != before ||
            fx_crystalizer_adaptive_intensity(c, 2U) != before) return 63;
        fx_crystalizer_set_band_intensity_db(c, 2U, -6.0f);
        if (fabsf(fx_crystalizer_base_intensity(c, 2U) - powf(10.0f, -6.0f / 20.0f)) > 1e-6f) return 64;
        for (unsigned i = 0; i < 8192; i++) cin[i] = 0.1f * sinf(2.0f * 3.14159265358979323846f * 1500.0f * (float)i / 48000.0f);
        fx_crystalizer_process(c, cin, cout, 8192);
        for (unsigned i = 0; i < 8192; i++) if (!isfinite(cout[i])) return 65;
        fx_crystalizer_destroy(c);
    }
    write_config(dir, "cryst-ok.conf",
        "rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n"
        "stage_begin 0 crystal native crystalizer\nparam intensity_band2_db -2\nstage_end\n");
    write_config(dir, "cryst-huge.conf",
        "rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n"
        "stage_begin 0 crystal native crystalizer\nparam intensity_band2_db 1e30\nstage_end\n");
    d = expect_load(dir, "cryst-ok.conf", 66);
    fxdsp_free(d); d = NULL;
    expect_load(dir, "cryst-huge.conf", 0);
    d = expect_load(dir, "cryst-ok.conf", 67);
    if (!fxdsp_live_begin(d)) return 68;
    if (fxdsp_live_param(d, "crystal", "intensity_band2_db", 1e30f)) return 69;
    if (!fxdsp_live_abort(d)) return 70;
    {
        static float in[8192], l[8192], r[8192];
        const float *input[] = {in, in};
        float *output[] = {l, r};
        for (unsigned i = 0; i < 8192; i++) in[i] = 0.1f * sinf(2.0f * 3.14159265358979323846f * 1500.0f * (float)i / 48000.0f);
        fxdsp_process(d, input, output, 8192);
        for (unsigned i = 0; i < 8192; i++) if (!isfinite(l[i]) || !isfinite(r[i])) return 71;
    }
    if (!fxdsp_live_begin(d) || !fxdsp_live_param(d, "crystal", "intensity_band2_db", -6.0f) ||
        !commit_live()) return 72;
    {
        static float in[8192], l[8192], r[8192];
        const float *input[] = {in, in};
        float *output[] = {l, r};
        for (unsigned i = 0; i < 8192; i++) in[i] = 0.1f * sinf(2.0f * 3.14159265358979323846f * 1500.0f * (float)i / 48000.0f);
        fxdsp_process(d, input, output, 8192);
        for (unsigned i = 0; i < 8192; i++) if (!isfinite(l[i]) || !isfinite(r[i])) return 73;
    }
    fxdsp_free(d); d = NULL;
    return 0;
}
'''
    )
    flags = shlex.split(subprocess.check_output(
        ["pkg-config", "--cflags", "--libs", "libebur128", "lilv-0", "samplerate",
         "speexdsp"], text=True))
    subprocess.run([
        "cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-pedantic",
        "-I", str(NATIVE), str(NATIVE / "dsp.c"), str(NATIVE / "autogain.c"),
        str(NATIVE / "crystalizer.c"), str(NATIVE / "lv2_host.c"), str(harness),
        *flags, "-lm", "-pthread", "-o", str(binary),
    ], check=True)
    subprocess.run([str(binary), str(tmp_path)], check=True)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="fxroute-safety-") as directory:
        test_native_dsp_safety_bounds_hold_and_state_survives_rejection(Path(directory))
    print("native DSP safety tests passed")
