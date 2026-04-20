"""Post-EasyEffects output peak monitor using the EasyEffects output-level node."""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

PEAK_THRESHOLD = 1.0
HOLD_SECONDS = 0.03
READ_SIZE = 4096
DISCOVERY_INTERVAL = 2.0
LINK_DISCOVERY_TIMEOUT = 8.0
CONSECUTIVE_HITS_REQUIRED = 2
CAPTURE_NODE_NAME = "fxroute_peak_capture"


@dataclass
class MonitorTarget:
    source_name: str
    source_id: int
    description: str


class EasyEffectsPeakMonitor:
    def __init__(self, on_change: Optional[Callable[[dict], Awaitable[None]]] = None):
        self.on_change = on_change
        self._task: Optional[asyncio.Task] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._running = False
        self._hold_until = 0.0
        self._last_emit: Optional[dict] = None
        self._target: Optional[MonitorTarget] = None
        self._last_over_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._consecutive_hits = 0

    async def start(self):
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="easyeffects-peak-monitor")

    async def restart(self):
        await self.stop()
        await asyncio.sleep(0.25)
        await self.start()

    async def stop(self):
        self._running = False
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2)
            except Exception:
                self._proc.kill()
        self._proc = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("Ignoring peak monitor shutdown error during stop: %s", exc)
        self._task = None
        self._target = None
        self._hold_until = 0.0
        self._consecutive_hits = 0
        self._last_error = None

    def snapshot(self) -> dict:
        now = time.monotonic()
        active = now < self._hold_until
        hold_ms = max(0, int((self._hold_until - now) * 1000))
        return {
            "available": self._target is not None,
            "detected": active,
            "hold_ms": hold_ms,
            "threshold": PEAK_THRESHOLD,
            "target": {
                "source_id": self._target.source_id,
                "source_name": self._target.source_name,
                "description": self._target.description,
            } if self._target else None,
            "last_over_at": self._last_over_at,
            "last_error": self._last_error,
        }

    async def _emit_if_changed(self, force: bool = False):
        snapshot = self.snapshot()
        if force or snapshot != self._last_emit:
            self._last_emit = snapshot
            if self.on_change:
                await self.on_change(snapshot)

    async def _run(self):
        while self._running:
            try:
                target = await self._discover_target()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("EasyEffects peak monitor target discovery failed: %s", exc)
                await self._emit_if_changed(force=True)
                await asyncio.sleep(1)
                continue

            if target is None:
                if self._target is not None:
                    self._target = None
                    self._last_error = None
                    await self._emit_if_changed(force=True)
                await asyncio.sleep(DISCOVERY_INTERVAL)
                continue

            if self._target != target:
                self._target = target
                self._last_error = None
                await self._emit_if_changed(force=True)

            try:
                await self._capture_target(target)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("EasyEffects peak monitor capture failed: %s", exc)
                await self._emit_if_changed(force=True)
                await asyncio.sleep(1)

    async def _capture_target(self, target: MonitorTarget):
        cmd = [
            "pw-record",
            "--target",
            str(target.source_id),
            "-P",
            "node.autoconnect=false",
            "-P",
            f"node.name={CAPTURE_NODE_NAME}",
            "--format",
            "f32",
            "--channels",
            "2",
            "-",
        ]
        logger.info("Starting EasyEffects peak monitor on node %s (%s)", target.source_name, target.description)
        self._last_error = None
        await self._emit_if_changed(force=True)
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._proc.stdout is not None
        try:
            try:
                await self._link_capture_stream(target)
            except Exception as exc:
                logger.warning("Peak monitor link setup failed, continuing without capture links yet: %s", exc)
                self._last_error = str(exc)
                await self._emit_if_changed(force=True)
            while self._running:
                try:
                    chunk = await asyncio.wait_for(self._proc.stdout.read(READ_SIZE), timeout=0.25)
                except asyncio.TimeoutError:
                    chunk = b""
                now = time.monotonic()
                if chunk:
                    peak = self._chunk_peak(chunk)
                    if peak >= PEAK_THRESHOLD:
                        self._consecutive_hits += 1
                        if self._consecutive_hits >= CONSECUTIVE_HITS_REQUIRED:
                            self._hold_until = now + HOLD_SECONDS
                            self._last_over_at = time.time()
                            await self._emit_if_changed(force=True)
                    else:
                        self._consecutive_hits = 0
                elif self._proc.returncode is not None:
                    break
                if self._hold_until and now >= self._hold_until:
                    self._hold_until = 0.0
                    await self._emit_if_changed(force=True)
            if self._hold_until and time.monotonic() >= self._hold_until:
                self._hold_until = 0.0
                await self._emit_if_changed(force=True)
            if self._proc.returncode is None:
                await self._proc.wait()
            stderr = b""
            if self._proc.stderr:
                try:
                    stderr = await asyncio.wait_for(self._proc.stderr.read(), timeout=0.2)
                except Exception:
                    stderr = b""
            if self._proc.returncode not in (0, None):
                raise RuntimeError((stderr.decode(errors="ignore").strip() or f"pw-record exited with {self._proc.returncode}"))
        finally:
            self._consecutive_hits = 0
            if self._proc and self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=1)
                except Exception:
                    self._proc.kill()
            self._proc = None

    async def _link_capture_stream(self, target: MonitorTarget):
        capture_fl = None
        capture_fr = None
        target_fl = f"{target.source_name}:output_FL"
        target_fr = f"{target.source_name}:output_FR"
        deadline = time.monotonic() + LINK_DISCOVERY_TIMEOUT
        while time.monotonic() < deadline and self._running:
            if self._proc and self._proc.returncode not in (None, 0):
                stderr = b""
                if self._proc.stderr:
                    try:
                        stderr = await asyncio.wait_for(self._proc.stderr.read(), timeout=0.1)
                    except Exception:
                        stderr = b""
                raise RuntimeError(stderr.decode(errors="ignore").strip() or f"pw-record exited with {self._proc.returncode}")
            proc = await asyncio.create_subprocess_exec(
                "pw-cli",
                "ls",
                "Port",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode(errors="ignore").strip() or "pw-cli ls Port failed")
            text = stdout.decode(errors="ignore")
            if f'port.alias = "{CAPTURE_NODE_NAME}:input_FL"' in text and f'port.alias = "{CAPTURE_NODE_NAME}_probe:input_FL"' not in text:
                capture_fl = f"{CAPTURE_NODE_NAME}:input_FL"
            if f'port.alias = "{CAPTURE_NODE_NAME}:input_FR"' in text and f'port.alias = "{CAPTURE_NODE_NAME}_probe:input_FR"' not in text:
                capture_fr = f"{CAPTURE_NODE_NAME}:input_FR"
            if capture_fl and capture_fr and target_fl in text and target_fr in text:
                break
            await asyncio.sleep(0.2)

        if not capture_fl or not capture_fr:
            raise RuntimeError("Peak capture ports did not appear")

        last_error = None
        for _ in range(8):
            try:
                await self._run_link(target_fl, capture_fl)
                await self._run_link(target_fr, capture_fr)
                return
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.25)

        raise RuntimeError(str(last_error) if last_error else "Peak capture link failed")

    async def _run_link(self, output_port: str, input_port: str):
        proc = await asyncio.create_subprocess_exec(
            "pw-link",
            output_port,
            input_port,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            message = stderr.decode(errors="ignore").strip() or f"pw-link failed: {output_port} -> {input_port}"
            lower = message.lower()
            if "file exists" in lower or "already linked" in lower:
                return
            raise RuntimeError(message)

    async def _discover_target(self) -> Optional[MonitorTarget]:
        proc = await asyncio.create_subprocess_exec(
            "pw-cli",
            "ls",
            "Node",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode(errors="ignore").strip() or "pw-cli ls Node failed")

        text = stdout.decode(errors="ignore")
        candidates: list[tuple[int, MonitorTarget]] = []
        current_id = 0
        current_name = ""
        current_description = ""

        def flush_current():
            nonlocal current_id, current_name, current_description, candidates
            node_name = current_name.strip()
            if not node_name:
                return
            haystack = node_name.lower()
            score = 0
            if haystack == "ee_soe_output_level":
                score += 100
            elif haystack == "ee_sie_output_level":
                score += 50
            elif "output_level" in haystack and "ee_" in haystack:
                score += 20
            else:
                return
            candidates.append((score, MonitorTarget(
                source_name=node_name,
                source_id=current_id,
                description=(current_description.strip() or node_name),
            )))

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("id ") and ", type PipeWire:Interface:Node" in line:
                flush_current()
                current_name = ""
                current_description = ""
                try:
                    current_id = int(line.split(",", 1)[0].split()[1])
                except Exception:
                    current_id = 0
                continue
            if line.startswith('node.name = "'):
                current_name = line.split('"', 1)[1].rsplit('"', 1)[0]
                continue
            if line.startswith('node.description = "'):
                current_description = line.split('"', 1)[1].rsplit('"', 1)[0]
                continue
        flush_current()

        if not candidates:
            return None
        candidates.sort(key=lambda item: (-item[0], item[1].source_id))
        return candidates[0][1]

    @staticmethod
    def _chunk_peak(chunk: bytes) -> float:
        if len(chunk) < 4:
            return 0.0
        usable = len(chunk) - (len(chunk) % 4)
        if usable <= 0:
            return 0.0
        peak = 0.0
        for (sample,) in struct.iter_unpack("<f", chunk[:usable]):
            if not math.isfinite(sample):
                continue
            value = abs(sample)
            if value > peak:
                peak = value
        return peak
