#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Regression test: Spotify claim watch survives a late provider install.

Image installs start FXRoute before providers (first boot uses
``--providers none``; Settings installs land later). The playerctl watch
must wait for the backend and become effective without an FXRoute restart:
start uninstalled -> install later -> follow loop starts -> an external
Spotify Playing edge claims the owner.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import playback.spotify_watch as spotify_watch_module
from playback.spotify_watch import SpotifyPlayerctlWatch, SpotifyWatchDependencies


class _ForeverStdout:
    """Emit one Playing line, then block until cancelled."""

    def __init__(self) -> None:
        self._first = True

    async def readline(self) -> bytes:
        if self._first:
            self._first = False
            return b"Playing|Late Song|Late Artist\n"
        await asyncio.Event().wait()
        return b""


class _EmptyStderr:
    async def read(self) -> bytes:
        return b""


class _FakeFollowProc:
    def __init__(self) -> None:
        self.stdout = _ForeverStdout()
        self.stderr = _EmptyStderr()
        self.returncode: int | None = None


class SpotifyWatchRearmTest(unittest.IsolatedAsyncioTestCase):
    async def test_late_install_starts_follow_and_claims_playing(self):
        installed = {"present": False}
        spawns: list[list[str]] = []
        claim = AsyncMock()
        refresh = Mock()

        async def fake_subprocess_exec(*args: str, **kwargs):
            spawns.append(list(args))
            return _FakeFollowProc()

        deps = SpotifyWatchDependencies(
            get_playback_state=lambda: SimpleNamespace(current_playback_owner="qobuz"),
            get_spotify_ui_state=AsyncMock(
                return_value={"status": "Playing", "trackId": "abc", "title": "Late Song"}
            ),
            list_spotify_sink_inputs=lambda: [],
            spotify_sink_input_observation=lambda entries, **kw: None,
            request_coordinated_recovery=AsyncMock(),
            schedule_spotify_state_refresh=refresh,
            claim_spotify_playback=claim,
        )
        watch = SpotifyPlayerctlWatch(deps, retry_seconds=0.05)
        with (
            patch.object(
                spotify_watch_module, "spotify_installed", lambda: installed["present"]
            ),
            patch.object(spotify_watch_module.shutil, "which", return_value="/usr/bin/playerctl"),
            patch.object(spotify_watch_module.asyncio, "create_subprocess_exec", fake_subprocess_exec),
            patch.object(spotify_watch_module, "_stop_process", AsyncMock()),
            patch.object(
                spotify_watch_module.samplerate,
                "get_samplerate_status",
                return_value={"active_rate": 44100},
            ),
        ):
            task = asyncio.create_task(watch.run_watch_loop())
            try:
                # No backend yet: the loop must wait, not exit, and spawn nothing.
                await asyncio.sleep(0.15)
                self.assertFalse(task.done(), "watch exited while Spotify was missing")
                self.assertEqual(spawns, [])
                # Late Settings install: the watch must pick it up unaided.
                installed["present"] = True
                watch.notify_provider_installed()
                await asyncio.wait_for(self._wait_claim(claim), timeout=3.0)
                self.assertGreaterEqual(len(spawns), 1)
                self.assertIn("--follow", spawns[0])
                claim.assert_awaited_with("playerctl-playing")
                self.assertTrue(refresh.called)
                self.assertFalse(task.done(), "watch exited after claiming")
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _wait_claim(self, claim: AsyncMock) -> None:
        while claim.await_count == 0:
            await asyncio.sleep(0.02)

    async def test_completed_external_claim_is_removed_from_watcher_registry(self):
        installed = {"present": True}
        claim = AsyncMock()

        async def fake_subprocess_exec(*args: str, **kwargs):
            return _FakeFollowProc()

        deps = SpotifyWatchDependencies(
            get_playback_state=lambda: SimpleNamespace(current_playback_owner="qobuz"),
            get_spotify_ui_state=AsyncMock(
                return_value={"status": "Playing", "trackId": "abc", "title": "Late Song"}
            ),
            list_spotify_sink_inputs=lambda: [],
            spotify_sink_input_observation=lambda entries, **kw: None,
            request_coordinated_recovery=AsyncMock(),
            schedule_spotify_state_refresh=lambda reason: None,
            claim_spotify_playback=claim,
        )
        watch = SpotifyPlayerctlWatch(deps, retry_seconds=0.05)
        watch_task = None
        try:
            with (
                patch.object(
                    spotify_watch_module, "spotify_installed", lambda: installed["present"]
                ),
                patch.object(spotify_watch_module.shutil, "which", return_value="/usr/bin/playerctl"),
                patch.object(spotify_watch_module.asyncio, "create_subprocess_exec", fake_subprocess_exec),
                patch.object(spotify_watch_module, "_stop_process", AsyncMock()),
                patch.object(
                    spotify_watch_module.samplerate,
                    "get_samplerate_status",
                    return_value={"active_rate": 44100},
                ),
            ):
                watch_task = asyncio.create_task(watch.run_watch_loop())
                watch.watch_task = watch_task
                await asyncio.wait_for(self._wait_claim(claim), timeout=2.0)
                await asyncio.sleep(0)
                claim_tasks = getattr(watch, "_claim_tasks", None)
                self.assertIsNotNone(claim_tasks, "the watcher must retain claim tasks")
                self.assertEqual(len(claim_tasks), 0, "completed claims must be removed")
        finally:
            if watch_task is not None and not watch_task.done():
                watch_task.cancel()
                await asyncio.gather(watch_task, return_exceptions=True)
            await watch.stop()

    async def test_failed_external_claim_is_observed_and_removed(self):
        claim = AsyncMock(side_effect=RuntimeError("claim failed"))
        deps = SpotifyWatchDependencies(
            get_playback_state=lambda: SimpleNamespace(current_playback_owner="qobuz"),
            get_spotify_ui_state=AsyncMock(return_value={"status": "Playing"}),
            list_spotify_sink_inputs=lambda: [],
            spotify_sink_input_observation=lambda entries, **kw: None,
            request_coordinated_recovery=AsyncMock(),
            schedule_spotify_state_refresh=lambda reason: None,
            claim_spotify_playback=claim,
        )
        watch = SpotifyPlayerctlWatch(deps)
        try:
            with self.assertLogs("playback.spotify_watch", level="WARNING") as captured:
                watch._schedule_external_claim("playerctl-playing")
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            self.assertEqual(len(watch._claim_tasks), 0)
            self.assertTrue(any("claim failed" in message for message in captured.output))
        finally:
            await watch.stop()

    async def test_stop_cancels_and_drains_blocking_external_claim(self):
        claim_started = asyncio.Event()
        claim_cancelled = asyncio.Event()
        claim_release = asyncio.Event()

        async def claim(_reason):
            claim_started.set()
            try:
                await claim_release.wait()
            except asyncio.CancelledError:
                claim_cancelled.set()
                raise

        async def fake_subprocess_exec(*args: str, **kwargs):
            return _FakeFollowProc()

        deps = SpotifyWatchDependencies(
            get_playback_state=lambda: SimpleNamespace(current_playback_owner="qobuz"),
            get_spotify_ui_state=AsyncMock(
                return_value={"status": "Playing", "trackId": "abc", "title": "Late Song"}
            ),
            list_spotify_sink_inputs=lambda: [],
            spotify_sink_input_observation=lambda entries, **kw: None,
            request_coordinated_recovery=AsyncMock(),
            schedule_spotify_state_refresh=lambda reason: None,
            claim_spotify_playback=claim,
        )
        watch = SpotifyPlayerctlWatch(deps, retry_seconds=0.05)
        watch_task = None
        try:
            with (
                patch.object(spotify_watch_module, "spotify_installed", return_value=True),
                patch.object(spotify_watch_module.shutil, "which", return_value="/usr/bin/playerctl"),
                patch.object(spotify_watch_module.asyncio, "create_subprocess_exec", fake_subprocess_exec),
                patch.object(spotify_watch_module, "_stop_process", AsyncMock()),
                patch.object(
                    spotify_watch_module.samplerate,
                    "get_samplerate_status",
                    return_value={"active_rate": 44100},
                ),
            ):
                watch_task = asyncio.create_task(watch.run_watch_loop())
                watch.watch_task = watch_task
                await asyncio.wait_for(claim_started.wait(), timeout=2.0)
                claim_tasks = getattr(watch, "_claim_tasks", None)
                self.assertIsNotNone(claim_tasks, "the watcher must retain claim tasks")
                self.assertEqual(len(claim_tasks), 1)

                await asyncio.wait_for(watch.stop(), timeout=1.0)
                await asyncio.sleep(0)

            self.assertTrue(claim_cancelled.is_set())
            self.assertEqual(len(getattr(watch, "_claim_tasks", set())), 0)
            self.assertIsNone(watch.watch_task)
            pending_claims = [
                task for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_name() == "spotify-external-claim"
                and not task.done()
            ]
            self.assertEqual(pending_claims, [])
        finally:
            if watch_task is not None and not watch_task.done():
                watch_task.cancel()
                await asyncio.gather(watch_task, return_exceptions=True)
            leftovers = [
                task for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_name() == "spotify-external-claim"
                and not task.done()
            ]
            for task in leftovers:
                task.cancel()
            if leftovers:
                await asyncio.gather(*leftovers, return_exceptions=True)
            await watch.stop()
    async def test_stop_and_rearm_are_serialized_while_claim_drains(self):
        claim_started = asyncio.Event()
        claim_cancel_entered = asyncio.Event()
        claim_cancel_release = asyncio.Event()

        async def claim(_reason):
            claim_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                claim_cancel_entered.set()
                await claim_cancel_release.wait()
                raise

        async def idle_watch():
            await asyncio.Event().wait()

        deps = SpotifyWatchDependencies(
            get_playback_state=lambda: SimpleNamespace(current_playback_owner="qobuz"),
            get_spotify_ui_state=AsyncMock(return_value={"status": "Playing"}),
            list_spotify_sink_inputs=lambda: [],
            spotify_sink_input_observation=lambda entries, **kw: None,
            request_coordinated_recovery=AsyncMock(),
            schedule_spotify_state_refresh=lambda reason: None,
            claim_spotify_playback=claim,
        )
        watch = SpotifyPlayerctlWatch(deps)
        old_watch_task = asyncio.create_task(asyncio.Event().wait())
        watch.watch_task = old_watch_task
        stop_task = None
        rearm_task = None
        try:
            with patch.object(watch, "run_watch_loop", new=idle_watch):
                watch._schedule_external_claim("playerctl-playing")
                await asyncio.wait_for(claim_started.wait(), timeout=1.0)
                stop_task = asyncio.create_task(watch.stop())
                await asyncio.wait_for(claim_cancel_entered.wait(), timeout=1.0)
                rearm_task = asyncio.create_task(watch.rearm())
                await asyncio.sleep(0)
                self.assertFalse(rearm_task.done())
                self.assertIs(watch.watch_task, old_watch_task)

                claim_cancel_release.set()
                await stop_task
                await asyncio.wait_for(rearm_task, timeout=1.0)
                self.assertIsNot(watch.watch_task, old_watch_task)
                self.assertFalse(watch.watch_task.done())
        finally:
            claim_cancel_release.set()
            for task in (stop_task, rearm_task, old_watch_task):
                if task is not None and not task.done():
                    task.cancel()
            pending = [task for task in (stop_task, rearm_task, old_watch_task) if task is not None]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await watch.stop()

    async def test_rearm_after_stop_allows_new_external_claim(self):
        claim_started = asyncio.Event()

        async def claim(_reason):
            claim_started.set()
            await asyncio.Event().wait()

        deps = SpotifyWatchDependencies(
            get_playback_state=lambda: SimpleNamespace(current_playback_owner="qobuz"),
            get_spotify_ui_state=AsyncMock(return_value={"status": "Playing"}),
            list_spotify_sink_inputs=lambda: [],
            spotify_sink_input_observation=lambda entries, **kw: None,
            request_coordinated_recovery=AsyncMock(),
            schedule_spotify_state_refresh=lambda reason: None,
            claim_spotify_playback=claim,
        )
        watch = SpotifyPlayerctlWatch(deps)
        await watch.stop()
        with patch.object(spotify_watch_module, "spotify_installed", return_value=False):
            await watch.rearm()
        watch._schedule_external_claim("playerctl-playing")
        try:
            await asyncio.wait_for(claim_started.wait(), timeout=1.0)
        finally:
            await watch.stop()


if __name__ == "__main__":
    unittest.main()
