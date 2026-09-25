#!/usr/bin/env python3
"""Station store mutations must serialize their own read -> persist cycle.

The API serializes station mutations externally (radio_api ownership), but
the store must hold its own invariant: every mutating operation runs its
complete read -> mutate -> persist -> cache-update cycle under one
module-owned RLock. Otherwise two concurrent writers read the same
snapshot and the second persist silently drops the first write.

Pinned deterministically (no network, no audio, temporary config dir):

1. Parallel direct add_station calls lose no write.
2. Parallel different mutations leave a consistent JSON file, cache and
   read view (no torn or missing entries).
3. Parallel delete/update of the same id stays consistent.
4. Reads (read-only mode) never serialize behind a mutation, and the
   URL dedupe from main still holds under concurrency.
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

import radio.stations as stations


class StationMutationLockTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._previous_config_home = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self._tmp.name
        self.addCleanup(self._restore_config_home)
        # Network-free resolution: every probe URL is a direct stream.
        patcher = patch.object(stations, "resolve_stream_url", side_effect=lambda url: stations._normalize_url(url))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._reset_cache)

    def _restore_config_home(self):
        if self._previous_config_home is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._previous_config_home

    def _reset_cache(self):
        with stations._cache_lock:
            stations._cached_stations = None
            stations._cache_generation = 0

    def _run_parallel(self, workers):
        barrier = threading.Barrier(len(workers))
        results: list = []
        errors: list = []
        lock = threading.Lock()

        def run(index, fn):
            try:
                barrier.wait(timeout=15)
                value = fn(index)
            except Exception as exc:  # recorded, asserted by the caller
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")
                return
            with lock:
                results.append(value)

        threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [], "worker raised")
        return results

    def _stored_ids(self):
        path = Path(self._tmp.name) / "fxroute" / "stations.json"
        return {str(item.get("id")) for item in json.loads(path.read_text(encoding="utf-8"))}


class StationMutationLockTests(StationMutationLockTestCase):
    def test_parallel_add_station_loses_no_write(self):
        workers = 24
        baseline = {s.id for s in stations.get_stations()}
        results = self._run_parallel([
            (lambda i: stations.add_station(f"Parallel {i}", f"https://example.org/p{i}.mp3"))
            for i in range(workers)
        ])
        self.assertEqual(len(results), workers)
        saved = {s.stream_url for s in stations.get_stations() if s.stream_url.startswith("https://example.org/p")}
        self.assertEqual(len(saved), workers, "a parallel station write was lost")
        # JSON file, cache and read view agree.
        probe_ids = {s.id for s in stations.get_stations() if s.stream_url.startswith("https://example.org/p")}
        self.assertEqual(len(probe_ids), workers)
        self.assertEqual(self._stored_ids(), baseline | probe_ids)

    def test_parallel_mixed_mutations_leave_consistent_state(self):
        seed = [stations.add_station(f"Seed {i}", f"https://example.org/seed{i}.mp3") for i in range(6)]
        workers = [
            lambda i: stations.add_station(f"Added {i % 6}", f"https://example.org/added{i % 6}.mp3"),
            lambda i: stations.update_station(seed[i % 3].id, f"Renamed {i % 3}", f"https://example.org/seed{i % 3}.mp3"),
            lambda i: stations.delete_station(seed[3 + (i % 3)].id),
            lambda i: stations.add_catalog_station("groovesalad"),
        ] * 3
        results = self._run_parallel(workers)
        self.assertEqual(len(results), 12)
        # A parseable array, and the cache view equals the file content.
        stored = self._stored_ids()
        self.assertEqual(stored, {s.id for s in stations.get_stations()})
        # The catalog add ran 4 times under concurrency: one entry only.
        catalog_entries = [s for s in stations.get_stations() if s.stream_url.startswith("https://ice4.somafm.com/groovesalad")]
        self.assertEqual(len(catalog_entries), 1, "concurrent catalog add must stay deduplicated")
        # Deleted ids are gone from the file, too.
        for deleted in seed[3:6]:
            self.assertNotIn(deleted.id, stored)

    def test_parallel_duplicate_adds_dedupe_to_one_station(self):
        results = self._run_parallel([
            (lambda i: stations.add_station("Same Sender", "https://example.org/same.mp3"))
            for _ in range(16)
        ])
        self.assertEqual(len({r.id for r in results}), 1, "dedupe regressed under concurrency")
        matching = [s for s in stations.get_stations() if s.stream_url == "https://example.org/same.mp3"]
        self.assertEqual(len(matching), 1)

    def test_parallel_delete_of_same_station_keeps_single_removal(self):
        target = stations.add_station("Doomed", "https://example.org/doomed.mp3")
        errors: list = []

        def worker():
            try:
                stations.delete_station(target.id)
            except FileNotFoundError:
                errors.append("missing")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        # Exactly one delete can succeed; the rest report "not found".
        self.assertEqual(len(errors), 7)
        self.assertNotIn(target.id, self._stored_ids())
        self.assertEqual([s.id for s in stations.get_stations() if s.id == target.id], [])

    def test_read_only_load_stays_off_the_mutation_lock(self):
        stations.add_station("Reader Base", "https://example.org/reader.mp3")
        stations.get_stations()  # populate the cache
        entries: list = []
        real_lock = stations._mutation_lock

        class RecordingLock:
            def __enter__(self):
                entries.append("acquired")
                return real_lock.__enter__()

            def __exit__(self, *exc):
                return real_lock.__exit__(*exc)

        with patch.object(stations, "_mutation_lock", RecordingLock()):
            # Warm cache: the read-only load must not serialize at all.
            stations.get_stations()
            self.assertEqual(entries, [], "read-only load must not take the mutation lock")
            # A mutation must (re-entrantly, for its nested helpers).
            stations.add_station("Writer", "https://example.org/writer.mp3")
            self.assertGreaterEqual(len(entries), 1, "mutation must run its cycle under the lock")
        self.assertEqual(
            {s.stream_url for s in stations.get_stations() if s.stream_url.startswith("https://example.org/")},
            {"https://example.org/reader.mp3", "https://example.org/writer.mp3"},
        )

    def test_steady_state_read_does_not_queue_behind_a_held_mutation_lock(self):
        """A read must not block on a mutation that holds the lock.

        The enrich path holds the mutation lock across network work, so a
        steady-state read (existing store file, warm cache) must not need
        the lock at all.
        """
        stations.add_station("Cold Base", "https://example.org/cold.mp3")
        stations.get_stations()
        holding = threading.Event()
        released = threading.Event()
        real_lock = stations._mutation_lock

        def holder():
            with real_lock:
                holding.set()
                released.wait(10)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(holding.wait(5), "the holder never took the mutation lock")
        try:
            done = threading.Event()

            def reader():
                stations.get_stations()
                done.set()

            read_thread = threading.Thread(target=reader)
            read_thread.start()
            self.assertTrue(done.wait(2), "steady-state read blocked behind the mutation lock")
            read_thread.join(5)
        finally:
            released.set()
            thread.join(10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
