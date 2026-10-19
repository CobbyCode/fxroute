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
from audio.output_state import routing_for_device, switch_mode
from scripts.test_output_state_api import make_manager, make_service
from audio.samplerate import _load_audio_output_selection, _save_audio_output_selection
from test_hdmi_transport import CARD, MARANTZ_ELD, PORTS_51, PORTS_71, STEREO, SURROUND, SURROUND71, card, tv_eld


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
        self.manager = make_manager(self.directory.name)
        self.service = make_service(self.directory.name, self.manager)
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
            mock.patch.object(main, "get_output_service", return_value=self.service),
            mock.patch.object(main, "_require_dsp_manager", return_value=self.manager),
            mock.patch.object(main, "get_samplerate_status", return_value={"active_rate": 44100}),
            mock.patch.object(main, "get_audio_output_overview", side_effect=self.overview),
            mock.patch.object(main, "_run_coordinated_transition", new=mock.AsyncMock()),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def refresh(self, reason):
        return None

    def overview(self):
        parsed = hdmi.parse_sink_key(self.sink or STEREO)
        channels = hdmi.transport(parsed.transport)["channels"]
        return {"selected_output": {"key": self.sink, "channels": channels,
                                    "hdmi": hdmi.describe(self.sink, card=self.card, eld=hdmi.parse_eld(self.eld))},
                "output_mode": {"effective_output_key": self.sink, "effective_output_channels": channels,
                                "hardware_playback_ports": PORTS_71[:channels]}}

    async def automatic(self, mode):
        main._seed_hdmi_routing(self.sink, self.overview())
        return await main.output_application.apply_candidate({
            "expected_revision": self.service.load()["revision"],
            "mutation": {"kind": "switch_mode", "mode": mode}})

    async def test_automatic_modes_switch_stereo_sub_surround_and_back(self):
        for mode, expected in (("stereo-sub", SURROUND), ("surround", SURROUND71), ("stereo", STEREO)):
            result = await self.automatic(mode)
            self.assertEqual(self.sink, expected)
            self.assertEqual(self.service.load()["active_mode"], mode)
            self.assertTrue(result["live_applied"])
        self.assertEqual(self.profile_changes(), ["output:hdmi-surround+input:analog-stereo",
                                                 "output:hdmi-surround71+input:analog-stereo",
                                                 "output:hdmi-stereo+input:analog-stereo"])

    async def test_routing_edit_switches_between_51_and_71(self):
        await self.automatic("surround")
        for assignments, expected in ((["main_l", "main_r", "rear_l", "rear_r", "center", "sub1", "off", "off"], SURROUND),
                                      (["main_l", "main_r", "rear_l", "rear_r", "center", "sub1", "side_l", "side_r"], SURROUND71)):
            await main.output_application.apply_candidate({"expected_revision": self.service.load()["revision"],
                "mutation": {"kind": "set_routing", "mode": "surround", "assignments": assignments}})
            self.assertEqual(self.sink, expected)
            self.assertEqual(routing_for_device(self.service.load(), "surround", self.sink, 8), assignments)

    async def test_automatic_failure_restores_mode_and_transport(self):
        self.fail_selection.add(SURROUND71)
        with self.assertRaises(Exception):
            await self.automatic("surround")
        self.assertEqual(self.service.load()["active_mode"], "stereo")
        self.assertEqual(self.sink, STEREO)
        self.assertEqual(_load_audio_output_selection()["selected_key"], STEREO)

    async def test_uninitialized_routing_cannot_supersede_failure_rollback(self):
        before = self.service.load()
        self.fail_selection.add(SURROUND)
        with self.assertRaises(Exception):
            await main.output_application.apply_candidate({"expected_revision": before["revision"],
                "mutation": {"kind": "switch_mode", "mode": "stereo-sub"}})
        self.assertEqual(self.service.load()["modes"], before["modes"])
        self.assertEqual(self.service.load()["active_mode"], "stereo")
        self.assertEqual(self.sink, STEREO)

    async def test_each_side_can_be_configured_in_a_separate_routing_edit(self):
        await self.automatic("surround")
        assignments = ["main_l", "main_r", "rear_l", "rear_r", "center", "sub1", "off", "off"]
        await main.output_application.apply_candidate({"expected_revision": self.service.load()["revision"],
            "mutation": {"kind": "set_routing", "mode": "surround", "assignments": assignments}})
        assignments[-2] = "side_l"
        result = await main.output_application.apply_candidate({"expected_revision": self.service.load()["revision"],
            "mutation": {"kind": "set_routing", "mode": "surround", "assignments": assignments}})
        self.assertFalse(result["live_applied"])
        self.assertEqual(result["live_reason"], "not-activatable")
        self.assertEqual(self.sink, SURROUND)
        self.commands.clear()
        await main._reconcile_hdmi_transport()
        await main._reconcile_hdmi_transport()
        self.assertEqual(self.profile_changes(), [])
        assignments[-1] = "side_r"
        result = await main.output_application.apply_candidate({"expected_revision": self.service.load()["revision"],
            "mutation": {"kind": "set_routing", "mode": "surround", "assignments": assignments}})
        self.assertTrue(result["live_applied"])
        self.assertEqual(self.sink, SURROUND71)

    async def test_watch_does_not_activate_a_draft_with_an_unrenderable_layout(self):
        with mock.patch.object(self.service, "compile_layout", side_effect=ValueError("Convolver IR is missing")):
            result = await self.automatic("surround")
            self.assertFalse(result["live_applied"])
            await main._reconcile_hdmi_transport()
            await main._reconcile_hdmi_transport()
        self.assertEqual(self.profile_changes(), [])
        self.assertEqual(self.sink, STEREO)

    async def test_implicit_default_output_does_not_need_reselection(self):
        main.samplerate._audio_output_selection_path().unlink()
        await self.automatic("stereo-sub")
        self.assertEqual(self.sink, SURROUND)

    async def test_watch_automates_an_implicit_default_output(self):
        main._seed_hdmi_routing(self.sink, self.overview())
        self.service.apply(lambda state: switch_mode(state, "surround"), expected_revision=self.service.load()["revision"])
        main.samplerate._audio_output_selection_path().unlink()
        await main._reconcile_hdmi_transport()
        await main._reconcile_hdmi_transport()
        self.assertEqual(self.sink, SURROUND71)

    async def test_measurement_and_stale_edits_do_not_change_the_profile(self):
        main._seed_hdmi_routing(self.sink, self.overview())
        before = self.service.load()
        for revision, busy, expected in ((before["revision"] - 1, False, 409),
                                         (before["revision"], True, 423)):
            self.session.has_active_jobs = busy
            with self.assertRaises(HTTPException) as failure:
                await main.output_application.apply_candidate({"expected_revision": revision,
                    "mutation": {"kind": "switch_mode", "mode": "surround"}})
            self.assertEqual(failure.exception.status_code, expected)
            self.assertEqual(self.service.load(), before)
            self.assertEqual(self.profile_changes(), [])

    async def test_caller_cancellation_drains_the_owned_profile_change(self):
        main._seed_hdmi_routing(self.sink, self.overview())
        entered, release = asyncio.Event(), asyncio.Event()
        select = self.select

        async def delayed(key, **kwargs):
            if key == SURROUND71:
                entered.set()
                await release.wait()
            return await select(key, **kwargs)

        with mock.patch.object(main, "_apply_audio_output_selection", side_effect=delayed):
            task = asyncio.create_task(main.output_application.apply_candidate({
                "expected_revision": self.service.load()["revision"],
                "mutation": {"kind": "switch_mode", "mode": "surround"}}))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertTrue(self.coordinator.lock.locked())
            self.assertTrue(self.session.lock.locked())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.sink, SURROUND71)
        self.assertEqual(self.service.load()["active_mode"], "surround")
        self.assertFalse(self.coordinator.lock.locked())

    async def test_transport_result_fingerprint_matches_the_rendered_target(self):
        result = await self.automatic("stereo-sub")
        expected = self.service.fingerprint(self.service.load(), output_key=SURROUND, channels=6, sample_rate_hz=44100)
        self.assertEqual(result["fingerprint"], expected)

    async def test_cancelled_selection_rolls_back_mode_and_transport(self):
        main._seed_hdmi_routing(self.sink, self.overview())
        before = self.service.load()
        select = self.select

        async def cancel_target(key, **kwargs):
            if key == SURROUND71:
                raise asyncio.CancelledError()
            return await select(key, **kwargs)

        with mock.patch.object(main, "_apply_audio_output_selection", side_effect=cancel_target):
            with self.assertRaises(asyncio.CancelledError):
                await main.output_application.apply_candidate({"expected_revision": before["revision"],
                    "mutation": {"kind": "switch_mode", "mode": "surround"}})
        self.assertEqual(self.service.load()["modes"], before["modes"])
        self.assertEqual(self.service.load()["active_mode"], "stereo")
        self.assertEqual(self.sink, STEREO)

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

    async def select(self, key, *, seed_routing=True):
        self.selections.append(key)
        if seed_routing:
            main._seed_hdmi_routing(key, self.overview())
        # The overview the selection returns already names the chosen layout.
        self.desired_during_selection = hdmi.load_desired().get(f"{CARD}#hdmi0")
        if key in self.fail_selection:
            raise RuntimeError("graph apply failed")
        channels = hdmi.transport(hdmi.parse_sink_key(key).transport)["channels"]
        plan = self.service.compile_plan(self.service.load(), output_key=key, channels=channels, sample_rate_hz=44100)
        self.rendered_layout = self.service.compile_layout(plan)
        _save_audio_output_selection(key)
        return self.overview()

    async def change(self, key=STEREO, transport="7.1"):
        try:
            return await main._owned_output_change(
                lambda: main._apply_hdmi_transport(hdmi.parse_sink_key(key), transport, remember=True),
                what="HDMI transport test")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

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

    async def test_measurement_blocks_the_change(self):
        self.session.has_active_jobs = True
        with self.assertRaises(HTTPException) as failure:
            await self.change()
        self.assertEqual(failure.exception.status_code, 423)
        self.assertEqual(self.profile_changes(), [])

    async def test_watch_restores_the_chosen_layout_after_a_lost_connection(self):
        await self.automatic("surround")
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
