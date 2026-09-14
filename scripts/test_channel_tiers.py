#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Scarlett playback altsets and explicit tier rate selection."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio import device_profiles as profiles
from audio import channel_tiers

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

    def test_rate_selection_preserves_family_without_switching_tiers(self):
        for source, rates, expected in [
            (44100, [176400, 192000], 176400), (48000, [176400, 192000], 192000),
            (96000, [44100, 48000], 48000), (88200, [44100, 48000], 44100),
            (None, [88200, 96000], 96000), (32000, [88200, 96000], 96000),
        ]:
            with self.subTest(source=source, rates=rates):
                self.assertEqual(profiles.rate_in_tier(source, rates), expected)


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
        self.force_rate = 48000
        self.events = []
        self.fail_reprobe = False
        from audio.samplerate.persistence import _save_audio_output_selection
        _save_audio_output_selection(self.old_key)

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
                self.key, self.channels, self.rate = self.new_key, 14, self.force_rate
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

    def test_default_tier_removes_rule_and_restores_multichannel_profile(self):
        from audio.channel_tiers import default_output_profile
        cards = ("Profiles:\n"
                 "  off: Off (sinks: 0, sources: 0, priority: 0, available: yes)\n"
                 "  output:multichannel-output+input:multichannel-input: Multichannel Duplex (sinks: 1, sources: 1, priority: 101, available: yes)\n"
                 "  pro-audio: Pro Audio (sinks: 1, sources: 1, priority: 1, available: yes)\n"
                 "Active Profile: pro-audio\n")
        self.assertEqual(default_output_profile(cards, self.card),
                         "output:multichannel-output+input:multichannel-input")
        self.assertIsNone(default_output_profile("Profiles:\n  pro-audio: Pro Audio (sinks: 1, sources: 1, priority: 1, available: yes)\n", self.card))
        multi = ("Card #1\n\tName: alsa_card.usb-OTHER-00\n\tProfiles:\n"
                 "\t\toff: Off (sinks: 0, sources: 0, priority: 0, available: yes)\n"
                 "\tActive Profile: off\n"
                 "Card #2\n\tName: " + self.card + "\n\tProfiles:\n"
                 "\t\toff: Off (sinks: 0, sources: 0, priority: 0, available: yes)\n"
                 "\t\toutput:multichannel-output+input:multichannel-input: Multichannel Duplex (sinks: 1, sources: 1, priority: 101, available: yes)\n"
                 "\tActive Profile: pro-audio\n")
        self.assertEqual(default_output_profile(multi, self.card),
                         "output:multichannel-output+input:multichannel-input")
        self.assertIsNone(default_output_profile(multi, "alsa_card.usb-OTHER-00"))
        # Currently on the small tier; switching back removes the rule file,
        # restores the stock profile and migrates the sink identity back.
        self.key, self.channels, self.rate = self.new_key, 14, 96000
        self.profile = "pro-audio"
        from audio.samplerate.persistence import _save_audio_output_selection
        _save_audio_output_selection(self.new_key)
        change = channel_tiers.ChannelTierChange(
            self.new_key, {"id": "18ch", "channels": 18, "rates": [44100, 48000], "probe_rate": 48000},
            48000, run=self.run_command, default_tier=True,
        )
        change.rule_path.parent.mkdir(parents=True, exist_ok=True)
        change.rule_path.write_text("stale rule")
        change.capture()
        change.apply()
        self.assertFalse(change.rule_path.exists())
        from audio.samplerate.persistence import _load_audio_output_selection
        self.assertEqual(_load_audio_output_selection()["selected_key"], self.old_key)
        self.assertEqual((self.channels, self.rate, self.profile), (18, 48000, "output:multichannel-output+input:multichannel-input"))

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


if __name__ == "__main__":
    unittest.main()
