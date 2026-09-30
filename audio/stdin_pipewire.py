# SPDX-License-Identifier: AGPL-3.0-only
"""Clock raw PCM through PipeWire with explicit links to the DSP ingress."""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys

import numpy

from audio import pw_link
from audio.input_links import input_links_present
from audio.stdin_protocol import CHUNK_BYTES, PcmSpec
from audio.tool_env import c_locale_env
from common.process_stop import run_stop_shielded

logger = logging.getLogger(__name__)


def pcm_adapter_error() -> str | None:
    if sys.byteorder != "little":
        return "STDIN PCM requires a little-endian host"
    if shutil.which("pw-cat") is None:
        return "pw-cat is not installed"
    try:
        numpy.zeros(1, dtype=numpy.float32)
    except Exception:
        return "numpy is required for PCM conversion"
    return None


def _s24le_to_f32le(frames: bytes) -> bytes:
    """Expand packed 3-byte samples to float32 without changing the scale."""
    raw = numpy.frombuffer(frames, dtype=numpy.uint8).reshape(-1, 3).astype(numpy.int32)
    code = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
    code = (code << 8) >> 8
    return (code.astype(numpy.float32) / 8388608.0).tobytes()


def pw_cat_argv(spec: PcmSpec, node_name: str) -> list[str]:
    # pw-cat ingests "--format s24" as 4-byte samples (its --verbose stride
    # is 8 bytes for 2 channels), so packed s24le is expanded to f32 above.
    formats = {"s16le": "s16", "s24le": "f32", "s32le": "s32", "f32le": "f32"}
    positions = [f"AUX{i}" for i in range(spec.channels)]
    # Bare AUX0 names the zero-channel layout in recent pw-cat versions.
    channel_map = "[ AUX0 ]" if spec.channels == 1 else ",".join(positions)
    # No remix property: channel discreteness comes from the explicit
    # per-channel links below, not from a stream hint.
    properties = (f"node.name={node_name} node.autoconnect=false node.dont-reconnect=true "
                  "audio.position=[ " + " ".join(positions) + " ]")
    return ["pw-cat", "--playback", "--raw", "--target", "0", "--rate", str(spec.rate),
            "--channels", str(spec.channels), "--channel-map", channel_map,
            "--format", formats[spec.format], "--latency", "100ms", "--properties", properties, "-"]


class PipeWirePcmSink:
    def __init__(self, spec: PcmSpec, session_id: str):
        self.spec = spec
        self.node_name = f"fxroute_stdin_{session_id}"
        self.channels = ((f"AUX{spec.left - 1}", "FL"), (f"AUX{spec.right - 1}", "FR"))
        self.process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail = b""
        self._aborted = False

    _s24le_to_f32le = staticmethod(_s24le_to_f32le)

    @property
    def _child_frame_bytes(self) -> int:
        if self.spec.format == "s24le":
            return self.spec.channels * 4
        return self.spec.frame_bytes

    async def _read_stderr(self) -> None:
        while chunk := await self.process.stderr.read(4096):
            self._stderr_tail = (self._stderr_tail + chunk)[-16384:]

    async def start(self) -> None:
        error = pcm_adapter_error()
        if error:
            raise RuntimeError(error)
        try:
            self.process = await asyncio.create_subprocess_exec(
                *pw_cat_argv(self.spec, self.node_name), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                env=c_locale_env(),
            )
            self._stderr_task = asyncio.create_task(self._read_stderr(), name="stdin-adapter-stderr")
            async with asyncio.timeout(2):
                expected = {f"{self.node_name}:output_AUX{i}" for i in range(self.spec.channels)}
                while True:
                    if self.process.returncode is not None:
                        raise RuntimeError(self._error())
                    ports = await pw_link.run_pw_link_command("-o")
                    if expected.issubset(set(ports.splitlines())):
                        break
                    await asyncio.sleep(0.05)
                await self.repair_links()
        except BaseException:
            await self.abort()
            raise

    def _error(self) -> str:
        return self._stderr_tail.decode(errors="replace").strip()[-1024:] or "STDIN PCM adapter exited"

    async def repair_links(self) -> None:
        if self._aborted or self.process is None or self.process.returncode is not None:
            raise RuntimeError("STDIN PCM adapter is not running")
        for channel, side in self.channels:
            await pw_link.connect_ports((f"{self.node_name}:output_{channel}",),
                                        f"fxroute_dsp_sink:playback_{side}")
        if not await self.links_present():
            raise RuntimeError("STDIN DSP links are missing")

    async def links_present(self) -> bool:
        return bool(self.process and self.process.returncode is None
                    and await input_links_present(self.node_name, self.channels))

    async def write(self, frames: bytes) -> None:
        if len(frames) % self.spec.frame_bytes:
            raise ValueError("PCM write is not frame-aligned")
        if self._aborted or self.process is None or self.process.returncode is not None:
            raise RuntimeError("STDIN PCM adapter is not running")
        if self.spec.format == "s24le":
            frames = self._s24le_to_f32le(frames)
        block = CHUNK_BYTES - CHUNK_BYTES % self._child_frame_bytes
        for offset in range(0, len(frames), block):
            self.process.stdin.write(frames[offset:offset + block])
            await self.process.stdin.drain()

    async def finish(self) -> None:
        try:
            async with asyncio.timeout(5):
                self.process.stdin.close()
                # Cancellation must not cancel the subprocess protocol's
                # close waiter: pipe_connection_lost still completes it.
                close_task = asyncio.create_task(self.process.stdin.wait_closed())
                # Observe late pipe errors even when finish was interrupted.
                close_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
                await asyncio.shield(close_task)
                await self.process.wait()
                await self._stderr_task
            if self.process.returncode != 0:
                raise RuntimeError(self._error())
        finally:
            await self.abort()

    async def _abort(self) -> None:
        if self._aborted:
            return
        self._aborted = True
        try:
            async with asyncio.timeout(1):
                for channel, side in self.channels:
                    await pw_link.disconnect_ports((f"{self.node_name}:output_{channel}",),
                                                   f"fxroute_dsp_sink:playback_{side}")
        except (OSError, RuntimeError, TimeoutError):
            logger.debug("STDIN link cleanup deferred to node removal")
        finally:
            proc = self.process
            if proc is not None and proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), 1)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
            if proc is not None and proc.stdin:
                proc.stdin.close()
            if self._stderr_task is not None:
                await self._stderr_task

    async def abort(self) -> None:
        cancelled = await run_stop_shielded(self._abort(), cleanup_log="STDIN adapter cleanup failed")
        if cancelled:
            raise asyncio.CancelledError
