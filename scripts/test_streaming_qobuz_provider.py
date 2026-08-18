# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for the Qobuz provider (qbzd control plane).

No qbzd daemon or network is required: the ``streaming.qobuz.backend`` HTTP
helpers are patched with canned JSON.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.qobuz.backend import default_base_url, qbzd_installed
from streaming.qobuz.provider import QobuzProvider


def _status_payload(**overrides):
    payload = {
        "auth": {"state": "logged_in", "subscription": "studio", "user_id": 123},
        "audio": {"backend": "pipewire", "sample_rate": 96000, "bit_depth": 24, "device_open": True},
        "playback": {"state": "playing", "title": None, "artist": None, "track_id": None,
                     "duration": None, "position": None, "volume": 0.75, "muted": False},
        "qconnect": {"device_name": "QBZ (fxroute)", "enabled": True, "session_active": True, "state": "on"},
        "network": {"online": True},
    }
    payload.update(overrides)
    return payload


def _now_playing_payload(**overrides):
    payload = {
        "playback": {
            "is_playing": True, "position": 30, "duration": 200, "track_id": 42,
            "volume": 0.8, "shuffle": True, "repeat": "all",
            "sample_rate": 96000, "bit_depth": 24, "muted": False, "queue_len": 3,
        },
        "track": {
            "id": 42, "title": "T", "artist": "A", "album": "L",
            "duration_secs": 200, "artwork_url": "https://art/q.jpg",
            "hires": True, "bit_depth": 24, "sample_rate": 96.0, "source": "qobuz",
        },
    }
    payload.update(overrides)
    return payload


class QobuzBackendTests(unittest.TestCase):
    def test_default_base_url(self):
        self.assertEqual(default_base_url(), "http://127.0.0.1:8182")

    def test_qbzd_installed_detection(self):
        with mock.patch("streaming.qobuz.backend.shutil.which", return_value="/usr/bin/qbzd"):
            self.assertTrue(qbzd_installed())
        with mock.patch("streaming.qobuz.backend.shutil.which", return_value=None):
            self.assertFalse(qbzd_installed())


class QobuzCapabilityTests(unittest.TestCase):
    def test_implemented_surface_only(self):
        caps = QobuzProvider().capabilities()
        for name in ("transport", "seek", "shuffle", "loop", "progress", "volume",
                     "cover", "audio_format", "sample_rate", "bit_depth"):
            self.assertTrue(getattr(caps, name), name)
        # Catalog is not implemented yet and must not be advertised.
        for name in ("search", "library", "favorites", "playlists", "recommendations",
                     "radio", "lyrics", "queue_editing"):
            self.assertFalse(getattr(caps, name), name)


class QobuzAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_constant_when_installed(self):
        provider = QobuzProvider()
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True):
            self.assertEqual(await provider.backend(), "qbzd")
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=False):
            self.assertIsNone(await provider.backend())

    async def test_available_requires_installed_and_reachable(self):
        provider = QobuzProvider()
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=False):
            self.assertFalse(await provider.is_available())
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)):
            self.assertTrue(await provider.is_available())
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(False)):
            self.assertFalse(await provider.is_available())


class QobuzStatusNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_playing_state_with_full_metadata(self):
        provider = QobuzProvider()
        getter = _fake_get({"/api/status": _status_payload(), "/api/now-playing": _now_playing_payload()})
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            status = await provider.status()

        self.assertEqual(status["source"], "qobuz")
        self.assertEqual(status["backend"], "qbzd")
        self.assertTrue(status["authenticated"])
        self.assertTrue(status["connected"])
        self.assertEqual(status["status"], "Playing")
        self.assertEqual(status["title"], "T")
        self.assertEqual(status["artist"], "A")
        self.assertEqual(status["album"], "L")
        self.assertEqual(status["trackId"], "42")
        self.assertEqual(status["artUrl"], "https://art/q.jpg")
        self.assertEqual(status["duration"], 200.0)
        self.assertEqual(status["position"], 30.0)
        self.assertEqual(status["sample_rate"], 96000)  # 96.0 kHz -> Hz
        self.assertEqual(status["bit_depth"], 24)
        self.assertEqual(status["audio_format"], "flac")
        self.assertTrue(status["shuffle"])
        self.assertEqual(status["loop"], "playlist")
        self.assertEqual(status["volume"], 80)

    async def test_khz_sample_rate_normalized_and_lowres_still_flac(self):
        provider = QobuzProvider()
        payload = _now_playing_payload(
            track={"id": 7, "title": "N", "artist": "X", "album": "Y",
                   "duration_secs": 200, "artwork_url": "", "hires": False,
                   "bit_depth": 16, "sample_rate": 44.1, "source": "qobuz"},
        )
        getter = _fake_get({"/api/status": _status_payload(), "/api/now-playing": payload})
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            status = await provider.status()

        self.assertEqual(status["sample_rate"], 44100)  # 44.1 kHz -> Hz
        self.assertEqual(status["bit_depth"], 16)
        self.assertEqual(status["audio_format"], "flac")

    async def test_unauthenticated_state_falls_back_to_status_summary(self):
        provider = QobuzProvider()
        payload = _status_payload(
            auth={"state": "needs_auth", "subscription": None, "user_id": None},
            qconnect={"device_name": "QBZ (fxroute)", "enabled": False, "session_active": False, "state": "off"},
        )
        getter = _fake_get({"/api/status": payload, "/api/now-playing": None})
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            status = await provider.status()

        self.assertFalse(status["authenticated"])
        self.assertFalse(status["connected"])
        self.assertEqual(status["status"], "Playing")
        self.assertEqual(status["trackId"], "")
        self.assertEqual(status["sample_rate"], 96000)  # from /api/status audio (Hz)
        self.assertEqual(status["bit_depth"], 24)

    async def test_unavailable_qbzd_reports_stopped_without_network(self):
        provider = QobuzProvider()
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=False), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=AssertionError("must not be called")):
            status = await provider.status()
        self.assertFalse(status["available"])
        self.assertEqual(status["status"], "Stopped")
        self.assertEqual(status["source"], "qobuz")


class QobuzTransportDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_posts_correct_routes_and_bodies(self):
        provider = QobuzProvider()
        calls = []

        async def fake_post(base_url, path, body=None, timeout=2.0):
            calls.append((path, body or {}))
            return {}

        getter = _fake_get({"/api/status": _status_payload(), "/api/now-playing": _now_playing_payload()})
        with mock.patch("streaming.qobuz.backend.post_json", side_effect=fake_post), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter), \
             mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)):
            await provider.play()
            await provider.pause()
            await provider.toggle()
            await provider.next()
            await provider.previous()
            await provider.seek(90.4)
            await provider.set_volume(50)

        self.assertIn(("/api/playback/play", {}), calls)
        self.assertIn(("/api/playback/pause", {}), calls)
        self.assertIn(("/api/playback/toggle", {}), calls)
        self.assertIn(("/api/playback/next", {}), calls)
        self.assertIn(("/api/playback/previous", {}), calls)
        self.assertIn(("/api/playback/seek", {"position": 90}), calls)
        self.assertIn(("/api/playback/volume", {"volume": 0.5}), calls)

    async def test_repeat_cycles_and_shuffle_toggles(self):
        provider = QobuzProvider()
        calls = []

        async def fake_post(base_url, path, body=None, timeout=2.0):
            calls.append((path, body or {}))
            return {}

        getter = _fake_get({"/api/status": _status_payload(), "/api/now-playing": _now_playing_payload()})
        with mock.patch("streaming.qobuz.backend.post_json", side_effect=fake_post), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter), \
             mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)):
            await provider.shuffle()
            await provider.repeat()

        self.assertIn(("/api/playback/shuffle", {"mode": "toggle"}), calls)
        # Current loop is "playlist" (repeat "all"), so the next cycle is "none" -> "off".
        self.assertIn(("/api/playback/repeat", {"mode": "off"}), calls)


def _queue_payload(**overrides):
    payload = {
        "current_index": 3,
        "current_track": {
            "id": 50, "title": "Current", "artist": "A", "album": "L",
            "artwork_url": "https://art/current.jpg", "duration_secs": 180,
        },
        "history": [],
        "history_len": 0,
        "repeat": "off",
        "shuffle": False,
        "stop_after_track_id": None,
        "total_tracks": 7,
        "upcoming": [
            {"id": 51, "title": "Next One", "artist": "N", "album": "M",
             "artwork_url": "https://art/next.jpg", "duration_secs": 200},
            {"id": 52, "title": "After", "artist": "B", "album": "C",
             "artwork_url": "", "duration_secs": 210},
        ],
    }
    payload.update(overrides)
    return payload


class QobuzQueueAndArtworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_state_passes_through(self):
        provider = QobuzProvider()
        getter = _fake_get({
            "/api/status": _status_payload(),
            "/api/now-playing": _now_playing_payload(),
            "/api/queue": _queue_payload(),
        })
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            status = await provider.status()

        self.assertEqual(status["queue_len"], 7)
        self.assertEqual(status["queue_index"], 3)
        self.assertEqual(status["next_track"]["title"], "Next One")
        self.assertEqual(status["next_track"]["artist"], "N")
        self.assertEqual(status["next_track"]["album"], "M")
        self.assertEqual(status["next_track"]["artUrl"], "https://art/next.jpg")
        self.assertEqual(status["next_track"]["id"], "51")

    async def test_artwork_recovered_from_queue_when_now_playing_track_missing(self):
        # The sporadic missing cover root cause: ``/api/now-playing`` returns no
        # dict track (transient/auth gap) and the old /api/status summary has no
        # artwork field. The queue current track carries the artwork and must be
        # used to pass existing metadata through.
        provider = QobuzProvider()
        getter = _fake_get({
            "/api/status": _status_payload(
                playback={"state": "playing", "title": "Cur", "artist": "Ar",
                          "track_id": 50, "duration": 180, "position": 5,
                          "volume": 0.5, "muted": False},
            ),
            "/api/now-playing": None,
            "/api/queue": _queue_payload(),
        })
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            status = await provider.status()

        self.assertEqual(status["artUrl"], "https://art/current.jpg")
        self.assertEqual(status["album"], "L")
        self.assertEqual(status["queue_len"], 7)

    async def test_artwork_stays_when_now_playing_has_it(self):
        provider = QobuzProvider()
        getter = _fake_get({
            "/api/status": _status_payload(),
            "/api/now-playing": _now_playing_payload(
                track={"id": 42, "title": "T", "artist": "A", "album": "L",
                       "duration_secs": 200, "artwork_url": "https://art/own.jpg",
                       "hires": True, "bit_depth": 24, "sample_rate": 96.0, "source": "qobuz"},
            ),
            "/api/queue": _queue_payload(),
        })
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            status = await provider.status()

        self.assertEqual(status["artUrl"], "https://art/own.jpg")
        self.assertEqual(status["queue_len"], 7)

    async def test_queue_fallback_never_borrows_artwork_across_tracks(self):
        # Regression (track race): now-playing reports track B (55) without
        # artwork while the queue snapshot's current track is still track A
        # (50) with artwork. B must never inherit A's artwork or album.
        provider = QobuzProvider()
        getter = _fake_get({
            "/api/status": _status_payload(),
            "/api/now-playing": _now_playing_payload(
                track={"id": 55, "title": "B", "artist": "B-Artist", "album": "B-Album",
                       "duration_secs": 200, "artwork_url": "", "hires": True,
                       "bit_depth": 16, "sample_rate": 44.1, "source": "qobuz"},
            ),
            "/api/queue": _queue_payload(),
        })
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            status = await provider.status()

        self.assertEqual(status["trackId"], "55")
        self.assertEqual(status["artUrl"], "")
        self.assertEqual(status["album"], "B-Album")

    async def test_queue_fallback_recovers_artwork_for_same_track_id(self):
        # The recovery is allowed when the queue current track is unambiguously
        # the same track as now-playing (by id): artwork fills the gap.
        provider = QobuzProvider()
        queue = _queue_payload(current_track={
            "id": 42, "title": "T", "artist": "A", "album": "L",
            "artwork_url": "https://art/queue.jpg", "duration_secs": 200,
        })
        getter = _fake_get({
            "/api/status": _status_payload(),
            "/api/now-playing": _now_playing_payload(
                track={"id": 42, "title": "T", "artist": "A", "album": "L",
                       "duration_secs": 200, "artwork_url": "", "hires": True,
                       "bit_depth": 24, "sample_rate": 96.0, "source": "qobuz"},
            ),
            "/api/queue": queue,
        })
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            status = await provider.status()

        self.assertEqual(status["trackId"], "42")
        self.assertEqual(status["artUrl"], "https://art/queue.jpg")
        self.assertEqual(status["album"], "L")

    async def test_queue_fallback_skipped_when_track_id_unknown(self):
        # No now-playing track and no summary track id: without an
        # unambiguous id match the queue must not be used as artwork source.
        provider = QobuzProvider()
        getter = _fake_get({
            "/api/status": _status_payload(
                playback={"state": "playing", "title": "Cur", "artist": "Ar",
                          "track_id": None, "duration": 180, "position": 5,
                          "volume": 0.5, "muted": False},
            ),
            "/api/now-playing": None,
            "/api/queue": _queue_payload(),
        })
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            status = await provider.status()

        self.assertEqual(status["trackId"], "")
        self.assertEqual(status["artUrl"], "")
        self.assertEqual(status["album"], "")

    async def test_queue_absent_leaves_neutral_fields(self):
        provider = QobuzProvider()
        getter = _fake_get({
            "/api/status": _status_payload(),
            "/api/now-playing": _now_playing_payload(),
            "/api/queue": None,
        })
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            status = await provider.status()

        self.assertEqual(status["queue_len"], 0)
        self.assertEqual(status["queue_index"], 0)
        self.assertIsNone(status["next_track"])


def _reachable(value):
    async def fake_is_reachable(base_url, timeout=1.0):
        return value
    return fake_is_reachable


def _fake_get(routes):
    async def fake_get(base_url, path, timeout=2.0):
        return routes.get(path)
    return fake_get


if __name__ == "__main__":
    unittest.main()
