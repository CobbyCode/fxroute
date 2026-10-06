#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Multichannel output meter: speaker positions and the peak monitor's output tap."""

import asyncio
import struct
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dsp.peak_monitor as peak_monitor
from dsp.output_meter import MIN_MULTICHANNEL_POSITIONS, meter_positions
from dsp.peak_monitor import DSPPeakMonitor, MonitorTarget

TARGET = MonitorTarget("fxroute_dsp", 42, "Output Level")
SURROUND_6 = ("surround", ("main_l", "main_r", "rear_l", "rear_r", "sub1", "sub2"))
STEREO = ("stereo", ("main_l", "main_r"))


def _keys(mode, roles):
    return [position.key for position in meter_positions(mode, roles)]


class MeterPositionTests(unittest.TestCase):
    def test_full_surround_reduces_to_eight_positions_in_smpte_order(self):
        roles = ("main_l", "main_r", "center", "side_l", "side_r", "rear_l", "rear_r", "sub1", "sub2")
        positions = meter_positions("surround", roles)
        self.assertEqual([p.key for p in positions],
                         ["fl", "fr", "c", "lfe", "sl", "sr", "rl", "rr"])
        self.assertEqual(dict((p.key, p.outputs) for p in positions)["lfe"], (7, 8))

    def test_surround_without_center_and_side_shows_only_present_speakers(self):
        positions = meter_positions(*SURROUND_6)
        self.assertEqual([p.key for p in positions], ["fl", "fr", "lfe", "rl", "rr"])
        self.assertEqual(positions[3].outputs, (2,))

    def test_surround_crossover_ways_meter_as_their_front_side(self):
        roles = ("left_low", "left_high", "right_low", "right_high",
                 "center", "side_l", "side_r", "rear_l", "rear_r", "sub1", "sub2")
        positions = meter_positions("surround", roles)
        self.assertEqual(len(positions), 8)
        self.assertEqual(positions[0].outputs, (0, 1))
        self.assertEqual(positions[1].outputs, (2, 3))

    def test_stereo_sub_pair_stays_separate_and_dual_mono_shares_one_meter(self):
        self.assertEqual(_keys("stereo-sub", ("main_l", "main_r", "sub_l", "sub_r")),
                         ["l", "r", "sub_l", "sub_r"])
        self.assertEqual(_keys("stereo-sub", ("main_l", "main_r", "sub1", "sub2")), ["l", "r", "sub"])
        self.assertEqual(_keys("stereo-sub", ("main_l", "main_r", "sub1")), ["l", "r", "sub"])

    def test_plain_stereo_and_stereo_crossover_stay_below_the_multichannel_threshold(self):
        self.assertLess(len(meter_positions(*STEREO)), MIN_MULTICHANNEL_POSITIONS)
        ways = ("left_low", "left_mid", "left_high", "right_low", "right_mid", "right_high")
        self.assertLess(len(meter_positions("stereo", ways)), MIN_MULTICHANNEL_POSITIONS)

    def test_unknown_role_disables_the_multichannel_meter(self):
        self.assertEqual(meter_positions("stereo", ("FL", "FR", "SUB1", "SUB2")), ())
        self.assertEqual(meter_positions("surround", ("main_l", "main_r", "sub_l")), ())


class _FakeProc:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None

    def terminate(self):
        self.returncode = 0

    async def wait(self):
        return self.returncode

    async def communicate(self):
        return (b"", b"")


def _frames(*channel_values, count=64):
    return struct.pack(f"<{len(channel_values)}f", *channel_values) * count


def _au_header(channels, rate=44100):
    """The 24-byte header pw-record writes before the samples (real capture:
    64 6e 73 2e 18 00 00 00 ff ff ff ff 06 00 00 00 44 ac 00 00 04 00 00 00)."""
    return b"dns." + struct.pack("<5I", 24, 0xFFFFFFFF, 6, rate, channels)


class _TapCaptureCase(unittest.IsolatedAsyncioTestCase):
    layout = SURROUND_6

    async def asyncSetUp(self):
        self.live_layout = self.layout
        self.monitor = DSPPeakMonitor(get_output_layout=lambda: self.live_layout)
        self.monitor._running = True
        self.monitor._target = TARGET
        self.proc = _FakeProc()
        self.spawn = AsyncMock(return_value=self.proc)
        self.patchers = [
            patch("dsp.peak_monitor.asyncio.create_subprocess_exec", new=self.spawn),
            patch.object(self.monitor, "_link_capture_stream", new=AsyncMock()),
            patch("dsp.peak_monitor.get_samplerate_status", return_value={"force_rate": 48000}),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.task = asyncio.create_task(self.monitor._capture_target(TARGET))
        await self._wait_for(lambda: self.monitor._proc is self.proc)

    async def asyncTearDown(self):
        for patcher in self.patchers:
            patcher.stop()
        self.monitor._running = False
        self.proc.returncode = 0
        self.proc.stdout.feed_eof()
        self.proc.stderr.feed_eof()
        if not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, RuntimeError):
                pass

    async def _wait_for(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.005)
        raise AssertionError("condition not met")

    async def _feed(self, chunk):
        fed_at = time.monotonic()
        self.proc.stdout.feed_data(chunk)
        await self._wait_for(lambda: (self.monitor._last_audio_sample_at or 0) >= fed_at - 0.001)

    def command(self):
        return list(self.spawn.call_args.args)


class MultichannelCaptureTests(_TapCaptureCase):
    async def test_capture_maps_post_effect_pair_then_one_channel_per_output(self):
        cmd = self.command()
        # Only AUX positions: named FL/FR next to AUXn makes the AUX channels silent.
        self.assertEqual(cmd[cmd.index("--channel-map") + 1], "AUX0,AUX1,AUX2,AUX3,AUX4,AUX5,AUX6,AUX7")
        self.assertNotIn("--channels", cmd)
        self.assertEqual(self.monitor._frame_bytes, 4 * 8)

    async def test_levels_follow_positions_and_lfe_takes_the_louder_sub(self):
        self.monitor._tap.linked = True
        # post-effect L/R, then FL FR RL RR SUB1 SUB2
        await self._feed(_frames(0.5, 0.5, 0.5, 0.25, 0.1, 0.05, 0.4, 0.01))
        channels = self.monitor.snapshot()["channels"]
        self.assertEqual(channels["keys"], ["fl", "fr", "lfe", "rl", "rr"])
        fl, fr, lfe, rl, rr = channels["vu_db"]
        self.assertGreater(fl, fr)
        self.assertGreater(fr, rl)
        self.assertGreater(rl, rr)
        self.assertAlmostEqual(lfe, 20 * __import__("math").log10(0.4), delta=0.2)
        self.assertEqual(channels["detected"], [False] * 5)
        # The stereo fields keep their post-effect meaning.
        snap = self.monitor.snapshot()
        self.assertAlmostEqual(snap["vu_db_l"], 20 * __import__("math").log10(0.5), delta=0.2)

    async def test_only_the_clipping_position_reports_a_peak(self):
        self.monitor._tap.linked = True
        for _ in range(2):
            await self._feed(_frames(0.1, 0.1, 0.1, 0.1, 1.3, 0.1, 0.1, 0.1))
        channels = self.monitor.snapshot()["channels"]
        self.assertEqual(channels["detected"], [False, False, False, True, False])
        self.assertFalse(self.monitor.snapshot()["detected"])

    async def test_chunks_split_inside_a_frame_never_shift_channels(self):
        self.monitor._tap.linked = True
        data = _frames(0.1, 0.1, 0.9, 0.1, 0.1, 0.1, 0.1, 0.1, count=300)
        fed_at = time.monotonic()
        for start in range(0, len(data), 37):
            self.proc.stdout.feed_data(data[start:start + 37])
        await self._wait_for(lambda: (self.monitor._last_audio_sample_at or 0) >= fed_at)
        await self._wait_for(lambda: not self.monitor._pending_frame_bytes)
        vu = self.monitor.snapshot()["channels"]["vu_db"]
        self.assertEqual(vu.index(max(vu)), 0)

    async def test_columns_exist_but_stay_dark_until_every_output_is_linked(self):
        await self._feed(_frames(0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5))
        channels = self.monitor.snapshot()["channels"]
        self.assertEqual(channels["keys"], ["fl", "fr", "lfe", "rl", "rr"])
        self.assertEqual(channels["vu_db"], [None] * 5)
        self.assertEqual(channels["detected"], [False] * 5)
        self.monitor._tap.linked = True
        self.assertTrue(all(level is not None for level in self.monitor.snapshot()["channels"]["vu_db"]))

    async def test_positions_follow_the_live_layout(self):
        self.monitor._tap.linked = True
        await self._feed(_frames(*([0.3] * 8)))
        # Hot swap keeping six outputs: a stereo crossover meters as plain stereo.
        self.live_layout = ("stereo", ("left_low", "left_mid", "left_high",
                                       "right_low", "right_mid", "right_high"))
        self.assertIsNone(self.monitor.snapshot()["channels"])
        # A different output count can never be read through the old capture.
        self.live_layout = ("surround", ("main_l", "main_r", "center", "sub1"))
        channels = self.monitor.snapshot()["channels"]
        self.assertEqual(channels["keys"], ["fl", "fr", "c", "lfe"])
        self.assertEqual(channels["vu_db"], [None] * 4)
        # No engine, no layout: back to the stereo meter.
        self.live_layout = None
        self.assertIsNone(self.monitor.snapshot()["channels"])

    async def test_levels_fall_to_the_floor_without_data(self):
        self.monitor._tap.linked = True
        await self._feed(_frames(*([0.5] * 8)))
        tap = self.monitor._tap
        tap.floor(time.monotonic() + 30.0)
        self.assertEqual(self.monitor.snapshot()["channels"]["vu_db"], [-60.0] * 5)


class _FastRecheckCase(_TapCaptureCase):
    async def asyncSetUp(self):
        patcher = patch.object(peak_monitor, "TAP_RECHECK_INTERVAL", 0.02)
        patcher.start()
        self.addCleanup(patcher.stop)
        await super().asyncSetUp()


class LayoutChangeTests(_FastRecheckCase):
    """The capture's channel count is fixed at launch; a new layout needs a new capture."""

    async def test_other_output_count_rearms_the_capture(self):
        self.live_layout = ("stereo-sub", ("main_l", "main_r", "sub1", "sub2"))
        with self.assertRaisesRegex(RuntimeError, "layout changed"):
            await asyncio.wait_for(self.task, 2)

    async def test_engine_gone_rearms_the_capture(self):
        self.live_layout = None
        with self.assertRaisesRegex(RuntimeError, "layout changed"):
            await asyncio.wait_for(self.task, 2)

    async def test_same_output_count_keeps_the_capture(self):
        self.live_layout = ("stereo-sub", ("main_l", "main_r", "sub_l", "sub_r", "left_high", "right_high"))
        await asyncio.sleep(0.3)
        self.assertFalse(self.task.done())

    async def test_unlinked_tap_is_linked_once_its_ports_exist(self):
        tap = self.monitor._tap
        self.assertFalse(tap.linked)
        capture = self.monitor._capture_node_name
        ports = [f"fxroute_dsp:output_{i}" for i in range(1, 7)]
        ports += [f"{capture}:input_AUX{i}" for i in range(8)]
        listing = _port_listing(TARGET.source_id, ports)
        self.monitor._run_link = AsyncMock()
        with patch("dsp.peak_monitor._run_bounded_command", new=AsyncMock(return_value=(0, listing, b""))):
            await self._wait_for(lambda: tap.linked)
        self.assertEqual(self.monitor._run_link.await_count, 6)

    async def test_tap_stays_unlinked_while_the_engine_ports_are_missing(self):
        capture = self.monitor._capture_node_name
        listing = _port_listing(TARGET.source_id, [f"{capture}:input_AUX{i}" for i in range(8)])
        self.monitor._run_link = AsyncMock()
        with patch("dsp.peak_monitor._run_bounded_command", new=AsyncMock(return_value=(0, listing, b""))):
            await asyncio.sleep(0.3)
        self.monitor._run_link.assert_not_called()
        self.assertFalse(self.monitor._tap.linked)
        self.assertFalse(self.task.done())


class StereoToMultichannelRearmTests(_FastRecheckCase):
    layout = STEREO

    async def test_layout_gaining_outputs_rearms_a_stereo_capture(self):
        self.live_layout = ("stereo-sub", ("main_l", "main_r", "sub1", "sub2"))
        with self.assertRaisesRegex(RuntimeError, "layout changed"):
            await asyncio.wait_for(self.task, 2)


class RelinkAfterLayoutChangeTests(unittest.IsolatedAsyncioTestCase):
    def _monitor(self, layout):
        from types import SimpleNamespace
        monitor = DSPPeakMonitor(get_output_layout=lambda: layout)
        monitor._running = True
        monitor._proc = SimpleNamespace(returncode=None)
        monitor._target = TARGET
        monitor._capture_node_name = "cap_1"
        monitor._tap = peak_monitor.OutputLevelTap(6)
        monitor._link_capture_stream = AsyncMock()
        return monitor

    async def test_relink_defers_to_a_restart_when_the_engine_layout_changed(self):
        monitor = self._monitor(("stereo-sub", ("main_l", "main_r", "sub1", "sub2")))
        self.assertFalse(await monitor.relink())
        monitor._link_capture_stream.assert_not_called()

    async def test_relink_repairs_links_while_the_layout_still_fits(self):
        monitor = self._monitor(SURROUND_6)
        self.assertTrue(await monitor.relink())
        monitor._link_capture_stream.assert_awaited_once()


class StreamHeaderTests(_TapCaptureCase):
    """pw-record opens stdout with an AU header; it must not shift channels."""

    async def test_header_does_not_shift_columns(self):
        self.monitor._tap.linked = True
        # Loud only on output 3 (rear L); header and samples arrive together.
        await self._feed(_au_header(8) + _frames(0.01, 0.01, 0.01, 0.01, 0.9, 0.01, 0.01, 0.01))
        channels = self.monitor.snapshot()["channels"]
        loudest = channels["keys"][channels["vu_db"].index(max(channels["vu_db"]))]
        self.assertEqual(loudest, "rl")

    async def test_header_split_across_reads(self):
        self.monitor._tap.linked = True
        data = _au_header(8) + _frames(0.01, 0.01, 0.9, 0.01, 0.01, 0.01, 0.01, 0.01, count=200)
        fed_at = time.monotonic()
        for start in range(0, len(data), 7):
            self.proc.stdout.feed_data(data[start:start + 7])
        await self._wait_for(lambda: (self.monitor._last_audio_sample_at or 0) >= fed_at)
        await self._wait_for(lambda: not self.monitor._pending_frame_bytes)
        channels = self.monitor.snapshot()["channels"]
        loudest = channels["keys"][channels["vu_db"].index(max(channels["vu_db"]))]
        self.assertEqual(loudest, "fl")
        self.assertAlmostEqual(self.monitor.snapshot()["vu_db_l"], -40.0, delta=1.0)


class StreamHeaderAlignmentTests(unittest.TestCase):
    def test_fixture_matches_the_captured_header_bytes(self):
        captured = bytes.fromhex("646e732e18000000ffffffff0600000044ac000004000000")
        self.assertEqual(_au_header(4), captured)

    def test_header_is_dropped_and_raw_streams_pass_through(self):
        monitor = DSPPeakMonitor()
        frames = struct.pack("<2f", 0.25, 0.75) * 4
        self.assertEqual(monitor._align_stereo_frames(_au_header(2) + frames), frames)
        monitor = DSPPeakMonitor()
        self.assertEqual(monitor._align_stereo_frames(frames), frames)

    def test_eight_channel_header_leaves_whole_frames(self):
        monitor = DSPPeakMonitor()
        monitor._frame_bytes = 32
        frame = struct.pack("<8f", *range(8))
        out = monitor._align_stereo_frames(_au_header(8) + frame * 3)
        self.assertEqual(out, frame * 3)

    def test_restart_expects_a_new_header(self):
        monitor = DSPPeakMonitor()
        frames = struct.pack("<2f", 0.1, 0.2) * 2
        monitor._align_stereo_frames(_au_header(2) + frames)
        self.assertTrue(monitor._stream_header_done)
        asyncio.run(monitor.stop())
        self.assertFalse(monitor._stream_header_done)


class StereoLayoutCaptureTests(_TapCaptureCase):
    layout = STEREO

    async def test_stereo_layout_keeps_the_plain_two_channel_capture(self):
        cmd = self.command()
        self.assertEqual(cmd[cmd.index("--channels") + 1], "2")
        self.assertNotIn("--channel-map", cmd)
        self.assertIsNone(self.monitor._tap)
        await self._feed(_frames(0.5, 0.25))
        self.assertIsNone(self.monitor.snapshot()["channels"])


class OutputTapLinkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.monitor = DSPPeakMonitor(get_output_layout=lambda: SURROUND_6)
        self.monitor._tap = peak_monitor.OutputLevelTap(6)

    async def test_links_each_output_to_its_own_capture_channel(self):
        calls = []

        async def record(source, target):
            calls.append((source, target))

        self.monitor._run_link = record
        await self.monitor._link_output_taps(TARGET, "cap_1", True)
        self.assertTrue(self.monitor._tap.linked)
        self.assertEqual(calls[0], ("fxroute_dsp:output_1", "cap_1:input_AUX2"))
        self.assertEqual(calls[-1], ("fxroute_dsp:output_6", "cap_1:input_AUX7"))
        self.assertEqual(len(calls), 6)

    async def test_link_failure_keeps_the_stereo_meter_alive(self):
        self.monitor._run_link = AsyncMock(side_effect=RuntimeError("no such port"))
        await self.monitor._link_output_taps(TARGET, "cap_1", True)
        self.assertFalse(self.monitor._tap.linked)

    async def test_unresolved_ports_skip_linking(self):
        self.monitor._run_link = AsyncMock()
        await self.monitor._link_output_taps(TARGET, "cap_1", False)
        self.monitor._run_link.assert_not_called()
        self.assertFalse(self.monitor._tap.linked)

    def test_port_resolution_needs_every_output_and_capture_channel(self):
        aliases = [f"fxroute_dsp:output_{i}" for i in range(1, 7)]
        aliases += [f"cap_1:input_AUX{i}" for i in range(2, 8)]
        ports = [{"alias": alias, "port_name": "", "node_id": 0} for alias in aliases]
        self.assertTrue(self.monitor._tap_ports_present(TARGET, "cap_1", ports))
        self.assertFalse(self.monitor._tap_ports_present(TARGET, "cap_1", ports[1:]))
        self.assertFalse(self.monitor._tap_ports_present(TARGET, "cap_1", ports[:-1]))


def _port_listing(node_id, aliases):
    lines = []
    for index, alias in enumerate(aliases):
        lines += [f"\tid {100 + index}, type PipeWire:Interface:Port/3",
                  f'\t\tnode.id = "{node_id}"',
                  f'\t\tport.name = "{alias.split(":", 1)[1]}"',
                  f'\t\tport.alias = "{alias}"']
    return "\n".join(lines).encode()


class LinkCaptureStreamTests(unittest.IsolatedAsyncioTestCase):
    """The whole link phase against a faked PipeWire port listing."""

    async def _link(self, capture_ports, tap):
        monitor = DSPPeakMonitor()
        monitor._running = True
        monitor._tap = tap
        links = []

        async def record(source, target):
            links.append((source, target))

        monitor._run_link = record
        dsp_ports = ["fxroute_dsp:post_effect_FL", "fxroute_dsp:post_effect_FR"]
        dsp_ports += [f"fxroute_dsp:output_{i}" for i in range(1, 7)]
        listing = _port_listing(TARGET.source_id, dsp_ports) + b"\n" + _port_listing(7, capture_ports)
        with patch("dsp.peak_monitor._run_bounded_command", new=AsyncMock(return_value=(0, listing, b""))):
            await monitor._link_capture_stream(TARGET, "cap_1")
        return monitor, links

    async def test_tap_capture_links_post_effect_pair_and_outputs_on_aux_ports(self):
        ports = [f"cap_1:input_AUX{i}" for i in range(8)]
        monitor, links = await self._link(ports, peak_monitor.OutputLevelTap(6))
        self.assertEqual(links[:2], [("fxroute_dsp:post_effect_FL", "cap_1:input_AUX0"),
                                     ("fxroute_dsp:post_effect_FR", "cap_1:input_AUX1")])
        self.assertEqual(links[2:], [(f"fxroute_dsp:output_{i}", f"cap_1:input_AUX{i + 1}") for i in range(1, 7)])
        self.assertTrue(monitor._tap.linked)

    async def test_plain_stereo_capture_keeps_its_named_ports(self):
        monitor, links = await self._link(["cap_1:input_FL", "cap_1:input_FR"], None)
        self.assertEqual(links, [("fxroute_dsp:post_effect_FL", "cap_1:input_FL"),
                                 ("fxroute_dsp:post_effect_FR", "cap_1:input_FR")])


class RuntimeLayoutTests(unittest.TestCase):
    def test_runtime_reports_mode_and_roles_only_while_the_engine_runs(self):
        from types import SimpleNamespace
        from dsp.runtime import DSPRuntime

        runtime = DSPRuntime.__new__(DSPRuntime)
        runtime._config = SimpleNamespace(output_mode="surround",
                                          layout=({"name": "main_l"}, {"name": "sub1"}))
        runtime._process = SimpleNamespace(returncode=None)
        self.assertEqual(runtime.output_meter_layout(), ("surround", ("main_l", "sub1")))
        runtime._process = SimpleNamespace(returncode=0)
        self.assertIsNone(runtime.output_meter_layout())
        runtime._process = None
        self.assertIsNone(runtime.output_meter_layout())


if __name__ == "__main__":
    unittest.main()
