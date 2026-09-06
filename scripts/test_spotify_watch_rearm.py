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


if __name__ == "__main__":
    unittest.main()
