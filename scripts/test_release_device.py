#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Invoke-time device validation for committed-plan release adapters.

A release adapter pins its device context (output key, channel count,
discovered playback ports) when a job commits. The session release may run
later, after an idle-window device change; rebuilding the pinned graph over
the live selection would then address the wrong device. The adapter must
instead resolve the live selection at invoke time and refuse fail-closed on
any mismatch, before touching the runtime graph.

* ``measurement.release_device.check_release_device`` is the pure shared
  primitive both adapters (AutoSub, Speaker Align) use, so both refuse
  identically.
* ``main._live_release_device_context`` resolves the live selection for the
  production wiring; both ``_create_*_release_adapter`` factories pin the
  resolver so the invoke-time check sees the live truth.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audio.output_service import OutputService, OutputServiceDeps  # noqa: E402
from audio.output_state import default_output_state, set_mode_routing  # noqa: E402
from audio.output_state_store import OutputStateStore  # noqa: E402
from dsp.manager import DSPManager  # noqa: E402

import main  # noqa: E402
from measurement.release_device import check_release_device  # noqa: E402

PINNED_KEY = "dev"
PINNED_CHANNELS = 4
PINNED_PORTS = [f"playback_AUX{i}" for i in range(4)]
RATE = 44100


def live_device(key=PINNED_KEY, channels=PINNED_CHANNELS, ports=None):
    return {"output_key": key, "channels": channels,
            "hardware_ports": list(PINNED_PORTS if ports is None else ports)}


class CheckReleaseDeviceTests(unittest.TestCase):
    def test_matching_live_device_passes(self):
        self.assertIsNone(check_release_device(
            output_key=PINNED_KEY, channels=PINNED_CHANNELS,
            hardware_ports=list(PINNED_PORTS), live=live_device(),
            owner="AutoSub"))

    def test_changed_key_refuses_and_names_the_field(self):
        with self.assertRaisesRegex(RuntimeError, "output_key"):
            check_release_device(
                output_key=PINNED_KEY, channels=PINNED_CHANNELS,
                hardware_ports=list(PINNED_PORTS),
                live=live_device(key="other"), owner="AutoSub")

    def test_changed_channels_refuses(self):
        with self.assertRaisesRegex(RuntimeError, "channels"):
            check_release_device(
                output_key=PINNED_KEY, channels=PINNED_CHANNELS,
                hardware_ports=list(PINNED_PORTS),
                live=live_device(channels=2), owner="Speaker Align")

    def test_changed_ports_refuse(self):
        for ports in (PINNED_PORTS[:2],
                      [f"playback_AUX{i}" for i in (0, 1, 2, 9)],
                      list(reversed(PINNED_PORTS))):
            with self.subTest(ports=ports), self.assertRaisesRegex(
                    RuntimeError, "hardware_ports"):
                check_release_device(
                    output_key=PINNED_KEY, channels=PINNED_CHANNELS,
                    hardware_ports=list(PINNED_PORTS),
                    live=live_device(ports=ports), owner="AutoSub")

    def test_unresolvable_live_device_refuses(self):
        for bad in (None, "dev", 4, {},
                    {"output_key": PINNED_KEY, "channels": PINNED_CHANNELS},
                    {"output_key": "", "channels": PINNED_CHANNELS,
                     "hardware_ports": list(PINNED_PORTS)},
                    {"output_key": PINNED_KEY, "channels": True,
                     "hardware_ports": list(PINNED_PORTS)},
                    {"output_key": PINNED_KEY, "channels": PINNED_CHANNELS,
                     "hardware_ports": "playback_AUX0"}):
            with self.subTest(live=bad), self.assertRaises(RuntimeError):
                check_release_device(
                    output_key=PINNED_KEY, channels=PINNED_CHANNELS,
                    hardware_ports=list(PINNED_PORTS), live=bad,
                    owner="AutoSub")

    def test_refusal_names_every_mismatched_field(self):
        with self.assertRaisesRegex(RuntimeError, "(?s)output_key.*channels.*hardware_ports"):
            check_release_device(
                output_key=PINNED_KEY, channels=PINNED_CHANNELS,
                hardware_ports=list(PINNED_PORTS),
                live={"output_key": "other", "channels": 2,
                      "hardware_ports": []},
                owner="AutoSub")


def overview_for(key="dev", channels=4, ports=None):
    return {"selected_output": {"key": key, "channels": channels},
            "output_mode": {"hardware_playback_ports": list(
                PINNED_PORTS if ports is None else ports)}}


class LiveReleaseDeviceContextTests(unittest.TestCase):
    def test_resolves_live_selection_shape(self):
        with patch.object(main, "get_audio_output_overview",
                          return_value=overview_for()):
            self.assertEqual(main._live_release_device_context(), {
                "output_key": "dev", "channels": 4,
                "hardware_ports": list(PINNED_PORTS)})

    def test_unselected_output_fails_closed(self):
        with patch.object(main, "get_audio_output_overview",
                          return_value={"selected_output": {},
                                        "output_mode": {}}):
            with self.assertRaises(RuntimeError):
                main._live_release_device_context()


class FakeRuntime:
    def __init__(self):
        self.targets = []

    async def sync_rendered(self, target):
        self.targets.append(target)


class ReleaseAdapterWiringTests(unittest.IsolatedAsyncioTestCase):
    """The production factories refuse a device-switched orphan at invoke time."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="release-device-")
        self.addCleanup(directory.cleanup)
        self.manager = DSPManager(home=Path(directory.name) / "home")
        self.store = OutputStateStore(Path(directory.name) / "output-state.json")
        self.service = OutputService(OutputServiceDeps(
            store=self.store, preset_loader=self.manager.preset_store.read,
            resolve_ir=lambda name: (_ for _ in ()).throw(AssertionError(name)),
            measurement_active=lambda: True))
        state = set_mode_routing(default_output_state(), "stereo", "dev",
                                 ["main_l", "main_r", "sub1"])
        self.service.commit(state, expected_revision=0)
        self.runtime = FakeRuntime()
        self.overview = overview_for()
        self.patches = [
            patch.object(main, "get_audio_output_overview",
                         lambda: dict(self.overview)),
            patch.object(main, "_require_dsp_manager",
                         lambda: self.manager),
            patch.object(main.runtime, "dsp_runtime", self.runtime),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_autosub_factory_refuses_switched_device(self):
        adapter = main._create_autosub_release_adapter(
            service=self.service, output_key="dev", channels=4)
        result = await adapter(RATE)
        self.assertEqual(result["sample_rate_hz"], RATE)
        self.assertEqual(len(self.runtime.targets), 1)
        self.overview = overview_for(key="other-dev")
        with self.assertRaisesRegex(RuntimeError, "output_key"):
            await adapter(RATE)
        self.assertEqual(len(self.runtime.targets), 1)

    async def test_speaker_factory_refuses_switched_device(self):
        adapter = main._create_speaker_align_release_adapter(
            service=self.service, output_key="dev", channels=4)
        result = await adapter(RATE)
        self.assertEqual(result["sample_rate_hz"], RATE)
        self.assertEqual(len(self.runtime.targets), 1)
        self.overview = overview_for(
            ports=["playback_AUX0", "playback_AUX1"])
        with self.assertRaisesRegex(RuntimeError, "hardware_ports"):
            await adapter(RATE)
        self.assertEqual(len(self.runtime.targets), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
