#!/usr/bin/env python3
"""Central trusted-origin enforcement for unsafe HTTP methods."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import http_origin  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402


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


FOREIGN_HOST = "attacker.example"


def _scope_request(
    headers: dict[str, str],
    *,
    peer: tuple[str, int] | None = None,
    scheme: str = "http",
    server: tuple[str, int] = ("192.168.1.5", 8000),
) -> Request:
    """Minimal Request scope with a controllable TCP peer."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/play",
        "scheme": scheme,
        "server": server,
        "headers": [
            (key.lower().encode(), value.encode()) for key, value in headers.items()
        ],
        "query_string": b"",
    }
    if peer is not None:
        scope["client"] = peer
    return Request(scope)


class HostAllowlistTests(unittest.TestCase):
    """The Host allowlist is the DNS-rebinding gate on every request."""

    def test_ip_host_is_allowed(self):
        client = TestClient(main.app, base_url="http://192.168.178.104:8000")
        response = client.get("/")
        self.assertNotEqual(response.status_code, 403, response.text)

    def test_single_label_host_is_allowed(self):
        # The default TestClient host is the single-label "testserver".
        response = TestClient(main.app).get("/")
        self.assertNotEqual(response.status_code, 403, response.text)

    def test_builtin_local_suffix_is_allowed(self):
        client = TestClient(main.app, base_url="http://fxroute.local:8000")
        response = client.get("/")
        self.assertNotEqual(response.status_code, 403, response.text)

    def test_foreign_dotted_host_is_rejected_on_safe_methods(self):
        client = TestClient(main.app, base_url=f"http://{FOREIGN_HOST}")
        response = client.get("/")
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json().get("detail"), "Host not allowed")

    def test_rebinding_style_same_origin_post_is_rejected_by_host(self):
        # Rebinding: Origin and Host agree on the attacker's name, which
        # would satisfy the same-origin verdict without the allowlist.
        client = TestClient(main.app, base_url=f"http://{FOREIGN_HOST}")
        response = client.post(
            "/api/play",
            json={},
            headers={"Origin": f"http://{FOREIGN_HOST}"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json().get("detail"), "Host not allowed")

    def test_allowed_hosts_env_accepts_exact_name(self):
        client = TestClient(main.app, base_url="http://fxroute.example.test")
        with mock.patch.dict(
            os.environ, {"FXROUTE_ALLOWED_HOSTS": "fxroute.example.test"}
        ):
            allowed = client.get("/")
        denied = client.get("/")
        self.assertNotEqual(allowed.status_code, 403, allowed.text)
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json().get("detail"), "Host not allowed")

    def test_allowed_hosts_env_accepts_suffix_rule(self):
        client = TestClient(main.app, base_url="http://stream.example.test")
        with mock.patch.dict(os.environ, {"FXROUTE_ALLOWED_HOSTS": ".example.test"}):
            allowed = client.get("/")
        self.assertNotEqual(allowed.status_code, 403, allowed.text)


class ForwardedHeaderTrustTests(unittest.TestCase):
    """X-Forwarded-* is honored only from trusted proxy peers."""

    HEADERS = {
        "host": "192.168.1.5:8000",
        "x-forwarded-host": "fxroute.lan",
        "x-forwarded-port": "80",
        "x-forwarded-proto": "https",
    }

    def test_forwarded_headers_honored_from_loopback_peer(self):
        request = _scope_request(self.HEADERS, peer=("127.0.0.1", 51234))
        self.assertEqual(http_origin.effective_request_host(request), "fxroute.lan")
        self.assertEqual(http_origin.effective_request_port(request), 80)
        self.assertEqual(http_origin.effective_request_scheme(request), "https")

    def test_forwarded_headers_ignored_from_untrusted_peer(self):
        request = _scope_request(self.HEADERS, peer=("203.0.113.9", 44000))
        self.assertEqual(http_origin.effective_request_host(request), "192.168.1.5")
        self.assertEqual(http_origin.effective_request_port(request), 8000)
        self.assertEqual(http_origin.effective_request_scheme(request), "http")

    def test_missing_peer_information_fails_closed(self):
        request = _scope_request(self.HEADERS)
        self.assertEqual(http_origin.effective_request_host(request), "192.168.1.5")
        self.assertEqual(http_origin.effective_request_port(request), 8000)

    def test_forwarded_headers_honored_from_env_configured_peer(self):
        with mock.patch.dict(os.environ, {"FXROUTE_TRUSTED_PROXIES": "203.0.113.9"}):
            request = _scope_request(self.HEADERS, peer=("203.0.113.9", 44000))
            self.assertEqual(http_origin.effective_request_host(request), "fxroute.lan")

    def test_forged_forwarded_host_cannot_rescue_foreign_origin(self):
        # Untrusted peer: the forged X-Forwarded-Host is ignored, so the
        # foreign Origin no longer matches the effective host.
        request = _scope_request(
            {
                "host": "192.168.1.5:8000",
                "origin": "https://evil.example",
                "x-forwarded-host": "evil.example",
                "x-forwarded-proto": "https",
            },
            peer=("203.0.113.9", 44000),
        )
        self.assertFalse(http_origin.is_request_origin_trusted(request))

    def test_forwarded_host_still_must_pass_allowlist(self):
        # A trusted proxy presenting a rebound name is refused as well.
        request = _scope_request(
            {"host": "192.168.1.5:8000", "x-forwarded-host": FOREIGN_HOST},
            peer=("127.0.0.1", 51234),
        )
        self.assertFalse(http_origin.is_request_host_allowed(request))


if __name__ == "__main__":
    unittest.main(verbosity=2)
