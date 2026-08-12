#!/usr/bin/env python3
"""Direct tests for the I/O-independent playback-rate reconcile policies."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import samplerate_orchestration as orchestration


class PolicyReconcileTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, policy, *, active=48000, force=0, waits=(False, True), pulse=True):
        events = []
        state = {"active_rate": active, "force_rate": force}
        wait_results = iter(waits)

        def read_status():
            events.append(("read", dict(state)))
            return dict(state)

        def write_force(rate):
            events.append(("force", rate))
            state["force_rate"] = rate

        async def wait(rate, timeout_ms):
            events.append(("align", rate, timeout_ms))
            return next(wait_results)

        async def sink_pulse(reason):
            events.append(("pulse", reason))
            return pulse

        result = await orchestration.reconcile_playback_samplerate(
            expected_rate=44100,
            reason="contract",
            policy=policy,
            read_status=read_status,
            write_force_rate=write_force,
            wait_for_alignment=wait,
            pulse_sink=sink_pulse,
        )
        return result, events

    async def test_default_policy_no_pulse(self):
        result, events = await self._run(orchestration.DEFAULT_POLICY, waits=(False,))
        self.assertFalse(result)
        self.assertEqual(events, [("read", {"active_rate": 48000, "force_rate": 0}),
                                  ("force", 44100), ("align", 44100, 400)])

    async def test_radio_policy_pulses_only_for_observed_active_rate_mismatch(self):
        result, events = await self._run(orchestration.RADIO_POLICY, waits=(False, True))
        self.assertTrue(result)
        self.assertEqual(events, [("read", {"active_rate": 48000, "force_rate": 0}),
                                  ("force", 44100), ("align", 44100, 400),
                                  ("pulse", "contract"), ("align", 44100, 1200)])

    async def test_radio_policy_does_not_pulse_when_initial_active_rate_was_unknown(self):
        result, events = await self._run(orchestration.RADIO_POLICY, active=None, waits=(False,))
        self.assertFalse(result)
        self.assertEqual(events, [("read", {"active_rate": None, "force_rate": 0}),
                                  ("force", 44100), ("align", 44100, 400)])

    async def test_already_aligned_is_noop(self):
        result, events = await self._run(orchestration.RADIO_POLICY, active=44100, force=44100, waits=())
        self.assertTrue(result)
        self.assertEqual(events, [("read", {"active_rate": 44100, "force_rate": 44100})])

    async def test_callback_error_is_propagated(self):
        def read_status():
            raise RuntimeError("status failure")

        async def never_wait(rate, timeout_ms):
            self.fail("wait must not run after a status callback error")

        with self.assertRaises(RuntimeError):
            await orchestration.reconcile_playback_samplerate(
                expected_rate=44100,
                reason="contract",
                policy=orchestration.DEFAULT_POLICY,
                read_status=read_status,
                write_force_rate=lambda rate: None,
                wait_for_alignment=never_wait,
                pulse_sink=lambda reason: asyncio.sleep(0, result=True),
            )


if __name__ == "__main__":
    unittest.main()
