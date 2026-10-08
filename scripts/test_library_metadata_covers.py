#!/usr/bin/env python3
"""Optional artwork failures must not lose matched album metadata."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library import metadata


class MetadataCoverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.store = metadata.LibraryMetadataStore(db_path=root / "metadata.sqlite", cover_dir=root / "covers",
                                                   artist_enrichment=Mock())
        with self.store._connect() as conn:
            conn.execute("INSERT INTO albums (album_key, album, artist) VALUES ('album', 'Album', 'Artist')")

    def row(self):
        with self.store._connect() as conn:
            return conn.execute("SELECT * FROM albums WHERE album_key = 'album'").fetchone()

    def test_all_saved_cover_formats_are_reused_without_network(self):
        for suffix, mime in ((".jpg", "image/jpeg"), (".png", "image/png"), (".webp", "image/webp")):
            with self.subTest(suffix=suffix):
                path = self.store.cover_dir / f"album{suffix}"
                path.write_bytes(b"cached")
                with patch.object(metadata, "safe_get", side_effect=RuntimeError("network must not run")):
                    self.assertEqual(self.store._fetch_cover("album", "release"), (path, mime))
                path.unlink()

    def test_cover_failure_persists_text_and_retry_success_clears_error(self):
        match = {"mb_release_id": "release", "genres": ["Rock"], "album_description": "Album facts",
                 "artist_description": "Artist facts"}
        with patch.object(self.store, "_find_musicbrainz_release", return_value=match), \
                patch.object(self.store, "_fetch_cover", side_effect=TimeoutError("cover timed out")):
            self.store._enrich_album(self.row())
        row = self.row()
        self.assertEqual(row["mb_release_id"], "release")
        self.assertEqual(row["genres_json"], '["Rock"]')
        self.assertEqual(row["album_description"], "Album facts")
        self.assertEqual(row["artist_description"], "Artist facts")
        self.assertIn("cover timed out", row["fetch_error"])
        with patch.object(self.store, "_find_musicbrainz_release", return_value=match), \
                patch.object(self.store, "_fetch_cover", return_value=(None, None)):
            self.store._enrich_album(row)
        self.assertIsNone(self.row()["fetch_error"])


if __name__ == "__main__":
    unittest.main()
