#!/usr/bin/env python3
"""Smoke-test native 2.1 helper branch delays without PipeWire graph routing."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "pipewire_stage1" / "build" / "fxroute_21_passthrough"
IMPULSE_RE = re.compile(
    r"main_delay_samples=(\d+)\s+sub_delay_samples=(\d+)\s+sub2_delay_samples=(\d+)\s+"
    r"output_1_impulse=(-?\d+)\s+output_2_impulse=(-?\d+)\s+"
    r"output_3_impulse=(-?\d+)\s+output_4_impulse=(-?\d+)"
)


def run_case(main_delay_ms: float, sub_delay_ms: float) -> dict[str, int]:
    if not HELPER.exists():
        raise SystemExit(f"Missing helper binary: {HELPER}")
    result = subprocess.run(
        [
            str(HELPER),
            "--self-test-alignment",
            "--rate",
            "48000",
            "--lowpass-hz",
            "0",
            "--highpass-hz",
            "0",
            "--main-delay-ms",
            str(main_delay_ms),
            "--sub-delay-ms",
            str(sub_delay_ms),
            "--sub-polarity",
            "normal",
            "--sub-level-db",
            "0",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    match = IMPULSE_RE.search(result.stdout)
    if not match:
        raise AssertionError(f"Could not parse helper alignment output: {result.stdout!r}")
    keys = (
        "main_delay_samples",
        "sub_delay_samples",
        "sub2_delay_samples",
        "output_1_impulse",
        "output_2_impulse",
        "output_3_impulse",
        "output_4_impulse",
    )
    return {key: int(value) for key, value in zip(keys, match.groups())}


def assert_impulses(label: str, result: dict[str, int], expected_main: int, expected_sub: int) -> None:
    expected = {
        "main_delay_samples": expected_main,
        "sub_delay_samples": expected_sub,
        "sub2_delay_samples": expected_sub,
        "output_1_impulse": expected_main,
        "output_2_impulse": expected_main,
        "output_3_impulse": expected_sub,
        "output_4_impulse": expected_sub,
    }
    if result != expected:
        raise AssertionError(f"{label}: expected {expected}, got {result}")


def main() -> int:
    baseline = run_case(0.0, 0.0)
    delay_3 = run_case(3.0, 0.0)
    delay_6 = run_case(6.0, 0.0)
    delay_30 = run_case(30.0, 0.0)

    assert_impulses("0 ms", baseline, 0, 0)
    assert_impulses("3 ms main delay", delay_3, 144, 0)
    assert_impulses("6 ms main delay", delay_6, 288, 0)
    assert_impulses("30 ms main delay", delay_30, 1440, 0)

    print(f"0 ms impulses: {baseline}")
    print(f"3 ms main delay impulses: {delay_3}")
    print(f"6 ms main delay impulses: {delay_6}")
    print(f"30 ms main delay impulses: {delay_30}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
