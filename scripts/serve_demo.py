#!/usr/bin/env python3
"""Serve the FXRoute web demo on port 8765.

The demo reuses the product frontend live — no manual copying:

- ``/`` and ``/index.html``  -> the frontend checkout's static/index.html
  with the demo transport layer injected on the fly (see build_demo.transform_index_html)
- ``/static/*``              -> the frontend checkout's static/ (always current)
- ``/static/demo/*``         -> demo-only artwork pool from this worktree
- ``/demo/*``                -> demo transport layer (state/routes/streaming/...)

The frontend is served from FRONTEND_ROOT (see build_demo.py): default is
this checkout itself, FXROUTE_FRONTEND_ROOT overrides it.
"""
from __future__ import annotations

import functools
import http.server
import mimetypes
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

SCRIPTS = Path(__file__).resolve().parent
DEMO_ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from build_demo import FRONTEND_ROOT, transform_index_html  # noqa: E402

FRONTEND_STATIC = FRONTEND_ROOT / "static"
DEMO_STATIC_ART = DEMO_ROOT / "static" / "demo"
DEMO_LAYER = DEMO_ROOT / "demo"


class DemoMuxHandler(http.server.SimpleHTTPRequestHandler):
    """Route requests to either the canonical frontend or the demo layer."""

    def do_GET(self):
        # Browsers send file names with spaces percent-encoded
        # ("USER%20GUIDE.jpg"); decode before mapping to the pool so the
        # artwork URLs the demo layer emits resolve.
        path = unquote(urlsplit(self.path).path)
        if path in ("", "/"):
            self._serve_index()
            return
        try:
            if path.startswith("/demo/"):
                self._serve_file(DEMO_LAYER / path[len("/demo/"):])
            elif path.startswith("/static/demo/"):
                self._serve_file(DEMO_STATIC_ART / path[len("/static/demo/"):])
            elif path.startswith("/static/"):
                self._serve_file(FRONTEND_STATIC / path[len("/static/"):])
            elif path == "/api/certificate/local-root":
                # The Settings certificate link is a plain anchor navigation
                # (no fetch), so the demo server must serve the placeholder
                # PEM itself — same body the in-page routes.js returns.
                self._serve_demo_certificate()
            else:
                self.send_error(404, "Not Found")
        except IsADirectoryError:
            self.send_error(404, "Not Found")

    def _serve_index(self):
        try:
            html = transform_index_html(
                (FRONTEND_STATIC / "index.html").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            self.send_error(500, f"frontend index unavailable: {exc}")
            return
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._no_cache()
        self.end_headers()
        self.wfile.write(payload)

    def _serve_file(self, file_path: Path):
        resolved = file_path.resolve()
        roots = (FRONTEND_STATIC.resolve(), DEMO_ROOT.resolve())
        if not any(str(resolved).startswith(str(root) + "/") for root in roots):
            self.send_error(403, "Forbidden")
            return
        if not resolved.is_file():
            self.send_error(404, "Not Found")
            return
        ctype = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        payload = resolved.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self._no_cache()
        self.end_headers()
        self.wfile.write(payload)

    def _serve_demo_certificate(self):
        pem = (
            "-----BEGIN CERTIFICATE-----\n"
            "FXRoute web-demo placeholder certificate. This is not a real TLS\n"
            "certificate; the simulated box serves no HTTPS in the demo.\n"
            "-----END CERTIFICATE-----\n"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-pem-file")
        self.send_header("Content-Disposition", 'attachment; filename="fxroute-demo-certificate.crt"')
        self.send_header("Content-Length", str(len(pem)))
        self._no_cache()
        self.end_headers()
        self.wfile.write(pem)

    def _no_cache(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Expires", "0")

    def log_message(self, fmt, *args):
        pass


def main() -> None:
    if not FRONTEND_STATIC.is_dir():
        print(f"frontend static dir not found at {FRONTEND_STATIC}", file=sys.stderr)
        raise SystemExit(1)
    handler = functools.partial(DemoMuxHandler, directory=str(DEMO_ROOT))
    server = http.server.HTTPServer(("0.0.0.0", 8765), handler)
    print(f"serving demo on http://0.0.0.0:8765 (frontend: {FRONTEND_ROOT})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
