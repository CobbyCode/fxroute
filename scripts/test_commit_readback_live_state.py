#!/usr/bin/env python3

"""Regression: the commit readback must trust live MPV IPC over the cached state.

The cached player state is driven by the async mpv event listener (a separate
socket from the command socket) and can lag a pause/unload/volume change mpv
already applied.  ``verify_committed_transition`` must fail when live
``pause``/``idle-active``/``volume`` contradict a stale cached "playing" state,
instead of committing a silently stalled or muted source.
"""

import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main
from playback.runtime import FxrouteTransitionRuntime
from playback.transition import TransitionRequest


class _StaleStatePlayer:
    """Cached state says "playing"; live IPC reports the real mpv state."""

    def __init__(self, *, current_file, live_pause, live_idle, live_volume):
        self.state = {
            "current_file": current_file,
            "playing": True,
            "paused": False,
            "volume": 100,
        }
        self._live = {
            "pause": live_pause,
            "idle-active": live_idle,
            "volume": live_volume,
        }

    def get_property(self, name):
        if name not in self._live:
            raise KeyError(name)
        return self._live[name]


class CommitReadbackLiveStateTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self):
        return FxrouteTransitionRuntime(main.make_playback_runtime_deps())

    def _request(self, *, operation="play", should_play=True):
        return TransitionRequest(
            operation=operation,
            source="local",
            target_url="/music/x.flac",
            target_track={"source": "local", "url": "/music/x.flac"},
            should_play=should_play,
            reload_source=True,
            detail="test",
        )

    async def test_commit_fails_when_live_pause_contradicts_stale_cached_state(self):
        player = _StaleStatePlayer(
            current_file="/music/x.flac",
            live_pause=True,
            live_idle=False,
            live_volume=100.0,
        )
        runtime = self._runtime()
        with patch.object(main.runtime, "player_instance", player):
            with self.assertRaisesRegex(
                RuntimeError, r"not actually playing at transition commit \(live IPC\)"
            ):
                await runtime.verify_committed_transition(self._request())

    async def test_commit_fails_when_live_unload_contradicts_stale_cached_state(self):
        player = _StaleStatePlayer(
            current_file="/music/x.flac",
            live_pause=False,
            live_idle=True,
            live_volume=100.0,
        )
        runtime = self._runtime()
        with patch.object(main.runtime, "player_instance", player):
            with self.assertRaisesRegex(
                RuntimeError, r"not actually playing at transition commit \(live IPC\)"
            ):
                await runtime.verify_committed_transition(self._request())

    async def test_commit_fails_when_live_volume_is_muted(self):
        player = _StaleStatePlayer(
            current_file="/music/x.flac",
            live_pause=False,
            live_idle=False,
            live_volume=0.0,
        )
        runtime = self._runtime()
        with patch.object(main.runtime, "player_instance", player):
            with self.assertRaisesRegex(
                RuntimeError, r"source volume was not restored: 0"
            ):
                await runtime.verify_committed_transition(self._request())

    async def test_cached_check_still_applies_without_live_ipc(self):
        """A player without get_property keeps the legacy cached-state check."""

        class CachedOnlyPlayer:
            state = {
                "current_file": "/music/x.flac",
                "playing": False,
                "paused": True,
                "volume": 100,
            }

        runtime = self._runtime()
        with patch.object(main.runtime, "player_instance", CachedOnlyPlayer()):
            # No "(live IPC)" suffix: the cached branch is authoritative here.
            with self.assertRaisesRegex(
                RuntimeError, r"not actually playing at transition commit$"
            ):
                await runtime.verify_committed_transition(self._request())


if __name__ == "__main__":
    unittest.main()
