#!/usr/bin/env python3
"""Focused tests: output device switches are selection-only.

A deliberate device switch changes the selection (and the default sink) and
nothing else: topology and DSP state live in the v2 output state and follow
explicitly through its own apply path. There is no per-device mode memory;
stale device_modes entries in old mode files are ignored and mode files are
never written by a device switch.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.samplerate as samplerate
from audio.samplerate.overview import set_audio_output_selection

UMC = "alsa_output.usb-Behringer_UMC204HD.analog-stereo"
SMSL = "alsa_output.usb-SMSL_DAC.analog-stereo"
OTHER = "alsa_output.usb-Other_DAC.analog-stereo"
MULTI = "alsa_output.usb-Multi_DAC.analog-stereo"


def _outputs(channels_by_key: dict[str, int], *, selectable: set[str] | None = None) -> list[dict]:
    if selectable is None:
        selectable = set(channels_by_key)
    return [
        {
            "key": key,
            "name": key,
            "label": key,
            "channels": channels,
            "selectable": key in selectable,
            "supported_rates": [44100, 48000, 96000],
            "active_rate": 48000,
            "is_current": False,
            "is_selected": False,
        }
        for key, channels in channels_by_key.items()
    ]


class OutputDeviceModeFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config_home = Path(self._tmp.name)
        self._old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.config_home)

    def tearDown(self) -> None:
        if self._old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._old_xdg
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------------

    def _read_mode_file(self) -> dict:
        path = self.config_home / "fxroute" / "audio-output-mode.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def _seed_mode_file(self, payload: dict) -> None:
        path = self.config_home / "fxroute" / "audio-output-mode.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n")

    def _persist_mode(self, mode: str) -> None:
        # Legacy persist helpers are deleted; seed a representative stale
        # file directly. The selection path must leave it byte-identical.
        self._seed_mode_file({"mode": mode})

    def _select(self, key: str, channels_by_key: dict[str, int]) -> dict:
        def live_overview() -> dict:
            return {
                "outputs": _outputs(channels_by_key),
                "available": True,
                "output_mode": {"mode": "stereo"},
            }

        with mock.patch(
            "audio.samplerate.overview.get_audio_output_overview",
            side_effect=live_overview,
        ), mock.patch("audio.samplerate.overview._set_default_sink") as set_sink:
            result = set_audio_output_selection(key)
        return result

    # -- scenarios ----------------------------------------------------------

    def test_switch_to_stereo_only_device_keeps_selection_only(self) -> None:
        samplerate._save_audio_output_selection(UMC)
        self._persist_mode("subwoofer-2.2")

        result = self._select(SMSL, {UMC: 4, SMSL: 2})

        # The mode file is untouched by the switch: no fallback persist,
        # no device memory written.
        self.assertEqual(self._read_mode_file()["mode"], "subwoofer-2.2")
        self.assertEqual(
            samplerate._load_audio_output_selection()["selected_key"], SMSL
        )
        self.assertNotIn("mode_adjustment", result["output_mode"])

    def test_return_switch_has_no_mode_memory(self) -> None:
        samplerate._save_audio_output_selection(UMC)
        self._persist_mode("subwoofer-2.2")
        self._select(SMSL, {UMC: 4, SMSL: 2})

        result = self._select(UMC, {UMC: 4, SMSL: 2})

        self.assertEqual(self._read_mode_file()["mode"], "subwoofer-2.2")
        self.assertEqual(
            samplerate._load_audio_output_selection()["selected_key"], UMC
        )
        self.assertNotIn("mode_adjustment", result["output_mode"])

    def test_stereo_to_other_stereo_device_no_mode_change(self) -> None:
        samplerate._save_audio_output_selection(SMSL)
        self._persist_mode("stereo")

        result = self._select(OTHER, {SMSL: 2, OTHER: 2})

        self.assertEqual(self._read_mode_file()["mode"], "stereo")
        self.assertNotIn("mode_adjustment", result["output_mode"])

    def test_multichannel_switch_ignores_stale_device_memory(self) -> None:
        # MULTI was once remembered as 2.1; current persisted mode is stereo.
        samplerate._save_audio_output_selection(SMSL)
        self._persist_mode("stereo")
        payload = self._read_mode_file()
        payload["device_modes"] = {MULTI: "subwoofer-2.1"}
        (self.config_home / "fxroute" / "audio-output-mode.json").write_text(
            json.dumps(payload, indent=2) + "\n"
        )

        result = self._select(MULTI, {MULTI: 4, SMSL: 2})

        self.assertEqual(self._read_mode_file()["mode"], "stereo")
        self.assertEqual(
            samplerate._load_audio_output_selection()["selected_key"], MULTI
        )
        self.assertNotIn("mode_adjustment", result["output_mode"])

    def test_bogus_device_memory_is_ignored(self) -> None:
        # SMSL was (incorrectly) remembered as 2.1; the memory is ignored.
        samplerate._save_audio_output_selection(UMC)
        self._persist_mode("subwoofer-2.2")
        payload = self._read_mode_file()
        payload["device_modes"] = {SMSL: "subwoofer-2.1", UMC: "subwoofer-2.2"}
        (self.config_home / "fxroute" / "audio-output-mode.json").write_text(
            json.dumps(payload, indent=2) + "\n"
        )

        result = self._select(SMSL, {UMC: 4, SMSL: 2})

        self.assertEqual(self._read_mode_file()["mode"], "subwoofer-2.2")
        self.assertEqual(
            samplerate._load_audio_output_selection()["selected_key"], SMSL
        )
        self.assertNotIn("mode_adjustment", result["output_mode"])

    # NOTE (backend-v2 migration): per-device mode memory and its loader
    # are deleted with the persistence helpers; stale device_modes
    # entries in old files are ignored (covered above).


    def test_unknown_and_non_selectable_outputs_still_error(self) -> None:
        with self.assertRaises(ValueError):
            set_audio_output_selection("no-such-output")
        with mock.patch(
            "audio.samplerate.overview.get_audio_output_overview",
            return_value={"outputs": _outputs({SMSL: 2}, selectable=set())},
        ), self.assertRaises(ValueError):
            set_audio_output_selection(SMSL)

    def test_fixed_rate_mismatch_still_errors(self) -> None:
        samplerate.persist_sample_rate_policy({"mode": "fixed", "rate": 192000})
        with mock.patch(
            "audio.samplerate.overview.get_audio_output_overview",
            return_value={"outputs": _outputs({SMSL: 2})},
        ), self.assertRaises(ValueError):
            set_audio_output_selection(SMSL)


if __name__ == "__main__":
    unittest.main()
