#!/usr/bin/env python3
"""Downloader lifespan ownership regression tests."""

import threading
import pathlib
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from downloader import Downloader


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class DownloaderShutdownTests(unittest.TestCase):
    def test_shutdown_stops_process_and_joins_worker(self):
        with patch.object(Downloader, "_verify_ytdlp"), patch("downloader.get_settings") as settings:
            settings.return_value.download_dir.mkdir = lambda **_kwargs: None
            downloader = Downloader()
        process = FakeProcess()
        release = threading.Event()
        worker = threading.Thread(target=release.wait, daemon=True)
        worker.start()
        downloader._process = process
        downloader._worker_thread = worker

        original_join = worker.join
        late_future = MagicMock()

        def join(timeout=None):
            downloader._callback_futures.add(late_future)
            release.set()
            original_join(timeout)

        with patch.object(worker, "join", side_effect=join):
            downloader.shutdown()

        self.assertTrue(process.terminated)
        self.assertFalse(worker.is_alive())
        self.assertEqual(downloader._callbacks, [])
        self.assertIsNone(downloader._callback_loop)
        late_future.cancel.assert_called_once()

    def test_shutdown_cancels_submitted_callback_futures(self):
        with patch.object(Downloader, "_verify_ytdlp"), patch("downloader.get_settings") as settings:
            settings.return_value.download_dir.mkdir = lambda **_kwargs: None
            downloader = Downloader()
        future = MagicMock()
        future.result.side_effect = __import__("concurrent.futures").futures.CancelledError
        downloader._callback_futures.add(future)

        downloader.shutdown()

        future.cancel.assert_called_once()
        future.result.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
