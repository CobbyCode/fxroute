#!/usr/bin/env python3
"""A stalled network cover lookup must not stall status or the event loop."""

import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BASE = Path(tempfile.mkdtemp(prefix="fxroute-network-artwork-"))
os.environ["MUSIC_ROOT"] = str(BASE)
os.environ["XDG_CONFIG_HOME"] = str(BASE / "config")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from fastapi import HTTPException
from library import api
from library.sources import MusicLibraryManager
from models import Track


class NetworkArtworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_artwork_saturation_does_not_use_control_worker_pool(self):
        from library.network_artwork import ArtworkWorkers
        release = threading.Event()
        with ThreadPoolExecutor(max_workers=2) as executor:
            workers = ArtworkWorkers(executor, max_pending=2)
            def tracks(**kwargs):
                release.wait(timeout=2)
                return []
            scanner = SimpleNamespace(get_tracks=tracks)
            runtime = api.LibraryApiRuntime(get_scanner=lambda: scanner, get_settings=lambda: SimpleNamespace(MUSIC_ROOT=BASE))
            with patch.object(api, "artwork_workers", workers), patch.object(api, "_runtime", runtime):
                jobs = [asyncio.create_task(api.get_track_cover_info("track")) for _ in range(2)]
                try:
                    await asyncio.sleep(.03)
                    with self.assertRaises(HTTPException) as caught:
                        await api.get_track_cover_info("track")
                    self.assertEqual(caught.exception.status_code, 503)
                    self.assertEqual(await asyncio.wait_for(asyncio.to_thread(lambda: "control ready"), .2), "control ready")
                finally:
                    release.set()
                    await asyncio.gather(*jobs)

    async def test_cancelled_probes_keep_worker_slots_until_real_completion(self):
        from library.network_artwork import NetworkCoverCache
        cache = NetworkCoverCache()
        release = threading.Event()
        calls = []
        tasks = []

        def probe():
            calls.append(threading.get_ident())
            release.wait(timeout=2)
            return True

        def schedule(coro, *, name):
            tasks.append(asyncio.create_task(coro, name=name))

        try:
            cache.get("first", probe, schedule)
            cache.get("second", probe, schedule)
            await asyncio.sleep(.03)
            for task in tasks:
                task.cancel()
            await asyncio.sleep(.03)
            cache.get("third", probe, schedule)
            cache.get("fourth", probe, schedule)
            await asyncio.sleep(.03)
            self.assertEqual(len(calls), 2, "cancelled wrappers must not free live worker accounting")
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_first_cover_probe_is_scheduled_during_early_boot(self):
        from library.network_artwork import NetworkCoverCache
        cache = NetworkCoverCache()
        tasks = []

        def schedule(coro, *, name):
            tasks.append(asyncio.create_task(coro, name=name))

        with patch("library.network_artwork.time.monotonic", return_value=1.0):
            self.assertFalse(cache.get("early-boot", lambda: True, schedule))
        await asyncio.gather(*tasks)
        self.assertEqual(len(tasks), 1)

    async def test_network_cover_probes_are_single_flight_and_bounded(self):
        from library.network_artwork import NetworkCoverCache
        cache = NetworkCoverCache()
        release = threading.Event()
        calls = []
        tasks = []

        def probe():
            calls.append(threading.get_ident())
            release.wait(timeout=2)
            return True

        def schedule(coro, *, name):
            tasks.append(asyncio.create_task(coro, name=name))

        try:
            for key in range(20):
                self.assertFalse(cache.get(key, probe, schedule))
                self.assertFalse(cache.get(key, probe, schedule))
            await asyncio.sleep(.03)
            self.assertEqual(len(tasks), 2)
            self.assertEqual(len(calls), 2)
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertTrue(cache.get(0, probe, schedule))
        self.assertTrue(cache.get(1, probe, schedule))
        self.assertEqual(len(tasks), 2)

    async def test_status_never_probes_network_covers_on_the_event_loop(self):
        manager = MusicLibraryManager(BASE, discovery_hosts=[])
        entry = manager.add_manual_url("nas:/music")
        with patch.object(manager, "_mounted_nfs_path", return_value=BASE):
            manager.activate(entry["id"])
        worker_threads = []

        def probe(track_id):
            worker_threads.append(threading.get_ident())
            time.sleep(.15)
            return True

        with patch.object(main.runtime.music_library, "manager", manager), patch.object(
            main.runtime.music_library, "scanner", SimpleNamespace()
        ), patch.object(main, "_track_cover_available", side_effect=probe):
            started = time.monotonic()
            result = main._playback_track_with_artwork_fields({"source": "local", "id": "local_track"})
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, .08, "status must never wait for network cover I/O")
            self.assertFalse(result["cover_available"])
            tasks = list(main.runtime.lifecycle_background_tasks)
            if tasks:
                await asyncio.gather(*tasks)
            result = main._playback_track_with_artwork_fields({"source": "local", "id": "local_track"})
            self.assertTrue(result["cover_available"])
            self.assertEqual(len(worker_threads), 1)
            self.assertNotEqual(worker_threads[0], threading.get_ident())

    async def test_cover_endpoints_keep_event_loop_live_during_remote_io(self):
        track_path = BASE / "track.mp3"
        track_path.write_bytes(b"fixture")
        track = Track(id="local_track", title="Track", path=track_path)
        scanner = SimpleNamespace(music_root=BASE, get_tracks=lambda **kwargs: [track],
                                  get_album_tracks=lambda *args, **kwargs: [track],
                                  get_album_external_cover=lambda *args: None)
        runtime = api.LibraryApiRuntime(get_scanner=lambda: scanner, get_settings=lambda: SimpleNamespace(MUSIC_ROOT=BASE))
        for endpoint, identifier in ((api.get_track_cover_info, "local_track"),
                                     (api.get_track_cover, "local_track"), (api.get_album_cover, "album")):
            gaps = []
            finished = asyncio.Event()

            async def heartbeat():
                previous = time.monotonic()
                while not finished.is_set():
                    await asyncio.sleep(.01)
                    now = time.monotonic()
                    gaps.append(now - previous)
                    previous = now

            def slow_folder(*args):
                time.sleep(.15)
                return None

            async def request():
                try:
                    await endpoint(identifier)
                except HTTPException as exc:
                    self.assertEqual(exc.status_code, 404)
                finally:
                    finished.set()

            with self.subTest(endpoint=endpoint.__name__), patch.object(api, "_runtime", runtime), patch.object(
                api, "_folder_cover_for_track", side_effect=slow_folder
            ), patch.object(api, "_cached_embedded_cover", return_value=(None, None)):
                await asyncio.gather(heartbeat(), request())
                self.assertLess(max(gaps), .08, "cover resolution must run outside the event loop")


class CoverPublicationTests(unittest.TestCase):
    def test_concurrent_embedded_covers_publish_without_tempfile_collision(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            track = root / "track.mp3"
            track.write_bytes(b"audio")
            barrier = threading.Barrier(2)
            original_replace = Path.replace

            def replace(path, target):
                barrier.wait(timeout=3)
                return original_replace(path, target)

            with patch.object(api, "COVER_CACHE_DIR", root / "covers"), patch.object(
                api, "_embedded_cover_bytes", return_value=(b"art", "image/jpeg")
            ), patch.object(Path, "replace", replace), ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [pool.submit(api._cached_embedded_cover, "track", track) for _ in range(2)]
                paths = [job.result(timeout=5)[0] for job in jobs]
            self.assertEqual(paths[0], paths[1])
            self.assertEqual(paths[0].read_bytes(), b"art")
            self.assertEqual(len(list((root / "covers").iterdir())), 1)

    def test_concurrent_thumbnails_publish_without_tempfile_collision(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            image = root / "cover.jpg"
            Image.new("RGB", (8, 8)).save(image)
            barrier = threading.Barrier(2)
            original_replace = Path.replace

            def replace(path, target):
                barrier.wait(timeout=3)
                return original_replace(path, target)

            with patch.object(api, "ALBUM_COVER_CACHE_DIR", root / "covers"), patch.object(
                Path, "replace", replace
            ), ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [pool.submit(api._serve_cover_image, image) for _ in range(2)]
                responses = [job.result(timeout=5) for job in jobs]
            self.assertEqual(responses[0].path, responses[1].path)
            self.assertEqual(len(list((root / "covers").iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
