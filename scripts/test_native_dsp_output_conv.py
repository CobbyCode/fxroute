#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Per-output convolver: exact taps, mix/gains, bypass retention, ordering."""

import array
import math
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DSP = ROOT / "native_dsp" / "build" / "fxroute-dsp-offline"

sys.path.insert(0, str(ROOT))

from dsp.manager import build_wav_bytes


def write_ir(tmp_path, name, channels, taps, rate=48000):
    flat = []
    for tap in taps:
        flat.extend(tap if channels > 1 else [tap])
    path = tmp_path / name
    path.write_bytes(build_wav_bytes(channels, rate, 32, 3, struct.pack(f"<{len(flat)}f", *flat)))
    return path


def run_impulse(tmp_path, name, cfg_text, frames=4096, n_inputs=1, n_outputs=1):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    cfg = tmp_path / f"{name}.conf"
    cfg.write_text(cfg_text)
    source = tmp_path / f"{name}.f32"
    target = tmp_path / f"{name}.out.f32"
    values = array.array("f", [0.0] * (frames * n_inputs))
    values[0] = 1.0
    values.tofile(source.open("wb"))
    completed = subprocess.run([str(DSP), str(cfg), str(source), str(target)],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    output = array.array("f")
    output.fromfile(target.open("rb"), target.stat().st_size // 4)
    return [list(output[channel::n_outputs]) for channel in range(n_outputs)]


def test_oconv_short_ir_reproduces_exact_taps(tmp_path):
    taps = [0.5, 0.25, 0.125, 0.0625]
    ir = write_ir(tmp_path, "short.wav", 1, taps)
    (channel,) = run_impulse(tmp_path, "oconv-short",
                             f"rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\n"
                             f"oconv 0 0 0 -100 0 0 \"{ir}\"\n")
    # dry -100 dB bleeds 1e-5 linear of the driven impulse into every tap.
    for index, tap in enumerate(taps):
        assert abs(channel[index] - tap) <= 2e-5, f"tap {index}: {channel[index]} != {tap}"
    for index, sample in enumerate(channel[len(taps):64]):
        assert abs(sample) <= 1e-7, f"tail {index}: {sample}"


def test_oconv_partitioned_tail_matches_full_ir(tmp_path):
    taps = [math.sin(index * 0.7) * math.exp(-index / 120.0) for index in range(600)]
    ir = write_ir(tmp_path, "long.wav", 1, taps)
    (channel,) = run_impulse(tmp_path, "oconv-long",
                             f"rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\n"
                             f"oconv 0 0 0 -100 0 0 \"{ir}\"\n",
                             frames=2048)
    for index, tap in enumerate(taps):
        assert abs(channel[index] - tap) <= 1e-4, f"tap {index}: {channel[index]} != {tap}"
    for index, sample in enumerate(channel[600:700]):
        assert abs(sample) <= 1e-4, f"tail {index}: {sample}"


def test_oconv_stereo_ir_channel_selection(tmp_path):
    ir = write_ir(tmp_path, "stereo.wav", 2, [(0.5, -0.25), (0.125, -0.0625)])
    for channel_index, expected in ((0, 0.5), (1, -0.25)):
        (channel,) = run_impulse(tmp_path, f"oconv-ch{channel_index}",
                                 f"rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\n"
                                 f"oconv 0 {channel_index} 0 -100 0 0 \"{ir}\"\n")
        assert abs(channel[0] - expected) <= 2e-5, f"ch{channel_index}: {channel[0]} != {expected}"


def test_oconv_dry_passthrough_and_wet_gain(tmp_path):
    ir = write_ir(tmp_path, "dry.wav", 1, [0.9, 0.1])
    (channel,) = run_impulse(tmp_path, "oconv-dry",
                             f"rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\n"
                             f"oconv 0 0 -100 0 0 0 \"{ir}\"\n")
    assert abs(channel[0] - 1.0) <= 2e-5, channel[0]
    assert abs(channel[1]) <= 1e-6, channel[1]
    (channel,) = run_impulse(tmp_path, "oconv-wet6",
                             f"rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\n"
                             f"oconv 0 0 -6.0206 -100 0 0 \"{ir}\"\n")
    assert abs(channel[0] - 0.45) <= 2e-5, channel[0]


def test_oconv_bypass_retains_output_convolver(tmp_path):
    ir = write_ir(tmp_path, "bypass.wav", 1, [0.5, 0.25])
    (channel,) = run_impulse(tmp_path, "oconv-bypass",
                             f"rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\n"
                             f"oconv 0 0 0 -100 0 0 \"{ir}\"\n"
                             f"output 0 0 0 normal\nbypass 1\n")
    assert abs(channel[0] - 0.5) <= 2e-5, channel[0]
    assert abs(channel[1] - 0.25) <= 2e-5, channel[1]


def test_oconv_applies_after_output_biquads(tmp_path):
    ir = write_ir(tmp_path, "shift.wav", 1, [0.0, 0.0, 1.0])
    plain = ("rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\n"
             "peq 0 lowpass 1000 0.70710678 0\n")
    (reference,) = run_impulse(tmp_path, "oconv-order-plain", plain)
    (delayed,) = run_impulse(tmp_path, "oconv-order-shift",
                             plain + f"oconv 0 0 0 -100 0 0 \"{ir}\"\n")
    for index in range(2, 512):
        assert abs(delayed[index] - reference[index - 2]) <= 1e-6, f"sample {index}"


def test_oconv_rejects_bad_configuration(tmp_path):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    ir = write_ir(tmp_path, "ok.wav", 1, [0.5])
    cases = [
        f"oconv 0 0 0 -100 0 0 \"{tmp_path}/missing.wav\"\n",
        f"oconv 0 5 0 -100 0 0 \"{ir}\"\n",
        f"oconv 0 0 0 -100 0 inf \"{ir}\"\n",
        f"oconv 0 0 0 -100 0 0 \"{ir}\"\noconv 0 0 0 -100 0 0 \"{ir}\"\n",
        f"oconv 40 0 0 -100 0 0 \"{ir}\"\n",
    ]
    for index, line in enumerate(cases):
        cfg = tmp_path / f"oconv-bad-{index}.conf"
        cfg.write_text("rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\n" + line)
        source = tmp_path / f"oconv-bad-{index}.f32"
        array.array("f", [0.0] * 64).tofile(source.open("wb"))
        target = tmp_path / f"oconv-bad-{index}.out.f32"
        completed = subprocess.run([str(DSP), str(cfg), str(source), str(target)],
                                   capture_output=True, text=True)
        assert completed.returncode != 0, f"accepted bad line {index}: {line.strip().splitlines()[0]}"


from native_test_runner import run_pytest_style_module

if __name__ == "__main__":
    sys.exit(run_pytest_style_module(globals()))
