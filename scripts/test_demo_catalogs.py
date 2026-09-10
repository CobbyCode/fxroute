"""Regression test: the generated demo catalogs are deterministic.

scripts/build_demo_catalogs.py derives every track, album, playlist and
cover for the local library, TIDAL, Spotify, Qobuz (demo/data/library.js)
and the two SMB shares (demo/data/library2.js/.library3.js) from the pure
manifest in scripts/demo_covers.py. Re-rendering that pure data must
reproduce the committed fixtures byte-for-byte, so content edits always go
through the manifest — never the fixtures.

Run: python3 scripts/test_demo_catalogs.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

DATA = ROOT / "demo" / "data"
POOL = ROOT / "static" / "demo"


def hash_code(text: str) -> int:
    h = 0
    for ch in str(text):
        h = ((h << 5) - h + ord(ch)) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return h & 0x7FFFFFFF


def extract_const(path: Path, name: str):
    text = path.read_text(encoding="utf-8")
    match = re.search(r"(?:const|var|let)\s+" + re.escape(name) + r"\s*=\s*", text)
    assert match, f"{path.name} has no {name}"
    start = match.end()
    depth = 0
    instr: str | None = None
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == instr:
                instr = None
            continue
        if ch in "\"'":
            instr = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise AssertionError(f"could not parse {name} in {path.name}")


class CatalogTest(unittest.TestCase):
    def test_check_mode_passes(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_demo_catalogs.py"), "--check"],
            capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(proc.returncode, 0,
                         f"build_demo_catalogs.py --check failed: {proc.stdout} {proc.stderr}")

    def test_counts(self):
        lib2_seeds = extract_const(DATA / "library2.js", "SEEDS")
        lib3_seeds = extract_const(DATA / "library3.js", "SEEDS")
        self.assertEqual(len(lib2_seeds), 16, "SMB_Demo_Library-1 albums")
        self.assertEqual(len(lib3_seeds), 12, "SMB_Demo_Library-2 albums")
        for seeds in (lib2_seeds, lib3_seeds):
            for seed in seeds:
                self.assertGreaterEqual(len(seed[6]), 8)
                self.assertLessEqual(len(seed[6]), 14)

    def test_smb_favorites_match_formula(self):
        for name, prefix in (("library2.js", "d2-"), ("library3.js", "d3-")):
            fav = extract_const(DATA / name, "FAVORITES")
            self.assertTrue(fav, f"{name} has no favorites")
            self.assertTrue(any(fav.values()) and not all(fav.values()))
            for key, value in fav.items():
                kind, ident = key.split(":", 1)
                self.assertTrue(ident.startswith(prefix), f"{key} wrong prefix")
                if kind == "album":
                    self.assertEqual(value, hash_code("fav-album:" + ident) % 3 == 0, key)
                else:
                    self.assertEqual(value, hash_code("fav-track:" + ident) % 4 == 0, key)

    def test_about_coverage(self):
        for name in ("library.js", "library2.js", "library3.js"):
            text = (DATA / name).read_text(encoding="utf-8")
            self.assertIn("artist_description", text, name)
        for name in ("library2.js", "library3.js"):
            about = extract_const(DATA / name, "ARTIST_ABOUT")
            seeds = extract_const(DATA / name, "SEEDS")
            for seed in seeds:
                self.assertIn(seed[0], about, f"{name} artist without about: {seed[0]}")
            album_about = extract_const(DATA / name, "ALBUM_ABOUT")
            self.assertTrue(album_about, f"{name} needs an album-level about override")

    def test_pool_covers_exist(self):
        for name, idx in (("library.js", None), ("library2.js", 4), ("library3.js", 4)):
            path = DATA / name
            if idx is None:
                covers = {s[7] for s in extract_const(path, "TRACK_SEEDS")}
                covers |= {a["art_url"].split("/")[-1] for a in extract_const(path, "TIDAL_ARTISTS")}
                for seed in extract_const(path, "SPOTIFY_TRACK_SEEDS"):
                    covers.add(seed[8])
                for seed in extract_const(path, "QOBUZ_TRACK_SEEDS"):
                    covers.add(seed[8])
            else:
                covers = {s[idx] for s in extract_const(path, "SEEDS")}
            for cover in covers:
                self.assertTrue((POOL / cover).is_file(), f"missing pool cover {cover}")


if __name__ == "__main__":
    unittest.main()
