#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Output-state HTTP layer: catalog, revision-guarded apply, bank binding."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dsp.api as dsp_api
import main
from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state import set_bank_preset, set_mode_routing, switch_mode
from audio.output_state_store import OutputStateStore
from dsp.manager import DSPManager


class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class FakeUpload:
    def __init__(self, filename, content):
        self.filename = filename
        self.content_type = "text/plain"
        self._content = content
        self._offset = 0

    async def read(self, size=-1):
        if size is None or size < 0:
            chunk, self._offset = self._content[self._offset:], len(self._content)
            return chunk
        chunk = self._content[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


def make_manager(directory):
    manager = DSPManager(home=Path(directory) / "home")
    manager.preset_store.write("Room", {"schema": "fxroute.dsp.preset", "version": 1, "chain": []})
    return manager


def make_service(directory, manager):
    return OutputService(OutputServiceDeps(
        store=OutputStateStore(Path(directory) / "output-state.json"),
        preset_loader=manager.preset_store.read,
        resolve_ir=lambda kernel: (_ for _ in ()).throw(AssertionError(kernel)),
        measurement_active=lambda: False,
    ))


def seed_sub_state(service):
    return service.apply(
        lambda state: switch_mode(set_mode_routing(state, "stereo-sub", "A", ["main_l", "main_r", "sub1", "sub1"]), "stereo-sub"),
        expected_revision=0)


def audio_context(service, *, active_jobs=False, channels=4, rate=48000):
    stack = mock.patch.multiple(
        main,
        get_output_service=mock.MagicMock(return_value=service),
        measurement_sr_session=mock.MagicMock(has_active_jobs=active_jobs),
        get_audio_output_overview=mock.MagicMock(return_value={
            "selected_output": {"key": "A", "channels": channels},
            "output_mode": {"effective_output_key": "A", "effective_output_channels": channels},
        }),
        get_samplerate_status=mock.MagicMock(return_value={"active_rate": rate}),
    )
    return stack


class AudioStateApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.manager = make_manager(directory.name)
        self.service = make_service(directory.name, self.manager)

    def test_catalog_reports_revision_banks_topology_and_capabilities(self):
        seed_sub_state(self.service)
        with audio_context(self.service):
            catalog = asyncio.run(main.get_audio_output_state())
        self.assertEqual(catalog["revision"], 1)
        self.assertEqual(catalog["active_mode"], "stereo-sub")
        stereo = catalog["modes"]["stereo-sub"]
        self.assertEqual(stereo["selected_bank"], "global")
        self.assertEqual(stereo["banks"]["sub1"]["preset"], "Neutral")
        self.assertEqual(stereo["topology"]["sub_mode"], "mono")
        self.assertEqual(stereo["topology"]["issues"], [])
        self.assertIn("linkwitz-riley", catalog["capabilities"]["filter_families"])
        self.assertEqual(catalog["capabilities"]["max_slope_db_oct"], 72)
        self.assertEqual(catalog["device"]["key"], "A")
        self.assertEqual(catalog["device"]["channels"], 4)
        self.assertEqual(catalog["device"]["routing"]["stereo-sub"],
                         ["main_l", "main_r", "sub1", "sub1"])

    def test_apply_routing_commits_with_fingerprint_report(self):
        seed_sub_state(self.service)
        with audio_context(self.service):
            result = asyncio.run(main.apply_audio_output_state(FakeRequest({
                "expected_revision": 1,
                "mutation": {"kind": "set_bank_preset", "mode": "stereo-sub",
                             "bank_id": "sub1", "preset": "Room"},
            })))
        self.assertEqual(result["revision"], 2)
        self.assertTrue(result["fingerprint_changed"])
        self.assertFalse(result["live_applied"])
        self.assertEqual(result["live_reason"], "dsp-runtime-unavailable")
        self.assertEqual(result["topology"]["sub_mode"], "mono")
        self.assertEqual(self.service.load()["modes"]["stereo-sub"]["banks"]["sub1"]["preset"], "Room")

    def test_apply_selection_only_reports_unchanged_fingerprint(self):
        seed_sub_state(self.service)
        with audio_context(self.service):
            result = asyncio.run(main.apply_audio_output_state(FakeRequest({
                "expected_revision": 1,
                "mutation": {"kind": "select_bank", "mode": "stereo-sub", "bank_id": "sub1"},
            })))
        self.assertEqual(result["revision"], 2)
        self.assertFalse(result["fingerprint_changed"])

    def test_apply_stale_revision_is_409_without_write(self):
        seed_sub_state(self.service)
        before = Path(self.directory, "output-state.json").read_bytes()
        with audio_context(self.service):
            with self.assertRaises(main.HTTPException) as ctx:
                asyncio.run(main.apply_audio_output_state(FakeRequest({
                    "expected_revision": 0,
                    "mutation": {"kind": "switch_mode", "mode": "stereo-sub"},
                })))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(Path(self.directory, "output-state.json").read_bytes(), before)

    def test_apply_during_measurement_is_423_without_write(self):
        seed_sub_state(self.service)
        before = Path(self.directory, "output-state.json").read_bytes()
        with audio_context(self.service, active_jobs=True):
            with self.assertRaises(main.HTTPException) as ctx:
                asyncio.run(main.apply_audio_output_state(FakeRequest({
                    "expected_revision": 1,
                    "mutation": {"kind": "switch_mode", "mode": "stereo-sub"},
                })))
        self.assertEqual(ctx.exception.status_code, 423)
        self.assertEqual(Path(self.directory, "output-state.json").read_bytes(), before)

    def test_apply_rejects_unknown_mutation_and_role(self):
        seed_sub_state(self.service)
        with audio_context(self.service):
            for mutation in (
                {"kind": "teleport", "mode": "stereo-sub"},
                {"kind": "set_processing", "mode": "stereo-sub", "role": "left_low",
                 "level_db": 0.0},
                {"kind": "select_bank", "mode": "stereo-sub", "bank_id": "left_low"},
                {"kind": "set_routing", "mode": "stereo-sub",
                 "assignments": ["left_low", "main_r"]},
                {"kind": "set_routing", "mode": "stereo-sub", "assignments": ["main_l"],
                 "unexpected": True},
            ):
                with self.subTest(mutation=mutation):
                    with self.assertRaises(main.HTTPException) as ctx:
                        asyncio.run(main.apply_audio_output_state(FakeRequest({
                            "expected_revision": 1, "mutation": mutation})))
                    self.assertEqual(ctx.exception.status_code, 400)


def dsp_deps(service, manager):
    async def drain(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    return dsp_api.DspApiDeps(
        require_dsp_manager=mock.MagicMock(return_value=manager),
        get_dsp_manager=mock.MagicMock(return_value=manager),
        get_dsp_runtime=lambda: None,
        get_dsp_preset_load_lock=lambda: asyncio.Lock(),
        dsp_mutation_lock=lambda: asyncio.Lock(),
        canonical_volume_write_lock=lambda: asyncio.Lock(),
        drain_worker=drain,
        run_locked_worker=drain,
        broadcast=mock.AsyncMock(),
        load_dsp_preset=mock.AsyncMock(),
        restore_volume_state=mock.AsyncMock(),
        volume_state_for_manager=mock.MagicMock(),
        schedule_peak_monitor_refresh=mock.MagicMock(),
        get_output_service=lambda: service,
    )


class DspBankApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.manager = make_manager(directory.name)
        self.service = make_service(directory.name, self.manager)
        seed_sub_state(self.service)
        self.previous = dsp_api._runtime.deps
        dsp_api.configure_dsp_api(dsp_deps(self.service, self.manager))

    def tearDown(self):
        dsp_api.configure_dsp_api(self.previous)

    def test_delete_pinned_bank_preset_is_refused(self):
        self.service.apply(
            lambda state: set_bank_preset(state, "stereo-sub", "sub1", preset="Room"),
            expected_revision=1)
        with self.assertRaises(dsp_api.HTTPException) as ctx:
            asyncio.run(dsp_api.delete_dsp_preset(FakeRequest({"preset_name": "Room"})))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertTrue(self.manager.preset_store.path("Room").is_file())

    def test_delete_legacy_compare_preset_is_refused(self):
        self.manager.save_compare_state({"presetA": "Room", "presetB": "", "activeSide": "A"})
        with self.assertRaises(dsp_api.HTTPException) as ctx:
            asyncio.run(dsp_api.delete_dsp_preset(FakeRequest({"preset_name": "Room"})))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertTrue(self.manager.preset_store.path("Room").is_file())

    def test_delete_unpinned_preset_still_works(self):
        result = asyncio.run(dsp_api.delete_dsp_preset(FakeRequest({"preset_name": "Room"})))
        self.assertEqual(result["deleted"], "Room")
        self.assertFalse(self.manager.preset_store.path("Room").exists())

    def test_create_peq_with_bank_binding_assigns_target(self):
        result = asyncio.run(dsp_api.create_peq_preset(FakeRequest({
            "presetName": "Bank EQ",
            "peq": {"enabled": True, "params": {"channelMode": "stereo-linked", "bands": [
                {"filterType": "bell", "frequencyHz": 120, "gainDb": -2, "q": 1}]}},
            "bank_mode": "stereo-sub", "bank_id": "sub1", "expected_revision": 1,
        })))
        self.assertTrue(result["bank"]["assigned"])
        self.assertEqual(result["bank"]["revision"], 2)
        bank = self.service.load()["modes"]["stereo-sub"]["banks"]["sub1"]
        self.assertEqual(bank["preset"], "Bank EQ")

    def test_create_peq_binding_conflict_keeps_preset_unassigned(self):
        with self.assertRaises(dsp_api.HTTPException) as ctx:
            asyncio.run(dsp_api.create_peq_preset(FakeRequest({
                "presetName": "Bank EQ",
                "peq": {"enabled": True, "params": {"channelMode": "stereo-linked", "bands": [
                    {"filterType": "bell", "frequencyHz": 120, "gainDb": -2, "q": 1}]}},
                "bank_mode": "stereo-sub", "bank_id": "sub1", "expected_revision": 0,
            })))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertFalse(ctx.exception.detail["assigned"])
        self.assertEqual(ctx.exception.detail["created"], "Bank EQ")
        self.assertTrue(self.manager.preset_store.path("Bank EQ").is_file())
        bank = self.service.load()["modes"]["stereo-sub"]["banks"]["sub1"]
        self.assertEqual(bank["preset"], "Neutral")

    def test_create_peq_binding_validates_target_before_creation(self):
        with self.assertRaises(dsp_api.HTTPException) as ctx:
            asyncio.run(dsp_api.create_peq_preset(FakeRequest({
                "presetName": "Orphan EQ",
                "peq": {"enabled": True, "params": {"channelMode": "stereo-linked", "bands": []}},
                "bank_mode": "stereo-sub", "bank_id": "left_low", "expected_revision": 1,
            })))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertFalse(self.manager.preset_store.path("Orphan EQ").exists())

    def test_rew_import_with_bank_binding(self):
        result = asyncio.run(dsp_api.import_rew_peq_preset(
            preset_name="REW Bank",
            file=FakeUpload("rew.txt", b"Equaliser: Generic\n1 True Auto PK 46.30 -4.80 3.387\n"),
            load_after_create=False,
            limiter_enabled=False, headroom_enabled=False, headroom_gain_db=-3.0,
            autogain_enabled=False, autogain_target_db=-12.0,
            delay_enabled=False, delay_left_ms=0.0, delay_right_ms=0.0,
            bass_enabled=False, bass_amount=0.0,
            tone_effect_enabled=False, tone_effect_mode="crystalizer",
            bank_mode="stereo-sub", bank_id="sub1", expected_revision=1,
        ))
        self.assertTrue(result["bank"]["assigned"])
        bank = self.service.load()["modes"]["stereo-sub"]["banks"]["sub1"]
        self.assertEqual(bank["preset"], "REW Bank")


if __name__ == "__main__":
    unittest.main()
