# SPDX-License-Identifier: AGPL-3.0-only
"""Isolated live-worktree demo server for browser checks."""

from contextlib import contextmanager
from functools import partial
from http.server import HTTPServer
from threading import Thread
from urllib.request import urlopen

from serve_demo import DEMO_ROOT, DemoMuxHandler


@contextmanager
def demo_server():
    """Yield a ready demo URL; close only this context's listener on exit."""
    handler = partial(DemoMuxHandler, directory=str(DEMO_ROOT))
    server = HTTPServer(("127.0.0.1", 0), handler)
    url = f"http://127.0.0.1:{server.server_port}"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(url, timeout=5) as response:
            if response.status != 200 or b'src="./demo/routes.js?' not in response.read():
                raise RuntimeError("demo server did not serve the demo shell")
        yield url
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
