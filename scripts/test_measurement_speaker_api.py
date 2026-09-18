#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Speaker Align HTTP maps service outcomes to status codes, nothing more."""

import sys
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.speaker_api import build_speaker_align_service, configure_speaker_align
from measurement.speaker_service import (
    SpeakerAlignBusyError,
    SpeakerAlignService,
    SpeakerAlignStaleError,
)


def job_summary(status="acquiring", job_id="job-1"):
    return {"id": job_id, "side": "left", "status": status,
            "message": "Acquiring speaker ways…", "dry_run": False,
            "params": {"input_id": "mic"}, "result": None, "error": None}


class FakeService:
    """HTTP-boundary double: canned outcomes, real exception taxonomy."""

    def __init__(self):
        self.calls = []
        self.start_result = "job-1"
        self.jobs_result = [job_summary()]
        self.status_result = job_summary()
        self.cancel_result = None

    def start(self, side, **kwargs):
        self.calls.append(("start", side, deepcopy(kwargs)))
        if isinstance(self.start_result, BaseException):
            raise self.start_result
        return self.start_result

    def jobs(self):
        return deepcopy(self.jobs_result)

    def status(self, job_id):
        if isinstance(self.status_result, BaseException):
            raise self.status_result
        summary = deepcopy(self.status_result)
        summary["id"] = job_id
        return summary

    def cancel(self, job_id):
        self.calls.append(("cancel", job_id))
        if isinstance(self.cancel_result, BaseException):
            raise self.cancel_result
        summary = deepcopy(self.status_result)
        summary["id"] = job_id
        summary["status"] = "cancelled"
        return summary


class SpeakerApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        httpx = __import__("httpx")
        from fastapi import FastAPI
        import measurement.speaker_api as speaker_api
        self.fake = FakeService()
        speaker_api.configure_speaker_align(lambda: self.fake)
        self.addCleanup(lambda: speaker_api.configure_speaker_align(lambda: None))
        app = FastAPI()
        app.include_router(speaker_api.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def test_start_returns_job(self):
        response = await self.client.post("/api/speaker-align/start", json={
            "side": "left", "input_id": "mic", "reference_input_channel": "2",
            "reference_id": "interface:input-2:upstream",
            "microphone_position_id": "seat-1-fixed", "dry_run": True})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["job"]["id"], "job-1")
        name, side, kwargs = self.fake.calls[0]
        self.assertEqual(side, "left")
        self.assertTrue(kwargs["dry_run"])
        self.assertEqual(kwargs["reference_input_channel"], "2")

    async def test_start_validation_is_400(self):
        self.fake.start_result = ValueError("Speaker Align side must be left or right")
        response = await self.client.post("/api/speaker-align/start", json={"side": "center"})
        self.assertEqual(response.status_code, 400, response.text)

    async def test_start_stale_is_409(self):
        self.fake.start_result = SpeakerAlignStaleError("stale")
        response = await self.client.post("/api/speaker-align/start", json={"side": "left"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("stale", response.text)

    async def test_start_busy_is_409(self):
        self.fake.start_result = SpeakerAlignBusyError("already running")
        response = await self.client.post("/api/speaker-align/start", json={"side": "left"})
        self.assertEqual(response.status_code, 409, response.text)

    async def test_non_object_body_is_400(self):
        response = await self.client.post("/api/speaker-align/start", content="[]",
                                          headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fake.calls, [])

    async def test_malformed_json_is_400(self):
        response = await self.client.post("/api/speaker-align/start", content="{oops",
                                          headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fake.calls, [])

    async def test_non_boolean_dry_run_is_400(self):
        response = await self.client.post("/api/speaker-align/start", json={
            "side": "left", "dry_run": "false"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fake.calls, [])

    async def test_missing_input_id_reaches_service_validation(self):
        self.fake.start_result = ValueError("Speaker Align service requires a capture input id")
        response = await self.client.post("/api/speaker-align/start", json={"side": "left"})
        self.assertEqual(response.status_code, 400, response.text)

    async def test_unconfigured_service_is_503(self):
        import measurement.speaker_api as speaker_api
        speaker_api.configure_speaker_align(lambda: None)
        try:
            response = await self.client.post("/api/speaker-align/start", json={"side": "left"})
            self.assertEqual(response.status_code, 503, response.text)
            response = await self.client.get("/api/speaker-align/jobs")
            self.assertEqual(response.status_code, 503, response.text)
        finally:
            speaker_api.configure_speaker_align(lambda: self.fake)

    async def test_list_and_get_jobs(self):
        response = await self.client.get("/api/speaker-align/jobs")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["jobs"][0]["id"], "job-1")
        response = await self.client.get("/api/speaker-align/jobs/job-9")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["job"]["id"], "job-9")

    async def test_unknown_job_is_404(self):
        self.fake.status_result = KeyError("nope")
        response = await self.client.get("/api/speaker-align/jobs/nope")
        self.assertEqual(response.status_code, 404, response.text)
        self.fake.cancel_result = KeyError("nope")
        response = await self.client.post("/api/speaker-align/jobs/nope/cancel")
        self.assertEqual(response.status_code, 404, response.text)

    async def test_cancel_returns_cancelled_job(self):
        response = await self.client.post("/api/speaker-align/jobs/job-1/cancel")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["job"]["status"], "cancelled")
        self.assertIn(("cancel", "job-1"), self.fake.calls)


class CompositionFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_factory_wires_boundaries_end_to_end(self):
        import tempfile
        import numpy as np
        from audio.output_service import OutputService, OutputServiceDeps
        from audio.output_state import default_output_state, set_mode_routing, switch_mode, set_crossover
        from dsp.manager import DSPManager
        from audio.output_state_store import OutputStateStore
        from measurement.target import REFERENCE_TAP_INGRESS

        rate = 48000
        directory = tempfile.TemporaryDirectory(prefix="speaker-api-compose-")
        self.addCleanup(directory.cleanup)
        home = Path(directory.name)
        manager = DSPManager(home=home / "dsp")
        manager.preset_store.write("Room", {
            "schema": "fxroute.dsp.preset", "version": 1, "chain": []})
        store = OutputStateStore(home / "output-state.json")
        output_service = OutputService(OutputServiceDeps(
            store=store, preset_loader=manager.preset_store.read,
            resolve_ir=lambda name: (_ for _ in ()).throw(AssertionError(name)),
            measurement_active=lambda: False))
        state = set_crossover(default_output_state(), "stereo-sub", True)
        state = set_mode_routing(
            state, "stereo-sub", "dev",
            [f"{side}_{way}" for side in ("right", "left") for way in ("high", "low")]
            + ["sub1", "left_low"])
        state = switch_mode(state, "stereo-sub")
        processing = state["modes"]["stereo-sub"]["processing"]
        for side in ("left", "right"):
            processing[f"{side}_low"]["lowpass"] = {
                "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000,
            }
            processing[f"{side}_high"]["highpass"] = {
                "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000,
            }
        for config in state["modes"].values():
            for bank in config["banks"].values():
                bank.update(preset="Room", preset_a="Neutral", preset_b="Room")
        head = store.commit(state, expected_revision=0)
        runtime = {"fingerprint": None, "gain_db": 0.0}
        ports = [f"playback_AUX{i}" for i in range(6)]

        class Native:
            async def guarded_rebuild_rendered(self, new, **kwargs):
                kwargs["apply_candidate"]()
                runtime["fingerprint"] = new.config.plan_fingerprint
                if kwargs.get("before_ramp") is not None:
                    await kwargs["before_ramp"]()
                runtime["gain_db"] = float(kwargs.get("ramp_target_db", 0.0))

            async def verify(self):
                return True

            def snapshot(self):
                return {"active": True, "helper_pid": 7,
                        "config": {"plan_fingerprint": runtime["fingerprint"],
                                   "sample_rate": rate, "output_key": "dev",
                                   "hardware_ports": list(ports)},
                        "output_gain_db": runtime["gain_db"]}

        native = Native()
        calls = {"n": 0}

        def lowpass_kernel(cutoff):
            offsets = np.arange(-256, 257, dtype=float)
            sigma = np.sqrt(2 * np.log(2)) * rate / (2 * np.pi * cutoff)
            kernel = np.exp(-0.5 * (offsets / sigma) ** 2)
            return kernel / np.sum(kernel)

        async def capture_runner(store_arg, alignment, **kwargs):
            self.assertIs(store_arg, None)
            calls["n"] += 1
            arrivals = (96, 240) if calls["n"] == 1 else (500, 500)
            delta = np.zeros(513)
            delta[256] = 1
            lowpass = lowpass_kernel(2000)
            boundaries = [np.zeros(513), lowpass, delta]
            captures = []
            for index, request in enumerate(alignment.capture_requests()):
                reference = 700 + index * 131
                arrival = reference + arrivals[index]
                ir = np.zeros(4096)
                ir[arrival - 256:arrival + 257] = boundaries[index + 1] - boundaries[index]
                captures.append({
                    "role": request["role"],
                    "measurement_target": request["measurement_target"],
                    "reference_id": request["reference_id"],
                    "microphone_position_id": request["microphone_position_id"],
                    "reference_tap": REFERENCE_TAP_INGRESS,
                    "time_reference": "deconvolved-sweep-origin",
                    "impulse_response": ir,
                    "analysis": {
                        "sample_rate": rate, "peak_dbfs": -12.0,
                        "quality_checks": {"status": "pass", "items": []},
                        "reference_path": {
                            "usable": True, "electrical_reference_used": True,
                            "timing_status": "electrical-reference", "stability": "stable",
                            "confidence": 0.95, "clipped": False, "peak_dbfs": -9.0,
                        },
                        "impulse_response": {
                            "direct_arrival_index": arrival,
                            "reference_peak_index": reference,
                            "direct_confidence": 0.95,
                            "timing_source": "direct_arrival_minus_reference_peak",
                        },
                    },
                })
            return {"captures": captures, "provenance": {"job_ids": ["composed"]}}

        service = build_speaker_align_service(
            output_service=output_service, measurement_store=None, dsp_manager=manager,
            get_native_runtime=lambda: native,
            describe_device=lambda state: {"output_key": "dev", "channels": 6,
                                           "hardware_ports": list(ports)},
            get_measurement_rate=lambda: rate,
            capture_runner=capture_runner)
        self.assertIsInstance(service, SpeakerAlignService)
        job_id = service.start(
            "left", input_id="mic", reference_input_channel="2",
            reference_id="interface:input-2:upstream",
            microphone_position_id="seat-1-fixed")
        job = await service.wait_for(job_id, timeout_seconds=30)
        self.assertEqual(job["status"], "committed", job)
        self.assertEqual(job["result"]["committed_revision"], head["revision"] + 1)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(output_service.load()["revision"], head["revision"] + 1)


if __name__ == "__main__":
    unittest.main()
