#!/usr/bin/env python3
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native_dsp"


def test_atomic_mutes_and_peak_snapshots_work_offline(tmp_path):
    harness = tmp_path / "control_test.c"
    binary = tmp_path / "control_test"
    config = tmp_path / "dsp.conf"
    config.write_text(
        "rate 48000\n"
        "inputs 1\n"
        "outputs 3\n"
        "matrix 0 0 1\n"
        "matrix 1 0 0.5\n"
        "matrix 2 0 0.25\n"
    )
    harness.write_text(
        r'''
#include "dsp.h"
#include <math.h>
#include <stdint.h>

int main(int argc, char **argv) {
    char error[256];
    fxdsp *d = fxdsp_load(argv[1], error, sizeof error);
    float source[] = {1.0f, 0.5f};
    float a[2], b[2], c[2];
    const float *input[] = {source};
    float *output[] = {a, b, c};
    float peaks[FXDSP_MAX_CHANNELS] = {0};
    if (!d) return 1;
    fxdsp_set_mute(d, UINT32_C(0x5), 1);
    fxdsp_process(d, input, output, 1);
    fxdsp_set_mute(d, UINT32_C(0x1), 0);
    source[0] = source[1];
    output[0] = a + 1;
    output[1] = b + 1;
    output[2] = c + 1;
    fxdsp_process(d, input, output, 1);
    if (a[0] != 0 || b[0] != 0.5f || c[0] != 0) return 2;
    if (a[1] != 0.5f || b[1] != 0.25f || c[1] != 0) return 3;
    if (fxdsp_peaks(d, peaks, FXDSP_MAX_CHANNELS) != 3) return 4;
    if (fabsf(peaks[0] - 0.5f) > 1e-6f ||
        fabsf(peaks[1] - 0.5f) > 1e-6f || peaks[2] != 0) return 5;
    fxdsp_reset_peaks(d);
    if (fxdsp_peaks(d, peaks, FXDSP_MAX_CHANNELS) != 3) return 6;
    if (peaks[0] != 0 || peaks[1] != 0 || peaks[2] != 0) return 7;
    fxdsp_free(d);
    (void)argc;
    return 0;
}
'''
    )
    subprocess.run(
        [
            "cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-pedantic",
            "-I", str(NATIVE), str(NATIVE / "dsp.c"), str(harness), "-lm", "-o", str(binary),
        ],
        check=True,
    )
    subprocess.run([str(binary), str(config)], check=True)


def test_pipewire_engine_exposes_non_rt_datagram_control_protocol():
    source = (NATIVE / "pipewire_engine.c").read_text()
    assert "usage: %s CONFIG [CONTROL_SOCKET]" in source
    assert "SOCK_DGRAM" in source and "AF_UNIX" in source
    assert '"mute"' in source and '"peaks reset"' in source and '"peaks get"' in source
    assert '"{\\"peaks\\":["' in source
    assert "pthread_create" in source and "pthread_join" in source


def test_pipewire_process_callback_has_no_non_rt_operations():
    source = (NATIVE / "pipewire_engine.c").read_text()
    start = source.index("static void on_process")
    body = source[start:source.index("\n}", start)]
    forbidden = (
        "malloc(", "calloc(", "realloc(", "free(", "fopen(", "read(", "write(",
        "recv", "send", "socket(", "printf(", "snprintf(", "pthread_", "mutex", "lock(",
    )
    assert all(call not in body for call in forbidden)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        test_atomic_mutes_and_peak_snapshots_work_offline(Path(directory))
    test_pipewire_engine_exposes_non_rt_datagram_control_protocol()
    test_pipewire_process_callback_has_no_non_rt_operations()
    print("native DSP control tests passed")
