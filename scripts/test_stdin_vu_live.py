#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in live STDIN waiting VU regression on the audio host.

Run with FXROUTE_STDIN_LIVE=1 using the product's Python environment.
Starts only from active radio or fully idle app playback; other contexts
are skipped before mutation to preserve paused and external transports.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import threading
import time
import unittest
import urllib.request


BASE_URL = os.environ.get("FXROUTE_BASE_URL", "http://127.0.0.1:8000")
RUN_LIVE = os.environ.get("FXROUTE_STDIN_LIVE") == "1"


def _http(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


@unittest.skipUnless(RUN_LIVE, "live STDIN VU acceptance needs FXROUTE_STDIN_LIVE=1 on the audio host")
class StdinWaitingVuLiveTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(shutil.which("fxroute"))
        self.saved_source = _http("GET", "/api/audio/source-mode")
        self.saved_status = _http("GET", "/api/status")
        track = self.saved_status.get("current_track") or {}
        active_radio = (track.get("source") == "radio"
                        and self.saved_status.get("playing")
                        and not self.saved_status.get("paused"))
        idle_player = (not track and not self.saved_status.get("current_file")
                       and not self.saved_status.get("playing"))
        external_playing = any(
            _http("GET", f"/api/streaming/{provider}/status").get("status") == "Playing"
            for provider in ("spotify", "qobuz")
        )
        power = _http("GET", "/api/power/state")
        if (self.saved_source.get("mode") != "app-playback"
                or not (active_radio or idle_player) or external_playing
                or self.saved_source["stdin"].get("session_id")
                or self.saved_source["stdin"].get("measurement_active")
                or power.get("measurement_window_open")):
            self.skipTest("requires active radio or fully idle app playback without external activity")
        self.addCleanup(self._restore)
        _http("POST", "/api/audio/source-mode", {"mode": "stdin-input"})

    def _restore(self):
        payload = {"mode": self.saved_source["mode"]}
        selected = (self.saved_source.get("selected_input") or {}).get("key")
        if selected:
            payload["inputKey"] = selected
        _http("POST", "/api/audio/source-mode", payload)
        track = self.saved_status.get("current_track") or {}
        if self.saved_status.get("playing") and track.get("source") in {"radio", "local"}:
            track_id = track["id"]
            if track["source"] == "radio":
                track_id = track_id.removeprefix("radio_")
            _http("POST", "/api/play", {"source": track["source"], "track_id": track_id})

    def _read(self):
        stdin = _http("GET", "/api/audio/source-mode")["stdin"]
        peak = _http("GET", "/api/status")["output_peak_warning"]
        return stdin, peak

    def _await(self, state, timeout=15):
        deadline = time.monotonic() + timeout
        while True:
            stdin, peak = self._read()
            if (stdin["state"] == state and peak["available"]
                    and peak["vu_fresh"] and peak["vu_db"] is not None):
                return stdin, peak
            if time.monotonic() >= deadline:
                self.fail(f"No fresh VU in {state}: stdin={stdin}, peak={peak}")
            time.sleep(0.2)

    def _assert_waiting_window(self, label):
        self._await("waiting")
        for _ in range(12):
            stdin, peak = self._read()
            print(json.dumps({"label": label, "stdin": stdin, "peak": peak}), flush=True)
            self.assertEqual(stdin["state"], "waiting")
            self.assertFalse(stdin["routed"])
            self.assertIsNone(stdin["session_id"])
            self.assertTrue(peak["available"])
            self.assertTrue(peak["vu_fresh"])
            self.assertIsNotNone(peak["vu_db"])
            power = _http("GET", "/api/power/state")
            self.assertFalse(power["amp_should_be_on"], power)
            self.assertEqual(power["reason"], "idle")
            time.sleep(0.5)

    def test_normal_writer_eof_restores_waiting_vu_twice(self):
        self._assert_waiting_window("initial-waiting")
        frames = []
        for n in range(4800):
            sample = int(300 * math.sin(2 * math.pi * 440 * n / 48000))
            frames.append(struct.pack("<hh", sample, sample))
        chunk = b"".join(frames)
        for cycle in range(2):
            with self.subTest(cycle=cycle):
                writer = subprocess.Popen(
                    ["fxroute", "stdin", "--format", "s16le", "--rate", "48000", "--channels", "2"],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                )
                errors = []

                def feed():
                    try:
                        for _ in range(40):
                            writer.stdin.write(chunk)
                            writer.stdin.flush()
                            time.sleep(0.1)
                    except OSError as exc:
                        errors.append(str(exc))
                    finally:
                        writer.stdin.close()

                producer = threading.Thread(target=feed)
                producer.start()
                try:
                    stdin, peak = self._await("streaming")
                    print(json.dumps({"label": f"stream-{cycle}", "stdin": stdin, "peak": peak}), flush=True)
                    self.assertTrue(stdin["routed"])
                    producer.join(timeout=10)
                    self.assertFalse(producer.is_alive())
                    self.assertEqual(writer.wait(timeout=15), 0, writer.stderr.read().decode())
                    self.assertEqual(errors, [])
                    self._assert_waiting_window(f"after-eof-{cycle}")
                finally:
                    if writer.poll() is None:
                        writer.kill()
                    writer.wait(timeout=5)
                    producer.join(timeout=5)
                    writer.stderr.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
