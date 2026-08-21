#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical volume contract: master and Loudness work point are independent.

The global FXRoute master is the single user-facing volume.  Loudness
``volumeDb`` is only the ISO-226 work point of the curve and is never derived
from, or promoted to, the master.  These tests pin that separation.
"""

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
import dsp.api as dsp_api

dsp_api.configure_dsp_api(main._make_dsp_api_deps())
import audio.system_volume as system_volume
import audio.volume_contract as volume_contract
from audio.volume_contract import VolumeAction, VolumeState, plan_transition, target_for


def db(percent: int) -> float:
    return system_volume.volume_percent_to_db(percent)


def _effective_db(state_obj: VolumeState) -> float:
    # Mirrors the production level model: master + guard only.  Loudness
    # volumeDb is only the ISO-226 work point and never contributes to the
    # output level (the loudness stage net is 0 dB).
    return system_volume.volume_percent_to_db(state_obj.master_percent) + float(state_obj.dsp_guard_db)


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
    samples = [_effective_db(current)]
    for action in actions:
        current = volume_contract.apply_action(current, action)
        samples.append(_effective_db(current))
    return current, samples


def assert_safe_transition(test, start, target, actions):
    final, samples = simulate(start, actions)
    ceiling = max(_effective_db(start), _effective_db(target))
    test.assertEqual(final.preset, target.preset)
    test.assertEqual(final.loudness_enabled, target.loudness_enabled)
    test.assertEqual(final.master_percent, target.master_percent)
    test.assertAlmostEqual(final.volume_db, target.volume_db, places=9)
    test.assertAlmostEqual(final.dsp_guard_db, target.dsp_guard_db, places=9)
    test.assertAlmostEqual(
        _effective_db(final),
        _effective_db(target),
        places=9,
    )
    test.assertLessEqual(max(samples), ceiling + 1e-9, msg=f"peak={max(samples)} ceiling={ceiling} samples={samples}")


class VolumeContractUnitTests(unittest.TestCase):
    def test_loudness_in_path_only_when_enabled_and_not_direct(self):
        self.assertTrue(state(preset="Neutral", loudness=True).loudness_in_path)
        self.assertFalse(state(preset="Direct", loudness=True).loudness_in_path)
        self.assertFalse(state(preset="Neutral", loudness=False).loudness_in_path)
        self.assertFalse(state(preset="Direct", loudness=False).loudness_in_path)

    def test_effective_gain_is_master_and_guard_only(self):
        # volumeDb is only the ISO-226 work point and never contributes to
        # the output level, so the effective gain is master + guard in every
        # state, loudness in the path or not.
        self.assertAlmostEqual(
            _effective_db(state(preset="Neutral", loudness=True, volume_db=-31.0, master=100)),
            0.0,
            places=9,
        )
        self.assertAlmostEqual(
            _effective_db(state(preset="Direct", loudness=True, volume_db=-10.0, master=30)),
            db(30),
            places=9,
        )
        self.assertAlmostEqual(
            _effective_db(state(preset="Neutral", loudness=False, volume_db=-10.0, master=30)),
            db(30),
            places=9,
        )

    def test_preset_and_loudness_transitions_never_move_master_or_work_point(self):
        cases = []
        low = 30
        mid = 50

        cases.append(("preset_switch", state(preset="Direct", loudness=False, master=low),
                      target_for(current=state(preset="Direct", loudness=False, master=low), preset="Neutral")))
        cases.append(("enable_loudness", state(preset="Neutral", loudness=False, master=low, volume_db=0.0),
                      target_for(current=state(preset="Neutral", loudness=False, master=low, volume_db=0.0), loudness_enabled=True)))
        cases.append(("disable_loudness", state(preset="Neutral", loudness=True, volume_db=-10.0, master=low),
                      target_for(current=state(preset="Neutral", loudness=True, volume_db=-10.0, master=low), loudness_enabled=False)))
        cases.append(("slider", state(preset="Neutral", loudness=False, master=low),
                      target_for(current=state(preset="Neutral", loudness=False, master=low), percent=mid)))

        for name, start, target in cases:
            with self.subTest(name):
                actions = plan_transition(start, target)
                if name == "slider":
                    self.assertEqual([action.op for action in actions], ["set_master"])
                    self.assertEqual(target.master_percent, mid)
                else:
                    self.assertFalse(any(action.op == "set_master" for action in actions))
                    self.assertFalse(any(action.op == "set_volume_db" for action in actions))
                    self.assertEqual(target.master_percent, start.master_percent)
                    self.assertAlmostEqual(target.volume_db, start.volume_db, places=9)
                assert_safe_transition(self, start, target, actions)

    def test_slider_only_changes_master_and_never_the_work_point(self):
        start = state(preset="Neutral", loudness=True, volume_db=-26.5, master=30)
        target = target_for(current=start, percent=60)
        self.assertEqual(target.master_percent, 60)
        self.assertAlmostEqual(target.volume_db, -26.5, places=9)
        self.assertTrue(target.loudness_enabled)

    def test_noop_transition_is_empty(self):
        start = state(preset="Neutral", loudness=True, volume_db=-26.5, master=100)
        same = target_for(current=start)
        self.assertAlmostEqual(same.volume_db, -26.5, places=9)
        self.assertEqual(same.master_percent, 100)
        self.assertEqual(plan_transition(start, same), [])


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
        # Guard: the footer slider must never rewrite the Loudness work point.
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
        self.samples.append(_effective_db(snap))
        return snap


class VolumeTransitionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = _RecordingManager()
        self.runtime = _RecordingRuntime()
        self.recorder = VolumePathRecorder(self.manager, self.runtime)
        self.patches = [
            mock.patch.object(main, "dsp_manager", self.manager),
            mock.patch.object(main.runtime, "dsp_runtime", self.runtime),
            mock.patch.object(main, "set_output_volume", self.recorder.set_master),
            mock.patch.object(main, "get_output_volume", self.recorder.get_master),
            mock.patch.object(main.dsp_orchestrator, "sync_runtime", mock.AsyncMock()),
            mock.patch.object(main.manager, "broadcast", mock.AsyncMock()),
            mock.patch.object(main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"),
            mock.patch.object(main, "get_audio_output_overview", return_value={"output_mode": {"mode": "stereo"}}),
        ]
        for patcher in self.patches:
            patcher.start()
        main.runtime.canonical_volume_write_lock = None
        main.runtime.dsp_mutation_lock = None

    async def asyncTearDown(self):
        for patcher in self.patches:
            patcher.stop()
        main.runtime.canonical_volume_write_lock = None
        main.runtime.dsp_mutation_lock = None
        main.dsp_manager = None
        main.runtime.dsp_runtime = None

    def peak(self):
        if not self.recorder.samples:
            self.recorder.capture()
        return max(self.recorder.samples)

    async def test_preset_switch_from_direct_keeps_master_and_work_point(self):
        self.manager.active_preset = "Direct"
        self.manager.extras["loudness"]["enabled"] = True
        self.manager.extras["loudness"]["params"]["volumeDb"] = 0.0
        self.recorder.master = 30
        start = self.recorder.capture()
        await main._load_dsp_preset("Neutral")
        final = self.recorder.capture()
        self.assertEqual(final.preset, "Neutral")
        self.assertEqual(final.master_percent, 30)
        self.assertAlmostEqual(final.volume_db, 0.0, places=6)
        self.assertLessEqual(self.peak(), _effective_db(start) + 1e-9)

    async def test_preset_round_trip_never_moves_master(self):
        self.manager.active_preset = "Direct"
        self.manager.extras["loudness"]["enabled"] = True
        self.manager.extras["loudness"]["params"]["volumeDb"] = 0.0
        self.recorder.master = 25
        writes = []
        original = self.recorder.set_master

        def record_master(value):
            writes.append(int(round(float(value))))
            return original(value)

        with mock.patch.object(main, "set_output_volume", record_master):
            await main._load_dsp_preset("Neutral")
            await main._load_dsp_preset("Direct")
            await main._load_dsp_preset("Neutral")

        self.assertEqual(writes, [])
        self.assertEqual(self.recorder.master, 25)
        final = self.recorder.capture()
        self.assertEqual(final.preset, "Neutral")
        self.assertEqual(final.master_percent, 25)

    async def test_preset_a_b_switch_never_moves_master(self):
        self.manager.active_preset = "Direct"
        self.manager.extras["loudness"]["enabled"] = True
        self.manager.extras["loudness"]["params"]["volumeDb"] = 0.0
        self.recorder.master = 25
        await main._load_dsp_preset("Room")
        await main._load_dsp_preset("Direct")
        self.assertEqual(self.manager.get_active_preset(), "Direct")
        self.assertEqual(self.recorder.master, 25)

    async def test_enter_direct_from_neutral_keeps_master_and_work_point(self):
        self.manager.active_preset = "Neutral"
        self.manager.extras["loudness"]["enabled"] = True
        self.manager.extras["loudness"]["params"]["volumeDb"] = db(30)
        self.recorder.master = 100
        await main._load_dsp_preset("Direct")
        final = self.recorder.capture()
        self.assertEqual(final.preset, "Direct")
        self.assertEqual(final.master_percent, 100)
        self.assertAlmostEqual(final.volume_db, db(30), places=6)

    async def test_enable_loudness_while_direct_keeps_master_and_work_point(self):
        self.manager.active_preset = "Direct"
        self.manager.extras["loudness"]["enabled"] = False
        self.manager.extras["loudness"]["params"]["volumeDb"] = 0.0
        self.recorder.master = 30

        class Request:
            async def json(self):
                return {"loudness_enabled": True}

        await dsp_api.save_dsp_extras(Request())
        self.assertEqual(self.recorder.master, 30)
        self.assertTrue(self.manager.extras["loudness"]["enabled"])
        self.assertAlmostEqual(self.manager.extras["loudness"]["params"]["volumeDb"], 0.0, places=6)

    async def test_enable_loudness_while_neutral_keeps_master(self):
        self.manager.active_preset = "Neutral"
        self.manager.extras["loudness"]["enabled"] = False
        self.manager.extras["loudness"]["params"]["volumeDb"] = 0.0
        self.recorder.master = 30

        class Request:
            async def json(self):
                return {"loudness_enabled": True}

        await dsp_api.save_dsp_extras(Request())
        final = self.recorder.capture()
        self.assertEqual(final.master_percent, 30)
        self.assertTrue(final.loudness_in_path)
        self.assertAlmostEqual(final.volume_db, 0.0, places=6)

    async def test_disable_loudness_while_direct_keeps_master(self):
        self.manager.active_preset = "Direct"
        self.manager.extras["loudness"]["enabled"] = True
        self.manager.extras["loudness"]["params"]["volumeDb"] = -10.0
        self.recorder.master = 30

        class Request:
            async def json(self):
                return {"loudness_enabled": False}

        await dsp_api.save_dsp_extras(Request())
        self.assertEqual(self.recorder.master, 30)
        self.assertFalse(self.manager.extras["loudness"]["enabled"])

    async def test_slider_always_drives_the_global_master(self):
        self.manager.active_preset = "Direct"
        self.manager.extras["loudness"]["enabled"] = True
        self.recorder.master = 30
        result = await main._set_canonical_output_volume(40)
        self.assertEqual(result["volume"], 40)
        self.assertEqual(self.recorder.master, 40)
        self.assertEqual(self.manager.loudness_volume_writes, [])

        self.manager.active_preset = "Neutral"
        self.recorder.master = 40
        self.manager.extras["loudness"]["params"]["volumeDb"] = db(40)
        result = await main._set_canonical_output_volume(50)
        self.assertEqual(result["volume"], 50)
        self.assertEqual(self.recorder.master, 50)
        self.assertEqual(self.manager.loudness_volume_writes, [])

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

            await dsp_api.save_dsp_extras(Request())
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
