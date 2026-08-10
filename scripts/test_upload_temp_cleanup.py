#!/usr/bin/env python3
"""Upload temp files are removed when the upload read/write fails, for
every EasyEffects upload/conversion endpoint that stages a temp file."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class FakeUpload:
    def __init__(self, filename, payload=b"data", fail=False, cancel=False):
        self.filename = filename
        self._payload = payload
        self._fail = fail
        self._cancel = cancel
        self._pos = 0

    async def read(self, size=-1):
        if self._cancel:
            raise asyncio.CancelledError()
        if self._fail:
            raise OSError("client disconnected")
        if size is None or size < 0:
            chunk = self._payload[self._pos:]
        else:
            chunk = self._payload[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk


class DummyManager:
    def __init__(self):
        self.calls = []

    def get_status(self):
        return {}

    def upload_ir(self, *_args, **_kwargs):
        self.calls.append(("upload_ir",))
        return {"name": "ir"}

    def create_convolver_preset_with_upload(self, *_args, **_kwargs):
        self.calls.append(("create_with_upload",))
        return {"preset": {"name": "p"}, "ir": {"name": "ir"}}

    def create_convolver_preset_with_dual_uploads(self, *_args, **_kwargs):
        self.calls.append(("dual",))
        return {"preset": {"name": "p"}, "ir": {"name": "ir"}}


class UploadTempCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.gettempdir = patch("tempfile.gettempdir", return_value=self.tmpdir.name)
        self.gettempdir.start()
        self.manager = DummyManager()
        self.require_manager = patch.object(main, "_require_easyeffects_manager",
                                            return_value=self.manager)
        self.require_manager.start()
        self.addCleanup(self.require_manager.stop)
        self.addCleanup(self.gettempdir.stop)
        self.addCleanup(self.tmpdir.cleanup)

    def _leftovers(self):
        return list(Path(self.tmpdir.name).iterdir())

    async def test_ir_upload_read_failure_leaves_no_temp_file(self):
        upload = FakeUpload("test.ir", fail=True)
        with self.assertRaises(Exception):
            await main.upload_easyeffects_ir(file=upload)
        self.assertEqual(self._leftovers(), [])

    async def test_create_with_ir_read_failure_leaves_no_temp_file(self):
        upload = FakeUpload("test.ir", fail=True)
        with self.assertRaises(Exception):
            await main.create_convolver_preset_with_ir(
                preset_name="p",
                load_after_create=False,
                limiter_enabled=False,
                headroom_enabled=False,
                headroom_gain_db=-3.0,
                autogain_enabled=False,
                autogain_target_db=-12.0,
                delay_enabled=False,
                delay_left_ms=0.0,
                delay_right_ms=0.0,
                bass_enabled=False,
                bass_amount=0.0,
                tone_effect_enabled=False,
                tone_effect_mode="crystalizer",
                file=upload,
            )
        self.assertEqual(self._leftovers(), [])

    async def test_bundle_write_failure_leaves_no_temp_zip(self):
        upload = FakeUpload("bundle.zip", fail=True)
        with self.assertRaises(Exception):
            await main.import_easyeffects_preset_bundle(file=upload)
        self.assertEqual(self._leftovers(), [])

    async def test_dual_import_second_upload_failure_cleans_both(self):
        left = FakeUpload("left.irs", payload=b"left")
        right = FakeUpload("right.irs", fail=True)
        with self.assertRaises(Exception):
            await main.import_dual_filter_preset(
                preset_name="p",
                left_text="", right_text="",
                load_after_create=False,
                limiter_enabled=False, headroom_enabled=False, headroom_gain_db=-3.0,
                autogain_enabled=False, autogain_target_db=-12.0,
                delay_enabled=False, delay_left_ms=0.0, delay_right_ms=0.0,
                bass_enabled=False, bass_amount=0.0,
                tone_effect_enabled=False, tone_effect_mode="",
                left_file=left, right_file=right,
            )
        self.assertEqual(self._leftovers(), [])

    async def test_dual_import_success_cleans_both_temp_files(self):
        left = FakeUpload("left.irs", payload=b"left")
        right = FakeUpload("right.irs", payload=b"right")
        run_locked = patch.object(
            main, "_run_locked_worker",
            return_value={"preset": {"name": "p"}, "ir": {"name": "ir"}},
        )
        finish = patch.object(
            main, "_finish_easyeffects_preset_mutation",
            return_value={"active_preset": "p"},
        )
        with run_locked, finish:
            result = await main.import_dual_filter_preset(
                preset_name="p",
                left_text="", right_text="",
                load_after_create=False,
                limiter_enabled=False, headroom_enabled=False, headroom_gain_db=-3.0,
                autogain_enabled=False, autogain_target_db=-12.0,
                delay_enabled=False, delay_left_ms=0.0, delay_right_ms=0.0,
                bass_enabled=False, bass_amount=0.0,
                tone_effect_enabled=False, tone_effect_mode="",
                left_file=left, right_file=right,
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self._leftovers(), [])

    async def test_unlink_failure_after_successful_upload_keeps_success(self):
        from pathlib import Path as _Path
        original_unlink = _Path.unlink

        def _flaky_unlink(path_self, *args, **kwargs):
            if str(path_self).startswith(self.tmpdir.name):
                raise PermissionError("unlink denied")
            return original_unlink(path_self, *args, **kwargs)

        async def _async_noop(*_args, **_kwargs):
            return None

        upload = FakeUpload("test.ir", payload=b"data")
        run_locked = patch.object(main, "_run_locked_worker", return_value={"name": "ir"})
        broadcast = patch.object(main.manager, "broadcast", new=_async_noop)
        refresh = patch.object(main, "schedule_peak_monitor_refresh_after_effects_change")
        with run_locked, broadcast, refresh, patch.object(_Path, "unlink", _flaky_unlink):
            result = await main.upload_easyeffects_ir(file=upload)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["ir"], {"name": "ir"})

    async def test_unlink_failure_on_error_path_keeps_original_error(self):
        from pathlib import Path as _Path
        from fastapi import HTTPException
        original_unlink = _Path.unlink

        def _flaky_unlink(path_self, *args, **kwargs):
            if str(path_self).startswith(self.tmpdir.name):
                raise PermissionError("unlink denied")
            return original_unlink(path_self, *args, **kwargs)

        upload = FakeUpload("test.ir", fail=True)
        with patch.object(_Path, "unlink", _flaky_unlink):
            with self.assertRaises(HTTPException) as ctx:
                await main.upload_easyeffects_ir(file=upload)

        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("client disconnected", str(ctx.exception.detail))

    async def test_bundle_cancellation_during_read_cleans_temp_zip(self):
        upload = FakeUpload("bundle.zip", cancel=True)
        with self.assertRaises(asyncio.CancelledError):
            await main.import_easyeffects_preset_bundle(file=upload)
        self.assertEqual(self._leftovers(), [])

    async def test_bundle_staging_failure_cleans_temp_zip(self):
        import zipfile
        import io
        with zipfile.ZipFile(io.BytesIO(), "w") as archive:
            archive.writestr("preset.json", json_dumps({"name": "p"}))
            payload = archive.fp.getvalue()
        upload = FakeUpload("bundle.zip", payload=payload)
        with patch.object(main, "_finish_easyeffects_preset_mutation",
                          return_value={"active_preset": "p"}):
            # The bundle stages, then fails later (no preset import backend
            # in the dummy manager); the staged zip must still be removed.
            with self.assertRaises(Exception):
                await main.import_easyeffects_preset_bundle(file=upload)
        self.assertEqual(self._leftovers(), [])


def json_dumps(value):
    import json
    return json.dumps(value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
