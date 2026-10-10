# SPDX-License-Identifier: AGPL-3.0-only
"""Own the digital capture helper, never the DSP/output links or hardware clock."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import tempfile

from audio import pw_link
from common.process_stop import run_stop_shielded
from dsp.runtime import _contains_link

NODE_NAME = "fxroute_surround_input"
BUS_POSITIONS = ("FL", "FR", "FC", "LFE", "SL", "SR", "BL", "BR")
DEFAULT_BINARY = Path(__file__).resolve().parents[1] / "native_surround/build/fxroute-surround-input"
# native_surround/surround.h: the live callback delays PCM and encoded paths alike.
LIVE_LATENCY_FRAMES = 2818


class DigitalInput:
    """Singleton-node lifecycle with verified capture links and bounded metadata."""

    _owner: DigitalInput | None = None

    def __init__(self, *, binary: str | Path | None = None):
        self.binary = Path(binary) if binary is not None else DEFAULT_BINARY
        self.node_name = NODE_NAME
        self.process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task | None = None
        self._recovery_task: asyncio.Task | None = None
        self._stderr_tail = b""
        self._source_name: str | None = None
        self._request: tuple[str, str, str, int, str] | None = None
        self._source_ports: tuple[str, ...] = ()
        self._status_dir: Path | None = None
        self._status_path: Path | None = None
        self._metadata: dict = {}
        self._error: str | None = None

    @property
    def output_ports(self) -> tuple[str, ...]:
        return tuple(f"{NODE_NAME}:output_{position}" for position in (*BUS_POSITIONS, "stereo_FL", "stereo_FR"))

    @property
    def carrier_ports(self) -> tuple[str, str]:
        return (f"{NODE_NAME}:carrier_L", f"{NODE_NAME}:carrier_R")

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        while chunk := await proc.stderr.read(4096):
            self._stderr_tail = (self._stderr_tail + chunk)[-16384:]

    def _process_error(self) -> str:
        return self._stderr_tail.decode(errors="replace").strip()[-1024:] or "Digital ingress helper exited"

    async def _spawn(self) -> None:
        # Shield process creation too: cancellation can arrive before Python
        # hands the newly spawned child to the caller.
        task = asyncio.create_task(asyncio.create_subprocess_exec(
            str(self.binary), "--status", str(self._status_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        ))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        self.process = task.result()
        self._stderr_task = asyncio.create_task(self._read_stderr(self.process), name="digital-input-stderr")
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    def _resolve_port(source: str, channel: str, outputs: set[str]) -> str:
        if ":" in channel:
            candidates = (channel,) if channel.startswith(f"{source}:") else ()
        elif channel.startswith(("capture_", "output_")):
            candidates = (f"{source}:{channel}",)
        else:
            candidates = (f"{source}:capture_{channel}", f"{source}:output_{channel}")
        for port in candidates:
            if port in outputs:
                return port
        raise RuntimeError(f"Digital capture port is missing: {source}:{channel}")

    async def start(self, source_name: str, left: str, right: str, rate: int = 48000,
                    *, right_source: str | None = None) -> None:
        if rate != 48000:
            raise ValueError("Digital ingress requires a fixed 48000 Hz carrier graph")
        source = str(source_name).strip()
        right_node = str(right_source or source).strip()
        left, right = str(left).strip(), str(right).strip()
        if not source or not right_node or not left or not right or (left == right and source == right_node):
            raise ValueError("Digital ingress requires two distinct capture ports")
        async with self._lock:
            request = (source, left, right, rate, right_node)
            if (self.process is not None and self.process.returncode is None
                    and self._request == request):
                try:
                    await self.repair_links()
                except BaseException as exc:
                    if not isinstance(exc, asyncio.CancelledError):
                        self._error = str(exc) or "Digital capture links are unavailable"
                    await run_stop_shielded(self._cleanup(), cleanup_log="Digital ingress link cleanup failed")
                    raise
                return
            if self.process is not None:
                cancelled = await run_stop_shielded(self._cleanup(), cleanup_log="Digital ingress cleanup failed")
                if cancelled:
                    raise asyncio.CancelledError
            if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
                raise RuntimeError(f"Digital ingress binary is missing or not executable: {self.binary}")
            if DigitalInput._owner is not None and DigitalInput._owner is not self:
                raise RuntimeError("Digital ingress node already has an owner")
            DigitalInput._owner = self
            self._error = None
            self._stderr_tail = b""
            self._metadata = {}
            try:
                outputs = set((await pw_link.run_pw_link_command("-o")).splitlines())
                inputs = set((await pw_link.run_pw_link_command("-i")).splitlines())
                if any(port.startswith(f"{NODE_NAME}:") for port in outputs | inputs):
                    raise RuntimeError("Digital ingress node already exists; refusing a duplicate")
                self._source_ports = (self._resolve_port(source, left, outputs),
                                      self._resolve_port(right_node, right, outputs))
                if self._source_ports[0] == self._source_ports[1]:
                    raise ValueError("Digital ingress capture ports must be distinct")
                self._source_name = source
                self._request = request
                temporary_root = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
                self._status_dir = Path(tempfile.mkdtemp(prefix="fxroute-digital-", dir=temporary_root))
                self._status_path = self._status_dir / "status.json"
                await self._spawn()
                async with asyncio.timeout(3):
                    while True:
                        if self.process.returncode is not None:
                            raise RuntimeError(self._process_error())
                        outputs = set((await pw_link.run_pw_link_command("-o")).splitlines())
                        inputs = set((await pw_link.run_pw_link_command("-i")).splitlines())
                        if set(self.output_ports) <= outputs and set(self.carrier_ports) <= inputs:
                            break
                        await asyncio.sleep(0.05)
                    await self.repair_links()
                self._recovery_task = asyncio.create_task(self._recover(), name="digital-input-links")
            except BaseException as exc:
                if not isinstance(exc, asyncio.CancelledError):
                    self._error = str(exc) or "Digital ingress startup failed"
                await run_stop_shielded(self._cleanup(), cleanup_log="Digital ingress startup cleanup failed")
                raise

    async def links_present(self) -> bool:
        if self.process is None or self.process.returncode is not None or len(self._source_ports) != 2:
            return False
        links = await pw_link.run_pw_link_command("-l")
        return all(_contains_link(links, source, target)
                   for source, target in zip(self._source_ports, self.carrier_ports))

    async def repair_links(self) -> None:
        if self.process is None or self.process.returncode is not None:
            raise RuntimeError(self._process_error())
        outputs = set((await pw_link.run_pw_link_command("-o")).splitlines())
        inputs = set((await pw_link.run_pw_link_command("-i")).splitlines())
        if not set(self._source_ports + self.output_ports) <= outputs or not set(self.carrier_ports) <= inputs:
            raise RuntimeError("Digital ingress or selected capture ports are missing")
        for source, target in zip(self._source_ports, self.carrier_ports):
            await pw_link.connect_ports((source,), target)
        if not await self.links_present():
            raise RuntimeError("Digital capture links are missing")

    async def _recover(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.5)
                if not await self.links_present():
                    await self.repair_links()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = str(exc) or "Digital ingress recovery failed"
            await run_stop_shielded(self._cleanup(from_recovery=True), cleanup_log="Digital ingress recovery cleanup failed")

    def deterministic_latency_ms(self) -> float:
        if self.process is None or self.process.returncode is not None:
            raise RuntimeError('Digital ingress latency is unavailable')
        return LIVE_LATENCY_FRAMES * 1000.0 / 48000

    def snapshot(self) -> dict:
        if self._status_path is not None:
            try:
                with self._status_path.open("rb") as file:
                    data = file.read(4097)
                if len(data) <= 4096:
                    value = json.loads(data)
                    if isinstance(value, dict):
                        self._metadata = {key: value[key] for key in (
                            "codec", "sample_rate", "layout", "positions", "bus_positions", "errors",
                            "decoded_bursts", "lookahead_frames", "state", "mute_reason",
                            "worker_margin_frames", "worker_quantum_frames", "worker_delay_max_us",
                            "worker_reserve_min_us", "input_queue_peak_frames", "late_frames",
                            "underrun_frames", "queued_muted_frames", "transport_faults") if key in value}
            except (OSError, ValueError):
                pass
        running = self.process is not None and self.process.returncode is None
        return {**self._metadata, "running": running, "node_name": NODE_NAME,
                "live_latency_frames": LIVE_LATENCY_FRAMES if running else None,
                "source_name": self._source_name, "source_ports": list(self._source_ports),
                "output_ports": list(self.output_ports), "rate": 48000,
                "status_path": str(self._status_path) if self._status_path else None,
                "pid": self.process.pid if running else None,
                "error": self._error if not running else None,
                "stderr": self._stderr_tail.decode(errors="replace")}

    async def _cleanup(self, *, from_recovery: bool = False) -> None:
        recovery = self._recovery_task
        self._recovery_task = None
        if recovery is not None and not from_recovery:
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
        proc = self.process
        try:
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), 1)
                except TimeoutError:
                    pass
                # Also terminate a descendant retaining stderr after its parent exits.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            if self._stderr_task is not None:
                try:
                    await asyncio.wait_for(self._stderr_task, 1)
                except TimeoutError:
                    self._stderr_task.cancel()
                    await asyncio.gather(self._stderr_task, return_exceptions=True)
        finally:
            self.process = None
            self._stderr_task = None
            # Node removal deletes only this helper's capture links. No DSP links
            # are inspected, created or disconnected by this adapter.
            self._source_name = None
            self._request = None
            self._source_ports = ()
            if self._status_path is not None:
                self._status_path.unlink(missing_ok=True)
            if self._status_dir is not None:
                for temporary in self._status_dir.glob("status.json.tmp.*"):
                    temporary.unlink(missing_ok=True)
                self._status_dir.rmdir()
            self._status_path = self._status_dir = None
            if DigitalInput._owner is self:
                DigitalInput._owner = None

    async def stop(self) -> None:
        async with self._lock:
            cancelled = await run_stop_shielded(self._cleanup(), cleanup_log="Digital ingress stop failed")
            if cancelled:
                raise asyncio.CancelledError
