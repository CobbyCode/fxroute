#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""HDMI transports: receiver ELD, card profiles, channel positions and recovery."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio import hdmi
from audio.output_api import output_channel_labels
from audio.output_state import default_output_state, routing_for_device, seed_device_routing, set_crossover
from audio.output_topology import derive_topology
from audio.samplerate import overview, recovery
from audio.samplerate.parsing import successor_sink_key

CARD = "alsa_card.pci-0000_00_1f.3"
STEREO = "alsa_output.pci-0000_00_1f.3.hdmi-stereo"
SURROUND = "alsa_output.pci-0000_00_1f.3.hdmi-surround"
SURROUND71 = "alsa_output.pci-0000_00_1f.3.hdmi-surround71"
ANALOG = "alsa_output.pci-0000_00_1f.3.analog-stereo"
# The Marantz AVR on the test machine: LPCM 8 ch 32-192 kHz plus bitstream codecs.
MARANTZ_ELD = bytes.fromhex(
    "10000c006b72005f0000000000000000354934006d6172616e747a2d4156520f7f073d1ec01507505f7e03570603677e035f7e01")
PORTS_51 = [f"playback_{name}" for name in ("FL", "FR", "RL", "RR", "FC", "LFE")]
PORTS_71 = PORTS_51 + ["playback_SL", "playback_SR"]


def tv_eld(name: bytes = b"TV") -> bytes:
    """A two-channel receiver: one LPCM descriptor, 2 ch at 32/44.1/48 kHz."""
    header = bytes([0x10, 0, 0, 0, 0x60 | len(name), 0x10, 0, 0x01]) + bytes(12)
    return header + name + bytes([0x09, 0x07, 0x07])


def card(connected: bool = True, active: str = "output:hdmi-stereo+input:analog-stereo",
         detected: str | None = "8") -> dict:
    profiles = {"off": {"available": True}, "pro-audio": {"available": True},
                "output:analog-stereo+input:analog-stereo": {"available": False}}
    ports = {}
    for connector in range(4):
        live = connected and connector == 0
        names = []
        for transport in ("stereo", "5.1", "7.1"):
            for input_mapping in (None, "analog-stereo"):
                name = hdmi.card_profile_name(connector, transport, input_mapping)
                profiles[name] = {"available": live}
                names.append(name)
        properties = {"audio.channels.detected": detected} if live and detected else {}
        ports[f"hdmi-output-{connector}"] = {
            "availability": "available" if live else "not available", "profiles": names, "properties": properties}
    return {"name": CARD, "active_profile": active, "profiles": profiles, "ports": ports,
            "properties": {"alsa.card": "3"}}


class SinkIdentityTests(unittest.TestCase):
    def test_transport_and_connector_from_sink_name(self):
        self.assertEqual(hdmi.parse_sink_key(SURROUND71), hdmi.HdmiSink(CARD, 0, "7.1"))
        self.assertEqual(hdmi.parse_sink_key(SURROUND), hdmi.HdmiSink(CARD, 0, "5.1"))
        self.assertEqual(hdmi.parse_sink_key("alsa_output.pci-0000_00_1f.3.hdmi-stereo-extra2"),
                         hdmi.HdmiSink(CARD, 2, "stereo"))
        self.assertIsNone(hdmi.parse_sink_key(ANALOG))
        self.assertIsNone(hdmi.parse_sink_key("fxroute_dsp_sink"))

    def test_identity_is_stable_across_transports(self):
        identities = {hdmi.parse_sink_key(key).identity for key in (STEREO, SURROUND, SURROUND71)}
        self.assertEqual(identities, {f"{CARD}#hdmi0"})
        self.assertEqual(hdmi.sink_key(CARD, 0, "7.1"), SURROUND71)
        self.assertEqual(hdmi.sink_key(CARD, 1, "5.1"), "alsa_output.pci-0000_00_1f.3.hdmi-surround-extra1")

    def test_successor_stays_on_the_same_connector(self):
        self.assertEqual(successor_sink_key(STEREO, [SURROUND71]), SURROUND71)
        # The card's analog output is another physical device.
        self.assertIsNone(successor_sink_key(STEREO, [ANALOG]))
        self.assertIsNone(successor_sink_key(ANALOG, [SURROUND71]))
        self.assertIsNone(successor_sink_key(STEREO, ["alsa_output.pci-0000_00_1f.3.hdmi-surround71-extra1"]))
        self.assertEqual(successor_sink_key(STEREO, [SURROUND, ANALOG]), SURROUND)


class EldTests(unittest.TestCase):
    def test_marantz_eld(self):
        eld = hdmi.parse_eld(MARANTZ_ELD)
        self.assertEqual(eld["monitor_name"], "marantz-AVR")
        self.assertEqual(eld["connection"], "HDMI")
        # Only the LPCM descriptor counts; DTS/AC-3/TrueHD are bitstream formats.
        self.assertEqual(eld["lpcm"], [{"channels": 8, "rates": [32000, 44100, 48000, 88200, 96000, 176400, 192000]}])
        self.assertEqual(hdmi.lpcm_max_channels(eld), 8)
        self.assertEqual(hdmi.lpcm_rates(eld, 6), eld["lpcm"][0]["rates"])

    def test_rates_follow_the_channel_count(self):
        eld = {"lpcm": [{"channels": 2, "rates": [44100, 48000, 96000, 192000]},
                        {"channels": 8, "rates": [44100, 48000]}]}
        self.assertEqual(hdmi.lpcm_rates(eld, 2), [44100, 48000, 96000, 192000])
        self.assertEqual(hdmi.lpcm_rates(eld, 8), [44100, 48000])

    def test_invalid_or_truncated_eld(self):
        self.assertIsNone(hdmi.parse_eld(None))
        self.assertIsNone(hdmi.parse_eld(b""))
        self.assertIsNone(hdmi.parse_eld(MARANTZ_ELD[:30]))
        self.assertIsNone(hdmi.parse_eld(bytes([0x08]) + MARANTZ_ELD[1:]))


class TransportOptionTests(unittest.TestCase):
    def test_eight_channel_receiver_takes_every_layout(self):
        options = hdmi.transport_options(card(), 0, hdmi.parse_eld(MARANTZ_ELD))
        self.assertEqual([(item["id"], item["channels"], item["available"]) for item in options],
                         [("stereo", 2, True), ("5.1", 6, True), ("7.1", 8, True)])

    def test_stereo_receiver_is_limited_to_stereo(self):
        options = hdmi.transport_options(card(), 0, hdmi.parse_eld(tv_eld()))
        self.assertEqual([item["available"] for item in options], [True, False, False])
        self.assertIn("at most 2 LPCM channels", options[2]["reason"])
        self.assertEqual(options[0]["rates"], [32000, 44100, 48000])
        self.assertEqual(hdmi.best_supported(options, "7.1"), "stereo")

    def test_missing_eld_uses_the_detected_channels(self):
        options = hdmi.transport_options(card(detected="6"), 0, None)
        self.assertEqual([item["available"] for item in options], [True, True, False])
        self.assertEqual(options[1]["rates"], [])

    def test_disconnected_connector_offers_nothing(self):
        options = hdmi.transport_options(card(connected=False), 0, None)
        self.assertFalse(any(item["available"] for item in options))
        self.assertEqual(options[0]["reason"], "No receiver is connected")

    def test_rates_are_narrowed_to_the_receiver(self):
        info = hdmi.describe(SURROUND71, card=card(), eld={"monitor_name": "AVR", "connection": "HDMI",
                                                          "lpcm": [{"channels": 8, "rates": [44100, 48000]}]})
        self.assertEqual(hdmi.limit_rates([44100, 48000, 96000], info), [44100, 48000])
        self.assertEqual(hdmi.limit_rates([44100, 96000], hdmi.describe(SURROUND71, card=card(), eld=None)),
                         [44100, 96000])


class CardProfileTests(unittest.TestCase):
    def test_combined_input_profile_is_kept(self):
        self.assertEqual(hdmi.target_card_profile(card(), 0, "7.1"), "output:hdmi-surround71+input:analog-stereo")
        self.assertEqual(hdmi.target_card_profile(card(active="output:hdmi-stereo"), 0, "5.1"), "output:hdmi-surround")

    def test_card_off_uses_the_remembered_input(self):
        self.assertEqual(hdmi.target_card_profile(card(active="off"), 0, "7.1", "analog-stereo"),
                         "output:hdmi-surround71+input:analog-stereo")
        self.assertEqual(hdmi.target_card_profile(card(active="off"), 0, "7.1"), "output:hdmi-surround71")

    def test_output_only_profile_when_the_combination_is_missing(self):
        source = card()
        del source["profiles"]["output:hdmi-surround71+input:analog-stereo"]
        self.assertEqual(hdmi.target_card_profile(source, 0, "7.1"), "output:hdmi-surround71")
        with self.assertRaises(ValueError):
            hdmi.target_card_profile({"profiles": {}, "active_profile": "off"}, 0, "7.1")


class ChannelPositionTests(unittest.TestCase):
    def test_labels_follow_the_receiver_channel_order(self):
        self.assertEqual(hdmi.channel_labels(PORTS_51),
                         ["Front L", "Front R", "Surround L", "Surround R", "Center", "LFE"])
        self.assertEqual(hdmi.channel_labels(PORTS_71),
                         ["Front L", "Front R", "Rear L", "Rear R", "Center", "LFE", "Side L", "Side R"])
        self.assertEqual(hdmi.channel_labels([f"{SURROUND71}:playback_FC"]), ["Center"])
        self.assertEqual(hdmi.channel_labels(["playback_AUX0"]), [None])

    def test_defaults_follow_ports_not_indices(self):
        self.assertEqual(hdmi.default_assignments(PORTS_71, "surround"),
                         ["main_l", "main_r", "rear_l", "rear_r", "center", "sub1", "side_l", "side_r"])
        self.assertEqual(hdmi.default_assignments(PORTS_51, "surround"),
                         ["main_l", "main_r", "rear_l", "rear_r", "center", "sub1"])
        shuffled = ["playback_FC", "playback_FL", "playback_LFE", "playback_FR"]
        self.assertEqual(hdmi.default_assignments(shuffled, "surround"), ["center", "main_l", "sub1", "main_r"])
        self.assertIsNone(hdmi.default_assignments(["playback_AUX0", "playback_AUX1"], "stereo"))

    def test_stereo_sub_uses_the_single_lfe_channel(self):
        defaults = hdmi.default_assignments(PORTS_51, "stereo-sub")
        self.assertEqual(defaults, ["main_l", "main_r", "off", "off", "off", "sub1"])
        topology = derive_topology("stereo-sub", defaults, channels=6)
        self.assertEqual(topology.sub_mode, "mono")
        self.assertFalse(topology.issues)
        self.assertEqual(hdmi.default_assignments(PORTS_71, "stereo"), ["main_l", "main_r"] + ["off"] * 6)

    def test_every_default_is_activatable(self):
        for ports in (PORTS_51, PORTS_71):
            for mode in ("stereo", "stereo-sub", "surround"):
                with self.subTest(channels=len(ports), mode=mode):
                    topology = derive_topology(mode, hdmi.default_assignments(ports, mode), channels=len(ports))
                    self.assertFalse(topology.issues)
                    self.assertLessEqual(len(topology.sub_roles), 1)

    def test_output_state_labels_only_for_hdmi(self):
        payload = {"output_mode": {"hardware_playback_ports": PORTS_51}}
        self.assertEqual(output_channel_labels(payload, SURROUND)[4:], ["Center", "LFE"])
        self.assertEqual(output_channel_labels(payload, "alsa_output.usb-X-00.analog-surround-51"), [])


class SeedRoutingTests(unittest.TestCase):
    def test_defaults_are_stored_once_per_layout(self):
        defaults = {mode: hdmi.default_assignments(PORTS_71, mode) for mode in ("stereo", "stereo-sub", "surround")}
        seeded = seed_device_routing(default_output_state(), SURROUND71, defaults)
        self.assertEqual(routing_for_device(seeded, "surround", SURROUND71, 8), defaults["surround"])
        self.assertIn("center", seeded["modes"]["surround"]["banks"])
        self.assertIn("sub1", seeded["modes"]["surround"]["processing"])
        edited = dict(seeded)
        edited["modes"]["surround"]["routing"][SURROUND71] = ["main_l", "main_r"] + ["off"] * 6
        again = seed_device_routing(edited, SURROUND71, defaults)
        self.assertEqual(again["modes"]["surround"]["routing"][SURROUND71], ["main_l", "main_r"] + ["off"] * 6)
        # Another layout of the same connector keeps its own routing.
        self.assertNotIn(SURROUND, again["modes"]["surround"]["routing"])

    def test_crossover_mode_keeps_its_no_default_behavior(self):
        state = set_crossover(default_output_state(), "surround", True)
        defaults = {"surround": hdmi.default_assignments(PORTS_71, "surround")}
        self.assertNotIn(SURROUND71, seed_device_routing(state, SURROUND71, defaults)["modes"]["surround"]["routing"])


class ProbeTests(unittest.TestCase):
    def test_live_sink_names_the_pcm_device(self):
        reads = []
        probe = hdmi.probe_connector(
            hdmi.HdmiSink(CARD, 0, "stereo"), cards=[card()],
            sinks=[{"name": SURROUND71, "properties": {"alsa.device": "3"}}],
            read_eld=lambda card_number, device: reads.append((card_number, device)) or MARANTZ_ELD)
        self.assertEqual(reads, [(3, 3)])
        self.assertTrue(probe["connected"])
        self.assertEqual(probe["current_key"], SURROUND71)

    def test_card_off_uses_the_remembered_device(self):
        probe = hdmi.probe_connector(hdmi.HdmiSink(CARD, 0, "7.1"), cards=[card(active="off")], sinks=[],
                                     read_eld=lambda card_number, device: MARANTZ_ELD, pcm_device=3)
        self.assertIsNone(probe["current_key"])
        self.assertTrue(probe["connected"])

    def test_known_device_without_eld_has_no_receiver(self):
        probe = hdmi.probe_connector(hdmi.HdmiSink(CARD, 0, "7.1"), cards=[card()], sinks=[],
                                     read_eld=lambda card_number, device: None, pcm_device=3)
        self.assertFalse(probe["connected"])


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.watch = hdmi.TransportWatch(clock=lambda: self.now)
        self.eight = hdmi.transport_options(card(), 0, hdmi.parse_eld(MARANTZ_ELD))
        self.two = hdmi.transport_options(card(), 0, hdmi.parse_eld(tv_eld()))
        self.identity = f"{CARD}#hdmi0"

    def decide(self, *, connected=True, current="7.1", options=None, receiver="avr", desired="7.1"):
        self.now += 2.0
        return self.watch.decide(self.identity, connected=connected, current=current, fallback="7.1",
                                 options=self.eight if options is None else options, receiver=receiver,
                                 desired=desired)

    def test_stable_connection_never_fights_an_outside_change(self):
        self.assertIsNone(self.decide())
        self.assertIsNone(self.decide(current="5.1"))
        self.assertIsNone(self.decide(current="5.1"))

    def test_lost_connection_restores_the_chosen_layout(self):
        self.decide()
        self.assertIsNone(self.decide(connected=False, current=None))
        # WirePlumber gets one tick to restore its saved profile.
        self.assertIsNone(self.decide(current="stereo"))
        self.assertEqual(self.decide(current="stereo"), "7.1")
        self.assertIsNone(self.decide(current="7.1"))
        self.assertIsNone(self.decide(current="5.1"))

    def test_card_off_after_reconnect_is_restored(self):
        self.decide(connected=False, current=None)
        self.decide(current=None)
        self.assertEqual(self.decide(current=None), "7.1")

    def test_smaller_receiver_falls_back_and_bigger_one_restores(self):
        self.decide()
        self.assertEqual(self.decide(options=self.two, receiver="tv"), "stereo")
        self.assertIsNone(self.decide(current="stereo", options=self.two, receiver="tv"))
        self.decide(connected=False, current=None)
        self.decide(current="stereo", receiver="avr")
        self.assertEqual(self.decide(current="stereo", receiver="avr"), "7.1")

    def test_receiver_change_arms_a_restore(self):
        self.decide(current="5.1", receiver="avr")
        self.decide(current="5.1", receiver="avr")
        self.assertEqual(self.decide(current="5.1", receiver="other-avr"), "7.1")

    def test_failed_restore_is_retried_only_after_a_pause(self):
        self.decide(connected=False, current=None)
        self.decide(current="stereo")
        self.assertEqual(self.decide(current="stereo"), "7.1")
        self.assertIsNone(self.decide(current="stereo"))
        self.now += hdmi.RESTORE_RETRY_SECONDS
        self.assertEqual(self.decide(current="stereo"), "7.1")

    def test_without_a_chosen_layout_only_unsupported_layouts_change(self):
        self.decide(desired=None)
        self.assertIsNone(self.decide(current="5.1", desired=None))
        self.assertEqual(self.decide(current="7.1", desired=None, options=self.two, receiver="tv"), "stereo")
        self.assertIsNone(self.decide(current="stereo", desired=None, options=self.two, receiver="tv"))


class DesiredStoreTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
            self.assertEqual(hdmi.load_desired(), {})
            hdmi.remember_desired(hdmi.HdmiSink(CARD, 0, "7.1"), "7.1", input_mapping="analog-stereo", pcm_device=3)
            self.assertEqual(hdmi.load_desired(), {f"{CARD}#hdmi0": {"transport": "7.1", "input": "analog-stereo",
                                                                    "pcm_device": 3}})
            Path(directory, "fxroute", "hdmi-transport.json").write_text("{broken")
            self.assertEqual(hdmi.load_desired(), {})


class OverviewTests(unittest.TestCase):
    def test_hdmi_outputs_carry_receiver_and_transports(self):
        outputs = [{"key": SURROUND71, "supported_rates": [44100, 48000, 96000],
                    "native_supported_rates": [44100, 48000, 96000]},
                   {"key": ANALOG, "supported_rates": [48000]}]
        eld = {"lpcm": [{"channels": 8, "rates": [44100, 48000]}]}
        raw = bytes([0x10, 0, 0, 0, 0x60, 0x10, 0, 0x7F]) + bytes(12) + bytes([0x0F, 0x06, 0x07])
        with mock.patch.object(overview, "_run_command", return_value=json.dumps([card()])), \
                mock.patch("audio.alsa_ctl.read_pcm_eld", return_value=raw) as read:
            overview._annotate_hdmi_outputs(outputs, {SURROUND71: {"alsa_card": "3", "alsa_device": "3"}}, [])
        read.assert_called_once_with(3, 3)
        info = outputs[0]["hdmi"]
        self.assertEqual(info["transport"], "7.1")
        self.assertEqual(info["receiver"]["lpcm_channels"], eld["lpcm"][0]["channels"])
        self.assertEqual(outputs[0]["supported_rates"], [44100, 48000])
        self.assertEqual(info["pcm_device"], 3)
        self.assertNotIn("hdmi", outputs[1])


class StartupRecoveryTests(unittest.TestCase):
    def test_missing_receiver_skips_the_wireplumber_reprobe(self):
        with mock.patch.object(recovery, "_run_command", return_value=json.dumps([card(connected=False)])):
            self.assertTrue(recovery._hdmi_receiver_absent(SURROUND71))
        with mock.patch.object(recovery, "_run_command", return_value=json.dumps([card()])):
            self.assertFalse(recovery._hdmi_receiver_absent(SURROUND71))
        self.assertFalse(recovery._hdmi_receiver_absent(ANALOG))


if __name__ == "__main__":
    unittest.main()
