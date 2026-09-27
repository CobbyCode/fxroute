# SPDX-License-Identifier: AGPL-3.0-only

"""Regression tests for the qbzd login session handling (daemon HTTP flow).

Two confirmed defects (2026-09-03, subprocess era) and their HTTP
equivalents:

* ``begin_login`` must return the sign-in URL as soon as the daemon answers
  ``POST /api/auth/oauth/start`` — there is no banner to wait for.
* ``begin_login`` must not answer ``already-in-progress`` forever when the
  previous daemon-side flow already ended (expired session): a non-pending
  daemon status clears the stale session so a fresh login can start, while
  a genuinely pending flow keeps answering in progress without starting a
  second OAuth flow.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.qobuz import login  # noqa: E402

OAUTH_URL = "https://www.qobuz.com/signin/oauth?ext_app_id=798273057"


class BeginLoginSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        login._session = None

    async def asyncTearDown(self):
        login._session = None

    async def test_begin_login_reaps_expired_session_and_starts_fresh(self):
        # A previous login whose daemon-side flow already ended (idle) is
        # still registered; begin_login must clear it and start a new flow
        # instead of answering already-in-progress with the stale URL.
        login._session = login._LoginSession("https://stale.example/old", None)
        posts = []

        def fake_post(path, body=None, timeout=0):
            posts.append(path)
            return {"oauth_url": OAUTH_URL + "?fresh=1"}

        def fake_get(path, timeout=0):
            return {"status": "idle"}

        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), \
             mock.patch.object(login, "_http_post", side_effect=fake_post), \
             mock.patch.object(login, "_http_get", side_effect=fake_get):
            result = await login.begin_login()
        self.assertTrue(result["started"])
        self.assertNotIn("reason", result)
        self.assertIn("fresh=1", result["login_url"])
        self.assertIsNotNone(login._session)
        self.assertIn("fresh=1", login._session.url)

    async def test_begin_login_reports_in_progress_for_pending_flow(self):
        # A genuinely pending daemon flow keeps answering already-in-progress
        # and never starts a second OAuth flow.
        login._session = login._LoginSession("https://play.qobuz.com/login?code=in-flight", None)
        posts = []

        def fake_post(path, body=None, timeout=0):
            posts.append(path)
            raise AssertionError("begin_login must not restart a pending flow")

        def fake_get(path, timeout=0):
            return {"status": "pending"}

        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), \
             mock.patch.object(login, "_http_post", side_effect=fake_post), \
             mock.patch.object(login, "_http_get", side_effect=fake_get):
            result = await login.begin_login()
        self.assertFalse(result["started"])
        self.assertEqual(result["reason"], "already-in-progress")
        self.assertEqual(result["login_url"], "https://play.qobuz.com/login?code=in-flight")
        self.assertEqual(posts, [])

    async def test_begin_login_fails_fast_when_daemon_reports_no_url(self):
        # No oauth_url in the daemon answer: begin_login raises instead of
        # leaving a half-started session behind.
        with mock.patch.object(login, "qbzd_binary", return_value="/usr/bin/qbzd"), \
             mock.patch.object(login, "_http_post", return_value={}), \
             mock.patch.object(login, "_http_get", return_value={"status": "idle"}):
            with self.assertRaisesRegex(RuntimeError, "did not report an authorization URL"):
                await login.begin_login()
        self.assertIsNone(login._session)


if __name__ == "__main__":
    unittest.main()
