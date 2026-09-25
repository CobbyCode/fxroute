# SPDX-License-Identifier: AGPL-3.0-only
"""TIDAL logout invalidates the per-account library cache.

Regression test for the logout leak: ``TidalSession.clear()`` removed the
token file but left ``tidal-cache.sqlite`` rows behind, so
``GET /api/streaming/tidal/library/snapshot?user=<old>`` kept serving the
previous account after logout. Logout must clear the previous account's rows
so a logged-out or new state never reuses them.
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from streaming.tidal import auth  # noqa: E402
from streaming.tidal.cache import TidalLibraryCache  # noqa: E402


class TidalLogoutCacheInvalidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_logout_clears_previous_account_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tidal-cache.sqlite"
            session_file = Path(tmp) / "tidal-session.json"
            store = TidalLibraryCache(db_path=db)
            store.put("42", "ids", {"tracks": ["t1"], "albums": [], "artists": [], "playlists": []})
            store.put("42", "tracks", [{"id": "t1", "title": "Song"}])
            self.assertIsNotNone(store.snapshot("42"))
            manager = auth.TidalSession()
            manager._last_user = {"id": "42", "email": "u@example.com", "country_code": "US"}
            with mock.patch.object(auth, "SESSION_FILE", session_file), mock.patch(
                "streaming.tidal.cache.library_cache", store
            ):
                session_file.write_text(json.dumps({"token_type": "Bearer", "access_token": "x"}))
                await manager.clear()
            self.assertIsNone(store.snapshot("42"))

    async def test_logout_falls_back_to_session_file_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tidal-cache.sqlite"
            session_file = Path(tmp) / "tidal-session.json"
            store = TidalLibraryCache(db_path=db)
            store.put("99", "ids", {"tracks": ["x"], "albums": [], "artists": [], "playlists": []})
            manager = auth.TidalSession()
            manager._last_user = None
            manager._session = None
            with mock.patch.object(auth, "SESSION_FILE", session_file), mock.patch(
                "streaming.tidal.cache.library_cache", store
            ):
                session_file.write_text(json.dumps({"user": {"id": "99"}}))
                await manager.clear()
            self.assertIsNone(store.snapshot("99"))

    async def test_logout_keeps_other_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tidal-cache.sqlite"
            session_file = Path(tmp) / "tidal-session.json"
            store = TidalLibraryCache(db_path=db)
            store.put("42", "ids", {"tracks": ["t1"], "albums": [], "artists": [], "playlists": []})
            store.put("77", "ids", {"tracks": ["t7"], "albums": [], "artists": [], "playlists": []})
            manager = auth.TidalSession()
            manager._last_user = {"id": "42", "email": "u@example.com", "country_code": "US"}
            with mock.patch.object(auth, "SESSION_FILE", session_file), mock.patch(
                "streaming.tidal.cache.library_cache", store
            ):
                await manager.clear()
            self.assertIsNone(store.snapshot("42"))
            self.assertIsNotNone(store.snapshot("77"))

    def test_frontend_resets_in_memory_cache_on_logout(self):
        text = (Path(__file__).resolve().parents[1] / "static" / "streaming.js").read_text()
        self.assertIn("function ensureTidalAccountState()", text)
        self.assertIn("if (state.tidal.cacheUser === userId) return;", text)
        self.assertNotIn("if (!userId || state.tidal.cacheUser === userId) return;", text)


if __name__ == "__main__":
    unittest.main()
