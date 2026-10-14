#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed preset deletion: a delete that fails while staging its files
restores the preset and its IR instead of leaving a half-deleted preset.

A preset that a bank slot still names is released through the output state
before it is deleted (see test_bank_preset_libraries).
"""

import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dsp.manager as dsp_manager_module
from dsp.manager import DSPManager


def _peq_definition():
    return {"enabled": True, "params": {"channelMode": "stereo-linked", "bands": [
        {"filterType": "bell", "frequencyHz": 100, "gainDb": 2, "q": 1}] }}


class PresetDeleteSafetyTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.manager = DSPManager(home=self.home)
        self.irs_dir = self.manager.library().irs_dir
        self.irs_dir.mkdir(parents=True)
        self.manager.create_peq_preset("Victim", _peq_definition())

    def _write_mono_wav(self, path, frames=b"\x00\x00\x00\x00"):
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(48000)
            handle.writeframes(frames)

    def _delete_leftovers(self):
        return [path.name for path in self.manager.library().presets_dir.iterdir()
                if path.name.startswith(".fxroute-delete-backup-")]

    def test_orphan_ir_failure_restores_preset_file(self):
        ir = self.irs_dir / "victim.irs"
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
        self.assertTrue((self.manager.library().presets_dir / "Conv2.json").exists())
        self.assertTrue(ir.exists())
        self.assertEqual(self._delete_leftovers(), [])

    def test_successful_delete_removes_the_orphaned_kernel(self):
        ir = self.irs_dir / "victim.irs"
        self._write_mono_wav(ir)
        self.manager.create_convolver_preset("Conv", ir.name)
        self.manager.delete_preset("Conv")
        self.assertFalse((self.manager.library().presets_dir / "Conv.json").exists())
        self.assertFalse(ir.exists())
        self.assertTrue((self.manager.library().presets_dir / "Victim.json").exists())
        self.assertEqual(self._delete_leftovers(), [])

    def test_slot_preset_is_refused_with_files_intact(self):
        with self.assertRaisesRegex(ValueError, "used by an output bank"):
            self.manager.delete_preset("Victim", pinned_presets=("Victim",))
        self.assertTrue((self.manager.library().presets_dir / "Victim.json").exists())
        self.assertEqual(self._delete_leftovers(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
