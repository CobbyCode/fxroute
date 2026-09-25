#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Output recovery across sink/profile renames of the same physical device.

After a re-probe, profile switch or PipeWire node re-creation the same
physical device can reappear under a different sink name. Recovery must find
it through the stable ALSA card identity (``alsa_card.<device>``), never
through fuzzy name matching: an unrelated device must not be taken over and
an ambiguous card (several live sinks) must keep the saved identity.
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
from audio.samplerate import overview, recovery
from audio.samplerate.parsing import card_name_for_sink_key, successor_sink_key

OLD = "alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40"
NEW = "alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-stereo"
NEW2 = "alsa_output.usb-BEHRINGER_UMC204HD_192k-00.iec958-stereo"
CARD = "alsa_card.usb-BEHRINGER_UMC204HD_192k-00"
OTHER = "alsa_output.usb-SMSL_SMSL_USB_AUDIO-00.analog-stereo"
OTHER_CARD = "alsa_card.usb-SMSL_SMSL_USB_AUDIO-00"
DSP = "fxroute_dsp_sink"


def _sink_line(name: str) -> str:
    return f"86\t{name}\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED"


class SuccessorIdentityTests(unittest.TestCase):
    def test_card_derivation_keeps_device_across_profiles(self) -> None:
        self.assertEqual(card_name_for_sink_key(OLD), CARD)
        self.assertEqual(card_name_for_sink_key(NEW), CARD)
        self.assertEqual(card_name_for_sink_key(OTHER), OTHER_CARD)

    def test_card_derivation_rejects_non_alsa_and_bare_names(self) -> None:
        self.assertIsNone(card_name_for_sink_key("fxroute_dsp_sink"))
        self.assertIsNone(card_name_for_sink_key("bluez_sink.11_22_33_44_55_66.a2dp-sink"))
        self.assertIsNone(card_name_for_sink_key("alsa_output.B"))
        self.assertIsNone(card_name_for_sink_key(""))
        self.assertIsNone(card_name_for_sink_key(None))

    def test_exact_name_wins_over_card_mate(self) -> None:
        self.assertEqual(successor_sink_key(OLD, [DSP, OLD, NEW, OTHER]), OLD)

    def test_unique_rename_resolves_to_same_device(self) -> None:
        self.assertEqual(successor_sink_key(OLD, [DSP, NEW, OTHER]), NEW)

    def test_unrelated_device_is_not_a_successor(self) -> None:
        self.assertIsNone(successor_sink_key(OLD, [DSP, OTHER]))

    def test_absent_device_has_no_successor(self) -> None:
        self.assertIsNone(successor_sink_key(OLD, [DSP]))
        self.assertIsNone(successor_sink_key(OLD, []))

    def test_ambiguous_card_never_takes_over(self) -> None:
        self.assertIsNone(successor_sink_key(OLD, [DSP, NEW, NEW2, OTHER]))

    def test_non_alsa_selection_has_no_successor(self) -> None:
        self.assertIsNone(successor_sink_key("fxroute_dsp_sink", [NEW, OTHER]))
        self.assertIsNone(successor_sink_key("", [NEW]))
        self.assertIsNone(successor_sink_key(None, [NEW]))


class WatcherReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.default = OTHER
        self.set_default_calls: list[str] = []

    def _run(self, live: list[str]):
        def run(args: list[str]) -> str:
            if args == ["pactl", "list", "sinks", "short"]:
                return "\n".join(_sink_line(name) for name in live)
            if args == ["pactl", "get-default-sink"]:
                return self.default
            raise AssertionError(f"unexpected command: {args}")
        return run

    def _set_default(self, name: str) -> None:
        self.set_default_calls.append(name)
        self.default = name

    def _reconcile(self, live: list[str]) -> str | None:
        with mock.patch.object(overview, "_run_command", side_effect=self._run(live)), \
                mock.patch.object(overview, "_set_default_sink", side_effect=self._set_default):
            return overview.reconcile_selected_output_default()

    def _read_state(self, live: list[str]) -> tuple[str | None, bool]:
        with mock.patch.object(overview, "_run_command", side_effect=self._run(live)):
            return overview.selected_output_default_state()

    def _saved(self) -> str | None:
        return samplerate._load_audio_output_selection()["selected_key"]

    def test_renamed_device_recovers_and_migrates_selection(self) -> None:
        samplerate._save_audio_output_selection(OLD)
        self.assertEqual(self._read_state([DSP, NEW, OTHER]), (OLD, True))
        self.assertEqual(self._reconcile([DSP, NEW, OTHER]), NEW)
        self.assertEqual(self._saved(), NEW)
        self.assertEqual(self.set_default_calls, [NEW])
        self.assertEqual(self._read_state([DSP, NEW, OTHER]), (NEW, False))

    def test_unrelated_device_is_not_taken_over(self) -> None:
        samplerate._save_audio_output_selection(OLD)
        self.assertIsNone(self._reconcile([DSP, OTHER]))
        self.assertEqual(self._saved(), OLD)
        self.assertEqual(self.set_default_calls, [])
        self.assertEqual(self._read_state([DSP, OTHER]), (OLD, False))

    def test_ambiguous_card_keeps_saved_identity(self) -> None:
        samplerate._save_audio_output_selection(OLD)
        self.assertIsNone(self._reconcile([DSP, NEW, NEW2, OTHER]))
        self.assertEqual(self._saved(), OLD)
        self.assertEqual(self.set_default_calls, [])
        self.assertEqual(self._read_state([DSP, NEW, NEW2, OTHER]), (OLD, False))

    def test_unchanged_sink_name_still_recovers(self) -> None:
        samplerate._save_audio_output_selection(OLD)
        self.assertEqual(self._reconcile([DSP, OLD, OTHER]), OLD)
        self.assertEqual(self._saved(), OLD)
        self.assertEqual(self.default, OLD)


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def _recover(self, state: dict) -> dict:
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

        with mock.patch.object(recovery, "_run_command", side_effect=run), \
                mock.patch.object(recovery, "RECOVERY_WAIT_SECONDS", 0.3), \
                mock.patch.object(recovery, "RECOVERY_POLL_SECONDS", 0.05):
            return recovery.recover_saved_output_sink()

    def _state(self, *, sinks, cards, sinks_after_restart=None) -> dict:
        return {
            "sinks": sinks,
            "cards": cards,
            "sinks_after_restart": sinks_after_restart if sinks_after_restart is not None else sinks,
            "restarts": [],
        }

    def test_renamed_sink_restored_after_reprobe(self) -> None:
        samplerate._save_audio_output_selection(OLD)
        state = self._state(
            sinks=[_sink_line(DSP)],
            cards=[f"1\t{CARD}\talsa", f"2\t{OTHER_CARD}\talsa"],
            sinks_after_restart=[_sink_line(DSP), _sink_line(NEW), _sink_line(OTHER)],
        )
        report = self._recover(state)
        self.assertTrue(report["attempted"])
        self.assertTrue(report["recovered"])
        self.assertEqual(report["reason"], "sink-restored-under-new-name")
        self.assertEqual(report["sink_key"], NEW)

    def test_already_renamed_sink_needs_no_reprobe(self) -> None:
        samplerate._save_audio_output_selection(OLD)
        state = self._state(
            sinks=[_sink_line(DSP), _sink_line(NEW), _sink_line(OTHER)],
            cards=[f"1\t{CARD}\talsa", f"2\t{OTHER_CARD}\talsa"],
        )
        report = self._recover(state)
        self.assertFalse(report["attempted"])
        self.assertEqual(report["reason"], "sink-present-under-new-name")
        self.assertEqual(report["sink_key"], NEW)
        self.assertEqual(state["restarts"], [])

    def test_unrelated_device_is_not_recovered(self) -> None:
        samplerate._save_audio_output_selection(OLD)
        state = self._state(
            sinks=[_sink_line(DSP), _sink_line(OTHER)],
            cards=[f"2\t{OTHER_CARD}\talsa"],
        )
        report = self._recover(state)
        self.assertEqual(report["reason"], "card-absent")
        self.assertFalse(report["attempted"])
        self.assertEqual(state["restarts"], [])

    def test_ambiguous_card_is_not_recovered(self) -> None:
        samplerate._save_audio_output_selection(OLD)
        state = self._state(
            sinks=[_sink_line(DSP)],
            cards=[f"1\t{CARD}\talsa"],
            sinks_after_restart=[_sink_line(DSP), _sink_line(NEW), _sink_line(NEW2)],
        )
        report = self._recover(state)
        self.assertTrue(report["attempted"])
        self.assertFalse(report["recovered"])
        self.assertEqual(report["reason"], "sink-did-not-return")


class PersistedApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.defaults: list[str] = []

    def _outputs(self, live: list[str]) -> list[dict]:
        return [
            {"key": name, "name": name, "selectable": True, "supported_rates": [44100, 48000]}
            for name in live
        ]

    def _apply(self, live: list[str]):
        fixture = {"outputs": self._outputs(live)}

        def run(args: list[str]) -> str:
            if args == ["pactl", "list", "sinks", "short"]:
                return "\n".join(_sink_line(name) for name in live)
            raise AssertionError(f"unexpected command: {args}")

        with mock.patch.object(overview, "get_audio_output_overview", return_value=fixture), \
                mock.patch.object(overview, "_set_default_sink", side_effect=self.defaults.append), \
                mock.patch.object(overview, "_run_command", side_effect=run):
            return overview.apply_persisted_audio_output_selection()

    def test_renamed_device_reapplies_successor(self) -> None:
        samplerate._save_audio_output_selection(OLD)
        self.assertIsNotNone(self._apply([NEW, OTHER]))
        self.assertEqual(samplerate._load_audio_output_selection()["selected_key"], NEW)
        self.assertEqual(self.defaults, [NEW])

    def test_unrelated_device_leaves_selection(self) -> None:
        samplerate._save_audio_output_selection(OLD)
        self.assertIsNone(self._apply([OTHER]))
        self.assertEqual(samplerate._load_audio_output_selection()["selected_key"], OLD)
        self.assertEqual(self.defaults, [])


class RelevantSinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        samplerate._save_audio_output_selection(OLD)

    def test_relevant_sink_prefers_renamed_same_device(self) -> None:
        sinks = [
            {"name": OTHER, "state": "RUNNING", "active_rate": 48000},
            {"name": NEW, "state": "IDLE", "active_rate": 44100},
        ]
        self.assertEqual(
            overview._select_relevant_sink({"name": OTHER}, sinks)["name"], NEW
        )

    def test_relevant_sink_ignores_ambiguous_card(self) -> None:
        sinks = [
            {"name": OTHER, "state": "RUNNING", "active_rate": 48000},
            {"name": NEW, "state": "IDLE", "active_rate": 44100},
            {"name": NEW2, "state": "IDLE", "active_rate": 44100},
        ]
        self.assertEqual(
            overview._select_relevant_sink({"name": OTHER}, sinks)["name"], OTHER
        )


if __name__ == "__main__":
    unittest.main()
