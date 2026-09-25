#!/usr/bin/env python3
"""Regression tests for the music-library switch / scan lifecycle guards.

Four reproduced defects are pinned here, each against the real production
objects (no stub of the guarded code itself):

  A. A playback transition prepared against a library must not commit after
     the user switched to another library.  The library generation is captured
     with the playback intent and validated at the app publish boundary, so a
     prepared transition cannot publish a track of the library that was left.
  B. A cancelled library switch must not leave a mixed state.  Once the
     manager published the new library the remaining commit steps run to
     completion; a genuinely failed switch is rolled back to the library that
     was active before, so manager, scanner, queue and player always describe
     the same library.
  C. A scanner worker that was cancelled (library switch, shutdown) or
     overtaken must not publish its results.  The scan holds a claim token
     that ``cancel_refresh`` and every new scan invalidate, so a thread that
     outlived its asyncio wrapper cannot replace the cache, ``last_scan`` or
     the stored metadata of the current state.
  D. Only files whose resolved real path lies inside the active music root are
     accepted.  A symlink below the root that points outside it never reaches
     the cache, the library or playback.

Everything runs against throwaway directories in a temp tree; no real audio,
no network, no mounted shares.
"""

import asyncio
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BASE_DIR = Path(tempfile.mkdtemp(prefix="fxroute-test-libguard-"))
MUSIC_ROOT = BASE_DIR / "music"
CONFIG_DIR = BASE_DIR / "config"

os.environ["MUSIC_ROOT"] = str(MUSIC_ROOT)
os.environ["XDG_CONFIG_HOME"] = str(CONFIG_DIR)
os.environ["LOG_LEVEL"] = "WARNING"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from fastapi import HTTPException
from library.core import LibraryScanner, is_within_resolved_root, path_within_root
from library.metadata import LibraryMetadataStore
from library.sources import LibraryContext, MusicLibraryManager
from playback.queue import PlaybackQueue, PlaybackQueueDependencies

logging.getLogger().setLevel(logging.WARNING)

GARBAGE_AUDIO = b"\x00garbage-not-audio\x00" * 64
SMB_LIBRARY_ID = "smb:openclaw:Music-Demo"


def _no_network(*args, **kwargs):
    raise RuntimeError("network disabled in tests")


async def _wait_until(predicate, timeout: float = 10.0) -> None:
    """Poll ``predicate`` (sync) from the event loop until it holds."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached within timeout")
        await asyncio.sleep(0.01)


async def _wait_thread_event(event: threading.Event, timeout: float = 10.0) -> None:
    """Wait for a worker-thread event without blocking the event loop."""
    deadline = time.monotonic() + timeout
    while not event.is_set():
        if time.monotonic() > deadline:
            raise AssertionError("worker did not reach the blocking point in time")
        await asyncio.sleep(0.01)


class _FakeScanner:
    """Scanner double for the switch tests (the real one scans the FS)."""

    def __init__(self, music_root: Path):
        self.music_root = Path(music_root)
        self.cancel_calls = 0
        self.prepared = 0

    def cancel_refresh(self) -> None:
        self.cancel_calls += 1

    def prepare_scan_status(self) -> None:
        self.prepared += 1

    def refresh(self, *args, **kwargs):
        return []

    @property
    def cancelled(self) -> bool:
        return self.cancel_calls > 0


class _FakePlayer:
    def __init__(self, *, running: bool = True, stop_error: BaseException | None = None):
        self._running = running
        self.stop_calls = 0
        self._stop_error = stop_error

    @property
    def state(self) -> dict:
        return {"current_file": "" if not self._running else "/music/old.mp3", "paused": False, "ended": False}

    def stop_playback(self) -> None:
        self.stop_calls += 1
        if self._stop_error is not None:
            raise self._stop_error
        self._running = False


class _FakeRequest:
    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self):
        return self._payload


def _make_manager(base: Path) -> tuple[MusicLibraryManager, Path, Path]:
    """A real manager with one local root and one mounted SMB share."""
    local = base / "Music"
    local.mkdir(parents=True, exist_ok=True)
    mounted = base / "mounts" / "openclaw" / "Music-Demo"
    mounted.mkdir(parents=True, exist_ok=True)
    manager = MusicLibraryManager(local, mount_root=base / "mounts", discovery_hosts=[])
    manager.add_manual_share("openclaw", "Music-Demo")
    return manager, local.resolve(), mounted.resolve()


def _metadata_store(base: Path, name: str) -> LibraryMetadataStore:
    return LibraryMetadataStore(base / f"{name}.sqlite", base / f"{name}-covers")


class _RuntimeGuard:
    """Restore the process-wide main runtime around one switch test."""

    def __init__(self, test, manager, scanner, player=None):
        self._test = test
        self._library = main.runtime.music_library
        self._manager = manager
        self._scanner = scanner
        self._player = player
        self._queue = main.playback_queue.queue

    def __enter__(self):
        self._previous = (
            self._library.manager,
            self._library.scanner,
            self._library.switch_lock,
            main.runtime.player_instance,
            self._queue.snapshot(),
        )
        self._library.manager = self._manager
        self._library.scanner = self._scanner
        self._library.switch_lock = None
        main.runtime.player_instance = self._player
        return self

    def __exit__(self, *exc_info):
        (
            self._library.manager,
            self._library.scanner,
            self._library.switch_lock,
            main.runtime.player_instance,
            snapshot,
        ) = self._previous
        self._queue.tracks = snapshot.tracks
        self._queue.original = snapshot.original
        self._queue.index = snapshot.index
        self._queue.mode = snapshot.mode
        self._queue.loop = snapshot.loop
        self._queue.shuffle = snapshot.shuffle
        self._queue.single_track_loop = snapshot.single_track_loop
        return False


# ---------------------------------------------------------------------------
# A. Playback commit vs. library switch
# ---------------------------------------------------------------------------

class PlaybackLibraryContextTests(unittest.IsolatedAsyncioTestCase):
    """A prepared playback intent may not commit across a library switch."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.manager, self.local_root, self.smb_root = _make_manager(self.base)

    def _activate_smb(self):
        with patch("library.sources.os.path.ismount", return_value=True):
            return self.manager.activate(SMB_LIBRARY_ID)

    async def test_library_switch_rejects_a_prepared_playback_intent(self):
        with patch.object(main.runtime.music_library, "manager", self.manager):
            intent = main._capture_source_intent()
            main._ensure_source_intent_current(intent)
            self._activate_smb()
            with self.assertRaises(HTTPException) as ctx:
                main._ensure_source_intent_current(intent)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Music library changed", str(ctx.exception.detail))

    async def test_reselecting_the_active_library_keeps_the_intent_valid(self):
        self._activate_smb()
        with patch.object(main.runtime.music_library, "manager", self.manager):
            intent = main._capture_source_intent()
            # Same library again: no switch happened, so prepared work stays valid.
            self._activate_smb()
            main._ensure_source_intent_current(intent)
            self.manager.activate("local")
            with self.assertRaises(HTTPException) as ctx:
                main._ensure_source_intent_current(intent)
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_intent_without_library_context_still_validates_the_source(self):
        """No manager means no library boundary, but the source one still counts."""
        with patch.object(main.runtime.music_library, "manager", None):
            intent = main._capture_source_intent()
            self.assertIsNone(intent.library)
            main._ensure_source_intent_current(intent)
            main.playback_state.note_source_selection("external-bluetooth")
            with self.assertRaises(HTTPException) as ctx:
                main._ensure_source_intent_current(intent)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Audio source changed", str(ctx.exception.detail))

    async def test_queue_transition_commits_nothing_after_a_library_switch(self):
        """The reproduced bug: a prepared transition publishes the old library's track."""
        committed: list[dict] = []
        old_tracks = [
            {"id": "local_old_a", "url": "/music/a.mp3", "source": "local"},
            {"id": "local_old_b", "url": "/music/b.mp3", "source": "local"},
        ]

        async def run_transition(request):
            # The switch commits while the transition is still running.
            self._activate_smb()
            return SimpleNamespace(committed=True, target_rate=48000, transition_id="transition-1")

        queue = PlaybackQueue(PlaybackQueueDependencies(
            player=lambda: None,
            run_transition=run_transition,
            commit_coordinated_track=lambda track, **kwargs: committed.append(track),
            get_current_track_info=lambda: None,
            set_track_context=lambda *args: None,
            transition_is_active=lambda: False,
            player_is_running=lambda *args: True,
            wait_for_player_current_file=None,
            coordinator_target_rate=lambda *args, **kwargs: 48000,
            coordinator_rate_change=lambda *args: False,
            sample_rate_policy_is_auto=lambda: True,
            transition_error_http=lambda exc: exc,
            get_tracks=lambda: [],
            build_playback_payload=lambda *args, **kwargs: {},
            resolve_stream_url=None,
            capture_source_intent=main._capture_source_intent,
            ensure_source_intent_current=main._ensure_source_intent_current,
        ))
        queue.tracks = [dict(track) for track in old_tracks]
        queue.index = 0

        with patch.object(main.runtime.music_library, "manager", self.manager):
            with self.assertRaises(HTTPException) as ctx:
                await queue.load_track(1)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Music library changed", str(ctx.exception.detail))
        self.assertEqual(committed, [], "no track of the previous library may be published")
        self.assertEqual(queue.index, 0, "the committed queue must stay untouched")
        self.assertEqual([track["id"] for track in queue.tracks], ["local_old_a", "local_old_b"])

    async def test_queue_transition_still_commits_without_a_library_switch(self):
        committed: list[dict] = []

        async def run_transition(request):
            return SimpleNamespace(committed=True, target_rate=48000, transition_id="transition-1")

        queue = PlaybackQueue(PlaybackQueueDependencies(
            player=lambda: None,
            run_transition=run_transition,
            commit_coordinated_track=lambda track, **kwargs: committed.append(track),
            get_current_track_info=lambda: None,
            set_track_context=lambda *args: None,
            transition_is_active=lambda: False,
            player_is_running=lambda *args: True,
            wait_for_player_current_file=None,
            coordinator_target_rate=lambda *args, **kwargs: 48000,
            coordinator_rate_change=lambda *args: False,
            sample_rate_policy_is_auto=lambda: True,
            transition_error_http=lambda exc: exc,
            get_tracks=lambda: [],
            build_playback_payload=lambda *args, **kwargs: {},
            resolve_stream_url=None,
            capture_source_intent=main._capture_source_intent,
            ensure_source_intent_current=main._ensure_source_intent_current,
        ))
        queue.tracks = [
            {"id": "local_a", "url": "/music/a.mp3", "source": "local"},
            {"id": "local_b", "url": "/music/b.mp3", "source": "local"},
        ]
        queue.index = 0

        with patch.object(main.runtime.music_library, "manager", self.manager):
            self.assertTrue(await queue.load_track(1))

        self.assertEqual([track["id"] for track in committed], ["local_b"])
        self.assertEqual(queue.index, 1)

    async def test_production_queue_wiring_captures_the_library_context(self):
        """The shipped deps must capture the full intent, not the raw tuple.

        The queue calls the injected capture/ensure pair.  If the wiring
        captured only the source tuple the library check would be silently
        skipped in production while the unit-level guard still looked fine.
        """
        with patch.object(main.runtime.music_library, "manager", self.manager):
            deps = main.playback_queue.queue._deps
            captured = deps.capture_source_intent()
            self.assertIsInstance(captured, main.PlaybackCommitIntent)
            self.assertIsInstance(captured.library, LibraryContext)
            deps.ensure_source_intent_current(captured)
            self._activate_smb()
            with self.assertRaises(HTTPException) as ctx:
                deps.ensure_source_intent_current(captured)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Music library changed", str(ctx.exception.detail))

    def test_manager_generation_advances_only_on_a_real_change(self):
        self.assertEqual(self.manager.active_id, "local")
        before = self.manager.capture_context()
        self._activate_smb()
        after_switch = self.manager.capture_context()
        self.assertGreater(after_switch.generation, before.generation)
        self.assertFalse(self.manager.context_is_current(before))
        self.assertTrue(self.manager.context_is_current(after_switch))

        self._activate_smb()
        self.assertEqual(
            self.manager.capture_context().generation,
            after_switch.generation,
            "re-selecting the active library must not invalidate prepared work",
        )
        self.assertTrue(self.manager.context_is_current(after_switch))

        self.manager.activate("local")
        self.assertFalse(self.manager.context_is_current(after_switch))

    def test_failed_activation_keeps_the_previous_context(self):
        before = self.manager.capture_context()
        with patch("library.sources.os.path.ismount", return_value=False), patch(
            "library.sources.subprocess.run", return_value=SimpleNamespace(returncode=1, stderr="", stdout="")
        ):
            with self.assertRaises(FileNotFoundError):
                self.manager.activate(SMB_LIBRARY_ID)
        self.assertEqual(self.manager.active_id, "local")
        self.assertTrue(self.manager.context_is_current(before))

    def test_restore_active_republishes_without_advancing_the_generation(self):
        before = self.manager.capture_context()
        self._activate_smb()
        self.manager.restore_active(*self.manager.active_snapshot())
        self.manager.restore_active("local", "local", self.local_root)
        self.assertEqual(self.manager.active_id, "local")
        self.assertFalse(
            self.manager.context_is_current(before),
            "a failed switch still counts as a context change for prepared work",
        )
        self.assertEqual(self.manager.context_is_current(None), False)


# ---------------------------------------------------------------------------
# B. Cancellation / failure during the library switch
# ---------------------------------------------------------------------------

class LibrarySwitchCancellationTests(unittest.IsolatedAsyncioTestCase):
    """A switch is completed or rolled back - never left half applied."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.manager, self.local_root, self.smb_root = _make_manager(self.base)
        self.scanner = _FakeScanner(self.local_root)
        self.player = _FakePlayer(running=True)
        self.addCleanup(self._clear_runtime)

    def _clear_runtime(self):
        for task in list(main.runtime.library_refresh_tasks):
            task.cancel()
        main.runtime.library_refresh_tasks.clear()
        main.runtime.library_scan_task = None
        main.playback_queue.queue.reset()

    async def _drain_pending_scan(self):
        task = main.runtime.library_scan_task
        if task is not None and not task.done():
            with patch.object(LibraryScanner, "refresh", lambda *a, **k: []):
                await asyncio.gather(task, return_exceptions=True)

    async def test_cancellation_after_activate_completes_the_switch(self):
        """The reproduced bug: manager on the new library, scanner/queue on the old."""
        gate = asyncio.Event()
        blocker = asyncio.create_task(gate.wait(), name="switch-blocker")
        main.runtime.library_refresh_tasks.add(blocker)

        with _RuntimeGuard(self, self.manager, self.scanner, self.player), \
                patch("library.sources.os.path.ismount", return_value=True):
            switch = asyncio.create_task(
                main.select_music_library(_FakeRequest({"id": SMB_LIBRARY_ID})),
                name="library-switch",
            )
            # Wait until the manager published the new library and the commit
            # section is inside the section that must not be cut short.
            await _wait_until(lambda: self.manager.active_id == SMB_LIBRARY_ID)
            await _wait_until(lambda: self.scanner.cancelled)
            await _wait_until(lambda: not blocker.done())

            switch.cancel()
            gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await switch

            # The switch ran to completion despite the cancellation.
            self.assertEqual(self.manager.active_id, SMB_LIBRARY_ID)
            self.assertEqual(self.manager.active_root, self.smb_root)
            self.assertIsNot(main.runtime.music_library.scanner, self.scanner)
            self.assertEqual(
                main.runtime.music_library.scanner.music_root,
                self.smb_root,
                "scanner must describe the same library as the manager",
            )
            self.assertEqual(self.player.stop_calls, 1)
            self.assertEqual(main.playback_queue.queue.tracks, [], "queue must be reset with the library")
            await self._drain_pending_scan()

    async def test_failed_switch_restores_the_previous_library(self):
        main.playback_queue.queue.tracks = [{"id": "local_a", "url": "/music/a.mp3", "source": "local"}]
        player = _FakePlayer(running=True, stop_error=RuntimeError("mpv is gone"))

        with _RuntimeGuard(self, self.manager, self.scanner, player), \
                patch("library.sources.os.path.ismount", return_value=True):
            with self.assertRaises(RuntimeError):
                await main.select_music_library(_FakeRequest({"id": SMB_LIBRARY_ID}))

            self.assertEqual(self.manager.active_id, "local", "a failed switch must roll the manager back")
            self.assertEqual(self.manager.active_type, "local")
            self.assertEqual(self.manager.active_root, self.local_root)
            self.assertIs(main.runtime.music_library.scanner, self.scanner, "scanner must stay on the old library")
            self.assertEqual(
                [track["id"] for track in main.playback_queue.queue.tracks],
                ["local_a"],
                "the old queue must not be left half torn down",
            )

    async def test_switch_without_cancellation_completes_normally(self):
        with _RuntimeGuard(self, self.manager, self.scanner, self.player), \
                patch("library.sources.os.path.ismount", return_value=True):
            response = await main.select_music_library(_FakeRequest({"id": SMB_LIBRARY_ID}))
            self.assertEqual(response["active_id"], SMB_LIBRARY_ID)
            self.assertEqual(response["active_type"], "smb")
            self.assertEqual(main.runtime.music_library.scanner.music_root, self.smb_root)
            await self._drain_pending_scan()

    async def test_rollback_failure_is_reported_but_never_hides_the_switch_error(self):
        player = _FakePlayer(running=True, stop_error=RuntimeError("mpv is gone"))
        with _RuntimeGuard(self, self.manager, self.scanner, player), \
                patch("library.sources.os.path.ismount", return_value=True), \
                patch.object(self.manager, "restore_active", side_effect=OSError("gone")):
            with self.assertRaises(RuntimeError) as ctx:
                await main.select_music_library(_FakeRequest({"id": SMB_LIBRARY_ID}))
        self.assertIn("mpv is gone", str(ctx.exception))

    async def test_finish_despite_cancellation_reports_a_failed_section(self):
        async def failing():
            raise ValueError("section failed")

        with self.assertRaises(ValueError):
            await main._finish_despite_cancellation(failing(), name="failing-section")

    async def test_finish_despite_cancellation_propagates_cancellation(self):
        finished = []

        async def slow():
            await asyncio.sleep(0.05)
            finished.append(True)

        task = asyncio.create_task(main._finish_despite_cancellation(slow(), name="slow-section"))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(finished, [True], "the section must run to completion before the cancel propagates")


# ---------------------------------------------------------------------------
# C. Scanner worker drain
# ---------------------------------------------------------------------------

class _FirstFileGate:
    """Hold a scan thread inside its first per-file metadata read."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self._calls = 0

    def instrument(self, store: LibraryMetadataStore) -> LibraryMetadataStore:
        real = store.get_cached_track

        def blocking(*args, **kwargs):
            with self._lock:
                self._calls += 1
                first = self._calls == 1
            if first:
                self.entered.set()
                if not self.release.wait(30):
                    raise RuntimeError("gate timed out waiting for release")
            return real(*args, **kwargs)

        store.get_cached_track = blocking
        return store


class ScannerDrainTests(unittest.IsolatedAsyncioTestCase):
    """A cancelled or overtaken scan must not publish anything."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        network = patch.object(LibraryMetadataStore, "_request_json", _no_network)
        network.start()
        self.addCleanup(network.stop)

    def _root(self, name: str, filenames: list[str]) -> Path:
        root = self.base / name
        root.mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            (root / filename).write_bytes(GARBAGE_AUDIO)
        return root

    def _scanner(self, name: str, root: Path, store=None) -> LibraryScanner:
        return LibraryScanner(
            root,
            metadata_store=store or _metadata_store(self.base, name),
        )

    async def test_scan_cancelled_mid_traversal_publishes_nothing(self):
        root = self._root("two", ["a.mp3", "b.mp3"])
        gate = _FirstFileGate()
        scanner = self._scanner("two", root, gate.instrument(_metadata_store(self.base, "two")))

        task = asyncio.create_task(asyncio.to_thread(scanner.refresh, True), name="scan")
        await _wait_thread_event(gate.entered)
        scanner.cancel_refresh()
        gate.release.set()
        await task

        self.assertEqual(scanner._track_cache, [], "a cancelled scan must not fill the cache")
        self.assertIsNone(scanner.last_scan, "a cancelled scan must not publish a scan timestamp")
        self.assertEqual(scanner.get_tracks(False), [])

    async def test_scan_cancelled_on_its_last_file_publishes_nothing(self):
        root = self._root("one", ["only.mp3"])
        gate = _FirstFileGate()
        scanner = self._scanner("one", root, gate.instrument(_metadata_store(self.base, "one")))

        task = asyncio.create_task(asyncio.to_thread(scanner.refresh, True), name="scan")
        await _wait_thread_event(gate.entered)
        scanner.cancel_refresh()
        gate.release.set()
        await task

        self.assertEqual(scanner._track_cache, [], "the pre-publish guard must discard the finished walk")
        self.assertIsNone(scanner.last_scan)

    async def test_cancelled_scan_does_not_publish_missing_track_state(self):
        """The metadata sync at the end of a scan must not run for a stale walk.

        It is the step that retires tracks the walk did not see, so a stale
        walk running it would mark live files as missing in the store.
        """
        root = self._root("meta", ["a.mp3", "b.mp3", "gone.mp3"])
        store = _metadata_store(self.base, "meta")
        scanner = LibraryScanner(root, metadata_store=store)
        await main._drain_worker(scanner.refresh, True)
        (root / "gone.mp3").unlink()

        def missing_since(rel_path: str):
            with store._connect() as conn:
                row = conn.execute(
                    "SELECT missing_since FROM tracks WHERE rel_path = ?", (rel_path,)
                ).fetchone()
            return None if row is None else row["missing_since"]

        self.assertIsNone(missing_since("gone.mp3"), "precondition: the track is present")

        gate = _FirstFileGate()
        gate.instrument(store)
        stale_scan = LibraryScanner(root, metadata_store=store)
        task = asyncio.create_task(asyncio.to_thread(stale_scan.refresh, True), name="scan")
        await _wait_thread_event(gate.entered)
        stale_scan.cancel_refresh()
        gate.release.set()
        await task

        self.assertIsNone(
            missing_since("gone.mp3"),
            "a cancelled scan must not mark tracks it never got to see as missing",
        )

        # A completed scan of the same directory does retire the track, so the
        # assertion above is a real regression guard and not a tautology.
        await main._drain_worker(LibraryScanner(root, metadata_store=store).refresh, True)
        self.assertIsNotNone(missing_since("gone.mp3"))

    async def test_new_scanner_after_a_switch_is_unaffected_by_the_cancelled_one(self):
        old_root = self._root("old", ["old_a.mp3", "old_b.mp3"])
        new_root = self._root("new", ["new_a.mp3"])
        gate = _FirstFileGate()
        old_scanner = self._scanner("old", old_root, gate.instrument(_metadata_store(self.base, "old")))
        new_scanner = self._scanner("new", new_root)

        task = asyncio.create_task(asyncio.to_thread(old_scanner.refresh, True), name="scan-old")
        await _wait_thread_event(gate.entered)
        # Library switch: the old scanner is cancelled, a fresh one takes over.
        old_scanner.cancel_refresh()
        new_scanner.prepare_scan_status()
        await main._drain_worker(new_scanner.refresh, True)
        gate.release.set()
        await task

        self.assertEqual(old_scanner.get_tracks(False), [], "the abandoned scanner stays empty")
        self.assertEqual(
            [Path(track.path).name for track in new_scanner.get_tracks(False)],
            ["new_a.mp3"],
            "the current scanner state is the one the switch established",
        )

    async def test_uncancelled_scan_still_publishes(self):
        root = self._root("plain", ["a.mp3", "b.mp3"])
        scanner = self._scanner("plain", root)
        await main._drain_worker(scanner.refresh, True)
        self.assertEqual(
            sorted(Path(track.path).name for track in scanner.get_tracks(False)),
            ["a.mp3", "b.mp3"],
        )
        self.assertIsNotNone(scanner.last_scan)

    def test_cancel_invalidates_the_running_claim(self):
        scanner = self._scanner("claim", self._root("claim", ["a.mp3"]))
        with scanner._scan_state_lock:
            scanner._scan_token += 1
            token = scanner._scan_token
        self.assertTrue(scanner._scan_claim_is_current(token))
        scanner.cancel_refresh()
        self.assertFalse(scanner._scan_claim_is_current(token))


# ---------------------------------------------------------------------------
# D. Symlink boundary
# ---------------------------------------------------------------------------

class LibrarySymlinkBoundaryTests(unittest.TestCase):
    """Only files that really live inside the music root are accepted."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        network = patch.object(LibraryMetadataStore, "_request_json", _no_network)
        network.start()
        self.addCleanup(network.stop)

    def _scanner(self, name: str, root: Path) -> LibraryScanner:
        return LibraryScanner(root, metadata_store=_metadata_store(self.base, name))

    def test_symlink_outside_the_music_root_is_not_added(self):
        root = self.base / "music"
        outside = self.base / "outside"
        root.mkdir()
        outside.mkdir()
        (root / "real.mp3").write_bytes(GARBAGE_AUDIO)
        (outside / "secret.mp3").write_bytes(GARBAGE_AUDIO)
        (root / "inside-link.mp3").symlink_to(root / "real.mp3")
        (root / "escape.mp3").symlink_to(outside / "secret.mp3")

        scanner = self._scanner("symlink", root)
        tracks = scanner.refresh(True)

        found = sorted(Path(track.path).name for track in tracks)
        self.assertEqual(
            found,
            ["inside-link.mp3", "real.mp3"],
            "a symlink inside the root keeps working, a symlink out of it is rejected",
        )
        self.assertNotIn("local_escape.mp3", [track.id for track in tracks])
        self.assertEqual(
            sorted(track.id for track in tracks),
            ["local_inside-link.mp3", "local_real.mp3"],
            "the real file and the in-root symlink are both indexed, the escape is not",
        )
        self.assertEqual(scanner.status()["files_outside_root"], 1)

    def test_escaped_file_never_reaches_playback_paths(self):
        root = self.base / "music2"
        outside = self.base / "outside2"
        root.mkdir()
        outside.mkdir()
        (outside / "secret.mp3").write_bytes(GARBAGE_AUDIO)
        (root / "escape.mp3").symlink_to(outside / "secret.mp3")
        (root / "keep.mp3").write_bytes(GARBAGE_AUDIO)

        scanner = self._scanner("symlink2", root)
        tracks = scanner.refresh(True)

        self.assertEqual([Path(track.path).name for track in tracks], ["keep.mp3"])
        for track in tracks:
            self.assertTrue(
                path_within_root(Path(track.path), root),
                "every accepted track must really live inside the music root",
            )

    def test_resolved_root_helper_agrees_with_path_within_root(self):
        root = self.base / "music3"
        nested = root / "album"
        nested.mkdir(parents=True)
        inside = nested / "song.mp3"
        inside.write_bytes(GARBAGE_AUDIO)
        outside = self.base / "outside3.mp3"
        outside.write_bytes(GARBAGE_AUDIO)
        escape = root / "escape.mp3"
        escape.symlink_to(outside)

        resolved_root = root.resolve()
        self.assertTrue(is_within_resolved_root(inside, resolved_root))
        self.assertTrue(is_within_resolved_root(root / "album", resolved_root))
        self.assertFalse(is_within_resolved_root(escape, resolved_root))
        self.assertFalse(is_within_resolved_root(outside, resolved_root))
        # Same verdict as the pre-resolving helper for every case.
        for candidate in (inside, root / "album", escape, outside):
            self.assertEqual(
                is_within_resolved_root(candidate, resolved_root),
                path_within_root(candidate, root),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
