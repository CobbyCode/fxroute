#!/usr/bin/env python3
"""Check spectral accumulation and optimized/reference stream parity."""
import array
import os
import platform
from pathlib import Path
import random
import shlex
import struct
import subprocess
import sys

from native_test_runner import run_pytest_style_module

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native_dsp"


def compile_native(target, sources, flags=()):
    for name in ("worker_pool.c", "timing.c"):
        if NATIVE / name not in sources:
            sources = [*sources, NATIVE / name]
    packages = ["libebur128", "lilv-0", "samplerate", "speexdsp"]
    cflags = shlex.split(subprocess.check_output(["pkg-config", "--cflags", *packages], text=True))
    libs = shlex.split(subprocess.check_output(["pkg-config", "--libs", *packages], text=True))
    subprocess.run([
        os.environ.get("CC", "cc"), "-std=c11", "-O2", "-Wall", "-Wextra",
        "-Werror", "-pedantic", "-I", str(NATIVE), *flags, *cflags,
        *map(str, sources), *libs, "-lm", "-pthread", "-o", str(target),
    ], check=True)


def test_spectrum_accumulator_keeps_per_bin_rounding(tmp_path):
    # Catches exchanged real/imaginary lanes, scalar-tail omissions, and
    # reassociation across partitions. The reference is independent arithmetic.
    source = tmp_path / "spectrum.c"
    source.write_text(r'''
#include "dsp.c"
#include <assert.h>
int main(void) {
    complex_value x[519], h[519], actual[519], expected[519];
    uint32_t seed = 42;
    for (size_t count = 0; count <= 513; count++) {
        memset(actual, 0, sizeof actual);
        memset(expected, 0, sizeof expected);
        for (unsigned partition = 0; partition < 19; partition++) {
            for (size_t i = 0; i < 519; i++) {
                float *values[] = {&x[i].re, &x[i].im, &h[i].re, &h[i].im};
                for (unsigned j = 0; j < 4; j++) {
                    seed = 1664525U * seed + 1013904223U;
                    *values[j] = ((float)(seed >> 8) / 16777216.0f - 0.5f);
                }
            }
            accumulate_convolution_spectrum(x + 1, h + 1, actual + 1, count);
            for (size_t i = 1; i <= count; i++) {
                volatile float rr = x[i].re * h[i].re, ii = x[i].im * h[i].im;
                volatile float ri = x[i].re * h[i].im, ir = x[i].im * h[i].re;
                expected[i].re += rr - ii;
                expected[i].im += ri + ir;
            }
            assert(memcmp(actual, expected, sizeof actual) == 0);
        }
    }
    static const complex_value directed_x[] = {
        {0.0f, -0.0f}, {-0.0f, 0.0f}, {0x1p-149f, -0x1p-149f},
        {1.0f, -1.0f}, {0.5f, 0.5f}, {1e-30f, -1e-30f}
    };
    static const complex_value directed_h[] = {
        {1.0f, -1.0f}, {-1.0f, 1.0f}, {1.0f, 0.5f},
        {1.0f, 1.0f}, {0.5f, -0.5f}, {1e-10f, 1e-10f}
    };
    memset(actual, 0, sizeof actual);
    memset(expected, 0, sizeof expected);
    for (unsigned partition = 0; partition < 3; partition++) {
        accumulate_convolution_spectrum(directed_x, directed_h, actual, 6);
        for (size_t i = 0; i < 6; i++) {
            volatile float rr = directed_x[i].re * directed_h[i].re;
            volatile float ii = directed_x[i].im * directed_h[i].im;
            volatile float ri = directed_x[i].re * directed_h[i].im;
            volatile float ir = directed_x[i].im * directed_h[i].re;
            expected[i].re += rr - ii;
            expected[i].im += ri + ir;
        }
        assert(memcmp(actual, expected, sizeof actual) == 0);
    }
    return 0;
}
''')
    target = tmp_path / "spectrum"
    compile_native(target, [source, NATIVE / "autogain.c", NATIVE / "crystalizer.c", NATIVE / "lv2_host.c"], ["-ffp-contract=off"])
    subprocess.run([str(target)], check=True)


def test_optimized_stream_matches_reference_with_irregular_quanta(tmp_path):
    # Catches state/latency changes, aliasing mistakes, FFT lane errors, and
    # changes to adaptive intensity after a silent tail and restart.
    sources = [NATIVE / name for name in ("dsp.c", "autogain.c", "crystalizer.c", "lv2_host.c", "offline.c")]
    optimized, scalar = tmp_path / "optimized", tmp_path / "scalar"
    compile_native(optimized, sources)
    # Keep the existing fused NEON arithmetic on ARM. Only x86's new SSE2
    # path promises byte parity with explicit SIMD disabled.
    reference_flags = []
    if platform.machine().lower() in ("x86_64", "amd64", "i386", "i686"):
        reference_flags.append("-DFXROUTE_FORCE_SCALAR=1")
    reference_binary = os.environ.get("FXROUTE_DSP_REFERENCE")
    if reference_binary:
        scalar = Path(reference_binary).resolve()
    else:
        compile_native(scalar, sources, reference_flags)
    random_source = random.Random(42)
    for rate, channels, taps in ((48000, 2, 17), (96000, 2, 1031), (176400, 2, 8193), (48000, 6, 4097)):
        kernel = array.array("f", [random_source.uniform(-0.005, 0.005) for _ in range(taps * channels)])
        payload = kernel.tobytes()
        ir = tmp_path / "room.wav"
        ir.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(payload)) + b"WAVEfmt " +
                       struct.pack("<IHHIIHH", 16, 3, channels, rate, rate * channels * 4, channels * 4, 32) +
                       b"data" + struct.pack("<I", len(payload)) + payload)
        signal = array.array("f")
        for n in range(32768):
            for c in range(channels):
                signal.append(random_source.uniform(-0.2, 0.2) if n < 8192 or n > 24576 else 0.0)
        source = tmp_path / "signal.f32"
        source.write_bytes(signal.tobytes())
        for location in ("stage", "output"):
            lines = [f"rate {rate}", f"inputs {channels}", f"outputs {channels}"]
            lines += [f"matrix {c} {c} 1" for c in range(channels)]
            if location == "output":
                lines += [f'oconv {c} {c} -2 -50 -1 1 "{ir}"' for c in range(channels)]
            else:
                lines += ["stage_begin 0 conv native convolver", f'param path "{ir}"', "stage_end"]
            index = 1 if location == "stage" else 0
            lines += [f"stage_begin {index} crystal native crystalizer", "stage_end"]
            lines += [f"output {c} -2 {c * 0.125} normal" for c in range(channels)]
            config = tmp_path / "pipeline.conf"
            config.write_text("\n".join(lines) + "\n")
            reference = tmp_path / "reference.f32"
            subprocess.run([str(scalar), str(config), str(source), str(reference), "1024"], check=True, capture_output=True)
            for quantum in (17, 128, 511, 1024):
                result = tmp_path / "result.f32"
                subprocess.run([str(optimized), str(config), str(source), str(result), str(quantum)], check=True, capture_output=True)
                assert result.read_bytes() == reference.read_bytes(), (rate, channels, taps, location, quantum)


def test_benchmark_stimulus_is_quantum_independent(tmp_path):
    sources = [NATIVE / name for name in ("dsp.c", "autogain.c", "crystalizer.c", "lv2_host.c", "benchmark_pipeline.c")]
    target = tmp_path / "benchmark"
    compile_native(target, sources)
    config = tmp_path / "neutral.conf"
    config.write_text("rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\noutput 0 0 0 normal\noutput 1 0 0 normal\n")
    reference = None
    for quantum in (17, 128, 1024):
        output = tmp_path / f"q{quantum}.f32"
        subprocess.run([str(target), str(config), "0.13", str(quantum), str(output)], check=True, capture_output=True)
        data = output.read_bytes()
        assert len(data) == 6240 * 2 * 4
        if reference is None:
            reference = data
        assert data == reference
    result = subprocess.run([str(target), str(config), "0.0000001", "128"], capture_output=True)
    assert result.returncode == 2


if __name__ == "__main__":
    sys.exit(run_pytest_style_module(globals()))
