#!/usr/bin/env python3
"""PCM metadata and frame boundaries must preserve every input channel."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.stdin_protocol import FrameAssembler, parse_spec


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


if __name__ == "__main__":
    unittest.main()
