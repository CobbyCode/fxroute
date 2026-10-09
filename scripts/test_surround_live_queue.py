#!/usr/bin/env python3
"""Exercise the actual ingress queue and quantum-aware worker measurements."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DEPS = ["libpipewire-0.3", "libavcodec", "libavutil", "libswresample"]
HARNESS = r'''
#define main ingress_main
#include "ingress.c"
#undef main
#include <assert.h>
#include <limits.h>

int main(void) {
    struct engine *e = calloc(1, sizeof *e);
    assert(e);
    e->base = 10000;
    e->worker_reserve_min_us = INT64_MAX;
    e->decoder = surround_create(live_emit, e);
    assert(e->decoder);
    surround_set_mute(e->decoder, live_mute);
    float pair[2] = {0.1f, 0.2f};
    /* Three raw-DTS syncs 1200 frames apart confirm after PCM was queued. */
    for (unsigned i = 0; i <= 2400; i++) {
        pair[0] = i % 1200 == 0 ? (float)(int16_t)0xfe7f / 32768 : 0.1f;
        pair[1] = i % 1200 == 0 ? (float)(int16_t)0x0180 / 32768 : 0.2f;
        surround_push(e->decoder, pair, 1);
        if (i == 2049) assert(atomic_load(&e->output_write) == 0);
        if (i == 2050) assert(atomic_load(&e->output_write) == 1);
    }
    uint64_t r = 0, w = atomic_load(&e->output_write);
    assert(w == 351);
    float out[SURROUND_OUTPUTS];
    for (unsigned i = 0; i < 351; i++) {
        assert(live_pull(e, 10000 + i, &r, w, out));
        for (unsigned c = 0; c < SURROUND_OUTPUTS; c++) assert(out[c] == 0);
    }
    assert(atomic_load(&e->queued_muted_frames) == 351);
    assert(atomic_load(&e->underrun_frames) == 0);

    /* A watermark survives ring wrap but must not mute future recovery. */
    atomic_store(&e->output_read, QUEUE - 1);
    atomic_store(&e->output_write, QUEUE - 1);
    float good[SURROUND_OUTPUTS] = {0.25f, -0.125f};
    live_emit(e, 2400, good);
    live_emit(e, 2401, good);
    r = QUEUE - 1; w = QUEUE + 1;
    assert(live_pull(e, 12400, &r, w, out) && out[0] == 0);
    assert(live_pull(e, 12401, &r, w, out) && out[0] == 0.25f);
    atomic_store(&e->output_read, r);
    atomic_store(&e->pull_before, 20000);
    live_emit(e, 9999, good);
    assert(atomic_load(&e->late_frames) == 1);
    assert(atomic_load(&e->output_write) == w);
    assert(!live_pull(e, 20000, &r, w, out));
    assert(atomic_load(&e->underrun_frames) == 1 && out[0] == 0);
    /* Time before a newly linked source's delayed first frame is not an underrun. */
    atomic_store(&e->ready_from, 21000);
    assert(!live_pull(e, 20500, &r, w, out));
    assert(atomic_load(&e->underrun_frames) == 1);
    atomic_store(&e->ready_from, 0);
    live_emit(e, 10000, good);
    assert(live_pull(e, 20000, &r, w + 1, out) && out[0] == 0.25f);

    /* Reset suppression reaches queued audio without changing queue slots. */
    atomic_store(&e->output_read, r);
    atomic_store(&e->pull_before, 0);
    atomic_store(&e->output_read, 0);
    atomic_store(&e->output_write, 0);
    atomic_store(&e->mute_before, 0);
    e->base = 30000;
    live_emit(e, 2400, good);
    surround_reset(e->decoder);
    e->base = 32401;
    live_emit(e, 0, good);
    r = 0;
    assert(live_pull(e, 32400, &r, 2, out) && out[0] == 0);
    assert(live_pull(e, 32401, &r, 2, out) && out[0] == 0.25f);

    /* Quantum 1024 leaves one callback period, not an additional period. */
    e->base = 0;
    struct input_frame f = {.seq = 2050, .block_seq = 2048,
                            .published_ns = 1000000000, .quantum = 1024};
    worker_observe(e, &f, 1005000000);
    assert(e->worker_delay_max_us == 5000);
    assert(e->worker_reserve_min_us == 16333);
    worker_observe(e, &f, 1022000000);
    assert(e->worker_delay_max_us == 22000);
    assert(e->worker_reserve_min_us == -666);
    surround_destroy(e->decoder);
    free(e);
    return 0;
}
'''


class LiveQueueTests(unittest.TestCase):
    def test_safety_watermark_late_discard_and_quantum_reserve(self):
        if subprocess.run(["pkg-config", "--exists", *DEPS]).returncode:
            self.skipTest("missing native Surround build dependencies; build on .104")
        with tempfile.TemporaryDirectory(prefix="surround-queue-", dir="/tmp/opencode") as directory:
            source = Path(directory) / "queue.c"
            binary = Path(directory) / "queue"
            source.write_text(HARNESS)
            flags = subprocess.check_output(["pkg-config", "--cflags", "--libs", *DEPS], text=True).split()
            subprocess.run([os.environ.get("CC", "cc"), "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT / "native_surround"), str(source),
                            str(ROOT / "native_surround/surround.c"), *flags, "-lm", "-pthread", "-o", str(binary)],
                           check=True)
            subprocess.run([str(binary)], check=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
