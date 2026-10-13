#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Surround Global is measured per speaker group, together with the subs.

Covers the frozen target (measured roles, output mask, input mute, commit
identity), the commit gate against the binding's group, the job lifecycle
that applies and clears the input mute, and the refusal cases.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import (default_output_state, set_crossover, set_mode_routing, switch_mode,
                                validate_output_state)
from measurement.store import MeasurementStore
from measurement.target import (area_measured_roles, freeze_measurement_target, require_commit_target,
                                sweep_output_masks,
                                target_input_mask, target_output_mask, targets_compatible)

QUAD_SUBS = ["main_l", "main_r", "rear_l", "rear_r", "sub1", "sub2"]
FULL = ["main_l", "main_r", "center", "side_l", "side_r", "rear_l", "rear_r", "sub1", "sub2"]


def surround_state(roles=QUAD_SUBS, rear_preset="Neutral"):
    state = switch_mode(set_mode_routing(default_output_state(), "surround", "A", roles), "surround")
    banks = state["modes"]["surround"]["banks"]
    if "rear_l" in banks:
        banks["rear_l"]["preset"] = banks["rear_l"]["preset_a"] = rear_preset
    return validate_output_state(state)


def freeze(state, group, **extra):
    return freeze_measurement_target(state, bank_id="global", output_key="A", channels=16,
                                     sample_rate_hz=48000, fingerprint="fp", group=group, **extra)


class GlobalGroupTargetTests(unittest.TestCase):
    def test_a_group_target_covers_its_speakers_plus_every_sub(self):
        target = freeze(surround_state(), "rear")
        self.assertEqual((target["bank_id"], target["global_group"]), ("global", "rear"))
        self.assertEqual(target["measured_roles"], ["rear_l", "rear_r", "sub1", "sub2"])
        self.assertEqual(target["roles"], QUAD_SUBS)
        # Front is muted at the outputs; its sink feed is zeroed at the inputs.
        self.assertEqual(target_output_mask(target, roles=target["roles"]), 0b000011)
        self.assertEqual(target_input_mask(target), 0b11)

    def test_front_keeps_its_own_inputs_and_mutes_the_other_speakers(self):
        target = freeze(surround_state(), "front")
        self.assertEqual(target["measured_roles"], ["main_l", "main_r", "sub1", "sub2"])
        self.assertEqual(target_output_mask(target, roles=target["roles"]), 0b001100)
        self.assertEqual(target_input_mask(target), 0)

    def test_front_with_nothing_else_routed_mutes_nothing(self):
        target = freeze(surround_state(["main_l", "main_r", "sub1"]), "front")
        self.assertEqual(target["measured_roles"], target["roles"])
        self.assertEqual(target_output_mask(target, roles=target["roles"]), 0)

    def test_center_side_and_crossover_ways_resolve_to_their_group(self):
        state = surround_state(FULL)
        self.assertEqual(freeze(state, "center")["measured_roles"], ["center", "sub1", "sub2"])
        self.assertEqual(freeze(state, "side")["measured_roles"], ["side_l", "side_r", "sub1", "sub2"])
        ways = ["left_low", "left_high", "right_low", "right_high", "rear_l", "rear_r", "sub1", "sub2"]
        crossover = set_crossover(surround_state(), "surround", True)
        crossover = set_mode_routing(crossover, "surround", "A", ways)
        self.assertEqual(freeze(crossover, "front")["measured_roles"],
                         ["left_low", "left_high", "right_low", "right_high", "sub1", "sub2"])

    def test_surround_global_needs_a_routed_group_and_no_other_mode_takes_one(self):
        state = surround_state()
        for group in (None, "side", "center", "lfe", "bogus"):
            with self.subTest(group=group), self.assertRaisesRegex(ValueError, "per speaker group"):
                freeze(state, group)
        stereo = validate_output_state(default_output_state())
        with self.assertRaisesRegex(ValueError, "Surround Global or All Banks only"):
            freeze(stereo, "front")
        with self.assertRaisesRegex(ValueError, "Surround Global or All Banks only"):
            freeze_measurement_target(state, bank_id="rear", output_key="A", channels=16,
                                      sample_rate_hz=48000, fingerprint="fp", group="rear")
        self.assertNotIn("global_group", freeze_measurement_target(
            stereo, bank_id="global", output_key="A", channels=16, sample_rate_hz=48000, fingerprint="fp"))

    def test_repeat_sweeps_keep_the_subs_audible_on_both_sides(self):
        target = freeze(surround_state(), "rear")
        masks = sweep_output_masks(target, roles=target["roles"])
        self.assertEqual(masks["left"], 0b001011)   # main_l, main_r, rear_r muted
        self.assertEqual(masks["right"], 0b000111)  # main_l, main_r, rear_l muted

    def test_groups_are_separate_measurement_identities(self):
        state = surround_state(FULL)
        self.assertFalse(targets_compatible(freeze(state, "rear"), freeze(state, "side")))
        self.assertTrue(targets_compatible(freeze(state, "rear"), freeze(state, "rear")))

    def test_commit_gate_compares_the_binding_group(self):
        state = surround_state()
        target = freeze(state, "rear")
        live = area_measured_roles(state, bank_id="global", output_key="A", channels=16, group="rear")
        self.assertEqual(live, target["measured_roles"])
        require_commit_target(target, live, mode="surround", bank_id="global", group="rear")
        # Another group's roles are another channel shape.
        front = area_measured_roles(state, bank_id="global", output_key="A", channels=16, group="front")
        with self.assertRaisesRegex(ValueError, "covers"):
            require_commit_target(target, front, mode="surround", bank_id="global", group="rear")
        for group in ("front", None):
            with self.subTest(group=group), self.assertRaisesRegex(ValueError, "speaker group"):
                require_commit_target(target, live, mode="surround", bank_id="global", group=group)
        stereo = validate_output_state(default_output_state())
        stereo_target = freeze_measurement_target(stereo, bank_id="global", output_key="A", channels=16,
                                                  sample_rate_hz=48000, fingerprint="fp")
        stereo_roles = stereo_target["measured_roles"]
        require_commit_target(stereo_target, stereo_roles, mode="stereo", bank_id="global")
        with self.assertRaisesRegex(ValueError, "speaker group"):
            require_commit_target(stereo_target, stereo_roles, mode="stereo", bank_id="global", group="rear")


class FakeMaskRuntime:
    def __init__(self):
        self.events = []

    async def apply(self, mask, **options):
        self.events.append(("apply", mask, options))
        return 0

    async def clear(self, mask, **options):
        self.events.append(("clear", mask, options))


class GlobalGroupJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        env = patch.dict("os.environ", {"XDG_CONFIG_HOME": self.tempdir.name, "XDG_STATE_HOME": self.tempdir.name})
        env.start()
        self.addCleanup(env.stop)
        self.state = surround_state()
        self.calls = []

    def make_store(self, worker=None):
        store = MeasurementStore(home=Path(self.tempdir.name))
        self.runtime = FakeMaskRuntime()
        store.output_mask_apply, store.output_mask_clear = self.runtime.apply, self.runtime.clear
        store.measurement_target_provider = self.provider
        store._execute_capture_job = worker or (lambda _job: {"message": "Measurement finished."})

        async def fake_setup(**kwargs):
            now = store._utc_now()
            return {"job": {"id": "measurement-job-group-test", "status": "queued", "created_at": now,
                            "updated_at": now, "input": {"measurement_sample_rate": 48000, "id": "mic"},
                            "input_channels": {"mic": 1}, "measurement_scope": "active_chain", "scope_note": "",
                            "calibration": {"filename": "", "applied": False}, "result": None, "error": None},
                    "channel": kwargs.get("channel", "left")}

        store._prepare_measurement_job_setup = fake_setup
        return store

    def provider(self, bank_id, sample_rate_hz, group=None):
        self.calls.append((bank_id, sample_rate_hz, group))
        return freeze_measurement_target(self.state, bank_id=bank_id or "global", output_key="A", channels=16,
                                         sample_rate_hz=sample_rate_hz, fingerprint="fp", group=group)

    async def run_job(self, store, **kwargs):
        await store.start_measurement(input_id="mic", channel="left", **kwargs)
        await store._job_tasks["measurement-job-group-test"]
        return store.get_job("measurement-job-group-test")

    async def test_group_job_applies_and_clears_the_input_mute_with_the_output_mask(self):
        store = self.make_store()
        job = await self.run_job(store, measurement_bank="global", measurement_group="rear")
        self.assertEqual(self.calls, [("global", 48000, "rear")])
        self.assertEqual((job["output_mask"], job["input_mask"]), (0b11, 0b11))
        self.assertEqual(job["measurement_target"]["global_group"], "rear")
        self.assertEqual(self.runtime.events, [("apply", 3, {"input_mask": 3}), ("clear", 3, {"input_mask": 3})])

    async def test_front_group_and_ordinary_jobs_carry_no_input_mute(self):
        store = self.make_store()
        job = await self.run_job(store, measurement_bank="global", measurement_group="front")
        self.assertNotIn("input_mask", job)
        self.assertEqual(self.runtime.events, [("apply", 0b1100, {}), ("clear", 0b1100, {})])
        self.runtime.events.clear()
        self.calls.clear()
        store = self.make_store()
        job = await self.run_job(store, measurement_bank="rear")
        self.assertEqual(self.calls, [("rear", 48000, None)])
        self.assertNotIn("input_mask", job)

    async def test_a_failing_group_job_still_releases_the_input_mute(self):
        def boom(_job):
            raise RuntimeError("capture failed")

        store = self.make_store(worker=boom)
        job = await self.run_job(store, measurement_bank="global", measurement_group="rear")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.runtime.events[-1], ("clear", 3, {"input_mask": 3}))

    async def test_missing_group_is_rejected_before_any_sweep(self):
        store = self.make_store()
        with self.assertRaisesRegex(ValueError, "per speaker group"):
            await store.start_measurement(input_id="mic", channel="left", measurement_bank="global")
        self.assertEqual(self.runtime.events, [])
        self.assertEqual(store._jobs, {})


if __name__ == "__main__":
    unittest.main()
