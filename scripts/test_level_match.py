#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Level Matching: scope planning, raw levels, cut-only trims and the measurement job."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from audio.output_state import (default_output_state, set_crossover, set_mode_routing,
                                set_output_processing, switch_mode)
from measurement import level_match as lm
from measurement import level_match_api as api
from measurement import speaker_sweep
from measurement.target import freeze_measurement_target

SURROUND_ROLES = ["main_l", "main_r", "center", "side_l", "side_r", "rear_l", "rear_r"]
FINGERPRINT = lm.make_fingerprint(device_key="a", sample_rate_hz=48000, mic_node="mic", mic_channel=1,
                                  capture_volume_percent=40.0, system_volume_percent=100.0)


def entry(role, raw_db, *, slot="global", family="surround", stamp="1", fingerprint=FINGERPRINT):
    return lm.make_entry(role=role, raw_db=raw_db, slot=slot, family=family,
                         fingerprint=fingerprint, measured_at=stamp)


def table(*entries):
    return {lm.entry_key(item["role"], item["slot"]): item for item in entries}


class ScopePlanTests(unittest.TestCase):
    def test_stereo_global_takes_each_side(self):
        scope = lm.plan_scope("stereo", "global", ["main_l", "main_r"])
        self.assertEqual((scope["slot"], scope["family"], scope["requires_reference"]),
                         ("global", "scope:global", False))
        self.assertEqual([(unit["roles"], unit["channel"]) for unit in scope["units"]],
                         [(["main_l"], "left"), (["main_r"], "right")])

    def test_crossover_global_measures_all_ways_of_a_side_together(self):
        roles = [f"{side}_{way}" for side in ("left", "right") for way in ("low", "high")]
        units = lm.plan_scope("stereo", "global", roles)["units"]
        self.assertEqual([unit["roles"] for unit in units],
                         [["left_low", "left_high"], ["right_low", "right_high"]])

    def test_crossover_bank_measures_one_way_per_take_into_the_bank_slot(self):
        roles = ["left_low", "right_low", "left_high", "right_high"]
        scope = lm.plan_scope("stereo", "low", roles)
        self.assertEqual((scope["slot"], scope["family"]), ("bank", "scope:low"))
        self.assertEqual([unit["roles"] for unit in scope["units"]], [["left_low"], ["right_low"]])

    def test_subwoofers_are_excluded(self):
        roles = ["main_l", "main_r", "sub_l", "sub_r"]
        for bank in ("sub", "sub_l"):
            with self.assertRaises(ValueError):
                lm.plan_scope("stereo-sub", bank, roles)
        units = lm.plan_scope("stereo-sub", "global", roles)["units"]
        self.assertEqual([unit["roles"] for unit in units], [["main_l"], ["main_r"]])

    def test_surround_front_uses_the_global_slot_and_needs_no_reference(self):
        scope = lm.plan_scope("surround", "main", SURROUND_ROLES + ["sub1"])
        self.assertEqual((scope["slot"], scope["family"], scope["requires_reference"]),
                         ("global", "surround", False))

    def test_surround_front_with_crossover_measures_all_front_ways(self):
        roles = ["left_low", "left_high", "right_low", "right_high", "center", "rear_l", "rear_r"]
        scope = lm.plan_scope("surround", "high", roles)
        self.assertEqual(scope["slot"], "global")
        self.assertEqual([unit["roles"] for unit in scope["units"]],
                         [["left_low", "left_high"], ["right_low", "right_high"]])

    def test_surround_other_groups_use_the_bank_slot_and_need_the_reference(self):
        for bank, expected in (("center", [["center"]]), ("side", [["side_l"], ["side_r"]]),
                               ("rear", [["rear_l"], ["rear_r"]])):
            scope = lm.plan_scope("surround", bank, SURROUND_ROLES)
            self.assertEqual((scope["slot"], scope["requires_reference"]), ("bank", True), bank)
            self.assertEqual([unit["roles"] for unit in scope["units"]], expected)

    def test_surround_has_no_global_and_a_lone_role_is_no_pair(self):
        with self.assertRaises(ValueError):
            lm.plan_scope("surround", "global", SURROUND_ROLES)
        with self.assertRaises(ValueError):
            lm.plan_scope("stereo", "global", ["main_l"])


class RawLevelAndTrimTests(unittest.TestCase):
    def test_raw_level_removes_the_own_slot(self):
        self.assertEqual(lm.raw_level_db(-18.0, [-2.0]), -16.0)
        self.assertEqual(lm.raw_level_db(-18.0, [-2.0, -2.0]), -16.0)
        self.assertEqual(lm.raw_level_db(-18.0, []), -18.0)
        with self.assertRaises(ValueError):
            lm.raw_level_db(None, [0.0])

    def test_quieter_front_side_stays_at_zero_and_the_louder_is_cut(self):
        values = lm.derive_slot_values(table(entry("main_l", -20.0), entry("main_r", -22.5, stamp="2")))
        self.assertEqual(values, {"main_l": {"global": -2.5}, "main_r": {"global": 0.0}})
        self.assertTrue(all(trim <= 0 for slots in values.values() for trim in slots.values()))

    def test_every_other_speaker_is_cut_to_the_front_reference(self):
        values = lm.derive_slot_values(table(
            entry("main_l", -20.0), entry("main_r", -20.0),
            entry("center", -17.0, slot="bank", stamp="2"), entry("rear_l", -19.0, slot="bank", stamp="3")))
        self.assertEqual(values["center"], {"bank": -3.0})
        self.assertEqual(values["rear_l"], {"bank": -1.0})
        self.assertEqual(values["main_l"], {"global": 0.0})

    def test_a_quieter_speaker_lowers_the_target_and_every_stored_trim_follows(self):
        entries = table(entry("main_l", -20.0), entry("main_r", -21.0),
                        entry("center", -19.0, slot="bank", stamp="2"))
        before = lm.derive_slot_values(entries)
        self.assertEqual(before["main_l"], {"global": -1.0})
        entries.update(table(entry("rear_l", -25.0, slot="bank", stamp="3")))
        after = lm.derive_slot_values(entries)
        self.assertEqual(after["main_l"], {"global": -5.0})
        self.assertEqual(after["main_r"], {"global": -4.0})
        self.assertEqual(after["center"], {"bank": -6.0})
        self.assertEqual(after["rear_l"], {"bank": 0.0})

    def test_a_role_measured_under_both_slots_keeps_both_entries(self):
        entries = table(entry("main_l", -20.0, slot="global", family="scope:global"),
                        entry("main_r", -21.0, slot="global", family="scope:global"),
                        entry("main_l", -30.0, slot="bank", family="scope:main"),
                        entry("main_r", -30.5, slot="bank", family="scope:main"))
        self.assertEqual(len(entries), 4)
        values = lm.derive_slot_values(entries)
        self.assertEqual(values["main_l"], {"global": -1.0, "bank": -0.5})
        self.assertEqual(values["main_r"], {"global": 0.0, "bank": 0.0})

    def test_trims_are_rounded_to_a_tenth(self):
        values = lm.derive_slot_values(table(entry("main_l", -20.0), entry("main_r", -21.234, stamp="2")))
        self.assertEqual(values["main_l"]["global"], -1.2)

    def test_entries_from_another_capture_chain_are_outdated_and_left_out(self):
        other = lm.make_fingerprint(device_key="a", sample_rate_hz=48000, mic_node="mic", mic_channel=1,
                                    capture_volume_percent=70.0, system_volume_percent=100.0)
        entries = table(entry("main_l", -20.0, stamp="1", fingerprint=other),
                        entry("main_r", -21.0, stamp="1", fingerprint=other),
                        entry("center", -30.0, slot="bank", stamp="2"))
        self.assertEqual(set(lm.valid_entries(entries)), {"bank:center"})
        self.assertEqual(lm.derive_slot_values(entries), {"center": {"bank": 0.0}})
        self.assertFalse(lm.has_front_reference(entries, ["main_l", "main_r"]))

    def test_volume_within_tolerance_is_the_same_chain(self):
        near = dict(FINGERPRINT, capture_volume_percent=40.3)
        self.assertTrue(lm.fingerprints_match(FINGERPRINT, near))
        self.assertFalse(lm.fingerprints_match(FINGERPRINT, dict(FINGERPRINT, capture_volume_percent=41.0)))
        self.assertFalse(lm.fingerprints_match(FINGERPRINT, dict(FINGERPRINT, capture_volume_percent=None)))
        self.assertFalse(lm.fingerprints_match(FINGERPRINT, dict(FINGERPRINT, mic_node="other")))

    def test_front_reference_needs_both_front_sides(self):
        entries = table(entry("main_l", -20.0))
        self.assertFalse(lm.has_front_reference(entries, ["main_l", "main_r"]))
        entries.update(table(entry("main_r", -20.0, stamp="2")))
        self.assertTrue(lm.has_front_reference(entries, ["main_l", "main_r"]))
        self.assertFalse(lm.has_front_reference(entries, []))

    def test_an_implausible_spread_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            lm.derive_slot_values(table(entry("main_l", -20.0), entry("main_r", -90.0, stamp="2")))
        self.assertIn("implausible", str(caught.exception))


class StoreTests(unittest.TestCase):
    def test_roundtrip_and_modes_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = lm.LevelMatchStore(Path(directory) / "level-match.json")
            self.assertEqual(store.entries("stereo"), {})
            store.save_entries("stereo", table(entry("main_l", -20.0)))
            store.save_entries("surround", table(entry("center", -18.0, slot="bank")))
            self.assertEqual(set(store.entries("stereo")), {"global:main_l"})
            self.assertEqual(set(store.entries("surround")), {"bank:center"})

    def test_a_foreign_document_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "level-match.json"
            path.write_text('{"schema": "other"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                lm.LevelMatchStore(path).load()


def mode_state(mode, roles, *, crossover=False):
    state = default_output_state()
    if crossover:
        state = set_crossover(state, mode, True)
    return switch_mode(set_mode_routing(state, mode, "A", roles), mode)


class FakeStore:
    """Measurement store double: freezes real targets and answers takes from a level table."""

    def __init__(self, state, levels, *, mic_node="mic", arrivals=None):
        self.state, self.levels, self.mic_node = state, levels, mic_node
        # Roles whose take the analyzer reports another timing status for ("valid" by default).
        self.arrivals = arrivals or {}
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
        analysis = {"sample_rate": 48000, "normalized_by_db": self.levels[roles]}
        status = self.arrivals.get(roles, "valid")
        if status is not None:
            analysis["impulse_response"] = {"timing_status": status}
        return {"status": "completed", "result": {
            "analysis": analysis,
            "capture": {"microphone_node": self.mic_node, "mic_input_channel": 1}}}

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)


class JobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.capture_volume = 40.0
        self.addCleanup(api.configure_level_match, None)

    def configure(self, state, levels, *, arrivals=None):
        self.state = state
        self.store = FakeStore(state, levels, arrivals=arrivals)

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
        self.table = lm.LevelMatchStore(Path(self.directory.name) / "level-match.json")
        api.configure_level_match(api.LevelMatchDeps(
            get_table=lambda: self.table, get_store=lambda: self.store, get_session=lambda: None,
            auto_sub_active=lambda: False, get_output_state=lambda: self.state,
            get_output_volume=lambda: 100, get_node_volume=lambda node: self.capture_volume))

    async def run_job(self, bank_id):
        response = await api.start_level_match(input_id="mic", input_key="", mic_input_channel="1",
                                               calibration_ref="", bank_id=bank_id)
        await api._slot.task
        return api._slot.job

    async def test_stereo_pair_cuts_the_louder_side_and_sweeps_each_side(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): -18.0, ("main_r",): -20.5})
        job = await self.run_job("main")
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual(job["result"]["values"], {"main_l": {"bank": -2.5}, "main_r": {"bank": 0.0}})
        self.assertEqual([call["channel"] for call in self.store.started], ["left", "right"])
        self.assertEqual([call["frozen_target"]["measured_roles"] for call in self.store.started],
                         [["main_l"], ["main_r"]])
        self.assertEqual(set(self.table.entries("stereo")), {"bank:main_l", "bank:main_r"})

    async def test_remeasuring_removes_the_own_slot_from_the_reading(self):
        state = set_output_processing(mode_state("stereo", ["main_l", "main_r"]), "stereo", "main_l",
                                      level_cal_db={"bank": -2.5})
        # main_l already plays 2.5 dB lower, so both sides now read the same.
        self.configure(state, {("main_l",): -20.5, ("main_r",): -20.5})
        job = await self.run_job("main")
        self.assertEqual(job["result"]["values"], {"main_l": {"bank": -2.5}, "main_r": {"bank": 0.0}})

    async def test_other_speakers_wait_for_the_front_reference(self):
        self.configure(mode_state("surround", SURROUND_ROLES), {})
        with self.assertRaises(HTTPException) as caught:
            await api.start_level_match(input_id="mic", input_key="", mic_input_channel="1",
                                        calibration_ref="", bank_id="rear")
        self.assertEqual(caught.exception.status_code, 409)
        status = await api.get_level_match("rear")
        self.assertFalse(status["reference_ready"])
        self.assertTrue((await api.get_level_match("main"))["reference_ready"])

    async def test_surround_reference_then_a_quieter_speaker_lowers_the_target(self):
        levels = {("main_l",): -20.0, ("main_r",): -21.0, ("center",): -19.0,
                  ("rear_l",): -25.0, ("rear_r",): -24.0}
        self.configure(mode_state("surround", SURROUND_ROLES), levels)
        front = await self.run_job("main")
        self.assertEqual(front["result"]["values"], {"main_l": {"global": -1.0}, "main_r": {"global": 0.0}})
        self.assertTrue((await api.get_level_match("rear"))["reference_ready"])
        center = await self.run_job("center")
        # The center is louder than the quieter front side (-21), so it is cut; the fronts stay.
        self.assertEqual(center["result"]["values"]["center"], {"bank": -2.0})
        self.assertEqual(center["result"]["values"]["main_l"], {"global": -1.0})
        rear = await self.run_job("rear")
        values = rear["result"]["values"]
        # The rear is quieter than every stored speaker: the target drops to its level and
        # the fronts and the center follow from their stored raw levels.
        self.assertEqual(values["rear_l"], {"bank": 0.0})
        self.assertEqual(values["rear_r"], {"bank": -1.0})
        self.assertEqual(values["main_l"], {"global": -5.0})
        self.assertEqual(values["main_r"], {"global": -4.0})
        self.assertEqual(values["center"], {"bank": -6.0})
        self.assertTrue(all(trim <= 0 for slots in values.values() for trim in slots.values()))

    async def test_a_changed_microphone_gain_since_the_reference_fails_the_job(self):
        levels = {("main_l",): -20.0, ("main_r",): -20.0, ("center",): -22.0}
        self.configure(mode_state("surround", SURROUND_ROLES), levels)
        await self.run_job("main")
        self.capture_volume = 70.0
        # The stored reference no longer matches the live capture chain.
        job = await self.run_job("center")
        self.assertEqual(job["status"], "failed")
        self.assertIn("measure Front L/R again", job["error"])
        self.assertEqual(set(self.table.entries("surround")), {"global:main_l", "global:main_r"})

    async def test_an_implausible_reading_fails_without_storing(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): -20.0, ("main_r",): -95.0})
        job = await self.run_job("main")
        self.assertEqual(job["status"], "failed")
        self.assertIn("implausible", job["error"])
        self.assertEqual(self.table.entries("stereo"), {})

    async def test_subwoofer_banks_are_refused(self):
        self.configure(mode_state("stereo-sub", ["main_l", "main_r", "sub1", "sub2"]), {})
        with self.assertRaises(HTTPException) as caught:
            await api.start_level_match(input_id="mic", input_key="", mic_input_channel="1",
                                        calibration_ref="", bank_id="sub1")
        self.assertEqual(caught.exception.status_code, 400)

    async def test_a_second_start_while_running_is_refused(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]), {("main_l",): -20.0, ("main_r",): -20.0})
        gate = asyncio.Event()
        original = self.store.start_measurement

        async def slow(**kwargs):
            await gate.wait()
            return await original(**kwargs)

        self.store.start_measurement = slow
        await api.start_level_match(input_id="mic", input_key="", mic_input_channel="1",
                                    calibration_ref="", bank_id="main")
        with self.assertRaises(HTTPException) as caught:
            await api.start_level_match(input_id="mic", input_key="", mic_input_channel="1",
                                        calibration_ref="", bank_id="main")
        self.assertEqual(caught.exception.status_code, 409)
        gate.set()
        await api._slot.task

    async def test_a_take_without_the_speakers_direct_sound_stores_and_computes_nothing(self):
        # The right side's sweep never reached the microphone: the left side's good reading
        # must not be kept either, and no trim may come out of a half measurement.
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): -18.0, ("main_r",): -80.0},
                       arrivals={("main_r",): "ambiguous"})
        job = await self.run_job("main")
        self.assertEqual(job["status"], "failed")
        self.assertIn("did not reach the microphone", job["error"])
        self.assertIn("right", job["error"])
        self.assertIsNone(job["result"])
        self.assertEqual(self.table.entries("stereo"), {})
        self.assertFalse(self.table.path.exists())

    async def test_the_first_take_without_direct_sound_ends_the_run(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): -80.0, ("main_r",): -80.0},
                       arrivals={("main_l",): "ambiguous", ("main_r",): "ambiguous"})
        job = await self.run_job("main")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(len(self.store.started), 1)
        self.assertEqual(self.table.entries("stereo"), {})

    async def test_an_analysis_without_timing_information_is_refused(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]),
                       {("main_l",): -18.0, ("main_r",): -20.0},
                       arrivals={("main_l",): None})
        job = await self.run_job("main")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.table.entries("stereo"), {})

    async def test_a_refused_take_leaves_stored_levels_and_trims_untouched(self):
        # Noise can read like a plausible level; only the missing direct sound marks it as unusable.
        levels = {("main_l",): -20.0, ("main_r",): -21.0, ("rear_l",): -25.0, ("rear_r",): -24.0}
        self.configure(mode_state("surround", SURROUND_ROLES), levels)
        front = await self.run_job("main")
        self.assertEqual(front["status"], "completed", front["error"])
        stored = self.table.entries("surround")
        self.store.arrivals = {("rear_l",): "ambiguous", ("rear_r",): "ambiguous"}
        rear = await self.run_job("rear")
        self.assertEqual(rear["status"], "failed")
        self.assertIsNone(rear["result"])
        self.assertEqual(self.table.entries("surround"), stored)
        status = await api.get_level_match("main")
        self.assertEqual({row["role"]: row["trim_db"] for row in status["entries"]},
                         {"main_l": -1.0, "main_r": 0.0})

    async def test_status_lists_the_stored_levels_with_their_trims(self):
        self.configure(mode_state("stereo", ["main_l", "main_r"]), {("main_l",): -18.0, ("main_r",): -20.0})
        await self.run_job("main")
        status = await api.get_level_match("main")
        rows = {(row["role"], row["slot"]): row for row in status["entries"]}
        self.assertEqual(rows[("main_l", "bank")]["trim_db"], -2.0)
        self.assertTrue(all(row["valid"] for row in rows.values()))
        self.assertEqual(status["units"], ["left", "right"])


if __name__ == "__main__":
    unittest.main()
