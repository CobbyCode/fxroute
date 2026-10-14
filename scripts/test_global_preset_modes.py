#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Every output mode owns its Global presets: tagging, assignment, split by use."""

import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio import output_state as state_api
from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state_migration import global_copy_name, split_shared_global_presets
from audio.output_state_store import OutputStateStore
from dsp.manager import DSPManager
from dsp.persistence import preset_mode

PEQ = {"enabled": True, "params": {"channelMode": "stereo-linked", "bands": [
    {"filterType": "bell", "frequencyHz": 100, "gainDb": -2, "q": 1},
]}}
COPY = "Shared (Stereo + Sub)"


def make_ir(manager: DSPManager, name: str = "room.wav") -> str:
    path = manager.irs_dir / name
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00\x00\x00\x40")
    return path.name


def make_service(home: Path, manager: DSPManager, *, commit: bool = True) -> OutputService:
    store = OutputStateStore(home / "out.json")
    service = OutputService(OutputServiceDeps(
        store=store, preset_loader=lambda name: manager.preset_store.read(name),
        resolve_ir=lambda kernel: {}, measurement_active=lambda: False,
        legacy_snapshot_loader=None))
    if commit:
        state = state_api.set_mode_routing(
            state_api.default_output_state(), "stereo-sub", "A", ["main_l", "main_r", "sub1"])
        store.commit(state, expected_revision=0)
    return service


class GlobalPresetOwnerTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.manager = DSPManager(home=self.home)
        self.service = make_service(self.home, self.manager)

    def listed(self):
        return {entry["name"]: entry["mode"] for entry in self.manager.list_presets()}

    def test_global_preset_is_tagged_with_the_mode_it_was_created_in(self):
        ir = make_ir(self.manager)
        self.manager.create_peq_preset("SubPeq", PEQ, bank="global", mode="stereo-sub")
        self.manager.create_convolver_preset("SubConv", ir, bank="global", mode="stereo-sub")
        self.manager.create_convolver_preset_with_upload(
            "SubUpload", self.manager.irs_dir / ir, ir, bank="global", mode="stereo-sub")
        self.manager.create_peq_preset("StereoPeq", PEQ, bank="global", mode="stereo")
        self.manager.create_peq_preset("FrontPeq", PEQ, bank="global", group="front", mode="surround")
        self.manager.create_peq_preset("MainPeq", PEQ, bank="main", mode="stereo-sub")
        self.manager.combine_presets("SubCombo", ["SubPeq", "SubConv"], bank="global", mode="stereo-sub")
        self.assertEqual(self.listed(), {
            "Direct": None, "Neutral": None, "SubPeq": "stereo-sub", "SubConv": "stereo-sub",
            "SubUpload": "stereo-sub", "StereoPeq": "stereo", "FrontPeq": "surround",
            "MainPeq": None, "SubCombo": "stereo-sub"})
        self.assertNotIn("mode", self.manager.preset_store.read("MainPeq")["metadata"])

    def test_global_preset_fits_only_the_global_bank_of_its_mode(self):
        self.manager.create_peq_preset("SubPeq", PEQ, bank="global", mode="stereo-sub")
        self.manager.create_peq_preset("StereoPeq", PEQ, bank="global", mode="stereo")
        state = self.service.load()
        self.service.validate_bank_preset(state, "stereo-sub", "global", "SubPeq")
        self.service.validate_bank_preset(state, "stereo", "global", "StereoPeq")
        with self.assertRaisesRegex(ValueError, "belongs to the Stereo Global bank, not Stereo \\+ Sub"):
            self.service.validate_bank_preset(state, "stereo-sub", "global", "StereoPeq")
        with self.assertRaisesRegex(ValueError, "belongs to the Stereo \\+ Sub Global bank, not Stereo"):
            self.service.validate_bank_preset(state, "stereo", "global", "SubPeq")
        for mode in ("stereo", "stereo-sub", "surround"):
            for builtin in ("Direct", "Neutral"):
                self.service.validate_bank_preset(state, mode, "global", builtin)

    def test_a_name_taken_in_one_modes_global_bank_is_not_rewritten_from_another(self):
        ir = make_ir(self.manager)
        self.manager.create_peq_preset("Room", PEQ, bank="global", mode="stereo")
        before = self.manager.preset_store.read("Room")
        for create in (
                lambda: self.manager.create_peq_preset("Room", PEQ, bank="global", mode="stereo-sub"),
                lambda: self.manager.create_convolver_preset("Room", ir, bank="global", mode="stereo-sub"),
                lambda: self.manager.import_preset_json("Room.json", json.dumps(before), bank="global",
                                                        mode="stereo-sub")):
            with self.assertRaisesRegex(ValueError, 'belongs to the Stereo Global bank, not Stereo \\+ Sub'):
                create()
        irs_before = sorted(path.name for path in self.manager.irs_dir.iterdir())
        with self.assertRaisesRegex(ValueError, "belongs to the Stereo Global bank"):
            self.manager.create_convolver_preset_with_upload(
                "Room", self.manager.irs_dir / ir, ir, bank="global", mode="stereo-sub")
        self.assertEqual(sorted(path.name for path in self.manager.irs_dir.iterdir()), irs_before)
        self.assertEqual(self.manager.preset_store.read("Room"), before)
        # The owning mode may rewrite it, and callers without a mode keep the owner.
        self.manager.create_peq_preset("Room", PEQ, bank="global", mode="stereo")
        self.manager.create_peq_preset("Room", PEQ, bank="global")
        self.assertEqual(self.listed()["Room"], "stereo")

    def test_import_joins_the_global_bank_it_is_imported_into(self):
        self.manager.create_peq_preset("Room", PEQ, bank="global", mode="stereo")
        exported = self.manager.export_preset_json("Room")
        self.manager.import_preset_json("Room Sub.json", exported, bank="global", mode="stereo-sub")
        self.assertEqual(self.listed()["Room Sub"], "stereo-sub")
        # Without a target the exported owner is kept.
        self.manager.import_preset_json("Room Copy.json", exported)
        self.assertEqual(self.listed()["Room Copy"], "stereo")

    def test_surround_layout_and_mode_must_agree(self):
        self.manager.create_peq_preset("FrontPeq", PEQ, bank="global", group="front", mode="surround")
        surround = self.manager.export_preset_json("FrontPeq")
        with self.assertRaisesRegex(ValueError, "fits only the Surround Global bank"):
            self.manager.import_preset_json("FrontSub.json", surround, bank="global", mode="stereo-sub")
        with self.assertRaisesRegex(ValueError, "need a speaker group"):
            self.manager.create_peq_preset("Plain", PEQ, bank="global", mode="surround")
        with self.assertRaisesRegex(ValueError, "Unsupported output mode"):
            self.manager.create_peq_preset("Plain", PEQ, bank="global", mode="quad")
        payload = self.manager.preset_store.read("FrontPeq")
        payload["metadata"]["mode"] = "stereo"
        with self.assertRaisesRegex(ValueError, "different output modes"):
            self.manager.preset_store.write("FrontPeq", payload)
        self.assertNotIn("FrontSub", self.listed())
        self.assertNotIn("Plain", self.listed())

    def test_untagged_global_preset_reads_as_stereo_or_surround(self):
        plain = {"schema": DSPManager.PRESET_SCHEMA, "version": DSPManager.PRESET_VERSION, "chain": []}
        self.assertEqual(preset_mode({**plain, "metadata": {"bank": "global"}}), "stereo")
        self.assertEqual(preset_mode({**plain, "metadata": {"bank": "global", "layout": "surround"}}), "surround")
        self.assertEqual(preset_mode({**plain, "metadata": {"bank": "global", "mode": "stereo-sub"}}), "stereo-sub")
        self.assertIsNone(preset_mode({**plain, "metadata": {"bank": "main", "mode": "stereo-sub"}}))
        self.assertIsNone(preset_mode({**plain, "metadata": {}}))


class GlobalPresetSplitTests(unittest.TestCase):
    """Presets from before the split are divided by the slots that use them."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.manager = DSPManager(home=self.home)
        self.service = make_service(self.home, self.manager)

    def legacy(self, name: str, *, layout: str | None = None, chain=None):
        """A Global preset as stored before every mode owned its own."""
        metadata = {"bank": "global", **({"layout": layout} if layout else {})}
        self.manager.preset_store.write(name, {
            "schema": DSPManager.PRESET_SCHEMA, "version": DSPManager.PRESET_VERSION,
            "chain": chain if chain is not None else [
                {"id": "equalizer#0", "type": "equalizer", "enabled": True, "params": {"tag": name},
                 **({"group": "front"} if layout else {})}],
            "metadata": metadata})

    def bind(self, mode: str, **slots):
        state = self.service.load()
        state["modes"][mode]["banks"]["global"] = {"preset": "Neutral", "preset_a": "Neutral",
                                                   "preset_b": None, **slots}
        return self.service.commit(state, expected_revision=state["revision"])

    def modes(self):
        return {entry["name"]: entry["mode"] for entry in self.manager.list_presets()
                if entry["name"] not in ("Direct", "Neutral")}

    def split(self):
        return self.service.assign_global_preset_modes(self.manager.preset_store)

    def test_presets_go_to_the_mode_that_uses_them(self):
        for name in ("Shared", "OnlySub", "OnlyStereo", "Unused"):
            self.legacy(name)
        self.legacy("Front", layout="surround")
        self.manager.create_peq_preset("MainPeq", PEQ, bank="main")
        self.bind("stereo", preset="Shared", preset_b="Shared")
        self.bind("stereo-sub", preset_a="OnlySub", preset_b="Shared")
        state = self.service.load()
        state["modes"]["stereo"]["banks"]["global"]["preset_a"] = "OnlyStereo"
        before = self.service.commit(state, expected_revision=state["revision"])

        changes = self.split()

        self.assertEqual(self.modes(), {
            "Shared": "stereo", COPY: "stereo-sub", "OnlySub": "stereo-sub",
            "OnlyStereo": "stereo", "Unused": "stereo", "Front": "surround", "MainPeq": None})
        after = self.service.load()
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(after["modes"]["stereo"]["banks"]["global"],
                         {"preset": "Shared", "preset_a": "OnlyStereo", "preset_b": "Shared"})
        self.assertEqual(after["modes"]["stereo-sub"]["banks"]["global"],
                         {"preset": "Neutral", "preset_a": "OnlySub", "preset_b": COPY})
        original, copied = (self.manager.preset_store.read(name) for name in ("Shared", COPY))
        self.assertEqual(copied["chain"], original["chain"])
        self.assertEqual(copied["metadata"], {"bank": "global", "mode": "stereo-sub"})
        self.assertEqual(len(changes), 6)
        self.assertTrue(any(COPY in change for change in changes))
        # Every binding still fits its bank, and nothing is left to split.
        for mode in ("stereo", "stereo-sub"):
            for name in set(after["modes"][mode]["banks"]["global"].values()) - {None}:
                self.service.validate_bank_preset(after, mode, "global", name)
        self.assertEqual(self.split(), [])
        self.assertEqual(self.service.load()["revision"], after["revision"])

    def test_listened_slot_follows_its_preset_to_the_copy(self):
        self.legacy("Shared")
        self.bind("stereo", preset="Shared", preset_a="Shared")
        self.bind("stereo-sub", preset="Shared", preset_b="Shared")
        self.split()
        self.assertEqual(self.service.load()["modes"]["stereo-sub"]["banks"]["global"],
                         {"preset": COPY, "preset_a": "Neutral", "preset_b": COPY})

    def test_copy_shares_the_ir_and_survives_deleting_the_original(self):
        ir = make_ir(self.manager, "Shared.wav")
        self.legacy("Shared", chain=[{"id": "convolver#0", "type": "convolver", "enabled": True,
                                      "params": {"kernel": "Shared"}}])
        self.bind("stereo", preset_b="Shared")
        self.bind("stereo-sub", preset_b="Shared")
        self.split()
        self.manager.delete_preset("Shared")
        self.assertTrue((self.manager.irs_dir / ir).is_file())
        self.assertEqual(self.manager.preset_store.read(COPY)["chain"][0]["params"]["kernel"], "Shared")

    def test_interrupted_split_resumes_with_the_copy_it_wrote(self):
        self.legacy("Shared")
        self.bind("stereo", preset_b="Shared")
        self.bind("stereo-sub", preset_b="Shared")
        written = self.manager.preset_store.read("Shared")
        written["metadata"] = {**written["metadata"], "mode": "stereo-sub"}
        self.manager.preset_store.write(COPY, written)
        self.split()
        self.assertEqual(self.modes(), {"Shared": "stereo", COPY: "stereo-sub"})
        self.assertEqual(self.service.load()["modes"]["stereo-sub"]["banks"]["global"]["preset_b"], COPY)

    def test_taken_copy_name_is_not_overwritten(self):
        self.legacy("Shared")
        self.manager.create_peq_preset(COPY, PEQ, bank="global", mode="stereo")
        foreign = self.manager.preset_store.read(COPY)
        self.bind("stereo", preset_b="Shared")
        self.bind("stereo-sub", preset_b="Shared")
        self.split()
        self.assertEqual(self.manager.preset_store.read(COPY), foreign)
        self.assertEqual(self.service.load()["modes"]["stereo-sub"]["banks"]["global"]["preset_b"],
                         f"{COPY} 2")
        self.assertEqual(self.modes()[f"{COPY} 2"], "stereo-sub")

    def test_without_an_output_state_presets_stay_with_stereo(self):
        home = Path(tempfile.mkdtemp())
        manager = DSPManager(home=home)
        service = make_service(home, manager, commit=False)
        manager.preset_store.write("Old", {
            "schema": DSPManager.PRESET_SCHEMA, "version": DSPManager.PRESET_VERSION,
            "chain": [], "metadata": {"bank": "global"}})
        self.assertEqual(len(service.assign_global_preset_modes(manager.preset_store)), 1)
        self.assertEqual(manager.preset_store.read("Old")["metadata"]["mode"], "stereo")
        self.assertFalse((home / "out.json").exists())

    def test_planner_is_pure_and_names_copies_uniquely(self):
        state = self.service.load()
        state["modes"]["stereo"]["banks"]["global"]["preset_b"] = "A"
        state["modes"]["stereo-sub"]["banks"]["global"]["preset_b"] = "A"
        snapshot = json.dumps(state, sort_keys=True)
        split, owners, copies = split_shared_global_presets(state, ["A", "B"], {"A", "B", "A (Stereo + Sub)"})
        self.assertEqual(json.dumps(state, sort_keys=True), snapshot)
        self.assertEqual(owners, {"A": "stereo", "B": "stereo"})
        self.assertEqual(copies, {"A": "A (Stereo + Sub) 2"})
        self.assertEqual(split["modes"]["stereo-sub"]["banks"]["global"]["preset_b"], "A (Stereo + Sub) 2")
        self.assertEqual(split["modes"]["stereo"]["banks"]["global"]["preset_b"], "A")
        self.assertEqual(global_copy_name("A", ()), "A (Stereo + Sub)")


if __name__ == "__main__":
    unittest.main()
