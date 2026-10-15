#!/usr/bin/env python3
"""Regression: samplerate alignment and AutoSub pre-arm stay off the loop.

The sample-rate reconciliation helpers used to run their sync subprocess
pipelines (samplerate status, pw-metadata force-rate writes, pactl suspend
pulses, ffmpeg trigger generation) directly on the asyncio event loop.
These tests pin the fix: wherever the async alignment paths build status,
write force-rate, pulse the sink or generate the idle trigger, the heavy
work must run inside a worker thread.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import audio.samplerate.alignment as alignment
import audio.samplerate_orchestration as orchestration
import measurement.session as measurement_session

MAIN_THREAD = threading.current_thread()


def _off_loop_probe(testcase, seen, value):
    """Return a probe that records whether it ran on the event-loop thread."""

    def probe(*_args, **_kwargs):
        seen.append(threading.current_thread() is MAIN_THREAD)
        if callable(value):
            return value()
        return value

    return probe


def _assert_all_off_loop(testcase, seen, label):
    testcase.assertTrue(
        seen,
        f"{label}: heavy helper was never invoked",
    )
    testcase.assertFalse(
        any(on_loop for on_loop in seen),
        f"{label}: heavy helper ran on the event-loop thread",
    )


class AlignmentOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_for_samplerate_alignment_reads_status_off_loop(self):
        seen = []

        def status():
            seen.append(threading.current_thread() is MAIN_THREAD)
            return {"active_rate": 48000}

        with patch.object(alignment, "get_samplerate_status", status):
            aligned = await alignment.wait_for_samplerate_alignment(48000, timeout_ms=2000)
        self.assertTrue(aligned)
        _assert_all_off_loop(self, seen, "wait_for_samplerate_alignment status read")

    async def test_ensure_playback_samplerate_force_offloads_all_io(self):
        seen: list[tuple[str, bool]] = []
        state = {"active_rate": 44100, "force_rate": 0}

        def status():
            seen.append(("status", threading.current_thread() is MAIN_THREAD))
            return dict(state)

        def write_force(rate):
            seen.append(("force", threading.current_thread() is MAIN_THREAD))
            state["force_rate"] = rate
            state["active_rate"] = rate

        def pulse(_output_key, _reason):
            seen.append(("pulse", threading.current_thread() is MAIN_THREAD))

        waits = iter([False, True])

        async def wait_alignment(rate, timeout_ms):
            state["active_rate"] = rate
            return next(waits)

        with (
            patch.object(alignment, "get_samplerate_status", status),
            patch.object(alignment, "set_pipewire_force_rate", write_force),
            patch.object(alignment, "wait_for_samplerate_alignment", wait_alignment),
            patch.object(
                alignment, "get_audio_output_overview",
                return_value={"output_mode": {"effective_output_key": "alsa_output.test"}},
            ),
            patch.object(alignment, "pulse_suspend_sink_for_samplerate", pulse),
        ):
            aligned = await alignment.ensure_playback_samplerate_force(
                48000, "offload-test",
                policy=orchestration.RADIO_POLICY,
            )
        self.assertTrue(aligned)
        self.assertTrue(seen, "no alignment I/O helper was invoked")
        self.assertFalse(
            any(on_loop for _name, on_loop in seen),
            "ensure_playback_samplerate_force ran status/force/pulse I/O on the event loop",
        )
        self.assertEqual({name for name, _ in seen}, {"status", "force", "pulse"})

    async def test_suspend_resume_playback_sink_pulses_off_loop(self):
        seen = []
        pulse = _off_loop_probe(self, seen, None)
        with patch.object(alignment, "pulse_suspend_sink_for_samplerate", pulse):
            completed = await alignment.suspend_resume_playback_sink(
                reason="offload-test", output_key="alsa_output.test", force=True,
            )
        self.assertTrue(completed)
        _assert_all_off_loop(self, seen, "suspend_resume_playback_sink pactl pulse")

    async def test_concurrent_suspend_resume_callers_respect_cooldown(self):
        """A caller reaching the lock mid-pulse must not double-pulse the sink.

        The cooldown timestamp is written only after the pactl pulse, so two
        nearly simultaneous callers used to both pass the 3 s check before
        either write landed and pulsed twice.  The second caller must instead
        block on the suspend lock and be cooldown-skipped once the first
        finished.
        """
        pulses: list[str] = []
        in_pulse = threading.Event()
        release = threading.Event()

        def pulse(output_key, reason):
            pulses.append(reason)
            in_pulse.set()
            release.wait(2)

        saved_at = alignment._last_sink_suspend_at
        try:
            alignment._last_sink_suspend_at = 0.0
            with (
                patch.object(alignment, "pulse_suspend_sink_for_samplerate", pulse),
                patch.object(
                    alignment, "get_audio_output_overview",
                    return_value={"output_mode": {"effective_output_key": "alsa_output.test"}},
                ),
            ):
                first = asyncio.create_task(
                    alignment.suspend_resume_playback_sink(reason="first", output_key="alsa_output.test")
                )
                while not in_pulse.is_set():
                    await asyncio.sleep(0.01)
                second = asyncio.create_task(
                    alignment.suspend_resume_playback_sink(reason="second", output_key="alsa_output.test")
                )
                await asyncio.sleep(0.05)
                self.assertFalse(second.done(), "second caller must wait on the suspend lock")
                self.assertEqual(pulses, ["first"], "second caller must not pulse while the first is running")
                release.set()
                results = await asyncio.gather(first, second)
        finally:
            release.set()
            alignment._last_sink_suspend_at = saved_at
        self.assertEqual(results, [True, False], "the late caller must be cooldown-skipped")
        self.assertEqual(pulses, ["first"])

    async def test_trigger_idle_sink_renegotiation_generates_file_off_loop(self):
        gen_seen = []
        status_seen = []

        def generate(rate):
            gen_seen.append(threading.current_thread() is MAIN_THREAD)
            return Path("/tmp/fxroute-offload-trigger.wav")

        def status():
            status_seen.append(threading.current_thread() is MAIN_THREAD)
            return {"active_rate": 48000}

        with (
            patch.object(alignment, "ensure_rate_renegotiation_trigger_file", generate),
            patch.object(alignment, "get_samplerate_status", status),
            patch.object(alignment.subprocess, "Popen"),
        ):
            ok = await alignment.trigger_idle_sink_renegotiation(48000)
        self.assertTrue(ok)
        _assert_all_off_loop(self, gen_seen, "trigger file generation (ffmpeg)")
        _assert_all_off_loop(self, status_seen, "trigger alignment status read")

    async def test_reconcile_transition_sink_rate_reads_status_off_loop(self):
        seen = []
        reads = iter([
            {"active_rate": 44100, "force_rate": 0},
            {"active_rate": 48000, "force_rate": 48000},
        ])

        def status():
            seen.append(threading.current_thread() is MAIN_THREAD)
            return next(reads)

        with (
            patch.object(alignment, "get_samplerate_status", status),
            patch.object(
                alignment, "ensure_playback_samplerate_force",
                new=AsyncMock(return_value=True),
            ),
        ):
            ok = await alignment.reconcile_transition_sink_rate(48000, reason="offload-test")
        self.assertTrue(ok)
        _assert_all_off_loop(self, seen, "reconcile_transition_sink_rate status read")


class MeasurementRateReadbackOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_output_readback_stays_off_loop(self):
        """Measurement release must read the effective output rate off-loop."""
        seen: list[tuple[str, bool]] = []

        def overview():
            seen.append(("overview", threading.current_thread() is MAIN_THREAD))
            return {
                "selected_output": {"key": "mock", "channels": 4, "active_rate": 48000},
                "output_mode": {
                    "mode": "subwoofer-2.1",
                    "effective_output_key": "alsa_output.test",
                    "effective_output_rate": 48000,
                },
            }

        services = {
            "get_audio_output_overview": overview,
            "pipewire_handoff_poll_interval_ms": 1,
        }
        with (
            patch.object(measurement_session, "_measurement_services", return_value=SimpleNamespace(**services)),
        ):
            aligned, result = await measurement_session._wait_for_selected_output_effective_rate(48000)
        self.assertTrue(aligned)
        self.assertEqual(result["output_mode"]["effective_output_rate"], 48000)
        _assert_all_off_loop(self, [on_loop for _, on_loop in seen], "measurement output readback")


if __name__ == "__main__":
    unittest.main(verbosity=2)
