#!/usr/bin/env python3
"""Compare saved real stereo S32 carriers against FFmpeg's reference decoder."""
import array
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
CAPTURES = Path(os.environ.get("SURROUND_CAPTURE_ROOT", "/tmp/opencode"))
BUS = ["FL", "FR", "FC", "LFE", "SL", "SR", "BL", "BR"]


def floats(path):
    result = array.array("f")
    result.frombytes(path.read_bytes())
    return result


class SavedCaptureTests(unittest.TestCase):
    def check_capture(self, directory, name, codec, positions):
        capture = CAPTURES / directory
        source = capture / f"{name}.s32"
        if not source.is_file():
            self.skipTest(f"saved capture unavailable: {source}")
        with tempfile.TemporaryDirectory(prefix="surround-verify-", dir=CAPTURES) as temporary:
            out = Path(temporary) / "decoded.f32"
            status_path = Path(temporary) / "status.json"
            subprocess.run([str(ROOT / "build/fxroute-surround-input"), "--offline", str(source),
                            str(out), "--status", str(status_path), "--chunk", "19"], check=True)
            actual = floats(out)
            status = json.loads(status_path.read_text())
            self.assertEqual(len(actual), source.stat().st_size)
            self.assertTrue(all(math.isfinite(v) for v in actual))
            self.assertEqual(status["codec"], codec)
            self.assertEqual(status["positions"], positions)
            if codec == "pcm":
                expected = array.array("f", (integer / 2147483648
                    for (integer,) in struct.iter_unpack("<i", source.read_bytes())))
                for channel in range(2):
                    self.assertTrue(actual[channel::8] == expected[channel::2])
                for channel in range(2, 8):
                    self.assertTrue(all(v == 0 for v in actual[channel::8]))
                print(f"{directory}: PCM unity, {len(actual) // 8} frames")
                return
            carrier = capture / f"{name}.iec61937"
            reference = array.array("f")
            reference.frombytes(subprocess.check_output([
                "ffmpeg", "-v", "error", "-f", "spdif", "-i", str(carrier), "-f", "f32le", "pipe:1"]))
            data = carrier.read_bytes()
            offset, reference_frame, bursts, error = 0, 0, 0, 0.0
            while True:
                offset = data.find(bytes.fromhex("72f81f4e"), offset)
                if offset < 0 or offset + 8 > len(data):
                    break
                kind, bits = struct.unpack_from("<HH", data, offset + 4)
                duration = {1: 1536, 11: 512, 12: 1024, 13: 2048}[kind & 31]
                if offset + 8 + (bits + 7) // 8 > len(data):
                    break
                first = offset // 4
                count = min(duration, len(actual) // 8 - first)
                for channel, position in enumerate(positions):
                    a = actual[first * 8 + BUS.index(position):(first + count) * 8:8]
                    b = reference[reference_frame * len(positions) + channel:
                                  (reference_frame + count) * len(positions):len(positions)]
                    self.assertEqual(len(a), len(b))
                    error = max(error, max((abs(x - y) for x, y in zip(a, b)), default=0))
                reference_frame += duration
                bursts += 1
                offset += duration * 4
            self.assertGreater(bursts, 100)
            self.assertEqual(status["decoded_bursts"], bursts)
            self.assertLess(error, 1e-5)
            for position in set(BUS) - set(positions):
                self.assertTrue(all(v == 0 for v in actual[BUS.index(position)::8]))
            self.assertGreater(max(abs(v) for v in actual), 0.001)
            print(f"{directory}: {codec} {positions}, {bursts} bursts, max error {error:.3g}, "
                  f"state={status['state']}, errors={status['errors']}")

    def test_ac3_51(self):
        self.check_capture("surround-fixed-pcm-20261004-113059", "spdif-pair", "ac3", BUS[:6])

    def test_ac3_stereo(self):
        self.check_capture("surround-dd20-source-20261004-114328", "pair", "ac3", BUS[:2])

    def test_dts_51(self):
        self.check_capture("surround-dts-source-20261004-114626", "pair", "dts", BUS[:6])

    def test_source_decoded_mpeg_pcm(self):
        self.check_capture("surround-mpeg-source-20261004-113459", "pair", "pcm", BUS[:2])

    def test_mpeg_pcm_with_raw_dts_lookalike_stays_unity(self):
        # Real TV MPEG-2 PCM whose -225/+232 pair at 49.5 s once latched raw DTS.
        self.check_capture("surround-mpeg-rawdts-hit-20261004", "pair", "pcm", BUS[:2])


if __name__ == "__main__":
    unittest.main()
