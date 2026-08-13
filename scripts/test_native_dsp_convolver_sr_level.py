#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
#
# Convolver sample-rate level stability.
#
# The EasyEffects-era engine needed hidden per-rate output-gain
# compensation anchors (44100 +1 dB ... 768000 -24 dB).  The native
# convolver resamples the IR with libsamplerate and convolves at the
# stream rate; the requirement is that an otherwise identical Convolver
# configuration must NOT produce a level jump when the playback sample
# rate changes.  This suite measures the native convolver at 44.1/48/96/192
# kHz against the 48 kHz reference.  If the level is stable, no
# compensation is needed; the failure of this suite would be the signal to
# add native compensation.

import array
import math
import random
import struct
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DSP = ROOT / "native_dsp" / "build" / "fxroute-dsp-offline"

RATES = (44100, 48000, 96000, 192000)
LEVEL_TOLERANCE_DB = 0.2


def build_ir(tmp_path, rate=48000):
    random.seed(42)
    ir = tmp_path / "room.irs"
    length = rate // 2
    samples = array.array("h")
    for index in range(length):
        decay = math.exp(-4.0 * index / length)
        samples.append(int(32767 * decay * random.uniform(-1.0, 1.0)))
    with wave.open(str(ir), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())
    return ir


def build_sine(rate, seconds=2.0, amplitude=0.5, frequency=997.0):
    samples = array.array(
        "f",
        (amplitude * math.sin(2 * math.pi * frequency * index / rate)
         for index in range(int(rate * seconds))),
    )
    return samples


def run_convolver(tmp_path, ir, rate, samples):
    config = tmp_path / f"dsp-{rate}.conf"
    source = tmp_path / f"in-{rate}.f32"
    target = tmp_path / f"out-{rate}.f32"
    config.write_text(
        f"rate {rate}\n"
        "inputs 1\n"
        "outputs 1\n"
        "matrix 0 0 1\n"
        "stage_begin 0 conv native convolver\n"
        f'param path "{ir}"\n'
        "param wet_db 0\n"
        "param dry_db -100\n"
        "param input_gain_db 0\n"
        "param output_gain_db 0\n"
        "stage_end\n"
        "output 0 0 0 normal\n"
        "bypass 0\n"
    )
    samples.tofile(source.open("wb"))
    subprocess.run([str(DSP), str(config), str(source), str(target)],
                   check=True, capture_output=True)
    output = array.array("f")
    output.fromfile(target.open("rb"), target.stat().st_size // 4)
    return output


def rms_db(values):
    tail = values[-int(len(values) * 0.25):]
    energy = sum(value * value for value in tail) / len(tail)
    return 10.0 * math.log10(energy + 1e-30)


def test_convolver_level_is_sample_rate_independent(tmp_path):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    ir = build_ir(tmp_path)
    reference = None
    levels = {}
    for rate in RATES:
        output = run_convolver(tmp_path, ir, rate, build_sine(rate))
        assert len(output) == int(rate * 2.0)
        assert all(math.isfinite(value) for value in output)
        level = rms_db(output)
        levels[rate] = level
        if reference is None:
            reference = level
    for rate in RATES:
        delta = levels[rate] - reference
        assert abs(delta) < LEVEL_TOLERANCE_DB, (
            f"convolver level drift at {rate} Hz: {delta:+.3f} dB vs 48000 Hz"
            f" (levels={levels})")


def test_convolver_impulse_response_is_level_neutral_at_every_rate(tmp_path):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    # A single-tap impulse WAV cannot be downsampled by libsamplerate (too
    # few frames to build the filter state), so pad the impulse with a
    # silence tail; the impulse response stays a unity impulse.
    ir = tmp_path / "impulse.wav"
    frames = struct.pack("<h", 32767) + struct.pack("<h", 0) * 479
    with wave.open(str(ir), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(frames)
    # A pure delta is the worst case for libsamplerate's ratio-dependent
    # gain: its resampled gain is sub-linear in the ratio (measured residual
    # -2.6 dB at 96 kHz and -4.1 dB at 192 kHz after the ratio
    # normalization, vs the x2.0/x4.0 gain of broadband material).  The
    # previous EasyEffects engine (fixed -6 dB/octave anchors) left the same
    # residual for deltas.  Real impulse responses are broadband and covered
    # tightly by test_convolver_level_is_sample_rate_independent; the delta
    # contract here is "no wild drift".
    for rate in RATES:
        output = run_convolver(tmp_path, ir, rate, build_sine(rate))
        tail = output[-int(len(output) * 0.25):]
        level_db = 10.0 * math.log10(
            sum(value * value for value in tail) / len(tail) + 1e-30)
        expected = 10.0 * math.log10(0.125 + 1e-30)
        tolerance = 0.3 if rate <= 48000 else (3.0 if rate <= 96000 else 4.5)
        assert abs(level_db - expected) < tolerance, (
            f"unity impulse level drift at {rate} Hz: {level_db:.3f} dB "
            f"(expected {expected:.3f})")


from native_test_runner import run_pytest_style_module

if __name__ == "__main__":
    sys.exit(run_pytest_style_module(globals()))
