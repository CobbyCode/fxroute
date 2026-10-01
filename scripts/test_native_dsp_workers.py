#!/usr/bin/env python3
"""Exercise graph-owned workers, fallback, histories and timing."""
import array
from pathlib import Path
import subprocess
import struct
import sys

from native_test_runner import run_pytest_style_module

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native_dsp"


def test_graph_workers_preserve_pcm_taps_controls_and_independent_graphs(tmp_path):
    # Catches scratch/state sharing, bank skips, reordered audio, stale live
    # updates, changes to unlinked-output histories and premature reclamation.
    from test_native_dsp_kernel_parity import compile_native
    payload = array.array("f", [0.4 if n == 0 else (0.0001 if n % 97 == 0 else 0) for n in range(4097)]).tobytes()
    ir = tmp_path / "ir.wav"
    ir.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(payload)) + b"WAVEfmt " +
                  struct.pack("<IHHIIHH", 16, 3, 1, 48000, 192000, 4, 32) + b"data" + struct.pack("<I", len(payload)) + payload)
    harness = tmp_path / "graphs.c"
    harness.write_text(r'''
#define _POSIX_C_SOURCE 200809L
#include "dsp.h"
#include <assert.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
enum { FRAMES = 32768 };
struct stream { fxdsp *d; unsigned channels; size_t quantum; float *pcm, *tap; };
static void *process(void *context) {
    struct stream *s = context;
    float in[8][8192], out[8][8192], tap[2][8192];
    const float *inputs[8]; float *outputs[8], *taps[] = {tap[0], tap[1]};
    for (unsigned c = 0; c < s->channels; c++) { inputs[c] = in[c]; outputs[c] = out[c]; }
    outputs[s->channels - 1] = NULL;
    for (size_t offset = 0; offset < FRAMES; offset += s->quantum) {
        size_t count = FRAMES - offset < s->quantum ? FRAMES - offset : s->quantum;
        outputs[s->channels - 1] = offset >= FRAMES / 2 ? out[s->channels - 1] : NULL;
        for (unsigned c = 0; c < s->channels; c++) for (size_t n = 0; n < count; n++) {
            size_t k = n + offset;
            in[c][n] = k >= 8192 && k < 24576 ? 0 : ((int)((k * 163 + c * 199) % 1021) - 510) * 0.0001f;
        }
        fxdsp_set_effect_bypass(s->d, offset >= 24576);
        fxdsp_set_mute(s->d, 2, offset >= 24576);
        fxdsp_process_tapped(s->d, inputs, outputs, taps, count);
        for (unsigned c = 0; c < s->channels; c++)
            if (outputs[c]) memcpy(s->pcm + c * FRAMES + offset, out[c], count * sizeof(float));
        for (unsigned c = 0; c < 2; c++) memcpy(s->tap + c * FRAMES + offset, tap[c], count * sizeof(float));
    }
    return NULL;
}
int main(int argc, char **argv) {
    (void)argc; char error[256];
    for (unsigned cycle = 0; cycle < 5; cycle++) {
        struct stream a = {0}, b = {0}, c = {0};
        a.d = fxdsp_load(argv[1], error, sizeof error); b.d = fxdsp_load(argv[1], error, sizeof error);
        c.d = fxdsp_load(argv[1], error, sizeof error);
        assert(a.d && b.d && c.d);
        fx_worker_policy disabled = {0};
        assert(!setenv("FXROUTE_DSP_WORKERS", "3", 1));
        assert(fxdsp_prepare_workers(a.d, &disabled) == 0);
        assert(!unsetenv("FXROUTE_DSP_WORKERS"));
        fx_worker_policy policy = {.count = 3};
        assert(fxdsp_prepare_workers(b.d, &policy) > 0);
        assert(fxdsp_prepare_workers(c.d, &policy) > 0);
        assert(fxdsp_worker_count(a.d) == 0);
        a.channels = b.channels = c.channels = fxdsp_inputs(a.d);
        static const size_t quanta[] = {17, 128, 511, 1024, 8192};
        a.quantum = b.quantum = c.quantum = quanta[cycle];
        a.pcm = calloc(a.channels * FRAMES, sizeof(float)); b.pcm = calloc(a.channels * FRAMES, sizeof(float));
        a.tap = calloc(2 * FRAMES, sizeof(float)); b.tap = calloc(2 * FRAMES, sizeof(float));
        c.pcm = calloc(c.channels * FRAMES, sizeof(float)); c.tap = calloc(2 * FRAMES, sizeof(float));
        assert(a.pcm && b.pcm && c.pcm && a.tap && b.tap && c.tap);
        for (unsigned g = 0; g < 3; g++) {
            fxdsp *d = g == 2 ? c.d : g ? b.d : a.d;
            assert(fxdsp_live_begin(d));
            assert(fxdsp_live_param(d, "crystal", "intensity_band2_db", -3));
            assert(fxdsp_live_matrix(d, 0, 0, 0.75f));
            assert(fxdsp_live_output(d, 0, -3, 0.125f, 1));
            assert(fxdsp_live_peq(d, 0, 0, "bell", 500, 0.7f, -2));
            assert(!fxdsp_live_commit(d)); /* stopped graph: pending transaction */
        }
        pthread_t other, third;
        assert(!pthread_create(&other, NULL, process, &b));
        assert(!pthread_create(&third, NULL, process, &c));
        process(&a); pthread_join(other, NULL); pthread_join(third, NULL);
        assert(!memcmp(a.pcm, b.pcm, a.channels * FRAMES * sizeof(float)));
        assert(!memcmp(a.tap, b.tap, 2 * FRAMES * sizeof(float)));
        assert(!memcmp(a.pcm, c.pcm, a.channels * FRAMES * sizeof(float)));
        assert(!memcmp(a.tap, c.tap, 2 * FRAMES * sizeof(float)));
        for (unsigned c = 0; c < a.channels; c++) {
            float ap, ar, bp, br; fxdsp_meter(a.d, c, &ap, &ar); fxdsp_meter(b.d, c, &bp, &br);
            assert(ap == bp && ar == br);
        }
        fxdsp_free(a.d); fxdsp_free(b.d); free(a.pcm); free(b.pcm); free(a.tap); free(b.tap);
        fxdsp_free(c.d); free(c.pcm); free(c.tap);
    }
    return 0;
}
''')
    target = tmp_path / "graphs"
    compile_native(target, [NATIVE / name for name in ("dsp.c", "worker_pool.c", "autogain.c", "crystalizer.c", "lv2_host.c")] + [harness])
    for channels in (2, 6, 8):
        lines = ["rate 48000", f"inputs {channels}", f"outputs {channels}"]
        lines += [f"matrix {c} {c} 0.9" for c in range(channels)] + ["matrix 0 1 0.1"]
        lines += [f"peq {c} bell 1000 0.7 1" for c in range(channels)]
        lines += [f'oconv {c} 0 0 -100 0 0 "{ir}"' for c in range(channels)]
        lines += ["stage_begin 0 conv native convolver", f'param path "{ir}"', "stage_end",
                  "stage_begin 1 crystal native crystalizer", "stage_end",
                  # Freeze the existing asynchronous gain estimator for byte
                  # parity; its publication phase is wall-clock dependent.
                  "stage_begin 2 level native autogain", "param silence_threshold_db 0", "stage_end",
                  "stage_begin 3 delay native delay", "param left_ms 0.125", "param right_ms 0.25", "stage_end"]
        lines += [f"output {c} -2 {c / 8} normal" for c in range(channels)]
        config = tmp_path / "graph.conf"
        config.write_text("\n".join(lines) + "\n")
        subprocess.run([str(target), str(config)], check=True, timeout=180)


def test_timing_reset_is_acknowledged_at_a_drained_callback_boundary(tmp_path):
    # Catches reset erasing maxima of retained samples and acknowledges only
    # a completed reset, even when the graph initially has no callbacks.
    from test_native_dsp_kernel_parity import compile_native
    harness = tmp_path / "timing-reset.c"
    harness.write_text(r'''
#define _POSIX_C_SOURCE 200809L
#include "dsp.h"
#include <assert.h>
#include <pthread.h>
#include <stdatomic.h>
#include <time.h>
static fxdsp *d;
static _Atomic int stop;
static void *process(void *unused) {
    (void)unused;
    while (!atomic_load(&stop)) {
        fxdsp_begin_callback(d);
        fxdsp_record_callback(d, 456000);
        struct timespec delay = {.tv_nsec=1000000}; nanosleep(&delay, NULL);
    }
    return NULL;
}
int main(int argc, char **argv) {
    (void)argc; char error[256];
    d = fxdsp_load(argv[1], error, sizeof error); assert(d);
    assert(fxdsp_enable_timing(d, 1));
    fxdsp_record_callback(d, 10000000);
    assert(!fxdsp_reset_timing(d));
    fxdsp_begin_callback(d);
    assert(fxdsp_read_timing(d, FXDSP_TIMING_CALLBACK).samples == 0);
    pthread_t worker; assert(!pthread_create(&worker, NULL, process, NULL));
    for (unsigned i = 0; i < 100; i++) {
        assert(fxdsp_reset_timing(d));
        fx_timing_summary summary = fxdsp_read_timing(d, FXDSP_TIMING_CALLBACK);
        if (summary.samples) assert(summary.max_ns == 456000 && summary.p99_ns == 456000);
    }
    atomic_store(&stop, 1); pthread_join(worker, NULL);
    fxdsp_free(d);
    return 0;
}
''')
    target = tmp_path / "timing-reset"
    compile_native(target, [NATIVE / name for name in ("dsp.c", "autogain.c", "crystalizer.c", "lv2_host.c")] + [harness])
    config = tmp_path / "neutral.conf"
    config.write_text("rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n")
    subprocess.run([str(target), str(config)], check=True, timeout=10)


if __name__ == "__main__":
    sys.exit(run_pytest_style_module(globals()))
