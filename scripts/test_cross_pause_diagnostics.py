#!/usr/bin/env python3
"""Cross-source pause helpers: best-effort, but never silently successful.

Contracts (no production code changed here):

- when pausing the other source fails (missing backend, wedged player
  control, failed state broadcast), the helpers still return normally so
  the surrounding handoff/endpoint keeps its established resilient
  semantics — no new errors are introduced;
- the failure is logged as a warning so a missed pause (which can leave
  two sources audible at once) is diagnosable instead of invisible.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class CrossPauseDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_spotify_pause_backend_failure_is_logged_not_raised(self):
        with patch.object(
            main, "_spotify_player_present", new=AsyncMock(return_value=True)
        ), patch(
            "shutil.which", return_value="/usr/bin/playerctl"
        ), patch.object(
            main.spotify_mpris,
            "detect_backend",
            new=AsyncMock(side_effect=RuntimeError("no backend")),
        ), self.assertLogs("main", level="WARNING") as captured:
            result = await main.pause_spotify_for_local_playback_broadcast()
        self.assertIsNone(result)
        self.assertTrue(
            any("potify" in line for line in captured.output), captured.output
        )

    async def test_spotify_broadcast_failure_is_logged_not_raised(self):
        async def fake_exec(*args, **kwargs):
            return SimpleNamespace(
                communicate=AsyncMock(return_value=(b"", b""))
            )

        with patch.object(
            main, "_spotify_player_present", new=AsyncMock(return_value=True)
        ), patch.object(
            main.spotify_mpris,
            "detect_backend",
            new=AsyncMock(return_value="spotify"),
        ), patch.object(
            main.spotify_mpris,
            "resolve_player_name",
            new=AsyncMock(return_value="spotify"),
        ), patch(
            "shutil.which", return_value="/usr/bin/playerctl"
        ), patch.object(
            main.asyncio, "create_subprocess_exec", new=fake_exec
        ), patch.object(
            main, "broadcast_spotify_state", new=AsyncMock(side_effect=RuntimeError("ws down"))
        ), self.assertLogs("main", level="WARNING") as captured:
            result = await main.pause_spotify_for_local_playback_broadcast()
        self.assertIsNone(result)
        self.assertTrue(
            any("potify" in line for line in captured.output), captured.output
        )

    async def test_local_pause_failure_is_logged_not_raised(self):
        player = SimpleNamespace(
            _running=True,
            state={},
            stop_playback=Mock(side_effect=RuntimeError("mpv gone")),
        )
        with patch.object(
            main.runtime, "player_instance", player
        ), self.assertLogs("main", level="WARNING") as captured:
            result = await main.pause_local_playback_for_spotify_broadcast()
        self.assertIsNone(result)
        self.assertTrue(
            any("ocal" in line for line in captured.output), captured.output
        )

    async def test_external_input_pause_failure_is_logged_not_raised(self):
        player = SimpleNamespace(
            _running=True,
            state={},
            stop_playback=Mock(side_effect=RuntimeError("mpv gone")),
        )
        with patch.object(
            main.runtime, "player_instance", player
        ), patch.object(
            main,
            "get_spotify_ui_state",
            new=AsyncMock(return_value={"status": "Stopped"}),
        ), self.assertLogs("main", level="WARNING") as captured:
            result = await main._pause_all_app_playback_for_external_input()
        self.assertIsNone(result)
        self.assertTrue(
            any("external input" in line for line in captured.output),
            captured.output,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
