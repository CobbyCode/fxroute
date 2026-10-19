#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""HDMI layout changes run through the verified output-selection transaction."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from audio import hdmi
from audio.output_state import routing_for_device
from audio.samplerate import _load_audio_output_selection, _save_audio_output_selection
from test_hdmi_transport import CARD, MARANTZ_ELD, PORTS_71, STEREO, SURROUND71, card, tv_eld


class HdmiTransportSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        _save_audio_output_selection(STEREO)
        self.card = card()
        self.sink = STEREO
        self.eld = MARANTZ_ELD
        self.commands = []
        self.selections = []
        self.fail_selection = set()
        self.coordinator = SimpleNamespace(lock=asyncio.Lock())
        self.session = SimpleNamespace(lock=asyncio.Lock(), has_active_jobs=False)
        patches = [
            mock.patch.object(main, "playback_transition_coordinator", self.coordinator),
            mock.patch.object(main, "measurement_sr_session", self.session),
            mock.patch.object(main.samplerate, "_run_command", side_effect=self.run_command),
            mock.patch.object(main.alsa_ctl, "read_pcm_eld", side_effect=lambda card_number, device: self.eld),
            mock.patch.object(main, "_apply_audio_output_selection", side_effect=self.select),
            mock.patch.object(main, "dsp_orchestrator", SimpleNamespace(
                refresh_peak_monitor_after_effects_change=self.refresh)),
            mock.patch.object(main.runtime, "dsp_runtime", SimpleNamespace(
                snapshot=lambda: {"active": True}, sync_in_progress=False)),
            mock.patch.object(main, "_measurement_session_owns_live_rate", return_value=False),
            mock.patch.object(main, "_hdmi_transport_watch", hdmi.TransportWatch()),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def refresh(self, reason):
        return None

    def run_command(self, args, *rest, **kwargs):
        self.commands.append(list(args))
        if args[:4] == ["pactl", "-f", "json", "list"]:
            if args[4] == "cards":
                return json.dumps([self.card])
            return json.dumps([{"name": self.sink, "properties": {"alsa.device": "3"}}] if self.sink else [])
        if args == ["pactl", "list", "sinks", "short"]:
            raise AssertionError("sink presence uses the JSON listing")
        if args[:2] == ["pactl", "set-card-profile"]:
            profile = args[3]
            self.card["active_profile"] = profile
            output, _ = hdmi.split_card_profile(profile)
            match = hdmi._MAPPING.match(output or "")
            self.sink = (hdmi.sink_key(CARD, int(match["extra"] or 0), hdmi._BY_MAPPING[match["mapping"]]["id"])
                         if match else None)
            return ""
        if args == ["pw-link", "-i"]:
            return "\n".join(f"{self.sink}:{port}" for port in PORTS_71) if self.sink else ""
        raise AssertionError(f"unexpected command {args}")

    async def select(self, key):
        self.selections.append(key)
        # The overview the selection returns already names the chosen layout.
        self.desired_during_selection = hdmi.load_desired().get(f"{CARD}#hdmi0")
        if key in self.fail_selection:
            raise RuntimeError("graph apply failed")
        _save_audio_output_selection(key)
        return {"selected_output": {"key": key}, "output_mode": {}}

    async def change(self, key=STEREO, transport="7.1"):
        async def body():
            return {"key": key, "transport": transport}
        return await main.save_audio_output_transport_route(SimpleNamespace(json=body))

    def profile_changes(self):
        return [command[3] for command in self.commands if command[:2] == ["pactl", "set-card-profile"]]

    async def test_layout_change_keeps_the_analog_input_and_remembers_the_choice(self):
        result = await self.change()
        self.assertEqual(result["selected_output"]["key"], SURROUND71)
        self.assertEqual(self.profile_changes(), ["output:hdmi-surround71+input:analog-stereo"])
        self.assertEqual(self.selections, [SURROUND71])
        self.assertEqual(hdmi.load_desired()[f"{CARD}#hdmi0"],
                         {"transport": "7.1", "input": "analog-stereo", "pcm_device": 3})
        self.assertEqual(self.desired_during_selection["transport"], "7.1")

    async def test_failed_selection_restores_the_previous_profile_and_output(self):
        self.fail_selection.add(SURROUND71)
        with self.assertRaises(HTTPException) as failure:
            await self.change()
        self.assertEqual(failure.exception.status_code, 500)
        self.assertEqual(self.profile_changes(), ["output:hdmi-surround71+input:analog-stereo",
                                                  "output:hdmi-stereo+input:analog-stereo"])
        self.assertEqual(self.selections, [SURROUND71, STEREO])
        self.assertEqual(_load_audio_output_selection()["selected_key"], STEREO)
        self.assertEqual(hdmi.load_desired(), {})

    async def test_failed_change_keeps_the_previous_choice(self):
        await self.change(transport="5.1")
        self.fail_selection.add(SURROUND71)
        with self.assertRaises(HTTPException):
            await self.change(key=hdmi.sink_key(CARD, 0, "5.1"))
        self.assertEqual(hdmi.load_desired()[f"{CARD}#hdmi0"]["transport"], "5.1")

    async def test_receiver_without_the_layout_is_refused(self):
        self.eld = tv_eld()
        with self.assertRaises(HTTPException) as failure:
            await self.change()
        self.assertEqual(failure.exception.status_code, 400)
        self.assertIn("at most 2 LPCM channels", failure.exception.detail)
        self.assertEqual(self.profile_changes(), [])

    async def test_only_the_selected_hdmi_output_changes_layout(self):
        _save_audio_output_selection("alsa_output.usb-X-00.analog-stereo")
        with self.assertRaises(HTTPException) as failure:
            await self.change()
        self.assertEqual(failure.exception.status_code, 409)
        with self.assertRaises(HTTPException) as failure:
            await self.change(key="alsa_output.usb-X-00.analog-stereo")
        self.assertEqual(failure.exception.status_code, 400)
        self.assertEqual(self.profile_changes(), [])

    async def test_measurement_blocks_the_change(self):
        self.session.has_active_jobs = True
        with self.assertRaises(HTTPException) as failure:
            await self.change()
        self.assertEqual(failure.exception.status_code, 423)
        self.assertEqual(self.profile_changes(), [])

    async def test_watch_restores_the_chosen_layout_after_a_lost_connection(self):
        await self.change()
        self.commands.clear()
        self.selections.clear()
        # Receiver off: WirePlumber turns the card off.
        self.card = card(connected=False, active="off")
        self.sink = None
        self.eld = None
        await main._reconcile_hdmi_transport()
        # Receiver back, but WirePlumber restored Stereo.
        self.card = card(active="output:hdmi-stereo+input:analog-stereo")
        self.sink = STEREO
        self.eld = MARANTZ_ELD
        await main._reconcile_hdmi_transport()
        self.assertEqual(self.profile_changes(), [])
        await main._reconcile_hdmi_transport()
        self.assertEqual(self.profile_changes(), ["output:hdmi-surround71+input:analog-stereo"])
        self.assertEqual(self.selections, [SURROUND71])
        await main._reconcile_hdmi_transport()
        self.assertEqual(len(self.profile_changes()), 1)

    async def test_watch_skips_while_the_coordinator_is_busy(self):
        await self.change()
        self.card = card(active="output:hdmi-stereo+input:analog-stereo")
        self.sink = STEREO
        self.eld = tv_eld()
        self.commands.clear()
        async with self.coordinator.lock:
            await main._reconcile_hdmi_transport()
        self.assertEqual(self.commands, [])


class SeedRoutingIntegrationTests(unittest.TestCase):
    def test_first_play_stores_the_port_defaults(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}), \
                mock.patch.object(main, "_output_service_instance", None), \
                mock.patch.object(main, "measurement_sr_session", None):
            main._seed_hdmi_routing(SURROUND71, {"output_mode": {"hardware_playback_ports": PORTS_71}})
            state = main.get_output_service().load()
            self.assertEqual(routing_for_device(state, "surround", SURROUND71, 8),
                             ["main_l", "main_r", "rear_l", "rear_r", "center", "sub1", "side_l", "side_r"])
            revision = state["revision"]
            main._seed_hdmi_routing(SURROUND71, {"output_mode": {"hardware_playback_ports": PORTS_71}})
            self.assertEqual(main.get_output_service().load()["revision"], revision)
            main._seed_hdmi_routing(STEREO, {"output_mode": {"hardware_playback_ports": PORTS_71[:2]}})
            self.assertEqual(main.get_output_service().load()["revision"], revision)


if __name__ == "__main__":
    unittest.main()
