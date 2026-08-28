#!/usr/bin/env python3
"""Dual-IR upload must accept every kernel-supported WAV encoding.

Regression: the measurement FIR export writes IEEE float32 mono WAVs (WAVE
format tag 3), and upload_ir_pair parsed them with Python's ``wave`` module,
which only reads PCM (format tag 1). Every "Take Both -> Create Convolver
Preset" therefore died with ``wave.Error: unknown format: 3`` (HTTP 500) and
the preset never existed — let alone appeared in the A/B list. The interleave
now parses the RIFF chunks directly and preserves the source encoding, so
float32 pairs produce a float32 stereo .irs kernel exactly like the ones the
native loader (native_dsp/dsp.c load_wav) accepts.
"""

import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp.manager import DSPManager, parse_wav_frames


def wav_bytes(samples, bits=32, format_tag=3, rate=48000, channels=1):
    """Serialize a canonical RIFF/WAVE file (the measurement export shape)."""
    width = bits // 8
    if format_tag == 3 and bits == 32:
        payload = b"".join(struct.pack("<f", float(sample)) for sample in samples)
    elif format_tag == 1 and bits == 16:
        payload = b"".join(struct.pack("<h", int(sample)) for sample in samples)
    elif format_tag == 1 and bits == 32:
        payload = b"".join(struct.pack("<i", int(sample)) for sample in samples)
    else:
        raise AssertionError("unsupported test encoding")
    data = payload if channels > 1 else payload
    byte_rate = rate * channels * width
    block_align = channels * width
    return b"".join([
        b"RIFF", (36 + len(data)).to_bytes(4, "little"), b"WAVE",
        b"fmt ", (16).to_bytes(4, "little"),
        format_tag.to_bytes(2, "little"), channels.to_bytes(2, "little"),
        rate.to_bytes(4, "little"), byte_rate.to_bytes(4, "little"),
        block_align.to_bytes(2, "little"), bits.to_bytes(2, "little"),
        b"data", len(data).to_bytes(4, "little"),
        data,
    ])


def interleaved_expected(left, right, bits=32, format_tag=3):
    width = bits // 8
    packer = "<f" if format_tag == 3 else ("<h" if bits == 16 else "<i")
    out = b""
    for left_sample, right_sample in zip(left, right):
        out += struct.pack(packer, float(left_sample) if packer == "<f" else int(left_sample))
        out += struct.pack(packer, float(right_sample) if packer == "<f" else int(right_sample))
    return out


class DualIrWavFormatTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.manager = DSPManager(home=self.home)
        self.tmp = Path(tempfile.mkdtemp())

    def write(self, name, payload):
        path = self.tmp / name
        path.write_bytes(payload)
        return path

    def test_float32_mono_pair_interleaves_into_stereo_float_kernel(self):
        # The exact encoding the measurement FIR export writes (format 3).
        left = self.write("l.wav", wav_bytes([0.25, -0.5, 1.0, 0.0]))
        right = self.write("r.wav", wav_bytes([-1.0, 0.5, 0.25, 0.75]))
        result = self.manager.upload_ir_pair(left, "l.wav", right, "r.wav", "Pair.irs")
        self.assertEqual(result["name"], "Pair.irs")
        parsed = parse_wav_frames(self.manager.irs_dir / "Pair.irs")
        self.assertEqual(parsed["channels"], 2)
        self.assertEqual(parsed["format"], 3)
        self.assertEqual(parsed["bits"], 32)
        self.assertEqual(parsed["rate"], 48000)
        self.assertEqual(parsed["samples"], 8)   # 4 frames x 2 channels
        expected = interleaved_expected([0.25, -0.5, 1.0, 0.0], [-1.0, 0.5, 0.25, 0.75])
        self.assertEqual(parsed["data"], expected)

    def test_pcm16_pair_interleaves_into_stereo_pcm_kernel(self):
        left = self.write("l.wav", wav_bytes([100, -200, 300], bits=16, format_tag=1))
        right = self.write("r.wav", wav_bytes([-100, 200, 150], bits=16, format_tag=1))
        self.manager.upload_ir_pair(left, "l.wav", right, "r.wav", "Pair.irs")
        parsed = parse_wav_frames(self.manager.irs_dir / "Pair.irs")
        self.assertEqual((parsed["channels"], parsed["format"], parsed["bits"]), (2, 1, 16))
        self.assertEqual(
            parsed["data"],
            interleaved_expected([100, -200, 300], [-100, 200, 150], bits=16, format_tag=1),
        )

    def test_shorter_right_channel_is_padded_with_silence(self):
        left = self.write("l.wav", wav_bytes([0.5, 0.5, 0.5, 0.5]))
        right = self.write("r.wav", wav_bytes([0.25, 0.25]))
        self.manager.upload_ir_pair(left, "l.wav", right, "r.wav", "Pair.irs")
        parsed = parse_wav_frames(self.manager.irs_dir / "Pair.irs")
        self.assertEqual(parsed["samples"], 8)
        samples = struct.unpack("<8f", parsed["data"])
        # Interleaved L,R pairs; right channel frames 3-4 are silence-padded.
        self.assertEqual(samples[1::2], (0.25, 0.25, 0.0, 0.0))
        self.assertEqual(samples[0::2], (0.5, 0.5, 0.5, 0.5))

    def test_stereo_input_is_rejected(self):
        left = self.write("l.wav", wav_bytes([0.5, 0.5], channels=2))
        right = self.write("r.wav", wav_bytes([0.5, 0.5]))
        with self.assertRaisesRegex(ValueError, "mono WAV files"):
            self.manager.upload_ir_pair(left, "l.wav", right, "r.wav", "Pair.irs")

    def test_mismatched_rates_are_rejected(self):
        left = self.write("l.wav", wav_bytes([0.5, 0.5], rate=44100))
        right = self.write("r.wav", wav_bytes([0.5, 0.5], rate=48000))
        with self.assertRaisesRegex(ValueError, "formats must match"):
            self.manager.upload_ir_pair(left, "l.wav", right, "r.wav", "Pair.irs")

    def test_unsupported_encoding_is_rejected(self):
        # float64 (format 3, 64 bits) is not kernel-supported.
        payload = struct.pack("<d", 0.5)

        def float64_wav():
            return b"".join([
                b"RIFF", (36 + len(payload)).to_bytes(4, "little"), b"WAVE",
                b"fmt ", (16).to_bytes(4, "little"),
                (3).to_bytes(2, "little"), (1).to_bytes(2, "little"),
                (48000).to_bytes(4, "little"), (48000 * 8).to_bytes(4, "little"),
                (8).to_bytes(2, "little"), (64).to_bytes(2, "little"),
                b"data", len(payload).to_bytes(4, "little"),
                payload,
            ])

        left = self.write("l.wav", float64_wav())
        right = self.write("r.wav", float64_wav())
        with self.assertRaisesRegex(ValueError, "not kernel-supported"):
            self.manager.upload_ir_pair(left, "l.wav", right, "r.wav", "Pair.irs")

    def test_non_wave_file_is_rejected(self):
        left = self.write("l.bin", b"not a wave file")
        right = self.write("r.wav", wav_bytes([0.5, 0.5]))
        with self.assertRaisesRegex(ValueError, "not a RIFF/WAVE"):
            self.manager.upload_ir_pair(left, "l.bin", right, "r.wav", "Pair.irs")


if __name__ == "__main__":
    unittest.main()
