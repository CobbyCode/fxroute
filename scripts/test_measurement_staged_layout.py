#!/usr/bin/env python3
"""Trusted staged plans must survive the production capture path without rebasing."""

import asyncio
from copy import deepcopy
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import default_output_state, set_mode_routing, switch_mode
from dsp.manager import DSPManager
from dsp.native_config import layout_from_plan
from dsp.processing_plan import compile_processing_plan
from dsp.runtime import DSPRuntimeConfig
from measurement.store import MeasurementStore


class StagedLayoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home = Path(directory.name)
        environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(home / "config"),
                                             "XDG_STATE_HOME": str(home / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        manager = DSPManager(home=home)
        roles = [f"{side}_{way}" for side in ("left", "right") for way in ("low", "mid", "high")]
        state = switch_mode(set_mode_routing(default_output_state(), "crossover", "dev",
                                             roles + ["sub1", "sub2"]), "crossover")
        for role in roles:
            processing = state["modes"]["crossover"]["processing"][role]
            for kind, frequency in (("highpass", 300 if role.endswith("mid") else 2500),
                                    ("lowpass", 300 if role.endswith("low") else 2500)):
                if (kind == "highpass" and not role.endswith("low")) or (kind == "lowpass" and not role.endswith("high")):
                    processing[kind] = {"family": "linkwitz-riley", "slope_db_oct": 24,
                                        "frequency_hz": frequency}
        plan = compile_processing_plan(state, output_key="dev", channels=8,
                                       sample_rate_hz=48000, preset_loader=manager.preset_store.read)
        self.layout = layout_from_plan(plan, resolve_ir=lambda _: self.fail("Unexpected IR"))
        manager.compile_engine_text(self.layout, preset_name="Direct", sample_rate_hz=48000)
        self.assertEqual(len(self.layout), 8)
        self.assertTrue(self.layout[0]["sos"])
        self.context = {"expected_native_layout": self.layout,
                        "expected_native_output_mode": "crossover",
                        "expected_plan_fingerprint": "staged-plan-1"}
        self.runtime = {"active": True, "effect_bypass": True, "config": {
            "sample_rate": 48000, "output_mode": "crossover", "layout": deepcopy(self.layout),
            "plan_fingerprint": "staged-plan-1"}}

        async def bypass(_value):
            return True

        self.store = MeasurementStore(home=home, runtime_snapshot_provider=lambda: self.runtime,
                                      effect_bypass_setter=bypass)
        self.store._discover_capture_inputs = lambda: [{"id": "mic", "label": "Mic",
            "node_name": "mic", "channels": 2, "sample_rate": 48000, "available": True}]
        self.store._list_pw_ports = lambda node: [f"{node}:{port}" for port in
            ("playback_FL", "playback_FR", "monitor_FL", "monitor_FR")]
        self.overview = {"output_mode": {"mode": "stereo"}}
        self.store._routing._get_output_overview = lambda: self.overview
        self.store._pw_record_supports_option = lambda _: False
        self.store._snapshot_fxroute_21_helper_processes = lambda _: {}
        self.store._write_sweep_file = lambda *args, **kwargs: {}
        self.spawned = []

        def stop_at_hardware(job_id, command):
            self.spawned.append(command[0])
            raise RuntimeError("hardware boundary reached")

        self.store._start_job_process = stop_at_hardware
        self.snapshots = []
        snapshot = self.store._routing._build_pre_sweep_state_snapshot

        def observe_snapshot(**kwargs):
            result = snapshot(**kwargs)
            self.snapshots.append(result)
            return result

        self.store._routing._build_pre_sweep_state_snapshot = observe_snapshot

    async def capture(self, context=None, *, scope="raw_helper", skip=True):
        job = await self.store.start_measurement(input_id="mic", channel="left",
            measurement_scope=scope, skip_pre_sweep_diagnostics=skip, **(context or {}))
        await self.store._job_tasks[job["id"]]
        return self.store.get_job(job["id"])

    async def test_staged_layout_reaches_hardware_despite_false_legacy_overview(self):
        with patch.object(DSPRuntimeConfig, "from_overview", side_effect=AssertionError("legacy rebase")):
            job = await self.capture(self.context)
        self.assertIn("hardware boundary reached", job["error"]["detail"])
        self.assertEqual(self.spawned, ["pw-record"])
        self.assertIsNone(self.snapshots[0]["validation_failure"])
        self.assertEqual(self.snapshots[0]["output_mode"], "crossover")

    async def test_context_is_detached_before_first_await_and_from_returned_job(self):
        self.assertIn("expected_native_layout", inspect.signature(self.store.start_measurement).parameters)
        entered, resume = asyncio.Event(), asyncio.Event()
        prepare = self.store._prepare_measurement_job_setup

        async def pause_setup(**kwargs):
            entered.set()
            await resume.wait()
            return await prepare(**kwargs)

        self.store._prepare_measurement_job_setup = pause_setup
        task = asyncio.create_task(self.capture(self.context))
        await entered.wait()
        self.layout[0]["routes"][0]["gain"] = 0.123
        self.layout[0]["sos"][0][0] = 0.456
        self.context["expected_native_output_mode"] = "stereo"
        self.context["expected_plan_fingerprint"] = "mutated"
        resume.set()
        job = await task
        self.assertIn("hardware boundary reached", job["error"]["detail"])
        frozen = job["_expected_native_layout"]
        self.assertEqual(frozen, self.runtime["config"]["layout"])
        frozen[0]["routes"][0]["gain"] = 0
        self.assertEqual(self.store.get_job(job["id"])["_expected_native_layout"], self.runtime["config"]["layout"])

    async def test_invalid_context_fails_before_setup_or_registration(self):
        invalid = [{key: value} for key, value in self.context.items()]
        invalid += [{**self.context, key: value} for key, values in {
            "expected_native_layout": [[], {}, [None], [{}], [{"name": "x"}], [{"name": "x", "routes": []}],
                                       [{"name": "x", "routes": {}}], [{"name": "", "routes": []}],
                                       [{"name": "x", "routes": [], "extra": float("nan")}],
                                       [{"name": "x", "routes": [], "extra": object()}]],
            "expected_native_output_mode": ["", " ", "unknown", 4],
            "expected_plan_fingerprint": ["", " ", 5],
        }.items() for value in values]
        for context in invalid:
            with self.subTest(context=context), patch.object(self.store, "_prepare_measurement_job_setup",
                    side_effect=AssertionError("invalid context reached setup")):
                with self.assertRaises(ValueError):
                    await self.capture(context)
            self.assertEqual(self.store._jobs, {})
            self.assertEqual(self.store._job_tasks, {})
            self.assertEqual(list(self.store.job_records_dir.glob("*.json")), [])

    async def test_active_chain_cannot_override_target_with_trusted_context(self):
        with self.assertRaisesRegex(ValueError, "raw_helper"):
            await self.capture(self.context, scope="active_chain")
        self.assertEqual(self.store._jobs, {})

    async def test_http_form_fields_cannot_override_expected_context(self):
        import httpx
        from fastapi import FastAPI
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        import measurement.session as session

        app = FastAPI()
        app.include_router(session.router)
        services = SimpleNamespace(get_store=lambda: self.store, get_session=lambda: None,
                                   auto_sub_active=lambda: False)
        self.runtime["config"] = {"sample_rate": 48000, "output_mode": "stereo",
                                 "layout": list(DSPRuntimeConfig.from_overview(self.overview).layout)}
        with patch.object(session, "_measurement_services", return_value=services), \
             patch.object(session, "_resolve_measurement_start_sample_rate", return_value=48000), \
             patch.object(session, "_measurement_entry_preflight", new=AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/api/measurements/start", data={
                    "input_id": "mic", "channel": "left", "measurement_scope": "raw_helper",
                    "expected_native_layout": json.dumps(self.layout),
                    "expected_native_output_mode": "crossover",
                    "expected_plan_fingerprint": "http-override"})
            self.assertEqual(response.status_code, 200, response.text)
            job_id = response.json()["job"]["id"]
            await self.store._job_tasks[job_id]
        job = self.store.get_job(job_id)
        self.assertEqual(job["measurement_scope"], "active_chain")
        self.assertFalse(any(key.startswith("_expected_") for key in job))
        self.assertIn("hardware boundary reached", job["error"]["detail"])
        for name in self.context:
            self.assertNotIn(name, json.dumps(app.openapi()))

    async def test_tuple_runtime_layout_is_supported(self):
        job = await self.capture({**self.context, "expected_native_layout": tuple(self.layout)})
        self.assertIn("hardware boundary reached", job["error"]["detail"])

    async def test_nonfinite_or_nonserializable_nested_layout_is_rejected(self):
        for bad in (float("nan"), float("inf"), object(), {1, 2}):
            context = deepcopy(self.context)
            context["expected_native_layout"][0]["routes"][0]["gain"] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                await self.capture(context)
            self.assertEqual(self.store._jobs, {})

    async def test_same_length_deep_layout_mismatch_blocks_spawn(self):
        self.runtime["config"]["layout"][0]["sos"][0][0] = 0.123
        job = await self.capture(self.context)
        self.assertIn("layout does not match", job["error"]["detail"])
        self.assertEqual(self.spawned, [])

    async def test_fingerprint_check_runs_with_diagnostics_enabled_too(self):
        from subprocess import CompletedProcess
        self.store._routing._run = lambda *args, **kwargs: CompletedProcess(args[0], 0, "", "")
        self.runtime["config"]["plan_fingerprint"] = "other-plan"
        job = await self.capture(self.context, skip=False)
        self.assertIn("fingerprint", job["error"]["detail"])
        self.assertEqual(self.spawned, [])

    async def test_fingerprint_mismatch_with_identical_layout_blocks_spawn(self):
        self.runtime["config"]["plan_fingerprint"] = "other-plan"
        job = await self.capture(self.context)
        self.assertIn("fingerprint", job["error"]["detail"])
        self.assertEqual(self.spawned, [])

    async def test_missing_fingerprint_blocks_spawn(self):
        del self.runtime["config"]["plan_fingerprint"]
        job = await self.capture(self.context)
        self.assertIn("fingerprint", job["error"]["detail"])
        self.assertEqual(self.spawned, [])

    async def test_diagnostics_skip_preserves_all_runtime_checks(self):
        cases = [("active", False, "inactive"), ("effect_bypass", False, "bypassed"),
                 ("sample_rate", 44100, "rate"), ("output_mode", "stereo", "mode"),
                 ("layout", self.layout[:-1], "layout")]
        for key, value, message in cases:
            with self.subTest(key=key):
                target = self.runtime if key in self.runtime else self.runtime["config"]
                previous = target[key]
                target[key] = value
                job = await self.capture(self.context)
                target[key] = previous
                self.assertIn(message, job["error"]["detail"])
                self.assertEqual(self.snapshots[-1]["diagnostics"], "skipped")
                self.assertEqual(self.spawned, [])

    def test_route_builder_detaches_nested_layout_without_overview(self):
        route = self.store._routing._build_measurement_playback_route("play", {"target_name": "sink"},
            measurement_scope="raw_helper", **self.context)
        self.layout[0]["routes"][0]["gain"] = 0.5
        self.layout[0]["sos"][0][0] = 0.5
        self.assertEqual(route["expected_native_layout"], self.runtime["config"]["layout"])
        self.assertEqual(route["expected_plan_fingerprint"], "staged-plan-1")

    def test_route_builder_rejects_partial_or_empty_context_not_fallback(self):
        for context in ({"expected_native_layout": self.layout}, {**self.context, "expected_native_layout": []}):
            with self.subTest(context=context), self.assertRaises(ValueError):
                self.store._routing._build_measurement_playback_route("play", {},
                    measurement_scope="raw_helper", **context)

    async def test_regular_measurement_uses_unchanged_legacy_route_and_old_signature(self):
        self.runtime["config"] = {"sample_rate": 48000, "output_mode": "stereo",
                                 "layout": list(DSPRuntimeConfig.from_overview(self.overview).layout)}
        builder = self.store._routing._build_measurement_playback_route
        routes = []

        def old_builder(play_node_name, playback_target, *, measurement_scope):
            route = builder(play_node_name, playback_target, measurement_scope=measurement_scope)
            routes.append(route)
            return route

        self.store._routing._build_measurement_playback_route = old_builder
        job = await self.capture(scope="active_chain")
        self.assertIn("hardware boundary reached", job["error"]["detail"])
        self.assertEqual(set(routes[0]), {"route", "measurement_scope", "output_mode", "play_node_name",
                                         "playback_target_name", "expected_native_layout"})
        self.assertFalse(any(key.startswith("_expected_") for key in job))


if __name__ == "__main__":
    unittest.main(verbosity=2)
