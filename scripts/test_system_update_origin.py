# SPDX-License-Identifier: AGPL-3.0-only

"""Trusted-origin guard on the privileged system update/restore endpoints.

Regression coverage for two review findings:

* ``POST /api/system/update`` and ``POST /api/system/restore`` run
  ``update_fxroute.sh`` (git checkout, pip installs) and can restart the
  service, so they must apply the same trusted-origin defence as the
  provider admin, Qobuz auth, power and device-name endpoints.  A foreign
  or "null" Origin/Referer is rejected with 403 before any side effect;
  headerless CLI/systemd calls and same-origin browser calls stay allowed.
  The read-only ``GET /api/system/update`` check stays reachable from
  anywhere on the LAN.
* The shared origin helper must reject malformed ``Origin``/``Referer``
  ports (out of range, non-numeric) cleanly as untrusted instead of
  raising ``ValueError`` and turning the designed 403 into a 500.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import http_origin  # noqa: E402
import main  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402


def _scope_request(
    headers: dict[str, str],
    *,
    host: str = "testserver",
    port: int = 80,
    scheme: str = "http",
) -> Request:
    """Build a minimal Request scope for direct helper unit tests."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/system/update",
        "scheme": scheme,
        "server": (host, port),
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        "query_string": b"",
    }
    return Request(scope)


def _headerless_request(path: str = "/api/system/update") -> Request:
    """Request scope emulating a headerless CLI/systemd caller."""
    return _scope_request({}, host=path)


class MalformedOriginPortTests(unittest.TestCase):
    """Invalid header ports are rejected as untrusted, never raise."""

    def test_out_of_range_origin_port_is_untrusted(self):
        request = _scope_request({"host": "testserver", "origin": "http://testserver:99999"})
        self.assertFalse(http_origin.is_request_origin_trusted(request))

    def test_out_of_range_referer_port_is_untrusted(self):
        request = _scope_request({"host": "testserver", "referer": "http://testserver:99999/x"})
        self.assertFalse(http_origin.is_request_origin_trusted(request))

    def test_non_numeric_origin_port_is_untrusted(self):
        request = _scope_request({"host": "testserver", "origin": "http://testserver:abc"})
        self.assertFalse(http_origin.is_request_origin_trusted(request))

    def test_out_of_range_port_does_not_raise(self):
        # The designed outcome is a clean False (-> HTTP 403), not a 500.
        request = _scope_request({"host": "testserver", "origin": "http://testserver:99999"})
        try:
            result = http_origin.is_request_origin_trusted(request)
        except ValueError as exc:  # pragma: no cover - failure path
            self.fail(f"origin helper raised ValueError instead of rejecting: {exc}")
        self.assertFalse(result)

    def test_malformed_host_header_port_fails_closed(self):
        # A malformed Host port must not let the default-port fallback
        # accept a headerless-port Origin.
        request = _scope_request(
            {"host": "testserver:abc", "origin": "http://testserver"}
        )
        self.assertEqual(http_origin.effective_request_port(request), -1)
        self.assertFalse(http_origin.is_request_origin_trusted(request))

    def test_same_origin_without_port_is_trusted(self):
        request = _scope_request({"host": "testserver", "origin": "http://testserver"})
        self.assertTrue(http_origin.is_request_origin_trusted(request))

    def test_same_origin_with_matching_explicit_port_is_trusted(self):
        # Scope without a host header: the effective port comes from the
        # server tuple (8080), matching the Origin's explicit port.
        request = _scope_request({"origin": "http://testserver:8080"}, port=8080)
        self.assertTrue(http_origin.is_request_origin_trusted(request))

    def test_headerless_caller_stays_trusted(self):
        request = _scope_request({"host": "testserver"})
        self.assertTrue(http_origin.is_request_origin_trusted(request))

    def test_foreign_origin_stays_untrusted(self):
        request = _scope_request(
            {"host": "testserver", "origin": "https://evil.example.com"}
        )
        self.assertFalse(http_origin.is_request_origin_trusted(request))


def _update_result(returncode: int = 1, stdout: str = "", stderr: str = "") -> dict:
    return {"returncode": returncode, "stdout": stdout, "stderr": stderr}


class SystemUpdateOriginGuardTests(unittest.TestCase):
    """POST /api/system/update|restore share the cross-site origin guard."""

    FOREIGN_ORIGIN = {"Origin": "https://evil.example.com"}
    NULL_ORIGIN = {"Origin": "null"}

    def setUp(self):
        import main as main_module

        self.main = main_module
        self.client = TestClient(main_module.app)

    def test_cross_site_update_is_rejected_and_never_runs_the_script(self):
        with mock.patch.object(
            self.main, "_run_update_operation", new_callable=mock.AsyncMock
        ) as op_mock:
            resp = self.client.post(
                "/api/system/update", json={}, headers=self.FOREIGN_ORIGIN
            )
        self.assertEqual(resp.status_code, 403)
        op_mock.assert_not_called()

    def test_cross_site_restore_is_rejected_and_never_runs_the_script(self):
        with mock.patch.object(
            self.main, "_run_update_operation", new_callable=mock.AsyncMock
        ) as op_mock:
            resp = self.client.post(
                "/api/system/restore", json={}, headers=self.FOREIGN_ORIGIN
            )
        self.assertEqual(resp.status_code, 403)
        op_mock.assert_not_called()

    def test_null_origin_is_rejected(self):
        with mock.patch.object(
            self.main, "_run_update_operation", new_callable=mock.AsyncMock
        ) as op_mock:
            resp = self.client.post(
                "/api/system/restore", json={}, headers=self.NULL_ORIGIN
            )
        self.assertEqual(resp.status_code, 403)
        op_mock.assert_not_called()

    def test_malformed_origin_port_is_rejected_not_500(self):
        with mock.patch.object(
            self.main, "_run_update_operation", new_callable=mock.AsyncMock
        ) as op_mock:
            resp = self.client.post(
                "/api/system/update",
                json={},
                headers={"Origin": "http://testserver:99999"},
            )
        self.assertEqual(resp.status_code, 403, resp.text)
        op_mock.assert_not_called()

    def test_headerless_post_is_allowed_and_shape_unchanged(self):
        # TestClient sends no Origin/Referer: the legitimate CLI/systemd path.
        with mock.patch.object(
            self.main,
            "_run_update_operation",
            new_callable=mock.AsyncMock,
            return_value=_update_result(),
        ):
            resp = self.client.post("/api/system/update", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(
            set(data.keys()),
            {"ok", "installed_version", "restart_scheduled", "service_name",
             "returncode", "stdout", "stderr"},
            "result shape must be unchanged",
        )
        self.assertFalse(data["ok"])
        self.assertFalse(data["restart_scheduled"])

    def test_failed_restore_reports_restart_not_scheduled(self):
        with mock.patch.object(
            self.main,
            "_run_update_operation",
            new_callable=mock.AsyncMock,
            return_value=_update_result(returncode=1),
        ):
            resp = self.client.post("/api/system/restore", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["restart_scheduled"])

    def test_get_check_endpoint_stays_unguarded(self):
        # The read-only check stays reachable from anywhere on the LAN.
        with mock.patch.object(
            self.main,
            "_run_update_operation",
            new_callable=mock.AsyncMock,
            return_value=_update_result(),
        ) as op_mock:
            resp = self.client.get(
                "/api/system/update", headers=self.FOREIGN_ORIGIN
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        op_mock.assert_awaited_once()


class SystemUpdateOriginWiringTests(unittest.TestCase):
    """Source wiring: guard runs inside the shared body, before the script."""

    @classmethod
    def setUpClass(cls):
        cls.main_text = (ROOT / "main.py").read_text()

    def _block(self, start_marker: str, end_marker: str) -> str:
        start = self.main_text.index(start_marker)
        end = self.main_text.index(end_marker, start)
        return self.main_text[start:end]

    def test_shared_body_guards_before_running_the_script(self):
        block = self._block(
            "async def _system_update_or_restore", "\n@app.post(\"/api/system/update\")"
        )
        self.assertIn("is_request_origin_trusted", block)
        self.assertIn("_run_update_operation", block)
        self.assertLess(
            block.index("is_request_origin_trusted"),
            block.index("_run_update_operation"),
            "the origin gate must run before any update-script side effect",
        )
        self.assertIn('status_code=403', block)

    def test_both_post_endpoints_delegate_to_the_guarded_body(self):
        update_block = self._block(
            "@app.post(\"/api/system/update\")", "\n@app.post(\"/api/system/restore\")"
        )
        restore_block = self._block(
            "@app.post(\"/api/system/restore\")", "\n\n\n\n@app.get(\"/api/audio/samplerate\")"
        )
        self.assertIn("_system_update_or_restore", update_block)
        self.assertIn("_system_update_or_restore", restore_block)

    def test_restore_passes_the_restore_flag(self):
        restore_block = self._block(
            "@app.post(\"/api/system/restore\")", "\n\n\n\n@app.get(\"/api/audio/samplerate\")"
        )
        self.assertIn('"--restore"', restore_block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
