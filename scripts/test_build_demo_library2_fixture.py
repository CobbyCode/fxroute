#!/usr/bin/env python3
"""Regression test: the committed Demo Library 2 webdemo fixture is deterministic.

scripts/build_demo_library2.py derives every track, album, playlist and
favorite deterministically from its ALBUMS table. Re-rendering the fixture
from that pure data must reproduce demo/data/library2.js byte-for-byte, so
a change to the builder (album list, track titles, favorites hash, template)
without regenerating the fixture fails here instead of drifting in the demo.

Also pins the fixture's favorites to the builder's hash_code() mirror of the
main catalog's hashCode(): the emitted FAVORITES map must be exactly what
hash_code predicts, and must keep a genuine mix of favored and non-favored
items for the UI contract (favorites exist, but are not all-or-nothing).
"""

import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "demo" / "data" / "library2.js"
FAVORITES_RE = re.compile(r"const FAVORITES = (\{.*\});")

# The builder imports PIL and mutagen at module level; skip instead of
# dying when they are not installed (they are only needed for the file
# outputs, not for the pure fixture render this test exercises).
BUILDER_DEPS = ("PIL", "mutagen")


def load_builder():
    spec = importlib.util.spec_from_file_location(
        "fxroute_build_demo_library2", ROOT / "scripts" / "build_demo_library2.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DemoLibrary2FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        missing = [dep for dep in BUILDER_DEPS if importlib.util.find_spec(dep) is None]
        if missing:
            raise unittest.SkipTest(
                "missing builder dependencies: " + ", ".join(missing))
        if not FIXTURE.is_file():
            raise unittest.SkipTest(
                "demo/data/library2.js missing; run build_demo_library2.py --webdemo-js")
        cls.builder = load_builder()

    def rendered_fixture(self):
        builder = self.builder
        manifest = {"albums": builder.album_seed_entries()}
        manifest["playlists"] = builder.build_playlists(manifest["albums"])
        return builder.render_webdemo_js(manifest)

    def test_fixture_renders_deterministically_and_stays_in_sync(self):
        rendered = self.rendered_fixture()
        self.assertEqual(rendered, self.rendered_fixture(),
                         "re-rendering the fixture must be byte-identical")
        self.assertEqual(
            rendered,
            FIXTURE.read_text(encoding="utf-8"),
            "demo/data/library2.js is out of sync with build_demo_library2.py; "
            "regenerate it (--webdemo-js)",
        )

    def test_favorite_flags_match_hash_code(self):
        builder = self.builder
        manifest = {"albums": builder.album_seed_entries()}
        manifest["playlists"] = builder.build_playlists(manifest["albums"])
        match = FAVORITES_RE.search(builder.render_webdemo_js(manifest))
        self.assertIsNotNone(match, "fixture must embed a FAVORITES map")
        emitted = json.loads(match.group(1))
        self.assertEqual(emitted, builder.favorite_flags(manifest["albums"]),
                         "emitted favorites must be exactly the hash_code output")
        favored = [key for key, flag in emitted.items() if flag]
        self.assertTrue(favored, "fixture must keep deterministic favorites")
        self.assertLess(len(favored), len(emitted),
                        "favorites must not be all-or-nothing")


if __name__ == "__main__":
    unittest.main()