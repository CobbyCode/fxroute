#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""External-input routing dropped by a measurement window is re-asserted.

Syncs that arrive while a measurement session owns the audio graph are
dropped on purpose (the session release restores rates only), so the swallowed
paths mark the routing stale and the digital guard loop re-syncs it once the
graph is free again. Regression for the surround ingress staying stopped after
a measurement.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import measurement.session as measurement_session
from measurement.session import MeasurementSampleRateSession


class IngressResyncFlagTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._pending = main._ingress_resync_pending
        main._ingress_resync_pending = False
        self.addCleanup(self._restore)

    def _restore(self):
        main._ingress_resync_pending = self._pending

    async def test_swallowed_surround_sync_marks_resync_pending(self):
        dsp = SimpleNamespace(set_surround_input=AsyncMock())
        with patch.object(main, "_measurement_session_owns_live_rate", return_value=True), \
                patch.object(main.runtime, "dsp_runtime", dsp), \
                patch.object(main, "_set_surround_carrier_owned", AsyncMock()):
            await main._sync_surround_input("alsa_input.usb-mic", "aux_FL", "aux_FR", 3)
        dsp.set_surround_input.assert_not_awaited()
        self.assertTrue(main._ingress_resync_pending)

    async def test_swallowed_carrier_hold_marks_resync_pending(self):
        carrier = AsyncMock()
        with patch.object(main, "_measurement_session_owns_live_rate", return_value=True), \
                patch.object(main, "_set_surround_carrier_owned", carrier):
            await main._hold_external_input_carrier({"alsa_card": "not-an-int"})
        carrier.assert_not_awaited()
        self.assertTrue(main._ingress_resync_pending)

    async def test_swallowed_mapped_sync_marks_resync_pending(self):
        dsp = SimpleNamespace(set_mapped_ingress=AsyncMock())
        with patch.object(main, "_measurement_session_owns_live_rate", return_value=True), \
                patch.object(main.runtime, "dsp_runtime", dsp):
            await main._sync_mapped_input((("alsa_input.usb-mic:capture_AUX1", "input_FL"),))
        dsp.set_mapped_ingress.assert_not_awaited()
        self.assertTrue(main._ingress_resync_pending)

    async def test_resync_waits_until_measurement_releases(self):
        main._ingress_resync_pending = True
        routing = SimpleNamespace(sync=AsyncMock())
        with patch.object(main, "_measurement_session_owns_live_rate", return_value=True), \
                patch.object(main, "external_input", routing):
            await main._resync_pending_ingress()
        routing.sync.assert_not_awaited()
        self.assertTrue(main._ingress_resync_pending)

    async def test_resync_reasserts_routing_once_free(self):
        main._ingress_resync_pending = True
        routing = SimpleNamespace(sync=AsyncMock())
        with patch.object(main, "_measurement_session_owns_live_rate", return_value=False), \
                patch.object(main, "external_input", routing):
            await main._resync_pending_ingress()
            await main._resync_pending_ingress()
        routing.sync.assert_awaited_once()
        self.assertFalse(main._ingress_resync_pending)

    async def test_resync_failure_keeps_pending_for_retry(self):
        main._ingress_resync_pending = True
        routing = SimpleNamespace(sync=AsyncMock(side_effect=RuntimeError("links down")))
        with patch.object(main, "_measurement_session_owns_live_rate", return_value=False), \
                patch.object(main, "external_input", routing):
            await main._resync_pending_ingress()
        routing.sync.assert_awaited_once()
        self.assertTrue(main._ingress_resync_pending)

    async def test_no_resync_without_pending_mark(self):
        routing = SimpleNamespace(sync=AsyncMock())
        with patch.object(main, "_measurement_session_owns_live_rate", return_value=False), \
                patch.object(main, "external_input", routing):
            await main._resync_pending_ingress()
        routing.sync.assert_not_awaited()


class ReleaseHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._pending = main._ingress_resync_pending
        main._ingress_resync_pending = False
        self._snapshot = measurement_session._playback_state_before_measurement
        measurement_session._playback_state_before_measurement = None
        self.addCleanup(self._restore)

    def _restore(self):
        main._ingress_resync_pending = self._pending
        measurement_session._playback_state_before_measurement = self._snapshot

    async def test_composed_release_hook_marks_resync_pending(self):
        services = main._make_measurement_services()
        await services.post_release_input_resync()
        self.assertTrue(main._ingress_resync_pending)

    async def test_session_release_invokes_post_release_hook(self):
        hook_calls = []

        async def hook():
            hook_calls.append(True)

        services = SimpleNamespace(
            get_current_track_info=lambda: None,
            begin_playback_transition_attempt=lambda: 0,
            end_playback_transition_attempt=lambda: None,
            get_playback_transition_coordinator=lambda: None,
            get_current_pipewire_force_rate=lambda: 0,
            set_pipewire_force_rate=lambda rate: None,
            ensure_playback_samplerate_force=None,
            wait_for_samplerate_alignment=None,
            get_dsp_orchestrator=lambda: SimpleNamespace(sync_runtime_at_rate=AsyncMock(return_value=True)),
            get_samplerate_status=lambda: {"force_rate": 0, "active_rate": 48000, "clock_rate": 48000},
            stdin_measurement_release=AsyncMock(),
            post_release_input_resync=hook,
        )
        session = MeasurementSampleRateSession()
        session.active = True
        session.close_requested = True
        session.measurement_rate = 48000
        saved_services = measurement_session._services
        saved_policy = measurement_session.samplerate.load_sample_rate_policy
        measurement_session._services = services
        measurement_session.samplerate.load_sample_rate_policy = lambda: {"mode": "auto", "rate": 0}
        try:
            await session._release()
        finally:
            measurement_session._services = saved_services
            measurement_session.samplerate.load_sample_rate_policy = saved_policy
        self.assertEqual(hook_calls, [True])
        self.assertFalse(session.active)
        services.stdin_measurement_release.assert_awaited_once()

    async def test_release_survives_a_failing_resync_hook(self):
        async def hook():
            raise RuntimeError("resync hook exploded")

        services = SimpleNamespace(
            get_current_track_info=lambda: None,
            begin_playback_transition_attempt=lambda: 0,
            end_playback_transition_attempt=lambda: None,
            get_playback_transition_coordinator=lambda: None,
            get_current_pipewire_force_rate=lambda: 0,
            set_pipewire_force_rate=lambda rate: None,
            ensure_playback_samplerate_force=None,
            wait_for_samplerate_alignment=None,
            get_dsp_orchestrator=lambda: SimpleNamespace(sync_runtime_at_rate=AsyncMock(return_value=True)),
            get_samplerate_status=lambda: {"force_rate": 0, "active_rate": 48000, "clock_rate": 48000},
            stdin_measurement_release=AsyncMock(),
            post_release_input_resync=hook,
        )
        session = MeasurementSampleRateSession()
        session.active = True
        session.close_requested = True
        session.measurement_rate = 48000
        saved_services = measurement_session._services
        saved_policy = measurement_session.samplerate.load_sample_rate_policy
        measurement_session._services = services
        measurement_session.samplerate.load_sample_rate_policy = lambda: {"mode": "auto", "rate": 0}
        try:
            await session._release()
        finally:
            measurement_session._services = saved_services
            measurement_session.samplerate.load_sample_rate_policy = saved_policy
        self.assertFalse(session.active)


class ReleaseTierRestoreTests(unittest.IsolatedAsyncioTestCase):
    """A fixed rate on another channel tier is restored after the release.

    Without a playback snapshot the release pins the restore rate directly.
    On a profiled output that rate may only run on another tier, where a pin
    can never settle: the release leaves the measurement rate in place and
    hands the reprobe to the composition root.
    """

    TIERS = [
        {"id": "18ch", "channels": 18, "rates": [44100, 48000]},
        {"id": "14ch", "channels": 14, "rates": [88200, 96000]},
    ]

    def setUp(self):
        self._snapshot = measurement_session._playback_state_before_measurement
        measurement_session._playback_state_before_measurement = None
        self.addCleanup(self._restore)

    def _restore(self):
        measurement_session._playback_state_before_measurement = self._snapshot

    async def _release(self, *, policy_rate, active_tier="18ch"):
        self.force_writes = []
        self.hook_calls = []
        self.sync = AsyncMock(return_value=True)

        async def hook():
            self.hook_calls.append(True)

        services = SimpleNamespace(
            get_current_track_info=lambda: None,
            begin_playback_transition_attempt=lambda: 0,
            end_playback_transition_attempt=lambda: None,
            get_playback_transition_coordinator=lambda: None,
            get_current_pipewire_force_rate=lambda: 48000,
            set_pipewire_force_rate=self.force_writes.append,
            ensure_playback_samplerate_force=None,
            wait_for_samplerate_alignment=None,
            get_dsp_orchestrator=lambda: SimpleNamespace(sync_runtime_at_rate=self.sync),
            get_samplerate_status=lambda: {"force_rate": 48000, "active_rate": 48000, "clock_rate": 44100},
            get_audio_output_overview=lambda: {"selected_output": {
                "key": "alsa_output.scarlett",
                "device_profile": {"active_tier": active_tier, "tiers": [dict(tier) for tier in self.TIERS]},
            }},
            stdin_measurement_release=AsyncMock(),
            post_release_policy_rate_restore=hook,
        )
        session = MeasurementSampleRateSession()
        session.active = True
        session.close_requested = True
        session.measurement_rate = 48000
        session._rate_changed = True
        saved_services = measurement_session._services
        saved_policy = measurement_session.samplerate.load_sample_rate_policy
        measurement_session._services = services
        measurement_session.samplerate.load_sample_rate_policy = lambda: {"mode": "fixed", "rate": policy_rate}
        try:
            await session._release()
        finally:
            measurement_session._services = saved_services
            measurement_session.samplerate.load_sample_rate_policy = saved_policy
        self.assertFalse(session.active)

    async def test_rate_on_another_tier_is_left_to_the_post_release_reprobe(self):
        await self._release(policy_rate=96000)
        self.assertEqual(self.force_writes, [])
        self.sync.assert_not_awaited()
        self.assertEqual(self.hook_calls, [True])

    async def test_rate_on_the_measurement_tier_is_pinned_directly(self):
        await self._release(policy_rate=44100)
        self.assertEqual(self.force_writes, [44100])
        self.sync.assert_awaited_once()
        self.assertEqual(self.hook_calls, [])

    async def test_composed_release_hook_marks_the_restore_pending(self):
        with patch.object(main, "_policy_rate_restore_pending", False):
            await main._make_measurement_services().post_release_policy_rate_restore()
            self.assertTrue(main._policy_rate_restore_pending)

    async def _run_pending_restore(self, *, rate_change=True, session_owned=False, ingress_pending=False,
                                   transition_active=False, carrier_owned=False):
        policy = {"mode": "fixed", "rate": 96000}
        transition = AsyncMock()
        orchestrator = SimpleNamespace(sample_rate_policy_request=AsyncMock(
            return_value=SimpleNamespace(rate_change=rate_change)))
        with patch.object(main, "_policy_rate_restore_pending", True), \
                patch.object(main, "_ingress_resync_pending", ingress_pending), \
                patch.object(main, "_surround_carrier_owned", carrier_owned), \
                patch.object(main, "_measurement_session_owns_live_rate", return_value=session_owned), \
                patch.object(main, "_playback_transition_is_active", return_value=transition_active), \
                patch.object(main, "_source_transition_lock", return_value=asyncio.Lock()), \
                patch.object(main.samplerate, "load_sample_rate_policy", return_value=policy), \
                patch.object(main.playback_orchestration, "configured", return_value=orchestrator), \
                patch.object(main, "_transition_sample_rate_policy", transition):
            await main._restore_pending_policy_rate()
            return transition, main._policy_rate_restore_pending, policy

    async def test_pending_restore_transitions_the_policy_once(self):
        transition, pending, policy = await self._run_pending_restore()
        transition.assert_awaited_once_with(policy, detail="measurement-release")
        self.assertFalse(pending)

    async def test_pending_restore_without_a_rate_change_does_nothing(self):
        transition, pending, _policy = await self._run_pending_restore(rate_change=False)
        transition.assert_not_awaited()
        self.assertFalse(pending)

    async def test_pending_restore_waits_for_a_busy_graph(self):
        # The input routing is re-asserted first: it may claim the carrier.
        for busy in ({"session_owned": True}, {"ingress_pending": True}, {"transition_active": True}):
            transition, pending, _policy = await self._run_pending_restore(**busy)
            transition.assert_not_awaited()
            self.assertTrue(pending, busy)

    async def test_pending_restore_leaves_the_carrier_rate_alone(self):
        transition, pending, _policy = await self._run_pending_restore(carrier_owned=True)
        transition.assert_not_awaited()
        self.assertFalse(pending)

    async def test_guard_loop_restores_the_policy_rate_after_the_input_resync(self):
        order = []

        async def resync():
            order.append("resync")

        async def restore():
            order.append("restore")

        async def stop(_seconds):
            raise asyncio.CancelledError

        with patch.object(main, "_resync_pending_ingress", resync), \
                patch.object(main, "_restore_pending_policy_rate", restore), \
                patch.object(main, "_settle_digital_clocks", AsyncMock()), \
                patch.object(main.asyncio, "sleep", stop):
            with self.assertRaises(asyncio.CancelledError):
                await main._digital_clock_guard_loop()
        self.assertEqual(order, ["resync", "restore"])


if __name__ == "__main__":
    unittest.main()
