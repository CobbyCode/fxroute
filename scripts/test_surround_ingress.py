#!/usr/bin/env python3
"""Fail-closed digital ingress and lifecycle regressions (stdlib only)."""
import array
import asyncio
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

    def run_carrier(self, data, chunk=257):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "input.s32").write_bytes(data)
            subprocess.run([str(self.binary), "--offline", str(base / "input.s32"),
                            str(base / "output.f32"), "--status", str(base / "status.json"),
                            "--chunk", str(chunk)], check=True, capture_output=True)
            output = array.array("f")
            output.frombytes((base / "output.f32").read_bytes())
            self.assertEqual(len(output), len(data) // 8 * 8)
            self.assertTrue(all(math.isfinite(v) for v in output))
            return output, json.loads((base / "status.json").read_text())

    def test_pcm_is_unity_front_pair_with_six_silent_positions(self):
        output, status = self.run_carrier(pcm(17001), 1)
        self.assertEqual(list(output[:16]), [0.125, -0.25, 0, 0, 0, 0, 0, 0] * 2)
        self.assertEqual(status["codec"], "pcm")
        self.assertEqual(status["positions"], ["FL", "FR"])

    def test_unsupported_corrupt_null_and_pause_never_escape_as_pcm(self):
        for kind in (0, 3, 4, 0x15, 1, 0x0B):
            with self.subTest(kind=kind):
                output, status = self.run_carrier(burst(kind, b"\xff\x7f" * 300) * 5, 3)
                self.assertTrue(all(v == 0 for v in output))
                if kind not in (0, 3):
                    self.assertGreater(status["errors"], 0)

    def test_unknown_latch_requires_safe_silence_before_pcm(self):
        output, _ = self.run_carrier(burst(4) + pcm(10000) + pcm(10000, 0, 0) + pcm(10000))
        self.assertTrue(all(v == 0 for v in output[:(1536 + 10000) * 8]))
        self.assertEqual(list(output[-8:]), [0.125, -0.25, 0, 0, 0, 0, 0, 0])

    def test_split_header_is_detected_before_pcm_release(self):
        # More than a maximum DTS burst of lookahead protects preamble transitions.
        output, _ = self.run_carrier(pcm(12000) + burst(4) + pcm(7000), 1)
        self.assertEqual(list(output[:8]), [0.125, -0.25, 0, 0, 0, 0, 0, 0])
        self.assertTrue(all(v == 0 for v in output[12000 * 8:]))

    def test_raw_dts_big_little_and_14bit_syncs_are_muted(self):
        for sync in ("7ffe8001", "fe7f0180", "1fffe800", "ff1f00e8"):
            with self.subTest(sync=sync):
                payload = words_to_carrier(bytes.fromhex(sync) + b"\x55\xaa" * 20000)
                output, status = self.run_carrier(payload, 1)
                self.assertTrue(all(v == 0 for v in output))
                self.assertGreater(status["errors"], 0)

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


if __name__ == "__main__":
    unittest.main()
