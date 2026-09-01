#!/usr/bin/env python3
"""Focused tests: startup recovery of a missing persisted hardware output sink.

After a degraded WirePlumber card probe the saved ``alsa_output.*`` sink can
be absent while its ALSA card is still enumerated.  Startup must detect that
state, restart WirePlumber once, and wait a bounded time for the sink to come
back.  No recovery may be attempted when the card is absent, the saved
selection is not a local ALSA sink, or the sink is simply present.
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
from audio.samplerate import recovery

UMC = "alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40"
UMC_CARD = "alsa_card.usb-BEHRINGER_UMC204HD_192k-00"
SMSL = "alsa_output.usb-SMSL_SMSL_USB_AUDIO-00.analog-stereo"
SMSL_CARD = "alsa_card.usb-SMSL_SMSL_USB_AUDIO-00"

DSP_SINK_LINE = "39\tfxroute_dsp_sink\tPipeWire\tfloat32le 2ch 44100Hz\tRUNNING"


def _sink_line(name: str) -> str:
    return f"86\t{name}\tPipeWire\ts32le 2ch 44100Hz\tSUSPENDED"


class SavedOutputSinkRecoveryTest(unittest.TestCase):
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

    def _persist_selection(self, key: str) -> None:
        samplerate._save_audio_output_selection(key)

    def _run_with_state(self, state: dict):
        def run(args: list[str]) -> str:
            if args[:3] == ["pactl", "list", "sinks"]:
                return "\n".join(state["sinks"])
            if args[:3] == ["pactl", "list", "cards"]:
                return "\n".join(state["cards"])
            if args[:4] == ["systemctl", "--user", "restart", "wireplumber.service"]:
                state["restarts"].append(args)
                state["sinks"] = list(state["sinks_after_restart"])
                return ""
            raise AssertionError(f"unexpected command: {args}")

        return run

    def _recover(self, state: dict) -> dict:
        with mock.patch.object(recovery, "_run_command", side_effect=self._run_with_state(state)):
            return recovery.recover_saved_output_sink()

    def _state(self, *, sinks: list[str], cards: list[str], sinks_after_restart: list[str] | None = None) -> dict:
        return {
            "sinks": sinks,
            "cards": cards,
            "sinks_after_restart": sinks_after_restart if sinks_after_restart is not None else sinks,
            "restarts": [],
        }

    # -- no-op scenarios ----------------------------------------------------

    def test_no_saved_selection_is_noop(self) -> None:
        state = self._state(sinks=[DSP_SINK_LINE], cards=[f"1\t{UMC_CARD}\talsa"])
        report = self._recover(state)
        self.assertEqual(report, {"attempted": False, "recovered": False, "reason": "no-saved-selection"})
        self.assertEqual(state["restarts"], [])

    def test_present_sink_is_noop(self) -> None:
        self._persist_selection(UMC)
        state = self._state(
            sinks=[DSP_SINK_LINE, _sink_line(UMC)],
            cards=[f"1\t{UMC_CARD}\talsa"],
        )
        report = self._recover(state)
        self.assertEqual(report["reason"], "saved-sink-present")
        self.assertFalse(report["attempted"])
        self.assertEqual(state["restarts"], [])

    def test_card_absent_is_noop(self) -> None:
        self._persist_selection(UMC)
        state = self._state(
            sinks=[DSP_SINK_LINE, _sink_line(SMSL)],
            cards=[f"1\t{SMSL_CARD}\talsa"],
        )
        report = self._recover(state)
        self.assertEqual(report["reason"], "card-absent")
        self.assertFalse(report["attempted"])
        self.assertEqual(state["restarts"], [])

    def test_non_alsa_selection_is_noop(self) -> None:
        self._persist_selection("bluez_sink.11_22_33_44_55_66.a2dp-sink")
        state = self._state(sinks=[DSP_SINK_LINE], cards=[f"1\t{UMC_CARD}\talsa"])
        report = self._recover(state)
        self.assertEqual(report["reason"], "saved-selection-not-local-alsa")
        self.assertEqual(state["restarts"], [])

    # -- recovery scenarios -------------------------------------------------

    def test_degraded_card_recovers_after_wireplumber_restart(self) -> None:
        self._persist_selection(UMC)
        state = self._state(
            sinks=[DSP_SINK_LINE, _sink_line(SMSL)],
            cards=[f"1\t{UMC_CARD}\talsa", f"2\t{SMSL_CARD}\talsa"],
            sinks_after_restart=[DSP_SINK_LINE, _sink_line(UMC), _sink_line(SMSL)],
        )
        report = self._recover(state)
        self.assertTrue(report["attempted"])
        self.assertTrue(report["recovered"])
        self.assertEqual(report["reason"], "sink-restored")
        self.assertEqual(state["restarts"], [["systemctl", "--user", "restart", "wireplumber.service"]])

    def test_sink_did_not_return_within_bound(self) -> None:
        self._persist_selection(UMC)
        state = self._state(
            sinks=[DSP_SINK_LINE, _sink_line(SMSL)],
            cards=[f"1\t{UMC_CARD}\talsa"],
        )
        with mock.patch.object(recovery, "RECOVERY_WAIT_SECONDS", 0.3), \
                mock.patch.object(recovery, "RECOVERY_POLL_SECONDS", 0.05):
            report = self._recover(state)
        self.assertTrue(report["attempted"])
        self.assertFalse(report["recovered"])
        self.assertEqual(report["reason"], "sink-did-not-return")
        self.assertEqual(len(state["restarts"]), 1)

    def test_card_name_derivation_handles_profile_suffix(self) -> None:
        self.assertEqual(
            recovery._alsa_card_name_for_sink_key(UMC),
            UMC_CARD,
        )
        self.assertEqual(
            recovery._alsa_card_name_for_sink_key(SMSL),
            SMSL_CARD,
        )
        self.assertIsNone(recovery._alsa_card_name_for_sink_key("fxroute_dsp_sink"))
        self.assertIsNone(recovery._alsa_card_name_for_sink_key("bluez_sink.11_22_33_44_55_66.a2dp-sink"))


if __name__ == "__main__":
    unittest.main()
