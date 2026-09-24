#!/usr/bin/env python3
"""Central trusted-origin enforcement for unsafe HTTP methods."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


FOREIGN_ORIGIN = {"Origin": "https://evil.example"}


class BackendOriginMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def assert_cross_origin_rejected(self, response):
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json().get("detail"), "Cross-site request rejected")

    def test_foreign_origin_json_post_is_rejected_before_play_handler(self):
        response = self.client.post("/api/play", json={}, headers=FOREIGN_ORIGIN)
        self.assert_cross_origin_rejected(response)

    def test_foreign_origin_multipart_post_is_rejected_before_upload(self):
        calls = []

        class FakeManager:
            def upload_ir(self, *_args):
                calls.append("upload_ir")
                return {"filename": "test.wav"}

            def get_status(self):
                return {}

        with mock.patch.object(main, "dsp_manager", FakeManager()):
            response = self.client.post(
                "/api/dsp/irs/upload",
                headers=FOREIGN_ORIGIN,
                files={"file": ("test.wav", b"test", "audio/wav")},
            )
        self.assertEqual(calls, [], "cross-origin upload reached the DSP handler")
        self.assert_cross_origin_rejected(response)

    def test_foreign_origin_urlencoded_form_post_is_rejected_before_measurement_handler(self):
        response = self.client.post(
            "/api/measurements/start",
            data={"input_id": "example"},
            headers=FOREIGN_ORIGIN,
        )
        self.assert_cross_origin_rejected(response)

    def test_foreign_origin_patch_is_rejected_before_measurement_handler(self):
        response = self.client.patch(
            "/api/measurements/settings",
            json={"measurementSampleRate": 48000},
            headers=FOREIGN_ORIGIN,
        )
        self.assert_cross_origin_rejected(response)

    def test_foreign_origin_put_is_rejected_before_station_handler(self):
        response = self.client.put(
            "/api/stations/example",
            json={"name": "Example", "stream_url": "https://example.com/stream"},
            headers=FOREIGN_ORIGIN,
        )
        self.assert_cross_origin_rejected(response)

    def test_foreign_origin_delete_is_rejected_before_playlist_handler(self):
        response = self.client.delete("/api/playlists/example", headers=FOREIGN_ORIGIN)
        self.assert_cross_origin_rejected(response)

    def test_same_origin_json_post_remains_allowed(self):
        response = self.client.post(
            "/api/play",
            json={},
            headers={"Origin": "http://testserver"},
        )
        self.assertNotEqual(response.status_code, 403, response.text)

    def test_headerless_json_post_remains_allowed(self):
        response = self.client.post("/api/play", json={})
        self.assertNotEqual(response.status_code, 403, response.text)

    def test_foreign_origin_get_remains_allowed(self):
        with mock.patch.object(
            main,
            "_run_update_operation",
            new_callable=mock.AsyncMock,
            return_value={"returncode": 0, "stdout": "", "stderr": ""},
        ) as operation:
            response = self.client.get("/api/system/update", headers=FOREIGN_ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        operation.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
