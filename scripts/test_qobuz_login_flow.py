# SPDX-License-Identifier: AGPL-3.0-only

"""Qobuz/qbzd login orchestration tests (daemon HTTP OAuth flow).

The fork daemon owns the OAuth flow over HTTP (``POST
/api/auth/oauth/start`` returns the sign-in URL, ``GET
/api/auth/oauth/callback`` consumes the pasted code, ``GET
/api/auth/oauth/status`` reports the verdict). These tests verify that
FXRoute only orchestrates that flow correctly:

* code extraction from pasted redirect URLs
* begin/finish happy path against a fake daemon
* single-flight guard (second begin returns already-in-progress)
* logout credential cleanup plus daemon restart
* provider delegation and the HTTP endpoint contract
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import streaming.qobuz.login as login  # noqa: E402

OAUTH_URL = (
    "https://www.qobuz.com/signin/oauth?ext_app_id=798273057"
    "&redirect_url=http%3A%2F%2F192.168.178.130%3A8182%2Fapi%2Fauth%2Foauth%2Fcallback"
)


class ParseTests(unittest.TestCase):
    def test_code_extraction_from_pasted_redirect(self):
        pasted = (
            "http://192.168.178.130:8182/api/auth/oauth/callback"
            "?code=abc-DEF_123~x"
        )
        self.assertEqual(login._extract_code(pasted), "abc-DEF_123~x")

    def test_code_extraction_from_raw_code(self):
        self.assertEqual(login._extract_code("tok123"), "tok123")

    def test_code_extraction_without_code_returns_input(self):
        self.assertEqual(login._extract_code("https://example.com/nothing"),
                         "https://example.com/nothing")

    def test_code_pattern_still_matches(self):
        match = login._CODE_PATTERN.search("http://x/cb?code=abc-DEF_123~x")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "abc-DEF_123~x")


def _daemon(post=None, get=None):
    """Patch the daemon HTTP helpers with canned responses."""
    post = post if post is not None else {}
    get = get if get is not None else {}

    def fake_post(path, body=None, timeout=0):
        value = post.get(path) if isinstance(post, dict) else post
        return value() if callable(value) else value

    def fake_get(path, timeout=0):
        value = get.get(path) if isinstance(get, dict) else get
        return value() if callable(value) else value

    return (mock.patch.object(login, "_http_post", side_effect=fake_post),
            mock.patch.object(login, "_http_get", side_effect=fake_get))


class LoginFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        login._session = None

    async def asyncTearDown(self):
        login._session = None

    async def test_begin_login_returns_sign_in_url(self):
        post, get = _daemon(post={"/api/auth/oauth/start": {"oauth_url": OAUTH_URL,
                                                             "callback_url": "http://lan:8182/cb"}})
        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), post, get:
            result = await login.begin_login()
        self.assertTrue(result["started"])
        self.assertTrue(result["login_url"].startswith("https://www.qobuz.com/signin/oauth"))

    async def test_begin_login_without_url_raises(self):
        post, get = _daemon(post={"/api/auth/oauth/start": {}},
                            get={"/api/status": None})
        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), post, get:
            with self.assertRaisesRegex(RuntimeError, "authorization URL"):
                await login.begin_login()
        self.assertIsNone(login._session)

    async def test_begin_login_without_url_on_legacy_daemon_names_update(self):
        post, get = _daemon(post={"/api/auth/oauth/start": {}},
                            get={"/api/status": {"state": "logged_in"}})
        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), post, get:
            with self.assertRaisesRegex(RuntimeError, "update the Qobuz provider"):
                await login.begin_login()
        self.assertIsNone(login._session)

    async def test_begin_login_without_binary_raises(self):
        with mock.patch.object(login, "qbzd_binary", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "not installed"):
                await login.begin_login()

    async def test_begin_login_single_flight(self):
        post, get = _daemon(
            post={"/api/auth/oauth/start": {"oauth_url": OAUTH_URL}},
            get={"/api/auth/oauth/status": {"status": "pending"}},
        )
        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), post, get:
            first = await login.begin_login()
            second = await login.begin_login()
        self.assertTrue(first["started"])
        self.assertFalse(second["started"])
        self.assertEqual(second["reason"], "already-in-progress")
        self.assertEqual(second["login_url"], first["login_url"])

    async def test_begin_replaces_dead_session(self):
        post, get = _daemon(
            post={"/api/auth/oauth/start": {"oauth_url": OAUTH_URL}},
            get={"/api/auth/oauth/status": {"status": "idle"}},
        )
        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), post, get:
            first = await login.begin_login()
            second = await login.begin_login()
        self.assertTrue(first["started"])
        self.assertTrue(second["started"])

    async def test_finish_login_consumes_code_and_reports_ok(self):
        seen = {}

        def fake_get(path, timeout=0):
            if path.startswith("/api/auth/oauth/callback"):
                seen["callback"] = path
                return "<html>ok</html>"
            return {"status": "success"}

        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), \
             mock.patch.object(login, "_http_post",
                               return_value={"oauth_url": OAUTH_URL}), \
             mock.patch.object(login, "_http_get", side_effect=fake_get):
            await login.begin_login()
            result = await login.finish_login("http://lan:8182/cb?code=tok123")
        self.assertTrue(result["ok"])
        self.assertIn("code=tok123", seen["callback"])
        self.assertIsNone(login._session)

    async def test_finish_login_reports_daemon_error(self):
        def fake_get(path, timeout=0):
            if path.startswith("/api/auth/oauth/callback"):
                return "<html>ok</html>"
            return {"status": "error", "message": "denied"}

        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), \
             mock.patch.object(login, "_http_post",
                               return_value={"oauth_url": OAUTH_URL}), \
             mock.patch.object(login, "_http_get", side_effect=fake_get):
            await login.begin_login()
            result = await login.finish_login("tok123")
        self.assertFalse(result["ok"])
        self.assertIsNone(login._session)

    async def test_finish_login_without_session_raises(self):
        post, get = _daemon(get={"/api/auth/oauth/status": {"status": "idle"}})
        with post, get:
            with self.assertRaisesRegex(RuntimeError, "No qbzd login"):
                await login.finish_login("http://x?code=1")

    async def test_finish_login_without_session_reports_success(self):
        # The browser completed the daemon-side flow directly (callback
        # reached the daemon without a paste): report the daemon verdict.
        post, get = _daemon(get={"/api/auth/oauth/status": {"status": "success"}})
        with post, get:
            result = await login.finish_login("http://x?code=1")
        self.assertTrue(result["ok"])

    async def test_finish_login_requires_payload(self):
        with self.assertRaises(ValueError):
            await login.finish_login("")

    async def test_cancel_drops_inflight_login(self):
        post, get = _daemon(post={"/api/auth/oauth/start": {"oauth_url": OAUTH_URL}})
        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), post, get:
            await login.begin_login()
            result = await login.cancel()
        self.assertTrue(result["cancelled"])
        self.assertIsNone(login._session)

    async def test_cancel_without_session_is_noop(self):
        result = await login.cancel()
        self.assertFalse(result["cancelled"])

    async def test_finish_after_cancel_reports_no_flow(self):
        post, get = _daemon(
            post={"/api/auth/oauth/start": {"oauth_url": OAUTH_URL}},
            get={"/api/auth/oauth/status": {"status": "idle"}},
        )
        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), post, get:
            await login.begin_login()
            await login.cancel()
        with mock.patch.object(login, "_http_post", return_value=None), \
             mock.patch.object(login, "_http_get",
                               return_value={"status": "idle"}):
            with self.assertRaisesRegex(RuntimeError, "No qbzd login"):
                await login.finish_login("http://x?code=1")

    async def test_logout_clears_credentials_and_restarts(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            token = root / ".oauth-token"
            token.write_text("secret")
            users = root / "users" / "123"
            users.mkdir(parents=True)
            marker = root / "last_user_id"
            marker.write_text("123")
            restarts = []

            async def fake_restart():
                restarts.append(True)

            def fake_status(path, timeout=0):
                return {"logged_in": False}

            with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), \
                 mock.patch.object(login, "_credential_paths",
                                   return_value=[token, marker, root / "users"]), \
                 mock.patch.object(login, "_restart_daemon", side_effect=fake_restart), \
                 mock.patch.object(login, "_http_get", side_effect=fake_status):
                result = await login.logout()
            self.assertTrue(result["ok"])
            self.assertFalse(result["logged_in"])
            self.assertTrue(restarts)
            self.assertFalse(token.exists())
            self.assertFalse(marker.exists())
            self.assertFalse((root / "users").exists())

    async def test_logout_without_binary_raises(self):
        with mock.patch.object(login, "qbzd_binary", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "not installed"):
                await login.logout()


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
