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


if __name__ == "__main__":
    unittest.main(verbosity=2)
