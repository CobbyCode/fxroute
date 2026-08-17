"""Focused tests for the provider-neutral playback owner model.

Covers the central source policy (source vs engine, graph identity) and the
authoritative ``current_playback_owner`` semantics: pause preserves the owner,
a metadata/status read never mutates it, and global transport actions route to
the owning source's adapter.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import playback.source_policy as source_policy


class SourcePolicyTests(unittest.TestCase):
    def test_engine_mapping(self):
        self.assertEqual(source_policy.engine_for("local"), "mpv")
        self.assertEqual(source_policy.engine_for("radio"), "mpv")
        self.assertEqual(source_policy.engine_for("tidal"), "mpv")
        self.assertEqual(source_policy.engine_for("spotify"), "spotify")
        self.assertEqual(source_policy.engine_for("qobuz"), "qbzd")
        self.assertIsNone(source_policy.engine_for("unknown"))

    def test_classification(self):
        self.assertTrue(source_policy.is_mpv_source("tidal"))
        self.assertFalse(source_policy.is_mpv_source("spotify"))
        self.assertFalse(source_policy.is_mpv_source("qobuz"))
        self.assertTrue(source_policy.is_external_source("qobuz"))
        self.assertTrue(source_policy.is_external_source("spotify"))
        self.assertFalse(source_policy.is_external_source("radio"))
        self.assertTrue(source_policy.is_known_source("local"))
        self.assertFalse(source_policy.is_known_source(None))

    def test_graph_ports(self):
        self.assertEqual(
            source_policy.graph_port_names("spotify"),
            ("spotify:output_FL", "spotify:output_FR"),
        )
        self.assertEqual(
            source_policy.graph_port_names("tidal"),
            ("mpv:output_FL", "mpv:output_FR"),
        )
        self.assertEqual(
            source_policy.graph_port_names("qobuz"),
            ("alsa_playback.qbzd:output_FL", "alsa_playback.qbzd:output_FR"),
        )
        self.assertIsNone(source_policy.graph_port_names(None))


class QobuzReleaseBudgetTests(unittest.TestCase):
    def test_qobuz_release_timeout_exceeds_generic_handoff_timeout(self):
        import main
        # qbzd fully disconnects its ALSA stream on pause instead of corking
        # it like Spotify Desktop (live-measured ~2.2 s), so it needs a
        # dedicated, longer bounded release budget than the 1800 ms Spotify
        # handoff timeout.
        self.assertGreater(
            main.PIPEWIRE_QOBUZ_RELEASE_TIMEOUT_MS,
            main.PIPEWIRE_HANDOFF_RELEASE_TIMEOUT_MS,
        )
        self.assertGreaterEqual(main.PIPEWIRE_QOBUZ_RELEASE_TIMEOUT_MS, 4000)

    def test_qobuz_release_wait_uses_qobuz_timeout_by_default(self):
        import inspect
        import main
        signature = inspect.signature(main._wait_for_pipewire_qobuz_release)
        self.assertEqual(
            signature.parameters["timeout_ms"].default,
            main.PIPEWIRE_QOBUZ_RELEASE_TIMEOUT_MS,
        )


class PlaybackOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import main
        self.main = main
        self._orig_owner = main.playback_state.current_playback_owner
        main.playback_state.current_playback_owner = None

    async def asyncTearDown(self):
        self.main.playback_state.current_playback_owner = self._orig_owner

    async def test_pause_preserves_owner(self):
        self.main._set_playback_owner("qobuz")
        # A paused owner is still the owner: resume/global controls stay
        # unambiguous.
        self.assertEqual(self.main._resolve_playback_owner(), "qobuz")
        self.assertEqual(self.main.playback_state.current_playback_owner, "qobuz")

    async def test_metadata_refresh_does_not_change_owner(self):
        self.main._set_playback_owner("tidal")
        self.main._resolve_playback_owner()
        self.assertEqual(self.main.playback_state.current_playback_owner, "tidal")

    async def test_readonly_derivation_never_persists(self):
        self.main._set_playback_owner(None)
        with mock.patch.object(
            self.main.runtime, "player_instance", mock.Mock(state={
                "current_file": "/music/x.flac", "playing": True, "paused": False, "ended": False,
            }),
        ), mock.patch.object(
            self.main.playback_state, "current_track_info", {"source": "local", "url": "/music/x.flac"}
        ), mock.patch.object(self.main.playback_state, "latest_spotify_state", None), mock.patch.object(
            self.main.playback_state, "latest_qobuz_state", None
        ):
            derived = self.main._derive_playback_owner_readonly()
            self.assertEqual(derived, "local")
            # Derivation is read-only; the committed owner stays None.
            self.assertIsNone(self.main.playback_state.current_playback_owner)

    async def test_route_global_control_by_owner(self):
        with mock.patch.object(
            self.main, "_spotify_global_control", new=mock.AsyncMock(return_value={"routed": "spotify"})
        ) as spot, mock.patch.object(
            self.main, "_qobuz_global_control", new=mock.AsyncMock(return_value={"routed": "qobuz"})
        ) as qob:
            self.main._set_playback_owner("spotify")
            self.assertEqual(await self.main._route_global_control("toggle"), {"routed": "spotify"})
            spot.assert_awaited_once()

            self.main._set_playback_owner("qobuz")
            self.assertEqual(await self.main._route_global_control("next"), {"routed": "qobuz"})
            qob.assert_awaited_once()

            # Native MPV sources fall through to the native transport path.
            self.main._set_playback_owner("tidal")
            self.assertIsNone(await self.main._route_global_control("toggle"))
            self.assertIsNone(await self.main._route_global_control("seek"))

    async def test_commit_coordinated_track_sets_owner(self):
        with mock.patch.object(self.main, "_mark_player_state_authoritative"), mock.patch.object(
            self.main.runtime, "player_instance", mock.Mock(state={})
        ):
            self.main._commit_coordinated_track(
                {"source": "tidal", "id": "1", "url": "https://x/stream"}, source="tidal"
            )
            self.assertEqual(self.main.playback_state.current_playback_owner, "tidal")


if __name__ == "__main__":
    unittest.main()
