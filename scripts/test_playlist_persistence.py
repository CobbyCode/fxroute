#!/usr/bin/env python3
"""Regression tests for playlist persistence atomicity and mutation locking.

Covers the P2 contract: every persistent playlist mutation runs as an
atomic write (temp file + fsync + os.replace), complete read->mutate->write
cycles are serialized by the module-local mutation lock, the cache is
publication-safe, the on-disk format is unchanged, no temp files are left
behind on any failure path, and a persist failure is reported as an error
instead of a success.  All scenarios use a temporary XDG_CONFIG_HOME; no
user playlist file is touched.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import playlists


class PlaylistPersistenceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_root = Path(self._tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.config_root)
        self.playlists_file = self.config_root / "fxroute" / "playlists.json"
        self.playlists_file.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_file = self.config_root / "legacy-playlists.json"
        legacy_patch = patch.object(playlists, "_legacy_playlists_file", return_value=self.legacy_file)
        legacy_patch.start()
        self.addCleanup(legacy_patch.stop)
        self.addCleanup(self._tmp.cleanup)
        self._reset_cache()

    def _reset_cache(self):
        with playlists._cache_lock:
            playlists._cached_playlists = None
            playlists._cache_generation = 0

    def _seed(self, payload):
        self.playlists_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        self._reset_cache()

    def _disk_data(self):
        return json.loads(self.playlists_file.read_text(encoding="utf-8"))

    def _temp_leftovers(self):
        return [path.name for path in self.playlists_file.parent.iterdir() if path.name.endswith(".tmp")]

    def _run_threads(self, *fns):
        threads = [threading.Thread(target=fn) for fn in fns]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        for thread in threads:
            self.assertFalse(thread.is_alive(), "test thread did not finish")

    def test_parallel_creates_both_survive(self):
        start = threading.Barrier(2)
        results = []

        def create(name, track_ids):
            start.wait(timeout=10)
            results.append(playlists.save_playlist(name, track_ids).id)

        self._run_threads(
            lambda: create("Rock", ["A"]),
            lambda: create("Jazz", ["B"]),
        )
        self.assertEqual(sorted(results), ["jazz", "rock"])
        self.assertEqual(sorted(self._disk_ids()), sorted(results))
        self.assertEqual(self._temp_leftovers(), [])

    def _disk_ids(self):
        return [item["id"] for item in self._disk_data()]

    def test_parallel_update_and_create_both_survive(self):
        self._seed([{"id": "mix", "name": "Mix", "track_ids": ["A"]}])
        start = threading.Barrier(2)

        def update():
            start.wait(timeout=10)
            playlists.save_playlist("Mix", ["A", "B"])

        def create():
            start.wait(timeout=10)
            playlists.save_playlist("Jazz", ["C"])

        self._run_threads(update, create)
        by_id = {item["id"]: item["track_ids"] for item in self._disk_data()}
        self.assertEqual(by_id, {"mix": ["A", "B"], "jazz": ["C"]})
        self.assertEqual(self._temp_leftovers(), [])

    def test_write_failure_before_replace_preserves_old_file(self):
        original = json.dumps([{"id": "mix", "name": "Mix", "track_ids": ["A"]}], indent=2) + "\n"
        self._seed([{"id": "mix", "name": "Mix", "track_ids": ["A"]}])
        playlists.get_playlists()
        with patch("playlists.os.replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                playlists.save_playlist("Mix", ["A", "B"])
        self.assertEqual(self.playlists_file.read_bytes(), original.encode("utf-8"))
        self.assertEqual(self._temp_leftovers(), [])
        with playlists._cache_lock:
            cached = playlists._cached_playlists
        self.assertEqual([playlist.track_ids for playlist in cached], [["A"]])

    def test_write_failure_during_fsync_preserves_old_file(self):
        original = json.dumps([{"id": "mix", "name": "Mix", "track_ids": ["A"]}], indent=2) + "\n"
        self._seed([{"id": "mix", "name": "Mix", "track_ids": ["A"]}])
        with patch("playlists.os.fsync", side_effect=OSError("simulated fsync failure")):
            with self.assertRaises(OSError):
                playlists.save_playlist("Mix", ["A", "B"])
        self.assertEqual(self.playlists_file.read_bytes(), original.encode("utf-8"))
        self.assertEqual(self._temp_leftovers(), [])

    def test_persist_failure_is_reported_not_success(self):
        self._seed([{"id": "mix", "name": "Mix", "track_ids": ["A"]}])
        with patch("playlists.os.replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                playlists.save_playlist("Mix", ["A", "B"])
        with patch("playlists.os.replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                playlists.delete_playlist("mix")
        self.assertEqual([item["id"] for item in self._disk_data()], ["mix"])

    def test_save_writes_exact_format(self):
        playlist = playlists.save_playlist("Mix", ["A", "B"])
        self.assertEqual(playlist.id, "mix")
        self.assertEqual(
            self.playlists_file.read_text(encoding="utf-8"),
            json.dumps([{"id": "mix", "name": "Mix", "track_ids": ["A", "B"]}], indent=2) + "\n",
        )
        self.assertEqual(self._temp_leftovers(), [])

    def test_missing_file_first_write_creates_valid_json(self):
        self.assertEqual(playlists.get_playlists(), [])
        self.assertEqual(self.playlists_file.read_text(encoding="utf-8"), "[]\n")
        playlists.save_playlist("Rock", ["A"])
        self.assertEqual([item["id"] for item in self._disk_data()], ["rock"])
        self.assertEqual(self._temp_leftovers(), [])

    def test_legacy_file_migrated_atomically(self):
        self.legacy_file.write_text(
            json.dumps([{"id": "old", "name": "Old", "track_ids": ["X"]}], indent=2) + "\n",
            encoding="utf-8",
        )
        playlists.get_playlists()
        self.assertEqual([item["id"] for item in self._disk_data()], ["old"])
        self.assertEqual(self._temp_leftovers(), [])

    def test_reader_never_publishes_stale_snapshot_after_commit(self):
        self._seed([{"id": "mix", "name": "Mix", "track_ids": ["A"]}])
        orig_load = playlists._load_raw_playlists
        reader_entered = threading.Event()
        release_reader = threading.Event()

        def gated_load():
            data = orig_load()
            if not reader_entered.is_set():
                reader_entered.set()
                self.assertTrue(release_reader.wait(timeout=15), "reader gate not released")
            return data

        reader_results = []

        def reader():
            reader_results.append(playlists.get_playlists())

        def writer():
            self.assertTrue(reader_entered.wait(timeout=15), "reader never entered gate")
            playlists.save_playlist("Mix", ["A", "B"])
            release_reader.set()

        playlists._load_raw_playlists = gated_load
        try:
            self._run_threads(reader, writer)
        finally:
            playlists._load_raw_playlists = orig_load

        self.assertEqual([playlist.track_ids for playlist in reader_results[0]], [["A", "B"]])
        with playlists._cache_lock:
            cached = playlists._cached_playlists
        self.assertEqual([playlist.track_ids for playlist in cached], [["A", "B"]])


if __name__ == "__main__":
    unittest.main()
