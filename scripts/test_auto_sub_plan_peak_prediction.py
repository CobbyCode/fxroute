#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""AutoSub peak prediction against the compiled multichannel output layout.

The legacy predictor models exactly four bass-management outputs and can no
longer describe routing-derived systems (fan-out, three-port 2.1, crossover
ways). The layout path processes every compiled output instead: route sums,
crossover SOS plus bank PEQ, the mono convolver, delay/trim/polarity, the
runtime output gain and the sink gain. Regression coverage:

* exactly one of ``config`` xor ``layout`` selects the model; a layout needs
  its plan fingerprint and a bounded runtime output gain,
* route gains sum the driven inputs (a mono-summed sub sees half a
  single-sided sweep, the full peak of a stereo sweep),
* crossover SOS shapes the band (a lowpassed sub stays below its flat main),
* bank PEQ moves the peak in the configured direction,
* trim gain scales exactly, delay/invert preserve the peak,
* the runtime output gain scales every output,
* the convolver applies its IR and wet/dry/input/output gains; rewriting the
  IR file recomputes instead of serving the stale cache entry,
* the cache key covers model tag, fingerprint, canonical layout, IR identity,
  rate, sweep, gains and channel,
* Main-only zeroing works on layout predictions and fails closed on outputs
  the model does not expose,
* the peak comparison fails closed when predicted and measured output sets
  differ,
* the async wrapper still runs the layout path off the event loop,
* a real compiled plan/layout from the output service predicts one peak per
  output.
"""

import copy
import math
import os
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audio.output_service import OutputService, OutputServiceDeps  # noqa: E402
from audio.output_state import default_output_state, set_mode_routing  # noqa: E402
from audio.output_state_store import OutputStateStore  # noqa: E402
from dsp.crossover import design_crossover  # noqa: E402
from dsp.manager import DSPManager, build_wav_bytes  # noqa: E402

import measurement.autosub.jobs as autosub_jobs  # noqa: E402
from measurement.autosub.jobs import (  # noqa: E402
    _auto_sub_stage_peak_comparison,
    _auto_sub_stage_peak_prediction,
    _auto_sub_zero_sub_peaks,
    _predict_auto_sub_stage_peaks,
)
from measurement.autosub.roles import sub_mute_indices  # noqa: E402

PROFILE = {"sweep_start_hz": 20.0, "sweep_end_hz": 600.0, "sweep_seconds": 0.25}
RATE = 48000


def flat_output(name, routes, **overrides):
    output = {"name": name, "routes": routes, "gain_db": 0.0, "delay_ms": 0.0,
              "invert": False, "filters": [], "sos": [], "oconv": None}
    output.update(overrides)
    return output


def lowpass_sos(cutoff_hz=80, slope_db_oct=24):
    return [[float(value) for value in section[:3] + section[4:]]
            for section in design_crossover(
                {"kind": "lowpass", "family": "linkwitz-riley",
                 "slope_db_oct": slope_db_oct, "frequency_hz": cutoff_hz}, RATE)]


def layout_args(outputs, **overrides):
    args = dict(sweep_profile=PROFILE, sample_rate=RATE, channel="left",
                layout=outputs, plan_fingerprint="fp-test",
                output_gain_db=0.0, playback_gain=1.0, sink_gain=1.0)
    args.update(overrides)
    return args


class PlanPeakPredictionTests(unittest.TestCase):
    def setUp(self) -> None:
        autosub_jobs._AUTO_SUB_PEAK_PREDICTION_CACHE_KEY = None
        autosub_jobs._AUTO_SUB_PEAK_PREDICTION_CACHE_RESULT = None
        autosub_jobs._AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_KEY = None
        autosub_jobs._AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_RESULT = None
        directory = tempfile.TemporaryDirectory(prefix="plan-peak-ir-")
        self.addCleanup(directory.cleanup)
        self.ir_dir = Path(directory.name)

    def write_ir(self, name="ir.wav", taps=(0.5,), channels=1):
        flat = []
        for tap in taps:
            flat.extend(tap if channels > 1 else [tap])
        path = self.ir_dir / name
        path.write_bytes(build_wav_bytes(channels, RATE, 32, 3, struct.pack(f"<{len(flat)}f", *flat)))
        return str(path)

    def test_layout_and_config_are_mutually_exclusive(self) -> None:
        overview = {
            "selected_output": {"key": "mock", "channels": 4, "active_rate": 48000},
            "output_mode": {
                "mode": "subwoofer-2.1", "crossover_frequency_hz": 80,
                "main_highpass_enabled": True,
                "subwoofer": {"crossover_frequency_hz": 80, "main_highpass_enabled": True,
                              "sub_alignment_ms": 0.0, "sub_level_db": 0.0,
                              "sub_polarity": "normal"},
            },
        }
        from dsp.runtime import BassManagementConfig
        config = BassManagementConfig.from_overview(overview)
        with self.assertRaises(ValueError):
            _auto_sub_stage_peak_prediction(**layout_args(
                [flat_output("main_l", [{"input": 0, "gain": 1.0}])], config=config))
        with self.assertRaises(ValueError):
            _auto_sub_stage_peak_prediction(
                sweep_profile=PROFILE, sample_rate=RATE, channel="left")

    def test_layout_requires_fingerprint_and_bounded_output_gain(self) -> None:
        outputs = [flat_output("main_l", [{"input": 0, "gain": 1.0}])]
        with self.assertRaises(ValueError):
            _auto_sub_stage_peak_prediction(**layout_args(outputs, plan_fingerprint=""))
        for bad_gain in (0.5, -80.5, float("nan"), float("inf"), "loud"):
            with self.subTest(bad_gain=bad_gain), self.assertRaises(ValueError):
                _auto_sub_stage_peak_prediction(**layout_args(outputs, output_gain_db=bad_gain))
        with self.assertRaises(ValueError):
            _auto_sub_stage_peak_prediction(**layout_args([]))

    def test_mono_sum_route_halves_single_sided_sweep(self) -> None:
        outputs = [flat_output("main_l", [{"input": 0, "gain": 1.0}]),
                   flat_output("sub1", [{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}])]
        result = _auto_sub_stage_peak_prediction(**layout_args(outputs))
        self.assertEqual(set(result["linear"]), {"output_1", "output_2"})
        self.assertAlmostEqual(
            result["linear"]["output_2"] / result["linear"]["output_1"], 0.5, places=9)

    def test_stereo_sweep_drives_both_sum_inputs(self) -> None:
        outputs = [flat_output("main_l", [{"input": 0, "gain": 1.0}]),
                   flat_output("sub1", [{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}])]
        result = _auto_sub_stage_peak_prediction(**layout_args(outputs, channel="stereo"))
        self.assertAlmostEqual(
            result["linear"]["output_2"] / result["linear"]["output_1"], 1.0, places=9)

    def test_crossover_lowpass_stays_below_flat_main(self) -> None:
        outputs = [flat_output("main_l", [{"input": 0, "gain": 1.0}]),
                   flat_output("sub1", [{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}],
                               sos=lowpass_sos())]
        result = _auto_sub_stage_peak_prediction(**layout_args(outputs))
        self.assertLess(result["linear"]["output_2"], result["linear"]["output_1"])
        self.assertGreater(result["linear"]["output_2"], 0.0)

    def test_bank_peq_moves_peak_in_configured_direction(self) -> None:
        bell = {"type": "bell", "frequency_hz": 100.0, "q": 1.0, "gain_db": 6.0, "stages": 1}
        boosted = _auto_sub_stage_peak_prediction(**layout_args(
            [flat_output("main_l", [{"input": 0, "gain": 1.0}]),
             flat_output("main_r", [{"input": 1, "gain": 1.0}], filters=[bell])],
            channel="stereo"))
        self.assertGreater(boosted["linear"]["output_2"], boosted["linear"]["output_1"])
        cut = dict(bell, gain_db=-6.0)
        attenuated = _auto_sub_stage_peak_prediction(**layout_args(
            [flat_output("main_l", [{"input": 0, "gain": 1.0}]),
             flat_output("main_r", [{"input": 1, "gain": 1.0}], filters=[cut])],
            channel="stereo"))
        self.assertLess(attenuated["linear"]["output_2"], attenuated["linear"]["output_1"])

    def test_trim_gain_scales_exactly_delay_and_invert_preserve_peak(self) -> None:
        reference = _auto_sub_stage_peak_prediction(**layout_args(
            [flat_output("main_l", [{"input": 0, "gain": 1.0}])]))
        gained = _auto_sub_stage_peak_prediction(**layout_args(
            [flat_output("main_l", [{"input": 0, "gain": 1.0}], gain_db=6.0206)]))
        self.assertAlmostEqual(
            gained["linear"]["output_1"] / reference["linear"]["output_1"], 2.0, places=6)
        shifted = _auto_sub_stage_peak_prediction(**layout_args(
            [flat_output("main_l", [{"input": 0, "gain": 1.0}], delay_ms=10.0, invert=True)]))
        self.assertAlmostEqual(
            shifted["linear"]["output_1"], reference["linear"]["output_1"], places=9)

    def test_output_gain_db_scales_every_output(self) -> None:
        outputs = [flat_output("main_l", [{"input": 0, "gain": 1.0}]),
                   flat_output("main_r", [{"input": 1, "gain": 1.0}])]
        loud = _auto_sub_stage_peak_prediction(
            **layout_args(outputs, channel="stereo", output_gain_db=0.0))
        quiet = _auto_sub_stage_peak_prediction(
            **layout_args(outputs, channel="stereo", output_gain_db=-6.0206))
        for key in ("output_1", "output_2"):
            self.assertAlmostEqual(quiet["linear"][key] / loud["linear"][key], 0.5, places=6)

    def test_unknown_route_input_and_malformed_sections_rejected(self) -> None:
        good = flat_output("main_l", [{"input": 0, "gain": 1.0}])
        bad_cases = [
            [flat_output("main_l", [{"input": 2, "gain": 1.0}])],
            [flat_output("main_l", [])],
            [flat_output("main_l", [{"input": 0, "gain": float("nan")}])],
            [dict(good, filters=[{"type": "comb", "frequency_hz": 100.0, "q": 1.0,
                                   "gain_db": 0.0, "stages": 1}])],
            [dict(good, filters=[{"type": "bell", "frequency_hz": 30000.0, "q": 1.0,
                                   "gain_db": 0.0, "stages": 1}])],
            [dict(good, filters=[{"type": "bell", "frequency_hz": 100.0, "q": 1.0,
                                   "gain_db": 0.0, "stages": 2.5}])],
            [dict(good, sos=[[0.5, 0.25, 0.125, 0.0625]])],
            [dict(good, sos=[[0.5, 0.25, 0.125, 0.0, 2.0]])],
        ]
        for outputs in bad_cases:
            with self.subTest(outputs=outputs), self.assertRaises(ValueError):
                _auto_sub_stage_peak_prediction(**layout_args(outputs))

    def test_convolver_applies_ir_and_gains(self) -> None:
        ir = self.write_ir(taps=(0.5,))
        oconv = {"path": ir, "channel": 0, "wet_db": 0.0, "dry_db": -100.0,
                 "input_gain_db": 0.0, "output_gain_db": 0.0}
        routes = [{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}]
        outputs = [flat_output("main_l", routes),
                   flat_output("sub1", routes, oconv=oconv)]
        result = _auto_sub_stage_peak_prediction(**layout_args(outputs))
        self.assertAlmostEqual(
            result["linear"]["output_2"] / result["linear"]["output_1"], 0.5, places=3)
        wet_half = dict(oconv, wet_db=-6.0206)
        quieter = _auto_sub_stage_peak_prediction(**layout_args(
            [flat_output("sub1", routes, oconv=wet_half)]))
        self.assertAlmostEqual(
            quieter["linear"]["output_1"] / result["linear"]["output_2"], 0.5, places=3)

    def test_convolver_rejects_missing_file_and_bad_channel(self) -> None:
        template = {"path": str(self.ir_dir / "missing.wav"), "channel": 0, "wet_db": 0.0,
                    "dry_db": -100.0, "input_gain_db": 0.0, "output_gain_db": 0.0}
        with self.assertRaises(ValueError):
            _auto_sub_stage_peak_prediction(**layout_args(
                [flat_output("sub1", [{"input": 0, "gain": 1.0}], oconv=template)]))
        stereo_ir = self.write_ir(name="stereo.wav", taps=((0.5, -0.25),), channels=2)
        bad_channel = dict(template, path=stereo_ir, channel=5)
        with self.assertRaises(ValueError):
            _auto_sub_stage_peak_prediction(**layout_args(
                [flat_output("sub1", [{"input": 0, "gain": 1.0}], oconv=bad_channel)]))

    def test_rewritten_ir_recomputes_instead_of_serving_stale_cache(self) -> None:
        ir = self.write_ir(name="rewrite.wav", taps=(0.5,))
        oconv = {"path": ir, "channel": 0, "wet_db": 0.0, "dry_db": -100.0,
                 "input_gain_db": 0.0, "output_gain_db": 0.0}
        outputs = [flat_output("sub1", [{"input": 0, "gain": 1.0}], oconv=oconv)]
        first = _auto_sub_stage_peak_prediction(**layout_args(outputs))
        Path(ir).write_bytes(build_wav_bytes(1, RATE, 32, 3, struct.pack("<1f", 0.25)))
        os.utime(ir, ns=(0, 1))
        second = _auto_sub_stage_peak_prediction(**layout_args(outputs))
        self.assertAlmostEqual(
            second["linear"]["output_1"] / first["linear"]["output_1"], 0.5, places=3)

    def test_cache_key_covers_fingerprint_layout_and_gains(self) -> None:
        outputs = [flat_output("main_l", [{"input": 0, "gain": 1.0}]),
                   flat_output("sub1", [{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}],
                               sos=lowpass_sos())]
        first = _auto_sub_stage_peak_prediction(**layout_args(outputs))
        key = autosub_jobs._AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_KEY
        self.assertIsNotNone(key)
        self.assertIn("fp-test", key)
        second = _auto_sub_stage_peak_prediction(**layout_args(outputs))
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first["linear"]["output_1"] = 999.0
        third = _auto_sub_stage_peak_prediction(**layout_args(outputs))
        self.assertNotEqual(third["linear"]["output_1"], 999.0)
        for variant in ({"plan_fingerprint": "fp-other"},
                        {"output_gain_db": -3.0},
                        {"sink_gain": 0.5},
                        {"channel": "stereo"}):
            with self.subTest(variant=variant):
                _auto_sub_stage_peak_prediction(**layout_args(outputs, **variant))
                self.assertNotEqual(autosub_jobs._AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_KEY, key)
                key = autosub_jobs._AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_KEY
        hotter = copy.deepcopy(outputs)
        hotter[0]["gain_db"] = 3.0
        _auto_sub_stage_peak_prediction(**layout_args(hotter))
        self.assertNotEqual(autosub_jobs._AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_KEY, key)

    def test_zero_sub_peaks_on_layout_prediction(self) -> None:
        outputs = [flat_output("main_l", [{"input": 0, "gain": 1.0}]),
                   flat_output("main_r", [{"input": 1, "gain": 1.0}]),
                   flat_output("sub1", [{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}])]
        predicted = _auto_sub_stage_peak_prediction(
            **layout_args(outputs, channel="stereo"))
        muted = _auto_sub_zero_sub_peaks(predicted, sub_mute_indices(0b100))
        self.assertEqual(muted["linear"]["output_3"], 0.0)
        self.assertEqual(muted["dbfs"]["output_3"], -240.0)
        self.assertEqual(muted["linear"]["output_1"], predicted["linear"]["output_1"])
        self.assertEqual(muted["maximum_dbfs"], max(muted["dbfs"].values()))
        with self.assertRaises(ValueError):
            _auto_sub_zero_sub_peaks(predicted, sub_mute_indices(0b1000))

    def test_comparison_fails_closed_on_output_set_mismatch(self) -> None:
        predicted = _auto_sub_stage_peak_prediction(**layout_args(
            [flat_output("main_l", [{"input": 0, "gain": 1.0}]),
             flat_output("sub1", [{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}])]))
        measured = {"output_1": 0.1, "output_2": 0.1, "output_3": 0.1}
        with self.assertRaises(ValueError):
            _auto_sub_stage_peak_comparison(predicted, measured, sink_gain=1.0)
        matching = {key: value for key, value in
                    (("output_1", 0.1), ("output_2", 0.05))}
        comparison = _auto_sub_stage_peak_comparison(predicted, matching, sink_gain=1.0)
        self.assertIn("relevant_mismatch", comparison)

    def test_biquad_matches_the_numpy_scalar_recurrence_bit_for_bit(self) -> None:
        # The list-of-floats rewrite of the per-sample loop is a pure speedup;
        # the predictor's verdicts must not move by a single ULP.
        coefficients = tuple(float(value) for value in lowpass_sos(80.0)[0])
        values = np.sin(np.arange(4096) * 0.017) * 0.7
        expected = np.empty_like(values)
        b0, b1, b2, a1, a2 = coefficients
        z1 = 0.0
        z2 = 0.0
        for index, value in enumerate(values):
            filtered = b0 * value + z1
            z1 = b1 * value - a1 * filtered + z2
            z2 = b2 * value - a2 * filtered
            expected[index] = filtered
        actual = autosub_jobs._auto_sub_run_plan_biquad(values, coefficients)
        self.assertTrue(np.array_equal(actual, expected))
        self.assertEqual(actual.dtype, expected.dtype)

    def test_undriven_outputs_skip_filtering_and_keep_their_zero_peak(self) -> None:
        outputs = [
            flat_output("left_low", [{"input": 0, "gain": 1.0}], sos=lowpass_sos(80.0)),
            flat_output("left_high", [{"input": 0, "gain": 1.0}], sos=lowpass_sos(80.0)),
            flat_output("right_low", [{"input": 1, "gain": 1.0}], sos=lowpass_sos(80.0)),
            flat_output("right_high", [{"input": 1, "gain": 1.0}], sos=lowpass_sos(80.0)),
            flat_output("sub1", [{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}],
                        sos=lowpass_sos(80.0)),
        ]
        driven_biquads = sum(len(entry["sos"]) for entry in outputs[:2]) \
            + sum(len(entry["sos"]) for entry in outputs[4:])
        total_biquads = sum(len(entry["sos"]) for entry in outputs)
        self.assertLess(driven_biquads, total_biquads)

        calls = []
        original = autosub_jobs._auto_sub_run_plan_biquad

        def counting(values, coefficients):
            calls.append(1)
            return original(values, coefficients)

        with patch.object(autosub_jobs, "_auto_sub_run_plan_biquad", counting):
            left = _auto_sub_stage_peak_prediction(**layout_args(outputs, channel="left"))
        self.assertEqual(len(calls), driven_biquads)
        # The right-hand outputs are filtered from a silent input on a
        # single-sided sweep, so their peak stays exactly zero either way.
        self.assertEqual(left["linear"]["output_3"], 0.0)
        self.assertEqual(left["linear"]["output_4"], 0.0)
        self.assertEqual(left["dbfs"]["output_3"], -240.0)
        self.assertEqual(left["dbfs"]["output_4"], -240.0)
        self.assertEqual(left["model"], "compiled-layout-v1")

        calls.clear()
        with patch.object(autosub_jobs, "_auto_sub_run_plan_biquad", counting):
            right = _auto_sub_stage_peak_prediction(**layout_args(outputs, channel="right"))
        self.assertEqual(len(calls), driven_biquads)
        self.assertEqual(right["linear"]["output_1"], 0.0)
        self.assertEqual(right["linear"]["output_2"], 0.0)
        self.assertGreater(right["linear"]["output_3"], 0.0)
        self.assertGreater(right["linear"]["output_4"], 0.0)

        calls.clear()
        with patch.object(autosub_jobs, "_auto_sub_run_plan_biquad", counting):
            stereo = _auto_sub_stage_peak_prediction(**layout_args(outputs, channel="stereo"))
        self.assertEqual(len(calls), total_biquads)
        # A stereo sweep drives both inputs, so every single-input output must
        # predict exactly what its own single-sided sweep predicted.
        for key, source in (("output_1", left), ("output_2", left),
                            ("output_3", right), ("output_4", right)):
            self.assertEqual(stereo["linear"][key], source["linear"][key])
            self.assertGreater(stereo["linear"][key], 0.0)

    def test_zero_gain_route_counts_as_silent(self) -> None:
        # A zero-gain route contributes nothing even when its input is driven,
        # so it must not pay for a biquad pass either.
        outputs = [
            flat_output("left", [{"input": 0, "gain": 0.0}], sos=lowpass_sos(80.0)),
            flat_output("right", [{"input": 1, "gain": 1.0}], sos=lowpass_sos(80.0)),
        ]
        original = autosub_jobs._auto_sub_run_plan_biquad
        calls = []

        def counting(values, coefficients):
            calls.append(1)
            return original(values, coefficients)

        with patch.object(autosub_jobs, "_auto_sub_run_plan_biquad", counting):
            predicted = _auto_sub_stage_peak_prediction(
                **layout_args(outputs, channel="stereo"))
        self.assertEqual(len(calls), len(outputs[1]["sos"]))
        self.assertEqual(predicted["linear"]["output_1"], 0.0)
        self.assertEqual(predicted["dbfs"]["output_1"], -240.0)
        self.assertGreater(predicted["linear"]["output_2"], 0.0)

    def test_real_compiled_layout_predicts_one_peak_per_output(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="plan-peak-service-")
        self.addCleanup(directory.cleanup)
        manager = DSPManager(home=Path(directory.name) / "dsp")
        service = OutputService(OutputServiceDeps(
            store=OutputStateStore(Path(directory.name) / "output-state.json"),
            preset_loader=manager.preset_store.read,
            resolve_ir=lambda name: (_ for _ in ()).throw(AssertionError(name)),
            measurement_active=lambda: False))
        from audio.output_state import switch_mode
        state = switch_mode(set_mode_routing(default_output_state(), "stereo-sub", "dev", ["main_l", "main_r", "sub1", "sub1"]), "stereo-sub")
        committed = service.commit(state, expected_revision=0)
        plan = service.compile_plan(committed, output_key="dev", channels=4,
                                    sample_rate_hz=RATE)
        layout = service.compile_layout(plan)
        fingerprint = service.fingerprint_plan(plan)
        result = _auto_sub_stage_peak_prediction(
            sweep_profile=PROFILE, sample_rate=RATE, channel="left",
            layout=layout, plan_fingerprint=fingerprint,
            output_gain_db=0.0, playback_gain=1.0, sink_gain=1.0)
        self.assertEqual(set(result["linear"]), {"output_1", "output_2", "output_3"})
        for value in result["linear"].values():
            self.assertTrue(math.isfinite(value) and value >= 0.0)
        self.assertTrue(result["safe"])
        self.assertEqual(result["plan_fingerprint"], fingerprint)


class PlanPeakPredictionOffloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        autosub_jobs._AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_KEY = None
        autosub_jobs._AUTO_SUB_PLAN_PEAK_PREDICTION_CACHE_RESULT = None

    async def test_layout_path_runs_in_a_worker_thread(self) -> None:
        main_thread = threading.get_ident()
        executed_in: list[int] = []

        def fake_prediction(**kwargs):
            executed_in.append(threading.get_ident())
            return {"linear": {"output_1": 0.5}, "safe": True}

        with patch("measurement.autosub.jobs._auto_sub_stage_peak_prediction",
                   side_effect=fake_prediction):
            result = await _predict_auto_sub_stage_peaks(**layout_args(
                [flat_output("main_l", [{"input": 0, "gain": 1.0}])]))
        self.assertEqual(result, {"linear": {"output_1": 0.5}, "safe": True})
        self.assertEqual(len(executed_in), 1)
        self.assertNotEqual(executed_in[0], main_thread)


if __name__ == "__main__":
    unittest.main(verbosity=2)
