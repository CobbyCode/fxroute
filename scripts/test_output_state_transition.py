#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Authoritative output service: guarded mutations, fingerprints, loading."""

import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_service import MeasurementActiveError, OutputService, OutputServiceDeps
from audio.output_state import (
    default_bass_management, default_output_state, set_bank_preset, set_bass_management,
    set_crossover, set_mode_extras, set_mode_routing, set_output_processing, switch_mode,
    select_bank,
)
from audio.output_state_store import OutputStateStore, StateConflictError
from dsp.manager import DSPManager, build_wav_bytes


def make_service(path, *, manager=None, active=False):
    return OutputService(OutputServiceDeps(
        store=OutputStateStore(path),
        preset_loader=manager.read_preset if manager else (lambda _mode, _bank, name: (_ for _ in ()).throw(
            AssertionError(f"unexpected preset load: {name}"))),
        resolve_ir=lambda _bank, kernel: (_ for _ in ()).throw(AssertionError(f"unexpected IR: {kernel}")),
        measurement_active=lambda: active,
    ))


def stereo_state(service):
    state = set_mode_routing(default_output_state(), "stereo", "A", ["main_l", "main_r"])
    return service._deps.store.commit(state, expected_revision=0)


class ServiceMutationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "output-state.json"
        home = Path(directory.name) / "home"
        self.manager = DSPManager(home=home)
        for bank in ("sub1", "global"):
            self.manager.library(bank, "stereo-sub").write(
                "Room", {"schema": "fxroute.dsp.preset", "version": 1, "chain": []})
        self.service = make_service(self.path, manager=self.manager)

    def test_apply_routing_and_bank_preset_commits_revision(self):
        committed = stereo_state(self.service)
        self.assertEqual(committed["revision"], 1)
        result = self.service.apply(
            lambda state: set_bank_preset(
                set_mode_routing(state, "stereo-sub", "A", ["main_l", "main_r", "sub1", "sub1"]),
                "stereo-sub", "sub1", preset="Room", active_side="A"),
            expected_revision=1)
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["modes"]["stereo-sub"]["routing"]["A"],
                         ["main_l", "main_r", "sub1", "sub1"])
        self.assertEqual(result["modes"]["stereo-sub"]["banks"]["sub1"]["preset"], "Room")
        self.assertEqual(result["modes"]["stereo-sub"]["banks"]["sub1"]["preset_a"], "Room")
        reloaded = OutputStateStore(self.path).load()
        self.assertEqual(reloaded, result)

    def test_apply_processing_bass_and_extras(self):
        stereo_state(self.service)
        result = self.service.apply(
            lambda state: set_mode_extras(
                set_bass_management(
                    set_output_processing(
                        set_mode_routing(state, "stereo-sub", "A",
                                         ["main_l", "main_r", "sub1", "sub1"]),
                        "stereo-sub", "sub1", level_db=-4.5,
                        alignment_ms=-2.0, polarity="invert"),
                    "stereo-sub", frequency_hz=90, main_highpass_enabled=False),
                "stereo-sub", {"headroom": {"enabled": True}}),
            expected_revision=1)
        processing = result["modes"]["stereo-sub"]["processing"]["sub1"]
        self.assertEqual((processing["level_db"], processing["alignment_ms"], processing["polarity"]),
                         (-4.5, -2.0, "invert"))
        self.assertEqual(result["modes"]["stereo-sub"]["bass_management"],
                         {**default_bass_management(), "frequency_hz": 90,
                          "main_highpass_enabled": False})
        self.assertEqual(result["modes"]["stereo-sub"]["extras"], {"headroom": {"enabled": True}})

    def test_stale_revision_conflict_preserves_committed_bytes(self):
        stereo_state(self.service)
        other = make_service(self.path, manager=self.manager)
        other.apply(lambda state: switch_mode(state, "stereo-sub"), expected_revision=1)
        committed = self.path.read_bytes()
        with self.assertRaises(StateConflictError):
            self.service.apply(lambda state: switch_mode(state, "stereo-sub"), expected_revision=1)
        self.assertEqual(self.path.read_bytes(), committed)

    def test_commit_prepared_candidate_and_revert_rebases_revision(self):
        committed = stereo_state(self.service)
        candidate = switch_mode(committed, "stereo-sub")
        second = self.service.commit(candidate, expected_revision=1)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["active_mode"], "stereo-sub")
        third = self.service.revert(committed, expected_revision=2)
        self.assertEqual(third["revision"], 3)
        self.assertEqual(third["active_mode"], "stereo")
        with self.assertRaises(StateConflictError):
            self.service.revert(committed, expected_revision=2)

    def test_measurement_active_blocks_without_touching_bytes(self):
        stereo_state(self.service)
        busy = make_service(self.path, manager=self.manager, active=True)
        before = self.path.read_bytes()
        with self.assertRaises(MeasurementActiveError):
            busy.apply(lambda state: switch_mode(state, "stereo-sub"), expected_revision=1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_mutation_rejected_before_any_write(self):
        stereo_state(self.service)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.service.apply(
                lambda state: set_output_processing(state, "stereo-sub", "left_low", level_db=0.0),
                expected_revision=1)
        with self.assertRaises(ValueError):
            self.service.apply(
                lambda state: set_output_processing(
                    state, "stereo-sub", "sub1",
                    lowpass={"family": "linkwitz-riley", "slope_db_oct": 18, "frequency_hz": 80}),
                expected_revision=1)
        self.assertEqual(self.path.read_bytes(), before)


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "output-state.json"
        home = Path(directory.name) / "home"
        self.manager = DSPManager(home=home)
        for bank in ("sub1", "global"):
            self.manager.library(bank, "stereo-sub").write(
                "Room", {"schema": "fxroute.dsp.preset", "version": 1, "chain": []})
        self.service = make_service(self.path, manager=self.manager)
        self.base = self.service.apply(
            lambda state: switch_mode(set_mode_routing(state, "stereo-sub", "A",
                                           ["main_l", "main_r", "sub1", "sub1"]), "stereo-sub"),
            expected_revision=0)

    def fingerprint(self, state):
        return self.service.fingerprint(state, output_key="A", channels=4, sample_rate_hz=48000)

    def test_stable_and_selection_independent(self):
        first = self.fingerprint(self.base)
        self.assertEqual(first, self.fingerprint(self.base))
        self.assertEqual(len(first), 64)
        reselected = select_bank(self.base, "stereo-sub", "A", 4, "main_l")
        self.assertEqual(self.fingerprint(reselected), first)

    def test_sensitive_to_effective_processing(self):
        first = self.fingerprint(self.base)
        with_bank = set_bank_preset(self.base, "stereo-sub", "global", preset="Room")
        self.assertNotEqual(self.fingerprint(with_bank), first)
        with_trim = set_output_processing(self.base, "stereo-sub", "main_l", level_db=-3.0)
        self.assertNotEqual(self.fingerprint(with_trim), first)
        with_bass = set_bass_management(self.base, "stereo-sub", frequency_hz=90)
        self.assertNotEqual(self.fingerprint(with_bass), first)

    def test_area_neutral_reassignment_and_inactive_slots_keep_fingerprint(self):
        first = self.fingerprint(self.base)
        neutral = set_bank_preset(self.base, "stereo-sub", "sub1", preset="Neutral")
        self.assertEqual(self.fingerprint(neutral), first)
        with_b = set_bank_preset(neutral, "stereo-sub", "sub1", preset_b="Room")
        self.assertEqual(self.fingerprint(with_b), first)

    def test_global_direct_remains_distinct_and_bypasses_only_global(self):
        state = set_mode_extras(self.base, "stereo-sub", {"headroom": {"enabled": True}})
        direct = set_bank_preset(state, "stereo-sub", "global", preset="Direct")
        neutral_plan = self.service.compile_plan(state, output_key="A", channels=4, sample_rate_hz=48000)
        direct_plan = self.service.compile_plan(direct, output_key="A", channels=4, sample_rate_hz=48000)
        self.assertNotEqual(self.fingerprint(direct), self.fingerprint(state))
        self.assertFalse(neutral_plan["global"]["bypass"])
        self.assertEqual(neutral_plan["global"]["extras"], {"headroom": {"enabled": True}})
        self.assertTrue(direct_plan["global"]["bypass"])
        self.assertEqual(direct_plan["global"]["extras"], {})
        self.assertEqual(direct_plan["outputs"], neutral_plan["outputs"])
        self.assertEqual(direct_plan["physical_routes"], neutral_plan["physical_routes"])


class LoadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "output-state.json"
        self.manager = DSPManager(home=Path(directory.name) / "home")

    def test_corrupt_file_raises_and_preserves_bytes(self):
        self.path.write_text("{broken")
        service = make_service(self.path, manager=self.manager)
        with self.assertRaises(ValueError):
            service.load()
        self.assertEqual(self.path.read_text(), "{broken")

    def test_missing_file_returns_defaults_unwritten(self):
        service = make_service(self.path, manager=self.manager)
        state = service.load()
        self.assertEqual(state["revision"], 0)
        self.assertFalse(self.path.exists())


class DeleteGuardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = DSPManager(home=Path(directory.name) / "home")
        for bank in ("sub1", "global"):
            self.manager.library(bank, "stereo-sub").write(
                "Room", {"schema": "fxroute.dsp.preset", "version": 1, "chain": []})
        ir = build_wav_bytes(1, 48000, 32, 3, struct.pack("<4f", 0.5, 0.25, 0.125, 0.0625))
        source = Path(directory.name) / "room.wav"
        source.write_bytes(ir)
        self.manager.upload_ir(source, "room.wav")
        self.manager.create_convolver_preset("Room IR", "room.wav")

    def test_pinned_preset_refused_with_files_intact(self):
        with self.assertRaises(ValueError):
            self.manager.delete_preset("Room IR", pinned_presets={"Room IR"})
        self.assertTrue(self.manager.library().path("Room IR").is_file())
        self.assertTrue((self.manager.library().irs_dir / "room.wav").is_file())

    def test_unpinned_orphan_gc_still_works(self):
        self.manager.delete_preset("Room IR")
        self.assertFalse(self.manager.library().path("Room IR").exists())
        self.assertFalse((self.manager.library().irs_dir / "room.wav").exists())

    def test_shared_kernel_survives_single_deletion(self):
        self.manager.create_convolver_preset("Room IR 2", "room.wav")
        self.manager.delete_preset("Room IR")
        self.assertTrue((self.manager.library().irs_dir / "room.wav").is_file())


if __name__ == "__main__":
    unittest.main()
