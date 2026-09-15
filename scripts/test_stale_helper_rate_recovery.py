#!/usr/bin/env python3
"""Regression: a stale measurement helper must not block later playback.

Observed on .104 (2026-09-12): a measurement entry rebuilt the native DSP
helper at the measurement rate (48 kHz), its restore was then skipped
(``intent-changed-after-quiet``) and the release re-sync deferred.  The helper
kept running at 48 kHz while the graph was pinned at 44.1 kHz, and a helper
stream pins the hardware sink at its own rate: neither ``clock.force-rate``,
nor the sink suspend/resume pulse, nor the idle silent trigger could move it.
Every later play then failed its ``target-rate`` stage ("target hardware rate
did not settle", HTTP 500 on /api/play) until an unrelated output-mode switch
rebuilt the helper.  The output-mode switch must not be the repair mechanism.

Covers the recovery at all three layers:
1. ``DspOrchestrator.sync_runtime_at_rate`` (measurement release) rebuilds the
   stale helper instead of silently deferring the restore.  The cases mirror
   the real call site: the session still reports graph ownership while the
   release (and therefore the repair) runs, because ``owns_audio_graph`` stays
   true until the release returns.  A rebuild releases the helper's hold on
   the sink, but the hardware node can renegotiate late or only after a nudge,
   so the repair keeps running in bounded rounds and also repairs the state
   where the helper already runs at the target while the sink lags behind it.
2. ``_RuntimeSourceMixin.establish_target_rate`` recovers a stale helper
   before failing the transition, so a later play self-heals.
3. The idle link-watch tick repairs a live force-rate pin the graph does not
   honour instead of preserving it until the next play fails, keeps its rate
   limit against rebuild loops, and still retries a failed attempt once the
   cooldown elapsed.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.orchestration import DspOrchestrator  # noqa: E402
from playback.runtime import FxrouteTransitionRuntime  # noqa: E402
from playback.transition import TransitionRequest  # noqa: E402

TARGET_RATE = 44_100
MEASUREMENT_RATE = 48_000


class _FakeDspRuntime:
    """Native DSP runtime facade whose helper rate drives the hardware sink."""

    def __init__(self, helper_rate: int) -> None:
        self.helper_rate = helper_rate
        self.sync_in_progress = False
        self.sync_calls: list[dict] = []
        # Set by the dependency bag: lets it model a hardware sink that
        # follows the helper rebuild (or deliberately does not).
        self.on_sync = None

    def snapshot(self) -> dict:
        return {
            "active": True,
            "helper_pid": 4242,
            "config": {"sample_rate": self.helper_rate},
        }

    async def sync(self, overview: dict) -> None:
        self.sync_calls.append(dict(overview))
        output_mode = overview.get("output_mode") or {}
        selected = overview.get("selected_output") or {}
        self.helper_rate = int(
            output_mode.get("effective_output_rate") or selected.get("active_rate")
        )
        if self.on_sync is not None:
            self.on_sync()


class _FailingSyncRuntime(_FakeDspRuntime):
    """Runtime whose rebuild fails before the running helper is replaced."""

    def __init__(self, helper_rate: int) -> None:
        super().__init__(helper_rate)
        self.attempts = 0

    async def sync(self, overview: dict) -> None:
        self.attempts += 1
        raise RuntimeError("native DSP config could not be prepared")


class _OrchestratorDeps:
    """Dependency bag where the hardware sink follows the running helper."""

    def __init__(self, runtime: _FakeDspRuntime, *, pinned_rate: int, owned: bool = False) -> None:
        self.runtime = runtime
        self.pinned_rate = pinned_rate
        self.owned = owned
        self.force_rate_writes: list[int] = []
        self.force_rate_kwargs: list[dict] = []
        # Read accounting: the idle repair must decide its early return from
        # the pin-only probe and only build the full status when a pin is live.
        self.force_rate_reads = 0
        self.samplerate_status_reads = 0
        # The hardware sink normally follows a helper rebuild (that is what the
        # rebuild is for).  A hardware node can also renegotiate late, so the
        # bounded nudge is what moves it there, on a configurable call.
        self.sink_follows_rebuild = True
        self.sink_follows_nudge = True
        self.nudges_before_sink_settles = 1
        self.nudge_calls = 0
        # A fully idle sink ignores the pin and the pulse; the silent stream is
        # the documented trigger that moves it then.
        self.sink_follows_trigger = True
        self.triggers_before_sink_settles = 1
        self.trigger_calls = 0
        self.sink_rate = runtime.helper_rate
        runtime.on_sync = self._helper_rebuilt

    def _helper_rebuilt(self) -> None:
        if self.sink_follows_rebuild:
            self.sink_rate = self.runtime.helper_rate

    # -- injected services -------------------------------------------------
    def get_dsp_runtime(self):
        return self.runtime

    def get_dsp_manager(self):
        return None

    def get_audio_output_overview(self):
        return {
            "selected_output": {"active_rate": self.runtime.helper_rate, "key": "hw"},
            "current_output": {"active_rate": self.runtime.helper_rate, "key": "hw"},
            "output_mode": {
                "mode": "subwoofer-2.2",
                "effective_output_key": "hw",
                "effective_output_rate": self.runtime.helper_rate,
            },
        }

    def get_current_force_rate(self):
        self.force_rate_reads += 1
        return self.pinned_rate

    def get_samplerate_status(self):
        self.samplerate_status_reads += 1
        return {
            "active_rate": self.sink_rate,
            "force_rate": self.pinned_rate,
        }

    def get_measurement_sr_session(self):
        return None

    def measurement_audio_graph_owned(self):
        return self.owned

    def observe_playback_samplerate_drift(self):
        raise AssertionError("not used")

    def playback_transition_is_active(self):
        return False

    def get_current_track_info(self):
        return {}

    def get_output_mode(self):
        return "subwoofer-2.2"

    def get_player_instance(self):
        return None

    def coordinator_target_rate(self, *_args, **_kwargs):
        return None

    def playback_graph_diagnosis(self, *_args, **_kwargs):
        raise AssertionError("not used")

    def request_coordinated_recovery(self, *_args, **_kwargs):
        raise AssertionError("not used")

    def create_lifecycle_background_task(self, *_args, **_kwargs):
        raise AssertionError("not used")

    async def ensure_playback_samplerate_force(self, rate, reason, **kwargs):
        # Stand-in for the canonical bounded reconcile policy: it retargets the
        # pin and, once a stale helper no longer clocks the sink, moves the
        # hardware node onto the rate.
        self.force_rate_writes.append(rate)
        self.force_rate_kwargs.append(dict(kwargs))
        self.pinned_rate = rate
        self.nudge_calls += 1
        if self.sink_follows_nudge and self.nudge_calls >= self.nudges_before_sink_settles:
            self.sink_rate = rate
            return True
        return False

    async def trigger_idle_sink_renegotiation(self, rate):
        # Stand-in for the silent-stream trigger of an idle hardware sink.
        self.trigger_calls += 1
        if self.sink_follows_trigger and self.trigger_calls >= self.triggers_before_sink_settles:
            self.sink_rate = rate
            return True
        return False

    # -- awaited callbacks -------------------------------------------------
    async def wait_for_samplerate_alignment(self, rate, timeout_ms=0):
        return self.sink_rate == rate

    async def wait_for_selected_output_effective_rate(self, rate, timeout_ms=0):
        return (self.sink_rate == rate, dict(self.get_audio_output_overview()))

    async def sleep(self, _delay):
        return None


async def _run_watch_ticks(
    orchestrator: DspOrchestrator, deps: _OrchestratorDeps, *, ticks: int = 1
) -> None:
    """Run ``ticks`` link-watch iterations of an existing orchestrator."""
    sleeps = 0

    async def cancel_after_the_requested_ticks(_delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps <= ticks:
            return
        raise asyncio.CancelledError

    deps.sleep = cancel_after_the_requested_ticks
    deps.observe_playback_samplerate_drift = AsyncMock()
    task = asyncio.create_task(orchestrator.runtime_link_watch_loop())
    try:
        await task
    except asyncio.CancelledError:
        pass


class MeasurementReleaseRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """Layer 1: the measurement release must not defer onto a stale helper.

    Every case mirrors the real call site: the release runs inside the
    sample-rate session, so ``measurement_audio_graph_owned()`` is true while
    the re-sync -- and therefore the repair -- runs.
    """

    async def test_release_rebuilds_stale_measurement_helper(self):
        runtime = _FakeDspRuntime(MEASUREMENT_RATE)
        deps = _OrchestratorDeps(runtime, pinned_rate=TARGET_RATE, owned=True)
        orchestrator = DspOrchestrator(deps)

        await orchestrator.sync_runtime_at_rate(TARGET_RATE, _rate_lock_held=True)

        self.assertEqual(runtime.helper_rate, TARGET_RATE)
        self.assertTrue(runtime.sync_calls, "the stale helper must be rebuilt")
        self.assertEqual(
            (runtime.sync_calls[0].get("output_mode") or {}).get("effective_output_rate"),
            TARGET_RATE,
            "the rebuild must run at the restore rate, not the measurement rate",
        )

    async def test_release_aligns_a_pin_that_contradicts_the_restore_rate(self):
        runtime = _FakeDspRuntime(MEASUREMENT_RATE)
        deps = _OrchestratorDeps(runtime, pinned_rate=MEASUREMENT_RATE, owned=True)
        orchestrator = DspOrchestrator(deps)

        await orchestrator.sync_runtime_at_rate(TARGET_RATE, _rate_lock_held=True)

        self.assertEqual(runtime.helper_rate, TARGET_RATE)
        self.assertEqual(deps.force_rate_writes, [TARGET_RATE])
        # The release holds the sample-rate lock, so the canonical reconcile
        # path must be allowed past the measurement gate it would otherwise
        # defer on.
        self.assertTrue(deps.force_rate_kwargs[0].get("allow_measurement_session"))

    async def test_pin_retarget_respects_the_gate_without_measurement_ownership(self):
        runtime = _FakeDspRuntime(MEASUREMENT_RATE)
        deps = _OrchestratorDeps(runtime, pinned_rate=MEASUREMENT_RATE, owned=False)
        orchestrator = DspOrchestrator(deps)

        recovered = await orchestrator.recover_stale_helper_samplerate(
            TARGET_RATE, reason="test"
        )

        self.assertTrue(recovered)
        self.assertEqual(deps.force_rate_writes, [TARGET_RATE])
        self.assertFalse(deps.force_rate_kwargs[0].get("allow_measurement_session"))

    async def test_owned_graph_is_never_repaired_without_the_session_lock(self):
        runtime = _FakeDspRuntime(MEASUREMENT_RATE)
        deps = _OrchestratorDeps(runtime, pinned_rate=TARGET_RATE, owned=True)
        orchestrator = DspOrchestrator(deps)

        recovered = await orchestrator.recover_stale_helper_samplerate(
            TARGET_RATE, reason="test"
        )

        self.assertFalse(recovered)
        self.assertEqual(runtime.sync_calls, [])

    async def test_no_rebuild_when_the_helper_already_matches(self):
        runtime = _FakeDspRuntime(TARGET_RATE)
        deps = _OrchestratorDeps(runtime, pinned_rate=TARGET_RATE)
        orchestrator = DspOrchestrator(deps)

        recovered = await orchestrator.recover_stale_helper_samplerate(
            TARGET_RATE, reason="test"
        )

        self.assertTrue(recovered)
        self.assertEqual(runtime.sync_calls, [])

    async def test_other_rate_mismatches_are_not_repaired_here(self):
        # A stopped helper is not this state: the recovery must stay narrow.
        runtime = _FakeDspRuntime(MEASUREMENT_RATE)
        runtime.snapshot = lambda: {"active": False, "config": {"sample_rate": MEASUREMENT_RATE}}
        deps = _OrchestratorDeps(runtime, pinned_rate=TARGET_RATE)
        orchestrator = DspOrchestrator(deps)

        recovered = await orchestrator.recover_stale_helper_samplerate(
            TARGET_RATE, reason="test"
        )

        self.assertFalse(recovered)
        self.assertEqual(runtime.sync_calls, [])


class BoundedReRepairTests(unittest.IsolatedAsyncioTestCase):
    """Layer 1b: one early alignment window is not the verdict.

    Rebuilding the helper releases its hold on the hardware sink, but the
    ALSA node can renegotiate late or need an explicit nudge.  The repair must
    keep going in bounded rounds, and it must also repair the state where the
    helper already runs at the target while the sink still lags behind it
    (that state used to return early and stay broken until a manual policy
    change).
    """

    async def test_release_recovers_when_the_sink_renegotiates_late(self):
        runtime = _FakeDspRuntime(MEASUREMENT_RATE)
        deps = _OrchestratorDeps(runtime, pinned_rate=TARGET_RATE, owned=True)
        deps.sink_follows_rebuild = False
        deps.sink_follows_trigger = False
        deps.nudges_before_sink_settles = 2  # only the second round moves it
        orchestrator = DspOrchestrator(deps)

        await orchestrator.sync_runtime_at_rate(TARGET_RATE, _rate_lock_held=True)

        self.assertEqual(runtime.helper_rate, TARGET_RATE)
        self.assertEqual(
            deps.sink_rate, TARGET_RATE,
            "the release must keep repairing until the hardware node settled",
        )
        self.assertGreaterEqual(deps.nudge_calls, 2)

    async def test_release_to_auto_rate_recovers_without_a_pin(self):
        # Auto policy: no force-rate pin is set, the leftover helper still runs
        # at the measurement rate and the sink only follows a nudge.
        runtime = _FakeDspRuntime(MEASUREMENT_RATE)
        deps = _OrchestratorDeps(runtime, pinned_rate=0, owned=True)
        deps.sink_follows_rebuild = False
        orchestrator = DspOrchestrator(deps)

        await orchestrator.sync_runtime_at_rate(TARGET_RATE, _rate_lock_held=True)

        self.assertEqual(runtime.helper_rate, TARGET_RATE)
        self.assertEqual(deps.sink_rate, TARGET_RATE)
        self.assertEqual(
            deps.pinned_rate, TARGET_RATE,
            "the bounded nudge re-establishes the restored rate on the sink",
        )

    async def test_helper_already_at_target_still_moves_the_lagging_sink(self):
        runtime = _FakeDspRuntime(TARGET_RATE)
        deps = _OrchestratorDeps(runtime, pinned_rate=TARGET_RATE, owned=True)
        deps.sink_follows_rebuild = False
        deps.sink_rate = MEASUREMENT_RATE
        orchestrator = DspOrchestrator(deps)

        recovered = await orchestrator.recover_stale_helper_samplerate(
            TARGET_RATE,
            reason="measurement-release",
            _rate_lock_held=True,
            allow_measurement_graph_owned=True,
        )

        self.assertTrue(recovered)
        self.assertEqual(runtime.sync_calls, [], "the helper already runs at the target")
        self.assertEqual(deps.sink_rate, TARGET_RATE)

    async def test_idle_sink_is_nudged_with_the_silent_trigger(self):
        # An idle hardware sink ignores the pin and the pulse; the silent
        # stream trigger is the documented way to move it.
        runtime = _FakeDspRuntime(TARGET_RATE)
        deps = _OrchestratorDeps(runtime, pinned_rate=TARGET_RATE, owned=True)
        deps.sink_follows_rebuild = False
        deps.sink_follows_nudge = False
        deps.sink_rate = MEASUREMENT_RATE
        orchestrator = DspOrchestrator(deps)

        recovered = await orchestrator.recover_stale_helper_samplerate(
            TARGET_RATE,
            reason="measurement-release",
            _rate_lock_held=True,
            allow_measurement_graph_owned=True,
        )

        self.assertTrue(recovered)
        self.assertEqual(deps.trigger_calls, 1)
        self.assertEqual(deps.sink_rate, TARGET_RATE)

    async def test_repair_reports_failure_and_keeps_the_rebuilt_helper(self):
        runtime = _FakeDspRuntime(MEASUREMENT_RATE)
        deps = _OrchestratorDeps(runtime, pinned_rate=TARGET_RATE, owned=True)
        deps.sink_follows_rebuild = False
        deps.sink_follows_nudge = False
        deps.sink_follows_trigger = False
        orchestrator = DspOrchestrator(deps)

        recovered = await orchestrator.recover_stale_helper_samplerate(
            TARGET_RATE,
            reason="measurement-release",
            _rate_lock_held=True,
            allow_measurement_graph_owned=True,
        )

        self.assertFalse(recovered)
        self.assertEqual(runtime.helper_rate, TARGET_RATE, "the rebuild is still applied")
        self.assertEqual(
            deps.sink_rate, MEASUREMENT_RATE,
            "an unrecoverable sink is reported, not silently accepted",
        )


class EstablishTargetRateRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """Layer 2: a later play start breaks the deadlock itself."""

    def _request(self) -> TransitionRequest:
        return TransitionRequest(operation="play", source="radio", target_rate=TARGET_RATE)

    def _deps(self, *, recover_result: bool) -> SimpleNamespace:
        return SimpleNamespace(
            get_samplerate_status=lambda: {
                "active_rate": MEASUREMENT_RATE,
                "force_rate": TARGET_RATE,
            },
            ensure_playback_samplerate_force=AsyncMock(return_value=False),
            trigger_idle_sink_renegotiation=AsyncMock(return_value=False),
            recover_stale_samplerate_helper=AsyncMock(return_value=recover_result),
        )

    async def test_stale_helper_is_rebuilt_before_the_transition_fails(self):
        deps = self._deps(recover_result=True)
        runtime = FxrouteTransitionRuntime(deps)

        await runtime.establish_target_rate(self._request())

        deps.recover_stale_samplerate_helper.assert_awaited_once()
        self.assertEqual(
            deps.recover_stale_samplerate_helper.await_args.args[0], TARGET_RATE
        )

    async def test_unrecoverable_rate_still_raises_with_the_real_diagnosis(self):
        deps = self._deps(recover_result=False)
        runtime = FxrouteTransitionRuntime(deps)

        with self.assertRaises(RuntimeError) as caught:
            await runtime.establish_target_rate(self._request())

        deps.recover_stale_samplerate_helper.assert_awaited_once()
        self.assertIn("target hardware rate did not settle", str(caught.exception))
        self.assertIn("active=48000", str(caught.exception))


class IdleLinkWatcherRepairTests(unittest.IsolatedAsyncioTestCase):
    """Layer 3: an idle broken graph is repaired, not preserved."""

    async def _run_one_tick(
        self, runtime: _FakeDspRuntime, *, pinned_rate: int, ticks: int = 1
    ) -> _OrchestratorDeps:
        deps = _OrchestratorDeps(runtime, pinned_rate=pinned_rate)
        sleeps = 0

        async def cancel_after_the_requested_ticks(_delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps <= ticks:
                return
            raise asyncio.CancelledError

        deps.sleep = cancel_after_the_requested_ticks
        deps.observe_playback_samplerate_drift = AsyncMock()
        orchestrator = DspOrchestrator(deps)
        task = asyncio.create_task(orchestrator.runtime_link_watch_loop())
        with self.assertRaises(asyncio.CancelledError):
            await task
        return deps

    async def test_idle_tick_rebuilds_a_helper_that_ignores_the_pin(self):
        runtime = _FakeDspRuntime(MEASUREMENT_RATE)
        await self._run_one_tick(runtime, pinned_rate=TARGET_RATE)
        self.assertEqual(runtime.helper_rate, TARGET_RATE)

    async def test_idle_tick_leaves_an_unpinned_graph_alone(self):
        runtime = _FakeDspRuntime(MEASUREMENT_RATE)
        deps = await self._run_one_tick(runtime, pinned_rate=0)
        self.assertEqual(runtime.helper_rate, MEASUREMENT_RATE)
        self.assertEqual(runtime.sync_calls, [])
        self.assertEqual(deps.force_rate_reads, 1, "the pin probe decides the tick")
        self.assertEqual(
            deps.samplerate_status_reads, 0,
            "an unpinned idle tick must not read the full samplerate status",
        )

    async def test_pinned_idle_tick_still_reads_the_full_status(self):
        # The pin-only probe only short-circuits the common unpinned tick; a
        # live pin still resolves through the authoritative full status.
        runtime = _FakeDspRuntime(MEASUREMENT_RATE)
        deps = await self._run_one_tick(runtime, pinned_rate=TARGET_RATE)
        self.assertEqual(deps.force_rate_reads, 1)
        self.assertGreaterEqual(
            deps.samplerate_status_reads, 1,
            "a live pin must still resolve through the authoritative status",
        )
        self.assertEqual(runtime.helper_rate, TARGET_RATE)

    async def test_idle_repair_is_rate_limited_between_ticks(self):
        # A rebuild that fails before the running helper is replaced must not
        # be retried on every 2 s watch tick.
        runtime = _FailingSyncRuntime(MEASUREMENT_RATE)
        await self._run_one_tick(runtime, pinned_rate=TARGET_RATE, ticks=2)
        self.assertEqual(runtime.attempts, 1)

    async def test_failed_idle_attempt_is_retried_after_the_cooldown(self):
        # The rate limit delays the next attempt, it never ends the repair.
        runtime = _FakeDspRuntime(MEASUREMENT_RATE)
        deps = _OrchestratorDeps(runtime, pinned_rate=TARGET_RATE)
        deps.sink_follows_rebuild = False
        deps.sink_follows_nudge = False
        deps.sink_follows_trigger = False
        orchestrator = DspOrchestrator(deps)

        with patch("dsp.orchestration.IDLE_STALE_HELPER_REPAIR_COOLDOWN_S", 0.0):
            await _run_watch_ticks(orchestrator, deps, ticks=1)
            self.assertEqual(
                deps.sink_rate, MEASUREMENT_RATE,
                "the first tick cannot move the sink",
            )
            self.assertEqual(runtime.helper_rate, TARGET_RATE, "the rebuild still ran")

            deps.sink_follows_nudge = True  # the hardware node settles later
            await _run_watch_ticks(orchestrator, deps, ticks=1)

        self.assertEqual(
            deps.sink_rate, TARGET_RATE,
            "a later tick must finish the repair the failed one could not",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
