#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Capture/reference context survives real single and L/R repeat analysis."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import default_output_state, set_mode_routing, switch_mode
from measurement.store import MeasurementStore
from measurement.target import freeze_measurement_target
from test_measurement_band_limited_reference import apply_band, crossover_spec


class MeasurementReferenceModeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="reference-modes-")
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        environment = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.home / "config"),
            "XDG_STATE_HOME": str(self.home / "state"),
            "XDG_DATA_HOME": str(self.home / "data"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.store = MeasurementStore(home=self.home)
        self.reference_filters = []
        self.reference_carriers = [1]
        self.clip_electrical_reference = False
        self.recordings = 0
        self.output_state = switch_mode(set_mode_routing(
            default_output_state(), "stereo-sub", "output", ["main_l", "main_r", "sub1", "sub2"]),
            "stereo-sub")

        # Only PipeWire/process operations are replaced. Capture policy,
        # analysis, pre-averaging, summaries and persistence stay real.
        routing = self.store._routing
        routing._resolve_playback_target = lambda **kw: {
            "target_name": "fxroute_dsp_sink", "target_label": "DSP"}
        routing._resolve_host_reference_capture = lambda **kw: {
            "source_node_name": "fxroute_dsp_sink.monitor", "channel_label": "monitor_FL"}
        routing._build_measurement_playback_route = lambda *args, **kw: {"route": "test"}
        routing._build_measurement_play_command = lambda **kw: ["pw-play"]
        routing._build_pre_sweep_state_snapshot = lambda **kw: {}
        routing._cleanup_fxroute_links = lambda **kw: None
        routing._cleanup_measurement_playback_links = lambda **kw: None
        routing._link_capture_channels_to_record_stream = lambda **kw: {}
        routing._link_host_reference_capture = lambda **kw: {}
        routing._lookup_pipewire_audio_node = lambda node: {"id": 1, "name": node}
        self.store._pw_record_supports_option = lambda _: True
        self.store._snapshot_fxroute_21_helper_processes = lambda _: {}
        self.store._monitor_capture_input_level = lambda *args: None
        self.store._start_job_process = self.spawn
        for target, value in (
            ("measurement.store.get_node_volume", 100),
            ("measurement.store.set_node_volume", None),
            ("measurement.store.get_audio_output_overview", {"output_mode": {"mode": "stereo-sub"}}),
        ):
            hardware = patch(target, return_value=value)
            hardware.start()
            self.addCleanup(hardware.stop)
        sleeping = patch("measurement.host_capture.time.sleep", lambda _: None)
        sleeping.start()
        self.addCleanup(sleeping.stop)

    def spawn(self, owner_job_id, command):
        if command[0] == "pw-record":
            self.recordings += 1
            capture_path = Path(command[-1])
            playback_path = self.store.playbacks_dir / capture_path.name
            rate, playback = self.store._load_wav_array(playback_path)
            program = playback[:, 1] if "-right" in capture_path.stem else playback[:, 0]
            host_reference = np.concatenate([np.zeros(rate // 2), program * 0.25, np.zeros(rate // 2)])
            if self.reference_filters:
                program = apply_band(program, self.reference_filters)
            reference = np.concatenate([np.zeros(rate // 2), program * 0.25, np.zeros(rate // 2)])
            mic = np.zeros_like(reference)
            mic[240:] = reference[:-240] * 0.5
            count = int(command[command.index("--channels") + 1])
            capture = np.zeros((reference.size, count))
            if "--channel-map" in command:
                capture[:, 0] = mic
                carriers = self.reference_carriers(capture_path) if callable(self.reference_carriers) else self.reference_carriers
                for index in carriers:
                    if index < count:
                        capture[:, index] = np.clip(reference * 10.0, -1.0, 1.0) if self.clip_electrical_reference else reference
            else:
                capture[:, 0] = host_reference
                capture[:, 1] = mic
            self.store._write_wav(capture_path, capture, rate)
        return SimpleNamespace(returncode=0, communicate=lambda **kw: ("", ""), poll=lambda: 0)

    def job(self, bank="global", *, reference=True, frozen=True):
        job = {
            "id": "measurement-job-reference-modes",
            "channel": "left",
            "measurement_scope": "active_chain",
            "measurement_bank": bank,
            "input": {"id": "mic", "label": "Mic", "node_name": "capture_1", "sample_rate": 48000},
            "input_channels": {"mic": 1, "electrical_reference": 2 if reference else None},
        }
        if frozen:
            job["measurement_target"] = freeze_measurement_target(
                self.output_state, bank_id=bank, output_key="output", channels=4,
                sample_rate_hz=48000, fingerprint="frozen")
        return job

    def repeat(self, job):
        job.update(id="measurement-repeat-job-reference-modes", repeat_count=2, base_name="Take")
        return self.store._execute_lr_repeat_job(job)

    def test_global_reference_uses_broadband_timing(self):
        result = self.store._execute_capture_job(self.job())
        analysis = result["analysis"]
        self.assertTrue(analysis["reference_path"]["electrical_reference_used"])
        self.assertFalse(analysis["clock"]["band_limited_reference"])
        self.assertEqual(analysis["clock"]["region_anchor_sources"]["start"], "registers")

    def test_frozen_global_target_wins_over_stale_job_bank(self):
        job = self.job()
        job["measurement_bank"] = "sub1"
        result = self.store._execute_capture_job(job)
        self.assertFalse(result["analysis"]["clock"]["band_limited_reference"])

    def test_legacy_global_reference_is_not_an_isolated_area(self):
        result = self.store._execute_capture_job(self.job(frozen=False))
        self.assertFalse(result["analysis"]["clock"]["band_limited_reference"])

    def test_missing_frozen_bank_is_not_an_isolated_area(self):
        job = self.job()
        job["measurement_target"]["bank_id"] = None
        result = self.store._execute_capture_job(job)
        self.assertFalse(result["analysis"]["clock"]["band_limited_reference"])

    def test_isolated_sub_reference_keeps_band_aware_timing_in_repeat(self):
        self.reference_filters = [crossover_spec("lowpass", 80.0)]
        result = self.repeat(self.job("sub1"))
        self.assertEqual(self.recordings, 4, "Usable references must not trigger host recaptures")
        for summary in result["measurements"]:
            with self.subTest(side=summary["channel"]):
                analysis = summary["analysis"]
                self.assertTrue(analysis["lr_repeat"].get("pre_averaged"))
                self.assertTrue(analysis["clock"]["band_limited_reference"])
                self.assertGreaterEqual(analysis["clock"]["end_score"], 0.84)
                self.assertTrue(analysis["reference_path"].get("electrical_reference_used"))
                self.assertEqual(summary["measurement_target"]["bank_id"], "sub1")

    def test_global_repeat_keeps_broadband_context(self):
        result = self.repeat(self.job())
        for summary in result["measurements"]:
            with self.subTest(side=summary["channel"]):
                self.assertTrue(summary["analysis"]["lr_repeat"].get("pre_averaged"))
                self.assertFalse(summary["analysis"]["clock"]["band_limited_reference"])
                self.assertTrue(summary["analysis"]["reference_path"].get("electrical_reference_used"))
                self.assertEqual(summary["analysis"]["reference_path"].get("capture_mode"), "electrical-input")
                self.assertEqual(summary["analysis"]["clock"].get("reference_capture_mode"), "electrical-input")
                self.assertEqual(summary["analysis"]["reference_path"].get("electrical_reference_input_channel"), 2)
                self.assertEqual(summary["analysis"]["clock"]["timing_channel"], "input_2_electrical_reference")

    def candidate_job(self):
        job = self.job("main")
        job["input_channels"]["electrical_reference_candidates"] = [2, 3]
        self.reference_carriers = [2]
        return job

    def test_private_capture_metadata_names_the_admitted_reference(self):
        result = self.store._execute_capture_job(self.candidate_job())
        self.assertEqual(result["capture"]["electrical_reference_input_channel"], 3)
        self.assertEqual(result["_electrical_reference_channel_index"], 2)
        self.assertTrue(result["_use_electrical_reference"])

    def test_repeat_averages_the_admitted_channel_not_its_silent_sibling(self):
        result = self.repeat(self.candidate_job())
        self.assertEqual(self.recordings, 4)
        for summary in result["measurements"]:
            with self.subTest(side=summary["channel"]):
                self.assertTrue(summary["analysis"]["lr_repeat"].get("pre_averaged"))
                self.assertEqual(summary["input_channels"]["electrical_reference"], 3)

    def test_split_left_right_references_keep_their_physical_channels(self):
        job = self.job()
        job["input_channels"].update(electrical_reference_left=2, electrical_reference_right=3)
        self.reference_carriers = lambda path: [2] if "-right" in path.stem else [1]
        result = self.repeat(job)
        self.assertEqual(self.recordings, 4)
        for summary, channel in zip(result["measurements"], (2, 3)):
            with self.subTest(side=summary["channel"]):
                analysis = summary["analysis"]
                self.assertTrue(analysis["lr_repeat"].get("pre_averaged"))
                self.assertTrue(analysis["reference_path"]["electrical_reference_used"])
                self.assertEqual(analysis["reference_path"]["electrical_reference_input_channel"], channel)
                self.assertEqual(analysis["clock"]["timing_channel"], f"input_{channel}_electrical_reference")

    def stereo_sub_job(self, bank):
        job = self.job(bank)
        job["channel"] = "stereo"
        job["input_channels"].update(
            electrical_reference=7, electrical_reference_left=7, electrical_reference_right=8)
        self.reference_filters = [crossover_spec("lowpass", 80.0)]
        return job

    def test_stereo_subs_admit_either_configured_reference_without_recapture(self):
        for bank in ("sub1", "sub2"):
            for carrier in (7, 8):
                with self.subTest(bank=bank, carrier=carrier):
                    job = self.stereo_sub_job(bank)
                    self.reference_carriers = [carrier - 1]
                    recordings_before = self.recordings
                    result = self.store._execute_capture_job(job)
                    reference = result["analysis"]["reference_path"]
                    self.assertTrue(reference["electrical_reference_used"])
                    self.assertFalse(reference.get("electrical_reference_fallback", False))
                    self.assertEqual(reference["electrical_reference_input_channel"], carrier)
                    self.assertEqual(result["capture"]["electrical_reference_input_channel"], carrier)
                    self.assertEqual(result["_electrical_reference_channel_index"], carrier - 1)
                    self.assertEqual(self.recordings - recordings_before, 1)
                    candidates = reference.get("electrical_reference_candidates", [])
                    self.assertEqual([item["input_channel"] for item in candidates], [7, 8])
                    self.assertEqual([item["usable"] for item in candidates], [carrier == 7, carrier == 8])

    def test_stereo_sub_falls_back_when_both_configured_references_are_invalid(self):
        for carriers, clipped in (([], False), ([6, 7], True)):
            with self.subTest(carriers=carriers, clipped=clipped):
                job = self.stereo_sub_job("sub2")
                self.reference_carriers = carriers
                self.clip_electrical_reference = clipped
                recordings_before = self.recordings
                result = self.store._execute_capture_job(job)
                reference = result["analysis"]["reference_path"]
                self.assertTrue(reference["electrical_reference_fallback"])
                self.assertFalse(reference["electrical_reference_used"])
                self.assertIsNone(result["capture"]["electrical_reference_input_channel"])
                self.assertFalse(result["_use_electrical_reference"])
                self.assertEqual(self.recordings - recordings_before, 2)

    def test_stereo_disabled_sibling_does_not_override_the_admitted_reference(self):
        job = self.stereo_sub_job("sub2")
        reason = "Mic input and electrical reference L input are the same channel; reference compensation disabled."
        job["input_channels"].update(
            electrical_reference=8, electrical_reference_left=None,
            reference_disabled_reason=reason, reference_disabled_reason_left=reason,
            reference_disabled_reason_right="")
        self.reference_carriers = [7]
        result = self.store._execute_capture_job(job)
        reference = result["analysis"]["reference_path"]
        self.assertTrue(reference["electrical_reference_used"])
        self.assertFalse(reference.get("electrical_reference_fallback", False))
        self.assertEqual(reference["electrical_reference_input_channel"], 8)
        self.assertTrue(result["_use_electrical_reference"])
        self.assertEqual(result["_electrical_reference_channel_index"], 7)
        self.assertEqual(self.recordings, 1)

    def test_changed_admitted_channel_uses_per_sweep_analysis(self):
        job = self.candidate_job()
        self.reference_carriers = lambda path: [1] if "-repeat1-" in path.stem else [2]
        with self.assertLogs("measurement.repeat_runner", level="WARNING") as logs:
            result = self.repeat(job)
        for summary in result["measurements"]:
            self.assertFalse(summary["analysis"]["lr_repeat"].get("pre_averaged"))
            self.assertTrue(summary["analysis"]["lr_repeat"]["electrical_reference_used"])
        self.assertTrue(all("reference channel changed" in line.lower() for line in logs.output), logs.output)

    def test_host_reference_keeps_its_own_capture_mode(self):
        result = self.store._execute_capture_job(self.job("sub1", reference=False))
        self.assertEqual(result["analysis"]["clock"]["reference_capture_mode"], "dual-channel")
        self.assertFalse(result["_use_electrical_reference"])
        self.assertIsNone(result["_electrical_reference_channel_index"])
        self.assertFalse(result["analysis"]["clock"]["band_limited_reference"])
        self.assertFalse(result["analysis"]["reference_path"]["electrical_reference_used"])

    def test_rejected_reference_does_not_leave_private_metadata_in_electrical_mode(self):
        self.clip_electrical_reference = True
        result = self.store._execute_capture_job(self.job())
        self.assertEqual(self.recordings, 2)
        self.assertTrue(result["analysis"]["reference_path"]["electrical_reference_fallback"])
        self.assertIsNone(result["capture"]["electrical_reference_input_channel"])
        self.assertFalse(result["_use_electrical_reference"])
        self.assertIsNone(result["_electrical_reference_channel_index"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
