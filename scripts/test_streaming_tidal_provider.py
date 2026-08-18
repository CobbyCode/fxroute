# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for the TIDAL provider.

tidalapi is not installed in the local test environment, so the tests patch the
lazy ``streaming.tidal.auth.tidalapi`` handle and the shared session manager
with lightweight fakes.  They cover stream/audio-info normalization, catalog
normalization, error mapping, provider state and the registry.
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import streaming
from streaming.tidal import auth, playback
from streaming.tidal.provider import TidalProvider


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeStream:
    def __init__(self, sample_rate=44100, bit_depth=16, codecs="flac", manifest="<?xml version='1.0'?><MPD/>"):
        self.sample_rate = sample_rate
        self.bit_depth = bit_depth
        self.codecs = codecs
        self._manifest = manifest

    def get_stream_manifest(self):
        return SimpleNamespace(
            sample_rate=self.sample_rate,
            codecs=self.codecs,
        )

    def get_manifest_data(self):
        return self._manifest


class FakeTrack:
    def __init__(self, track_id=1, url=None, stream=None, url_error=None, stream_error=None):
        self.id = track_id
        self.name = "Track"
        self.title = "Track"
        self.duration = 200
        self.artist = SimpleNamespace(name="Artist")
        self.artists = [self.artist]
        self.album = SimpleNamespace(name="Album", image=lambda size=640: f"https://c/{track_id}.jpg")
        self.audio_quality = "LOSSLESS"
        self.is_hi_res_lossless = False
        self.is_lossless = True
        self.available = True
        self.explicit = False
        self.session = SimpleNamespace()
        self._url = url
        self._stream = stream or FakeStream()
        self._url_error = url_error
        self._stream_error = stream_error

    def get_url(self):
        if self._url_error:
            raise self._url_error
        return self._url

    def get_stream(self):
        if self._stream_error:
            raise self._stream_error
        return self._stream


class _FakeSearchResults(dict):
    pass


class FakeSession:
    def __init__(self, logged_in=True):
        self._logged_in = logged_in
        self.config = SimpleNamespace(quality="LOSSLESS")
        self.token_type = "Bearer"
        self.access_token = "access"
        self.refresh_token = "refresh"
        self.expiry_time = None
        self.is_pkce = False
        self.country_code = "US"
        self.user = SimpleNamespace(id=42, email="u@example.com", favorites=SimpleNamespace(tracks=lambda limit=50: []))

    def check_login(self):
        return self._logged_in

    def track(self, track_id):
        return FakeTrack(track_id=int(track_id))

    def search(self, query, models=None, limit=50):
        return _FakeSearchResults()


def _fake_tidalapi_module():
    return SimpleNamespace(
        Session=FakeSession,
        Track=object,
        Album=object,
        Artist=object,
        Playlist=object,
    )


# ---------------------------------------------------------------------------
# playback normalization
# ---------------------------------------------------------------------------

class StreamInfoNormalizationTests(unittest.TestCase):
    def test_sample_rate_hz_passthrough(self):
        self.assertEqual(playback._normalize_sample_rate_hz(44100), 44100)
        self.assertEqual(playback._normalize_sample_rate_hz(88200), 88200)

    def test_sample_rate_khz_scaled(self):
        self.assertEqual(playback._normalize_sample_rate_hz(44.1), 44100)
        self.assertEqual(playback._normalize_sample_rate_hz(88.2), 88200)

    def test_sample_rate_invalid(self):
        self.assertIsNone(playback._normalize_sample_rate_hz(None))
        self.assertIsNone(playback._normalize_sample_rate_hz(0))
        self.assertIsNone(playback._normalize_sample_rate_hz("x"))

    def test_bit_depth_normalization(self):
        self.assertEqual(playback._normalize_bit_depth(24), 24)
        self.assertEqual(playback._normalize_bit_depth("16"), 16)
        self.assertIsNone(playback._normalize_bit_depth(None))

    def test_format_from_codecs(self):
        self.assertEqual(playback._format_from_codecs("flac"), "flac")
        self.assertEqual(playback._format_from_codecs("mp4a.40.2"), "aac")
        self.assertEqual(playback._format_from_codecs(""), "aac")


_SAMPLE_MPD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">'
    '<Period><AdaptationSet contentType="audio"><Representation codecs="flac">'
    '<SegmentTemplate timescale="44100" initialization="https://cdn/init.mp4" '
    'media="https://cdn/$Number$.mp4">'
    '<SegmentTimeline><S d="176128" r="2"/><S d="37504"/></SegmentTimeline>'
    '</SegmentTemplate></Representation></AdaptationSet></Period></MPD>'
)


def _fake_http_download(url, dest, **kwargs):
    """Emulate a CDN fetch: write a per-URL marker body."""
    name = str(url).rsplit("/", 1)[-1]
    dest.write_bytes(b"BODY:%s:" % name.encode())


class StreamResolutionTests(unittest.TestCase):
    def test_dash_template_parses_init_media_and_count(self):
        init_url, media_url, count = playback._dash_template(_SAMPLE_MPD)
        self.assertEqual(init_url, "https://cdn/init.mp4")
        self.assertEqual(media_url, "https://cdn/$Number$.mp4")
        self.assertEqual(count, 4)  # 3 repeated S + 1 final S

    def test_dash_template_rejects_unsupported_manifest(self):
        with self.assertRaises(playback.TidalStreamError) as ctx:
            playback._dash_template("<?xml?><MPD/>")
        self.assertEqual(ctx.exception.kind, playback.KIND_UNSUPPORTED)

    def test_download_dash_concatenates_init_and_segments_in_order(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(playback, "_http_download", side_effect=_fake_http_download):
            out = playback._download_dash(_SAMPLE_MPD, directory=Path(tmp))
            self.assertTrue(out.endswith(".mp4"))
            data = Path(out).read_bytes()
        self.assertEqual(
            data,
            b"BODY:init.mp4:" + b"".join(b"BODY:%d.mp4:" % n for n in range(1, 5)),
        )

    def test_download_dash_cache_hit_reuses_fresh_file(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(playback, "_http_download", side_effect=_fake_http_download) as fetch:
            first = playback._download_dash(_SAMPLE_MPD, directory=Path(tmp), cache_key="track-1-44100-16")
            second = playback._download_dash(_SAMPLE_MPD, directory=Path(tmp), cache_key="track-1-44100-16")
            self.assertEqual(first, second)
            # Only the first call materializes (init + 4 segments).
            self.assertEqual(fetch.call_count, 5)

    def test_download_dash_segment_failure_is_network_error(self):
        def _fail(url, dest, **kwargs):
            raise playback.urllib.error.URLError("boom")

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(playback, "_http_download", side_effect=_fail):
            with self.assertRaises(playback.TidalStreamError) as ctx:
                playback._download_dash(_SAMPLE_MPD, directory=Path(tmp))
        self.assertEqual(ctx.exception.kind, playback.KIND_NETWORK)
        self.assertFalse(any(Path(tmp).glob(".*tidal-*-parts")) or any(Path(tmp).glob("tidal-*.mp4")))

    def test_direct_url_preferred(self):
        track = FakeTrack(track_id=7, url="https://cdn/7.flac")
        with tempfile.TemporaryDirectory() as tmp:
            result = playback.resolve_stream_for_track(track, directory=Path(tmp))
        self.assertEqual(result["url"], "https://cdn/7.flac")
        self.assertEqual(result["sample_rate"], 44100)
        self.assertEqual(result["bit_depth"], 16)
        self.assertEqual(result["audio_format"], "flac")
        self.assertFalse(result["dash"])

    def test_dash_fallback_materializes_local_file_when_direct_url_unavailable(self):
        track = FakeTrack(
            track_id=8,
            url=None,
            url_error=RuntimeError("no direct url"),
            stream=FakeStream(sample_rate=88200, bit_depth=24, codecs="flac"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(playback, "_download_dash", return_value=str(Path(tmp) / "out.mp4")):
                result = playback.resolve_stream_for_track(track, directory=Path(tmp))
            self.assertTrue(result["dash"])
            self.assertEqual(result["url"], str(Path(tmp) / "out.mp4"))
        self.assertEqual(result["sample_rate"], 88200)
        self.assertEqual(result["bit_depth"], 24)
        self.assertEqual(result["audio_format"], "flac")

    def test_hi_res_manifest_rate_is_authoritative(self):
        # Stream carries defaults (44100/16) but the manifest reports 96 kHz.
        manifest = SimpleNamespace(
            sample_rate=96000,
            codecs="flac",
        )
        stream = FakeStream(sample_rate=44100, bit_depth=16, codecs="flac")
        stream.get_stream_manifest = lambda: manifest
        track = FakeTrack(track_id=9, url=None, url_error=RuntimeError("no"), stream=stream)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(playback, "_download_dash", return_value=str(Path(tmp) / "out.mp4")):
                result = playback.resolve_stream_for_track(track, directory=Path(tmp))
        self.assertEqual(result["sample_rate"], 96000)

    def test_rights_error_is_normalized(self):
        try:
            import tidalapi.exceptions as tex
        except ImportError:
            self.skipTest("tidalapi not installed")
        track = FakeTrack(track_id=10, stream_error=tex.StreamNotAvailable("no stream"))
        with self.assertRaises(playback.TidalStreamError) as ctx:
            playback.resolve_stream_for_track(track)
        self.assertEqual(ctx.exception.kind, playback.KIND_RIGHTS)

    def test_map_exception_network_fallback(self):
        err = playback._map_exception(RuntimeError("boom"))
        self.assertEqual(err.kind, playback.KIND_NETWORK)


# ---------------------------------------------------------------------------
# catalog normalization
# ---------------------------------------------------------------------------

class CatalogNormalizationTests(unittest.TestCase):
    def test_normalize_track(self):
        from streaming.tidal import catalog

        track = FakeTrack(track_id=123)
        data = catalog.normalize_track(track)
        self.assertEqual(data["id"], "123")
        self.assertEqual(data["title"], "Track")
        self.assertEqual(data["artist"], "Artist")
        self.assertEqual(data["album"], "Album")
        self.assertEqual(data["art_url"], "https://c/123.jpg")
        self.assertEqual(data["duration"], 200.0)
        self.assertEqual(data["audio_quality"], "LOSSLESS")
        self.assertFalse(data["is_hi_res_lossless"])
        self.assertTrue(data["is_lossless"])

    def test_normalize_playlist(self):
        from streaming.tidal import catalog

        playlist = SimpleNamespace(
            id=5,
            name="My Mix",
            num_tracks=12,
            description="desc",
            square_picture=lambda size=640: "https://c/p.jpg",
            image=None,
            picture=None,
        )
        data = catalog.normalize_playlist(playlist)
        self.assertEqual(data["id"], "5")
        self.assertEqual(data["name"], "My Mix")
        self.assertEqual(data["track_count"], 12)
        self.assertEqual(data["art_url"], "https://c/p.jpg")

    def test_search_normalizes_tracks(self):
        from streaming.tidal import catalog

        fake_mod = _fake_tidalapi_module()
        session = FakeSession()

        class _Results(dict):
            pass

        def fake_search(query, models=None, limit=50):
            return _Results({"tracks": [FakeTrack(track_id=1), FakeTrack(track_id=2)]})

        session.search = fake_search
        with mock.patch.object(auth, "tidalapi", fake_mod), \
             mock.patch.object(auth.manager, "session", return_value=session), \
             mock.patch.dict(sys.modules, {"tidalapi": fake_mod}):
            result = catalog.search("query", types=["tracks"])
        self.assertEqual(len(result["tracks"]), 2)
        self.assertEqual(result["tracks"][0]["id"], "1")


# ---------------------------------------------------------------------------
# provider state / capabilities
# ---------------------------------------------------------------------------

class ProviderStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_capabilities_surface(self):
        caps = TidalProvider().capabilities()
        for name in ("transport", "seek", "shuffle", "loop", "progress", "volume",
                     "search", "library", "favorites", "playlists", "cover",
                     "audio_format", "sample_rate", "bit_depth"):
            self.assertTrue(getattr(caps, name), name)
        for name in ("radio", "lyrics", "recommendations", "queue_editing"):
            self.assertFalse(getattr(caps, name), name)

    async def test_status_unavailable_without_dependency(self):
        provider = TidalProvider()
        with mock.patch.object(auth, "tidalapi_available", return_value=False):
            status = await provider.status()
        self.assertFalse(status["available"])
        self.assertFalse(status["authenticated"])
        self.assertIsNone(status["backend"])

    async def test_status_authenticated(self):
        provider = TidalProvider()
        session = FakeSession(logged_in=True)
        with mock.patch.object(auth, "tidalapi_available", return_value=True), \
             mock.patch.object(auth.manager, "is_authenticated", new=_async_true()), \
             mock.patch.object(auth.manager, "get_session", new=_async_return(session)):
            status = await provider.status()
        self.assertTrue(status["available"])
        self.assertTrue(status["authenticated"])
        self.assertEqual(status["backend"], "tidalapi")
        self.assertEqual(status["user"]["email"], "u@example.com")

    async def test_status_reflects_tidal_playback(self):
        provider = TidalProvider()
        provider.configure(lambda: {
            "playing": True,
            "paused": False,
            "position": 12.0,
            "volume": 80,
            "queue": {"shuffle": False, "loop": False},
            "current_track": {
                "source": "tidal",
                "id": "9",
                "title": "T",
                "artist": "A",
                "album": "L",
                "art_url": "https://c/9.jpg",
                "duration": 200,
                "sample_rate_hz": 88200,
                "bit_depth": 24,
                "audio_format": "flac",
            },
        })
        with mock.patch.object(auth, "tidalapi_available", return_value=True), \
             mock.patch.object(auth.manager, "is_authenticated", new=_async_false()):
            status = await provider.status()
        self.assertEqual(status["status"], "Playing")
        self.assertEqual(status["title"], "T")
        self.assertEqual(status["trackId"], "9")
        self.assertEqual(status["sample_rate"], 88200)
        self.assertEqual(status["bit_depth"], 24)
        self.assertEqual(status["audio_format"], "flac")


class SessionPersistenceTests(unittest.TestCase):
    """The OAuth session persists to a credential file (0600) and reloads."""

    def test_save_session_writes_restricted_permissions(self):
        from datetime import datetime, timedelta

        session = SimpleNamespace(
            token_type="Bearer",
            access_token="access-token",
            refresh_token="refresh-token",
            expiry_time=datetime.now() + timedelta(hours=1),
            is_pkce=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(auth, "SESSION_FILE", Path(tmp) / "tidal-session.json"):
                auth.manager._save_session(session)
            path = Path(tmp) / "tidal-session.json"
            self.assertTrue(path.exists())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            data = json.loads(path.read_text())
            self.assertEqual(data["access_token"], "access-token")
            self.assertTrue(data["is_pkce"])

    def test_load_session_roundtrip(self):
        from datetime import datetime, timedelta

        class _Loaded:
            def __init__(self):
                self.config = SimpleNamespace(quality="HI_RES_LOSSLESS")
                self.token_type = None
                self.access_token = None
                self.refresh_token = None
                self.expiry_time = None
                self.is_pkce = False

            def load_oauth_session(self, token_type, access_token, refresh_token=None, expiry_time=None, is_pkce=False):
                self.token_type = token_type
                self.access_token = access_token
                self.refresh_token = refresh_token
                self.expiry_time = expiry_time
                self.is_pkce = is_pkce
                return True

            def check_login(self):
                return bool(self.access_token)

        loaded = _Loaded()
        fake_mod = SimpleNamespace(Session=lambda: loaded)
        stored = {
            "token_type": "Bearer",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expiry_time": (datetime.now() + timedelta(hours=1)).isoformat(),
            "is_pkce": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tidal-session.json"
            path.write_text(json.dumps(stored))
            with mock.patch.object(auth, "SESSION_FILE", path), \
                 mock.patch.object(auth, "tidalapi", fake_mod):
                result = auth.manager._load_session()
        self.assertIsNotNone(result)
        self.assertEqual(result.access_token, "access-token")
        self.assertTrue(result.is_pkce)


class RegistryTests(unittest.TestCase):
    def test_registry_includes_tidal_as_implemented(self):
        tidal = streaming.get_provider("tidal")
        self.assertIsInstance(tidal, TidalProvider)
        self.assertTrue(tidal.implemented)
        self.assertEqual(tidal.provider_id, "tidal")
        self.assertEqual(tidal.display_name, "TIDAL")


class MainResolverTests(unittest.IsolatedAsyncioTestCase):
    """The main.py queue/play resolver delegates to the TIDAL provider."""

    async def test_resolve_tidal_stream_url_updates_track(self):
        import main

        class _FakeProvider:
            async def resolve_stream(self, track_id):
                return {
                    "url": "https://cdn/9.flac",
                    "sample_rate": 88200,
                    "bit_depth": 24,
                    "audio_format": "flac",
                }

        track = {"id": "9", "source": "tidal", "url": ""}
        with mock.patch.object(main.streaming, "get_provider", return_value=_FakeProvider()):
            url = await main._resolve_tidal_stream_url(track)
        self.assertEqual(url, "https://cdn/9.flac")
        self.assertEqual(track["url"], "https://cdn/9.flac")
        self.assertEqual(track["sample_rate_hz"], 88200)
        self.assertEqual(track["bit_depth"], 24)
        self.assertEqual(track["audio_format"], "flac")

    async def test_resolve_tidal_track_builds_track_dict(self):
        import main

        class _FakeProvider:
            async def get_track(self, track_id):
                return {
                    "id": "9", "title": "T", "artist": "A", "album": "L",
                    "art_url": "https://c/9.jpg", "duration": 200,
                }

            async def resolve_stream(self, track_id):
                return {
                    "url": "https://cdn/9.flac", "sample_rate": 44100,
                    "bit_depth": 16, "audio_format": "flac",
                }

        with mock.patch.object(main.streaming, "get_provider", return_value=_FakeProvider()):
            track = await main._resolve_tidal_track("9")
        self.assertEqual(track["source"], "tidal")
        self.assertEqual(track["id"], "9")
        self.assertEqual(track["title"], "T")
        self.assertEqual(track["art_url"], "https://c/9.jpg")
        self.assertEqual(track["sample_rate_hz"], 44100)
        self.assertEqual(track["url"], "https://cdn/9.flac")


def _async_return(value):
    async def fn(*a, **k):
        return value
    return fn


def _async_true():
    return _async_return(True)


def _async_false():
    return _async_return(False)


if __name__ == "__main__":
    unittest.main()
