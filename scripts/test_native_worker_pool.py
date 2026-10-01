#!/usr/bin/env python3
"""Run dependency-free native worker lifecycle and timing regressions."""
import os
from pathlib import Path
import subprocess
import sys

from native_test_runner import run_pytest_style_module

NATIVE = Path(__file__).resolve().parents[1] / "native_dsp"


def test_pool_lifecycle_fixed_ownership_fp_mode_and_rt_failure(tmp_path):
    # Catches duplicate/missing jobs, publication races, unstable ownership,
    # leaked helpers, wrong FP mode, and accepting a non-RT live helper.
    target = tmp_path / "workers"
    subprocess.run([
        os.environ.get("CC", "cc"), "-std=c11", "-O2", "-Wall", "-Wextra",
        "-Werror", "-pedantic", "-I", str(NATIVE),
        str(NATIVE / "worker_pool.c"), str(NATIVE / "test_worker_pool.c"), str(NATIVE / "timing.c"),
        "-pthread", "-lm", "-o", str(target),
    ], check=True)
    subprocess.run([str(target)], check=True, timeout=30)


if __name__ == "__main__":
    sys.exit(run_pytest_style_module(globals()))
