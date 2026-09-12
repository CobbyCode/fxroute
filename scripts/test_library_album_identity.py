#!/usr/bin/env python3
"""Album identity: same-titled albums of different artists stay separate.

A library must not merge two artists' identically named albums into one
``Various Artists`` release just because the album title matches.  Only tracks
that share the album folder may form a compilation; listing and resolution must
derive the same album identity.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library.core import LibraryScanner
from models import Track


class _FakeMetadataStore:
    def get_album(self, album_id):
        return {}


def _scanner(tracks) -> LibraryScanner:
    scanner = LibraryScanner.__new__(LibraryScanner)
    scanner._track_cache = list(tracks)
    scanner.metadata_store = _FakeMetadataStore()
    return scanner


class AlbumIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _track(self, track_id, *, artist, album, folder, album_artist=None):
        folder_path = self.root / folder
        folder_path.mkdir(parents=True, exist_ok=True)
        return Track(
            id=track_id,
            title=track_id,
            artist=artist,
            album=album,
            album_artist=album_artist,
            path=folder_path / f"{track_id}.flac",
        )

    def test_same_album_title_different_artists_stay_separate(self):
        tracks = [
            self._track("a1", artist="Artist A", album="Live", folder="a"),
            self._track("a2", artist="Artist A", album="Live", folder="a"),
            self._track("b1", artist="Artist B", album="Live", folder="b"),
            self._track("b2", artist="Artist B", album="Live", folder="b"),
        ]
        scanner = _scanner(tracks)
        albums = scanner.get_albums(include_metadata=False)

        by_artist = {album["artist"]: album for album in albums}
        self.assertNotIn("Various Artists", by_artist)
        self.assertEqual(set(by_artist), {"Artist A", "Artist B"})
        self.assertEqual(by_artist["Artist A"]["track_count"], 2)
        self.assertEqual(by_artist["Artist B"]["track_count"], 2)

        # Resolution uses the same identity for both albums.
        for artist in ("Artist A", "Artist B"):
            album_id = by_artist[artist]["id"]
            resolved = scanner.get_album_tracks(album_id)
            self.assertEqual(len(resolved), 2)
            self.assertEqual({track.artist for track in resolved}, {artist})

    def test_shared_folder_compilation_still_collapses_to_various_artists(self):
        tracks = [
            self._track("c1", artist="Artist A", album="Mixtape", folder="mix"),
            self._track("c2", artist="Artist B", album="Mixtape", folder="mix"),
        ]
        scanner = _scanner(tracks)
        albums = scanner.get_albums(include_metadata=False)

        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["artist"], "Various Artists")
        self.assertEqual(albums[0]["track_count"], 2)

        resolved = scanner.get_album_tracks(albums[0]["id"])
        self.assertEqual({track.id for track in resolved}, {"c1", "c2"})

    def test_explicit_album_artist_is_never_treated_as_compilation(self):
        tracks = [
            self._track("d1", artist="Artist A", album="Split", folder="s", album_artist="Various Artists"),
            self._track("d2", artist="Artist B", album="Split", folder="s", album_artist="Various Artists"),
        ]
        scanner = _scanner(tracks)
        albums = scanner.get_albums(include_metadata=False)

        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["artist"], "Various Artists")
        self.assertEqual(albums[0]["track_count"], 2)


if __name__ == "__main__":
    unittest.main()
