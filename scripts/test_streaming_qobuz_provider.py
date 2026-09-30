# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for the Qobuz provider (fork qbzd control plane).

No qbzd daemon or network is required: the ``streaming.qobuz.backend`` HTTP
helpers are patched with canned JSON in the fork daemon shapes
(``/api/status`` with ``logged_in``/``qconnect``, ``/api/playback`` transport
facts, ``/api/queue`` with ``QueueTrack`` objects).
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.qobuz import connect_state
from streaming.qobuz.backend import default_base_url, qbzd_installed
from streaming.qobuz.provider import QobuzProvider


def _status_payload(**overrides):
    payload = {
        "audio": {"cache_mb": 400},
        "logged_in": True,
        "qconnect": True,
        "state": "playing",
        "track_id": 42,
    }
    payload.update(overrides)
    return payload


def _playback_payload(**overrides):
    payload = {
        "state": "Playing",
        "track_id": 42,
        "position_secs": 30,
        "duration_secs": 200,
        "volume": 0.8,
        "sample_rate": 96000,
        "bit_depth": 24,
    }
    payload.update(overrides)
    return payload


def _track_payload(**overrides):
    payload = {
        "id": 42, "title": "T", "artist": "A", "album": "L",
        "duration_secs": 200, "artwork_url": "https://art/q.jpg",
        "hires": True, "bit_depth": 24, "sample_rate": 96.0, "source": "qobuz",
    }
    payload.update(overrides)
    return payload


def _queue_payload(**overrides):
    payload = {
        "current_index": 3,
        "current_track": _track_payload(
            id=50, title="Current", artist="A", album="L",
            artwork_url="https://art/current.jpg", duration_secs=180,
        ),
        "history": [],
        "repeat": "Off",
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


def _full_routes(**overrides):
    routes = {
        "/api/status": _status_payload(),
        "/api/playback": _playback_payload(),
        "/api/queue": _queue_payload(current_track=_track_payload(), repeat="All", shuffle=True),
    }
    routes.update(overrides)
    return routes


class QobuzBackendTests(unittest.TestCase):
    def test_default_base_url(self):
        self.assertEqual(default_base_url(), "http://127.0.0.1:8182")

    def test_qbzd_installed_detection(self):
        with mock.patch("streaming.qobuz.backend.shutil.which", return_value="/usr/bin/qbzd"):
            self.assertTrue(qbzd_installed())
        with mock.patch("streaming.qobuz.backend.shutil.which", return_value=None):
            self.assertFalse(qbzd_installed())

    def test_qbzd_installed_user_bin_fallback(self):
        # systemd user services run with a system PATH that omits ~/.local/bin;
        # a pip/pipx user install must still be detected.
        def fake_which(name, path=None):
            return None if path is None else "/home/tester/.local/bin/qbzd"

        with mock.patch("streaming.qobuz.backend.shutil.which", side_effect=fake_which):
            self.assertTrue(qbzd_installed())


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
        getter = _fake_get(_full_routes())
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
        queue = _queue_payload(current_track={
            "id": 7, "title": "N", "artist": "X", "album": "Y",
            "duration_secs": 200, "artwork_url": "", "hires": False,
            "bit_depth": 16, "sample_rate": 44.1, "source": "qobuz",
        })
        getter = _fake_get(_full_routes(**{
            "/api/queue": queue,
            "/api/playback": _playback_payload(track_id=7, sample_rate=0, bit_depth=0),
        }))
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            status = await provider.status()

        self.assertEqual(status["sample_rate"], 44100)  # 44.1 kHz -> Hz
        self.assertEqual(status["bit_depth"], 16)
        self.assertEqual(status["audio_format"], "flac")

    async def test_unauthenticated_state_reports_stopped_without_metadata(self):
        provider = QobuzProvider()
        getter = _fake_get({
            "/api/status": _status_payload(logged_in=False, qconnect=False, state="no_session",
                                           track_id=0),
            "/api/playback": _playback_payload(state="Stopped", track_id=0, position_secs=0,
                                               duration_secs=0, volume=1.0, sample_rate=0,
                                               bit_depth=0),
            "/api/queue": _queue_payload(current_track=None, current_index=None, total_tracks=0,
                                         upcoming=[]),
        })
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            status = await provider.status()

        self.assertFalse(status["authenticated"])
        self.assertFalse(status["connected"])
        self.assertEqual(status["status"], "Stopped")
        self.assertEqual(status["trackId"], "")
        self.assertIsNone(status["sample_rate"])
        self.assertIsNone(status["bit_depth"])

    async def test_unavailable_qbzd_reports_stopped_without_network(self):
        provider = QobuzProvider()
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=False), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=AssertionError("must not be called")):
            status = await provider.status()
        self.assertFalse(status["available"])
        self.assertEqual(status["status"], "Stopped")
        self.assertEqual(status["source"], "qobuz")


class QobuzStandbyFlagTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        connect_state.reset()

    def tearDown(self):
        connect_state.reset()

    async def _status(self, routes):
        provider = QobuzProvider()
        getter = _fake_get(routes)
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            return await provider.status()

    async def test_deselected_device_keeps_track_paused_and_flags_standby(self):
        # After the device is deselected in the app, the daemon keeps the
        # last track paused with its metadata while the session persists.
        # Journal-tracked selection says inactive: standby.
        connect_state.set_device_active(False)
        routes = _full_routes(**{
            "/api/status": _status_payload(state="paused"),
            "/api/playback": _playback_payload(state="Paused", position_secs=105,
                                               duration_secs=194, volume=1.0),
            "/api/queue": _queue_payload(current_track=_track_payload(
                id=42, title="Diamonds", artist="A", album="L", duration_secs=194,
                artwork_url="https://art/d.jpg", hires=True, bit_depth=16,
                sample_rate=44.1)),
        })
        status = await self._status(routes)
        self.assertEqual(status["status"], "Paused")
        self.assertTrue(status["connected"])
        self.assertTrue(status["qbzd_standby"])

    async def test_selected_device_paused_shows_card_not_standby(self):
        # Pause while FXRoute is the selected device is a paused card,
        # not standby: standby tracks selection, not play/pause.
        connect_state.set_device_active(True)
        routes = _full_routes(**{
            "/api/playback": _playback_payload(state="Paused", position_secs=105,
                                               duration_secs=194, volume=1.0),
        })
        status = await self._status(routes)
        self.assertEqual(status["status"], "Paused")
        self.assertFalse(status["qbzd_standby"])

    async def test_stopped_with_inactive_device_flags_standby(self):
        connect_state.set_device_active(False)
        routes = _full_routes(**{
            "/api/status": _status_payload(logged_in=True, qconnect=False, state="idle",
                                           track_id=0),
            "/api/playback": _playback_payload(state="Stopped", track_id=0, position_secs=0,
                                               duration_secs=0, volume=1.0, sample_rate=0,
                                               bit_depth=0),
            "/api/queue": _queue_payload(current_track=None, current_index=None,
                                         total_tracks=0, upcoming=[]),
        })
        status = await self._status(routes)
        self.assertEqual(status["status"], "Stopped")
        self.assertTrue(status["qbzd_standby"])

    async def test_playing_never_flags_standby(self):
        connect_state.set_device_active(True)
        status = await self._status(_full_routes())
        self.assertEqual(status["status"], "Playing")
        self.assertFalse(status["qbzd_standby"])

    async def test_unknown_selection_state_is_conservative(self):
        # Fresh start without journal evidence: show the card instead of a
        # possibly wrong "ready".
        self.assertIsNone(connect_state.is_device_active())
        routes = _full_routes(**{
            "/api/playback": _playback_payload(state="Paused", position_secs=5,
                                               duration_secs=100, volume=1.0),
        })
        status = await self._status(routes)
        self.assertFalse(status["qbzd_standby"])

    async def test_unavailable_daemon_has_no_standby_flag(self):
        provider = QobuzProvider()
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=False), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=AssertionError("must not be called")):
            status = await provider.status()
        self.assertNotIn("qbzd_standby", status)


class QobuzStreamFactsStabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_facts_survive_transient_queue_gap(self):
        # A transient queue gap (pause/transition) must not degrade a
        # complete track's quality data: the footer tag would collapse from
        # 'FLAC · 24bit · 96kHz' to the bare rate.
        provider = QobuzProvider()
        first = _fake_get(_full_routes())
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=first):
            status = await provider.status()
        self.assertEqual(status["trackId"], "42")
        self.assertEqual(status["sample_rate"], 96000)
        self.assertEqual(status["bit_depth"], 24)
        self.assertEqual(status["audio_format"], "flac")

        # Same track, but the queue drops the track object while playback
        # still reports the negotiated rate/depth: the known facts stay.
        gap = _fake_get({
            "/api/status": _status_payload(),
            "/api/playback": _playback_payload(),
            "/api/queue": _queue_payload(current_track=None),
        })
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=gap):
            status = await provider.status()
        self.assertEqual(status["trackId"], "42")
        self.assertEqual(status["sample_rate"], 96000)
        self.assertEqual(status["bit_depth"], 24)
        self.assertEqual(status["audio_format"], "flac")

    async def test_consecutive_full_and_reduced_readings_stay_complete(self):
        # The canonical footer state must stay fully complete across a sequence
        # of full and reduced readings of the *same* track. A partial reading
        # (queue track gone, playback still reporting the rate) must never
        # erase a field the previous complete reading already delivered.
        provider = QobuzProvider()

        async def read(queue):
            getter = _fake_get({
                "/api/status": _status_payload(),
                "/api/playback": _playback_payload(),
                "/api/queue": queue,
            })
            with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
                 mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
                 mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
                return await provider.status()

        full_queue = _queue_payload(current_track=_track_payload())
        reduced_queue = _queue_payload(current_track=None)

        sequence = [full_queue, reduced_queue, full_queue, reduced_queue, reduced_queue]
        for index, queue in enumerate(sequence):
            status = await read(queue)
            self.assertEqual(status["trackId"], "42", f"step {index}")
            self.assertEqual(status["sample_rate"], 96000, f"step {index}")
            self.assertEqual(status["bit_depth"], 24, f"step {index}")
            self.assertEqual(status["audio_format"], "flac", f"step {index}")

    async def test_unattributed_reading_never_clobbers_remembered_facts(self):
        # A reading with no track id at all (queue track gone and playback
        # track_id reset) must not erase the remembered facts of the track
        # that just played: the next attributable partial reading of the same
        # track must still restore the full facts.
        provider = QobuzProvider()
        first = _fake_get(_full_routes())
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=first):
            status = await provider.status()
        self.assertEqual(status["trackId"], "42")
        self.assertEqual(status["audio_format"], "flac")

        # Unattributed gap: queue track gone and playback reports no track.
        anonymous = _fake_get({
            "/api/status": _status_payload(track_id=0),
            "/api/playback": _playback_payload(track_id=0, position_secs=0, duration_secs=0,
                                               volume=0.8, sample_rate=0, bit_depth=0),
            "/api/queue": _queue_payload(current_track=None),
        })
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=anonymous):
            status = await provider.status()
        self.assertEqual(status["trackId"], "")
        self.assertIsNone(status["sample_rate"])
        self.assertIsNone(status["bit_depth"])
        self.assertIsNone(status["audio_format"])

        # Same track again, but only the negotiated rate is available: the
        # remembered facts must survive the anonymous reading in between.
        partial = _fake_get({
            "/api/status": _status_payload(),
            "/api/playback": _playback_payload(sample_rate=96000, bit_depth=0),
            "/api/queue": _queue_payload(current_track=None),
        })
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=partial):
            status = await provider.status()
        self.assertEqual(status["trackId"], "42")
        self.assertEqual(status["sample_rate"], 96000)
        self.assertEqual(status["bit_depth"], 24)
        self.assertEqual(status["audio_format"], "flac")

    async def test_stream_facts_never_borrowed_across_tracks(self):
        provider = QobuzProvider()
        first = _fake_get(_full_routes())
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=first):
            await provider.status()

        # A gap that reports a *different* track id must not borrow the
        # previous track's facts.
        gap = _fake_get({
            "/api/status": _status_payload(track_id=55),
            "/api/playback": _playback_payload(track_id=55, position_secs=30, duration_secs=200,
                                               volume=0.8, sample_rate=0, bit_depth=0),
            "/api/queue": _queue_payload(current_track=None),
        })
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=gap):
            status = await provider.status()
        self.assertEqual(status["trackId"], "55")
        self.assertIsNone(status["sample_rate"])
        self.assertIsNone(status["bit_depth"])
        self.assertIsNone(status["audio_format"])


class QobuzTransportDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_posts_correct_routes_and_bodies(self):
        provider = QobuzProvider()
        calls = []
        routes = _full_routes()

        async def fake_post(base_url, path, body=None, timeout=2.0):
            calls.append((path, body or {}))
            if path in {"/api/playback/next", "/api/playback/previous"}:
                return {"track": _track_payload()}
            if path == "/api/playback/play-track":
                return {"playing": True, "track_id": body["track_id"]}
            if path == "/api/playback/seek":
                routes["/api/playback"]["position_secs"] = body["position_secs"]
            return {}

        getter = _fake_get(routes)
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
        # Playing fixture: toggle pauses (the fork has no toggle endpoint).
        self.assertIn(("/api/playback/pause", {}), calls)
        self.assertIn(("/api/playback/next", {}), calls)
        self.assertIn(("/api/playback/previous", {}), calls)
        self.assertIn(("/api/playback/seek", {"position_secs": 90}), calls)
        self.assertIn(("/api/playback/volume", {"volume": 0.5}), calls)

    async def test_toggle_plays_when_stopped(self):
        provider = QobuzProvider()
        calls = []

        async def fake_post(base_url, path, body=None, timeout=2.0):
            calls.append((path, body or {}))
            return {}

        getter = _fake_get(_full_routes(**{
            "/api/playback": _playback_payload(state="Stopped", track_id=0),
        }))
        with mock.patch("streaming.qobuz.backend.post_json", side_effect=fake_post), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter), \
             mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)):
            await provider.toggle()

        self.assertIn(("/api/playback/play", {}), calls)

    async def test_repeat_cycles_and_shuffle_toggles(self):
        provider = QobuzProvider()
        calls = []

        async def fake_post(base_url, path, body=None, timeout=2.0):
            calls.append((path, body or {}))
            return {}

        getter = _fake_get(_full_routes())
        with mock.patch("streaming.qobuz.backend.post_json", side_effect=fake_post), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter), \
             mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)):
            await provider.shuffle()
            await provider.repeat()

        # Fixture shuffle is on: shuffle switches it off on the queue.
        self.assertIn(("/api/queue/shuffle", {"enabled": False}), calls)
        # Current loop is "playlist" (repeat "All"), so the next cycle is "none" -> "off".
        self.assertIn(("/api/queue/repeat", {"mode": "off"}), calls)


class QobuzQueueAndArtworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_state_passes_through(self):
        provider = QobuzProvider()
        getter = _fake_get(_full_routes())
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

    async def test_artwork_missing_without_queue_track(self):
        # The queue current track is the only artwork source: without it
        # (transient/auth gap) the card carries the playback id but no
        # artwork or album.
        provider = QobuzProvider()
        getter = _fake_get({
            "/api/status": _status_payload(),
            "/api/playback": _playback_payload(),
            "/api/queue": _queue_payload(current_track=None),
        })
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            status = await provider.status()

        self.assertEqual(status["trackId"], "42")
        self.assertEqual(status["artUrl"], "")
        self.assertEqual(status["album"], "")
        self.assertEqual(status["queue_len"], 7)

    async def test_queue_current_track_carries_full_metadata(self):
        provider = QobuzProvider()
        getter = _fake_get(_full_routes())
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            status = await provider.status()

        self.assertEqual(status["artUrl"], "https://art/q.jpg")
        self.assertEqual(status["queue_len"], 7)

    async def test_queue_absent_leaves_neutral_fields(self):
        provider = QobuzProvider()
        getter = _fake_get({
            "/api/status": _status_payload(),
            "/api/playback": _playback_payload(),
            "/api/queue": None,
        })
        with mock.patch("streaming.qobuz.backend.qbzd_installed", return_value=True), \
             mock.patch("streaming.qobuz.backend.is_reachable", new=_reachable(True)), \
             mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            status = await provider.status()

        self.assertEqual(status["queue_len"], 0)
        self.assertEqual(status["queue_index"], 0)
        self.assertIsNone(status["next_track"])


class QobuzPlayTrackTests(unittest.IsolatedAsyncioTestCase):
    async def test_play_track_posts_id_with_download_timeout(self):
        provider = QobuzProvider()
        calls = []

        async def fake_post(base_url, path, body=None, timeout=2.0):
            calls.append((path, body or {}, timeout))
            return {"playing": True, "track_id": 107361968}

        with mock.patch("streaming.qobuz.backend.post_json", side_effect=fake_post):
            result = await provider.play_track(107361968)
        self.assertEqual(len(calls), 1)
        path, body, timeout = calls[0]
        self.assertEqual(path, "/api/playback/play-track")
        self.assertEqual(body, {"track_id": 107361968})
        self.assertGreaterEqual(timeout, 60.0)
        self.assertIsInstance(result, dict)

    async def test_play_track_accepts_numeric_string_id(self):
        provider = QobuzProvider()
        calls = []

        async def fake_post(base_url, path, body=None, timeout=2.0):
            calls.append((path, body or {}, timeout))
            return {"playing": True, "track_id": 42}

        with mock.patch("streaming.qobuz.backend.post_json", side_effect=fake_post):
            await provider.play_track("42")
        self.assertIn(("/api/playback/play-track", {"track_id": 42}), [(p, b) for p, b, _t in calls])

    async def test_play_track_rejects_unparseable_id(self):
        provider = QobuzProvider()
        with mock.patch("streaming.qobuz.backend.post_json") as post:
            with self.assertRaises(ValueError):
                await provider.play_track("not-a-track")
        post.assert_not_awaited()

    async def test_play_track_raises_when_daemon_refuses(self):
        provider = QobuzProvider()

        async def fake_post(base_url, path, body=None, timeout=2.0):
            return None

        with mock.patch("streaming.qobuz.backend.post_json", side_effect=fake_post):
            with self.assertRaises(RuntimeError):
                await provider.play_track(42)

    async def test_loaded_track_id_reads_raw_playback(self):
        provider = QobuzProvider()
        getter = _fake_get(_full_routes(**{
            "/api/playback": _playback_payload(state="Stopped", track_id=0),
        }))
        with mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            self.assertEqual(await provider.loaded_track_id(), 0)
        getter = _fake_get(_full_routes(**{
            "/api/playback": _playback_payload(state="Paused", track_id=42),
        }))
        with mock.patch("streaming.qobuz.backend.get_json", side_effect=getter):
            self.assertEqual(await provider.loaded_track_id(), 42)

    async def test_loaded_track_id_is_zero_when_unreachable(self):
        provider = QobuzProvider()
        with mock.patch("streaming.qobuz.backend.get_json", return_value=None):
            self.assertEqual(await provider.loaded_track_id(), 0)


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
