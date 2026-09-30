#!/usr/bin/env python3
"""External input: act on a confirmed loss only, never substitute the selection.

A single failed pactl probe empties the input list and must not tear down a
working loopback; only a loss that persists for
SOURCE_UNAVAILABLE_CONFIRM_SECONDS (observed by the routing, read by the
overview) unlinks it (and falls back to App
playback when no input is left). A saved input that disappears while other
inputs remain stays selected and is shown as unavailable instead of another
input being linked, and it is relinked when it returns. The scenarios run
the real get_audio_source_overview over scripted pactl output and the real
monitor wired through main (shared publish path).
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.external_input as external_module
import audio.samplerate.overview as overview_mod
from audio.source_monitor import UnavailabilityConfirmation
import main
from scripts.test_external_input_stereo_pairs import (
    FOURCH_NAME, UMC_NAME, UMC_PORTS, _detailed_block, _pactl_info, _short_line,
)
from scripts.test_peak_monitor_line_source_arming import make_coordinator

TICK = 3.0  # EXTERNAL_INPUT_MONITOR_INTERVAL_SECONDS
SELECTED = f"{FOURCH_NAME}::pair:3-4"


class _Host:
    """Scripted capture devices, a failing-probe switch and a manual clock."""

    def __init__(self):
        self.fourch = True
        self.umc = True
        self.fourch_channels = 4
        self.probe_fails = False
        self.now = 1000.0

    def run(self, args):
        if args[:2] == ["pactl", "info"]:
            return _pactl_info()
        if self.probe_fails and args[:3] == ["pactl", "list", "sources"]:
            raise RuntimeError("Connection failure: Timeout")
        short, detailed = [], []
        if self.fourch:
            spec = f"s32le {self.fourch_channels}ch 48000Hz"
            channel_map = ("front-left,front-right" if self.fourch_channels == 2
                           else "front-left,front-right,rear-left,rear-right")
            short.append(_short_line(200, FOURCH_NAME, spec))
            detailed.append(_detailed_block(number=200, name=FOURCH_NAME, description="Test 4ch Interface",
                                            spec=spec, channel_map=channel_map))
        if self.umc:
            short.append(_short_line(100, UMC_NAME, "s32le 2ch 44100Hz"))
            detailed.append(_detailed_block(number=100, name=UMC_NAME, description="UMC204HD 192k Analog Stereo",
                                            spec="s32le 2ch 44100Hz", channel_map="front-left,front-right",
                                            ports=UMC_PORTS, active_port="analog-input-mic"))
        if args == ["pactl", "list", "sources", "short"]:
            return "\n".join(short)
        if args == ["pactl", "list", "sources"]:
            return "\n\n".join(detailed)
        raise AssertionError(f"unexpected command: {args}")


def _host_patches(host, persisted):
    return (
        patch.object(overview_mod, "_run_command", side_effect=host.run),
        patch.object(overview_mod, "get_bluetooth_audio_overview",
                     return_value={"roles": {}, "receiver_session": {}}),
        patch.object(overview_mod, "_load_audio_source_selection", return_value=persisted),
        patch.object(main.samplerate, "_load_audio_source_selection", return_value=persisted),
        patch.object(main.external_input, "availability",
                     UnavailabilityConfirmation(monotonic=lambda: host.now)),
        patch.object(main.external_input, "_remembered_input", None),
    )


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.host = _Host()
        self.persisted = {"mode": "external-input", "selected_input_key": SELECTED}
        for patcher in _host_patches(self.host, self.persisted):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _probe(self, advance=0.0, **host):
        """Build then observe, like the routing's monitor tick."""
        self.host.now += advance
        for name, value in host.items():
            setattr(self.host, name, value)
        overview = overview_mod.get_audio_source_overview()
        main.external_input._observe_availability(overview)
        return overview

    def test_single_failed_probe_keeps_the_selection(self):
        self.assertEqual(self._probe()["external_input_state"], "available")
        blip = self._probe(TICK, probe_fails=True)
        self.assertEqual(blip["mode"], "external-input")
        self.assertEqual(blip["external_input_state"], "unconfirmed")
        self.assertEqual(blip["selected_input"]["key"], SELECTED)
        self.assertTrue(blip["selected_input"]["label"].endswith("Input 3–4"), "remembered label")
        back = self._probe(TICK, probe_fails=False)
        self.assertEqual(back["external_input_state"], "available")
        self.assertTrue(back["selected_input"].get("available", True))

    def test_sustained_loss_of_every_input_falls_back(self):
        self._probe()
        self.assertEqual(self._probe(TICK, probe_fails=True)["mode"], "external-input")
        self.assertEqual(self._probe(TICK)["mode"], "external-input")
        confirmed = self._probe(TICK)
        self.assertEqual(confirmed["mode"], "app-playback")
        self.assertEqual(confirmed["external_input_state"], "unavailable")

    def test_missing_selection_is_kept_and_never_substituted(self):
        self._probe()
        self._probe(TICK, fourch=False)
        self._probe(TICK)
        gone = self._probe(TICK)
        self.assertEqual(gone["mode"], "external-input", "other inputs keep external input usable")
        self.assertEqual(gone["external_input_state"], "unavailable")
        self.assertEqual(gone["selected_input"]["key"], SELECTED)
        self.assertFalse(gone["selected_input"]["available"])
        self.assertIsNone(gone["current_input"], "no default input stands in")
        self.assertEqual([item["key"] for item in gone["inputs"]], [f"{UMC_NAME}::analog-input-mic"])
        back = self._probe(TICK, fourch=True)
        self.assertEqual((back["external_input_state"], back["selected_input"]["key"]), ("available", SELECTED))

    def test_building_the_overview_never_records_a_probe(self):
        for _ in range(5):
            self.host.now += TICK
            self.host.fourch = False
            self.assertEqual(overview_mod.get_audio_source_overview()["external_input_state"], "unconfirmed")
        self.assertFalse(main.external_input.availability.confirmed())

    def test_a_vanished_pair_never_migrates_to_another_pair(self):
        missing = self._probe(fourch_channels=2)
        self.assertEqual(missing["selected_input"]["key"], SELECTED)
        self.assertFalse(missing["selected_input"]["available"])
        self.assertIsNone(missing["current_input"])


class MonitorScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = _Host()
        self._lock = main.runtime.source_transition_lock
        main.runtime.source_transition_lock = None
        main.source_overview_feed.reset()
        self.broadcast = AsyncMock()
        self.coordinator, _monitor = make_coordinator()
        self.links = []
        self.routing = main.external_input

        async def connect(ports, sink):
            self.links.append(("connect", ports[0].split(":")[0], ports[0].split("_")[-1], sink))

        async def disconnect(ports, sink):
            self.links.append(("disconnect", ports[0].split(":")[0], ports[0].split("_")[-1], sink))

        persisted = {"mode": "external-input", "selected_input_key": SELECTED}
        for patcher in (
            *_host_patches(self.host, persisted),
            patch.object(main.manager, "broadcast", self.broadcast),
            patch.object(main, "peak_monitor_coordinator", self.coordinator),
            patch.object(external_module.pw_link, "connect_ports", connect),
            patch.object(external_module.pw_link, "disconnect_ports", disconnect),
            patch.object(external_module, "input_links_present", AsyncMock(return_value=True)),
            patch.object(self.routing, "loopback_source_name", None),
            patch.object(self.routing, "loopback_selection_key", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        main.runtime.source_transition_lock = self._lock
        main.source_overview_feed.reset()

    async def _tick(self, **host):
        self.host.now += TICK
        for name, value in host.items():
            setattr(self.host, name, value)
        await self.routing._monitor_once()

    def _pushed(self):
        return [(call.args[0]["data"]["mode"], call.args[0]["data"]["external_input_state"])
                for call in self.broadcast.await_args_list if call.args[0]["type"] == "source"]

    def _linked_sources(self, action):
        return [source for kind, source, _channel, _sink in self.links if kind == action]

    async def test_transient_probe_failure_keeps_the_loopback(self):
        await self._tick()
        self.assertEqual(self.routing.loopback_selection_key, SELECTED)
        self.assertEqual(self.coordinator.signature, f"external:{SELECTED}")
        links_before = list(self.links)
        await self._tick(probe_fails=True)
        await self._tick(probe_fails=False)
        self.assertEqual(self.links, links_before, "no unlink, no relink")
        self.assertEqual(self.routing.loopback_selection_key, SELECTED)
        self.assertEqual(self._pushed(), [("external-input", "available")], "nothing pushed for the blip")
        self.assertTrue(self.coordinator.armed)

    async def test_sustained_loss_unlinks_falls_back_and_recovers(self):
        await self._tick()
        await self._tick(probe_fails=True)
        await self._tick()
        self.assertEqual(self._linked_sources("disconnect"), [], "3 s of loss is not confirmed yet")
        await self._tick()
        self.assertEqual(self._linked_sources("disconnect"), [FOURCH_NAME, FOURCH_NAME])
        self.assertIsNone(self.routing.loopback_source_name)
        self.assertEqual(self._pushed()[-1], ("app-playback", "unavailable"))
        self.assertFalse(self.coordinator.armed)

        await self._tick(probe_fails=False)
        self.assertEqual(self.routing.loopback_selection_key, SELECTED)
        self.assertEqual(self._linked_sources("connect")[-2:], [FOURCH_NAME, FOURCH_NAME])
        self.assertEqual(self._pushed()[-1], ("external-input", "available"))
        self.assertEqual(self.coordinator.signature, f"external:{SELECTED}")

    async def test_missing_selection_is_shown_unavailable_and_restored(self):
        await self._tick()
        await self._tick(fourch=False)
        await self._tick()
        self.assertEqual(self._linked_sources("disconnect"), [])
        await self._tick()
        self.assertIsNone(self.routing.loopback_source_name)
        self.assertNotIn(UMC_NAME, self._linked_sources("connect"), "never relinked to another input")
        self.assertEqual(self._pushed()[-1], ("external-input", "unavailable"))
        pushed = self.broadcast.await_args_list[-1].args[0]["data"]
        self.assertEqual((pushed["selected_input"]["key"], pushed["selected_input"]["available"]), (SELECTED, False))
        self.assertTrue(pushed["selected_input"]["label"].startswith("Test 4ch Interface"),
                        "labelled with the entry the routing last linked")
        self.assertFalse(self.coordinator.armed, "no peak monitor for an unlinked input")

        await self._tick(fourch=True)
        self.assertEqual(self.routing.loopback_selection_key, SELECTED)
        self.assertEqual([channel for kind, _source, channel, _sink in self.links if kind == "connect"][-2:],
                         ["RL", "RR"], "the saved pair 3-4 is relinked")
        self.assertEqual(self._pushed()[-1], ("external-input", "available"))
        self.assertEqual(self.coordinator.signature, f"external:{SELECTED}")

    async def test_switch_without_key_refuses_a_missing_selection(self):
        self.host.fourch = False
        with self.assertRaisesRegex(ValueError, "not currently available"):
            overview_mod.set_audio_source_selection("external-input", None)


if __name__ == "__main__":
    unittest.main()
