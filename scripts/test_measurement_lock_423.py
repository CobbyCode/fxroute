#!/usr/bin/env python3
"""Measurement locks: samplerate policy and output-state apply are 423-locked.

Observable contracts of the two privileged audio routes while a measurement
session has active jobs:

- POST /api/audio/samplerate -> HTTP 423, the rate-policy transition never
  starts (the lock runs before any body parsing or mutation);
- POST /api/audio/output-state/apply -> HTTP 423, the output-state mutation
  never runs (the lock runs before the output service is even resolved).

A dropped or reordered lock would let a policy/topology change fight the
active measurement rate. Only the lock ordering is pinned here (status code
plus proof that the downstream mutation entry points were never reached).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from fastapi import HTTPException
from starlette.requests import Request


def _request(path: str, payload: dict | None = None) -> Request:
    body = json.dumps(payload or {}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "scheme": "http",
            "server": ("testserver", 80),
            "headers": [(b"host", b"testserver")],
            "query_string": b"",
        },
        receive=receive,
    )


def _active_measurement():
    return patch.object(
        main, "measurement_sr_session", SimpleNamespace(has_active_jobs=True)
    )


class MeasurementLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_samplerate_policy_change_is_locked_during_measurement(self):
        transition = AsyncMock()
        with (
            _active_measurement(),
            patch.object(main, "_transition_sample_rate_policy", new=transition),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await main.save_audio_samplerate_policy(
                    _request("/api/audio/samplerate")
                )
        self.assertEqual(ctx.exception.status_code, 423)
        self.assertIn("locked", ctx.exception.detail)
        transition.assert_not_awaited()

    async def test_output_state_apply_is_locked_during_measurement(self):
        service = Mock(side_effect=AssertionError("service resolved during lock"))
        with (
            _active_measurement(),
            patch.object(main, "get_output_service", new=service),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await main.apply_audio_output_state(
                    _request("/api/audio/output-state/apply", {
                        "expected_revision": 1,
                        "mutation": {"kind": "set_bank_preset", "mode": "stereo-sub",
                                     "bank_id": "sub1", "preset": "Room"},
                    })
                )
        self.assertEqual(ctx.exception.status_code, 423)
        self.assertIn("locked", ctx.exception.detail)
        service.assert_not_called()


class LateMeasurementOwnerTests(unittest.IsolatedAsyncioTestCase):
    """An edit that passed the lock loses to a job that took the graph meanwhile."""

    async def test_output_state_apply_rechecks_the_owner_before_commit(self):
        import asyncio
        import threading
        from dataclasses import replace
        from audio.output_service import OutputService
        from test_measurement_speaker_commit import SessionFixture

        class Rig(SessionFixture, unittest.TestCase):
            def runTest(self):
                pass

        rig = Rig()
        rig.setUp()
        self.addCleanup(rig.doCleanups)
        owner = SimpleNamespace(has_active_jobs=False)
        service = OutputService(replace(
            rig.service._deps, measurement_active=lambda: owner.has_active_jobs))
        overview_entered = threading.Event()
        release_overview = threading.Event()
        self.addCleanup(release_overview.set)

        def slow_overview():
            overview_entered.set()
            if not release_overview.wait(5):
                raise TimeoutError("overview gate was not released")
            return {"selected_output": {"key": "dev", "channels": 6},
                    "output_mode": {"effective_output_key": "dev", "effective_output_channels": 6,
                                    "hardware_playback_ports": [f"playback_AUX{i}" for i in range(6)]}}

        rebuild = AsyncMock()
        runtime = SimpleNamespace(snapshot=lambda: {"output_gain_db": 0.0},
                                  guarded_rebuild_rendered=rebuild, sync_rendered=AsyncMock())
        with (
            patch.object(main, "get_output_service", return_value=service),
            patch.object(main, "measurement_sr_session", owner),
            patch.object(main, "get_audio_output_overview", new=slow_overview),
            patch.object(main, "get_samplerate_status", return_value={"active_rate": 48000}),
            patch.object(main, "_require_dsp_manager", return_value=rig.manager),
            patch.object(main.runtime, "dsp_runtime", runtime),
        ):
            worker = asyncio.create_task(main._apply_audio_output_state_body({
                "expected_revision": rig.base["revision"],
                "mutation": {"kind": "set_processing", "mode": "stereo-sub",
                             "role": "left_low", "level_db": -5.0}}))
            self.assertTrue(await asyncio.to_thread(overview_entered.wait, 3))
            # Speaker Align registers its owner while the edit is preparing.
            owner.has_active_jobs = True
            release_overview.set()
            with self.assertRaises(HTTPException) as ctx:
                await worker
        self.assertEqual(ctx.exception.status_code, 423)
        self.assertEqual(service.load()["revision"], rig.base["revision"])
        self.assertEqual(
            service.load()["modes"]["stereo-sub"]["processing"]["left_low"]["level_db"], 0.0)
        rebuild.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
