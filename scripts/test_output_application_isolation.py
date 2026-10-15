#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Output application contracts without importing the application server."""

import asyncio
import copy
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state import set_mode_routing, set_output_processing
from audio.output_state_store import OutputStateStore, StateConflictError
from audio.output_apply import OutputApplication, OutputApplyDeps
from dsp.manager import DSPManager
from dsp.output_render import OutputRenderer, OutputRenderDeps
from fastapi import HTTPException


class OutputApplicationIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "output-state.json"
        self.manager = DSPManager(home=Path(directory.name) / "home")
        self.manager.save_global_extras({"limiter": {"enabled": False}})
        self.session = SimpleNamespace(has_active_jobs=False, entry_in_progress=False,
                                       lock=asyncio.Lock())
        self.service = OutputService(OutputServiceDeps(
            store=OutputStateStore(self.path), preset_loader=self.manager.read_preset,
            resolve_ir=lambda _bank, name: self.fail(f"Unexpected IR: {name}"),
            measurement_active=lambda: self.session.has_active_jobs,
            global_extras_loader=self.manager.load_global_extras))
        self.state = self.service.commit(set_mode_routing(
            self.service.load(), "stereo", "A", ["main_l", "main_r"]), expected_revision=0)
        self.ports = ["playback_AUX0", "playback_AUX1"]
        self.overview = {"selected_output": {"key": "A", "channels": 2}, "output_mode": {
            "effective_output_key": "A", "effective_output_channels": 2,
            "hardware_playback_ports": self.ports}}
        self.native = SimpleNamespace(
            snapshot=lambda: {"active": False, "config": {}, "output_gain_db": 0.0},
            preflight_rendered=AsyncMock(), sync_rendered=AsyncMock(),
            guarded_rebuild_rendered=AsyncMock(), fail_closed=AsyncMock())
        self.graph_lock = asyncio.Lock()
        self.mutation_lock = asyncio.Lock()
        self.renderer = OutputRenderer(OutputRenderDeps(
            get_service=lambda: self.service, get_manager=lambda: self.manager,
            get_runtime=lambda: self.native, get_overview=lambda: self.overview,
            get_status=lambda: {"active_rate": 48000},
            graph_write_lock=lambda: self.graph_lock, mutation_lock=lambda: self.mutation_lock,
            volume_state_for_manager=AsyncMock()))

        @asynccontextmanager
        async def mode_guard():
            yield

        self.app = OutputApplication(OutputApplyDeps(
            renderer=self.renderer, get_service=lambda: self.service,
            get_manager=lambda: self.manager, get_runtime=lambda: self.native,
            get_session=lambda: self.session, get_overview=lambda: self.overview,
            get_status=lambda: {"active_rate": 48000},
            graph_write_lock=lambda: self.graph_lock, mutation_lock=lambda: self.mutation_lock,
            mode_switch_guard=mode_guard, get_carrier=lambda: (False, None),
            set_carrier=AsyncMock(), carrier_request=AsyncMock(return_value=(False, None)),
            disable_external_input=AsyncMock(), sync_external_input=AsyncMock(),
            resync_stdin=AsyncMock(), mark_ingress_pending=AsyncMock(), mark_policy_pending=AsyncMock(),
            current_playback_context=AsyncMock(return_value={}), run_transition=AsyncMock(),
            transition_error_http=lambda exc: HTTPException(status_code=500, detail=str(exc)),
            source_selection=lambda: {"mode": "app-playback"},
            validate_level_result=lambda *_args: None, mark_level_applied=lambda *_args: None))

    def edit(self, revision=1):
        return {"expected_revision": revision, "mutation": {
            "kind": "set_processing", "mode": "stereo", "role": "main_l", "level_db": -7}}

    async def test_stale_revision_preserves_document_without_applying_audio(self):
        before = self.path.read_bytes()
        with self.assertRaises(HTTPException) as caught:
            await self.app.apply(self.edit(0))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.path.read_bytes(), before)
        self.native.guarded_rebuild_rendered.assert_not_awaited()

    async def test_fast_apply_owns_existing_rate_and_graph_locks(self):
        async def rebuild(target, **_kwargs):
            self.assertTrue(self.session.lock.locked())
            self.assertTrue(self.graph_lock.locked())
            self.assertEqual(target.config.layout[0]["gain_db"], -7)
        self.native.guarded_rebuild_rendered.side_effect = rebuild
        result = await self.app.apply(self.edit())
        self.assertTrue(result["live_applied"])
        self.assertEqual(self.service.load_applied(
            output_key="A", channels=2, hardware_ports=self.ports), self.service.load())

    async def test_failed_apply_restores_previous_processing_as_new_revision(self):
        self.native.guarded_rebuild_rendered.side_effect = RuntimeError("engine failed")
        with self.assertRaises(HTTPException) as caught:
            await self.app.apply(self.edit())
        self.assertEqual(caught.exception.detail["rollback"], "committed")
        self.assertEqual(self.service.load()["revision"], 3)
        self.assertEqual(self.service.load()["modes"], self.state["modes"])

    async def test_measurement_owner_blocks_before_any_render_or_write(self):
        self.session.has_active_jobs = True
        with self.assertRaises(HTTPException) as caught:
            await self.app.apply(self.edit())
        self.assertEqual(caught.exception.status_code, 423)
        self.assertEqual(self.service.load(), self.state)
        self.native.preflight_rendered.assert_not_awaited()

    async def test_rendered_target_refuses_a_newer_edit_before_activation(self):
        target = await self.renderer.render_sync_target(48000, self.overview)
        self.service.commit(set_output_processing(
            self.state, "stereo", "main_r", level_db=-4), expected_revision=1)
        with self.assertRaises(StateConflictError):
            target.validate_current()

    async def test_recovery_preserves_invalid_draft_and_recompiles_checkpoint_rate(self):
        self.service.remember_applied(self.state, output_key="A", channels=2,
                                      hardware_ports=self.ports, sample_rate_hz=48000)
        draft = copy.deepcopy(self.state)
        draft["modes"]["stereo"]["banks"]["global"]["preset"] = "Removed"
        self.service.commit(draft, expected_revision=1)
        before = self.path.read_bytes()
        target = await self.renderer.render_sync_target(44100, self.overview)
        self.assertEqual(target.config.sample_rate, 44100)
        self.assertEqual(target.config.route_pairs, ((1, self.ports[0]), (2, self.ports[1])))
        self.assertEqual(self.path.read_bytes(), before)
        self.native.fail_closed.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
