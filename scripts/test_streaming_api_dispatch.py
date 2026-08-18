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

    async def get_album_tracks(self, album_id):
        return [{"id": "3", "title": "AT"}]

    async def favorite_state(self):
        return {"tracks": ["1"], "albums": ["a1"], "playlists": ["p1"]}

    async def get_album(self, album_id):
        return {"id": album_id, "title": "Album", "artist": "A", "year": 1996,
                "audio_quality": "LOSSLESS", "num_tracks": 10}

    async def set_track_favorite(self, track_id, favorite):
        return {"type": "track", "id": track_id, "favorite": favorite}

    async def set_album_favorite(self, album_id, favorite):
        return {"type": "album", "id": album_id, "favorite": favorite}

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


class StreamingApiDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main_module.app)

    def _patch(self, provider):
        return mock.patch.object(main_module.streaming, "get_provider", return_value=provider)

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


if __name__ == "__main__":
    unittest.main()
