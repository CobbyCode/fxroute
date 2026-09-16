#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Authoritative output service: guarded mutations, fingerprints, migration."""

import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_service import MeasurementActiveError, OutputService, OutputServiceDeps
from audio.output_state import (
    default_output_state, set_bank_preset, set_bass_management, set_mode_extras,
    set_mode_routing, set_output_processing, switch_mode, select_bank,
)
from audio.output_state_migration import migrate_legacy_output_state
from audio.output_state_store import OutputStateStore, StateConflictError
from dsp.manager import DSPManager, build_wav_bytes


def make_service(path, *, manager=None, active=False, legacy=None):
    return OutputService(OutputServiceDeps(
        store=OutputStateStore(path),
        preset_loader=manager.preset_store.read if manager else (lambda name: (_ for _ in ()).throw(
            AssertionError(f"unexpected preset load: {name}"))),
        resolve_ir=lambda kernel: (_ for _ in ()).throw(AssertionError(f"unexpected IR: {kernel}")),
        measurement_active=lambda: active,
        legacy_snapshot_loader=legacy,
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
        self.manager.preset_store.write("Room", {"schema": "fxroute.dsp.preset", "version": 1, "chain": []})
        self.service = make_service(self.path, manager=self.manager)

    def test_apply_routing_and_bank_preset_commits_revision(self):
        committed = stereo_state(self.service)
        self.assertEqual(committed["revision"], 1)
        result = self.service.apply(
            lambda state: set_bank_preset(
                set_mode_routing(state, "stereo", "A", ["main_l", "main_r", "sub1", "sub1"]),
                "stereo", "sub1", preset="Room", active_side="A"),
            expected_revision=1)
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["modes"]["stereo"]["routing"]["A"],
                         ["main_l", "main_r", "sub1", "sub1"])
        self.assertEqual(result["modes"]["stereo"]["banks"]["sub1"]["preset"], "Room")
        self.assertEqual(result["modes"]["stereo"]["banks"]["sub1"]["preset_a"], "Room")
        reloaded = OutputStateStore(self.path).load()
        self.assertEqual(reloaded, result)

    def test_apply_processing_bass_and_extras(self):
        stereo_state(self.service)
        result = self.service.apply(
            lambda state: set_mode_extras(
                set_bass_management(
                    set_output_processing(
                        set_mode_routing(state, "stereo", "A",
                                         ["main_l", "main_r", "sub1", "sub1"]),
                        "stereo", "sub1", level_db=-4.5,
                        alignment_ms=-2.0, polarity="invert"),
                    "stereo", frequency_hz=90, main_highpass_enabled=False),
                "stereo", {"headroom": {"enabled": True}}),
            expected_revision=1)
        processing = result["modes"]["stereo"]["processing"]["sub1"]
        self.assertEqual((processing["level_db"], processing["alignment_ms"], processing["polarity"]),
                         (-4.5, -2.0, "invert"))
        self.assertEqual(result["modes"]["stereo"]["bass_management"],
                         {"frequency_hz": 90, "main_highpass_enabled": False})
        self.assertEqual(result["modes"]["stereo"]["extras"], {"headroom": {"enabled": True}})

    def test_stale_revision_conflict_preserves_committed_bytes(self):
        stereo_state(self.service)
        other = make_service(self.path, manager=self.manager)
        other.apply(lambda state: switch_mode(state, "crossover"), expected_revision=1)
        committed = self.path.read_bytes()
        with self.assertRaises(StateConflictError):
            self.service.apply(lambda state: switch_mode(state, "crossover"), expected_revision=1)
        self.assertEqual(self.path.read_bytes(), committed)

    def test_commit_prepared_candidate_and_revert_rebases_revision(self):
        committed = stereo_state(self.service)
        candidate = switch_mode(committed, "crossover")
        second = self.service.commit(candidate, expected_revision=1)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["active_mode"], "crossover")
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
            busy.apply(lambda state: switch_mode(state, "crossover"), expected_revision=1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_mutation_rejected_before_any_write(self):
        stereo_state(self.service)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.service.apply(
                lambda state: set_output_processing(state, "stereo", "left_low", level_db=0.0),
                expected_revision=1)
        with self.assertRaises(ValueError):
            self.service.apply(
                lambda state: set_output_processing(
                    state, "stereo", "sub1",
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
        self.manager.preset_store.write("Room", {"schema": "fxroute.dsp.preset", "version": 1, "chain": []})
        self.service = make_service(self.path, manager=self.manager)
        self.base = self.service.apply(
            lambda state: set_mode_routing(state, "stereo", "A",
                                           ["main_l", "main_r", "sub1", "sub1"]),
            expected_revision=0)

    def fingerprint(self, state):
        return self.service.fingerprint(state, output_key="A", channels=4, sample_rate_hz=48000)

    def test_stable_and_selection_independent(self):
        first = self.fingerprint(self.base)
        self.assertEqual(first, self.fingerprint(self.base))
        self.assertEqual(len(first), 64)
        reselected = select_bank(self.base, "stereo", "A", 4, "main_l")
        self.assertEqual(self.fingerprint(reselected), first)

    def test_sensitive_to_effective_processing(self):
        first = self.fingerprint(self.base)
        with_bank = set_bank_preset(self.base, "stereo", "global", preset="Room")
        self.assertNotEqual(self.fingerprint(with_bank), first)
        with_trim = set_output_processing(self.base, "stereo", "main_l", level_db=-3.0)
        self.assertNotEqual(self.fingerprint(with_trim), first)
        with_bass = set_bass_management(self.base, "stereo", frequency_hz=90)
        self.assertNotEqual(self.fingerprint(with_bass), first)


class EnsureStateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "output-state.json"
        self.calls = []
        home = Path(directory.name) / "home"
        self.manager = DSPManager(home=home)

    def legacy(self):
        self.calls.append(1)
        return {"mode": {"mode": "subwoofer-2.1"}, "routing": {}, "active_preset": "Neutral",
                "compare": {}, "extras": {}, "output_key": "A", "channels": 4}

    def test_migrates_once_then_restarts_clean(self):
        service = make_service(self.path, manager=self.manager, legacy=self.legacy)
        first = service.ensure_state()
        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["modes"]["stereo"]["routing"]["A"],
                         ["main_l", "main_r", "sub1", "sub1"])
        self.assertEqual(len(self.calls), 1)
        second = make_service(self.path, manager=self.manager, legacy=self.legacy).ensure_state()
        self.assertEqual(second, first)
        self.assertEqual(len(self.calls), 1)

    def test_corrupt_file_raises_and_preserves_bytes(self):
        self.path.write_text("{broken")
        service = make_service(self.path, manager=self.manager, legacy=self.legacy)
        with self.assertRaises(ValueError):
            service.ensure_state()
        self.assertEqual(self.path.read_text(), "{broken")
        self.assertEqual(self.calls, [])

    def test_missing_file_without_legacy_returns_defaults_unwritten(self):
        service = make_service(self.path, manager=self.manager)
        state = service.ensure_state()
        self.assertEqual(state["revision"], 0)
        self.assertFalse(self.path.exists())


class DeleteGuardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = DSPManager(home=Path(directory.name) / "home")
        self.manager.preset_store.write("Room", {"schema": "fxroute.dsp.preset", "version": 1, "chain": []})
        ir = build_wav_bytes(1, 48000, 32, 3, struct.pack("<4f", 0.5, 0.25, 0.125, 0.0625))
        source = Path(directory.name) / "room.wav"
        source.write_bytes(ir)
        self.manager.upload_ir(source, "room.wav")
        self.manager.create_convolver_preset("Room IR", "room.wav")

    def test_pinned_preset_refused_with_files_intact(self):
        with self.assertRaises(ValueError):
            self.manager.delete_preset("Room IR", pinned_presets={"Room IR"})
        self.assertTrue(self.manager.preset_store.path("Room IR").is_file())
        self.assertTrue((self.manager.irs_dir / "room.wav").is_file())

    def test_unpinned_orphan_gc_still_works(self):
        self.manager.delete_preset("Room IR")
        self.assertFalse(self.manager.preset_store.path("Room IR").exists())
        self.assertFalse((self.manager.irs_dir / "room.wav").exists())

    def test_shared_kernel_survives_single_deletion(self):
        self.manager.create_convolver_preset("Room IR 2", "room.wav")
        self.manager.delete_preset("Room IR")
        self.assertTrue((self.manager.irs_dir / "room.wav").is_file())


if __name__ == "__main__":
    unittest.main()
