#!/usr/bin/env python3
"""Regression tests: a corrupt playlists.json must never be overwritten.

Fail-closed contract for the playlist store:
- valid store -> mutations work normally
- syntactically broken JSON -> every mutation raises
  PlaylistStoreCorruptedError and the file stays byte-identical
- structurally invalid content (valid JSON, not an array) -> every
  mutation raises PlaylistStoreCorruptedError and the file stays unchanged
- reads stay available during corruption (empty list fallback, uncached)
- after restoring a valid store, mutations and reads work again without
  any manual cache reset
- the HTTP layer reports store corruption as 500, not as a 400
  client-validation error

All scenarios use a temporary XDG_CONFIG_HOME; no user playlist file is
touched.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library.api as library_api
import library.playlists as playlists
from library.playlists import PlaylistStoreCorruptedError

BROKEN_PAYLOADS = (
    b"{broken",
    b'[{"id": "mix", "name": "Mix", "track_ids": ["A"]',
    b"\xff\xfe not json \x00",
)

STRUCTURALLY_INVALID_PAYLOADS = (
    b'{"id": "mix", "name": "Mix"}',
    b"null",
    b'"just a string"',
    b"42",
)


class PlaylistStoreCorruptionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_root = Path(self._tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.config_root)
        self.playlists_file = self.config_root / "fxroute" / "playlists.json"
        self.playlists_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_patch = patch.object(
            playlists,
            "_legacy_playlists_file",
            return_value=self.config_root / "legacy-playlists.json",
        )
        legacy_patch.start()
        self.addCleanup(legacy_patch.stop)
        self.addCleanup(self._tmp.cleanup)
        self._reset_cache()

    def _reset_cache(self):
        with playlists._cache_lock:
            playlists._cached_playlists = None
            playlists._cache_generation = 0

    def _write_bytes(self, payload: bytes):
        self.playlists_file.write_bytes(payload)
        self._reset_cache()

    def _seed(self, payload):
        self._write_bytes((json.dumps(payload, indent=2) + "\n").encode("utf-8"))

    def _disk_bytes(self) -> bytes:
        return self.playlists_file.read_bytes()

    def _mutations(self):
        return (
            lambda: playlists.save_playlist("New", ["A"]),
            lambda: playlists.save_new_playlist("New", ["A"]),
            lambda: playlists.delete_playlist("mix"),
        )

    def test_valid_store_mutations_work(self):
        self._seed([{"id": "mix", "name": "Mix", "track_ids": ["A"]}])
        created = playlists.save_playlist("Rock", ["B"])
        self.assertEqual(created.id, "rock")
        updated = playlists.save_playlist("Mix", ["A", "B"])
        self.assertEqual(updated.id, "mix")
        imported = playlists.save_new_playlist("Mix", ["C"])
        self.assertEqual(imported.name, "Mix (2)")
        playlists.delete_playlist("rock")
        self.assertEqual(
            [item["id"] for item in json.loads(self.playlists_file.read_text(encoding="utf-8"))],
            ["mix", "mix-2"],
        )

    def test_broken_json_mutations_fail_and_preserve_bytes(self):
        for payload in BROKEN_PAYLOADS:
            with self.subTest(payload=payload):
                self._write_bytes(payload)
                for mutate in self._mutations():
                    with self.assertRaises(PlaylistStoreCorruptedError):
                        mutate()
                self.assertEqual(self._disk_bytes(), payload)

    def test_structurally_invalid_mutations_fail_and_preserve_bytes(self):
        for payload in STRUCTURALLY_INVALID_PAYLOADS:
            with self.subTest(payload=payload):
                self._write_bytes(payload)
                for mutate in self._mutations():
                    with self.assertRaises(PlaylistStoreCorruptedError):
                        mutate()
                self.assertEqual(self._disk_bytes(), payload)

    def test_corruption_error_is_recognizable(self):
        self._write_bytes(b"{broken")
        with self.assertRaises(PlaylistStoreCorruptedError) as ctx:
            playlists.save_playlist("New", ["A"])
        self.assertIsInstance(ctx.exception, ValueError)
        self.assertIn("corrupt", str(ctx.exception))

    def test_reads_stay_available_during_corruption(self):
        self._write_bytes(b"{broken")
        self.assertEqual(playlists.get_playlists(), [])

    def test_reads_stay_available_for_structurally_invalid_store(self):
        for payload in STRUCTURALLY_INVALID_PAYLOADS:
            with self.subTest(payload=payload):
                self._write_bytes(payload)
                self.assertEqual(playlists.get_playlists(), [])
                self.assertEqual(self._disk_bytes(), payload)

    def test_recovery_after_restore_needs_no_cache_reset(self):
        self._write_bytes(b"{broken")
        with self.assertRaises(PlaylistStoreCorruptedError):
            playlists.save_playlist("New", ["A"])
        self.assertEqual(playlists.get_playlists(), [])
        self._seed([{"id": "mix", "name": "Mix", "track_ids": ["A"]}])
        created = playlists.save_playlist("Rock", ["B"])
        self.assertEqual(created.id, "rock")
        self.assertEqual(
            sorted(playlist.id for playlist in playlists.get_playlists()),
            ["mix", "rock"],
        )

    def test_api_reports_corruption_as_server_error(self):
        self._write_bytes(b"{broken")
        before = self._disk_bytes()
        with self.assertRaises(library_api.HTTPException) as ctx:
            asyncio.run(
                library_api.create_or_update_playlist(
                    SimpleNamespace(name="New", track_ids=["A"])
                )
            )
        self.assertEqual(ctx.exception.status_code, 500)
        with self.assertRaises(library_api.HTTPException) as ctx:
            asyncio.run(library_api.remove_playlist("mix"))
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(self._disk_bytes(), before)

    def test_api_reports_structural_corruption_as_server_error(self):
        self._write_bytes(b'{"id": "mix", "name": "Mix"}')
        before = self._disk_bytes()
        with self.assertRaises(library_api.HTTPException) as ctx:
            asyncio.run(
                library_api.create_or_update_playlist(
                    SimpleNamespace(name="New", track_ids=["A"])
                )
            )
        self.assertEqual(ctx.exception.status_code, 500)
        with self.assertRaises(library_api.HTTPException) as ctx:
            asyncio.run(library_api.remove_playlist("mix"))
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(self._disk_bytes(), before)


if __name__ == "__main__":
    unittest.main()
