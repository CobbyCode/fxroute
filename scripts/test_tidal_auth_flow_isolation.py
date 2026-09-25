# SPDX-License-Identifier: AGPL-3.0-only
"""TIDAL Device/PKCE pending-state isolation.

Regression tests for the shared-slot bug: a started device login and a
started PKCE login overwrote the same ``_pending_session`` without
invalidating each other, so a superseded attempt could commit late and
overwrite the newer session. Only the currently valid attempt may commit.
"""

import asyncio
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
        is_pkce=("pkce" in name),
        country_code="US",
        user=SimpleNamespace(id=user_id, email=f"{name}@example.com"),
        check_login=lambda: True,
    )
    sess.get_link_login = lambda: SimpleNamespace(
        verification_uri="https://link.tidal.com",
        verification_uri_complete="https://link.tidal.com/complete",
        user_code=f"code-{name}",
        expires_in=600,
    )
    sess.pkce_login_url = lambda: f"https://login.tidal.com/pkce-{name}"
    sess.pkce_get_auth_token = lambda url: f"token-{name}"
    sess.process_auth_token = lambda token, is_pkce_token=True: None
    sess.process_link_login = lambda link, until_expiry=False: None
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

    async def test_device_then_pkce_invalidates_device(self):
        manager = auth.TidalSession()
        self._patch_new_session([_make_session("device", 1), _make_session("pkce", 2)])
        await manager.start_device_login_async()
        self.assertEqual(manager._pending_flow, "device")
        await manager.pkce_login_url_async()
        self.assertEqual(manager._pending_flow, "pkce")
        self.assertIsNone(manager._pending)
        with self.assertRaises(auth.TidalAuthError):
            await manager.finish_device_login_async()
        payload = await manager.finish_pkce_login_async("https://oops?code=1")
        self.assertEqual(payload["user_id"], 2)
        self.assertEqual(manager._session.access_token, "tok-pkce")

    async def test_pkce_then_device_invalidates_pkce(self):
        manager = auth.TidalSession()
        self._patch_new_session([_make_session("pkce", 10), _make_session("device", 11)])
        await manager.pkce_login_url_async()
        self.assertEqual(manager._pending_flow, "pkce")
        await manager.start_device_login_async()
        self.assertEqual(manager._pending_flow, "device")
        with self.assertRaises(auth.TidalAuthError):
            await manager.finish_pkce_login_async("https://oops?code=1")
        self.assertIsNone(manager._session)

    async def test_stale_device_cannot_overwrite_newer_pkce(self):
        import threading

        manager = auth.TidalSession()
        old = _make_session("device-old", 20)
        new = _make_session("pkce-new", 21)
        entered = threading.Event()
        release = threading.Event()

        def blocking_approve(link, until_expiry=False):
            entered.set()
            release.wait(5)

        old.process_link_login = blocking_approve
        self._patch_new_session([old, new])
        await manager.start_device_login_async()
        task = asyncio.create_task(manager.finish_device_login_async())
        await asyncio.to_thread(entered.wait, 3)
        await manager.pkce_login_url_async()
        pkce_payload = await manager.finish_pkce_login_async("https://oops?code=1")
        self.assertEqual(pkce_payload["user_id"], 21)
        release.set()
        with self.assertRaises(auth.TidalAuthError):
            await asyncio.wait_for(task, 3)
        self.assertIs(manager._session, new)
        self.assertEqual(manager._session.access_token, "tok-pkce-new")

    async def test_stale_pkce_cannot_overwrite_newer_device(self):
        manager = auth.TidalSession()
        old = _make_session("pkce-old", 30)
        new = _make_session("device-new", 31)
        self._patch_new_session([old, new])
        await manager.pkce_login_url_async()
        await manager.start_device_login_async()
        with self.assertRaises(auth.TidalAuthError):
            await manager.finish_pkce_login_async("https://oops?code=1")
        self.assertIsNone(manager._session)
        self.assertTrue(auth.SESSION_FILE is not None and not auth.SESSION_FILE.exists())

    async def test_stale_pkce_exchange_cannot_commit_after_device_start(self):
        import threading

        manager = auth.TidalSession()
        old = _make_session("pkce-old", 40)
        new = _make_session("device-new", 41)
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
            await manager.start_device_login_async()
            release.set()
            with self.assertRaises(auth.TidalAuthError):
                await asyncio.wait_for(task, 3)
        self.assertIsNone(manager._session)

    def test_sync_finish_rejects_wrong_flow(self):
        manager = auth.TidalSession()
        pkce_sess = _make_session("pkce", 50)
        manager._pending_session = pkce_sess
        manager._pending = None
        manager._pending_flow = "pkce"
        with self.assertRaises(auth.TidalAuthError):
            manager.finish_device_login()
        manager._pending_flow = "device"
        manager._pending = SimpleNamespace()
        # Device wait would block; only the guard is asserted here via flow mismatch above.
        # Reset to a clean PKCE state and verify PKCE finish works synchronously.
        manager._pending = None
        manager._pending_session = pkce_sess
        manager._pending_flow = "pkce"
        with mock.patch.object(auth, "SESSION_FILE", Path(self.tmp.name) / "sync.json"):
            payload = manager.finish_pkce_login("https://oops?code=1")
        self.assertEqual(payload["user_id"], 50)


if __name__ == "__main__":
    unittest.main()
