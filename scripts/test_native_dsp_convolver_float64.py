#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
#
# Float64 convolver import end to end.
#
# A small genuine IEEE float64 WAV must survive the whole chain: import
# (converted cleanly to the engine's float32 encoding), preset load (the
# stored kernel validates and is referenced), and real engine playback
# through fxroute-dsp-offline.  The engine proves the Load step: its
# load_wav rejects anything but PCM 16/24/32 and float32, so a successful
# run means the converted kernel is exactly what the native convolver
# accepts.  An impulse input makes the output equal the kernel taps, so the
# test compares the playback result tap-for-tap against the stored file and
# against the original float64 source.

import array
import math
import random
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DSP = ROOT / "native_dsp" / "build" / "fxroute-dsp-offline"

from dsp.manager import DSPManager, parse_wav_frames  # noqa: E402
from native_test_runner import run_pytest_style_module  # noqa: E402

RATE = 48000
TAPS = 1024
TAIL = 512


def build_float64_ir(path):
    """A small genuine float64 IR: decaying noise with sub-float32 detail."""
    random.seed(20260920)
    payload = b"".join(
        struct.pack("<d", 0.9 * math.exp(-3.0 * index / TAPS) * random.uniform(-1.0, 1.0))
        for index in range(TAPS)
    )
    width = 8
    path.write_bytes(b"".join([
        b"RIFF", (36 + len(payload)).to_bytes(4, "little"), b"WAVE",
        b"fmt ", (16).to_bytes(4, "little"),
        (3).to_bytes(2, "little"), (1).to_bytes(2, "little"),
        RATE.to_bytes(4, "little"), (RATE * width).to_bytes(4, "little"),
        width.to_bytes(2, "little"), (64).to_bytes(2, "little"),
        b"data", len(payload).to_bytes(4, "little"),
        payload,
    ]))
    return [struct.unpack("<d", payload[i * 8:(i + 1) * 8])[0] for i in range(TAPS)]


def run_engine(tmp_path, ir_path, frames):
    config = tmp_path / "dsp-float64.conf"
    source = tmp_path / "in-float64.f32"
    target = tmp_path / "out-float64.f32"
    config.write_text(
        f"rate {RATE}\n"
        "inputs 1\n"
        "outputs 1\n"
        "matrix 0 0 1\n"
        "stage_begin 0 conv native convolver\n"
        f'param path "{ir_path}"\n'
        "param wet_db 0\n"
        "param dry_db -100\n"
        "param input_gain_db 0\n"
        "param output_gain_db 0\n"
        "stage_end\n"
        "output 0 0 0 normal\n"
        "bypass 0\n"
    )
    samples = array.array("f", [1.0] + [0.0] * (frames - 1))
    samples.tofile(source.open("wb"))
    subprocess.run([str(DSP), str(config), str(source), str(target)],
                   check=True, capture_output=True)
    output = array.array("f")
    output.fromfile(target.open("rb"), target.stat().st_size // 4)
    return output


def test_float64_ir_import_load_and_engine_playback(tmp_path):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    home = tmp_path / "dsp-home"
    home.mkdir()
    manager = DSPManager(home=home)

    # Import: the float64 source converts to a float32 kernel on store.
    source = tmp_path / "room64.wav"
    reference64 = build_float64_ir(source)
    uploaded = manager.upload_ir(source, "room64.wav")
    stored = Path(uploaded["path"])
    parsed = parse_wav_frames(stored)
    assert (parsed["channels"], parsed["format"], parsed["bits"]) == (1, 3, 32)
    assert parsed["rate"] == RATE
    stored_taps = list(struct.unpack(f"<{TAPS}f", parsed["data"]))
    for index, (original, tap) in enumerate(zip(reference64, stored_taps)):
        assert abs(original - tap) <= 1e-6, f"tap {index} lost precision in conversion"

    # Load: the converted kernel validates and is referenced by a preset.
    preset = manager.create_convolver_preset("Float64 IR", uploaded["name"])
    assert preset["kernel_name"] == Path(uploaded["name"]).stem
    assert Path(preset["path"]).is_file()

    # Playback: an impulse through the engine must come back as the kernel.
    # Tap 0 additionally carries the -100 dB dry bleed of the unit impulse
    # (1e-5 by design); taps 1..N must match the stored kernel bit-nearly.
    output = run_engine(tmp_path, stored, TAPS + TAIL)
    assert len(output) == TAPS + TAIL
    assert all(math.isfinite(value) for value in output)
    assert abs(output[0] - stored_taps[0]) < 2e-5, (
        f"engine tap 0 off: {output[0]:.8f} vs {stored_taps[0]:.8f}")
    rest_error = max(abs(value - tap) for value, tap in zip(output[1:TAPS], stored_taps[1:]))
    assert rest_error < 1e-6, f"engine output differs from stored taps: {rest_error:.3g}"
    tail_peak = max(abs(value) for value in output[TAPS:])
    assert tail_peak < 1e-6, f"engine output tail is not silent: {tail_peak:.3g}"


if __name__ == "__main__":
    sys.exit(run_pytest_style_module(globals()))
