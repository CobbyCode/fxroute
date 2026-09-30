#!/usr/bin/env python3
"""Exercise the real CLI against a local socket, including idle stdin."""

import json
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "stdin.sock")
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(self.path)
        self.listener.listen()
        self.listener.settimeout(3)
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=3)
        self.listener.close()
        self.temp.cleanup()

    def client(self, *extra):
        child = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts/fxroute_stdin.py"), "stdin",
             "--socket", self.path, "--format", "s24le", "--rate", "48000",
             "--channels", "8", "--left", "5", "--right", "6", *extra],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.children.append(child)
        return child

    def test_real_pipe_roundtrip_is_byte_exact_and_stdout_empty(self):
        received = bytearray()
        headers = []
        errors = []

        def receive():
            try:
                with self.listener.accept()[0] as peer:
                    peer.settimeout(3)
                    stream = peer.makefile("rb")
                    headers.append(json.loads(stream.readline()))
                    peer.sendall(b'{"event":"accepted","session_id":"abc"}\n')
                    peer.sendall(b'{"event":"ready"}\n')
                    received.extend(stream.read())
                    peer.sendall(b'{"event":"done"}\n')
                    stream.close()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=receive)
        thread.start()
        child = self.client()
        payload = bytes(range(256)) * 768
        stdout, stderr = child.communicate(payload, timeout=5)
        thread.join(timeout=4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(child.returncode, 0, stderr.decode())
        self.assertEqual(bytes(received), payload)
        self.assertEqual(headers[0]["channels"], 8)
        self.assertEqual(headers[0]["left"], 5)
        self.assertEqual(stdout, b"")

    def test_server_error_interrupts_idle_stdin_before_and_after_ready(self):
        for ready in (False, True):
            with self.subTest(ready=ready):
                child = self.client()
                with self.listener.accept()[0] as peer:
                    peer.settimeout(2)
                    stream = peer.makefile("rb")
                    json.loads(stream.readline())
                    peer.sendall(b'{"event":"accepted","session_id":"abc"}\n')
                    if ready:
                        peer.sendall(b'{"event":"ready"}\n')
                    peer.sendall(b'{"event":"error","code":"source-changed","message":"Source changed"}\n')
                    self.assertEqual(child.wait(timeout=2), 1)
                    stream.close()
                stdout, stderr = child.communicate(timeout=2)
                self.assertEqual(stdout, b"")
                self.assertIn(b"Source changed", stderr)

    def test_no_pcm_before_ready_and_signal_interrupts_wait(self):
        child = self.client()
        with self.listener.accept()[0] as peer:
            peer.settimeout(2)
            stream = peer.makefile("rb")
            json.loads(stream.readline())
            peer.sendall(b'{"event":"accepted","session_id":"abc"}\n')
            child.stdin.write(b"audio")
            child.stdin.flush()
            peer.settimeout(0.1)
            with self.assertRaises(socket.timeout):
                peer.recv(1)
            child.send_signal(signal.SIGTERM)
            self.assertEqual(child.wait(timeout=2), 143)
            stream.close()

    def test_missing_service_and_invalid_metadata_fail_cleanly(self):
        child = self.client("--socket", self.path + ".missing")
        stdout, stderr = child.communicate(timeout=3)
        self.assertEqual(child.returncode, 1)
        self.assertNotIn(b"Traceback", stderr)
        self.assertEqual(stdout, b"")
        child = self.client("--channels", "33")
        child.communicate(timeout=3)
        self.assertEqual(child.returncode, 2)


if __name__ == "__main__":
    unittest.main()
