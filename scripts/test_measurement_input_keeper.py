#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Input keeper holds the mic source open across a speaker job.

A keeper tap keeps the capture device streaming between way captures so no
suspend/resume cycle can slip the mic timing by an ALSA period mid-run.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.input_keeper import input_keeper_scope, reap_keeper_process

RATE = 48000


class ProcessDouble:
    """pw-record double exposing the lifecycle calls the keeper uses."""

    def __init__(self, *, running=True, ignore_sigterm=False):
        self._running = running
        self._ignore_sigterm = ignore_sigterm
        self.returncode: int | None = None if running else 0
        self.signals: list[str] = []
        self.reaps = 0

    def poll(self):
        return None if self._running else self.returncode

    def terminate(self):
        self.signals.append("terminate")
        if not self._ignore_sigterm:
            self._running = False
            self.returncode = -15

    def kill(self):
        self.signals.append("kill")
        self._running = False
        self.returncode = -9

    def wait(self, timeout=None):
        if self._running:
            raise subprocess.TimeoutExpired("pw-record", timeout)
        self.reaps += 1
        return self.returncode


class KeeperFixture:
    def make_store(self, test):
        from measurement.store import MeasurementStore
        directory = tempfile.TemporaryDirectory(prefix="input-keeper-")
        test.addCleanup(directory.cleanup)
        store = MeasurementStore(home=Path(directory.name))
        store._cached_capture_inputs = lambda: [{
            "id": "mic", "label": "Mic", "node_name": "capture_1",
            "node_serial": "serial-9", "channels": 2, "sample_rate": RATE,
            "measurement_sample_rate": RATE, "available": True,
        }]
        store._measurement_inputs_with_sample_rate = lambda inputs: inputs
        return store

    def wire(self, store, *, verify_result=None, spawn=None):
        """Fake hardware-facing calls; keep store/routing/persistence real."""
        store._routing._list_source_output_ports = lambda node: [f"{node}:capture_FL"]
        store._list_pw_ports = lambda node: (
            [f"{node}:input_FL", f"{node}:input_FR"] if "keeper" in node else [])
        created = []
        store._create_pipewire_link = lambda src, dst: created.append(("link", src, dst))

        def fake_spawn(job_id, command):
            created.append((job_id, list(command)))
            if spawn is not None:
                return spawn(job_id, command)
            return SimpleNamespace(returncode=None, poll=lambda: None,
                                   terminate=lambda: created.append(("terminate", job_id)),
                                   wait=lambda timeout=None: created.append(("reap", job_id)),
                                   communicate=lambda **kw: ("", ""))

        store._start_job_process = fake_spawn
        if verify_result is None:
            store._routing._verify_record_links = lambda expected, **kw: {
                label: 0.01 for _, _, label in expected}
        else:
            store._routing._verify_record_links = lambda expected, **kw: verify_result
        store._routing._cleanup_fxroute_links = lambda **kw: []
        return created


class KeeperReapTests(unittest.TestCase):
    """A stopped keeper must be waited for, not just signalled.

    ``terminate()`` alone leaves the child defunct under the app until it
    exits; one leaked keeper per speaker run is exactly that bug.
    """

    def test_reap_waits_for_a_stopped_process(self):
        process = ProcessDouble(running=False)
        reap_keeper_process(process)
        self.assertEqual(process.reaps, 1)
        self.assertEqual(process.signals, [])

    def test_reap_kills_a_process_that_ignores_sigterm(self):
        process = ProcessDouble(ignore_sigterm=True)
        reap_keeper_process(process)
        self.assertEqual(process.signals, ["kill"])
        self.assertEqual(process.reaps, 1)

    def test_reap_tolerates_objects_without_wait(self):
        reap_keeper_process(SimpleNamespace(returncode=0))


class KeeperLifecycleTests(KeeperFixture, unittest.IsolatedAsyncioTestCase):
    async def test_hold_and_release(self):
        store = self.make_store(self)
        created = self.wire(store)
        async with input_keeper_scope(store, input_id="mic", mic_input_channel="1",
                                      owner="speaker-test") as keeper:
            self.assertTrue(keeper.active)
            self.assertIn("fxroute-input-keeper", keeper.node_name)
            spawn_commands = [cmd for entry in created if len(entry) == 2
                              for owner, cmd in [entry]
                              if isinstance(cmd, list) and cmd[:1] == ["pw-record"]]
            self.assertEqual(len(spawn_commands), 1)
            self.assertIn("pw-record", spawn_commands[0][0])
            owner_id = f"keeper:{keeper.node_name}"
        self.assertFalse(keeper.active)
        self.assertIn(("terminate", owner_id), created)
        self.assertIn(("reap", owner_id), created)

    async def test_hold_and_release_reaps_the_keeper_process(self):
        store = self.make_store(self)
        process = ProcessDouble()
        self.wire(store, spawn=lambda job_id, command: process)
        async with input_keeper_scope(store, input_id="mic", mic_input_channel="1",
                                      owner="speaker-reap"):
            self.assertTrue(process.poll() is None)
        self.assertEqual(process.signals, ["terminate"])
        self.assertEqual(process.reaps, 1)

    async def test_keeper_that_exited_on_its_own_is_still_reaped(self):
        store = self.make_store(self)
        process = ProcessDouble(running=False)
        self.wire(store, spawn=lambda job_id, command: process)
        async with input_keeper_scope(store, input_id="mic", mic_input_channel="1",
                                      owner="speaker-reap"):
            pass
        self.assertEqual(process.signals, [])
        self.assertEqual(process.reaps, 1)

    async def test_unknown_input_fails_before_spawn(self):
        store = self.make_store(self)
        created = self.wire(store)
        with self.assertRaisesRegex(ValueError, "available"):
            async with input_keeper_scope(store, input_id="nope", mic_input_channel="1",
                                          owner="speaker-test"):
                pass
        self.assertEqual(created, [])

    async def test_unverifiable_link_kills_process_and_raises(self):
        store = self.make_store(self)
        created = self.wire(store, verify_result={"keeper-mic-to-record": None})
        with self.assertRaisesRegex(RuntimeError, "keeper"):
            async with input_keeper_scope(store, input_id="mic", mic_input_channel="1",
                                          owner="speaker-test"):
                pass
        terminates = [entry for entry in created if entry[0] == "terminate"]
        self.assertEqual(len(terminates), 1)

    async def test_blank_channel_defaults_to_first(self):
        store = self.make_store(self)
        self.wire(store)
        async with input_keeper_scope(store, input_id="mic", mic_input_channel="",
                                      owner="speaker-test"):
            pass


if __name__ == "__main__":
    unittest.main()
