# SPDX-License-Identifier: AGPL-3.0-only

"""Qobuz/qbzd login orchestration tests.

The qbzd CLI owns the actual OAuth flow (``qbzd login`` prints the upstream
URL and accepts the pasted redirect; ``qbzd logout`` clears the credential).
These tests verify that FXRoute only orchestrates that process correctly:

* URL extraction from the login banner (including percent-encoded redirects)
* code extraction from pasted redirect URLs
* begin/finish happy path against a fake qbzd process
* single-flight guard (second begin returns already-in-progress)
* logout invocation
* provider delegation and the HTTP endpoint contract
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import streaming.qobuz.login as login  # noqa: E402

BANNER = (
    "Open this URL in a browser and sign in to Qobuz:\n"
    "  https://www.qobuz.com/signin/oauth?ext_app_id=798273057"
    "&redirect_url=http%3A%2F%2F127.0.0.1%3A43717%2Fdcc26f6c715fafe96c933521d85562723db08f1c4bad8600\n"
    "\n"
    "Your browser will land on a page that fails to load — that is expected.\n"
    "Paste the full redirect URL (or just the code) here: "
)


class ParseTests(unittest.TestCase):
    def test_extracts_upstream_url_from_banner(self):
        url, _ = login._parse_login_url(BANNER) if hasattr(login, "_parse_login_url") else (None, None)
        # The function lives inline in _read_until_url; exercise the pattern.
        match = login._URL_PATTERN.search(BANNER)
        self.assertIsNotNone(match)
        self.assertTrue(match.group(0).startswith("https://www.qobuz.com/signin/oauth"))

    def test_code_extraction_from_pasted_redirect(self):
        pasted = (
            "http://127.0.0.1:43717/dcc26f6c715fafe96c933521d85562723db08f1c4bad8600"
            "?code=abc-DEF_123~x"
        )
        match = login._CODE_PATTERN.search(pasted)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "abc-DEF_123~x")

    def test_code_extraction_without_code_returns_none(self):
        self.assertIsNone(login._CODE_PATTERN.search("https://example.com/nothing"))


class _FakeProc:
    """Minimal subprocess stand-in for the qbzd login flow."""

    def __init__(self, stdout_lines, returncode=0):
        import asyncio

        self.stdout = _FakeReader(stdout_lines)
        self.stdin = _FakeWriter()
        self.returncode = None
        self.terminated = False
        self.killed = False

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    async def communicate(self):
        output = b""
        while True:
            line = await self.stdout.readline()
            if not line:
                break
            output += line
        await self.wait()
        return output, b""

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9


class _FakeReader:
    """Line reader over a fixed buffer; returns b'' at EOF (never blocks)."""

    def __init__(self, lines):
        import collections

        self._lines = collections.deque(line.encode() for line in lines)

    def at_eof(self):
        return False

    async def readline(self):
        if not self._lines:
            return b""
        return self._lines.popleft()


class _FakeWriter:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, data):
        self.data += data

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class LoginFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reset module-level single-flight state between tests.
        login._session = None

    async def asyncTearDown(self):
        login._session = None

    async def test_begin_login_parses_banner_url(self):
        proc = _FakeProc([BANNER])
        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), \
                mock.patch.object(login.asyncio, "create_subprocess_exec", return_value=proc):
            result = await login.begin_login()
        self.assertTrue(result["started"])
        self.assertTrue(result["login_url"].startswith("https://www.qobuz.com/signin/oauth"))

    async def test_begin_login_single_flight(self):
        proc = _FakeProc([BANNER])
        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), \
                mock.patch.object(login.asyncio, "create_subprocess_exec", return_value=proc):
            first = await login.begin_login()
            second = await login.begin_login()
        self.assertTrue(first["started"])
        self.assertFalse(second["started"])
        self.assertEqual(second["reason"], "already-in-progress")
        self.assertEqual(second["login_url"], first["login_url"])

    async def test_begin_login_without_url_raises(self):
        proc = _FakeProc(["some unrelated output\n"])
        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), \
                mock.patch.object(login.asyncio, "create_subprocess_exec", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "authorization URL"):
                await login.begin_login()
        self.assertIsNone(login._session)

    async def test_finish_login_pipes_code_and_reports_ok(self):
        proc = _FakeProc([BANNER, "Login successful.\n"])
        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), \
                mock.patch.object(login.asyncio, "create_subprocess_exec", return_value=proc):
            await login.begin_login()
            result = await login.finish_login(
                "http://127.0.0.1:43717/x?code=tok123"
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["returncode"], 0)
        self.assertIn("tok123", proc.stdin.data.decode())
        self.assertIsNone(login._session)

    async def test_finish_login_without_session_raises(self):
        with self.assertRaisesRegex(RuntimeError, "No qbzd login"):
            await login.finish_login("http://x?code=1")

    async def test_finish_login_requires_payload(self):
        with self.assertRaises(ValueError):
            await login.finish_login("")

    async def test_logout_runs_binary(self):
        proc = _FakeProc(["Logged out.\n"])
        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), \
                mock.patch.object(login.asyncio, "create_subprocess_exec", return_value=proc):
            result = await login.logout()
        self.assertTrue(result["ok"])
        self.assertEqual(result["returncode"], 0)

    async def test_cancel_terminates_inflight_login(self):
        proc = _FakeProc([BANNER])
        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), \
                mock.patch.object(login.asyncio, "create_subprocess_exec", return_value=proc):
            await login.begin_login()
            result = await login.cancel()
        self.assertTrue(result["cancelled"])
        self.assertIsNone(login._session)
        self.assertTrue(proc.terminated)

    async def test_cancel_without_session_is_noop(self):
        result = await login.cancel()
        self.assertFalse(result["cancelled"])

    async def test_finish_after_cancel_reports_no_flow(self):
        proc = _FakeProc([BANNER])
        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), \
                mock.patch.object(login.asyncio, "create_subprocess_exec", return_value=proc):
            await login.begin_login()
            await login.cancel()
        with self.assertRaisesRegex(RuntimeError, "No qbzd login"):
            await login.finish_login("http://x?code=1")


class ProviderDelegationTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_delegates_login_logout(self):
        from streaming.qobuz.provider import QobuzProvider

        provider = QobuzProvider()
        with mock.patch.object(login, "begin_login", return_value={"started": True, "login_url": "u"}), \
                mock.patch.object(login, "finish_login", return_value={"ok": True}), \
                mock.patch.object(login, "logout", return_value={"ok": True}), \
                mock.patch.object(provider, "is_authenticated", return_value=True):
            begun = await provider.begin_login()
            finished = await provider.finish_login("x?code=1")
            out = await provider.logout()
        self.assertTrue(begun["started"])
        self.assertTrue(finished["ok"])
        self.assertTrue(finished["authenticated"])
        self.assertTrue(out["ok"])
        self.assertTrue(out["authenticated"])


class EndpointTests(unittest.TestCase):
    def setUp(self):
        import main as main_module
        from fastapi.testclient import TestClient

        self.main = main_module
        self.client = TestClient(main_module.app)

    def test_auth_state_shape(self):
        data = self.client.get("/api/streaming/qobuz/auth/state").json()
        self.assertIn("installed", data)
        self.assertIn("authenticated", data)
        self.assertIn("login", data)
        self.assertIn("in_progress", data["login"])

    def test_login_finish_without_flow_conflicts(self):
        resp = self.client.post(
            "/api/streaming/qobuz/auth/login/finish", json={"redirect_url": "x"}
        )
        self.assertEqual(resp.status_code, 409)

    def test_login_finish_requires_payload(self):
        with mock.patch.object(login, "_session", object()):
            resp = self.client.post(
                "/api/streaming/qobuz/auth/login/finish", json={"redirect_url": ""}
            )
            self.assertEqual(resp.status_code, 400)

    def test_login_cancel_without_flow_is_noop(self):
        resp = self.client.post("/api/streaming/qobuz/auth/login/cancel")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["cancelled"])


if __name__ == "__main__":
    unittest.main()
