# SPDX-License-Identifier: AGPL-3.0-only

"""Spotify provider (playerctl/MPRIS, desktop and spotifyd backends)."""

from streaming.spotify.provider import (
    SPOTIFY_PREARM_SAMPLE_RATE_HZ,
    SpotifyProvider,
    get_status,
    loop_cycle,
    next_track,
    pause,
    play,
    previous,
    seek_to,
    set_volume,
    shuffle_toggle,
    toggle,
)

__all__ = [
    "SPOTIFY_PREARM_SAMPLE_RATE_HZ",
    "SpotifyProvider",
    "get_status",
    "loop_cycle",
    "next_track",
    "pause",
    "play",
    "previous",
    "seek_to",
    "set_volume",
    "shuffle_toggle",
    "toggle",
]
