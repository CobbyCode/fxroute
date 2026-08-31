#!/usr/bin/env python3
"""Regression test: the yt-dlp startup verification must tolerate slow hosts."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import downloader
from downloader import Downloader


class DownloaderYtdlpVerifyTimeoutTests(unittest.TestCase):
    def test_verify_ytdlp_allows_slow_startup(self):
        """The --version probe must not use a tight timeout that fails on
        slow boards (or emulated guests) where Python startup alone can
        exceed a few seconds."""
        captured = {}

        def fake_run(*args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return mock.Mock(returncode=0, stdout="2024.01.01\n", stderr="")

        with mock.patch.object(downloader, "get_settings") as settings, \
                mock.patch.object(subprocess, "run", side_effect=fake_run):
            settings.return_value.download_dir.mkdir = lambda **_kwargs: None
            Downloader()

        self.assertIsNotNone(captured["timeout"])
        self.assertGreaterEqual(captured["timeout"], 30)


if __name__ == "__main__":
    unittest.main()
