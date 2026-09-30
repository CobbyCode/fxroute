#!/usr/bin/env python3
"""Regression tests for qbzd async resume confirmation in start_target_source.

Root cause of the live .104 blocker: qbzd's ``/api/playback/play`` returns
immediately while the audio thread reinitializes the PipeWire stream
(~1.15s after a pause-suspend) before it reports Playing.  The Qobuz start
boundary previously read the provider status exactly once right after the
play POST, saw the stale Paused state, aborted the transition, and the
failure cleanup's pause then queued behind the still-running resume --
producing the observed "resumed then paused 3ms later".

The fix waits bounded for the real Playing edge (mirroring the MPV IPC
readback loop) instead of trusting one immediate status read.

These tests exercise the production ``start_target_source`` through the real
runtime adapter with mocked qbzd HTTP accessors.
"""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from playback_transition_test_support import make_transition_runtime
from playback.transition import TransitionRequest


def _qobuz_state(status: str) -> dict:
    return {
        "available": True,
        "installed": True,
        "source": "qobuz",
        "backend": "qbzd",
        "authenticated": True,
        "connected": True,
        "status": status,
        "artist": "Dub FX",
        "title": "ALL IN",
        "trackId": "351323758",
        "position": 249.0,
        "duration": 288.0,
        "volume": 98,
        "sample_rate": 44100,
        "bit_depth": 24,
        "audio_format": "flac",
    }


def _qobuz_play_request() -> TransitionRequest:
    return TransitionRequest(
        operation="qobuz-play",
        source="qobuz",
        target_rate=44100,
        target_url="351323758",
        target_track={"source": "qobuz", "id": "351323758", "url": "351323758"},
        should_play=True,
        rate_change=True,
        reload_source=True,
        detail="test-qobuz-play",
    )


class QobuzAsyncResumeConfirmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        rate = patch.object(main, "get_samplerate_status", return_value={
            "active_rate": 44100, "force_rate": 44100})
        rate.start()
        self.addCleanup(rate.stop)
    async def test_play_waits_for_playing_after_async_resume(self):
        """A delayed Playing edge must not abort the handoff.

        The first status reads after ``qbzd play`` return the stale Paused
        state (stream reinit in progress); the start boundary must keep
        polling until the real Playing edge appears and then succeed.
        """
        reads = iter([_qobuz_state("Paused"), _qobuz_state("Paused"), _qobuz_state("Playing")])

        async def fake_status():
            return next(reads)

        runtime = make_transition_runtime()
        with patch.object(main, "qobuz_play", new=AsyncMock(return_value=_qobuz_state("Paused"))), \
                patch.object(main, "qobuz_loaded_track_id", new=AsyncMock(return_value=351323758)), \
                patch.object(main, "qobuz_play_track", new=AsyncMock()) as load, \
                patch.object(main, "get_qobuz_ui_state", new=fake_status):
            await runtime.start_target_source(_qobuz_play_request())
        # No exception: the bounded wait found the Playing edge.
        load.assert_not_awaited()

    async def test_play_times_out_bounded_when_qbzd_stays_paused(self):
        """A genuinely stuck qbzd still fails the start boundary."""
        runtime = make_transition_runtime()
        with patch.object(main, "qobuz_play", new=AsyncMock(return_value=_qobuz_state("Paused"))), \
                patch.object(main, "qobuz_loaded_track_id", new=AsyncMock(return_value=351323758)), \
                patch.object(main, "qobuz_play_track", new=AsyncMock()) as load, \
                patch.object(main, "get_qobuz_ui_state", new=AsyncMock(return_value=_qobuz_state("Paused"))):
            with self.assertRaisesRegex(RuntimeError, "Qobuz did not enter Playing state"):
                await runtime.start_target_source(_qobuz_play_request())
        load.assert_not_awaited()

    async def test_pause_request_skips_playing_confirm(self):
        """should_play=False never polls for a Playing edge."""
        runtime = make_transition_runtime()
        request = dataclasses.replace(_qobuz_play_request(), should_play=False)
        with patch.object(main, "qobuz_play", new=AsyncMock()) as play, \
                patch.object(main, "get_qobuz_ui_state", new=AsyncMock(return_value=_qobuz_state("Paused"))) as status:
            await runtime.start_target_source(request)
        play.assert_not_awaited()
        status.assert_awaited_once()


class QobuzColdStartLoadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        rate = patch.object(main, "get_samplerate_status", return_value={
            "active_rate": 44100, "force_rate": 44100})
        rate.start()
        self.addCleanup(rate.stop)
    async def test_cold_start_loads_target_track_before_resume(self):
        """Empty daemon player: load the requested track, then resume.

        A bare resume cannot put audio into an empty fork player (Connect
        handoff with no current track, fresh daemon): the start boundary
        must download the target track via play-track first instead of
        running into the Playing-confirm timeout.
        """
        reads = iter([_qobuz_state("Stopped"), _qobuz_state("Playing")])

        async def fake_status():
            return next(reads)

        runtime = make_transition_runtime()
        with patch.object(main, "qobuz_play", new=AsyncMock(return_value=None)), \
                patch.object(main, "qobuz_loaded_track_id", new=AsyncMock(side_effect=[0, 351323758])), \
                patch.object(main, "qobuz_play_track", new=AsyncMock(return_value=None)) as load, \
                patch.object(main, "get_qobuz_ui_state", new=fake_status):
            await runtime.start_target_source(_qobuz_play_request())
        load.assert_awaited_once_with(351323758)

    async def test_pause_resume_reuses_loaded_track(self):
        """Paused mid-track with the target loaded: resume only, no reload.

        Reloading would restart the track from zero and re-download it;
        the start boundary must resume the loaded player instead.
        """
        state = _qobuz_state("Paused")
        runtime = make_transition_runtime()
        with patch.object(main, "qobuz_play", new=AsyncMock(return_value=None)), \
                patch.object(main, "qobuz_loaded_track_id", new=AsyncMock(return_value=351323758)), \
                patch.object(main, "qobuz_play_track", new=AsyncMock()) as load, \
                patch.object(main, "get_qobuz_ui_state", new=AsyncMock(return_value=state)):
            with self.assertRaisesRegex(RuntimeError, "Qobuz did not enter Playing state"):
                await runtime.start_target_source(_qobuz_play_request())
        load.assert_not_awaited()

    async def test_unparseable_target_falls_back_to_resume(self):
        """No usable target id: never call play-track, resume as before."""
        request = dataclasses.replace(
            _qobuz_play_request(), target_url="nope", target_track={"source": "qobuz"}
        )
        runtime = make_transition_runtime()
        with patch.object(main, "qobuz_play", new=AsyncMock(return_value=None)), \
                patch.object(main, "qobuz_loaded_track_id", new=AsyncMock(return_value=351323758)), \
                patch.object(main, "qobuz_play_track", new=AsyncMock()) as load, \
                patch.object(main, "get_qobuz_ui_state", new=AsyncMock(return_value=_qobuz_state("Playing"))):
            await runtime.start_target_source(request)
        load.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
