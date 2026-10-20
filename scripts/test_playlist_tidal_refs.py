#!/usr/bin/env python3
"""Playlists that mix local tracks and TIDAL references (``tidal:<id>``).

Covers storage (references, whitelisted display metadata, unchanged format
for local-only playlists, old files, same-name overwrite), the playlist API
payloads and the M3U export that leaves TIDAL titles out and says so.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library.api as library_api
import library.playlist_io as playlist_io
import library.playlists as playlists
import main
from models import PlaylistSaveRequest

META = {
    "tidal:101": {"title": "One", "artist": "A", "album": "Alb", "duration": 200, "art_url": "https://img/1"},
    "tidal:102": {"title": "Two", "artist": "B"},
}


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.root)
        self.file = self.root / "fxroute" / "playlists.json"
        self.file.parent.mkdir(parents=True, exist_ok=True)
        legacy = patch.object(playlists, "_legacy_playlists_file", return_value=self.root / "legacy.json")
        legacy.start()
        self.addCleanup(legacy.stop)
        self.addCleanup(self._tmp.cleanup)
        self._reset_cache()

    def _reset_cache(self):
        with playlists._cache_lock:
            playlists._cached_playlists = None
            playlists._cache_generation = 0

    def _seed(self, payload):
        self.file.write_text(json.dumps(payload), encoding="utf-8")
        self._reset_cache()

    def _disk(self):
        return json.loads(self.file.read_text(encoding="utf-8"))


class PlaylistStoreTests(_StoreCase):
    def test_mixed_playlist_keeps_order_refs_and_metadata(self):
        saved = playlists.save_playlist("Mix", ["a", "tidal:101", "b", "tidal:102"], META)
        self.assertEqual(saved.track_ids, ["a", "tidal:101", "b", "tidal:102"])
        self._reset_cache()
        loaded = playlists.get_playlists()[0]
        self.assertEqual(loaded.track_ids, ["a", "tidal:101", "b", "tidal:102"])
        self.assertEqual(loaded.track_meta["tidal:101"], {
            "title": "One", "artist": "A", "album": "Alb", "duration": 200.0, "art_url": "https://img/1",
        })
        self.assertEqual(loaded.track_meta["tidal:102"], {"title": "Two", "artist": "B"})

    def test_local_only_playlist_document_is_unchanged(self):
        playlists.save_playlist("Plain", ["a", "b"])
        self.assertEqual(self._disk(), [{"id": "plain", "name": "Plain", "track_ids": ["a", "b"]}])

    def test_metadata_without_a_matching_reference_is_dropped(self):
        meta = dict(META, **{"tidal:999": {"title": "Orphan"}, "a": {"title": "Local id"}})
        saved = playlists.save_playlist("Mix", ["a", "tidal:101"], meta)
        self.assertEqual(list(saved.track_meta), ["tidal:101"])
        self.assertEqual(list(self._disk()[0]["track_meta"]), ["tidal:101"])

    def test_metadata_is_whitelisted_and_capped(self):
        meta = {"tidal:101": {
            "title": "x" * 1000, "artist": 5, "album": "  ", "duration": -3, "url": "https://stream", "stream": "secret",
            "art_url": "https://img/" + "y" * 2000,
        }}
        saved = playlists.save_playlist("Mix", ["tidal:101"], meta)
        entry = saved.track_meta["tidal:101"]
        self.assertEqual(set(entry), {"title", "art_url"})
        self.assertEqual(len(entry["title"]), playlists._TRACK_META_TEXT_LIMIT)
        self.assertEqual(len(entry["art_url"]), playlists._TRACK_META_URL_LIMIT)
        self.assertNotIn("stream", self._disk()[0]["track_meta"]["tidal:101"])

    def test_malformed_references_are_rejected(self):
        for bad in ("tidal:", "tidal:../x", "tidal:a b", "tidal:" + "9" * 65):
            with self.assertRaises(ValueError, msg=bad):
                playlists.save_playlist("Mix", ["a", bad])
        self.assertEqual(self._disk() if self.file.exists() else [], [])

    def test_save_new_playlist_takes_references_and_a_unique_name(self):
        playlists.save_playlist("Mix", ["a"])
        created = playlists.save_new_playlist("Mix", ["tidal:101"], META)
        self.assertEqual(created.name, "Mix (2)")
        self.assertEqual(created.track_meta, {"tidal:101": META["tidal:101"] | {"duration": 200.0}})

    def test_same_name_overwrite_replaces_metadata_and_keeps_other_keys(self):
        self._seed([{"id": "mix", "name": "Mix", "track_ids": ["a"], "future_key": 7}])
        playlists.save_playlist("mix", ["tidal:101"], META)
        doc = self._disk()[0]
        self.assertEqual(doc["future_key"], 7)
        self.assertEqual(doc["track_ids"], ["tidal:101"])
        self.assertIn("tidal:101", doc["track_meta"])
        playlists.save_playlist("mix", ["a", "b"])
        self.assertNotIn("track_meta", self._disk()[0])

    def test_files_from_before_tidal_refs_still_load(self):
        self._seed([{"id": "old", "name": "Old", "track_ids": ["a", "b"]}])
        loaded = playlists.get_playlists()
        self.assertEqual([(p.id, p.track_ids, p.track_meta) for p in loaded], [("old", ["a", "b"], {})])

    def test_garbage_metadata_in_the_file_is_ignored(self):
        self._seed([{"id": "x", "name": "X", "track_ids": ["tidal:1"], "track_meta": "nope"},
                    {"id": "y", "name": "Y", "track_ids": ["tidal:2"], "track_meta": {"tidal:2": ["list"]}}])
        loaded = {p.id: p for p in playlists.get_playlists()}
        self.assertEqual(loaded["x"].track_meta, {})
        self.assertEqual(loaded["y"].track_meta, {})

    def test_helpers(self):
        self.assertTrue(playlists.is_tidal_ref("tidal:12"))
        self.assertFalse(playlists.is_tidal_ref("12"))
        self.assertEqual(playlists.tidal_ref_id("tidal:12"), "12")


class PlaylistApiTests(_StoreCase, unittest.IsolatedAsyncioTestCase):
    async def test_save_and_list_carry_metadata_only_for_mixed_playlists(self):
        req = PlaylistSaveRequest(name="Mix", track_ids=["a", "tidal:101"], track_meta=META)
        saved = (await library_api.create_or_update_playlist(req))["playlist"]
        self.assertEqual(saved["track_count"], 2)
        self.assertEqual(saved["track_meta"]["tidal:101"]["title"], "One")
        await library_api.create_or_update_playlist(PlaylistSaveRequest(name="Plain", track_ids=["a"]))
        listed = {item["name"]: item for item in await library_api.list_playlists()}
        self.assertIn("track_meta", listed["Mix"])
        self.assertNotIn("track_meta", listed["Plain"])

    async def test_malformed_reference_is_a_400(self):
        with self.assertRaises(library_api.HTTPException) as ctx:
            await library_api.create_or_update_playlist(PlaylistSaveRequest(name="Bad", track_ids=["tidal:"]))
        self.assertEqual(ctx.exception.status_code, 400)


def _track(track_id, rel, title, artist):
    return SimpleNamespace(id=track_id, path=rel, url=None, title=title, artist=artist, duration=240)


class PlaylistExportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.settings = SimpleNamespace(MUSIC_ROOT=self.root)
        self.tracks = [_track("t1", self.root / "a" / "one.flac", "One", "Artist"),
                       _track("t2", self.root / "a" / "two.flac", "Two", "Artist")]

    def test_build_skips_tidal_refs_and_names_them(self):
        playlist = SimpleNamespace(
            id="p", name="Mix", track_ids=["t1", "tidal:101", "t2", "tidal:102"],
            track_meta={"tidal:101": {"title": "Streamed\nSong", "artist": "Band"}},
        )
        text = playlist_io.build_m3u_for_playlist(playlist, self.tracks, self.root)
        lines = text.splitlines()
        self.assertEqual(lines[0], "#EXTM3U")
        self.assertEqual(lines[1], "# FXRoute: 2 TIDAL track(s) not exported (streaming only)")
        self.assertEqual(lines[2], "# not exported (TIDAL): Band - Streamed Song")
        self.assertEqual(lines[3], "# not exported (TIDAL): tidal:102")
        self.assertEqual([line for line in lines if not line.startswith("#")], ["a/one.flac", "a/two.flac"])
        self.assertNotIn("tidal:101\n", text.replace("# not exported (TIDAL): ", ""))

    def test_export_without_tidal_refs_is_byte_identical_to_before(self):
        playlist = SimpleNamespace(id="p", name="Mix", track_ids=["t1"])
        text = playlist_io.build_m3u_for_playlist(playlist, self.tracks, self.root)
        self.assertEqual(text, "#EXTM3U\n#EXTINF:240,Artist - One\na/one.flac\n")

    def test_comment_lines_do_not_come_back_as_import_entries(self):
        playlist = SimpleNamespace(id="p", name="Mix", track_ids=["t1", "tidal:101"], track_meta={})
        text = playlist_io.build_m3u_for_playlist(playlist, self.tracks, self.root)
        self.assertEqual(playlist_io.parse_m3u_entries(text), ["a/one.flac"])

    def test_only_tidal_refs_has_nothing_to_export(self):
        playlist = SimpleNamespace(id="p", name="Mix", track_ids=["tidal:101", "tidal:102"], track_meta={})
        with self.assertRaises(playlist_io.PlaylistHasNoLocalTracksError) as ctx:
            playlist_io.build_m3u_for_playlist(playlist, self.tracks, self.root)
        self.assertEqual(ctx.exception.tidal_count, 2)

    def test_missing_local_id_still_fails_closed_next_to_tidal_refs(self):
        playlist = SimpleNamespace(id="p", name="Mix", track_ids=["t1", "gone", "tidal:101"], track_meta={})
        with self.assertRaises(playlist_io.PlaylistTrackUnavailableError) as ctx:
            playlist_io.build_m3u_for_playlist(playlist, self.tracks, self.root)
        self.assertEqual(ctx.exception.missing_track_ids, ["gone"])

    async def _export(self, playlist):
        scanner = SimpleNamespace(get_tracks=lambda refresh=True, **kwargs: self.tracks)
        with (
            patch.object(main, "settings", self.settings),
            patch.object(main.runtime.music_library, "scanner", scanner),
            patch.object(library_api, "get_playlists", return_value=[playlist]),
        ):
            return await library_api.export_playlist(playlist.id)

    async def test_api_reports_the_skipped_count_in_a_header(self):
        playlist = SimpleNamespace(id="p", name="Mix", track_ids=["t1", "tidal:101", "tidal:102"], track_meta={})
        response = await self._export(playlist)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-fxroute-skipped-tidal"], "2")
        self.assertIn("2 TIDAL track(s) not exported", response.body.decode())

    async def test_api_has_no_skip_header_for_local_playlists(self):
        playlist = SimpleNamespace(id="p", name="Mix", track_ids=["t1"], track_meta={})
        response = await self._export(playlist)
        self.assertNotIn("x-fxroute-skipped-tidal", response.headers)

    async def test_api_answers_409_for_a_tidal_only_playlist(self):
        playlist = SimpleNamespace(id="p", name="Mix", track_ids=["tidal:101"], track_meta={})
        with self.assertRaises(library_api.HTTPException) as ctx:
            await self._export(playlist)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["error"], "playlist_has_no_local_tracks")
        self.assertEqual(ctx.exception.detail["tidal_track_count"], 1)


if __name__ == "__main__":
    unittest.main()
