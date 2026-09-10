#!/usr/bin/env python3
"""Downloader start-response semantics: no fabricated filename.

The /api/download POST response must never present a synthetic name as the
saved file: the real filename is only known once yt-dlp reports it during
the run, and the status endpoint (active_download["filename"]) is the
authoritative source for it. The start call therefore returns None and the
initial download state carries filename=None, not a guessed "download_<ts>".
"""

import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from downloader import Downloader


class DownloaderFilenameTests(unittest.TestCase):
    def _downloader(self):
        with patch.object(Downloader, "_verify_ytdlp"), patch(
            "downloader.get_settings"
        ) as settings:
            settings.return_value.download_dir.mkdir = lambda **_kwargs: None
            return Downloader()

    def test_start_returns_none_not_a_fabricated_filename(self):
        downloader = self._downloader()
        with patch.object(Downloader, "_download_thread", return_value=None):
            result = downloader.download("https://www.youtube.com/watch?v=abc123")
        self.assertIsNone(result, "download() must not return a synthetic filename")

    def test_initial_state_has_no_fabricated_filename(self):
        downloader = self._downloader()
        # A no-op thread leaves the start state in place so it can be inspected.
        with patch.object(Downloader, "_download_thread", return_value=None):
            downloader.download("https://www.youtube.com/watch?v=abc123")
        state = downloader.active_download
        self.assertIsNotNone(state)
        self.assertIsNone(
            state["filename"],
            "the status payload must not claim a filename before yt-dlp reports one",
        )
        self.assertEqual(state["status"], "starting")

    def test_no_synthetic_filename_helper_remains(self):
        downloader = self._downloader()
        self.assertFalse(
            hasattr(downloader, "_get_output_filename"),
            "the fabricated-name helper must be gone",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)