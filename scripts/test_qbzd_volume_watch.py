# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for the qbzd journal-driven remote volume coupling.

Verifies that FXRoute maps the phone slider intent (``volume_mode=locked:
ignoring remote SetVolume(0.NNN); player stays at 100%`` journal line) onto
master *deltas* through the shared translator, without ever touching qbzd's
own gain or the Loudness work point.

Live-verified contract (2026-08-21, .104): the Qobuz app pushes its own media
volume as an absolute remote SetVolume right after every Connect activation.
That value anchors the controller scale and must never move the master; only
later changes are user gestures applied as relative steps.

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


class ParseTests(unittest.TestCase):
    def test_locked_line_yields_percent(self):
        self.assertEqual(parse_ignored_volume(_LOCKED_LINE), 45)
        self.assertEqual(parse_ignored_volume(_locked_line("1.000")), 100)
        self.assertEqual(parse_ignored_volume(_locked_line("0.000")), 0)
        self.assertEqual(parse_ignored_volume(_locked_line("0.260")), 26)
        self.assertEqual(parse_ignored_volume(_locked_line("0.980")), 98)

    def test_timestamped_journal_prefix_is_accepted(self):
        line = (
            "Aug 21 05:46:25 fxroute qbzd[1076]: 2026-08-21 05:46:25.846 INFO  "
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


class DeltaTranslatorTests(unittest.IsolatedAsyncioTestCase):
    """The shared delta semantics: anchor first, deltas afterwards."""

    def _translator(self, applied, active=True):
        async def apply_delta(delta):
            applied.append(delta)

        return QobuzRemoteVolumeTranslator(
            is_active=lambda: active,
            apply_volume_delta=apply_delta,
        )

    async def test_first_value_anchors_without_applying(self):
        applied = []
        translator = self._translator(applied)
        self.assertFalse(translator.submit(100))
        self.assertTrue(translator.anchored)
        self.assertIsNone(translator.pending)
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_next_value_applies_relative_delta(self):
        applied = []
        translator = self._translator(applied)
        translator.submit(100)
        self.assertTrue(translator.submit(96))
        await translator.flush()
        self.assertEqual(applied, [-4])

    async def test_burst_applies_net_delta_exactly_once(self):
        applied = []
        translator = self._translator(applied)
        translator.submit(100)
        translator.submit(99)
        translator.submit(98)
        self.assertEqual(translator.pending, -2)
        await translator.flush()
        self.assertEqual(applied, [-2])
        # A following single step continues from the advanced anchor.
        translator.submit(94)
        await translator.flush()
        self.assertEqual(applied, [-2, -4])

    async def test_round_trip_back_to_anchor_nets_zero(self):
        applied = []
        translator = self._translator(applied)
        translator.submit(100)
        translator.submit(96)
        translator.submit(100)
        self.assertIsNone(translator.pending)
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_identical_repeat_value_produces_no_delta(self):
        applied = []
        translator = self._translator(applied)
        translator.submit(100)
        self.assertFalse(translator.submit(100))
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_activation_resets_anchor_and_pending(self):
        applied = []
        translator = self._translator(applied)
        translator.submit(100)
        translator.submit(96)
        translator.observe_activation()
        self.assertFalse(translator.anchored)
        self.assertIsNone(translator.pending)
        await translator.flush()
        self.assertEqual(applied, [])
        # The next observation re-anchors instead of applying a stale delta.
        self.assertFalse(translator.submit(37))
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_inactive_owner_drops_intent_and_anchor(self):
        applied = []
        translator = self._translator(applied, active=False)
        self.assertFalse(translator.submit(50))
        self.assertFalse(translator.anchored)
        await translator.flush()
        self.assertEqual(applied, [])

    async def test_owner_change_inside_debounce_discards_pending(self):
        applied = []
        translator = self._translator(applied, active=True)
        translator.submit(100)
        translator.submit(96)
        translator.is_active = lambda: False
        await translator.flush()
        self.assertEqual(applied, [])


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

        proc.stdout = mock.Mock()
        proc.stdout.readline = readline
        return proc

    def _fake_spawn(self, proc):
        async def spawn(*args, **kwargs):
            return proc
        return spawn

    def _real_sleep_patch(self, watch):
        # Keep real suspension so the event loop can still fire the wait_for
        # timeout; patched AsyncMocks would starve it (no await yields).
        return mock.patch.object(watch, "_sleep", new=lambda delay: asyncio.sleep(delay))

    async def test_loop_applies_phone_deltas_when_qobuz_owns(self):
        applied = []

        async def apply_delta(delta):
            applied.append(delta)

        proc = self._fake_proc(lines=[
            _LOCKED_LINE,
            _locked_line("0.310"),
            "random line",
            _locked_line("0.290"),
        ])
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_delta=apply_delta,
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
            # 45 anchors; 31 and 29 are gestures relative to the running anchor.
            self.assertEqual(applied, [-14, -2])

    async def test_loop_respawns_after_eof_keeping_anchor(self):
        applied = []
        spawned = []

        async def apply_delta(delta):
            applied.append(delta)

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
                apply_volume_delta=apply_delta,
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("asyncio.create_subprocess_exec", new=spawn):
            with self._real_sleep_patch(watch):
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(watch.run_watch_loop(), timeout=3.0)
        await asyncio.sleep(0.05)
        # The respawn is transparent for an ongoing session: 45 anchored before
        # the EOF, so the post-respawn 5 is a -40 gesture, not a new baseline.
        self.assertEqual(applied, [-40])
        # journalctl was (re)spawned after the first EOF, with the tail command.
        self.assertGreaterEqual(len(spawned), 2)
        self.assertEqual(spawned[0][3], "qbzd.service")

    async def test_loop_ignores_intents_while_qobuz_does_not_own(self):
        applied = []

        async def apply_delta(delta):
            applied.append(delta)

        proc = self._fake_proc(lines=[_LOCKED_LINE])
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: False,
                apply_volume_delta=apply_delta,
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

        async def apply_delta(delta):
            applied.append(delta)

        software_line = "[QConnect] Renderer command applied: SetVolume { volume: Some(69) }"
        proc = self._fake_proc(lines=[software_line, software_line])
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_delta=apply_delta,
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


class RemoteVolumeRegressionTests(unittest.IsolatedAsyncioTestCase):
    """The five contract scenarios, exercised end-to-end through the loop.

    Source of truth: live trace on .104, 2026-08-21 06:06-06:07 — the phone
    activated FXRoute via Connect and the app pushed SET_VOLUME 100 six seconds
    later; the master jumped 37 -> 100 before any hardware press.
    """

    def _watch(self, applied, lines):
        async def apply_delta(delta):
            applied.append(delta)

        proc = self._fake_proc_static(lines)
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_delta=apply_delta,
            ),
            debounce_seconds=0.0,
        )
        return watch, proc

    @staticmethod
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

    async def _run(self, watch, proc):
        async def spawn(*args, **kwargs):
            return proc

        with mock.patch("asyncio.create_subprocess_exec", new=spawn):
            with mock.patch.object(watch, "_sleep", new=lambda delay: asyncio.sleep(delay)):
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(watch.run_watch_loop(), timeout=3.0)
        await asyncio.sleep(0.05)

    async def test_connect_activation_and_sync_push_do_not_move_master(self):
        applied = []
        watch, proc = self._watch(applied, [
            _ACTIVATION_LINE,
            _locked_line("1.000"),   # app pushes the phone's own media volume
        ])
        await self._run(watch, proc)
        self.assertEqual(applied, [])

    async def test_initial_source_value_100_does_not_move_master(self):
        applied = []
        watch, proc = self._watch(applied, [
            _locked_line("1.000"),   # watcher start mid-session: pure baseline
        ])
        await self._run(watch, proc)
        self.assertEqual(applied, [])

    async def test_first_real_gesture_moves_master_by_its_delta(self):
        applied = []
        watch, proc = self._watch(applied, [
            _ACTIVATION_LINE,
            _locked_line("1.000"),   # connect sync anchors at 100
            _locked_line("0.990"),   # first hardware press (down)
        ])
        await self._run(watch, proc)
        self.assertEqual(applied, [-1])

    async def test_no_provider_master_feedback_loop(self):
        applied = []
        # A master write never reaches qbzd's journal, so the only observations
        # are genuine remote values; repeats of already-applied values dedup.
        watch, proc = self._watch(applied, [
            _locked_line("0.500"),
            _locked_line("0.450"),
            _locked_line("0.450"),   # echo/repeat of the applied value
        ])
        await self._run(watch, proc)
        self.assertEqual(applied, [-5])

    async def test_loudness_work_point_is_never_touched(self):
        # The bridge's only output is the canonical master delta writer; it has
        # no handle on loudness state by construction. Pin the dependency
        # surface: exactly is_active + apply_volume_delta, nothing else.
        self.assertEqual(
            set(QobuzVolumeWatchDependencies.__dataclass_fields__.keys()),
            {"is_active", "apply_volume_delta"},
        )
        applied = []
        watch, proc = self._watch(applied, [
            _locked_line("0.500"),
            _locked_line("0.480"),
        ])
        await self._run(watch, proc)
        self.assertEqual(applied, [-2])


if __name__ == "__main__":
    unittest.main()
