#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Strict per-bank preset separation: ownership, assignment, All Banks."""

import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio import output_state as state_api
from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state_store import OutputStateStore
from dsp.manager import DSPManager
from dsp.persistence import clean_bank, preset_bank


PEQ = {"enabled": True, "params": {"channelMode": "stereo-linked", "bands": [
    {"filterType": "bell", "frequencyHz": 100, "gainDb": -2, "q": 1},
]}}


def make_ir(manager: DSPManager, name: str = "room.wav") -> str:
    path = manager.irs_dir / name
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00\x00\x00\x40")
    return path.name


def make_service(home: Path, manager: DSPManager) -> OutputService:
    store = OutputStateStore(home / "out.json")
    service = OutputService(OutputServiceDeps(
        store=store, preset_loader=lambda name: manager.preset_store.read(name),
        resolve_ir=lambda kernel: {}, measurement_active=lambda: False,
        legacy_snapshot_loader=None))
    state = state_api.set_mode_routing(
        state_api.default_output_state(), "stereo-sub", "A", ["main_l", "main_r", "sub1"])
    store.commit(state, expected_revision=0)
    return service


class PresetBankSeparationTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.manager = DSPManager(home=self.home)
        self.service = make_service(self.home, self.manager)

    def test_clean_bank_rejects_paths(self):
        self.assertEqual(clean_bank("low"), "low")
        self.assertEqual(clean_bank("  low_mid  "), "low_mid")
        self.assertIsNone(clean_bank(""))
        self.assertIsNone(clean_bank(None))
        self.assertIsNone(clean_bank("a/b"))
        self.assertIsNone(clean_bank(".."))

    def test_peq_creation_tags_bank(self):
        created = self.manager.create_peq_preset("LowCorr", PEQ, bank="low")
        self.assertEqual(created.get("bank"), "low")
        self.assertEqual(self.manager.preset_bank("LowCorr"), "low")
        listed = {e["name"]: e.get("bank") for e in self.manager.list_presets()}
        self.assertEqual(listed["LowCorr"], "low")

    def test_convolver_creation_tags_bank(self):
        ir = make_ir(self.manager)
        created = self.manager.create_convolver_preset("LowIR", ir, bank="low")
        self.assertEqual(created.get("bank"), "low")
        with_upload = self.manager.create_convolver_preset_with_upload(
            "LowIR2", self.manager.irs_dir / ir, ir, bank="low")
        self.assertEqual(with_upload["preset"].get("bank"), "low")

    def test_cross_bank_overwrite_is_refused(self):
        self.manager.create_peq_preset("LowCorr", PEQ, bank="low")
        with self.assertRaisesRegex(ValueError, "low.*high|high.*low|belongs to bank"):
            self.manager.create_peq_preset("LowCorr", PEQ, bank="high")
        self.assertEqual(self.manager.preset_bank("LowCorr"), "low")

    def test_legacy_preset_adopts_requesting_bank(self):
        self.manager.create_peq_preset("LegacyX", PEQ)
        self.assertIsNone(self.manager.preset_bank("LegacyX"))
        self.manager.create_peq_preset("LegacyX", PEQ, bank="mid")
        self.assertEqual(self.manager.preset_bank("LegacyX"), "mid")

    def test_import_retags_into_target_bank(self):
        payload = ('{"schema": "fxroute.dsp.preset", "version": 1, "chain": [],'
                   ' "metadata": {"bank": "high"}}')
        created = self.manager.import_preset_json("ImpLow.json", payload, bank="low")
        self.assertEqual(created.get("bank"), "low")
        stored = self.manager.preset_store.read("ImpLow")
        self.assertEqual(preset_bank(stored), "low")

    def test_combine_tags_target_bank(self):
        self.manager.create_peq_preset("A1", PEQ, bank="main")
        self.manager.create_peq_preset("A2", PEQ, bank="main")
        created = self.manager.combine_presets("CombinedMain", ["A1", "A2"], bank="main")
        self.assertEqual(created.get("bank"), "main")

    def test_combined_bank_preset_is_an_independent_b_slot(self):
        self.manager.create_peq_preset("A1", PEQ, bank="main")
        self.manager.create_peq_preset("A2", PEQ, bank="main")
        self.manager.combine_presets("CombinedMain", ["A1", "A2"], bank="main")
        state = self.service.load()
        for preset in ("A1", "CombinedMain"):
            self.service.validate_bank_preset(state, "stereo-sub", "main", preset)
        state = state_api.set_bank_preset(state, "stereo-sub", "main", preset_a="A1", active_side="A")
        state = state_api.set_bank_preset(state, "stereo-sub", "main", preset_b="CombinedMain")
        banks = state["modes"]["stereo-sub"]["banks"]
        for role in ("main_l", "main_r"):
            self.assertEqual(banks[role], {"preset": "A1", "preset_a": "A1", "preset_b": "CombinedMain"})
        state = state_api.set_bank_preset(state, "stereo-sub", "main", active_side="B")
        banks = state["modes"]["stereo-sub"]["banks"]
        for role in ("main_l", "main_r"):
            self.assertEqual(banks[role], {"preset": "CombinedMain", "preset_a": "A1", "preset_b": "CombinedMain"})

    def test_startup_cleanup_commits_foreign_slots_once(self):
        self.manager.create_peq_preset("GlobCorr", PEQ, bank="global")
        self.manager.create_peq_preset("MainCorr", PEQ, bank="main")
        state = self.service.load()
        for role in ("main_l", "main_r"):
            state["modes"]["stereo-sub"]["banks"][role] = {
                "preset": "GlobCorr", "preset_a": "MainCorr", "preset_b": "GlobCorr"}
        committed = self.service.commit(state, expected_revision=state["revision"])
        self.assertEqual(len(self.service.drop_foreign_bank_presets()), 2)
        stored = self.service.load()
        self.assertEqual(stored["revision"], committed["revision"] + 1)
        for role in ("main_l", "main_r"):
            self.assertEqual(stored["modes"]["stereo-sub"]["banks"][role],
                             {"preset": "MainCorr", "preset_a": "MainCorr", "preset_b": None})
        self.assertEqual(self.service.drop_foreign_bank_presets(), [])
        self.assertEqual(self.service.load()["revision"], stored["revision"])
        absent = OutputService(OutputServiceDeps(
            store=OutputStateStore(self.home / "absent.json"),
            preset_loader=lambda name: self.manager.preset_store.read(name),
            resolve_ir=lambda kernel: {}, measurement_active=lambda: False))
        self.assertEqual(absent.drop_foreign_bank_presets(), [])
        self.assertFalse((self.home / "absent.json").exists())

    def test_same_bank_assignment_ok_cross_bank_refused(self):
        self.manager.create_peq_preset("MainCorr", PEQ, bank="main")
        self.manager.create_peq_preset("LowCorr", PEQ, bank="low")
        state = self.service.load()
        self.service.validate_bank_preset(state, "stereo-sub", "main", "MainCorr")
        with self.assertRaisesRegex(ValueError, "belongs to bank"):
            self.service.validate_bank_preset(state, "stereo-sub", "main", "LowCorr")

    def test_global_has_own_presets(self):
        self.manager.create_peq_preset("GlobCorr", PEQ, bank="global")
        self.manager.create_peq_preset("MainCorr", PEQ, bank="main")
        state = self.service.load()
        self.service.validate_bank_preset(state, "stereo-sub", "global", "GlobCorr")
        with self.assertRaisesRegex(ValueError, "belongs to bank"):
            self.service.validate_bank_preset(state, "stereo-sub", "main", "GlobCorr")
        with self.assertRaisesRegex(ValueError, "belongs to bank"):
            self.service.validate_bank_preset(state, "stereo-sub", "global", "MainCorr")

    def test_legacy_presets_migrate_to_global_once(self):
        self.manager.preset_store.write("OldStock", {"schema": DSPManager.PRESET_SCHEMA,
                                                     "version": DSPManager.PRESET_VERSION,
                                                     "chain": [], "metadata": {}})
        self.manager.preset_store.write("LowKeep", {"schema": DSPManager.PRESET_SCHEMA,
                                                    "version": DSPManager.PRESET_VERSION,
                                                    "chain": [], "metadata": {"bank": "low"}})
        fresh = DSPManager(home=self.home)
        self.assertEqual(fresh.preset_bank("OldStock"), "global")
        self.assertEqual(fresh.preset_bank("LowKeep"), "low")
        self.assertIsNone(fresh.preset_bank("Direct"))
        self.assertIsNone(fresh.preset_bank("Neutral"))

    def test_builtin_and_legacy_assignable_everywhere(self):
        self.manager.create_peq_preset("UntaggedY", PEQ)
        state = self.service.load()
        for preset in ("Direct", "Neutral", "UntaggedY"):
            self.service.validate_bank_preset(state, "stereo-sub", "main", preset)
            self.service.validate_bank_preset(state, "stereo-sub", "global", preset)

    def test_all_banks_is_not_assignable(self):
        state = self.service.load()
        with self.assertRaises(ValueError):
            state_api.set_bank_preset(state, "stereo-sub", "all", preset="Room")


if __name__ == "__main__":
    unittest.main()
