#!/usr/bin/env python3
"""Verhaltenstests für die REFACTOR-007-Extraktion:

- library.path_within_root
- library.is_removable_artwork_file
- library.is_removable_metadata_sidecar
- library.is_cleanup_only_file
- library.folder_has_audio_files
- library.cleanup_track_parent_folder

sowie Wrapper-Parität gegen main._path_within_root.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
from library import (
    cleanup_track_parent_folder,
    folder_has_audio_files,
    is_cleanup_only_file,
    is_removable_artwork_file,
    is_removable_metadata_sidecar,
    path_within_root,
)


def _make_folder(tmp: Path, name: str, files: dict[str, bytes] | None = None) -> Path:
    folder = tmp / name
    for filename, content in (files or {}).items():
        target = folder / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


class PathWithinRootTests(unittest.TestCase):
    def test_same_path(self):
        root = Path("/music")
        self.assertTrue(path_within_root(root, root))

    def test_subpath(self):
        root = Path("/music")
        self.assertTrue(path_within_root(root / "artist" / "album", root))

    def test_foreign_path(self):
        root = Path("/music")
        self.assertFalse(path_within_root(Path("/other/file.mp3"), root))
        self.assertFalse(path_within_root(Path("/musicary"), root))

    def test_resolve_error_returns_false(self):
        # Resolve-Exception -> Funktion muss False liefern (deterministisch via Mock)
        from unittest.mock import patch

        root = Path("/music")
        with patch.object(Path, "resolve", side_effect=OSError("boom")):
            self.assertFalse(path_within_root(root / "album", root))
            self.assertFalse(path_within_root(root, root))


class RemovableArtworkTests(unittest.TestCase):
    def test_cover_jpg_removable(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            folder = _make_folder(Path(td), "album", {"cover.jpg": b"x"})
            self.assertTrue(is_removable_artwork_file(folder / "cover.jpg"))

    def test_folder_stem_match_removable(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            folder = _make_folder(Path(td), "album", {"album.jpg": b"x"})
            self.assertTrue(is_removable_artwork_file(folder / "album.jpg"))

    def test_albumart_prefix_removable(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            folder = _make_folder(Path(td), "album", {"albumart1.png": b"x"})
            self.assertTrue(is_removable_artwork_file(folder / "albumart1.png"))

    def test_non_artwork_not_removable(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            folder = _make_folder(Path(td), "album", {"notes.jpg": b"x"})
            self.assertFalse(is_removable_artwork_file(folder / "notes.jpg"))

    def test_audio_file_not_artwork(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            folder = _make_folder(Path(td), "album", {"track.mp3": b"x"})
            self.assertFalse(is_removable_artwork_file(folder / "track.mp3"))


class RemovableSidecarTests(unittest.TestCase):
    def test_sidecar_suffixes_removable(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            folder = _make_folder(Path(td), "album", {"album.cue": b"", "album.log": b"", "album.nfo": b"", "album.txt": b"", "album.m3u8": b""})
            for suffix in (".cue", ".log", ".nfo", ".txt", ".m3u8"):
                self.assertTrue(is_removable_metadata_sidecar(folder / f"album{suffix}"), suffix)

    def test_sidecar_size_independent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            folder = _make_folder(Path(td), "album", {"big.txt": b"x" * 100000})
            self.assertTrue(is_removable_metadata_sidecar(folder / "big.txt"))

    def test_audio_not_sidecar(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            folder = _make_folder(Path(td), "album", {"track.flac": b"x"})
            self.assertFalse(is_removable_metadata_sidecar(folder / "track.flac"))


class CleanupOnlyFileTests(unittest.TestCase):
    def test_artwork_suffix_counts(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            folder = _make_folder(Path(td), "album", {"random.jpg": b"x"})
            # beliebige Datei mit Artwork-Endung zählt für cleanup_only_folder
            self.assertTrue(is_cleanup_only_file(folder / "random.jpg"))

    def test_sidecar_counts(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            folder = _make_folder(Path(td), "album", {"x.txt": b"x"})
            self.assertTrue(is_cleanup_only_file(folder / "x.txt"))

    def test_audio_not_cleanup_only(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            folder = _make_folder(Path(td), "album", {"track.wav": b"x"})
            self.assertFalse(is_cleanup_only_file(folder / "track.wav"))

    def test_missing_file_false(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            self.assertFalse(is_cleanup_only_file(base / "nope.jpg"))


class FolderHasAudioFilesTests(unittest.TestCase):
    def test_audio_file_detected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            folder = _make_folder(Path(td), "album", {"track.mp3": b"x"})
            self.assertTrue(folder_has_audio_files(folder))

    def test_artwork_only_no_audio(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            folder = _make_folder(Path(td), "album", {"cover.jpg": b"x"})
            self.assertFalse(folder_has_audio_files(folder))

    def test_nested_audio_not_counted(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            folder = _make_folder(Path(td), "album", {"sub/track.flac": b"x"})
            self.assertFalse(folder_has_audio_files(folder))

    def test_missing_folder_false(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(folder_has_audio_files(Path(td) / "nope"))


class CleanupTrackParentFolderTests(unittest.TestCase):
    def test_folder_with_audio_untouched(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            music = _make_folder(base, "music", {"album/track.flac": b"x"})
            folder = music / "album"
            result = cleanup_track_parent_folder(folder, music)
            self.assertEqual(result["removed_files"], [])
            self.assertFalse(result["removed_folder"])
            self.assertEqual(result["kept"], [])
            self.assertTrue((folder / "track.flac").is_file())
            self.assertEqual(
                result,
                {"folder": str(folder), "removed_files": [], "removed_folder": False, "kept": []},
            )

    def test_artwork_only_folder_removed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            music = _make_folder(base, "music", {"album/cover.jpg": b"x", "album/folder.png": b"y"})
            folder = music / "album"
            expected_order = [str(child) for child in folder.iterdir() if child.is_file()]
            result = cleanup_track_parent_folder(folder, music)
            self.assertEqual(result["removed_files"], expected_order)
            self.assertTrue(result["removed_folder"])
            self.assertEqual(result["kept"], [])
            self.assertFalse(folder.exists())

    def test_mixed_folder_keeps_non_removable(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            music = _make_folder(base, "music", {"album/cover.jpg": b"x", "album/scan.pdf": b"y"})
            folder = music / "album"
            result = cleanup_track_parent_folder(folder, music)
            self.assertEqual(result["removed_files"], [str(folder / "cover.jpg")])
            self.assertFalse(result["removed_folder"])
            self.assertEqual(result["kept"], [])
            self.assertTrue((folder / "scan.pdf").is_file())
            self.assertFalse((folder / "cover.jpg").exists())

    def test_sidecar_folder_removed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            music = _make_folder(base, "music", {"album/big.txt": b"x" * 50000})
            folder = music / "album"
            result = cleanup_track_parent_folder(folder, music)
            self.assertEqual(result["removed_files"], [str(folder / "big.txt")])
            self.assertTrue(result["removed_folder"])
            self.assertFalse(folder.exists())

    def test_music_root_untouched(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            music = _make_folder(base, "music", {"cover.jpg": b"x"})
            result = cleanup_track_parent_folder(music, music)
            self.assertEqual(result["removed_files"], [])
            self.assertFalse(result["removed_folder"])
            self.assertTrue((music / "cover.jpg").is_file())

    def test_protected_folder_untouched(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            music = _make_folder(base, "music", {"album/cover.jpg": b"x"})
            folder = music / "album"
            protected = {folder}
            result = cleanup_track_parent_folder(folder, music, protected_folders=protected)
            self.assertEqual(result["removed_files"], [])
            self.assertFalse(result["removed_folder"])
            self.assertTrue((folder / "cover.jpg").is_file())

    def test_protected_set_not_mutated(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            music = _make_folder(base, "music", {"album/cover.jpg": b"x"})
            folder = music / "album"
            protected = {folder}
            before = set(protected)
            cleanup_track_parent_folder(folder, music, protected_folders=protected)
            self.assertEqual(protected, before)

    def test_missing_folder_empty_result(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            music = _make_folder(base, "music")
            result = cleanup_track_parent_folder(music / "nope", music)
            self.assertEqual(result["removed_files"], [])
            self.assertFalse(result["removed_folder"])

    def test_cleanup_only_any_artwork_suffix(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            # beliebige .jpg (nicht als Artwork erkannt) + .png: cleanup_only_folder -> alle entfernt
            music = _make_folder(base, "music", {"album/random.jpg": b"x", "album/other.png": b"y"})
            folder = music / "album"
            result = cleanup_track_parent_folder(folder, music)
            self.assertEqual(len(result["removed_files"]), 2)
            self.assertTrue(result["removed_folder"])
            self.assertFalse(folder.exists())


class WrapperParityTests(unittest.TestCase):
    def test_path_within_root_parity(self):
        root = Path("/music")
        cases = [(root, root), (root / "a", root), (Path("/other"), root), (Path("/musicary"), root)]
        for path, base in cases:
            self.assertEqual(
                main._path_within_root(path, base),
                path_within_root(path, base),
                f"mismatch for {path} in {base}",
            )


if __name__ == "__main__":
    unittest.main()
