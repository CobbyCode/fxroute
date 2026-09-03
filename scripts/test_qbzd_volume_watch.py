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
    QobuzRemoteVolumeTranslator,
    QobuzVolumeWatch,
    QobuzVolumeWatchDependencies,
    is_session_activation,
    is_session_deactivation,
    is_software_volume_apply,
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


def _locked_line(percent: float) -> str:
    return (
        f"[QConnect] volume_mode=locked: ignoring remote SetVolume({percent}); "
        "player stays at 100%"
    )


def _fake_proc_static(lines):
    proc = mock.Mock()
    proc.returncode = None
    results = [line.encode() for line in lines] + [b""]

    async def readline():
        if results:
            return results.pop(0)
        raise StopAsyncIteration

    proc.stdout = mock.Mock()
    proc.stdout.readline = readline
    return proc


class ParseTests(unittest.TestCase):
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
    def _fake_proc(self, lines, gap=0.02):
        proc = mock.Mock()
        proc.returncode = None

        results = [line.encode() for line in lines] + [b""]

        async def readline():
            if gap > 0:
                await asyncio.sleep(gap)
            if results:
                return results.pop(0)
            raise StopAsyncIteration

        async def communicate():
            # The one-shot bootstrap scan reads nothing here; history tests
            # build their own proc with a scripted communicate().
            return (b"", b"")

        async def wait():
            return 0

        proc.stdout = mock.Mock()
        proc.stdout.readline = readline
        proc.communicate = communicate
        proc.wait = wait
        return proc

    def _fake_spawn(self, proc):
        async def spawn(*args, **kwargs):
            return proc

        return spawn

    def _real_sleep_patch(self, watch):
        # Keep real suspension so the event loop can still fire the wait_for
        # timeout; patched AsyncMocks would starve it (no await yields).
        return mock.patch.object(watch, "_sleep", new=lambda delay: asyncio.sleep(delay))

    async def test_loop_ignores_far_side_gestures_and_picks_up_on_crossing(self):
        applied = []

        async def apply_value(value):
            applied.append(value)

        proc = self._fake_proc(lines=[
            _locked_line("0.980"),   # connect push (phone media volume)
            _locked_line("0.900"),
            _locked_line("0.600"),
            _locked_line("0.370"),   # lands on the master: pickup
            _locked_line("0.360"),   # picked up: absolute tracking
            _locked_line("0.290"),
        ])
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: 37,
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("asyncio.create_subprocess_exec", new=self._fake_spawn(proc)):
            with self._real_sleep_patch(watch):
                try:
                    await asyncio.wait_for(watch.run_watch_loop(), timeout=3.0)
                except asyncio.TimeoutError:  # loop waits on backoff forever after EOF
                    pass
            await asyncio.sleep(0.05)
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

    async def test_loop_respawns_after_eof_keeping_session_state(self):
        applied = []
        spawned = []

        async def apply_value(value):
            applied.append(value)

        first = self._fake_proc(lines=[_LOCKED_LINE])
        second = self._fake_proc(lines=[_locked_line("0.440")])

        def factory():
            return first if not spawned else second

        async def spawn(*args, **kwargs):
            chosen = factory()
            spawned.append(args)
            return chosen

        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: 45,
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("asyncio.create_subprocess_exec", new=spawn):
            with self._real_sleep_patch(watch):
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(watch.run_watch_loop(), timeout=3.0)
        await asyncio.sleep(0.05)
        # The respawn is transparent: 45 anchors; 44 lands on the master
        # (45) within the same crossing window and picks up.
        self.assertEqual(applied, [44])
        # journalctl was (re)spawned after the first EOF, with the tail command.
        self.assertGreaterEqual(len(spawned), 2)
        self.assertEqual(spawned[0][3], "qbzd.service")

    async def test_loop_ignores_intents_while_qobuz_does_not_own(self):
        applied = []

        async def apply_value(value):
            applied.append(value)

        proc = self._fake_proc(lines=[_LOCKED_LINE])
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: False,
                apply_volume_value=apply_value,
                current_master=lambda: 37,
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("asyncio.create_subprocess_exec", new=self._fake_spawn(proc)):
            with self._real_sleep_patch(watch):
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(watch.run_watch_loop(), timeout=3.0)
        await asyncio.sleep(0.05)
        self.assertEqual(applied, [])

    async def test_loop_warns_once_on_software_mode_lines(self):
        applied = []

        async def apply_value(value):
            applied.append(value)

        software_line = "[QConnect] Renderer command applied: SetVolume { volume: Some(69) }"
        proc = self._fake_proc(lines=[software_line, software_line])
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: 37,
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("asyncio.create_subprocess_exec", new=self._fake_spawn(proc)):
            with self._real_sleep_patch(watch):
                with mock.patch.object(watch, "_warn_software_mode_once") as warn:
                    try:
                        await asyncio.wait_for(watch.run_watch_loop(), timeout=3.0)
                    except asyncio.TimeoutError:
                        pass
        # The first apply line only arms the pair check; the second consecutive
        # apply (no ignore line in between) proves software mode.
        self.assertEqual(warn.call_count, 1)
        self.assertEqual(applied, [])

    async def test_loop_notifies_device_state_on_activation_lines(self):
        applied = []
        device_events = []

        async def apply_value(value):
            applied.append(value)

        proc = self._fake_proc(lines=[
            _ACTIVATION_LINE,
            _locked_line("0.500"),
        ])
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: 37,
                on_device_active=lambda value: device_events.append(value),
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("asyncio.create_subprocess_exec", new=self._fake_spawn(proc)):
            with self._real_sleep_patch(watch):
                try:
                    await asyncio.wait_for(watch.run_watch_loop(), timeout=3.0)
                except asyncio.TimeoutError:
                    pass
            await asyncio.sleep(0.05)
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
        with mock.patch("asyncio.create_subprocess_exec", new=spawn):
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
        with mock.patch("asyncio.create_subprocess_exec", new=spawn):
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

        proc = _fake_proc_static(lines)
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: master,
            ),
            debounce_seconds=0.0,
        )
        return watch, proc

    async def _run(self, watch, proc):
        async def spawn(*args, **kwargs):
            return proc

        with mock.patch("asyncio.create_subprocess_exec", new=spawn):
            with mock.patch.object(watch, "_sleep", new=lambda delay: asyncio.sleep(delay)):
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
        # the optional device-selection notifier, nothing else.
        self.assertEqual(
            set(QobuzVolumeWatchDependencies.__dataclass_fields__.keys()),
            {"is_active", "apply_volume_value", "current_master", "on_device_active"},
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
