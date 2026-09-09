#!/usr/bin/env python3
"""RadioReconnect retry bounds: bounded retries, stale guards, reset and stop.

Observable contracts of playback/radio_reconnect.py (no main.py import needed):

- an unexpected radio end schedules at most RADIO_RECONNECT_MAX_ATTEMPTS
  recovery requests with the production retry delay; afterwards only a
  warning is logged and no further retry loop runs;
- a reconnect in flight goes stale (no recovery call) when a newer
  transition generation commits, the track URL changes, or the source
  leaves radio before the delay expires;
- reset() (new play) clears attempts/URL/window so the next drop starts a
  fresh attempt window;
- stop() (shutdown) cancels a pending reconnect without leaking the task.

Regression coverage: an unbounded or stale-blind reconnect would hammer the
station, or reload radio over a newer local/Spotify/Qobuz context the user
already started. RadioReconnect was previously only ever mocked as a
collaborator, never the system under test.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import playback.radio_reconnect as reconnect_module
from playback.radio_reconnect import (
    RadioReconnect,
    RadioReconnectDependencies,
)

RADIO_URL = "https://radio.example/live"
RADIO_TRACK = {"source": "radio", "url": RADIO_URL, "title": "Example FM"}
ENDED_STATE = {"ended": True}


class _FakePlaybackState:
    """Minimal playback-state double: committed track plus epoch guard."""

    def __init__(self, track: dict, *, generation: int = 7):
        self.current_track_info = dict(track)
        self._generation = generation
        self._current = True

    def commit_newer_generation(self) -> None:
        self._current = False

    def transition_context_is_current(self, generation) -> bool:
        return self._current and generation == self._generation

    def capture_transition_epoch(self):
        return self._generation


class _FakePlayer:
    def __init__(self, *, running: bool = True, state: dict | None = None):
        self._running = running
        self.state = dict(state or {})


def _make_reconnect(
    track: dict = RADIO_TRACK,
    *,
    running: bool = True,
    player_state: dict | None = None,
) -> tuple[RadioReconnect, _FakePlaybackState, AsyncMock]:
    playback_state = _FakePlaybackState(track)
    player = _FakePlayer(running=running, state=player_state)
    recovery = AsyncMock()
    reconnect = RadioReconnect(
        RadioReconnectDependencies(
            get_player_instance=lambda: player,
            get_playback_state=lambda: playback_state,
            request_coordinated_recovery=recovery,
        )
    )
    return reconnect, playback_state, recovery


class RadioReconnectBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def _drain_pending_task(self, reconnect: RadioReconnect) -> None:
        task = reconnect.task
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)

    async def test_five_attempts_then_warning_and_no_further_retry(self):
        reconnect, _state, recovery = _make_reconnect()
        with patch.object(
            reconnect_module, "RADIO_RECONNECT_DELAY_SECONDS", 0.01
        ), self.assertLogs("playback.radio_reconnect", level="INFO") as captured:
            for _ in range(reconnect_module.RADIO_RECONNECT_MAX_ATTEMPTS):
                reconnect.schedule(dict(ENDED_STATE))
                await self._drain_pending_task(reconnect)
            self.assertEqual(
                recovery.await_count,
                reconnect_module.RADIO_RECONNECT_MAX_ATTEMPTS,
            )
            # One more ended event past the limit: warning, no new task/call.
            reconnect.schedule(dict(ENDED_STATE))
            await asyncio.sleep(0.05)
        self.assertEqual(
            recovery.await_count,
            reconnect_module.RADIO_RECONNECT_MAX_ATTEMPTS,
            "no retry loop may run past the attempt bound",
        )
        self.assertIsNone(reconnect.task)
        self.assertTrue(
            any("reconnect limit reached" in line for line in captured.output),
            captured.output,
        )
        _, reason = recovery.await_args_list[0].args
        self.assertEqual(reason, "radio-reconnect")
        self.assertTrue(recovery.await_args_list[0].kwargs.get("reload_source"))

    async def test_newer_generation_aborts_inflight_reconnect(self):
        reconnect, playback_state, recovery = _make_reconnect()
        with patch.object(reconnect_module, "RADIO_RECONNECT_DELAY_SECONDS", 0.3):
            reconnect.schedule(dict(ENDED_STATE))
            self.assertIsNotNone(reconnect.task)
            await asyncio.sleep(0.05)
            # A newer transition commits while the reconnect delay is pending.
            playback_state.commit_newer_generation()
            await asyncio.wait_for(asyncio.shield(reconnect.task), timeout=5.0)
        recovery.assert_not_awaited()
        self.assertIsNone(reconnect.task)

    async def test_track_switch_aborts_inflight_reconnect(self):
        cases = {
            "url-change": {"source": "radio", "url": "https://radio.example/other"},
            "source-change": {"source": "local", "id": "t1", "url": "/music/t1.flac"},
        }
        for name, new_track in cases.items():
            with self.subTest(name):
                reconnect, playback_state, recovery = _make_reconnect()
                with patch.object(
                    reconnect_module, "RADIO_RECONNECT_DELAY_SECONDS", 0.3
                ):
                    reconnect.schedule(dict(ENDED_STATE))
                    await asyncio.sleep(0.05)
                    playback_state.current_track_info = dict(new_track)
                    await asyncio.wait_for(asyncio.shield(reconnect.task), timeout=5.0)
                recovery.assert_not_awaited()
                self.assertIsNone(reconnect.task)

    async def test_reset_starts_a_fresh_attempt_window(self):
        reconnect, _state, recovery = _make_reconnect()
        reconnect.attempts = 3
        reconnect.url = RADIO_URL
        reconnect.active_since = 123.0
        reconnect.reset()
        self.assertEqual(reconnect.attempts, 0)
        self.assertIsNone(reconnect.url)
        self.assertEqual(reconnect.active_since, 0.0)
        with patch.object(reconnect_module, "RADIO_RECONNECT_DELAY_SECONDS", 0.01):
            reconnect.schedule(dict(ENDED_STATE))
            self.assertEqual(reconnect.attempts, 1)
            await self._drain_pending_task(reconnect)
        self.assertEqual(recovery.await_count, 1)

    async def test_stop_cancels_pending_task_without_leak(self):
        reconnect, _state, recovery = _make_reconnect()
        with patch.object(reconnect_module, "RADIO_RECONNECT_DELAY_SECONDS", 30.0):
            reconnect.schedule(dict(ENDED_STATE))
            task = reconnect.task
            self.assertIsNotNone(task)
            await reconnect.stop()
            # Cancellation is delivered on the next loop pass; afterwards the
            # pending reconnect must be observably gone.
            await asyncio.sleep(0.01)
            self.assertTrue(task.done() and task.cancelled())
            self.assertIsNone(reconnect.task)
        recovery.assert_not_awaited()
        # Stopping an idle reconnect is a no-op.
        await reconnect.stop()
        self.assertIsNone(reconnect.task)


if __name__ == "__main__":
    unittest.main(verbosity=2)
