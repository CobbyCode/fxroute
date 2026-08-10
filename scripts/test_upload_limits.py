#!/usr/bin/env python3
"""Upload size-limit tests for the shared bounded upload helpers and the
EasyEffects upload endpoints.

No network or real hardware is touched; uploads are fake in-memory objects
and the EasyEffects manager is faked.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

import main
import uploads


class FakeUpload:
    """Chunked in-memory upload with read-size and call logging."""

    def __init__(self, data: bytes, *, filename="upload.bin", content_length=None):
        self.data = data
        self.filename = filename
        self.content_length = content_length
        self._pos = 0
        self.read_sizes = []
        self.cancelled_on_first_read = False

    async def read(self, size: int = -1):
        if self.cancelled_on_first_read:
            raise asyncio.CancelledError()
        self.read_sizes.append(size)
        if size is None or size < 0:
            raise AssertionError("uploads must never be read in one unbounded chunk")
        chunk = self.data[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk


class ReadUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_under_limit_succeeds(self):
        upload = FakeUpload(b"x" * 1024)
        data = await uploads.read_upload(upload, 2048)
        self.assertEqual(data, b"x" * 1024)
        self.assertTrue(all(size > 0 for size in upload.read_sizes))

    async def test_exactly_at_limit_succeeds(self):
        upload = FakeUpload(b"x" * 2048)
        data = await uploads.read_upload(upload, 2048)
        self.assertEqual(data, b"x" * 2048)

    async def test_one_byte_over_limit_rejected(self):
        upload = FakeUpload(b"x" * 2049)
        with self.assertRaises(uploads.UploadTooLargeError):
            await uploads.read_upload(upload, 2048)

    async def test_content_length_fast_reject_without_reading(self):
        upload = FakeUpload(b"", content_length=2049)
        with self.assertRaises(uploads.UploadTooLargeError):
            await uploads.read_upload(upload, 2048)
        self.assertEqual(upload.read_sizes, [])

    async def test_missing_content_length_cannot_bypass_counted_read(self):
        upload = FakeUpload(b"x" * 2049, content_length=None)
        with self.assertRaises(uploads.UploadTooLargeError):
            await uploads.read_upload(upload, 2048)


class SaveUploadToFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_under_limit_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.bin"
            upload = FakeUpload(b"x" * 1024)
            written = await uploads.save_upload_to_file(upload, dest, 2048)
            self.assertEqual(written, 1024)
            self.assertEqual(dest.read_bytes(), b"x" * 1024)
            self.assertTrue(all(size > 0 for size in upload.read_sizes))

    async def test_exactly_at_limit_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.bin"
            upload = FakeUpload(b"x" * 2048)
            written = await uploads.save_upload_to_file(upload, dest, 2048)
            self.assertEqual(written, 2048)

    async def test_one_byte_over_limit_rejected_mid_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.bin"
            upload = FakeUpload(b"x" * 2049)
            with self.assertRaises(uploads.UploadTooLargeError):
                await uploads.save_upload_to_file(upload, dest, 2048)
            self.assertLess(dest.stat().st_size, 2049)

    async def test_content_length_fast_reject_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.bin"
            upload = FakeUpload(b"", content_length=2049)
            with self.assertRaises(uploads.UploadTooLargeError):
                await uploads.save_upload_to_file(upload, dest, 2048)
            self.assertEqual(upload.read_sizes, [])


class FakeEEManager:
    def load_global_extras(self):
        return {}

    def normalize_effects_extras(self, extras=None):
        return extras or {}


class EasyEffectsEndpointLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_manager = main.easyeffects_manager
        main.easyeffects_manager = FakeEEManager()

    async def asyncTearDown(self):
        main.easyeffects_manager = self.original_manager
        self.temp_dir.cleanup()

    def _recording_tempfile(self, created):
        real = tempfile.NamedTemporaryFile

        def factory(*args, **kwargs):
            handle = real(*args, **kwargs)
            created.append(handle.name)
            return handle

        return patch("main.tempfile.NamedTemporaryFile", factory)

    async def test_ir_upload_oversized_by_content_length_returns_413(self):
        created = []
        upload = FakeUpload(
            b"",
            filename="ir.wav",
            content_length=uploads.EASYEEFFECTS_IR_MAX_BYTES + 1,
        )
        with self._recording_tempfile(created):
            with self.assertRaises(HTTPException) as ctx:
                await main.upload_easyeffects_ir(upload)
        self.assertEqual(ctx.exception.status_code, 413)
        self.assertEqual(upload.read_sizes, [])
        self.assertTrue(all(not Path(path).exists() for path in created))

    async def test_ir_upload_oversized_by_counted_read_returns_413(self):
        created = []
        upload = FakeUpload(
            b"x" * (uploads.EASYEEFFECTS_IR_MAX_BYTES + 1),
            filename="ir.irs",
            content_length=None,
        )
        with self._recording_tempfile(created):
            with self.assertRaises(HTTPException) as ctx:
                await main.upload_easyeffects_ir(upload)
        self.assertEqual(ctx.exception.status_code, 413)
        self.assertTrue(all(size > 0 for size in upload.read_sizes))
        self.assertTrue(all(not Path(path).exists() for path in created))

    async def test_dual_upload_oversized_first_file_cleans_up_temps(self):
        created = []
        left = FakeUpload(
            b"x" * (uploads.EASYEEFFECTS_IR_MAX_BYTES + 1),
            filename="left.irs",
            content_length=None,
        )
        right = FakeUpload(b"small", filename="right.irs")
        with self._recording_tempfile(created):
            with self.assertRaises(HTTPException) as ctx:
                await main.import_dual_filter_preset(
                    preset_name="Dual",
                    left_file=left,
                    right_file=right,
                )
        self.assertEqual(ctx.exception.status_code, 413)
        self.assertTrue(created, "the first temp file was created before rejection")
        self.assertTrue(all(not Path(path).exists() for path in created))

    async def test_dual_upload_oversized_content_length_rejects_before_writing(self):
        created = []
        left = FakeUpload(
            b"",
            filename="left.irs",
            content_length=uploads.EASYEEFFECTS_IR_MAX_BYTES + 1,
        )
        right = FakeUpload(b"small", filename="right.irs")
        with self._recording_tempfile(created):
            with self.assertRaises(HTTPException) as ctx:
                await main.import_dual_filter_preset(
                    preset_name="Dual",
                    left_file=left,
                    right_file=right,
                )
        self.assertEqual(ctx.exception.status_code, 413)
        self.assertEqual(left.read_sizes, [])
        self.assertEqual(len(created), 1, "the empty temp file is created before the fast reject")
        self.assertTrue(all(not Path(path).exists() for path in created))

    async def test_dual_upload_cancellation_cleans_up_temps(self):
        created = []
        left = FakeUpload(b"x" * 1024, filename="left.irs")
        left.cancelled_on_first_read = True
        right = FakeUpload(b"small", filename="right.irs")
        with self._recording_tempfile(created):
            with self.assertRaises(asyncio.CancelledError):
                await main.import_dual_filter_preset(
                    preset_name="Dual",
                    left_file=left,
                    right_file=right,
                )
        self.assertTrue(all(not Path(path).exists() for path in created))

    async def test_dual_upload_second_file_oversize_cleans_up_first_temp(self):
        created = []
        left = FakeUpload(b"small", filename="left.irs")
        right = FakeUpload(
            b"y" * (uploads.EASYEEFFECTS_IR_MAX_BYTES + 1),
            filename="right.irs",
            content_length=None,
        )
        with self._recording_tempfile(created):
            with self.assertRaises(HTTPException) as ctx:
                await main.import_dual_filter_preset(
                    preset_name="Dual",
                    left_file=left,
                    right_file=right,
                )
        self.assertEqual(ctx.exception.status_code, 413)
        self.assertEqual(len(created), 2, "both temp files were created before the second failed")
        self.assertTrue(all(not Path(path).exists() for path in created))

    async def test_preset_json_upload_oversized_returns_413(self):
        upload = FakeUpload(
            b'{"x": 1}' + b" " * uploads.EASYEEFFECTS_PRESET_TEXT_MAX_BYTES,
            filename="preset.json",
            content_length=None,
        )
        with self.assertRaises(HTTPException) as ctx:
            await main.import_easyeffects_preset_json(upload)
        self.assertEqual(ctx.exception.status_code, 413)

    async def test_rew_peq_upload_oversized_returns_413(self):
        upload = FakeUpload(
            b"1;2;3;" * (uploads.EASYEEFFECTS_PRESET_TEXT_MAX_BYTES // 6 + 2),
            filename="rew.txt",
            content_length=None,
        )
        with self.assertRaises(HTTPException) as ctx:
            await main.import_rew_peq_preset(preset_name="Rew", file=upload)
        self.assertEqual(ctx.exception.status_code, 413)

    async def test_dual_rew_text_oversized_returns_413(self):
        left = FakeUpload(
            b"1;2;3;" * (uploads.EASYEEFFECTS_PRESET_TEXT_MAX_BYTES // 6 + 2),
            filename="left.txt",
            content_length=None,
        )
        right = FakeUpload(b"1;2;3", filename="right.txt")
        with self.assertRaises(HTTPException) as ctx:
            await main.import_dual_filter_preset(
                preset_name="Dual",
                left_file=left,
                right_file=right,
            )
        self.assertEqual(ctx.exception.status_code, 413)


if __name__ == "__main__":
    unittest.main()
