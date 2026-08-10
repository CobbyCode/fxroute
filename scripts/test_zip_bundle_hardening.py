#!/usr/bin/env python3
"""ZIP / preset-bundle hardening tests.

Covers the EasyEffects preset-bundle import endpoint and the library album
ZIP extraction: size limits (upload, member count, total uncompressed,
per-member, read budget), traversal / absolute / drive / UNC paths,
symlink / special / encrypted entries, counted extraction reads, and
temp/partial-file cleanup on every rejection.  All archives are built in
memory; no network access.
"""

import asyncio
import io
import json
import os
import stat
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

import library_api
import main
import zip_album


class FakeUpload:
    def __init__(self, data: bytes, *, filename="bundle.zip", content_length=None,
                 cancel_after=None):
        self.data = data
        self.filename = filename
        self.content_length = content_length
        self._pos = 0
        self._reads = 0
        self._cancel_after = cancel_after

    async def read(self, size: int = -1):
        self._reads += 1
        if self._cancel_after is not None and self._reads > self._cancel_after:
            raise asyncio.CancelledError()
        chunk = self.data[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    async def close(self):
        pass


class FakeEEManager:
    def __init__(self, irs_dir: Path, fail_import=False):
        self.irs_dir = irs_dir
        self.fail_import = fail_import

    def load_global_extras(self):
        return {}

    def normalize_effects_extras(self, extras=None):
        return extras or {}

    def _extract_kernel_names_from_payload(self, payload):
        return set()

    def _find_ir_paths_for_kernel_name(self, name):
        return []

    def import_preset_json(self, filename, text):
        if self.fail_import:
            raise ValueError("preset import rejected")
        return {"name": Path(filename).stem}


def write_zip(entries) -> bytes:
    """entries: list of (name, data) or (name, data, opts)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry in entries:
            name, data = entry[0], entry[1]
            opts = entry[2] if len(entry) > 2 else {}
            if opts.get("symlink"):
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, data)
            elif opts.get("fifo"):
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFIFO | 0o644) << 16
                archive.writestr(info, data)
            else:
                archive.writestr(name, data)
    return buffer.getvalue()


def mark_encrypted(zip_bytes: bytes, member_name: str) -> bytes:
    """Set the encryption flag bit on one member's local and central headers."""
    data = bytearray(zip_bytes)
    for sig, flag_offset, name_offset, name_len_offset in (
        (b"PK\x03\x04", 6, 30, 26),
        (b"PK\x01\x02", 8, 46, 28),
    ):
        index = 0
        while True:
            index = data.find(sig, index)
            if index < 0:
                break
            name_len = struct.unpack_from("<H", data, index + name_len_offset)[0]
            name_start = index + name_offset
            name = bytes(data[name_start:name_start + name_len]).decode("utf-8", errors="replace")
            if name == member_name:
                flags = struct.unpack_from("<H", data, index + flag_offset)[0]
                struct.pack_into("<H", data, index + flag_offset, flags | 0x1)
            index += 1
    return bytes(data)


def shrunken_local_header_size(zip_bytes: bytes, member_name: str) -> bytes:
    """Patch a member's declared uncompressed size in both headers.

    The central directory is authoritative for zipfile reads, so both the
    local header and the central directory entry are patched to a smaller
    value; the CRC still covers the real data, so extraction must fail.
    """
    data = bytearray(zip_bytes)
    for sig, size_offset, name_offset, name_len_offset in (
        (b"PK\x03\x04", 18, 30, 26),
        (b"PK\x01\x02", 24, 46, 28),
    ):
        index = 0
        while True:
            index = data.find(sig, index)
            if index < 0:
                raise AssertionError(f"header {sig!r} not found")
            name_len = struct.unpack_from("<H", data, index + name_len_offset)[0]
            name_start = index + name_offset
            name = bytes(data[name_start:name_start + name_len]).decode("utf-8", errors="replace")
            if name == member_name:
                struct.pack_into("<I", data, index + size_offset, 8)
                break
            index += 1
    return bytes(data)


def standard_bundle_bytes() -> bytes:
    return write_zip([
        ("manifest.json", json.dumps({"type": "fxroute-preset-bundle", "version": 1})),
        ("preset.json", '{"output": "test"}'),
        ("ir-left.irs", b"IRDATA" * 16),
    ])


class PresetBundleEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.irs_dir = Path(self.temp_dir.name) / "irs"
        self.original_manager = main.easyeffects_manager
        self.original_finish = main._finish_easyeffects_preset_mutation
        self.original_limits = {
            name: getattr(main, name)
            for name in (
                "PRESET_BUNDLE_MAX_MEMBERS",
                "PRESET_BUNDLE_MAX_TOTAL_UNCOMPRESSED_BYTES",
                "PRESET_BUNDLE_MAX_MEMBER_BYTES",
                "PRESET_BUNDLE_MAX_JSON_BYTES",
                "PRESET_BUNDLE_MAX_CENTRAL_DIRECTORY_BYTES",
            )
        }
        main.easyeffects_manager = FakeEEManager(self.irs_dir)
        main._finish_easyeffects_preset_mutation = self._fake_finish
        self.created_temps = []
        real_tempfile = tempfile.NamedTemporaryFile

        def recording_tempfile(*args, **kwargs):
            handle = real_tempfile(*args, **kwargs)
            self.created_temps.append(handle.name)
            return handle

        self.tempfile_patch = patch("main.tempfile.NamedTemporaryFile", recording_tempfile)
        self.tempfile_patch.start()

    async def _fake_finish(self, **kwargs):
        return {"active_preset": None}

    async def asyncTearDown(self):
        self.tempfile_patch.stop()
        main.easyeffects_manager = self.original_manager
        main._finish_easyeffects_preset_mutation = self.original_finish
        for name, value in self.original_limits.items():
            setattr(main, name, value)
        self.temp_dir.cleanup()

    def _assert_clean(self):
        self.assertTrue(all(not Path(path).exists() for path in self.created_temps))
        if self.irs_dir.exists():
            self.assertEqual(list(self.irs_dir.iterdir()), [])

    def _assert_no_transaction_leftovers(self):
        leftovers = [
            path.name for path in self.irs_dir.iterdir()
            if path.name.startswith(".fxroute-")
        ]
        self.assertEqual(leftovers, [])

    async def _import(self, zip_bytes: bytes, *, filename="bundle.zip"):
        return await main.import_easyeffects_preset_bundle(
            FakeUpload(zip_bytes, filename=filename)
        )

    def _bundle_with_ir(self, ir_name: str, ir_data: bytes) -> bytes:
        return write_zip([
            ("manifest.json", json.dumps({"type": "fxroute-preset-bundle", "version": 1})),
            ("preset.json", '{"output": "test"}'),
            (ir_name, ir_data),
        ])

    async def test_normal_bundle_imports_successfully(self):
        result = await self._import(standard_bundle_bytes())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["irs"], ["ir-left.irs"])
        self.assertEqual((self.irs_dir / "ir-left.irs").read_bytes(), b"IRDATA" * 16)
        self.assertTrue(all(not Path(path).exists() for path in self.created_temps))

    async def test_traversal_member_rejected(self):
        evil = write_zip([("preset.json", "{}"), ("../evil.irs", b"X" * 8)])
        with self.assertRaises(HTTPException) as ctx:
            await self._import(evil)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Unsafe ZIP member path", str(ctx.exception.detail))
        self._assert_clean()

    async def test_absolute_path_member_rejected(self):
        evil = write_zip([("preset.json", "{}"), ("/etc/evil.irs", b"X" * 8)])
        with self.assertRaises(HTTPException) as ctx:
            await self._import(evil)
        self.assertEqual(ctx.exception.status_code, 400)
        self._assert_clean()

    async def test_windows_drive_path_member_rejected(self):
        evil = write_zip([("preset.json", "{}"), ("C:/evil.irs", b"X" * 8)])
        with self.assertRaises(HTTPException) as ctx:
            await self._import(evil)
        self.assertEqual(ctx.exception.status_code, 400)
        self._assert_clean()

    async def test_unc_path_member_rejected(self):
        evil = write_zip([("preset.json", "{}"), (r"\\server\share\evil.irs", b"X" * 8)])
        with self.assertRaises(HTTPException) as ctx:
            await self._import(evil)
        self.assertEqual(ctx.exception.status_code, 400)
        self._assert_clean()

    async def test_symlink_member_rejected(self):
        evil = write_zip([
            ("preset.json", "{}"),
            ("link.irs", "/etc/passwd", {"symlink": True}),
        ])
        with self.assertRaises(HTTPException) as ctx:
            await self._import(evil)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("symbolic-link", str(ctx.exception.detail))
        self._assert_clean()

    async def test_fifo_member_rejected(self):
        evil = write_zip([
            ("preset.json", "{}"),
            ("fifo.irs", b"", {"fifo": True}),
        ])
        with self.assertRaises(HTTPException) as ctx:
            await self._import(evil)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("special-file", str(ctx.exception.detail))
        self._assert_clean()

    async def test_encrypted_member_rejected(self):
        evil = mark_encrypted(
            write_zip([("preset.json", "{}"), ("secret.irs", b"X" * 8)]),
            "secret.irs",
        )
        with self.assertRaises(HTTPException) as ctx:
            await self._import(evil)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("encrypted", str(ctx.exception.detail))
        self._assert_clean()

    async def test_too_many_members_rejected(self):
        main.PRESET_BUNDLE_MAX_MEMBERS = 4
        entries = [("preset.json", "{}")]
        entries += [(f"dummy-{index}.bin", b"x") for index in range(5)]
        with self.assertRaises(HTTPException) as ctx:
            await self._import(write_zip(entries))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("too many", str(ctx.exception.detail))
        self._assert_clean()

    async def test_total_uncompressed_limit_rejected(self):
        main.PRESET_BUNDLE_MAX_TOTAL_UNCOMPRESSED_BYTES = 64
        with self.assertRaises(HTTPException) as ctx:
            await self._import(standard_bundle_bytes())
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("uncompressed", str(ctx.exception.detail))
        self._assert_clean()

    async def test_single_member_too_large_rejected(self):
        main.PRESET_BUNDLE_MAX_MEMBER_BYTES = 64
        evil = write_zip([("preset.json", "{}"), ("big.irs", b"X" * 128)])
        with self.assertRaises(HTTPException) as ctx:
            await self._import(evil)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("too large", str(ctx.exception.detail))
        self._assert_clean()

    async def test_preset_json_read_budget_enforced(self):
        main.PRESET_BUNDLE_MAX_JSON_BYTES = 8
        evil = write_zip([("preset.json", '{"output": "' + "x" * 64 + '"}')])
        with self.assertRaises(HTTPException) as ctx:
            await self._import(evil)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("read budget", str(ctx.exception.detail))
        self._assert_clean()

    async def test_lying_size_member_rejected_without_leftovers(self):
        lying = shrunken_local_header_size(write_zip([
            ("preset.json", '{"output": "test"}'),
            ("ir.irs", b"A" * 4096),
        ]), "ir.irs")
        with self.assertRaises(HTTPException) as ctx:
            await self._import(lying)
        self.assertEqual(ctx.exception.status_code, 400)
        self._assert_clean()

    async def test_oversized_upload_rejected_413(self):
        upload = FakeUpload(
            b"x" * 1024,
            filename="bundle.zip",
            content_length=main.EASYEEFFECTS_BUNDLE_MAX_BYTES + 1,
        )
        with self.assertRaises(HTTPException) as ctx:
            await main.import_easyeffects_preset_bundle(upload)
        self.assertEqual(ctx.exception.status_code, 413)
        self._assert_clean()

    async def test_missing_preset_json_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._import(write_zip([("readme.txt", "hello")]))
        self.assertEqual(ctx.exception.status_code, 400)
        self._assert_clean()

    async def test_existing_ir_restored_when_import_fails(self):
        self.irs_dir.mkdir()
        (self.irs_dir / "same.irs").write_bytes(b"OLD" * 8)
        main.easyeffects_manager = FakeEEManager(self.irs_dir, fail_import=True)
        with self.assertRaises(HTTPException) as ctx:
            await self._import(self._bundle_with_ir("same.irs", b"NEW" * 16))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual((self.irs_dir / "same.irs").read_bytes(), b"OLD" * 8)
        self._assert_no_transaction_leftovers()
        self.assertTrue(all(not Path(path).exists() for path in self.created_temps))

    async def test_new_ir_removed_when_import_fails(self):
        self.irs_dir.mkdir()
        main.easyeffects_manager = FakeEEManager(self.irs_dir, fail_import=True)
        with self.assertRaises(HTTPException) as ctx:
            await self._import(self._bundle_with_ir("new.irs", b"NEW" * 16))
        self.assertEqual(ctx.exception.status_code, 400)
        self._assert_clean()

    async def test_destination_symlink_rejected(self):
        self.irs_dir.mkdir()
        external = Path(self.temp_dir.name) / "external-target.bin"
        external.write_bytes(b"EXTERNAL")
        (self.irs_dir / "same.irs").symlink_to(external)
        with self.assertRaises(HTTPException) as ctx:
            await self._import(self._bundle_with_ir("same.irs", b"NEW" * 16))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("symlink", str(ctx.exception.detail))
        self.assertEqual(external.read_bytes(), b"EXTERNAL")
        self.assertTrue((self.irs_dir / "same.irs").is_symlink())
        self._assert_no_transaction_leftovers()
        self.assertTrue(all(not Path(path).exists() for path in self.created_temps))

    async def test_existing_ir_overwritten_on_success(self):
        self.irs_dir.mkdir()
        (self.irs_dir / "same.irs").write_bytes(b"OLD" * 8)
        result = await self._import(self._bundle_with_ir("same.irs", b"NEW" * 16))
        self.assertEqual(result["status"], "ok")
        self.assertEqual((self.irs_dir / "same.irs").read_bytes(), b"NEW" * 16)
        self._assert_no_transaction_leftovers()
        self.assertTrue(all(not Path(path).exists() for path in self.created_temps))


class ZipAlbumExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.zip_dir = Path(self.temp_dir.name)
        self.original_limits = {
            "ALBUM_ZIP_MAX_MEMBERS": zip_album.ALBUM_ZIP_MAX_MEMBERS,
            "ALBUM_ZIP_MAX_TOTAL_UNCOMPRESSED_BYTES": zip_album.ALBUM_ZIP_MAX_TOTAL_UNCOMPRESSED_BYTES,
            "ALBUM_ZIP_MAX_MEMBER_BYTES": zip_album.ALBUM_ZIP_MAX_MEMBER_BYTES,
            "ALBUM_ZIP_MAX_CENTRAL_DIRECTORY_BYTES": zip_album.ALBUM_ZIP_MAX_CENTRAL_DIRECTORY_BYTES,
        }

    def tearDown(self):
        for name, value in self.original_limits.items():
            setattr(zip_album, name, value)
        self.temp_dir.cleanup()

    def _extract(self, zip_bytes, name="album.zip"):
        zip_path = self.zip_dir / name
        zip_path.write_bytes(zip_bytes)
        target = self.zip_dir / "out"
        target.mkdir(exist_ok=True)
        return zip_path, target

    def test_normal_album_zip_extracts(self):
        zip_path, target = self._extract(write_zip([
            ("track1.flac", b"FLAC" * 100),
            ("track2.mp3", b"MP3" * 100),
            ("playlist.m3u", "#EXTM3U\ntrack1.flac"),
            ("cover.jpg", b"JPG"),
        ], ))
        result = zip_album.extract_zip_album(zip_path, target)
        self.assertEqual(len(result["audio_files"]), 2)
        self.assertEqual(len(result["playlist_files"]), 1)
        self.assertEqual(result["skipped_entries"], [])
        self.assertTrue((target / "track1.flac").is_file())

    def test_unsafe_members_are_skipped(self):
        zip_path, target = self._extract(write_zip([
            ("track1.flac", b"FLAC"),
            ("../evil.flac", b"EVIL"),
            ("/abs.mp3", b"ABS"),
            ("C:/drive.mp3", b"DRIVE"),
            (r"\\server\share\x.flac", b"UNC"),
            ("link.flac", "/etc/passwd", {"symlink": True}),
        ]))
        result = zip_album.extract_zip_album(zip_path, target)
        self.assertEqual(len(result["audio_files"]), 1)
        self.assertEqual(len(result["skipped_entries"]), 5)
        self.assertFalse((target.parent / "evil.flac").exists())
        self.assertEqual(list(target.iterdir()), [target / "track1.flac"])

    def test_member_count_limit_rejected(self):
        zip_album.ALBUM_ZIP_MAX_MEMBERS = 3
        zip_path, target = self._extract(write_zip([
            ("a.flac", b"A"), ("b.flac", b"B"), ("c.flac", b"C"), ("d.flac", b"D"),
        ]))
        with self.assertRaises(zip_album.ZipLimitError):
            zip_album.extract_zip_album(zip_path, target)

    def test_total_uncompressed_limit_rejected(self):
        zip_album.ALBUM_ZIP_MAX_TOTAL_UNCOMPRESSED_BYTES = 64
        zip_path, target = self._extract(write_zip([("big.flac", b"X" * 128)]))
        with self.assertRaises(zip_album.ZipLimitError):
            zip_album.extract_zip_album(zip_path, target)

    def test_single_member_limit_rejected(self):
        zip_album.ALBUM_ZIP_MAX_MEMBER_BYTES = 32
        zip_path, target = self._extract(write_zip([("big.flac", b"X" * 64)]))
        with self.assertRaises(zip_album.ZipLimitError):
            zip_album.extract_zip_album(zip_path, target)

    def test_encrypted_member_is_skipped(self):
        zip_path, target = self._extract(mark_encrypted(write_zip([
            ("track1.flac", b"FLAC"),
            ("secret.flac", b"X" * 8),
        ]), "secret.flac"))
        result = zip_album.extract_zip_album(zip_path, target)
        self.assertEqual(len(result["audio_files"]), 1)
        self.assertEqual(result["skipped_entries"], ["secret.flac"])

    def test_copy_member_bounded_removes_partial_destination(self):
        zip_path = self.zip_dir / "bounded.zip"
        zip_path.write_bytes(write_zip([("big.irs", b"X" * 4096)]))
        target = self.zip_dir / "out.bin"
        with zipfile.ZipFile(zip_path) as archive:
            member = archive.infolist()[0]
            with self.assertRaises(zip_album.ZipLimitError):
                zip_album.copy_member_bounded(archive, member, target, remaining_bytes=64)
        self.assertFalse(target.exists())

    def test_copy_member_bounded_enforces_member_and_total_budgets(self):
        # Callers hand over min(remaining_total, max_member_bytes).  Any
        # budget below the declared member size must abort the actual
        # counted read and remove the partial destination; a budget at or
        # above the declared size extracts the member fully.
        zip_path = self.zip_dir / "budgets.zip"
        zip_path.write_bytes(write_zip([("big.irs", b"X" * 4096)]))
        target = self.zip_dir / "out.bin"
        with zipfile.ZipFile(zip_path) as archive:
            member = archive.infolist()[0]
            for budget in (64, 1000):
                with self.assertRaises(zip_album.ZipLimitError):
                    zip_album.copy_member_bounded(archive, member, target, remaining_bytes=budget)
                self.assertFalse(target.exists())
            written = zip_album.copy_member_bounded(archive, member, target, remaining_bytes=4096)
            self.assertEqual(written, 4096)
            self.assertEqual(target.stat().st_size, 4096)
        self.assertEqual(target.read_bytes(), b"X" * 4096)

    def test_album_extraction_enforces_member_budget(self):
        zip_path, target = self._extract(write_zip([("big.flac", b"X" * 4096)]))
        with self.assertRaises(zip_album.ZipLimitError):
            zip_album.extract_zip_album(
                zip_path,
                target,
                max_members=8,
                max_total_uncompressed_bytes=1024 * 1024,
                max_member_bytes=64,
            )
        self.assertEqual(list(target.iterdir()), [])


class LibraryUploadZipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.download_dir = Path(self.temp_dir.name) / "downloads"
        self.download_dir.mkdir()
        self.original_runtime = library_api._library_runtime
        self.original_limits = {
            "ALBUM_ZIP_MAX_MEMBERS": zip_album.ALBUM_ZIP_MAX_MEMBERS,
            "ALBUM_ZIP_MAX_CENTRAL_DIRECTORY_BYTES": zip_album.ALBUM_ZIP_MAX_CENTRAL_DIRECTORY_BYTES,
        }

        class FakeSettings:
            def __init__(self, download_dir, music_root):
                self.download_dir = download_dir
                self.MUSIC_ROOT = music_root

        class FakeScanner:
            scanning = False

            def prepare_scan_status(self):
                pass

            def status(self):
                return {}

        music_root = Path(self.temp_dir.name) / "music"
        library_api._library_runtime = lambda: (
            FakeScanner(),
            FakeSettings(self.download_dir, music_root),
        )
        self.created_temps = []
        real_tempfile = tempfile.NamedTemporaryFile

        def recording_tempfile(*args, **kwargs):
            handle = real_tempfile(*args, **kwargs)
            self.created_temps.append(handle.name)
            return handle

        self.tempfile_patch = patch("library_api.tempfile.NamedTemporaryFile", recording_tempfile)
        self.tempfile_patch.start()

    async def asyncTearDown(self):
        self.tempfile_patch.stop()
        library_api._library_runtime = self.original_runtime
        for name, value in self.original_limits.items():
            setattr(zip_album, name, value)
        self.temp_dir.cleanup()

    async def test_too_many_members_zip_rejected_with_full_cleanup(self):
        zip_album.ALBUM_ZIP_MAX_MEMBERS = 3
        entries = [(f"t{index}.flac", b"X") for index in range(5)]
        upload = FakeUpload(write_zip(entries), filename="album.zip")
        with self.assertRaises(HTTPException) as ctx:
            await library_api.upload_track(upload)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(list(self.download_dir.iterdir()), [])
        self.assertTrue(all(not Path(path).exists() for path in self.created_temps))

    async def test_oversized_zip_upload_rejected_with_cleanup(self):
        upload = FakeUpload(
            b"",
            filename="album.zip",
            content_length=library_api.LIBRARY_UPLOAD_MAX_BYTES + 1,
        )
        with self.assertRaises(HTTPException) as ctx:
            await library_api.upload_track(upload)
        self.assertEqual(ctx.exception.status_code, 413)
        self.assertEqual(list(self.download_dir.iterdir()), [])
        self.assertTrue(all(not Path(path).exists() for path in self.created_temps))

    async def test_oversized_audio_upload_rejected_with_cleanup(self):
        upload = FakeUpload(
            b"x" * 1024,
            filename="track.flac",
            content_length=library_api.LIBRARY_UPLOAD_MAX_BYTES + 1,
        )
        with self.assertRaises(HTTPException) as ctx:
            await library_api.upload_track(upload)
        self.assertEqual(ctx.exception.status_code, 413)
        self.assertEqual(list(self.download_dir.iterdir()), [])

    async def test_audio_upload_cancellation_removes_partial_file(self):
        upload = FakeUpload(b"x" * 1024, filename="track.flac", cancel_after=1)
        with self.assertRaises(asyncio.CancelledError):
            await library_api.upload_track(upload)
        self.assertEqual(list(self.download_dir.iterdir()), [])

    async def test_zip_upload_cancellation_removes_staged_files(self):
        upload = FakeUpload(
            b"PK\x05\x06" + b"x" * 1024,
            filename="album.zip",
            cancel_after=1,
        )
        with self.assertRaises(asyncio.CancelledError):
            await library_api.upload_track(upload)
        self.assertEqual(list(self.download_dir.iterdir()), [])
        self.assertTrue(all(not Path(path).exists() for path in self.created_temps))


if __name__ == "__main__":
    unittest.main()
