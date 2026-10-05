# SPDX-License-Identifier: AGPL-3.0-only
"""Clock raw PCM through PipeWire with explicit links to the DSP ingress."""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from dataclasses import dataclass
from typing import Awaitable, Callable

import numpy

from audio import pw_link
from audio.input_links import ingress_links_present, input_links_present
from audio.stdin_protocol import CHUNK_BYTES, PcmSpec
from audio.tool_env import c_locale_env
from audio.src_policy import client_env
from common.process_stop import run_stop_shielded
from dsp.runtime import mapped_ingress_edges

logger = logging.getLogger(__name__)

Edges = tuple[tuple[str, str], ...]
# A release waits for the DSP runtime lock, which an engine rebuild can hold.
RELEASE_TIMEOUT_SECONDS = 10


@dataclass
class StdinIngress:
    """Engine-side hooks of the direct multichannel route.

    ``surround_active`` is None while the engine is being rebuilt; the stream
    then keeps its route instead of racing the rebuild for the engine links.
    """

    surround_active: Callable[[], bool | None]
    set_edges: Callable[[Edges], Awaitable[None]]
    release_edges: Callable[[Edges], Awaitable[None]]
    edges_present: Callable[[Edges], Awaitable[bool]] = ingress_links_present


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
    """Clock one PCM stream and link its channels to the DSP ingress.

    The front pair normally reaches the engine through ``fxroute_dsp_sink``.
    A multichannel layout in Surround output instead links every mapped
    channel straight to its engine input (the mapped-ingress slot of the DSP
    runtime), so the sink path and the direct path never carry the same pair.
    """

    def __init__(self, spec: PcmSpec, session_id: str, ingress: StdinIngress | None = None):
        self.spec = spec
        self.node_name = f"fxroute_stdin_{session_id}"
        self.channels = ((f"AUX{spec.left - 1}", "FL"), (f"AUX{spec.right - 1}", "FR"))
        self._ingress = ingress
        self.edges: Edges = mapped_ingress_edges(
            self.node_name, {position: f"AUX{channel - 1}" for position, channel in spec.routes},
            port_kind="output")
        self._route: str | None = None
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
                env=client_env(c_locale_env()),
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

    def _wanted_route(self) -> str:
        if self._ingress is None or not self.spec.multichannel:
            return "sink"
        surround = self._ingress.surround_active()
        if surround is None:
            return self._route or "sink"
        return "direct" if surround else "sink"

    async def _disconnect_sink_links(self) -> None:
        for channel, side in self.channels:
            await pw_link.disconnect_ports((f"{self.node_name}:output_{channel}",),
                                           f"fxroute_dsp_sink:playback_{side}")

    async def repair_links(self) -> None:
        if self._aborted or self.process is None or self.process.returncode is not None:
            raise RuntimeError("STDIN PCM adapter is not running")
        route = self._wanted_route()
        # Break before make: the front pair must never reach the engine twice.
        if route == "direct":
            if self._route == "sink":
                await self._disconnect_sink_links()
            await self._ingress.set_edges(self.edges)
        else:
            if self._route == "direct":
                await self._ingress.release_edges(self.edges)
            for channel, side in self.channels:
                await pw_link.connect_ports((f"{self.node_name}:output_{channel}",),
                                            f"fxroute_dsp_sink:playback_{side}")
        self._route = route
        if not await self.links_present():
            raise RuntimeError("STDIN DSP links are missing")

    async def links_present(self) -> bool:
        if not (self.process and self.process.returncode is None):
            return False
        route = self._wanted_route()
        if route != self._route:
            return False
        if route == "direct":
            return await self._ingress.edges_present(self.edges)
        return await input_links_present(self.node_name, self.channels)

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
            if self._ingress is not None and self.spec.multichannel:
                # Release before the node disappears so the runtime never
                # holds links of a stream that no longer exists.
                try:
                    async with asyncio.timeout(RELEASE_TIMEOUT_SECONDS):
                        await self._ingress.release_edges(self.edges)
                except (OSError, RuntimeError, TimeoutError):
                    logger.warning("STDIN multichannel ingress could not be released")
            async with asyncio.timeout(1):
                await self._disconnect_sink_links()
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
