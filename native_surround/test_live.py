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
    def exercise(self, codec, layout, rate=48000):
        with tempfile.TemporaryDirectory(prefix="surround-live-", dir="/tmp/opencode") as temporary:
            base = Path(temporary)
            env = {**os.environ, "PIPEWIRE_RUNTIME_DIR": str(base), "XDG_RUNTIME_DIR": str(base),
                   "PIPEWIRE_REMOTE": "pipewire-0", "PIPEWIRE_DEBUG": "1"}
            (base / "pipewire.conf").write_text(CONFIG.replace("48000", str(rate)))
            if codec == "pcm":
                source = struct.pack("<ff", 0.125, -0.25) * (48000 * 5)
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
                spawn("record", ["pw-cat", "--record", "--raw", "--target", "0", "--rate", "48000",
                    "--channels", "8", "--channel-map", ",".join(f"AUX{i}" for i in range(8)),
                    "--format", "f32", "--latency", "256", "--properties",
                    f"{properties} node.name=surround_test_record", str(base / "record.f32")])
                spawn("source", ["pw-cat", "--playback", "--raw", "--target", "0", "--rate", "48000",
                    "--channels", "2", "--channel-map", "AUX0,AUX1", "--format", "f32", "--latency", "256",
                    "--properties", f"{properties} node.name=surround_test_source", str(base / "source.f32")])
                expected_outputs = {"surround_test_source:output_AUX0", "surround_test_source:output_AUX1"}
                expected_outputs |= {f"fxroute_surround_input:output_{position}" for position in BUS}
                expected_inputs = {"fxroute_surround_input:carrier_L", "fxroute_surround_input:carrier_R"}
                expected_inputs |= {f"surround_test_record:input_AUX{i}" for i in range(8)}
                wait_for(lambda: expected_outputs <= set(command("-o").splitlines()) and
                         expected_inputs <= set(command("-i").splitlines()), "ten helper ports")
                for channel, position in enumerate(BUS):
                    command(f"fxroute_surround_input:output_{position}", f"surround_test_record:input_AUX{channel}")
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
                self.assertGreater(len(recorded), 48000 * 8)
                self.assertTrue(all(math.isfinite(v) for v in recorded))
                if rate != 48000:
                    self.assertTrue(all(v == 0 for v in recorded))
                    self.assertGreater(status["errors"], 0)
                    self.assertEqual(status["state"], "muted")
                    return
                for channel in range(channels):
                    peak = max(abs(v) for v in recorded[channel::8])
                    self.assertGreater(peak, 0.005)
                    self.assertLess(peak, 0.5)
                for channel in range(channels, 8):
                    self.assertTrue(all(v == 0 for v in recorded[channel::8]))
                if codec == "pcm":
                    self.assertGreater(sum(v == 0.125 for v in recorded[0::8]), 10000)
                    self.assertGreater(sum(v == -0.25 for v in recorded[1::8]), 10000)
                else:
                    self.assertGreater(status["decoded_bursts"], 20)
                print(f"private PipeWire: {codec}, {len(recorded) // 8} recorded frames, status={status}")
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

    def test_wrong_private_graph_rate_fails_closed(self):
        self.exercise("pcm", "stereo", rate=44100)


if __name__ == "__main__":
    unittest.main()
