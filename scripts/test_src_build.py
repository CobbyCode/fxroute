#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Publishing a rebuilt SRC runtime must retain live mapped/executable artifacts."""
import importlib.util
import json
import mmap
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("src_build", ROOT / "native_src/build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class RuntimePublishTests(unittest.TestCase):
    def test_overlay_supports_the_pinned_control_buffer_contract(self):
        source=ROOT / "native_src/build/sources/pipewire/pipewire-1.6.8/spa/plugins/audioconvert/audioconvert.c"
        if not source.is_file():
            self.skipTest("Build the pinned SPA source first")
        self.assertIn("uint8_t buffer[8192];",build.overlay(source.read_text()))

    def test_activation_keeps_old_mapped_library_and_executing_engine_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "build"
            output.mkdir()
            old = output / "runtime-old"
            old.mkdir()
            source = root / "sleep.c"
            source.write_text('#include <unistd.h>\nint main(void){sleep(30);return 0;}\n')
            subprocess.run(["cc", str(source), "-o", str(old / "sox")], check=True)
            (old / "libspa-fxroute-src.so").write_bytes(b"old library" * 1024)
            (old / "manifest.json").write_text('{"generation":"old"}')
            (output / "current").symlink_to(old.name)
            for name in ("sox", "libspa-fxroute-src.so", "manifest.json"):
                (output / name).symlink_to("current/" + name)
            process = subprocess.Popen([str(output / "sox")])
            try:
                with (output / "libspa-fxroute-src.so").open("rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
                    generation = output / "runtime-new"
                    generation.mkdir()
                    (generation / "sox").write_bytes(b"new executable")
                    (generation / "libspa-fxroute-src.so").write_bytes(b"new library")
                    (generation / "manifest.json").write_text('{"generation":"new"}')
                    build.activate_runtime(generation, output)
                    self.assertEqual(mapping[:], b"old library" * 1024)
                    self.assertEqual((old / "sox").read_bytes(), Path(f"/proc/{process.pid}/exe").read_bytes())
                    self.assertIsNone(process.poll())
                    self.assertEqual((output / "libspa-fxroute-src.so").read_bytes(), b"new library")
                    self.assertEqual(json.loads((output / "manifest.json").read_text()), {"generation":"new"})
            finally:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
