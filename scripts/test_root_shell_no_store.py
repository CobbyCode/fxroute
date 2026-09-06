#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""The app shell (GET /) must never be browser-cached.

The shell carries the versioned asset URLs (app.js?v=...). A cached stale
shell keeps booting stale JS/CSS after a deploy even though the assets
themselves are cache-busted — e.g. a Music Library selector without the
discovery loading state while the server already ships it.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class FakeURL:
    scheme = "http"


class FakeRequest:
    headers = {}
    url = FakeURL()


class RootShellCacheHeadersTests(unittest.TestCase):
    def test_root_shell_is_not_stored(self):
        response = asyncio.run(main.read_root(FakeRequest()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("cache-control"),
            "no-store, must-revalidate",
        )

    def test_root_shell_references_versioned_app(self):
        response = asyncio.run(main.read_root(FakeRequest()))
        body = response.body.decode("utf-8")
        self.assertRegex(body, r"/static/app\.js\?v=\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
