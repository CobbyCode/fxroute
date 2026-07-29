#!/usr/bin/env python3
"""Focused regression checks for the peak monitor's PipeWire rate."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import peak_monitor


def check(status: dict, expected: int) -> None:
    original = peak_monitor.get_samplerate_status
    try:
        peak_monitor.get_samplerate_status = lambda: status
        actual = peak_monitor._resolve_capture_rate()
        assert actual == expected, (status, actual, expected)
    finally:
        peak_monitor.get_samplerate_status = original


check(
    {"force_rate": 0, "configured_default_rate": 44100, "clock_rate": 44100},
    44100,
)
check(
    {"force_rate": 48000, "configured_default_rate": 44100, "clock_rate": 44100},
    48000,
)
check(
    {"force_rate": 0, "configured_default_rate": None, "clock_rate": 96000},
    96000,
)
check({}, 48000)

print("peak monitor samplerate regression checks passed")
