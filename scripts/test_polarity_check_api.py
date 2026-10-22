#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Polarity Check: area planning and the measurement job."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import HTTPException

from dsp.crossover import design_crossover
import test_polarity_guard as fixtures

from audio.output_state import (default_output_state, set_crossover, set_mode_routing,
                                set_output_processing, switch_mode)
from measurement import polarity_check_api as api
from measurement import polarity_guard, speaker_scope, speaker_sweep
from measurement.target import freeze_measurement_target

SURROUND_ROLES = ["main_l", "main_r", "center", "side_l", "side_r", "rear_l", "rear_r"]


def mode_state(mode, roles, *, crossover=False):
    state = default_output_state()
    if crossover:
        state = set_crossover(state, mode, True)
    return switch_mode(set_mode_routing(state, mode, "A", roles), mode)


def path_config(*outputs, chain=()):
    """Engine configuration double: the rendered outputs of the units' roles."""
    return {"outputs": list(outputs), "chain": list(chain)}


def neutral_output(role, *, sos=()):
    return {"name": role, "routes": [{"input": 1 if role.endswith("_r") or role.startswith("right_") else 0, "gain": 1.0}],
            "gain_db": 0.0, "delay_ms": 0.0, "invert": False, "filters": [], "sos": list(sos), "oconv": None}


def reading(verdict, reason="", phase_cos=0.98, snr_db=60.0):
    return {"verdict": verdict, "peak_sign": -1 if verdict == "reversed" else 1, "reason": reason,
            "phase_cos": -phase_cos if verdict == "reversed" else phase_cos, "snr_db": snr_db}


class AreaPlanTests(unittest.TestCase):
    def test_a_stereo_pair_is_one_take_per_side(self):
        area = speaker_scope.plan_area("stereo", "global", ["main_l", "main_r"])
        self.assertEqual([(unit["roles"], unit["channel"]) for unit in area["units"]],
                         [(["main_l"], "left"), (["main_r"], "right")])

    def test_all_ways_of_a_side_play_together_in_the_global_area(self):
        roles = [f"{side}_{way}" for side in ("left", "right") for way in ("low", "high")]
        area = speaker_scope.plan_area("stereo", "global", roles)
        self.assertEqual([unit["roles"] for unit in area["units"]],
                         [["left_low", "left_high"], ["right_low", "right_high"]])

    def test_a_single_surround_speaker_is_a_valid_area(self):
        area = speaker_scope.plan_area("surround", "center", SURROUND_ROLES)
        self.assertEqual((area["group"], [unit["roles"] for unit in area["units"]]), ("center", [["center"]]))

    def test_subwoofers_are_excluded(self):
        roles = ["main_l", "main_r", "sub_l", "sub_r"]
        with self.assertRaises(ValueError):
            speaker_scope.plan_area("stereo-sub", "sub_l", roles)
        area = speaker_scope.plan_area("stereo-sub", "global", roles)
        self.assertEqual([unit["roles"] for unit in area["units"]], [["main_l"], ["main_r"]])


class FakeStore:
    """Measurement store double: freezes real targets and answers takes with a polarity reading per side."""

    def __init__(self, state, readings):
        self.state, self.readings = state, readings
        self.started, self.cancelled = [], []
        self.measurement_target_provider = self._target

    def _target(self, bank_id, rate):
        channels = len(self.state["modes"][self.state["active_mode"]]["routing"]["A"])
        return freeze_measurement_target(self.state, bank_id=bank_id, output_key="A", channels=channels,
                                         sample_rate_hz=rate, fingerprint="0" * 16)

    async def start_measurement(self, **kwargs):
        self.started.append(kwargs)
        return {"id": f"take-{len(self.started)}"}

    def get_job(self, job_id):
        roles = tuple(self.started[int(job_id.split("-")[1]) - 1]["frozen_target"]["measured_roles"])
        analysis = {"sample_rate": 48000}
        if self.readings[roles] is not None:
            analysis["polarity"] = self.readings[roles]
        return {"status": "completed", "result": {
            "analysis": analysis, "capture": {"microphone_node": "mic", "mic_input_channel": 1}}}

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)

    async def drain_job(self, job_id):
        pass


class JobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.auto_sub = False
        self.addCleanup(api.configure_polarity_check, None)

    def configure(self, state, readings):
        self.state = state
        self.store = FakeStore(state, readings)
        self.resolved = []
        self.path = lambda target: path_config(*(neutral_output(role) for role in target["roles"]))

        async def start_registered(rate, start_job, entry_epoch=None):
            return await start_job()

        patches = [
            patch.object(speaker_sweep.measurement_session, "_resolve_measurement_start_sample_rate", lambda: 48000),
            patch.object(speaker_sweep.measurement_session, "_start_registered_manual_measurement", start_registered),
            patch.object(speaker_sweep.measurement_session, "_stage_bank_v2_context", lambda **kwargs: {}),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        api.configure_polarity_check(api.PolarityCheckDeps(
            get_store=lambda: self.store, get_session=lambda: None,
            auto_sub_active=lambda: self.auto_sub, get_output_state=lambda: self.state,
            resolve_dsp_path=self.resolve_path))

    def resolve_path(self, target):
        self.resolved.append(target)
        return {"config": self.path(target)}

    async def start(self, bank_id):
        return await api.start_polarity_check(input_id="mic", input_key="", mic_input_channel="1",
                                              calibration_ref="", bank_id=bank_id)

    async def run_job(self, bank_id):
        await self.start(bank_id)
        await api._slot.task
        return api._slot.job

    async def test_each_side_gets_its_own_verdict(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): reading("correct"), ("main_r",): reading("reversed")})
        job = await self.run_job("main")
        self.assertEqual(job["status"], "completed", job["error"])
        rows = job["result"]["rows"]
        self.assertEqual([(row["label"], row["roles"], row["verdict"]) for row in rows],
                         [("left", ["main_l"], "correct"), ("right", ["main_r"], "reversed")])
        self.assertFalse(job["result"]["all_reversed"])
        self.assertEqual([call["channel"] for call in self.store.started], ["left", "right"])
        self.assertEqual([call["frozen_target"]["measured_roles"] for call in self.store.started],
                         [["main_l"], ["main_r"]])

    async def test_all_speakers_reversed_points_at_the_microphone(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): reading("reversed"), ("main_r",): reading("reversed")})
        job = await self.run_job("main")
        self.assertTrue(job["result"]["all_reversed"])

    async def test_a_single_reversed_speaker_proves_nothing_about_the_microphone(self):
        self.configure(mode_state("surround", SURROUND_ROLES), {("center",): reading("reversed")})
        job = await self.run_job("center")
        self.assertEqual([row["verdict"] for row in job["result"]["rows"]], ["reversed"])
        self.assertFalse(job["result"]["all_reversed"])

    async def test_the_row_reports_an_inversion_set_in_fxroute(self):
        state = set_output_processing(mode_state("stereo", ["main_l", "main_r"]), "stereo", "main_r",
                                      polarity="invert")
        self.configure(state, {("main_l",): reading("correct"), ("main_r",): reading("correct")})
        job = await self.run_job("main")
        self.assertEqual([row["inverted"] for row in job["result"]["rows"]], [False, True])

    async def test_an_inconclusive_reading_keeps_its_reason(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): reading("inconclusive", "unclear_phase", phase_cos=0.2),
                        ("main_r",): reading("correct")})
        job = await self.run_job("main")
        row = job["result"]["rows"][0]
        self.assertEqual((row["verdict"], row["reason"], row["phase_cos"]), ("inconclusive", "unclear_phase", 0.2))

    def crossover_state(self):
        roles = ["left_low", "left_high", "right_low", "right_high"]
        return mode_state("stereo", roles, crossover=True)

    def high_pass(self, role, frequency):
        spec = {"kind": "highpass", "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": frequency}
        return [[b0, b1, b2, a1, a2] for b0, b1, b2, _, a1, a2 in design_crossover(spec, 48000)]

    async def test_a_filter_in_the_measured_band_withholds_a_reversed_reading(self):
        self.configure(self.crossover_state(), {("left_high",): reading("reversed"), ("right_high",): reading("reversed")})
        self.path = lambda target: path_config(
            neutral_output("left_high", sos=self.high_pass("left_high", 2500.0)),
            neutral_output("right_high", sos=self.high_pass("right_high", 2500.0)))
        job = await self.run_job("high")
        self.assertEqual(job["status"], "completed", job["error"])
        rows = job["result"]["rows"]
        self.assertEqual([(row["verdict"], row["reason"]) for row in rows], [("inconclusive", "dsp_filter_in_band")] * 2)
        self.assertEqual([row["guard"]["state"] for row in rows], ["rotates", "rotates"])
        self.assertEqual(rows[0]["guard"]["stage"], "left_high crossover")
        self.assertEqual((rows[0]["phase_cos"], rows[0]["snr_db"]), (-0.98, 60.0))
        self.assertFalse(job["result"]["all_reversed"])

    async def test_the_same_reading_without_a_filter_stays_reversed(self):
        self.configure(self.crossover_state(), {("left_high",): reading("reversed"), ("right_high",): reading("reversed")})
        job = await self.run_job("high")
        rows = job["result"]["rows"]
        self.assertEqual([(row["verdict"], row["reason"]) for row in rows], [("reversed", "")] * 2)
        self.assertEqual(rows[0]["guard"], {"state": "ok", "dsp_cos": 1.0, "stage": ""})
        self.assertTrue(job["result"]["all_reversed"])

    async def test_a_correct_reading_behind_a_rotating_filter_is_withheld_too(self):
        self.configure(self.crossover_state(), {("left_high",): reading("correct"), ("right_high",): reading("correct")})
        self.path = lambda target: path_config(
            neutral_output("left_high", sos=self.high_pass("left_high", 3500.0)),
            neutral_output("right_high"))
        rows = (await self.run_job("high"))["result"]["rows"]
        self.assertEqual([(row["verdict"], row["reason"]) for row in rows],
                         [("inconclusive", "dsp_filter_in_band"), ("correct", "")])

    async def test_all_reversed_counts_after_the_guard(self):
        self.configure(self.crossover_state(), {("left_high",): reading("reversed"), ("right_high",): reading("reversed")})
        self.path = lambda target: path_config(
            neutral_output("left_high", sos=self.high_pass("left_high", 2500.0)), neutral_output("right_high"))
        job = await self.run_job("high")
        self.assertEqual([row["verdict"] for row in job["result"]["rows"]], ["inconclusive", "reversed"])
        self.assertFalse(job["result"]["all_reversed"])

    async def test_a_global_convolver_is_read_through_the_resolved_kernel_file(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): reading("reversed"), ("main_r",): reading("reversed")})
        with tempfile.TemporaryDirectory() as directory:
            ir = fixtures.write_wav(Path(directory) / "room.wav", fixtures.phase_swing_ir(48000), 48000)
            convolver = {"id": "conv", "type": "convolver", "enabled": True, "params": {"kernel": "room.wav"}}
            self.resolve_path = lambda target: {
                "config": path_config(neutral_output("main_l"), neutral_output("main_r"), chain=[convolver]),
                "kernel_path": lambda kernel: ir}
            api.configure_polarity_check(api.PolarityCheckDeps(
                get_store=lambda: self.store, get_session=lambda: None, auto_sub_active=lambda: False,
                get_output_state=lambda: self.state, resolve_dsp_path=self.resolve_path))
            job = await self.run_job("main")
        rows = job["result"]["rows"]
        self.assertEqual([(row["reason"], row["guard"]["stage"]) for row in rows],
                         [("dsp_filter_in_band", "global convolver room.wav")] * 2)

    async def test_an_unmodellable_stage_makes_the_reading_dsp_unknown(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): reading("correct"), ("main_r",): reading("reversed")})
        loudness = {"id": "global-loudness", "type": "loudness", "enabled": True, "params": {}}
        self.path = lambda target: path_config(neutral_output("main_l"), neutral_output("main_r"), chain=[loudness])
        job = await self.run_job("main")
        rows = job["result"]["rows"]
        self.assertEqual([(row["verdict"], row["reason"]) for row in rows], [("inconclusive", "dsp_unknown")] * 2)
        self.assertEqual(rows[0]["guard"], {"state": "unknown", "dsp_cos": None, "stage": "global loudness"})
        self.assertFalse(job["result"]["all_reversed"])

    async def test_a_path_that_changed_since_the_sweep_is_unknown_and_does_not_abort_the_job(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): reading("correct"), ("main_r",): reading("correct")})

        def changed(target):
            raise polarity_guard.PathUnavailable("DSP path changed")

        self.path = changed
        job = await self.run_job("main")
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual([(row["verdict"], row["reason"], row["guard"]["stage"]) for row in job["result"]["rows"]],
                         [("inconclusive", "dsp_unknown", "DSP path changed")] * 2)

    async def test_an_unexpected_resolver_failure_is_unknown_not_a_failed_job(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): reading("correct"), ("main_r",): reading("correct")})

        def broken(target):
            raise RuntimeError("renderer offline")

        self.path = broken
        with self.assertLogs(api.logger, "WARNING"):
            job = await self.run_job("main")
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual({row["reason"] for row in job["result"]["rows"]}, {"dsp_unknown"})
        self.assertEqual(job["result"]["rows"][0]["guard"]["stage"], "DSP path")

    async def test_the_guard_only_looks_at_readings_that_would_name_a_polarity(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): reading("inconclusive", "weak_signal", phase_cos=0.2),
                        ("main_r",): reading("inconclusive", "no_arrival")})
        job = await self.run_job("main")
        self.assertEqual(self.resolved, [])
        rows = job["result"]["rows"]
        self.assertEqual([(row["reason"], row["guard"]) for row in rows], [("weak_signal", None), ("no_arrival", None)])

    async def test_each_unit_is_judged_against_the_frozen_target_of_the_run(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): reading("correct"), ("main_r",): reading("correct")})
        await self.run_job("main")
        self.assertEqual(len(self.resolved), 2)
        self.assertTrue(all(target["sample_rate_hz"] == 48000 and target["bank_id"] == "main" for target in self.resolved))

    async def test_a_take_without_a_polarity_reading_fails_the_job(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]), {("main_l",): None, ("main_r",): None})
        job = await self.run_job("main")
        self.assertEqual(job["status"], "failed")
        self.assertIn("no polarity reading", job["error"])
        self.assertIsNone(job["result"])

    async def test_aggregate_areas_are_refused_but_a_crossover_bank_checks_its_wiring(self):
        roles = ["left_low", "left_high", "right_low", "right_high"]
        self.configure(mode_state("stereo", roles, crossover=True),
                       {("left_low",): reading("correct"), ("right_low",): reading("correct")})
        for bank in ("global", "all"):
            with self.assertRaises(HTTPException) as caught:
                await self.start(bank)
            self.assertEqual(caught.exception.status_code, 400)
        job = await self.run_job("low")
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual([row["roles"] for row in job["result"]["rows"]],
                          [["left_low"], ["right_low"]])

    async def test_plain_stereo_without_bank_selection_keeps_polarity_check(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): reading("correct"), ("main_r",): reading("correct")})
        self.assertEqual((await self.run_job("global"))["status"], "completed")

    async def test_subwoofer_banks_are_refused(self):
        self.configure(mode_state("stereo-sub", ["main_l", "main_r", "sub1", "sub2"]), {})
        with self.assertRaises(HTTPException) as caught:
            await self.start("sub1")
        self.assertEqual(caught.exception.status_code, 400)

    async def test_auto_sub_blocks_a_start(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]), {})
        self.auto_sub = True
        with self.assertRaises(HTTPException) as caught:
            await self.start("main")
        self.assertEqual(caught.exception.status_code, 423)

    async def test_a_second_start_while_running_is_refused(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): reading("correct"), ("main_r",): reading("correct")})
        gate = asyncio.Event()
        original = self.store.start_measurement

        async def slow(**kwargs):
            await gate.wait()
            return await original(**kwargs)

        self.store.start_measurement = slow
        await self.start("main")
        with self.assertRaises(HTTPException) as caught:
            await self.start("main")
        self.assertEqual(caught.exception.status_code, 409)
        gate.set()
        await api._slot.task

    async def test_cancelling_a_running_job_stops_before_the_next_take(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): reading("correct"), ("main_r",): reading("correct")})
        gate = asyncio.Event()
        original = self.store.start_measurement

        async def slow(**kwargs):
            await gate.wait()
            return await original(**kwargs)

        self.store.start_measurement = slow
        job = (await self.start("main"))["job"]
        await api.cancel_polarity_check_job(job["id"])
        gate.set()
        await api._slot.task
        self.assertEqual(api._slot.job["status"], "cancelled")
        self.assertIsNone(api._slot.job["result"])

    async def test_status_lists_the_units_and_the_newest_job(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): reading("correct"), ("main_r",): reading("correct")})
        await self.run_job("main")
        status = await api.get_polarity_check("main")
        self.assertEqual(status["units"], ["left", "right"])
        self.assertEqual(status["job"]["status"], "completed")
        self.assertNotIn("_params", status["job"])


if __name__ == "__main__":
    unittest.main()
