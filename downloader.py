"""YouTube audio downloader using yt-dlp with progress tracking."""

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from config import get_settings

logger = logging.getLogger(__name__)


class Downloader:
    """Manages audio downloads from YouTube URLs using yt-dlp."""

    def __init__(self):
        self.settings = get_settings()
        self.download_dir: Path = self.settings.download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)

        self._active_download: Optional[Dict] = None
        self._lock = threading.RLock()
        self._cancel_requested = False
        self._callbacks = []
        self._callback_loop = None

        # Verify yt-dlp exists
        self._verify_ytdlp()

    def _ytdlp_bin(self) -> str:
        """Get path to yt-dlp binary, preferring newer user-local installs."""
        candidates = [
            Path.home() / ".local/bin/yt-dlp",
            Path(sys.executable).parent / "yt-dlp",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return "yt-dlp"

    def _verify_ytdlp(self):
        """Check that yt-dlp is installed and accessible."""
        try:
            result = subprocess.run(
                [self._ytdlp_bin(), "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                logger.info(f"Found yt-dlp: {result.stdout.strip()}")
            else:
                raise RuntimeError(f"yt-dlp check failed: {result.stderr}")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.error("yt-dlp is not installed or not in PATH")
            raise RuntimeError(
                "yt-dlp is required for downloads. "
                "Install with: pip install yt-dlp (or apt install yt-dlp)"
            ) from e

    def download(self, url: str) -> str:
        """
        Start download of a YouTube URL.
        Returns the expected output filename.
        Raises RuntimeError if another download is active or yt-dlp missing.
        """
        with self._lock:
            if self._active_download:
                raise RuntimeError("Download already in progress")

            self._cancel_requested = False
            self._active_download = {
                "url": url,
                "status": "starting",
                "progress_percent": 0.0,
                "filename": None,
                "started_at": datetime.now(),
                "error": None,
                "status_text": "Preparing download…",
            }

            # Strip playlist params from YouTube URLs
            url = self._clean_youtube_url(url)

            # Start download thread
            thread = threading.Thread(target=self._download_thread, args=(url,), daemon=True)
            thread.start()

            filename = self._get_output_filename(url)
            return filename

    def _clean_youtube_url(self, url: str) -> str:
        """Remove playlist/queue params from YouTube URLs to get single video."""
        import urllib.parse
        if "youtube.com" in url or "youtu.be" in url:
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)
            if "v" in params:
                clean = urllib.parse.urlencode({"v": params["v"][0]})
                return urllib.parse.urlunparse(parsed._replace(query=clean))
            if "youtu.be" in url:
                return urllib.parse.urlunparse(parsed._replace(query="", fragment=""))
        return url

    def _get_output_filename(self, url: str) -> str:
        """Generate expected output filename."""
        # Use yt-dlp's default template: %(title)s.%(ext)s
        # We'll get the actual filename from yt-dlp output
        return f"download_{int(time.time())}"

    def _download_thread(self, url: str):
        """Background thread executing yt-dlp."""
        try:
            # Construct output template
            output_template = str(self.download_dir / "%(title)s.%(ext)s")

            # yt-dlp command: prefer native audio downloads and keep the source format whenever possible.
            # Optional transcoding can still be requested explicitly through DOWNLOAD_TRANSCODE_FORMAT.
            cmd = [
                self._ytdlp_bin(),
                "-f", "bestaudio/best",
                "-o", output_template,
                "--progress",
                "--newline",
                "--no-playlist",
                url,
            ]

            transcode_format = self.settings.download_transcode_format
            if transcode_format:
                cmd[1:1] = ["-x", "--audio-format", transcode_format, "--audio-quality", "0"]
                cmd.extend(["--postprocessor-args", "ffmpeg:-nostats -loglevel error"])

            logger.info(f"Starting yt-dlp: {' '.join(cmd)}")
            self._update_status("downloading", 0.0)

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )

            # Parse output for progress and filename
            filename = None
            progress_re = re.compile(r'\[download\]\s+([\d.]+)%')
            dest_re = re.compile(r'Destination:\s+(.+)')
            error_line = None

            def handle_output_line(line: str):
                nonlocal filename, error_line
                line = line.strip()
                if not line:
                    return
                logger.info(f"yt-dlp: {line}")
                with self._lock:
                    if self._active_download:
                        self._active_download["status_text"] = line
                dest_match = dest_re.search(line)
                if dest_match:
                    filename = os.path.basename(dest_match.group(1))
                    with self._lock:
                        self._active_download["filename"] = filename
                        self._active_download["status_text"] = f"Saving as {filename}"
                progress_match = progress_re.search(line)
                if progress_match:
                    progress = float(progress_match.group(1))
                    with self._lock:
                        self._active_download["progress_percent"] = progress
                        self._active_download["status_text"] = f"Downloading… {progress:.1f}%"
                if "ERROR:" in line:
                    error_line = line.split("ERROR:", 1)[1].strip() or line
                elif line.startswith("WARNING:") and error_line is None:
                    error_line = line
                self._notify_callbacks()

            # Read both stdout and stderr without blocking on carriage-return progress output
            import select
            streams = {}
            buffers = {}
            if process.stdout:
                streams[process.stdout.fileno()] = process.stdout
                buffers[process.stdout.fileno()] = ""
            if process.stderr:
                streams[process.stderr.fileno()] = process.stderr
                buffers[process.stderr.fileno()] = ""
            while streams:
                if self._cancel_requested:
                    process.terminate()
                    break
                readable, _, _ = select.select(list(streams.keys()), [], [], 1.0)
                if not readable and process.poll() is not None:
                    break
                for fd in readable:
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        remainder = buffers.pop(fd, "").strip()
                        if remainder:
                            handle_output_line(remainder)
                        streams.pop(fd, None)
                        continue
                    text_chunk = chunk.decode(errors="replace").replace("\r", "\n")
                    buffers[fd] = buffers.get(fd, "") + text_chunk
                    parts = buffers[fd].split("\n")
                    buffers[fd] = parts.pop() if parts else ""
                    for part in parts:
                        handle_output_line(part)

            for fd, remainder in list(buffers.items()):
                remainder = remainder.strip()
                if remainder:
                    handle_output_line(remainder)

            process.wait()

            if self._cancel_requested:
                self._update_status("cancelled", None)
                logger.info("Download cancelled")
            elif process.returncode == 0:
                self._set_status_text(f"Download complete: {filename or 'file saved'}")
                self._update_status("complete", 100.0)
                logger.info(f"Download complete: {filename}")
                self._notify_complete(filename)
            else:
                error_msg = error_line or f"yt-dlp exited with code {process.returncode}"
                self._update_status("error", None, error=error_msg)
                logger.error(error_msg)

        except Exception as e:
            logger.error(f"Download thread error: {e}")
            self._update_status("error", None, error=str(e))
        finally:
            # Keep final state for a while before clearing
            time.sleep(2)
            with self._lock:
                self._active_download = None
                self._cancel_requested = False

    def _update_status(self, status: str, progress: Optional[float], error: Optional[str] = None):
        """Update active download status."""
        with self._lock:
            if self._active_download:
                self._active_download["status"] = status
                if progress is not None:
                    self._active_download["progress_percent"] = progress
                if error:
                    self._active_download["error"] = error
                self._notify_callbacks()

    def _set_status_text(self, text: str):
        with self._lock:
            if self._active_download:
                self._active_download["status_text"] = text
                self._notify_callbacks()

    def _notify_callbacks(self):
        """Notify registered callbacks with current download state."""
        if self._active_download:
            state = self._active_download.copy()
            for callback in self._callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        loop = self._callback_loop
                        if loop is None:
                            logger.warning("No callback loop set for async download callback")
                            continue
                        asyncio.run_coroutine_threadsafe(callback(state), loop)
                    else:
                        callback(state)
                except Exception as e:
                    logger.error(f"Download callback error: {e}")

    def _notify_complete(self, filename: str):
        """Notify completion with final filename."""
        # Could trigger library refresh, playback, etc.
        logger.info(f"Download completed: {filename}")

    def register_callback(self, callback, loop=None):
        """Register a callback for download state changes."""
        self._callbacks.append(callback)
        if loop is not None:
            self._callback_loop = loop

    def cancel(self):
        """Cancel the active download."""
        with self._lock:
            if self._active_download:
                self._cancel_requested = True
                logger.info("Download cancel requested")

    @property
    def active_download(self) -> Optional[Dict]:
        """Get current active download state."""
        with self._lock:
            return self._active_download.copy() if self._active_download else None

    @property
    def download_dir_exists(self) -> bool:
        """Check if download directory exists and is writable."""
        try:
            return self.download_dir.exists() and os.access(self.download_dir, os.W_OK)
        except:
            return False
