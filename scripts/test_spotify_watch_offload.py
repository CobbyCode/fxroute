#!/usr/bin/env python3
"""Regression test: Spotify watch detect probes stay off the event loop.

``_event_detect_check`` runs pactl-backed sink-input listing and the
4-command samplerate-status pipeline once per burst probe. Both were invoked
synchronously from the async detect task; on a wedged PipeWire/pactl they
block the whole event loop. This test pins the fix: the listing and the
status read must run in a worker thread, never on the event-loop thread.
"""

import asyncio
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import playback.spotify_watch as spotify_watch_module
from playback.spotify_watch import SpotifyPlayerctlWatch, SpotifyWatchDependencies

MAIN_THREAD = threading.current_thread()


class SpotifyWatchOffloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_event_detect_check_lists_sink_inputs_and_status_off_loop(self):
        seen = []

        def list_sink_inputs():
            seen.append(("inputs", threading.current_thread() is MAIN_THREAD))
            self.assertNotEqual(
                threading.current_thread(),
                MAIN_THREAD,
                "list_spotify_sink_inputs ran on the event-loop thread",
            )
            return [{"id": 42, "properties": {"node.name": "spotify"}}]

        def status_reader():
            seen.append(("status", threading.current_thread() is MAIN_THREAD))
            self.assertNotEqual(
                threading.current_thread(),
                MAIN_THREAD,
                "get_samplerate_status ran on the event-loop thread",
            )
            return {"active_rate": 44100, "force_rate": 44100}

        deps = SpotifyWatchDependencies(
            get_playback_state=lambda: SimpleNamespace(current_playback_owner="spotify"),
            get_spotify_ui_state=AsyncMock(
                return_value={"status": "Playing", "trackId": "abc", "title": "t"}
            ),
            list_spotify_sink_inputs=list_sink_inputs,
            spotify_sink_input_observation=lambda entries, **kw: (42, 44100),
            request_coordinated_recovery=AsyncMock(),
            schedule_spotify_state_refresh=lambda reason: None,
            claim_spotify_playback=lambda reason: None,
        )
        watch = SpotifyPlayerctlWatch(deps)
        with (
            patch.object(
                spotify_watch_module.samplerate,
                "get_samplerate_status",
                status_reader,
            ),
            patch.object(spotify_watch_module.asyncio, "sleep", AsyncMock()),
        ):
            await watch._event_detect_check("test")
        kinds = {kind for kind, _ in seen}
        self.assertEqual(kinds, {"inputs", "status"})
        self.assertFalse(
            any(on_loop for _, on_loop in seen),
            "Spotify watch detect probe ran pactl/status reads on the event loop",
        )
        # Aligned rates: no recovery may be requested by this probe.
        deps.request_coordinated_recovery.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()