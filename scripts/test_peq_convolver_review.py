#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
#
# PEQ/convolver review regressions: seven verified findings.
#
# 1. Dual PEQ with differing L/R gain trims is rejected at validation
#    (engine only supports one shared stereo trim).
# 2. Dual preset band_count counts both sides.
# 3. REW enabled flag is preserved.
# 4. Unsupported REW filter types are reported, never silently dropped.
# 5. Empty/invalid IRs are rejected Python-side and never reach the engine.
# 6. Ambiguous IR stems are rejected instead of silently picking one file.
# 7. Output-filter layout is validated with ValueError (no KeyError,
#    no silent stage drops).

import sys
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp.manager import DSPManager, build_wav_bytes


def write_wav(path, frames=b"\x00\x00\x00\x00", channels=1, rate=48000):
    path = Path(path)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)
    return path


def gain_definition(left_db, right_db):
    def side(db):
        return [{"filterType": "gain", "frequencyHz": 1000,
                 "gainDb": db, "q": 1.0, "enabled": True}]
    return {"enabled": True, "params": {
        "channelMode": "dual", "eqMode": "IIR",
        "leftBands": side(left_db), "rightBands": side(right_db)}}


class PeqConvolverReviewTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="fxroute-peq-review-"))
        self.manager = DSPManager(home=self.home)

    def test_dual_gain_mismatch_rejected_at_validation(self):
        with self.assertRaisesRegex(ValueError, "shared stereo trim"):
            self.manager.validate_peq_v1(gain_definition(0, 6))
        with self.assertRaisesRegex(ValueError, "shared stereo trim"):
            self.manager.validate_peq_v1(gain_definition(3, 6))
        with self.assertRaisesRegex(ValueError, "shared stereo trim"):
            self.manager.create_peq_preset("Mismatch", gain_definition(0, 6))
        # Equal trims stay valid.
        self.manager.create_peq_preset("Match", gain_definition(6, 6))
        self.manager.create_peq_preset("Zero", gain_definition(0, 0))
        # A stored asymmetric preset must fail closed at compile time,
        # never render with one side's trim on both channels.
        chain = [{"id": "equalizer#0", "type": "equalizer", "enabled": True,
                  "params": {"channelMode": "dual", "eqMode": "IIR",
                             "leftBands": [{"filterType": "gain", "frequencyHz": 1000,
                                            "gainDb": 0.0, "q": 1.0, "enabled": True}],
                             "rightBands": [{"filterType": "gain", "frequencyHz": 1000,
                                             "gainDb": 6.0, "q": 1.0, "enabled": True}]}}]
        self.manager.preset_store.write("StoredMismatch", self.manager._native_preset(chain))
        with self.assertRaisesRegex(ValueError, "shared stereo trim"):
            self.manager.compile_engine_text(
                [{"name": "FL", "source": 0}, {"name": "FR", "source": 1}],
                preset_name="StoredMismatch")

    def test_dual_band_count_counts_both_sides(self):
        created = self.manager.create_peq_preset("Dual", {
            "enabled": True, "params": {"channelMode": "dual", "eqMode": "IIR",
                "leftBands": [
                    {"filterType": "bell", "frequencyHz": 100, "gainDb": 2, "q": 1},
                    {"filterType": "notch", "frequencyHz": 200, "gainDb": 0, "q": 2}],
                "rightBands": [
                    {"filterType": "bell", "frequencyHz": 300, "gainDb": -1, "q": 1}]}})
        self.assertEqual(created["channel_mode"], "dual")
        self.assertEqual(created["band_count"], 3)
        single = self.manager.create_peq_preset("Single", {
            "enabled": True, "params": {"channelMode": "stereo-linked", "bands": [
                {"filterType": "bell", "frequencyHz": 100, "gainDb": 2, "q": 1}]}})
        self.assertEqual(single["band_count"], 1)

    def test_rew_enabled_flag_is_preserved(self):
        imported = self.manager.import_rew_peq_text(
            "1 off PK 100 3.0 1.0\n2 on PK 200 -2.0 2.0\n")
        bands = imported["peq"]["params"]["bands"]
        self.assertFalse(bands[0]["enabled"])
        self.assertTrue(bands[1]["enabled"])
        imported = self.manager.import_rew_peq_text(
            "1 False PK 100 3.0 1.0\n2 True PK 200 -2.0 2.0\n")
        self.assertFalse(imported["peq"]["params"]["bands"][0]["enabled"])
        self.assertTrue(imported["peq"]["params"]["bands"][1]["enabled"])

    def test_rew_unsupported_types_are_reported(self):
        with self.assertRaisesRegex(ValueError, "Unsupported REW.*LS"):
            self.manager.import_rew_peq_text("1 on LS 100 3.0 1.0\n")
        # Mixed files must not silently drop the shelf line.
        with self.assertRaisesRegex(ValueError, "Unsupported REW"):
            self.manager.import_rew_peq_text(
                "1 on PK 100 3.0 1.0\n2 on HS 5000 2.0 1.0\n")

    def test_empty_and_invalid_ir_never_reach_engine(self):
        empty = self.home / "empty.wav"
        write_wav(empty, frames=b"")
        with self.assertRaisesRegex(ValueError, "no audio frames"):
            self.manager.upload_ir(empty, empty.name)
        self.assertEqual(list(self.manager.irs_dir.iterdir()), [])
        blob = self.home / "bad.wav"
        blob.write_bytes(b"not a wav file")
        with self.assertRaisesRegex(ValueError, "Invalid IR"):
            self.manager.upload_ir(blob, blob.name)
        self.assertEqual(list(self.manager.irs_dir.iterdir()), [])
        # A corrupt file placed manually must still fail at preset
        # creation and at compile time, never produce an engine config.
        smuggled = self.manager.irs_dir / "smuggled.wav"
        smuggled.write_bytes(b"not a wav file")
        with self.assertRaisesRegex(ValueError, "Invalid IR"):
            self.manager.create_convolver_preset("Bad", smuggled.name)
        valid = self.manager.irs_dir / "valid.wav"
        write_wav(valid)
        self.manager.create_convolver_preset("Good", valid.name)
        smuggled.unlink()
        smuggled.write_bytes(b"not a wav file")
        # Point the stored preset at the corrupt kernel stem.
        payload = self.manager.preset_store.read("Good")
        payload["chain"][0]["params"]["kernel"] = "smuggled"
        self.manager.preset_store.write("Good", payload)
        with self.assertRaisesRegex(ValueError, "Invalid IR"):
            self.manager.compile_engine_text(
                [{"name": "FL", "source": 0}, {"name": "FR", "source": 1}],
                preset_name="Good")

    def test_ambiguous_ir_stem_is_rejected(self):
        first = self.manager.irs_dir / "room.irs"
        second = self.manager.irs_dir / "room.wav"
        write_wav(first)
        write_wav(second)
        with self.assertRaisesRegex(ValueError, "Ambiguous IR"):
            self.manager.create_convolver_preset("Room", first.name)
        # Bypass creation to prove compile also refuses to guess.
        self.manager.preset_store.write("RoomDirect", self.manager._native_preset([
            {"id": "convolver#0", "type": "convolver", "enabled": True,
             "params": {"kernel": "room"}}]))
        with self.assertRaisesRegex(ValueError, "Ambiguous IR"):
            self.manager.compile_engine_text(
                [{"name": "FL", "source": 0}], preset_name="RoomDirect")

    def test_output_filter_layout_is_validated(self):
        layout = lambda filters: [{"name": "FL", "routes": [{"input": 0, "gain": 1.0}],
                                   "filters": filters}]
        with self.assertRaises(ValueError):
            self.manager.compile_engine_config(
                layout([{"type": "lowpass"}]), preset_name="Neutral")
        with self.assertRaisesRegex(ValueError, "stages"):
            self.manager.compile_engine_config(
                layout([{"type": "lowpass", "frequency_hz": 80,
                         "q": 0.707, "stages": 0}]), preset_name="Neutral")
        with self.assertRaisesRegex(ValueError, "type"):
            self.manager.compile_engine_config(
                layout([{"type": "bogus", "frequency_hz": 80,
                         "q": 0.707, "stages": 1}]), preset_name="Neutral")
        with self.assertRaisesRegex(ValueError, "whole number"):
            self.manager.compile_engine_config(
                layout([{"type": "lowpass", "frequency_hz": 80,
                         "q": 0.707, "stages": 33}]), preset_name="Neutral")
        with self.assertRaisesRegex(ValueError, "biquad"):
            self.manager.compile_engine_config(
                layout([{"type": "lowpass", "frequency_hz": 80, "q": 0.707, "stages": 20},
                        {"type": "highpass", "frequency_hz": 120, "q": 0.707, "stages": 20}]),
                preset_name="Neutral")
        text = self.manager.compile_engine_text(
            layout([{"type": "highpass", "frequency_hz": 80,
                     "q": 0.70710678, "stages": 2}]),
            preset_name="Neutral")
        self.assertEqual(text.count("peq 0 highpass 80"), 2)


if __name__ == "__main__":
    unittest.main()
