#!/usr/bin/env python3
"""PCM metadata and frame boundaries must preserve every input channel."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_topology import SURROUND_POSITIONS
from audio.stdin_protocol import DEFAULT_LAYOUTS, MAP_ORDER, FrameAssembler, parse_channel_map, parse_spec


class PcmContractTests(unittest.TestCase):
    def test_packed_multichannel_frames_survive_fragmented_reads(self):
        spec = parse_spec(dict(version=1, format="s24le", rate=48000,
                               channels=8, left=5, right=6))
        self.assertEqual(spec.frame_bytes, 24)
        self.assertEqual((spec.left, spec.right), (5, 6))
        assembler = FrameAssembler(spec.frame_bytes)
        frames = bytes(range(72))
        result = b"".join(assembler.feed(part) for part in
                          (frames[:1], frames[1:29], frames[29:]))
        assembler.finish()
        self.assertEqual(result, frames)

    def test_mono_stereo_defaults_and_explicit_multichannel_pair(self):
        for channels, mapping in ((1, (1, 1)), (2, (1, 2))):
            spec = parse_spec(dict(version=1, format="s16le", rate=44100, channels=channels))
            self.assertEqual((spec.left, spec.right), mapping)
        spec = parse_spec(dict(version=1, format="f32le", rate=384000,
                               channels=32, left=31, right=32))
        self.assertEqual(spec.frame_bytes, 128)
        self.assertEqual(spec.header()["left"], 31)

    def test_invalid_metadata_never_becomes_an_audio_format(self):
        base = dict(version=1, format="s16le", rate=48000, channels=2)
        for change in ({"channels": 0}, {"channels": 33}, {"channels": True},
                       {"channels": 3}, {"rate": 7999}, {"rate": 384001},
                       {"rate": "48000"}, {"format": "wav"}, {"format": []},
                       {"version": 2}, {"version": True}, {"left": 1},
                       {"left": 2, "right": 2}, {"left": 0, "right": 1},
                       {"unknown": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_spec(base | change)
        for payload in (None, [], {}, {"version": 1}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_spec(payload)

    def test_partial_final_frame_is_an_error(self):
        assembler = FrameAssembler(8)
        self.assertEqual(assembler.feed(b"123456789"), b"12345678")
        with self.assertRaisesRegex(ValueError, "Incomplete PCM frame"):
            assembler.finish()


def _spec(channels, **extra):
    return parse_spec(dict(version=1, format="s16le", rate=48000, channels=channels, **extra))


class ChannelMapTests(unittest.TestCase):
    def test_map_order_covers_exactly_the_engine_positions(self):
        self.assertEqual(sorted(MAP_ORDER), sorted(SURROUND_POSITIONS))

    def test_mono_stereo_and_explicit_pairs_stay_plain_pairs(self):
        for channels, pair in ((1, (1, 1)), (2, (1, 2))):
            spec = _spec(channels)
            self.assertEqual(((spec.left, spec.right), spec.channel_map, spec.multichannel), (pair, (), False))
        spec = _spec(8, left=5, right=6)
        self.assertEqual((spec.channel_map, spec.routes, spec.multichannel),
                         ((), (("FL", 5), ("FR", 6)), False))
        self.assertEqual(spec.header(), {"version": 1, "format": "s16le", "rate": 48000,
                                         "channels": 8, "left": 5, "right": 6})

    def test_common_channel_counts_have_a_default_layout(self):
        expected = {
            4: ("FL", "FR", "BL", "BR"),
            6: ("FL", "FR", "FC", "LFE", "BL", "BR"),
            8: ("FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"),
        }
        self.assertEqual(DEFAULT_LAYOUTS, expected)
        for channels, layout in expected.items():
            with self.subTest(channels=channels):
                spec = _spec(channels)
                self.assertEqual(spec.channel_map, layout)
                self.assertEqual((spec.left, spec.right, spec.multichannel), (1, 2, True))

    def test_eight_channel_default_follows_the_wav_order_into_engine_positions(self):
        routes = dict(_spec(8).routes)
        self.assertEqual(routes, {"FL": 1, "FR": 2, "FC": 3, "LFE": 4, "BL": 5, "BR": 6, "SL": 7, "SR": 8})
        self.assertEqual([position for position, _ in _spec(8).routes], list(SURROUND_POSITIONS))

    def test_other_channel_counts_need_an_explicit_choice(self):
        for channels in (3, 5, 7, 10, 32):
            with self.subTest(channels=channels), self.assertRaisesRegex(ValueError, "--channel-map"):
                _spec(channels)

    def test_explicit_map_names_special_layouts(self):
        spec = _spec(6, channel_map="FL,FR,FC,LFE,SL,SR")
        self.assertEqual(dict(spec.routes), {"FL": 1, "FR": 2, "FC": 3, "LFE": 4, "SL": 5, "SR": 6})
        spec = _spec(4, channel_map="FL,FR,FC,LFE")
        self.assertEqual((spec.left, spec.right), (1, 2))
        spec = _spec(2, channel_map="FR,FL")
        self.assertEqual((spec.left, spec.right, spec.multichannel), (2, 1, False))

    def test_names_are_case_insensitive_and_accept_the_rear_aliases(self):
        spec = _spec(8, channel_map=" fl, fr ,fc,lfe,RL,rr,sl,SR")
        self.assertEqual(spec.channel_map, ("FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"))
        self.assertEqual(parse_channel_map(["FL", "FR", "BL", "BR"], 4), ("FL", "FR", "BL", "BR"))

    def test_skipped_and_trailing_channels_stay_unused(self):
        spec = _spec(10, channel_map="FL,FR,-,-,RL,RR")
        self.assertEqual(spec.channel_map, ("FL", "FR", "-", "-", "BL", "BR", "-", "-", "-", "-"))
        self.assertEqual(dict(spec.routes), {"FL": 1, "FR": 2, "BL": 5, "BR": 6})
        self.assertEqual(spec.frame_bytes, 20)
        spec = _spec(32, channel_map="FR,FL")
        self.assertEqual((spec.left, spec.right, spec.channel_map[2:]), (2, 1, ("-",) * 30))

    def test_front_pair_only_map_is_not_multichannel(self):
        self.assertFalse(_spec(8, channel_map="FL,FR").multichannel)
        self.assertTrue(_spec(8, channel_map="FL,FR,FC").multichannel)

    def test_header_round_trips_every_layout(self):
        for spec in (_spec(1), _spec(2), _spec(6), _spec(8, left=5, right=6),
                     _spec(12, channel_map="FL,FR,-,-,RL,RR,SL,SR")):
            with self.subTest(spec=spec):
                header = spec.header()
                self.assertEqual(("channel_map" in header), bool(spec.channel_map))
                self.assertEqual(("left" in header), not spec.channel_map)
                self.assertEqual(parse_spec(header), spec)

    def test_invalid_maps_are_rejected(self):
        for change in ({"channel_map": "FL,FR,FL"}, {"channel_map": "FL,FR,RL,BL"},
                       {"channel_map": "FL,FC"}, {"channel_map": "FR,FC,LFE"},
                       {"channel_map": "FL,FR,XX"}, {"channel_map": "FL,FR,"},
                       {"channel_map": ""}, {"channel_map": []}, {"channel_map": [1, 2]},
                       {"channel_map": None}, {"channel_map": "FL,FR,FC,LFE,RL,RR,SL,SR,FL"},
                       {"channel_map": "FL,FR,-,-,-,-,-,-,-", "channels": 8},
                       {"channel_map": "FL,FR", "left": 1, "right": 2},
                       {"channel_map": "FL,FR", "left": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                _spec(change.get("channels", 8), **{k: v for k, v in change.items() if k != "channels"})

    def test_mono_cannot_carry_a_map(self):
        with self.assertRaisesRegex(ValueError, "FL and FR"):
            _spec(1, channel_map="FL")

    def test_error_messages_name_the_problem(self):
        for text, message in (("FL,FR,FL", "more than once"), ("FL,XX", "Unknown channel position 'XX'"),
                              ("FL,FC", "include FL and FR"),
                              ("FL,FR,FC,LFE,RL,RR,SL,SR,FC", "names 9 channels")):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, message):
                _spec(8, channel_map=text)


if __name__ == "__main__":
    unittest.main()
