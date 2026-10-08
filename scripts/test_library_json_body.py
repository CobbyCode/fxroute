#!/usr/bin/env python3
"""Favorite routes reject malformed JSON without changing library state."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from starlette.requests import Request
from library import api


def request_body(body):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return Request({"type": "http", "method": "POST", "headers": []}, receive)


class FavoriteJsonTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_favorite_routes_reject_malformed_or_nonobject_bodies(self):
        scanner = SimpleNamespace(get_album_tracks=lambda *a, **k: [SimpleNamespace(id="track")],
                                  get_tracks=lambda **k: [SimpleNamespace(id="track")],
                                  set_album_favorite=Mock(), set_track_favorite=Mock())
        with patch.object(api, "_library_runtime", return_value=(scanner, None)):
            for route, identifier in ((api.set_album_favorite, "album"), (api.set_track_favorite, "track")):
                for body in (b"{", b"null", b"[]", b'"text"'):
                    with self.subTest(route=route.__name__, body=body):
                        with self.assertRaises(HTTPException) as caught:
                            await route(identifier, request_body(body))
                        self.assertEqual(caught.exception.status_code, 400)
        scanner.set_album_favorite.assert_not_called()
        scanner.set_track_favorite.assert_not_called()


if __name__ == "__main__":
    unittest.main()
