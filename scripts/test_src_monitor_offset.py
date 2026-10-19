#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Real SPA monitor bounds and complete device audio after idle/relink cycles."""
import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "native_src/build"
LIBRARY = BUILD / "libspa-fxroute-src.so"
SPA = BUILD / "sources/pipewire/pipewire-1.6.8/spa"
sys.path.insert(0, str(ROOT))


@unittest.skipUnless(LIBRARY.is_file() and SPA.is_dir(), "build the common SRC runtime first")
class MonitorOffsetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.path = Path(cls.directory.name)
        policy = cls.path / "fxroute/src-policy.json"
        policy.parent.mkdir()
        policy.write_text('{"mode":"high"}')
        cls.environment = {**os.environ, "XDG_CONFIG_HOME": str(cls.path)}
        cls.executable = cls.path / "monitor-offset"
        cls.flags = ["cc", "-std=gnu11", "-O1", "-g", "-I", str(SPA / "include"),
                     "-I", str(SPA / "plugins/audioconvert"), "-I", str(ROOT / "native_src")]
        subprocess.run([*cls.flags, str(ROOT / "scripts/src_monitor_offset.c"),
                        "-ldl", "-lm", "-o", str(cls.executable)], check=True)

    def run_scenario(self, library, channels, fmt, quanta, idle, relink, rate, direction="output", *,
                     exact=False, retained=False, executable=None):
        command = [str(executable or self.executable), str(library), str(channels), fmt,
                   *map(str, quanta), str(idle), relink, "3", rate]
        if exact:
            command.append("exact")
        if retained:
            command.append("retained")
        if direction == "input":
            command.append("split")
        result = subprocess.run(command, capture_output=True, text=True, timeout=10,
                                env={**self.environment, "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1"})
        self.assertEqual(result.returncode, 0, " ".join(command) + "\n" + result.stdout + result.stderr)
        line = next(line for line in result.stdout.splitlines() if line.startswith("MONITOR_OFFSET "))
        metrics = json.loads(line.split(" ", 1)[1])
        self.assertEqual(metrics["failures"], 0)
        self.assertEqual(metrics["played"], metrics["delivered"])
        self.assertEqual(metrics["mirrored"], metrics["delivered"] * 3 // 2 if retained else metrics["delivered"])
        self.assertEqual(metrics["short_cycles"], 0)
        self.assertEqual(metrics["first_output_delay"], 0)
        self.assertNotIn("AddressSanitizer", result.stderr)

    def test_complete_first_block_and_monitor_sequence_across_idle_relinks_and_rate_changes(self):
        # Removing the EMPTY-input return loses resume blocks; removing the
        # remaining-input bound permits reads beyond retained input chunks.
        quanta = ((256, 256), (1024, 2048), (2048, 1024), (2048, 2048), (2048, 8192))
        for case in itertools.product((2, 6, 8), ("f32", "s32"), quanta,
                                      (0, 1, 3, 4, 7, 9), ("renegotiate", "stream"), ("none", "rate"), ("input", "output")):
            with self.subTest(case=case):
                self.run_scenario(LIBRARY, *case)

    def test_retained_nonempty_input_monitor_exposes_only_the_unread_suffix(self):
        for channels, fmt, direction in itertools.product((2, 6, 8), ("f32", "s32"), ("input", "output")):
            with self.subTest(channels=channels, fmt=fmt, direction=direction):
                self.run_scenario(LIBRARY, channels, fmt, (1024, 1024), 0, "stream", "none", direction, retained=True)

    def test_raw_empty_buffer_preserves_its_entire_silent_duration(self):
        executable = self.path / "empty-duration"
        subprocess.run([*self.flags, str(ROOT / "scripts/src_empty_duration.c"),
                        "-ldl", "-o", str(executable)], check=True)
        result = subprocess.run([str(executable), str(LIBRARY)], env=self.environment,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("FIRST_NONZERO_CYCLE 4", result.stdout)

    def test_exact_input_allocations_under_address_sanitizer(self):
        probe = subprocess.run(["cc", "-fsanitize=address", "-x", "c", "-", "-o", str(self.path / "asan-probe")],
                               input="int main(void){return 0;}\n", text=True, capture_output=True)
        if probe.returncode:
            self.skipTest("Compiler AddressSanitizer runtime is unavailable: " + probe.stderr.strip())
        directory = self.path / "asan"
        directory.mkdir()
        flags = [*self.flags, "-fsanitize=address", "-fno-omit-frame-pointer", "-fPIC",
                 "-D_GNU_SOURCE", "-DRESAMPLE_DISABLE_PRECOMP"]
        replacements = {"audioconvert": BUILD / "audioconvert.c",
                        "volume-ops-c": SPA / "plugins/audioconvert/volume-ops-c.c",
                        "fmt-ops-c": SPA / "plugins/audioconvert/fmt-ops-c.c"}
        objects = [str(p) for p in BUILD.glob("*.o") if p.stem not in replacements]
        for name, source in replacements.items():
            target = directory / (name + ".o")
            subprocess.run([*flags, "-c", str(source), "-o", str(target)], check=True)
            objects.append(str(target))
        library = directory / LIBRARY.name
        shutil.copy2(BUILD / "sox", directory / "sox")
        subprocess.run(["cc", "-fsanitize=address", "-shared", "-Wl,-z,defs", *objects,
                        "-l:libsoxr.so.0", "-lpthread", "-ldl", "-lm", "-o", str(library)], check=True)
        executable = directory / "monitor-offset"
        subprocess.run([*flags, "-no-pie", str(ROOT / "scripts/src_monitor_offset.c"),
                        "-ldl", "-lm", "-o", str(executable)], check=True)
        for case in itertools.product((2, 6, 8), ("f32", "s32"),
                                      ((1024, 2048), (2048, 1024), (2048, 2048), (2048, 8192)),
                                      (1, 3, 9), ("renegotiate", "stream"), ("none", "rate"), ("input", "output")):
            with self.subTest(case=case):
                self.run_scenario(library, *case, exact=True, executable=executable)
        for channels, fmt, direction in itertools.product((2, 6, 8), ("f32", "s32"), ("input", "output")):
            with self.subTest(retained=True, channels=channels, fmt=fmt, direction=direction):
                self.run_scenario(library, channels, fmt, (1024, 1024), 0, "stream", "none", direction,
                                  exact=True, retained=True, executable=executable)

    @unittest.skipUnless(all(shutil.which(name) for name in ("pipewire", "pw-cli", "pw-dump")),
                         "PipeWire server and diagnostic clients are unavailable")
    def test_product_ingress_uses_fixed_factory_without_changing_other_virtual_sinks(self):
        from audio import src_policy
        config, runtime = self.path / "config", self.path / "run"
        runtime.mkdir(mode=0o700)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}):
            for path, content in src_policy.configuration(BUILD).items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        environment = {**self.environment, "XDG_CONFIG_HOME": str(config),
                       "PIPEWIRE_RUNTIME_DIR": str(runtime), "PIPEWIRE_REMOTE": "pipewire-0",
                       "PIPEWIRE_CORE": "pipewire-0", "SPA_PLUGIN_DIR": src_policy.plugin_directories(BUILD)}
        with (self.path / "pipewire.log").open("w+") as log:
            server = subprocess.Popen(["pipewire"], env=environment, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 10
                while not (runtime / "pipewire-0").exists() and server.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                log.seek(0)
                self.assertTrue((runtime / "pipewire-0").exists(), log.read())
                for name in ("fxroute_dsp_sink", "other_virtual_sink"):
                    subprocess.run(["pw-cli", "create-node", "adapter",
                                    "{ factory.name=support.null-audio-sink node.name=" + name +
                                    " media.class=Audio/Sink object.linger=true audio.position=[FL FR] }"],
                                   env=environment, capture_output=True, text=True, check=True, timeout=10)
                observed = json.loads(subprocess.check_output(["pw-dump"], env=environment, text=True, timeout=10))
                nodes = {n["info"]["props"].get("node.name"): n for n in observed
                         if n.get("type") == "PipeWire:Interface:Node"}
                ingress = nodes["fxroute_dsp_sink"]["info"]
                self.assertEqual(ingress["props"]["audio.adapt.converter"], src_policy.FACTORY)
                params = ingress["params"]["Props"][0]["params"]
                params = params if isinstance(params, dict) else dict(zip(params[::2], params[1::2]))
                self.assertEqual(params["fxroute.src.generation"], json.loads((BUILD / "manifest.json").read_text())["generation"])
                self.assertNotIn("audio.adapt.converter", nodes["other_virtual_sink"]["info"]["props"])
            finally:
                server.terminate()
                server.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
