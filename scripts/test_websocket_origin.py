#!/usr/bin/env python3
"""WebSocket handshake gate: Host allowlist + origin check before /ws runs.

The browser's WebSocket API performs no CORS check, so before this gate a
foreign page could open ws://<lan-ip>/ws itself and read the init payload.
Both checks run in TrustedOriginMiddleware before the endpoint connects.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.testclient import WebSocketDenialResponse  # noqa: E402

FOREIGN_ORIGIN = "https://evil.example"
REBINDING_HOST = "attacker.example"


class WebSocketOriginTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def _connect(self, url: str = "/ws", **headers):
        return self.client.websocket_connect(url, headers=headers)

    def _assert_denied(self, url: str, expected_detail: str, **headers):
        with self.assertRaises(WebSocketDenialResponse) as caught:
            with self._connect(url, **headers):
                pass
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.json().get("detail"), expected_detail)

    def test_same_origin_handshake_receives_init(self):
        with self._connect(Origin="http://testserver") as session:
            payload = session.receive_json()
        self.assertEqual(payload.get("type"), "init")

    def test_headerless_non_browser_client_is_accepted(self):
        # CLI/scripted callers send no Origin, matching the HTTP baseline.
        with self._connect() as session:
            payload = session.receive_json()
        self.assertEqual(payload.get("type"), "init")

    def test_foreign_origin_handshake_is_denied(self):
        self._assert_denied(
            "/ws", "Cross-site request rejected", Origin=FOREIGN_ORIGIN
        )

    def test_sandboxed_origin_null_is_denied(self):
        self._assert_denied("/ws", "Cross-site request rejected", Origin="null")

    def test_rebinding_host_is_denied_even_when_origin_matches(self):
        # Rebinding: page Origin and Host agree on the attacker's name,
        # which only the Host allowlist can catch.
        self._assert_denied(
            f"ws://{REBINDING_HOST}/ws",
            "Host not allowed",
            Origin=f"http://{REBINDING_HOST}",
        )

    def test_headerless_foreign_host_is_denied(self):
        self._assert_denied(f"ws://{REBINDING_HOST}/ws", "Host not allowed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
