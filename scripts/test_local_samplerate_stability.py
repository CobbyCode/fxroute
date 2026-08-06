#!/usr/bin/env python3
"""Focused regression checks for the local samplerate handoff stability gate."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


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
) -> tuple[list[dict], bool]:
    """Verify the thin local wrapper delegates to the shared handoff.

    The wrapper only determines the target rate (library metadata
    preferred, MPV live rate as fallback) and forwards the transition
    generation; the reconciliation itself runs in the shared
    _complete_playback_handoff.
    """
    original_handoff = main._complete_playback_handoff
    original_generation = main.playback_transition_generation
    original_live = main._wait_for_player_audio_samplerate
    calls: list[dict] = []

    async def fake_shared(**kwargs):
        calls.append(kwargs)
        return True

    async def fake_live():
        return live_rate

    try:
        main._complete_playback_handoff = fake_shared
        main.playback_transition_generation = generation
        main._wait_for_player_audio_samplerate = fake_live
        await main._complete_local_playback_handoff(
            {"url": "test.flac"}, expected_rate,
            transition_generation=generation,
        )
        return calls, True
    finally:
        main._complete_playback_handoff = original_handoff
        main.playback_transition_generation = original_generation
        main._wait_for_player_audio_samplerate = original_live


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

    calls, _ = await run_handoff(expected_rate=48000, generation=42)
    assert len(calls) == 1, "the local wrapper must delegate to the shared handoff"
    assert calls[0]["target_rate"] == 48000, "library metadata must be the preferred target rate"
    assert calls[0]["reason"] == "local-playback-handoff"
    assert calls[0]["transition_generation"] == 42, "the wrapper must forward the transition generation"

    calls, _ = await run_handoff(expected_rate=None, generation=42, live_rate=44100)
    assert len(calls) == 1, "metadata-less tracks must still hand off"
    assert calls[0]["target_rate"] == 44100, "missing metadata falls back to the MPV live rate"


if __name__ == "__main__":
    asyncio.run(main_async())
    print("local samplerate stability regression checks passed")
