#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Scarlett playback altsets and explicit tier rate selection."""

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio import device_profiles as profiles
from audio import channel_tiers
from audio import system_volume

DESCRIPTORS = """Focusrite Scarlett 16i16 4th Gen
Playback:
  Status: Running
    Interface = 1
    Altset = 1
  Interface 1
    Altset 1
    Format: S32_LE
    Channels: 18
    Rates: 44100, 48000
  Interface 1
    Altset 2
    Format: S32_LE
    Channels: 14
    Rates: 88200, 96000
  Interface 1
    Altset 3
    Format: S32_LE
    Channels: 10
    Rates: 176400, 192000
Capture:
  Interface 2
    Altset 1
    Format: S32_LE
    Channels: 26
    Rates: 44100, 48000
"""


class ProfileTests(unittest.TestCase):
    def test_allowlist_requires_model_and_generation(self):
        for model in ("16i16", "18i16", "18i20"):
            self.assertIsNotNone(profiles.profile_id(f"alsa_output.usb-Focusrite_Scarlett_{model}_4th_Gen_SERIAL-00.pro-output-0"))
        for key in ("Scarlett 18i20 3rd Gen", "MOTU 18i20", "Scarlett 4i4 4th Gen", "Scarlett 18i20"):
            self.assertIsNone(profiles.profile_id(key))

    def test_usb_playback_altsets_do_not_include_capture_or_running_state(self):
        self.assertEqual(profiles.playback_tiers(DESCRIPTORS), [
            {"id": "18ch", "channels": 18, "rates": [44100, 48000], "probe_rate": 48000},
            {"id": "14ch", "channels": 14, "rates": [88200, 96000], "probe_rate": 96000},
            {"id": "10ch", "channels": 10, "rates": [176400, 192000], "probe_rate": 192000},
        ])

    def test_capture_only_and_constant_channel_devices_have_no_tiers(self):
        self.assertEqual(profiles.playback_tiers(DESCRIPTORS.split("Capture:")[1]), [])
        self.assertEqual(profiles.playback_tiers("Playback:\n  Interface 1\n    Channels: 4\n    Rates: 44100, 48000, 96000"), [])

    def test_native_tier_lookup_identifies_the_carrying_inventory(self):
        tiers = [
            {"id": "18ch", "channels": 18, "rates": [44100, 48000]},
            {"id": "14ch", "channels": 14, "rates": [88200, 96000]},
        ]
        self.assertEqual(profiles.native_tier_for_rate(tiers, 48000)["id"], "18ch")
        self.assertEqual(profiles.native_tier_for_rate(tiers, 96000)["id"], "14ch")
        self.assertIsNone(profiles.native_tier_for_rate(tiers, 192000))
        self.assertIsNone(profiles.native_tier_for_rate([], 48000))

    def test_required_tier_switch_points_at_the_carrying_inventory(self):
        selected = {
            "key": "alsa_output.scarlett",
            "device_profile": {
                "active_tier": "14ch",
                "tiers": [
                    {"id": "18ch", "channels": 18, "rates": [44100, 48000]},
                    {"id": "14ch", "channels": 14, "rates": [88200, 96000]},
                ],
            },
        }
        switch = profiles.required_tier_switch(selected, 48000)
        self.assertEqual(switch["tier"]["id"], "18ch")
        self.assertEqual(switch["key"], "alsa_output.scarlett")
        self.assertIsNone(profiles.required_tier_switch(selected, 96000))
        self.assertIsNone(profiles.required_tier_switch(selected, 32000))
        self.assertIsNone(profiles.required_tier_switch({"key": "x"}, 48000))
        self.assertEqual(profiles.device_rates(selected["device_profile"]["tiers"]),
                         [44100, 48000, 88200, 96000])


class HardwareTierTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": root.name})
        env.start()
        self.addCleanup(env.stop)
        self.old_key = "alsa_output.usb-Focusrite_Scarlett_16i16_4th_Gen_SERIAL-00.multichannel-output"
        self.new_key = self.old_key.rsplit(".", 1)[0] + ".pro-output-0"
        self.card = self.old_key.replace("alsa_output.", "alsa_card.").rsplit(".", 1)[0]
        self.profile = "output:multichannel-output+input:multichannel-input"
        self.key = self.old_key
        self.channels, self.rate, self.volume, self.mute = 18, 48000, 23, False
        self.pro_channels = 14
        self.force_rate = 48000
        self.events = []
        self.fail_reprobe = False
        wake_patch = patch.object(channel_tiers.ChannelTierChange, "_wake_sink", return_value=None)
        wake_patch.start()
        self._wake_patch = wake_patch
        self.addCleanup(self._stop_wake_patch)
        from audio.samplerate.persistence import _save_audio_output_selection
        _save_audio_output_selection(self.old_key)

    def _stop_wake_patch(self):
        try:
            self._wake_patch.stop()
        except RuntimeError:
            pass

    def run_command(self, command):
        self.events.append(tuple(command))
        if command == ["pactl", "-f", "json", "list", "sinks"]:
            return json.dumps([{"name": self.key, "sample_specification": f"s32le {self.channels}ch {self.rate}Hz",
                                "volume": {"aux0": {"value_percent": f"{self.volume}%"}}, "mute": self.mute}])
        if command == ["pactl", "list", "cards"]:
            return (f"Name: {self.card}\nProfiles:\n"
                    "  off: Off (sinks: 0, sources: 0, priority: 0, available: yes)\n"
                    "  output:multichannel-output+input:multichannel-input: Multichannel Duplex (sinks: 1, sources: 1, priority: 101, available: yes)\n"
                    "  pro-audio: Pro Audio (sinks: 1, sources: 1, priority: 1, available: yes)\n"
                    f"Active Profile: {self.profile}\n")
        if command[:2] == ["pw-metadata", "-n"]:
            self.assertEqual(self.profile, "off", "Release the device before changing the probe rate")
            self.force_rate = int(command[-1])
            return ""
        if command == ["systemctl", "--user", "restart", "wireplumber.service"]:
            if self.fail_reprobe:
                self.fail_reprobe = False
                raise RuntimeError("reprobe failed")
            self.assertEqual(self.profile, "off")
            self.volume, self.mute = 100, False
            return ""
        if command[:2] == ["pactl", "set-card-profile"]:
            self.profile = command[-1]
            if self.profile == "pro-audio":
                self.key, self.channels, self.rate = self.new_key, self.pro_channels, self.force_rate
            elif self.profile != "off":
                self.key, self.channels, self.rate = self.old_key, 18, self.force_rate
            return ""
        if command[:2] == ["pactl", "set-sink-mute"]:
            self.assertEqual(command[2], self.key)
            self.mute = command[-1] == "1"
            return ""
        if command[:2] == ["pactl", "set-sink-volume"]:
            self.assertTrue(self.mute, "Restore volume under a closed hardware gate")
            self.volume = int(command[-1].rstrip("%"))
            return ""
        if command[:2] == ["pactl", "set-default-sink"]:
            self.assertTrue(self.mute)
            self.assertEqual(self.volume, 23)
            return ""
        raise AssertionError(f"Unexpected command: {command}")

    def change(self):
        return channel_tiers.ChannelTierChange(
            self.old_key, {"id": "14ch", "channels": 14, "rates": [88200, 96000], "probe_rate": 96000},
            96000, run=self.run_command,
        )

    def test_lower_rate_accepted_inside_smaller_tier(self):
        change = channel_tiers.ChannelTierChange(
            self.old_key,
            {"id": "10ch", "channels": 10, "rates": [176400, 192000], "probe_rate": 192000},
            48000, run=self.run_command,
            tiers=[
                {"id": "18ch", "channels": 18, "rates": [44100, 48000], "probe_rate": 48000},
                {"id": "10ch", "channels": 10, "rates": [176400, 192000], "probe_rate": 192000},
            ],
        )
        self.assertEqual(change.target_rate, 48000)
        with self.assertRaisesRegex(ValueError, "not available"):
            channel_tiers.ChannelTierChange(
                self.old_key,
                {"id": "18ch", "channels": 18, "rates": [44100, 48000], "probe_rate": 48000},
                96000, run=self.run_command,
                tiers=[
                    {"id": "18ch", "channels": 18, "rates": [44100, 48000], "probe_rate": 48000},
                    {"id": "10ch", "channels": 10, "rates": [176400, 192000], "probe_rate": 192000},
                ],
            )

    def test_recreated_sink_is_gated_and_volume_restored_before_selection(self):
        change = self.change()
        change.capture()
        change.apply()
        from audio.samplerate.persistence import _load_audio_output_selection
        self.assertEqual(_load_audio_output_selection()["selected_key"], self.new_key)
        self.assertEqual((self.channels, self.rate, self.volume, self.mute), (14, 96000, 23, True))
        rule = change.rule_path.read_text()
        self.assertIn('"api.acp.pro-channels": 14', rule)
        self.assertIn('"api.acp.probe-rate": 96000', rule)

    def test_largest_tier_uses_explicit_pro_audio_path(self):
        # The stock multichannel special case is gone: even the largest
        # inventory reprobes through the pro-audio rule (PipeWire 1.4.6
        # exposed only 10ch on the stock profile at 44.1 kHz).
        self.assertFalse(hasattr(channel_tiers, "default_output_profile"))
        tiers = [
            {"id": "18ch", "channels": 18, "rates": [44100, 48000], "probe_rate": 48000},
            {"id": "14ch", "channels": 14, "rates": [88200, 96000], "probe_rate": 96000},
            {"id": "10ch", "channels": 10, "rates": [176400, 192000], "probe_rate": 192000},
        ]
        change = channel_tiers.prepare_change(
            self.old_key,
            {"id": "18ch", "channels": 18, "rates": [44100, 48000], "probe_rate": 48000},
            48000, tiers,
        )
        change.run = self.run_command
        self.assertEqual(change.new_key, self.new_key)
        self.pro_channels = 18
        change.capture()
        change.apply()
        from audio.samplerate.persistence import _load_audio_output_selection
        self.assertEqual(_load_audio_output_selection()["selected_key"], self.new_key)
        self.assertEqual((self.channels, self.rate, self.profile), (18, 48000, "pro-audio"))
        rule = change.rule_path.read_text()
        self.assertIn('"api.acp.pro-channels": 18', rule)
        self.assertIn('"api.acp.probe-rate": 48000', rule)
        self.assertIn('"device.profile": "pro-audio"', rule)

    def test_tier_to_tier_switch_stays_on_pro_audio_path(self):
        # Switching between tiers (pro -> pro) keeps the sink identity and
        # only rewrites the rule; the source rewrite is a no-op.
        from audio.samplerate.persistence import (
            _audio_source_selection_path,
            _save_audio_output_selection,
        )
        self.key, self.channels, self.rate = self.new_key, 14, 96000
        self.profile = "pro-audio"
        self.pro_channels = 14
        _save_audio_output_selection(self.new_key)
        tiers = [
            {"id": "18ch", "channels": 18, "rates": [44100, 48000], "probe_rate": 48000},
            {"id": "14ch", "channels": 14, "rates": [88200, 96000], "probe_rate": 96000},
        ]
        stale_rule = {"monitor.alsa.rules": [{"matches": [{"device.name": self.card}],
            "actions": {"update-props": {"api.acp.pro-channels": 14,
                                         "api.acp.probe-rate": 96000,
                                         "device.profile": "pro-audio"}}}]}
        source_path = _audio_source_selection_path()
        pro_source = self.new_key.replace("alsa_output.", "alsa_input.", 1).rsplit(".", 1)[0] + ".pro-input-0"
        source_path.write_text(json.dumps({"selected_input_key": pro_source}))
        change = channel_tiers.prepare_change(
            self.new_key,
            {"id": "18ch", "channels": 18, "rates": [44100, 48000], "probe_rate": 48000},
            48000, tiers,
        )
        change.run = self.run_command
        self.assertEqual(change.new_key, self.new_key)
        change.rule_path.parent.mkdir(parents=True, exist_ok=True)
        change.rule_path.write_text(json.dumps(stale_rule))
        self.pro_channels = 18
        change.capture()
        change.apply()
        from audio.samplerate.persistence import _load_audio_output_selection
        self.assertEqual(_load_audio_output_selection()["selected_key"], self.new_key)
        self.assertEqual((self.channels, self.rate, self.profile), (18, 48000, "pro-audio"))
        rule = change.rule_path.read_text()
        self.assertIn('"api.acp.pro-channels": 18', rule)
        self.assertEqual(json.loads(source_path.read_text())["selected_input_key"], pro_source)

    def test_failed_tier_to_tier_switch_rolls_back_to_pro_tier(self):
        from audio.samplerate.persistence import _save_audio_output_selection
        self.key, self.channels, self.rate = self.new_key, 14, 96000
        self.profile = "pro-audio"
        self.pro_channels = 14
        _save_audio_output_selection(self.new_key)
        tiers = [
            {"id": "18ch", "channels": 18, "rates": [44100, 48000], "probe_rate": 48000},
            {"id": "14ch", "channels": 14, "rates": [88200, 96000], "probe_rate": 96000},
        ]
        change = channel_tiers.prepare_change(
            self.new_key,
            {"id": "18ch", "channels": 18, "rates": [44100, 48000], "probe_rate": 48000},
            48000, tiers,
        )
        change.run = self.run_command
        change.rule_path.parent.mkdir(parents=True, exist_ok=True)
        change.rule_path.write_text("stale 14ch rule")
        change.capture()
        self.fail_reprobe = True
        with self.assertRaisesRegex(RuntimeError, "reprobe failed"):
            change.apply()
        change.rollback()
        from audio.samplerate.persistence import _load_audio_output_selection
        self.assertEqual(_load_audio_output_selection()["selected_key"], self.new_key)
        self.assertEqual((self.channels, self.rate, self.profile), (14, 96000, "pro-audio"))
        self.assertEqual(change.rule_path.read_text(), "stale 14ch rule")


    def test_transient_gate_readback_is_retried_not_failed(self):
        change = self.change()
        change.capture()
        self.flaky_confirm = True
        original_volume = self.volume
        real_run = self.run_command

        def flaky(command):
            if command[:2] == ["pactl", "set-sink-volume"] and self.flaky_confirm:
                self.flaky_confirm = False
                return ""
            return real_run(command)

        change.run = flaky
        with patch("time.sleep", return_value=None):
            change.apply()
        self.assertEqual(self.volume, original_volume)
        self.assertTrue(self.mute)

    def test_late_settling_spec_confirms_on_a_fresh_read(self):
        change = self.change()
        change.capture()
        # The first read that finds the recreated sink still reports the old
        # 18-channel spec; the spec settles afterwards. The confirm must
        # re-read per attempt instead of re-checking that one stale read on
        # every retry, which failed the whole reprobe before.
        real_run = self.run_command
        state = {"profiled": False, "stale": True}

        def late(command):
            if command[:2] == ["pactl", "set-card-profile"] and command[-1] == "pro-audio":
                state["profiled"] = True
            result = real_run(command)
            if state["profiled"] and state["stale"] and command == ["pactl", "-f", "json", "list", "sinks"]:
                state["stale"] = False
                payload = json.loads(result)
                payload[0]["sample_specification"] = "s32le 18ch 96000Hz"
                return json.dumps(payload)
            return result

        change.run = late
        with patch("time.sleep", return_value=None):
            change.apply()
        self.assertEqual(self.channels, 14)

    def test_reprobe_failure_restores_old_rule_selection_rate_and_volume(self):
        change = self.change()
        change.capture()
        self.fail_reprobe = True
        with self.assertRaisesRegex(RuntimeError, "reprobe failed"):
            change.apply()
        change.rollback()
        from audio.samplerate.persistence import _load_audio_output_selection
        self.assertEqual(_load_audio_output_selection()["selected_key"], self.old_key)
        self.assertEqual((self.channels, self.rate, self.volume, self.mute), (18, 48000, 23, True))
        self.assertFalse(change.rule_path.exists())

    async def _monitor_during_reprobe(self, *, rollback=False, pending_read=False, failed_apply=False):
        change = self.change()
        change.capture()
        if rollback:
            with patch("audio.channel_tiers.time.sleep", return_value=None):
                change.apply()
        old_cache = system_volume._status_volume_cache
        system_volume._status_volume_cache = (23, 0.0)
        offline = threading.Event()
        release_offline = threading.Event()
        read_started = threading.Event()
        release_read = threading.Event()
        applying = threading.Event()
        overlap = threading.Event()
        ticked = asyncio.Event()
        real_run = self.run_command
        real_sleep = asyncio.sleep
        ticks = 0

        def hardware(command):
            if command[:2] == ["pactl", "set-card-profile"] and command[-1] == "off":
                if pending_read and not release_read.is_set():
                    overlap.set()
                result = real_run(command)
                offline.set()
                if not pending_read and not release_offline.wait(3):
                    raise RuntimeError("Test did not release the offline sink")
                return result
            return real_run(command)

        def volume_command(args, **kwargs):
            if args[:2] != ["pactl", "get-sink-volume"]:
                raise AssertionError(f"Unexpected volume command: {args}")
            if pending_read and not read_started.is_set():
                read_started.set()
                if not release_read.wait(3):
                    raise RuntimeError("Test did not release the pending volume read")
            missing = self.profile == "off" or args[2] != self.key
            return system_volume.subprocess.CompletedProcess(
                args, 1 if missing else 0,
                stdout="" if missing else f"Volume: aux0: 15000 / {self.volume}% / -20.0 dB",
                stderr="Failed to get sink information: No such entity" if missing else "")

        async def monitor_sleep(delay):
            nonlocal ticks
            ticks += 1
            if ticks >= 2:
                ticked.set()
            await real_sleep(0.01)

        def reconfigure():
            applying.set()
            if failed_apply:
                self.fail_reprobe = True
                change.apply()
            else:
                (change.rollback if rollback else change.apply)()

        change.run = hardware
        worker = None
        try:
            with patch("audio.channel_tiers.time.sleep", return_value=None), \
                    patch("audio.system_volume.subprocess.run", side_effect=volume_command), \
                    patch("audio.system_volume.asyncio.sleep", side_effect=monitor_sleep), \
                    self.assertNoLogs("audio.system_volume", level="WARNING"):
                if pending_read:
                    system_volume.start_volume_read_monitor()
                    self.assertTrue(await asyncio.to_thread(read_started.wait, 2))
                worker = asyncio.create_task(asyncio.to_thread(reconfigure))
                self.assertTrue(await asyncio.to_thread(applying.wait, 2))
                if pending_read:
                    # The hardware worker must wait for the bounded read,
                    # not invalidate its target while that read is pending.
                    await real_sleep(0.05)
                    self.assertFalse(overlap.is_set())
                    release_read.set()
                else:
                    self.assertTrue(await asyncio.to_thread(offline.wait, 2))
                    system_volume.start_volume_read_monitor()
                    await asyncio.wait_for(ticked.wait(), 2)
                    self.assertEqual(system_volume.get_status_volume(), 23)
                    release_offline.set()
                if failed_apply:
                    with self.assertRaisesRegex(RuntimeError, "reprobe failed"):
                        await asyncio.wait_for(worker, 3)
                    ticks = 0
                    ticked.clear()
                    await asyncio.wait_for(ticked.wait(), 2)
                    self.assertEqual(self.profile, "off")
                    worker = asyncio.create_task(asyncio.to_thread(change.rollback))
                await asyncio.wait_for(worker, 3)
                self.assertEqual((self.channels, self.volume, self.mute),
                                 (18 if rollback or failed_apply else 14, 23, True))
                self.volume = 41  # An external change after the sink returned.
                async with asyncio.timeout(2):
                    while system_volume.get_status_volume() != 41:
                        await real_sleep(0.01)
                await system_volume.stop_volume_read_monitor()
        finally:
            release_read.set()
            release_offline.set()
            if worker is not None:
                await asyncio.gather(worker, return_exceptions=True)
            if failed_apply and self.profile == "off":
                with patch("audio.channel_tiers.time.sleep", return_value=None):
                    await asyncio.to_thread(change.rollback)
            await system_volume.stop_volume_read_monitor()
            system_volume._status_volume_cache = old_cache

    def test_volume_monitor_retains_cache_during_tier_reprobe_then_reads_new_sink(self):
        asyncio.run(self._monitor_during_reprobe())

    def test_volume_monitor_retains_cache_during_rollback_then_resumes(self):
        asyncio.run(self._monitor_during_reprobe(rollback=True))

    def test_tier_reprobe_waits_for_pending_volume_monitor_read(self):
        asyncio.run(self._monitor_during_reprobe(pending_read=True))

    def test_failed_apply_keeps_monitor_paused_until_rollback_restores_sink(self):
        asyncio.run(self._monitor_during_reprobe(failed_apply=True))

    def test_wake_keepalive_spans_gate_confirm(self):
        self._stop_wake_patch()
        FakeWakeProcess.created = []
        FakeWakeProcess.exit_first = 0
        change = self.change()
        change.capture()
        with patch("audio.channel_tiers.subprocess.Popen", FakeWakeProcess):
            with patch("time.sleep", return_value=None):
                change.apply()
        self.assertEqual(len(FakeWakeProcess.created), 1)
        player = FakeWakeProcess.created[0]
        self.assertEqual(player.argv[0], "pw-cat")
        self.assertEqual(player.argv[player.argv.index("--target") + 1], self.new_key)
        self.assertTrue(player.terminated, "keep-alive must stop after the confirm")
        self.assertFalse(Path(player.argv[-1]).exists(), "silence file must be removed")
        self.assertEqual((self.channels, self.rate, self.volume, self.mute), (14, 96000, 23, True))

    def test_exited_wake_player_is_respawned(self):
        self._stop_wake_patch()
        FakeWakeProcess.created = []
        FakeWakeProcess.exit_first = 1
        change = self.change()
        change.capture()
        with patch("audio.channel_tiers.subprocess.Popen", FakeWakeProcess):
            with patch("time.sleep", return_value=None):
                change.apply()
        self.assertEqual(len(FakeWakeProcess.created), 2)
        self.assertTrue(all(player.terminated for player in FakeWakeProcess.created))
        self.assertEqual((self.channels, self.rate), (14, 96000))

    def test_missing_player_degrades_to_direct_confirm(self):
        self._stop_wake_patch()
        change = self.change()
        change.capture()
        with patch("audio.channel_tiers.subprocess.Popen", side_effect=FileNotFoundError("pw-cat")):
            with patch("time.sleep", return_value=None):
                change.apply()
        self.assertEqual((self.channels, self.rate, self.volume, self.mute), (14, 96000, 23, True))


class FakeWakeProcess:
    """Stand-in for the silence keep-alive player (no audio stack needed)."""

    created = []
    exit_first = 0

    def __init__(self, args, **kwargs):
        self.argv = list(args)
        self.terminated = False
        self.killed = False
        type(self).created.append(self)
        self._exited = len(type(self).created) <= type(self).exit_first

    def poll(self):
        return 0 if self._exited else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class TierCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    """Rate-to-tier injection: the coordinator reprobes with no per-caller wiring."""

    PROFILE_18 = {
        "key": "alsa_output.scarlett",
        "supported_rates": [44100, 48000],
        "device_profile": {
            "id": "scarlett-16i16-4th-gen",
            "active_tier": "18ch",
            "tiers": [
                {"id": "18ch", "channels": 18, "rates": [44100, 48000]},
                {"id": "14ch", "channels": 14, "rates": [88200, 96000]},
            ],
        },
    }

    async def test_tier_reprobe_precedes_rate_and_replaces_old_capabilities(self):
        from test_playback_transition_coordinator import FakeRuntime
        from playback.transition import PlaybackTransitionCoordinator, TransitionRequest

        class TierRuntime(FakeRuntime):
            async def apply_channel_tier(self, request, snapshot):
                self.events.append("tier")
                if not self.muted or not self.paused:
                    raise AssertionError("Tier changed before source and hardware were quiet")
                assert (request.channel_tier or {})["tier"]["id"] == "14ch", request.channel_tier
                return {"selected_output": {"supported_rates": [88200, 96000]}}

            async def establish_target_rate(self, request):
                if request.audio_overview["selected_output"]["supported_rates"] != [88200, 96000]:
                    raise AssertionError("Rate alignment used stale tier capabilities")
                await super().establish_target_rate(request)

            async def commit_sample_rate_policy(self, request):
                return {"sample_rate_policy": dict(request.sample_rate_policy)}

        runtime = TierRuntime()
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        result = await coordinator.execute(TransitionRequest(
            operation="sample-rate-policy", source="local", target_rate=96000,
            target_url="/music/target.flac", should_play=True, rate_change=True,
            audio_overview={"selected_output": dict(self.PROFILE_18)},
            sample_rate_policy={"mode": "fixed", "rate": 96000},
        ))
        self.assertTrue(result.committed)
        self.assertLess(runtime.events.index("quiet"), runtime.events.index("tier"))
        self.assertLess(runtime.events.index("tier"), runtime.events.index("rate"))
        self.assertFalse(runtime.muted)

    async def test_same_tier_rate_needs_no_reprobe(self):
        from test_playback_transition_coordinator import FakeRuntime
        from playback.transition import PlaybackTransitionCoordinator, TransitionRequest

        class TierRuntime(FakeRuntime):
            async def commit_sample_rate_policy(self, request):
                return {"sample_rate_policy": dict(request.sample_rate_policy)}

        runtime = TierRuntime()
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        result = await coordinator.execute(TransitionRequest(
            operation="sample-rate-policy", source="local", target_rate=48000,
            target_url="/music/target.flac", should_play=True, rate_change=True,
            audio_overview={"selected_output": dict(self.PROFILE_18)},
            sample_rate_policy={"mode": "fixed", "rate": 48000},
        ))
        self.assertTrue(result.committed)
        self.assertNotIn("tier", runtime.events)

    async def test_refit_after_reprobe_follows_the_fitted_tier(self):
        from test_playback_transition_coordinator import FakeRuntime
        from playback.transition import PlaybackTransitionCoordinator, TransitionRequest

        seen = {}

        class DriftRuntime(FakeRuntime):
            async def apply_channel_tier(self, request, snapshot):
                self.events.append("tier")
                tier_id = (request.channel_tier or {})["tier"]["id"]
                # The hardware re-read disagrees with the pre-reprobe
                # profile: the probed band is gone, so the fit must move the
                # rate and the rate-to-tier mapping must follow it.
                tiers = [
                    {"id": "18ch", "channels": 18, "rates": [44100, 48000]},
                    {"id": "14ch", "channels": 14, "rates": [88200, 96000]},
                ]
                if tier_id != "10ch":
                    tiers.append({"id": "10ch", "channels": 10, "rates": [176400, 192000]})
                return {"selected_output": {
                    "key": "alsa_output.scarlett",
                    "supported_rates": [44100, 48000, 88200, 96000],
                    "device_profile": {
                        "id": "scarlett-16i16-4th-gen",
                        "active_tier": tier_id,
                        "tiers": tiers,
                    },
                }}

            async def establish_target_rate(self, request):
                seen["channel_tier"] = dict((request.channel_tier or {}).get("tier") or {})
                seen["target_rate"] = request.target_rate
                seen["selected"] = dict(request.audio_overview.get("selected_output") or {})
                await super().establish_target_rate(request)

            async def commit_sample_rate_policy(self, request):
                return {"sample_rate_policy": dict(request.sample_rate_policy)}

        runtime = DriftRuntime()
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        result = await coordinator.execute(TransitionRequest(
            operation="sample-rate-policy", source="local", target_rate=192000,
            target_url="/music/target.flac", should_play=True, rate_change=True,
            audio_overview={"selected_output": {
                "key": "alsa_output.scarlett",
                "supported_rates": [44100, 48000, 88200, 96000, 176400, 192000],
                "device_profile": {
                    "id": "scarlett-16i16-4th-gen",
                    "active_tier": "18ch",
                    "tiers": [
                        {"id": "18ch", "channels": 18, "rates": [44100, 48000]},
                        {"id": "14ch", "channels": 14, "rates": [88200, 96000]},
                        {"id": "10ch", "channels": 10, "rates": [176400, 192000]},
                    ],
                },
            }},
            sample_rate_policy={"mode": "fixed", "rate": 192000},
        ))
        self.assertTrue(result.committed)
        self.assertEqual(runtime.events.count("tier"), 2)
        self.assertEqual(seen["target_rate"], 96000)
        self.assertEqual(seen["channel_tier"].get("id"), "14ch")
        self.assertIsNone(profiles.required_tier_switch(seen["selected"], seen["target_rate"]))

    async def test_nonconverging_reprobe_rejects_and_rolls_back(self):
        from test_playback_transition_coordinator import FakeRuntime
        from playback.transition import (
            PlaybackTransitionCoordinator,
            PlaybackTransitionFailure,
            TransitionRequest,
        )

        full_tiers = [
            {"id": "18ch", "channels": 18, "rates": [44100, 48000]},
            {"id": "14ch", "channels": 14, "rates": [88200, 96000]},
            {"id": "10ch", "channels": 10, "rates": [176400, 192000]},
        ]

        class DriftRuntime(FakeRuntime):
            async def apply_channel_tier(self, request, snapshot):
                self.events.append("tier")
                # The hardware re-read never settles: it drops the probed
                # band every time, so each fit lands on yet another tier.
                probed = (request.channel_tier or {})["tier"]["id"]
                tiers = [dict(tier) for tier in full_tiers if tier["id"] != probed]
                rates = sorted({rate for tier in tiers for rate in tier["rates"]})
                return {"selected_output": {
                    "key": "alsa_output.scarlett",
                    "supported_rates": rates,
                    "device_profile": {
                        "id": "scarlett-16i16-4th-gen",
                        "active_tier": probed,
                        "tiers": tiers,
                    },
                }}

            async def rollback_channel_tier(self, request, snapshot, transition_id):
                self.events.append("rollback-tier")
                return True

            async def commit_sample_rate_policy(self, request):
                return {"sample_rate_policy": dict(request.sample_rate_policy)}

        runtime = DriftRuntime()
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        with self.assertRaises(PlaybackTransitionFailure):
            await coordinator.execute(TransitionRequest(
                operation="sample-rate-policy", source="local", target_rate=192000,
                should_play=False, reload_source=False, rate_change=True,
                audio_overview={"selected_output": {
                    "key": "alsa_output.scarlett",
                    "supported_rates": [44100, 48000, 88200, 96000, 176400, 192000],
                    "device_profile": {
                        "id": "scarlett-16i16-4th-gen",
                        "active_tier": "18ch",
                        "tiers": [dict(tier) for tier in full_tiers],
                    },
                }},
                sample_rate_policy={"mode": "fixed", "rate": 192000},
            ))
        self.assertEqual(runtime.events.count("tier"), 4, "reprobe passes stay bounded")
        self.assertIn("rollback-tier", runtime.events)

    async def test_verified_idle_rollback_releases_failed_transition_mute(self):
        from test_playback_transition_coordinator import FakeRuntime
        from playback.transition import PlaybackTransitionCoordinator, PlaybackTransitionFailure, TransitionRequest

        class TierRuntime(FakeRuntime):
            async def apply_channel_tier(self, request, snapshot):
                self.rate = 96000
                raise RuntimeError("sink recreation failed")

            async def rollback_channel_tier(self, request, snapshot, transition_id):
                self.rate = snapshot["active_rate"]
                self.volume = 100
                self.muted = True
                return True

        runtime = TierRuntime()
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        with self.assertRaises(PlaybackTransitionFailure) as failure:
            await coordinator.execute(TransitionRequest(
                operation="sample-rate-policy", source="local", target_rate=96000,
                should_play=False, reload_source=False, rate_change=True,
                audio_overview={"selected_output": dict(self.PROFILE_18)},
                sample_rate_policy={"mode": "fixed", "rate": 96000},
            ))
        self.assertFalse(failure.exception.failure_latched)
        self.assertFalse(coordinator.transition_blocked)
        self.assertFalse(runtime.muted)
        self.assertEqual(runtime.rate, 44100)

    async def test_failed_external_claim_rolls_back_tier_then_restores_committed_source(self):
        from test_playback_transition_coordinator import FakeRuntime
        from playback.transition import PlaybackTransitionCoordinator, PlaybackTransitionFailure, TransitionRequest

        restore = TransitionRequest(
            operation="replay", source="tidal", target_rate=44100,
            target_url="/cache/tidal.mp4",
            target_track={"source": "tidal", "url": "/cache/tidal.mp4"},
            should_play=False, rate_change=True, reload_source=True,
            detail="failed-transition-restore",
        )

        class TierRuntime(FakeRuntime):
            async def apply_channel_tier(self, request, snapshot):
                self.events.append("tier")
                return {"selected_output": {"supported_rates": [88200, 96000]}}

            async def rollback_channel_tier(self, request, snapshot, transition_id):
                self.events.append("rollback-tier")
                self.rate = snapshot["active_rate"]
                return True

            async def reconcile_post_start_graph(self, request):
                await self._stage("reconcile-post-start-graph")
                if request.source == "spotify":
                    raise RuntimeError("no producer ports for source 'spotify'")
                return {"graph_complete": True, "committed": True}

            async def abort_failed_transition(self, request, snapshot, *, target_staged):
                self.events.append("abort")
                return {"restore": restore}

        runtime = TierRuntime()
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        with self.assertRaises(PlaybackTransitionFailure) as failure:
            await coordinator.execute(TransitionRequest(
                operation="spotify-claim", source="spotify", target_rate=96000,
                should_play=True, rate_change=True, reload_source=True,
                audio_overview={"selected_output": dict(self.PROFILE_18)},
            ))
        # Same restore contract as a failed handoff without a tier switch:
        # old tier graph first, then the committed source, then the gate.
        self.assertLess(runtime.events.index("rollback-tier"), runtime.events.index("abort"))
        self.assertLess(runtime.events.index("abort"), runtime.events.index("publish-restored-source"))
        self.assertFalse(failure.exception.failure_latched)
        self.assertFalse(coordinator.gate.failure_latched)
        self.assertFalse(runtime.muted)
        self.assertEqual(runtime.rate, 44100)


class TierApplySnapshotTests(unittest.IsolatedAsyncioTestCase):
    """apply_channel_tier stores the captured change on the snapshot for rollback."""

    TIER = {"id": "14ch", "channels": 14, "rates": [88200, 96000], "probe_rate": 96000}
    KEY = "alsa_output.usb-Focusrite_Scarlett_16i16_4th_Gen_SERIAL-00.multichannel-output"

    def _runtime(self, drained):
        from playback.runtime.channel_tier import _RuntimeChannelTierMixin

        class TierRuntime(_RuntimeChannelTierMixin):
            def __init__(self, deps):
                self._deps = deps
                self.applied = None

            async def _change_tier_hardware(self, change, *, rollback=False):
                self.applied = change
                return {"selected_output": {}}

        async def drain_worker(change_step):
            # The mixin test targets the snapshot store, not the hardware read:
            # record the step (a bound method, as the real drain receives it).
            drained.append(change_step.__name__)

        return TierRuntime(SimpleNamespace(drain_worker=drain_worker))

    def _request(self):
        return SimpleNamespace(
            audio_overview={"selected_output": {"device_profile": {"tiers": [dict(self.TIER)]}}},
            channel_tier={"key": self.KEY, "tier": dict(self.TIER)},
            target_rate=96000,
        )

    async def test_dict_snapshot_stores_the_change_for_rollback(self):
        drained = []
        runtime = self._runtime(drained)
        snapshot = {}
        overview = await runtime.apply_channel_tier(self._request(), snapshot)
        self.assertIs(snapshot["channel_tier_change"], runtime.applied)
        self.assertEqual(drained, ["capture"])
        self.assertIn("selected_output", overview)

    async def test_immutable_snapshot_still_applies_but_warns_about_rollback(self):
        runtime = self._runtime([])
        snapshot = MappingProxyType({})
        with self.assertLogs("playback.runtime.channel_tier", level="WARNING") as logs:
            await runtime.apply_channel_tier(self._request(), snapshot)
        self.assertTrue(any("rollback will be unavailable" in message for message in logs.output))
        self.assertIsNotNone(runtime.applied, "the unstoraged change must still drive the hardware stage")


class TierAdapterMultiPassTests(unittest.IsolatedAsyncioTestCase):
    """Real adapter multi-pass: each newly derived tier/rate gets its own apply.

    Drives the real ``_RuntimeChannelTierMixin`` with real
    ``ChannelTierChange`` objects over a fake pactl transport: two passes
    must switch the hardware to the two derived tier/rate targets while the
    pass-1 change stays the rollback record, and the rollback must restore
    the pre-transition hardware state.
    """

    FULL_TIERS = [
        {"id": "18ch", "channels": 18, "rates": [44100, 48000], "probe_rate": 48000},
        {"id": "14ch", "channels": 14, "rates": [88200, 96000], "probe_rate": 96000},
        {"id": "10ch", "channels": 10, "rates": [176400, 192000], "probe_rate": 192000},
    ]
    CHANNELS_BY_RATE = {48000: 18, 96000: 14, 192000: 10}
    MULTI_PROFILE = "output:multichannel-output+input:multichannel-input"

    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": root.name})
        env.start()
        self.addCleanup(env.stop)
        self.old_key = "alsa_output.usb-Focusrite_Scarlett_16i16_4th_Gen_SERIAL-00.multichannel-output"
        self.new_key = self.old_key.rsplit(".", 1)[0] + ".pro-output-0"
        self.card = self.old_key.replace("alsa_output.", "alsa_card.", 1).rsplit(".", 1)[0]
        self.key, self.channels, self.rate = self.old_key, 18, 48000
        self.profile, self.volume, self.mute = self.MULTI_PROFILE, 23, False
        self.force_rate = 48000
        wake_patch = patch.object(channel_tiers.ChannelTierChange, "_wake_sink", return_value=None)
        wake_patch.start()
        self.addCleanup(wake_patch.stop)
        prepare_patch = patch.object(channel_tiers, "prepare_change", self._prepare_with_fake_run)
        prepare_patch.start()
        self.addCleanup(prepare_patch.stop)
        sleep_patch = patch("time.sleep", return_value=None)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        from audio.samplerate.persistence import _save_audio_output_selection
        _save_audio_output_selection(self.old_key)

    def _prepare_with_fake_run(self, output_key, tier, target_rate, tiers):
        return channel_tiers.ChannelTierChange(
            output_key, tier, target_rate, run=self.run_command, tiers=tiers,
        )

    def run_command(self, command):
        if command == ["pactl", "-f", "json", "list", "sinks"]:
            return json.dumps([{"name": self.key,
                                 "sample_specification": f"s32le {self.channels}ch {self.rate}Hz",
                                 "volume": {"aux0": {"value_percent": f"{self.volume}%"}}, "mute": self.mute}])
        if command == ["pactl", "list", "cards"]:
            return (f"Name: {self.card}\nProfiles:\n"
                    "  off: Off (sinks: 0, sources: 0, priority: 0, available: yes)\n"
                    "  output:multichannel-output+input:multichannel-input: Multichannel Duplex (sinks: 1, sources: 1, priority: 101, available: yes)\n"
                    "  pro-audio: Pro Audio (sinks: 1, sources: 1, priority: 1, available: yes)\n"
                    f"Active Profile: {self.profile}\n")
        if command[:2] == ["pw-metadata", "-n"]:
            self.force_rate = int(command[-1])
            return ""
        if command == ["systemctl", "--user", "restart", "wireplumber.service"]:
            return ""
        if command[:2] == ["pactl", "set-card-profile"]:
            self.profile = command[-1]
            if self.profile == "pro-audio":
                self.key, self.channels, self.rate = self.new_key, self.CHANNELS_BY_RATE[self.force_rate], self.force_rate
            elif self.profile != "off":
                self.key, self.channels, self.rate = self.old_key, 18, self.force_rate
            return ""
        if command[:2] == ["pactl", "set-sink-mute"]:
            self.mute = command[-1] == "1"
            return ""
        if command[:2] == ["pactl", "set-sink-volume"]:
            self.volume = int(command[-1].rstrip("%"))
            return ""
        if command[:2] == ["pactl", "set-default-sink"]:
            return ""
        raise AssertionError(f"Unexpected command: {command}")

    def _overview(self):
        by_channels = {18: "18ch", 14: "14ch", 10: "10ch"}
        return {"selected_output": {
            "key": self.key,
            "supported_rates": [44100, 48000, 88200, 96000, 176400, 192000],
            "device_profile": {
                "id": "scarlett-16i16-4th-gen",
                "active_tier": by_channels[self.channels],
                "tiers": [dict(tier) for tier in self.FULL_TIERS],
            },
        }}

    def _runtime(self, *, player=None, quieted=None):
        from playback.runtime.channel_tier import _RuntimeChannelTierMixin
        from playback.runtime.snapshot import _RuntimeSnapshotMixin

        class AdapterRuntime(_RuntimeChannelTierMixin, _RuntimeSnapshotMixin):
            def __init__(self, deps):
                self._deps = deps
                self._dsp_runtime = None
                self._player = player
                self._staged_target_url = None
                self._quieted_external_source = quieted
                self.rates = []

            def invalidate_gate_sink_resolution(self):
                pass

            async def establish_target_rate(self, request):
                self.rates.append(request.target_rate)

            async def establish_effects_and_helper(self, request):
                pass

            async def reconcile_post_start_graph(self, request):
                pass

            async def verify_output_mode_runtime(self, request):
                pass

            async def set_source_volume(self, volume, transition_id):
                pass

            async def stabilize_effects_after_rate_change(self, request, dsp_reinitialized=False):
                pass

        async def drain_worker(step, *args):
            return step(*args)

        return AdapterRuntime(SimpleNamespace(
            audio_configuration_lock=None,
            measurement_audio_graph_owned=lambda: False,
            drain_worker=drain_worker,
            get_audio_output_overview=self._overview,
            player_is_running=lambda: player is not None,
            mark_player_state_authoritative=lambda _state: None,
            queue=lambda: SimpleNamespace(native_request_fields=lambda: {}),
            get_samplerate_status=lambda: {"active_rate": self.rate},
            coordinator_target_rate=lambda _source, _track: None,
        ))

    def _request(self, tier_id, target_rate):
        from playback.transition import TransitionRequest
        tier = next(tier for tier in self.FULL_TIERS if tier["id"] == tier_id)
        return TransitionRequest(
            operation="sample-rate-policy", source="local",
            target_rate=target_rate, target_url=None,
            audio_overview=self._overview(),
            channel_tier={"key": self.key, "tier": dict(tier), "rates": list(tier["rates"])},
            sample_rate_policy={"mode": "fixed", "rate": target_rate},
        )

    async def test_two_passes_apply_two_targets_then_rollback_restores_origin(self):
        runtime = self._runtime()
        snapshot = {"player": {}, "sample_rate_policy": {"mode": "fixed", "rate": 48000}}
        await runtime.apply_channel_tier(self._request("14ch", 96000), snapshot)
        self.assertEqual((self.key, self.channels, self.rate), (self.new_key, 14, 96000))
        await runtime.apply_channel_tier(self._request("10ch", 192000), snapshot)
        self.assertEqual((self.key, self.channels, self.rate), (self.new_key, 10, 192000))
        stored = snapshot.get("channel_tier_change")
        self.assertEqual(stored.tier.get("id"), "14ch")
        self.assertEqual(stored.target_rate, 96000)
        self.assertTrue(await runtime.rollback_channel_tier(
            self._request("10ch", 192000), snapshot, "test-tid"))
        self.assertEqual((self.key, self.channels, self.rate, self.profile),
                         (self.old_key, 18, 48000, self.MULTI_PROFILE))
        from audio.samplerate.persistence import _load_audio_output_selection
        self.assertEqual(_load_audio_output_selection()["selected_key"], self.old_key)

    def _record_source_stages(self, runtime):
        started, reconciled = [], []

        async def prepare_target_source(request):
            started.append(("prepare", request))

        async def start_target_source(request):
            started.append(("start", request))

        async def reconcile_post_start_graph(request):
            reconciled.append(request)

        runtime.prepare_target_source = prepare_target_source
        runtime.start_target_source = start_target_source
        runtime.reconcile_post_start_graph = reconcile_post_start_graph
        return started, reconciled

    def _old_tidal_snapshot(self):
        return {
            "player": {"current_file": "/cache/tidal.mp4", "paused": True, "position": 12.0},
            "current_track": {"source": "tidal", "url": "/cache/tidal.mp4"},
            "sample_rate_policy": {"mode": "fixed", "rate": 48000},
        }

    async def test_rollback_after_external_target_neither_starts_nor_awaits_it(self):
        from dataclasses import replace
        runtime = self._runtime()
        started, reconciled = self._record_source_stages(runtime)
        snapshot = self._old_tidal_snapshot()
        request = replace(self._request("14ch", 96000), operation="spotify-claim",
                          source="spotify", should_play=True)
        await runtime.apply_channel_tier(request, snapshot)
        self.assertTrue(await runtime.rollback_channel_tier(request, snapshot, "test-tid"))
        self.assertEqual((self.key, self.channels, self.rate), (self.old_key, 18, 48000))
        self.assertEqual(started, [])
        self.assertIsNone(reconciled[0].target_url)
        self.assertFalse(reconciled[0].should_play)

    NEW_TIDAL = "/cache/tidal-new.mp4"
    NEW_RADIO = "https://radio.example/new"

    async def _rollback_mpv_target(self, source, target_url, snapshot, *, quieted=None):
        """Fail a TIDAL/Radio target after its tier switch and roll back."""
        from dataclasses import replace
        player = SimpleNamespace(stops=0, state={})

        def stop_playback():
            player.stops += 1

        player.stop_playback = stop_playback
        runtime = self._runtime(player=player, quieted=quieted)
        started, reconciled = self._record_source_stages(runtime)
        snapshot = dict(snapshot, active_rate=48000,
                        sample_rate_policy={"mode": "auto", "rate": None})
        request = replace(
            self._request("14ch", 96000), operation="play", source=source,
            target_url=target_url, target_track={"source": source, "url": target_url},
            should_play=True, sample_rate_policy=None,
        )
        await runtime.apply_channel_tier(request, snapshot)
        self.assertTrue(await runtime.rollback_channel_tier(request, snapshot, "test-tid"))
        self.assertEqual((self.key, self.channels, self.rate), (self.old_key, 18, 48000))
        # The failed target is never started, loaded or awaited again.
        for stage_request in [req for _, req in started] + reconciled:
            self.assertNotEqual(stage_request.target_url, target_url)
        return [(stage, req.source, req.target_url, req.should_play, req.restore_position)
                for stage, req in started], reconciled, player

    async def test_rollback_spotify_to_tidal_resumes_spotify(self):
        started, _reconciled, player = await self._rollback_mpv_target(
            "tidal", self.NEW_TIDAL, {"player": {"current_file": None}}, quieted="spotify")
        self.assertEqual(started, [("prepare", "spotify", None, True, None),
                                   ("start", "spotify", None, True, None)])
        self.assertEqual(player.stops, 1)

    async def test_rollback_qobuz_to_radio_resumes_qobuz(self):
        started, _reconciled, player = await self._rollback_mpv_target(
            "radio", self.NEW_RADIO, {"player": {"current_file": None}}, quieted="qobuz")
        self.assertEqual(started, [("prepare", "qobuz", None, True, None),
                                   ("start", "qobuz", None, True, None)])
        self.assertEqual(player.stops, 1)

    async def test_rollback_from_empty_mpv_starts_nothing(self):
        for source, url in (("tidal", self.NEW_TIDAL), ("radio", self.NEW_RADIO)):
            with self.subTest(source=source):
                started, reconciled, player = await self._rollback_mpv_target(
                    source, url, {"player": {}})
                self.assertEqual(started, [])
                self.assertEqual(player.stops, 1)
                self.assertIsNone(reconciled[0].target_url)
                self.assertFalse(reconciled[0].should_play)

    async def test_rollback_restores_playing_local_source_at_position(self):
        snapshot = {
            "player": {"current_file": "/music/old.flac", "playing": True, "paused": False,
                       "position": 30.0},
            "current_track": {"source": "local", "url": "/music/old.flac"},
        }
        started, _reconciled, player = await self._rollback_mpv_target(
            "radio", self.NEW_RADIO, snapshot)
        self.assertEqual(started, [("prepare", "local", "/music/old.flac", True, 30.0),
                                   ("start", "local", "/music/old.flac", True, 30.0)])
        self.assertEqual(player.stops, 0)

    async def test_rollback_restores_paused_tidal_source_at_position(self):
        snapshot = {
            "player": {"current_file": "/cache/tidal-old.mp4", "playing": False, "paused": True,
                       "position": 12.0},
            "current_track": {"source": "tidal", "url": "/cache/tidal-old.mp4"},
        }
        started, _reconciled, player = await self._rollback_mpv_target(
            "radio", self.NEW_RADIO, snapshot, quieted=None)
        self.assertEqual(started, [("prepare", "tidal", "/cache/tidal-old.mp4", False, 12.0),
                                   ("start", "tidal", "/cache/tidal-old.mp4", False, 12.0)])
        self.assertEqual(player.stops, 0)

    async def test_rollback_restores_playing_radio_without_position(self):
        snapshot = {
            "player": {"current_file": "https://radio.example/old", "playing": True,
                       "paused": False, "position": 95.0},
            "current_track": {"source": "radio", "url": "https://radio.example/old"},
        }
        started, _reconciled, _player = await self._rollback_mpv_target(
            "tidal", self.NEW_TIDAL, snapshot)
        self.assertEqual(started, [("prepare", "radio", "https://radio.example/old", True, None),
                                   ("start", "radio", "https://radio.example/old", True, None)])

    async def test_failed_later_pass_keeps_pause_on_original_rollback_owner(self):
        runtime = self._runtime()
        snapshot = {"player": {}, "sample_rate_policy": {"mode": "fixed", "rate": 48000}}
        await runtime.apply_channel_tier(self._request("14ch", 96000), snapshot)
        request = self._request("10ch", 192000)
        run = self.run_command

        def fail_restart(command):
            if command == ["systemctl", "--user", "restart", "wireplumber.service"]:
                raise RuntimeError("later reprobe failed")
            return run(command)

        def read_volume(args, **kwargs):
            missing = self.profile == "off" or args[2] != self.key
            return system_volume.subprocess.CompletedProcess(
                args, 1 if missing else 0,
                stdout="" if missing else f"Volume: aux0: 15000 / {self.volume}% / -20.0 dB",
                stderr="Failed to get sink information: No such entity" if missing else "")

        old_cache = system_volume._status_volume_cache
        system_volume._status_volume_cache = (23, 0.0)
        self.run_command = fail_restart
        try:
            with patch("audio.system_volume.subprocess.run", side_effect=read_volume), \
                    patch.object(system_volume, "VOLUME_MONITOR_INTERVAL_SECONDS", 0.01), \
                    self.assertNoLogs("audio.system_volume", level="WARNING"):
                with self.assertRaisesRegex(RuntimeError, "later reprobe failed"):
                    await runtime.apply_channel_tier(request, snapshot)
                system_volume.start_volume_read_monitor()
                await asyncio.sleep(0.05)
                self.assertEqual(system_volume.get_status_volume(), 23)
                self.assertTrue(await runtime.rollback_channel_tier(request, snapshot, "test-tid"))
                self.assertEqual((self.key, self.channels, self.rate), (self.old_key, 18, 48000))
                self.volume = 41
                async with asyncio.timeout(2):
                    while system_volume.get_status_volume() != 41:
                        await asyncio.sleep(0.01)
                await system_volume.stop_volume_read_monitor()
        finally:
            await system_volume.stop_volume_read_monitor()
            system_volume._status_volume_cache = old_cache


if __name__ == "__main__":
    unittest.main()
