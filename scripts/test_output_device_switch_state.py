#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Device selection keeps crossover intent, routing and recovery device-local."""

import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import audio.samplerate.overview as output_overview
from audio import hdmi
from audio.output_routing import device_key
from audio.output_api import build_output_state_mutation, output_state_response
from audio.output_device_state import prepare_device_state
from audio.output_state import (default_output_state, routing_for_device, set_crossover,
                                set_mode_routing, set_output_processing, switch_mode)
from audio.samplerate import _load_audio_output_selection, _save_audio_output_selection
from dsp.orchestration import DspOrchestrator
from output_render_test_support import RenderHarness, make_manager, make_service
from test_dsp_v2_sync_preference import _FakeDeps

USB = "alsa_output.usb-Test-00.pro-output-0"
HDMI = "alsa_output.pci-card.hdmi-surround71"
USB_PORTS = [f"playback_AUX{i}" for i in range(18)]
HDMI_PORTS = [f"playback_{p}" for p in ("FL", "FR", "RL", "RR", "FC", "LFE", "SL", "SR")]
SPEAKERS = ["main_l", "main_r", "rear_l", "rear_r", "center", "sub1", "side_l", "side_r"]
WAYS = ["left_low", "right_low", "rear_l", "rear_r", "sub1", "sub2", "left_high", "right_high"] + ["off"] * 10


def legacy_state(mode="surround"):
    state = set_mode_routing(default_output_state(), mode, HDMI,
                             SPEAKERS if mode == "surround" else ["main_l", "main_r"] + ["off"] * 6)
    state = set_crossover(state, mode, True)
    routes = WAYS if mode == "surround" else ["left_low", "right_low", "left_high", "right_high"] + ["off"] * 14
    state = set_mode_routing(state, mode, USB, routes)
    state = set_output_processing(state, mode, "left_low", lowpass={
        "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000}, level_db=-3)
    for config in state["modes"].values():
        config["device_crossovers"] = {}
    return switch_mode(state, mode)


class DeviceCrossoverMutationTests(unittest.TestCase):
    def test_api_crossover_edit_does_not_transform_another_devices_routing(self):
        state = set_mode_routing(default_output_state(), "surround", HDMI, SPEAKERS)
        state = set_mode_routing(state, "surround", USB, ["main_l", "main_r"] + ["off"] * 16)
        mutate = build_output_state_mutation(
            {"kind": "set_crossover", "mode": "surround", "enabled": True},
            output_key=USB, channels=18, get_service=lambda: None, validate_level_result=lambda *_: None)
        changed = mutate(state)
        self.assertEqual(routing_for_device(changed, "surround", HDMI, 8), SPEAKERS)
        self.assertEqual(changed["modes"]["surround"]["routing"][hdmi.routing_key(HDMI)], SPEAKERS)

    def test_explicit_same_value_crossover_draft_is_not_reinterpreted_on_selection(self):
        state = legacy_state()
        mutate = build_output_state_mutation(
            {"kind": "set_crossover", "mode": "surround", "enabled": True},
            output_key=HDMI, channels=8, get_service=lambda: None, validate_level_result=lambda *_: None)
        draft = mutate(state)
        prepared = prepare_device_state(draft, HDMI, defaults={})
        self.assertEqual(routing_for_device(prepared, "surround", HDMI, 8)[:2], ["left_low", "right_low"])

    def test_new_target_uses_its_own_full_range_defaults_without_copying_source_ways(self):
        state = legacy_state()
        prepared = prepare_device_state(state, "new-device", defaults={})
        self.assertEqual(routing_for_device(prepared, "surround", "new-device", 8), ["main_l", "main_r"])
        self.assertEqual(prepared["modes"]["surround"]["routing"][device_key(USB)], WAYS)
        self.assertEqual(prepared["active_mode"], "surround")

    def test_explicit_routing_edit_keeps_an_incomplete_crossover_draft(self):
        state = legacy_state()
        draft = set_mode_routing(state, "surround", HDMI,
                                 ["left_low", "right_low", "off", "off", "center", "sub1", "off", "off"])
        prepared = prepare_device_state(draft, HDMI, defaults={})
        self.assertEqual(routing_for_device(prepared, "surround", HDMI, 8)[:2], ["left_low", "right_low"])


class DeviceSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": directory.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = make_manager(directory.name)
        self.manager.save_global_extras({"limiter": {"enabled": True}})
        self.service = make_service(directory.name, self.manager)
        self.head = self.service.commit(legacy_state(), expected_revision=0)
        self.default = USB
        _save_audio_output_selection(USB)
        self.applied = None
        self.fail_preflight = set()
        self.fail_readback = set()
        self.fail_sync = set()
        self.fail_closed_errors = []
        self.closed_output_keys = []
        self.native = SimpleNamespace(snapshot=self.snapshot, preflight_rendered=self.preflight,
                                      sync_rendered=self.sync, fail_closed=self.fail_closed)
        self.harness = RenderHarness(self.service, self.manager, self.native)
        self.session = SimpleNamespace(lock=asyncio.Lock(), has_active_jobs=False)
        deps = _FakeDeps()
        deps.runtime = self.native
        deps.get_audio_output_overview = self.overview
        deps.get_measurement_sr_session = lambda: self.session
        deps.try_render_v2_target = self.harness.renderer.render_sync_target
        deps.graph_write_lock = lambda: self.harness.graph_lock
        self.orchestrator = DspOrchestrator(deps)
        self.orchestrator.refresh_peak_monitor_after_effects_change = mock.AsyncMock()
        for patcher in (
                mock.patch.object(main, "get_output_service", return_value=self.service),
                mock.patch.object(main, "get_audio_output_overview", side_effect=self.overview),
                mock.patch.object(output_overview, "get_audio_output_overview", side_effect=self.overview),
                mock.patch.object(main.samplerate, "_set_default_sink", side_effect=self.set_sink),
                mock.patch.object(main, "measurement_sr_session", self.session),
                mock.patch.object(main, "playback_transition_coordinator", SimpleNamespace(lock=asyncio.Lock())),
                mock.patch.object(main, "dsp_orchestrator", self.orchestrator),
                mock.patch.object(main.runtime, "dsp_runtime", self.native),
                mock.patch.object(main.playback_orchestration, "configured", return_value=SimpleNamespace(
                    playback_graph_diagnosis=self.diagnose))):
            patcher.start()
            self.addCleanup(patcher.stop)
        await self.sync(await self.harness.renderer.render_sync_target(48000, self.overview()))

    def overview(self, *, selection_key=None):
        key = selection_key or _load_audio_output_selection()["selected_key"]
        ports = USB_PORTS if key == USB else HDMI_PORTS
        return {"outputs": [{"key": k, "name": k, "selectable": True, "channels": n,
                              "supported_rates": [48000]} for k, n in ((USB, 18), (HDMI, 8))],
                "selected_output": {"key": key, "channels": len(ports)},
                "default_output": {"key": self.default},
                "output_mode": {"effective_output_key": key, "effective_output_channels": len(ports),
                                "hardware_playback_ports": list(ports)}}

    def set_sink(self, key):
        self.default = key

    def snapshot(self):
        config = self.applied.config if self.applied else None
        return {"active": bool(config), "config": ({"output_key": config.output_key,
                "hardware_ports": list(config.hardware_ports), "sample_rate": config.sample_rate,
                "plan_fingerprint": config.plan_fingerprint} if config else {})}

    async def preflight(self, target):
        if target.config.output_key in self.fail_preflight:
            raise RuntimeError("Native preflight rejected target")

    async def sync(self, target):
        if target.config.output_key in self.fail_sync:
            raise RuntimeError("Native apply failed")
        if target.validate_current:
            target.validate_current()
        self.applied = target
        if target.on_applied:
            target.on_applied()

    async def fail_closed(self, message, **kwargs):
        self.fail_closed_errors.append(message)
        self.closed_output_keys.append(self.applied.config.output_key if self.applied else None)
        self.applied = None

    async def diagnose(self, target):
        key = target["selected_output"]["key"]
        return {"output_key": key, "links_complete": bool(
            self.applied and self.applied.config.output_key == key and key not in self.fail_readback)}

    async def select(self, key):
        return await main.save_audio_output_selection_route(SimpleNamespace(json=lambda: self.body(key)))

    async def body(self, key):
        return {"key": key}

    def assert_restored(self, head=None):
        self.assertEqual(self.default, USB)
        self.assertEqual(_load_audio_output_selection()["selected_key"], USB)
        self.assertEqual(self.applied.config.output_key, USB)
        restored = self.service.load()
        expected = copy.deepcopy(head or self.head)
        restored.pop("revision")
        expected.pop("revision")
        self.assertEqual(restored, expected)

    async def test_direct_18_8_18_surround_crossover_keeps_target_roles_and_dsp(self):
        before = copy.deepcopy(self.head)
        for key in (HDMI, USB):
            result = await self.select(key)
            config = self.applied.config
            self.assertEqual(result["selected_output"]["key"], key)
            self.assertEqual(config.output_mode, "surround")
            self.assertEqual(len(config.hardware_ports), 8 if key == HDMI else 18)
            self.assertIn("sc_limiter", self.applied.text)
            response = output_state_response(self.service.load(), self.overview())
            self.assertEqual(response["modes"]["surround"]["crossover_enabled"], key == USB)
            if key == HDMI:
                self.assertEqual([r["name"] for r in config.layout],
                                 ["main_l", "main_r", "center", "side_l", "side_r", "rear_l", "rear_r", "sub1"])
                self.assertEqual(config.route_pairs, ((1, HDMI_PORTS[0]), (2, HDMI_PORTS[1]),
                    (6, HDMI_PORTS[2]), (7, HDMI_PORTS[3]), (3, HDMI_PORTS[4]), (8, HDMI_PORTS[5]),
                    (4, HDMI_PORTS[6]), (5, HDMI_PORTS[7])))
            else:
                self.assertEqual(config.layout[0]["name"], "left_low")
                self.assertEqual(config.layout[0]["gain_db"], -3)
                self.assertTrue(config.layout[0]["sos"])
        after = self.service.load()
        for mode in before["modes"]:
            for field in ("banks", "processing", "bass_management", "extras", "av_sync"):
                if field in ("banks", "processing"):
                    for role, value in before["modes"][mode][field].items():
                        self.assertEqual(after["modes"][mode][field][role], value)
                else:
                    self.assertEqual(after["modes"][mode][field], before["modes"][mode][field])
            if device_key(USB) in before["modes"][mode]["routing"]:
                self.assertEqual(after["modes"][mode]["routing"][device_key(USB)],
                                 before["modes"][mode]["routing"][device_key(USB)])

    async def test_direct_18_8_18_in_every_output_mode(self):
        for mode in ("stereo", "stereo-sub", "surround"):
            with self.subTest(mode=mode):
                state = legacy_state(mode)
                state["revision"] = self.service.load()["revision"]
                self.service.commit(state, expected_revision=state["revision"])
                await self.sync(await self.harness.renderer.render_sync_target(48000, self.overview()))
                for key in (HDMI, USB):
                    await self.select(key)
                    self.assertEqual(self.applied.config.output_key, key)
                    self.assertEqual(self.applied.config.output_mode, mode)
                    self.assertEqual(self.applied.config.layout[0]["name"], "left_low" if key == USB else "main_l")

    async def test_complete_target_crossover_is_preserved(self):
        state = set_mode_routing(self.head, "surround", HDMI,
                                 ["left_low", "right_low", "left_high", "right_high", "center", "sub1", "rear_l", "rear_r"])
        self.service.commit(state, expected_revision=1)
        await self.select(HDMI)
        self.assertEqual([r["name"] for r in self.applied.config.layout][:4],
                         ["left_low", "left_high", "right_low", "right_high"])
        self.assertEqual(self.applied.config.route_pairs[:4],
                         ((1, HDMI_PORTS[0]), (3, HDMI_PORTS[1]), (2, HDMI_PORTS[2]), (4, HDMI_PORTS[3])))

    async def test_current_device_draft_is_not_reinterpreted_by_reselection(self):
        state = set_mode_routing(self.head, "surround", USB, ["left_low", "right_low"] + ["off"] * 16)
        draft = self.service.commit(state, expected_revision=1)
        await self.select(USB)
        self.assertEqual(self.service.load(), draft)
        self.assertEqual(len(self.applied.config.layout), 8)

    async def test_native_apply_failure_keeps_source_and_reverts_preparation(self):
        self.fail_sync.add(HDMI)
        with self.assertRaises(main.HTTPException):
            await self.select(HDMI)
        self.assert_restored()

    async def test_newer_edit_survives_failed_switch_and_is_used_by_rollback(self):
        diagnose = self.diagnose
        newer = None

        async def race(target):
            nonlocal newer
            if target["selected_output"]["key"] == HDMI:
                head = self.service.load()
                newer = self.service.commit(set_output_processing(head, "surround", "left_low", level_db=-9),
                                             expected_revision=head["revision"])
            return await diagnose(target)

        self.fail_readback.add(HDMI)
        with mock.patch.object(main.playback_orchestration.configured(), "playback_graph_diagnosis", side_effect=race):
            with self.assertRaises(main.HTTPException):
                await self.select(HDMI)
        self.assertEqual(self.service.load(), newer)
        self.assertEqual(self.applied.config.output_key, USB)
        self.assertEqual(self.applied.config.layout[0]["gain_db"], -9)

    async def test_first_hdmi_selection_seeds_receiver_capacity_not_current_transport(self):
        state = copy.deepcopy(self.head)
        for config in state["modes"].values():
            config["routing"].pop(hdmi.routing_key(HDMI), None)
        self.service.commit(state, expected_revision=1)
        overview = self.overview
        stereo_key = "alsa_output.pci-card.hdmi-stereo"

        def narrow_overview(**kwargs):
            value = overview(**kwargs)
            for output in value["outputs"]:
                if output["key"] == HDMI:
                    output.update(key=stereo_key, name=stereo_key, channels=2)
            if value["selected_output"]["key"] == stereo_key:
                value["selected_output"].update(channels=2, hdmi={"transports": [{"channels": 8, "available": True}]})
                value["output_mode"].update(effective_output_channels=2, hardware_playback_ports=HDMI_PORTS[:2])
            return value

        with mock.patch.object(main, "get_audio_output_overview", side_effect=narrow_overview), \
                mock.patch.object(output_overview, "get_audio_output_overview", side_effect=narrow_overview):
            await self.select(stereo_key)
        self.assertEqual(routing_for_device(self.service.load(), "surround", HDMI, 8), SPEAKERS)
        self.assertEqual(len(self.applied.config.hardware_ports), 2)
        self.assertEqual(self.applied.config.route_pairs, ((1, HDMI_PORTS[0]), (2, HDMI_PORTS[1])))

    async def test_state_rollback_io_error_still_restores_default_selection_and_source_graph(self):
        self.fail_readback.add(HDMI)
        with mock.patch.object(self.service, "revert", side_effect=OSError("disk full")):
            with self.assertRaises(main.HTTPException):
                await self.select(HDMI)
        self.assertEqual(self.default, USB)
        self.assertEqual(_load_audio_output_selection()["selected_key"], USB)
        self.assertEqual(self.closed_output_keys, [USB])

    async def test_preflight_failure_reconciles_a_newer_source_edit_without_stopping_it(self):
        preflight = self.preflight
        changed = False

        async def race(target):
            nonlocal changed
            if target.config.output_key == HDMI and not changed:
                changed = True
                head = self.service.load()
                self.service.commit(set_output_processing(head, "surround", "left_low", level_db=-9),
                                    expected_revision=head["revision"])
            return await preflight(target)

        self.fail_preflight.add(HDMI)
        self.native.preflight_rendered = race
        with self.assertRaises(main.HTTPException):
            await self.select(HDMI)
        self.assertEqual(self.service.load()["modes"]["surround"]["processing"]["left_low"]["level_db"], -9)
        self.assertEqual(self.applied.config.output_key, USB)
        self.assertEqual(self.applied.config.layout[0]["gain_db"], -9)
        self.assertEqual(self.fail_closed_errors, [])

    async def test_edit_during_default_rollback_is_reconciled_under_graph_ownership(self):
        set_sink = self.set_sink

        def race(key):
            set_sink(key)
            if key == USB:
                head = self.service.load()
                self.service.commit(set_output_processing(head, "surround", "left_low", level_db=-9),
                                    expected_revision=head["revision"])

        self.fail_preflight.add(HDMI)
        with mock.patch.object(main.samplerate, "_set_default_sink", side_effect=race):
            with self.assertRaises(main.HTTPException):
                await self.select(HDMI)
        self.assertEqual(self.service.load()["modes"]["surround"]["processing"]["left_low"]["level_db"], -9)
        self.assertEqual(self.applied.config.layout[0]["gain_db"], -9)
        self.assertEqual(self.fail_closed_errors, [])

    async def test_preflight_failure_preserves_old_graph_and_all_device_state(self):
        self.fail_preflight.add(HDMI)
        with self.assertRaises(main.HTTPException) as failure:
            await self.select(HDMI)
        self.assertEqual(failure.exception.status_code, 500)
        self.assert_restored()
        self.assertEqual(self.fail_closed_errors, [])

    async def test_failed_readback_rolls_back_even_if_previous_head_is_a_draft(self):
        draft = set_mode_routing(self.head, "surround", USB, ["left_low", "right_low"] + ["off"] * 16)
        draft = self.service.commit(draft, expected_revision=1)
        self.fail_readback.add(HDMI)
        with self.assertRaises(main.HTTPException):
            await self.select(HDMI)
        self.assert_restored(draft)
        self.assertEqual(self.applied.config.layout[0]["name"], "left_low")
        self.assertEqual(len(self.applied.config.layout), 8)

    async def test_selection_persistence_failure_restores_the_source_checkpoint(self):
        with mock.patch.object(main.samplerate, "_save_audio_output_selection", side_effect=OSError("disk full")):
            with self.assertRaises(main.HTTPException):
                await self.select(HDMI)
        self.assert_restored()

    async def test_failed_rollback_fails_closed(self):
        self.fail_readback.add(HDMI)
        self.fail_sync.add(USB)
        with self.assertRaises(main.HTTPException) as failure:
            await self.select(HDMI)
        self.assertIn("previous graph could not be restored", failure.exception.detail)
        self.assertFalse(self.snapshot()["active"])
        self.assertTrue(self.fail_closed_errors)

    async def test_checkpoints_survive_another_device_and_service_restart(self):
        # Both states are independently renderable; no selection preparation is involved.
        stereo = self.service.commit(switch_mode(self.head, "stereo"), expected_revision=1)
        for key, ports in ((USB, USB_PORTS), (HDMI, HDMI_PORTS)):
            self.service.remember_applied(stereo, output_key=key, channels=len(ports),
                                          hardware_ports=ports, sample_rate_hz=48000)
        restarted = make_service(str(self.directory), self.manager)
        self.assertEqual(restarted.load_applied(output_key=USB, channels=18, hardware_ports=USB_PORTS), stereo)
        for channels, ports in ((8, USB_PORTS[:8]), (18, list(reversed(USB_PORTS)))):
            with self.assertRaises((ValueError, FileNotFoundError)):
                restarted.load_applied(output_key=USB, channels=channels, hardware_ports=ports)


if __name__ == "__main__":
    unittest.main()
