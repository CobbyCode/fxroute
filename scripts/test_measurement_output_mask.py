#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Area measurement freezes its target at job creation and masks other outputs.

The store is driven with a fake target provider (the real application hook is
wired in main.py) and fake runtime mask hooks, so the sweep itself never runs.
Assertions cover: mask derivation from role order (not fixed output 3/4),
Global measuring the full system, restoration after failure and cancellation,
and legacy behavior when no provider is configured.
"""

import asyncio
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import (
    default_output_state,
    set_crossover,
    set_mode_routing,
    switch_mode,
    validate_output_state,
)
from measurement.store import MeasurementStore
from measurement.target import freeze_measurement_target

CROSSOVER_ROLES = [f"{side}_{way}" for side in ("left", "right") for way in ("low", "mid", "high")]


def crossover_filter(frequency):
    return {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": frequency}


def crossover_state():
    assignments = [*CROSSOVER_ROLES, "sub1", "sub2"]
    state = switch_mode(set_mode_routing(set_crossover(default_output_state(), "stereo-sub", True), "stereo-sub", "A", assignments), "stereo-sub")
    for role, settings in state["modes"]["stereo-sub"]["processing"].items():
        if not role.startswith(("left_", "right_")):
            continue
        if not role.endswith("low"):
            settings["highpass"] = crossover_filter(300 if "mid" in role else 2500)
        if not role.endswith("high"):
            settings["lowpass"] = crossover_filter(300 if role.endswith("low") else 2500)
    return validate_output_state(state)


class FakeMaskRuntime:
    """Records mask writes like DSPRuntime.apply/clear_output_mask would."""

    def __init__(self):
        self.mask = 0
        self.events = []

    async def apply(self, mask):
        self.events.append(("apply", mask))
        self.mask |= mask
        return 0

    async def clear(self, mask):
        self.events.append(("clear", mask))
        self.mask &= ~mask


class MeasurementOutputMaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.env = patch.dict("os.environ", {
            "XDG_CONFIG_HOME": self.tempdir.name,
            "XDG_STATE_HOME": self.tempdir.name,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.state = crossover_state()
        self.provider_calls = []

    def make_store(self, *, worker=None, with_provider=True):
        store = MeasurementStore(home=Path(self.tempdir.name))
        self.runtime = FakeMaskRuntime()
        store.output_mask_apply = self.runtime.apply
        store.output_mask_clear = self.runtime.clear
        if with_provider:
            store.measurement_target_provider = self.target_provider
        store._execute_capture_job = worker or (lambda _job: {"message": "Measurement finished."})

        async def fake_setup(**kwargs):
            return {
                "job": {
                    "id": "measurement-job-target-test",
                    "status": "queued",
                    "created_at": store._utc_now(),
                    "updated_at": store._utc_now(),
                    "input": {"measurement_sample_rate": 48000, "id": "mic"},
                    "input_channels": {"mic": 1},
                    "measurement_scope": kwargs.get("measurement_scope", "active_chain"),
                    "scope_note": "",
                    "calibration": {"filename": "", "applied": False},
                    "result": None,
                    "error": None,
                },
                "channel": kwargs.get("channel", "left"),
            }

        store._prepare_measurement_job_setup = fake_setup
        return store

    def target_provider(self, bank_id, sample_rate_hz):
        self.provider_calls.append((bank_id, sample_rate_hz))
        return freeze_measurement_target(
            self.state, bank_id=str(bank_id or "global"), output_key="A", channels=8,
            sample_rate_hz=sample_rate_hz, fingerprint="test-fingerprint")

    async def start(self, store, **kwargs):
        await store.start_measurement(input_id="mic", channel="left", **kwargs)
        job_id = "measurement-job-target-test"
        task = store._job_tasks.get(job_id)
        self.assertIsNotNone(task)
        await task
        return store.get_job(job_id)

    async def test_area_job_masks_every_other_way_and_restores_it(self):
        store = self.make_store()
        job = await self.start(store, measurement_bank="left_mid")
        # Roles order: left_low, left_mid, left_high, right_*, sub1, sub2.
        expected_mask = 0b11111101
        self.assertEqual(job["measurement_bank"], "left_mid")
        self.assertEqual(job["output_mask"], expected_mask)
        self.assertEqual(job["measurement_target"]["bank_id"], "left_mid")
        self.assertEqual(job["measurement_target"]["measured_roles"], ["left_mid"])
        self.assertEqual(job["measurement_target"]["processing_fingerprint"], "test-fingerprint")
        self.assertEqual(self.provider_calls, [("left_mid", 48000)])
        self.assertEqual(self.runtime.events, [("apply", expected_mask), ("clear", expected_mask)])
        self.assertEqual(self.runtime.mask, 0)
        self.assertEqual(job["status"], "completed")

    async def test_global_job_masks_nothing_and_records_the_full_system(self):
        store = self.make_store()
        job = await self.start(store, measurement_bank="global")
        self.assertEqual(job["output_mask"], 0)
        self.assertEqual(job["measurement_target"]["measured_roles"], list(job["measurement_target"]["roles"]))
        self.assertEqual(self.runtime.events, [])

    async def test_empty_bank_follows_the_editing_selection(self):
        store = self.make_store()
        await self.start(store)
        self.assertEqual(self.provider_calls, [("", 48000)])

    async def test_failing_worker_still_clears_the_mask(self):
        def boom(_job):
            raise RuntimeError("capture failed")

        store = self.make_store(worker=boom)
        job = await self.start(store, measurement_bank="sub1")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.runtime.events[-1][0], "clear")
        self.assertEqual(self.runtime.mask, 0)

    async def test_cancelled_job_still_clears_the_mask(self):
        entered = threading.Event()
        release = threading.Event()

        def worker(_job):
            entered.set()
            release.wait(timeout=5)
            return {"message": "finished"}

        store = self.make_store(worker=worker)
        await store.start_measurement(input_id="mic", channel="left", measurement_bank="right_high")
        job_id = "measurement-job-target-test"
        task = store._job_tasks[job_id]
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        self.assertEqual(self.runtime.mask, 0b11011111)
        store.cancel_job(job_id)
        release.set()
        await task
        self.assertEqual(store.get_job(job_id)["status"], "cancelled")
        self.assertEqual(self.runtime.mask, 0)
        self.assertEqual(self.runtime.events[-1][0], "clear")

    async def test_unrouted_area_is_rejected_before_any_sweep(self):
        store = self.make_store()
        with self.assertRaisesRegex(ValueError, "not stored in output mode"):
            await store.start_measurement(input_id="mic", channel="left", measurement_bank="sub3")
        self.assertEqual(self.runtime.events, [])
        self.assertEqual(store._jobs, {})

    async def test_raw_helper_sweeps_are_never_masked_by_an_editing_area(self):
        # Auto-Sub optimize and SPL calibration drive their own bypass and
        # isolation; an editing area selected on screen must not mute the
        # outputs they are measuring.
        store = self.make_store()
        job = await self.start(store, measurement_bank="left_mid",
                               measurement_scope="raw_helper")

        for key in ("measurement_bank", "measurement_target", "output_mask"):
            self.assertNotIn(key, job)
        self.assertEqual(self.provider_calls, [])
        self.assertEqual(self.runtime.events, [])

    async def test_store_without_a_provider_keeps_the_legacy_job_shape(self):
        store = self.make_store(with_provider=False)
        job = await self.start(store, measurement_bank="left_mid")
        self.assertNotIn("measurement_target", job)
        self.assertNotIn("output_mask", job)
        self.assertEqual(self.runtime.events, [])
        self.assertEqual(self.provider_calls, [])


if __name__ == "__main__":
    unittest.main()
