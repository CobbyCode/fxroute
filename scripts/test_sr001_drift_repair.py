#!/usr/bin/env python3
"""SR-001 regression tests: serialized status-poll drift repair + resume helper sync.

Covers:
- status repair 48000 -> 44100 with final triple match (force-rate, sink, helper)
- full no-op while an active measurement session owns the rate
- resume with helper at 48000 and playback at 44100 (transition sync not skipped)
- stale playback generation / changed track aborts the repair
- missing sink alignment aborts without a false helper restart
- already-consistent state is a no-op
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import samplerate_orchestration


class FakePlayer:
    def __init__(self, state):
        self.state = state
        self._running = True


class FakeMeasurementSession:
    def __init__(self, active=False, measurement_rate=48000):
        self.active = active
        self.measurement_rate = measurement_rate


class FakeSubwooferRuntime:
    def __init__(self, helper_rate=None, active=True):
        self._helper_rate = helper_rate
        self._active = active

    def snapshot(self):
        return {
            "active": self._active,
            "helper_pid": 1234 if self._active else None,
            "helper_args": ["--rate", str(self._helper_rate)] if self._helper_rate else [],
            "config": {"sample_rate": self._helper_rate} if self._helper_rate else {},
            "last_error": None,
        }


def _radio_track():
    return {"source": "radio", "url": "http://radio.example/stream", "id": "radio-1", "title": "Station"}


def _local_track():
    return {"source": "local", "url": "/music/track.flac", "id": "t1", "title": "Track", "sample_rate_hz": 44100}


def _playing_state(url):
    return {"current_file": url, "paused": False, "ended": False, "position": 1.0}


class StatusRepairSerializedTests(unittest.IsolatedAsyncioTestCase):
    """_maybe_repair_active_app_samplerate_drift with full serialization."""

    async def asyncSetUp(self):
        self.originals = {}
        for name in (
            "player_instance", "current_track_info", "measurement_sr_session",
            "source_transition_lock", "playback_transition_generation",
            "last_app_samplerate_drift_repair_at", "subwoofer_runtime",
        ):
            self.originals[name] = getattr(main, name, None)
        main.player_instance = FakePlayer(_playing_state(_radio_track()["url"]))
        main.current_track_info = _radio_track()
        main.measurement_sr_session = FakeMeasurementSession(active=False)
        main.source_transition_lock = None
        main.playback_transition_generation = 2  # committed (even)
        main.last_app_samplerate_drift_repair_at = 0.0
        # Non-None so the helper-sync guard `if subwoofer_runtime is not None`
        # passes; _sync_subwoofer_runtime itself is mocked in each test.
        main.subwoofer_runtime = FakeSubwooferRuntime(helper_rate=48000)

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main, name, value)

    async def _run_repair(self, active_rate=48000):
        status = {"active_rate": active_rate}
        return await main._maybe_repair_active_app_samplerate_drift(status)

    def _enter_base(self, stack):
        stack.enter_context(patch.object(main, "_is_local_playback_active", return_value=True))
        stack.enter_context(patch.object(main, "_playback_state_matches_track", return_value=True))
        stack.enter_context(patch.object(main, "_current_track_matches", return_value=True))
        stack.enter_context(patch.object(
            main, "_resolve_expected_playback_samplerate", AsyncMock(return_value=44100),
        ))

    async def test_repair_48000_to_44100_triple_match(self):
        calls = []

        async def fake_ensure(rate, reason, *, policy):
            calls.append(("ensure", rate, reason, policy.name))
            return True  # force + sink aligned

        async def fake_sync(**kwargs):
            calls.append(("sync", kwargs.get("reason")))

        with ExitStack() as stack:
            self._enter_base(stack)
            stack.enter_context(patch.object(main, "_ensure_playback_samplerate_force", side_effect=fake_ensure))
            suspend = AsyncMock(return_value=True)
            stack.enter_context(patch.object(main, "_suspend_resume_playback_sink", suspend))
            stack.enter_context(patch.object(main, "_wait_for_samplerate_alignment", AsyncMock(return_value=True)))
            stack.enter_context(patch.object(main, "_sync_subwoofer_runtime", side_effect=fake_sync))
            await self._run_repair(active_rate=48000)

        self.assertEqual(
            calls,
            [("ensure", 44100, "status-drift-repair:radio", "status-drift-repair"), ("sync", "status-poll-rate-repair")],
        )
        suspend.assert_not_awaited()  # aligned without sink pulse

    async def test_repair_uses_common_reconcile_result_before_helper_sync(self):
        calls = []

        async def fake_ensure(rate, reason, *, policy):
            calls.append(("ensure", rate, reason, policy.name))
            return False

        async def fake_sync(**kwargs):
            calls.append(("sync", kwargs.get("reason")))

        with ExitStack() as stack:
            self._enter_base(stack)
            stack.enter_context(patch.object(main, "_ensure_playback_samplerate_force", side_effect=fake_ensure))
            stack.enter_context(patch.object(main, "_sync_subwoofer_runtime", side_effect=fake_sync))
            await self._run_repair(active_rate=48000)

        self.assertEqual(
            calls,
            [("ensure", 44100, "status-drift-repair:radio", "status-drift-repair")],
        )

    async def test_noop_during_active_measurement_session(self):
        main.measurement_sr_session = FakeMeasurementSession(active=True)
        with ExitStack() as stack:
            self._enter_base(stack)
            ensure = AsyncMock()
            suspend = AsyncMock()
            sync = AsyncMock()
            stack.enter_context(patch.object(main, "_ensure_playback_samplerate_force", ensure))
            stack.enter_context(patch.object(main, "_suspend_resume_playback_sink", suspend))
            stack.enter_context(patch.object(main, "_sync_subwoofer_runtime", sync))
            await self._run_repair(active_rate=48000)
        ensure.assert_not_awaited()
        suspend.assert_not_awaited()
        sync.assert_not_awaited()

    async def test_noop_when_already_consistent(self):
        with ExitStack() as stack:
            self._enter_base(stack)
            ensure = AsyncMock()
            suspend = AsyncMock()
            sync = AsyncMock()
            stack.enter_context(patch.object(main, "_ensure_playback_samplerate_force", ensure))
            stack.enter_context(patch.object(main, "_suspend_resume_playback_sink", suspend))
            stack.enter_context(patch.object(main, "_sync_subwoofer_runtime", sync))
            await self._run_repair(active_rate=44100)  # active == expected
        ensure.assert_not_awaited()
        suspend.assert_not_awaited()
        sync.assert_not_awaited()

    async def test_abort_on_stale_generation(self):
        main.playback_transition_generation = 3  # odd = transition in flight
        with ExitStack() as stack:
            self._enter_base(stack)
            ensure = AsyncMock()
            suspend = AsyncMock()
            sync = AsyncMock()
            stack.enter_context(patch.object(main, "_ensure_playback_samplerate_force", ensure))
            stack.enter_context(patch.object(main, "_suspend_resume_playback_sink", suspend))
            stack.enter_context(patch.object(main, "_sync_subwoofer_runtime", sync))
            await self._run_repair(active_rate=48000)
        ensure.assert_not_awaited()
        suspend.assert_not_awaited()
        sync.assert_not_awaited()

    async def test_abort_on_changed_track(self):
        with ExitStack() as stack:
            self._enter_base(stack)
            stack.enter_context(patch.object(main, "_current_track_matches", return_value=False))
            ensure = AsyncMock()
            suspend = AsyncMock()
            sync = AsyncMock()
            stack.enter_context(patch.object(main, "_ensure_playback_samplerate_force", ensure))
            stack.enter_context(patch.object(main, "_suspend_resume_playback_sink", suspend))
            stack.enter_context(patch.object(main, "_sync_subwoofer_runtime", sync))
            await self._run_repair(active_rate=48000)
        ensure.assert_not_awaited()
        suspend.assert_not_awaited()
        sync.assert_not_awaited()

    async def test_abort_on_missing_sink_alignment_no_helper_restart(self):
        with ExitStack() as stack:
            self._enter_base(stack)
            ensure = AsyncMock(return_value=False)
            stack.enter_context(patch.object(main, "_ensure_playback_samplerate_force", ensure))
            sync = AsyncMock()
            stack.enter_context(patch.object(main, "_sync_subwoofer_runtime", sync))
            await self._run_repair(active_rate=48000)
        ensure.assert_awaited_once_with(
            44100,
            "status-drift-repair:radio",
            policy=samplerate_orchestration.STATUS_DRIFT_REPAIR_POLICY,
        )
        sync.assert_not_awaited()

    async def test_abort_when_sink_suspend_skipped(self):
        with ExitStack() as stack:
            self._enter_base(stack)
            ensure = AsyncMock(return_value=False)
            stack.enter_context(patch.object(main, "_ensure_playback_samplerate_force", ensure))
            sync = AsyncMock()
            stack.enter_context(patch.object(main, "_sync_subwoofer_runtime", sync))
            await self._run_repair(active_rate=48000)
        ensure.assert_awaited_once_with(
            44100,
            "status-drift-repair:radio",
            policy=samplerate_orchestration.STATUS_DRIFT_REPAIR_POLICY,
        )
        sync.assert_not_awaited()

    async def test_repair_serialized_under_source_transition_lock(self):
        main.source_transition_lock = asyncio.Lock()
        with ExitStack() as stack:
            self._enter_base(stack)
            stack.enter_context(patch.object(main, "_ensure_playback_samplerate_force", AsyncMock(return_value=True)))
            stack.enter_context(patch.object(main, "_suspend_resume_playback_sink", AsyncMock(return_value=True)))
            sync = AsyncMock()
            stack.enter_context(patch.object(main, "_sync_subwoofer_runtime", sync))
            await self._run_repair(active_rate=48000)
        sync.assert_awaited_once()

    async def test_repair_skipped_when_playback_not_active(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "_is_local_playback_active", return_value=False))
            ensure = AsyncMock()
            sync = AsyncMock()
            stack.enter_context(patch.object(main, "_ensure_playback_samplerate_force", ensure))
            stack.enter_context(patch.object(main, "_sync_subwoofer_runtime", sync))
            await self._run_repair(active_rate=48000)
        ensure.assert_not_awaited()
        sync.assert_not_awaited()


class ResumeHelperSyncTests(unittest.IsolatedAsyncioTestCase):
    """_sync_subwoofer_runtime_after_playback_transition on resume paths."""

    async def asyncSetUp(self):
        self.originals = {}
        for name in (
            "player_instance", "current_track_info", "subwoofer_runtime",
            "source_transition_lock", "playback_transition_generation",
            "local_playback_handoff_completed_url", "local_playback_handoff_completed_rate",
        ):
            self.originals[name] = getattr(main, name, None)
        main.source_transition_lock = None
        main.playback_transition_generation = 2  # committed
        main.local_playback_handoff_completed_url = _local_track()["url"]
        main.local_playback_handoff_completed_rate = 44100
        main.player_instance = FakePlayer(_playing_state(_local_track()["url"]))
        main.current_track_info = _local_track()

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main, name, value)

    def _enter_base(self, stack):
        stack.enter_context(patch.object(main, "_wait_for_player_current_file", AsyncMock(return_value=True)))
        stack.enter_context(patch.object(main, "_current_track_matches", return_value=True))
        stack.enter_context(patch.object(
            main, "_resolve_expected_playback_samplerate", AsyncMock(return_value=44100),
        ))
        stack.enter_context(patch.object(main, "_wait_for_samplerate_alignment", AsyncMock(return_value=True)))
        stack.enter_context(patch.object(
            main, "get_audio_output_overview",
            return_value={"output_mode": {"mode": "subwoofer-2.2"}},
        ))

    async def test_resume_with_helper_48000_syncs_helper_to_44100(self):
        # Completed local handoff marker exists (44.1 kHz), but the helper still
        # runs at 48 kHz (e.g. after a measurement): the no-op guard must NOT
        # skip the transition sync.
        main.subwoofer_runtime = FakeSubwooferRuntime(helper_rate=48000)
        with ExitStack() as stack:
            self._enter_base(stack)
            sync = AsyncMock()
            stack.enter_context(patch.object(main, "_sync_subwoofer_runtime", sync))
            await main._sync_subwoofer_runtime_after_playback_transition(
                _local_track(), transition_generation=main.playback_transition_generation,
            )
        sync.assert_awaited_once()
        kwargs = sync.await_args.kwargs
        self.assertEqual(kwargs.get("reason"), "playback-transition")

    async def test_resume_with_helper_already_44100_is_noop(self):
        main.subwoofer_runtime = FakeSubwooferRuntime(helper_rate=44100)
        with ExitStack() as stack:
            self._enter_base(stack)
            sync = AsyncMock()
            stack.enter_context(patch.object(main, "_sync_subwoofer_runtime", sync))
            await main._sync_subwoofer_runtime_after_playback_transition(
                _local_track(), transition_generation=main.playback_transition_generation,
            )
        sync.assert_not_awaited()

    async def test_resume_without_completed_handoff_still_syncs(self):
        main.subwoofer_runtime = FakeSubwooferRuntime(helper_rate=44100)
        main.local_playback_handoff_completed_url = None
        main.local_playback_handoff_completed_rate = None
        with ExitStack() as stack:
            self._enter_base(stack)
            sync = AsyncMock()
            stack.enter_context(patch.object(main, "_sync_subwoofer_runtime", sync))
            await main._sync_subwoofer_runtime_after_playback_transition(
                _local_track(), transition_generation=main.playback_transition_generation,
            )
        sync.assert_awaited_once()

    async def test_resume_aborts_on_stale_generation(self):
        main.subwoofer_runtime = FakeSubwooferRuntime(helper_rate=48000)
        with ExitStack() as stack:
            self._enter_base(stack)
            sync = AsyncMock()
            stack.enter_context(patch.object(main, "_sync_subwoofer_runtime", sync))
            await main._sync_subwoofer_runtime_after_playback_transition(
                _local_track(), transition_generation=3,  # odd / stale
            )
        sync.assert_not_awaited()

    async def test_resume_radio_uses_force_then_helper_sync(self):
        main.subwoofer_runtime = FakeSubwooferRuntime(helper_rate=48000)
        main.current_track_info = _radio_track()
        radio = _radio_track()
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "_wait_for_player_current_file", AsyncMock(return_value=True)))
            stack.enter_context(patch.object(main, "_current_track_matches", return_value=True))
            stack.enter_context(patch.object(
                main, "_resolve_expected_playback_samplerate", AsyncMock(return_value=44100),
            ))
            ensure = AsyncMock(return_value=True)
            stack.enter_context(patch.object(main, "_ensure_playback_samplerate_force", ensure))
            stack.enter_context(patch.object(
                main, "get_audio_output_overview",
                return_value={"output_mode": {"mode": "subwoofer-2.2"}},
            ))
            sync = AsyncMock()
            stack.enter_context(patch.object(main, "_sync_subwoofer_runtime", sync))
            await main._sync_subwoofer_runtime_after_playback_transition(
                radio, transition_generation=main.playback_transition_generation,
            )
        ensure.assert_awaited_once()
        ensure.assert_awaited_with(44100, "radio-playback-transition", policy=samplerate_orchestration.RADIO_POLICY)
        sync.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
