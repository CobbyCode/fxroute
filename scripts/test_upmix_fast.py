#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Build and run the Fast upmixer kernel test (libm only, no native DSP dependencies)."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

NATIVE = Path(__file__).resolve().parents[1] / "native_dsp"
COMPILER = shutil.which("cc") or shutil.which("gcc")


@unittest.skipUnless(COMPILER, "no C compiler available")
class UpmixFastKernelTests(unittest.TestCase):
    def test_kernel_suite_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "test-upmix-fast"
            subprocess.run([COMPILER, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-pedantic",
                            str(NATIVE / "upmix_fast.c"), str(NATIVE / "test_upmix_fast.c"),
                            "-I", str(NATIVE), "-lm", "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True, timeout=120)


if __name__ == "__main__":
    unittest.main()
