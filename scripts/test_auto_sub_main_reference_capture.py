#!/usr/bin/env python3
"""Focused tests for exact-mute AutoSub Main-only reference capture."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
from audio.samplerate import (
    OUTPUT_MODE_SUBWOOFER_21,
    OUTPUT_MODE_SUBWOOFER_22,
    OUTPUT_MODE_SUBWOOFER_22_STEREO,
)
import measurement.session as measurement_session
import measurement.autosub as autosub
import measurement.autosub.candidates as autosub_candidates
import measurement.autosub.jobs as autosub_jobs
import measurement.autosub.measurement as autosub_measurement
from measurement.autosub import deps as autosub_deps
from dsp.runtime import DSPRuntime, BassManagementConfig


class FakeProcess:
    pid = 4242
    returncode = None

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def runtime_config() -> BassManagementConfig:
    return BassManagementConfig(
        output_mode="subwoofer-2.1", output_key="mock", output_label="Mock",
        output_channels=4, sample_rate=48_000, crossover_frequency_hz=80,
        main_highpass_enabled=True, sub_level_db=-3.0, sub_alignment_ms=2.0,
        sub_polarity="normal",
    )


def original_snapshot(mode: str) -> dict:
    if mode == OUTPUT_MODE_SUBWOOFER_21:
        return {"subwoofer": {"sub_alignment_ms": 2.0, "sub_level_db": -3.0, "sub_polarity": "normal", "main_highpass_enabled": True}}
    return {
        "main_highpass_enabled": True,
        "subwoofers": {
            "sub1": {"alignment_ms": 1.0, "level_db": -2.0, "polarity": "normal"},
            "sub2": {"alignment_ms": 3.0, "level_db": -4.0, "polarity": "normal"},
        },
    }


class ExactMuteRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_control_changes_only_atomic_mute_state(self):
        runtime = DSPRuntime(object())
        runtime._config = type("Config", (), {
            "hardware_ports": ("FL", "FR", "RL", "RR"),
            "sample_rate": 48000,
            "output_mode": "subwoofer-2.1",
            "output_key": "mock",
        })()
        control = AsyncMock(return_value="ok")
        runtime._control = control
        self.assertFalse(await runtime.set_exact_sub_mute(True))
        self.assertTrue(runtime.snapshot()["exact_sub_mute"])
        self.assertTrue(await runtime.set_exact_sub_mute(False))
        self.assertFalse(runtime.snapshot()["exact_sub_mute"])
        self.assertEqual(control.await_args_list[0].args, ("mute 12 1",))
        self.assertEqual(control.await_args_list[1].args, ("mute 12 0",))


class MainReferenceSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def test_normalization_inverse_sign_is_addition(self):
        # MeasurementStore builds normalized_db = raw_db - normalized_by_db.
        self.assertEqual(
            autosub_measurement._auto_sub_reconstruct_calibrated_points([[20.0, -7.5], [80.0, 1.25]], -12.5),
            [[20.0, -20.0], [80.0, -11.25]],
        )

    async def test_exactly_two_structurally_identical_references_for_all_modes(self):
        for mode in (
            OUTPUT_MODE_SUBWOOFER_21,
            OUTPUT_MODE_SUBWOOFER_22,
            OUTPUT_MODE_SUBWOOFER_22_STEREO,
        ):
            calls = []

            async def fake_measure(**kwargs):
                calls.append(kwargs)
                side = kwargs["channel"]
                return {
                    "status": "completed", "sweep_id": f"sweep-{side}",
                    "calibrated_points": [[20.0, -30.0], [80.0, -20.0]],
                    "normalized_by_db": -20.0, "exact_sub_mute": True,
                    "measurement_channel": side, "sample_rate": 48_000,
                }

            # Service jobs map only the slots the mode actually has: a 2.1
            # system has one sub slot, so references must not claim sub2.
            role_map = {"sub1": "left"} if mode == OUTPUT_MODE_SUBWOOFER_21 else {"sub1": "left", "sub2": "right"}
            job = {
                "auto_gain": {"available": False, "reason": "gain not implemented"},
                "output_state_context": {
                    "mode": mode, "revision": 1, "output_key": "mock", "channels": 4,
                    "sub_role_map": role_map, "sub_mute_mask": [1] * len(role_map),
                },
            }
            expected_slots = tuple(slot for slot in ("sub1", "sub2") if slot in role_map)
            with patch.object(autosub.measurement, "_measure_auto_sub_candidate", side_effect=fake_measure):
                await autosub_measurement._capture_auto_sub_main_references(
                    job=job, fc=80, input_id="mic", mic_input_channel="1",
                    reference_input_channel="", calibration_ref="", calibration_filename=None,
                    calibration_bytes=None, auto_sub_rate=48_000,
                    output_mode=mode, original_config_snapshot=original_snapshot(mode),
                )
            self.assertEqual(len(calls), 2)
            self.assertEqual([call["channel"] for call in calls], ["left", "right"])
            self.assertTrue(all(call["exact_sub_mute"] for call in calls))
            self.assertTrue(all(call["active_subs"] == expected_slots for call in calls))
            # The reference profile is band-limited to the level-reference band
            # the anchor actually reads, not the full 10 Hz..22 kHz measurement
            # sweep; see _auto_sub_main_reference_sweep_profile.
            self.assertTrue(all(
                call["auto_sub_sweep_profile"] == autosub_candidates._auto_sub_main_reference_sweep_profile()
                for call in calls))
            self.assertEqual(job["main_references"]["status"], "completed")
            for side in ("left", "right"):
                self.assertEqual(set(job["main_references"][side]), {
                    "status", "points", "normalized_by_db", "sweep_id", "channel",
                    "measurement_channel", "sample_rate", "crossover_frequency_hz",
                    "main_highpass_enabled", "exact_sub_mute",
                })

    async def test_reference_failure_marks_only_auto_gain_unavailable(self):
        async def fake_measure(**kwargs):
            if kwargs["channel"] == "left":
                return {"status": "failed", "error": "capture failed", "exact_sub_mute": True}
            return {
                "status": "completed", "sweep_id": "right", "calibrated_points": [[20, -20], [80, -10]],
                "normalized_by_db": -10, "exact_sub_mute": True, "measurement_channel": "right",
                "sample_rate": 48_000,
            }

        job = {"auto_gain": {"available": False, "reason": "pending"}}
        with patch.object(autosub.measurement, "_measure_auto_sub_candidate", side_effect=fake_measure):
            await autosub_measurement._capture_auto_sub_main_references(
                job=job, fc=80, input_id="mic", mic_input_channel="1", reference_input_channel="",
                calibration_ref="", calibration_filename=None, calibration_bytes=None,
                auto_sub_rate=48_000,
                output_mode=OUTPUT_MODE_SUBWOOFER_21,
                original_config_snapshot=original_snapshot(OUTPUT_MODE_SUBWOOFER_21),
            )
        self.assertEqual(job["main_references"]["status"], "unavailable")
        self.assertIn("left", job["auto_gain"]["reason"])
        self.assertNotIn("status", job)  # Existing optimization state is not failed here.

    def test_candidate_profile_remains_bass_focused(self):
        for crossover_hz, expected_high_hz in ((40, 600.0), (80, 640.0), (200, 1600.0)):
            profile = autosub_candidates._auto_sub_sweep_profile(crossover_hz)
            self.assertEqual(profile["sweep_start_hz"], 20.0)
            self.assertEqual(profile["sweep_end_hz"], expected_high_hz)
            self.assertLessEqual(profile["sweep_seconds"], 3.5)

    async def test_candidate_restores_exact_mute_on_success_error_and_cancel(self):
        # Realistic sweep profile: the candidate path runs the native DSP peak
        # prediction against it, so it must be a valid profile that predicts a safe sweep.
        sweep_profile = {"sweep_seconds": 0.1, "sweep_start_hz": 20.0, "sweep_end_hz": 200.0}
        prediction = autosub_jobs._auto_sub_stage_peak_prediction(
            sweep_profile=sweep_profile, sample_rate=48_000, channel="left",
            config=runtime_config(),
        )
        # exact_sub_mute zeroes the muted sub outputs; measured peaks must
        # match that (single-sub mask mutes output_3 only).
        measured_peaks = dict(prediction["linear"])
        measured_peaks["output_3"] = 0.0

        class FakeRuntime:
            def __init__(self, fail_restore=False):
                self.muted = False
                self.calls = []
                self.fail_restore = fail_restore

            async def sync(self, _config):
                return None

            async def set_exact_sub_mute(self, enabled, mask=None):
                previous = self.muted
                self.calls.append(bool(enabled))
                if not enabled and self.fail_restore:
                    raise RuntimeError("restore acknowledgement missing")
                self.muted = bool(enabled)
                return previous

            async def reset_output_peaks(self):
                return None

            async def read_output_peaks(self):
                return dict(measured_peaks)

            def snapshot(self):
                return {"exact_sub_mute": self.muted, "output_gain_db": 0.0}

        class FakeOwner:
            committed = False

            async def ensure_ready(self, _rate):
                return None

            async def restore(self):
                return None

        class FakeStore:
            def __init__(self, outcome, job):
                self.outcome = outcome
                self.job = job

            async def start_measurement(self, **_kwargs):
                if self.outcome == "error":
                    raise RuntimeError("synthetic sweep error")
                if self.outcome == "cancel":
                    self.job["cancel_requested"] = True
                return {"id": "ref-sweep"}

            async def drain_job(self, _sweep_id):
                return None

            def cancel_job(self, _sweep_id):
                return None

            def get_job(self, _sweep_id):
                return {
                    "status": "completed",
                    "result": {"measurement": {
                        "channel": "left",
                        "traces": [{"kind": "sweep-response", "points": [[20, -1], [80, 1]]}],
                        "analysis": {"normalized_by_db": -20, "sample_rate": 48_000},
                    }},
                }

        async def no_sleep(_seconds):
            return None

        async def _config_prediction(**kwargs):
            return await autosub_jobs._predict_auto_sub_stage_peaks(
                sweep_profile=kwargs["sweep_profile"],
                sample_rate=kwargs["sample_rate"],
                channel=kwargs["channel"],
                config=runtime_config(),
                playback_gain=kwargs.get("playback_gain", 1.0),
                sink_gain=kwargs.get("sink_gain", 1.0),
            )

        for outcome in ("success", "error", "cancel", "restore_error"):
            job = {"id": f"ref-{outcome}", "cancel_requested": False,
                   "_sweep_timings": [], "auto_gain": {"available": False, "reason": "pending"},
                   # Backend-v2 migration: the funnel requires a service job.
                   "output_state_context": {
                       "mode": "stereo", "revision": 0, "output_key": "dev",
                       "channels": 4, "optimizer_path": "single-sub",
                       "sub_role_map": {"sub1": "sub1"}, "sub_mute_mask": 4,
                   }}
            runtime = FakeRuntime(fail_restore=outcome == "restore_error")
            store = FakeStore("success" if outcome == "restore_error" else outcome, job)
            owner = FakeOwner()
            autosub_jobs_module = autosub.jobs
            with (
                patch.object(main.runtime, "dsp_runtime", runtime),
                patch.object(main, "measurement_store", store),
                # Staging itself is covered by the owner-prearm suite; the
                # exact-mute restore contract under test needs only a staged
                # triple, and peaks predict from the equivalent config.
                patch.object(
                    autosub.measurement, "_stage_auto_sub_service_candidate",
                    new=AsyncMock(return_value={
                        "expected_native_layout": [],
                        "fingerprint": "fp-test",
                        "expected_native_output_mode": "subwoofer-2.1",
                    }),
                ),
                patch.object(
                    autosub.measurement, "_predict_auto_sub_stage_peaks",
                    side_effect=_config_prediction,
                ),
                # The pre-sweep safety decision uses a fresh unclamped live
                # master read off the loop; pin it so the candidate stays hermetic.
                patch.object(autosub.measurement, "get_output_volume_unclamped", return_value=100),
                patch.object(main.asyncio, "sleep", side_effect=no_sleep),
            ):
                autosub_deps.register_candidate_owner(job["id"], owner)
                try:
                    call = autosub_measurement._measure_auto_sub_candidate(
                            delay_ms=2.0, job=job, candidate_index=1, total=2,
                            stage="main_reference", fc=80, input_id="mic", channel="left",
                            mic_input_channel="1", reference_input_channel="", calibration_ref="",
                            calibration_filename=None, calibration_bytes=None, auto_sub_sweep_profile=sweep_profile,
                            auto_sub_rate=48_000, original_level=-3.0, original_polarity="normal",
                            original_highpass=True, exact_sub_mute=True,
                        )
                    if outcome == "restore_error":
                        with self.assertRaisesRegex(RuntimeError, "AutoSub stopped"):
                            await call
                        self.assertIn("restore failed", job["auto_gain"]["reason"].lower())
                        continue
                    result = await call
                finally:
                    autosub_deps.drop_candidate_owner(job["id"])
            self.assertFalse(runtime.muted, outcome)
            self.assertEqual(runtime.calls, [True, False], outcome)
            self.assertIn(result["status"], {"completed", "error", "cancelled"})

if __name__ == "__main__":
    unittest.main()
