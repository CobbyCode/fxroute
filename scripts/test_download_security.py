#!/usr/bin/env python3
"""Security contracts for the media download endpoint and yt-dlp process."""

from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
import safe_http  # noqa: E402
from downloader import Downloader  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def public_dns(host, port=None, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 0))]


def private_dns(host, port=None, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.5", 0))]


def make_downloader():
    with mock.patch.object(Downloader, "_verify_ytdlp"), mock.patch(
        "downloader.get_settings"
    ) as settings:
        settings.return_value.download_dir.mkdir = lambda **_kwargs: None
        settings.return_value.download_transcode_format = None
        return Downloader()


class DownloaderUrlValidationTests(unittest.TestCase):
    def setUp(self):
        self.downloader = make_downloader()

    def test_private_literal_is_rejected_before_state_or_thread_start(self):
        with mock.patch.object(self.downloader, "_download_thread") as worker:
            with self.assertRaises(safe_http.BlockedUrlError):
                self.downloader.download("http://127.0.0.1:8000/private")
        worker.assert_not_called()
        self.assertIsNone(self.downloader.active_download)

    def test_hostname_resolving_to_private_address_is_rejected(self):
        with mock.patch.object(safe_http.socket, "getaddrinfo", side_effect=private_dns):
            with self.assertRaises(safe_http.BlockedUrlError):
                self.downloader.download("https://internal.example/media")
        self.assertIsNone(self.downloader.active_download)

    def test_non_http_url_is_rejected(self):
        with self.assertRaises(safe_http.BlockedUrlError):
            self.downloader.download("file:///etc/passwd")
        self.assertIsNone(self.downloader.active_download)

    def test_option_like_value_is_not_accepted_as_a_url(self):
        with self.assertRaises(safe_http.BlockedUrlError):
            self.downloader.download("--exec=touch${IFS}/tmp/fxroute-test")
        self.assertIsNone(self.downloader.active_download)

    def test_public_url_is_trimmed_before_download_state(self):
        url = "  https://example.com/audio.mp3  "
        with mock.patch.object(safe_http.socket, "getaddrinfo", side_effect=public_dns), \
                mock.patch.object(self.downloader, "_download_thread"):
            self.downloader.download(url)
        self.assertEqual(self.downloader.active_download["url"], url.strip())

    def test_ytdlp_command_has_option_terminator_before_url(self):
        command = self.downloader._build_ytdlp_command("https://example.com/audio.mp3")
        separator = command.index("--")
        self.assertEqual(command[separator + 1], "https://example.com/audio.mp3")
        self.assertEqual(command.count("--"), 1)


class DownloadApiSecurityTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.original_downloader = main.downloader

    def tearDown(self):
        downloader = main.downloader
        if isinstance(downloader, Downloader):
            downloader._active_download = None
            downloader._worker_thread = None
        main.downloader = self.original_downloader

    def use_real_downloader(self):
        downloader = make_downloader()
        main.downloader = downloader
        return downloader

    def assert_rejected(self, response, expected_status=400):
        self.assertEqual(response.status_code, expected_status, response.text)
        self.assertGreaterEqual(response.status_code, 400)
        self.assertLess(response.status_code, 500)

    def test_missing_url_is_a_client_error(self):
        self.use_real_downloader()
        response = self.client.post("/api/download", json={})
        self.assert_rejected(response)

    def test_invalid_json_is_a_client_error(self):
        self.use_real_downloader()
        response = self.client.post(
            "/api/download",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        self.assert_rejected(response)

    def test_private_literal_is_a_client_error(self):
        downloader = self.use_real_downloader()
        response = self.client.post(
            "/api/download",
            json={"url": "http://192.168.1.10/private.mp3"},
        )
        self.assert_rejected(response)
        self.assertIsNone(downloader.active_download)

    def test_private_dns_target_is_a_client_error(self):
        downloader = self.use_real_downloader()
        with mock.patch.object(safe_http.socket, "getaddrinfo", side_effect=private_dns):
            response = self.client.post(
                "/api/download",
                json={"url": "https://internal.example/media.mp3"},
            )
        self.assert_rejected(response)
        self.assertIsNone(downloader.active_download)

    def test_invalid_scheme_is_a_client_error(self):
        downloader = self.use_real_downloader()
        response = self.client.post(
            "/api/download",
            json={"url": "file:///etc/passwd"},
        )
        self.assert_rejected(response)
        self.assertIsNone(downloader.active_download)

    def test_option_like_url_is_a_client_error(self):
        downloader = self.use_real_downloader()
        response = self.client.post(
            "/api/download",
            json={"url": "--exec=touch${IFS}/tmp/fxroute-test"},
        )
        self.assert_rejected(response)
        self.assertIsNone(downloader.active_download)

    def test_public_url_is_accepted(self):
        downloader = self.use_real_downloader()
        with mock.patch.object(safe_http.socket, "getaddrinfo", side_effect=public_dns), \
                mock.patch.object(downloader, "_download_thread"):
            response = self.client.post(
                "/api/download",
                json={"url": "https://example.com/audio.mp3"},
                headers={"Origin": "http://testserver"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(downloader.active_download["url"], "https://example.com/audio.mp3")

    def test_expected_http_exception_is_not_rewritten_as_500(self):
        class BusyDownloader:
            def download(self, _url):
                raise HTTPException(status_code=409, detail="Download already in progress")

        main.downloader = BusyDownloader()
        response = self.client.post(
            "/api/download",
            json={"url": "https://example.com/audio.mp3"},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "Download already in progress")

    def test_foreign_origin_is_rejected_before_downloader(self):
        calls = []

        class RecordingDownloader:
            def download(self, _url):
                calls.append("download")
                raise AssertionError("cross-origin download reached the handler")

        main.downloader = RecordingDownloader()
        response = self.client.post(
            "/api/download",
            json={"url": "https://example.com/audio.mp3"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
