#!/usr/bin/env python3
"""Exercise the worker queues on a hardware-free, private PipeWire server."""
import array
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent
BUS = ["FL", "FR", "FC", "LFE", "SL", "SR", "BL", "BR"]
CONFIG = """
context.properties = {
    core.daemon = true
    core.name = pipewire-0
    support.dbus = false
    default.clock.rate = 48000
    default.clock.allowed-rates = [ 48000 ]
    default.clock.quantum = 256
    default.clock.min-quantum = 256
    default.clock.max-quantum = 256
}
context.spa-libs = { support.* = support/libspa-support audio.convert.* = audioconvert/libspa-audioconvert }
context.modules = [
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-access args = { access.socket = { pipewire-0 = "unrestricted" } } }
    { name = libpipewire-module-spa-node-factory }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-link-factory }
    { name = libpipewire-module-adapter }
    { name = libpipewire-module-metadata }
]
context.objects = [
    { factory = spa-node-factory args = {
        factory.name = support.node.driver
        node.name = Test-Driver
        node.group = pipewire.test
        priority.driver = 20000
        node.always-process = true
    } }
]
"""


class PrivatePipeWireTests(unittest.TestCase):
    def exercise(self, codec, layout, rate=48000, stereo=False):
        with tempfile.TemporaryDirectory(prefix="surround-live-", dir="/tmp/opencode") as temporary:
            base = Path(temporary)
            env = {**os.environ, "PIPEWIRE_RUNTIME_DIR": str(base), "XDG_RUNTIME_DIR": str(base),
                   "PIPEWIRE_REMOTE": "pipewire-0", "PIPEWIRE_DEBUG": "1"}
            (base / "pipewire.conf").write_text(CONFIG.replace("48000", str(rate)))
            if codec == "pcm":
                source = b"".join(struct.pack("<ff", 0.25, -0.125) if frame % 257 == 0
                                  else struct.pack("<ff", 0.125, -0.25) for frame in range(48000 * 5))
                channels = 2
            else:
                channels = 2 if layout == "stereo" else 6
                frequencies = [180, 270, 360, 55, 450, 540]
                expressions = "|".join(f"0.04*sin(2*PI*{frequency}*t)" for frequency in frequencies[:channels])
                carrier = subprocess.check_output(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    f"aevalsrc={expressions}:s=48000:d=5:c={layout}", "-c:a", codec,
                    "-strict", "-2", "-b:a", "640k" if codec == "ac3" else "1411200", "-f", "spdif", "pipe:1"])
                source = array.array("f", (word / 32768 for (word,) in struct.iter_unpack("<h", carrier))).tobytes()
            (base / "source.f32").write_bytes(source)
            children, logs = [], []

            def spawn(name, args):
                log = (base / f"{name}.log").open("wb")
                logs.append(log)
                process = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=log)
                children.append(process)
                return process

            def command(*args):
                try:
                    return subprocess.check_output(["pw-link", *args], env=env, stderr=subprocess.STDOUT,
                                                   timeout=3, text=True)
                except subprocess.TimeoutExpired:
                    self.fail("private registry timed out; " + " ".join(path.read_text() for path in base.glob("*.log")))

            def wait_for(predicate, description):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    for child in children:
                        if child.poll() is not None:
                            self.fail(f"{description}: child exited {child.returncode}; "
                                      + " ".join(path.read_text() for path in base.glob("*.log")))
                    try:
                        if predicate():
                            return
                    except subprocess.CalledProcessError:
                        pass
                    time.sleep(0.02)
                self.fail(f"timed out: {description}; outputs={command('-o')}; inputs={command('-i')}; "
                          + " ".join(path.read_text() for path in base.glob("*.log")))

            try:
                spawn("server", ["pipewire", "-c", str(base / "pipewire.conf")])
                wait_for(lambda: (base / "pipewire-0").exists(), "private server socket")
                spawn("helper", [str(ROOT / "build/fxroute-surround-input"), "--status", str(base / "status.json")])
                properties = ("node.autoconnect=false node.dont-reconnect=true "
                              "adapter.auto-port-config={mode=dsp monitor=false control=false position=preserve}")
                output_channels = 2 if stereo else 8
                # Record the raw pair in the same callback timeline as the decoded outputs.
                recorded_channels = output_channels + 2
                spawn("record", ["pw-cat", "--record", "--raw", "--target", "0", "--rate", "48000",
                    "--channels", str(recorded_channels), "--channel-map", ",".join(f"AUX{i}" for i in range(recorded_channels)),
                    "--format", "f32", "--latency", "256", "--properties",
                    f"{properties} node.name=surround_test_record", str(base / "record.f32")])
                spawn("source", ["pw-cat", "--playback", "--raw", "--target", "0", "--rate", "48000",
                    "--channels", "2", "--channel-map", "AUX0,AUX1", "--format", "f32", "--latency", "256",
                    "--properties", f"{properties} node.name=surround_test_source", str(base / "source.f32")])
                expected_outputs = {"surround_test_source:output_AUX0", "surround_test_source:output_AUX1"}
                expected_outputs |= {f"fxroute_surround_input:output_{position}" for position in BUS}
                expected_outputs |= {f"fxroute_surround_input:output_stereo_{position}" for position in BUS[:2]}
                expected_inputs = {"fxroute_surround_input:carrier_L", "fxroute_surround_input:carrier_R"}
                expected_inputs |= {f"surround_test_record:input_AUX{i}" for i in range(recorded_channels)}
                wait_for(lambda: expected_outputs <= set(command("-o").splitlines()) and
                         expected_inputs <= set(command("-i").splitlines()), "helper ports")
                positions = [f"stereo_{position}" for position in BUS[:2]] if stereo else BUS
                for channel, position in enumerate(positions):
                    command(f"fxroute_surround_input:output_{position}", f"surround_test_record:input_AUX{channel}")
                for channel in range(2):
                    command(f"surround_test_source:output_AUX{channel}",
                            f"surround_test_record:input_AUX{output_channels + channel}")
                command("surround_test_source:output_AUX0", "fxroute_surround_input:carrier_L")
                command("surround_test_source:output_AUX1", "fxroute_surround_input:carrier_R")
                time.sleep(1.2)
                status = json.loads((base / "status.json").read_text())
                if rate == 48000:
                    self.assertEqual(status["codec"], "dts" if codec == "dca" else codec)
                    self.assertEqual(status["positions"], BUS[:channels])
                for child in reversed(children):
                    child.terminate()
                    child.wait(timeout=2)
                recorded = array.array("f")
                recorded.frombytes((base / "record.f32").read_bytes())
                self.assertGreater(len(recorded), 48000 * recorded_channels)
                self.assertTrue(all(math.isfinite(v) for v in recorded))
                if rate != 48000:
                    for channel in range(output_channels):
                        self.assertTrue(all(v == 0 for v in recorded[channel::recorded_channels]))
                    self.assertGreater(status["errors"], 0)
                    self.assertEqual(status["state"], "muted")
                    return
                for channel in range(2 if stereo else channels):
                    peak = max(abs(v) for v in recorded[channel::recorded_channels])
                    self.assertGreater(peak, 0.005)
                    self.assertLess(peak, 0.5)
                for channel in range(channels, output_channels):
                    self.assertTrue(all(v == 0 for v in recorded[channel::recorded_channels]))
                if codec == "pcm":
                    self.assertGreater(sum(v == 0.125 for v in recorded[0::recorded_channels]), 10000)
                    self.assertGreater(sum(v == -0.25 for v in recorded[1::recorded_channels]), 10000)
                else:
                    self.assertGreater(status["decoded_bursts"], 20)
                raw = array.array("i", (round(recorded[frame * recorded_channels + output_channels + side] * 2147483648)
                                        for frame in range(len(recorded) // recorded_channels) for side in range(2)))
                (base / "input.s32").write_bytes(raw.tobytes())
                subprocess.run([str(ROOT / "build/fxroute-surround-input"), "--offline", str(base / "input.s32"),
                                str(base / "expected.f32")] + (["--stereo"] if stereo else []), check=True)
                expected = array.array("f")
                expected.frombytes((base / "expected.f32").read_bytes())
                # Leave startup and the final incomplete burst outside the comparison.
                # This graph's raw observer bypasses one 256-frame filter scheduling hop.
                live_offset = 8192 + 256
                start, stop = 24000, len(recorded) // recorded_channels - live_offset - 2048
                self.assertGreater(stop - start, 10000)
                compared, late = 0, 0
                for frame in range(start, stop):
                    actual = recorded[(frame + live_offset) * recorded_channels:
                                      (frame + live_offset) * recorded_channels + output_channels]
                    # Late worker frames are intentionally silent, never replayed off-timeline.
                    if not any(actual):
                        late += 1
                        continue
                    reference = expected[frame * output_channels:(frame + 1) * output_channels]
                    # Independent decoder starts can differ by low-level AC-3 dither/history.
                    self.assertLess(max(abs(a - b) for a, b in zip(actual, reference)), 1e-5,
                                    f"frame {frame}: live offset differs from {live_offset} frames")
                    compared += 1
                self.assertGreater(compared, 10000, "insufficient live samples for timing verification")
                print(f"private PipeWire: {codec}, stereo={stereo}, offset={live_offset}, "
                      f"verified={compared}, late/silent={late}, status={status}")
            finally:
                for child in reversed(children):
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
                for log in logs:
                    log.close()

    def test_pcm_worker_queue(self):
        self.exercise("pcm", "stereo")

    def test_ac3_worker_queue(self):
        self.exercise("ac3", "stereo")

    def test_dts_worker_queue(self):
        self.exercise("dca", "5.1(side)")

    def test_stereo_pcm_worker_queue(self):
        self.exercise("pcm", "stereo", stereo=True)

    def test_stereo_ac3_worker_queue(self):
        self.exercise("ac3", "5.1(side)", stereo=True)

    def test_stereo_dts_worker_queue(self):
        self.exercise("dca", "5.1(side)", stereo=True)

    def test_wrong_private_graph_rate_fails_closed(self):
        self.exercise("pcm", "stereo", rate=44100)


if __name__ == "__main__":
    unittest.main()
