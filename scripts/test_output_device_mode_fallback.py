#!/usr/bin/env python3
"""Focused tests: output device switch auto-falls back the output mode.

A deliberate switch to a stereo-only device while a subwoofer mode is active
must succeed by falling back to Stereo instead of refusing the switch, and
the last valid mode per output device must be remembered so a later switch
back restores it.  Only valid, capability-checked combinations are stored.
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

    def _device_modes(self) -> dict[str, str]:
        return samplerate._load_device_output_modes()

    def _persist_mode(self, mode: str) -> None:
        samplerate.persist_audio_output_mode(
            samplerate._build_audio_output_mode_payload(mode)
        )

    def _select(self, key: str, channels_by_key: dict[str, int]) -> dict:
        def live_overview() -> dict:
            return {
                "outputs": _outputs(channels_by_key),
                "available": True,
                "output_mode": dict(samplerate._load_audio_output_mode()),
            }

        with mock.patch(
            "audio.samplerate.overview.get_audio_output_overview",
            side_effect=live_overview,
        ), mock.patch("audio.samplerate.overview._set_default_sink") as set_sink:
            result = set_audio_output_selection(key)
        return result

    # -- scenarios ----------------------------------------------------------

    def test_fallback_to_stereo_on_stereo_only_device(self) -> None:
        samplerate._save_audio_output_selection(UMC)
        self._persist_mode("subwoofer-2.2")
        self.assertEqual(self._device_modes().get(UMC), "subwoofer-2.2")

        result = self._select(SMSL, {UMC: 4, SMSL: 2})

        file_payload = self._read_mode_file()
        self.assertEqual(file_payload["mode"], "stereo")
        self.assertEqual(self._device_modes().get(UMC), "subwoofer-2.2")
        self.assertEqual(self._device_modes().get(SMSL), "stereo")
        self.assertEqual(
            samplerate._load_audio_output_selection()["selected_key"], SMSL
        )
        adjustment = result["output_mode"]["mode_adjustment"]
        self.assertTrue(adjustment["adjusted"])
        self.assertEqual(adjustment["reason"], "device-channel-capacity")
        self.assertEqual(adjustment["previous_mode"], "subwoofer-2.2")
        self.assertIn("Stereo", adjustment["message"])
        self.assertIn("2 channels", adjustment["message"])

    def test_restores_remembered_mode_on_return(self) -> None:
        samplerate._save_audio_output_selection(UMC)
        self._persist_mode("subwoofer-2.2")
        self._select(SMSL, {UMC: 4, SMSL: 2})  # fallback to stereo on SMSL
        self.assertEqual(self._device_modes().get(SMSL), "stereo")

        result = self._select(UMC, {UMC: 4, SMSL: 2})

        self.assertEqual(self._read_mode_file()["mode"], "subwoofer-2.2")
        self.assertEqual(self._device_modes().get(UMC), "subwoofer-2.2")
        adjustment = result["output_mode"]["mode_adjustment"]
        self.assertEqual(adjustment["reason"], "device-remembered-mode")
        self.assertIn("2.2", adjustment["message"])

    def test_stereo_to_other_stereo_device_no_mode_change(self) -> None:
        samplerate._save_audio_output_selection(SMSL)
        self._persist_mode("stereo")

        result = self._select(OTHER, {SMSL: 2, OTHER: 2})

        self.assertEqual(self._read_mode_file()["mode"], "stereo")
        self.assertNotIn(OTHER, self._device_modes())
        self.assertNotIn("mode_adjustment", result["output_mode"])

    def test_multichannel_device_uses_remembered_mode(self) -> None:
        # MULTI previously ran 2.1; current persisted mode is stereo.
        samplerate._save_audio_output_selection(SMSL)
        self._persist_mode("stereo")
        payload = self._read_mode_file()
        payload["device_modes"] = {MULTI: "subwoofer-2.1"}
        (self.config_home / "fxroute" / "audio-output-mode.json").write_text(
            json.dumps(payload, indent=2) + "\n"
        )

        result = self._select(MULTI, {MULTI: 4, SMSL: 2})

        self.assertEqual(self._read_mode_file()["mode"], "subwoofer-2.1")
        adjustment = result["output_mode"]["mode_adjustment"]
        self.assertEqual(adjustment["reason"], "device-remembered-mode")
        self.assertIn("2.1", adjustment["message"])

    def test_stale_remembered_mode_falls_back_to_capability(self) -> None:
        # SMSL was (incorrectly) remembered as 2.1; it only supports 2 channels.
        samplerate._save_audio_output_selection(UMC)
        self._persist_mode("subwoofer-2.2")
        payload = self._read_mode_file()
        payload["device_modes"] = {SMSL: "subwoofer-2.1", UMC: "subwoofer-2.2"}
        (self.config_home / "fxroute" / "audio-output-mode.json").write_text(
            json.dumps(payload, indent=2) + "\n"
        )

        result = self._select(SMSL, {UMC: 4, SMSL: 2})

        self.assertEqual(self._read_mode_file()["mode"], "stereo")
        self.assertEqual(self._device_modes().get(SMSL), "stereo")
        adjustment = result["output_mode"]["mode_adjustment"]
        self.assertEqual(adjustment["reason"], "device-channel-capacity")

    def test_persist_audio_output_mode_records_current_device(self) -> None:
        samplerate._save_audio_output_selection(OTHER)
        self._persist_mode("stereo")
        self.assertEqual(self._device_modes().get(OTHER), "stereo")

        # No selected device -> nothing recorded, no device_modes key added.
        path = self.config_home / "fxroute" / "audio-output-mode.json"
        payload = json.loads(path.read_text())
        del payload["device_modes"]
        path.write_text(json.dumps(payload, indent=2) + "\n")
        os.remove(self.config_home / "fxroute" / "audio-output-selection.json")
        self._persist_mode("stereo")
        self.assertNotIn("device_modes", self._read_mode_file())

    def test_load_device_output_modes_filters_invalid(self) -> None:
        path = self.config_home / "fxroute" / "audio-output-mode.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "mode": "stereo",
            "device_modes": {
                UMC: "subwoofer-2.2",
                "bad-mode": "bogus",
                "empty": "",
                "numeric": 42,
                "valid-21": "subwoofer-2.1",
            },
        }, indent=2) + "\n")
        self.assertEqual(
            self._device_modes(),
            {UMC: "subwoofer-2.2", "valid-21": "subwoofer-2.1"},
        )

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
