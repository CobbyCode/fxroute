#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Actual state/plan activation boundaries, input memory and ingress rollback."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state import default_output_state, set_mode_routing
from audio.output_state_store import OutputStateStore
from dsp.manager import DSPManager


class ActivationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        self.manager = DSPManager(home=path)
        self.service = OutputService(OutputServiceDeps(
            store=OutputStateStore(path / "state.json"), preset_loader=self.manager.preset_store.read,
            resolve_ir=lambda name: {}, measurement_active=lambda: False))
        state = set_mode_routing(default_output_state(), "surround", "A",
                                 ["main_l", "main_r", "sub1", "sub2", "center", "side_l", "side_r", "rear_l", "rear_r"])
        self.state = self.service.commit(state, expected_revision=0)
        self.transitions = []

        async def transition(request):
            self.transitions.append(request)
            prepared = request.output_state_transition
            self.service.commit(prepared["candidate_state"], expected_revision=prepared["expected_revision"])
            return SimpleNamespace(committed=True)

        overview = {"selected_output": {"key": "A", "channels": 9}, "output_mode": {
            "effective_output_key": "A", "effective_output_channels": 9,
            "hardware_playback_ports": [f"playback_AUX{i}" for i in range(9)]}}
        self.input = SimpleNamespace(disable=AsyncMock(), sync=AsyncMock())
        for patcher in (
            patch.object(main, "get_output_service", return_value=self.service),
            patch.object(main, "get_audio_output_overview", return_value=overview),
            patch.object(main, "get_samplerate_status", return_value={"active_rate": 48000, "force_rate": 48000}),
            patch.object(main, "_require_dsp_manager", return_value=self.manager),
            patch.object(main, "_coordinator_current_playback_context", AsyncMock(return_value={"source": "local"})),
            patch.object(main, "_run_coordinated_transition", transition),
            patch.object(main.samplerate, "_load_audio_source_selection", return_value={
                "mode": "external-input", "selected_input_key": "scarlett::pair:9-10"}),
            patch.object(main, "external_input", self.input),
            patch.object(main, "_set_surround_carrier_owned", AsyncMock()),
            patch.object(main, "_sync_v2_head_after_bank_assign", AsyncMock()),
            patch.object(main, "measurement_sr_session", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def switch(self):
        return await main._apply_audio_output_state_body({"expected_revision": self.state["revision"],
            "mutation": {"kind": "switch_mode", "mode": "surround"}})

    async def test_activation_uses_eight_input_plan_and_remembers_selected_pair(self):
        response = await self.switch()
        self.assertTrue(response["live_applied"])
        head = self.service.load()
        self.assertEqual(head["active_mode"], "surround")
        self.assertEqual(head["input_modes"]["external-input:scarlett::pair:9-10"], "surround")
        target = self.transitions[0].output_state_transition["target"]
        self.assertEqual(target.config.input_count, 8)
        self.assertIn("surround 1\n", target.text)
        self.assertIn("postmatrix 7 3 1\n", target.text)

    async def test_ingress_failure_reverts_committed_mode_and_bindings(self):
        self.input.sync.side_effect = RuntimeError("decoder did not start")
        with self.assertRaisesRegex(RuntimeError, "decoder did not start"):
            await self.switch()
        head = self.service.load()
        self.assertEqual(head["active_mode"], "stereo")
        self.assertEqual(head["input_modes"], {})
        self.assertEqual(head["modes"], self.state["modes"])

    async def test_invalid_mutation_has_no_ingress_or_clock_side_effects(self):
        with self.assertRaises(main.HTTPException) as raised:
            await main._apply_audio_output_state_body({"expected_revision": self.state["revision"],
                "mutation": {"kind": "switch_mode", "mode": "surround", "unknown": 1}})
        self.assertEqual(raised.exception.status_code, 400)
        self.input.disable.assert_not_awaited()
        main._set_surround_carrier_owned.assert_not_awaited()

    async def test_clock_policy_guard_is_rechecked_after_request_body(self):
        class Request:
            async def json(self):
                main._surround_carrier_owned = True
                return {"mode": "fixed", "rate": 44100}
        with patch.object(main, "_surround_carrier_owned", False), patch.object(
                main, "_transition_sample_rate_policy", AsyncMock()) as transition:
            with self.assertRaises(main.HTTPException) as raised:
                await main.save_audio_samplerate_policy(Request())
            self.assertEqual(raised.exception.status_code, 409)
            transition.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
