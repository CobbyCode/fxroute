#!/usr/bin/env python3
"""Regression tests for Spotify async start confirmation in start_target_source.

Live .104 root cause of the Qobuz->Spotify 500 at target-source-start:
``playerctl play`` against a cross-device Spotify transfer still reports
the stale Paused state at the first readback (~0.45s; measured Playing
only from +1s with qbzd playing), so the single-read start boundary
aborted a handoff that would have settled a second later -- and the
failure cleanup then paused the just-started Spotify and latched the
output gate.

The fix waits bounded for the real Playing edge (same shape as the qbzd
async resume confirmation) instead of trusting one immediate read.

These tests exercise the production ``start_target_source`` through the
real runtime adapter with a mocked MPRIS play and provider status.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import playback.runtime.source as transition_source
from playback_transition_test_support import make_transition_runtime
from playback.transition import TransitionRequest


def _spotify_state(status: str) -> dict:
    return {
        "available": True,
        "installed": True,
        "source": "spotify",
        "backend": "desktop",
        "connected": True,
        "status": status,
        "artist": "Parov Stelar",
        "title": "Soul Fever Blues (feat. Muddy Waters)",
        "album": "The Burning Spider",
        "trackId": "/com/spotify/track/5NMxSNy8w2Zv4RZ2A5kRid",
        "position": 16.827,
        "duration": 184.52,
    }


def _spotify_toggle_request() -> TransitionRequest:
    return TransitionRequest(
        operation="spotify-toggle",
        source="spotify",
        target_rate=44100,
        should_play=True,
        rate_change=False,
        reload_source=True,
        detail="test-spotify-toggle",
    )


class SpotifyAsyncStartConfirmTests(unittest.IsolatedAsyncioTestCase):
    async def test_play_waits_for_playing_after_transfer(self):
        """A delayed Playing edge must not abort the handoff.

        The first status reads after MPRIS ``play`` return the stale Paused
        state (cross-device transfer in progress); the start boundary must
        keep polling until the real Playing edge appears and then succeed.
        """
        reads = iter([_spotify_state("Paused"), _spotify_state("Paused"), _spotify_state("Playing")])

        async def fake_status(*_args, **_kwargs):
            return next(reads)

        runtime = make_transition_runtime()
        with patch.object(transition_source, "spotify_play", new=AsyncMock(return_value=_spotify_state("Paused"))), \
                patch.object(main, "get_spotify_ui_state", new=fake_status):
            await runtime.start_target_source(_spotify_toggle_request())
        # No exception: the bounded wait found the Playing edge.

    async def test_play_times_out_bounded_when_spotify_stays_paused(self):
        """A genuinely stuck Spotify still fails the start boundary."""
        runtime = make_transition_runtime()
        with patch.object(transition_source, "spotify_play", new=AsyncMock(return_value=_spotify_state("Paused"))), \
                patch.object(main, "get_spotify_ui_state", new=AsyncMock(return_value=_spotify_state("Paused"))):
            with self.assertRaisesRegex(RuntimeError, "Spotify did not enter Playing state"):
                await runtime.start_target_source(_spotify_toggle_request())

    async def test_pause_request_skips_playing_confirm(self):
        """should_play=False never polls for a Playing edge."""
        runtime = make_transition_runtime()
        request = TransitionRequest(
            operation="spotify-pause",
            source="spotify",
            target_rate=44100,
            should_play=False,
            rate_change=False,
            reload_source=False,
            detail="test-spotify-pause",
        )
        with patch.object(main, "get_spotify_ui_state", new=AsyncMock(return_value=_spotify_state("Paused"))) as status:
            await runtime.start_target_source(request)
        status.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
