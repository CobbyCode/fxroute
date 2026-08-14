#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical volume contract: final state and max intermediate gain."""

from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
import system_volume
import volume_contract
from volume_contract import VolumeAction, VolumeState, plan_transition, target_for


def db(percent: int) -> float:
    return system_volume.volume_percent_to_db(percent)


def state(*, preset="Neutral", loudness=False, volume_db=0.0, master=100, guard=0.0):
    return VolumeState(
        preset=preset,
        loudness_enabled=loudness,
        volume_db=volume_db,
        master_percent=master,
        dsp_guard_db=guard,
    )


def simulate(start: VolumeState, actions: list[VolumeAction]):
    current = start
    samples = [volume_contract.effective_db(current)]
    for action in actions:
        current = volume_contract.apply_action(current, action)
        samples.append(volume_contract.effective_db(current))
    return current, samples


def assert_safe_transition(test, start, target, actions):
    final, samples = simulate(start, actions)
    ceiling = max(volume_contract.effective_db(start), volume_contract.effective_db(target))
    test.assertEqual(final.preset, target.preset)
    test.assertEqual(final.loudness_enabled, target.loudness_enabled)
    test.assertEqual(final.master_percent, target.master_percent)
    test.assertAlmostEqual(final.volume_db, target.volume_db, places=9)
    test.assertAlmostEqual(final.dsp_guard_db, target.dsp_guard_db, places=9)
    test.assertAlmostEqual(
        volume_contract.effective_db(final),
        volume_contract.effective_db(target),
        places=9,
    )
    test.assertLessEqual(max(samples), ceiling + 1e-9, msg=f"peak={max(samples)} ceiling={ceiling} samples={samples}")


class VolumeContractUnitTests(unittest.TestCase):
    def test_loudness_owns_only_when_in_active_path(self):
        self.assertTrue(state(preset="Neutral", loudness=True).loudness_in_path)
        self.assertFalse(state(preset="Direct", loudness=True).loudness_in_path)
        self.assertFalse(state(preset="Neutral", loudness=False).loudness_in_path)
        self.assertFalse(state(preset="Direct", loudness=False).loudness_in_path)

    def test_effective_gain_uses_one_owner(self):
        self.assertAlmostEqual(
            volume_contract.effective_db(state(preset="Neutral", loudness=True, volume_db=-31.0, master=100)),
            -31.0,
            places=9,
        )
        self.assertAlmostEqual(
            volume_contract.effective_db(state(preset="Direct", loudness=True, volume_db=-10.0, master=30)),
            db(30),
            places=9,
        )
        self.assertAlmostEqual(
            volume_contract.effective_db(state(preset="Neutral", loudness=False, volume_db=-10.0, master=30)),
            db(30),
            places=9,
        )

    def test_matrix_preserves_volume_and_never_spikes(self):
        cases = []
        low = 30
        mid = 50
        low_db = db(low)

        cases.append((
            "direct_neutral_loudness_off",
            state(preset="Direct", loudness=False, master=low),
            target_for(current=state(preset="Direct", loudness=False, master=low), preset="Neutral"),
        ))
        cases.append((
            "neutral_direct_loudness_off",
            state(preset="Neutral", loudness=False, master=low),
            target_for(current=state(preset="Neutral", loudness=False, master=low), preset="Direct"),
        ))
        cases.append((
            "direct_neutral_loudness_on",
            state(preset="Direct", loudness=True, volume_db=-10.0, master=low),
            target_for(current=state(preset="Direct", loudness=True, volume_db=-10.0, master=low), preset="Neutral"),
        ))
        cases.append((
            "neutral_direct_loudness_on",
            state(preset="Neutral", loudness=True, volume_db=low_db, master=100),
            target_for(current=state(preset="Neutral", loudness=True, volume_db=low_db, master=100), preset="Direct"),
        ))
        cases.append((
            "enable_loudness_neutral",
            state(preset="Neutral", loudness=False, master=low),
            target_for(current=state(preset="Neutral", loudness=False, master=low), loudness_enabled=True),
        ))
        cases.append((
            "disable_loudness_neutral",
            state(preset="Neutral", loudness=True, volume_db=low_db, master=100),
            target_for(current=state(preset="Neutral", loudness=True, volume_db=low_db, master=100), loudness_enabled=False),
        ))
        cases.append((
            "enable_loudness_direct",
            state(preset="Direct", loudness=False, master=low),
            target_for(current=state(preset="Direct", loudness=False, master=low), loudness_enabled=True),
        ))
        cases.append((
            "disable_loudness_direct",
            state(preset="Direct", loudness=True, volume_db=-10.0, master=low),
            target_for(current=state(preset="Direct", loudness=True, volume_db=-10.0, master=low), loudness_enabled=False),
        ))
        cases.append((
            "slider_neutral_loudness_on",
            state(preset="Neutral", loudness=True, volume_db=low_db, master=100),
            target_for(current=state(preset="Neutral", loudness=True, volume_db=low_db, master=100), percent=mid),
        ))
        cases.append((
            "slider_direct_loudness_on",
            state(preset="Direct", loudness=True, volume_db=-10.0, master=low),
            target_for(current=state(preset="Direct", loudness=True, volume_db=-10.0, master=low), percent=mid),
        ))
        cases.append((
            "slider_neutral_loudness_off",
            state(preset="Neutral", loudness=False, master=low),
            target_for(current=state(preset="Neutral", loudness=False, master=low), percent=mid),
        ))
        cases.append((
            "preset_ab_loudness_on",
            state(preset="Neutral", loudness=True, volume_db=low_db, master=100),
            target_for(current=state(preset="Neutral", loudness=True, volume_db=low_db, master=100), preset="Room"),
        ))
        cases.append((
            "leave_direct_at_30_percent",
            state(preset="Direct", loudness=True, volume_db=0.0, master=low),
            target_for(current=state(preset="Direct", loudness=True, volume_db=0.0, master=low), preset="Neutral"),
        ))
        cases.append((
            "neutral_room_loudness_off",
            state(preset="Neutral", loudness=False, master=low),
            target_for(current=state(preset="Neutral", loudness=False, master=low), preset="Room"),
        ))

        for name, start, target in cases:
            with self.subTest(name):
                self.assertAlmostEqual(
                    volume_contract.canonical_percent(target),
                    volume_contract.canonical_percent(target_for(current=start, preset=target.preset, loudness_enabled=target.loudness_enabled, percent=volume_contract.canonical_percent(target))),
                    delta=0,
                )
                if name.startswith("slider_"):
                    self.assertEqual(volume_contract.canonical_percent(target), mid)
                elif not name.startswith("slider_"):
                    self.assertEqual(
                        volume_contract.canonical_percent(target),
                        volume_contract.canonical_percent(start),
                    )
                actions = plan_transition(start, target)
                assert_safe_transition(self, start, target, actions)

    def test_enable_loudness_while_direct_keeps_master(self):
        start = state(preset="Direct", loudness=False, master=30)
        target = target_for(current=start, loudness_enabled=True)
        self.assertEqual(target.master_percent, 30)
        self.assertFalse(target.loudness_in_path)
        self.assertAlmostEqual(target.volume_db, db(30), places=9)
        _, samples = simulate(start, plan_transition(start, target))
        self.assertLessEqual(max(samples), db(30) + 1e-9)

    def test_leave_direct_at_30_never_uses_shallower_guard(self):
        start = state(preset="Direct", loudness=True, volume_db=0.0, master=30)
        target = target_for(current=start, preset="Neutral")
        actions = plan_transition(start, target)
        _, samples = simulate(start, actions)
        self.assertLessEqual(max(samples), db(30) + 1e-9)
        self.assertTrue(any(action.op == "set_volume_db" for action in actions))
        raise_master = [i for i, action in enumerate(actions) if action.op == "set_master" and int(action.value) == 100]
        enter_path = [i for i, action in enumerate(actions) if action.op == "set_preset" and action.value != "Direct"]
        self.assertTrue(raise_master)
        self.assertTrue(enter_path)
        self.assertLess(enter_path[0], raise_master[0])

    def test_autogain_and_calibration_do_not_retarget_canonical_volume(self):
        start = state(preset="Neutral", loudness=True, volume_db=-26.5, master=100)
        same = target_for(current=start)
        self.assertAlmostEqual(same.volume_db, -26.5, places=9)
        self.assertEqual(same.master_percent, 100)
        self.assertEqual(plan_transition(start, same), [])

    def test_fixed_minus_18_guard_before_master_100_would_fail_the_contract(self):
        start = state(preset="Direct", loudness=True, volume_db=0.0, master=30)
        unsafe = [
            VolumeAction("set_volume_db", db(30)),
            VolumeAction("set_guard", -18.0),
            VolumeAction("set_master", 100),
            VolumeAction("set_preset", "Neutral"),
            VolumeAction("set_guard", 0.0),
        ]
        _, samples = simulate(start, unsafe)
        self.assertGreater(max(samples), db(30) + 1.0)


class _RecordingManager:
    PURE_PRESET = "Direct"
    EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}

    def __init__(self, *, preset="Neutral", extras=None):
        self.active_preset = preset
        self.extras = extras or {
            "limiter": {"enabled": True, "params": {}},
            "headroom": {"enabled": False, "params": {"gainDb": -3.0}},
            "delay": {"enabled": False, "params": {"leftMs": 0.0, "rightMs": 0.0}},
            "bass_enhancer": {"enabled": False, "params": {"amount": 0.0}},
            "autogain": {"enabled": False, "params": {"targetDb": -12.0}},
            "loudness": {"enabled": False, "params": {
                "fftSize": 4096, "strength": 10, "volumeDb": 0.0,
                "calibration": {}, "calibrationProfiles": {},
            }},
            "tone_effect": {"enabled": False, "mode": "crystalizer"},
        }
        self.saved = []
        self.loudness_volume_writes = []
        self.runtime_properties = []

    def load_global_extras(self):
        return copy.deepcopy(self.extras)

    def save_global_extras(self, extras):
        self.extras = copy.deepcopy(extras)
        self.saved.append(copy.deepcopy(extras))
        return self.extras

    def normalize_effects_extras(self, extras):
        return copy.deepcopy(extras)

    def get_active_preset(self):
        return self.active_preset

    def loudness_db_from_percent(self, percent):
        return system_volume.volume_percent_to_db(percent)

    def loudness_percent_from_db(self, volume_db):
        return system_volume.volume_db_to_percent(volume_db)

    def apply_global_extras_to_all_presets(self, extras):
        self.save_global_extras(extras)
        return {"extras": copy.deepcopy(self.extras), "updated": 1, "skipped": ["Direct"], "runtime_applied": False}

    def apply_global_extras_to_active_preset(self, extras):
        return self.apply_global_extras_to_all_presets(extras)

    def loudness_transition_guard_db(self, _previous, _candidate):
        return -18.0

    LOUDNESS_STRENGTH_VOLUME_SETTLE_SECONDS = 0.0

    def apply_autogain_loudness_runtime(self, _previous, extras, persist_all_presets=True):
        del persist_all_presets
        self.save_global_extras(extras)
        return {"extras": copy.deepcopy(self.extras), "updated": 1, "skipped": ["Direct"], "runtime_applied": True}

    def apply_loudness_strength_runtime(self, previous, extras):
        return self.apply_autogain_loudness_runtime(previous, extras)

    def set_loudness_volume_db(self, volume_db):
        self.loudness_volume_writes.append(float(volume_db))
        self.extras["loudness"]["params"]["volumeDb"] = float(volume_db)
        return {"extras": copy.deepcopy(self.extras), "runtime_applied": True, "updated": 1, "skipped": []}

    def load_preset(self, preset_name, **_kwargs):
        self.active_preset = preset_name

    def get_status(self):
        return {"status": "ok", "active_preset": self.active_preset}

    def load_compare_state(self):
        return {"presetA": "Neutral", "presetB": "", "activeSide": "A"}

    def save_compare_state(self, compare):
        return compare


class _RecordingRuntime:
    def __init__(self):
        self.gain_db = 0.0
        self.gains = []
        self.syncs = []
        self.rebuilds = []

    def snapshot(self):
        return {"active": True, "output_gain_db": self.gain_db}

    async def set_output_gain_db(self, value):
        self.gain_db = float(value)
        self.gains.append(float(value))
        return self.gain_db

    async def sync(self, overview, **kwargs):
        self.syncs.append((overview, kwargs))

    async def guarded_rebuild(self, overview, *, guard_db, apply_candidate, apply_previous,
                              settle_seconds=0, candidate_extras=None, previous_extras=None,
                              before_ramp=None, before_rollback_ramp=None):
        del overview, apply_previous, settle_seconds, previous_extras, before_rollback_ramp
        self.rebuilds.append({
            "guard_db": float(guard_db),
            "candidate_enabled": bool((candidate_extras or {}).get("loudness", {}).get("enabled")),
        })
        await self.set_output_gain_db(guard_db)
        if before_ramp:
            await before_ramp()
        await self.set_output_gain_db(0.0)
        apply_candidate()


class VolumePathRecorder:
    def __init__(self, manager, runtime):
        self.manager = manager
        self.runtime = runtime
        self.master = 100
        self.samples = []

    def set_master(self, value):
        self.master = int(round(float(value)))
        self.capture()
        return self.master

    def get_master(self):
        return self.master

    def capture(self):
        extras = self.manager.load_global_extras()
        loudness = extras.get("loudness") or {}
        params = loudness.get("params") or {}
        snap = VolumeState(
            preset=self.manager.get_active_preset() or "",
            loudness_enabled=bool(loudness.get("enabled")),
            volume_db=float(params.get("volumeDb") or 0.0),
            master_percent=self.master,
            dsp_guard_db=float(self.runtime.gain_db),
        )
        self.samples.append(volume_contract.effective_db(snap))
        return snap


class VolumeTransitionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = _RecordingManager()
        self.runtime = _RecordingRuntime()
        self.recorder = VolumePathRecorder(self.manager, self.runtime)
        self.patches = [
            mock.patch.object(main, "dsp_manager", self.manager),
            mock.patch.object(main, "dsp_runtime", self.runtime),
            mock.patch.object(main, "set_output_volume", self.recorder.set_master),
            mock.patch.object(main, "get_output_volume", self.recorder.get_master),
            mock.patch.object(main, "_sync_dsp_runtime", mock.AsyncMock()),
            mock.patch.object(main.manager, "broadcast", mock.AsyncMock()),
            mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"),
            mock.patch.object(main, "get_audio_output_overview", return_value={"output_mode": {"mode": "stereo"}}),
        ]
        for patcher in self.patches:
            patcher.start()
        main.canonical_volume_write_lock = None
        main.dsp_mutation_lock = None

    async def asyncTearDown(self):
        for patcher in self.patches:
            patcher.stop()
        main.canonical_volume_write_lock = None
        main.dsp_mutation_lock = None
        main.dsp_manager = None
        main.dsp_runtime = None

    def peak(self):
        if not self.recorder.samples:
            self.recorder.capture()
        return max(self.recorder.samples)

    async def test_leave_direct_at_30_never_exceeds_start(self):
        self.manager.active_preset = "Direct"
        self.manager.extras["loudness"]["enabled"] = True
        self.manager.extras["loudness"]["params"]["volumeDb"] = 0.0
        self.recorder.master = 30
        start = db(30)
        self.recorder.capture()
        await main._load_dsp_preset("Neutral")
        final = self.recorder.capture()
        self.assertEqual(final.preset, "Neutral")
        self.assertTrue(final.loudness_in_path)
        self.assertEqual(final.master_percent, 100)
        self.assertAlmostEqual(final.volume_db, start, places=6)
        self.assertLessEqual(self.peak(), start + 1e-9)

    async def test_direct_neutral_direct_neutral_syncs_before_master_restore(self):
        events = []
        self.manager.active_preset = "Direct"
        self.manager.extras["loudness"]["enabled"] = True
        self.manager.extras["loudness"]["params"]["volumeDb"] = 0.0
        self.recorder.master = 25
        self.recorder.capture()

        original_set_master = self.recorder.set_master

        def record_master(value):
            events.append(("master", int(round(float(value)))))
            return original_set_master(value)

        self.manager.load_preset = lambda preset_name, **_kwargs: (
            setattr(self.manager, "active_preset", preset_name),
            events.append(("preset", preset_name)),
        )[-1]

        async def confirm_sync(*_args, **_kwargs):
            events.append(("sync", self.manager.get_active_preset()))

        with mock.patch.object(main, "set_output_volume", record_master), \
                mock.patch.object(main, "_sync_dsp_runtime", side_effect=confirm_sync):
            start = self.recorder.capture()
            await main._load_dsp_preset("Neutral")
            await main._load_dsp_preset("Direct")
            await main._load_dsp_preset("Neutral")

        self.assertEqual([event for event in events if event == ("master", 100)],
                         [("master", 100), ("master", 100)])
        for index, event in enumerate(events):
            if event == ("master", 100):
                self.assertEqual(events[index - 1][0], "sync")
                self.assertEqual(events[index - 1][1], "Neutral")
        self.assertLessEqual(self.peak(), volume_contract.effective_db(start) + 1e-9)
        final = self.recorder.capture()
        self.assertEqual(final.preset, "Neutral")
        self.assertEqual(final.master_percent, 100)
        self.assertAlmostEqual(volume_contract.effective_db(final), volume_contract.effective_db(start), places=6)

    async def test_direct_preset_a_b_transition_syncs_before_master_restore(self):
        events = []
        self.manager.active_preset = "Direct"
        self.manager.extras["loudness"]["enabled"] = True
        self.manager.extras["loudness"]["params"]["volumeDb"] = 0.0
        self.recorder.master = 25
        self.recorder.capture()

        original_set_master = self.recorder.set_master

        def record_master(value):
            events.append(("master", int(round(float(value)))))
            return original_set_master(value)

        def load(preset_name, **_kwargs):
            self.manager.active_preset = preset_name
            events.append(("preset", preset_name))

        async def confirm_sync(*_args, **_kwargs):
            events.append(("sync", self.manager.get_active_preset()))

        self.manager.load_preset = load
        with mock.patch.object(main, "set_output_volume", record_master), \
                mock.patch.object(main, "_sync_dsp_runtime", side_effect=confirm_sync):
            await main._load_dsp_preset("Room")
            await main._load_dsp_preset("Direct")

        room_sync = events.index(("sync", "Room"))
        room_master = events.index(("master", 100))
        self.assertLess(room_sync, room_master)
        self.assertEqual(self.manager.get_active_preset(), "Direct")
        self.assertEqual(self.recorder.master, 25)

    async def test_enter_direct_from_neutral_loudness_preserves_volume(self):
        self.manager.active_preset = "Neutral"
        self.manager.extras["loudness"]["enabled"] = True
        self.manager.extras["loudness"]["params"]["volumeDb"] = db(30)
        self.recorder.master = 100
        self.recorder.capture()
        await main._load_dsp_preset("Direct")
        final = self.recorder.capture()
        self.assertEqual(final.preset, "Direct")
        self.assertEqual(final.master_percent, 30)
        self.assertNotIn(100, [sample for sample in self.recorder.samples[1:]])
        self.assertLessEqual(self.peak(), 0.0 + 1e-9)
        self.assertAlmostEqual(volume_contract.effective_db(final), db(30), places=6)

    async def test_enable_loudness_while_direct_does_not_pin_master(self):
        self.manager.active_preset = "Direct"
        self.manager.extras["loudness"]["enabled"] = False
        self.recorder.master = 30
        self.recorder.capture()

        class Request:
            async def json(self):
                return {"loudness_enabled": True}

        await main.save_easyeffects_extras(Request())
        self.assertEqual(self.recorder.master, 30)
        self.assertTrue(self.manager.extras["loudness"]["enabled"])
        self.assertAlmostEqual(self.manager.extras["loudness"]["params"]["volumeDb"], db(30), places=6)
        self.assertLessEqual(self.peak(), db(30) + 1e-9)

    async def test_enable_loudness_while_neutral_pins_master_after_path_owns(self):
        self.manager.active_preset = "Neutral"
        self.manager.extras["loudness"]["enabled"] = False
        self.recorder.master = 30
        self.recorder.capture()

        class Request:
            async def json(self):
                return {"loudness_enabled": True}

        await main.save_easyeffects_extras(Request())
        final = self.recorder.capture()
        self.assertEqual(final.master_percent, 100)
        self.assertTrue(final.loudness_in_path)
        self.assertAlmostEqual(final.volume_db, db(30), places=6)
        self.assertLessEqual(self.peak(), db(30) + 1e-9)

    async def test_disable_loudness_while_direct_keeps_master(self):
        self.manager.active_preset = "Direct"
        self.manager.extras["loudness"]["enabled"] = True
        self.manager.extras["loudness"]["params"]["volumeDb"] = -10.0
        self.recorder.master = 30

        class Request:
            async def json(self):
                return {"loudness_enabled": False}

        await main.save_easyeffects_extras(Request())
        self.assertEqual(self.recorder.master, 30)
        self.assertFalse(self.manager.extras["loudness"]["enabled"])

    async def test_slider_follows_active_owner(self):
        self.manager.active_preset = "Direct"
        self.manager.extras["loudness"]["enabled"] = True
        self.recorder.master = 30
        result = await main._set_canonical_output_volume(40)
        self.assertEqual(result["volume"], 40)
        self.assertEqual(self.recorder.master, 40)
        self.assertEqual(self.manager.loudness_volume_writes, [])

        self.manager.active_preset = "Neutral"
        self.recorder.master = 100
        self.manager.extras["loudness"]["params"]["volumeDb"] = db(40)
        result = await main._set_canonical_output_volume(50)
        self.assertEqual(result["volume"], 50)
        self.assertEqual(self.recorder.master, 100)
        self.assertEqual(len(self.manager.loudness_volume_writes), 1)

    async def test_autogain_change_does_not_retarget_master(self):
        self.manager.active_preset = "Neutral"
        self.manager.extras["loudness"]["enabled"] = True
        self.manager.extras["loudness"]["params"]["volumeDb"] = db(30)
        self.recorder.master = 100
        writes = []
        original = self.recorder.set_master

        def counting(value):
            writes.append(int(round(float(value))))
            return original(value)

        with mock.patch.object(main, "set_output_volume", counting):
            class Request:
                async def json(self):
                    return {"autogain_enabled": True, "autogain_target_db": -18.0}

            await main.save_easyeffects_extras(Request())
        self.assertEqual(writes, [])
        self.assertEqual(self.recorder.master, 100)
        self.assertAlmostEqual(self.manager.extras["loudness"]["params"]["volumeDb"], db(30), places=6)

    async def test_guarded_rebuild_does_not_pin_master_when_direct(self):
        self.manager.active_preset = "Direct"
        previous = self.manager.load_global_extras()
        candidate = copy.deepcopy(previous)
        candidate["loudness"]["enabled"] = True
        candidate["loudness"]["params"]["volumeDb"] = db(30)
        self.recorder.master = 30
        await main._guarded_effects_transition(previous, candidate, True)
        self.assertEqual(self.recorder.master, 30)

    async def test_rollback_restores_complete_volume_state(self):
        self.manager.active_preset = "Direct"
        self.manager.extras["loudness"]["enabled"] = True
        self.manager.extras["loudness"]["params"]["volumeDb"] = 0.0
        self.recorder.master = 30

        def failing_load(preset_name, **_kwargs):
            self.manager.active_preset = preset_name
            raise RuntimeError("preset load failed")

        self.manager.load_preset = failing_load
        with self.assertRaisesRegex(RuntimeError, "preset load failed"):
            await main._load_dsp_preset("Neutral")
        self.assertEqual(self.recorder.master, 30)
        self.assertEqual(self.manager.active_preset, "Direct")
        self.assertAlmostEqual(self.manager.extras["loudness"]["params"]["volumeDb"], 0.0, places=9)


if __name__ == "__main__":
    unittest.main()
