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


class WatchLoopTests(unittest.IsolatedAsyncioTestCase):
    def _fake_proc(self, lines):
        proc = mock.Mock()
        proc.returncode = None

        read = mock.AsyncMock()
        results = [line.encode() for line in lines] + [b""]
        read.side_effect = results
        proc.stdout = mock.Mock()
        proc.stdout.readline = read
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


if __name__ == "__main__":
    unittest.main()