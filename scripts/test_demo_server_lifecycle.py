#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""UI checks own a ready demo listener and always close it."""

import socket
import unittest
from urllib.parse import urlsplit
from urllib.request import urlopen

from demo_server_test_support import demo_server


class DemoServerLifecycleTests(unittest.TestCase):
    def assert_closed(self, url):
        address = urlsplit(url)
        with self.assertRaises(OSError):
            socket.create_connection((address.hostname, address.port), timeout=1)

    def test_ready_demo_serves_live_shell_and_assets_then_closes(self):
        with demo_server() as url:
            with urlopen(url, timeout=2) as response:
                self.assertEqual(response.status, 200)
                html = response.read().decode()
            self.assertIn('src="./demo/routes.js?', html)
            self.assertIn('src="/static/app.js?', html)
            for asset in ("/demo/routes.js", "/static/app.js"):
                with urlopen(url + asset, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertGreater(len(response.read()), 100)
        self.assert_closed(url)

    def test_independent_servers_do_not_collide_or_close_each_other(self):
        with demo_server() as first:
            with demo_server() as second:
                self.assertNotEqual(first, second)
            self.assert_closed(second)
            with urlopen(first, timeout=2) as response:
                self.assertEqual(response.status, 200)
        self.assert_closed(first)

    def test_exception_still_closes_the_listener(self):
        with self.assertRaisesRegex(RuntimeError, "browser check failed"):
            with demo_server() as url:
                raise RuntimeError("browser check failed")
        self.assert_closed(url)


if __name__ == "__main__":
    unittest.main()
