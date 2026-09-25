#!/usr/bin/env python3
"""Regression tests: a favorite changed mid-scan must survive the scan publish.

Reproduced defect: a scan builds its track list with a stale
``favorite=false`` snapshot. The user then sets ``favorite=true`` (correct in
the DB, live cache patched). When the already prepared scan publishes its
track list afterwards, the wholesale ``_track_cache`` swap discards the
patch and the live library shows the old state again until the next scan.

Each test holds a scan thread immediately before its cache publish, mutates
a favorite from the main thread, releases the scan, and then asserts that
the DB and the published track cache agree on the new state.

BulkFavoritesScaleTests covers libraries above the SQLite variable limit
(``MAX_VARIABLE_NUMBER``): the favorite overlay must return complete
results for 500 / 5.000 / 40.000 ids, keep excluding ``missing_since``
rows, and keep the favorite-vs-scan race closed at that scale.
"""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

BASE_DIR = Path(tempfile.mkdtemp(prefix="fxroute-test-scan-fav-race-"))
MUSIC_ROOT = BASE_DIR / "music"
CONFIG_DIR = BASE_DIR / "config"
MUSIC_ROOT.mkdir(parents=True, exist_ok=True)

os.environ["MUSIC_ROOT"] = str(MUSIC_ROOT)
os.environ["XDG_CONFIG_HOME"] = str(CONFIG_DIR)
os.environ["LOG_LEVEL"] = "WARNING"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library.core import LibraryScanner
from library.metadata import LibraryMetadataStore

GARBAGE_AUDIO = b"\x00garbage-not-audio\x00" * 64


def _no_network(*args, **kwargs):
    raise RuntimeError("network disabled in tests")


class _PrePublishGate:
    """Hold a scan thread right before it publishes its track list.

    The hook wraps ``_log_metadata_fd_counts`` at the
    ``after-track-cache-pass`` checkpoint: the file walk (and with it the
    potentially stale favorite snapshot) is done, the sort + cache swap
    have not run yet.
    """

    def __init__(self, scanner: LibraryScanner):
        self.entered = threading.Event()
        self.release = threading.Event()
        self._real = scanner._log_metadata_fd_counts

        def wrapper(label: str) -> None:
            if label == "after-track-cache-pass":
                self.entered.set()
                if not self.release.wait(30):
                    raise RuntimeError("pre-publish gate timed out waiting for release")
            return self._real(label)

        scanner._log_metadata_fd_counts = wrapper


class ScanFavoriteRaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        network = patch.object(LibraryMetadataStore, "_request_json", _no_network)
        network.start()
        self.addCleanup(network.stop)
        self.root = self.base / "music"
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("a.mp3", "b.mp3"):
            (self.root / name).write_bytes(GARBAGE_AUDIO)
        self.store = LibraryMetadataStore(
            self.base / "meta.sqlite", self.base / "meta-covers"
        )
        self.scanner = LibraryScanner(self.root, metadata_store=self.store)
        # Baseline: one completed scan, both tracks unfavorited.
        self.scanner.refresh(True)

    def _run_gated_scan(self):
        """Run a scan held just before publish; returns (thread, errors, gate)."""
        gate = _PrePublishGate(self.scanner)
        errors: list[BaseException] = []

        def _target():
            try:
                self.scanner.refresh(True)
            except BaseException as exc:  # noqa: BLE001 - re-raised in main thread
                errors.append(exc)

        thread = threading.Thread(target=_target, name="gated-scan")
        thread.start()
        self.assertTrue(
            gate.entered.wait(30),
            "scan did not reach the pre-publish checkpoint in time",
        )
        return thread, errors, gate

    def _finish_gated_scan(self, thread, errors, gate):
        gate.release.set()
        thread.join(30)
        self.assertFalse(thread.is_alive(), "scan thread did not finish in time")
        if errors:
            raise errors[0]

    def _db_favorite(self, track_id: str) -> bool:
        # Plain SQL (no new helpers) so this assertion also runs against
        # the unfixed code and shows the stale-cache mismatch there.
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT favorite FROM tracks WHERE track_id = ? AND missing_since IS NULL",
                (track_id,),
            ).fetchone()
        return bool(row["favorite"]) if row else False

    def _published_favorite(self, track_id: str) -> bool:
        for track in self.scanner.get_tracks(refresh=False):
            if track.id == track_id:
                return bool(track.favorite)
        self.fail(f"track {track_id} missing from published cache")

    def test_favorite_set_during_scan_survives_publish(self):
        track_id = "local_a.mp3"
        self.assertFalse(self._published_favorite(track_id))
        thread, errors, gate = self._run_gated_scan()
        try:
            result = self.scanner.set_track_favorite(track_id, True)
            self.assertTrue(result["favorite"])
        finally:
            self._finish_gated_scan(thread, errors, gate)
        self.assertTrue(self._db_favorite(track_id), "DB must hold the new favorite")
        self.assertTrue(
            self._published_favorite(track_id),
            "published cache must not overwrite the newer favorite with the stale snapshot",
        )

    def test_favorite_unset_during_scan_survives_publish(self):
        track_id = "local_a.mp3"
        self.scanner.set_track_favorite(track_id, True)
        self.assertTrue(self._published_favorite(track_id))
        thread, errors, gate = self._run_gated_scan()
        try:
            result = self.scanner.set_track_favorite(track_id, False)
            self.assertFalse(result["favorite"])
        finally:
            self._finish_gated_scan(thread, errors, gate)
        self.assertFalse(self._db_favorite(track_id), "DB must hold the unset favorite")
        self.assertFalse(
            self._published_favorite(track_id),
            "published cache must not resurrect the stale favorite snapshot",
        )

    def test_scan_without_parallel_mutation_is_unchanged(self):
        self.scanner.set_track_favorite("local_a.mp3", True)
        tracks = self.scanner.refresh(True)
        by_id = {track.id: track for track in tracks}
        self.assertTrue(by_id["local_a.mp3"].favorite)
        self.assertFalse(by_id["local_b.mp3"].favorite)
        self.assertTrue(self._db_favorite("local_a.mp3"))
        self.assertFalse(self._db_favorite("local_b.mp3"))

    def test_bulk_favorites_read_skips_unknown_ids(self):
        self.assertEqual(self.store.get_track_favorites([]), {})
        self.assertEqual(
            self.store.get_track_favorites(["local_nope.mp3", ""]),
            {},
        )
        self.scanner.set_track_favorite("local_a.mp3", True)
        self.assertEqual(
            self.store.get_track_favorites(["local_a.mp3", "local_b.mp3", "local_nope.mp3"]),
            {"local_a.mp3": True, "local_b.mp3": False},
        )


class BulkFavoritesScaleTests(unittest.TestCase):
    """Favorite overlay above the SQLite variable limit.

    Reproduced defect: ``get_track_favorites`` built a single ``IN`` query
    with one placeholder per id. Above ``MAX_VARIABLE_NUMBER`` (32766 on
    current builds, 999 on older ones) SQLite raised "too many SQL
    variables", the lookup returned ``{}``, and the scan-publish overlay
    silently stopped applying stored favorites for large libraries.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        network = patch.object(LibraryMetadataStore, "_request_json", _no_network)
        network.start()
        self.addCleanup(network.stop)
        self.root = self.base / "music"
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = LibraryMetadataStore(
            self.base / "meta.sqlite", self.base / "meta-covers"
        )
        self.scanner = LibraryScanner(self.root, metadata_store=self.store)

    def _seed(self, count: int) -> set[str]:
        """Insert ``count`` tracks; every 7th is favorited. Returns favored ids."""
        favored = {f"local_scale_{index:05d}.mp3" for index in range(count) if index % 7 == 0}
        with self.store._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO tracks"
                " (rel_path, track_id, mtime_ns, size_bytes, title, favorite, missing_since)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL)",
                [
                    (
                        f"scale_{index:05d}.mp3",
                        f"local_scale_{index:05d}.mp3",
                        1,
                        100,
                        f"Scale {index}",
                        1 if f"local_scale_{index:05d}.mp3" in favored else 0,
                    )
                    for index in range(count)
                ],
            )
        return favored

    def _assert_full_overlay(self, count: int) -> None:
        favored = self._seed(count)
        ids = [f"local_scale_{index:05d}.mp3" for index in range(count)]
        result = self.store.get_track_favorites(ids)
        self.assertEqual(len(result), count)
        self.assertEqual(
            {track_id for track_id, is_favorite in result.items() if is_favorite},
            favored,
        )
        # Unknown ids stay omitted, empty/blank ids stay ignored.
        self.assertNotIn("local_scale_nope.mp3", self.store.get_track_favorites([*ids, "local_scale_nope.mp3", " ", ""]))
        self.assertEqual(
            self.store.get_track_favorites([*ids, "local_scale_nope.mp3"]),
            result,
        )

    def test_bulk_favorites_read_at_typical_sizes(self):
        self._assert_full_overlay(500)
        self._assert_full_overlay(5000)

    def test_bulk_favorites_read_above_sqlite_variable_limit(self):
        self._assert_full_overlay(40000)

    def test_bulk_favorites_read_excludes_missing_since_at_scale(self):
        favored = self._seed(40000)
        hidden_id = "local_scale_00007.mp3"
        self.assertIn(hidden_id, favored)
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE tracks SET missing_since = ? WHERE track_id = ?",
                ("2026-01-01T00:00:00+00:00", hidden_id),
            )
        ids = [f"local_scale_{index:05d}.mp3" for index in range(40000)]
        result = self.store.get_track_favorites(ids)
        self.assertEqual(len(result), 39999)
        self.assertNotIn(hidden_id, result)
        self.assertEqual(
            {track_id for track_id, is_favorite in result.items() if is_favorite},
            favored - {hidden_id},
        )

    def test_scan_publish_overlay_applies_at_scale(self):
        """The favorite-vs-scan race stays closed above the variable limit."""
        from models import Track

        favored = self._seed(40000)
        # A freshly walked list carries a stale favorite=false snapshot.
        built = [
            Track(id=f"local_scale_{index:05d}.mp3", title=f"Scale {index}", favorite=False)
            for index in range(40000)
        ]
        self.scanner._publish_tracks(built)
        published = {track.id: track.favorite for track in self.scanner.get_tracks(refresh=False)}
        self.assertEqual(len(published), 40000)
        self.assertEqual(
            {track_id for track_id, is_favorite in published.items() if is_favorite},
            favored,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
