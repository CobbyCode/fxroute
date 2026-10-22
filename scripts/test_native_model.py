#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Native-stage mirrors shared by the AutoSub predictor and the Polarity Check guard.

The expected values were produced by the AutoSub helpers before they moved into
dsp/native_model.py; the module has two consumers, so they stay as a guard.
"""

import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.native_model import native_peq_coefficients, read_mono_ir

COEFFICIENTS = {
    ("bell", 100.0, 48000, 1.0, 6.0): (1.004590142798984, -1.9906054594511924, 0.986185871262942, -1.9906054594511924, 0.990776014061926),
    ("bell", 3000.0, 44100, 0.7071, -4.5): (0.8887046561257189, -1.3190898218353462, 0.5607886718270337, -1.3190898218353462, 0.4494933279527527),
    ("bell", 10000.0, 96000, 5.0, 12.0): (1.0882606647666668, -1.5397290160875508, 0.8525252850628575, -1.5397290160875508, 0.9407859498295243),
    ("notch", 100.0, 48000, 1.0, 6.0): (0.9934977580728941, -1.9868252854194828, 0.9934977580728941, -1.9868252854194828, 0.9869955161457884),
    ("notch", 3000.0, 44100, 0.7071, -4.5): (0.7733230657550614, -1.4075022842205975, 0.7733230657550614, -1.4075022842205975, 0.5466461315101228),
    ("notch", 10000.0, 96000, 5.0, 12.0): (0.9426171063340048, -1.4956568598514821, 0.9426171063340048, -1.4956568598514821, 0.8852342126680093),
    ("lowpass", 100.0, 48000, 1.0, 6.0): (4.255768157635559e-05, 8.511536315271118e-05, 4.255768157635559e-05, -1.9868252854194828, 0.9869955161457884),
    ("lowpass", 3000.0, 44100, 0.7071, -4.5): (0.03478596182238132, 0.06957192364476264, 0.03478596182238132, -1.4075022842205975, 0.5466461315101228),
    ("lowpass", 10000.0, 96000, 5.0, 12.0): (0.09739433820413183, 0.19478867640826367, 0.09739433820413183, -1.4956568598514821, 0.8852342126680093),
    ("highpass", 100.0, 48000, 1.0, 6.0): (0.9934552003913177, -1.9869104007826355, 0.9934552003913177, -1.9868252854194828, 0.9869955161457884),
    ("highpass", 3000.0, 44100, 0.7071, -4.5): (0.7385371039326801, -1.4770742078653603, 0.7385371039326801, -1.4075022842205975, 0.5466461315101228),
    ("highpass", 10000.0, 96000, 5.0, 12.0): (0.8452227681298728, -1.6904455362597457, 0.8452227681298728, -1.4956568598514821, 0.8852342126680093),
    ("lowshelf", 100.0, 48000, 1.0, 6.0): (1.0022893494695855, -1.9888659840027618, 0.9868173430567425, -1.9889260182450266, 0.9890466582840636),
    ("lowshelf", 3000.0, 44100, 0.7071, -4.5): (0.9259746727886379, -1.3664905931394211, 0.5436138733204074, -1.3314989407714253, 0.5045801984770412),
    ("lowshelf", 10000.0, 96000, 5.0, 12.0): (1.1989451334213717, -1.3927970223777983, 1.025501247507687, -1.7041712646775005, 0.9130721386293561),
    ("highshelf", 100.0, 48000, 1.0, 6.0): (1.990704895771644, -3.9593647618479757, 1.9689000247926691, -1.9843231747950583, 0.9845633335113948),
    ("highshelf", 3000.0, 44100, 0.7071, -4.5): (0.6432812484332124, -0.8565283009069425, 0.3245869800109891, -1.4757321482931478, 0.5870720758304069),
    ("highshelf", 10000.0, 96000, 5.0, 12.0): (3.320478639564081, -5.6586642825205455, 3.03183653269987, -1.1616853712089732, 0.8553362609523788),
}


def wav_bytes(frames: np.ndarray, *, bits: int, float_format: bool = False, rate: int = 48000) -> bytes:
    """A RIFF/WAVE file holding ``frames`` (frames x channels) in the given encoding."""
    channels = frames.shape[1]
    if float_format:
        data = frames.astype("<f4").tobytes()
    elif bits == 16:
        data = np.round(frames * 32768.0).astype("<i2").tobytes()
    elif bits == 32:
        data = np.round(frames * 2147483648.0).astype("<i4").tobytes()
    else:
        ints = np.round(frames * 8388608.0).astype(np.int32).reshape(-1)
        data = b"".join(int(value & 0xFFFFFF).to_bytes(3, "little") for value in ints)
    block = channels * (bits // 8)
    fmt = struct.pack("<HHIIHH", 3 if float_format else 1, channels, rate, rate * block, block, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


class NativePeqCoefficientsTest(unittest.TestCase):
    def test_all_six_filter_types_keep_their_coefficients(self):
        for arguments, expected in COEFFICIENTS.items():
            with self.subTest(arguments=arguments):
                self.assertEqual(native_peq_coefficients(*arguments), expected)

    def test_unsupported_type_and_non_positive_q_fail(self):
        with self.assertRaisesRegex(ValueError, "Unsupported PEQ filter type: allpass"):
            native_peq_coefficients("allpass", 1000.0, 48000, 1.0, 0.0)
        with self.assertRaisesRegex(ValueError, "PEQ q must be positive"):
            native_peq_coefficients("bell", 1000.0, 48000, 0.0, 3.0)


class ReadMonoIrTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.frames = np.array([[0.5, -0.25], [-1.0 + 1 / 32768, 0.125], [0.0, 0.75]])

    def write(self, name: str, raw: bytes) -> str:
        path = Path(self.directory.name) / name
        path.write_bytes(raw)
        return str(path)

    def test_every_supported_encoding_reads_the_selected_channel(self):
        cases = {
            "pcm16": wav_bytes(self.frames, bits=16),
            "pcm24": wav_bytes(self.frames, bits=24),
            "pcm32": wav_bytes(self.frames, bits=32),
            "float32": wav_bytes(self.frames, bits=32, float_format=True),
        }
        for name, raw in cases.items():
            path = self.write(f"{name}.wav", raw)
            for channel in (0, 1):
                with self.subTest(encoding=name, channel=channel):
                    np.testing.assert_allclose(read_mono_ir(path, channel), self.frames[:, channel], atol=1e-4)

    def test_negative_24_bit_samples_sign_extend(self):
        path = self.write("pcm24.wav", wav_bytes(np.array([[-0.5], [0.5]]), bits=24))
        self.assertEqual(read_mono_ir(path, 0).tolist(), [-0.5, 0.5])

    def test_bad_files_and_channels_fail_closed(self):
        good = self.write("good.wav", wav_bytes(self.frames, bits=16))
        with self.assertRaisesRegex(ValueError, "IR channel must be a non-negative integer"):
            read_mono_ir(good, -1)
        with self.assertRaisesRegex(ValueError, "IR channel must be a non-negative integer"):
            read_mono_ir(good, True)
        with self.assertRaisesRegex(ValueError, "IR channel 2 exceeds the 2 channels"):
            read_mono_ir(good, 2)
        with self.assertRaisesRegex(ValueError, "IR file is not readable"):
            read_mono_ir(str(Path(self.directory.name) / "missing.wav"), 0)
        with self.assertRaisesRegex(ValueError, "IR file is not readable"):
            read_mono_ir(self.write("junk.wav", b"not a wave file at all"), 0)
        eight_bit = bytearray(wav_bytes(self.frames, bits=16))
        eight_bit[34:36] = struct.pack("<H", 8)
        with self.assertRaisesRegex(ValueError, "IR file has an unsupported encoding"):
            read_mono_ir(self.write("eight.wav", bytes(eight_bit)), 0)


if __name__ == "__main__":
    unittest.main()
