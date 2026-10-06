#!/usr/bin/env python3
"""Exercise borrowed worker pools, serial fallback, histories and timing."""
import array
from pathlib import Path
import subprocess
import struct
import sys

from native_test_runner import run_pytest_style_module

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native_dsp"


def test_borrowed_pool_matches_serial_and_carries_no_graph_state(tmp_path):
    # One long-lived pool serves every graph, the way the live engine owns it:
    # byte parity of a graph against its NULL-pool run, then graph A followed
    # by a differently shaped graph B over the same pool, each byte identical
    # to its own serial run.  Also catches scratch/state sharing, bank skips,
    # reordered audio, stale live updates, changes to unlinked-output
    # histories and a pool that would die with the graph it was created for.
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
static fxdsp *load(const char *path) {
    char error[256]; fxdsp *d = fxdsp_load(path, error, sizeof error); assert(d); return d;
}
static void stage_live(fxdsp *d) {
    assert(fxdsp_live_begin(d));
    assert(fxdsp_live_param(d, "crystal", "intensity_band2_db", -3));
    assert(fxdsp_live_matrix(d, 0, 0, 0.75f));
    assert(fxdsp_live_output(d, 0, -3, 0.125f, 1));
    assert(fxdsp_live_peq(d, 0, 0, "bell", 500, 0.7f, -2));
    assert(!fxdsp_live_commit(d)); /* stopped graph: pending transaction */
}
static void process(struct stream *s) {
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
}
static void capture_with_ownership(const char *path, size_t quantum, fx_workers *pool, struct stream *s, int owned) {
    s->d = load(path);
    if (owned) assert(fxdsp_prepare_workers(s->d, NULL) > 0);
    else fxdsp_borrow_workers(s->d, pool); /* NULL pool: the serial reference run */
    s->channels = fxdsp_inputs(s->d);
    s->quantum = quantum;
    s->pcm = calloc(s->channels * FRAMES, sizeof(float));
    s->tap = calloc(2 * FRAMES, sizeof(float));
    assert(s->pcm && s->tap);
    stage_live(s->d);
    process(s);
}
static void capture(const char *path, size_t quantum, fx_workers *pool, struct stream *s) {
    capture_with_ownership(path, quantum, pool, s, 0);
}
struct owned_stream { const char *path; size_t quantum; struct stream stream; };
static void *capture_owned(void *context) {
    struct owned_stream *s = context;
    capture_with_ownership(s->path, s->quantum, NULL, &s->stream, 1);
    return NULL;
}
static void release(struct stream *s) { fxdsp_free(s->d); free(s->pcm); free(s->tap); }
static void compare(const struct stream *serial, const struct stream *pooled) {
    assert(serial->channels == pooled->channels);
    assert(!memcmp(serial->pcm, pooled->pcm, serial->channels * FRAMES * sizeof(float)));
    assert(!memcmp(serial->tap, pooled->tap, 2 * FRAMES * sizeof(float)));
    for (unsigned c = 0; c < serial->channels; c++) {
        float sp, sr, pp, pr;
        fxdsp_meter(serial->d, c, &sp, &sr); fxdsp_meter(pooled->d, c, &pp, &pr);
        assert(sp == pp && sr == pr);
    }
}
int main(int argc, char **argv) {
    (void)argc;
    /* A disabled policy and a zero environment cap both stay serial. */
    assert(!setenv("FXROUTE_DSP_WORKERS", "3", 1));
    fxdsp *probe = load(argv[1]);
    fx_worker_policy disabled = {0};
    assert(fxdsp_prepare_workers(probe, &disabled) == 0);
    fxdsp_free(probe);
    assert(!setenv("FXROUTE_DSP_WORKERS", "0", 1));
    probe = load(argv[1]);
    assert(fxdsp_prepare_workers(probe, NULL) == 0);
    fxdsp_free(probe);
    assert(!unsetenv("FXROUTE_DSP_WORKERS"));
    for (unsigned cycle = 0; cycle < 5; cycle++) {
        static const size_t quanta[] = {17, 128, 511, 1024, 8192};
        size_t quantum = quanta[cycle];
        struct stream serial_a = {0}, pooled_a = {0}, serial_b = {0}, pooled_b = {0};
        capture(argv[1], quantum, NULL, &serial_a);
        capture(argv[2], quantum, NULL, &serial_b);
        /* One pool for both graphs, created for a graph that is freed first. */
        fx_worker_policy policy = {.count = 3};
        fxdsp *owner = load(argv[1]);
        fx_workers *pool = fxdsp_create_engine_workers(owner, &policy);
        assert(pool && fx_workers_count(pool) > 0);
        fxdsp_free(owner);
        capture(argv[1], quantum, pool, &pooled_a);
        capture(argv[2], quantum, pool, &pooled_b);
        assert(fxdsp_worker_count(pooled_a.d) == fx_workers_count(pool));
        assert(fxdsp_worker_count(pooled_b.d) == fx_workers_count(pool));
        compare(&serial_a, &pooled_a);
        compare(&serial_b, &pooled_b);
        struct owned_stream independent[2] = {{.path=argv[1], .quantum=quantum},
                                             {.path=argv[1], .quantum=quantum}};
        pthread_t threads[2];
        for (unsigned i = 0; i < 2; i++) assert(!pthread_create(&threads[i], NULL, capture_owned, &independent[i]));
        for (unsigned i = 0; i < 2; i++) {
            assert(!pthread_join(threads[i], NULL));
            compare(&serial_a, &independent[i].stream); release(&independent[i].stream);
        }
        release(&serial_a); release(&pooled_a); release(&serial_b); release(&pooled_b);
        fx_workers_destroy(pool);
    }
    return 0;
}
''')
    target = tmp_path / "graphs"
    compile_native(target, [NATIVE / name for name in ("dsp.c", "worker_pool.c", "autogain.c", "crystalizer.c", "upmix_fast.c", "upmix_hq.c", "lv2_host.c")] + [harness])
    # A second, narrower graph: no convolver stage and no output convolution,
    # so it spreads fewer jobs and leaves part of the shared pool idle.
    narrow = tmp_path / "narrow.conf"
    narrow.write_text("\n".join(["rate 48000", "inputs 2", "outputs 2",
                                 "matrix 0 0 0.9", "matrix 1 1 0.9",
                                 "peq 0 bell 1200 0.9 -3", "peq 1 bell 1200 0.9 -3",
                                 "stage_begin 0 crystal native crystalizer", "stage_end",
                                 "stage_begin 1 delay native delay",
                                 "param left_ms 0.125", "param right_ms 0.25", "stage_end",
                                 "output 0 -3 0.25 normal", "output 1 -3 0.5 normal"]) + "\n")
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
        subprocess.run([str(target), str(config), str(narrow)], check=True, timeout=180)


def test_timing_reset_is_acknowledged_at_a_drained_callback_boundary(tmp_path):
    # Catches reset erasing maxima of retained samples and acknowledges only
    # a completed reset, even when the graph initially has no callbacks.
    from test_native_dsp_kernel_parity import CORE_SOURCES, compile_native
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
    compile_native(target, [*CORE_SOURCES, harness])
    config = tmp_path / "neutral.conf"
    config.write_text("rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n")
    subprocess.run([str(target), str(config)], check=True, timeout=10)


def test_borrowed_destruction_keeps_helpers_and_offline_destruction_joins(tmp_path):
    # Catches ownership changes, creation for the initial serial graph only,
    # and joining a borrowed pool while another graph still needs it.
    from test_native_dsp_kernel_parity import CORE_SOURCES, compile_native
    serial = tmp_path / "serial.conf"
    serial.write_text("rate 48000\ninputs 2\noutputs 4\nmatrix 0 0 1\nmatrix 1 1 1\n")
    parallel = tmp_path / "parallel.conf"
    parallel.write_text(serial.read_text() + "peq 0 bell 1000 0.7 1\n")
    harness = tmp_path / "ownership.c"
    harness.write_text(r'''
#include "dsp.h"
#include <assert.h>
#include <pthread.h>
struct backend { unsigned created, joined; pthread_t thread; };
static void *create(void *(*function)(void *), void *argument, void *context) {
    struct backend *b = context;
    assert(b->created++ == 0);
    assert(!pthread_create(&b->thread, NULL, function, argument)); return &b->thread;
}
static void join(void *thread, void *context) {
    struct backend *b = context;
    assert(!pthread_join(*(pthread_t *)thread, NULL)); b->joined++;
}
static fxdsp *load(const char *path) {
    char error[256]; fxdsp *d = fxdsp_load(path, error, sizeof error); assert(d); return d;
}
int main(int argc, char **argv) {
    (void)argc;
    struct backend live = {0}, offline = {0};
    fx_worker_policy policy = {.count=1, .create_thread=create, .join_thread=join, .context=&live};
    fxdsp *a = load(argv[1]);
    fx_workers *pool = fxdsp_create_engine_workers(a, &policy);
    assert(pool && live.created == 1);
    fxdsp_borrow_workers(a, pool); assert(fxdsp_worker_demand(a) == 1);
    fxdsp_free(a); assert(live.joined == 0);
    for (unsigned i = 0; i < 12; i++) {
        fxdsp *b = load(argv[i % 2 ? 1 : 2]); fxdsp_borrow_workers(b, pool);
        float in[2][128] = {{0}}, out[4][128];
        const float *inputs[] = {in[0], in[1]}; float *outputs[] = {out[0], out[1], out[2], out[3]};
        assert(fxdsp_enable_timing(b, 1));
        assert(!fxdsp_read_timing(b, FXDSP_TIMING_CALLBACK).samples);
        fxdsp_process(b, inputs, outputs, 128); fxdsp_record_callback(b, 123);
        assert(fxdsp_read_timing(b, FXDSP_TIMING_CALLBACK).samples == 1);
        fxdsp_free(b); assert(live.created == 1 && live.joined == 0);
    }
    fx_workers_destroy(pool); assert(live.joined == 1);
    policy.context = &offline;
    a = load(argv[2]); assert(fxdsp_prepare_workers(a, &policy) == 1);
    fxdsp_free(a); assert(offline.created == 1 && offline.joined == 1);
    return 0;
}
''')
    target = tmp_path / "ownership"
    compile_native(target, [*CORE_SOURCES, harness])
    subprocess.run([str(target), str(serial), str(parallel)], check=True, timeout=30)


def test_timing_names_keep_json_metric_mapping_and_reject_unknown_indices(tmp_path):
    # Catches swapped metric labels and out-of-bounds name lookup.
    from test_native_dsp_kernel_parity import CORE_SOURCES, compile_native
    harness = tmp_path / "names.c"
    harness.write_text(r'''
#include "dsp.h"
#include <assert.h>
#include <string.h>
int main(void) {
    assert(!strcmp(fxdsp_timing_name(FXDSP_TIMING_CALLBACK), "callback"));
    assert(!strcmp(fxdsp_timing_name(FXDSP_TIMING_BARRIER), "barrier"));
    assert(!strcmp(fxdsp_timing_name(FXDSP_TIMING_WAKE), "wake"));
    assert(!fxdsp_timing_name(FXDSP_TIMING_COUNT));
    assert(!fxdsp_timing_name(UINT32_MAX));
    return 0;
}
''')
    target = tmp_path / "names"
    compile_native(target, [*CORE_SOURCES, harness])
    subprocess.run([str(target)], check=True, timeout=10)


def test_live_swap_fixtures_load_through_the_real_config_parser(tmp_path):
    # Catches stage index gaps and bank lines placed after the stage section.
    from test_native_dsp_kernel_parity import CORE_SOURCES, compile_native
    from verify_native_worker_swaps import write_configs
    target = tmp_path / "offline"
    compile_native(target, [*CORE_SOURCES, NATIVE / "offline.c"])
    for rate in (44100, 48000):
        write_configs(tmp_path, rate)
        for kind in ("serial", "stage", "bank"):
            subprocess.run([str(target), str(tmp_path / f"{kind}.conf"), "/dev/null", "/dev/null"],
                           check=True, stdout=subprocess.DEVNULL, timeout=10)


if __name__ == "__main__":
    sys.exit(run_pytest_style_module(globals()))
