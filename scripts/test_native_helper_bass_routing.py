#!/usr/bin/env python3
"""Smoke-test native helper mono/stereo bass routing without PipeWire graph routing."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "pipewire_stage1" / "build" / "fxroute_21_passthrough"
ROUTE_RE = re.compile(r"case=(L-only|R-only)\s+bass_routing=(mono|stereo)\s+output_3_rms=([0-9.]+)\s+output_4_rms=([0-9.]+)")


def run_case(routing: str) -> dict[str, tuple[float, float]]:
    if not HELPER.exists():
        raise SystemExit(f"Missing helper binary: {HELPER}")
    result = subprocess.run(
        [
            str(HELPER),
            "--self-test-bass-routing",
            "--bass-routing",
            routing,
            "--lowpass-hz",
            "0",
            "--highpass-hz",
            "0",
            "--main-delay-ms",
            "0",
            "--sub-delay-ms",
            "0",
            "--sub2-delay-ms",
            "0",
            "--sub-polarity",
            "normal",
            "--sub2-polarity",
            "normal",
            "--sub-level-db",
            "0",
            "--sub2-level-db",
            "0",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    parsed: dict[str, tuple[float, float]] = {}
    for match in ROUTE_RE.finditer(result.stdout):
        case, got_routing, out3, out4 = match.groups()
        if got_routing == routing:
            parsed[case] = (float(out3), float(out4))
    if set(parsed) != {"L-only", "R-only"}:
        raise AssertionError(f"Could not parse helper bass-routing output: {result.stdout!r}")
    return parsed


def main() -> int:
    mono = run_case("mono")
    stereo = run_case("stereo")

    for case, (out3, out4) in mono.items():
        if out3 <= 0.01 or out4 <= 0.01:
            raise AssertionError(f"mono {case}: expected both subs active, got output_3={out3} output_4={out4}")

    l_out3, l_out4 = stereo["L-only"]
    r_out3, r_out4 = stereo["R-only"]
    if l_out3 <= 0.01 or l_out4 >= 0.000001:
        raise AssertionError(f"stereo L-only: expected only output_3 active, got output_3={l_out3} output_4={l_out4}")
    if r_out4 <= 0.01 or r_out3 >= 0.000001:
        raise AssertionError(f"stereo R-only: expected only output_4 active, got output_3={r_out3} output_4={r_out4}")

    print(f"mono L-only output_3/output_4={mono['L-only']}")
    print(f"mono R-only output_3/output_4={mono['R-only']}")
    print(f"stereo L-only output_3/output_4={stereo['L-only']}")
    print(f"stereo R-only output_3/output_4={stereo['R-only']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
