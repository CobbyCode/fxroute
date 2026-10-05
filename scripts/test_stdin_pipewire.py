#!/usr/bin/env python3
"""PCM adapter routing and real child-process cleanup."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.stdin_protocol import parse_spec
from audio.stdin_pipewire import PipeWirePcmSink, StdinIngress, pw_cat_argv


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.capture = Path(self.temp.name) / "pcm"
        self.spec = parse_spec(dict(version=1, format="f32le", rate=96000,
                                    channels=8, left=5, right=6))
        self.sink = PipeWirePcmSink(self.spec, "test")
        self.argv = None
        self.child_script = ("import sys,pathlib; sys.stderr.write('x'*100000); "
                             f"pathlib.Path({str(self.capture)!r}).write_bytes(sys.stdin.buffer.read())")
        create = asyncio.create_subprocess_exec

        async def spawn(*argv, **kwargs):
            self.argv = argv
            return await create(sys.executable, "-c", self.child_script, **kwargs)

        self.patches = [
            patch("audio.stdin_pipewire.pcm_adapter_error", return_value=None),
            patch("audio.stdin_pipewire.asyncio.create_subprocess_exec", side_effect=spawn),
            patch("audio.stdin_pipewire.pw_link.run_pw_link_command", AsyncMock(return_value="\n".join(
                f"fxroute_stdin_test:output_AUX{i}" for i in range(8)))),
            patch("audio.stdin_pipewire.pw_link.connect_ports", AsyncMock()),
            patch("audio.stdin_pipewire.pw_link.disconnect_ports", AsyncMock()),
            patch("audio.stdin_pipewire.input_links_present", AsyncMock(return_value=True)),
        ]
        self.mocks = [p.start() for p in self.patches]

    async def asyncTearDown(self):
        await self.sink.abort()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_no_remixing_or_autoconnect_and_all_channels_declared(self):
        argv = pw_cat_argv(self.spec, "fxroute_stdin_test")
        self.assertEqual(argv[argv.index("--channels") + 1], "8")
        self.assertEqual(argv[argv.index("--channel-map") + 1],
                         "AUX0,AUX1,AUX2,AUX3,AUX4,AUX5,AUX6,AUX7")
        self.assertEqual(argv[argv.index("--target") + 1], "0")
        # pw-cat ingests "s24" as 4-byte samples (observed stride 8 for 2ch),
        # so packed s24le is expanded to f32 service-side.
        self.assertEqual(argv[argv.index("--format") + 1], "f32")
        self.assertIn("--raw", argv)
        self.assertEqual(argv[-1], "-")

    def test_packed_s24_expands_to_full_scale_f32(self):
        import struct
        samples = (0, 1, -1, 8388607, -8388608, 1234567)
        packed = b"".join(code.to_bytes(3, "little", signed=True) for code in samples)
        expanded = PipeWirePcmSink._s24le_to_f32le(packed)
        self.assertEqual(struct.unpack(f"<{len(samples)}f", expanded),
                         tuple(code / 8388608.0 for code in samples))

    async def test_selected_pair_and_complete_payload_reach_the_child(self):
        await self.sink.start()
        self.assertEqual(self.mocks[3].call_args_list[0].args,
                         (("fxroute_stdin_test:output_AUX4",), "fxroute_dsp_sink:playback_FL"))
        self.assertEqual(self.mocks[3].call_args_list[1].args,
                         (("fxroute_stdin_test:output_AUX5",), "fxroute_dsp_sink:playback_FR"))
        payload = bytes(range(256)) * 768
        await self.sink.write(payload)
        await self.sink.finish()
        self.assertEqual(self.capture.read_bytes(), payload)
        self.assertEqual(self.sink.process.returncode, 0)

    def test_mono_aux_position_is_not_the_zero_channel_aux_layout(self):
        spec = parse_spec(dict(version=1, format="s16le", rate=48000, channels=1))
        argv = pw_cat_argv(spec, "fxroute_stdin_test")
        self.assertEqual(argv[argv.index("--channel-map") + 1], "[ AUX0 ]")

    async def test_failed_second_link_reaps_child_and_disconnects_partial_route(self):
        self.mocks[3].side_effect = [None, RuntimeError("Link unavailable")]
        with self.assertRaisesRegex(RuntimeError, "Link unavailable"):
            await self.sink.start()
        self.assertIsNotNone(self.sink.process.returncode)
        self.assertEqual({call.args[1] for call in self.mocks[4].call_args_list},
                         {"fxroute_dsp_sink:playback_FL", "fxroute_dsp_sink:playback_FR"})

    async def test_partial_frame_is_rejected_before_child_write(self):
        await self.sink.start()
        with self.assertRaises(ValueError):
            await self.sink.write(b"partial")

    async def test_abort_reaps_child_with_open_stdin(self):
        await self.sink.start()
        await asyncio.wait_for(self.sink.abort(), 3)
        self.assertIsNotNone(self.sink.process.returncode)

    async def _interrupt_buffered_finish(self, *, timeout):
        self.child_script = "import time; time.sleep(30)"
        await self.sink.start()
        # Fill the real pipe so close() cannot complete before interruption.
        stdin = self.sink.process.stdin
        stdin.write(b"\0" * 1048576)
        waiting = asyncio.Event()
        wait_closed = stdin.wait_closed

        async def observe_close_wait():
            waiting.set()
            await wait_closed()

        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        callback_errors = []
        loop.set_exception_handler(lambda _loop, context: callback_errors.append(context))
        real_timeout = asyncio.timeout

        def finish_timeout(delay):
            return real_timeout(0.05 if timeout and delay == 5 else delay)

        try:
            with patch.object(stdin, "wait_closed", side_effect=observe_close_wait), \
                    patch("audio.stdin_pipewire.asyncio.timeout", side_effect=finish_timeout):
                finish = asyncio.create_task(self.sink.finish())
                await asyncio.wait_for(waiting.wait(), 2)
                if not timeout:
                    finish.cancel()
                with self.assertRaises(TimeoutError if timeout else asyncio.CancelledError):
                    await asyncio.wait_for(finish, 3)
            await asyncio.sleep(0)
            self.assertIsNotNone(self.sink.process.returncode)
            self.assertTrue(self.sink._stderr_task.done())
            self.assertEqual(callback_errors, [], [str(c.get("exception")) for c in callback_errors])
        finally:
            await self.sink.abort()
            loop.set_exception_handler(previous_handler)

    async def test_cancelled_buffered_finish_does_not_cancel_pipe_close_callback(self):
        await self._interrupt_buffered_finish(timeout=False)

    async def test_timed_out_buffered_finish_does_not_cancel_pipe_close_callback(self):
        await self._interrupt_buffered_finish(timeout=True)


class FakeIngress:
    """Engine side of the direct route; records every call in graph order."""

    def __init__(self, log, surround=True):
        self.log = log
        self.surround = surround
        self.edges = ()
        self.set_error = None
        self.surround_checks = 0

    def hooks(self):
        return StdinIngress(self.surround_active, self.set_edges, self.release_edges, self.edges_present)

    def surround_active(self):
        self.surround_checks += 1
        return self.surround

    async def set_edges(self, edges):
        self.log.append(("set", edges))
        if self.set_error:
            self.edges = edges
            raise self.set_error
        self.edges = edges

    async def release_edges(self, edges):
        self.log.append(("release", edges))
        if self.edges == edges:
            self.edges = ()

    async def edges_present(self, edges):
        return bool(edges) and self.edges == edges


class DirectRouteTests(unittest.IsolatedAsyncioTestCase):
    NODE = "fxroute_stdin_test"

    async def asyncSetUp(self):
        self.log = []
        self.ingress = FakeIngress(self.log)
        self.sink = None
        create = asyncio.create_subprocess_exec

        async def spawn(*argv, **kwargs):
            return await create(sys.executable, "-c", "import sys; sys.stdin.buffer.read()", **kwargs)

        async def connect(sources, target):
            self.log.append(("connect", sources[0], target))

        async def disconnect(sources, target):
            self.log.append(("disconnect", sources[0], target))

        self.patches = [
            patch("audio.stdin_pipewire.pcm_adapter_error", return_value=None),
            patch("audio.stdin_pipewire.asyncio.create_subprocess_exec", side_effect=spawn),
            patch("audio.stdin_pipewire.pw_link.run_pw_link_command", AsyncMock(side_effect=self.ports)),
            patch("audio.stdin_pipewire.pw_link.connect_ports", side_effect=connect),
            patch("audio.stdin_pipewire.pw_link.disconnect_ports", side_effect=disconnect),
            patch("audio.stdin_pipewire.input_links_present", AsyncMock(return_value=True)),
        ]
        for patcher in self.patches:
            patcher.start()

    async def asyncTearDown(self):
        if self.sink is not None:
            await self.sink.abort()
        for patcher in reversed(self.patches):
            patcher.stop()

    async def ports(self, *_args):
        return "\n".join(f"{self.NODE}:output_AUX{i}" for i in range(32))

    def make(self, channels=8, ingress=True, **extra):
        spec = parse_spec(dict(version=1, format="s16le", rate=48000, channels=channels, **extra))
        self.sink = PipeWirePcmSink(spec, "test", self.ingress.hooks() if ingress else None)
        return self.sink

    def expected_edges(self, positions):
        engine = {"FL": 1, "FR": 2, "FC": 3, "LFE": 4, "SL": 5, "SR": 6, "BL": 7, "BR": 8}
        return tuple((f"{self.NODE}:output_AUX{channel - 1}", f"fxroute_dsp:input_{engine[position]}")
                     for position, channel in positions)

    def sink_links(self, kind):
        return [entry for entry in self.log if entry[0] == kind and "fxroute_dsp_sink" in entry[2]]

    async def test_surround_links_every_mapped_channel_straight_to_its_engine_input(self):
        sink = self.make(8)
        await sink.start()
        self.assertEqual(self.log, [("set", self.expected_edges((
            ("FL", 1), ("FR", 2), ("FC", 3), ("LFE", 4), ("SL", 7), ("SR", 8), ("BL", 5), ("BR", 6))))])
        self.assertEqual(sink.edges, self.ingress.edges)
        self.assertTrue(await sink.links_present())

    async def test_special_layout_links_only_its_positions(self):
        sink = self.make(10, channel_map="FR,FL,-,-,SL,SR")
        await sink.start()
        self.assertEqual(self.ingress.edges, self.expected_edges(
            (("FL", 2), ("FR", 1), ("SL", 5), ("SR", 6))))

    async def test_stereo_outputs_keep_the_sink_route_untouched(self):
        self.ingress.surround = False
        sink = self.make(8)
        await sink.start()
        self.assertEqual(self.log, [
            ("connect", f"{self.NODE}:output_AUX0", "fxroute_dsp_sink:playback_FL"),
            ("connect", f"{self.NODE}:output_AUX1", "fxroute_dsp_sink:playback_FR")])
        self.assertTrue(await sink.links_present())

    async def test_front_pair_streams_never_leave_the_sink_route(self):
        for kwargs in ({"channels": 1}, {"channels": 2}, {"channels": 8, "left": 5, "right": 6},
                       {"channels": 8, "channel_map": "FL,FR"}):
            with self.subTest(kwargs=kwargs):
                self.log.clear()
                self.ingress.surround_checks = 0
                sink = self.make(**kwargs)
                await sink.start()
                self.assertEqual([entry[0] for entry in self.log], ["connect", "connect"])
                self.assertTrue(await sink.links_present())
                self.assertEqual(self.ingress.surround_checks, 0)
                await sink.abort()
                self.assertNotIn("release", [entry[0] for entry in self.log])

    async def test_a_sink_without_engine_hooks_uses_the_sink_route(self):
        sink = self.make(8, ingress=False)
        await sink.start()
        self.assertEqual([entry[0] for entry in self.log], ["connect", "connect"])

    async def test_entering_surround_drops_the_sink_links_before_linking_directly(self):
        self.ingress.surround = False
        sink = self.make(8)
        await sink.start()
        self.log.clear()
        self.ingress.surround = True
        self.assertFalse(await sink.links_present())
        await sink.repair_links()
        self.assertEqual([entry[0] for entry in self.log], ["disconnect", "disconnect", "set"])
        self.assertTrue(await sink.links_present())

    async def test_a_rebuilding_engine_keeps_the_current_route(self):
        for start, kept in ((True, "direct"), (False, "sink")):
            with self.subTest(surround=start):
                self.log.clear()
                self.ingress.surround = start
                sink = self.make(8)
                await sink.start()
                self.log.clear()
                self.ingress.surround = None
                self.assertEqual(sink._wanted_route(), kept)
                self.assertTrue(await sink.links_present())
                await sink.repair_links()
                self.assertEqual(sink._route, kept)
                self.assertEqual([entry[0] for entry in self.log], ["set"] if kept == "direct" else ["connect", "connect"])
                await sink.abort()

    async def test_a_stream_starting_during_a_rebuild_takes_the_sink_route_and_follows_afterwards(self):
        self.ingress.surround = None
        sink = self.make(8)
        await sink.start()
        self.assertEqual([entry[0] for entry in self.log], ["connect", "connect"])
        self.ingress.surround = True
        self.assertFalse(await sink.links_present())
        await sink.repair_links()
        self.assertEqual(self.ingress.edges, sink.edges)

    async def test_leaving_surround_releases_the_edges_before_relinking_the_sink(self):
        sink = self.make(6)
        await sink.start()
        self.log.clear()
        self.ingress.surround = False
        self.assertFalse(await sink.links_present())
        await sink.repair_links()
        self.assertEqual([entry[0] for entry in self.log], ["release", "connect", "connect"])
        self.assertEqual(self.ingress.edges, ())
        self.assertTrue(await sink.links_present())

    async def test_missing_edges_are_not_present_and_are_repaired_in_place(self):
        sink = self.make(8)
        await sink.start()
        self.ingress.edges = ()
        self.assertFalse(await sink.links_present())
        self.log.clear()
        await sink.repair_links()
        self.assertEqual([entry[0] for entry in self.log], ["set"])
        self.assertTrue(await sink.links_present())

    async def test_failed_direct_link_reaps_the_child_and_releases(self):
        self.ingress.set_error = RuntimeError("Cannot connect DSP ingress")
        sink = self.make(8)
        with self.assertRaisesRegex(RuntimeError, "Cannot connect DSP ingress"):
            await sink.start()
        self.assertIsNotNone(sink.process.returncode)
        self.assertEqual(self.ingress.edges, ())
        self.assertEqual(self.log[-3][0], "release")

    async def test_abort_releases_the_edges_before_the_node_goes(self):
        sink = self.make(8)
        await sink.start()
        process = sink.process
        self.log.clear()
        await sink.abort()
        self.assertEqual(self.log[0][0], "release")
        self.assertEqual(self.ingress.edges, ())
        self.assertIsNotNone(process.returncode)
        await sink.abort()
        self.assertEqual([entry[0] for entry in self.log].count("release"), 1)

    async def test_a_hung_release_does_not_block_abort(self):
        sink = self.make(8)
        await sink.start()

        async def hang(_edges):
            await asyncio.sleep(30)

        sink._ingress.release_edges = hang
        with patch("audio.stdin_pipewire.RELEASE_TIMEOUT_SECONDS", 0.05):
            await asyncio.wait_for(sink.abort(), 3)
        self.assertIsNotNone(sink.process.returncode)

    async def test_finish_releases_the_edges_after_a_clean_end(self):
        sink = self.make(6)
        await sink.start()
        await sink.write(b"\0" * 12 * 10)
        await sink.finish()
        self.assertEqual(self.ingress.edges, ())
        self.assertEqual(sink.process.returncode, 0)

    async def test_the_node_ports_stay_discrete_aux_channels(self):
        argv = pw_cat_argv(self.make(8).spec, self.NODE)
        self.assertEqual(argv[argv.index("--channel-map") + 1], "AUX0,AUX1,AUX2,AUX3,AUX4,AUX5,AUX6,AUX7")


if __name__ == "__main__":
    unittest.main()
