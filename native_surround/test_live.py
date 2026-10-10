#!/usr/bin/env python3
"""Exercise the worker queues on a hardware-free, private PipeWire server."""
import array
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import shutil
import tempfile
import time
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parent
BUS = ["FL", "FR", "FC", "LFE", "SL", "SR", "BL", "BR"]
CONFIG = """
context.properties = {
    core.daemon = true
    core.name = pipewire-0
    support.dbus = false
    default.clock.rate = 48000
    default.clock.allowed-rates = [ 48000 ]
    default.clock.quantum = 768
    default.clock.min-quantum = 768
    default.clock.max-quantum = 768
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
    def exercise(self, codec, layout, rate=48000, stereo=False, switches=False, load=False, quantum=768):
        production = os.environ.get("SURROUND_PRODUCTION") == "1"
        if production and (not switches or quantum != 768):
            self.fail("production verification only supports the rapid-switch tests at quantum 768")
        seconds = float(os.environ.get("SURROUND_LIVE_SECONDS", "8")) if switches else 1.8
        with tempfile.TemporaryDirectory(prefix="surround-live-", dir="/tmp/opencode") as temporary:
            base = Path(temporary)
            env = {**os.environ, "PIPEWIRE_RUNTIME_DIR": str(base), "XDG_RUNTIME_DIR": str(base),
                   "PIPEWIRE_REMOTE": "pipewire-0", "PIPEWIRE_DEBUG": "1"}
            if production:
                env = {**os.environ, "PIPEWIRE_DEBUG": "1"}
            (base / "pipewire.conf").write_text(CONFIG.replace("48000", str(rate)).replace("256", str(quantum)))
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
            if switches:
                segments = []
                for fmt, channel_layout, count in (("ac3", "stereo", 3), ("dca", "5.1(side)", 6),
                                                    ("ac3", "5.1(side)", 3)):
                    encoded = subprocess.check_output(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        f"aevalsrc=0.04*sin(2*PI*180*t):s=48000:d=0.2:c={channel_layout}", "-c:a", fmt,
                        "-strict", "-2", "-b:a", "640k" if fmt == "ac3" else "1411200", "-f", "spdif", "pipe:1"])
                    length = (1536 if fmt == "ac3" else 512) * count * 4
                    segments.append(array.array("f", (word / 32768 for (word,) in
                                    struct.iter_unpack("<h", encoded[:length]))).tobytes())
                # Include the safe sync-timeout gap in each cycle. Silence (not PCM)
                # separates the codec segments: broad prefix safety mutes pending
                # PCM tails by design, and a non-RT observer cannot place that
                # boundary sample-precisely. PCM acquisition itself is covered by
                # the captured switch fixtures and the RT product verification.
                cycle = struct.pack("<ff", 0, 0) * 8000 + b"".join(segments)
                source = cycle * max(30, int(seconds * 48000 * 8 / len(cycle)) + 3)
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

            def read_status():
                if production:
                    with urllib.request.urlopen("http://localhost:8000/api/audio/surround", timeout=3) as response:
                        return json.load(response)["input"]
                return json.loads((base / "status.json").read_text())

            try:
                if not production:
                    spawn("server", ["pipewire", "-c", str(base / "pipewire.conf")])
                    wait_for(lambda: (base / "pipewire-0").exists(), "private server socket")
                    spawn("helper", [str(ROOT / "build/fxroute-surround-input"), "--status", str(base / "status.json")])
                properties = ("node.autoconnect=false node.dont-reconnect=true "
                              "adapter.auto-port-config={mode=dsp monitor=false control=false position=preserve}")
                output_channels = 10 if switches else 2 if stereo else 8
                # Record the raw pair in the same callback timeline as the decoded outputs.
                recorded_channels = output_channels + (4 if production else 2)
                spawn("record", ["pw-cat", "--record", "--raw", "--target", "0", "--rate", "48000",
                    "--channels", str(recorded_channels), "--channel-map", ",".join(f"AUX{i}" for i in range(recorded_channels)),
                    "--format", "f32", "--latency", str(quantum), "--properties",
                    f"{properties} node.name=surround_test_record", str(base / "record.f32")])
                spawn("source", ["pw-cat", "--playback", "--raw", "--target", "0", "--rate", "48000",
                    "--channels", "2", "--channel-map", "AUX0,AUX1", "--format", "f32", "--latency", str(quantum),
                    "--properties", f"{properties} node.name=surround_test_source", str(base / "source.f32")])
                expected_outputs = {"surround_test_source:output_AUX0", "surround_test_source:output_AUX1"}
                expected_outputs |= {f"fxroute_surround_input:output_{position}" for position in BUS}
                expected_outputs |= {f"fxroute_surround_input:output_stereo_{position}" for position in BUS[:2]}
                expected_inputs = {"fxroute_surround_input:carrier_L", "fxroute_surround_input:carrier_R"}
                expected_inputs |= {f"surround_test_record:input_AUX{i}" for i in range(recorded_channels)}
                wait_for(lambda: expected_outputs <= set(command("-o").splitlines()) and
                         expected_inputs <= set(command("-i").splitlines()), "helper ports")
                positions = BUS + ["stereo_FL", "stereo_FR"] if switches else (
                    [f"stereo_{position}" for position in BUS[:2]] if stereo else BUS)
                for channel, position in enumerate(positions):
                    command(f"fxroute_surround_input:output_{position}", f"surround_test_record:input_AUX{channel}")
                for channel in range(2):
                    command(f"surround_test_source:output_AUX{channel}",
                            f"surround_test_record:input_AUX{output_channels + channel}")
                if production:
                    source_ports = read_status()["source_ports"]
                    for channel, port in enumerate(source_ports):
                        command(port, f"surround_test_record:input_AUX{output_channels + 2 + channel}")
                    # Only inject alongside an exactly quiet TV carrier, never mix programmes.
                    time.sleep(0.3)
                    probe = array.array("f"); probe.frombytes((base / "record.f32").read_bytes())
                    self.assertGreater(len(probe), recorded_channels * 1024)
                    self.assertLess(max(abs(value) for c in range(2) for value in
                        probe[output_channels + 2 + c::recorded_channels]), 1e-7, "TV carrier is not quiet")
                baseline = read_status()
                command("surround_test_source:output_AUX0", "fxroute_surround_input:carrier_L")
                command("surround_test_source:output_AUX1", "fxroute_surround_input:carrier_R")
                if load:
                    for i in range(6):
                        spawn(f"load-{i}", ["python3", "-c", "while True: pass"])
                statuses = []
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    statuses.append(read_status())
                    time.sleep(0.05)
                status = read_status()
                (base / "metrics.json").write_text(json.dumps({"baseline": baseline, "final": status,
                                                              "samples": statuses}, indent=2))
                # The helper forces 768-frame callbacks while active; a larger
                # configured quantum never reaches the recognition decision.
                quantum = status.get("worker_quantum_frames") or quantum
                if rate == 48000 and quantum <= 768 and not switches:
                    self.assertEqual(status["codec"], "dts" if codec == "dca" else codec)
                    self.assertEqual(status["positions"], BUS[:channels])
                for child in reversed(children):
                    child.terminate()
                    child.wait(timeout=2)
                recorded = array.array("f")
                recorded.frombytes((base / "record.f32").read_bytes())
                self.assertGreater(len(recorded), 48000 * recorded_channels)
                self.assertTrue(all(math.isfinite(v) for v in recorded))
                if production:
                    self.assertLess(max(abs(value) for c in range(2) for value in
                        recorded[output_channels + 2 + c::recorded_channels]), 1e-7, "TV carrier changed during injection")
                if rate != 48000:
                    for channel in range(output_channels):
                        self.assertTrue(all(v == 0 for v in recorded[channel::recorded_channels]))
                    self.assertGreater(status["errors"], 0)
                    self.assertEqual(status["state"], "muted")
                    return
                if quantum > 768:
                    # The helper forces 768-frame callbacks while active, so an
                    # oversized configured quantum never reaches the recognition
                    # decision; the graph runs at the forced quantum instead.
                    self.assertEqual(status["worker_quantum_frames"], 768)
                    self.assertEqual(status["transport_faults"], baseline.get("transport_faults", 0))
                for channel in range(2 if switches or stereo else channels):
                    peak = max(abs(v) for v in recorded[channel::recorded_channels])
                    self.assertGreater(peak, 0.005)
                    self.assertLess(peak, 0.5)
                for channel in ([] if switches else range(channels, output_channels)):
                    self.assertTrue(all(v == 0 for v in recorded[channel::recorded_channels]))
                if codec == "pcm" and not switches:
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
                if switches:
                    subprocess.run([str(ROOT / "build/fxroute-surround-input"), "--offline", str(base / "input.s32"),
                                    str(base / "stereo.f32"), "--stereo"], check=True)
                    fold = array.array("f"); fold.frombytes((base / "stereo.f32").read_bytes())
                    expected = array.array("f", (value for frame in range(len(fold) // 2)
                        for value in (*expected[frame * 8:frame * 8 + 8], *fold[frame * 2:frame * 2 + 2])))
                # Leave startup and the final incomplete burst outside the comparison.
                # This graph's raw observer bypasses one 768-frame filter scheduling hop.
                live_offset = 2818 + quantum
                start, stop = 24000, len(recorded) // recorded_channels - live_offset - 2048
                self.assertGreater(stop - start, 10000)
                compared, silent = 0, 0
                observer_offsets = {}
                if switches:
                    # A non-RT recorder may observe the helper before or after the raw tap's
                    # callback. Select a whole-block offset, never excuse a helper underrun.
                    for block in range(start, stop - quantum, quantum):
                        choices = (2818, live_offset, live_offset + quantum)
                        errors = []
                        for offset in choices:
                            error = max(abs(recorded[(frame + offset) * recorded_channels + c]
                                            - expected[frame * output_channels + c])
                                for frame in range(block, block + quantum) for c in range(output_channels))
                            errors.append(error)
                        best = min(range(len(choices)), key=errors.__getitem__)
                        self.assertLess(errors[best], 1e-5, f"switch block {block}: {errors}")
                        observer_offsets[choices[best]] = observer_offsets.get(choices[best], 0) + 1
                        compared += quantum
                    for key in ("late_frames", "underrun_frames", "transport_faults", "errors"):
                        self.assertEqual(status[key], baseline[key], status)
                    formats = {(item["codec"], item["layout"]) for item in statuses}
                    self.assertTrue({("pcm", "stereo"), ("ac3", "stereo"), ("ac3", "5.1(side)"),
                                     ("dts", "5.1(side)")} <= formats, formats)
                    self.assertGreater(status["worker_reserve_min_us"], 0, status)
                    print(f"rapid switches: load={load}, quantum={quantum}, compared={compared}, "
                          f"observer_offsets={observer_offsets}, status={status}")
                    return
                for frame in range(start, stop):
                    actual = recorded[(frame + live_offset) * recorded_channels:
                                      (frame + live_offset) * recorded_channels + output_channels]
                    reference = expected[frame * output_channels:(frame + 1) * output_channels]
                    if not any(actual):
                        silent += 1
                    # Independent decoder starts can differ by low-level AC-3 dither/history.
                    self.assertLess(max(abs(a - b) for a, b in zip(actual, reference)), 1e-5,
                                    f"frame {frame}: live offset differs from {live_offset} frames")
                    compared += 1
                self.assertGreater(compared, 10000, "insufficient live samples for timing verification")
                self.assertEqual(status["late_frames"], 0, status)
                self.assertEqual(status["underrun_frames"], 0, status)
                print(f"private PipeWire: {codec}, stereo={stereo}, offset={live_offset}, "
                      f"verified={compared}, silent={silent}, status={status}")
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
                report = os.environ.get("SURROUND_LIVE_REPORT")
                if report:
                    shutil.copytree(base, Path(report) / self._testMethodName, dirs_exist_ok=False)

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

    def test_oversized_configured_quantum_is_forced_to_768(self):
        self.exercise("pcm", "stereo", quantum=2048)

    def test_rapid_switches_at_production_quantum(self):
        self.exercise("ac3", "stereo", switches=True, quantum=768)

    @unittest.skipUnless(os.environ.get("SURROUND_STRESS") == "1", "opt-in CPU stress")
    def test_loaded_rapid_switches_at_production_quantum(self):
        self.exercise("ac3", "stereo", switches=True, load=True, quantum=768)


if __name__ == "__main__":
    unittest.main()
