#!/usr/bin/env python3
"""Regression tests for three reproduced library-scan state findings.

Each finding was first reproduced deterministically against current main;
only reproducible behavior is pinned here (no real audio, no network).

1. Incomplete scan must not publish partial state.
   A real traversal error (unreadable directory: ``os.walk`` skips it
   silently by default) used to publish the partial track list as a
   successful scan, replacing the previous complete cache, advancing
   ``last_scan`` and marking the unseen live tracks as missing in the
   metadata store. An incomplete walk now keeps the previous cache,
   keeps ``last_scan``, reports an error and skips the metadata sync.
   A file that regularly vanishes mid-scan stays a per-file warning and
   never fails the whole scan.

2. Rescan prunes the committed queue.
   A successful rescan that retires tracks left the committed queue (and
   its index) referencing the removed ids, so later queue navigation
   served files the library no longer knows. The scan publish hook now
   prunes retired ids from the committed queue; a retired id can no
   longer be resolved as a library track.

3. Authoritative read waits for a scheduled scan.
   With a populated cache, an authoritative read returned the old cache
   immediately even though a necessary scan was already scheduled
   (``prepare_scan_status`` + queued worker), answering knowingly stale
   while ``scanning`` was true. It now waits for the scheduled scan and
   returns its result; with no scan scheduled it still uses the cache
   without scanning.
"""

import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

BASE_DIR = Path(tempfile.mkdtemp(prefix="fxroute-test-scan-state-"))
MUSIC_ROOT = BASE_DIR / "music"
CONFIG_DIR = BASE_DIR / "config"

os.environ["MUSIC_ROOT"] = str(MUSIC_ROOT)
os.environ["XDG_CONFIG_HOME"] = str(CONFIG_DIR)
os.environ["LOG_LEVEL"] = "WARNING"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library.core import LibraryScanner
from library.metadata import LibraryMetadataStore
from playback.queue import PlaybackQueue, PlaybackQueueDependencies

logging.getLogger().setLevel(logging.WARNING)

GARBAGE_AUDIO = b"\x00garbage-not-audio\x00" * 64


def _no_network(*args, **kwargs):
    raise RuntimeError("network disabled in tests")


def _store(base: Path, name: str) -> LibraryMetadataStore:
    return LibraryMetadataStore(base / f"{name}.sqlite", base / f"{name}-covers")


def _walk_reports_error(root: Path) -> bool:
    """Whether the test host really enforces directory permissions."""
    errors: list = []
    for _entry in os.walk(root, onerror=errors.append):
        pass
    return bool(errors)


class _ScanTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        network = patch.object(LibraryMetadataStore, "_request_json", _no_network)
        network.start()
        self.addCleanup(network.stop)

    def _scanner(self, root: Path, name: str = "lib") -> LibraryScanner:
        return LibraryScanner(root, metadata_store=_store(self.base, name))


# ---------------------------------------------------------------------------
# 1. Incomplete scan keeps the previous state
# ---------------------------------------------------------------------------

class IncompleteScanKeepsPreviousStateTests(_ScanTestBase):
    def _two_dir_root(self) -> Path:
        root = self.base / "music"
        (root / "a").mkdir(parents=True)
        (root / "b").mkdir(parents=True)
        (root / "a" / "song-a.mp3").write_bytes(GARBAGE_AUDIO)
        (root / "b" / "song-b.mp3").write_bytes(GARBAGE_AUDIO)
        return root

    def test_traversal_error_keeps_previous_cache_and_marks_error(self):
        root = self._two_dir_root()
        scanner = self._scanner(root, "incomplete")
        initial = scanner.refresh(True)
        self.assertEqual({track.id for track in initial}, {"local_a/song-a.mp3", "local_b/song-b.mp3"})
        self.assertIsNone(scanner.error)
        last_scan = scanner.last_scan
        self.assertIsNotNone(last_scan)

        (root / "b").chmod(0o000)
        self.addCleanup(os.chmod, root / "b", 0o755)
        if not _walk_reports_error(root):
            self.skipTest("host does not enforce directory permissions for os.walk")
        try:
            rescanned = scanner.refresh(True)
        finally:
            os.chmod(root / "b", 0o755)

        self.assertEqual(
            {track.id for track in rescanned},
            {"local_a/song-a.mp3", "local_b/song-b.mp3"},
            "an incomplete walk must return the previous state, not the partial list",
        )
        self.assertEqual(
            {track.id for track in scanner.get_tracks(refresh=False)},
            {"local_a/song-a.mp3", "local_b/song-b.mp3"},
        )
        self.assertEqual(scanner.last_scan, last_scan, "a failed scan must not advance last_scan")
        self.assertIsNotNone(scanner.error)
        self.assertIn("incomplete", str(scanner.error).lower())
        status = scanner.status()
        self.assertEqual(status["track_count"], 2)
        self.assertIsNotNone(status["error"])

        with scanner.metadata_store._connect() as conn:
            rows = conn.execute("SELECT rel_path, missing_since FROM tracks").fetchall()
        by_path = {row["rel_path"]: row["missing_since"] for row in rows}
        self.assertIsNone(
            by_path.get("b/song-b.mp3"),
            "a partial walk must not mark the unseen live track as missing",
        )

    def test_regularly_vanishing_file_stays_successful(self):
        root = self.base / "music-vanish"
        root.mkdir(parents=True)
        (root / "keep.mp3").write_bytes(GARBAGE_AUDIO)
        (root / "gone.mp3").write_bytes(GARBAGE_AUDIO)
        scanner = self._scanner(root, "vanish")
        self.assertEqual(len(scanner.refresh(True)), 2)
        (root / "gone.mp3").unlink()
        tracks = scanner.refresh(True)
        self.assertEqual([track.id for track in tracks], ["local_keep.mp3"])
        self.assertIsNone(scanner.error)
        self.assertIsNotNone(scanner.last_scan)


# ---------------------------------------------------------------------------
# 2. Rescan prunes the committed queue
# ---------------------------------------------------------------------------

def _queue_deps(scanner: LibraryScanner) -> PlaybackQueueDependencies:
    return PlaybackQueueDependencies(
        player=lambda: None,
        run_transition=lambda request: None,
        commit_coordinated_track=lambda *args, **kwargs: None,
        get_current_track_info=lambda: None,
        set_track_context=lambda *args: None,
        transition_is_active=lambda: False,
        player_is_running=lambda *args: False,
        wait_for_player_current_file=None,
        coordinator_target_rate=lambda *args, **kwargs: 44100,
        coordinator_rate_change=lambda *args: False,
        sample_rate_policy_is_auto=lambda: False,
        transition_error_http=lambda exc: exc,
        get_tracks=lambda: scanner.get_tracks(refresh=False),
        build_playback_payload=lambda *args, **kwargs: {},
    )


class RescanPrunesQueueTests(_ScanTestBase):
    def test_successful_rescan_prunes_retired_queue_entries(self):
        root = self.base / "music-queue"
        root.mkdir(parents=True)
        for name in ("a.mp3", "b.mp3", "c.mp3"):
            (root / name).write_bytes(GARBAGE_AUDIO)
        scanner = self._scanner(root, "queue")
        scanner.refresh(True)
        queue = PlaybackQueue(_queue_deps(scanner))
        queue.commit(queue.prepare_local_queue(
            "local_b.mp3", ["local_a.mp3", "local_b.mp3", "local_c.mp3"],
        ))
        self.assertEqual(queue.index, 1)
        scanner.set_scan_published_hook(lambda ids: queue.prune_removed_tracks(ids))

        (root / "b.mp3").unlink()
        scanner.refresh(True)

        self.assertEqual({track.id for track in scanner.get_tracks(refresh=False)}, {"local_a.mp3", "local_c.mp3"})
        self.assertIsNone(scanner.error, "the rescan itself succeeded")
        remaining = [track["id"] for track in queue.tracks]
        self.assertNotIn("local_b.mp3", remaining, "retired track must leave the queue")
        self.assertEqual(remaining, ["local_a.mp3", "local_c.mp3"])
        self.assertNotIn("local_b.mp3", [track.get("id") for track in queue.original])
        self.assertTrue(0 <= queue.index < len(queue.tracks))
        current = queue.tracks[queue.index]
        self.assertIn(current["id"], {"local_a.mp3", "local_c.mp3"})
        self.assertTrue(Path(current["url"]).is_file(), "queue must only reference existing files")

    def test_retired_id_is_no_longer_a_valid_library_track(self):
        root = self.base / "music-stale"
        root.mkdir(parents=True)
        for name in ("a.mp3", "b.mp3"):
            (root / name).write_bytes(GARBAGE_AUDIO)
        scanner = self._scanner(root, "stale")
        scanner.refresh(True)
        queue = PlaybackQueue(_queue_deps(scanner))
        queue.commit(queue.prepare_local_queue("local_a.mp3", ["local_a.mp3", "local_b.mp3"]))
        (root / "b.mp3").unlink()
        scanner.refresh(True)
        summary = queue.prune_removed_tracks([track.id for track in scanner.get_tracks(refresh=False)])
        self.assertEqual(summary["removed"], 1)
        from fastapi import HTTPException as FastAPIHTTPException

        with self.assertRaises(FastAPIHTTPException) as ctx:
            queue.prepare_local_queue("local_b.mp3", tracks=scanner.get_tracks(refresh=False))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_prune_to_empty_resets_the_queue(self):
        scanner = self._scanner(self.base / "music-empty", "empty")
        queue = PlaybackQueue(_queue_deps(scanner))
        queue.tracks = [{"id": "local_a.mp3", "url": "/music/a.mp3", "source": "local"}]
        queue.original = [dict(queue.tracks[0])]
        queue.index = 0
        queue.loop = True
        summary = queue.prune_removed_tracks(set())
        self.assertEqual(summary, {"removed": 1, "count": 0, "index": -1})
        self.assertEqual(queue.tracks, [])
        self.assertEqual(queue.index, -1)
        self.assertFalse(queue.loop)
        self.assertFalse(queue.shuffle)
        self.assertFalse(queue.single_track_loop)

    def test_prune_native_queue_falls_back_to_app_replace(self):
        scanner = self._scanner(self.base / "music-native", "native")
        queue = PlaybackQueue(_queue_deps(scanner))
        queue.tracks = [
            {"id": "local_a.mp3", "url": "/music/a.mp3", "source": "local", "sample_rate_hz": 44100},
            {"id": "local_b.mp3", "url": "/music/b.mp3", "source": "local", "sample_rate_hz": 44100},
        ]
        queue.original = [dict(item) for item in queue.tracks]
        queue.index = 0
        queue.mode = "native_mpv"
        summary = queue.prune_removed_tracks({"local_a.mp3"})
        self.assertEqual(summary["removed"], 1)
        self.assertEqual([track["id"] for track in queue.tracks], ["local_a.mp3"])
        self.assertEqual(queue.mode, "app_replace")


# ---------------------------------------------------------------------------
# 3. Authoritative read waits for a scheduled scan
# ---------------------------------------------------------------------------

class AuthoritativeWaitsForScheduledScanTests(_ScanTestBase):
    def test_authoritative_waits_for_scheduled_scan_with_populated_cache(self):
        root = self.base / "music-auth"
        root.mkdir(parents=True)
        (root / "a.mp3").write_bytes(GARBAGE_AUDIO)
        scanner = self._scanner(root, "auth")
        scanner.refresh(True)
        self.assertEqual([track.id for track in scanner.get_tracks(refresh=False)], ["local_a.mp3"])

        (root / "b.mp3").write_bytes(GARBAGE_AUDIO)
        with scanner.metadata_store._connect() as conn:
            conn.execute("DELETE FROM tracks WHERE rel_path = 'b.mp3'")
        entered = threading.Event()
        release = threading.Event()
        failsafe = threading.Timer(25, release.set)
        failsafe.daemon = True
        failsafe.start()
        self.addCleanup(failsafe.cancel)
        self.addCleanup(release.set)
        original = LibraryScanner._create_track_from_file

        def slow(self_obj, filepath):
            if filepath.name == "b.mp3":
                entered.set()
                if not release.wait(25):
                    raise RuntimeError("gate timed out waiting for release")
            return original(self_obj, filepath)

        with patch.object(LibraryScanner, "_create_track_from_file", slow):
            # A necessary scan is scheduled while the cache is still
            # populated; the authoritative read starts before any worker.
            scanner.prepare_scan_status()
            self.assertTrue(scanner.scanning)
            outcome: dict = {}

            def authoritative():
                outcome["tracks"] = scanner.get_tracks(authoritative=True)

            reader = threading.Thread(target=authoritative)
            reader.start()
            time.sleep(0.5)
            self.assertTrue(reader.is_alive(), "authoritative read must wait for the scheduled scan")
            worker = threading.Thread(target=lambda: scanner.refresh(True))
            worker.start()
            self.assertTrue(entered.wait(20), "worker did not reach the scan body in time")
            time.sleep(0.3)
            self.assertTrue(reader.is_alive(), "authoritative read must keep waiting while the scan runs")
            release.set()
            worker.join(20)
            reader.join(20)

        self.assertFalse(reader.is_alive(), "authoritative read did not settle")
        self.assertFalse(worker.is_alive(), "scan worker did not settle")
        self.assertEqual(
            sorted(track.id for track in outcome["tracks"]),
            ["local_a.mp3", "local_b.mp3"],
            "authoritative read must return the scheduled scan result, not the stale cache",
        )

    def test_authoritative_idle_uses_cache_without_scan(self):
        root = self.base / "music-auth-idle"
        root.mkdir(parents=True)
        (root / "a.mp3").write_bytes(GARBAGE_AUDIO)
        scanner = self._scanner(root, "auth-idle")
        scanner.refresh(True)
        calls = {"n": 0}
        real_run = LibraryScanner._run_scan_locked

        def counting_run(self_obj):
            calls["n"] += 1
            return real_run(self_obj)

        with patch.object(LibraryScanner, "_run_scan_locked", counting_run):
            tracks = scanner.get_tracks(authoritative=True)
        self.assertEqual([track.id for track in tracks], ["local_a.mp3"])
        self.assertEqual(calls["n"], 0, "idle authoritative read must not scan")


if __name__ == "__main__":
    unittest.main(verbosity=2)
