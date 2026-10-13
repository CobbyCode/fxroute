#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
#
# Convolver sample-rate level stability.
#
# The pre-native-DSP engine needed hidden per-rate output-gain
# compensation anchors (44100 +1 dB ... 768000 -24 dB).  The native
# convolver resamples the IR with libsamplerate and convolves at the
# stream rate; the requirement is that an otherwise identical Convolver
# configuration must NOT produce a level jump when the playback sample
# rate changes.  This suite measures the native convolver at 44.1/48/96/192
# kHz against the 48 kHz reference.  If the level is stable, no
# compensation is needed; the failure of this suite would be the signal to
# add native compensation.
#
# Resampling must also keep the IR's own response.  The converter emits
# nothing ahead of the first IR sample, so the loader puts a faded zero
# lead-in of 32 periods of the lower rate in front of every resampled IR;
# without it a minimum-phase IR lost 2.7 dB at twice its rate and more inside
# every cut.  The suite checks the response, the channel balance of a delayed
# stereo pair and the latency of that lead-in.  The engine's readback of the
# same latency is covered by native_dsp/test_av_sync.c.

import array
import cmath
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
ENGINE_RATES = (44100, 48000, 88200, 96000, 176400, 192000)
LEVEL_TOLERANCE_DB = 0.2
RESPONSE_TOLERANCE_DB = 0.15
BALANCE_TOLERANCE_DB = 0.05
LEAD_PERIODS = 32


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
    # A delta at the first sample is the worst case for the cut-off
    # pre-ringing: without the lead-in it measured -2.6 dB at 96 kHz and
    # -4.1 dB at 192 kHz after the ratio normalization.
    for rate in RATES:
        output = run_convolver(tmp_path, ir, rate, build_sine(rate))
        tail = output[-int(len(output) * 0.25):]
        level_db = 10.0 * math.log10(
            sum(value * value for value in tail) / len(tail) + 1e-30)
        expected = 10.0 * math.log10(0.125 + 1e-30)
        assert abs(level_db - expected) < RESPONSE_TOLERANCE_DB, (
            f"unity impulse level drift at {rate} Hz: {level_db:.3f} dB "
            f"(expected {expected:.3f})")


def write_float_wav(path, rate, channels):
    frames = len(channels[0])
    data = array.array("f", (channels[channel][frame] for frame in range(frames)
                             for channel in range(len(channels)))).tobytes()
    path.write_bytes(
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 3, len(channels), rate, rate * 4 * len(channels),
                      4 * len(channels), 32)
        + b"data" + struct.pack("<I", len(data)) + data)
    return path


def minimum_phase_ir(rate, length=2048):
    # Impulse response of a PEQ cascade, as a REW filter export or a
    # minimum-phase correction FIR: all of its onset sits in the first sample.
    samples = [0.0] * length
    samples[0] = 10.0 ** (-2.0 / 20.0)
    for frequency, gain_db, q in ((300.0, -9.0, 2.0), (2000.0, 3.0, 1.5), (6000.0, -5.0, 2.0)):
        amplitude = 10.0 ** (gain_db / 40.0)
        omega = 2.0 * math.pi * frequency / rate
        alpha = math.sin(omega) / (2.0 * q)
        b0, b1, b2 = 1.0 + alpha * amplitude, -2.0 * math.cos(omega), 1.0 - alpha * amplitude
        a0, a1, a2 = 1.0 + alpha / amplitude, -2.0 * math.cos(omega), 1.0 - alpha / amplitude
        state1 = state2 = 0.0
        for index, value in enumerate(samples):
            output = (b0 * value + state1) / a0
            state1 = b1 * value - a1 * output + state2
            state2 = b2 * value - a2 * output
            samples[index] = output
    return samples


def linear_phase_ir(rate, length=512):
    # Autocorrelation of a minimum-phase IR: symmetric, peak in the middle.
    source = minimum_phase_ir(rate, length)
    return [sum(source[index] * source[index + abs(lag)] for index in range(length - abs(lag)))
            for lag in range(1 - length, length)]


def lead_in_frames(file_rate, rate):
    # Latency of the resampling lead-in in engine frames, before rounding.
    if file_rate == rate:
        return 0.0
    return math.ceil(LEAD_PERIODS * max(1.0, file_rate / rate)) * rate / file_rate


def impulse_responses(tmp_path, ir, file_rate, rate, taps, channels=1):
    # Loaded taps per channel, read back with a unit impulse on every input.
    frames = int(math.ceil((taps + 2 * LEAD_PERIODS * max(1.0, file_rate / rate)) * rate / file_rate)) + 256
    config = tmp_path / "ir.conf"
    source = tmp_path / "ir-in.f32"
    target = tmp_path / "ir-out.f32"
    config.write_text(
        f"rate {rate}\ninputs {channels}\noutputs {channels}\n"
        + "".join(f"matrix {channel} {channel} 1\n" for channel in range(channels))
        + "stage_begin 0 conv native convolver\n"
        f'param path "{ir}"\n'
        "param wet_db 0\nparam dry_db -200\nparam input_gain_db 0\nparam output_gain_db 0\n"
        "stage_end\n"
        + "".join(f"output {channel} 0 0 normal\n" for channel in range(channels))
        + "bypass 0\n")
    stimulus = array.array("f", [0.0]) * (frames * channels)
    for channel in range(channels):
        stimulus[channel] = 0.5
    stimulus.tofile(source.open("wb"))
    subprocess.run([str(DSP), str(config), str(source), str(target)],
                   check=True, capture_output=True)
    output = array.array("f")
    output.fromfile(target.open("rb"), target.stat().st_size // 4)
    assert len(output) == frames * channels
    return [[2.0 * value for value in output[channel::channels]] for channel in range(channels)]


def response_grid(file_rate, rate, points=40):
    top = min(20000.0, 0.45 * min(file_rate, rate))
    return [20.0 * (top / 20.0) ** (index / (points - 1)) for index in range(points)]


def response_db(samples, rate, frequencies):
    result = []
    for frequency in frequencies:
        step = cmath.exp(-2j * math.pi * frequency / rate)
        phasor, total = 1.0 + 0.0j, 0.0j
        for value in samples:
            total += value * phasor
            phasor *= step
        result.append(20.0 * math.log10(max(abs(total), 1e-12)))
    return result


def peak_index(samples):
    return max(range(len(samples)), key=lambda index: abs(samples[index]))


def test_minimum_phase_ir_keeps_its_response_at_every_rate(tmp_path):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    for file_rate in (44100, 48000):
        taps = minimum_phase_ir(file_rate)
        ir = write_float_wav(tmp_path / f"minimum-{file_rate}.wav", file_rate, [taps])
        native = impulse_responses(tmp_path, ir, file_rate, file_rate, len(taps))[0]
        # At its own rate the IR is loaded as written, without a lead-in.
        assert max(abs(native[index] - array.array("f", [taps[index]])[0])
                   for index in range(len(taps))) < 1e-6
        for rate in ENGINE_RATES:
            if rate == file_rate:
                continue
            frequencies = response_grid(file_rate, rate)
            reference = response_db(native, file_rate, frequencies)
            resampled = impulse_responses(tmp_path, ir, file_rate, rate, len(taps))[0]
            measured = response_db(resampled, rate, frequencies)
            worst = max(range(len(frequencies)), key=lambda index: abs(measured[index] - reference[index]))
            assert abs(measured[worst] - reference[worst]) < RESPONSE_TOLERANCE_DB, (
                f"minimum-phase IR {file_rate} -> {rate} Hz: "
                f"{measured[worst] - reference[worst]:+.3f} dB at {frequencies[worst]:.0f} Hz")


def test_delayed_stereo_pair_keeps_balance_and_delay(tmp_path):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    # Minimum phase aligned: one side starts later by a run of zeros and so
    # carried its own lead-in; only the other side lost level.
    file_rate, delay = 48000, 24
    taps = minimum_phase_ir(file_rate)
    ir = write_float_wav(tmp_path / "aligned.wav", file_rate,
                         [[0.0] * delay + taps, taps + [0.0] * delay])
    for rate in ENGINE_RATES:
        left, right = impulse_responses(tmp_path, ir, file_rate, rate, len(taps) + delay, channels=2)
        frequencies = response_grid(file_rate, rate)
        frequencies += [frequency for frequency in (17000.0, 18000.0, 19000.0) if frequency < frequencies[-1]]
        left_db, right_db = response_db(left, rate, frequencies), response_db(right, rate, frequencies)
        for index, frequency in enumerate(frequencies):
            # The converter's transition band rings longer than the lead-in.
            # Beside a 44.1 kHz Nyquist that reaches the top of the grid:
            # measured 0.07 dB at 19.8 kHz, 0.01 dB below 16 kHz.
            tolerance = BALANCE_TOLERANCE_DB if frequency <= 0.44 * min(file_rate, rate) else 0.1
            assert abs(left_db[index] - right_db[index]) < tolerance, (
                f"channel balance at {rate} Hz: {left_db[index] - right_db[index]:+.3f} dB "
                f"at {frequency:.0f} Hz")
        if rate % file_rate == 0:
            assert peak_index(left) - peak_index(right) == delay * rate // file_rate, rate


def test_linear_phase_ir_keeps_its_response_and_gains_only_the_lead_in(tmp_path):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    file_rate = 48000
    taps = linear_phase_ir(file_rate)
    centre = len(taps) // 2
    ir = write_float_wav(tmp_path / "linear.wav", file_rate, [taps])
    native = impulse_responses(tmp_path, ir, file_rate, file_rate, len(taps))[0]
    assert peak_index(native) == centre
    for rate in ENGINE_RATES:
        if rate == file_rate:
            continue
        frequencies = response_grid(file_rate, rate)
        reference = response_db(native, file_rate, frequencies)
        resampled = impulse_responses(tmp_path, ir, file_rate, rate, len(taps))[0]
        measured = response_db(resampled, rate, frequencies)
        assert max(abs(measured[index] - reference[index]) for index in range(len(frequencies))) < 0.02, rate
        if rate % file_rate == 0:
            assert peak_index(resampled) == (centre + LEAD_PERIODS) * rate // file_rate, rate


def test_resampling_lead_in_is_32_periods_of_the_lower_rate(tmp_path):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    for file_rate in (44100, 48000, 96000):
        ir = write_float_wav(tmp_path / f"delta-{file_rate}.wav", file_rate, [[1.0] + [0.0] * 255])
        for rate in ENGINE_RATES:
            delay = peak_index(impulse_responses(tmp_path, ir, file_rate, rate, 256)[0])
            expected = lead_in_frames(file_rate, rate)
            assert delay == round(expected), (
                f"lead-in {file_rate} -> {rate} Hz: {delay} frames, expected {expected:.2f}")
            if rate == file_rate:
                assert delay == 0
            else:
                # One frame of rounding around 32 periods of the lower rate.
                wanted_seconds = LEAD_PERIODS / min(file_rate, rate)
                assert abs(delay / rate - wanted_seconds) <= 1.0 / rate, (file_rate, rate, delay)


from native_test_runner import run_pytest_style_module

if __name__ == "__main__":
    sys.exit(run_pytest_style_module(globals()))
