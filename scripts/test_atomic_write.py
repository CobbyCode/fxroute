#!/usr/bin/env python3
"""Shared atomic-write contract: replace without partial files.

Behavioral contract for the canonical common.atomic_write_text used by
every JSON store (DSP presets/state, playlists, stations):

- overwrite replaces the target atomically with the complete new content;
- an existing regular target keeps its permission mode;
- a failure before the replace leaves the old target untouched;
- no temporary leftovers remain in the directory afterwards;
- a symlink target itself is replaced, never followed.

Only observable filesystem state is pinned (content, modes, directory
listing) — never temp names, descriptors, or other implementation details.
A wiring test additionally pins that all three stores resolve to the
canonical implementation instead of carrying their own copy.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.atomic_write import atomic_write_text

import dsp.persistence
import library.playlists
import radio.stations


IMPLEMENTATIONS = {"common.atomic_write": atomic_write_text}


class SingleCopyTests(unittest.TestCase):
    def test_all_stores_resolve_to_the_canonical_implementation(self):
        self.assertIs(dsp.persistence.atomic_write_text, atomic_write_text)
        self.assertIs(library.playlists.atomic_write_text, atomic_write_text)
        self.assertIs(radio.stations.atomic_write_text, atomic_write_text)
        for module in (dsp.persistence, library.playlists, radio.stations):
            self.assertFalse(
                any(
                    name in ("_atomic_write_text",)
                    for name, _ in vars(module).items()
                ),
                f"{module.__name__} must not carry its own copy",
            )


class AtomicWriteContractTests(unittest.TestCase):
    def test_replace_is_atomic_and_complete(self):
        for name, write in IMPLEMENTATIONS.items():
            with self.subTest(impl=name), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "store.json"
                target.write_text('{"v": 1}\n', encoding="utf-8")
                write(target, '{"v": 2, "items": [1, 2, 3]}\n')
                self.assertEqual(target.read_text(encoding="utf-8"), '{"v": 2, "items": [1, 2, 3]}\n')
                self.assertEqual(
                    sorted(entry.name for entry in Path(tmp).iterdir()),
                    ["store.json"],
                    "no temporary leftovers may remain",
                )

    def test_existing_mode_preserved(self):
        for name, write in IMPLEMENTATIONS.items():
            with self.subTest(impl=name), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "store.json"
                target.write_text("{}\n", encoding="utf-8")
                os.chmod(target, 0o640)
                write(target, '{"v": 2}\n')
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_error_before_replace_keeps_old_file(self):
        for name, write in IMPLEMENTATIONS.items():
            with self.subTest(impl=name), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "store.json"
                target.write_text("original\n", encoding="utf-8")
                with self.assertRaises(TypeError):
                    write(target, b"not-text")
                self.assertEqual(target.read_text(encoding="utf-8"), "original\n")
                self.assertEqual(
                    sorted(entry.name for entry in Path(tmp).iterdir()),
                    ["store.json"],
                    "failed writes must clean up their temp file",
                )

    def test_new_file_gets_safe_default(self):
        for name, write in IMPLEMENTATIONS.items():
            with self.subTest(impl=name), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "store.json"
                write(target, "{}\n")
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_symlink_target_replaced_not_followed(self):
        for name, write in IMPLEMENTATIONS.items():
            with self.subTest(impl=name), tempfile.TemporaryDirectory() as tmp:
                real = Path(tmp) / "real.json"
                real.write_text("real-data\n", encoding="utf-8")
                link = Path(tmp) / "store.json"
                link.symlink_to(real)
                write(link, '{"v": 9}\n')
                self.assertFalse(link.is_symlink(), "the link itself is replaced")
                self.assertEqual(link.read_text(encoding="utf-8"), '{"v": 9}\n')
                self.assertEqual(real.read_text(encoding="utf-8"), "real-data\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
