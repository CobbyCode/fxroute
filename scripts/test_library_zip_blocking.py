#!/usr/bin/env python3
"""ZIP I/O must run on the existing blocking-work path.

Album ZIP extraction (import) and selection ZIP creation (export) are blocking
file/CPU work.  They must be offloaded through the library runtime's
``run_blocking`` runner (``main._drain_worker`` in production) instead of
running inline in the async request handler.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import library.api as library_api
from library.api import LibraryApiRuntime
from models import DownloadTracksRequest


class _Scanner:
    scanning = False

    def __init__(self, music_root: Path, tracks=None):
        self.music_root = music_root
        self._tracks = tracks or []

    def prepare_scan_status(self):
        pass

    def status(self):
        return {}

    def refresh(self, *args, **kwargs):
        return self._tracks

    def get_tracks(self, *args, **kwargs):
        return self._tracks


class _Settings:
    def __init__(self, download_dir: Path, music_root: Path):
        self.download_dir = download_dir
        self.MUSIC_ROOT = music_root


class _Upload:
    def __init__(self, data: bytes, filename: str):
        self.data = data
        self.filename = filename
        self._read = False

    async def read(self, size: int = -1):
        if self._read:
            return b""
        self._read = True
        return self.data

    async def close(self):
        pass


class ZipBlockingPathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.music_root = base / "music"
        self.download_dir = base / "downloads"
        self.music_root.mkdir()
        self.download_dir.mkdir()
        self.calls: list[str] = []

        async def run_blocking(func, *args, **kwargs):
            self.calls.append(getattr(func, "__name__", repr(func)))
            return await asyncio.to_thread(func, *args, **kwargs)

        self.runner = run_blocking
        self.original_runtime = library_api._runtime

    async def asyncTearDown(self):
        library_api._runtime = self.original_runtime
        self.tmp.cleanup()

    def _configure(self, tracks=None):
        scanner = _Scanner(self.music_root, tracks)
        library_api.configure_runtime(LibraryApiRuntime(
            get_scanner=lambda: scanner,
            get_settings=lambda: _Settings(self.download_dir, self.music_root),
            run_blocking=self.runner,
        ))
        return scanner

    async def test_zip_album_import_extraction_is_offloaded(self):
        self._configure()
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("track.flac", b"FLAC" * 64)
            archive.writestr("playlist.m3u8", b"#EXTM3U\ntrack.flac\n")
        upload = _Upload(buffer.getvalue(), "album.zip")

        result = await library_api.upload_track(upload)

        self.assertEqual(result["status"], "imported")
        self.assertIn(
            "extract_zip_album",
            self.calls,
            "ZIP extraction must run through the blocking-work path",
        )

    async def test_track_selection_export_zip_creation_is_offloaded(self):
        first = self.music_root / "first.flac"
        second = self.music_root / "second.flac"
        first.write_bytes(b"A")
        second.write_bytes(b"B")
        tracks = [
            SimpleNamespace(id="t1", path=first),
            SimpleNamespace(id="t2", path=second),
        ]
        self._configure(tracks)

        response = await library_api.download_tracks(
            DownloadTracksRequest(track_ids=["t1", "t2"])
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "_build_tracks_zip",
            self.calls,
            "ZIP export creation must run through the blocking-work path",
        )
        self.assertTrue(await asyncio.to_thread(Path(response.path).is_file))
        await response.background()


if __name__ == "__main__":
    unittest.main()
