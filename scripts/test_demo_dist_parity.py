#!/usr/bin/env python3
"""Regression test: the demo snapshot (demo/dist) must not drift from static/.

scripts/build_demo.py mirrors the canonical static/ tree into
demo/dist/static and rebuilds demo/dist/index.html from static/index.html,
carrying the source asset version strings. A frontend change without
re-running the build leaves the public demo serving stale assets, so this
test compares both halves against the canonical tree:

* demo/dist/static is byte-identical with static/ (minus build_demo.STATIC_EXCLUDE)
* the page shell references exactly the same asset versions as static/index.html
"""

import importlib.util
import os
import re
import shutil
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "demo" / "dist"

# Absolute /static/ asset references in either page shell (the demo build may
# prefix the public base path, but the "/static/..." tail still matches).
ASSET_RE = re.compile(r"/static/([A-Za-z0-9_./-]+\.(?:js|css))\?v=([^\"'\s<>]+)")


def load_build_demo():
    spec = importlib.util.spec_from_file_location(
        "fxroute_build_demo", ROOT / "scripts" / "build_demo.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DemoDistParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DIST.is_dir():
            raise unittest.SkipTest("demo/dist does not exist; run scripts/build_demo.py")
        cls.build_demo = load_build_demo()

    def mirrored_files(self):
        """Paths copytree(static/, dist/static) must produce.

        Uses the build's own exclusion set with the same per-directory ignore
        semantics as shutil.copytree, so the expectation follows build_demo.py
        instead of hardcoding a file list here.
        """
        src = ROOT / "static"
        ignore = shutil.ignore_patterns(*self.build_demo.STATIC_EXCLUDE)
        expected = {}
        for dirpath, dirnames, filenames in os.walk(src):
            ignored = ignore(dirpath, dirnames + filenames)
            keep_dirs = [name for name in dirnames if name not in ignored]
            dirnames[:] = keep_dirs
            for name in filenames:
                if name in ignored:
                    continue
                path = Path(dirpath) / name
                expected[path.relative_to(ROOT).as_posix()] = path
        return expected

    def test_dist_static_is_byte_identical_with_the_canonical_tree(self):
        expected = self.mirrored_files()
        actual = {
            path.relative_to(DIST).as_posix(): path
            for dirpath, _dirnames, filenames in os.walk(DIST / "static")
            for path in (Path(dirpath) / name for name in filenames)
        }

        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        self.assertFalse(
            missing,
            f"demo snapshot is stale, files missing from demo/dist/static: "
            f"{missing}; run scripts/build_demo.py",
        )
        self.assertFalse(
            extra,
            f"unexpected files in demo/dist/static: {extra}; run scripts/build_demo.py",
        )

        differing = sorted(
            relative
            for relative, path in expected.items()
            if path.read_bytes() != actual[relative].read_bytes()
        )
        self.assertFalse(
            differing,
            f"demo/dist/static differs from static/ for: {differing}; "
            f"run scripts/build_demo.py",
        )

    def test_page_shell_carries_the_canonical_asset_versions(self):
        def asset_versions(path):
            return sorted(ASSET_RE.findall(path.read_text(encoding="utf-8")))

        canonical = asset_versions(ROOT / "static" / "index.html")
        demo_shell = asset_versions(DIST / "index.html")
        self.assertEqual(
            canonical,
            demo_shell,
            "demo/dist/index.html references different asset versions than "
            "static/index.html; run scripts/build_demo.py",
        )


if __name__ == "__main__":
    unittest.main()
