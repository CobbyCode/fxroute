#!/usr/bin/env python3
"""Measurement locks: samplerate policy and output-mode switches are 423-locked.

Observable contracts of the two privileged audio routes while a measurement
session has active jobs:

- POST /api/audio/samplerate -> HTTP 423, the rate-policy transition never
  starts (the lock runs before any body parsing or mutation);
- POST /api/audio/output-mode -> HTTP 423, the output-mode preparation never
  runs (the lock runs before any DSP/routing mutation).

A dropped or reordered lock would let a policy/mode change fight the active
measurement rate. Only the lock ordering is pinned here (status code plus
proof that the downstream mutation entry points were never reached).
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

    async def test_output_mode_change_is_locked_during_measurement(self):
        prepare = Mock()
        with (
            _active_measurement(),
            patch.object(main, "prepare_audio_output_mode", new=prepare),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await main.save_audio_output_mode_route(
                    _request("/api/audio/output-mode", {"mode": "stereo"})
                )
        self.assertEqual(ctx.exception.status_code, 423)
        self.assertIn("locked", ctx.exception.detail)
        prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
