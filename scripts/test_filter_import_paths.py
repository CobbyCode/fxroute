#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Multipart filter imports preserve values, channel projection and bank isolation."""

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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import dsp.api as api
from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state import default_output_state, set_crossover, set_mode_routing, switch_mode
from audio.output_state_store import OutputStateStore
from dsp.manager import DSPManager, parse_wav_frames
from test_dual_ir_wav_formats import wav_bytes


LEFT_REW = "Equaliser: Generic\n1 True Auto PK 46.30 -4.80 3.387\n2 off PK 100 2 1\n"
RIGHT_REW = "Filter Settings file\nFilter 1: ON PK Fc 1234 Hz Gain -2.5 dB Q 1.25\n"


class FilterImportPathsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.manager = DSPManager(home=self.home)
        self.service = OutputService(OutputServiceDeps(
            store=OutputStateStore(self.home / "output.json"),
            preset_loader=self.manager.preset_store.read,
            resolve_ir=self.resolve_ir, measurement_active=lambda: False))

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

    def resolve_ir(self, kernel):
        path = self.manager._resolve_kernel_path(kernel)
        return {"path": str(path), "channels": parse_wav_frames(path)["channels"]}

    def seed(self, roles, crossover=False):
        current = self.service.load()
        state = set_crossover(default_output_state(), "stereo-sub", crossover)
        state = switch_mode(set_mode_routing(state, "stereo-sub", "A", roles), "stereo-sub")
        state["revision"] = current["revision"]
        self.service.commit(state, expected_revision=current["revision"])

    def request_import(self, kind, bank, name, revision=None):
        data = {"preset_name": name, "load_after_create": "false", "bank_mode": "stereo-sub",
                "bank_id": bank, "expected_revision": str(self.service.load()["revision"] if revision is None else revision)}
        mono = bank in ("sub1", "sub2")
        if kind in ("wav", "irs", "float64"):
            samples = [0.5, -0.125] if mono else [0.5, 0.25, -0.125, -0.0625]
            files = {"file": (name + (".irs" if kind == "irs" else ".wav"),
                     wav_bytes(samples, bits=64 if kind == "float64" else 32,
                               channels=1 if mono else 2), "audio/wav")}
            endpoint = "create-with-ir"
        elif kind == "dual-ir":
            files = {"left_file": ("left.wav", wav_bytes([0.5, -0.125]), "audio/wav"),
                     "right_file": ("right.irs", wav_bytes([0.25]), "audio/wav")}
            endpoint = "import-filter-dual"
        elif kind == "rew-mono":
            files = {"file": ("rew.txt", LEFT_REW.encode(), "text/plain")}
            endpoint = "import-rew-peq"
        elif kind == "rew-files":
            files = {"left_file": ("left.txt", b"\xef\xbb\xbf" + LEFT_REW.encode(), "text/plain"),
                     "right_file": ("right.txt", RIGHT_REW.encode(), "text/plain")}
            endpoint = "import-filter-dual"
        elif kind == "json":
            payload = json.dumps({"schema": "fxroute.dsp.preset", "version": 1,
                                  "chain": [], "metadata": {"bank": "global"}})
            files = {"file": (name + ".json", payload.encode(), "application/json")}
            endpoint = "import-json"
        elif kind == "bundle":
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("preset.json", json.dumps(
                    {"schema": "fxroute.dsp.preset", "version": 1,
                     "chain": [], "metadata": {}}))
            files = {"file": (name + ".zip", buffer.getvalue(), "application/zip")}
            endpoint = "import-bundle"
        else:
            data.update(left_text=LEFT_REW, right_text=RIGHT_REW)
            files = None
            endpoint = "import-filter-dual"
        return self.client.post("/api/dsp/presets/" + endpoint, data=data, files=files)

    def test_import_matrix_preserves_bank_values_and_other_outputs(self):
        topologies = [
            (["main_l", "main_r", "sub1", "sub2"], False,
             {"global": ["global"], "main": ["main_l", "main_r"], "sub1": ["sub1"], "sub2": ["sub2"]}),
            ([f"{side}_{way}" for side in ("left", "right") for way in ("low", "low_mid", "mid", "high")], True,
             {way: [f"left_{way}", f"right_{way}"] for way in ("low", "low_mid", "mid", "high")}),
            (["main_l", "main_r", "sub_l", "sub_r"], False, {"sub": ["sub_l", "sub_r"]}),
        ]
        for roles, crossover, targets in topologies:
            for bank, target_roles in targets.items():
                kinds = ("wav", "irs", "float64", "rew-mono", "json", "bundle")
                if bank not in ("sub1", "sub2"):
                    kinds += ("dual-ir", "rew-files", "rew-paste")
                for kind in kinds:
                    with self.subTest(bank=bank, kind=kind):
                        self.seed(roles, crossover)
                        before = self.service.load()
                        name = f"Review-{bank}-{kind}"
                        response = self.request_import(kind, bank, name)
                        self.assertEqual(response.status_code, 200, response.text)
                        result = response.json()
                        self.assertTrue(result["bank"]["assigned"])
                        after = self.service.load()
                        expected = copy.deepcopy(before)
                        expected["revision"] = after["revision"]
                        for role in target_roles:
                            expected["modes"]["stereo-sub"]["banks"][role]["preset"] = name
                            expected["modes"]["stereo-sub"]["banks"][role]["preset_a"] = name
                        self.assertEqual(after, expected)
                        stored = self.manager.preset_store.read(name)
                        self.assertEqual(stored["metadata"]["bank"], bank)
                        if kind in ("json", "bundle"):
                            self.assertEqual(stored["chain"], [])
                            params = None
                        else:
                            params = stored["chain"][0]["params"]
                        if kind.startswith("rew"):
                            bands = params.get("bands", params.get("leftBands"))
                            self.assertEqual([(b["frequencyHz"], b["gainDb"], b["q"], b["enabled"])
                                              for b in bands], [(46.3, -4.8, 3.387, True), (100, 2, 1, False)])
                            if kind != "rew-mono":
                                self.assertEqual(params["rightBands"][0]["frequencyHz"], 1234)
                        elif kind not in ("json", "bundle"):
                            ir = parse_wav_frames(self.manager._resolve_kernel_path(params["kernel"]))
                            expected_samples = (0.5, -0.125) if bank in ("sub1", "sub2") else (
                                (0.5, 0.25, -0.125, 0.0) if kind == "dual-ir" else (0.5, 0.25, -0.125, -0.0625))
                            self.assertEqual(struct.unpack(f"<{ir['samples']}f", ir["data"]), expected_samples)
                        plan = self.service.compile_plan(after, output_key="A", channels=len(roles), sample_rate_hz=48000)
                        layout = self.service.compile_layout(plan)
                        for output in layout:
                            if output["name"] not in target_roles or bank == "global" or kind in ("json", "bundle"):
                                self.assertEqual(output["filters"], [])
                                self.assertIsNone(output["oconv"])
                            elif kind.startswith("rew"):
                                right = output["name"].startswith("right_") or output["name"] in ("main_r", "sub_r")
                                self.assertEqual(output["filters"][0]["frequency_hz"],
                                                 1234 if right and kind != "rew-mono" else 46.3)
                            else:
                                right = output["name"].startswith("right_") or output["name"] in ("main_r", "sub_r")
                                self.assertEqual(output["oconv"]["channel"], 1 if right else 0)

    def test_rejected_import_does_not_assign_another_bank(self):
        self.seed(["main_l", "main_r", "sub1", "sub2"])
        for bank, kind, status in (("all", "wav", 400), ("missing", "rew-mono", 400),
                                   ("sub1", "dual-ir", 400), ("sub2", "rew-files", 400)):
            with self.subTest(bank=bank, kind=kind):
                before = self.service.load()
                response = self.request_import(kind, bank, f"Invalid-{bank}-{kind}")
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(self.service.load(), before)
        before = self.service.load()
        response = self.request_import("rew-mono", "sub1", "Conflict", revision=0)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.service.load(), before)


if __name__ == "__main__":
    unittest.main()
