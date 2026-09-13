#!/usr/bin/env python3
"""Regression: mpv source-readiness wait accepts the ``file-loaded`` signal.

``_wait_for_player_current_file`` must not treat the optimistic ``current_file``
set by ``loadfile`` as "loaded": a follow-up seek would race the load.  It
accepts a source once mpv reports a positive ``duration`` (known-length
files/streams) or fires the ``file-loaded`` event (live/unknown-length streams
that never report a duration).
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import playback.media_readiness as media_readiness
from playback.player import MPVWrapper
from playback.transition import TransitionRequest
from playback_transition_test_support import make_transition_runtime


async def _wait(expected_url, state, timeout_ms=60):
    player = SimpleNamespace(state=state)
    with patch.object(media_readiness, "PIPEWIRE_HANDOFF_POLL_INTERVAL_MS", 10):
        return await media_readiness.wait_for_player_current_file(
            expected_url, timeout_ms=timeout_ms, get_player=lambda: player
        )


class WaitForPlayerCurrentFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_when_duration_reported(self):
        state = {"current_file": "/music/a.flac", "duration": 300.0}
        self.assertTrue(await _wait("/music/a.flac", state))

    async def test_ready_when_file_loaded_without_duration(self):
        # Live/unknown-length sources never report a positive duration; the
        # file-loaded event is the fallback readiness signal.
        state = {"current_file": "https://stream.example/radio", "duration": 0.0, "file_loaded": True}
        self.assertTrue(await _wait("https://stream.example/radio", state))

    async def test_not_ready_when_only_current_file_set(self):
        # loadfile sets current_file optimistically; without duration or the
        # file-loaded event the wait must time out instead of declaring ready.
        state = {"current_file": "/music/a.flac", "duration": 0.0, "file_loaded": False}
        self.assertFalse(await _wait("/music/a.flac", state))

    async def test_not_ready_when_file_mismatched(self):
        state = {"current_file": "/music/other.flac", "duration": 300.0}
        self.assertFalse(await _wait("/music/a.flac", state))

    async def test_missing_url_or_player_is_not_ready(self):
        self.assertFalse(await _wait(None, {}))


class RadioColdStartSettleBudgetTests(unittest.IsolatedAsyncioTestCase):
    """A cold radio stream gets the radio settle budget, not the generic one.

    Live .104 evidence: a cold SomaFM loadfile exceeded the generic 1600 ms
    media-settle budget (stage ``target-rate-resolve`` took 1644.7 ms) and a
    healthy stream turned into a hard 500 ("radio target stream did not
    settle while paused"); the immediate retry succeeded.  The radio branch
    must wait with the same ~4 s cold-start budget its PipeWire port
    readiness wait already uses, while local files keep the short budget.
    """

    class _Player:
        _running = True

        def __init__(self) -> None:
            self.state = {"current_file": None, "duration": 0.0, "file_loaded": False,
                          "playing": False, "paused": True}

        def set_pause(self, paused: bool) -> None:
            self.state["paused"] = bool(paused)

        def loadfile(self, path: str, *, mode=None, start_paused=False) -> None:
            # Mirrors MPVWrapper: current_file is set optimistically before
            # mpv confirms the load via file-loaded/duration.
            self.state["current_file"] = path
            self.state["file_loaded"] = False

        def set_volume(self, volume: int) -> None:
            self.volume = volume

        def get_property(self, name: str):
            return {"samplerate": 48000} if name == "audio-params" else None

    async def _settled_with(self, request: TransitionRequest) -> int:
        seen: list[int] = []

        async def fake_settle(expected_url, timeout_ms=1600, **kwargs):
            seen.append(timeout_ms)
            return True

        player = self._Player()
        with patch.object(main.runtime, "player_instance", player), patch.object(
            media_readiness, "wait_for_player_current_file", fake_settle
        ):
            runtime = make_transition_runtime()
            await runtime.resolve_target_rate(request)
        self.assertEqual(len(seen), 1)
        return seen[0]

    async def test_radio_cold_start_uses_radio_settle_budget(self):
        request = TransitionRequest(
            operation="play",
            source="radio",
            target_rate=None,
            target_url="https://radio.example/slow-cold-start",
            should_play=True,
            reload_source=True,
        )
        timeout_ms = await self._settled_with(request)
        self.assertEqual(timeout_ms, media_readiness.RADIO_LOAD_SETTLE_TIMEOUT_MS)
        self.assertGreater(
            timeout_ms,
            1600,
            "the generic media settle budget is too short for a cold stream",
        )

    async def test_local_reload_keeps_the_generic_settle_budget(self):
        request = TransitionRequest(
            operation="play",
            source="local",
            target_rate=None,
            target_url="/music/slow-local.flac",
            should_play=True,
            reload_source=True,
        )
        self.assertEqual(await self._settled_with(request), 1600)


class PlayerFileLoadedStateTests(unittest.TestCase):
    def _player(self):
        player = MPVWrapper()
        player._send_command = lambda *a, **k: {}
        return player

    def test_file_loaded_event_sets_flag(self):
        player = self._player()
        player._handle_event({"event": "file-loaded"})
        self.assertTrue(player._state["file_loaded"])

    def test_loadfile_resets_flag_until_mpv_confirms(self):
        player = self._player()
        player._state["file_loaded"] = True
        player.loadfile("/music/a.flac")
        self.assertFalse(player._state["file_loaded"])

    def test_stop_playback_resets_flag(self):
        player = self._player()
        player._state["file_loaded"] = True
        player.stop_playback()
        self.assertFalse(player._state["file_loaded"])


if __name__ == "__main__":
    unittest.main()
