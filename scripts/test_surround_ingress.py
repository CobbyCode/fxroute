#!/usr/bin/env python3
"""Fail-closed digital ingress and lifecycle regressions (stdlib only)."""
import array
import asyncio
import gzip
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
NATIVE = ROOT / "native_surround"
POSITIONS = ["FL", "FR", "FC", "LFE", "SL", "SR", "BL", "BR"]


def words_to_carrier(data):
    return b"".join(struct.pack("<i", word << 16)
                    for (word,) in struct.iter_unpack("<h", data))


def burst(kind, payload=b"\x00\x00", frames=1536):
    header = struct.pack("<HHHH", 0xF872, 0x4E1F, kind, len(payload) * 8)
    return words_to_carrier((header + payload).ljust(frames * 4, b"\x00"))


def pcm(frames, left=0.125, right=-0.25):
    return struct.pack("<ii", int(left * 2147483648), int(right * 2147483648)) * frames


class NativeIngressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (NATIVE / "build.sh").is_file():
            raise AssertionError("native Surround implementation is missing")
        deps = ["libpipewire-0.3", "libavcodec", "libavutil", "libswresample"]
        if subprocess.run(["pkg-config", "--exists", *deps]).returncode:
            raise unittest.SkipTest("missing native Surround build dependencies; build on .104")
        subprocess.run(["sh", str(NATIVE / "build.sh")], check=True)
        cls.binary = NATIVE / "build/fxroute-surround-input"

    def run_carrier(self, data, chunk=257, stereo=False):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "input.s32").write_bytes(data)
            command = [str(self.binary), "--offline", str(base / "input.s32"),
                            str(base / "output.f32"), "--status", str(base / "status.json"),
                            "--chunk", str(chunk)] + (["--stereo"] if stereo else [])
            result = subprocess.run(command, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            output = array.array("f")
            output.frombytes((base / "output.f32").read_bytes())
            self.assertEqual(len(output), len(data) // 8 * (2 if stereo else 8))
            self.assertTrue(all(math.isfinite(v) for v in output))
            return output, json.loads((base / "status.json").read_text())

    def test_pcm_is_unity_front_pair_with_six_silent_positions(self):
        output, status = self.run_carrier(pcm(17001), 1)
        self.assertEqual(list(output[:16]), [0.125, -0.25, 0, 0, 0, 0, 0, 0] * 2)
        self.assertEqual(status["codec"], "pcm")
        self.assertEqual(status["positions"], ["FL", "FR"])

    def test_stereo_tap_preserves_pcm_and_encoded_stereo_at_unity(self):
        output, _ = self.run_carrier(pcm(17001), stereo=True)
        self.assertEqual(list(output), [0.125, -0.25] * 17001)
        for codec in ("ac3", "dca"):
            with self.subTest(codec=codec):
                carrier = self.encoded_fixture(codec, "stereo")
                output, _ = self.run_carrier(words_to_carrier(carrier), stereo=True)
                reference = self.reference(carrier)
                self.assertEqual(len(output), len(reference))
                self.assertLess(max(abs(a-b) for a, b in zip(output, reference)), 1e-5)

    def test_stereo_downmix_contains_center_surround_and_lfe_with_shared_headroom(self):
        for codec in ("ac3", "dca"):
            with self.subTest(codec=codec):
                carrier = self.encoded_fixture(codec, "5.1(side)")
                output, status = self.run_carrier(words_to_carrier(carrier), stereo=True)
                reference = self.reference(carrier)
                # FL/FR unity, Center/Surround -3 dB, LFE -6 dB; one common normalization.
                gain = 1 / (1 + math.sqrt(2) + 0.5)
                for side in range(2):
                    expected = [(reference[i + side] + math.sqrt(0.5) * reference[i + 2]
                                 + 0.5 * reference[i + 3] + math.sqrt(0.5) * reference[i + 4 + side]) * gain
                                for i in range(0, len(reference), 6)]
                    self.assertLess(max(abs(a-b) for a, b in zip(output[side::2], expected)), 1e-5)
                self.assertEqual(status["positions"], POSITIONS[:6], "downmix must not relabel the input format")

    def test_stereo_tap_tracks_layout_changes_without_stale_normalization(self):
        stereo = self.encoded_fixture("ac3", "stereo")
        multichannel = self.encoded_fixture("ac3", "5.1(side)")
        data = words_to_carrier(stereo + multichannel + stereo) + pcm(20000)
        output, _ = self.run_carrier(data, chunk=1, stereo=True)
        start = (len(stereo) + len(multichannel)) // 4
        reference = self.reference(stereo)
        actual = output[start * 2:start * 2 + len(reference)]
        self.assertLess(max(abs(a-b) for a, b in zip(actual, reference)), 1e-5)
        self.assertEqual(list(output[-8000:]), [0.125, -0.25] * 4000)

    def test_stereo_tap_mutes_unsupported_and_corrupt_carriers(self):
        for kind in (0, 3, 4, 1, 0x0b):
            with self.subTest(kind=kind):
                output, _ = self.run_carrier(burst(kind, b"\xff\x7f" * 300) * 5, stereo=True)
                self.assertTrue(all(value == 0 for value in output))

    def test_unsupported_corrupt_null_and_pause_never_escape_as_pcm(self):
        for kind in (0, 3, 4, 0x15, 1, 0x0B):
            with self.subTest(kind=kind):
                output, status = self.run_carrier(burst(kind, b"\xff\x7f" * 300) * 5, 3)
                self.assertTrue(all(v == 0 for v in output))
                if kind not in (0, 3):
                    self.assertGreater(status["errors"], 0)

    def test_unknown_latch_requires_safe_silence_before_pcm(self):
        output, _ = self.run_carrier(burst(4) + pcm(4000) + pcm(10000, 0, 0) + pcm(10000))
        self.assertTrue(all(v == 0 for v in output[:(1536 + 4000) * 8]))
        self.assertEqual(list(output[-8:]), [0.125, -0.25, 0, 0, 0, 0, 0, 0])

    def test_split_header_is_detected_before_pcm_release(self):
        # More than a maximum DTS burst of lookahead protects preamble transitions.
        output, _ = self.run_carrier(pcm(12000) + burst(4) + pcm(4000), 1)
        self.assertEqual(list(output[:8]), [0.125, -0.25, 0, 0, 0, 0, 0, 0])
        self.assertTrue(all(v == 0 for v in output[12000 * 8:]))

    def test_raw_dts_big_little_and_14bit_syncs_are_muted(self):
        # Raw DTS repeats its sync every frame; it stays muted past the sync timeout.
        for sync in ("7ffe8001", "fe7f0180", "1fffe800", "ff1f00e8"):
            with self.subTest(sync=sync):
                frame = (bytes.fromhex(sync) + b"\x55\xaa" * 1023)
                output, status = self.run_carrier(words_to_carrier(frame * 32), 1)
                self.assertTrue(all(v == 0 for v in output))
                self.assertGreater(status["errors"], 0)
                self.assertEqual(status["mute_reason"], "raw-dts")

    def test_isolated_raw_dts_patterns_in_pcm_never_drop_audio(self):
        # Captured TV PCM hit 0xff1f00e8 (-225/+232) once a minute; it is not DTS.
        words = [1000 + i % 509 for i in range(2 * 24000)]
        for frame, (left, right) in ((3000, (0xff1f, 0x00e8)), (7001, (0xfe7f, 0x0180)),
                                     (12345, (0xff1f, 0x00e8)), (20000, (0x1fff, 0xe800))):
            words[2 * frame], words[2 * frame + 1] = left, right
        data = struct.pack(f"<{len(words)}H", *words)
        output, status = self.run_carrier(words_to_carrier(data), 7)
        expected = [struct.unpack("<h", struct.pack("<H", word))[0] / 32768 for word in words]
        self.assertEqual([output[i * 8 + c] for i in range(24000) for c in (0, 1)], expected)
        self.assertEqual((status["codec"], status["state"], status["errors"]), ("pcm", "pcm", 0))

    def test_confirmed_raw_dts_never_reopens_pcm_between_long_sync_intervals(self):
        for spacing in (7000, 8192):
            with self.subTest(spacing=spacing):
                frame = bytes.fromhex("fe7f0180") + b"\x55\xaa" * (spacing * 2 - 2)
                output, status = self.run_carrier(words_to_carrier(frame * 5), 1)
                self.assertTrue(all(value == 0 for value in output[spacing * 2 * 8:]),
                                "confirmed raw-DTS carrier escaped before its next accepted sync")
                self.assertEqual(status["state"], "muted")
                self.assertEqual(status["mute_reason"], "raw-dts")

    def test_iec_recovery_uses_short_timeout_after_confirmed_raw_dts(self):
        raw = bytes.fromhex("fe7f0180") + b"\x55\xaa" * 1022
        carrier = self.encoded_fixture("ac3", "stereo")
        data = words_to_carrier(raw * 5 + carrier) + pcm(9000)
        output, status = self.run_carrier(data, 1)
        resumed = 5 * 512 + len(carrier) // 4 + 5122
        self.assertEqual(list(output[resumed * 8:(resumed + 1) * 8]),
                         [0.125, -0.25, 0, 0, 0, 0, 0, 0])
        self.assertEqual(status["state"], "pcm")

    def test_confirmed_raw_dts_keeps_suppression_during_spacing_and_packing_changes(self):
        for change in ("spacing", "packing"):
            with self.subTest(change=change):
                period = 3584 if change == "spacing" else 4096
                syncs = [(0, "fe7f0180"), (4096, "fe7f0180"), (8192, "fe7f0180")]
                syncs += [(8192 + period * i, "7ffe8001" if change == "packing" else "fe7f0180")
                          for i in range(1, 7)]
                carrier = bytearray(b"\x55\xaa" * ((syncs[-1][0] + period) * 2))
                for frame, sync in syncs:
                    carrier[frame * 4:frame * 4 + 4] = bytes.fromhex(sync)
                output, status = self.run_carrier(words_to_carrier(carrier), 1)
                self.assertTrue(all(value == 0 for value in output[8192 * 8:]),
                                "confirmed raw-DTS suppression was lost during requalification")
                self.assertEqual(status["state"], "muted")

    def test_encoded_stream_ending_without_silence_returns_to_pcm(self):
        output, status = self.run_carrier(burst(4) * 3 + pcm(20000), 5)
        last_burst = 2 * 1536
        resumed = last_burst + 6658
        self.assertTrue(all(v == 0 for v in output[:resumed * 8]))
        self.assertEqual(list(output[resumed * 8:(resumed + 1) * 8]),
                         [0.125, -0.25, 0, 0, 0, 0, 0, 0])
        self.assertEqual(list(output[-8:]), [0.125, -0.25, 0, 0, 0, 0, 0, 0])
        self.assertEqual(status["state"], "pcm")

    def test_pcm_tail_survives_supported_unsupported_and_pause_transitions(self):
        carriers = [self.encoded_fixture("ac3", "stereo"),
                    self.encoded_fixture("dca", "5.1(side)")]
        carriers += [struct.pack("<HHHH", 0xf872, 0x4e1f, kind, 16).ljust(6144, b"\0")
                     for kind in (0, 3, 4, 0x81)]
        for carrier in carriers:
            for shifted in (False, True):
                with self.subTest(kind=carrier[4], shifted=shifted):
                    prefix = pcm(9000)
                    if shifted:
                        # Preamble starts on the right port of the boundary frame.
                        prefix += struct.pack("<i", 0)
                    output, _ = self.run_carrier(prefix + words_to_carrier(carrier)
                                                + (struct.pack("<i", 0) if shifted else b""), 1)
                    # Prefix safety mutes the still-pending 2050 frames before IEC
                    # acquisition; earlier PCM stays unity. Up to 42.71 ms of real
                    # PCM immediately before a transition is lost by design.
                    kept = 9000 - 2050 + (1 if shifted else 0)
                    self.assertTrue(output[:kept * 8] == array.array("f",
                                    [0.125, -0.25, 0, 0, 0, 0, 0, 0] * kept),
                                    "IEC transition discarded PCM before the retractable window")
                    self.assertTrue(all(v == 0 for v in output[kept * 8:9000 * 8]),
                                    "pending PCM before IEC acquisition was not suppressed")

    def test_mid_payload_startup_is_not_established_pcm(self):
        for codec, duration in (("ac3", 1536), ("dca", 512)):
            for stereo in (False, True):
                with self.subTest(codec=codec, stereo=stereo):
                    carrier = self.encoded_fixture(codec, "stereo")[40:]
                    output, status = self.run_carrier(words_to_carrier(carrier), 1, stereo=stereo)
                    channels = 2 if stereo else 8
                    self.assertTrue(all(value == 0 for value in output[:(duration - 10) * channels]),
                                    "partial startup payload was released as PCM")
                    self.assertGreater(max(abs(value) for value in output), 0.001)
                    self.assertLess(max(abs(value) for value in output), 0.5)
                    self.assertEqual(status["codec"], "dts" if codec == "dca" else codec)

    def check_captured_switch_prefix(self, pcm_source):
        fixtures = ROOT / "scripts/fixtures/spdif-switch"
        manifest = json.loads((fixtures / "manifest.json").read_text())
        for event in manifest["events"]:
            data = gzip.decompress((fixtures / event["file"]).read_bytes())
            self.assertEqual(hashlib.sha256(data).hexdigest(), event["s32_sha256"])
            preamble = event["preamble_frame"]
            if pcm_source:
                # Established PCM followed by the actual unframed TV payload.
                data = pcm(9000) + data[(preamble - 2050) * 8:]
                preamble = 9000 + 2050
            carrier = b"".join(struct.pack("<h", integer >> 16)
                               for (integer,) in struct.iter_unpack("<i", data[preamble * 8:]))
            reference = self.reference(carrier)
            self.assertEqual(len(reference), 3 * 1536 * 2)
            for stereo in (False, True):
                channels = 2 if stereo else 8
                for chunk in (1, 257, 1024):
                    with self.subTest(event=event["event"], stereo=stereo, chunk=chunk):
                        output, status = self.run_carrier(data, chunk, stereo=stereo)
                        prefix = output[(preamble - 2050) * channels:preamble * channels]
                        self.assertGreater(max(abs(integer / 2147483648) for (integer,) in
                            struct.iter_unpack("<i", data[(preamble - 2050) * 8:preamble * 8])), 0.99)
                        self.assertEqual(max(abs(value) for value in prefix), 0,
                                         "captured orphan payload escaped as PCM before IEC acquisition")
                        decoded = output[preamble * channels:]
                        self.assertGreater(max(abs(value) for value in decoded), 0.001)
                        for side in range(2):
                            self.assertLess(max(abs(a - b) for a, b in
                                zip(decoded[side::channels], reference[side::2])), 1e-5)
                        if not stereo:
                            self.assertTrue(all(value == 0 for channel in range(2, 8)
                                                for value in decoded[channel::8]))
                        self.assertEqual((status["codec"], status["state"]), ("ac3", "encoded"))

    def test_captured_dd_gap_dd_orphan_prefix_is_silent(self):
        self.check_captured_switch_prefix(pcm_source=False)

    def test_captured_pcm_dd_orphan_prefix_is_silent(self):
        self.check_captured_switch_prefix(pcm_source=True)

    def test_longest_corrupt_dts_burst_cannot_leak_past_lookahead(self):
        # Payload completes at the last word, including a cross-frame preamble.
        data = pcm(9000) + struct.pack("<i", 0)
        data += burst(0x0d, b"\xff\x7f" * 4092, 2048) + struct.pack("<i", 0)
        output, status = self.run_carrier(data, 1)
        kept = 9000 - 2050 + 1
        self.assertTrue(output[:kept * 8] == array.array("f", [0.125, -0.25, 0, 0, 0, 0, 0, 0] * kept))
        self.assertTrue(all(v == 0 for v in output[kept * 8:]))
        self.assertEqual(status["mute_reason"], "corrupt")

    def test_eac3_maximum_period_never_falls_back_between_bursts(self):
        output, status = self.run_carrier(burst(0x15, b"\xff\x7f" * 100, 6144) * 5, 19)
        self.assertTrue(all(v == 0 for v in output))
        self.assertEqual(status["state"], "muted")

    def test_rapid_codec_switches_preserve_all_decoded_frames(self):
        ac3 = self.encoded_fixture("ac3", "stereo")[:6144]
        dts = self.encoded_fixture("dca", "5.1(side)")[:2048]
        multichannel = self.encoded_fixture("ac3", "5.1(side)")[:6144]
        segments = [(ac3, 2), (dts, 6), (multichannel, 6)] * 12
        output, status = self.run_carrier(words_to_carrier(b"".join(data for data, _ in segments)), 1)
        offset = 0
        for data, channels in segments:
            reference = self.reference(data)
            for channel in range(channels):
                actual = output[offset * 8 + channel:(offset + len(data) // 4) * 8:8]
                self.assertLess(max(abs(a - b) for a, b in zip(actual, reference[channel::channels])), 1e-5)
            offset += len(data) // 4
        self.assertEqual(status["errors"], 0)
        self.assertEqual(status["decoded_bursts"], 36)

    def encoded_fixture(self, codec, layout):
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg fixture encoder unavailable")
        channels = 2 if layout == "stereo" else 6
        expressions = "|".join(f"0.05*sin(2*PI*{90 + i * 70}*t)" for i in range(channels))
        command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                   f"aevalsrc={expressions}:s=48000:d=0.32:c={layout}",
                   # Full 2048-byte DTS packets trigger FFmpeg's raw-DTS muxing exception.
                   "-c:a", codec, "-strict", "-2", "-b:a", "640k" if codec == "ac3" else "1411200",
                   "-f", "spdif", "pipe:1"]
        return subprocess.check_output(command)

    def reference(self, carrier):
        decoded = subprocess.check_output(
            ["ffmpeg", "-v", "error", "-f", "spdif", "-i", "pipe:0", "-f", "f32le", "pipe:1"],
            input=carrier)
        result = array.array("f")
        result.frombytes(decoded)
        return result

    def test_ac3_stereo_side_layout_and_lfe_unity_against_decoder(self):
        for layout, positions in (("stereo", ["FL", "FR"]),
                                  ("5.1(side)", ["FL", "FR", "FC", "LFE", "SL", "SR"]),
                                  ("5.1", ["FL", "FR", "FC", "LFE", "SL", "SR"])):
            with self.subTest(layout=layout):
                carrier = self.encoded_fixture("ac3", layout)
                output, status = self.run_carrier(words_to_carrier(carrier), 7)
                reference = self.reference(carrier)
                self.assertEqual(status["codec"], "ac3")
                self.assertEqual(status["positions"], positions)
                self.assertEqual(status["sample_rate"], 48000)
                self.assertEqual(status["errors"], 0)
                for ch, pos in enumerate(positions):
                    actual = output[POSITIONS.index(pos)::8]
                    expected = reference[ch::len(positions)]
                    self.assertLess(max(abs(a - b) for a, b in zip(actual, expected)), 1e-5)
                for pos in set(POSITIONS) - set(positions):
                    self.assertTrue(all(v == 0 for v in output[POSITIONS.index(pos)::8]))

    def test_codec_and_layout_switches_without_demuxer_reset(self):
        ac3 = self.encoded_fixture("ac3", "stereo")
        dts = self.encoded_fixture("dca", "5.1(side)")
        ac3_51 = self.encoded_fixture("ac3", "5.1(side)")
        output, status = self.run_carrier(words_to_carrier(ac3 + dts + ac3_51), 13)
        self.assertEqual(status["codec"], "ac3")
        self.assertEqual(status["positions"], ["FL", "FR", "FC", "LFE", "SL", "SR"])
        self.assertEqual(status["errors"], 0)
        segments = [(0, ac3, "stereo"), (len(ac3) // 4, dts, "5.1(side)"),
                    ((len(ac3) + len(dts)) // 4, ac3_51, "5.1(side)")]
        for offset, carrier, layout in segments:
            reference = self.reference(carrier)
            count = 2 if layout == "stereo" else 6
            for ch in range(count):
                actual = output[offset * 8 + ch:(offset + len(carrier) // 4) * 8:8]
                expected = reference[ch::count]
                self.assertGreater(max(abs(v) for v in actual), 0.001)
                self.assertLess(max(abs(a - b) for a, b in zip(actual, expected)), 1e-5)

    def test_same_codec_layout_changes_reset_channel_history(self):
        stereo = self.encoded_fixture("ac3", "stereo")
        surround = self.encoded_fixture("ac3", "5.1(side)")
        output, status = self.run_carrier(words_to_carrier(stereo + surround + stereo), 31)
        self.assertEqual(status["positions"], ["FL", "FR"])
        for offset, data, channels in ((0, stereo, 2), (len(stereo) // 4, surround, 6),
                ((len(stereo) + len(surround)) // 4, stereo, 2)):
            reference = self.reference(data)
            count = len(data) // 4
            for channel in range(channels):
                actual = output[offset * 8 + channel:(offset + count) * 8:8]
                expected = reference[channel::channels]
                self.assertLess(max(abs(a - b) for a, b in zip(actual, expected)), 1e-5)
            for channel in range(channels, 8):
                self.assertTrue(all(v == 0 for v in output[offset * 8 + channel:(offset + count) * 8:8]))

    def test_iec_error_flag_mutes_otherwise_valid_ac3(self):
        carrier = bytearray(self.encoded_fixture("ac3", "stereo"))
        for offset in range(0, len(carrier), 6144):
            carrier[offset + 4] |= 0x80
        output, status = self.run_carrier(words_to_carrier(carrier))
        self.assertTrue(all(v == 0 for v in output))
        self.assertGreater(status["errors"], 0)

    def test_unsupported_and_pause_payload_cannot_be_reinterpreted_as_nested_iec(self):
        payload = self.encoded_fixture("ac3", "stereo")[:6144]
        for kind in (0, 3, 4):
            with self.subTest(kind=kind):
                output, _ = self.run_carrier(burst(kind, payload, 4096))
                self.assertTrue(all(v == 0 for v in output))

    def test_truncated_tail_preserves_prior_decoded_frames_and_metadata(self):
        carrier = self.encoded_fixture("ac3", "5.1(side)")
        good, _ = self.run_carrier(words_to_carrier(carrier))
        output, status = self.run_carrier(words_to_carrier(carrier + carrier[:400]))
        self.assertTrue(output[:len(good)] == good, "truncated tail changed prior valid decoded audio")
        self.assertTrue(all(v == 0 for v in output[len(good):]))
        self.assertEqual(status["codec"], "ac3")
        self.assertEqual(status["positions"], ["FL", "FR", "FC", "LFE", "SL", "SR"])
        self.assertEqual(status["state"], "muted")
        self.assertEqual(status["mute_reason"], "corrupt")

    def test_right_port_header_and_swapped_words_keep_projection(self):
        carrier = self.encoded_fixture("ac3", "stereo")
        expected, _ = self.run_carrier(words_to_carrier(carrier))
        shifted, status = self.run_carrier(words_to_carrier(b"\x00\x00" + carrier + b"\x00\x00"), 1)
        self.assertTrue(shifted[:len(expected)] == expected)
        self.assertEqual(status["errors"], 0)
        swapped = b"".join(carrier[i:i + 2][::-1] for i in range(0, len(carrier), 2))
        output, status = self.run_carrier(words_to_carrier(swapped), 1)
        self.assertTrue(output == expected)
        self.assertEqual(status["errors"], 0)


@unittest.skipIf(os.environ.get("SURROUND_NATIVE_ONLY") == "1", "native-only test host")
class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = ROOT / "audio/digital_input.py"
        self.assertTrue(path.is_file(), "DigitalInput lifecycle adapter is missing")
        spec = importlib.util.spec_from_file_location("surround_adapter_test", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    async def test_missing_binary_fails_without_owning_graph(self):
        adapter = self.module.DigitalInput(binary="/nonexistent/surround-helper")
        with self.assertRaisesRegex(RuntimeError, "binary"):
            await adapter.start("capture_node", "FL", "FR")
        self.assertFalse(adapter.snapshot()["running"])

    async def test_wrong_rate_rejected_before_starting_process(self):
        adapter = self.module.DigitalInput()
        with self.assertRaisesRegex(ValueError, "48000"):
            await adapter.start("capture_node", "FL", "FR", rate=44100)
        self.assertFalse(adapter.snapshot()["running"])

    async def test_exit_stderr_is_bounded_and_process_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "helper"
            binary.write_text("#!/usr/bin/env python3\nimport sys\nsys.stderr.write('x'*100000)\nsys.exit(7)\n")
            binary.chmod(0o755)
            adapter = self.module.DigitalInput(binary=binary)
            with mock.patch.object(self.module.pw_link, "run_pw_link_command",
                                   return_value="capture_node:capture_FL\ncapture_node:capture_FR"):
                with self.assertRaises(RuntimeError) as error:
                    await adapter.start("capture_node", "FL", "FR")
            self.assertIn("x", str(error.exception))
            self.assertFalse(adapter.snapshot()["running"])
            self.assertEqual(len(adapter.snapshot()["stderr"]), 16384)
            await adapter.stop()

    async def test_cancelled_start_reaps_real_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "helper"
            pidfile = Path(directory) / "pid"
            binary.write_text("#!/usr/bin/env python3\nimport os,time\n"
                              f"open({str(pidfile)!r},'w').write(str(os.getpid()))\n"
                              "time.sleep(60)\n")
            binary.chmod(0o755)
            adapter = self.module.DigitalInput(binary=binary)
            with mock.patch.object(self.module.pw_link, "run_pw_link_command",
                                   return_value="capture_node:capture_FL\ncapture_node:capture_FR"):
                task = asyncio.create_task(adapter.start("capture_node", "FL", "FR"))
                while not pidfile.exists():
                    await asyncio.sleep(0.01)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)
            self.assertFalse(adapter.snapshot()["running"])

    async def test_verified_links_recover_only_selected_capture_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "helper"
            binary.write_text("#!/usr/bin/env python3\nimport sys,time,pathlib\n"
                              "pathlib.Path(sys.argv[2]).write_text('{\"codec\":\"pcm\",\"positions\":[\"FL\",\"FR\"],\"decoded_bursts\":9}')\n"
                              "time.sleep(60)\n")
            binary.chmod(0o755)
            adapter = self.module.DigitalInput(binary=binary)
            edges = set()
            source_ports = "capture_node:capture_AUX2\ncapture_node:capture_AUX3"
            other_ports = "capture_node:capture_AUX4\ncapture_node:capture_AUX5"
            expected = {("capture_node:capture_AUX2", "fxroute_surround_input:carrier_L"),
                        ("capture_node:capture_AUX3", "fxroute_surround_input:carrier_R")}
            changed = {("capture_node:capture_AUX4", "fxroute_surround_input:carrier_L"),
                       ("capture_node:capture_AUX5", "fxroute_surround_input:carrier_R")}

            async def command(*args):
                running = adapter.process is not None and adapter.process.returncode is None
                if args == ("-o",):
                    if not running:
                        edges.clear()
                    return source_ports + "\n" + other_ports + ("\n" + "\n".join(adapter.output_ports) if running else "")
                if args == ("-i",):
                    return "\n".join(adapter.carrier_ports) if running else ""
                if args == ("-l",):
                    return "\n".join(f"{source}\n  |-> {target}" for source, target in edges)
                if args not in expected | changed:
                    raise RuntimeError("attempted an unrelated or DSP link")
                edges.add(args)
                return ""

            with mock.patch.object(self.module.pw_link, "run_pw_link_command", side_effect=command):
                try:
                    await adapter.start("capture_node", "AUX2", "AUX3")
                    initial_pid = adapter.process.pid
                    await adapter.start("capture_node", "AUX2", "AUX3", rate=48000)
                    self.assertEqual(adapter.process.pid, initial_pid, "same selection restarted the decoder")
                    self.assertEqual(edges, expected)
                    self.assertTrue(await adapter.links_present())
                    edges.remove(("capture_node:capture_AUX2", "fxroute_surround_input:carrier_L"))
                    self.assertFalse(await adapter.links_present())
                    async with asyncio.timeout(2):
                        while edges != expected:
                            await asyncio.sleep(0.05)
                    self.assertTrue(await adapter.links_present())
                    self.assertEqual(adapter.snapshot()["codec"], "pcm")
                    self.assertEqual(adapter.snapshot()["decoded_bursts"], 9)
                    edges.clear()
                    await adapter.start("capture_node", "AUX2", "AUX3")
                    self.assertEqual(adapter.process.pid, initial_pid)
                    self.assertEqual(edges, expected)
                    self.assertEqual(adapter.snapshot()["source_ports"], source_ports.splitlines())
                    second = self.module.DigitalInput(binary=binary)
                    with self.assertRaisesRegex(RuntimeError, "owner"):
                        await second.start("capture_node", "AUX2", "AUX3")
                    await adapter.start("capture_node", "AUX4", "AUX5")
                    self.assertNotEqual(adapter.process.pid, initial_pid)
                    self.assertEqual(edges, changed)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(initial_pid, 0)
                    pid = adapter.process.pid
                finally:
                    await adapter.stop()
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertFalse(adapter.snapshot()["running"])

    async def test_split_mono_nodes_use_distinct_ports_and_restart_on_right_node_change(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "helper"
            binary.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n")
            binary.chmod(0o755)
            adapter = self.module.DigitalInput(binary=binary)
            edges = set()
            captures = {"left:capture_MONO", "right:capture_MONO", "other:capture_MONO"}

            async def command(*args):
                running = adapter.process is not None and adapter.process.returncode is None
                if args == ("-o",):
                    if not running:
                        edges.clear()
                    return "\n".join(captures | (set(adapter.output_ports) if running else set()))
                if args == ("-i",):
                    return "\n".join(adapter.carrier_ports) if running else ""
                if args == ("-l",):
                    return "\n".join(f"{a} -> {b}" for a, b in edges)
                self.assertIn(args[0], captures)
                self.assertIn(args[1], adapter.carrier_ports)
                edges.add(args)
                return ""

            with mock.patch.object(self.module.pw_link, "run_pw_link_command", side_effect=command):
                try:
                    await adapter.start("left", "MONO", "MONO", right_source="right")
                    status_dir = adapter._status_dir
                    self.assertIsNotNone(status_dir)
                    self.assertTrue(status_dir.is_dir())
                    self.assertNotIn("/tmp/opencode", status_dir.parts)
                    pid = adapter.process.pid
                    self.assertEqual(edges, {("left:capture_MONO", "fxroute_surround_input:carrier_L"),
                                             ("right:capture_MONO", "fxroute_surround_input:carrier_R")})
                    await adapter.start("left", "MONO", "MONO", right_source="right")
                    self.assertEqual(adapter.process.pid, pid)
                    await adapter.start("left", "MONO", "MONO", right_source="other")
                    self.assertFalse(status_dir.exists())
                    status_dir = adapter._status_dir
                    self.assertIsNotNone(status_dir)
                    self.assertTrue(status_dir.is_dir())
                    self.assertNotEqual(adapter.process.pid, pid)
                    self.assertEqual(edges, {("left:capture_MONO", "fxroute_surround_input:carrier_L"),
                                             ("other:capture_MONO", "fxroute_surround_input:carrier_R")})
                finally:
                    await adapter.stop()
            self.assertFalse(status_dir.exists())
            self.assertIsNone(adapter._status_dir)
            self.assertIsNone(adapter._status_path)


if __name__ == "__main__":
    unittest.main()
