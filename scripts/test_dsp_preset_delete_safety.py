#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed preset deletion: a failing active-preset delete must not
diverge runtime, stored and published preset state.

Deleting the active preset removes its file and moves the persisted active
state to the Neutral fallback.  When the fallback cannot load (missing
fallback, persist error, apply error), the deletion must be rolled back:
the preset file is restored and active.json keeps pointing at an existing
preset.  The API layer must broadcast the truthful state on resync failure
instead of a false success.
"""

import asyncio
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
import dsp.api as dsp_api
import dsp.manager as dsp_manager_module
from dsp.manager import DSPManager

dsp_api.configure_dsp_api(main._make_dsp_api_deps())


def _peq_definition():
    return {"enabled": True, "params": {"channelMode": "stereo-linked", "bands": [
        {"filterType": "bell", "frequencyHz": 100, "gainDb": 2, "q": 1}] }}


class PresetDeleteSafetyTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.manager = DSPManager(home=self.home)
        self.manager.create_peq_preset("Victim", _peq_definition())
        self.manager.load_preset("Victim")
        self.assertEqual(self.manager.get_active_preset(), "Victim")

    def _write_mono_wav(self, path, frames=b"\x00\x00\x00\x00"):
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(48000)
            handle.writeframes(frames)

    def _delete_leftovers(self):
        return [path.name for path in self.manager.output_dir.iterdir()
                if path.name.startswith(".fxroute-delete-backup-")]

    def test_broken_neutral_fallback_keeps_file_and_active_state(self):
        (self.manager.output_dir / "Neutral.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.manager.delete_preset("Victim")
        self.assertTrue((self.manager.output_dir / "Victim.json").exists())
        self.assertEqual(self.manager.get_active_preset(), "Victim")
        self.assertEqual(self.manager.get_status()["active_preset"], "Victim")
        self.assertEqual(self._delete_leftovers(), [])

    def test_active_persist_failure_restores_preset_file(self):
        original_write = self.manager.state_store.write

        def failing_write(filename, payload):
            if filename == "active.json":
                raise OSError("ENOSPC simulated")
            return original_write(filename, payload)

        self.manager.state_store.write = failing_write
        with self.assertRaises(OSError):
            self.manager.delete_preset("Victim")
        self.manager.state_store.write = original_write
        self.assertTrue((self.manager.output_dir / "Victim.json").exists())
        self.assertEqual(self.manager.get_active_preset(), "Victim")
        self.assertEqual(self.manager.get_status()["active_preset"], "Victim")
        self.assertEqual(self._delete_leftovers(), [])

    def test_apply_failure_restores_preset_file(self):
        self.manager.apply_callback = lambda op: (_ for _ in ()).throw(
            RuntimeError("engine apply failed"))
        with self.assertRaises(RuntimeError):
            self.manager.delete_preset("Victim")
        self.assertTrue((self.manager.output_dir / "Victim.json").exists())
        self.assertEqual(self.manager.get_active_preset(), "Victim")
        self.assertEqual(self._delete_leftovers(), [])

    def test_orphan_ir_failure_restores_preset_file(self):
        ir = self.manager.irs_dir / "victim.irs"
        self._write_mono_wav(ir)
        self.manager.create_convolver_preset("Conv", ir.name)
        self.manager.create_convolver_preset("Conv2", ir.name)
        self.manager.delete_preset("Conv")
        # The kernel is still referenced by Conv2, so the IR survives.
        self.assertTrue(ir.exists())
        # Fail the orphan-IR staging (second os.replace): the already
        # staged preset file must be restored.
        real_replace = dsp_manager_module.os.replace
        calls = {"count": 0}

        def flaky_replace(src, dst):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("disk fault simulated")
            return real_replace(src, dst)

        with mock.patch.object(dsp_manager_module.os, "replace", side_effect=flaky_replace):
            with self.assertRaises(OSError):
                self.manager.delete_preset("Conv2")
        self.assertTrue((self.manager.output_dir / "Conv2.json").exists())
        self.assertTrue(ir.exists())
        self.assertEqual(self._delete_leftovers(), [])

    def test_successful_active_delete_moves_to_neutral(self):
        ir = self.manager.irs_dir / "victim.irs"
        self._write_mono_wav(ir)
        self.manager.create_convolver_preset("Conv", ir.name)
        self.manager.load_preset("Conv")
        self.manager.delete_preset("Conv")
        self.assertFalse((self.manager.output_dir / "Conv.json").exists())
        # The orphaned kernel is removed with the preset.
        self.assertFalse(ir.exists())
        self.assertEqual(self.manager.get_active_preset(), "Neutral")
        self.assertEqual(self.manager.get_status()["active_preset"], "Neutral")
        self.assertEqual(self._delete_leftovers(), [])

    def test_successful_inactive_delete_keeps_active_preset(self):
        self.manager.create_peq_preset("Other", _peq_definition())
        self.manager.delete_preset("Other")
        self.assertFalse((self.manager.output_dir / "Other.json").exists())
        self.assertEqual(self.manager.get_active_preset(), "Victim")


class PresetDeleteApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        main.runtime.dsp_mutation_lock = None
        main.dsp_manager = None

    async def test_resync_failure_broadcasts_truthful_state(self):
        from fastapi import HTTPException

        class FakeManager:
            def __init__(self):
                self.active = "Room"

            def get_active_preset(self):
                return self.active

            def delete_preset(self, preset_name, pinned_presets=()):
                self.active = "Neutral"

            def get_status(self):
                return {"status": "ok", "active_preset": self.active}

        class FakeDeleteRequest:
            async def json(self):
                return {"preset_name": "Room"}

        fake = FakeManager()
        broadcast = mock.AsyncMock()
        load_preset = mock.AsyncMock(side_effect=RuntimeError("engine sync failed"))
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main, "_load_dsp_preset", load_preset
        ), mock.patch.object(main.manager, "broadcast", broadcast), mock.patch.object(
            main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"
        ):
            with self.assertRaises(HTTPException):
                await dsp_api.delete_dsp_preset(FakeDeleteRequest())

        load_preset.assert_awaited_once_with("Neutral")
        # No false success may be published: the broadcast carries the
        # truthful post-delete state (Neutral), never the deleted preset.
        broadcast.assert_awaited_once()
        published = broadcast.await_args.args[0]
        self.assertEqual(published["type"], "dsp")
        self.assertEqual(published["data"]["active_preset"], "Neutral")


if __name__ == "__main__":
    unittest.main(verbosity=2)
