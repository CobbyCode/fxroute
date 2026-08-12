#!/usr/bin/env python3
"""Behavior tests for the REFACTOR-008 extraction:

- zip_album.dedupe_archive_name
- zip_album.choose_unique_path
- zip_album.choose_unique_dir
- zip_album.is_safe_relative_zip_path
- zip_album.extract_zip_album

plus wrapper parity against main._dedupe_archive_name and
main._is_safe_relative_zip_path.
Tests use real temporary ZIP files (zipfile, tmp dirs).
"""
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
from fastapi import HTTPException
from zip_album import (
    choose_unique_dir,
    choose_unique_path,
    dedupe_archive_name,
    extract_zip_album,
    is_safe_relative_zip_path,
)


def _write_zip(zip_path: Path, members: list[tuple[str, bytes]]) -> None:
    """Echte ZIP-Datei mit (name, content)-Members schreiben."""
    with zipfile.ZipFile(zip_path, "w") as archive:
        for name, content in members:
            archive.writestr(name, content)


def _corrupt_zip_data(zip_path: Path, member_name: str) -> None:
    """Corrupt the data of a STORED member so that testzip() fails."""
    data = bytearray(zip_path.read_bytes())
    # local header: 30 bytes + filename + (extra field, empty here)
    offset = 30 + len(member_name.encode())
    data[offset] ^= 0xFF
    zip_path.write_bytes(bytes(data))


class DedupeArchiveNameTests(unittest.TestCase):
    def test_first_name_kept(self):
        used: set[str] = set()
        self.assertEqual(dedupe_archive_name("track.mp3", used), "track.mp3")
        self.assertEqual(used, {"track.mp3"})

    def test_collision_suffix_increments(self):
        used: set[str] = {"track.mp3"}
        self.assertEqual(dedupe_archive_name("track.mp3", used), "track-2.mp3")
        self.assertEqual(dedupe_archive_name("track.mp3", used), "track-3.mp3")
        self.assertEqual(used, {"track.mp3", "track-2.mp3", "track-3.mp3"})

    def test_suffix_preserved(self):
        used: set[str] = {"cover.jpg"}
        self.assertEqual(dedupe_archive_name("cover.jpg", used), "cover-2.jpg")

    def test_path_basename_used(self):
        used: set[str] = set()
        self.assertEqual(dedupe_archive_name("sub/dir/track.flac", used), "track.flac")

    def test_empty_name_falls_back_to_track(self):
        used: set[str] = set()
        self.assertEqual(dedupe_archive_name("", used), "track")
        self.assertEqual(dedupe_archive_name(None, used), "track-2")


class ChooseUniquePathTests(unittest.TestCase):
    def test_free_path_returned(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "album" / "track.mp3"
            self.assertEqual(choose_unique_path(target), target)

    def test_collision_increments(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "track.mp3").write_bytes(b"x")
            (base / "track-2.mp3").write_bytes(b"x")
            result = choose_unique_path(base / "track.mp3")
            self.assertEqual(result, base / "track-3.mp3")

    def test_extension_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "cover.jpg").write_bytes(b"x")
            result = choose_unique_path(base / "cover.jpg")
            self.assertEqual(result, base / "cover-2.jpg")


class ChooseUniqueDirTests(unittest.TestCase):
    def test_free_dir_returned(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "album"
            self.assertEqual(choose_unique_dir(target), target)

    def test_collision_increments(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "album").mkdir()
            (base / "album-2").mkdir()
            result = choose_unique_dir(base / "album")
            self.assertEqual(result, base / "album-3")


class IsSafeRelativeZipPathTests(unittest.TestCase):
    def test_safe_relative_path(self):
        self.assertEqual(
            is_safe_relative_zip_path("album/track.mp3"),
            Path("album/track.mp3"),
        )

    def test_backslash_normalized(self):
        self.assertEqual(
            is_safe_relative_zip_path("album\\track.mp3"),
            Path("album/track.mp3"),
        )

    def test_empty_rejected(self):
        self.assertIsNone(is_safe_relative_zip_path(""))
        self.assertIsNone(is_safe_relative_zip_path("///"))

    def test_traversal_rejected(self):
        self.assertIsNone(is_safe_relative_zip_path("../evil.mp3"))
        self.assertIsNone(is_safe_relative_zip_path("a/../../evil.mp3"))
        self.assertIsNone(is_safe_relative_zip_path("../a/../evil.mp3"))

    def test_dot_segment_normalized_by_pathlib(self):
        # pathlib normalizes "." segments -> allowed as a/b (stays under target)
        self.assertEqual(is_safe_relative_zip_path("a/./b"), Path("a/b"))

    def test_leading_slashes_rejected(self):
        # Hardening: absolute paths are never treated as relative; an
        # archive with leading-slash members is refused outright.
        self.assertIsNone(is_safe_relative_zip_path("/etc/passwd"))
        self.assertIsNone(is_safe_relative_zip_path("//server/share/x"))

    def test_drive_style_path_rejected(self):
        # Hardening: Windows drive-letter paths are rejected even though
        # they are not absolute on POSIX.
        self.assertIsNone(is_safe_relative_zip_path("C:/evil.mp3"))
        self.assertIsNone(is_safe_relative_zip_path("C:\\evil.mp3"))
        self.assertIsNone(is_safe_relative_zip_path("Z:/album/x.flac"))

    def test_macosx_metadata_rejected(self):
        self.assertIsNone(is_safe_relative_zip_path("__MACOSX/album/cover.jpg"))
        self.assertIsNone(is_safe_relative_zip_path("album/__MACOSX/x"))

    def test_ds_store_rejected(self):
        self.assertIsNone(is_safe_relative_zip_path(".DS_Store"))
        self.assertIsNone(is_safe_relative_zip_path("album/.ds_store"))

    def test_thumbs_db_rejected(self):
        self.assertIsNone(is_safe_relative_zip_path("thumbs.db"))


class ExtractZipAlbumTests(unittest.TestCase):
    def test_safe_zip_extracts_with_categories(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            zip_path = base / "album.zip"
            target = base / "out"
            _write_zip(zip_path, [
                ("track.mp3", b"audio"),
                ("cover.jpg", b"image"),
                ("playlist.m3u8", b"#EXTM3U"),
                ("notes.txt", b"notes"),
            ])
            result = extract_zip_album(zip_path, target)
            self.assertEqual(len(result["extracted_files"]), 4)
            self.assertEqual(len(result["audio_files"]), 1)
            self.assertEqual(result["audio_files"][0], target / "track.mp3")
            self.assertEqual(len(result["playlist_files"]), 1)
            self.assertEqual(result["playlist_files"][0], target / "playlist.m3u8")
            self.assertEqual(result["skipped_entries"], [])
            self.assertTrue((target / "track.mp3").is_file())
            self.assertTrue((target / "notes.txt").is_file())

    def test_metadata_entries_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            zip_path = base / "album.zip"
            target = base / "out"
            _write_zip(zip_path, [
                ("track.flac", b"audio"),
                ("__MACOSX/track.flac", b"meta"),
                ("album/.DS_Store", b"meta"),
            ])
            result = extract_zip_album(zip_path, target)
            self.assertEqual(len(result["extracted_files"]), 1)
            self.assertEqual(result["skipped_entries"], ["__MACOSX/track.flac", "album/.DS_Store"])
            self.assertFalse((target / "__MACOSX").exists())
            self.assertFalse((target / "album" / ".DS_Store").exists())

    def test_traversal_entries_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            zip_path = base / "album.zip"
            target = base / "out"
            _write_zip(zip_path, [
                ("safe.mp3", b"audio"),
                ("../evil.mp3", b"evil"),
            ])
            result = extract_zip_album(zip_path, target)
            self.assertEqual(len(result["extracted_files"]), 1)
            self.assertEqual(result["skipped_entries"], ["../evil.mp3"])
            self.assertFalse((base / "evil.mp3").exists())
            self.assertTrue((target / "safe.mp3").is_file())

    def test_leading_slash_entry_skipped(self):
        # Hardening: absolute-path members are refused and skipped, never
        # stripped down to a relative path under the target.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            zip_path = base / "album.zip"
            target = base / "out"
            _write_zip(zip_path, [
                ("safe.mp3", b"audio"),
                ("/abs.mp3", b"evil"),
            ])
            result = extract_zip_album(zip_path, target)
            self.assertEqual(len(result["extracted_files"]), 1)
            self.assertEqual(result["skipped_entries"], ["/abs.mp3"])
            self.assertFalse((base / "abs.mp3").exists())
            self.assertFalse((target / "abs.mp3").exists())
            self.assertTrue((target / "safe.mp3").is_file())

    def test_name_collision_deduplicated(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            zip_path = base / "album.zip"
            target = base / "out"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("track.mp3", b"first")
                archive.writestr("track.mp3", b"second")
            result = extract_zip_album(zip_path, target)
            names = [p.name for p in result["extracted_files"]]
            self.assertEqual(names, ["track.mp3", "track-2.mp3"])
            self.assertTrue((target / "track.mp3").is_file())
            self.assertTrue((target / "track-2.mp3").is_file())

    def test_invalid_zip_raises_http_400(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            zip_path = base / "notazip.zip"
            zip_path.write_bytes(b"this is definitely not a zip archive")
            with self.assertRaises(HTTPException) as ctx:
                extract_zip_album(zip_path, base / "out")
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(ctx.exception.detail, "Invalid ZIP archive")

    def test_corrupt_member_raises_http_400(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            zip_path = base / "album.zip"
            target = base / "out"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr("track.mp3", b"x" * 100)
            _corrupt_zip_data(zip_path, "track.mp3")
            with self.assertRaises(HTTPException) as ctx:
                extract_zip_album(zip_path, target)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(ctx.exception.detail, "Invalid ZIP archive")


class WrapperParityTests(unittest.TestCase):
    def test_dedupe_archive_name_parity(self):
        used_w: set[str] = set()
        used_m: set[str] = set()
        for name in ("track.mp3", "track.mp3", "cover.jpg", "", None):
            self.assertEqual(
                main._dedupe_archive_name(name, used_m),
                dedupe_archive_name(name, used_w),
                f"mismatch for {name!r}",
            )
        self.assertEqual(used_w, used_m)

    def test_is_safe_relative_zip_path_parity(self):
        cases = [
            "album/track.mp3",
            "album\\track.mp3",
            "",
            "///",
            "../evil.mp3",
            "a/../../evil.mp3",
            "/etc/passwd",
            "C:/evil.mp3",
            "__MACOSX/x",
            ".DS_Store",
            "album/.ds_store",
            "thumbs.db",
        ]
        for case in cases:
            self.assertEqual(
                main._is_safe_relative_zip_path(case),
                is_safe_relative_zip_path(case),
                f"mismatch for {case!r}",
            )


if __name__ == "__main__":
    unittest.main()
