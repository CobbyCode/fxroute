#!/usr/bin/env python3
"""Regression: the shared device clock is released after a measurement.

A measurement opens its card's nodes at the measurement rate.  While an ALSA
node keeps that rate, the device's shared clock stays there and the output node
cannot return to the playback rate: force-rate writes, sink suspend/resume
pulses, silent sink streams, capture streams at the target rate and a DSP
helper rebuild all fail, so the next play dies in its target-rate stage and the
output gate stays latched from the stale mute.  Only releasing the card's nodes
recovered (recycling WirePlumber, or the card profile).  These tests pin the
bounded recovery ladder -- silent stream first, card profile cycle as the last
resort -- and the rule that idle playback paths no longer clear the pin while a
measurement window owns the live rate.
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
from audio.samplerate import alignment

SCARLETT_OUTPUT = (
    "alsa_output.usb-Focusrite_Scarlett_16i16_4th_Gen_S63ZKU25B036F6-00"
    ".multichannel-output"
)
SCARLETT_CARD = (
    "alsa_card.usb-Focusrite_Scarlett_16i16_4th_Gen_S63ZKU25B036F6-00"
)

PACTL_CARDS = (
    "\tName: alsa_card.usb-BEHRINGER_UMC204HD_192k-00\n"
    "\t\tActive Profile: output:analog-surround-40+input:iec958-stereo\n"
    "\tName: " + SCARLETT_CARD + "\n"
    "\t\tProfiles:\n"
    "\t\t\toutput:multichannel-output: Multichannel Output (sinks: 1, sources: 0)\n"
    "\t\tActive Profile: output:multichannel-output+input:multichannel-input\n"
)


class CardResolutionTests(unittest.TestCase):
    def test_usb_output_maps_to_its_card(self):
        self.assertEqual(
            alignment.card_name_for_output(SCARLETT_OUTPUT), SCARLETT_CARD
        )

    def test_pci_output_maps_to_its_card(self):
        self.assertEqual(
            alignment.card_name_for_output("alsa_output.pci-0000_00_1f.3.analog-stereo"),
            "alsa_card.pci-0000_00_1f.3",
        )

    def test_non_alsa_keys_have_no_card(self):
        self.assertIsNone(alignment.card_name_for_output("fxroute_dsp_sink"))
        self.assertIsNone(alignment.card_name_for_output(""))

    def test_active_profile_is_read_for_the_requested_card_only(self):
        with patch.object(alignment, "_run_command", return_value=PACTL_CARDS):
            self.assertEqual(
                alignment.active_card_profile(SCARLETT_CARD),
                "output:multichannel-output+input:multichannel-input",
            )
            self.assertEqual(
                alignment.active_card_profile("alsa_card.usb-BEHRINGER_UMC204HD_192k-00"),
                "output:analog-surround-40+input:iec958-stereo",
            )
            self.assertIsNone(alignment.active_card_profile("alsa_card.usb-Missing-00"))

    def test_profile_lookup_failure_is_not_fatal(self):
        with patch.object(alignment, "_run_command", side_effect=RuntimeError("boom")):
            self.assertIsNone(alignment.active_card_profile(SCARLETT_CARD))

    def test_effective_output_key_reads_the_overview(self):
        overview = {"output_mode": {"effective_output_key": SCARLETT_OUTPUT}}
        with patch.object(alignment, "get_audio_output_overview", return_value=overview):
            self.assertEqual(alignment.effective_output_key(), SCARLETT_OUTPUT)
        with patch.object(
            alignment, "get_audio_output_overview", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(alignment.effective_output_key(), "")


class CardProfileRecycleTests(unittest.TestCase):
    def test_profile_cycle_turns_the_card_off_then_back_on(self):
        calls: list[list[str]] = []

        def run(command, **_kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stderr="")

        with (
            patch.object(alignment.subprocess, "run", run),
            patch.object(alignment.time, "sleep"),
        ):
            recycled = alignment.recycle_card_profile(
                SCARLETT_CARD, "output:multichannel-output", "test"
            )
        self.assertTrue(recycled)
        self.assertEqual(
            calls,
            [
                ["pactl", "set-card-profile", SCARLETT_CARD, "off"],
                [
                    "pactl", "set-card-profile", SCARLETT_CARD,
                    "output:multichannel-output",
                ],
            ],
        )

    def test_rejected_profile_step_reports_failure(self):
        with (
            patch.object(
                alignment.subprocess, "run",
                return_value=SimpleNamespace(returncode=1, stderr="Failure: no such card"),
            ),
            patch.object(alignment.time, "sleep"),
        ):
            self.assertFalse(
                alignment.recycle_card_profile(SCARLETT_CARD, "profile", "test")
            )


class IdleGraphRenegotiationTests(unittest.IsolatedAsyncioTestCase):
    async def test_graph_trigger_falls_back_to_the_card_recycle(self):
        recycle = AsyncMock(return_value=True)
        with (
            patch.object(
                alignment, "_trigger_silent_sink_stream",
                new=AsyncMock(return_value=False),
            ),
            patch.object(alignment, "trigger_card_rate_recycle", new=recycle),
        ):
            aligned = await alignment.trigger_idle_sink_renegotiation(44100)
        self.assertTrue(aligned)
        recycle.assert_awaited_once_with(44100)

    async def test_sink_stream_alignment_skips_the_card_recycle(self):
        recycle = AsyncMock(return_value=True)
        with (
            patch.object(
                alignment, "_trigger_silent_sink_stream",
                new=AsyncMock(return_value=True),
            ),
            patch.object(alignment, "trigger_card_rate_recycle", new=recycle),
        ):
            aligned = await alignment.trigger_idle_sink_renegotiation(48000)
        self.assertTrue(aligned)
        recycle.assert_not_awaited()

    async def test_card_recycle_can_be_disabled_for_measurement_entry(self):
        recycle = AsyncMock(return_value=True)
        with (
            patch.object(
                alignment, "_trigger_silent_sink_stream",
                new=AsyncMock(return_value=False),
            ),
            patch.object(alignment, "trigger_card_rate_recycle", new=recycle),
        ):
            aligned = await alignment.trigger_idle_sink_renegotiation(
                48000, allow_card_recycle=False
            )
        self.assertFalse(aligned)
        recycle.assert_not_awaited()

    async def test_measurement_entry_preflight_disables_the_card_recycle(self):
        trigger = AsyncMock(return_value=False)
        reads = iter([
            {"active_rate": 44100, "force_rate": 0},
            {"active_rate": 44100, "force_rate": 0},
        ])
        with (
            patch.object(alignment, "get_samplerate_status", side_effect=lambda: next(reads)),
            patch.object(
                alignment, "ensure_playback_samplerate_force",
                new=AsyncMock(return_value=False),
            ),
            patch.object(alignment, "trigger_idle_sink_renegotiation", new=trigger),
        ):
            aligned = await alignment.reconcile_transition_sink_rate(
                48000, reason="measurement-entry"
            )
        self.assertFalse(aligned)
        self.assertEqual(trigger.await_args.kwargs, {"allow_card_recycle": False})

    async def test_card_recycle_runs_the_cycle_off_loop(self):
        main_thread = threading.current_thread()
        seen: list[bool] = []

        def lookup(_card):
            seen.append(threading.current_thread() is main_thread)
            return "output:multichannel-output"

        recycle = Mock(return_value=True)

        def cycle(*_args):
            seen.append(threading.current_thread() is main_thread)
            return recycle()

        with (
            patch.object(
                alignment, "effective_output_key", return_value=SCARLETT_OUTPUT
            ),
            patch.object(alignment, "active_card_profile", side_effect=lookup),
            patch.object(alignment, "recycle_card_profile", side_effect=cycle),
            patch.object(
                alignment, "wait_for_samplerate_alignment",
                new=AsyncMock(return_value=True),
            ),
        ):
            aligned = await alignment.trigger_card_rate_recycle(44100, reason="test")
        self.assertTrue(aligned)
        self.assertTrue(seen)
        self.assertFalse(
            any(seen), "pactl card lookup/profile cycle ran on the event-loop thread"
        )
        recycle.assert_called_once()

    async def test_card_recycle_without_an_alsa_card_is_a_no_op(self):
        recycle = Mock(return_value=True)
        with (
            patch.object(alignment, "effective_output_key", return_value="fxroute_dsp_sink"),
            patch.object(alignment, "recycle_card_profile", recycle),
        ):
            self.assertFalse(await alignment.trigger_card_rate_recycle(44100))
        recycle.assert_not_called()

    async def test_card_recycle_without_an_active_profile_is_a_no_op(self):
        recycle = Mock(return_value=True)
        with (
            patch.object(alignment, "effective_output_key", return_value=SCARLETT_OUTPUT),
            patch.object(alignment, "active_card_profile", return_value="off"),
            patch.object(alignment, "recycle_card_profile", recycle),
        ):
            self.assertFalse(await alignment.trigger_card_rate_recycle(44100))
        recycle.assert_not_called()


class StopIdleForceRateOwnershipTests(unittest.TestCase):
    def test_measurement_window_owns_the_live_rate(self):
        session = SimpleNamespace(owns_audio_graph=True)
        with patch.object(main, "measurement_sr_session", session):
            self.assertTrue(main._measurement_session_owns_live_rate())

    def test_closed_measurement_window_does_not_own_the_rate(self):
        session = SimpleNamespace(owns_audio_graph=False)
        with patch.object(main, "measurement_sr_session", session):
            self.assertFalse(main._measurement_session_owns_live_rate())
        with patch.object(main, "measurement_sr_session", None):
            self.assertFalse(main._measurement_session_owns_live_rate())


if __name__ == "__main__":
    unittest.main(verbosity=2)
