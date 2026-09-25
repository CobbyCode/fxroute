#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Regression: output device fallback, atomic policy persist, apply TOCTOU.

1. Stale saved output device: output_mode derives from the validated
   effective device, so reported device and mode agree after fallback.
2. Sample-rate policy persist is atomic: a failure before the final
   replace keeps the previous file; reload still sees the old state.
3. OutputService.apply refuses when measurement takes ownership between
   the initial guard and the commit; nothing is committed.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STALE = "alsa_output.usb-Stale_Device.analog-stereo"
EFFECTIVE = "alsa_output.usb-Real_Device.analog-surround-40"


def _short_line(sid: int, name: str, spec: str, state: str) -> str:
    return f"{sid}\t{name}\tPipeWire\t{spec}\t{state}"


def _detailed_block(sid: int, name: str, desc: str, spec: str,
                    state: str, channel_map: str | None = None) -> str:
    block = (f"Sink #{sid}\n\tState: {state}\n\tName: {name}\n"
             f"\tDescription: {desc}\n\tSample Specification: {spec}\n")
    if channel_map is not None:
        block += f"\tChannel Map: {channel_map}\n"
    return block


class StaleDeviceFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self._tmp.name
        self.addCleanup(self._restore_xdg)

    def _restore_xdg(self) -> None:
        if self._old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._old_xdg

    def tearDown(self) -> None:
        from audio.samplerate.overview import configure_output_state_head
        configure_output_state_head(None)

    def _overview_with_stale_selection(self):
        import audio.samplerate as samplerate
        from audio.samplerate import overview as ov
        from audio.output_state import (
            default_output_state, set_mode_routing, switch_mode)

        samplerate._save_audio_output_selection(STALE)
        head = switch_mode(set_mode_routing(
            default_output_state(), "stereo-sub", EFFECTIVE,
            ["main_l", "main_r", "sub1", "sub1"]), "stereo-sub")
        head["revision"] = 1
        ov.configure_output_state_head(lambda: head)

        spec = "s32le 4ch 48000Hz"
        short = _short_line(42, EFFECTIVE, spec, "IDLE")
        detailed = _detailed_block(
            42, EFFECTIVE, "Real Device", spec, "IDLE",
            channel_map="front-left,front-right,rear-left,rear-right")
        status = {"available": True,
                  "sink": {"id": 42, "name": EFFECTIVE,
                           "description": "Real Device"},
                  "relevant_sink": {"name": EFFECTIVE}, "notes": []}

        def fake_run(args):
            if args[:1] == ["pactl"] and "short" in args:
                return short
            if args[:1] == ["pactl"]:
                return detailed
            if args[:2] == ["pw-cli", "ls"]:
                return ""
            if args[:2] == ["pw-cli", "enum-params"]:
                return ""
            if args[:1] == ["pw-link"]:
                return ""
            raise AssertionError(f"unexpected command: {args}")

        with mock.patch.object(ov, "get_samplerate_status",
                               return_value=status), \
             mock.patch.object(ov, "get_bluetooth_audio_overview",
                               return_value={"available": False}), \
             mock.patch.object(ov, "_run_command", side_effect=fake_run):
            return ov.get_audio_output_overview()

    def test_stale_selection_falls_back_with_matching_mode(self) -> None:
        payload = self._overview_with_stale_selection()
        mode = payload["output_mode"]
        selected = payload.get("selected_output") or {}
        self.assertEqual(selected.get("key"), EFFECTIVE)
        self.assertEqual(mode.get("effective_output_key"), EFFECTIVE)
        # Effective device carries sub routing: mode must follow it.
        self.assertEqual(mode.get("mode"), "subwoofer-2.1")
        self.assertTrue(any("not currently available" in str(note)
                            for note in payload.get("notes") or []))


class AtomicPolicyPersistTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self._tmp.name
        self.addCleanup(self._restore_xdg)

    def _restore_xdg(self) -> None:
        if self._old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._old_xdg

    def test_write_reload_roundtrip(self) -> None:
        from audio.samplerate import persistence as per
        per.persist_sample_rate_policy({"mode": "fixed", "rate": 48000})
        self.assertEqual(per.load_sample_rate_policy(),
                         {"mode": "fixed", "rate": 48000})

    def test_failure_before_replace_keeps_previous_state(self) -> None:
        from audio.samplerate import persistence as per
        per.persist_sample_rate_policy({"mode": "fixed", "rate": 48000})
        path = per._sample_rate_policy_path()
        before = path.read_bytes()
        with mock.patch("os.replace",
                        side_effect=RuntimeError("simulated pre-replace crash")):
            with self.assertRaises(RuntimeError):
                per.persist_sample_rate_policy({"mode": "fixed", "rate": 96000})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(per.load_sample_rate_policy(),
                         {"mode": "fixed", "rate": 48000})
        self.assertEqual(
            list(path.parent.glob("sample-rate-policy.json.*.tmp")), [])


class ApplyMeasurementGuardTests(unittest.TestCase):
    def test_mid_apply_ownership_refuses_commit(self) -> None:
        from audio.output_service import (
            MeasurementActiveError, OutputService, OutputServiceDeps)
        from audio.output_state import (
            default_output_state, set_mode_routing, switch_mode)
        from audio.output_state_store import OutputStateStore

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output-state.json"
            active = {"value": False}
            service = OutputService(OutputServiceDeps(
                store=OutputStateStore(path),
                preset_loader=lambda name: {"chain": []},
                resolve_ir=lambda kernel: {"path": "x", "channels": 1},
                measurement_active=lambda: active["value"],
            ))
            base = set_mode_routing(default_output_state(), "stereo",
                                    "A", ["main_l", "main_r"])
            service.commit(base, expected_revision=0)
            before = service.load()
            before_bytes = path.read_bytes()

            def sneaky_mutate(state):
                active["value"] = True
                return switch_mode(state, "stereo-sub")

            with self.assertRaises(MeasurementActiveError):
                service.apply(sneaky_mutate, expected_revision=before["revision"])
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(service.load()["revision"], before["revision"])
            self.assertEqual(service.load()["active_mode"], before["active_mode"])

    def test_quiet_apply_still_commits(self) -> None:
        from audio.output_service import OutputService, OutputServiceDeps
        from audio.output_state import (
            default_output_state, set_mode_routing, switch_mode)
        from audio.output_state_store import OutputStateStore

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output-state.json"
            service = OutputService(OutputServiceDeps(
                store=OutputStateStore(path),
                preset_loader=lambda name: {"chain": []},
                resolve_ir=lambda kernel: {"path": "x", "channels": 1},
                measurement_active=lambda: False,
            ))
            base = set_mode_routing(default_output_state(), "stereo",
                                    "A", ["main_l", "main_r"])
            service.commit(base, expected_revision=0)
            result = service.apply(
                lambda state: switch_mode(state, "stereo-sub"),
                expected_revision=1)
            self.assertEqual(result["revision"], 2)
            self.assertEqual(result["active_mode"], "stereo-sub")


if __name__ == "__main__":
    unittest.main(verbosity=2)
