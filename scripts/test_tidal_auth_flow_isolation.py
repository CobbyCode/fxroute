# SPDX-License-Identifier: AGPL-3.0-only
"""TIDAL PKCE pending-state isolation and session loading.

Only the currently valid PKCE attempt may commit: a login started later, or a
logout, supersedes an earlier attempt, and a late answer for the earlier one
never overwrites the newer session.  Sessions written by the removed device
login keep loading.
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from streaming.tidal import auth  # noqa: E402


def _make_session(name, user_id):
    sess = SimpleNamespace(
        config=SimpleNamespace(quality=None),
        token_type="Bearer",
        access_token=f"tok-{name}",
        refresh_token="refresh",
        expiry_time=None,
        is_pkce=True,
        country_code="US",
        user=SimpleNamespace(id=user_id, email=f"{name}@example.com"),
        check_login=lambda: True,
    )
    sess.pkce_login_url = lambda: f"https://login.tidal.com/pkce-{name}"
    sess.pkce_get_auth_token = lambda url: f"token-{name}"
    sess.process_auth_token = lambda token, is_pkce_token=True: None
    return sess


class TidalAuthFlowIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file_patch = mock.patch.object(
            auth, "SESSION_FILE", Path(self.tmp.name) / "session.json"
        )
        self.file_patch.start()
        self.addCleanup(self.file_patch.stop)
        self.tidal_patch = mock.patch.object(auth, "tidalapi", object())
        self.tidal_patch.start()
        self.addCleanup(self.tidal_patch.stop)

    def _patch_new_session(self, sessions):
        patcher = mock.patch.object(auth, "_new_session", side_effect=sessions)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_pkce_login_commits_and_persists_the_session(self):
        manager = auth.TidalSession()
        self._patch_new_session([_make_session("pkce", 2)])
        url = await manager.pkce_login_url_async()
        self.assertEqual(url, "https://login.tidal.com/pkce-pkce")
        payload = await manager.finish_pkce_login_async("https://oops?code=1")
        self.assertEqual(payload["user_id"], 2)
        self.assertEqual(manager._session.access_token, "tok-pkce")
        saved = json.loads(auth.SESSION_FILE.read_text())
        self.assertTrue(saved["is_pkce"])

    async def test_finish_without_a_started_login_is_rejected(self):
        manager = auth.TidalSession()
        with self.assertRaises(auth.TidalAuthError):
            await manager.finish_pkce_login_async("https://oops?code=1")
        with self.assertRaises(auth.TidalAuthError):
            manager.finish_pkce_login("https://oops?code=1")

    async def test_a_second_login_supersedes_the_first(self):
        manager = auth.TidalSession()
        self._patch_new_session([_make_session("first", 10), _make_session("second", 11)])
        await manager.pkce_login_url_async()
        await manager.pkce_login_url_async()
        payload = await manager.finish_pkce_login_async("https://oops?code=1")
        self.assertEqual(payload["user_id"], 11)
        self.assertEqual(manager._session.access_token, "tok-second")
        # The first attempt is gone, finishing again has nothing to finish.
        with self.assertRaises(auth.TidalAuthError):
            await manager.finish_pkce_login_async("https://oops?code=1")

    async def test_stale_exchange_cannot_commit_after_a_newer_login_started(self):
        import threading

        manager = auth.TidalSession()
        old = _make_session("old", 40)
        new = _make_session("new", 41)
        entered = threading.Event()
        release = threading.Event()
        real_exchange = auth.TidalSession._exchange_pkce_login

        @staticmethod
        def blocking_exchange(session, redirect_url):
            entered.set()
            release.wait(5)
            return real_exchange(session, redirect_url)

        self._patch_new_session([old, new])
        await manager.pkce_login_url_async()
        with mock.patch.object(auth.TidalSession, "_exchange_pkce_login", blocking_exchange):
            task = asyncio.create_task(manager.finish_pkce_login_async("https://oops?code=1"))
            await asyncio.to_thread(entered.wait, 3)
            await manager.pkce_login_url_async()
            release.set()
            with self.assertRaises(auth.TidalAuthError):
                await asyncio.wait_for(task, 3)
        self.assertIsNone(manager._session)
        self.assertFalse(auth.SESSION_FILE.exists())

    def test_sync_finish_commits_the_pending_login(self):
        manager = auth.TidalSession()
        manager._pending_session = _make_session("pkce", 50)
        with mock.patch.object(auth, "SESSION_FILE", Path(self.tmp.name) / "sync.json"):
            payload = manager.finish_pkce_login("https://oops?code=1")
        self.assertEqual(payload["user_id"], 50)

    def test_session_file_of_the_removed_device_login_still_loads(self):
        auth.SESSION_FILE.write_text(json.dumps({
            "token_type": "Bearer",
            "access_token": "legacy",
            "refresh_token": "legacy-refresh",
            "expiry_time": None,
            "is_pkce": False,
            "user": {"id": 7, "email": "legacy@example.com"},
        }))
        loaded = {}
        legacy = SimpleNamespace(config=SimpleNamespace(quality=None))

        def load_oauth_session(token_type, access_token, **kwargs):
            loaded.update(token_type=token_type, access_token=access_token, **kwargs)
            return True

        legacy.load_oauth_session = load_oauth_session
        with mock.patch.object(auth, "_new_session", return_value=legacy):
            session = auth.TidalSession().session()
        self.assertIs(session, legacy)
        self.assertEqual(loaded["access_token"], "legacy")
        self.assertIs(loaded["is_pkce"], False)


if __name__ == "__main__":
    unittest.main()
