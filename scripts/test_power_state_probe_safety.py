# SPDX-License-Identifier: AGPL-3.0-only
"""Power-state probe safety: offloaded Bluetooth read, live external link.

The served ``GET /api/power/state`` path must not block the event loop on
a slow ``wpctl`` Bluetooth probe, and the external-input hint must follow
the loopback link present in the live graph instead of the stored source
name.
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402


def _base_patches(*, bt_source=None, ext_source=None, heartbeat=0.0, external_extra=None):
    return (
        mock.patch.object(main.runtime, "player_instance", None),
        mock.patch.object(main.playback_state, "latest_spotify_state", {}),
        mock.patch.object(main.playback_state, "latest_qobuz_state", {}),
        mock.patch.object(main, "last_measurement_window_seen_at", heartbeat),
        mock.patch.object(main, "bluetooth_input", SimpleNamespace(input_source_name=bt_source)),
        mock.patch.object(main, "external_input", SimpleNamespace(
            loopback_source_name=ext_source, **(external_extra or {}))),
    )


class PowerProbeOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_bluetooth_probe_does_not_block_event_loop(self):
        def slow_blocking_probe(source_name):
            time.sleep(1.0)
            return True

        patches = _base_patches(bt_source="bluez_input.11_22_33_44_55_66.1")
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             mock.patch.object(main, "is_bluetooth_audio_streaming", side_effect=slow_blocking_probe):
            tick_times = []

            async def ticker():
                while True:
                    await asyncio.sleep(0.05)
                    tick_times.append(time.monotonic())

            ticker_task = asyncio.create_task(ticker())
            try:
                await asyncio.sleep(0.2)
                del tick_times[:]
                start = time.monotonic()
                payload = await main.get_power_state()
                end = time.monotonic()
            finally:
                ticker_task.cancel()
                try:
                    await ticker_task
                except asyncio.CancelledError:
                    pass
            in_window = [t for t in tick_times if start <= t <= end]
            self.assertTrue(payload["amp_should_be_on"])
            self.assertEqual(payload["reason"], "bluetooth")
            self.assertGreaterEqual(len(in_window), 5)

    async def test_probe_error_stays_fail_closed(self):
        patches = _base_patches(bt_source="bluez_input.11_22_33_44_55_66.1")
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             mock.patch.object(main, "is_bluetooth_audio_streaming", side_effect=RuntimeError("wpctl wedged")):
            payload = await main.get_power_state()
            self.assertFalse(payload["amp_should_be_on"])
            self.assertEqual(payload["reason"], "idle")


MAPPED_LINKS = (
    ("alsa_input.scarlett:capture_AUX2", "fxroute_dsp:input_1"),
    ("alsa_input.scarlett:capture_AUX3", "fxroute_dsp:input_2"),
    ("alsa_input.scarlett:capture_AUX4", "fxroute_dsp:input_3"),
)
MAPPED_EXTRA = {"mapped_source_name": "alsa_input.scarlett", "mapped_ingress_links": MAPPED_LINKS}


class MappedIngressLivenessTests(unittest.IsolatedAsyncioTestCase):
    """Mapped Surround ingress has no sink links: the hint follows its own edges."""

    async def _state(self, links_present):
        patches = _base_patches(external_extra=MAPPED_EXTRA)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             mock.patch.object(main, "is_bluetooth_audio_streaming", return_value=False), \
             mock.patch.object(main, "ingress_links_present", new=links_present) as links, \
             mock.patch.object(main, "input_links_present", new=mock.AsyncMock(
                 side_effect=AssertionError("sink links are not part of the mapped ingress"))):
            payload = await main.get_power_state()
            return payload, links

    async def test_present_links_keep_amp_hint_on(self):
        payload, links = await self._state(mock.AsyncMock(return_value=True))
        self.assertTrue(payload["amp_should_be_on"])
        self.assertEqual(payload["reason"], "external-input")
        links.assert_awaited_once_with(MAPPED_LINKS)

    async def test_missing_links_keep_amp_hint_off(self):
        payload, _ = await self._state(mock.AsyncMock(return_value=False))
        self.assertFalse(payload["amp_should_be_on"])
        self.assertEqual(payload["reason"], "idle")

    async def test_link_check_error_stays_fail_closed(self):
        payload, _ = await self._state(mock.AsyncMock(side_effect=RuntimeError("pw-link down")))
        self.assertFalse(payload["amp_should_be_on"])
        self.assertEqual(payload["reason"], "idle")

    async def test_no_mapped_ingress_state_leaves_other_sources_to_decide(self):
        patches = _base_patches(external_extra={"mapped_source_name": None, "mapped_ingress_links": ()})
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             mock.patch.object(main, "is_bluetooth_audio_streaming", return_value=False), \
             mock.patch.object(main, "ingress_links_present", new=mock.AsyncMock(
                 side_effect=AssertionError("no mapped links to check"))):
            payload = await main.get_power_state()
        self.assertEqual(payload["reason"], "idle")


class ExternalLinkLivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_link_keeps_amp_hint_off(self):
        patches = _base_patches(ext_source="alsa_input.scarlett")
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             mock.patch.object(main, "is_bluetooth_audio_streaming", return_value=False), \
             mock.patch.object(main, "input_links_present", new=mock.AsyncMock(return_value=False)):
            payload = await main.get_power_state()
            self.assertFalse(payload["amp_should_be_on"])
            self.assertEqual(payload["reason"], "idle")

    async def test_present_link_keeps_amp_hint_on(self):
        patches = _base_patches(ext_source="alsa_input.scarlett")
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             mock.patch.object(main, "is_bluetooth_audio_streaming", return_value=False), \
             mock.patch.object(main, "input_links_present", new=mock.AsyncMock(return_value=True)) as links:
            payload = await main.get_power_state()
            self.assertTrue(payload["amp_should_be_on"])
            self.assertEqual(payload["reason"], "external-input")
            links.assert_awaited_once()

    async def test_link_check_error_stays_fail_closed(self):
        patches = _base_patches(ext_source="alsa_input.scarlett")
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             mock.patch.object(main, "is_bluetooth_audio_streaming", return_value=False), \
             mock.patch.object(main, "input_links_present", new=mock.AsyncMock(side_effect=RuntimeError("pw-link down"))):
            payload = await main.get_power_state()
            self.assertFalse(payload["amp_should_be_on"])
            self.assertEqual(payload["reason"], "idle")

    async def test_measurement_window_skips_line_source_probe(self):
        patches = _base_patches(
            bt_source="bluez_input.11_22_33_44_55_66.1",
            ext_source="alsa_input.scarlett",
            heartbeat=9e9,
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             mock.patch.object(main, "is_bluetooth_audio_streaming") as probe, \
             mock.patch.object(main, "input_links_present", new=mock.AsyncMock()) as links:
            payload = await main.get_power_state()
            self.assertTrue(payload["amp_should_be_on"])
            self.assertEqual(payload["reason"], "measurement_window")
            probe.assert_not_called()
            links.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
