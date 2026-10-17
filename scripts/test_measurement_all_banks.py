#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""All Banks measures the system or a Surround group as listened, without import.

Whole-system targets mute nothing; group targets keep only their speakers and
subs audible. Both record every bank's listened preset and the joint A/B side,
can be saved, and are rejected by the filter commit gate.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import (default_output_state, set_bank_preset, set_mode_routing, switch_mode,
                                switch_all_banks, validate_output_state)
from dsp.banks import BankState
from measurement.store import MeasurementStore
from measurement.target import (freeze_measurement_target, require_commit_target, target_input_mask,
                                target_output_mask, targets_compatible, sweep_output_masks)

SURROUND = ["main_l", "main_r", "center", "rear_l", "rear_r", "sub1", "sub2"]


def state_for(mode, roles):
    return switch_mode(set_mode_routing(default_output_state(), mode, "A", roles), mode)


def freeze(state, bank_id, **extra):
    return freeze_measurement_target(state, bank_id=bank_id, output_key="A", channels=16,
                                     sample_rate_hz=48000, fingerprint="fp", **extra)


class AllBanksTargetTests(unittest.TestCase):
    def test_group_result_can_be_saved_and_reloaded_without_importing_a_filter(self):
        target = freeze(state_for("surround", SURROUND), "all", group="rear")
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                "os.environ", {"XDG_CONFIG_HOME": directory, "XDG_STATE_HOME": directory}):
            store = MeasurementStore(home=Path(directory))
            saved = store.save_measurement({"id": "all-group-save", "name": "Rear as listened", "channel": "left",
                "measurement_target": target, "traces": [{"kind": "sweep-response", "role": "trusted",
                    "points": [[20, 0], [100, 1], [1000, 0], [10000, -1]]}]})
            loaded = store.get_measurement(saved["id"])
            self.assertEqual(loaded["measurement_target"], target)
            self.assertEqual(loaded["channel"], "left")
            with self.assertRaisesRegex(ValueError, "through Ind. Banks"):
                require_commit_target(loaded["measurement_target"], target["measured_roles"],
                                      mode="surround", bank_id="rear")

    def test_group_targets_cover_only_the_group_and_subs(self):
        roles = ["main_l", "main_r", "center", "side_l", "side_r", "rear_l", "rear_r", "sub1", "sub2"]
        state = state_for("surround", roles)
        for group, measured, mask, input_mask in (
                ("front", ["main_l", "main_r", "sub1", "sub2"], 0b001111100, 0),
                ("center", ["center", "sub1", "sub2"], 0b001111011, 3),
                ("side", ["side_l", "side_r", "sub1", "sub2"], 0b001100111, 3),
                ("rear", ["rear_l", "rear_r", "sub1", "sub2"], 0b000011111, 3)):
            with self.subTest(group=group):
                target = freeze(state, "all", group=group)
                self.assertEqual(target["bank_id"], "all")
                self.assertEqual(target["global_group"], group)
                self.assertEqual(target["measured_roles"], measured)
                self.assertEqual(target_output_mask(target, roles=roles), mask)
                self.assertEqual(target_input_mask(target), input_mask)
                with self.assertRaisesRegex(ValueError, "through Ind. Banks"):
                    require_commit_target(target, measured, mode="surround", bank_id="global", group=group)

    def test_group_repeat_masks_keep_subs_on_both_sides(self):
        target = freeze(state_for("surround", SURROUND), "all", group="rear")
        self.assertEqual(sweep_output_masks(target, roles=SURROUND), {"left": 0b0010111, "right": 0b0001111})
        center = freeze(state_for("surround", SURROUND), "all", group="center")
        self.assertEqual(sweep_output_masks(center, roles=SURROUND), {"left": 0b0011011, "right": 0b0011011})

    def test_group_selection_preserves_every_banks_listened_state(self):
        state = state_for("surround", SURROUND)
        state = set_bank_preset(state, "surround", "main", preset_b="Front B", active_side="B")
        grouped = freeze(state, "all", group="rear")
        whole = freeze(state, "all")
        for field in ("active_side", "preset", "bank_presets"):
            self.assertEqual(grouped[field], whole[field])
        self.assertEqual(grouped["bank_presets"]["main_l"], "Front B")
        self.assertEqual(grouped["bank_presets"]["rear_l"], "Neutral")

    def test_unrouted_and_non_surround_group_requests_are_rejected(self):
        state = state_for("surround", SURROUND)
        for group in ("side", "lfe", "bogus"):
            with self.subTest(group=group), self.assertRaisesRegex(ValueError, "routed"):
                freeze(state, "all", group=group)
        for mode in ("stereo", "stereo-sub"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "Surround"):
                freeze(state_for(mode, ["main_l", "main_r"]), "all", group="front")

    def test_groups_and_whole_system_are_distinct_identities(self):
        state = state_for("surround", SURROUND)
        rear = freeze(state, "all", group="rear")
        self.assertFalse(targets_compatible(rear, freeze(state, "all")))
        self.assertFalse(targets_compatible(rear, freeze(state, "all", group="front")))
        self.assertFalse(targets_compatible(rear, freeze(state, "global", group="rear")))
        self.assertTrue(targets_compatible(rear, freeze(state, "all", group="rear")))

    def test_every_routed_speaker_is_measured_and_nothing_is_muted(self):
        for mode, roles in (("surround", SURROUND), ("stereo-sub", ["main_l", "main_r", "sub_l", "sub_r"])):
            with self.subTest(mode=mode):
                target = freeze(state_for(mode, roles), "all")
                self.assertEqual((target["bank_id"], target["mode"]), ("all", mode))
                self.assertEqual(target["measured_roles"], roles)
                self.assertEqual(target_output_mask(target, roles=target["roles"]), 0)
                self.assertEqual(target_input_mask(target), 0)
                self.assertNotIn("global_group", target)

    def test_the_listened_state_of_every_bank_is_recorded(self):
        state = state_for("surround", SURROUND)
        self.assertEqual(freeze(state, "all")["active_side"], "A")
        state = set_bank_preset(state, "surround", "main", preset_b="Front B")
        state = set_bank_preset(state, "surround", "main", active_side="B")
        target = freeze(state, "all")
        self.assertIsNone(target["active_side"])
        self.assertEqual(target["bank_presets"], {
            "main_l": "Front B", "main_r": "Front B", "center": "Neutral",
            "rear_l": "Neutral", "rear_r": "Neutral", "sub1": "Neutral", "sub2": "Neutral",
        })
        # Mixed banks have no common A/B side or single preset.
        self.assertIsNone(target["preset"])
        # Global stays in the chain as listened but is no bank of this list.
        self.assertNotIn("global", target["bank_presets"])

    def test_joint_b_switch_records_b_including_equal_neutral_slots(self):
        state = state_for("surround", SURROUND)
        state = set_bank_preset(state, "surround", "main", preset_b="Front B", active_side="B")
        global_before = state["modes"]["surround"]["banks"]["global"]
        mixed = freeze(state, "all")
        switched = switch_all_banks(state, "surround", "A", 16, "B")
        target = freeze(switched, "all")
        self.assertEqual(target["active_side"], "B")
        self.assertIsNone(target["preset"])
        self.assertEqual(target["bank_presets"], mixed["bank_presets"])
        for role in SURROUND:
            self.assertEqual(BankState.from_dict(switched["modes"]["surround"]["banks"][role]).active_side, "B", role)
        self.assertEqual(switched["modes"]["surround"]["banks"]["global"], global_before)
        self.assertIsNone(mixed["active_side"], "a frozen target must not change after a switch")

    def test_the_commit_gate_refuses_an_all_banks_measurement(self):
        state = state_for("surround", SURROUND)
        target = freeze(state, "all")
        for bank in ("all", "global", "main"):
            with self.subTest(bank=bank), self.assertRaisesRegex(ValueError, "Ind. Banks cannot be imported|through Ind. Banks"):
                require_commit_target(target, target["measured_roles"], mode="surround", bank_id=bank)

    def test_it_never_merges_with_a_bank_or_global_measurement(self):
        state = state_for("stereo-sub", ["main_l", "main_r", "sub_l", "sub_r"])
        every = freeze(state, "all")
        self.assertFalse(targets_compatible(every, freeze(state, "global")))
        self.assertFalse(targets_compatible(every, freeze(state, "main")))
        self.assertTrue(targets_compatible(every, freeze(state, "all")))


class AllBanksJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_jobs_apply_and_clear_masks_even_on_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory, patch.dict(
                    "os.environ", {"XDG_CONFIG_HOME": directory, "XDG_STATE_HOME": directory}):
                state = state_for("surround", SURROUND)
                events = []
                store = MeasurementStore(home=Path(directory))

                async def apply(mask, **options):
                    events.append(("apply", mask, options))

                async def clear(mask, **options):
                    events.append(("clear", mask, options))

                def capture(job):
                    if fail:
                        raise RuntimeError("capture failed")
                    return {"message": "Measurement finished."}

                store.output_mask_apply, store.output_mask_clear = apply, clear
                store.measurement_target_provider = lambda bank, rate, group=None: freeze(
                    state, bank, group=group)
                store._execute_capture_job = capture

                async def setup(**kwargs):
                    now = store._utc_now()
                    return {"job": {"id": "measurement-job-all-group", "status": "queued", "created_at": now,
                                    "updated_at": now, "input": {"measurement_sample_rate": 48000, "id": "mic"},
                                    "input_channels": {"mic": 1}, "measurement_scope": "active_chain", "scope_note": "",
                                    "calibration": {"filename": "", "applied": False}, "result": None, "error": None},
                            "channel": kwargs.get("channel", "stereo")}

                store._prepare_measurement_job_setup = setup
                await store.start_measurement(input_id="mic", channel="left", measurement_bank="all",
                                              measurement_group="rear")
                await store._job_tasks["measurement-job-all-group"]
                job = store.get_job("measurement-job-all-group")
                self.assertEqual(job["status"], "failed" if fail else "completed")
                self.assertEqual(job["measurement_target"]["global_group"], "rear")
                self.assertEqual(events, [("apply", 0b111, {"input_mask": 3}),
                                          ("clear", 0b111, {"input_mask": 3})])

    async def test_the_job_freezes_the_whole_system_and_masks_nothing(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                "os.environ", {"XDG_CONFIG_HOME": directory, "XDG_STATE_HOME": directory}):
            state = state_for("surround", SURROUND)
            events = []

            async def record(mask, **options):
                events.append((mask, options))

            store = MeasurementStore(home=Path(directory))
            store.output_mask_apply = store.output_mask_clear = record
            store.measurement_target_provider = lambda bank, rate, group=None: freeze_measurement_target(
                state, bank_id=bank, output_key="A", channels=16, sample_rate_hz=rate, fingerprint="fp", group=group)
            store._execute_capture_job = lambda _job: {"message": "Measurement finished."}

            async def fake_setup(**kwargs):
                now = store._utc_now()
                return {"job": {"id": "measurement-job-all-test", "status": "queued", "created_at": now,
                                "updated_at": now, "input": {"measurement_sample_rate": 48000, "id": "mic"},
                                "input_channels": {"mic": 1}, "measurement_scope": "active_chain", "scope_note": "",
                                "calibration": {"filename": "", "applied": False}, "result": None, "error": None},
                        "channel": kwargs.get("channel", "stereo")}

            store._prepare_measurement_job_setup = fake_setup
            await store.start_measurement(input_id="mic", channel="stereo", measurement_bank="all")
            await store._job_tasks["measurement-job-all-test"]
            job = store.get_job("measurement-job-all-test")
        self.assertEqual(job["status"], "completed")
        self.assertEqual((job["measurement_bank"], job["output_mask"]), ("all", 0))
        self.assertNotIn("input_mask", job)
        self.assertEqual(events, [])
        self.assertEqual(job["measurement_target"]["measured_roles"], SURROUND)


if __name__ == "__main__":
    unittest.main()
