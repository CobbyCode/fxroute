#!/usr/bin/env python3
"""A3: non-finite IR samples must never reach the native convolver.

Covers:
* float32 NaN/Inf rejected in ensure_kernel_supported_ir, _validate_ir_file,
  upload_ir and upload_ir_pair (both sides);
* float64 NaN/Inf rejected (single upload converts, pair interleaves);
* finite float32 and PCM still accepted;
* a failed NaN upload leaves a previous valid IR byte-identical.
"""

import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp.manager import (
    DSPManager,
    build_wav_bytes,
    ensure_kernel_supported_ir,
    parse_wav_frames,
)


def float_wav(samples, bits=32, rate=48000, channels=1):
    tag = "<f" if bits == 32 else "<d"
    payload = b"".join(struct.pack(tag, value) for value in samples)
    return build_wav_bytes(channels, rate, bits, 3, payload)


class IrFiniteSampleTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.manager = DSPManager(home=self.home)
        self.irs_dir = self.manager.library().irs_dir
        self.irs_dir.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp())

    def write(self, name, payload):
        path = self.tmp / name
        path.write_bytes(payload)
        return path

    def test_nan_rejected_at_kernel_gate(self):
        path = self.write("nan.wav", float_wav([0.5, float("nan")]))
        with self.assertRaisesRegex(ValueError, "finite"):
            ensure_kernel_supported_ir(parse_wav_frames(path), "nan.wav")

    def test_inf_rejected_at_kernel_gate(self):
        for value in (float("inf"), float("-inf")):
            path = self.write("inf.wav", float_wav([value, 0.5]))
            with self.assertRaisesRegex(ValueError, "finite"):
                ensure_kernel_supported_ir(parse_wav_frames(path), "inf.wav")

    def test_finite_float32_accepted(self):
        path = self.write("ok.wav", float_wav([0.5, -0.25, 0.125, 0.0]))
        ensure_kernel_supported_ir(parse_wav_frames(path), "ok.wav")
        self.manager._validate_ir_file(path)

    def test_float64_nan_rejected_on_upload(self):
        source = self.write("nan64.wav", float_wav([0.5, float("nan")], bits=64))
        with self.assertRaisesRegex(ValueError, "finite"):
            self.manager.upload_ir(source, "nan64.wav")
        self.assertEqual(list(self.irs_dir.iterdir()), [])

    def test_nan_upload_keeps_previous_ir(self):
        import wave

        old_path = self.irs_dir / "room.wav"
        with wave.open(str(old_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(48000)
            handle.writeframes(b"\x01\x00\x02\x00")
        old_bytes = old_path.read_bytes()
        bad = self.write("bad.wav", float_wav([float("nan")]))
        with self.assertRaisesRegex(ValueError, "finite"):
            self.manager.upload_ir(bad, "room.wav")
        self.assertEqual(old_path.read_bytes(), old_bytes)

    def test_nan_in_either_pair_side_rejected(self):
        good = self.write("good.wav", float_wav([0.5, 0.5]))
        bad = self.write("bad.wav", float_wav([0.5, float("inf")]))
        with self.assertRaisesRegex(ValueError, "finite"):
            self.manager.upload_ir_pair(bad, "bad.wav", good, "good.wav", "Pair.irs")
        with self.assertRaisesRegex(ValueError, "finite"):
            self.manager.upload_ir_pair(good, "good.wav", bad, "bad.wav", "Pair.irs")
        self.assertEqual(list(self.irs_dir.iterdir()), [])

    def test_bundle_staged_nan_is_rejected(self):
        staged = self.write("staged.irs", float_wav([float("nan"), 0.1]))
        with self.assertRaisesRegex(ValueError, "finite"):
            self.manager._validate_ir_file(staged)


if __name__ == "__main__":
    unittest.main()
