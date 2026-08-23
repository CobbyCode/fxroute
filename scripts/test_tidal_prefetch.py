# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for TIDAL N+1 DASH prefetch."""

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TestPrefetchStream(unittest.TestCase):
    """Unit tests for the silent prefetch_stream wrapper."""

    def setUp(self):
        # Isolate the module-level patch handle
        self._resolve_patcher = mock.patch(
            "streaming.tidal.playback.resolve_stream_for_id",
            autospec=True,
        )
        self.mock_resolve = self._resolve_patcher.start()
        self.addCleanup(self._resolve_patcher.stop)

    def test_prefetch_resolves_next_track(self):
        from streaming.tidal.playback import prefetch_stream

        prefetch_stream("12345")
        self.mock_resolve.assert_called_once_with("12345")

    def test_prefetch_silently_swallows_errors(self):
        from streaming.tidal.playback import prefetch_stream

        self.mock_resolve.side_effect = RuntimeError("network down")
        # Must not raise
        prefetch_stream("12345")
        self.mock_resolve.assert_called_once_with("12345")

    def test_prefetch_silently_swallows_auth_error(self):
        from streaming.tidal.playback import prefetch_stream
        from streaming.tidal.playback import TidalStreamError

        self.mock_resolve.side_effect = TidalStreamError("auth", "expired")
        prefetch_stream("12345")
        self.mock_resolve.assert_called_once_with("12345")


class TestScheduleTidalPrefetch(unittest.TestCase):
    """Tests for the fire-and-forget scheduler in main.py."""

    def setUp(self):
        # Patch the queue so we control its state without importing the real
        # module tree.
        self._queue_patcher = mock.patch("main.playback_queue")
        self.mock_pq = self._queue_patcher.start()
        self.addCleanup(self._queue_patcher.stop)

        # Patch prefetch_stream at its definition site (it's imported inside
        # _schedule_tidal_prefetch via `from streaming.tidal.playback import
        # prefetch_stream`).
        self._pf_patcher = mock.patch(
            "streaming.tidal.playback.prefetch_stream", autospec=True
        )
        self.mock_pf = self._pf_patcher.start()
        self.addCleanup(self._pf_patcher.stop)

    # -- helpers -----------------------------------------------------------

    def _set_queue(self, tracks, index):
        self.mock_pq.queue.tracks = tracks
        self.mock_pq.queue.index = index

    def _wait_thread(self, timeout=2.0):
        """Wait until the prefetch thread finishes (or timeout)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.mock_pf.called:
                return
            time.sleep(0.02)
        raise AssertionError("prefetch_stream was never called within timeout")

    def _wait_no_thread(self, settle=0.15):
        """Let any spawned thread finish, then assert none was started."""
        time.sleep(settle)
        self.assertFalse(self.mock_pf.called, "prefetch_stream should not be called")

    # -- tests -------------------------------------------------------------

    def test_schedules_thread_for_next_tidal_track(self):
        from main import _schedule_tidal_prefetch

        self._set_queue(
            [
                {"id": "111", "source": "tidal", "title": "Now"},
                {"id": "222", "source": "tidal", "title": "Next"},
            ],
            index=0,
        )
        _schedule_tidal_prefetch()
        self._wait_thread()
        self.mock_pf.assert_called_once_with("222")

    def test_no_prefetch_when_at_queue_end(self):
        from main import _schedule_tidal_prefetch

        self._set_queue(
            [{"id": "111", "source": "tidal", "title": "Last"}],
            index=0,
        )
        _schedule_tidal_prefetch()
        self._wait_no_thread()

    def test_no_prefetch_for_empty_queue(self):
        from main import _schedule_tidal_prefetch

        self._set_queue([], index=-1)
        _schedule_tidal_prefetch()
        self._wait_no_thread()

    def test_no_prefetch_when_next_is_local(self):
        from main import _schedule_tidal_prefetch

        self._set_queue(
            [
                {"id": "111", "source": "tidal", "title": "Now"},
                {"id": "333", "source": "local", "title": "LocalNext"},
            ],
            index=0,
        )
        _schedule_tidal_prefetch()
        self._wait_no_thread()

    def test_no_prefetch_when_next_has_no_id(self):
        from main import _schedule_tidal_prefetch

        self._set_queue(
            [
                {"id": "111", "source": "tidal", "title": "Now"},
                {"source": "tidal", "title": "NoID"},
            ],
            index=0,
        )
        _schedule_tidal_prefetch()
        self._wait_no_thread()

    def test_no_prefetch_for_radio_source(self):
        """_commit_coordinated_track skips prefetch on radio source."""
        from main import _commit_coordinated_track

        self._set_queue(
            [
                {"id": "station1", "source": "radio", "title": "Radio"},
                {"id": "222", "source": "tidal", "title": "Next"},
            ],
            index=0,
        )
        with mock.patch("main._publish_playback_context_commit"), \
             mock.patch("main._mark_player_state_authoritative"), \
             mock.patch("main._record_local_track_started"):
            self.mock_pq.runtime.player_instance = mock.Mock(state={})
            _commit_coordinated_track(
                {"id": "station1", "source": "radio", "url": "dummy"},
                source="radio",
            )
        self._wait_no_thread()

    def test_prefetch_happens_via_commit_coordinated_track(self):
        from main import _commit_coordinated_track

        self._set_queue(
            [
                {"id": "111", "source": "tidal", "title": "Now"},
                {"id": "222", "source": "tidal", "title": "Next"},
            ],
            index=0,
        )

        with mock.patch("main._publish_playback_context_commit"), \
             mock.patch("main._mark_player_state_authoritative"), \
             mock.patch("main._record_local_track_started"):
            self.mock_pq.runtime.player_instance = mock.Mock(state={})
            _commit_coordinated_track(
                {"id": "111", "source": "tidal", "url": "dummy"},
                source="tidal",
            )

        self._wait_thread()
        self.mock_pf.assert_called_once_with("222")

    def test_commit_coordinated_track_no_prefetch_for_local(self):
        from main import _commit_coordinated_track

        self._set_queue(
            [
                {"id": "111", "source": "local", "title": "Now"},
                {"id": "222", "source": "tidal", "title": "Next"},
            ],
            index=0,
        )

        with mock.patch("main._publish_playback_context_commit"), \
             mock.patch("main._mark_player_state_authoritative"), \
             mock.patch("main._record_local_track_started"):
            self.mock_pq.runtime.player_instance = mock.Mock(state={})
            _commit_coordinated_track(
                {"id": "111", "source": "local", "url": "dummy"},
                source="local",
            )

        self._wait_no_thread()


if __name__ == "__main__":
    unittest.main()