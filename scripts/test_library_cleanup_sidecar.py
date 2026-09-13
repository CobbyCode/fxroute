#!/usr/bin/env python3
"""Track-parent cleanup must not delete user data.

Only known artwork names and empty sidecar files are removable. A folder
holding an unrelated image (vacation.jpg) or non-empty notes/logs keeps
those files and the folder itself.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from library.core import (
    cleanup_track_parent_folder,
    is_cleanup_only_file,
    is_removable_artwork_file,
    is_removable_metadata_sidecar,
)


class CleanupSidecarTests(unittest.TestCase):
    def test_unrelated_image_is_not_cleanup_only(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "vacation.jpg"
            target.write_bytes(b"x")
            self.assertFalse(is_removable_artwork_file(target))
            self.assertFalse(is_cleanup_only_file(target))

    def test_nonempty_sidecar_is_kept(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "notes.txt"
            target.write_bytes(b"important, not empty")
            self.assertFalse(is_removable_metadata_sidecar(target))
            self.assertFalse(is_cleanup_only_file(target))

    def test_empty_sidecar_is_removable(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "empty.nfo"
            target.write_bytes(b"")
            self.assertTrue(is_removable_metadata_sidecar(target))
            self.assertTrue(is_cleanup_only_file(target))

    def test_folder_with_user_data_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "music"
            root.mkdir()
            folder = root / "album"
            folder.mkdir()
            (folder / "vacation.jpg").write_bytes(b"x")
            (folder / "notes.txt").write_bytes(b"important notes")
            (folder / "session.log").write_bytes(b"log data")
            result = cleanup_track_parent_folder(folder, root, set())
            self.assertEqual(result["removed_files"], [])
            self.assertFalse(result["removed_folder"])
            self.assertTrue(folder.is_dir())
            self.assertEqual(
                sorted(p.name for p in folder.iterdir()),
                ["notes.txt", "session.log", "vacation.jpg"],
            )

    def test_cover_plus_empty_sidecar_still_cleans(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "music"
            root.mkdir()
            folder = root / "album"
            folder.mkdir()
            (folder / "cover.jpg").write_bytes(b"x")
            (folder / "empty.nfo").write_bytes(b"")
            result = cleanup_track_parent_folder(folder, root, set())
            self.assertFalse(folder.exists())
            self.assertTrue(result["removed_folder"])
            self.assertEqual(len(result["removed_files"]), 2)


if __name__ == "__main__":
    unittest.main()
