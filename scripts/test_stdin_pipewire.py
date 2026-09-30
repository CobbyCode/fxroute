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
from audio.stdin_pipewire import PipeWirePcmSink, pw_cat_argv


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


if __name__ == "__main__":
    unittest.main()
