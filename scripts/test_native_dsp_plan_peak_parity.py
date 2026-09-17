#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Compiled-layout peak prediction matches native PCM execution.

Renders one compiled output layout (bank PEQ, crossover SOS, mono
convolver, trim/delay/polarity) through the real offline engine and compares
every output peak against the Python predictor. Needs the native toolchain,
so it only runs on .104 (registered in NATIVE_HELPER_TESTS); the local
runner skips it with an explicit reason.
"""

import array
import math
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DSP = ROOT / "native_dsp" / "build" / "fxroute-dsp-offline"

sys.path.insert(0, str(ROOT))

from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state import default_output_state, set_mode_routing
from audio.output_state_store import OutputStateStore
from dsp.manager import DSPManager, build_wav_bytes
from measurement.autosub.jobs import (
    _auto_sub_stage_peak_prediction,
    _auto_sub_sweep_input_pcm,
)

RATE = 48000
PROFILE = {"sweep_start_hz": 20.0, "sweep_end_hz": 600.0, "sweep_seconds": 0.5}
TOLERANCE_DB = 0.05


def write_ir(path, taps):
    path.write_bytes(build_wav_bytes(1, RATE, 32, 3, struct.pack(f"<{len(taps)}f", *taps)))


def run_offline(tmp_path, cfg_text, left, right):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    frames = len(left)
    assert len(right) == frames
    cfg = tmp_path / "parity.conf"
    cfg.write_text(cfg_text)
    source = tmp_path / "parity.f32"
    target = tmp_path / "parity.out.f32"
    interleaved = array.array("f", [0.0] * (frames * 2))
    interleaved[0::2] = array.array("f", (float(v) for v in left))
    interleaved[1::2] = array.array("f", (float(v) for v in right))
    with source.open("wb") as handle:
        interleaved.tofile(handle)
    completed = subprocess.run([str(DSP), str(cfg), str(source), str(target)],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    output = array.array("f")
    with target.open("rb") as handle:
        output.fromfile(handle, target.stat().st_size // 4)
    assert len(output) == frames * 3
    return [[float(v) for v in output[channel::3]] for channel in range(3)]


def test_plan_peak_parity(tmp_path):
    directory = tempfile.TemporaryDirectory(prefix="plan-peak-parity-")
    try:
        home = Path(directory.name)
        manager = DSPManager(home=home / "dsp")
        service = OutputService(OutputServiceDeps(
            store=OutputStateStore(home / "output-state.json"),
            preset_loader=manager.preset_store.read,
            resolve_ir=lambda name: (_ for _ in ()).throw(AssertionError(name)),
            measurement_active=lambda: False))
        state = set_mode_routing(default_output_state(), "stereo", "dev",
                                 ["main_l", "main_r", "sub1", "sub1"])
        committed = service.commit(state, expected_revision=0)
        plan = service.compile_plan(committed, output_key="dev", channels=4,
                                    sample_rate_hz=RATE)
        layout = service.compile_layout(plan)
        assert [entry["name"] for entry in layout] == ["main_l", "main_r", "sub1"]
        ir = tmp_path / "parity-ir.wav"
        write_ir(ir, [0.5, 0.25])
        bell = {"type": "bell", "frequency_hz": 200.0, "q": 1.0,
                "gain_db": 3.0, "stages": 1}
        layout[0]["filters"].append(dict(bell))
        layout[1]["filters"].append(dict(bell))
        layout[1]["gain_db"] = -6.0206
        layout[1]["delay_ms"] = 2.0
        layout[1]["invert"] = True
        layout[2]["oconv"] = {"path": str(ir), "channel": 0, "wet_db": 0.0,
                              "dry_db": -100.0, "input_gain_db": 0.0,
                              "output_gain_db": 0.0}
        fingerprint = service.fingerprint_plan(plan)
        text = manager.compile_engine_text(
            [dict(entry) for entry in layout], preset_name="Direct",
            sample_rate_hz=RATE, extras_override=plan["global"]["extras"])
        sweep = _auto_sub_sweep_input_pcm(PROFILE, RATE)
        native = run_offline(tmp_path, text, sweep, sweep)
        predicted = _auto_sub_stage_peak_prediction(
            sweep_profile=PROFILE, sample_rate=RATE, channel="stereo",
            layout=layout, plan_fingerprint=fingerprint,
            output_gain_db=0.0, playback_gain=1.0, sink_gain=1.0)
        assert set(predicted["linear"]) == {"output_1", "output_2", "output_3"}
        for index, channel in enumerate(native):
            key = f"output_{index + 1}"
            native_peak = max(abs(v) for v in channel)
            python_peak = predicted["linear"][key]
            assert python_peak > 1e-6, f"{key} has no audible peak"
            diff_db = abs(20.0 * math.log10(native_peak / python_peak))
            assert diff_db <= TOLERANCE_DB, f"{key}: native={native_peak} python={python_peak}"
    finally:
        directory.cleanup()


from native_test_runner import run_pytest_style_module

if __name__ == "__main__":
    sys.exit(run_pytest_style_module(globals()))
