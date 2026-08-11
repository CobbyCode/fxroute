#!/usr/bin/env python3
"""Behavior tests for M3U/M3U8 playlist I/O (playlist_io + main.py wrappers).

Covers REFACTOR-002 parity: parsing (BOM/comments), path resolution
(relative/absolute, Windows/POSIX, file://), unknown/duplicate entries,
ordering, export content, download filename, import without match, and the
existing API responses for playlist import and export.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
import library_api
import playlist_io


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
        with patch.object(main, "settings", self.settings), patch.object(main, "library_scanner", scanner):
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
        with patch.object(main, "settings", self.settings), patch.object(main, "library_scanner", scanner):
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
        with patch.object(main, "settings", self.settings), patch("playlist_io.save_playlist") as save:
            result = playlist_io.import_m3u_playlist(
                "nope.m3u8", "#EXTM3U\nunknown.flac\n", tracks=[],
                music_root=self.music_root,
            )
        self.assertIsNone(result)
        save.assert_not_called()

    def test_import_matches_and_returns_payload(self):
        track = make_track("t1", self.music_root / "album" / "song.flac", title="Song", duration=240)
        saved = SimpleNamespace(id="p9", name="mix", track_ids=["t1"])
        with patch.object(main, "settings", self.settings), patch("playlist_io.save_playlist", return_value=saved) as save:
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

    def test_main_wrappers_delegate_to_playlist_io(self):
        # Thin wrappers in main.py must stay behavior-identical.
        self.assertEqual(
            main._parse_m3u_entries("\ufeff#EXTM3U\ntrack.mp3\n"),
            playlist_io.parse_m3u_entries("\ufeff#EXTM3U\ntrack.mp3\n"),
        )
        self.assertEqual(
            main._playlist_download_filename("My Playlist"),
            playlist_io.playlist_download_filename("My Playlist"),
        )
        with patch.object(main, "settings", self.settings), patch.object(main, "library_scanner", SimpleNamespace(get_tracks=lambda refresh=True, **kwargs: [])):
            self.assertEqual(
                main._resolve_m3u_track_ids(["album/song.flac"], tracks=[]),
                playlist_io.resolve_m3u_track_ids(["album/song.flac"], self.music_root, tracks=[]),
            )


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
            patch.object(main, "library_scanner", scanner),
            patch.object(library_api, "get_playlists", return_value=[playlist]),
        ):
            response = await library_api.export_playlist("p1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "audio/x-mpegurl; charset=utf-8")
        self.assertIn('attachment; filename="My-Mix.m3u8"', response.headers["content-disposition"])
        self.assertEqual(response.body.decode(), "#EXTM3U\n#EXTINF:240,Artist - Song\nalbum/song.flac\n")

    async def test_export_api_404_for_unknown_playlist(self):
        with patch.object(main, "settings", self.settings), patch.object(main, "library_scanner", SimpleNamespace(get_tracks=lambda refresh=True, **kwargs: [])), patch.object(library_api, "get_playlists", return_value=[]):
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
            patch.object(main, "library_scanner", scanner),
            patch("playlist_io.save_playlist", return_value=saved) as save,
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

        with patch.object(main, "settings", self.settings), patch.object(main, "library_scanner", scanner):
            with self.assertRaises(library_api.HTTPException) as ctx:
                await library_api.upload_track(file=FakeUpload())
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Playlist did not match any library tracks")


if __name__ == "__main__":
    unittest.main()
