# SPDX-License-Identifier: AGPL-3.0-only

"""HTTP-level tests for the generic streaming provider API.

The provider registry's catalog/auth methods are patched with a fake provider
so the route registration, the ``type`` dispatch on favorites, the album/playlist
track endpoints, the auth endpoints and the capability-gated transport action
are exercised end-to-end through FastAPI without any network or tidalapi
dependency.
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main as main_module
from fastapi.testclient import TestClient


class _FakeProvider:
    """Fake provider exposing every generic catalog/auth method the API uses."""

    async def search(self, query, types=None, limit=25):
        return {"tracks": [{"id": "1", "title": "T"}]}

    async def favorites(self, limit=50):
        return [{"id": "1", "title": "Track"}]

    async def favorites_albums(self, limit=50):
        return [{"id": "a1", "title": "Album"}]

    async def favorites_artists(self, limit=50):
        return [{"id": "ar1", "name": "Artist"}]

    async def playlists(self):
        return [{"id": "p1", "name": "Playlist"}]

    async def playlist_tracks(self, playlist_id):
        return [{"id": "2", "title": "PT"}]

    async def get_playlist(self, playlist_id):
        return {"id": playlist_id, "name": "Playlist", "description": "A mix",
                "tracks": [{"id": "2", "title": "PT"}],
                "artists": [{"id": "a1", "name": "Artist"}],
                "enrichment": {"available": True}}

    async def get_album_tracks(self, album_id):
        return [{"id": "3", "title": "AT"}]

    async def favorite_state(self):
        return {"tracks": ["1"], "albums": ["a1"], "artists": ["ar1"], "playlists": ["p1"]}

    async def get_album(self, album_id):
        return {"id": album_id, "title": "Album", "artist": "A", "year": 1996,
                "audio_quality": "LOSSLESS", "num_tracks": 10}

    async def get_artist(self, artist_id):
        return {"id": artist_id, "name": "Artist", "art_url": "https://c/a.jpg",
                "albums": [{"id": "a1", "title": "Album"}],
                "top_tracks": [{"id": "5", "title": "Track"}]}

    async def set_track_favorite(self, track_id, favorite):
        return {"type": "track", "id": track_id, "favorite": favorite}

    async def set_album_favorite(self, album_id, favorite):
        return {"type": "album", "id": album_id, "favorite": favorite}

    async def set_artist_favorite(self, artist_id, favorite):
        return {"type": "artist", "id": artist_id, "favorite": favorite}

    async def set_playlist_favorite(self, playlist_id, favorite):
        return {"type": "playlist", "id": playlist_id, "favorite": favorite}

    async def start_device_login(self):
        return {"user_code": "ABC", "verification_uri_complete": "https://link.tidal.com/ABC"}

    async def finish_device_login(self):
        return {"authenticated": True}

    async def pkce_login_url(self):
        return "https://login.tidal.com/authorize?..."

    async def finish_pkce_login(self, redirect_url):
        return {"authenticated": True, "is_pkce": True}

    async def logout(self):
        return None


class _FailingProvider(_FakeProvider):
    """Raises a provider-neutral error for the error-mapping test."""

    def __init__(self, exc):
        self._exc = exc

    async def search(self, query, types=None, limit=25):
        raise self._exc


class _NoTransportProvider(_FakeProvider):
    """Fake provider without seek/volume, mirroring the base-class default
    (``StreamingProvider.seek``/``set_volume`` raise ProviderNotImplemented)."""

    async def seek(self, position_sec):
        raise main_module.streaming.ProviderNotImplemented("tidal", "seek")

    async def set_volume(self, percent):
        raise main_module.streaming.ProviderNotImplemented("tidal", "set_volume")


class StreamingApiDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main_module.app)

    def _patch(self, provider):
        return mock.patch.object(main_module.streaming, "get_provider", return_value=provider)

    def test_provider_discovery_uses_lightweight_registry_contract(self):
        payload = [{"id": "tidal", "name": "TIDAL", "implemented": True, "installed": True, "capabilities": {}}]
        with mock.patch.object(main_module.streaming, "discover_providers", new=mock.AsyncMock(return_value=payload)):
            resp = self.client.get("/api/streaming/providers/discovery")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"providers": payload})

    def test_search_dispatches_to_provider(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/search?q=daft&types=tracks")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["tracks"][0]["id"], "1")

    def test_favorites_defaults_to_tracks(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/favorites")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["id"], "1")

    def test_favorites_albums_dispatch(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/favorites?type=albums")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["id"], "a1")

    def test_favorites_artists_dispatch(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/favorites?type=artists")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["name"], "Artist")

    def test_favorites_unknown_type_is_400(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/favorites?type=unknown")
        self.assertEqual(resp.status_code, 400)

    def test_playlists_dispatch(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/playlists")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["name"], "Playlist")

    def test_playlist_tracks_dispatch(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/playlists/p1/tracks")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["title"], "PT")

    def test_playlist_meta_dispatch(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/playlists/p1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Playlist")
        self.assertEqual(resp.json()["description"], "A mix")
        self.assertEqual(len(resp.json()["tracks"]), 1)
        self.assertEqual(resp.json()["enrichment"]["available"], True)

    def test_album_tracks_dispatch(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/albums/a1/tracks")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["title"], "AT")

    def test_favorite_ids_dispatch(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/favorites/ids")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["tracks"], ["1"])
        self.assertEqual(resp.json()["albums"], ["a1"])

    def test_album_meta_dispatch(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/albums/a1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["title"], "Album")
        self.assertEqual(resp.json()["year"], 1996)

    def test_artist_meta_dispatch(self):
        with self._patch(_FakeProvider()):
            resp = self.client.get("/api/streaming/tidal/artists/ar1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Artist")
        self.assertEqual(len(resp.json()["albums"]), 1)
        self.assertEqual(len(resp.json()["top_tracks"]), 1)

    def test_track_favorite_post(self):
        with self._patch(_FakeProvider()):
            resp = self.client.post("/api/streaming/tidal/tracks/9/favorite", json={"favorite": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["favorite"], True)

    def test_album_favorite_post(self):
        with self._patch(_FakeProvider()):
            resp = self.client.post("/api/streaming/tidal/albums/a1/favorite", json={"favorite": False})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["favorite"], False)

    def test_artist_favorite_post(self):
        with self._patch(_FakeProvider()):
            resp = self.client.post("/api/streaming/tidal/artists/ar1/favorite", json={"favorite": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["favorite"], True)
        self.assertEqual(resp.json()["type"], "artist")
        self.assertEqual(resp.json()["id"], "ar1")

    def test_playlist_favorite_post(self):
        with self._patch(_FakeProvider()):
            resp = self.client.post("/api/streaming/tidal/playlists/p1/favorite", json={"favorite": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["favorite"], True)
        self.assertEqual(resp.json()["type"], "playlist")
        self.assertEqual(resp.json()["id"], "p1")

    def test_auth_pkce_url(self):
        with self._patch(_FakeProvider()):
            resp = self.client.post("/api/streaming/tidal/auth/pkce")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["url"].startswith("https://login.tidal.com"))

    def test_auth_pkce_finish_requires_redirect_url(self):
        with self._patch(_FakeProvider()):
            resp = self.client.post("/api/streaming/tidal/auth/pkce/finish", json={})
        self.assertEqual(resp.status_code, 400)

    def test_auth_pkce_finish(self):
        with self._patch(_FakeProvider()):
            resp = self.client.post(
                "/api/streaming/tidal/auth/pkce/finish",
                json={"redirect_url": "https://tidal.com/android/login/auth?code=x"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["is_pkce"])

    def test_provider_auth_error_maps_to_401(self):
        import streaming.tidal.auth as tidal_auth

        failing = _FailingProvider(tidal_auth.TidalAuthError("not authenticated"))
        with self._patch(failing):
            resp = self.client.get("/api/streaming/tidal/search?q=x")
        self.assertEqual(resp.status_code, 401)


class GenericTransportActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main_module.app)

    async def _status(self):
        return {"status": "Paused"}

    def test_transport_action_dispatches_by_capability(self):
        provider = _FakeProvider()
        provider.toggle = lambda: self._status()
        # A non-Qobuz provider id exercises the generic capability dispatch;
        # qobuz play/toggle is routed through the source handoff instead.
        with mock.patch.object(main_module.streaming, "get_provider", return_value=provider):
            resp = self.client.post("/api/streaming/fake/toggle")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "Paused")

    def test_unknown_action_is_404(self):
        provider = _FakeProvider()
        with mock.patch.object(main_module.streaming, "get_provider", return_value=provider):
            resp = self.client.post("/api/streaming/qobuz/bogus")
        self.assertEqual(resp.status_code, 404)

    def test_seek_unimplemented_maps_to_501(self):
        # A provider without seek (e.g. TIDAL, whose transport rides the shared
        # FXRoute owner) must surface 501 like the other transport actions, not
        # an unhandled 500.
        provider = _NoTransportProvider()
        with mock.patch.object(main_module.streaming, "get_provider", return_value=provider):
            resp = self.client.post("/api/streaming/tidal/seek", json={"position": 30})
        self.assertEqual(resp.status_code, 501)

    def test_volume_unimplemented_maps_to_501(self):
        provider = _NoTransportProvider()
        with mock.patch.object(main_module.streaming, "get_provider", return_value=provider):
            resp = self.client.post("/api/streaming/tidal/volume", json={"volume": 50})
        self.assertEqual(resp.status_code, 501)

    def test_qobuz_toggle_playing_is_transport_only(self):
        playing = {"available": True, "status": "Playing", "trackId": "7"}
        with mock.patch.object(
            main_module.streaming, "get_provider", return_value=_FakeProvider()
        ), mock.patch.object(
            main_module, "get_qobuz_ui_state", new=mock.AsyncMock(return_value=playing)
        ), mock.patch.object(
            main_module, "_is_qobuz_playback_active", return_value=True
        ), mock.patch.object(
            main_module, "qobuz_pause", new=mock.AsyncMock(return_value={"status": "Paused"})
        ) as pause, mock.patch.object(
            main_module, "broadcast_qobuz_state", new=mock.AsyncMock(side_effect=lambda d: d)
        ), mock.patch.object(
            main_module, "_run_coordinated_transition", new=mock.AsyncMock()
        ) as run:
            resp = self.client.post("/api/streaming/qobuz/toggle")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "Paused")
        pause.assert_awaited_once()
        run.assert_not_awaited()


class QobuzUiStartHandoffTests(unittest.IsolatedAsyncioTestCase):
    """Qobuz UI play/toggle must ride the authoritative source handoff and
    publish the committed owner on the playback broadcast, mirroring the
    Spotify endpoints instead of the raw provider transport."""

    async def asyncSetUp(self):
        self._orig_owner = main_module.playback_state.current_playback_owner
        main_module.playback_state.current_playback_owner = None
        self.committed = type(
            "Result", (),
            {"committed": True, "transition_id": "tr-qobuz-1"},
        )()

    async def asyncTearDown(self):
        main_module.playback_state.current_playback_owner = self._orig_owner

    def _paused_state(self):
        return {
            "available": True, "status": "Paused", "title": "T", "artist": "A",
            "album": "B", "trackId": "123", "artUrl": "http://x/c.jpg",
            "sample_rate": 88200,
        }

    def _patches(self, action, state):
        return [
            mock.patch.object(
                main_module, "get_qobuz_ui_state", new=mock.AsyncMock(return_value=state)
            ),
            mock.patch.object(
                main_module, "_is_qobuz_playback_active",
                return_value=state.get("status") == "Playing",
            ),
            mock.patch.object(
                main_module, "qobuz_pause", new=mock.AsyncMock(return_value={"status": "Paused"})
            ),
            mock.patch.object(
                main_module, "broadcast_qobuz_state",
                new=mock.AsyncMock(
                    side_effect=lambda *a, **k: a[0] if a else state
                ),
            ),
            mock.patch.object(
                main_module, "_run_coordinated_transition",
                new=mock.AsyncMock(return_value=self.committed),
            ),
            mock.patch.object(
                main_module.manager, "broadcast", new=mock.AsyncMock(),
            ),
            mock.patch.object(
                main_module, "build_playback_payload",
                return_value={"playback_owner": "qobuz"},
            ),
        ]

    async def test_toggle_from_paused_commits_qobuz_through_coordinator(self):
        state = self._paused_state()
        patches = self._patches("toggle", state)
        with patches[0], patches[1], patches[2], patches[3], patches[4] as run, \
                patches[5] as manager, patches[6]:
            result = await main_module._qobuz_ui_start_action("toggle")
        run.assert_awaited_once()
        request = run.await_args.args[0]
        self.assertEqual(request.source, "qobuz")
        self.assertEqual(request.operation, "qobuz-toggle")
        self.assertTrue(request.should_play)
        self.assertTrue(request.reload_source)
        self.assertEqual(request.target_rate, 88200)
        self.assertEqual(main_module.playback_state.current_playback_owner, "qobuz")
        playback_call = next(
            c for c in manager.await_args_list
            if c.args[0].get("type") == "playback"
        )
        self.assertEqual(
            playback_call.args[0]["data"]["playback_owner"], "qobuz"
        )
        self.assertEqual(result["status"], "Paused")

    async def test_play_from_paused_commits_qobuz_through_coordinator(self):
        state = self._paused_state()
        patches = self._patches("play", state)
        with patches[0], patches[1], patches[2], patches[3], patches[4] as run, \
                patches[5] as manager, patches[6]:
            await main_module._qobuz_ui_start_action("play")
        run.assert_awaited_once()
        request = run.await_args.args[0]
        self.assertEqual(request.operation, "qobuz-play")
        self.assertEqual(main_module.playback_state.current_playback_owner, "qobuz")
        playback_call = next(
            c for c in manager.await_args_list
            if c.args[0].get("type") == "playback"
        )
        self.assertEqual(
            playback_call.args[0]["data"]["playback_owner"], "qobuz"
        )

    async def test_toggle_from_playing_never_runs_coordinator(self):
        state = dict(self._paused_state(), status="Playing")
        patches = self._patches("toggle", state)
        with patches[0], patches[1], patches[2] as pause, patches[3], \
                patches[4] as run, patches[5], patches[6]:
            await main_module._qobuz_ui_start_action("toggle")
        pause.assert_awaited_once()
        run.assert_not_awaited()
        self.assertIsNone(main_module.playback_state.current_playback_owner)

    async def test_play_while_qobuz_owner_active_is_noop(self):
        # A repeated play for the committed, already-playing Qobuz owner must
        # be idempotent: no source handoff, no coordinator transition, no
        # pause, no playback re-broadcast.
        state = dict(self._paused_state(), status="Playing")
        main_module.playback_state.current_playback_owner = "qobuz"
        patches = self._patches("play", state)
        with patches[0], patches[1], patches[2] as pause, patches[3], \
                patches[4] as run, patches[5] as manager, patches[6]:
            result = await main_module._qobuz_ui_start_action("play")
        run.assert_not_awaited()
        pause.assert_not_awaited()
        manager.assert_not_awaited()
        self.assertEqual(result["status"], "Playing")
        self.assertEqual(main_module.playback_state.current_playback_owner, "qobuz")

    async def test_play_after_pause_still_resumes_through_coordinator(self):
        # Idempotency applies only to an already active Qobuz owner: a paused
        # committed owner must still resume through the authoritative handoff.
        state = self._paused_state()
        main_module.playback_state.current_playback_owner = "qobuz"
        patches = self._patches("play", state)
        with patches[0], patches[1], patches[2], patches[3], patches[4] as run, \
                patches[5] as manager, patches[6]:
            await main_module._qobuz_ui_start_action("play")
        run.assert_awaited_once()
        request = run.await_args.args[0]
        self.assertEqual(request.operation, "qobuz-play")
        self.assertTrue(request.should_play)


class SpotifyClaimRaceTests(unittest.IsolatedAsyncioTestCase):
    """The MPRIS watcher claim must re-validate the committed owner inside
    the transition lock, so a claim queued behind an FXRoute-initiated
    Spotify start becomes a no-op instead of closing the output gate over
    already-audible audio."""

    def _playing_state(self):
        return {
            "available": True, "status": "Playing", "title": "T", "artist": "A",
            "album": "B", "trackId": "42", "artUrl": "http://x/c.jpg",
            "sample_rate": 44100,
        }

    async def asyncSetUp(self):
        self._orig_owner = main_module.playback_state.current_playback_owner
        main_module.playback_state.current_playback_owner = None

    async def asyncTearDown(self):
        main_module.playback_state.current_playback_owner = self._orig_owner

    def _committed_result(self):
        return type("Result", (), {
            "committed": True,
            "transition_id": "tr-claim",
            "source": "spotify",
            "target_rate": 44100,
            "state": {"committed": True},
        })()

    def _skipped_result(self):
        return type("Result", (), {
            "committed": False,
            "transition_id": "tr-claim-skipped",
            "source": "spotify",
            "target_rate": 44100,
            "state": {"committed": False, "skipped": True,
                       "reason": "claim-owner-already-committed"},
        })()

    def _patches(self, run_transition):
        return [
            mock.patch.object(
                main_module, "get_spotify_ui_state",
                new=mock.AsyncMock(return_value=self._playing_state()),
            ),
            mock.patch.object(
                main_module, "_is_spotify_playback_active", return_value=True
            ),
            mock.patch.object(
                main_module, "_run_coordinated_transition", new=run_transition
            ),
            mock.patch.object(
                main_module, "_publish_committed_playback_owner",
                new=mock.AsyncMock(),
            ),
            mock.patch.object(
                main_module, "broadcast_spotify_state",
                new=mock.AsyncMock(return_value=self._playing_state()),
            ),
        ]

    async def test_claim_queued_behind_spotify_commit_is_skipped_in_lock(self):
        # The claim's pre-lock guard sees owner=None; inside the lock the
        # FXRoute-initiated start has already committed spotify, so the claim
        # must be skipped without publishing or broadcasting.
        captured = {}

        async def run_transition(request):
            captured["request"] = request
            # The initiating start commits the owner synchronously after
            # releasing the lock, before the queued claim can acquire it.
            main_module.playback_state.current_playback_owner = "spotify"
            if await request.skip_if_committed_owner():
                return self._skipped_result()
            return self._committed_result()

        patches = self._patches(run_transition)
        with patches[0], patches[1], patches[2], patches[3] as publish, patches[4] as broadcast:
            result = await main_module._claim_spotify_playback("playerctl-playing")

        request = captured["request"]
        self.assertIsNotNone(request.skip_if_committed_owner)
        self.assertEqual(request.operation, "spotify-claim")
        self.assertEqual(request.source, "spotify")
        self.assertEqual(request.detail, "playerctl-playing")
        self.assertTrue(request.reload_source)
        publish.assert_not_awaited()
        broadcast.assert_not_awaited()
        self.assertEqual(result["status"], "Playing")

    async def test_claim_revalidation_tracks_committed_owner(self):
        # The lock-side callback reflects the committed owner: with no
        # same-source commit behind it, the claim must proceed and publish.
        async def run_transition(request):
            if await request.skip_if_committed_owner():
                return self._skipped_result()
            return self._committed_result()

        patches = self._patches(run_transition)
        with patches[0], patches[1], patches[2], patches[3] as publish, patches[4] as broadcast:
            result = await main_module._claim_spotify_playback("playerctl-playing")

        publish.assert_awaited_once_with("spotify", "tr-claim")
        broadcast.assert_awaited_once()
        self.assertEqual(result["status"], "Playing")


class QobuzClaimRaceTests(unittest.IsolatedAsyncioTestCase):
    """The qbzd claim watcher must re-validate the committed owner inside
    the transition lock, mirroring the Spotify claim contract: a claim queued
    behind an FXRoute-initiated Qobuz start becomes a no-op instead of
    re-running the handoff over already-playing audio."""

    def _playing_state(self):
        return {
            "available": True, "status": "Playing", "title": "T", "artist": "A",
            "album": "B", "trackId": "42", "artUrl": "http://x/c.jpg",
            "sample_rate": 88200,
        }

    async def asyncSetUp(self):
        self._orig_owner = main_module.playback_state.current_playback_owner
        main_module.playback_state.current_playback_owner = None

    async def asyncTearDown(self):
        main_module.playback_state.current_playback_owner = self._orig_owner

    def _committed_result(self):
        return type("Result", (), {
            "committed": True,
            "transition_id": "tr-claim",
            "source": "qobuz",
            "target_rate": 88200,
            "state": {"committed": True},
        })()

    def _skipped_result(self):
        return type("Result", (), {
            "committed": False,
            "transition_id": "tr-claim-skipped",
            "source": "qobuz",
            "target_rate": 88200,
            "state": {"committed": False, "skipped": True,
                       "reason": "claim-owner-already-committed"},
        })()

    def _patches(self, run_transition):
        return [
            mock.patch.object(
                main_module, "get_qobuz_ui_state",
                new=mock.AsyncMock(return_value=self._playing_state()),
            ),
            mock.patch.object(
                main_module, "_is_qobuz_playback_active", return_value=True
            ),
            mock.patch.object(
                main_module, "_run_coordinated_transition", new=run_transition
            ),
            mock.patch.object(
                main_module, "_publish_committed_playback_owner",
                new=mock.AsyncMock(),
            ),
            mock.patch.object(
                main_module, "_qobuz_pin_unity", new=mock.AsyncMock(),
            ),
            mock.patch.object(
                main_module, "broadcast_qobuz_state",
                new=mock.AsyncMock(return_value=self._playing_state()),
            ),
        ]

    async def test_claim_queued_behind_qobuz_commit_is_skipped_in_lock(self):
        # The claim's pre-lock guard sees owner=None; inside the lock the
        # FXRoute-initiated start has already committed qobuz, so the claim
        # must be skipped without publishing, pinning or broadcasting.
        captured = {}

        async def run_transition(request):
            captured["request"] = request
            main_module.playback_state.current_playback_owner = "qobuz"
            if await request.skip_if_committed_owner():
                return self._skipped_result()
            return self._committed_result()

        patches = self._patches(run_transition)
        with patches[0], patches[1], patches[2], patches[3] as publish, \
                patches[4] as pin, patches[5] as broadcast:
            result = await main_module._claim_qobuz_playback("qbzd-playing")

        request = captured["request"]
        self.assertIsNotNone(request.skip_if_committed_owner)
        self.assertEqual(request.operation, "qobuz-claim")
        self.assertEqual(request.source, "qobuz")
        self.assertEqual(request.detail, "qbzd-playing")
        self.assertFalse(request.reload_source)
        publish.assert_not_awaited()
        pin.assert_not_awaited()
        broadcast.assert_not_awaited()
        self.assertEqual(result["status"], "Playing")

    async def test_claim_revalidation_tracks_committed_owner(self):
        # With no same-source commit behind it, the claim must proceed,
        # publish the owner and pin qbzd unity as before.
        async def run_transition(request):
            if await request.skip_if_committed_owner():
                return self._skipped_result()
            return self._committed_result()

        patches = self._patches(run_transition)
        with patches[0], patches[1], patches[2], patches[3] as publish, \
                patches[4] as pin, patches[5] as broadcast:
            result = await main_module._claim_qobuz_playback("qbzd-playing")

        publish.assert_awaited_once_with("qobuz", "tr-claim")
        pin.assert_awaited_once()
        broadcast.assert_awaited_once()
        self.assertEqual(result["status"], "Playing")


if __name__ == "__main__":
    unittest.main()
