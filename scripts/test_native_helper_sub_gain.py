#!/usr/bin/env python3
"""Smoke-test native 2.1 helper sub gain without PipeWire graph routing."""

from __future__ import annotations

import math
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "pipewire_stage1" / "build" / "fxroute_21_passthrough"
RMS_RE = re.compile(r"output_3_rms=([0-9.]+)\s+output_4_rms=([0-9.]+)")


def run_case(db: float) -> tuple[float, float]:
    if not HELPER.exists():
        raise SystemExit(f"Missing helper binary: {HELPER}")
    result = subprocess.run(
        [
            str(HELPER),
            "--self-test-sub-gain",
            "--lowpass-hz",
            "0",
            "--highpass-hz",
            "0",
            "--main-delay-ms",
            "0",
            "--sub-delay-ms",
            "0",
            "--sub-polarity",
            "normal",
            "--sub-level-db",
            str(db),
            "--sub2-level-db",
            str(db),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    match = RMS_RE.search(result.stdout)
    if not match:
        raise AssertionError(f"Could not parse helper self-test output: {result.stdout!r}")
    return float(match.group(1)), float(match.group(2))


def assert_close(name: str, actual: float, expected: float, tolerance: float) -> None:
    if not math.isclose(actual, expected, rel_tol=tolerance, abs_tol=tolerance):
        raise AssertionError(f"{name}: expected {expected:.4f}, got {actual:.4f}")


def main() -> int:
    baseline_l, baseline_r = run_case(0.0)
    minus20_l, minus20_r = run_case(-20.0)
    plus6_l, plus6_r = run_case(6.0)

    minus20_ratio_l = minus20_l / baseline_l
    minus20_ratio_r = minus20_r / baseline_r
    plus6_ratio_l = plus6_l / baseline_l
    plus6_ratio_r = plus6_r / baseline_r

    assert_close("-20 dB output_3 ratio", minus20_ratio_l, 0.1, 0.02)
    assert_close("-20 dB output_4 ratio", minus20_ratio_r, 0.1, 0.02)
    assert_close("+6 dB output_3 ratio", plus6_ratio_l, math.pow(10.0, 6.0 / 20.0), 0.03)
    assert_close("+6 dB output_4 ratio", plus6_ratio_r, math.pow(10.0, 6.0 / 20.0), 0.03)

    print(f"0 dB RMS: output_3={baseline_l:.9f} output_4={baseline_r:.9f}")
    print(f"-20 dB RMS ratio: output_3={minus20_ratio_l:.4f} output_4={minus20_ratio_r:.4f}")
    print(f"+6 dB RMS ratio: output_3={plus6_ratio_l:.4f} output_4={plus6_ratio_r:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
