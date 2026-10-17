#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Every filterbank has its own preset library; Global has one per output mode."""

import asyncio
import copy
import io
import json
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import dsp.api as api
from audio import output_state as state_api
from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state_store import OutputStateStore
from audio.output_topology import BANK_IDS, MODES, derive_topology
from dsp.manager import DSPManager, build_wav_bytes, parse_wav_frames
from dsp.persistence import clean_bank
from dsp.surround_global import neutral_selection
from preset_test_support import seed_bound_preset

STEREO_SUB = ("global", "stereo-sub")
BANKS = (("global", "stereo"), STEREO_SUB, ("global", "surround"), ("main", None), ("sub1", None))


def peq(gain_db):
    return {"enabled": True, "params": {"channelMode": "stereo-linked", "bands": [
        {"filterType": "bell", "frequencyHz": 100, "gainDb": gain_db, "q": 1}]}}


def gain(payload):
    return payload["chain"][0]["params"]["bands"][0]["gainDb"]


def mono_ir(*samples):
    return build_wav_bytes(1, 48000, 32, 3, struct.pack(f"<{len(samples)}f", *samples))


def make_service(home, manager):
    def resolve_ir(bank, kernel):
        path = manager._resolve_kernel_path(kernel, manager.library(bank))
        return {"path": str(path), "channels": parse_wav_frames(path)["channels"]}

    service = OutputService(OutputServiceDeps(
        store=OutputStateStore(home / "out.json"), preset_loader=manager.read_preset,
        resolve_ir=resolve_ir, measurement_active=lambda: False))
    state = state_api.set_mode_routing(
        state_api.default_output_state(), "stereo-sub", "A", ["main_l", "main_r", "sub1"])
    state = state_api.set_mode_routing(
        state, "surround", "A", ["main_l", "main_r", "side_l", "side_r", "sub1", "sub2"])
    service.commit(state_api.switch_mode(state, "stereo-sub"), expected_revision=0)
    return service


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.manager = DSPManager(home=self.home)

    def create(self, name, gain_db, bank=None, mode=None):
        group = "front" if (bank, mode) == ("global", "surround") else None
        return self.manager.create_peq_preset(name, peq(gain_db), bank=bank, mode=mode, group=group)

    def stored(self, name):
        return sorted(((entry["bank"], entry["mode"]) for entry in self.manager.list_presets()
                       if entry["name"] == name), key=str)


class LibraryTests(Fixture):
    def test_seeding_ignores_compare_side_metadata(self):
        state = state_api.set_bank_preset(state_api.default_output_state(), "stereo", "global", preset="B")
        state = state_api.set_bank_preset(state, "stereo", "main", active_side="B")
        seed_bound_preset(self.manager, "B", state)
        self.assertEqual(self.stored("B"), [("global", "stereo")])

    def test_a_bank_id_is_one_an_output_mode_can_define(self):
        for bank in ("global", "low", "  low_mid  ", "sub_l", "rear_r", "left_high"):
            self.assertEqual(clean_bank(bank), bank.strip())
        for invalid in ("", None, "a/b", "..", "ir-backup", "Main", "all"):
            self.assertIsNone(clean_bank(invalid))
        for bank, mode in (("a/b", None), ("..", None), ("ir-backup", None), ("global", "mono")):
            with self.subTest(bank=bank, mode=mode), self.assertRaises(ValueError):
                self.manager.library(bank, mode)

    def test_one_name_is_a_separate_preset_in_every_bank(self):
        for index, (bank, mode) in enumerate(BANKS):
            created = self.create("Room", -float(index + 1), bank, mode)
            self.assertEqual(created["bank"], bank)
        for index, (bank, mode) in enumerate(BANKS):
            self.assertEqual(gain(self.manager.library(bank, mode).read("Room")), -float(index + 1))
        self.assertEqual(self.stored("Room"), sorted(BANKS, key=str))
        root = self.manager.output_dir
        self.assertEqual(sorted(str(path.relative_to(root)) for path in root.rglob("*.json")), [
            "global/stereo-sub/Room.json", "global/stereo/Room.json", "global/surround/Room.json",
            "main/Room.json", "sub1/Room.json"])

    def test_rewrite_and_delete_stay_inside_the_bank(self):
        for bank, mode in BANKS:
            self.create("Room", -1.0, bank, mode)
        self.create("Room", -9.0, "sub1")
        self.assertEqual(gain(self.manager.library("main").read("Room")), -1.0)
        self.assertEqual(gain(self.manager.library("sub1").read("Room")), -9.0)
        self.manager.delete_preset("Room", bank="global", mode="stereo-sub")
        self.assertEqual(self.stored("Room"), sorted((bank for bank in BANKS if bank != STEREO_SUB), key=str))
        with self.assertRaises(FileNotFoundError):
            self.manager.delete_preset("Room", bank="global", mode="stereo-sub")

    def test_a_request_without_a_bank_means_stereo_global(self):
        self.create("Plain", -2.0)
        self.assertEqual(self.stored("Plain"), [("global", "stereo")])
        self.assertEqual(gain(self.manager.read_preset("stereo", "global", "Plain")), -2.0)
        # An area bank is the same library in every output mode.
        self.create("Area", -3.0, "main", "surround")
        self.assertEqual(self.stored("Area"), [("main", None)])
        self.assertEqual(gain(self.manager.read_preset("stereo-sub", "main", "Area")), -3.0)

    def test_direct_is_global_only_and_neutral_exists_in_every_bank(self):
        self.assertEqual([(entry["name"], entry["bank"], entry["mode"]) for entry in self.manager.list_presets()],
                         [("Direct", "global", None), ("Neutral", None, None)])
        for mode in MODES:
            for name in ("Direct", "Neutral"):
                self.assertEqual(self.manager.read_preset(mode, "global", name)["chain"], [])
            for bank in BANK_IDS:
                with self.subTest(mode=mode, bank=bank):
                    self.assertEqual(self.manager.read_preset(mode, bank, "Neutral")["chain"], [])
                    with self.assertRaisesRegex(ValueError, "Global"):
                        self.manager.read_preset(mode, bank, "Direct")
                    with self.assertRaisesRegex(ValueError, "built-in preset"):
                        self.create("Neutral", -1.0, bank, mode)
        self.assertEqual(list(self.manager.output_dir.iterdir()), [])

    def test_a_bank_folder_nobody_created_yet_is_a_library(self):
        store = self.manager.presets.store("sub1")
        store.presets_dir.mkdir(parents=True)
        store.write("Handmade", {"schema": "fxroute.dsp.preset", "version": 1, "chain": [], "metadata": {}})
        self.assertEqual(self.stored("Handmade"), [("sub1", None)])

    def test_a_pair_role_routed_alone_is_a_library_of_its_own(self):
        """A lone pair role (a mono Sub on sub_l, say) keeps presets and IRs under its role id."""
        banks = ("rear_l", "side_l", "sub_l", "sub_r")
        source = self.home / "ir.wav"
        source.write_bytes(mono_ir(0.5, 0.0))
        for bank in banks:
            self.create("Room", -1.0, bank)
            self.manager.create_convolver_preset_with_upload("Conv", source, "upload.wav", bank=bank)
        for name in ("Room", "Conv"):
            self.assertEqual(self.stored(name), [(bank, None) for bank in banks])
        self.assertEqual(sorted((ir["bank"], ir["name"]) for ir in self.manager.get_status()["irs"]),
                         [(bank, "Conv.wav") for bank in banks])
        for bank in banks:
            self.assertEqual([item["name"] for item in self.manager.list_irs(bank)], ["Conv.wav"])

    def test_a_stray_directory_is_not_a_bank(self):
        """A library backup with real presets and IRs is neither listed, inventoried nor writable."""
        preset = json.dumps({"schema": "fxroute.dsp.preset", "version": 1, "chain": [], "metadata": {}})
        for name in ("ir-backup", "my backup", "irs"):
            (self.manager.output_dir / name).mkdir(parents=True)
            (self.manager.output_dir / name / "Stray.json").write_text(preset)
            (self.manager.irs_dir / name).mkdir(parents=True)
            (self.manager.irs_dir / name / "Stray.wav").write_bytes(mono_ir(0.5, 0.0))
        self.create("Room", -1.0, "main")
        status = self.manager.get_status()
        self.assertEqual([(entry["name"], entry["bank"], entry["mode"]) for entry in status["presets"]],
                          [("Direct", "global", None), ("Neutral", None, None), ("Room", "main", None)])
        self.assertEqual(status["irs"], [])
        self.assertEqual([store.presets_dir.name for store in self.manager.presets.stores()],
                         ["stereo", "stereo-sub", "surround", "main"])
        with self.assertRaises(ValueError):
            self.create("Written", -1.0, "ir-backup")
        self.assertEqual(sorted(path.name for path in self.manager.output_dir.rglob("*.json")),
                         ["Room.json", "Stray.json", "Stray.json", "Stray.json"])

    def test_uploaded_ir_belongs_to_the_bank_of_its_preset(self):
        for bank, sample in (("main", 0.5), ("sub1", 0.25)):
            source = self.home / f"{bank}.wav"
            source.write_bytes(mono_ir(sample, 0.0))
            created = self.manager.create_convolver_preset_with_upload("Room", source, "upload.wav", bank=bank)
            self.assertEqual(created["preset"]["kernel_name"], "Room")
        for bank, sample in (("main", 0.5), ("sub1", 0.25)):
            store = self.manager.library(bank)
            ir = parse_wav_frames(self.manager._resolve_kernel_path("Room", store))
            self.assertEqual(struct.unpack("<2f", ir["data"]), (sample, 0.0))
            self.assertEqual([item["name"] for item in self.manager.list_irs(bank)], ["Room.wav"])
        self.manager.delete_preset("Room", bank="main")
        self.assertEqual(self.manager.list_irs("main"), [])
        self.assertEqual([item["name"] for item in self.manager.list_irs("sub1")], ["Room.wav"])
        self.assertEqual(self.stored("Room"), [("sub1", None)])

    def test_combine_reads_its_sources_from_the_bank_it_writes_to(self):
        self.create("A", -1.0, "main")
        self.create("B", -2.0, "main")
        self.create("A", -7.0, "sub1")
        with self.assertRaises(FileNotFoundError):
            self.manager.combine_presets("Both", ["A", "B"], bank="sub1")
        created = self.manager.combine_presets("Both", ["A", "B"], bank="main")
        self.assertEqual((created["bank"], created["source_presets"]), ("main", ["A", "B"]))
        chain = self.manager.library("main").read("Both")["chain"]
        self.assertEqual([plugin["params"]["bands"][0]["gainDb"] for plugin in chain], [-1.0, -2.0])
        self.assertEqual(self.stored("Both"), [("main", None)])

    def test_import_lands_in_the_target_bank_whatever_the_file_says(self):
        exported = json.dumps({"schema": "fxroute.dsp.preset", "version": 1, "chain": [],
                               "metadata": {"bank": "high", "mode": "stereo"}})
        created = self.manager.import_preset_json("Imported.json", exported, bank="global", mode="stereo-sub")
        self.assertEqual(created["bank"], "global")
        self.assertEqual(self.stored("Imported"), [STEREO_SUB])
        self.assertEqual(json.loads(self.manager.export_preset_json("Imported", "global", "stereo-sub"))["chain"], [])

class OutputStateTests(Fixture):
    def setUp(self):
        super().setUp()
        self.service = make_service(self.home, self.manager)

    def bind(self, bindings):
        state = self.service.load()
        for role, name in bindings.items():
            state["modes"]["stereo-sub"]["banks"][role] = {"preset": name, "preset_a": name, "preset_b": None}
        return state_api.validate_output_state(state)

    def plan(self, state):
        return self.service.compile_plan(state, output_key="A", channels=3, sample_rate_hz=48000)

    def test_a_slot_accepts_only_presets_of_its_own_bank(self):
        self.create("GlobCorr", -1.0, *STEREO_SUB)
        self.create("StereoCorr", -1.0, "global", "stereo")
        self.create("MainCorr", -1.0, "main")
        state = self.service.load()
        self.service.validate_bank_preset(state, "stereo-sub", "global", "GlobCorr")
        self.service.validate_bank_preset(state, "stereo-sub", "main", "MainCorr")
        self.service.validate_bank_preset(state, "stereo", "global", "StereoCorr")
        # The Main bank is the same library in Surround.
        self.service.validate_bank_preset(state, "surround", "main", "MainCorr")
        for mode, bank, name in (("stereo-sub", "main", "GlobCorr"), ("stereo-sub", "global", "MainCorr"),
                                 ("stereo-sub", "global", "StereoCorr"), ("stereo", "global", "GlobCorr"),
                                 ("stereo-sub", "sub1", "MainCorr")):
            with self.subTest(mode=mode, bank=bank, name=name), self.assertRaises(FileNotFoundError):
                self.service.validate_bank_preset(state, mode, bank, name)
        for bank in ("global", "main", "sub1"):
            self.service.validate_bank_preset(state, "stereo-sub", bank, "Neutral")
        self.service.validate_bank_preset(state, "stereo-sub", "global", "Direct")
        for bank in ("main", "sub1"):
            with self.subTest(bank=bank), self.assertRaisesRegex(ValueError, "Global"):
                self.service.validate_bank_preset(state, "stereo-sub", bank, "Direct")

    def test_the_plan_reads_every_slot_from_its_own_bank(self):
        for (bank, mode), gain_db in zip((STEREO_SUB, ("main", None), ("sub1", None)), (-1.0, -2.0, -3.0)):
            self.create("Room", gain_db, bank, mode)
        self.create("Room", -8.0, "global", "stereo")
        plan = self.plan(self.bind({role: "Room" for role in ("global", "main_l", "main_r", "sub1")}))
        self.assertEqual(gain(plan["global"]), -1.0)
        outputs = {output["role"]: output["bank"] for output in plan["outputs"]}
        self.assertEqual({role: (bank["library"], gain(bank)) for role, bank in outputs.items()},
                         {"main_l": ("main", -2.0), "main_r": ("main", -2.0), "sub1": ("sub1", -3.0)})
        text = self.manager.compile_engine_text(
            self.service.compile_layout(plan), preset_name=plan["global"]["preset"], mode=plan["mode"])
        # The Stereo + Sub Global preset (-1 dB), not Stereo's of the same name.
        self.assertIn("control gl_0 0.891250938", text)

    def test_a_slot_naming_another_banks_preset_blocks_the_plan(self):
        self.create("MainCorr", -2.0, "main")
        with self.assertRaises(FileNotFoundError):
            self.plan(self.bind({"sub1": "MainCorr"}))
        with self.assertRaises(FileNotFoundError):
            self.plan(self.bind({"global": "MainCorr"}))
        self.plan(self.bind({"main_l": "MainCorr", "main_r": "MainCorr"}))

    def test_an_area_ir_resolves_from_the_bank_of_its_slot(self):
        for bank in ("main", "sub1"):
            source = self.home / f"{bank}.wav"
            source.write_bytes(mono_ir(0.5, 0.25))
            self.manager.create_convolver_preset_with_upload("Room", source, "upload.wav", bank=bank)
        layout = self.service.compile_layout(self.plan(self.bind({"main_l": "Room", "main_r": "Room", "sub1": "Room"})))
        paths = {row["name"]: Path(row["oconv"]["path"]).relative_to(self.manager.irs_dir) for row in layout}
        self.assertEqual({name: str(path) for name, path in paths.items()},
                         {"main_l": "main/Room.wav", "main_r": "main/Room.wav", "sub1": "sub1/Room.wav"})
        self.service.validate_bank_preset(self.service.load(), "stereo-sub", "sub1", "Room")

    def test_neutral_preserves_the_former_area_direct_engine_text(self):
        cases = (
            ("stereo", False, ["main_l", "main_r"]),
            ("stereo-sub", False, ["main_l", "main_r", "sub1"]),
            ("stereo-sub", False, ["main_l", "main_r", "sub1", "sub2"]),
            ("stereo-sub", False, ["main_l", "main_r", "sub_l", "sub_r"]),
            ("stereo-sub", True, ["left_low", "right_low", "left_high", "right_high", "sub1", "sub2"]),
            ("surround", False, ["main_l", "main_r", "center", "side_l", "side_r", "rear_l", "rear_r", "sub1", "sub2"]),
            ("surround", True, ["left_low", "right_low", "left_high", "right_high", "center", "rear_l", "rear_r", "sub1"]),
        )
        for mode, crossover, routing in cases:
            with self.subTest(mode=mode, crossover=crossover, routing=routing):
                self.create("Global EQ", -3.0, "global", mode)
                state = state_api.set_crossover(state_api.default_output_state(), mode, crossover)
                state = state_api.set_mode_routing(state, mode, "A", routing)
                state = state_api.switch_mode(state, mode)
                config = state["modes"][mode]
                # Surround Global selects the preset for its speaker group.
                slot = {**neutral_selection(), "front": "Global EQ"} if mode == "surround" else "Global EQ"
                config["banks"]["global"] = {"preset": slot, "preset_a": slot, "preset_b": None}
                for index, role in enumerate(routing):
                    config["processing"][role].update(level_db=-index, alignment_ms=index / 2,
                                                       polarity="invert" if index % 2 else "normal")
                    if role.endswith("low"):
                        config["processing"][role]["lowpass"] = {
                            "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2500}
                    elif role.endswith("high"):
                        config["processing"][role]["highpass"] = {
                            "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2500}
                plan = self.service.compile_plan(state, output_key="A", channels=len(routing), sample_rate_hz=48000)
                legacy = copy.deepcopy(plan)
                for output in legacy["outputs"]:
                    output["bank"].update(preset="Direct", bypass=True)
                neutral_layout = self.service.compile_layout(plan)
                self.assertEqual(neutral_layout, self.service.compile_layout(legacy))
                extras = {"headroom": {"enabled": True, "params": {"gainDb": -4}}}
                text = self.manager.compile_engine_text(neutral_layout, preset_name=slot, mode=mode,
                                                        extras_override=extras)
                old_text = self.manager.compile_engine_text(self.service.compile_layout(legacy),
                                                            preset_name=slot, mode=mode, extras_override=extras)
                self.assertEqual(text.encode(), old_text.encode())
                self.assertTrue(plan["global"]["chain"])
                self.assertTrue(any(row["gain_db"] < 0 and row["delay_ms"] > 0 for row in neutral_layout))

    def test_combined_bank_preset_is_an_independent_b_slot(self):
        self.create("A1", -1.0, "main")
        self.create("A2", -2.0, "main")
        self.manager.combine_presets("CombinedMain", ["A1", "A2"], bank="main")
        state = self.service.load()
        for preset in ("A1", "CombinedMain"):
            self.service.validate_bank_preset(state, "stereo-sub", "main", preset)
        state = state_api.set_bank_preset(state, "stereo-sub", "main", preset_a="A1", active_side="A")
        state = state_api.set_bank_preset(state, "stereo-sub", "main", preset_b="CombinedMain")
        for role in ("main_l", "main_r"):
            self.assertEqual(state["modes"]["stereo-sub"]["banks"][role],
                             {"preset": "A1", "preset_a": "A1", "preset_b": "CombinedMain"})
        state = state_api.set_bank_preset(state, "stereo-sub", "main", active_side="B")
        for role in ("main_l", "main_r"):
            self.assertEqual(state["modes"]["stereo-sub"]["banks"][role],
                             {"preset": "CombinedMain", "preset_a": "A1", "preset_b": "CombinedMain"})

    def test_all_banks_is_not_assignable(self):
        with self.assertRaises(ValueError):
            state_api.set_bank_preset(self.service.load(), "stereo-sub", "all", preset="Room")


def bank_state(bindings):
    state = state_api.set_mode_routing(
        state_api.default_output_state(), "stereo-sub", "A", ["main_l", "main_r", "sub_l", "sub_r"])
    state = state_api.set_mode_routing(state, "surround", "A", ["main_l", "main_r", "sub1"])
    for (mode, role), (preset, preset_a, preset_b) in bindings.items():
        state["modes"][mode]["banks"][role] = {"preset": preset, "preset_a": preset_a, "preset_b": preset_b}
    return state_api.validate_output_state(state)


class ReleaseTests(unittest.TestCase):
    ROOM = ("Room", "Room", None)
    FREE = {"preset": "Neutral", "preset_a": "Neutral", "preset_b": None}

    def release(self, state, mode, bank_id, name="Room"):
        result, changes = state_api.release_bank_preset(state, mode, bank_id, name)
        return {(slot_mode, role): binding for slot_mode, config in result["modes"].items()
                for role, binding in config["banks"].items()}, changes

    def test_a_global_preset_is_released_in_its_own_output_mode_only(self):
        room = {**neutral_selection(), "front": "Room"}
        state = bank_state({(mode, "global"): self.ROOM for mode in ("stereo", "stereo-sub")}
                           | {("surround", "global"): (room, room, None), ("stereo-sub", "main_l"): self.ROOM})
        banks, changes = self.release(state, "stereo-sub", "global")
        self.assertEqual(banks[("stereo-sub", "global")], self.FREE)
        for slot in (("stereo", "global"), ("stereo-sub", "main_l")):
            self.assertEqual(banks[slot]["preset"], "Room")
        self.assertEqual(banks[("surround", "global")]["preset"], room)
        self.assertEqual(len(changes), 1)
        self.assertIn("stereo-sub/global", changes[0])

    def test_an_area_preset_is_released_in_every_output_mode(self):
        state = bank_state({("stereo-sub", "main_l"): self.ROOM, ("stereo-sub", "main_r"): self.ROOM,
                            ("surround", "main_l"): self.ROOM, ("stereo-sub", "global"): self.ROOM,
                            ("stereo-sub", "sub_l"): self.ROOM})
        banks, changes = self.release(state, None, "main")
        for slot in (("stereo-sub", "main_l"), ("stereo-sub", "main_r"), ("surround", "main_l")):
            self.assertEqual(banks[slot], self.FREE)
        for slot in (("stereo-sub", "global"), ("stereo-sub", "sub_l")):
            self.assertEqual(banks[slot]["preset"], "Room")
        self.assertEqual(len(changes), 3)

    def test_a_pair_bank_covers_both_of_its_roles(self):
        state = bank_state({("stereo-sub", "sub_l"): self.ROOM, ("stereo-sub", "sub_r"): self.ROOM})
        banks, _changes = self.release(state, None, "sub")
        self.assertEqual((banks[("stereo-sub", "sub_l")], banks[("stereo-sub", "sub_r")]), (self.FREE, self.FREE))

    def test_compare_rules_for_a_released_preset(self):
        for binding, expected in (
                (("Room", "Neutral", "Room"), ("Neutral", "Neutral", None)),
                (("Keep", "Room", "Keep"), ("Keep", "Neutral", "Keep")),
                (("Room", "Room", "Keep"), ("Neutral", "Neutral", "Keep")),
                (("Room", "Room", "Neutral"), ("Neutral", "Neutral", "Neutral")),
                (("Other", "Direct", "Room"), ("Other", "Direct", None)),
                (("Room", "Direct", "Keep"), ("Direct", "Direct", "Keep"))):
            with self.subTest(binding=binding):
                banks, _changes = self.release(bank_state({("stereo-sub", "global"): binding}), "stereo-sub", "global")
                self.assertEqual(tuple(banks[("stereo-sub", "global")].values()), expected)

    def test_untouched_state_reports_no_change(self):
        state = bank_state({("stereo-sub", "global"): self.ROOM})
        result, changes = state_api.release_bank_preset(state, "stereo", "global", "Room")
        self.assertEqual((result, changes), (state, []))

    def test_references_are_counted_per_bank(self):
        state = bank_state({("stereo-sub", "global"): ("GlobB", "GlobA", "GlobB"),
                            ("stereo-sub", "main_l"): ("MainA", "MainA", None),
                            ("surround", "main_r"): ("MainS", "MainS", None)})
        self.assertEqual(state_api.referenced_presets(state, "stereo-sub", "global"), {"GlobA", "GlobB"})
        self.assertEqual(state_api.referenced_presets(state, "stereo", "global"), {"Neutral"})
        self.assertEqual(state_api.referenced_presets(state, None, "main"), {"MainA", "MainS", "Neutral"})


class ApiTests(Fixture):
    def setUp(self):
        super().setUp()
        self.service = make_service(self.home, self.manager)

        async def drain(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        async def routed_roles(state, mode):
            """Device "A", as main.py derives the bank catalog's topology."""
            routing = state_api.routing_for_device(state, mode, "A")
            crossover = state["modes"][mode]["crossover_enabled"]
            return list(derive_topology(mode, routing, crossover_enabled=crossover).roles)

        previous = api._runtime.deps
        self.addCleanup(api.configure_dsp_api, previous)
        api.configure_dsp_api(api.DspApiDeps(
            require_dsp_manager=lambda: self.manager, get_dsp_manager=lambda: self.manager,
            get_dsp_runtime=lambda: None,
            dsp_mutation_lock=lambda: asyncio.Lock(), canonical_volume_write_lock=lambda: asyncio.Lock(),
            drain_worker=drain, run_locked_worker=drain, broadcast=AsyncMock(),
            restore_volume_state=AsyncMock(),
            volume_state_for_manager=lambda *args, **kwargs: {},
            schedule_peak_monitor_refresh=lambda *args, **kwargs: None,
            get_output_service=lambda: self.service,
            sync_v2_head_live=AsyncMock(return_value={"live_applied": True}),
            routed_roles=routed_roles))
        app = FastAPI()
        app.include_router(api.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def owner(self, bank, mode="stereo-sub"):
        return {"bank_mode": mode, "bank_id": bank}

    def post_peq(self, name, gain_db, bank, mode="stereo-sub", **fields):
        return self.client.post("/api/dsp/presets/create-peq", json={
            "presetName": name, "peq": peq(gain_db), **self.owner(bank, mode),
            "expected_revision": self.service.load()["revision"], **fields})

    def create_peq(self, name, gain_db, bank, mode="stereo-sub", **fields):
        response = self.post_peq(name, gain_db, bank, mode, **fields)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def route(self, *roles, mode="stereo-sub"):
        state = self.service.load()
        self.service.commit(state_api.set_mode_routing(state, mode, "A", list(roles)),
                            expected_revision=state["revision"])

    def test_one_name_can_be_created_in_every_bank_and_mode(self):
        for gain_db, (bank, mode) in zip((-1.0, -2.0, -3.0), (("global", "stereo-sub"), ("main", "stereo-sub"),
                                                                 ("global", "stereo"))):
            created = self.create_peq("Room", gain_db, bank, mode)
            self.assertTrue(created["bank"]["assigned"])
        self.assertEqual(self.stored("Room"), [("global", "stereo"), ("global", "stereo-sub"), ("main", None)])
        state = self.service.load()
        self.assertEqual(state["modes"]["stereo-sub"]["banks"]["global"]["preset"], "Room")
        self.assertEqual(state["modes"]["stereo-sub"]["banks"]["main_l"]["preset"], "Room")
        self.assertEqual(state["modes"]["stereo"]["banks"]["global"]["preset"], "Room")
        self.assertEqual(state["modes"]["stereo-sub"]["banks"]["sub1"]["preset"], "Neutral")
        listed = {(entry["bank"], entry["mode"]) for entry in self.client.get("/api/dsp/presets").json()["presets"]
                  if entry["name"] == "Room"}
        self.assertEqual(listed, {("global", "stereo"), ("global", "stereo-sub"), ("main", None)})

    def test_surround_global_filters_fill_their_group_of_the_listened_slot(self):
        self.route("main_l", "main_r", "center", "side_l", "side_r", "rear_l", "rear_r", "sub1",
                   mode="surround")
        state = self.service.load()
        self.service.commit(state_api.switch_mode(state, "surround"), expected_revision=state["revision"])
        selection = {}
        for group, name in (("front", "Front EQ"), ("center", "Center EQ"), ("side", "Side EQ"), ("rear", "Rear EQ")):
            left = [{"filterType": "bell", "frequencyHz": 100, "gainDb": -1.0, "q": 1}]
            right = [{"filterType": "bell", "frequencyHz": 200, "gainDb": -2.0, "q": 1}]
            params = {"channelMode": "stereo-linked", "eqMode": "IIR", "bands": left} if group == "center" else {
                "channelMode": "dual", "eqMode": "IIR", "leftBands": left, "rightBands": right}
            self.create_peq(name, -1.0, "global", "surround", bank_group=group,
                            peq={"enabled": True, "params": params})
            selection[group] = name
            bank = self.service.load()["modes"]["surround"]["banks"]["global"]
            self.assertEqual(bank["preset"], {**neutral_selection(), **selection})
            stored = self.manager.read_preset("surround", "global", name)["chain"][0]
            self.assertEqual(stored["group"], group)
            self.assertEqual(stored["params"]["channelMode"], params["channelMode"])
            if group == "center":
                self.assertEqual(stored["params"]["bands"][0]["frequencyHz"], 100)
                self.assertNotIn("rightBands", stored["params"])
            else:
                self.assertEqual(stored["params"]["leftBands"][0]["frequencyHz"], 100)
                self.assertEqual(stored["params"]["rightBands"][0]["frequencyHz"], 200)
        bank = self.service.load()["modes"]["surround"]["banks"]["global"]
        self.assertEqual(bank["preset"], selection)
        self.assertEqual(bank["preset_a"], bank["preset"])
        listed = {entry["name"]: entry["group"] for entry in self.client.get("/api/dsp/presets").json()["presets"]
                   if entry["mode"] == "surround"}
        self.assertEqual(listed, {"Front EQ": "front", "Center EQ": "center", "Side EQ": "side", "Rear EQ": "rear"})
        plan = self.service.compile_plan(self.service.load(), output_key="A", channels=16, sample_rate_hz=48000)
        text = self.manager.compile_engine_text(self.service.compile_layout(plan), preset_name=selection, mode="surround")
        self.assertEqual([line for line in text.splitlines() if line.startswith("pairs ")],
                         ["pairs 1", "pairs 2", "pairs 8", "pairs 16"])

    def test_surround_global_imports_belong_to_the_chosen_group(self):
        """An imported file is tagged with the group chosen for it, like a measured filter."""
        self.route("main_l", "main_r", "rear_l", "rear_r", "sub1", mode="surround")
        state = self.service.load()
        self.service.commit(state_api.switch_mode(state, "surround"), expected_revision=state["revision"])
        self.create_peq("Rear EQ", -2.0, "global", "surround", bank_group="rear")
        rear_file = self.manager.export_preset_json("Rear EQ", bank="global", mode="surround")
        self.create("Stereo EQ", -3.0, "global", "stereo")
        stereo_file = self.manager.export_preset_json("Stereo EQ", bank="global", mode="stereo")

        def post(endpoint, name, content, media, **group):
            return self.client.post(f"/api/dsp/presets/{endpoint}", data={
                "bank_mode": "surround", "bank_id": "global", **group,
                "expected_revision": str(self.service.load()["revision"])},
                files={"file": (name, content, media)})

        refused = post("import-json", "No group.json", rear_file.encode(), "application/json")
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertIn("speaker group", refused.text)
        # The chosen group replaces the file's own tag, and tags an untagged file.
        for name, content, group in (("Front copy", rear_file, "front"), ("Rear stereo", stereo_file, "rear")):
            response = post("import-json", f"{name}.json", content.encode(), "application/json", bank_group=group)
            self.assertEqual(response.status_code, 200, response.text)
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("preset.json", stereo_file)
        response = post("import-bundle", "Front bundle.zip", bundle.getvalue(), "application/zip", bank_group="front")
        self.assertEqual(response.status_code, 200, response.text)
        listed = {entry["name"]: entry["group"] for entry in self.client.get("/api/dsp/presets").json()["presets"]
                  if entry["mode"] == "surround"}
        self.assertEqual(listed, {"Rear EQ": "rear", "Front copy": "front", "Rear stereo": "rear",
                                  "Front bundle": "front"})
        for name, group in (("Front copy", "front"), ("Rear stereo", "rear")):
            payload = self.manager.read_preset("surround", "global", name)
            self.assertEqual({plugin["group"] for plugin in payload["chain"]}, {group})
        # Each import takes only its own group's place in the listened slot.
        self.assertEqual(self.service.load()["modes"]["surround"]["banks"]["global"]["preset"],
                         {**neutral_selection(), "front": "Front bundle", "rear": "Rear stereo"})

    def test_surround_group_deletes_preserve_unselected_presets_and_other_side(self):
        self.route("main_l", "main_r", "center", "side_l", "side_r", "rear_l", "rear_r", "sub1",
                   mode="surround")
        state = self.service.load()
        self.service.commit(state_api.switch_mode(state, "surround"), expected_revision=state["revision"])
        groups = {"front": "Front A", "center": "Center A", "side": "Side A", "rear": "Rear A"}
        for group, name in groups.items():
            self.create_peq(name, -1.0, "global", "surround", bank_group=group)
        self.manager.create_peq_preset("Rear B", peq(-2.0), bank="global", mode="surround", group="rear")
        state = self.service.load()
        state = state_api.set_bank_preset(state, "surround", "global", preset_b="Rear B", group="rear")
        self.service.commit(state, expected_revision=state["revision"])
        inactive = copy.deepcopy(self.service.load()["modes"]["surround"]["banks"]["global"]["preset_b"])
        # Delete each group separately, including Rear while Front is Neutral.
        remaining = dict(groups)
        for group in ("front", "center", "side", "rear"):
            with self.subTest(group=group):
                response = self.client.post("/api/dsp/presets/delete", json={
                    "preset_name": groups[group], **self.owner("global", "surround")})
                self.assertEqual(response.status_code, 200, response.text)
                remaining[group] = "Neutral"
                bank = self.service.load()["modes"]["surround"]["banks"]["global"]
                self.assertEqual(bank["preset"], remaining)
                self.assertEqual(bank["preset_a"], remaining)
                inactive = {key: "Neutral" if value == groups[group] else value for key, value in inactive.items()}
                self.assertEqual(bank["preset_b"], inactive)
                names = {entry["name"] for entry in self.manager.list_presets()}
                self.assertNotIn(groups[group], names)
                self.assertIn("Rear B", names)
                self.assertTrue(set(remaining.values()) - {"Neutral"} <= names)

    def test_a_lone_sub_l_lists_its_presets_and_irs(self):
        """Stereo + Sub with one mono sub on sub_l: its bank is sub_l, not the Sub L/R pair."""
        state = self.service.load()
        self.service.commit(state_api.set_mode_routing(state, "stereo-sub", "A", ["main_l", "main_r", "sub_l"]),
                            expected_revision=state["revision"])
        self.create_peq("Room", -1.0, "sub_l")
        response = self.client.post("/api/dsp/presets/create-with-ir", data={
            "preset_name": "Conv", **self.owner("sub_l"),
            "expected_revision": str(self.service.load()["revision"])},
            files={"file": ("upload.wav", mono_ir(0.5, 0.0), "audio/wav")})
        self.assertEqual(response.status_code, 200, response.text)
        status = self.client.get("/api/dsp/presets").json()
        self.assertEqual([(entry["name"], entry["bank"]) for entry in status["presets"] if entry["bank"]],
                         [("Direct", "global"), ("Conv", "sub_l"), ("Room", "sub_l")])
        self.assertEqual([(ir["name"], ir["bank"]) for ir in status["irs"]], [("Conv.wav", "sub_l")])
        self.assertEqual(self.service.load()["modes"]["stereo-sub"]["banks"]["sub_l"]["preset"], "Conv")

    def test_a_role_whose_pair_partner_is_unrouted_is_its_own_bank(self):
        """Stored bindings outlive routing; only the routed roles form banks."""
        self.route("main_l", "main_r", "sub_l", "sub_r")
        self.create_peq("Pair", -1.0, "sub")
        self.route("main_l", "main_r", "sub_l", "off")
        created = self.create_peq("Lone", -2.0, "sub_l")
        self.assertEqual(created["bank"]["bank_id"], "sub_l")
        self.assertEqual(self.stored("Lone"), [("sub_l", None)])
        banks = self.service.load()["modes"]["stereo-sub"]["banks"]
        self.assertEqual((banks["sub_l"]["preset"], banks["sub_r"]["preset"]), ("Lone", "Pair"))
        plan = self.service.compile_plan(self.service.load(), output_key="A", channels=4, sample_rate_hz=48000)
        self.assertEqual([(item["role"], item["bank"]["library"]) for item in plan["outputs"]],
                         [("main_l", "main"), ("main_r", "main"), ("sub_l", "sub_l")])
        # The temporarily unrouted pair is no target; its library stays as it is.
        refused = self.post_peq("Other", -3.0, "sub")
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertEqual(self.stored("Other"), [])
        self.assertEqual(self.stored("Pair"), [("sub", None)])
        deleted = self.client.post("/api/dsp/presets/delete", json={"preset_name": "Lone", **self.owner("sub_l")})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.stored("Lone"), [])
        self.route("main_l", "main_r", "sub_l", "sub_r")
        self.assertEqual(self.create_peq("Back", -4.0, "sub_l")["bank"]["bank_id"], "sub")

    def test_a_created_bank_preset_is_activated_by_its_bank_assignment(self):
        for bank, mode in (("main", "stereo-sub"), ("sub1", "stereo-sub"), ("global", "stereo-sub")):
            with self.subTest(bank=bank, mode=mode):
                created = self.create_peq("Room", -2.0, bank, mode)
                self.assertTrue(created["bank"]["assigned"])
        banks = self.service.load()["modes"]["stereo-sub"]["banks"]
        self.assertEqual({role: banks[role]["preset"] for role in ("global", "main_l", "main_r", "sub1")},
                         dict.fromkeys(("global", "main_l", "main_r", "sub1"), "Room"))

    def test_combine_creates_in_the_owners_bank_without_activating(self):
        for name in ("A", "B"):
            self.create_peq(name, -1.0, "main", "stereo-sub")
        before = self.service.load()
        combined = self.client.post("/api/dsp/presets/combine", json={
            "presetName": "Both", "presetNames": ["A", "B"], **self.owner("main")})
        self.assertEqual(combined.status_code, 200, combined.text)
        self.assertEqual(self.stored("Both"), [("main", None)])
        self.assertEqual(self.service.load(), before)

    def test_download_serves_the_named_banks_preset(self):
        self.create_peq("Room", -1.0, "global")
        self.create_peq("Room", -2.0, "main")
        for bank, expected in (("global", -1.0), ("main", -2.0)):
            response = self.client.get("/api/dsp/presets/Room/file", params=self.owner(bank))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(gain(response.json()), expected)
        self.assertEqual(self.client.get("/api/dsp/presets/Room/file", params=self.owner("sub1")).status_code, 404)
        self.assertEqual(self.client.get("/api/dsp/presets/Room/file").status_code, 404)
        builtin = self.client.get("/api/dsp/presets/Neutral/file", params=self.owner("sub1"))
        self.assertEqual((builtin.status_code, builtin.json()["chain"]), (200, []))
        self.assertIn('filename="Neutral.json"', builtin.headers["content-disposition"])

    def test_download_bundles_the_banks_own_ir(self):
        for bank, sample in (("main", 0.5), ("sub1", 0.25)):
            response = self.client.post("/api/dsp/presets/create-with-ir", data={
                "preset_name": "Room", **self.owner(bank),
                "expected_revision": str(self.service.load()["revision"])},
                files={"file": ("upload.wav", mono_ir(sample, 0.0), "audio/wav")})
            self.assertEqual(response.status_code, 200, response.text)
        for bank, sample in (("main", 0.5), ("sub1", 0.25)):
            response = self.client.get("/api/dsp/presets/Room/file", params=self.owner(bank))
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                self.assertEqual(sorted(archive.namelist()), ["Room.wav", "manifest.json", "preset.json"])
                self.assertEqual(archive.read("Room.wav"), mono_ir(sample, 0.0))

    def test_bundle_import_writes_the_ir_into_the_target_bank(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("preset.json", json.dumps({
                "schema": "fxroute.dsp.preset", "version": 1, "metadata": {},
                "chain": [{"id": "convolver#0", "type": "convolver", "enabled": True,
                           "params": {"kernel": "Kernel", "wet_db": 0.0, "dry_db": -100.0,
                                      "input_gain_db": 0.0, "output_gain_db": 0.0}}]}))
            archive.writestr("Kernel.wav", mono_ir(0.5, 0.0))
        response = self.client.post("/api/dsp/presets/import-bundle", data={
            **self.owner("sub1"),
            "expected_revision": str(self.service.load()["revision"])},
            files={"file": ("Bundle.zip", buffer.getvalue(), "application/zip")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.stored("Bundle"), [("sub1", None)])
        self.assertEqual([item["name"] for item in self.manager.list_irs("sub1")], ["Kernel.wav"])
        self.assertEqual(self.manager.list_irs(), [])

    def test_combine_and_delete_address_the_named_bank(self):
        for name, gain_db in (("A", -1.0), ("B", -2.0)):
            self.create_peq(name, gain_db, "main")
        self.create_peq("A", -5.0, "global")
        refused = self.client.post("/api/dsp/presets/combine", json={
            "presetName": "Both", "presetNames": ["A", "B"], **self.owner("global")})
        self.assertEqual(refused.status_code, 404, refused.text)
        combined = self.client.post("/api/dsp/presets/combine", json={
            "presetName": "Both", "presetNames": ["A", "B"], **self.owner("main")})
        self.assertEqual(combined.status_code, 200, combined.text)
        self.assertEqual(self.stored("Both"), [("main", None)])
        # Main listens to B; deleting Global's "A" leaves Main's presets and slots alone.
        deleted = self.client.post("/api/dsp/presets/delete", json={"preset_name": "A", **self.owner("global")})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.stored("A"), [("main", None)])
        banks = self.service.load()["modes"]["stereo-sub"]["banks"]
        self.assertEqual(banks["global"], {"preset": "Neutral", "preset_a": "Neutral", "preset_b": "Neutral"})
        self.assertEqual(banks["main_l"]["preset"], "B")
        deleted = self.client.post("/api/dsp/presets/delete", json={"preset_name": "B", **self.owner("main")})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.service.load()["modes"]["stereo-sub"]["banks"]["main_r"]["preset"], "Neutral")
        self.assertEqual(self.client.post("/api/dsp/presets/delete", json={
            "preset_name": "B", **self.owner("main")}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
