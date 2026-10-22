#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Polarity Check guard: the rendered DSP path of a unit against the detector's measured band."""

import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import FILTER_SLOPES
from dsp.crossover import design_crossover
from dsp.native_model import native_peq_coefficients
from measurement import polarity, polarity_guard as guard

RATE = 48_000
RATES = (44_100, 48_000, 88_200, 96_000, 192_000)


def sections(kind, family, slope, frequency, rate=RATE):
    spec = {"kind": kind, "family": family, "slope_db_oct": slope, "frequency_hz": frequency}
    return [[b0, b1, b2, a1, a2] for b0, b1, b2, _, a1, a2 in design_crossover(spec, rate)]


def output(role, *, source=0, sos=(), filters=(), gain_db=0.0, delay_ms=0.0, invert=False, oconv=None,
           route_gain=1.0, positions=False, **extra):
    entry = {"name": role, "routes": [{"input": source, "gain": route_gain}], "gain_db": gain_db,
             "delay_ms": delay_ms, "invert": invert, "filters": list(filters), "sos": [list(s) for s in sos],
             "oconv": oconv, **extra}
    if positions:
        entry["input_positions"] = ["FL", "FR", "FC", "LFE", "SL", "SR", "BL", "BR"]
    return entry


def config(*outputs, chain=()):
    return {"outputs": list(outputs), "chain": list(chain)}


def unit(*roles, channel="left"):
    return {"roles": list(roles), "channel": channel, "label": channel}


def band(kind, frequency, gain=0.0, q=1.0, **extra):
    return {"enabled": True, "filterType": kind, "frequencyHz": frequency, "gainDb": gain, "q": q, **extra}


def equalizer(*bands, mode="IIR", dual=None, group=None, plugin_id="eq"):
    params = {"channelMode": "stereo-linked", "eqMode": mode, "bands": list(bands)}
    if dual:
        params.update(channelMode="dual", leftBands=list(dual[0]), rightBands=list(dual[1]))
    plugin = {"id": plugin_id, "type": "equalizer", "enabled": True, "params": params}
    if group:
        plugin["group"] = group
    return plugin


def stage(kind, **extra):
    return {"id": f"global-{kind}", "type": kind, "enabled": True, "params": {}, **extra}


def assess(cfg, subject=None, rate=RATE):
    return guard.assess_unit(cfg, subject or unit("main_l"), sample_rate_hz=rate)


def write_wav(path, frames, rate):
    """A float32 IEEE WAV of a (frames,) or (frames, channels) array."""
    frames = np.asarray(frames, dtype="<f4")
    frames = frames.reshape(frames.shape[0], -1)
    data = frames.tobytes()
    block = frames.shape[1] * 4
    fmt = struct.pack("<HHIIHH", 3, frames.shape[1], rate, rate * block, block, 32)
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data
    Path(path).write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return str(path)


def phase_swing_ir(rate, *, swing=3.0, latency_ms=20.0, taps=16384):
    """An all-pass IR whose phase swings by `swing` radians around 8 kHz, shifted by a fixed latency.

    The continuous phase spec is the same at every rate, so the same IR stored at two rates is the same filter.
    """
    frequency = np.arange(taps // 2 + 1) * rate / taps
    spectrum = np.exp(1j * swing * np.exp(-0.5 * ((frequency - 8000.0) / 2500.0) ** 2))
    return np.roll(np.fft.irfft(spectrum, taps), int(round(latency_ms / 1000.0 * rate)))


def dirac_ir(rate, latency_ms=0.0, taps=16384):
    ir = np.zeros(taps)
    ir[int(round(latency_ms / 1000.0 * rate))] = 1.0
    return ir


def detector_cos(ir, rate=RATE):
    padded = np.concatenate((np.zeros(2400), ir, np.zeros(2 * rate)))
    return polarity.analyze_ir_polarity(padded, rate, int(np.argmax(np.abs(padded))))["phase_cos"]


def lowpass_filter(values, coefficients):
    """Direct-form biquad over a signal, independent of the guard's frequency-domain model."""
    b0, b1, b2, a1, a2 = coefficients
    z1 = z2 = 0.0
    result = []
    for value in values:
        out = b0 * value + z1
        z1 = b1 * value - a1 * out + z2
        z2 = b2 * value - a2 * out
        result.append(out)
    return np.array(result)


class NormalSetupsAreNeverWithheldTest(unittest.TestCase):
    def test_a_bass_management_high_pass_never_fires_on_any_family_slope_or_rate(self):
        lowest = 1.0
        for rate in RATES:
            frequencies = (40.0, 80.0, 120.0, 160.0, 200.0) if rate == RATE else (40.0, 200.0)
            for family, slopes in FILTER_SLOPES.items():
                for slope in slopes:
                    for frequency in frequencies:
                        with self.subTest(rate=rate, family=family, slope=slope, frequency=frequency):
                            cfg = config(output("main_l", sos=sections("highpass", family, slope, frequency, rate)))
                            result = assess(cfg, rate=rate)
                            self.assertEqual(result["state"], guard.OK, result)
                            lowest = min(lowest, abs(result["dsp_cos"]))
        self.assertGreaterEqual(lowest, guard.GUARD_MIN_COS)

    def test_no_filter_at_all_reads_a_clean_positive_peak(self):
        self.assertEqual(assess(config(output("main_l"))), {"state": "ok", "dsp_cos": 1.0, "stage": ""})

    def test_a_room_correction_peq_in_the_bass_range_is_ok_in_the_area_bank_and_in_global(self):
        bands = [band("bell", frequency, gain, 2.0) for frequency, gain in
                 ((60, 4), (90, -3), (130, 5), (180, -4), (240, 3), (300, -2), (350, 2), (400, -3))]
        area = [{"type": "bell", "frequency_hz": b["frequencyHz"], "q": b["q"], "gain_db": b["gainDb"], "stages": 1}
                for b in bands]
        for label, cfg in (("area bank", config(output("main_l", filters=area))),
                           ("global", config(output("main_l"), chain=[equalizer(*bands)]))):
            with self.subTest(label):
                self.assertEqual(assess(cfg)["state"], guard.OK)


class RotatingPathsAreWithheldTest(unittest.TestCase):
    def test_a_crossover_high_pass_in_the_band_withholds_the_verdict_and_names_the_stage(self):
        expectations = (("linkwitz-riley", 24, 2500.0, guard.ROTATES), ("butterworth", 12, 5000.0, guard.ROTATES),
                        ("linkwitz-riley", 48, 1600.0, guard.ROTATES), ("linkwitz-riley", 48, 2500.0, guard.ROTATES),
                        ("linkwitz-riley", 24, 1200.0, guard.OK), ("butterworth", 12, 2500.0, guard.OK))
        for family, slope, frequency, expected in expectations:
            with self.subTest(family=family, slope=slope, frequency=frequency):
                cfg = config(output("left_high", sos=sections("highpass", family, slope, frequency)))
                result = assess(cfg, unit("left_high"))
                self.assertEqual(result["state"], expected, result)
                self.assertEqual(result["stage"], "left_high crossover" if expected == guard.ROTATES else "")

    def test_an_active_two_way_crossover_is_judged_as_the_sum_of_its_ways(self):
        for frequency, expected in ((500.0, guard.OK), (1200.0, guard.OK), (2500.0, guard.ROTATES),
                                    (3500.0, guard.ROTATES)):
            with self.subTest(frequency=frequency):
                cfg = config(
                    output("left_low", sos=sections("lowpass", "linkwitz-riley", 24, frequency)),
                    output("left_high", sos=sections("highpass", "linkwitz-riley", 24, frequency)))
                self.assertEqual(assess(cfg, unit("left_low", "left_high"))["state"], expected)

    def test_a_peq_band_with_a_strong_phase_swing_in_the_band_is_withheld(self):
        cfg = config(output("main_l"), chain=[equalizer(band("bell", 10_000.0, 12.0, 1.0))])
        result = assess(cfg)
        self.assertEqual((result["state"], result["stage"]), (guard.ROTATES, "global equalizer"))
        self.assertAlmostEqual(result["dsp_cos"], 0.252, places=2)

    def test_bands_that_do_not_change_the_phase_are_ignored(self):
        flat = [band("bell", 10_000.0, 0.0), band("high_shelf", 8000.0, 0.0), band("low_shelf", 9000.0, 0.0),
                band("gain", 1000.0, 6.0), band("delay", 1000.0, 0.0, delayMs=3.0),
                band("bell", 10_000.0, 12.0, 1.0, enabled=False)]
        self.assertEqual(assess(config(output("main_l"), chain=[equalizer(*flat)])), {"state": "ok", "dsp_cos": 1.0, "stage": ""})

    def test_a_high_shelf_and_a_bell_in_the_band_stay_readable(self):
        for bands in ([band("high_shelf", 8000.0, 6.0, 0.7)], [band("bell", 6000.0, 6.0, 2.0)],
                      [band("high_shelf", 4000.0, -6.0, 0.7)]):
            with self.subTest(bands=bands):
                self.assertEqual(assess(config(output("main_l"), chain=[equalizer(*bands)]))["state"], guard.OK)


class StagesOutsideTheAllowListAreUnknownTest(unittest.TestCase):
    def test_gain_and_delay_stages_do_not_touch_the_phase(self):
        chain = [stage("limiter"), stage("headroom"), stage("autogain"), stage("delay")]
        self.assertEqual(assess(config(output("main_l"), chain=chain))["state"], guard.OK)

    def test_every_active_stage_without_phase_evidence_makes_the_path_unknown(self):
        for kind, name in (("loudness", "global loudness"), ("bass_enhancer", "global bass enhancer"),
                           ("crystalizer", "global crystalizer"), ("maximizer", "global maximizer"),
                           ("upmixer", "global upmixer"), ("mystery_plugin", "global mystery plugin")):
            with self.subTest(kind=kind):
                result = assess(config(output("main_l"), chain=[stage(kind)]))
                self.assertEqual(result, {"state": "unknown", "dsp_cos": None, "stage": name})

    def test_a_disabled_stage_is_ignored(self):
        chain = [stage("loudness", enabled=False)]
        self.assertEqual(assess(config(output("main_l"), chain=chain))["state"], guard.OK)

    def test_non_iir_peq_is_unknown_only_with_an_active_band(self):
        for mode in ("FIR", "FFT", "SPM"):
            with self.subTest(mode=mode):
                neutral = equalizer(band("bell", 1000.0, 0.0), band("gain", 1000.0, 3.0), mode=mode)
                active = equalizer(band("bell", 1000.0, 3.0), mode=mode)
                self.assertEqual(assess(config(output("main_l"), chain=[neutral]))["state"], guard.OK)
                self.assertEqual(assess(config(output("main_l"), chain=[active])),
                                 {"state": "unknown", "dsp_cos": None, "stage": "global equalizer"})

    def test_a_band_type_the_engine_does_not_render_is_unknown(self):
        result = assess(config(output("main_l"), chain=[equalizer(band("pk", 1000.0, 3.0))]))
        self.assertEqual((result["state"], result["stage"]), (guard.UNKNOWN, "global equalizer"))

    def test_a_band_beyond_nyquist_is_unknown(self):
        result = assess(config(output("main_l"), chain=[equalizer(band("bell", 30_000.0, 3.0))]))
        self.assertEqual(result["state"], guard.UNKNOWN)

    def test_a_malformed_path_is_unknown_and_never_raises(self):
        cases = ((config(output("main_l")), unit("ghost")),
                 (config({"name": "main_l", "routes": []}), unit("main_l")),
                 (config(output("main_l", sos=[[1.0, 2.0]])), unit("main_l")),
                 (config(output("main_l"), chain=[equalizer(band("bell", "x", 3.0))]), unit("main_l")),
                 ({"outputs": [{"name": "main_l"}]}, unit("main_l")))
        with self.assertLogs(guard.logger, "WARNING"):
            for cfg, subject in cases:
                with self.subTest(cfg=cfg):
                    self.assertEqual(assess(cfg, subject)["state"], guard.UNKNOWN)

    def test_a_bass_bus_role_is_unknown(self):
        cfg = config(output("main_l", post_crossover_routes=[{"input": 3, "gain": 1.0}]))
        self.assertEqual(assess(cfg), {"state": "unknown", "dsp_cos": None, "stage": "main_l bass bus"})


class InversionAndRoleRatioTest(unittest.TestCase):
    def test_an_inverted_output_reads_the_mirrored_phase_and_stays_ok(self):
        normal = assess(config(output("left_high", sos=sections("highpass", "linkwitz-riley", 24, 1200.0))), unit("left_high"))
        inverted = assess(config(output("left_high", sos=sections("highpass", "linkwitz-riley", 24, 1200.0), invert=True)), unit("left_high"))
        self.assertEqual((normal["state"], inverted["state"]), (guard.OK, guard.OK))
        self.assertAlmostEqual(inverted["dsp_cos"], -normal["dsp_cos"], places=3)

    def test_a_correctly_wired_two_way_with_one_inverted_way_is_not_a_false_alarm(self):
        for invert, sign in ((False, 1), (True, -1)):
            with self.subTest(invert=invert):
                cfg = config(
                    output("left_low", sos=sections("lowpass", "butterworth", 12, 1000.0)),
                    output("left_high", sos=sections("highpass", "butterworth", 12, 1000.0), invert=invert))
                result = assess(cfg, unit("left_low", "left_high"))
                self.assertEqual(result["state"], guard.OK, result)
                self.assertGreater(sign * result["dsp_cos"], 0.9)

    def test_level_and_delay_do_not_rotate_the_band(self):
        cfg = config(output("main_l", gain_db=-9.0, delay_ms=12.5, route_gain=0.5))
        self.assertEqual(assess(cfg)["state"], guard.OK)

    def test_the_delay_is_rounded_to_whole_samples_like_the_engine(self):
        # 0.0105 ms is half a sample at 48 kHz: the engine rounds it up to one sample, which the model must too.
        cfg_a = config(output("main_l", delay_ms=0.0105), output("main_r", delay_ms=0.0))
        exact = config(output("main_l", delay_ms=1000 / RATE), output("main_r", delay_ms=0.0))
        self.assertEqual(assess(cfg_a, unit("main_l", "main_r")), assess(exact, unit("main_l", "main_r")))


class SurroundGroupsTest(unittest.TestCase):
    HARMFUL = (band("bell", 10_000.0, 12.0, 1.0),)

    def surround(self, role, source, chain):
        return config(output(role, source=source, positions=True), chain=chain)

    def test_a_global_plugin_of_another_speaker_group_is_ignored(self):
        cfg = self.surround("rear_l", 6, [equalizer(*self.HARMFUL, group="front")])
        self.assertEqual(assess(cfg, unit("rear_l"))["state"], guard.OK)

    def test_a_global_plugin_of_the_units_own_group_is_modelled(self):
        cfg = self.surround("rear_l", 6, [equalizer(*self.HARMFUL, group="rear")])
        result = assess(cfg, unit("rear_l"))
        self.assertEqual((result["state"], result["stage"]), (guard.ROTATES, "global equalizer"))

    def test_an_ungrouped_plugin_reaches_every_group(self):
        cfg = self.surround("side_l", 4, [equalizer(*self.HARMFUL)])
        self.assertEqual(assess(cfg, unit("side_l"))["state"], guard.ROTATES)

    def test_a_dual_peq_acts_per_side_of_the_driven_input(self):
        dual = equalizer(dual=([], list(self.HARMFUL)), group="rear")
        self.assertEqual(assess(self.surround("rear_l", 6, [dual]), unit("rear_l"))["state"], guard.OK)
        self.assertEqual(assess(self.surround("rear_r", 7, [dual]), unit("rear_r"))["state"], guard.ROTATES)

    def test_the_centre_has_no_named_side_so_the_worse_side_counts(self):
        left_harmful = equalizer(dual=(list(self.HARMFUL), []), group="center")
        right_harmful = equalizer(dual=([], list(self.HARMFUL)), group="center")
        for plugin in (left_harmful, right_harmful):
            with self.subTest(plugin=plugin["params"]["channelMode"]):
                self.assertEqual(assess(self.surround("center", 2, [plugin]), unit("center"))["state"], guard.ROTATES)

    def test_a_group_crossover_is_read_from_the_rendered_role(self):
        cfg = config(output("rear_l", source=6, positions=True, sos=sections("highpass", "linkwitz-riley", 24, 2500.0)))
        self.assertEqual(assess(cfg, unit("rear_l")), {"state": "rotates", "dsp_cos": -0.505, "stage": "rear_l crossover"})

    def test_a_stereo_pair_input_selects_its_side_of_a_dual_peq(self):
        dual = equalizer(dual=(list(self.HARMFUL), []))
        left = config(output("main_l", source=0), chain=[dual])
        right = config(output("main_r", source=1), chain=[dual])
        self.assertEqual(assess(left, unit("main_l"))["state"], guard.ROTATES)
        self.assertEqual(assess(right, unit("main_r"))["state"], guard.OK)


class ConvolverTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def wav(self, name, frames, rate=RATE):
        return write_wav(self.directory / name, frames, rate)

    def oconv(self, path, *, channel=0, wet_db=0.0, dry_db=-100.0, input_gain_db=0.0, output_gain_db=0.0):
        return {"path": path, "channel": channel, "wet_db": wet_db, "dry_db": dry_db,
                "input_gain_db": input_gain_db, "output_gain_db": output_gain_db}

    def global_convolver(self, kernel="room.wav", **params):
        return {"id": "conv", "type": "convolver", "enabled": True, "params": {"kernel": kernel, **params}}

    def assess_global(self, chain, kernels, subject=None, rate=RATE, outputs=None):
        return guard.assess_unit(config(*(outputs or [output("main_l")]), chain=chain), subject or unit("main_l"),
                                 sample_rate_hz=rate, kernel_path=lambda kernel: kernels[kernel])

    def test_a_flat_phase_ir_is_ok_in_the_area_bank_and_in_global(self):
        correction = dirac_ir(RATE, taps=8192)
        for coefficients in (native_peq_coefficients("bell", 80.0, RATE, 2.0, 6.0),
                             native_peq_coefficients("bell", 200.0, RATE, 1.0, -4.0)):
            correction = lowpass_filter(correction, coefficients)
        for label, ir in (("dirac", dirac_ir(RATE)), ("delayed dirac", dirac_ir(RATE, 85.0)),
                          ("minimum phase correction", correction)):
            path = self.wav(f"{label}.wav".replace(" ", "_"), ir)
            with self.subTest(label, stage="area bank"):
                result = assess(config(output("main_l", oconv=self.oconv(path))))
                self.assertEqual(result["state"], guard.OK, result)
            with self.subTest(label, stage="global"):
                result = self.assess_global([self.global_convolver()], {"room.wav": path})
                self.assertEqual(result["state"], guard.OK, result)

    def test_a_mixed_phase_ir_that_swings_the_band_is_withheld(self):
        path = self.wav("swing.wav", phase_swing_ir(RATE))
        for stage, result, name in (
                ("area bank", assess(config(output("main_l", oconv=self.oconv(path)))), "main_l convolver swing.wav"),
                ("global", self.assess_global([self.global_convolver("swing.wav")], {"swing.wav": path}),
                 "global convolver swing.wav")):
            with self.subTest(stage):
                self.assertEqual((result["state"], result["stage"]), (guard.ROTATES, name))
                self.assertAlmostEqual(result["dsp_cos"], detector_cos(phase_swing_ir(RATE)), delta=0.01)

    def test_the_model_reads_what_the_detector_reads_behind_the_ir(self):
        for swing in (0.5, 1.5, 3.0, 4.5):
            ir = phase_swing_ir(RATE, swing=swing, latency_ms=85.0)
            path = self.wav(f"swing{swing}.wav", ir)
            with self.subTest(swing=swing):
                result = assess(config(output("main_l", oconv=self.oconv(path))))
                self.assertAlmostEqual(result["dsp_cos"], detector_cos(ir), delta=0.01)

    def test_an_ir_stored_at_another_rate_reads_like_the_same_filter_built_at_the_stream_rate(self):
        for rate in (44_100, 96_000):
            stored = self.wav(f"stored{rate}.wav", phase_swing_ir(rate, swing=2.0), rate)
            direct = self.wav(f"direct{rate}.wav", phase_swing_ir(RATE, swing=2.0))
            with self.subTest(rate=rate):
                resampled = assess(config(output("main_l", oconv=self.oconv(stored))))["dsp_cos"]
                built = assess(config(output("main_l", oconv=self.oconv(direct))))["dsp_cos"]
                self.assertLessEqual(abs(resampled - built), 0.02)

    def test_the_wet_dry_mix_decides_what_the_path_sounds_like(self):
        path = self.wav("swing.wav", phase_swing_ir(RATE))
        dry_only = assess(config(output("main_l", oconv=self.oconv(path, wet_db=-100.0, dry_db=0.0))))
        wet_only = assess(config(output("main_l", oconv=self.oconv(path, wet_db=0.0, dry_db=-100.0))))
        self.assertEqual((dry_only["state"], wet_only["state"]), (guard.OK, guard.ROTATES))
        mixed = assess(config(output("main_l", oconv=self.oconv(path, wet_db=-20.0, dry_db=0.0))))
        self.assertEqual(mixed["state"], guard.OK)

    def test_the_gains_scale_the_response_but_not_the_phase(self):
        path = self.wav("dirac.wav", dirac_ir(RATE))
        result = assess(config(output("main_l", oconv=self.oconv(path, input_gain_db=-12.0, output_gain_db=6.0))))
        self.assertEqual((result["state"], result["dsp_cos"]), (guard.OK, 1.0))

    def test_an_area_bank_convolver_uses_the_channel_it_names(self):
        path = self.wav("stereo.wav", np.stack([dirac_ir(RATE), phase_swing_ir(RATE)], axis=1))
        states = {channel: assess(config(output("main_l", oconv=self.oconv(path, channel=channel))))["state"]
                  for channel in (0, 1)}
        self.assertEqual(states, {0: guard.OK, 1: guard.ROTATES})
        self.assertEqual(assess(config(output("main_l", oconv=self.oconv(path, channel=2))))["state"], guard.UNKNOWN)

    def test_a_two_channel_global_ir_counts_with_its_worse_channel(self):
        mixed = self.wav("mixed.wav", np.stack([dirac_ir(RATE), phase_swing_ir(RATE)], axis=1))
        flat = self.wav("flat.wav", np.stack([dirac_ir(RATE), dirac_ir(RATE)], axis=1))
        self.assertEqual(self.assess_global([self.global_convolver("mixed.wav")], {"mixed.wav": mixed})["state"], guard.ROTATES)
        self.assertEqual(self.assess_global([self.global_convolver("flat.wav")], {"flat.wav": flat})["state"], guard.OK)
        mono_swing = self.wav("mono.wav", phase_swing_ir(RATE))
        self.assertEqual(self.assess_global([self.global_convolver("mono.wav")], {"mono.wav": mono_swing})["state"], guard.ROTATES)

    def test_an_ir_that_cannot_be_read_makes_the_path_unknown(self):
        junk = self.directory / "junk.wav"
        junk.write_bytes(b"not a wave file")
        eight_bit = bytearray(Path(self.wav("ok.wav", dirac_ir(RATE))).read_bytes())
        eight_bit[34:36] = struct.pack("<H", 8)
        (self.directory / "eight.wav").write_bytes(bytes(eight_bit))
        cases = {"missing": str(self.directory / "missing.wav"), "junk": str(junk), "unsupported": str(self.directory / "eight.wav")}
        for label, path in cases.items():
            with self.subTest(label, stage="area bank"):
                self.assertEqual(assess(config(output("main_l", oconv=self.oconv(path)))),
                                 {"state": "unknown", "dsp_cos": None, "stage": f"main_l convolver {Path(path).name}"})
            with self.subTest(label, stage="global"):
                self.assertEqual(self.assess_global([self.global_convolver("x.wav")], {"x.wav": path}),
                                 {"state": "unknown", "dsp_cos": None, "stage": "global convolver x.wav"})

    def test_a_global_kernel_that_does_not_resolve_makes_the_path_unknown(self):
        plugin = self.global_convolver("gone.wav")
        self.assertEqual(guard.assess_unit(config(output("main_l"), chain=[plugin]), unit("main_l"), sample_rate_hz=RATE),
                         {"state": "unknown", "dsp_cos": None, "stage": "global convolver gone.wav"})

        def resolve(kernel):
            raise FileNotFoundError(f"IR file not found: {kernel}")

        result = guard.assess_unit(config(output("main_l"), chain=[plugin]), unit("main_l"),
                                   sample_rate_hz=RATE, kernel_path=resolve)
        self.assertEqual(result["stage"], "global convolver gone.wav")

    def test_a_surround_global_convolver_acts_on_its_own_group_only(self):
        path = self.wav("swing.wav", phase_swing_ir(RATE))
        rear = output("rear_l", source=6, positions=True)
        for group, expected in (("front", guard.OK), ("rear", guard.ROTATES)):
            with self.subTest(group=group):
                plugin = {**self.global_convolver("swing.wav"), "group": group}
                result = self.assess_global([plugin], {"swing.wav": path}, unit("rear_l"), outputs=[rear])
                self.assertEqual(result["state"], expected)

    def test_a_long_high_rate_ir_is_modelled_at_the_stream_rate(self):
        ir = phase_swing_ir(192_000, swing=1.0, taps=65536)
        path = self.wav("long.wav", ir, 192_000)
        result = assess(config(output("main_l", oconv=self.oconv(path))), rate=192_000)
        self.assertAlmostEqual(result["dsp_cos"], detector_cos(ir, 192_000), delta=0.02)


class ModelMatchesTheDetectorTest(unittest.TestCase):
    """The model reads the same cosine the product detector reads behind a real filter."""

    def detector_cos(self, biquads, arrival=2400, length=RATE * 2):
        ir = np.zeros(length)
        ir[arrival] = 1.0
        for coefficients in biquads:
            ir = lowpass_filter(ir, coefficients)
        return polarity.analyze_ir_polarity(ir, RATE, int(np.argmax(np.abs(ir))))["phase_cos"]

    def test_crossover_corners_agree_with_a_time_domain_run(self):
        for frequency in (1200.0, 2500.0, 3500.0):
            with self.subTest(frequency=frequency):
                sos = sections("highpass", "linkwitz-riley", 24, frequency)
                modelled = assess(config(output("left_high", sos=sos)), unit("left_high"))["dsp_cos"]
                self.assertAlmostEqual(modelled, self.detector_cos([tuple(s) for s in sos]), delta=0.01)

    def test_peq_bands_agree_with_a_time_domain_run(self):
        for kind, frequency, gain, q in (("bell", 10_000.0, 12.0, 1.0), ("bell", 3000.0, 6.0, 1.0),
                                          ("highshelf", 8000.0, 6.0, 0.7), ("notch", 5000.0, 0.0, 5.0)):
            with self.subTest(kind=kind, frequency=frequency):
                coefficients = native_peq_coefficients(kind, frequency, RATE, q, gain)
                filters = [{"type": kind, "frequency_hz": frequency, "q": q, "gain_db": gain, "stages": 1}]
                modelled = assess(config(output("main_l", filters=filters)))["dsp_cos"]
                self.assertAlmostEqual(modelled, self.detector_cos([coefficients]), delta=0.01)

    def test_the_model_reads_with_the_detectors_own_phase_reader(self):
        ir = np.zeros(RATE * 2)
        ir[2400] = -1.0
        phase_cos, peak, tail = polarity.peak_phase(ir, RATE, 2400)
        self.assertAlmostEqual(phase_cos, polarity.analyze_ir_polarity(ir, RATE, 2400)["phase_cos"], places=3)
        self.assertLess(phase_cos, -0.99)
        self.assertGreater(peak, 0.0)
        self.assertGreater(tail.size, 0)


if __name__ == "__main__":
    unittest.main()
