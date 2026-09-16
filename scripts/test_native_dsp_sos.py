#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""SOS crossover execution: the C engine applies Python-designed coefficients."""

import array
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DSP = ROOT / "native_dsp" / "build" / "fxroute-dsp-offline"

sys.path.insert(0, str(ROOT))

from dsp.crossover import crossover_response, design_crossover


def sos_block(sections, output=0):
    lines = []
    for b0, b1, b2, _, a1, a2 in sections:
        lines.append(f"sos {output} {b0!r} {b1!r} {b2!r} {a1!r} {a2!r}")
    return "\n".join(lines) + "\n"


def run_offline(tmp_path, name, cfg_text, freq_hz, rate=48000, frames=48000,
                n_inputs=1, n_outputs=1, amplitude=0.5):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    cfg = tmp_path / f"{name}.conf"
    cfg.write_text(cfg_text)
    source = tmp_path / f"{name}.f32"
    target = tmp_path / f"{name}.out.f32"
    values = array.array("f")
    for index in range(frames):
        sample = amplitude * math.sin(2.0 * math.pi * freq_hz * index / rate)
        for _ in range(n_inputs):
            values.append(sample)
    values.tofile(source.open("wb"))
    completed = subprocess.run([str(DSP), str(cfg), str(source), str(target)],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    output = array.array("f")
    output.fromfile(target.open("rb"), target.stat().st_size // 4)
    return [output[channel::n_outputs] for channel in range(n_outputs)]


def steady_rms(samples, skip=16000):
    window = samples[skip:]
    return math.sqrt(sum(value * value for value in window) / len(window))


def check_level(measured_db, expected_db, context, delta=0.3):
    if expected_db < -90.0:
        assert measured_db < -80.0, f"{context}: {measured_db:.1f} dB not below floor"
    else:
        assert abs(measured_db - expected_db) <= delta, \
            f"{context}: {measured_db:.2f} dB != {expected_db:.2f} dB"


def test_sos_matches_python_prediction(tmp_path):
    cases = [
        ("butterworth", 12, "lowpass", (500, 2000, 8000)),
        ("butterworth", 24, "highpass", (8000, 2000, 500)),
        ("bessel", 24, "lowpass", (500, 2000, 8000)),
        ("bessel", 12, "highpass", (8000, 2000, 500)),
        ("linkwitz-riley", 24, "lowpass", (500, 2000, 8000)),
        ("linkwitz-riley", 24, "highpass", (8000, 2000, 500)),
    ]
    for family, slope, kind, (pass_hz, cut_hz, stop_hz) in cases:
        spec = {"kind": kind, "family": family, "slope_db_oct": slope, "frequency_hz": 2000}
        sections = design_crossover(spec, 48000)
        cfg = ("rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\n" + sos_block(sections))
        for freq_hz in (pass_hz, cut_hz, stop_hz):
            (channel,) = run_offline(tmp_path, f"sos-{family}-{slope}-{kind}-{freq_hz}",
                                     cfg, freq_hz)
            measured = 20.0 * math.log10(steady_rms(channel) / (0.5 / math.sqrt(2.0)))
            predicted = 20.0 * math.log10(abs(crossover_response(sections, freq_hz, 48000)))
            check_level(measured, predicted, f"{family}/{slope}/{kind}@{freq_hz}")


def test_sos_lr24_complementary_sum_is_flat(tmp_path):
    low = design_crossover({"kind": "lowpass", "family": "linkwitz-riley",
                            "slope_db_oct": 24, "frequency_hz": 1000}, 48000)
    high = design_crossover({"kind": "highpass", "family": "linkwitz-riley",
                             "slope_db_oct": 24, "frequency_hz": 1000}, 48000)
    cfg = ("rate 48000\ninputs 1\noutputs 2\nmatrix 0 0 1\nmatrix 1 0 1\n"
           + sos_block(low, 0) + sos_block(high, 1))
    for freq_hz in (250, 1000, 4000):
        low_ch, high_ch = run_offline(tmp_path, f"sos-sum-{freq_hz}", cfg, freq_hz,
                                      n_outputs=2)
        summed = [a + b for a, b in zip(low_ch[16000:], high_ch[16000:])]
        measured = 20.0 * math.log10(steady_rms(summed, skip=0) / (0.5 / math.sqrt(2.0)))
        assert abs(measured) <= 0.2, f"LR24 sum @{freq_hz}: {measured:.2f} dB"


def test_sos_rejects_unstable_and_malformed_lines(tmp_path):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    bad_lines = [
        "sos 0 1 0 0 0 1.5\n",          # pole outside unit circle
        "sos 0 1 0 0 2.5 0.5\n",        # violates |a1| < 1 + a2
        "sos 0 1 0 0 nan 0.1\n",        # non-finite coefficient
        "sos 0 1 0 0 inf 0.1\n",
        "sos 0 1 0 0 0.1\n",            # missing field
        "sos 40 1 0 0 0.1 0.05\n",      # output index out of range
    ]
    for index, line in enumerate(bad_lines):
        cfg = tmp_path / f"sos-bad-{index}.conf"
        cfg.write_text("rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\n" + line)
        source = tmp_path / f"sos-bad-{index}.f32"
        array.array("f", [0.0] * 64).tofile(source.open("wb"))
        target = tmp_path / f"sos-bad-{index}.out.f32"
        completed = subprocess.run([str(DSP), str(cfg), str(source), str(target)],
                                   capture_output=True, text=True)
        assert completed.returncode != 0, f"accepted bad line: {line.strip()}"


def test_plan_render_end_to_end_matches_prediction(tmp_path):
    from audio.output_state import default_output_state, set_mode_routing, switch_mode
    from dsp.manager import DSPManager
    from dsp.native_config import layout_from_plan
    from dsp.processing_plan import compile_processing_plan
    manager = DSPManager(home=tmp_path / "home")
    assignments = [f"{side}_{way}" for side in ("left", "right") for way in ("low", "mid", "high")]
    state = switch_mode(set_mode_routing(default_output_state(), "crossover", "A", assignments), "crossover")
    for role, settings in state["modes"]["crossover"]["processing"].items():
        if not role.endswith("low"):
            settings["highpass"] = {"family": "linkwitz-riley", "slope_db_oct": 24,
                                    "frequency_hz": 300 if role.endswith("mid") else 2500}
        if not role.endswith("high"):
            settings["lowpass"] = {"family": "linkwitz-riley", "slope_db_oct": 24,
                                   "frequency_hz": 300 if role.endswith("low") else 2500}
    plan = compile_processing_plan(state, output_key="A", channels=6, sample_rate_hz=48000,
                                   preset_loader=manager.preset_store.read)

    def no_ir(kernel):
        raise AssertionError(f"no IR expected, got {kernel}")

    layout = layout_from_plan(plan, resolve_ir=no_ir)
    # Direct bypasses the global helpers (the default limiter audibly colors
    # even sub-threshold sines, +0.9 dB measured) so this asserts exactly the
    # plan-derived output path. Output processing survives bypass by design.
    text = manager.compile_engine_text(layout, sample_rate_hz=48000, preset_name="Direct")
    for role, out_index, freq_hz in (("left_low", 0, 100), ("left_mid", 1, 1000), ("right_high", 5, 10000)):
        channels = run_offline(tmp_path, f"sos-e2e-{role}", text, freq_hz,
                               n_inputs=2, n_outputs=6)
        measured = 20.0 * math.log10(steady_rms(channels[out_index]) / (0.5 / math.sqrt(2.0)))
        assert abs(measured) <= 0.5, f"{role}: {measured:.2f} dB"
    channels = run_offline(tmp_path, "sos-e2e-stop", text, 5000, n_inputs=2, n_outputs=6)
    measured = 20.0 * math.log10(steady_rms(channels[0]) / (0.5 / math.sqrt(2.0)))
    assert measured < -60.0, f"left_low stopband: {measured:.1f} dB"


from native_test_runner import run_pytest_style_module

if __name__ == "__main__":
    sys.exit(run_pytest_style_module(globals()))
