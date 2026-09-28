# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for the qbzd journal-driven remote volume coupling.

Verifies the pickup contract (2026-08-28, live-verified on .104): the Qobuz
app pushes the *phone's media volume* as an absolute SetVolume right after
every Connect activation (deactivate/reactivate pushed 98% while the session
slider sat at ~50%), and gestures are slider drags on a scale whose zero
point is unrelated to the master. Therefore:

* the connect-time push only anchors the controller scale and never writes
  (the master must not jump to the pushed level),
* a gesture writes only when it crosses the current master level (pickup) —
  the write is bounded by the gesture step at the crossing,
* after the pickup the master tracks the controller value absolutely, so the
  phone display and the FXRoute display show the same number.

Contract history: the 2026-08-21 anchor+delta design never matched the two
displays; the 2026-08-28 absolute-adoption design matched them but jumped the
master to the pushed 98% at connect (live-reproduced). Pickup keeps both
properties.

No journald or subprocess is required: the parser, the debouncing translator
and the async watch loop are exercised with canned input.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playback.qbzd_volume_watch import (
    ENGINE_IDLE_POLL_SECONDS,
    ENGINE_PIN_RETRY_SECONDS,
    ENGINE_PLAYING_POLL_SECONDS,
    QobuzRemoteVolumeTranslator,
    QobuzVolumeWatch,
    QobuzVolumeWatchDependencies,
    is_fork_session_boundary,
    is_session_activation,
    is_session_deactivation,
    is_software_volume_apply,
    parse_engine_volume,
    parse_ignored_volume,
)

_LOCKED_LINE = (
    "[QConnect] volume_mode=locked: ignoring remote SetVolume(0.450); player stays at 100%"
)
_ACTIVATION_LINE = (
    "[QConnect] <-- Inbound renderer command: MESSAGE_TYPE_SRVR_RNDR_SET_ACTIVE "
    'payload={"active":true}'
)
_DEACTIVATION_LINE = (
    "[QConnect] <-- Inbound renderer command: MESSAGE_TYPE_SRVR_RNDR_SET_ACTIVE "
    'payload={"active":false}'
)


# Live fork journal lines (qbzd 0.1.9, .130).
_FORK_JOIN_LINE = (
    "[2026-09-27T23:39:08.310Z INFO  qbzd::qconnect] [qbzd/qconnect] "
    "Renderer join complete — visible to other devices"
)
_FORK_WS_DOWN_LINE = (
    "[2026-09-27T23:40:00.000Z WARN  qbzd::qconnect] [qbzd/qconnect] "
    "WebSocket disconnected, resetting renderer_joined"
)
_FORK_DISCONNECTED_LINE = (
    "[2026-09-27T23:40:00.000Z WARN  qbzd::qconnect] [qbzd/qconnect] Disconnected"
)
_FORK_ENGINE_VOLUME_LINE = (
    "[2026-09-28T00:02:49.268Z INFO  qbz_player::player] Audio thread: volume set to 0.33"
)


def _locked_line(percent: float) -> str:
    return (
        f"[QConnect] volume_mode=locked: ignoring remote SetVolume({percent}); "
        "player stays at 100%"
    )


class ParseTests(unittest.TestCase):
    def test_journal_query_resolves_unit_across_split_files(self):
        # --user-unit (not --user -u) is the only form that follows the
        # unit's entries when user lines land outside user-UID.journal
        # files (live-verified: --user -u follows a stale file forever).
        # Cursor polling (not -f streaming) additionally survives rotation.
        from playback.qbzd_volume_watch import JOURNALCTL_QUERY_BASE
        self.assertIn("--user-unit=qbzd.service", JOURNALCTL_QUERY_BASE)
        self.assertNotIn("--user", JOURNALCTL_QUERY_BASE)
        self.assertNotIn("-u", JOURNALCTL_QUERY_BASE)
        self.assertNotIn("-f", JOURNALCTL_QUERY_BASE)

    def test_locked_line_yields_percent(self):
        self.assertEqual(parse_ignored_volume(_LOCKED_LINE), 45)
        self.assertEqual(parse_ignored_volume(_locked_line("1.000")), 100)
        self.assertEqual(parse_ignored_volume(_locked_line("0.000")), 0)
        self.assertEqual(parse_ignored_volume(_locked_line("0.260")), 26)
        self.assertEqual(parse_ignored_volume(_locked_line("0.980")), 98)

    def test_timestamped_journal_prefix_is_accepted(self):
        line = (
            "Aug 28 21:36:38 fxroute qbzd[1079]: 2026-08-28 21:36:38.599 INFO  "
            "qbzd::qconnect::engine " + _LOCKED_LINE
        )
        self.assertEqual(parse_ignored_volume(line), 45)

    def test_non_volume_lines_yield_none(self):
        self.assertIsNone(parse_ignored_volume(None))
        self.assertIsNone(parse_ignored_volume(""))
        self.assertIsNone(parse_ignored_volume("Renderer command applied: SetVolume {"))
        self.assertIsNone(parse_ignored_volume("completely unrelated"))
        self.assertIsNone(
            parse_ignored_volume("volume_mode=locked: ignoring remote SetVolume(150)")
        )

    def test_session_activation_detection(self):
        self.assertTrue(is_session_activation(_ACTIVATION_LINE))
        self.assertFalse(is_session_activation(_DEACTIVATION_LINE))
        self.assertFalse(is_session_activation(_LOCKED_LINE))
        self.assertFalse(is_session_activation(None))
        self.assertTrue(is_session_deactivation(_DEACTIVATION_LINE))
        self.assertFalse(is_session_deactivation(_ACTIVATION_LINE))

    def test_software_mode_line_detection(self):
        self.assertTrue(
            is_software_volume_apply(
                "[QConnect] Renderer command applied: SetVolume { volume: Some(69) }"
            )
        )
        self.assertFalse(is_software_volume_apply(_LOCKED_LINE))
        self.assertFalse(is_software_volume_apply(None))


class ForkParseTests(unittest.TestCase):
    def test_fork_session_boundaries_are_recognized(self):
        for line in (_FORK_JOIN_LINE, _FORK_WS_DOWN_LINE, _FORK_DISCONNECTED_LINE):
            self.assertTrue(is_fork_session_boundary(line), line)

    def test_fork_non_boundary_lines_are_ignored(self):
        for line in (
            _FORK_ENGINE_VOLUME_LINE,
            "[qbzd/qconnect] Connected to Qobuz servers",
            "[qbzd/qconnect] Renderer join with session_uuid=c5ad0b99",
            _ACTIVATION_LINE,
            "",
            None,
        ):
            self.assertFalse(is_fork_session_boundary(line), line)
        # The engine line is never a volume intent on its own: FXRoute's own
        # unity pin logs the same line.
        self.assertIsNone(parse_ignored_volume(_FORK_ENGINE_VOLUME_LINE))

    def test_engine_volume_parse(self):
        self.assertEqual(parse_engine_volume(0.33000001311302185), 33)
        self.assertEqual(parse_engine_volume(1.0), 100)
        self.assertEqual(parse_engine_volume(0), 0)
        self.assertEqual(parse_engine_volume(1.7), 100)
        for bad in (None, "0.5", True):
            self.assertIsNone(parse_engine_volume(bad))


class EngineUnityGuardTests(unittest.IsolatedAsyncioTestCase):
    """Fork backend: remote SetVolume lands on the qbzd engine gain."""

    def _guard(self, applied, readings, *, master=37, active=True):
        pins = []
        docs = iter(readings)

        async def read():
            return next(docs)

        async def pin():
            pins.append(True)

        async def apply_value(value):
            applied.append(value)

        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: active,
                apply_volume_value=apply_value,
                current_master=lambda: master,
                pin_unity=pin,
                read_engine_playback=read,
            ),
            debounce_seconds=0.0,
        )
        return watch, pins

    async def _passes(self, watch, count):
        delays = []
        for _ in range(count):
            delays.append(await watch.poll_engine_once())
            drain = watch._drain_task
            if drain is not None:
                await drain
        return delays

    async def test_deviating_engine_volume_is_pinned_and_only_anchors(self):
        applied = []
        watch, pins = self._guard(applied, [{"state": "Playing", "volume": 0.98}])
        delays = await self._passes(watch, 1)
        self.assertEqual(pins, [True])
        # Connect-time/first value: anchors the controller scale, no write.
        self.assertEqual(applied, [])
        self.assertEqual(delays, [ENGINE_PLAYING_POLL_SECONDS])

    async def test_unity_engine_volume_is_left_alone(self):
        applied = []
        watch, pins = self._guard(applied, [
            {"state": "Paused", "volume": 1.0},
            {"state": "Playing", "volume": 1.0},
        ])
        delays = await self._passes(watch, 2)
        self.assertEqual(pins, [])
        self.assertEqual(applied, [])
        self.assertEqual(delays, [ENGINE_IDLE_POLL_SECONDS, ENGINE_PLAYING_POLL_SECONDS])

    async def test_phone_drag_picks_up_at_the_master_and_tracks(self):
        applied = []
        playing = lambda volume: {"state": "Playing", "volume": volume}
        # Every remote step is read once, then the pinned 1.0.
        watch, pins = self._guard(applied, [
            playing(0.98), playing(1.0),
            playing(0.60), playing(1.0),
            playing(0.38), playing(1.0),
            playing(0.36), playing(1.0),
            playing(0.30), playing(1.0),
        ], master=37)
        await self._passes(watch, 10)
        self.assertEqual(len(pins), 5)
        # 98 anchors; 60 and 38 stay above the master; 36 crosses 37 and
        # picks up; 30 tracks absolutely.
        self.assertEqual(applied, [36, 30])

    async def test_value_stuck_after_failed_pin_is_one_intent(self):
        applied = []
        watch, pins = self._guard(applied, [
            {"state": "Playing", "volume": 0.5},
            {"state": "Playing", "volume": 0.5},
        ], master=50)
        await self._passes(watch, 2)
        # The pickup sees the value once: a second submit of 50 at master 50
        # would count as a crossing and write.
        self.assertEqual(applied, [])
        self.assertEqual(len(pins), 1)

    async def test_stuck_value_repins_at_a_bounded_rate(self):
        applied = []
        watch, pins = self._guard(applied, [{"state": "Playing", "volume": 0.5}] * 25)
        clock = {"now": 100.0}
        with mock.patch("playback.qbzd_volume_watch.time.monotonic", side_effect=lambda: clock["now"]):
            for _ in range(20):  # one second of 50 ms passes
                await watch.poll_engine_once()
                clock["now"] += ENGINE_PLAYING_POLL_SECONDS
            self.assertEqual(len(pins), 1)
            clock["now"] = 100.0 + ENGINE_PIN_RETRY_SECONDS  # retry window elapsed
            await watch.poll_engine_once()
        self.assertEqual(len(pins), 2)

    async def test_new_remote_value_is_pinned_at_once(self):
        applied = []
        watch, pins = self._guard(applied, [
            {"state": "Playing", "volume": 0.5},
            {"state": "Playing", "volume": 0.45},
        ])
        with mock.patch("playback.qbzd_volume_watch.time.monotonic", return_value=5.0):
            await self._passes(watch, 2)
        self.assertEqual(len(pins), 2)

    async def test_playing_state_is_read_through_the_provider_normalizer(self):
        applied = []
        watch, _pins = self._guard(applied, [
            {"state": "playing", "volume": 1.0},
            {"state": "Loading", "volume": 1.0},
            {"state": "Paused", "is_playing": True, "volume": 1.0},
            {"state": "paused", "volume": 1.0},
        ])
        delays = await self._passes(watch, 4)
        self.assertEqual(delays, [ENGINE_PLAYING_POLL_SECONDS] * 3 + [ENGINE_IDLE_POLL_SECONDS])

    async def test_unreachable_daemon_polls_idle_without_pin(self):
        applied = []
        watch, pins = self._guard(applied, [None])
        self.assertEqual(await self._passes(watch, 1), [ENGINE_IDLE_POLL_SECONDS])
        self.assertEqual(pins, [])

    async def test_non_owner_is_pinned_but_never_drives_the_master(self):
        applied = []
        watch, pins = self._guard(applied, [
            {"state": "Playing", "volume": 0.40},
            {"state": "Playing", "volume": 0.37},
        ], master=37, active=False)
        await self._passes(watch, 2)
        self.assertEqual(len(pins), 2)
        self.assertEqual(applied, [])

    async def test_watch_loop_runs_the_guard_with_a_pin_writer(self):
        applied = []
        watch, pins = self._guard(applied, [{"state": "Playing", "volume": 0.5}] + [
            {"state": "Playing", "volume": 1.0}
        ] * 200)

        async def hang(_cursor):
            await asyncio.Event().wait()

        with mock.patch.object(watch, "_poll_journal_lines", new=hang), \
                mock.patch.object(watch, "_bootstrap_device_state", new=mock.AsyncMock()):
            task = asyncio.create_task(watch.run_watch_loop())
            await asyncio.sleep(0.2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(pins, [True])
        self.assertIsNone(watch._engine_guard_task)

    async def test_fork_rejoin_rearms_pickup_without_touching_selection(self):
        applied = []
        device_events = []

        async def apply_value(value):
            applied.append(value)

        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: 37,
                on_device_active=lambda value: device_events.append(value),
            ),
            debounce_seconds=0.0,
        )
        translator = watch._translator
        translator.submit(40)
        translator.submit(36)
        await translator.flush()
        self.assertTrue(translator.picked_up)
        batches = iter([([_FORK_JOIN_LINE], "c1")])

        async def poll(_cursor):
            try:
                return next(batches)
            except StopIteration:
                await asyncio.Event().wait()

        with mock.patch.object(watch, "_poll_journal_lines", new=poll), \
                mock.patch.object(watch, "_bootstrap_device_state", new=mock.AsyncMock()):
            try:
                await asyncio.wait_for(watch.run_watch_loop(), timeout=0.3)
            except asyncio.TimeoutError:
                pass
        # The rejoin reports 100% to the server: the next controller value
        # only anchors again, and selection tracking stays untouched.
        self.assertFalse(translator.picked_up)
        self.assertFalse(translator.submit(99))
        self.assertEqual(applied, [36])
        self.assertEqual(device_events, [])


class PickupTranslatorTests(unittest.IsolatedAsyncioTestCase):
    """The pickup contract: anchor the push, adopt on crossing, track 1:1."""

    def _translator(self, applied, active=True, master=37):
        async def apply_value(value):
            applied.append(value)

        return QobuzRemoteVolumeTranslator(
            is_active=lambda: active,
            apply_volume_value=apply_value,
            current_master=lambda: master,
        )

    async def test_connect_push_anchors_and_never_writes(self):
        # The push carries the phone's media volume (98): adopting it hijacked
        # the master (live-reproduced 37 -> 98). It must only anchor.
        applied = []
        translator = self._translator(applied, master=37)
        self.assertFalse(translator.submit(98))
        await translator.flush()
        self.assertEqual(applied, [])
        self.assertFalse(translator.picked_up)

    async def test_gesture_far_from_master_is_ignored_until_crossing(self):
        # The user drags down from the pushed 98; the master sits at 37. Every
        # value above 37 is on the far side of the master: no write, ever.
        applied = []
        translator = self._translator(applied, master=37)
        translator.submit(98)
        for value in (97, 96, 92, 88, 84, 80, 76, 72, 68, 63, 59, 55, 51, 47, 43, 39, 38):
            translator.submit(value)
        await translator.flush()
        self.assertEqual(applied, [])
        self.assertFalse(translator.picked_up)

    async def test_crossing_picks_up_and_tracks_absolutely(self):
        applied = []
        translator = self._translator(applied, master=37)
        translator.submit(98)
        translator.submit(38)
        translator.submit(37)   # lands on the master level: pickup
        await translator.flush()
        translator.submit(36)   # picked up: 1:1 tracking
        await translator.flush()
        translator.submit(30)
        await translator.flush()
        self.assertEqual(applied, [37, 36, 30])
        self.assertTrue(translator.picked_up)

    async def test_upward_catch_when_controller_starts_below_master(self):
        # Phone at ~1%, master at 13%: dragging up must stay silent until the
        # gesture reaches 13, then catch and track 1:1 ("catched es").
        applied = []
        translator = self._translator(applied, master=13)
        translator.submit(1)
        for value in (2, 6, 10, 12):
            translator.submit(value)
        await translator.flush()
        self.assertEqual(applied, [])
        translator.submit(13)
        await translator.flush()
        translator.submit(14)
        await translator.flush()
        translator.submit(20)
        await translator.flush()
        self.assertEqual(applied, [13, 14, 20])

    async def test_value_landing_exactly_on_master_picks_up_without_jump(self):
        applied = []
        translator = self._translator(applied, master=13)
        translator.submit(1)
        translator.submit(13)
        await translator.flush()
        self.assertEqual(applied, [13])

    async def test_pickup_write_is_bounded_by_the_crossing_step(self):
        # A fast burst that jumps across the master lands on its final value:
        # the write is the observed controller value, never the pushed scale.
        applied = []
        translator = self._translator(applied, master=37)
        translator.submit(98)
        translator.submit(30)   # single observation crossing 37
        await translator.flush()
        self.assertEqual(applied, [30])

    async def test_identical_repeat_after_pickup_produces_no_write(self):
        applied = []
        translator = self._translator(applied, master=13)
        translator.submit(1)
        translator.submit(13)
        await translator.flush()
        self.assertFalse(translator.submit(13))
        await translator.flush()
        self.assertEqual(applied, [13])

    async def test_fresh_session_requires_pickup_again(self):
        # After a reactivation the app re-pushes (possibly the same value):
        # the pickup must re-arm, nothing may write from the push alone.
        applied = []
        translator = self._translator(applied, master=13)
        translator.submit(1)
        translator.submit(13)
        await translator.flush()
        translator.observe_activation()
        translator.submit(13)
        await translator.flush()
        self.assertEqual(applied, [13])
        self.assertFalse(translator.picked_up)

    async def test_inactive_owner_drops_intent(self):
        applied = []
        translator = self._translator(applied, active=False, master=13)
        self.assertFalse(translator.submit(50))
        self.assertIsNone(translator.pending)
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_owner_change_inside_debounce_discards_pending(self):
        applied = []
        translator = self._translator(applied, active=True, master=13)
        translator.submit(1)
        translator.submit(13)
        translator.is_active = lambda: False
        await translator.flush()
        self.assertEqual(applied, [])
        self.assertIsNone(translator.pending)

    async def test_failed_write_keeps_value_for_retry(self):
        applied = []
        attempts = 0

        async def apply_value(value):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary volume failure")
            applied.append(value)

        translator = QobuzRemoteVolumeTranslator(
            is_active=lambda: True,
            apply_volume_value=apply_value,
            current_master=lambda: 13,
        )
        translator.submit(1)
        translator.submit(13)

        with self.assertRaisesRegex(RuntimeError, "temporary volume failure"):
            await translator.flush()
        self.assertEqual(translator.pending, 13)

        await translator.flush()
        self.assertEqual(applied, [13])

    async def test_owner_loss_cancels_inflight_write(self):
        applied = []
        write_started = asyncio.Event()
        write_cancelled = asyncio.Event()

        async def apply_value(value):
            write_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                write_cancelled.set()
                raise
            applied.append(value)

        active = True
        translator = QobuzRemoteVolumeTranslator(
            is_active=lambda: active,
            apply_volume_value=apply_value,
            current_master=lambda: 13,
        )
        translator.submit(1)
        translator.submit(13)
        flush_task = asyncio.create_task(translator.flush())
        await write_started.wait()

        active = False
        await asyncio.wait_for(write_cancelled.wait(), timeout=1.0)
        await flush_task

        self.assertEqual(applied, [])
        self.assertIsNone(translator.pending)


class WatchLoopTests(unittest.IsolatedAsyncioTestCase):
    def _fake_poll(self, batches):
        """Script _poll_journal_lines batches; then hang for wait_for timeout."""
        calls = []
        it = iter(batches)

        async def poll(cursor):
            calls.append(cursor)
            try:
                return next(it)
            except StopIteration:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        return poll, calls

    def _make_watch(self, applied, master=37, active=True, device_events=None):
        async def apply_value(value):
            applied.append(value)

        kwargs = {}
        if device_events is not None:
            kwargs["on_device_active"] = lambda value: device_events.append(value)
        return QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: active,
                apply_volume_value=apply_value,
                current_master=lambda: master,
                **kwargs,
            ),
            debounce_seconds=0.0,
        )

    async def _run_loop_until_timeout(self, watch, timeout=3.0):
        try:
            await asyncio.wait_for(watch.run_watch_loop(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(0.05)

    async def test_loop_ignores_far_side_gestures_and_picks_up_on_crossing(self):
        applied = []
        watch = self._make_watch(applied)
        # Lines arrive spread across polls (as in live operation); each poll
        # drains before the next, so the pickup write and the absolute
        # tracking writes are all observable.
        poll, _calls = self._fake_poll([
            ([_locked_line("0.980"), _locked_line("0.900"), _locked_line("0.600"),
              _locked_line("0.370")], "c1"),
            ([_locked_line("0.360")], "c2"),
            ([_locked_line("0.290")], "c3"),
        ])
        with mock.patch.object(watch, "_poll_journal_lines", new=poll):
            await self._run_loop_until_timeout(watch)
        self.assertEqual(applied, [37, 36, 29])

    async def test_value_seen_during_master_write_is_drained_without_next_event(self):
        applied = []
        write_started = asyncio.Event()
        release_write = asyncio.Event()

        async def apply_value(value):
            applied.append(value)
            if len(applied) == 1:
                write_started.set()
                await release_write.wait()

        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: 37,
            ),
            debounce_seconds=0.0,
        )

        watch._translator.submit(98)   # push anchors
        watch._translator.submit(37)   # crossing: pickup, pending 37
        watch._schedule_drain()
        drain_task = watch._drain_task
        await write_started.wait()

        watch._translator.submit(30)   # final value arrives mid-write
        watch._schedule_drain()  # the active drain must retain this pending value
        release_write.set()
        await drain_task

        self.assertEqual(applied, [37, 30])

    async def test_failed_master_write_is_retried(self):
        applied = []
        attempts = 0

        async def apply_value(value):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary volume failure")
            applied.append(value)

        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: 13,
            ),
            debounce_seconds=0.0,
        )

        watch._translator.submit(1)
        watch._translator.submit(13)
        watch._schedule_drain()
        await watch._drain_task

        self.assertEqual(applied, [13])

    async def test_owner_loss_cancels_inflight_master_write(self):
        applied = []
        write_started = asyncio.Event()
        write_cancelled = asyncio.Event()

        async def apply_value(value):
            write_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                write_cancelled.set()
                raise
            applied.append(value)

        active = True
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: active,
                apply_volume_value=apply_value,
                current_master=lambda: 13,
            ),
            debounce_seconds=0.0,
        )
        watch._translator.submit(1)
        watch._translator.submit(13)
        watch._schedule_drain()
        drain_task = watch._drain_task
        await write_started.wait()

        active = False
        await asyncio.wait_for(write_cancelled.wait(), timeout=1.0)
        await drain_task

        self.assertEqual(applied, [])
        self.assertIsNone(watch._translator.pending)

    async def test_stop_resets_translator_after_cancelling_drain(self):
        write_started = asyncio.Event()

        async def apply_value(_value):
            write_started.set()
            await asyncio.Event().wait()

        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: 13,
            ),
            debounce_seconds=0.0,
        )
        watch._translator.submit(1)
        watch._translator.submit(13)
        watch._schedule_drain()
        await write_started.wait()

        await watch.stop()

        self.assertIsNone(watch._translator.pending)

    async def test_loop_advances_cursor_across_polls_without_replay(self):
        applied = []
        watch = self._make_watch(applied, master=45)
        poll, calls = self._fake_poll([
            ([_LOCKED_LINE], "c1"),
            ([_locked_line("0.440")], "c2"),
        ])
        with mock.patch.object(watch, "_poll_journal_lines", new=poll):
            await self._run_loop_until_timeout(watch)
        # The cursor chains polls: 45 anchors; 44 lands on the master
        # (45) within the same crossing window and picks up.
        self.assertEqual(applied, [44])
        # Polls chain cursors instead of replaying history.
        self.assertEqual(calls[0], None)
        self.assertIn("c1", calls)

    async def test_loop_survives_poll_failure_with_backoff(self):
        applied = []
        watch = self._make_watch(applied, master=37)
        calls = []
        batches = [([_locked_line("0.980"), _locked_line("0.370")], "c1")]
        it = iter(batches)

        async def flaky_poll(cursor):
            calls.append(cursor)
            if len(calls) == 1:
                raise RuntimeError("journal temporarily unavailable")
            return next(it)

        with mock.patch.object(watch, "_poll_journal_lines", new=flaky_poll):
            await self._run_loop_until_timeout(watch, timeout=4.0)
        # 98 anchors, 37 lands on the master: pickup despite the failed poll.
        self.assertEqual(applied, [37])
        self.assertGreaterEqual(len(calls), 2)

    async def test_poll_returns_lines_and_advances_cursor(self):
        seen_args = []

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"line one\nline two\n-- cursor: CURSOR-9\n", b"")

            async def wait(self):
                return 0

        async def spawn(*args, **kwargs):
            seen_args.append(args)
            return FakeProc()

        watch = self._make_watch([])
        with mock.patch("asyncio.create_subprocess_exec", new=spawn):
            lines, cursor = await watch._poll_journal_lines("CURSOR-0")
        self.assertEqual(lines, ["line one", "line two"])
        self.assertEqual(cursor, "CURSOR-9")
        flat = " ".join(seen_args[0])
        self.assertIn("--after-cursor", flat)
        self.assertIn("CURSOR-0", flat)
        self.assertNotIn("--lines=0", flat)

    async def test_poll_anchors_without_replay_and_resets_on_failure(self):
        seen_args = []

        class FakeProc:
            def __init__(self, returncode, output):
                self.returncode = returncode
                self._output = output

            async def communicate(self):
                return (self._output, b"")

            async def wait(self):
                return self.returncode

        async def spawn_ok(*args, **kwargs):
            seen_args.append(args)
            return FakeProc(0, b"-- cursor: C-ANCHOR\n")

        watch = self._make_watch([])
        with mock.patch("asyncio.create_subprocess_exec", new=spawn_ok):
            lines, cursor = await watch._poll_journal_lines(None)
        self.assertEqual(lines, [])
        self.assertEqual(cursor, "C-ANCHOR")
        self.assertIn("--lines=0", " ".join(seen_args[0]))

        async def spawn_bad(*args, **kwargs):
            return FakeProc(1, b"garbage without cursor\n")

        with mock.patch("asyncio.create_subprocess_exec", new=spawn_bad):
            lines, cursor = await watch._poll_journal_lines("C-OLD")
        self.assertEqual(lines, [])
        self.assertIsNone(cursor)

    async def test_loop_ignores_intents_while_qobuz_does_not_own(self):
        applied = []
        watch = self._make_watch(applied, master=37, active=False)
        poll, _calls = self._fake_poll([([_LOCKED_LINE], "c1")])
        with mock.patch.object(watch, "_poll_journal_lines", new=poll):
            await self._run_loop_until_timeout(watch)
        self.assertEqual(applied, [])

    async def test_loop_warns_once_on_software_mode_lines(self):
        applied = []
        watch = self._make_watch(applied, master=37)
        software_line = "[QConnect] Renderer command applied: SetVolume { volume: Some(69) }"
        poll, _calls = self._fake_poll([([software_line, software_line], "c1")])
        with mock.patch.object(watch, "_poll_journal_lines", new=poll):
            with mock.patch.object(watch, "_warn_software_mode_once") as warn:
                await self._run_loop_until_timeout(watch)
        # The first apply line only arms the pair check; the second consecutive
        # apply (no ignore line in between) proves software mode.
        self.assertEqual(warn.call_count, 1)
        self.assertEqual(applied, [])

    async def test_loop_notifies_device_state_on_activation_lines(self):
        applied = []
        device_events = []
        watch = self._make_watch(applied, master=37, device_events=device_events)
        poll, _calls = self._fake_poll([([_ACTIVATION_LINE, _locked_line("0.500")], "c1")])
        with mock.patch.object(watch, "_poll_journal_lines", new=poll):
            await self._run_loop_until_timeout(watch)
        # Selection tracking follows the renderer commands; the app's
        # post-activation push only anchors (master 37, push 50: no crossing).
        self.assertEqual(device_events, [True])
        self.assertEqual(applied, [])

    async def test_bootstrap_recovers_last_selection_state(self):
        seen = []
        applied = []

        async def apply_value(value):
            applied.append(value)

        history = "\n".join([
            _ACTIVATION_LINE,
            "unrelated line",
            _DEACTIVATION_LINE,   # last evidence: device deselected
        ])

        class HistoryProc:
            returncode = 0

            async def communicate(self):
                return (history.encode(), b"")

        async def spawn(*args, **kwargs):
            return HistoryProc()

        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: 37,
                on_device_active=lambda value: seen.append(value),
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("asyncio.create_subprocess_exec", new=spawn), mock.patch(
            "streaming.qobuz.backend.qbzd_installed", return_value=True
        ):
            await watch._bootstrap_device_state()
        self.assertEqual(seen, [False])
        self.assertEqual(applied, [])

    async def test_bootstrap_without_history_reports_nothing(self):
        seen = []
        applied = []

        async def apply_value(value):
            applied.append(value)

        class EmptyProc:
            returncode = 0

            async def communicate(self):
                return (b"unrelated line\n", b"")

        async def spawn(*args, **kwargs):
            return EmptyProc()

        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: 37,
                on_device_active=lambda value: seen.append(value),
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("asyncio.create_subprocess_exec", new=spawn), mock.patch(
            "streaming.qobuz.backend.qbzd_installed", return_value=True
        ):
            await watch._bootstrap_device_state()
        self.assertEqual(seen, [])
        self.assertEqual(applied, [])


class RemoteVolumeRegressionTests(unittest.IsolatedAsyncioTestCase):
    """The live .104 trace from 2026-08-28 21:36, replayed through the loop.

    deactivate/reactivate pushed the phone's media volume (98%) while the
    master sat far below; the user's rapid down-drag then started at the
    pushed level and absolute adoption jumped the master to ~98 before it
    followed back down. The pickup contract must keep the master silent
    until the drag reaches the master level and then track 1:1.
    """

    def _watch(self, applied, lines, master=37):
        async def apply_value(value):
            applied.append(value)

        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: master,
            ),
            debounce_seconds=0.0,
        )
        return watch, [ (lines, "c1") ]

    async def _run(self, watch, batches):
        async def poll(cursor):
            if batches:
                return batches.pop(0)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        with mock.patch.object(watch, "_poll_journal_lines", new=poll):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(watch.run_watch_loop(), timeout=3.0)
        await asyncio.sleep(0.05)

    async def test_connect_push_and_down_drag_never_jump_the_master(self):
        applied = []
        watch, proc = self._watch(applied, [
            _ACTIVATION_LINE,
            _locked_line("0.980"),   # app pushes the phone's media volume
            _locked_line("0.970"),
            _locked_line("0.920"),
            _locked_line("0.880"),
            _locked_line("0.800"),
            _locked_line("0.720"),
            _locked_line("0.630"),
            _locked_line("0.550"),
            _locked_line("0.470"),
            _locked_line("0.390"),
            _locked_line("0.370"),   # drag reaches the master level: pickup
            _locked_line("0.360"),   # picked up: track the drag
        ], master=37)
        await self._run(watch, proc)
        # The burst collapses into one debounced write at the drag's final
        # value; nothing above the master was ever written.
        self.assertEqual(applied, [36])

    async def test_upward_drag_catches_at_the_master_level(self):
        applied = []
        watch, proc = self._watch(applied, [
            _ACTIVATION_LINE,
            _locked_line("0.010"),   # phone media volume at 1%
            _locked_line("0.060"),
            _locked_line("0.120"),
            _locked_line("0.130"),   # reaches the master at 13: pickup
            _locked_line("0.200"),   # catched: 1:1 tracking
        ], master=13)
        await self._run(watch, proc)
        self.assertEqual(applied[-1], 20)

    async def test_loudness_work_point_is_never_touched(self):
        # The bridge's only output is the canonical master writer; it has no
        # handle on loudness state by construction. Pin the dependency
        # surface: exactly is_active + apply_volume_value + current_master +
        # the optional device-selection notifier + the fork unity guard's
        # engine reader and qbzd unity pin, nothing else.
        self.assertEqual(
            set(QobuzVolumeWatchDependencies.__dataclass_fields__.keys()),
            {"is_active", "apply_volume_value", "current_master", "on_device_active",
             "pin_unity", "read_engine_playback"},
        )
        applied = []
        watch, proc = self._watch(applied, [
            _locked_line("0.500"),
            _locked_line("0.480"),
        ], master=48)
        await self._run(watch, proc)
        self.assertEqual(applied, [48])


if __name__ == "__main__":
    unittest.main()
