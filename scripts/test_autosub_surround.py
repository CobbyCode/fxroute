#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Surround AutoSub prediction, silent positions and native rendering parity."""

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state import default_output_state, set_bass_management, set_crossover, set_mode_routing, switch_mode
from audio.output_state_store import OutputStateStore
from dsp.manager import DSPManager
from measurement.autosub.jobs import (
    _auto_sub_stage_peak_comparison, _auto_sub_stage_peak_prediction,
    _auto_sub_sweep_input_pcm, _auto_sub_zero_sub_peaks,
)
from measurement.routing import MeasurementRouting

RATE = 48000
PROFILE = {"sweep_start_hz": 20.0, "sweep_end_hz": 600.0, "sweep_seconds": 0.25}
POSITIONS = ["FL", "FR", "FC", "LFE", "SL", "SR", "BL", "BR"]
QUAD_SUBS = ["main_l", "main_r", "rear_l", "rear_r", "sub1", "sub2"]


def output(routes, **extra):
    return {"name": "sub1", "routes": routes, "input_positions": POSITIONS,
            "gain_db": 0.0, "delay_ms": 0.0, "invert": False,
            "filters": [], "sos": [], "oconv": None, **extra}


def prediction(layout, channel="left", **extra):
    return _auto_sub_stage_peak_prediction(
        sweep_profile=PROFILE, sample_rate=RATE, channel=channel,
        layout=layout, plan_fingerprint="surround-test", output_gain_db=0.0, **extra)


class SurroundPeakTests(unittest.TestCase):
    def test_silent_center_lfe_side_and_rear_routes_are_valid(self):
        layout = [output([{"input": index, "gain": 1.0}]) for index in range(8)]
        for channel, driven in (("left", {1}), ("right", {2}), ("stereo", {1, 2})):
            with self.subTest(channel=channel):
                peaks = prediction(layout, channel)["linear"]
                for index in range(1, 9):
                    self.assertAlmostEqual(peaks[f"output_{index}"], 0.8 if index in driven else 0.0)

    def test_primary_crossover_precedes_output_bank_sos(self):
        layout = [output([{"input": 0, "gain": 1.0}],
                         crossover_sos=[[0.5, 0, 0, 0, 0]],
                         sos=[[0.25, 0, 0, 0, 0]])]
        self.assertAlmostEqual(prediction(layout)["linear"]["output_1"], 0.1)

    def test_group_buses_and_post_routes_sum_before_bank_processing(self):
        layout = [output([{"input": 0, "gain": 1.0}],
                         crossover_sos=[[0.5, 0, 0, 0, 0]],
                         crossover_groups=[{"routes": [{"input": 1, "gain": 0.5}],
                                            "sos": [[0.25, 0, 0, 0, 0]]}],
                         post_crossover_routes=[{"input": 1, "gain": 0.25},
                                                {"input": 3, "gain": 10.0}],
                         sos=[[0.5, 0, 0, 0, 0]])]
        for channel, want in (("left", 0.2), ("right", 0.15), ("stereo", 0.35)):
            with self.subTest(channel=channel):
                self.assertAlmostEqual(prediction(layout, channel)["linear"]["output_1"], want)

    def test_invalid_routes_and_crossover_data_fail_closed_even_when_silent(self):
        valid = output([{"input": 0, "gain": 1.0}])
        cases = [
            {"routes": [{"input": value, "gain": 1.0}]}
            for value in (-1, 8, True, 1.5, "2")
        ] + [
            {"input_positions": POSITIONS[:-1]},
            {"crossover_sos": "invalid"},
            {"crossover_sos": [[0.5, 0, 0, 0, 2]]},
            {"post_crossover_routes": [{"input": 8, "gain": 1.0}]},
            {"post_crossover_routes": [{"input": 3, "gain": float("nan")}]},
            {"crossover_groups": "invalid"},
            {"crossover_groups": [{"routes": [], "sos": []}]},
            {"crossover_groups": [{"routes": [{"input": 7, "gain": 0.5}],
                                    "sos": [[1, 0, 0, 0, float("nan")]]}]},
        ]
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                prediction([{**valid, **extra}])
        with self.assertRaises(ValueError):
            prediction([valid, {**valid, "input_positions": None}])

    def test_cache_recomputes_when_primary_crossover_changes(self):
        layout = [output([{"input": 0, "gain": 1.0}], crossover_sos=[[0.5, 0, 0, 0, 0]])]
        self.assertAlmostEqual(prediction(layout)["linear"]["output_1"], 0.4)
        layout[0]["crossover_sos"][0][0] = 0.25
        self.assertAlmostEqual(prediction(layout)["linear"]["output_1"], 0.2)


class CompiledSurroundTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="autosub-surround-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.manager = DSPManager(home=self.directory / "dsp")
        self.service = OutputService(OutputServiceDeps(
            store=OutputStateStore(self.directory / "output-state.json"),
            preset_loader=self.manager.preset_store.read, resolve_ir=lambda name: {},
            measurement_active=lambda: False))

    def layout(self, *, mono=False, crossover=False, linked=False):
        roles = QUAD_SUBS[:-1] if mono else QUAD_SUBS
        state = set_crossover(default_output_state(), "surround", crossover)
        if crossover:
            roles = ["left_low", "left_high", "right_low", "right_high", *roles[2:]]
        state = switch_mode(set_mode_routing(state, "surround", "dev", roles), "surround")
        if not linked:
            state = set_bass_management(state, "surround", frequency_hz=200, sub_link=False,
                                       sub_filters={group: {"family": "linkwitz-riley",
                                                           "slope_db_oct": 24,
                                                           "frequency_hz": frequency,
                                                           "highpass_enabled": True}
                                                    for group, frequency in (("front", 80), ("rear", 120))})
        plan = self.service.compile_alignment_plan(state, output_key="dev", channels=len(roles), sample_rate_hz=RATE)
        return self.service.compile_layout(plan)

    def test_real_quad_sub_layout_matches_front_only_measurement_routing(self):
        for mono in (False, True):
            for linked in (False, True):
                with self.subTest(mono=mono, linked=linked):
                    layout = self.layout(mono=mono, linked=linked)
                    router = MeasurementRouting(SimpleNamespace(
                        _routing_command_runner=None, _routing_output_overview=None))
                    inputs = router._surround_measurement_inputs(
                        {"measurement_scope": "raw_helper", "expected_native_layout": layout},
                        {"measurement_channel": "left"})
                    self.assertEqual(inputs, [])
                    peaks = prediction(layout)
                    self.assertGreater(peaks["linear"]["output_1"], 0.0)
                    for index in (2, 3, 4):
                        self.assertEqual(peaks["linear"][f"output_{index}"], 0.0)
                    # Compare the real Surround sub path to its equivalent stereo
                    # bass bus, including the lowpass stored in crossover_sos.
                    reference = copy.deepcopy(layout[4])
                    reference.pop("input_positions")
                    reference["routes"] = [{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}]
                    reference["sos"] = reference.pop("crossover_sos") + reference["sos"]
                    reference.pop("crossover_groups", None)
                    reference.pop("post_crossover_routes")
                    want = prediction([reference])["linear"]["output_1"]
                    self.assertAlmostEqual(peaks["linear"]["output_5"], want)
                    if not mono:
                        self.assertEqual(peaks["linear"]["output_6"], want)
                    mask = 0b10000 if mono else 0b110000
                    muted = _auto_sub_zero_sub_peaks(peaks, tuple(i for i in range(len(layout)) if mask & (1 << i)))
                    self.assertEqual(muted["linear"]["output_5"], 0.0)
                    self.assertEqual(muted["linear"]["output_1"], peaks["linear"]["output_1"])

    def test_surround_prediction_matches_native_offline_peaks(self):
        binary = ROOT / "native_dsp/build/fxroute-dsp-offline"
        if not binary.is_file():
            self.skipTest("Native offline binary unavailable; verify on the test host")
        layouts = [self.layout(crossover=crossover) for crossover in (False, True)]
        layouts.append([output(
            [{"input": 0, "gain": 0.5}],
            crossover_sos=[[0.5, 0.25, 0, -0.25, 0.1]],
            crossover_groups=[{"routes": [{"input": 1, "gain": 0.3}],
                               "sos": [[0.25, 0.1, 0, -0.1, 0.05]]}],
            post_crossover_routes=[{"input": 1, "gain": 0.2}, {"input": 3, "gain": 1.0}],
            sos=[[0.5, 0.1, 0, -0.2, 0.1]])])
        for layout_index, layout in enumerate(layouts):
            config = self.directory / "engine.conf"
            config.write_text(self.manager.compile_engine_text(layout, preset_name="Direct", sample_rate_hz=RATE))
            for channel in ("left", "right", "stereo"):
                with self.subTest(layout=layout_index, channel=channel):
                    sweep = _auto_sub_sweep_input_pcm(PROFILE, RATE)
                    pcm = np.zeros((len(sweep), 8), dtype="<f4")
                    if channel in ("left", "stereo"):
                        pcm[:, 0] = sweep
                    if channel in ("right", "stereo"):
                        pcm[:, 1] = sweep
                    source, target = self.directory / "input.f32", self.directory / "output.f32"
                    pcm.tofile(source)
                    subprocess.run([str(binary), str(config), str(source), str(target)], check=True, capture_output=True)
                    rendered = np.fromfile(target, dtype="<f4").reshape(-1, len(layout))
                    measured = {f"output_{index + 1}": float(np.max(np.abs(rendered[:, index])))
                                for index in range(len(layout))}
                    compared = _auto_sub_stage_peak_comparison(prediction(layout, channel), measured)
                    self.assertFalse(compared["relevant_mismatch"], compared)
                    self.assertTrue(compared["measured_safe"])
                    for difference in compared["difference_db"].values():
                        self.assertLess(abs(difference), 0.01)


if __name__ == "__main__":
    unittest.main(verbosity=2)
