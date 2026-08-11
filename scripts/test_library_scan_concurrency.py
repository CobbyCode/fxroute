#!/usr/bin/env python3
"""P1-5 regression tests: single-scan ownership + library scans off the event loop.

Deterministic tests (no real audio, no network) proving:

  A.  Event-loop liveness: real API force/mandatory endpoints keep a 10 ms
      asyncio ticker running while the full scan happens in a worker.
  B.  Single scan: a mandatory force scan behind a running scan never enters
      the scan body concurrently (max parallel scan depth == 1).
  C.  Force waits and rescans: a mandatory scan waits for the running scan
      and then rescans, so a file added meanwhile is included; no stale
      publish by the older scan.
  D.  Status lifecycle: scanning stays True while a mandatory scan waits
      behind a running scan and while it runs; only False after it ends.
  E.  Passive semantics: get_tracks(refresh=False) neither waits for nor
      triggers a scan while one is running.
  F.  Manual refresh: with a scan already running, no second scan is queued.
  G.  Cancellation: cancelling the API caller does not let another scan in
      before the real worker finished; ownership is released afterwards.
  H.  Shutdown: cancel_refresh() ends a scan in a controlled way and
      releases ownership (also for a queued mandatory worker).
  I.  Structural guard: no async route may call scan-capable library
      methods (refresh/get_tracks/get_albums/get_album_tracks) directly.

Scans run against throwaway files in a temp directory; the real sync
building blocks (LibraryScanner._create_track_from_file, os.walk within
library.py) are patched with unittest.mock and restored afterwards.
"""

import ast
import asyncio
import base64
import io
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch
import unittest

BASE_DIR = Path(tempfile.mkdtemp(prefix="fxroute-test-scan-"))
MUSIC_ROOT = BASE_DIR / "music"
CONFIG_DIR = BASE_DIR / "config"

os.environ["MUSIC_ROOT"] = str(MUSIC_ROOT)
os.environ["XDG_CONFIG_HOME"] = str(CONFIG_DIR)
os.environ["LOG_LEVEL"] = "WARNING"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as config_mod
import library as library_mod
import library_api
import main
from fastapi import UploadFile
from library import LibraryScanner
from library_metadata import LibraryMetadataStore
from models import DeleteTracksRequest

from library_api import LibraryApiRuntime, configure_runtime

logging.getLogger().setLevel(logging.WARNING)


def _clear_track_cache(scanner: LibraryScanner) -> None:
    with scanner.metadata_store._connect() as conn:
        conn.execute("DELETE FROM tracks")


# ---------------------------------------------------------------------------
# Instrumentation (patches of real sync scan building blocks)
# ---------------------------------------------------------------------------

class Ticker:
    """10 ms asyncio ticker; records monotonic timestamps while it runs."""

    def __init__(self):
        self.ticks: list[float] = []
        self._task = None

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            self.ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    async def stop(self):
        self.ticks.append(time.monotonic())
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task

    def max_gap(self) -> float:
        return max((b - a for a, b in zip(self.ticks, self.ticks[1:])), default=0.0)


def slow_patch(original, seconds: float):
    """Wrap a real sync scan building block with a controlled blocking delay."""

    def wrapper(self_obj, filepath):
        time.sleep(seconds)
        return original(self_obj, filepath)

    return wrapper


class FirstCallGate:
    """Pause the first per-thread call inside the real per-file processing loop.

    Each queued event is assigned to the next thread that makes its first
    call; that call blocks on the event.  Used to deterministically hold a
    scan mid-traversal at the real sync building block.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pending_events: list[threading.Event] = []
        self._assigned = {}
        self._calls = {}
        self.active = 0
        self.max_active = 0

    def add_block(self, event: threading.Event) -> None:
        with self._lock:
            self._pending_events.append(event)

    def make_patch(self, original):
        def wrapper(self_obj, filepath):
            with self._lock:
                tid = threading.current_thread().ident
                idx = self._calls.get(tid, 0)
                self._calls[tid] = idx + 1
                event = None
                if idx == 0:
                    event = self._assigned.get(tid)
                    if event is None and self._pending_events:
                        event = self._pending_events.pop(0)
                        self._assigned[tid] = event
                self.active += 1
                if self.active > self.max_active:
                    self.max_active = self.active
            try:
                if event is not None:
                    if not event.wait(30):
                        raise RuntimeError("gate timed out waiting for release")
                return original(self_obj, filepath)
            finally:
                with self._lock:
                    self.active -= 1

        return wrapper


class WalkSnapshots:
    """Snapshot of os.walk results, scoped to library.py.

    Each os.walk call (one per scan body) captures the directory listing
    once, at the moment that scan starts; later filesystem changes are not
    visible to that scan.  Makes the stale-publish race deterministic and
    is independent of which executor thread the scan runs on.
    """

    def __init__(self, real_walk):
        self._real_walk = real_walk

    def __call__(self, root, *args, **kwargs):
        snapshot = [entry for entry in self._real_walk(MUSIC_ROOT)]
        for entry in snapshot:
            yield entry


class OsProxy:
    """Shadow os inside library.py so only library.py's os.walk is patched."""

    def __init__(self, real_os, snapshots):
        self._real_os = real_os
        self._snapshots = snapshots

    def __getattr__(self, name):
        if name == "walk":
            return self._snapshots
        return getattr(self._real_os, name)


def _no_network(*args, **kwargs):
    raise RuntimeError("network disabled in tests")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class LibraryScanConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        album_dir = MUSIC_ROOT / "AlbumA"
        album_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, 7):
            (album_dir / f"track{i:02d}.mp3").write_bytes(b"\x00garbage-not-audio\x00" * 64)
        (album_dir / "cover.jpg").write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
        ))

    def setUp(self):
        (MUSIC_ROOT / "AlbumA" / "track07.mp3").unlink(missing_ok=True)
        shutil.rmtree(MUSIC_ROOT / "incoming", ignore_errors=True)
        self.scanner = LibraryScanner()
        _clear_track_cache(self.scanner)
        configure_runtime(LibraryApiRuntime(
            get_scanner=lambda: self.scanner,
            get_settings=lambda: config_mod.get_settings(),
            run_blocking=main._drain_worker,
        ))
        main.library_scanner = self.scanner
        network_patch = patch.object(LibraryMetadataStore, "_request_json", _no_network)
        network_patch.start()
        self.addCleanup(network_patch.stop)

    async def _wait_until(self, predicate, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError("condition not reached within timeout")
            await asyncio.sleep(0.01)
    # ── A. Event-loop liveness ───────────────────────────────────────────

    async def test_a_event_loop_liveness_delete_tracks(self):
        original = LibraryScanner._create_track_from_file
        with patch.object(LibraryScanner, "_create_track_from_file", slow_patch(original, 0.3)):
            ticker = Ticker()
            await ticker.start()
            try:
                resp = await library_api.delete_tracks(DeleteTracksRequest(track_ids=["nonexistent"]))
            finally:
                await ticker.stop()
        self.assertEqual(resp["status"], "ok")
        self.assertGreater(len(ticker.ticks), 10, "ticker must keep running while the scan is offloaded")
        self.assertLess(ticker.max_gap(), 0.1, "event loop must stay responsive during the scan")
        self.assertFalse(self.scanner.scanning)

    async def test_a2_event_loop_liveness_upload(self):
        original = LibraryScanner._create_track_from_file
        with patch.object(LibraryScanner, "_create_track_from_file", slow_patch(original, 0.3)):
            ticker = Ticker()
            await ticker.start()
            try:
                file = UploadFile(filename="new-track.mp3", file=io.BytesIO(b"\x00garbage\x00" * 32))
                resp = await library_api.upload_track(file)
            finally:
                await ticker.stop()
        self.assertEqual(resp["status"], "uploaded")
        self.assertEqual(resp["track_count"], 7)
        self.assertGreater(len(ticker.ticks), 10, "ticker must keep running while the upload scan is offloaded")
        self.assertLess(ticker.max_gap(), 0.1, "event loop must stay responsive during the upload scan")
        self.assertFalse(self.scanner.scanning)

    async def test_a3_event_loop_liveness_track_download(self):
        await main._drain_worker(self.scanner.refresh, True)
        self.scanner._track_cache = []
        _clear_track_cache(self.scanner)
        original = LibraryScanner._create_track_from_file
        with patch.object(LibraryScanner, "_create_track_from_file", slow_patch(original, 0.3)):
            ticker = Ticker()
            await ticker.start()
            try:
                resp = await library_api.download_track_file("local_AlbumA/track01.mp3")
            finally:
                await ticker.stop()
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(len(ticker.ticks), 10, "ticker must keep running while the download scan is offloaded")
        self.assertLess(ticker.max_gap(), 0.1, "event loop must stay responsive during the download scan")
        self.assertFalse(self.scanner.scanning)

    # ── B. Single scan ───────────────────────────────────────────────────

    async def test_b_parallel_force_scan_is_serialized(self):
        gate = FirstCallGate()
        ev_a = threading.Event()
        gate.add_block(ev_a)
        original = LibraryScanner._create_track_from_file
        body_count = {"n": 0}
        real_run = LibraryScanner._run_scan_locked

        def counting_run(self_obj):
            body_count["n"] += 1
            return real_run(self_obj)

        with patch.object(LibraryScanner, "_create_track_from_file", gate.make_patch(original)), \
             patch.object(LibraryScanner, "_run_scan_locked", counting_run):
            a_task = asyncio.create_task(asyncio.to_thread(self.scanner.refresh, True), name="scan-A")
            await asyncio.sleep(0.3)
            self.assertTrue(self.scanner.scanning)
            b_task = asyncio.create_task(
                library_api.delete_tracks(DeleteTracksRequest(track_ids=["nonexistent"])),
                name="scan-B-api",
            )
            await asyncio.sleep(0.3)
            self.assertFalse(b_task.done(), "B's authoritative pre-lookup must wait for scan A")
            self.assertEqual(body_count["n"], 1, "B must not start a scan while A runs")
            self.assertEqual(gate.max_active, 1, "only scan A may be inside the scan body")
            ev_a.set()
            await a_task
            resp = await b_task
            self.assertEqual(resp["status"], "ok")
            self.assertEqual(body_count["n"], 2, "only A plus B's mandatory post-delete scan run")
        self.assertEqual(gate.max_active, 1, "scans must never run concurrently")
        self.assertFalse(self.scanner.scanning)

    # ── C. Force waits and rescans; no stale publish ─────────────────────

    async def test_c_force_waits_and_rescans_no_stale_publish(self):
        saved_os = library_mod.os
        snapshots = WalkSnapshots(saved_os.walk)
        library_mod.os = OsProxy(saved_os, snapshots)
        gate = FirstCallGate()
        ev_a = threading.Event()
        gate.add_block(ev_a)
        original = LibraryScanner._create_track_from_file
        timer = threading.Timer(25, ev_a.set)
        timer.daemon = True
        timer.start()
        try:
            with patch.object(LibraryScanner, "_create_track_from_file", slow_patch(gate.make_patch(original), 0.15)):
                a_task = asyncio.create_task(asyncio.to_thread(self.scanner.refresh, True), name="scan-A")
                await asyncio.sleep(0.4)
                (MUSIC_ROOT / "AlbumA" / "track07.mp3").write_bytes(b"\x00garbage-not-audio\x00" * 64)
                b_task = asyncio.create_task(
                    library_api.delete_tracks(DeleteTracksRequest(track_ids=["nonexistent"])),
                    name="scan-B-mandatory",
                )
                await asyncio.sleep(0.3)
                self.assertFalse(b_task.done(), "B's pre-lookup waits for scan A")
                self.assertTrue(self.scanner.scanning, "scanning True while B waits behind A")
                ev_a.set()
                await self._wait_until(lambda: self.scanner._scan_pending == 1)
                self.assertTrue(self.scanner.scanning, "scanning True while mandatory B runs after A")
                await b_task
                self.assertFalse(self.scanner.scanning, "scanning False only after B finished")
                await a_task
        finally:
            library_mod.os = saved_os
            (MUSIC_ROOT / "AlbumA" / "track07.mp3").unlink(missing_ok=True)
        final_cache = self.scanner._track_cache
        self.assertEqual(len(final_cache), 7, "final cache must contain the newer scan state")
        self.assertTrue(any(t.path and t.path.name == "track07.mp3" for t in final_cache))
        with self.scanner.metadata_store._connect() as conn:
            row = conn.execute(
                "SELECT missing_since FROM tracks WHERE rel_path = ?",
                ("AlbumA/track07.mp3",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["missing_since"], "newer track must not be marked missing by the older scan")
        self.assertEqual(gate.max_active, 1, "scans must never run concurrently")

    # ── D. Status lifecycle ──────────────────────────────────────────────

    async def test_d_status_lifecycle_mandatory_wait(self):
        gate = FirstCallGate()
        ev_a = threading.Event()
        gate.add_block(ev_a)
        original = LibraryScanner._create_track_from_file
        with patch.object(LibraryScanner, "_create_track_from_file", slow_patch(gate.make_patch(original), 0.15)):
            a_task = asyncio.create_task(asyncio.to_thread(self.scanner.refresh, True), name="scan-A")
            await asyncio.sleep(0.3)
            b_task = asyncio.create_task(
                main._drain_worker(self.scanner.refresh, True, wait_if_running=True),
                name="scan-B-mandatory",
            )
            await self._wait_until(lambda: self.scanner._scan_pending == 1)
            self.assertTrue(self.scanner.scanning, "scanning True while B waits behind A")
            ev_a.set()
            await a_task
            await self._wait_until(lambda: self.scanner._scan_in_progress)
            self.assertTrue(self.scanner.scanning, "scanning stays True while B runs after A")
            await b_task
            self.assertFalse(self.scanner.scanning, "scanning False only after B finished")

    # ── E. Passive semantics ─────────────────────────────────────────────

    async def test_e_passive_does_not_wait_or_scan(self):
        gate = FirstCallGate()
        ev_a = threading.Event()
        gate.add_block(ev_a)
        original = LibraryScanner._create_track_from_file
        with patch.object(LibraryScanner, "_create_track_from_file", gate.make_patch(original)):
            a_task = asyncio.create_task(asyncio.to_thread(self.scanner.refresh, True), name="scan-A")
            await asyncio.sleep(0.3)
            self.assertTrue(self.scanner.scanning)
            t0 = time.monotonic()
            tracks = await asyncio.to_thread(self.scanner.get_tracks, False)
            elapsed = time.monotonic() - t0
            self.assertEqual(tracks, [], "empty cache is returned while a scan runs")
            self.assertLess(elapsed, 1.0, "passive lookup must not wait for the running scan")
            self.assertEqual(gate.max_active, 1, "passive lookup must not start a second scan")
            ev_a.set()
            await a_task

        # Populated-cache variant: passive lookup returns the cache directly.
        await main._drain_worker(self.scanner.refresh, True)
        gate2 = FirstCallGate()
        ev_a2 = threading.Event()
        gate2.add_block(ev_a2)
        with patch.object(LibraryScanner, "_create_track_from_file", gate2.make_patch(original)):
            a2 = asyncio.create_task(asyncio.to_thread(self.scanner.refresh, True), name="scan-A2")
            await asyncio.sleep(0.3)
            t0 = time.monotonic()
            tracks = self.scanner.get_tracks(False)
            elapsed = time.monotonic() - t0
            self.assertEqual(len(tracks), 6)
            self.assertLess(elapsed, 1.0, "passive lookup with populated cache must not wait")
            ev_a2.set()
            await a2

    # ── F. Manual refresh ────────────────────────────────────────────────

    async def test_f_manual_refresh_no_redundant_scan(self):
        gate = FirstCallGate()
        ev_a = threading.Event()
        gate.add_block(ev_a)
        original = LibraryScanner._create_track_from_file
        body_count = {"n": 0}
        real_run = LibraryScanner._run_scan_locked

        def counting_run(self_obj):
            body_count["n"] += 1
            return real_run(self_obj)

        with patch.object(LibraryScanner, "_create_track_from_file", gate.make_patch(original)), \
             patch.object(LibraryScanner, "_run_scan_locked", counting_run):
            a_task = asyncio.create_task(asyncio.to_thread(self.scanner.refresh, True), name="scan-A")
            await asyncio.sleep(0.3)
            self.assertTrue(self.scanner.scanning)
            resp = await main.refresh_library()
            self.assertEqual(resp["status"], "scanning")
            self.assertEqual(body_count["n"], 1, "manual refresh must not queue a second full scan")
            ev_a.set()
            await a_task

        with patch.object(LibraryScanner, "_run_scan_locked", counting_run):
            resp = await main.refresh_library()
            self.assertEqual(resp["status"], "scanning")
            await self._wait_until(lambda: not self.scanner.scanning)
        self.assertEqual(body_count["n"], 2, "idle manual refresh still runs one scan")

    # ── G. Cancellation ──────────────────────────────────────────────────

    async def test_g_cancelled_caller_does_not_break_ownership(self):
        gate = FirstCallGate()
        ev_a = threading.Event()
        gate.add_block(ev_a)
        original = LibraryScanner._create_track_from_file
        body_gate = threading.Event()
        real_run = LibraryScanner._run_scan_locked
        calls = {"n": 0}

        def gated_run(self_obj):
            calls["n"] += 1
            if calls["n"] == 2:
                body_gate.wait(30)
            return real_run(self_obj)

        with patch.object(LibraryScanner, "_create_track_from_file", gate.make_patch(original)), \
             patch.object(LibraryScanner, "_run_scan_locked", gated_run):
            failsafe = threading.Timer(25, lambda: (ev_a.set(), body_gate.set()))
            failsafe.daemon = True
            failsafe.start()
            a_task = asyncio.create_task(asyncio.to_thread(self.scanner.refresh, True), name="scan-A")
            await asyncio.sleep(0.3)
            b_task = asyncio.create_task(
                library_api.delete_tracks(DeleteTracksRequest(track_ids=["nonexistent"])),
                name="scan-B-cancelled",
            )
            await asyncio.sleep(0.3)
            self.assertFalse(b_task.done(), "B's authoritative pre-lookup waits for scan A")
            ev_a.set()
            await self._wait_until(lambda: self.scanner._scan_pending == 1)
            await a_task
            b_task.cancel()
            await asyncio.sleep(0.2)
            self.assertTrue(self.scanner.scanning, "ownership must survive caller cancellation")
            self.assertEqual(self.scanner._scan_pending, 1)
            body_gate.set()
            with suppress(asyncio.CancelledError):
                await b_task
            self.assertTrue(b_task.cancelled())
            await self._wait_until(lambda: self.scanner._scan_pending == 0 and not self.scanner.scanning)
        self.assertFalse(self.scanner.scanning)
        self.assertEqual(calls["n"], 2, "only A plus B's mandatory post-delete scan run")
        self.assertTrue(self.scanner._scan_lock.acquire(blocking=False), "scan ownership lock must be free")
        self.scanner._scan_lock.release()

    # ── H. Shutdown ──────────────────────────────────────────────────────

    async def test_h_shutdown_cancel_releases_ownership(self):
        gate = FirstCallGate()
        ev_a = threading.Event()
        gate.add_block(ev_a)
        original = LibraryScanner._create_track_from_file
        with patch.object(LibraryScanner, "_create_track_from_file", gate.make_patch(original)):
            a_task = asyncio.create_task(asyncio.to_thread(self.scanner.refresh, True), name="scan-A")
            await asyncio.sleep(0.3)
            b_task = asyncio.create_task(
                main._drain_worker(self.scanner.refresh, True, wait_if_running=True),
                name="scan-B-shutdown",
            )
            await self._wait_until(lambda: self.scanner._scan_pending == 1)
            self.scanner.cancel_refresh()
            ev_a.set()
            await a_task
            await b_task
        self.assertFalse(self.scanner.scanning)
        self.assertEqual(self.scanner._scan_pending, 0)
        self.assertEqual(gate.max_active, 1, "queued worker must not open a parallel scan")
        self.assertTrue(self.scanner._scan_lock.acquire(blocking=False), "no lock leak after shutdown cancel")
        self.scanner._scan_lock.release()

    # ── J. Authoritative read: wait for the running scan, no second scan ──

    async def test_j_authoritative_download_waits_for_running_scan(self):
        gate = FirstCallGate()
        ev_a = threading.Event()
        gate.add_block(ev_a)
        original = LibraryScanner._create_track_from_file
        body_count = {"n": 0}
        real_run = LibraryScanner._run_scan_locked

        def counting_run(self_obj):
            body_count["n"] += 1
            return real_run(self_obj)

        with patch.object(LibraryScanner, "_create_track_from_file", gate.make_patch(original)), \
             patch.object(LibraryScanner, "_run_scan_locked", counting_run):
            a_task = asyncio.create_task(asyncio.to_thread(self.scanner.refresh, True), name="startup-scan")
            await asyncio.sleep(0.3)
            self.assertTrue(self.scanner.scanning)
            dl_task = asyncio.create_task(
                library_api.download_track_file("local_AlbumA/track01.mp3"),
                name="authoritative-download",
            )
            await asyncio.sleep(0.3)
            self.assertFalse(dl_task.done(), "authoritative read must wait for the running scan")
            self.assertEqual(body_count["n"], 1, "authoritative read must not start a second scan")
            ev_a.set()
            resp = await dl_task
            await a_task
            self.assertEqual(resp.status_code, 200)
        self.assertEqual(body_count["n"], 1, "exactly one scan ran (the startup scan)")
        self.assertEqual(gate.max_active, 1)

    # ── L. Authoritative read sees the post-mandatory state ──────────────

    async def test_l_authoritative_waits_for_pending_mandatory_scan(self):
        saved_os = library_mod.os
        snapshots = WalkSnapshots(saved_os.walk)
        library_mod.os = OsProxy(saved_os, snapshots)
        gate = FirstCallGate()
        ev_a = threading.Event()
        gate.add_block(ev_a)
        original = LibraryScanner._create_track_from_file
        timer = threading.Timer(25, ev_a.set)
        timer.daemon = True
        timer.start()
        try:
            with patch.object(LibraryScanner, "_create_track_from_file", slow_patch(gate.make_patch(original), 0.15)):
                a_task = asyncio.create_task(asyncio.to_thread(self.scanner.refresh, True), name="scan-A")
                await asyncio.sleep(0.4)
                (MUSIC_ROOT / "AlbumA" / "track07.mp3").write_bytes(b"\x00garbage-not-audio\x00" * 64)
                b_task = asyncio.create_task(
                    main._drain_worker(self.scanner.refresh, True, wait_if_running=True),
                    name="scan-B-mandatory",
                )
                await self._wait_until(lambda: self.scanner._scan_pending == 1)
                c_task = asyncio.create_task(
                    main._drain_worker(self.scanner.get_tracks, authoritative=True),
                    name="scan-C-authoritative",
                )
                await asyncio.sleep(0.3)
                self.assertFalse(c_task.done(), "authoritative read must wait for the pending mandatory scan")
                ev_a.set()
                await a_task
                await b_task
                tracks = await c_task
        finally:
            library_mod.os = saved_os
            (MUSIC_ROOT / "AlbumA" / "track07.mp3").unlink(missing_ok=True)
        self.assertEqual(len(tracks), 7, "authoritative read sees the post-mandatory state, not A's")
        self.assertTrue(any(t.path and t.path.name == "track07.mp3" for t in tracks))
        self.assertEqual(gate.max_active, 1, "scans must never run concurrently")

    # ── M. Passive listing stays non-blocking during a scan ──────────────

    async def test_m_passive_listing_nonblocking_during_scan(self):
        gate = FirstCallGate()
        ev_a = threading.Event()
        gate.add_block(ev_a)
        original = LibraryScanner._create_track_from_file
        with patch.object(LibraryScanner, "_create_track_from_file", gate.make_patch(original)):
            a_task = asyncio.create_task(asyncio.to_thread(self.scanner.refresh, True), name="scan-A")
            await asyncio.sleep(0.3)
            self.assertTrue(self.scanner.scanning)
            t0 = time.monotonic()
            tracks = await library_api.list_tracks()
            elapsed = time.monotonic() - t0
            self.assertEqual(tracks, [])
            self.assertLess(elapsed, 1.0, "passive listing must not wait for the running scan")
            self.assertEqual(gate.max_active, 1, "passive listing must not start a second scan")
            ev_a.set()
            await a_task

    # ── N. Authoritative read with populated cache: no scan ──────────────

    async def test_n_authoritative_idle_uses_cache_without_scan(self):
        body_count = {"n": 0}
        real_run = LibraryScanner._run_scan_locked

        def counting_run(self_obj):
            body_count["n"] += 1
            return real_run(self_obj)

        await main._drain_worker(self.scanner.refresh, True)
        with patch.object(LibraryScanner, "_run_scan_locked", counting_run):
            tracks = await main._drain_worker(self.scanner.get_tracks, authoritative=True)
            self.assertEqual(len(tracks), 6)
            self.assertEqual(body_count["n"], 0, "authoritative read with populated cache must not scan")

    # ── O. Authoritative album ID lookup waits for the running scan ──────

    async def test_o_authoritative_album_tracks_waits_for_running_scan(self):
        gate = FirstCallGate()
        ev_a = threading.Event()
        gate.add_block(ev_a)
        original = LibraryScanner._create_track_from_file
        body_count = {"n": 0}
        real_run = LibraryScanner._run_scan_locked

        def counting_run(self_obj):
            body_count["n"] += 1
            return real_run(self_obj)

        album_id = library_mod._album_id("Various", "Various")
        with patch.object(LibraryScanner, "_create_track_from_file", gate.make_patch(original)), \
             patch.object(LibraryScanner, "_run_scan_locked", counting_run):
            a_task = asyncio.create_task(asyncio.to_thread(self.scanner.refresh, True), name="scan-A")
            await asyncio.sleep(0.3)
            lookup = asyncio.create_task(
                library_api.get_album_tracks(album_id),
                name="authoritative-album-lookup",
            )
            await asyncio.sleep(0.3)
            self.assertFalse(lookup.done(), "album ID lookup must wait for the running scan")
            self.assertEqual(body_count["n"], 1, "album ID lookup must not start a second scan")
            ev_a.set()
            resp = await lookup
            await a_task
            self.assertEqual(len(resp), 6, "album resolves from the settled scan result")
        self.assertEqual(body_count["n"], 1, "exactly one full scan ran")
        self.assertEqual(gate.max_active, 1)

    # ── P. Authoritative cover lookups wait for the running scan ─────────

    async def test_p_authoritative_covers_waits_for_running_scan(self):
        gate = FirstCallGate()
        ev_a = threading.Event()
        gate.add_block(ev_a)
        original = LibraryScanner._create_track_from_file
        body_count = {"n": 0}
        real_run = LibraryScanner._run_scan_locked

        def counting_run(self_obj):
            body_count["n"] += 1
            return real_run(self_obj)

        album_id = library_mod._album_id("Various", "Various")
        with patch.object(LibraryScanner, "_create_track_from_file", gate.make_patch(original)), \
             patch.object(LibraryScanner, "_run_scan_locked", counting_run):
            a_task = asyncio.create_task(asyncio.to_thread(self.scanner.refresh, True), name="scan-A")
            await asyncio.sleep(0.3)
            tasks = {
                "album_cover": asyncio.create_task(
                    library_api.get_album_cover(album_id), name="authoritative-album-cover"
                ),
                "track_cover": asyncio.create_task(
                    library_api.get_track_cover("local_AlbumA/track01.mp3"),
                    name="authoritative-track-cover",
                ),
                "cover_info": asyncio.create_task(
                    library_api.get_track_cover_info("local_AlbumA/track01.mp3"),
                    name="authoritative-cover-info",
                ),
            }
            await asyncio.sleep(0.3)
            for name, task in tasks.items():
                self.assertFalse(task.done(), f"{name} must wait for the running scan")
            self.assertEqual(body_count["n"], 1, "cover lookups must not start a second scan")
            ev_a.set()
            album_cover = await tasks["album_cover"]
            track_cover = await tasks["track_cover"]
            cover_info = await tasks["cover_info"]
            await a_task
            self.assertEqual(album_cover.status_code, 200)
            self.assertEqual(track_cover.status_code, 200)
            self.assertEqual(cover_info["available"], True)
        self.assertEqual(body_count["n"], 1, "exactly one full scan ran")
        self.assertEqual(gate.max_active, 1)

    # ── R. Smart top tracks: cache-miss scan offloaded ───────────────────

    async def test_r_smart_top_tracks_offloaded(self):
        original = LibraryScanner._create_track_from_file
        with patch.object(LibraryScanner, "_create_track_from_file", slow_patch(original, 0.3)):
            ticker = Ticker()
            await ticker.start()
            try:
                resp = await library_api.get_smart_top_tracks()
            finally:
                await ticker.stop()
        self.assertEqual(resp, [])
        self.assertGreater(len(ticker.ticks), 10, "ticker must keep running while the top-tracks scan is offloaded")
        self.assertLess(ticker.max_gap(), 0.1, "event loop must stay responsive during the top-tracks scan")
        self.assertFalse(self.scanner.scanning)


class LibraryScanStructureTests(unittest.TestCase):
    def test_no_direct_scan_calls_from_async_routes(self):
        """Guard: async routes must never call scan-capable methods directly.

        All scan-capable calls from async code must be offloaded through the
        runtime blocking runner (_run_blocking / _drain_worker).
        """
        scan_methods = {"refresh", "get_tracks", "get_albums", "get_album_tracks", "get_top_played_tracks"}
        violations = []
        for module in (library_api, main):
            source = Path(module.__file__).resolve().read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                        if sub.func.attr in scan_methods:
                            violations.append(
                                f"{module.__name__}.{node.name}:{sub.lineno}: .{sub.func.attr}("
                            )
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
