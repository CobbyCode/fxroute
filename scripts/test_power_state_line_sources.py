# SPDX-License-Identifier: AGPL-3.0-only
"""Home Assistant amp hint covers Bluetooth and external line sources.

``amp_should_be_on`` stays a plain boolean with the same four payload keys;
Bluetooth counts only while its linked capture source is RUNNING, external
input while its loopback link is established. Measurement keeps priority.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import audio.samplerate.bluetooth as bluetooth_mod
from audio.samplerate import is_bluetooth_audio_streaming
import main


WPCTL_STATUS = """\
PipeWire 'pipewire-0' [1.6.8, paul@fxroute, cookie:4101633295]
Audio
 ├─ Devices:
 │     402. ZENBOOK                             [bluez5]
 ├─ Sinks:
 │      36. FXRoute_DSP_Ingress                 [vol: 1.00]
 ├─ Sources:
 │
 ├─ Filters:
 │
 └─ Streams:
       133. Spotify
             96. output_FL       > FXRoute_DSP_Ingress:playback_FL\t[paused]
       402. bluez_input.11_22_33_44_55_66.1
            661. output_FL       > FXRoute_DSP_Ingress:playback_FL\t[active]
            724. output_FR       > FXRoute_DSP_Ingress:playback_FR\t[active]
       403. bluez_input.AA_BB_CC_DD_EE_FF.1
            662. output_FL       > FXRoute_DSP_Ingress:playback_FL\t[init]

Video
"""


class BluetoothStreamingProbeTests(unittest.TestCase):
    @contextmanager
    def _probe(self, pactl_output, *, available=True):
        with (
            patch.object(bluetooth_mod, "_command_available", return_value=available),
            patch.object(bluetooth_mod, "_run_command", return_value=pactl_output) as run_command,
        ):
            yield run_command

    def test_running_source_streams(self):
        with self._probe(WPCTL_STATUS):
            self.assertTrue(is_bluetooth_audio_streaming("bluez_input.11_22_33_44_55_66.1"))

    def test_idle_source_does_not_stream(self):
        with self._probe(WPCTL_STATUS):
            self.assertFalse(is_bluetooth_audio_streaming("bluez_input.AA_BB_CC_DD_EE_FF.1"))

    def test_unknown_source_does_not_stream(self):
        with self._probe(WPCTL_STATUS):
            self.assertFalse(is_bluetooth_audio_streaming("bluez_input.00_00_00_00_00_00.1"))

    def test_missing_name_never_runs_command(self):
        with self._probe(WPCTL_STATUS) as run_command:
            self.assertFalse(is_bluetooth_audio_streaming(None))
            self.assertFalse(is_bluetooth_audio_streaming("   "))
            run_command.assert_not_called()

    def test_missing_pactl_is_fail_closed(self):
        with self._probe(WPCTL_STATUS, available=False) as run_command:
            self.assertFalse(is_bluetooth_audio_streaming("bluez_input.11_22_33_44_55_66.1"))
            run_command.assert_not_called()

    def test_command_error_is_fail_closed(self):
        with patch.object(bluetooth_mod, "_command_available", return_value=True), patch.object(
            bluetooth_mod, "_run_command", side_effect=RuntimeError("boom")
        ):
            self.assertFalse(is_bluetooth_audio_streaming("bluez_input.11_22_33_44_55_66.1"))


def _payload(*, bt_source=None, bt_streaming=False, ext_source=None, heartbeat=0.0):
    with (
        patch.object(main.runtime, "player_instance", None),
        patch.object(main.playback_state, "latest_spotify_state", {}),
        patch.object(main.playback_state, "latest_qobuz_state", {}),
        patch.object(main, "last_measurement_window_seen_at", heartbeat),
        patch.object(main, "bluetooth_input", SimpleNamespace(input_source_name=bt_source)),
        patch.object(main, "external_input", SimpleNamespace(loopback_source_name=ext_source)),
        patch.object(main, "is_bluetooth_audio_streaming", return_value=bt_streaming),
    ):
        return main._build_power_state_payload()


class PowerPayloadLineSourceTests(unittest.TestCase):
    def test_idle_shape_unchanged(self):
        self.assertEqual(
            _payload(),
            {
                "amp_should_be_on": False,
                "reason": "idle",
                "playback_active": False,
                "measurement_window_open": False,
            },
        )

    def test_streaming_bluetooth_switches_on(self):
        payload = _payload(bt_source="bluez_input.11_22_33_44_55_66.1", bt_streaming=True)
        self.assertTrue(payload["amp_should_be_on"])
        self.assertEqual(payload["reason"], "bluetooth")
        self.assertFalse(payload["playback_active"])

    def test_linked_but_idle_bluetooth_stays_off(self):
        self.assertEqual(_payload(bt_source="bluez_input.11_22_33_44_55_66.1", bt_streaming=False)["reason"], "idle")
        self.assertFalse(_payload(bt_source="bluez_input.11_22_33_44_55_66.1", bt_streaming=False)["amp_should_be_on"])

    def test_external_input_switches_on(self):
        payload = _payload(ext_source="alsa_input.scarlett")
        self.assertTrue(payload["amp_should_be_on"])
        self.assertEqual(payload["reason"], "external-input")

    def test_measurement_keeps_priority_over_line_sources(self):
        payload = _payload(
            bt_source="bluez_input.11_22_33_44_55_66.1",
            bt_streaming=True,
            ext_source="alsa_input.scarlett",
            heartbeat=9e9,
        )
        self.assertTrue(payload["amp_should_be_on"])
        self.assertEqual(payload["reason"], "measurement_window")
        self.assertTrue(payload["measurement_window_open"])


if __name__ == "__main__":
    unittest.main()
