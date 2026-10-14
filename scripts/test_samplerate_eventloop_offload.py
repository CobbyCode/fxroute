#!/usr/bin/env python3
"""B3: blocking PipeWire reads must not run on the asyncio event loop.

Covers:
* static regression: no async function calls get_samplerate_status or
  get_audio_output_overview except as an asyncio.to_thread argument;
* liveness: the peak-monitor rate gate stays responsive while the
  samplerate status read blocks in a worker thread.
"""

import ast
import asyncio
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dsp.peak_monitor as peak_monitor

BLOCKING_NAMES = {"get_samplerate_status", "get_audio_output_overview"}
SCANNED_FILES = [
    "main.py",
    "dsp/peak_monitor.py",
    "audio/samplerate/alignment.py",
    "playback/runtime/output_mode.py",
]


def _direct_blocking_calls(path):
    """Async functions calling PipeWire readers without to_thread offload."""
    text = (ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    # Parent map for ancestor checks (to_thread argument detection).
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    blocking_call_ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = ""
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name in BLOCKING_NAMES:
            blocking_call_ids.add(id(node))
    to_thread_covered = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_to_thread = (
            (isinstance(func, ast.Attribute) and func.attr == "to_thread")
            or (isinstance(func, ast.Name) and func.id == "to_thread")
        )
        if not is_to_thread:
            continue
        for arg in node.args:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Call):
                    to_thread_covered.add(id(sub))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call) or id(sub) not in blocking_call_ids:
                continue
            if id(sub) in to_thread_covered:
                continue
            offenders.append(f"{path}:{sub.lineno} in async {node.name}")
    return offenders


class EventLoopOffloadStaticTests(unittest.TestCase):
    def test_no_direct_pipewire_reads_on_event_loop(self):
        offenders = []
        for path in SCANNED_FILES:
            offenders.extend(_direct_blocking_calls(path))
        self.assertEqual(offenders, [], f"blocking calls on event loop: {offenders}")


class PeakMonitorLivenessTests(unittest.TestCase):
    def test_rate_gate_stays_responsive_while_status_blocks(self):
        entered = threading.Event()
        release = threading.Event()

        def blocking_status():
            entered.set()
            self.assertTrue(release.wait(10.0), "status gate never released")
            return {"active_rate": 48000, "force_rate": 48000}

        async def run():
            monitor = peak_monitor.DSPPeakMonitor.__new__(peak_monitor.DSPPeakMonitor)
            ticks = 0
            with patch.object(peak_monitor, "get_samplerate_status", side_effect=blocking_status):
                gate = asyncio.create_task(monitor._rate_change_in_progress())
                self.assertTrue(await asyncio.to_thread(entered.wait, 10.0))
                deadline = time.monotonic() + 2.0
                while not gate.done() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                    ticks += 1
                release.set()
                result = await asyncio.wait_for(gate, 10.0)
            return ticks, result

        ticks, result = asyncio.run(run())
        self.assertGreaterEqual(ticks, 5, "event loop was blocked by the status read")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
