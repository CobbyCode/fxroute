#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Every filterbank has its own preset library; Global has one per output mode."""

import asyncio
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
from dsp.manager import DSPManager, build_wav_bytes, parse_wav_frames
from dsp.persistence import clean_bank

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
    def test_bank_ids_are_folder_safe(self):
        self.assertEqual(clean_bank("low"), "low")
        self.assertEqual(clean_bank("  low_mid  "), "low_mid")
        for invalid in ("", None, "a/b", ".."):
            self.assertIsNone(clean_bank(invalid))
        for bank, mode in (("a/b", None), ("..", None), ("global", "mono")):
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

    def test_builtins_exist_in_every_bank_without_a_file(self):
        self.assertEqual([(entry["name"], entry["bank"], entry["mode"]) for entry in self.manager.list_presets()],
                         [("Direct", None, None), ("Neutral", None, None)])
        for bank, mode in BANKS:
            for name in ("Direct", "Neutral"):
                self.assertEqual(self.manager.library(bank, mode).read(name)["chain"], [])
            with self.assertRaisesRegex(ValueError, "built-in preset"):
                self.create("Neutral", -1.0, bank, mode)
        self.assertEqual(list(self.manager.output_dir.iterdir()), [])

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

    def test_only_the_stereo_global_preset_is_the_active_one(self):
        for bank, mode in (("global", "stereo"), STEREO_SUB, ("sub1", None)):
            self.create("Room", -1.0, bank, mode)
        self.manager.load_preset("Room")
        for bank, mode in (STEREO_SUB, ("sub1", None)):
            self.assertFalse(self.manager.is_active_preset("Room", bank, mode))
            self.manager.delete_preset("Room", bank=bank, mode=mode)
            self.assertEqual(self.manager.get_active_preset(), "Room")
        self.assertTrue(self.manager.is_active_preset("Room"))
        self.manager.delete_preset("Room")
        self.assertEqual(self.manager.get_active_preset(), "Neutral")
        # A restart keeps an active preset only while Stereo Global still has it.
        self.create("Room", -1.0, "sub1")
        self.manager.state_store.write("active.json", {"schema": "fxroute.dsp.active", "version": 1, "preset": "Room"})
        self.assertEqual(DSPManager(home=self.home).get_active_preset(), "Neutral")


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
        for name in ("Direct", "Neutral"):
            for bank in ("global", "main", "sub1"):
                self.service.validate_bank_preset(state, "stereo-sub", bank, name)

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
        state = bank_state({(mode, "global"): self.ROOM for mode in ("stereo", "stereo-sub", "surround")}
                           | {("stereo-sub", "main_l"): self.ROOM})
        banks, changes = self.release(state, "stereo-sub", "global")
        self.assertEqual(banks[("stereo-sub", "global")], self.FREE)
        for slot in (("stereo", "global"), ("surround", "global"), ("stereo-sub", "main_l")):
            self.assertEqual(banks[slot]["preset"], "Room")
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
                (("Room", "Room", "Neutral"), ("Neutral", "Neutral", None)),
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

        previous = api._runtime.deps
        self.addCleanup(api.configure_dsp_api, previous)
        api.configure_dsp_api(api.DspApiDeps(
            require_dsp_manager=lambda: self.manager, get_dsp_manager=lambda: self.manager,
            get_dsp_runtime=lambda: None, get_dsp_preset_load_lock=lambda: asyncio.Lock(),
            dsp_mutation_lock=lambda: asyncio.Lock(), canonical_volume_write_lock=lambda: asyncio.Lock(),
            drain_worker=drain, run_locked_worker=drain, broadcast=AsyncMock(),
            load_dsp_preset=AsyncMock(), restore_volume_state=AsyncMock(),
            volume_state_for_manager=lambda *args, **kwargs: {},
            schedule_peak_monitor_refresh=lambda *args, **kwargs: None,
            get_output_service=lambda: self.service,
            sync_v2_head_live=AsyncMock(return_value={"live_applied": True})))
        app = FastAPI()
        app.include_router(api.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def owner(self, bank, mode="stereo-sub"):
        return {"bank_mode": mode, "bank_id": bank}

    def create_peq(self, name, gain_db, bank, mode="stereo-sub"):
        response = self.client.post("/api/dsp/presets/create-peq", json={
            "presetName": name, "peq": peq(gain_db), **self.owner(bank, mode),
            "expected_revision": self.service.load()["revision"]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

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
                "preset_name": "Room", "load_after_create": "false", **self.owner(bank),
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
            "load_after_create": "false", **self.owner("sub1"),
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
        self.assertEqual(banks["global"], {"preset": "Neutral", "preset_a": "Neutral", "preset_b": None})
        self.assertEqual(banks["main_l"]["preset"], "B")
        deleted = self.client.post("/api/dsp/presets/delete", json={"preset_name": "B", **self.owner("main")})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.service.load()["modes"]["stereo-sub"]["banks"]["main_r"]["preset"], "Neutral")
        self.assertEqual(self.client.post("/api/dsp/presets/delete", json={
            "preset_name": "B", **self.owner("main")}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
