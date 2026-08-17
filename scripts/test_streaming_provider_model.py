# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for the streaming provider foundation.

Covers the provider/capability model, the Spotify backend detection and the
metadata normalization of the playerctl status read. No real playerctl or
MPRIS is required; the low-level ``_run`` helper is patched.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import streaming
from streaming.base.capabilities import CAPABILITY_NAMES, Capabilities
from streaming.base.models import PlaybackState, ProviderState, Track
from streaming.spotify.mpris import detect_backend, player_name, spotify_installed
from streaming.spotify.provider import SpotifyProvider


class CapabilitiesModelTests(unittest.TestCase):
    def test_defaults_are_all_false(self):
        caps = Capabilities()
        self.assertEqual(caps.supported(), set())
        self.assertTrue(all(value is False for value in caps.to_dict().values()))

    def test_to_dict_covers_every_canonical_name(self):
        caps = Capabilities(transport=True, seek=True, cover=True)
        payload = caps.to_dict()
        self.assertEqual(set(payload), set(CAPABILITY_NAMES))
        self.assertTrue(payload["transport"])
        self.assertTrue(payload["seek"])
        self.assertTrue(payload["cover"])
        self.assertFalse(payload["search"])
        self.assertFalse(payload["bit_depth"])

    def test_from_dict_round_trips_and_ignores_unknown(self):
        payload = {"transport": True, "shuffle": True, "cover": True, "bogus": True}
        caps = Capabilities.from_dict(payload)
        self.assertTrue(caps.transport)
        self.assertTrue(caps.shuffle)
        self.assertTrue(caps.cover)
        self.assertFalse(caps.bit_depth)

    def test_supported_returns_enabled_names_only(self):
        caps = Capabilities(transport=True, seek=True, shuffle=True, loop=True,
                            progress=True, volume=True, cover=True)
        self.assertEqual(
            caps.supported(),
            {"transport", "seek", "shuffle", "loop", "progress", "volume", "cover"},
        )


class ModelsTests(unittest.TestCase):
    def test_provider_state_serialization(self):
        state = ProviderState(
            provider_id="spotify",
            available=True,
            installed=True,
            backend="desktop",
            playback=PlaybackState(
                status="Playing",
                track=Track(id="spotify:track:1", title="T", artist="A", album="L", art_url="http://c"),
                shuffle=True,
                loop="playlist",
                position=10.0,
                duration=200.0,
                volume=75,
            ),
            capabilities={"transport": True, "cover": True},
        )
        payload = state.to_dict()
        self.assertEqual(payload["source"], "spotify")
        self.assertEqual(payload["backend"], "desktop")
        self.assertEqual(payload["status"], "Playing")
        self.assertEqual(payload["trackId"], "spotify:track:1")
        self.assertEqual(payload["title"], "T")
        self.assertEqual(payload["artist"], "A")
        self.assertEqual(payload["album"], "L")
        self.assertEqual(payload["artUrl"], "http://c")
        self.assertEqual(payload["loop"], "playlist")
        self.assertEqual(payload["volume"], 75)


class RegistryTests(unittest.TestCase):
    def test_registry_lists_declared_providers_with_implemented_flag(self):
        described = {p["id"]: p for p in streaming.describe_providers()}
        self.assertIn("spotify", described)
        self.assertIn("qobuz", described)
        self.assertIn("tidal", described)
        self.assertTrue(described["spotify"]["implemented"])
        self.assertFalse(described["qobuz"]["implemented"])
        self.assertFalse(described["tidal"]["implemented"])
        # Declared providers never claim to be available or working.
        self.assertFalse(described["qobuz"]["available"])
        self.assertFalse(described["tidal"]["available"])

    def test_get_provider_returns_instance_and_unknown_is_none(self):
        self.assertIsInstance(streaming.get_provider("spotify"), SpotifyProvider)
        self.assertIsNone(streaming.get_provider("unknown"))


class SpotifyBackendTests(unittest.TestCase):
    def test_detect_backend_prefers_desktop(self):
        with mock.patch("streaming.spotify.mpris._spotify_desktop_installed", return_value=True), \
             mock.patch("streaming.spotify.mpris._spotifyd_installed", return_value=True):
            self.assertEqual(detect_backend(), "desktop")

    def test_detect_backend_falls_back_to_spotifyd(self):
        with mock.patch("streaming.spotify.mpris._spotify_desktop_installed", return_value=False), \
             mock.patch("streaming.spotify.mpris._spotifyd_installed", return_value=True):
            self.assertEqual(detect_backend(), "spotifyd")

    def test_detect_backend_none_when_neither_installed(self):
        with mock.patch("streaming.spotify.mpris._spotify_desktop_installed", return_value=False), \
             mock.patch("streaming.spotify.mpris._spotifyd_installed", return_value=False):
            self.assertIsNone(detect_backend())

    def test_player_name_maps_backends(self):
        self.assertEqual(player_name("desktop"), "spotify")
        self.assertEqual(player_name("spotifyd"), "spotifyd")
        # Unknown/absent backend stays on the desktop player for compatibility.
        self.assertEqual(player_name(None), "spotify")

    def test_spotify_installed_is_union_of_backends(self):
        with mock.patch("streaming.spotify.mpris._spotify_desktop_installed", return_value=False), \
             mock.patch("streaming.spotify.mpris._spotifyd_installed", return_value=True):
            self.assertTrue(spotify_installed())
        with mock.patch("streaming.spotify.mpris._spotify_desktop_installed", return_value=True), \
             mock.patch("streaming.spotify.mpris._spotifyd_installed", return_value=False):
            self.assertTrue(spotify_installed())
        with mock.patch("streaming.spotify.mpris._spotify_desktop_installed", return_value=False), \
             mock.patch("streaming.spotify.mpris._spotifyd_installed", return_value=False):
            self.assertFalse(spotify_installed())


class SpotifyCapabilitySurfaceTests(unittest.TestCase):
    def test_spotify_transport_caps_only(self):
        caps = SpotifyProvider().capabilities()
        for name in ("transport", "seek", "shuffle", "loop", "progress", "volume", "cover"):
            self.assertTrue(getattr(caps, name), name)
        for name in ("search", "library", "favorites", "playlists", "recommendations",
                     "radio", "lyrics", "queue_editing", "audio_format",
                     "sample_rate", "bit_depth"):
            self.assertFalse(getattr(caps, name), name)


class SpotifyStatusNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_parses_playerctl_fields(self):
        async def fake_run(*args, timeout=4.0):
            cmd = args[1] if len(args) > 1 else ""
            if cmd == "metadata" and "--format" in args:
                return "Playing|Artist Name|Track Title|Album Name|235000000|spotify:track:abc123"
            if cmd == "metadata" and "mpris:artUrl" in args:
                return "https://example.com/cover.jpg"
            if cmd == "shuffle":
                return "On"
            if cmd == "loop":
                return "Playlist"
            if cmd == "position":
                return "30.5"
            if cmd == "volume":
                return "0.75"
            return None

        provider = SpotifyProvider()
        with mock.patch("streaming.spotify.mpris.playerctl_available", return_value=True), \
             mock.patch("streaming.spotify.mpris._run", side_effect=fake_run), \
             mock.patch("streaming.spotify.mpris.detect_backend", return_value="desktop"):
            status = await provider.status()

        self.assertEqual(status["source"], "spotify")
        self.assertEqual(status["status"], "Playing")
        self.assertEqual(status["artist"], "Artist Name")
        self.assertEqual(status["title"], "Track Title")
        self.assertEqual(status["album"], "Album Name")
        self.assertEqual(status["duration"], 235.0)
        self.assertEqual(status["trackId"], "spotify:track:abc123")
        self.assertEqual(status["artUrl"], "https://example.com/cover.jpg")
        self.assertTrue(status["shuffle"])
        self.assertEqual(status["loop"], "playlist")
        self.assertEqual(status["position"], 30.5)
        self.assertEqual(status["volume"], 75)
        self.assertTrue(status["capabilities"]["transport"])
        self.assertTrue(status["capabilities"]["cover"])
        self.assertFalse(status["capabilities"]["search"])

    async def test_status_reports_unavailable_when_playerctl_missing(self):
        provider = SpotifyProvider()
        with mock.patch("streaming.spotify.mpris.playerctl_available", return_value=False):
            status = await provider.status()
        self.assertFalse(status["available"])
        self.assertEqual(status["status"], "Stopped")
        self.assertEqual(status["source"], "spotify")


if __name__ == "__main__":
    unittest.main()
