#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""All Banks measures the whole system as listened and is never importable.

The frozen target covers every routed speaker with nothing muted, records each
bank's listened preset and the joint A/B side, and the commit gate refuses it
with its own message; the sweep itself needs no mask.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import (default_output_state, set_bank_preset, set_mode_routing, switch_mode,
                                validate_output_state)
from dsp.processing_plan import compile_processing_plan
from measurement.store import MeasurementStore
from measurement.target import (freeze_measurement_target, require_commit_target, target_input_mask,
                                target_output_mask, targets_compatible)

SURROUND = ["main_l", "main_r", "center", "rear_l", "rear_r", "sub1", "sub2"]


def state_for(mode, roles):
    return switch_mode(set_mode_routing(default_output_state(), mode, "A", roles), mode)


def freeze(state, bank_id, **extra):
    return freeze_measurement_target(state, bank_id=bank_id, output_key="A", channels=16,
                                     sample_rate_hz=48000, fingerprint="fp", **extra)


def plan_for(state):
    return compile_processing_plan(state, output_key="A", channels=16, sample_rate_hz=48000,
                                   preset_loader=lambda name: {"chain": [], "metadata": {}})


class AllBanksTargetTests(unittest.TestCase):
    def test_every_routed_speaker_is_measured_and_nothing_is_muted(self):
        for mode, roles in (("surround", SURROUND), ("stereo-sub", ["main_l", "main_r", "sub_l", "sub_r"])):
            with self.subTest(mode=mode):
                target = freeze(state_for(mode, roles), "all", plan=plan_for(state_for(mode, roles)))
                self.assertEqual((target["bank_id"], target["mode"]), ("all", mode))
                self.assertEqual(target["measured_roles"], roles)
                self.assertEqual(target_output_mask(target, roles=target["roles"]), 0)
                self.assertEqual(target_input_mask(target), 0)
                self.assertNotIn("global_group", target)
                self.assertNotIn("commit_fingerprint", target)

    def test_the_listened_state_of_every_bank_is_recorded(self):
        state = state_for("surround", SURROUND)
        self.assertEqual(freeze(state, "all")["active_side"], "A")
        state = set_bank_preset(state, "surround", "main", preset_b="Front B")
        state = set_bank_preset(state, "surround", "main", active_side="B")
        target = freeze(state, "all")
        self.assertEqual(target["active_side"], "B")
        self.assertEqual(target["bank_presets"]["main_l"], "Front B")
        self.assertEqual(target["bank_presets"]["rear_l"], "Neutral")
        # Mixed banks have no single preset.
        self.assertIsNone(target["preset"])
        # Global stays in the chain as listened but is no bank of this list.
        self.assertNotIn("global", target["bank_presets"])

    def test_the_commit_gate_refuses_an_all_banks_measurement(self):
        state = state_for("surround", SURROUND)
        target = freeze(state, "all")
        for bank in ("all", "global", "main"):
            with self.subTest(bank=bank), self.assertRaisesRegex(ValueError, "All Banks cannot be imported|through All Banks"):
                require_commit_target(target, target, mode="surround", bank_id=bank)

    def test_it_never_merges_with_a_bank_or_global_measurement(self):
        state = state_for("stereo-sub", ["main_l", "main_r", "sub_l", "sub_r"])
        every = freeze(state, "all")
        self.assertFalse(targets_compatible(every, freeze(state, "global")))
        self.assertFalse(targets_compatible(every, freeze(state, "main")))
        self.assertTrue(targets_compatible(every, freeze(state, "all")))


class AllBanksJobTests(unittest.IsolatedAsyncioTestCase):
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
