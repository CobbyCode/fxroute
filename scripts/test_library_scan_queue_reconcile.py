#!/usr/bin/env python3
"""Post-scan queue reconcile must run on the owning asyncio event loop.

The library scan runs in ``asyncio.to_thread`` workers while PlaybackQueue
is owned by the event loop without a thread lock. The scan publish hook
therefore only snapshots the fresh id list and schedules the reconcile on
the owning loop; the worker never mutates queue state itself.

Pinned deterministically (no audio, no network):

1. Ownership: the prune triggered by a worker-thread publish runs on the
   asyncio event loop, never in the worker thread.
2. Race closed: a /api/play-style snapshot/index/access sequence racing a
   worker-thread publish no longer raises IndexError.
3. Atomicity: loop readers only ever observe pre- or post-prune snapshots,
   never new tracks with a stale index/mode/loop.
4. Success: a rescan still removes retired ids (end to end through the
   real scanner, worker thread and main hook); current_track_info stays
   untouched; a reconcile error never fails the scan.
5. Failure: a failed scan publishes nothing and prunes nothing.
6. Shutdown/switch: invalidating drops a posted-but-unrun reconcile, so no
   stale state is committed afterwards; later reconciles still apply.
"""

import asyncio
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_BASE = Path(tempfile.mkdtemp(prefix="fxroute-test-scan-reconcile-"))
os.environ["MUSIC_ROOT"] = str(_BASE / "music")
os.environ["XDG_CONFIG_HOME"] = str(_BASE / "config")
os.environ["XDG_STATE_HOME"] = str(_BASE / "state")
os.environ["XDG_DATA_HOME"] = str(_BASE / "data")
os.environ["LOG_LEVEL"] = "WARNING"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import playback.queue as playback_queue
import main
from library.core import LibraryScanner
from library.metadata import LibraryMetadataStore

logging.getLogger().setLevel(logging.WARNING)

GARBAGE_AUDIO = b"\x00garbage-not-audio\x00" * 64


def _no_network(*args, **kwargs):
    raise RuntimeError("network disabled in tests")


def _track(name: str) -> dict:
    return {
        "id": f"local_{name}.mp3",
        "source": "local",
        "url": f"/music/{name}.mp3",
        "title": name,
        "sample_rate_hz": 44100,
    }


async def _settle(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


class ScanReconcileBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        network = patch.object(LibraryMetadataStore, "_request_json", _no_network)
        network.start()
        self.addCleanup(network.stop)
        queue = playback_queue.queue
        self._queue_fields = (
            [dict(t) for t in queue.tracks],
            [dict(t) for t in queue.original],
            queue.index,
            queue.mode,
            queue.loop,
            queue.shuffle,
            queue.single_track_loop,
            queue.prune_removed_tracks,
        )
        self._owner_loop = main._scan_queue_owner_loop
        self._generation = main._queue_reconcile_generation
        self._current_track_info = main.playback_state.current_track_info
        self.addCleanup(self._restore)

    def _restore(self):
        queue = playback_queue.queue
        (tracks, original, index, mode, loop, shuffle, single, prune) = self._queue_fields
        queue.tracks = tracks
        queue.original = original
        queue.index = index
        queue.mode = mode
        queue.loop = loop
        queue.shuffle = shuffle
        queue.single_track_loop = single
        queue.prune_removed_tracks = prune
        main._scan_queue_owner_loop = self._owner_loop
        main._queue_reconcile_generation = self._generation
        main.playback_state.current_track_info = self._current_track_info

    def _seed_queue(self, names=("a", "b", "c"), index=2):
        queue = playback_queue.queue
        queue.tracks = [_track(name) for name in names]
        queue.original = [dict(item) for item in queue.tracks]
        queue.index = index
        queue.mode = "app_replace"
        queue.loop = False
        queue.shuffle = False
        queue.single_track_loop = False
        return queue

    def _scanner(self, root: Path, name: str) -> LibraryScanner:
        return LibraryScanner(
            root,
            metadata_store=LibraryMetadataStore(
                Path(self._tmp.name) / f"{name}.sqlite",
                Path(self._tmp.name) / f"{name}-covers",
            ),
        )


class QueueReconcileOwnershipTests(ScanReconcileBase):
    def test_worker_publish_prunes_on_event_loop_only(self):
        async def scenario():
            queue = self._seed_queue()
            main._scan_queue_owner_loop = asyncio.get_running_loop()
            loop_ident = threading.get_ident()
            seen: dict = {}
            real_prune = queue.prune_removed_tracks

            def spy(valid_ids):
                seen["prune_ident"] = threading.get_ident()
                return real_prune(valid_ids)

            queue.prune_removed_tracks = spy
            worker_ident: dict = {}

            def worker():
                worker_ident["id"] = threading.get_ident()
                main._prune_queue_after_scan(["local_a.mp3", "local_b.mp3"])

            await asyncio.to_thread(worker)
            settled = await _settle(lambda: len(queue.tracks) == 2)
            self.assertTrue(settled, "the loop reconcile did not run")
            self.assertEqual(seen.get("prune_ident"), loop_ident)
            self.assertNotEqual(seen.get("prune_ident"), worker_ident.get("id"))
            self.assertEqual([t["id"] for t in queue.tracks], ["local_a.mp3", "local_b.mp3"])
            self.assertTrue(0 <= queue.index < len(queue.tracks))

        asyncio.run(scenario())

    def test_play_read_racing_worker_publish_has_no_index_error(self):
        async def scenario():
            queue = self._seed_queue()
            main._scan_queue_owner_loop = asyncio.get_running_loop()
            # /api/play native path: snapshot ids, derive target_index ...
            ids_snapshot = [item.get("id") for item in queue.tracks]
            target_index = ids_snapshot.index("local_c.mp3")
            # ... scan worker publishes concurrently ...
            worker_done = threading.Event()

            def worker():
                main._prune_queue_after_scan(["local_a.mp3", "local_b.mp3"])
                worker_done.set()

            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(worker_done.wait(5), "scan worker did not finish")
            thread.join(5)
            # ... reader indexes with the previously derived target_index.
            try:
                info = dict(queue.tracks[target_index])
            except IndexError:
                self.fail("worker-thread publish must not shorten the queue under a loop reader")
            self.assertEqual(info["id"], "local_c.mp3")
            settled = await _settle(lambda: len(queue.tracks) == 2)
            self.assertTrue(settled, "the loop reconcile did not run")
            self.assertEqual([t["id"] for t in queue.tracks], ["local_a.mp3", "local_b.mp3"])
            self.assertTrue(0 <= queue.index < len(queue.tracks))

        asyncio.run(scenario())

    def test_readers_only_see_pre_or_post_prune_snapshots(self):
        async def scenario():
            queue = self._seed_queue()
            main._scan_queue_owner_loop = asyncio.get_running_loop()
            snapshots: list = []

            def read():
                snapshots.append((
                    tuple(item.get("id") for item in queue.tracks),
                    queue.index,
                    queue.mode,
                    queue.loop,
                    queue.shuffle,
                ))

            readers = [asyncio.ensure_future(asyncio.to_thread(lambda: None)) for _ in range(4)]
            await asyncio.gather(*readers)
            read()
            await asyncio.to_thread(main._prune_queue_after_scan, ["local_a.mp3", "local_b.mp3"])
            for _ in range(8):
                read()
                await asyncio.sleep(0)
            settled = await _settle(lambda: len(queue.tracks) == 2)
            self.assertTrue(settled, "the loop reconcile did not run")
            read()
            for ids, index, mode, loop, shuffle in snapshots:
                if ids == ("local_a.mp3", "local_b.mp3", "local_c.mp3"):
                    self.assertEqual((index, mode, loop, shuffle), (2, "app_replace", False, False))
                elif ids == ("local_a.mp3", "local_b.mp3"):
                    self.assertEqual((mode, loop, shuffle), ("app_replace", False, False))
                    self.assertTrue(0 <= index < 2, (ids, index))
                else:
                    self.fail(f"torn queue snapshot observed: {(ids, index, mode, loop, shuffle)}")

        asyncio.run(scenario())


class QueueReconcileLifecycleTests(ScanReconcileBase):
    def test_successful_rescan_prunes_retired_ids_end_to_end(self):
        async def scenario():
            root = Path(self._tmp.name) / "music"
            root.mkdir(parents=True)
            for name in ("a.mp3", "b.mp3", "c.mp3"):
                (root / name).write_bytes(GARBAGE_AUDIO)
            scanner = main._library_scanner_for(root)
            try:
                self.assertIs(scanner._on_scan_published, main._prune_queue_after_scan)
                await asyncio.to_thread(scanner.refresh, True)
                self.assertIsNone(scanner.error)
                live = {track.id for track in scanner.get_tracks(refresh=False)}
                self.assertEqual(live, {"local_a.mp3", "local_b.mp3", "local_c.mp3"})
                queue = self._seed_queue()
                main.playback_state.current_track_info = {"id": "local_b.mp3", "source": "local"}
                (root / "b.mp3").unlink()
                await asyncio.to_thread(scanner.refresh, True)
                self.assertIsNone(scanner.error, "the rescan itself succeeded")
                settled = await _settle(
                    lambda: [t["id"] for t in queue.tracks] == ["local_a.mp3", "local_c.mp3"]
                )
                self.assertTrue(settled, "retired id was not pruned from the queue")
                self.assertTrue(0 <= queue.index < len(queue.tracks))
                self.assertEqual(
                    main.playback_state.current_track_info,
                    {"id": "local_b.mp3", "source": "local"},
                    "current_track_info must stay untouched by the prune",
                )
            finally:
                scanner.set_scan_published_hook(None)

        asyncio.run(scenario())

    def test_reconcile_error_never_fails_the_scan(self):
        async def scenario():
            root = Path(self._tmp.name) / "music-failing-prune"
            root.mkdir(parents=True)
            for name in ("a.mp3", "b.mp3"):
                (root / name).write_bytes(GARBAGE_AUDIO)
            scanner = main._library_scanner_for(root)
            try:
                queue = playback_queue.queue
                queue.tracks = [_track("a"), _track("b")]
                queue.original = [dict(item) for item in queue.tracks]
                queue.index = 0

                def broken(_valid_ids):
                    raise RuntimeError("prune exploded")

                queue.prune_removed_tracks = broken
                (root / "b.mp3").unlink()
                await asyncio.to_thread(scanner.refresh, True)
                self.assertIsNone(scanner.error, "a reconcile error must not fail the scan")
                self.assertEqual(
                    {track.id for track in scanner.get_tracks(refresh=False)},
                    {"local_a.mp3"},
                )
            finally:
                scanner.set_scan_published_hook(None)

        asyncio.run(scenario())

    def test_failed_scan_prunes_nothing(self):
        async def scenario():
            missing = Path(self._tmp.name) / "no-such-root"
            scanner = main._library_scanner_for(missing)
            try:
                queue = self._seed_queue()
                before = ([dict(t) for t in queue.tracks], queue.index, queue.mode)
                await asyncio.to_thread(scanner.refresh, True)
                self.assertIsNotNone(scanner.error)
                await asyncio.sleep(0.3)
                self.assertEqual([dict(t) for t in queue.tracks], before[0])
                self.assertEqual((queue.index, queue.mode), (before[1], before[2]))
            finally:
                scanner.set_scan_published_hook(None)

        asyncio.run(scenario())

    def test_invalidated_reconcile_commits_nothing_stale(self):
        async def scenario():
            queue = self._seed_queue()
            main._scan_queue_owner_loop = asyncio.get_running_loop()
            # Post from a worker thread, then invalidate before the owning
            # loop runs the reconcile. No `await` may happen between the
            # post and the invalidate: awaiting would let the loop run the
            # posted reconcile first (FIFO ready queue), making the
            # invalidate a silent no-op. The event only signals that the
            # worker posted; it never yields to the loop, so the reconcile
            # stays posted-but-unrun until the invalidate lands.
            posted = threading.Event()

            def worker():
                main._prune_queue_after_scan(["local_a.mp3"])
                posted.set()

            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(posted.wait(5), "scan worker did not post the reconcile")
            # Shutdown/switch drops the posted-but-unrun reconcile ...
            main._invalidate_pending_queue_reconcile()
            thread.join(5)
            await asyncio.sleep(0.3)
            self.assertEqual([t["id"] for t in queue.tracks], ["local_a.mp3", "local_b.mp3", "local_c.mp3"])
            self.assertEqual(queue.index, 2)
            # ... while a reconcile posted afterwards still applies.
            await asyncio.to_thread(main._prune_queue_after_scan, ["local_a.mp3"])
            settled = await _settle(lambda: len(queue.tracks) == 1)
            self.assertTrue(settled, "post-invalidation reconcile did not run")
            self.assertEqual([t["id"] for t in queue.tracks], ["local_a.mp3"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
