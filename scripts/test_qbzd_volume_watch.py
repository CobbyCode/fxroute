# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for the qbzd journal-driven remote volume coupling.

Verifies the absolute-adoption contract (2026-08-28 revision, live-verified on
.104): the Qobuz app maintains a persistent per-renderer volume slider and
pushes its value on Connect activation and on every gesture (0.01-step slider
drags). The FXRoute master therefore *adopts* each observed locked-mode value
instead of applying deltas — the previous anchor+delta design left the phone
display and the master permanently offset (phone 0% vs master 13%, gestures
moving both in parallel), while absolute adoption is Spotify-Connect-like sync.

The 2026-08-21 anchor rationale ("the activation push is the phone's media
volume and must never move the master") was disproved by the same traces: the
push equals the app's own slider state, not the ephemeral media volume.

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
            "Aug 28 21:11:10 fxroute qbzd[1079]: 2026-08-28 21:11:10.647 INFO  "
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


class AbsoluteTranslatorTests(unittest.IsolatedAsyncioTestCase):
    """Every observed value is an absolute renderer-volume intent."""

    def _translator(self, applied, active=True):
        async def apply_value(value):
            applied.append(value)

        return QobuzRemoteVolumeTranslator(
            is_active=lambda: active,
            apply_volume_value=apply_value,
        )

    async def test_first_session_value_adopts_controller_volume(self):
        # The activation push is the app's renderer slider: the master adopts
        # it, which is what keeps the two displays aligned at connect time.
        applied = []
        translator = self._translator(applied)
        self.assertTrue(translator.submit(37))
        self.assertEqual(translator.pending, 37)
        await translator.flush()
        self.assertEqual(applied, [37])

    async def test_next_value_writes_absolute_not_delta(self):
        applied = []
        translator = self._translator(applied)
        translator.submit(100)
        await translator.flush()
        translator.submit(96)
        await translator.flush()
        self.assertEqual(applied, [100, 96])

    async def test_burst_collapses_to_latest_value(self):
        applied = []
        translator = self._translator(applied)
        translator.submit(100)
        translator.submit(99)
        translator.submit(98)
        self.assertEqual(translator.pending, 98)
        await translator.flush()
        self.assertEqual(applied, [98])

    async def test_identical_repeat_produces_no_write(self):
        applied = []
        translator = self._translator(applied)
        translator.submit(100)
        await translator.flush()
        self.assertFalse(translator.submit(100))
        await translator.flush()
        self.assertEqual(applied, [100])

    async def test_fresh_session_readopts_same_value(self):
        # The dedupe only holds inside a session: after a reactivation the
        # connect-time push must adopt again, even for the same value.
        applied = []
        translator = self._translator(applied)
        translator.submit(50)
        await translator.flush()
        translator.observe_activation()
        self.assertTrue(translator.submit(50))
        await translator.flush()
        self.assertEqual(applied, [50, 50])

    async def test_pending_value_is_dropped_on_activation(self):
        applied = []
        translator = self._translator(applied)
        translator.submit(100)
        translator.observe_activation()
        self.assertIsNone(translator.pending)
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_inactive_owner_drops_intent(self):
        applied = []
        translator = self._translator(applied, active=False)
        self.assertFalse(translator.submit(50))
        self.assertIsNone(translator.pending)
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_owner_change_inside_debounce_discards_pending(self):
        applied = []
        translator = self._translator(applied, active=True)
        translator.submit(100)
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
        )
        translator.submit(40)
        translator.submit(70)

        with self.assertRaisesRegex(RuntimeError, "temporary volume failure"):
            await translator.flush()
        self.assertEqual(translator.pending, 70)

        await translator.flush()
        self.assertEqual(applied, [70])

    async def test_committed_write_with_failed_readback_retries_idempotently(self):
        # Absolute writes are idempotent: a readback failure after a committed
        # write is retried with the same value and can never double-apply.
        applied = []
        attempts = 0

        async def apply_value(value):
            nonlocal attempts
            attempts += 1
            applied.append(value)
            if attempts == 1:
                raise RuntimeError("readback failed after set")

        translator = QobuzRemoteVolumeTranslator(
            is_active=lambda: True,
            apply_volume_value=apply_value,
        )
        translator.submit(70)
        with self.assertRaisesRegex(RuntimeError, "readback failed"):
            await translator.flush()
        self.assertEqual(translator.pending, 70)
        await translator.flush()
        self.assertEqual(applied, [70, 70])

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
        )
        translator.submit(40)
        translator.submit(70)
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

    async def test_loop_applies_phone_values_when_qobuz_owns(self):
        applied = []

        async def apply_value(value):
            applied.append(value)

        proc = self._fake_proc(lines=[
            _LOCKED_LINE,
            _locked_line("0.310"),
            "random line",
            _locked_line("0.290"),
        ])
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
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
            self.assertEqual(applied, [45, 31, 29])

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
            ),
            debounce_seconds=0.0,
        )

        watch._translator.submit(40)
        watch._schedule_drain()
        drain_task = watch._drain_task
        await write_started.wait()

        watch._translator.submit(70)   # final value arrives mid-write
        watch._schedule_drain()  # the active drain must retain this pending value
        release_write.set()
        await drain_task

        self.assertEqual(applied, [40, 70])

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
            ),
            debounce_seconds=0.0,
        )

        watch._translator.submit(70)
        watch._schedule_drain()
        await watch._drain_task

        self.assertEqual(applied, [70])

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
            ),
            debounce_seconds=0.0,
        )
        watch._translator.submit(70)
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
            ),
            debounce_seconds=0.0,
        )
        watch._translator.submit(70)
        watch._schedule_drain()
        await write_started.wait()

        await watch.stop()

        self.assertIsNone(watch._translator.pending)

    async def test_loop_respawns_after_eof(self):
        applied = []
        spawned = []

        async def apply_value(value):
            applied.append(value)

        first = self._fake_proc(lines=[_LOCKED_LINE])
        second = self._fake_proc(lines=[_locked_line("0.050")])

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
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("asyncio.create_subprocess_exec", new=spawn):
            with self._real_sleep_patch(watch):
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(watch.run_watch_loop(), timeout=3.0)
        await asyncio.sleep(0.05)
        # The respawn is transparent for an ongoing session: each observed
        # value is written absolutely, across the journalctl restart.
        self.assertEqual(applied, [45, 5])
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
        # post-activation push is adopted absolutely.
        self.assertEqual(device_events, [True])
        self.assertEqual(applied, [50])

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
                on_device_active=lambda value: seen.append(value),
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("asyncio.create_subprocess_exec", new=spawn):
            await watch._bootstrap_device_state()
        self.assertEqual(seen, [])
        self.assertEqual(applied, [])


class RemoteVolumeRegressionTests(unittest.IsolatedAsyncioTestCase):
    """The five contract scenarios, exercised end-to-end through the loop.

    Source of truth: live traces on .104 — 2026-08-28 21:07-21:11, the phone
    sat at 0% while the master stayed at 13%, and a drag up to 26% and back
    netted +1 on the master (13 -> 14): deltas preserved the offset forever.
    The absolute contract adopted here makes the master track the controller
    value so both displays show the same number.
    """

    def _watch(self, applied, lines):
        async def apply_value(value):
            applied.append(value)

        proc = _fake_proc_static(lines)
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
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

    async def test_connect_activation_push_adopts_controller_volume(self):
        applied = []
        watch, proc = self._watch(applied, [
            _ACTIVATION_LINE,
            _locked_line("1.000"),   # app pushes its renderer slider position
        ])
        await self._run(watch, proc)
        self.assertEqual(applied, [100])

    async def test_gesture_moves_master_to_the_absolute_controller_value(self):
        applied = []
        watch, proc = self._watch(applied, [
            _ACTIVATION_LINE,
            _locked_line("1.000"),
            _locked_line("0.990"),   # first hardware/slider gesture
        ])
        await self._run(watch, proc)
        # Push and gesture arrive within one debounce window in the static
        # fixture, so they collapse to the latest controller value.
        self.assertEqual(applied, [99])

    async def test_drag_burst_lands_on_its_final_value(self):
        applied = []
        watch, proc = self._watch(applied, [
            _locked_line("0.500"),
            _locked_line("0.450"),
            _locked_line("0.470"),   # drag continues; latest value wins
        ])
        await self._run(watch, proc)
        self.assertEqual(applied[-1], 47)

    async def test_no_provider_master_feedback_loop(self):
        applied = []
        # A master write never reaches qbzd's journal, so the only observations
        # are genuine remote values; repeats of the applied value dedup.
        watch, proc = self._watch(applied, [
            _locked_line("0.500"),
            _locked_line("0.450"),
            _locked_line("0.450"),   # echo/repeat of the applied value
        ])
        await self._run(watch, proc)
        self.assertEqual(applied, [45])

    async def test_loudness_work_point_is_never_touched(self):
        # The bridge's only output is the canonical master writer; it has no
        # handle on loudness state by construction. Pin the dependency
        # surface: exactly is_active + apply_volume_value + the optional
        # device-selection notifier, nothing else.
        self.assertEqual(
            set(QobuzVolumeWatchDependencies.__dataclass_fields__.keys()),
            {"is_active", "apply_volume_value", "on_device_active"},
        )
        applied = []
        watch, proc = self._watch(applied, [
            _locked_line("0.500"),
            _locked_line("0.480"),
        ])
        await self._run(watch, proc)
        self.assertEqual(applied, [48])


if __name__ == "__main__":
    unittest.main()
