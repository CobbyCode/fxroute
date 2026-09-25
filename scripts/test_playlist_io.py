#!/usr/bin/env python3
"""Behavior tests for M3U/M3U8 playlist I/O (playlist_io).

Covers REFACTOR-002: parsing (BOM/comments), path resolution
(relative/absolute, Windows/POSIX, file://), unknown/duplicate entries,
ordering, export content, download filename, import without match, and the
existing API responses for playlist import and export.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
import library.api as library_api
import library.playlist_io as playlist_io
import library.playlists as playlist_store


def make_track(track_id, rel_path=None, url=None, title=None, artist=None, duration=None):
    return SimpleNamespace(
        id=track_id,
        path=rel_path,
        url=url,
        title=title,
        artist=artist,
        duration=duration,
    )


class PlaylistIOParseTests(unittest.TestCase):
    def test_parse_m3u_bom_comments_and_blank_lines(self):
        content = (
            "\ufeff#EXTM3U\r\n"
            "#EXTINF:240,Artist - Song\r\n"
            "album/song.flac\r\n"
            "\r\n"
            "# a comment line\r\n"
            "  album/other.flac  \r\n"
            "#EXTINF:-1,\n"
            "single.mp3\n"
        )
        self.assertEqual(
            playlist_io.parse_m3u_entries(content),
            ["album/song.flac", "album/other.flac", "single.mp3"],
        )

    def test_parse_m3u8_bom_comments_and_crlf(self):
        content = (
            "\ufeff#EXTM3U\n"
            "#EXTINF:180,Radio Edit\n"
            "mix/radio.flac\n"
            "#EXTINF:-1,\n"
            "mix/instrumental.flac\n"
            "\n"
        )
        self.assertEqual(
            playlist_io.parse_m3u_entries(content),
            ["mix/radio.flac", "mix/instrumental.flac"],
        )

    def test_parse_m3u_entries_empty_and_none(self):
        self.assertEqual(playlist_io.parse_m3u_entries(""), [])
        self.assertEqual(playlist_io.parse_m3u_entries(None), [])
        self.assertEqual(playlist_io.parse_m3u_entries("#EXTM3U\n# comment only\n"), [])


class PlaylistIODownloadFilenameTests(unittest.TestCase):
    def test_download_filename_slugified(self):
        self.assertEqual(playlist_io.playlist_download_filename("My Playlist"), "My-Playlist.m3u8")
        self.assertEqual(playlist_io.playlist_download_filename("a/b?c*"), "a-b-c.m3u8")
        self.assertEqual(playlist_io.playlist_download_filename("Mixed.Case_Name-v2"), "Mixed.Case_Name-v2.m3u8")

    def test_download_filename_fallbacks(self):
        self.assertEqual(playlist_io.playlist_download_filename(""), "playlist.m3u8")
        self.assertEqual(playlist_io.playlist_download_filename("   "), "playlist.m3u8")
        self.assertEqual(playlist_io.playlist_download_filename(None), "playlist.m3u8")


class PlaylistIOExportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.music_root = Path(self._tmp.name) / "music"
        self.settings = SimpleNamespace(MUSIC_ROOT=self.music_root, download_dir=Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_track_relative_m3u_path_under_music_root(self):
        track = make_track("t1", self.music_root / "album" / "song.flac")
        with patch.object(main, "settings", self.settings):
            self.assertEqual(playlist_io.track_relative_m3u_path(track, self.music_root), "album/song.flac")

    def test_track_relative_m3u_path_fallback_to_url_or_id(self):
        with patch.object(main, "settings", self.settings):
            self.assertEqual(
                playlist_io.track_relative_m3u_path(make_track("t2", None, url="http://h/stream.flac"), self.music_root),
                "stream.flac",
            )
            self.assertEqual(playlist_io.track_relative_m3u_path(make_track("t3"), self.music_root), "t3")

    def test_build_m3u_for_playlist_content_and_order(self):
        tracks = [
            make_track("t1", self.music_root / "album" / "song.flac", title="Song", artist="Artist", duration=240),
            make_track("t2", self.music_root / "album" / "second.flac", title="Second", duration=0),
            make_track("t3", self.music_root / "single" / "one.flac", title="One", duration=-1),
        ]
        scanner = SimpleNamespace(get_tracks=lambda refresh=True, **kwargs: tracks)
        playlist = SimpleNamespace(id="p1", name="My Mix", track_ids=["t3", "t1", "missing", "t2"])
        with patch.object(main, "settings", self.settings), patch.object(main.runtime.music_library, "scanner", scanner):
            content = playlist_io.build_m3u_for_playlist(playlist, tracks, self.music_root)
        self.assertEqual(
            content,
            "#EXTM3U\n"
            "#EXTINF:-1,One\nsingle/one.flac\n"
            "#EXTINF:240,Artist - Song\nalbum/song.flac\n"
            "#EXTINF:-1,Second\nalbum/second.flac\n",
        )

    def test_build_m3u_for_playlist_label_fallback_to_stem(self):
        tracks = [make_track("t1", self.music_root / "album" / "untitled.flac", title=None, duration=5)]
        scanner = SimpleNamespace(get_tracks=lambda refresh=True, **kwargs: tracks)
        playlist = SimpleNamespace(id="p1", name="P", track_ids=["t1"])
        with patch.object(main, "settings", self.settings), patch.object(main.runtime.music_library, "scanner", scanner):
            content = playlist_io.build_m3u_for_playlist(playlist, tracks, self.music_root)
        self.assertIn("#EXTINF:5,untitled\nalbum/untitled.flac", content)


class PlaylistIOResolveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.music_root = Path(self._tmp.name) / "music"
        self.settings = SimpleNamespace(MUSIC_ROOT=self.music_root, download_dir=Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_resolve_relative_and_absolute_paths(self):
        a = make_track("a", self.music_root / "album" / "song.flac")
        b = make_track("b", self.music_root / "album" / "other.flac")
        tracks = [a, b]
        abs_a = str((self.music_root / "album" / "song.flac").resolve())
        with patch.object(main, "settings", self.settings):
            self.assertEqual(
                playlist_io.resolve_m3u_track_ids(["album/song.flac", abs_a, "album/other.flac"], self.music_root, tracks=tracks),
                ["a", "b"],
            )

    def test_resolve_relative_with_base_dir(self):
        a = make_track("a", self.music_root / "album" / "song.flac")
        base_dir = self.music_root / "uploads" / "xyz"
        with patch.object(main, "settings", self.settings):
            self.assertEqual(
                playlist_io.resolve_m3u_track_ids(["song.flac"], self.music_root, base_dir=base_dir, tracks=[a]),
                ["a"],
            )
            self.assertEqual(
                playlist_io.resolve_m3u_track_ids(["../album/song.flac"], self.music_root, base_dir=base_dir, tracks=[a]),
                ["a"],
            )

    def test_album_relative_entry_wins_over_library_root_track(self):
        tracks = [
            make_track("root", self.music_root / "song.flac"),
            make_track("album", self.music_root / "album" / "song.flac"),
        ]
        self.assertEqual(
            playlist_io.resolve_m3u_track_ids(
                ["song.flac"], self.music_root,
                base_dir=self.music_root / "album", tracks=tracks,
            ),
            ["album"],
        )

    def test_exported_literal_percent_filename_round_trips(self):
        tracks = [
            make_track("literal", self.music_root / "100%20Live.flac", title="Live"),
            make_track("space", self.music_root / "100 Live.flac", title="Live"),
        ]
        playlist = SimpleNamespace(track_ids=["literal"])
        content = playlist_io.build_m3u_for_playlist(playlist, tracks, self.music_root)
        self.assertEqual(
            playlist_io.resolve_m3u_track_ids(
                playlist_io.parse_m3u_entries(content), self.music_root, tracks=tracks,
            ),
            ["literal"],
        )
        self.assertEqual(
            playlist_io.resolve_m3u_track_ids(
                [tracks[0].path.as_uri(), tracks[1].path.as_uri()],
                self.music_root, tracks=tracks,
            ),
            ["literal", "space"],
        )

    def test_resolve_windows_and_posix_variants(self):
        a = make_track("a", self.music_root / "album" / "song.flac")
        abs_a = str((self.music_root / "album" / "song.flac").resolve())
        # Tracks without a path are skipped entirely by the match index;
        # URL keys only exist for tracks that also have a path.
        url_track = make_track("u", self.music_root / "stream" / "radio.flac", url="http://example.com/stream/radio.flac")
        spaced = make_track("s", self.music_root / "album" / "song copy.flac")
        with patch.object(main, "settings", self.settings):
            # Windows backslashes are normalized.
            self.assertEqual(playlist_io.resolve_m3u_track_ids(["album\\song.flac"], self.music_root, tracks=[a]), ["a"])
            # file:// prefix is stripped.
            self.assertEqual(playlist_io.resolve_m3u_track_ids([f"file://{abs_a}"], self.music_root, tracks=[a]), ["a"])
            # POSIX absolute path matches the resolved absolute key.
            self.assertEqual(playlist_io.resolve_m3u_track_ids([abs_a], self.music_root, tracks=[a]), ["a"])
            # URL entries match the track URL key.
            self.assertEqual(
                playlist_io.resolve_m3u_track_ids(["http://example.com/stream/radio.flac"], self.music_root, tracks=[url_track]),
                ["u"],
            )
            # Quoted entries are unquoted.
            self.assertEqual(playlist_io.resolve_m3u_track_ids(['"album/song.flac"'], self.music_root, tracks=[a]), ["a"])
            # Percent-encoded entries are unquoted and match the decoded path.
            self.assertEqual(
                playlist_io.resolve_m3u_track_ids(["album/song%20copy.flac"], self.music_root, tracks=[spaced]),
                ["s"],
            )

    def test_resolve_unknown_and_duplicate_entries_keep_order(self):
        a = make_track("a", self.music_root / "album" / "a.flac")
        b = make_track("b", self.music_root / "album" / "b.flac")
        c = make_track("c", self.music_root / "album" / "c.flac")
        with patch.object(main, "settings", self.settings):
            result = playlist_io.resolve_m3u_track_ids(
                ["album/b.flac", "unknown.flac", "album/a.flac", "album/b.flac", "album/c.flac"], self.music_root,
                tracks=[a, b, c],
            )
        # Unknown entry skipped, duplicate dropped, order preserved.
        self.assertEqual(result, ["b", "a", "c"])

    def test_resolve_exact_relative_path_survives_basename_collision(self):
        # A track at the music root and a same-named track in a subfolder:
        # the root track's exact relative path must still resolve.  The old
        # single-index merge dropped it as "ambiguous" because the nested
        # track's basename collided with it.
        root_track = make_track("root", self.music_root / "song.flac")
        nested_track = make_track("nested", self.music_root / "album" / "song.flac")
        with patch.object(main, "settings", self.settings):
            self.assertEqual(
                playlist_io.resolve_m3u_track_ids(
                    ["song.flac"], self.music_root, tracks=[root_track, nested_track]
                ),
                ["root"],
            )
            self.assertEqual(
                playlist_io.resolve_m3u_track_ids(
                    ["album/song.flac"], self.music_root, tracks=[root_track, nested_track]
                ),
                ["nested"],
            )
            # Order of the track list must not matter.
            self.assertEqual(
                playlist_io.resolve_m3u_track_ids(
                    ["song.flac", "album/song.flac"],
                    self.music_root,
                    tracks=[nested_track, root_track],
                ),
                ["root", "nested"],
            )
            # A bare basename with no exact match still resolves when unique.
            self.assertEqual(
                playlist_io.resolve_m3u_track_ids(
                    ["song.flac"], self.music_root, tracks=[nested_track]
                ),
                ["nested"],
            )

    def test_resolve_empty_entries(self):
        with patch.object(main, "settings", self.settings):
            self.assertEqual(playlist_io.resolve_m3u_track_ids([], self.music_root, tracks=[]), [])


class PlaylistIOImportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.music_root = Path(self._tmp.name) / "music"
        self.settings = SimpleNamespace(MUSIC_ROOT=self.music_root, download_dir=Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_import_without_match_returns_none(self):
        with patch.object(main, "settings", self.settings), patch("library.playlist_io.save_new_playlist") as save:
            result = playlist_io.import_m3u_playlist(
                "nope.m3u8", "#EXTM3U\nunknown.flac\n", tracks=[],
                music_root=self.music_root,
            )
        self.assertIsNone(result)
        save.assert_not_called()

    def test_import_matches_and_returns_payload(self):
        track = make_track("t1", self.music_root / "album" / "song.flac", title="Song", duration=240)
        saved = SimpleNamespace(id="p9", name="mix", track_ids=["t1"])
        with patch.object(main, "settings", self.settings), patch("library.playlist_io.save_new_playlist", return_value=saved) as save:
            result = playlist_io.import_m3u_playlist(
                "mix.m3u8", "\ufeff#EXTM3U\n#EXTINF:240,Song\nalbum/song.flac\nunknown.flac\n",
                self.music_root,
                tracks=[track],
            )
        save.assert_called_once_with("mix", ["t1"])
        self.assertEqual(result, {
            "id": "p9",
            "name": "mix",
            "track_ids": ["t1"],
            "track_count": 1,
            "matched_track_count": 1,
            "entry_count": 2,
        })


class PlaylistIOApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.music_root = Path(self._tmp.name) / "music"
        self.settings = SimpleNamespace(MUSIC_ROOT=self.music_root, download_dir=Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def test_export_api_response_unchanged(self):
        tracks = [make_track("t1", self.music_root / "album" / "song.flac", title="Song", artist="Artist", duration=240)]
        scanner = SimpleNamespace(get_tracks=lambda refresh=True, **kwargs: tracks)
        playlist = SimpleNamespace(id="p1", name="My Mix", track_ids=["t1"])
        with (
            patch.object(main, "settings", self.settings),
            patch.object(main.runtime.music_library, "scanner", scanner),
            patch.object(library_api, "get_playlists", return_value=[playlist]),
        ):
            response = await library_api.export_playlist("p1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "audio/x-mpegurl; charset=utf-8")
        self.assertIn('attachment; filename="My-Mix.m3u8"', response.headers["content-disposition"])
        self.assertEqual(response.body.decode(), "#EXTM3U\n#EXTINF:240,Artist - Song\nalbum/song.flac\n")

    async def test_export_api_404_for_unknown_playlist(self):
        with patch.object(main, "settings", self.settings), patch.object(main.runtime.music_library, "scanner", SimpleNamespace(get_tracks=lambda refresh=True, **kwargs: [])), patch.object(library_api, "get_playlists", return_value=[]):
            with self.assertRaises(library_api.HTTPException) as ctx:
                await library_api.export_playlist("missing")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_upload_playlist_api_response_unchanged(self):
        tracks = [make_track("t1", self.music_root / "album" / "song.flac", title="Song", duration=240)]
        scanner = SimpleNamespace(get_tracks=lambda refresh=True, **kwargs: tracks)
        saved = SimpleNamespace(id="p9", name="mix", track_ids=["t1"])

        class FakeUpload:
            filename = "mix.m3u8"

            def __init__(self, content: bytes):
                self.content = content
                self._read = False

            async def read(self, size=-1):
                if self._read:
                    return b""
                self._read = True
                return self.content

            async def close(self):
                return None

        with (
            patch.object(main, "settings", self.settings),
            patch.object(main.runtime.music_library, "scanner", scanner),
            patch("library.playlist_io.save_new_playlist", return_value=saved) as save,
        ):
            payload = await library_api.upload_track(file=FakeUpload(b"#EXTM3U\nalbum/song.flac\n"))
        self.assertEqual(payload["status"], "imported")
        self.assertEqual(payload["kind"], "playlist")
        self.assertEqual(payload["filename"], "mix.m3u8")
        self.assertEqual(payload["track_count"], 1)
        self.assertEqual(payload["imported_playlist_count"], 1)
        self.assertEqual(payload["playlist"], {
            "id": "p9", "name": "mix", "track_ids": ["t1"],
            "track_count": 1, "matched_track_count": 1, "entry_count": 1,
        })
        self.assertIn("Imported playlist mix with 1 track", payload["message"])
        save.assert_called_once_with("mix", ["t1"])

    async def test_upload_playlist_without_match_raises_400(self):
        scanner = SimpleNamespace(get_tracks=lambda refresh=True, **kwargs: [])

        class FakeUpload:
            filename = "empty.m3u"

            def __init__(self):
                self._read = False

            async def read(self, size=-1):
                if self._read:
                    return b""
                self._read = True
                return b"#EXTM3U\nunknown.flac\n"

            async def close(self):
                return None

        with patch.object(main, "settings", self.settings), patch.object(main.runtime.music_library, "scanner", scanner):
            with self.assertRaises(library_api.HTTPException) as ctx:
                await library_api.upload_track(file=FakeUpload())
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Playlist did not match any library tracks")


# ---------------------------------------------------------------------------
# Import name collisions: an import adds a playlist, it never replaces one
# ---------------------------------------------------------------------------

class _IsolatedPlaylistStore:
    """Point library.playlists at a throwaway config dir and reset its cache."""

    def __init__(self, test, base: Path):
        self.playlists = playlist_store
        patcher = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(base / "config")})
        patcher.start()
        test.addCleanup(patcher.stop)
        self._reset()
        test.addCleanup(self._reset)

    def _reset(self):
        with self.playlists._cache_lock:
            self.playlists._cached_playlists = None
            self.playlists._cache_generation = 0

    def stored(self) -> dict:
        return {playlist.id: playlist for playlist in self.playlists.get_playlists()}


class PlaylistImportNameCollisionTests(unittest.IsolatedAsyncioTestCase):
    """An M3U import must never silently replace an existing user playlist."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.music_root = self.base / "music"
        self.music_root.mkdir(parents=True)
        self.settings = SimpleNamespace(MUSIC_ROOT=self.music_root, download_dir=self.base / "downloads")
        self.store = _IsolatedPlaylistStore(self, self.base)
        self.addCleanup(self._tmp.cleanup)
        self.track = make_track("t1", self.music_root / "one.mp3", title="One")

    def test_import_does_not_replace_an_existing_user_playlist(self):
        existing = self.store.playlists.save_playlist("Road Trip", ["local_a/one.mp3", "local_a/two.mp3"])

        result = playlist_io.import_m3u_playlist(
            "Road Trip.m3u8", "#EXTM3U\none.mp3\n", self.music_root, tracks=[self.track]
        )

        self.assertEqual(result["name"], "Road Trip (2)", "the import gets its own unique name")
        self.assertNotEqual(result["id"], existing.id)
        stored = self.store.stored()
        self.assertEqual(len(stored), 2, "the import is added, not substituted")
        self.assertEqual(stored[existing.id].name, "Road Trip")
        self.assertEqual(stored[existing.id].track_ids, ["local_a/one.mp3", "local_a/two.mp3"])
        self.assertEqual(stored[result["id"]].track_ids, ["t1"])

    def test_import_does_not_replace_a_case_variant(self):
        existing = self.store.playlists.save_playlist("Road Trip", ["local_a/one.mp3"])

        result = playlist_io.import_m3u_playlist(
            "Road Trip.m3u8", "#EXTM3U\none.mp3\n", self.music_root, tracks=[self.track]
        )

        self.assertNotEqual(result["id"], existing.id)
        self.assertEqual(self.store.stored()[existing.id].track_ids, ["local_a/one.mp3"])

    async def test_upload_endpoint_reports_the_unique_name(self):
        existing = self.store.playlists.save_playlist("Road Trip", ["local_a/one.mp3"])
        scanner = SimpleNamespace(get_tracks=lambda refresh=True, **kwargs: [self.track])

        class FakeUpload:
            filename = "Road Trip.m3u8"

            def __init__(self, content: bytes):
                self._chunks = [content]

            async def read(self, size=-1):
                return self._chunks.pop(0) if self._chunks else b""

            async def close(self):
                return None

        with patch.object(main, "settings", self.settings), \
                patch.object(main.runtime.music_library, "scanner", scanner):
            payload = await library_api.upload_track(
                file=FakeUpload(b"#EXTM3U\none.mp3\n")
            )

        self.assertEqual(payload["imported_playlist_count"], 1)
        self.assertEqual(payload["playlist"]["name"], "Road Trip (2)")
        stored = self.store.stored()
        self.assertEqual(stored[existing.id].track_ids, ["local_a/one.mp3"])


def _build_zip(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members:
            archive.writestr(name, content)
    return buffer.getvalue()


class ZipSameStemImportTests(unittest.IsolatedAsyncioTestCase):
    """Two ZIP members with the same stem must both be persisted."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.music_root = self.base / "music"
        (self.music_root / "album").mkdir(parents=True)
        self.download_dir = self.base / "downloads"
        self.settings = SimpleNamespace(MUSIC_ROOT=self.music_root, download_dir=self.download_dir)
        self.store = _IsolatedPlaylistStore(self, self.base)
        self.addCleanup(self._tmp.cleanup)
        self.tracks = [
            make_track("t1", self.music_root / "album" / "cd1.mp3", title="CD1 Track"),
            make_track("t2", self.music_root / "album" / "cd2.mp3", title="CD2 Track"),
        ]
        self.scanner = SimpleNamespace(
            music_root=self.music_root,
            get_tracks=lambda refresh=True, **kwargs: self.tracks,
            refresh=lambda *args, **kwargs: self.tracks,
        )

    async def _upload(self, members: list[tuple[str, bytes]], filename: str = "album.zip") -> dict:
        class FakeUpload:
            def __init__(self, content: bytes):
                self._chunks = [content]
                self.filename = filename

            async def read(self, size=-1):
                return self._chunks.pop(0) if self._chunks else b""

            async def close(self):
                return None

        with patch.object(main, "settings", self.settings), \
                patch.object(main.runtime.music_library, "scanner", self.scanner):
            return await library_api.upload_track(file=FakeUpload(_build_zip(members)))

    async def test_same_stem_zip_playlists_are_both_persisted(self):
        payload = await self._upload([
            ("CD1/mix.m3u8", b"#EXTM3U\ncd1.mp3\n"),
            ("CD2/mix.m3u8", b"#EXTM3U\ncd2.mp3\n"),
        ])

        self.assertEqual(payload["imported_playlist_count"], 2)
        stored = self.store.stored()
        self.assertEqual(len(stored), 2, "both members must be persisted, not just the last one")
        self.assertEqual(
            sorted(playlist.name for playlist in stored.values()),
            ["CD2 mix", "mix"],
            "the colliding name carries the member's position in the archive",
        )
        self.assertEqual(payload["imported_track_count"], 0)
        self.assertEqual(
            sorted(playlist["track_ids"][0] for playlist in payload["playlists"]),
            ["t1", "t2"],
            "each member keeps its own matched tracks",
        )

    async def test_same_stem_zip_does_not_overwrite_a_user_playlist(self):
        existing = self.store.playlists.save_playlist("mix", ["local_kept/track.mp3"])

        payload = await self._upload([
            ("CD1/mix.m3u8", b"#EXTM3U\ncd1.mp3\n"),
            ("CD2/mix.m3u8", b"#EXTM3U\ncd2.mp3\n"),
        ])

        self.assertEqual(payload["imported_playlist_count"], 2)
        self.assertEqual(
            self.store.stored()[existing.id].track_ids,
            ["local_kept/track.mp3"],
            "an existing user playlist must survive a ZIP import untouched",
        )
        self.assertEqual(len(self.store.stored()), 3)

    async def test_repeated_import_of_the_same_zip_keeps_persisting_both(self):
        members = [
            ("CD1/mix.m3u8", b"#EXTM3U\ncd1.mp3\n"),
            ("CD2/mix.m3u8", b"#EXTM3U\ncd2.mp3\n"),
        ]
        first = await self._upload(members, filename="album.zip")
        second = await self._upload(members, filename="album2.zip")
        self.assertEqual(first["imported_playlist_count"], 2)
        self.assertEqual(second["imported_playlist_count"], 2)
        self.assertEqual(len(self.store.stored()), 4)

    async def test_single_playlist_zip_keeps_the_plain_stem(self):
        payload = await self._upload([("mix.m3u8", b"#EXTM3U\ncd1.mp3\n")])
        self.assertEqual(payload["imported_playlist_count"], 1)
        self.assertEqual(payload["playlists"][0]["name"], "mix")


class ZipPlaylistRollbackTests(unittest.IsolatedAsyncioTestCase):
    """A failed ZIP playlist persist must not leave partial playlists behind."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.music_root = self.base / "music"
        (self.music_root / "album").mkdir(parents=True)
        self.download_dir = self.base / "downloads"
        self.settings = SimpleNamespace(MUSIC_ROOT=self.music_root, download_dir=self.download_dir)
        self.store = _IsolatedPlaylistStore(self, self.base)
        self.addCleanup(self._tmp.cleanup)
        self.tracks = [
            make_track("t1", self.music_root / "album" / "cd1.mp3", title="CD1 Track"),
            make_track("t2", self.music_root / "album" / "cd2.mp3", title="CD2 Track"),
        ]
        self.scanner = SimpleNamespace(
            music_root=self.music_root,
            get_tracks=lambda refresh=True, **kwargs: self.tracks,
            refresh=lambda *args, **kwargs: self.tracks,
        )

    async def _upload(self, members: list[tuple[str, bytes]], filename: str = "album.zip") -> dict:
        class FakeUpload:
            def __init__(self, content: bytes):
                self._chunks = [content]
                self.filename = filename

            async def read(self, size=-1):
                return self._chunks.pop(0) if self._chunks else b""

            async def close(self):
                return None

        with patch.object(main, "settings", self.settings), \
                patch.object(main.runtime.music_library, "scanner", self.scanner):
            return await library_api.upload_track(file=FakeUpload(_build_zip(members)))

    async def test_second_playlist_persist_failure_rolls_back_first(self):
        existing = self.store.playlists.save_playlist("keep-me", ["t1"])
        real_save = playlist_io.save_new_playlist
        calls = {"count": 0}

        def flaky(name, track_ids):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("boom persist 2")
            return real_save(name, track_ids)

        with patch.object(playlist_io, "save_new_playlist", side_effect=flaky):
            with self.assertRaises(library_api.HTTPException) as ctx:
                await self._upload([
                    ("CD1/mix.m3u8", b"#EXTM3U\ncd1.mp3\n"),
                    ("CD2/mix.m3u8", b"#EXTM3U\ncd2.mp3\n"),
                ])
        self.assertEqual(ctx.exception.status_code, 500)
        stored = self.store.stored()
        self.assertEqual(set(stored), {existing.id}, "no playlist from this import may remain")
        self.assertEqual(stored[existing.id].track_ids, ["t1"], "user playlists stay unchanged")


if __name__ == "__main__":
    unittest.main()
