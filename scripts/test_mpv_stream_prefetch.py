#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""The production MPV options must open a queued HTTP stream before EOF."""

import io
import json
import math
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playback.player import MPVWrapper


@unittest.skipUnless(shutil.which("mpv"), "mpv binary is not installed")
class MPVStreamPrefetchTests(unittest.TestCase):
    def test_queued_http_stream_opens_while_current_track_is_still_playing(self):
        next_requested = threading.Event()
        wav = io.BytesIO()
        with wave.open(wav, "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(44100)
            pcm = b"".join(struct.pack("<hh", sample, sample) for sample in
                           (int(math.sin(index * 2 * math.pi * 440 / 44100) * 4000)
                            for index in range(10 * 44100)))
            handle.writeframes(pcm)
        media = wav.getvalue()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path == "/next.wav":
                    next_requested.set()
                    time.sleep(.35)
                start = 0
                range_header = self.headers.get("Range")
                if range_header:
                    start = int(range_header.split("=", 1)[1].split("-", 1)[0])
                    if start >= len(media):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(media)}")
                        self.end_headers()
                        return
                self.send_response(206 if range_header else 200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(media) - start))
                if range_header:
                    self.send_header("Content-Range", f"bytes {start}-{len(media)-1}/{len(media)}")
                self.end_headers()
                try:
                    self.wfile.write(media[start:])
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        with tempfile.TemporaryDirectory(prefix="fxroute-mpv-prefetch-") as directory:
            ipc_path = str(Path(directory) / "mpv.sock")
            # Use the production options, replacing only the hardware output
            # with a timed null output in this isolated MPV process.
            command = MPVWrapper._mpv_command(ipc_path)
            command = [option for option in command if not option.startswith("--audio-device=")]
            proc = subprocess.Popen(command + ["--no-config", "--ao=null"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while not Path(ipc_path).exists():
                    self.assertIsNone(proc.poll(), "Isolated MPV exited before IPC readiness")
                    self.assertLess(time.monotonic(), deadline, "MPV IPC readiness timed out")
                    time.sleep(.01)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.connect(ipc_path)
                    sock.settimeout(5)
                    reader = sock.makefile("rb")

                    def ipc(args, request_id):
                        sock.sendall((json.dumps({"command": args, "request_id": request_id}) + "\n").encode())
                        while True:
                            message = json.loads(reader.readline())
                            if message.get("request_id") == request_id:
                                self.assertEqual(message.get("error"), "success")
                                return message.get("data")

                    ipc(["loadfile", url + "/current.wav", "replace"], 1)
                    ipc(["loadfile", url + "/next.wav", "append"], 2)
                    self.assertTrue(next_requested.wait(6),
                                    "Queued HTTP stream must open before the current track reaches EOF")
                    self.assertEqual(ipc(["get_property", "path"], 3), url + "/current.wav")
                    self.assertLess(ipc(["get_property", "time-pos"], 4), 8)
            finally:
                proc.terminate()
                proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
