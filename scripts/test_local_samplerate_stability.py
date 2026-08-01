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


async def run_handoff(*, stability_results: list[bool], status: dict) -> tuple[list[int], int]:
    original_wait = main._wait_for_local_samplerate_stability
    original_status = main.get_samplerate_status
    original_set_force_rate = main._set_pipewire_force_rate
    original_pulse = main._suspend_resume_playback_sink
    original_overview = main.get_audio_output_overview
    results = iter(stability_results)
    force_rate_updates: list[int] = []
    pulse_count = 0

    async def fake_wait(expected_rate: int, *, timeout_ms: int, stable_ms: int = 350) -> bool:
        assert expected_rate == 48000
        return next(results)

    def fake_set_force_rate(rate: int) -> None:
        force_rate_updates.append(rate)

    async def fake_pulse(*, reason: str, force: bool) -> bool:
        nonlocal pulse_count
        assert reason == "local-playback-handoff"
        assert force
        pulse_count += 1
        return True

    try:
        main._wait_for_local_samplerate_stability = fake_wait
        main.get_samplerate_status = lambda: status
        main._set_pipewire_force_rate = fake_set_force_rate
        main._suspend_resume_playback_sink = fake_pulse
        main.get_audio_output_overview = lambda: {"output_mode": {"mode": "stereo"}}
        await main._complete_local_playback_handoff({"url": "test.flac"}, 48000)
        return force_rate_updates, pulse_count
    finally:
        main._wait_for_local_samplerate_stability = original_wait
        main.get_samplerate_status = original_status
        main._set_pipewire_force_rate = original_set_force_rate
        main._suspend_resume_playback_sink = original_pulse
        main.get_audio_output_overview = original_overview


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

    force_rate_updates, pulse_count = await run_handoff(
        stability_results=[False, True],
        status={"active_rate": 48000, "force_rate": 44100},
    )
    assert force_rate_updates == [48000], "a lost force_rate must be restored before the pulse"
    assert pulse_count == 1, "an unstable handoff must pulse the sink once"

    force_rate_updates, pulse_count = await run_handoff(
        stability_results=[True],
        status={"active_rate": 48000, "force_rate": 48000},
    )
    assert force_rate_updates == [], "a stable handoff must not rewrite force_rate"
    assert pulse_count == 0, "an already stable handoff must not pulse the sink"


if __name__ == "__main__":
    asyncio.run(main_async())
    print("local samplerate stability regression checks passed")
