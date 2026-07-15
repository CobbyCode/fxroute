#!/usr/bin/env python3
"""Focused tests for exact-mute AutoSub Main-only reference capture."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
from subwoofer_runtime import Subwoofer21Runtime, SubwooferRuntimeConfig


class FakeProcess:
    pid = 4242
    returncode = None

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def runtime_config() -> SubwooferRuntimeConfig:
    return SubwooferRuntimeConfig(
        output_mode="subwoofer-2.1", output_key="mock", output_label="Mock",
        output_channels=4, sample_rate=48_000, crossover_frequency_hz=80,
        main_highpass_enabled=True, sub_level_db=-3.0, sub_alignment_ms=2.0,
        sub_polarity="normal",
    )


def original_snapshot(mode: str) -> dict:
    if mode == main.OUTPUT_MODE_SUBWOOFER_21:
        return {"subwoofer": {"sub_alignment_ms": 2.0, "sub_level_db": -3.0, "sub_polarity": "normal", "main_highpass_enabled": True}}
    return {
        "main_highpass_enabled": True,
        "subwoofers": {
            "sub1": {"alignment_ms": 1.0, "level_db": -2.0, "polarity": "normal"},
            "sub2": {"alignment_ms": 3.0, "level_db": -4.0, "polarity": "normal"},
        },
    }


class ExactMuteRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def attach_ack_socket(self, runtime):
        ack_dir = __import__("tempfile").mkdtemp(prefix="fxroute-test-ack-")
        ack_path = str(Path(ack_dir) / "ack.sock")
        ack_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        ack_socket.setblocking(False)
        ack_socket.bind(ack_path)
        runtime._exact_sub_mute_ack_socket = ack_socket
        runtime._exact_sub_mute_ack_path = ack_path
        runtime._exact_sub_mute_ack_dir = ack_dir
        return ack_path

    async def test_runtime_signal_changes_only_atomic_mute_state(self):
        runtime = Subwoofer21Runtime()
        runtime._process = FakeProcess()
        runtime._config = runtime_config()
        runtime._links_configured = True
        ack_path = self.attach_ack_socket(runtime)
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

        def acknowledge(_pid, sent_signal):
            sender.sendto(b"1" if sent_signal == signal.SIGUSR1 else b"0", ack_path)

        with patch.object(os, "kill", side_effect=acknowledge) as kill:
            previous = await runtime.set_exact_sub_mute(True)
            self.assertFalse(previous)
            self.assertTrue(runtime.snapshot()["exact_sub_mute"])
            kill.assert_called_once_with(4242, signal.SIGUSR1)
            previous = await runtime.set_exact_sub_mute(False)
            self.assertTrue(previous)
            self.assertFalse(runtime.snapshot()["exact_sub_mute"])
            self.assertEqual(kill.call_args_list[-1].args, (4242, signal.SIGUSR2))
        sender.close()
        runtime._close_exact_sub_mute_ack_socket()

    async def test_helper_change_clears_mute_state(self):
        runtime = Subwoofer21Runtime()
        process = FakeProcess()
        runtime._process = process
        runtime._config = runtime_config()
        self.attach_ack_socket(runtime)
        with patch.object(os, "kill"):
            with self.assertRaisesRegex(RuntimeError, "not acknowledged"):
                await runtime.set_exact_sub_mute(True)
        self.assertFalse(runtime.snapshot()["exact_sub_mute"])
        self.assertIsNone(runtime._process)


class MainReferenceSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def test_normalization_inverse_sign_is_addition(self):
        # MeasurementStore builds normalized_db = raw_db - normalized_by_db.
        self.assertEqual(
            main._auto_sub_reconstruct_calibrated_points([[20.0, -7.5], [80.0, 1.25]], -12.5),
            [[20.0, -20.0], [80.0, -11.25]],
        )

    async def test_exactly_two_structurally_identical_references_for_all_modes(self):
        for mode in (
            main.OUTPUT_MODE_SUBWOOFER_21,
            main.OUTPUT_MODE_SUBWOOFER_22,
            main.OUTPUT_MODE_SUBWOOFER_22_STEREO,
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

            job = {"auto_gain": {"available": False, "reason": "gain not implemented"}}
            with patch.object(main, "_measure_auto_sub_candidate", side_effect=fake_measure):
                await main._capture_auto_sub_main_references(
                    job=job, fc=80, input_id="mic", mic_input_channel="1",
                    reference_input_channel="", calibration_ref="", calibration_filename=None,
                    calibration_bytes=None, auto_sub_sweep_profile={}, auto_sub_rate=48_000,
                    output_mode=mode, original_config_snapshot=original_snapshot(mode),
                )
            self.assertEqual(len(calls), 2)
            self.assertEqual([call["channel"] for call in calls], ["left", "right"])
            self.assertTrue(all(call["exact_sub_mute"] for call in calls))
            self.assertTrue(all(call["active_subs"] == ("sub1", "sub2") for call in calls))
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
        with patch.object(main, "_measure_auto_sub_candidate", side_effect=fake_measure):
            await main._capture_auto_sub_main_references(
                job=job, fc=80, input_id="mic", mic_input_channel="1", reference_input_channel="",
                calibration_ref="", calibration_filename=None, calibration_bytes=None,
                auto_sub_sweep_profile={}, auto_sub_rate=48_000,
                output_mode=main.OUTPUT_MODE_SUBWOOFER_21,
                original_config_snapshot=original_snapshot(main.OUTPUT_MODE_SUBWOOFER_21),
            )
        self.assertEqual(job["main_references"]["status"], "unavailable")
        self.assertIn("left", job["auto_gain"]["reason"])
        self.assertNotIn("status", job)  # Existing optimization state is not failed here.

    async def test_candidate_restores_exact_mute_on_success_error_and_cancel(self):
        class FakeRuntime:
            def __init__(self, fail_restore=False):
                self.muted = False
                self.calls = []
                self.fail_restore = fail_restore

            async def sync(self, _config):
                return None

            async def set_exact_sub_mute(self, enabled):
                previous = self.muted
                self.calls.append(bool(enabled))
                if not enabled and self.fail_restore:
                    raise RuntimeError("restore acknowledgement missing")
                self.muted = bool(enabled)
                return previous

            def snapshot(self):
                return {"exact_sub_mute": self.muted}

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

        for outcome in ("success", "error", "cancel", "restore_error"):
            job = {"cancel_requested": False, "_sweep_timings": [], "auto_gain": {"available": False, "reason": "pending"}}
            runtime = FakeRuntime(fail_restore=outcome == "restore_error")
            store = FakeStore("success" if outcome == "restore_error" else outcome, job)
            with (
                patch.object(main, "subwoofer_runtime", runtime),
                patch.object(main, "measurement_store", store),
                patch.object(main, "set_audio_output_mode"),
                patch.object(main, "get_audio_output_overview", return_value={}),
                patch.object(main.SubwooferRuntimeConfig, "from_overview", return_value=runtime_config()),
                patch.object(main, "_prepare_subwoofer_runtime_for_measurement_start", new_callable=AsyncMock, return_value=None),
                patch.object(main.asyncio, "sleep", side_effect=no_sleep),
                patch("samplerate._load_audio_output_mode", return_value={"subwoofer": {"sub_alignment_ms": 2.0}}),
            ):
                call = main._measure_auto_sub_candidate(
                        delay_ms=2.0, job=job, candidate_index=1, total=2,
                        stage="main_reference", fc=80, input_id="mic", channel="left",
                        mic_input_channel="1", reference_input_channel="", calibration_ref="",
                        calibration_filename=None, calibration_bytes=None, auto_sub_sweep_profile={},
                        auto_sub_rate=48_000, original_level=-3.0, original_polarity="normal",
                        original_highpass=True, exact_sub_mute=True,
                    )
                if outcome == "restore_error":
                    with self.assertRaisesRegex(RuntimeError, "AutoSub stopped"):
                        await call
                    self.assertIn("restore failed", job["auto_gain"]["reason"].lower())
                    continue
                result = await call
            self.assertFalse(runtime.muted, outcome)
            self.assertEqual(runtime.calls, [True, False], outcome)
            self.assertIn(result["status"], {"completed", "error", "cancelled"})

    def test_helper_dsp_zeroes_only_sub_outputs_without_graph_operations(self):
        source = (ROOT / "pipewire_stage1" / "fxroute_21_passthrough.c").read_text(encoding="utf-8")
        self.assertIn("output_3[frame] = exact_sub_mute ? 0.0f : sub1;", source)
        self.assertIn("output_4[frame] = exact_sub_mute ? 0.0f : sub2;", source)
        self.assertIn("output_1[frame] = delay_line_process", source)
        self.assertIn("output_2[frame] = delay_line_process", source)
        self.assertNotIn("pw_link", source)


if __name__ == "__main__":
    unittest.main()
