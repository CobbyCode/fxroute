# SPDX-License-Identifier: AGPL-3.0-only
"""Album Discover must use the library worker contract, not block ASGI."""
import asyncio
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from common.run_to_completion import run_to_completion
from library import api
from library import metadata
from library.core import LibraryScanner


class LibraryDiscoverOffloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scanner = SimpleNamespace(get_album_tracks=lambda *a, **kw: [object()])
        self.worker_calls = []

        async def run_blocking(function, *args, **kwargs):
            self.worker_calls.append(function)
            return await run_to_completion(asyncio.to_thread(function, *args, **kwargs))

        self.runner = run_blocking
        self.runtime = api.LibraryApiRuntime(
            get_scanner=lambda: self.scanner, get_settings=lambda: None,
            run_blocking=self.runner,
        )
        patcher = mock.patch.object(api, "_runtime", self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def check_liveness(self, runner):
        entered = asyncio.Event()
        loop_ran = threading.Event()
        loop = asyncio.get_running_loop()
        progress_during_discover = []

        def discover(album_id, force=False):
            loop.call_soon_threadsafe(entered.set)
            progress_during_discover.append(loop_ran.wait(2))
            return {"items": [{"name": "Artist"}], "source": "artist", "cached": False}

        self.scanner.get_album_discover = discover

        async def other_request():
            await entered.wait()
            loop_ran.set()

        task = asyncio.create_task(other_request())
        try:
            with mock.patch.object(api, "_runtime", api.LibraryApiRuntime(
                get_scanner=lambda: self.scanner, get_settings=lambda: None,
                run_blocking=runner,
            )):
                result = await api.get_album_discover("album")
            await asyncio.wait_for(task, 2)
            self.assertEqual(progress_during_discover, [True], "Discover blocked the event loop")
            self.assertEqual(result["items"], [{"name": "Artist"}])
        finally:
            loop_ran.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_discover_keeps_loop_live_with_runtime_runner(self):
        await self.check_liveness(self.runner)
        self.assertIn(self.scanner.get_album_discover, self.worker_calls)

    async def test_discover_keeps_loop_live_with_fallback_runner(self):
        await self.check_liveness(None)

    async def test_force_provider_mapping_and_response_contract_are_preserved(self):
        calls = []

        def tracks(album_id, authoritative=False):
            calls.append(("tracks", album_id, authoritative))
            return [object()]

        def discover(album_id, force=False):
            calls.append(("discover", album_id, force))
            return {"items": [{"name": "Artist"}], "source": "artist", "cached": True, "error": "offline"}

        def attach(provider, items):
            calls.append(("mapping", provider))
            return [{**items[0], "provider_id": "42"}]

        self.scanner.get_album_tracks = tracks
        self.scanner.get_album_discover = discover
        self.scanner.metadata_store = SimpleNamespace(artist_enrichment=SimpleNamespace(attach_provider_mappings=attach))
        result = await api.get_album_discover("album", refresh=True, provider=" TIDAL ")
        self.assertEqual(calls, [("tracks", "album", True), ("discover", "album", True), ("mapping", "tidal")])
        self.assertEqual(result, {"album_id": "album", "items": [{"name": "Artist", "provider_id": "42"}],
                                  "source": "artist", "cached": True, "error": "offline"})
        self.assertEqual(self.worker_calls, [tracks, discover, attach])

    async def test_unknown_album_stops_before_discover(self):
        self.scanner.get_album_tracks = lambda *a, **kw: []
        with self.assertRaises(HTTPException) as caught:
            await api.get_album_discover("absent")
        self.assertEqual(caught.exception.status_code, 404)

    async def test_unavailable_library_preserves_503(self):
        self.scanner = None
        with self.assertRaises(HTTPException) as caught:
            await api.get_album_discover("album")
        self.assertEqual(caught.exception.status_code, 503)

    async def test_discover_error_is_not_swallowed(self):
        def fail(*a, **kw):
            raise RuntimeError("discover failed")

        self.scanner.get_album_discover = fail
        with self.assertRaisesRegex(RuntimeError, "discover failed"):
            await api.get_album_discover("album")

    async def test_cancel_drains_discover_worker_before_propagating(self):
        entered = asyncio.Event()
        release = threading.Event()
        completed = threading.Event()
        loop = asyncio.get_running_loop()

        def discover(*a, **kw):
            loop.call_soon_threadsafe(entered.set)
            release.wait(2)
            completed.set()
            return {"items": []}

        self.scanner.get_album_discover = discover
        task = asyncio.create_task(api.get_album_discover("album"))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            self.assertFalse(completed.is_set(), "Discover blocked until the worker timeout")
            task.cancel()
            turn = asyncio.Event()
            loop.call_soon(turn.set)
            await turn.wait()
            self.assertFalse(task.done(), "cancel released the caller before the worker completed")
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertTrue(completed.is_set())
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


class ConcurrentDiscoverPacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_discover_workers_keep_request_spacing_and_cache_results(self):
        # A virtual sleep joins concurrent waiters at the same deadline. A
        # serialized waiter instead times out at the barrier, then the next
        # waiter sees the updated clock and gets its own deadline. No network
        # latency or short real-time spacing threshold determines the result.
        barrier = threading.Barrier(2)
        first_request = threading.Event()
        clock_lock = threading.Lock()
        now = 100.0
        waits = []
        requests = []

        def monotonic():
            with clock_lock:
                return now

        def sleep(seconds):
            nonlocal now
            with clock_lock:
                deadline = now + seconds
                index = len(waits)
                waits.append(seconds)
            try:
                barrier.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass
            if index == 1 and not first_request.wait(2):
                raise AssertionError("first request never reached HTTP boundary")
            with clock_lock:
                now = max(now, deadline)

        def safe_get(url, **kwargs):
            with clock_lock:
                requests.append((url, now))
            first_request.set()
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                                   json=lambda: {"tracks": [{"similar_artist_mbid": "similar", "similar_artist_name": "Similar"}]})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = metadata.LibraryMetadataStore(db_path=root / "metadata.sqlite", cover_dir=root / "covers")
            with store._connect() as conn:
                conn.executemany("INSERT INTO albums (album_key, album, artist, mb_artist_id) VALUES (?, ?, ?, ?)",
                                 [("a1", "One", "First", "mb-1"), ("a2", "Two", "Second", "mb-2")])
            store._last_request_at = 100.0
            with mock.patch("library.core.get_settings", return_value=SimpleNamespace(MUSIC_ROOT=root)):
                scanner = LibraryScanner(music_root=root, metadata_store=store)
            runtime = api.LibraryApiRuntime(get_scanner=lambda: scanner, get_settings=lambda: None)
            with mock.patch.object(api, "_runtime", runtime), \
                    mock.patch.object(scanner, "get_album_tracks", return_value=[object()]), \
                    mock.patch.object(metadata, "time", SimpleNamespace(monotonic=monotonic, sleep=sleep, time=time.time)), \
                    mock.patch.object(metadata, "safe_get", side_effect=safe_get):
                results = await asyncio.gather(api.get_album_discover("a1", refresh=True),
                                               api.get_album_discover("a2", refresh=True))
                self.assertEqual(len(requests), 2)
                self.assertAlmostEqual(requests[0][1], 101.1)
                self.assertAlmostEqual(requests[1][1], 102.2, msg="parallel workers bypassed the shared request pacing")
                self.assertEqual([result["items"][0]["artist"] for result in results], ["Similar", "Similar"])
                cached = await asyncio.gather(api.get_album_discover("a1"), api.get_album_discover("a2"))
                self.assertTrue(all(result["cached"] for result in cached))
                self.assertEqual(len(requests), 2, "both album cache writes must survive concurrency")


if __name__ == "__main__":
    unittest.main()
