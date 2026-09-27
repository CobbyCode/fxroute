#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Full bank chain agreement: plan render through the real offline engine.

Crossover SOS, area convolvers (short and partitioned IRs) and output
trims render from one processing plan; steady-state sine levels through
the offline binary must match the Python cascade prediction
(crossover response x exact IR DTFT) on every way.
"""

import array
import cmath
import math
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DSP = ROOT / "native_dsp" / "build" / "fxroute-dsp-offline"

sys.path.insert(0, str(ROOT))

from audio.output_state import default_output_state, set_crossover, set_mode_routing, switch_mode
from dsp.crossover import design_crossover
from dsp.manager import DSPManager, build_wav_bytes
from dsp.native_config import layout_from_plan
from dsp.processing_plan import compile_processing_plan


def build_once():
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)


def run_offline(tmp_path, name, cfg_text, freq_hz, rate=48000, frames=48000,
                n_inputs=2, n_outputs=6, amplitude=0.5):
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


def ir_magnitude_db(taps, freq_hz, rate=48000):
    total = sum(tap * cmath.exp(-2j * math.pi * freq_hz * index / rate)
                for index, tap in enumerate(taps))
    return 20.0 * math.log10(abs(total))


def write_mono_ir(tmp_path, manager, name, taps):
    source = tmp_path / f"{name}.wav"
    source.write_bytes(build_wav_bytes(
        1, 48000, 32, 3, struct.pack(f"<{len(taps)}f", *taps)))
    manager.upload_ir(source, f"{name}.wav")


def test_full_bank_chain_matches_python_prediction(tmp_path):
    build_once()
    manager = DSPManager(home=tmp_path / "home")
    short_taps = [0.5, -0.25, 0.125, -0.0625, 0.03125, -0.015625, 0.0078125, -0.00390625]
    long_taps = [math.sin(index * 0.9) * math.exp(-index / 60.0) for index in range(300)]
    write_mono_ir(tmp_path, manager, "mid-short", short_taps)
    write_mono_ir(tmp_path, manager, "mid-long", long_taps)
    manager.preset_store.write(
        "Short IR", {"schema": "fxroute.dsp.preset", "version": 1,
                     "chain": [{"id": "conv", "type": "convolver",
                                "params": {"kernel": "mid-short"}}]})
    manager.preset_store.write(
        "Long IR", {"schema": "fxroute.dsp.preset", "version": 1,
                    "chain": [{"id": "conv", "type": "convolver",
                               "params": {"kernel": "mid-long"}}]})

    assignments = [f"{side}_{way}" for side in ("left", "right") for way in ("low", "mid", "high")]
    state = switch_mode(set_mode_routing(
        set_crossover(default_output_state(), "stereo-sub", True),
        "stereo-sub", "A", assignments), "stereo-sub")
    for role, settings in state["modes"]["stereo-sub"]["processing"].items():
        if not role.startswith(("left_", "right_")):
            continue
        if not role.endswith("low"):
            settings["highpass"] = {"family": "linkwitz-riley", "slope_db_oct": 24,
                                    "frequency_hz": 300 if role.endswith("mid") else 2500}
        if not role.endswith("high"):
            settings["lowpass"] = {"family": "linkwitz-riley", "slope_db_oct": 24,
                                   "frequency_hz": 300 if role.endswith("low") else 2500}
    # Divergent per-channel chains predate pair banks; the native layer still
    # renders each stored role binding, so assign them directly.
    for role, preset in (("left_mid", "Short IR"), ("right_mid", "Long IR")):
        state["modes"]["stereo-sub"]["banks"][role] = {
            "preset": preset, "preset_a": preset, "preset_b": None}
    plan = compile_processing_plan(state, output_key="A", channels=6, sample_rate_hz=48000,
                                   preset_loader=manager.preset_store.read)

    def resolve_ir(kernel):
        calls.append(kernel)
        path = manager._resolve_kernel_path(kernel)
        return {"path": str(path), "channels": 1}

    calls: list = []
    layout = layout_from_plan(plan, resolve_ir=resolve_ir)
    assert calls, "area convolvers must resolve through the IR store"
    # Direct bypasses the default global limiter (which audibly colors even
    # sub-threshold sines, +0.9 dB measured) so this asserts exactly the
    # plan-derived output path; output processing survives bypass by design.
    text = manager.compile_engine_text(
        [dict(entry) for entry in layout], preset_name="Direct",
        sample_rate_hz=48000, extras_override=plan["global"]["extras"])
    assert "oconv 1 " in text and "oconv 4 " in text

    outputs = {row["role"]: row for row in plan["outputs"]}

    def predict(role, freq_hz):
        cascade = 1.0 + 0.0j
        for crossover in outputs[role]["crossover"]:
            sections = design_crossover(dict(crossover), 48000)
            for section in sections:
                b0, b1, b2, _, a1, a2 = section
                z = cmath.exp(2j * math.pi * freq_hz / 48000)
                cascade *= (b0 + b1 / z + b2 / z / z) / (1.0 + a1 / z + a2 / z / z)
        level = 20.0 * math.log10(abs(cascade))
        bank = outputs[role]["bank"]
        assert bank["preset"] in ("Neutral", "Short IR", "Long IR")
        if bank["preset"] == "Short IR":
            level += ir_magnitude_db(short_taps, freq_hz)
        elif bank["preset"] == "Long IR":
            level += ir_magnitude_db(long_taps, freq_hz)
        return level

    order = [row["role"] for row in plan["outputs"]]
    for role, freq_hz in (("left_low", 100), ("left_mid", 1000), ("left_high", 10000),
                          ("right_low", 100), ("right_mid", 1000), ("right_high", 10000)):
        channels = run_offline(tmp_path, f"chain-{role}", text, freq_hz)
        measured = 20.0 * math.log10(steady_rms(channels[order.index(role)]) / (0.5 / math.sqrt(2.0)))
        predicted = predict(role, freq_hz)
        assert abs(measured - predicted) <= 0.4, f"{role}@{freq_hz}: {measured:.2f} != {predicted:.2f}"

    channels = run_offline(tmp_path, "chain-stop", text, 5000, n_outputs=6)
    measured = 20.0 * math.log10(steady_rms(channels[0]) / (0.5 / math.sqrt(2.0)))
    assert measured < -60.0, f"left_low stopband: {measured:.1f} dB"


def test_global_lsp_peq_renders_the_area_bank_rbj_response(tmp_path):
    """One PEQ preset sounds the same in the Global bank and an area bank.

    The Global bank renders PEQ through LSP para_equalizer (filter mode
    APO (DR)), area banks through the native RBJ biquads, so a REW Q means
    the same in both. LSP's default RLC (BT) drew a Q 45.76 bell as Q 28.
    """
    build_once()
    manager = DSPManager(home=tmp_path / "home")
    manager.create_peq_preset("RBJ", {"params": {"bands": [
        {"filterType": kind, "frequencyHz": frequency, "gainDb": gain, "q": q}
        for kind, frequency, gain, q in (
            ("bell", 120, -10.0, 2.0), ("bell", 688, 7.0, 45.76), ("notch", 3000, 0.0, 4.0),
            ("high_pass", 40, 0.0, 2.0), ("low_shelf", 250, 4.0, 0.707),
            ("high_shelf", 6000, -5.0, 0.707), ("low_pass", 15000, 0.0, 0.707))]}})
    frames = 1 << 15

    def impulse_response(bank):
        state = switch_mode(set_mode_routing(
            default_output_state(), "stereo", "A", ["main_l", "main_r"]), "stereo")
        if bank is not None:
            state["modes"]["stereo"]["banks"][bank]["preset"] = "RBJ"
        plan = compile_processing_plan(state, output_key="A", channels=2, sample_rate_hz=48000,
                                       preset_loader=manager.preset_store.read)
        text = manager.compile_engine_text(
            layout_from_plan(plan, resolve_ir=lambda name: {}),
            preset_name=plan["global"]["preset"], sample_rate_hz=48000,
            extras_override=plan["global"]["extras"])
        name = bank or "reference"
        cfg, source, target = (tmp_path / f"rbj-{name}{suffix}"
                               for suffix in (".conf", ".f32", ".out.f32"))
        cfg.write_text(text)
        # A small impulse keeps every global stage (limiter) linear.
        values = array.array("f", [0.0] * (2 * frames))
        values[0] = values[1] = 0.1
        with source.open("wb") as handle:
            values.tofile(handle)
        completed = subprocess.run([str(DSP), str(cfg), str(source), str(target)],
                                   capture_output=True, text=True)
        assert completed.returncode == 0, completed.stderr
        output = array.array("f")
        with target.open("rb") as handle:
            output.fromfile(handle, target.stat().st_size // 4)
        return output[0::2]

    reference, via_global, via_area = (
        impulse_response(bank) for bank in (None, "global", "main_l"))
    # Half-gain edges of the Q 45.76 bell sit at 688 +- 7.5 Hz; the notch null
    # itself is skipped (its depth is numerically meaningless).
    for frequency in (30, 40, 60, 90, 120, 160, 250, 400, 680.5, 688, 695.5,
                      1000, 2700, 3300, 6000, 9000, 12000, 15000, 18000):
        level = ir_magnitude_db(reference, frequency)
        global_db = ir_magnitude_db(via_global, frequency) - level
        area_db = ir_magnitude_db(via_area, frequency) - level
        assert abs(global_db - area_db) <= 0.1, (
            f"{frequency} Hz: Global {global_db:+.2f} dB != area {area_db:+.2f} dB")


from native_test_runner import run_pytest_style_module

if __name__ == "__main__":
    sys.exit(run_pytest_style_module(globals()))
