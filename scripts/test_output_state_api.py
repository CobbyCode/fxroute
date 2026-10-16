#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Output-state HTTP layer: catalog, revision-guarded apply, bank binding."""

import asyncio
import sys
import tempfile
import unittest
from dataclasses import replace
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
    """Every bank has its own "Room": Sub 1, both Global banks the tests use."""
    manager = DSPManager(home=Path(directory) / "home")
    for bank, mode in (("sub1", None), ("global", "stereo-sub"), ("global", "stereo")):
        manager.library(bank, mode).write("Room", {"schema": "fxroute.dsp.preset", "version": 1, "chain": []})
    return manager


def stored_in(manager, name):
    """Banks (with the Global output mode) that hold a preset of this name."""
    return sorted((entry["bank"], entry["mode"]) for entry in manager.list_presets() if entry["name"] == name)


def make_service(directory, manager):
    return OutputService(OutputServiceDeps(
        store=OutputStateStore(Path(directory) / "output-state.json"),
        preset_loader=manager.read_preset,
        resolve_ir=lambda _bank, kernel: (_ for _ in ()).throw(AssertionError(kernel)),
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
        self.assertEqual(list(stereo["banks"]), ["global", "main", "sub1"])
        self.assertEqual(stereo["banks"]["main"]["roles"], ["main_l", "main_r"])
        self.assertEqual(stereo["banks"]["main"]["channel_mode"], "stereo")
        self.assertEqual(stereo["banks"]["sub1"]["channel_mode"], "mono")
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

    def test_area_direct_is_rejected_without_commit_or_transition(self):
        seed_sub_state(self.service)
        before = Path(self.directory, "output-state.json").read_bytes()
        with audio_context(self.service), mock.patch.object(main.runtime, "dsp_runtime", mock.Mock()) as runtime:
            for bank in ("main", "main_l", "sub1"):
                for field in ("preset", "preset_a", "preset_b"):
                    with self.subTest(bank=bank, field=field):
                        with self.assertRaises(main.HTTPException) as error:
                            asyncio.run(main.apply_audio_output_state(FakeRequest({
                                "expected_revision": 1,
                                "mutation": {"kind": "set_bank_preset", "mode": "stereo-sub",
                                             "bank_id": bank, field: "Direct"},
                            })))
                        self.assertEqual(error.exception.status_code, 400)
            self.assertEqual(runtime.mock_calls, [])
        self.assertEqual(Path(self.directory, "output-state.json").read_bytes(), before)

    def test_reassigning_area_neutral_does_not_start_a_live_transition(self):
        seed_sub_state(self.service)
        with audio_context(self.service), mock.patch.object(main.runtime, "dsp_runtime", mock.Mock()) as runtime:
            result = asyncio.run(main.apply_audio_output_state(FakeRequest({
                "expected_revision": 1,
                "mutation": {"kind": "set_bank_preset", "mode": "stereo-sub",
                             "bank_id": "sub1", "preset": "Neutral"},
            })))
            self.assertEqual(runtime.mock_calls, [])
        self.assertFalse(result["fingerprint_changed"])
        self.assertFalse(result["live_applied"])

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
                {"kind": "set_bank_preset", "mode": "stereo-sub",
                 "bank_id": "sub1", "preset": "Missing Preset"},
                {"kind": "set_routing", "mode": "stereo-sub",
                 "assignments": ["left_low", "main_r"]},
                {"kind": "set_routing", "mode": "stereo-sub", "assignments": ["main_l"],
                 "unexpected": True},
                # Removed legacy kind: bass is set via set_subwoofers.
                {"kind": "set_bass", "mode": "stereo-sub",
                 "frequency_hz": 80, "main_highpass_enabled": True},
            ):
                with self.subTest(mutation=mutation):
                    with self.assertRaises(main.HTTPException) as ctx:
                        asyncio.run(main.apply_audio_output_state(FakeRequest({
                            "expected_revision": 1, "mutation": mutation})))
                    self.assertEqual(ctx.exception.status_code, 400)

    def test_level_result_cannot_rebase_its_original_revision(self):
        seed_sub_state(self.service)
        self.service.apply(lambda state: state, expected_revision=1)
        before = Path(self.directory, "output-state.json").read_bytes()
        with audio_context(self.service):
            with self.assertRaises(main.HTTPException) as caught:
                asyncio.run(main.apply_audio_output_state(FakeRequest({
                    "expected_revision": 2,
                    "mutation": {"kind": "set_level_calibration", "mode": "stereo-sub",
                                 "state_revision": 1, "measurement_job_id": "old-job",
                                 "values": {"main_l": {"global": -4}}},
                })))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(Path(self.directory, "output-state.json").read_bytes(), before)

    def test_level_result_revalidates_context_after_async_prepare(self):
        seed_sub_state(self.service)
        before = Path(self.directory, "output-state.json").read_bytes()
        from fastapi import HTTPException
        with audio_context(self.service), mock.patch.object(main.level_match_api, "validate_result",
                side_effect=[None, HTTPException(status_code=409, detail="Measure again")]) as validate:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.apply_audio_output_state(FakeRequest({
                    "expected_revision": 1, "mutation": {"kind": "set_level_calibration", "mode": "stereo-sub",
                    "state_revision": 1, "measurement_job_id": "job-1", "values": {"main_l": {"global": -4}}}})))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(Path(self.directory, "output-state.json").read_bytes(), before)

    def test_level_result_atomic_commit_preserves_a_concurrent_clients_winner(self):
        seed_sub_state(self.service)
        original = self.service.commit_unowned
        winner = None
        def racing_commit(candidate, *, expected_revision):
            nonlocal winner
            winner = self.service.load()
            winner["modes"]["stereo-sub"]["processing"]["main_l"]["level_db"] = -9
            winner = self.service.commit(winner, expected_revision=expected_revision)
            return original(candidate, expected_revision=expected_revision)
        with audio_context(self.service), mock.patch.object(main.level_match_api, "validate_result"), \
                mock.patch.object(self.service, "commit_unowned", side_effect=racing_commit):
            with self.assertRaises(main.HTTPException) as caught:
                asyncio.run(main.apply_audio_output_state(FakeRequest({
                    "expected_revision": 1, "mutation": {"kind": "set_level_calibration", "mode": "stereo-sub",
                    "state_revision": 1, "measurement_job_id": "job-1", "values": {"main_l": {"global": -4}}}})))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.service.load(), winner)

    def test_level_result_requires_original_revision_even_without_job_id(self):
        seed_sub_state(self.service)
        before = Path(self.directory, "output-state.json").read_bytes()
        with audio_context(self.service):
            with self.assertRaises(main.HTTPException) as caught:
                asyncio.run(main.apply_audio_output_state(FakeRequest({
                    "expected_revision": 1,
                    "mutation": {"kind": "set_level_calibration", "mode": "stereo-sub",
                                 "values": {"main_l": {"global": -4}}},
                })))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(Path(self.directory, "output-state.json").read_bytes(), before)

    def test_removed_legacy_routes_are_unknown(self):
        routes = {getattr(route, "path", "") for route in main.app.routes}
        self.assertNotIn("/api/audio/output-mode", routes)
        self.assertNotIn("/api/audio/output-routing", routes)


def dsp_deps(service, manager, sync_hook=None):
    async def drain(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    return dsp_api.DspApiDeps(
        require_dsp_manager=mock.MagicMock(return_value=manager),
        get_dsp_manager=mock.MagicMock(return_value=manager),
        get_dsp_runtime=lambda: None,
        dsp_mutation_lock=lambda: asyncio.Lock(),
        canonical_volume_write_lock=lambda: asyncio.Lock(),
        drain_worker=drain,
        run_locked_worker=drain,
        broadcast=mock.AsyncMock(),
        restore_volume_state=mock.AsyncMock(),
        volume_state_for_manager=mock.MagicMock(),
        schedule_peak_monitor_refresh=mock.MagicMock(),
        get_output_service=lambda: service,
        sync_v2_head_live=sync_hook,
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

    def test_dual_import_projects_each_side_of_shared_main_bank(self):
        bands = lambda frequency, gain: [{"filterType": "bell", "frequencyHz": frequency, "gainDb": gain, "q": 1}]
        result = asyncio.run(dsp_api.create_peq_preset(FakeRequest({
            "presetName": "Pair EQ",
            "peq": {"enabled": True, "params": {"channelMode": "dual",
                    "leftBands": bands(900, -2), "rightBands": bands(1100, -4)}},
            "bank_mode": "stereo-sub", "bank_id": "main", "expected_revision": 1,
        })))
        self.assertTrue(result["bank"]["assigned"])
        state = self.service.load()
        plan = self.service.compile_plan(state, output_key="A", channels=4, sample_rate_hz=48000)
        layout = self.service.compile_layout(plan)
        self.assertEqual(plan["global"]["preset"], "Neutral")
        self.assertEqual([entry["bank"]["preset"] for entry in plan["outputs"][:2]], ["Pair EQ", "Pair EQ"])
        self.assertEqual([entry["filters"][0]["frequency_hz"] for entry in layout[:2]], [900, 1100])
        self.assertEqual([entry["filters"][0]["gain_db"] for entry in layout[:2]], [-2, -4])

    def test_mono_target_rejects_different_lr_correction_before_assignment(self):
        before = self.service.load()
        with self.assertRaises(dsp_api.HTTPException) as ctx:
            asyncio.run(dsp_api.create_peq_preset(FakeRequest({
                "presetName": "Bad mono", "peq": {"enabled": True, "params": {"channelMode": "dual",
                    "leftBands": [{"filterType": "bell", "frequencyHz": 100, "gainDb": -2, "q": 1}],
                    "rightBands": []}},
                "bank_mode": "stereo-sub", "bank_id": "sub1", "expected_revision": 1,
            })))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.service.load(), before)

    def test_all_banks_cannot_receive_an_import(self):
        with self.assertRaises(dsp_api.HTTPException) as ctx:
            asyncio.run(dsp_api.create_peq_preset(FakeRequest({
                "presetName": "Invalid group", "peq": {"params": {"bands": []}},
                "bank_mode": "stereo-sub", "bank_id": "all", "expected_revision": 1,
            })))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(stored_in(self.manager, "Invalid group"), [])

    def test_delete_releases_bank_slots_like_global_compare(self):
        self.service.apply(
            lambda state: set_bank_preset(state, "stereo-sub", "sub1", preset="Room"),
            expected_revision=1)
        self.service.apply(
            lambda state: set_bank_preset(state, "stereo-sub", "global", preset_b="Room", active_side="B"),
            expected_revision=2)
        sync = mock.AsyncMock(return_value={"live_applied": True, "live_reason": None})
        dsp_api.configure_dsp_api(dsp_deps(self.service, self.manager, sync_hook=sync))
        # A delete names one bank's preset: Sub 1 loses its "Room", Global keeps its own.
        result = asyncio.run(dsp_api.delete_dsp_preset(FakeRequest(
            {"preset_name": "Room", "bank_mode": "stereo-sub", "bank_id": "sub1"})))
        self.assertEqual(result["deleted"], "Room")
        self.assertEqual(stored_in(self.manager, "Room"), [("global", "stereo"), ("global", "stereo-sub")])
        state = self.service.load()
        banks = state["modes"]["stereo-sub"]["banks"]
        self.assertEqual(state["revision"], 4)
        self.assertEqual(banks["sub1"], {"preset": "Neutral", "preset_a": "Neutral", "preset_b": "Neutral"})
        self.assertEqual(banks["global"], {"preset": "Room", "preset_a": "Neutral", "preset_b": "Room"})
        sync.assert_awaited_once()
        asyncio.run(dsp_api.delete_dsp_preset(FakeRequest(
            {"preset_name": "Room", "bank_mode": "stereo-sub", "bank_id": "global"})))
        self.assertEqual(stored_in(self.manager, "Room"), [("global", "stereo")])
        self.assertEqual(self.service.load()["modes"]["stereo-sub"]["banks"]["global"],
                         {"preset": "Neutral", "preset_a": "Neutral", "preset_b": "Neutral"})

    def test_delete_keeps_slots_when_the_preset_cannot_be_deleted(self):
        self.service.apply(
            lambda state: set_bank_preset(state, "stereo-sub", "global", preset_b="Direct"),
            expected_revision=1)
        before = self.service.load()
        for name, status in (("Direct", 400), ("Missing", 404)):
            with self.subTest(name=name), self.assertRaises(dsp_api.HTTPException) as ctx:
                asyncio.run(dsp_api.delete_dsp_preset(FakeRequest({"preset_name": name})))
            self.assertEqual(ctx.exception.status_code, status)
        self.assertEqual(self.service.load(), before)

    def test_delete_unpinned_preset_still_works(self):
        result = asyncio.run(dsp_api.delete_dsp_preset(FakeRequest({"preset_name": "Room"})))
        self.assertEqual(result["deleted"], "Room")
        self.assertNotIn(("global", "stereo"), stored_in(self.manager, "Room"))

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

    def test_create_peq_binding_conflict_creates_nothing(self):
        """The binding guard covers the creation, not only the assignment.

        A create and its bank assignment are one transaction: refusing the
        assignment after the preset file exists would leave an orphan behind.
        """
        with self.assertRaises(dsp_api.HTTPException) as ctx:
            asyncio.run(dsp_api.create_peq_preset(FakeRequest({
                "presetName": "Bank EQ",
                "peq": {"enabled": True, "params": {"channelMode": "stereo-linked", "bands": [
                    {"filterType": "bell", "frequencyHz": 120, "gainDb": -2, "q": 1}]}},
                "bank_mode": "stereo-sub", "bank_id": "sub1", "expected_revision": 0,
            })))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertFalse(ctx.exception.detail["assigned"])
        self.assertIsNone(ctx.exception.detail["created"])
        self.assertEqual(ctx.exception.detail["code"], "bank-assign-conflict")
        self.assertEqual(ctx.exception.detail["revision"], 1)
        self.assertEqual(stored_in(self.manager, "Bank EQ"), [])
        bank = self.service.load()["modes"]["stereo-sub"]["banks"]["sub1"]
        self.assertEqual(bank["preset"], "Neutral")

    def test_create_peq_binding_writes_nothing_while_a_measurement_owns_the_graph(self):
        locked = replace(self.service._deps, measurement_active=lambda: True)
        with mock.patch.object(self.service, "_deps", locked):
            with self.assertRaises(dsp_api.HTTPException) as ctx:
                asyncio.run(dsp_api.create_peq_preset(FakeRequest({
                    "presetName": "Locked EQ",
                    "peq": {"enabled": True, "params": {"channelMode": "stereo-linked", "bands": [
                        {"filterType": "bell", "frequencyHz": 120, "gainDb": -2, "q": 1}]}},
                    "bank_mode": "stereo-sub", "bank_id": "sub1", "expected_revision": 1,
                })))
        self.assertEqual(ctx.exception.status_code, 423)
        self.assertEqual(ctx.exception.detail["code"], "bank-assign-locked")
        self.assertFalse(ctx.exception.detail["assigned"])
        self.assertEqual(stored_in(self.manager, "Locked EQ"), [])
        self.assertEqual(self.service.load()["revision"], 1)

    def test_create_peq_binding_validates_target_before_creation(self):
        with self.assertRaises(dsp_api.HTTPException) as ctx:
            asyncio.run(dsp_api.create_peq_preset(FakeRequest({
                "presetName": "Orphan EQ",
                "peq": {"enabled": True, "params": {"channelMode": "stereo-linked", "bands": []}},
                "bank_mode": "stereo-sub", "bank_id": "left_low", "expected_revision": 1,
            })))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(stored_in(self.manager, "Orphan EQ"), [])

    def test_rew_import_with_bank_binding(self):
        result = asyncio.run(dsp_api.import_rew_peq_preset(
            preset_name="REW Bank",
            file=FakeUpload("rew.txt", b"Equaliser: Generic\n1 True Auto PK 46.30 -4.80 3.387\n"),
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

    def test_bank_binding_triggers_live_sync_when_wired(self):
        hook = mock.AsyncMock(return_value={"live_applied": True, "live_reason": None})
        dsp_api.configure_dsp_api(dsp_deps(self.service, self.manager, sync_hook=hook))
        try:
            result = asyncio.run(dsp_api.create_peq_preset(FakeRequest({
                "presetName": "Live EQ",
                "peq": {"enabled": True, "params": {"channelMode": "stereo-linked", "bands": [
                    {"filterType": "bell", "frequencyHz": 120, "gainDb": -2, "q": 1}]}},
                "bank_mode": "stereo-sub", "bank_id": "sub1", "expected_revision": 1,
            })))
        finally:
            dsp_api.configure_dsp_api(dsp_deps(self.service, self.manager))
        self.assertTrue(result["bank"]["assigned"])
        self.assertTrue(result["bank"]["live_applied"])
        self.assertIsNone(result["bank"]["live_reason"])
        hook.assert_awaited_once()

    def test_bank_binding_survives_live_sync_failure(self):
        async def failing_hook():
            raise RuntimeError("engine unreachable")

        dsp_api.configure_dsp_api(dsp_deps(self.service, self.manager, sync_hook=failing_hook))
        try:
            result = asyncio.run(dsp_api.create_peq_preset(FakeRequest({
                "presetName": "Stale EQ",
                "peq": {"enabled": True, "params": {"channelMode": "stereo-linked", "bands": [
                    {"filterType": "bell", "frequencyHz": 120, "gainDb": -2, "q": 1}]}},
                "bank_mode": "stereo-sub", "bank_id": "sub1", "expected_revision": 1,
            })))
        finally:
            dsp_api.configure_dsp_api(dsp_deps(self.service, self.manager))
        self.assertTrue(result["bank"]["assigned"])
        self.assertFalse(result["bank"]["live_applied"])
        bank = self.service.load()["modes"]["stereo-sub"]["banks"]["sub1"]
        self.assertEqual(bank["preset"], "Stale EQ")

    def test_bank_binding_without_hook_reports_unavailable(self):
        result = asyncio.run(dsp_api.create_peq_preset(FakeRequest({
            "presetName": "Quiet EQ",
            "peq": {"enabled": True, "params": {"channelMode": "stereo-linked", "bands": [
                {"filterType": "bell", "frequencyHz": 120, "gainDb": -2, "q": 1}]}},
            "bank_mode": "stereo-sub", "bank_id": "sub1", "expected_revision": 1,
        })))
        self.assertTrue(result["bank"]["assigned"])
        self.assertFalse(result["bank"]["live_applied"])
        self.assertEqual(result["bank"]["live_reason"], "live-sync-unavailable")


if __name__ == "__main__":
    unittest.main()
