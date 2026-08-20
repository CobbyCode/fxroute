# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for the qbzd journal-driven remote volume coupling.

Verifies that FXRoute maps the phone slider intent (``volume_mode=locked:
ignoring remote SetVolume(0.NNN); player stays at 100%`` journal line) onto the
canonical FXRoute master volume, without ever touching qbzd's own gain.

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
    is_software_volume_apply,
    parse_ignored_volume,
)

_LOCKED_LINE = (
    "[QConnect] volume_mode=locked: ignoring remote SetVolume(0.450); player stays at 100%"
)


class ParseIgnoredVolumeTests(unittest.TestCase):
    def test_locked_line_returns_percent(self):
        self.assertEqual(parse_ignored_volume(_LOCKED_LINE), 45)
        self.assertEqual(parse_ignored_volume(
            "[QConnect] volume_mode=locked: ignoring remote SetVolume(1.000); player stays at 100%"
        ), 100)
        self.assertEqual(parse_ignored_volume(
            "[QConnect] volume_mode=locked: ignoring remote SetVolume(0.000); player stays at 100%"
        ), 0)
        self.assertEqual(parse_ignored_volume(
            "volume_mode=locked: ignoring remote SetVolume(0.260); player stays at 100%"
        ), 26)
        self.assertEqual(parse_ignored_volume(
            "volume_mode=locked: ignoring remote SetVolume(0.980); player stays at 100%"
        ), 98)

    def test_parser_accepts_timestamped_journal_line(self):
        line = (
            "Aug 18 07:19:12 fxroute qbzd[4076459]: 2026-08-18 07:19:12.263 INFO  "
            "qbzd::qconnect::engine [QConnect] volume_mode=locked: ignoring remote "
            "SetVolume(0.490); player stays at 100%"
        )
        self.assertEqual(parse_ignored_volume(line), 49)

    def test_software_mode_and_garbage_lines_are_ignored(self):
        software = (
            "[QConnect] Renderer command applied: SetVolume { volume: Some(45), volume_delta: None }"
        )
        self.assertIsNone(parse_ignored_volume(software))
        self.assertIsNone(parse_ignored_volume(""))
        self.assertIsNone(parse_ignored_volume(None))
        self.assertIsNone(parse_ignored_volume("random noise"))
        # A set value outside the 0..1 range or malformed brackets is not an intent.
        self.assertIsNone(parse_ignored_volume(
            "volume_mode=locked: ignoring remote SetVolume(150); player stays at 100%"
        ))


class TranslatorDebounceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.applied = []
        self.active = True

        async def apply_volume(percent):
            self.applied.append(percent)

        self.translator = QobuzRemoteVolumeTranslator(
            is_active=lambda: self.active,
            apply_volume=apply_volume,
        )

    async def test_inactive_drops_volume_intent(self):
        self.active = False
        self.assertTrue(self.translator.submit(50) is False or self.translator.submit(50) is None)
        await self.translator.flush()
        self.assertEqual(self.applied, [])

    async def test_flush_discards_pending_when_owner_left_during_debounce(self):
        # Regression: Qobuz volume intent arrives, the owner switches to
        # Tidal/Spotify inside the debounce window, and flush must discard the
        # stale pending value right before apply_volume() instead of writing
        # the old Qobuz value to the FXRoute master.
        self.active = True
        self.assertTrue(self.translator.submit(55))
        self.active = False  # owner changed inside the debounce window
        await self.translator.flush()
        self.assertEqual(self.applied, [])
        self.assertIsNone(self.translator.pending)

    async def test_flush_applies_when_owner_survives_debounce(self):
        self.active = True
        self.assertTrue(self.translator.submit(62))
        await self.translator.flush()
        self.assertEqual(self.applied, [62])

    async def test_active_applies_latest_pending_value_once(self):
        for value in (40, 50, 55):
            self.assertTrue(self.translator.submit(value))
        await self.translator.flush()
        self.assertEqual(self.applied, [55])
        # A second flush with no new intent calls nothing.
        await self.translator.flush()
        self.assertEqual(self.applied, [55])

    async def test_each_new_intent_replaces_pending(self):
        self.assertTrue(self.translator.submit(30))
        self.assertTrue(self.translator.submit(29))
        self.assertEqual(self.translator.pending, 29)
        await self.translator.flush()
        self.assertEqual(self.applied, [29])


class TranslatorPickupTests(unittest.IsolatedAsyncioTestCase):
    async def _make(self, master):
        self.master = master
        self.applied = []

        async def apply_volume(percent, generation=None):
            self.applied.append(percent)
            self.master = percent

        return QobuzRemoteVolumeTranslator(
            is_active=lambda: True,
            apply_volume=apply_volume,
            get_canonical_volume=lambda: self.master,
        )

    async def test_remote_above_master_must_cross_before_writing(self):
        translator = await self._make(30)
        for value in range(90, 30, -1):
            self.assertFalse(translator.submit(value))
            await translator.flush()
        self.assertFalse(self.applied)
        self.assertFalse(translator.submit(30))
        await translator.flush()
        self.assertEqual(self.applied, [])
        self.assertTrue(translator.picked_up)
        self.assertTrue(translator.submit(29))
        await translator.flush()
        self.assertEqual(self.applied, [29])

    async def test_remote_below_master_crosses_up(self):
        translator = await self._make(70)
        for value in range(20, 70):
            self.assertFalse(translator.submit(value))
            await translator.flush()
        self.assertFalse(self.applied)
        self.assertFalse(translator.submit(70))
        await translator.flush()
        self.assertTrue(translator.picked_up)
        self.assertTrue(translator.submit(71))
        await translator.flush()
        self.assertEqual(self.applied, [71])

    async def test_equal_initial_remote_arms_then_picks_up_downward(self):
        translator = await self._make(30)
        self.assertFalse(translator.submit(30))
        await translator.flush()
        self.assertEqual(self.applied, [])
        self.assertTrue(translator.picked_up)
        self.assertTrue(translator.submit(29))
        await translator.flush()
        self.assertEqual(self.applied, [29])
        self.assertTrue(translator.picked_up)

    async def test_equal_initial_remote_arms_then_picks_up_upward(self):
        translator = await self._make(30)
        self.assertFalse(translator.submit(30))
        await translator.flush()
        self.assertEqual(self.applied, [])
        self.assertTrue(translator.picked_up)
        self.assertTrue(translator.submit(31))
        await translator.flush()
        self.assertEqual(self.applied, [31])
        self.assertTrue(translator.picked_up)

    async def test_equal_initial_remote_large_overshoot_stays_safe(self):
        translator = await self._make(30)
        self.assertFalse(translator.submit(30))
        self.assertFalse(translator.submit(100))
        await translator.flush()
        self.assertEqual(self.applied, [])

    async def test_different_initial_remote_large_crossing_stays_safe(self):
        translator = await self._make(30)
        self.assertFalse(translator.submit(10))
        self.assertFalse(translator.submit(100))
        await translator.flush()
        self.assertEqual(self.applied, [])

    async def test_skipping_target_still_picks_up(self):
        translator = await self._make(30)
        translator.submit(90)
        self.assertTrue(translator.submit(29))
        await translator.flush()
        self.assertTrue(translator.picked_up)
        translator.submit(28)
        await translator.flush()
        self.assertEqual(self.applied, [29, 28])

    async def test_external_master_change_resets_pickup(self):
        translator = await self._make(30)
        translator.submit(90)
        translator.submit(30)
        await translator.flush()
        self.assertTrue(translator.picked_up)
        self.master = 50
        self.assertFalse(translator.submit(29))
        self.assertFalse(translator.picked_up)

    async def test_reset_discards_pending_and_latch(self):
        translator = await self._make(30)
        translator.submit(90)
        translator.submit(30)
        translator.submit(29)
        translator.reset()
        await translator.flush()
        self.assertEqual(self.applied, [])
        self.assertFalse(translator.picked_up)

    async def test_large_upward_overshoot_does_not_pick_up(self):
        translator = await self._make(30)
        translator.submit(10)
        self.assertFalse(translator.submit(100))
        await translator.flush()
        self.assertEqual(self.applied, [])
        self.assertFalse(translator.picked_up)

    async def test_large_downward_overshoot_does_not_pick_up(self):
        translator = await self._make(30)
        translator.submit(90)
        self.assertFalse(translator.submit(0))
        await translator.flush()
        self.assertEqual(self.applied, [])
        self.assertFalse(translator.picked_up)

    async def test_external_change_invalidates_inflight_remote_generation(self):
        translator = await self._make(30)
        translator.submit(90)
        translator.submit(29)
        generation = translator.pending_generation
        translator.canonical_volume_changed(60)
        self.assertFalse(translator.is_generation_current(generation))
        await translator.flush()
        self.assertEqual(self.applied, [])

    async def test_external_change_during_remote_barrier_cannot_be_overwritten(self):
        started = asyncio.Event()
        release = asyncio.Event()
        master = 30
        applied = []

        async def apply_volume(percent, generation):
            started.set()
            await release.wait()
            if translator.is_generation_current(generation):
                applied.append(percent)

        translator = QobuzRemoteVolumeTranslator(
            is_active=lambda: True,
            apply_volume=apply_volume,
            get_canonical_volume=lambda: master,
        )
        translator.submit(90)
        translator.submit(29)
        flush_task = asyncio.create_task(translator.flush())
        await started.wait()
        master = 60
        translator.canonical_volume_changed(master)
        release.set()
        await flush_task
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

    async def test_loop_applies_phone_intent_when_qobuz_owns(self):
        applied = []

        async def apply_volume(percent):
            applied.append(percent)

        proc = self._fake_proc(lines=[
            _LOCKED_LINE,
            "volume_mode=locked: ignoring remote SetVolume(0.310); player stays at 100%",
            "random line",
            "volume_mode=locked: ignoring remote SetVolume(0.290); player stays at 100%",
        ])
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume=apply_volume,
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
            self.assertEqual(applied[-1:], [29])

    async def test_loop_respawns_after_eof(self):
        applied = []
        spawned = []

        async def apply_volume(percent):
            applied.append(percent)

        first = self._fake_proc(lines=[_LOCKED_LINE])
        second = self._fake_proc(lines=["volume_mode=locked: ignoring remote SetVolume(0.050); player stays at 100%"])

        def factory():
            return first if not spawned else second

        async def spawn(*args, **kwargs):
            chosen = factory()
            spawned.append(args)
            return chosen

        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume=apply_volume,
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("asyncio.create_subprocess_exec", new=spawn):
            with self._real_sleep_patch(watch):
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(watch.run_watch_loop(), timeout=3.0)
        await asyncio.sleep(0.05)
        self.assertEqual(applied, [45, 5])
        # journalctl was (re)spawned after the first EOF, with the tail command.
        self.assertGreaterEqual(len(spawned), 2)
        self.assertEqual(spawned[0][3], "qbzd.service")

    async def test_loop_ignores_intents_while_qobuz_does_not_own(self):
        applied = []

        async def apply_volume(percent):
            applied.append(percent)

        proc = self._fake_proc(lines=[_LOCKED_LINE])
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: False,
                apply_volume=apply_volume,
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
        warnings = []

        async def apply_volume(percent):
            applied.append(percent)

        software = (
            "[QConnect] Renderer command applied: SetVolume { volume: Some(45), volume_delta: None }"
        )
        proc = self._fake_proc(lines=[software, software])
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume=apply_volume,
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("asyncio.create_subprocess_exec", new=self._fake_spawn(proc)), \
             mock.patch("playback.qbzd_volume_watch.logger.warning", side_effect=lambda msg, *a: warnings.append(msg)):
            with self._real_sleep_patch(watch):
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(watch.run_watch_loop(), timeout=3.0)
        await asyncio.sleep(0.05)
        self.assertEqual(applied, [])
        # Both lines are software-mode applies; the rate-limited warning fires
        # once because the second call happens within the rate-limit window.
        mode_warnings = [w for w in warnings if "volume_mode" in str(w)]
        self.assertEqual(len(mode_warnings), 1)
        self.assertIn("locked", mode_warnings[0])

    async def test_locked_pair_does_not_warn(self):
        # In locked mode every sink apply line is immediately followed by the
        # engine ignore line; the pair must not trigger the software warning.
        applied = []
        warnings = []

        async def apply_volume(percent):
            applied.append(percent)

        locked_pair = [
            "[QConnect] Renderer command applied: SetVolume { volume: Some(99), volume_delta: None }",
            "[QConnect] volume_mode=locked: ignoring remote SetVolume(0.990); player stays at 100%",
            "[QConnect] Renderer command applied: SetVolume { volume: Some(77), volume_delta: None }",
            "[QConnect] volume_mode=locked: ignoring remote SetVolume(0.770); player stays at 100%",
        ]
        proc = self._fake_proc(lines=locked_pair)
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume=apply_volume,
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("asyncio.create_subprocess_exec", new=self._fake_spawn(proc)), \
             mock.patch("playback.qbzd_volume_watch.logger.warning", side_effect=lambda msg, *a: warnings.append(msg)):
            with self._real_sleep_patch(watch):
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(watch.run_watch_loop(), timeout=3.0)
        await asyncio.sleep(0.05)
        self.assertEqual(applied[-1:], [77])
        mode_warnings = [w for w in warnings if "volume_mode" in str(w)]
        self.assertEqual(mode_warnings, [])

    async def test_drain_failure_is_logged_and_loop_continues(self):
        # Regression: an exception raised by apply_volume() inside the detached
        # drain task must be caught and logged (no unobserved task exception)
        # and the watch loop must keep processing later intents.
        applied = []
        warnings = []

        async def apply_volume(percent):
            if percent == 50:
                raise RuntimeError("master write exploded")
            applied.append(percent)

        proc = self._fake_proc(lines=[
            _LOCKED_LINE,  # 45 -> fine
            "volume_mode=locked: ignoring remote SetVolume(0.500); player stays at 100%",  # 50 -> raises
            "volume_mode=locked: ignoring remote SetVolume(0.300); player stays at 100%",  # 30 -> fine
        ])
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume=apply_volume,
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("asyncio.create_subprocess_exec", new=self._fake_spawn(proc)), \
             mock.patch("playback.qbzd_volume_watch.logger.warning",
                        side_effect=lambda msg, *a: warnings.append(msg)):
            with self._real_sleep_patch(watch):
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(watch.run_watch_loop(), timeout=3.0)
        await asyncio.sleep(0.05)
        self.assertEqual(applied, [45, 30])
        drain_warnings = [w for w in warnings if "drain failed" in str(w)]
        self.assertEqual(len(drain_warnings), 1)

    async def test_drain_reflushes_intent_arriving_during_apply(self):
        started = asyncio.Event()
        release = asyncio.Event()
        applied = []

        async def apply_volume(percent, generation=None):
            applied.append(percent)
            if percent == 29:
                started.set()
                await release.wait()

        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume=apply_volume,
                get_canonical_volume=lambda: 30,
            ),
            debounce_seconds=0.0,
        )
        watch._translator.submit(90)
        self.assertTrue(watch._translator.submit(29))
        watch._schedule_drain()
        await started.wait()
        self.assertTrue(watch._translator.submit(28))
        watch._schedule_drain()
        drain_task = watch._drain_task
        release.set()
        await asyncio.wait_for(drain_task, timeout=1.0)
        self.assertEqual(applied, [29, 28])
        self.assertIsNone(watch._translator.pending)
        self.assertIsNone(watch._drain_task)

    async def test_drain_coalesces_multiple_intents_during_apply(self):
        started = asyncio.Event()
        release = asyncio.Event()
        applied = []

        async def apply_volume(percent, generation=None):
            applied.append(percent)
            if percent == 29:
                started.set()
                await release.wait()

        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume=apply_volume,
                get_canonical_volume=lambda: 30,
            ),
            debounce_seconds=0.0,
        )
        watch._translator.submit(90)
        watch._translator.submit(29)
        watch._schedule_drain()
        await started.wait()
        for value in (28, 27, 26):
            self.assertTrue(watch._translator.submit(value))
            watch._schedule_drain()
        drain_task = watch._drain_task
        release.set()
        await asyncio.wait_for(drain_task, timeout=1.0)
        self.assertEqual(applied, [29, 26])
        self.assertIsNone(watch._translator.pending)

    def test_software_volume_apply_detection(self):
        self.assertTrue(is_software_volume_apply(
            "[QConnect] Renderer command applied: SetVolume { volume: Some(45), volume_delta: None }"
        ))
        self.assertTrue(is_software_volume_apply(
            "Renderer command applied: SetVolume { volume: Some(0), volume_delta: None }"
        ))
        self.assertFalse(is_software_volume_apply(_LOCKED_LINE))
        self.assertFalse(is_software_volume_apply(""))
        self.assertFalse(is_software_volume_apply(None))


class SoftwareWarningTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_warning_fires_immediately_after_boot(self):
        # Regression: with ``_software_warned_at = 0.0`` the first warning is
        # swallowed while the monotonic uptime is still below the rate-limit
        # window (e.g. 42 s after boot < 300 s). The initial state must be
        # "never warned", so the very first call always warns.
        warnings = []
        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume=lambda p: None,
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("playback.qbzd_volume_watch.logger.warning",
                        side_effect=lambda msg, *a: warnings.append(msg)):
            watch._warn_software_mode_once(42.0)   # low monotonic uptime
            watch._warn_software_mode_once(42.5)   # still inside the window
        self.assertEqual(len(warnings), 1)
        self.assertIn("volume_mode", warnings[0])


if __name__ == "__main__":
    unittest.main()
