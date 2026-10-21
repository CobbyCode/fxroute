#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit output edits preserve inherited gate ownership and user mute."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playback.transition import PlaybackTransitionCoordinator
from test_playback_transition_coordinator import FakeRuntime


class OutputGateLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_lease_requires_coordinator_lock_before_touching_hardware(self):
        runtime = FakeRuntime()
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        with self.assertRaisesRegex(RuntimeError, 'coordinator ownership'):
            async with coordinator.output_gate_lease():
                self.fail('Unowned output edit was admitted')
        self.assertFalse(runtime.events)

    async def test_success_does_not_open_an_inherited_failure_gate(self):
        runtime = FakeRuntime(muted=False)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        coordinator._startup_gate_reconciled = True
        async with coordinator.lock:
            await coordinator._close_gate('prior-transition')
            await coordinator._latch_failure('prior-transition')
            async with coordinator.output_gate_lease():
                self.assertTrue(runtime.muted)
        self.assertTrue(runtime.muted)
        self.assertTrue(coordinator.gate.closed)
        self.assertTrue(coordinator.gate.failure_latched)
        self.assertEqual(coordinator.gate.transition_id, 'prior-transition')
        self.assertFalse(coordinator.gate.original_user_muted)

    async def test_failed_edit_preserves_original_error_and_latches_the_gate(self):
        runtime = FakeRuntime()
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        original = RuntimeError('Output edit failed')
        async with coordinator.lock:
            with self.assertRaises(RuntimeError) as raised:
                async with coordinator.output_gate_lease():
                    raise original
        self.assertIs(raised.exception, original)
        self.assertTrue(runtime.muted)
        self.assertTrue(coordinator.gate.failure_latched)


if __name__ == '__main__':
    unittest.main()
