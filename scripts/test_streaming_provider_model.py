# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for the streaming provider foundation.

Covers the provider/capability model, the Spotify backend detection and the
metadata normalization of the playerctl status read. No real playerctl or
MPRIS is required; the low-level ``_run``/``list_players`` helpers are patched.
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
from streaming.spotify.mpris import (
    detect_backend,
    detect_running_backend,
    player_name,
    spotify_installed,
    spotifyd_standby,
)
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


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_lists_providers_with_implemented_flag(self):
        described = {p["id"]: p for p in await streaming.describe_providers()}
        self.assertIn("spotify", described)
        self.assertIn("qobuz", described)
        self.assertIn("tidal", described)
        self.assertTrue(described["spotify"]["implemented"])
        self.assertTrue(described["qobuz"]["implemented"])
        self.assertTrue(described["tidal"]["implemented"])

    async def test_describe_includes_authenticated_field(self):
        described = {p["id"]: p for p in await streaming.describe_providers()}
        for provider_id in ("spotify", "qobuz", "tidal"):
            self.assertIn("authenticated", described[provider_id])
        # Spotify controls an external player and has no FXRoute-side account.
        self.assertIsNone(described["spotify"]["authenticated"])

    def test_get_provider_returns_instance_and_unknown_is_none(self):
        self.assertIsInstance(streaming.get_provider("spotify"), SpotifyProvider)
        self.assertIsNone(streaming.get_provider("unknown"))

    async def test_tidal_without_dependency_reports_implemented_but_unavailable(self):
        # Simulate a host without tidalapi regardless of the local environment
        # (the dependency is present on some test hosts): TIDAL stays a real
        # provider (implemented=True) but is not usable (available=False).
        with mock.patch("streaming.tidal.auth.tidalapi_available", return_value=False):
            tidal = streaming.get_provider("tidal")
            described = await tidal.describe()
            self.assertTrue(described["implemented"])
            self.assertFalse(described["available"])
            self.assertFalse(described["installed"])
            self.assertIsNone(described["backend"])


class SpotifyBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_backend_prefers_desktop_when_only_desktop(self):
        with mock.patch("streaming.spotify.mpris.list_players", new=_players(["spotify"])):
            self.assertEqual(await detect_running_backend(), "desktop")

    async def test_running_backend_prefers_spotifyd_when_only_spotifyd(self):
        with mock.patch("streaming.spotify.mpris.list_players", new=_players(["spotifyd"])):
            self.assertEqual(await detect_running_backend(), "spotifyd")

    async def test_running_backend_desktop_wins_when_both_running(self):
        with mock.patch("streaming.spotify.mpris.list_players", new=_players(["spotify", "spotifyd"])):
            self.assertEqual(await detect_running_backend(), "desktop")

    async def test_running_backend_none_when_none_running(self):
        with mock.patch("streaming.spotify.mpris.list_players", new=_players([])):
            self.assertIsNone(await detect_running_backend())

    async def test_detect_backend_falls_back_to_install_profile(self):
        with mock.patch("streaming.spotify.mpris.list_players", new=_players([])), \
             mock.patch("streaming.spotify.mpris._spotify_desktop_installed", return_value=False), \
             mock.patch("streaming.spotify.mpris._spotifyd_installed", return_value=True):
            self.assertEqual(await detect_backend(), "spotifyd")

    async def test_detect_backend_none_when_neither_installed(self):
        with mock.patch("streaming.spotify.mpris.list_players", new=_players([])), \
             mock.patch("streaming.spotify.mpris._spotify_desktop_installed", return_value=False), \
             mock.patch("streaming.spotify.mpris._spotifyd_installed", return_value=False):
            self.assertIsNone(await detect_backend())

    def test_player_name_maps_backends(self):
        self.assertEqual(player_name("desktop"), "spotify")
        self.assertEqual(player_name("spotifyd"), "spotifyd")
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
             mock.patch("streaming.spotify.mpris.list_players", new=_players(["spotify"])), \
             mock.patch("streaming.spotify.mpris._run", side_effect=fake_run):
            status = await provider.status()

        self.assertEqual(status["source"], "spotify")
        self.assertEqual(status["backend"], "desktop")
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


class SpotifydStandbyTests(unittest.IsolatedAsyncioTestCase):
    """spotifyd 0.4.x hides MPRIS without an active Connect session; the
    always-present controls name must still let the UI show standby."""

    @staticmethod
    def _fake_dbus_send(names):
        class FakeProc:
            def __init__(self):
                self._out = "\n".join(f'      string "{n}"' for n in names).encode()
                self.returncode = 0

            async def communicate(self):
                return (self._out, b"")

            def kill(self):
                pass

            def terminate(self):
                pass

            async def wait(self):
                return self.returncode

        async def fake_exec(*args, **kwargs):
            return FakeProc()

        return fake_exec

    async def test_standby_true_when_controls_name_visible(self):
        with mock.patch("streaming.spotify.mpris._find_dbus_send", return_value="/usr/bin/dbus-send"), \
             mock.patch("asyncio.create_subprocess_exec",
                        new=self._fake_dbus_send(["org.mpris.MediaPlayer2.com.blitzfc.qbz",
                                                  "rs.spotifyd.instance1681535"])):
            self.assertTrue(await spotifyd_standby())

    async def test_standby_false_without_spotifyd_name(self):
        with mock.patch("streaming.spotify.mpris._find_dbus_send", return_value="/usr/bin/dbus-send"), \
             mock.patch("asyncio.create_subprocess_exec",
                        new=self._fake_dbus_send(["org.mpris.MediaPlayer2.com.blitzfc.qbz"])):
            self.assertFalse(await spotifyd_standby())

    async def test_standby_false_when_dbus_send_missing(self):
        with mock.patch("streaming.spotify.mpris._find_dbus_send", return_value=None):
            self.assertFalse(await spotifyd_standby())

    async def test_status_flags_standby_when_no_mpris_player(self):
        async def fake_run(*args, timeout=4.0):
            return None

        provider = SpotifyProvider()
        with mock.patch("streaming.spotify.mpris.playerctl_available", return_value=True), \
             mock.patch("streaming.spotify.mpris.list_players", new=_players([])), \
             mock.patch("streaming.spotify.mpris._run", side_effect=fake_run), \
             mock.patch("streaming.spotify.mpris.spotifyd_standby", return_value=True):
            status = await provider.status()

        self.assertEqual(status["status"], "Stopped")
        self.assertTrue(status["spotifyd_standby"])

    async def test_status_omits_standby_flag_when_daemon_absent(self):
        async def fake_run(*args, timeout=4.0):
            return None

        provider = SpotifyProvider()
        with mock.patch("streaming.spotify.mpris.playerctl_available", return_value=True), \
             mock.patch("streaming.spotify.mpris.list_players", new=_players([])), \
             mock.patch("streaming.spotify.mpris._run", side_effect=fake_run), \
             mock.patch("streaming.spotify.mpris.spotifyd_standby", return_value=False):
            status = await provider.status()

        self.assertEqual(status["status"], "Stopped")
        self.assertNotIn("spotifyd_standby", status)


def _players(names):
    async def fake_list_players(timeout=2.0):
        return list(names)
    return fake_list_players


if __name__ == "__main__":
    unittest.main()
