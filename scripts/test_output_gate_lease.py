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

    async def test_verified_rollback_releases_only_a_new_lease_and_preserves_user_mute(self):
        for inherited in (False, True):
            for user_muted in (False, True):
                with self.subTest(inherited=inherited, user_muted=user_muted):
                    runtime = FakeRuntime(muted=user_muted)
                    coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
                    coordinator._startup_gate_reconciled = True
                    original = RuntimeError('Edit failed but was restored')
                    async def verified():
                        self.assertTrue(runtime.muted)
                        return True
                    async with coordinator.lock:
                        if inherited:
                            await coordinator._close_gate('prior')
                            await coordinator._latch_failure('prior')
                        with self.assertRaises(RuntimeError) as raised:
                            async with coordinator.output_gate_lease(verify_rollback=verified):
                                raise original
                    self.assertIs(raised.exception, original)
                    self.assertEqual(coordinator.gate.closed, inherited)
                    self.assertEqual(runtime.muted, inherited or user_muted)

    async def test_failed_rollback_verification_or_unmute_remains_fail_closed(self):
        for failure in ('unverified', 'readback', 'unmute'):
            with self.subTest(failure=failure):
                runtime = FakeRuntime()
                coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
                original = RuntimeError('Original edit error')
                async def verify():
                    if failure == 'readback':
                        raise RuntimeError('Readback failed')
                    if failure == 'unmute':
                        async def fail_unmute(muted, transition_id):
                            if not muted:
                                raise RuntimeError('Unmute failed')
                            runtime.muted = True
                        runtime.set_hardware_mute = fail_unmute
                    return failure != 'unverified'
                async with coordinator.lock:
                    with self.assertRaises(RuntimeError) as raised:
                        async with coordinator.output_gate_lease(verify_rollback=verify):
                            raise original
                self.assertIs(raised.exception, original)
                self.assertTrue(runtime.muted)
                self.assertTrue(coordinator.gate.failure_latched)


if __name__ == '__main__':
    unittest.main()
