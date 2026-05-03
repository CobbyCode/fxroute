"""Post-EasyEffects output peak monitor using the EasyEffects output-level node."""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from dataclasses import dataclass
from itertools import count
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

PEAK_THRESHOLD = 1.0
HOLD_SECONDS = 0.03
READ_SIZE = 4096
DISCOVERY_INTERVAL = 0.4
LINK_DISCOVERY_TIMEOUT = 3.0
PORT_DISCOVERY_POLL_INTERVAL = 0.1
LINK_RETRY_ATTEMPTS = 12
LINK_RETRY_INTERVAL = 0.12
ERROR_RETRY_INTERVAL = 0.35
RESTART_SETTLE_SECONDS = 0.1
CONSECUTIVE_HITS_REQUIRED = 2
CAPTURE_NODE_NAME = "fxroute_peak_capture"
CAPTURE_NODE_SEQUENCE = count(1)
VU_FLOOR_DB = -60.0
VU_ATTACK_SECONDS = 0.18
VU_RELEASE_SECONDS = 0.85
VU_EMIT_INTERVAL = 0.25


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
        self._capture_node_name: Optional[str] = None
        self._vu_db: Optional[float] = None
        self._last_vu_update_at: Optional[float] = None
        self._last_vu_emit_at = 0.0

    async def start(self):
        if self._task and not self._task.done():
            logger.info("Peak monitor start skipped because task is already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="easyeffects-peak-monitor")
        logger.info("Peak monitor start armed: task_created=true")

    async def restart(self):
        restart_started_at = time.monotonic()
        logger.info("Peak monitor restart requested")
        await self.stop()
        stop_completed_at = time.monotonic()
        logger.info("Peak monitor restart stop phase completed in %.3fs", stop_completed_at - restart_started_at)
        await asyncio.sleep(RESTART_SETTLE_SECONDS)
        await self.start()
        logger.info("Peak monitor restart fully armed in %.3fs", time.monotonic() - restart_started_at)

    async def stop(self):
        stop_started_at = time.monotonic()
        had_task = bool(self._task and not self._task.done())
        had_proc = bool(self._proc and self._proc.returncode is None)
        self._running = False
        task = self._task
        self._task = None
        proc = self._proc
        self._proc = None
        if task:
            task.cancel()
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except Exception:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except Exception:
                    logger.warning("Peak monitor process did not exit cleanly during stop")
        if task:
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                logger.warning("Peak monitor task did not cancel cleanly during stop")
            except Exception as exc:
                logger.warning("Ignoring peak monitor shutdown error during stop: %s", exc)
        self._target = None
        self._hold_until = 0.0
        self._consecutive_hits = 0
        self._capture_node_name = None
        self._last_error = None
        self._vu_db = None
        self._last_vu_update_at = None
        self._last_vu_emit_at = 0.0
        logger.info("Peak monitor stop completed in %.3fs (had_task=%s had_proc=%s)", time.monotonic() - stop_started_at, had_task, had_proc)

    def snapshot(self) -> dict:
        now = time.monotonic()
        active = now < self._hold_until
        hold_ms = max(0, int((self._hold_until - now) * 1000))
        return {
            "available": self._target is not None,
            "detected": active,
            "hold_ms": hold_ms,
            "threshold": PEAK_THRESHOLD,
            "vu_db": round(self._vu_db, 1) if self._vu_db is not None else None,
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
                await asyncio.sleep(ERROR_RETRY_INTERVAL)
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
                await asyncio.sleep(ERROR_RETRY_INTERVAL)

    async def _capture_target(self, target: MonitorTarget):
        capture_started_at = time.monotonic()
        capture_node_name = f"{CAPTURE_NODE_NAME}_{next(CAPTURE_NODE_SEQUENCE)}"
        self._capture_node_name = capture_node_name
        cmd = [
            "pw-record",
            "--target",
            str(target.source_id),
            "-P",
            "node.autoconnect=false",
            "-P",
            f"node.name={capture_node_name}",
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
        logger.info("Peak monitor pw-record spawned in %.3fs for capture node %s", time.monotonic() - capture_started_at, capture_node_name)
        assert self._proc.stdout is not None
        try:
            try:
                link_started_at = time.monotonic()
                await self._link_capture_stream(target, capture_node_name)
                logger.info("Peak monitor link setup completed in %.3fs for capture node %s", time.monotonic() - link_started_at, capture_node_name)
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
                    rms = self._chunk_rms(chunk)
                    self._update_vu_db(self._linear_to_db(rms), now)
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
                else:
                    self._update_vu_db(VU_FLOOR_DB, now)
                if self._hold_until and now >= self._hold_until:
                    self._hold_until = 0.0
                    await self._emit_if_changed(force=True)
                elif now - self._last_vu_emit_at >= VU_EMIT_INTERVAL:
                    self._last_vu_emit_at = now
                    await self._emit_if_changed()
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
            self._capture_node_name = None

    async def _link_capture_stream(self, target: MonitorTarget, capture_node_name: str):
        discovery_started_at = time.monotonic()
        port_scan_attempts = 0
        capture_fl = None
        capture_fr = None
        target_fl = f"{target.source_name}:output_FL"
        target_fr = f"{target.source_name}:output_FR"
        deadline = time.monotonic() + LINK_DISCOVERY_TIMEOUT
        while time.monotonic() < deadline and self._running:
            port_scan_attempts += 1
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
            for port in self._iter_ports(text):
                alias = port["alias"]
                port_name = port["port_name"]
                node_id = port["node_id"]

                if alias == f"{capture_node_name}:input_FL":
                    capture_fl = alias
                elif alias == f"{capture_node_name}:input_FR":
                    capture_fr = alias

                if node_id == target.source_id:
                    if port_name == "output_FL":
                        target_fl = f"{target.source_name}:output_FL"
                    elif port_name == "output_FR":
                        target_fr = f"{target.source_name}:output_FR"

            if capture_fl and capture_fr and target_fl and target_fr:
                logger.info(
                    "Peak monitor port discovery completed in %.3fs after %d scans for %s (target_fl=%s target_fr=%s)",
                    time.monotonic() - discovery_started_at,
                    port_scan_attempts,
                    capture_node_name,
                    target_fl,
                    target_fr,
                )
                break
            await asyncio.sleep(PORT_DISCOVERY_POLL_INTERVAL)

        if not capture_fl or not capture_fr or not target_fl or not target_fr:
            logger.warning(
                "Peak monitor port discovery timed out in %.3fs after %d scans for %s "
                "(capture_fl=%s capture_fr=%s target_fl=%s target_fr=%s target_id=%s target_name=%s)",
                time.monotonic() - discovery_started_at,
                port_scan_attempts,
                capture_node_name,
                capture_fl,
                capture_fr,
                target_fl,
                target_fr,
                target.source_id,
                target.source_name,
            )
            raise RuntimeError("Peak monitor ports did not resolve in time")

        last_error = None
        link_attempt_started_at = time.monotonic()
        for attempt in range(1, LINK_RETRY_ATTEMPTS + 1):
            try:
                await self._run_link(target_fl, capture_fl)
                await self._run_link(target_fr, capture_fr)
                logger.info("Peak monitor pw-link completed in %.3fs after %d attempts for %s", time.monotonic() - link_attempt_started_at, attempt, capture_node_name)
                return
            except Exception as exc:
                last_error = exc
                if attempt == 1 or attempt == LINK_RETRY_ATTEMPTS:
                    logger.warning("Peak monitor pw-link attempt %d/%d failed for %s: %s", attempt, LINK_RETRY_ATTEMPTS, capture_node_name, exc)
                await asyncio.sleep(LINK_RETRY_INTERVAL)

        raise RuntimeError(str(last_error) if last_error else "Peak capture link failed")

    @staticmethod
    def _iter_ports(text: str):
        current_alias = ""
        current_port_name = ""
        current_node_id = 0
        in_port = False

        def flush():
            nonlocal current_alias, current_port_name, current_node_id, in_port
            if not in_port:
                return None
            port = {
                "alias": current_alias,
                "port_name": current_port_name,
                "node_id": current_node_id,
            }
            current_alias = ""
            current_port_name = ""
            current_node_id = 0
            in_port = False
            return port

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("id ") and ", type PipeWire:Interface:Port" in line:
                port = flush()
                if port is not None:
                    yield port
                in_port = True
                continue
            if not in_port:
                continue
            if line.startswith('port.alias = "'):
                current_alias = line.split('"', 1)[1].rsplit('"', 1)[0]
            elif line.startswith('port.name = "'):
                current_port_name = line.split('"', 1)[1].rsplit('"', 1)[0]
            elif line.startswith('node.id = '):
                try:
                    current_node_id = int(line.split('=', 1)[1].strip().strip('"'))
                except Exception:
                    current_node_id = 0

        port = flush()
        if port is not None:
            yield port

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
        discover_started_at = time.monotonic()
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
            logger.info("Peak monitor target discovery found no matching EasyEffects node in %.3fs", time.monotonic() - discover_started_at)
            return None
        candidates.sort(key=lambda item: (-item[0], item[1].source_id))
        selected = candidates[0][1]
        logger.info("Peak monitor target discovery selected %s (id=%s) in %.3fs from %d candidate(s)", selected.source_name, selected.source_id, time.monotonic() - discover_started_at, len(candidates))
        return selected

    def _update_vu_db(self, target_db: float, now: float):
        target_db = max(VU_FLOOR_DB, min(6.0, target_db))
        if self._vu_db is None or self._last_vu_update_at is None:
            self._vu_db = target_db
            self._last_vu_update_at = now
            return
        elapsed = max(0.001, now - self._last_vu_update_at)
        tau = VU_ATTACK_SECONDS if target_db > self._vu_db else VU_RELEASE_SECONDS
        alpha = 1.0 - math.exp(-elapsed / tau)
        self._vu_db = self._vu_db + ((target_db - self._vu_db) * alpha)
        self._last_vu_update_at = now

    @staticmethod
    def _linear_to_db(value: float) -> float:
        if not math.isfinite(value) or value <= 0.0:
            return VU_FLOOR_DB
        return 20.0 * math.log10(value)

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

    @staticmethod
    def _chunk_rms(chunk: bytes) -> float:
        if len(chunk) < 4:
            return 0.0
        usable = len(chunk) - (len(chunk) % 4)
        if usable <= 0:
            return 0.0
        sum_squares = 0.0
        count = 0
        for (sample,) in struct.iter_unpack("<f", chunk[:usable]):
            if not math.isfinite(sample):
                continue
            sum_squares += float(sample) * float(sample)
            count += 1
        if count <= 0:
            return 0.0
        return math.sqrt(sum_squares / count)
