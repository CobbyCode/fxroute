#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Fresh-install output fallback: the internal DSP sink is never selected.

On a fresh install there is no audio-output-selection.json yet.  When the
hardware sinks are SUSPENDED (idle) while fxroute_dsp_sink is RUNNING, the
old fallback promoted the running DSP sink to selected/effective output:
the device dropdown had no matching option (empty field) and the DSP graph
pointed at its own ingress sink.  The fallback must land on the PipeWire
default (when selectable) or the first selectable hardware sink instead,
regardless of RUNNING/SUSPENDED states.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.samplerate as samplerate
from audio.samplerate import overview

DSP = "fxroute_dsp_sink"
BUILTIN = "alsa_output.platform-auge_sound.stereo-fallback"
SMSL = "alsa_output.usb-SMSL_SMSL_USB_AUDIO-00.analog-stereo"


def _short_line(sink_id: int, name: str, state: str) -> str:
    return f"{sink_id}\t{name}\tPipeWire\ts32le 2ch 44100Hz\t{state}"


def _detailed_block(sink_id: int, name: str, description: str, state: str) -> str:
    return (
        f"Sink #{sink_id}\n"
        f"\tState: {state}\n"
        f"\tName: {name}\n"
        f"\tDescription: {description}\n"
        "\tSample Specification: s32le 2ch 44100Hz\n"
    )


def _126_like_state(*, smsl_state: str = "SUSPENDED") -> dict:
    """Live .126 shape: DSP sink RUNNING, hardware idle, default SMSL."""
    return {
        "short": "\n".join([
            _short_line(39, DSP, "RUNNING"),
            _short_line(54, BUILTIN, "SUSPENDED"),
            _short_line(56, SMSL, smsl_state),
        ]),
        "detailed": "\n".join([
            _detailed_block(39, DSP, "FXRoute_DSP_Ingress", "RUNNING"),
            _detailed_block(54, BUILTIN, "Built-in Audio Stereo", "SUSPENDED"),
            _detailed_block(56, SMSL, "SMSL USB AUDIO Analog Stereo", smsl_state),
        ]),
    }


def _status(*, relevant: str) -> dict:
    return {
        "available": True,
        "sink": {"id": 56, "name": SMSL, "description": "SMSL USB AUDIO Analog Stereo"},
        "relevant_sink": {"name": relevant},
        "notes": [],
    }


class FreshInstallOutputFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self._tmp.name

    def tearDown(self) -> None:
        if self._old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._old_xdg
        self._tmp.cleanup()

    def _overview(self, state: dict, relevant: str) -> dict:
        def fake_run(args: list[str]) -> str:
            if args[:1] == ["pactl"] and "short" in args:
                return state["short"]
            if args[:1] == ["pactl"]:
                return state["detailed"]
            if args[:2] == ["pw-cli", "ls"]:
                return ""
            raise AssertionError(f"unexpected command in fallback test: {args}")

        with mock.patch.object(
            overview, "get_samplerate_status", return_value=_status(relevant=relevant)
        ), mock.patch.object(
            overview, "get_bluetooth_audio_overview", return_value={"available": False}
        ), mock.patch.object(
            overview, "_run_command", side_effect=fake_run
        ):
            return samplerate.get_audio_output_overview()

    def _selectable_keys(self, payload: dict) -> set[str]:
        return {item["key"] for item in payload["outputs"] if item.get("selectable")}

    def test_suspended_hardware_falls_back_to_pipewire_default(self) -> None:
        payload = self._overview(_126_like_state(), relevant=DSP)
        self.assertEqual(payload["selected_output"]["key"], SMSL)
        self.assertEqual(payload["output_mode"]["effective_output_key"], SMSL)
        self.assertIn(SMSL, self._selectable_keys(payload))
        self.assertNotIn(DSP, self._selectable_keys(payload))

    def test_running_hardware_keeps_deterministic_default(self) -> None:
        payload = self._overview(_126_like_state(smsl_state="RUNNING"), relevant=SMSL)
        self.assertEqual(payload["selected_output"]["key"], SMSL)
        self.assertEqual(payload["output_mode"]["effective_output_key"], SMSL)

    def test_persisted_hardware_selection_still_wins(self) -> None:
        samplerate._save_audio_output_selection(BUILTIN)
        payload = self._overview(_126_like_state(), relevant=DSP)
        self.assertEqual(payload["selected_output"]["key"], BUILTIN)
        self.assertEqual(payload["output_mode"]["effective_output_key"], BUILTIN)

    def test_stale_dsp_sink_selection_is_ignored_with_note(self) -> None:
        samplerate._save_audio_output_selection(DSP)
        payload = self._overview(_126_like_state(), relevant=DSP)
        self.assertEqual(payload["selected_output"]["key"], SMSL)
        self.assertEqual(payload["output_mode"]["effective_output_key"], SMSL)
        self.assertTrue(
            any("not a selectable output device" in note for note in payload["notes"]),
            payload["notes"],
        )

    def test_no_selectable_sink_leaves_selection_empty_without_crash(self) -> None:
        state = {
            "short": _short_line(39, DSP, "RUNNING"),
            "detailed": _detailed_block(39, DSP, "FXRoute_DSP_Ingress", "RUNNING"),
        }
        payload = self._overview(state, relevant=DSP)
        self.assertIsNone(payload["selected_output"])
        # Last resort only: with no hardware at all the live sink is reported.
        self.assertEqual(payload["output_mode"]["effective_output_key"], DSP)


if __name__ == "__main__":
    unittest.main()
