#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Atomic IR upload: a failed upload must never damage the previous valid IR.

New IR content is staged to a temp file in irs_dir, validated there, and
only then atomically published with os.replace.  Failures at any step
(stage, validation, publish) leave a pre-existing IR byte-identical, and a
successful upload replaces it fully.
"""

import os
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp.manager import DSPManager, build_wav_bytes, parse_wav_frames


class IrUploadAtomicTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.manager = DSPManager(home=self.home)
        self.irs_dir = self.manager.library().irs_dir
        self.irs_dir.mkdir(parents=True)

    def _write_mono_wav(self, path, frames=b"\x00\x00\x00\x00"):
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(48000)
            handle.writeframes(frames)

    def _stage_leftovers(self):
        return [path.name for path in self.irs_dir.iterdir()
                if path.name.startswith(".fxroute-ir-stage-")]

    def _existing_ir(self, name="room.wav", frames=b"\x01\x00\x02\x00\x03\x00\x04\x00"):
        path = self.irs_dir / name
        self._write_mono_wav(path, frames)
        return path, path.read_bytes()

    def _new_source(self, frames=b"\x05\x00\x06\x00\x07\x00\x08\x00"):
        path = self.home / "new.wav"
        self._write_mono_wav(path, frames)
        return path

    def test_stage_failure_keeps_previous_ir(self):
        old, old_bytes = self._existing_ir()
        with mock.patch.object(DSPManager, "_stage_ir_bytes",
                               side_effect=OSError("disk fault simulated")):
            with self.assertRaises(OSError):
                self.manager.upload_ir(self._new_source(), "room.wav")
        self.assertEqual(old.read_bytes(), old_bytes)
        self.assertEqual(self._stage_leftovers(), [])

    def test_staged_validation_failure_keeps_previous_ir(self):
        old, old_bytes = self._existing_ir()
        with mock.patch.object(DSPManager, "_validate_ir_file",
                               side_effect=ValueError("staged check failed")):
            with self.assertRaises(ValueError):
                self.manager.upload_ir(self._new_source(), "room.wav")
        self.assertEqual(old.read_bytes(), old_bytes)
        self.assertEqual(self._stage_leftovers(), [])

    def test_publish_failure_keeps_previous_ir(self):
        old, old_bytes = self._existing_ir()
        with mock.patch.object(DSPManager, "_publish_staged_ir",
                               side_effect=OSError("publish fault simulated")):
            with self.assertRaises(OSError):
                self.manager.upload_ir(self._new_source(), "room.wav")
        self.assertEqual(old.read_bytes(), old_bytes)

    def test_dual_publish_failure_keeps_merged_ir(self):
        left = self.home / "left.wav"
        right = self.home / "right.wav"
        self._write_mono_wav(left, b"\x01\x00\x02\x00")
        self._write_mono_wav(right, b"\x03\x00\x04\x00")
        self.manager.upload_ir_pair(left, "left.wav", right, "right.wav", "Stereo.irs")
        merged = self.irs_dir / "Stereo.irs"
        merged_bytes = merged.read_bytes()
        with mock.patch.object(DSPManager, "_publish_staged_ir",
                               side_effect=OSError("publish fault simulated")):
            with self.assertRaises(OSError):
                self.manager.upload_ir_pair(left, "left.wav", right, "right.wav", "Stereo.irs")
        self.assertEqual(merged.read_bytes(), merged_bytes)

    def test_successful_upload_replaces_previous_ir_fully(self):
        old, old_bytes = self._existing_ir()
        new_source = self._new_source()
        result = self.manager.upload_ir(new_source, "room.wav")
        self.assertEqual(result["name"], "room.wav")
        self.assertEqual(old.read_bytes(), new_source.read_bytes())
        self.assertNotEqual(old.read_bytes(), old_bytes)
        self.manager._validate_ir_file(old)
        self.assertEqual(self._stage_leftovers(), [])

    def test_successful_dual_upload_replaces_merged_ir_fully(self):
        left = self.home / "left.wav"
        right = self.home / "right.wav"
        self._write_mono_wav(left, b"\x01\x00\x02\x00")
        self._write_mono_wav(right, b"\x03\x00\x04\x00")
        first = self.manager.upload_ir_pair(left, "left.wav", right, "right.wav", "Stereo.irs")
        first_bytes = (self.irs_dir / "Stereo.irs").read_bytes()
        self._write_mono_wav(left, b"\x05\x00\x06\x00")
        self._write_mono_wav(right, b"\x07\x00\x08\x00")
        second = self.manager.upload_ir_pair(left, "left.wav", right, "right.wav", "Stereo.irs")
        merged = self.irs_dir / "Stereo.irs"
        self.assertEqual(second["name"], "Stereo.irs")
        self.assertNotEqual(merged.read_bytes(), first_bytes)
        self.assertEqual(first["name"], second["name"])
        self.manager._validate_ir_file(merged)
        self.assertEqual(self._stage_leftovers(), [])

    def test_65536_tap_presets_keep_all_frames_after_reload(self):
        left_data = struct.pack('<f', 0.5) + b'\0' * (65534 * 4) + struct.pack('<f', 0.25)
        right_data = struct.pack('<f', -0.25) + b'\0' * (65534 * 4) + struct.pack('<f', -0.125)
        left = self.home / 'long-left.wav'
        right = self.home / 'long-right.wav'
        left.write_bytes(build_wav_bytes(1, 48000, 32, 3, left_data))
        right.write_bytes(build_wav_bytes(1, 48000, 32, 3, right_data))
        single = self.manager.create_convolver_preset_with_upload('LongSingle', left, left.name)
        dual = self.manager.create_convolver_preset_with_dual_uploads(
            'LongDual', left, left.name, right, right.name)
        reloaded = DSPManager(home=self.home)
        for result, channels, first, last in (
            (single, 1, (0.5,), (0.25,)),
            (dual, 2, (0.5, -0.25), (0.25, -0.125)),
        ):
            with self.subTest(channels=channels):
                preset = reloaded.library().read(result['preset']['name'])
                self.assertEqual(preset['chain'][0]['params']['kernel'], result['ir']['basename'])
                stored = parse_wav_frames(Path(result['ir']['path']))
                self.assertEqual(stored['frames'], 65536)
                self.assertEqual(stored['channels'], channels)
                self.assertEqual(len(stored['data']), 65536 * channels * 4)
                self.assertEqual(struct.unpack_from(f'<{channels}f', stored['data']), first)
                self.assertEqual(struct.unpack_from(f'<{channels}f', stored['data'], 65535 * channels * 4), last)
                compiled = reloaded.compile_engine_text(
                    [{'name': 'FL', 'source': 0}, {'name': 'FR', 'source': 1}],
                    preset_name=result['preset']['name'])
                self.assertIn(result['ir']['path'], compiled)
        self.assertEqual(self._stage_leftovers(), [])

    def test_symlink_destination_is_blocked(self):
        target = self.home / "outside.wav"
        self._write_mono_wav(target, b"\x01\x00\x02\x00")
        before = target.read_bytes()
        link = self.irs_dir / "room.wav"
        os.symlink(target, link)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.manager.upload_ir(self._new_source(), "room.wav")
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), before)

    def test_directory_destination_is_blocked(self):
        (self.irs_dir / "room.wav").mkdir()
        with self.assertRaisesRegex(ValueError, "directory"):
            self.manager.upload_ir(self._new_source(), "room.wav")


if __name__ == "__main__":
    unittest.main(verbosity=2)
