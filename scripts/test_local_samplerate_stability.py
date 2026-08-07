#!/usr/bin/env python3
"""Focused regression checks for the local samplerate handoff stability gate."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from playback_transition_test_support import run_main_handoff_through_coordinator


async def run_gate(statuses: list[dict], *, timeout_ms: int, stable_ms: int = 350) -> bool:
    original_status = main.get_samplerate_status
    original_monotonic = main.time.monotonic
    original_sleep = main.asyncio.sleep
    clock = 0.0
    index = 0

    def fake_monotonic() -> float:
        return clock

    def fake_status() -> dict:
        nonlocal index
        value = statuses[min(index, len(statuses) - 1)]
        index += 1
        return value

    async def fake_sleep(delay: float) -> None:
        nonlocal clock
        clock += delay

    try:
        main.get_samplerate_status = fake_status
        main.time.monotonic = fake_monotonic
        main.asyncio.sleep = fake_sleep
        return await main._wait_for_local_samplerate_stability(
            48000, timeout_ms=timeout_ms, stable_ms=stable_ms,
        )
    finally:
        main.get_samplerate_status = original_status
        main.time.monotonic = original_monotonic
        main.asyncio.sleep = original_sleep


async def run_handoff(
    *, expected_rate: int | None, generation: int, live_rate: int | None = None,
) -> tuple[list[str], object]:
    """Verify metadata/live-rate resolution enters the Coordinator contract."""
    original_generation = main.playback_transition_generation
    events: list[str] = []
    status = {"active_rate": 44100, "force_rate": 44100}

    def samplerate_status() -> dict:
        return dict(status)

    async def ensure_force(rate, reason, *, policy=None) -> bool:
        status["active_rate"] = rate
        status["force_rate"] = rate
        return True

    try:
        main.playback_transition_generation = generation
        target_rate = expected_rate if expected_rate is not None else live_rate
        with patch.object(main, "get_samplerate_status", samplerate_status), patch.object(
            main, "_ensure_playback_samplerate_force", ensure_force
        ), patch.object(main, "_get_current_pipewire_force_rate", lambda: status["force_rate"]):
            result, _runtime = await run_main_handoff_through_coordinator(
                target_rate=target_rate,
                generation=generation,
                source="local",
                detail="local-playback-handoff",
                use_core=False,
                rate_change=True,
                events=events,
            )
        return events, result
    finally:
        main.playback_transition_generation = original_generation


async def main_async() -> None:
    transient = await run_gate(
        [
            {"active_rate": 44100, "force_rate": 48000},
            {"active_rate": 48000, "force_rate": 48000},
            {"active_rate": 44100, "force_rate": 48000},
        ],
        timeout_ms=700,
    )
    assert not transient, "a transient 48 kHz snapshot must not pass the gate"

    missing_force = await run_gate(
        [{"active_rate": 48000, "force_rate": 44100}],
        timeout_ms=500,
    )
    assert not missing_force, "active rate alone must not pass the gate"

    stable = await run_gate(
        [{"active_rate": 48000, "force_rate": 48000}],
        timeout_ms=700,
    )
    assert stable, "matching active and force rates must pass after stable_ms"

    events, result = await run_handoff(expected_rate=48000, generation=42)
    assert result.target_rate == 48000, "library metadata must be the preferred target rate"
    assert events.index("gate.set:True") < events.index("rate")
    assert events.index("commit-readback") < events.index("gate.set:False")

    events, result = await run_handoff(expected_rate=None, generation=42, live_rate=44100)
    assert result.target_rate == 44100, "missing metadata falls back to the MPV live rate"
    assert events.index("gate.set:True") < events.index("start")


if __name__ == "__main__":
    asyncio.run(main_async())
    print("local samplerate stability regression checks passed")
