#!/usr/bin/env python3
"""Build a self-contained demo index.html from the real FXRoute frontend.

Reads the canonical FXRoute checkout's static/index.html (single source of
truth for the frontend), injects the demo transport scripts and writes
demo/dist/index.html plus the referenced assets.

The whole canonical static/ tree is mirrored into demo/dist/static/ (minus
the small deliberate exclusion set in STATIC_EXCLUDE, see below), so new
product JS/CSS/font/image assets are picked up automatically — no per-asset
list.

The public base path is configurable:

- ``--base-path /`` (default): absolute /static/... URLs, for domain-root
  hosting.
- ``--base-path /fxroute/``: every /static/ reference in the built page, the
  copied JS/CSS and the web manifest is prefixed with the base path, for
  subpath hosting (e.g. a GitHub Pages project site). The demo layer keeps
  its relative ./demo/ references, which resolve under the base path.

The canonical checkout is also what scripts/serve_demo.py serves live; this
build step only exists for a self-contained dist/ snapshot.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

DEMO_ROOT = Path(__file__).resolve().parents[1]
# The demo and the frontend live in the same worktree: default to this
# checkout's own static tree. FXROUTE_FRONTEND_ROOT still overrides when a
# different frontend checkout is wanted.
FRONTEND_ROOT = Path(os.environ.get("FXROUTE_FRONTEND_ROOT", str(DEMO_ROOT)))
SRC = FRONTEND_ROOT / "static" / "index.html"
DEFAULT_DIST = DEMO_ROOT / "demo" / "dist"

DEMO_TITLE = "FXRoute Web-Demo (simulated)"

DEMO_STYLES = """    <link rel="stylesheet" href="./demo/demo.css?v=6">
"""

DEMO_PRE_SCRIPTS = """    <!-- Demo transport: must load before the real app scripts -->
    <script src="./demo/data/library.js?v=5"></script>
    <script src="./demo/data/library2.js?v=1"></script>
    <script src="./demo/data/radio.js?v=1"></script>
    <script src="./demo/data/measurements.js?v=1"></script>
    <script src="./demo/state.js?v=15"></script>
    <script src="./demo/routes.js?v=15"></script>
    <script src="./demo/streaming.js?v=1"></script>
    <script src="./demo/ws.js?v=1"></script>
    <script src="./demo/boot.js?v=8"></script>
    <script src="./demo/tour.js?v=12"></script>
"""

# Files in the canonical static/ tree that are deliberately NOT mirrored
# into the public demo snapshot:
# - index.html: the page shell; transformed and written separately.
# - fxroute-logo.jpg/png + branding/: unreferenced design/logo sources (no
#   src/href anywhere in the served frontend); kept out so the snapshot does
#   not ship design files. Everything else is mirrored automatically.
STATIC_EXCLUDE = {
    "index.html",
    "fxroute-logo.jpg",
    "fxroute-logo.png",
    "branding",
}

# Any real product script referenced from the canonical page. Names and
# version strings are read from the source, so the built page stays in sync
# with main without a manual list.
APP_SCRIPT_RE = re.compile(
    r'^\s*<script src="/static/[^"]+\.js\?v=[^"]*"></script>\s*$',
    re.MULTILINE,
)
DEMO_SCRIPT_RE = re.compile(
    r'^\s*<script src="/demo/(?:data/library2?|data/radio|data/measurements|state|routes|streaming|ws|boot|tour)\.js\?v=[^"]*"></script>\s*$',
    re.MULTILINE,
)

# Copied files that may contain /static/ URL references and therefore need
# base-path prefixing for subpath builds. Binary assets (fonts, images) and
# fixture data never reference /static/ URLs.
REWRITE_SUFFIXES = (".js", ".css", ".webmanifest")


def normalize_base_path(value: str) -> str:
    """Normalize --base-path: "/" or "" -> "" (root), "/fxroute" -> "/fxroute/"."""
    value = (value or "/").strip()
    if value == "":
        return ""
    if not value.startswith("/"):
        raise ValueError(f"--base-path must be root-relative, got {value!r}")
    if value != "/" and not value.endswith("/"):
        value += "/"
    return "" if value == "/" else value


def prefix_static_urls(text: str, base: str) -> str:
    """Prefix absolute /static/ references with the public base path."""
    if not base:
        return text
    return text.replace("/static/", f"{base}static/")


def transform_index_html(html: str, base: str = "") -> str:
    """Inject the demo transport layer into the canonical frontend page."""
    head_match = re.search(r"<head>(.*?)</head>", html, re.DOTALL)
    body_match = re.search(r"<body[^>]*>(.*)</body>", html, re.DOTALL)
    if not head_match or not body_match:
        raise ValueError("canonical index.html has no <head>/<body>")

    head = head_match.group(1)
    # Swap the page title for the demo title; keep every other tag
    # (icons, manifest, theme-color, base stylesheet) from the real page.
    head = re.sub(r"<title>.*?</title>", f"<title>{DEMO_TITLE}</title>", head, flags=re.DOTALL)

    # Strip all real app scripts from the body; they are re-added below after
    # the demo transport, in their original order and with their source
    # version strings.
    body = body_match.group(1)
    app_scripts = []
    for m in APP_SCRIPT_RE.finditer(html):
        ver_match = re.search(r'/static/([^" ]+)\.js\?v=([^"]+)', m.group(0))
        if ver_match:
            app_scripts.append((ver_match.group(1), ver_match.group(2)))
    body = APP_SCRIPT_RE.sub("", body)
    body = DEMO_SCRIPT_RE.sub("", body)

    out = ["<!DOCTYPE html>", '<html lang="en">', "<head>"]
    out.append(head.strip())
    out.append(DEMO_STYLES.rstrip())
    out.append("</head>")
    out.append("<body>")
    out.append(DEMO_PRE_SCRIPTS.rstrip())
    out.append(body.rstrip())
    # Re-add all real app scripts with versions from the source.
    for name, version in app_scripts:
        out.append(f'    <script src="/static/{name}.js?v={version}"></script>')
    out.append("</body>")
    out.append("</html>")
    return prefix_static_urls("\n".join(out) + "\n", base)


def build(base: str, dist_dir: Path) -> int:
    html = SRC.read_text(encoding="utf-8")
    try:
        page = transform_index_html(html, base)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    dist_dir.mkdir(parents=True, exist_ok=True)
    index = dist_dir / "index.html"
    index.write_text(page, encoding="utf-8")
    print(f"wrote {index} ({index.stat().st_size} bytes)")
    return 0


def _rewrite_static_urls(dist: Path, base: str) -> None:
    """Prefix /static/ references in copied text files for subpath builds."""
    if not base:
        return
    for root, _dirs, files in os.walk(dist):
        for name in files:
            if name.endswith(REWRITE_SUFFIXES):
                path = Path(root) / name
                path.write_text(
                    prefix_static_urls(path.read_text(encoding="utf-8"), base),
                    encoding="utf-8",
                )


def copy_static_assets(dist: Path, base: str) -> None:
    """Mirror the canonical static tree and the demo layer into dist/."""
    static_src = FRONTEND_ROOT / "static"
    demo_src = DEMO_ROOT / "demo"

    # Demo transport layer lives at ./demo/ next to index.html. Mirrored
    # wholesale so new demo scripts/styles are picked up automatically.
    demo_target = dist / "demo"
    if demo_target.exists():
        shutil.rmtree(demo_target)
    shutil.copytree(
        demo_src,
        demo_target,
        ignore=shutil.ignore_patterns("dist", "README.md", "__pycache__"),
    )

    # Frontend: full mirror of the canonical static tree minus the deliberate
    # exclusions in STATIC_EXCLUDE. The directory is rebuilt from scratch so
    # files removed from main do not linger in the snapshot.
    static_target = dist / "static"
    if static_target.exists():
        shutil.rmtree(static_target)
    shutil.copytree(static_src, static_target, ignore=shutil.ignore_patterns(*STATIC_EXCLUDE))

    # Demo-owned artwork pool lives only in this worktree (the canonical
    # checkout has no static/demo) and is added on top of the mirror.
    demo_art = DEMO_ROOT / "static" / "demo"
    if demo_art.is_dir():
        target = static_target / "demo"
        target.mkdir(exist_ok=True)
        for item in demo_art.iterdir():
            if item.is_file():
                shutil.copy2(item, target / item.name)

    _rewrite_static_urls(dist, base)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the self-contained FXRoute web demo snapshot."
    )
    parser.add_argument(
        "--base-path",
        default="/",
        metavar="PATH",
        help="public base path: '/' (default) for domain-root hosting, or a "
             "root-relative subpath such as '/fxroute/' for a GitHub Pages "
             "project site",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_DIST),
        metavar="DIR",
        help=f"output directory (default: {DEFAULT_DIST})",
    )
    args = parser.parse_args(argv)
    try:
        base = normalize_base_path(args.base_path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    dist_dir = Path(args.out).resolve()
    rc = build(base, dist_dir)
    if rc == 0:
        copy_static_assets(dist_dir, base)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
