"""Post-DSP peak monitor using the FXRoute native engine output node.

Hosts the capture process mechanics (:class:`DSPPeakMonitor`) and the
application-level state machine (:class:`PeakMonitorCoordinator`) that
starts/stops/relinks it based on the committed playback, Spotify and
source-mode context.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from dataclasses import dataclass
from itertools import count
from typing import Any, Awaitable, Callable, Optional

import playback.state as playback_state
from audio.samplerate import SOURCE_MODE_BLUETOOTH_INPUT, authoritative_sample_rate, get_samplerate_status

logger = logging.getLogger(__name__)

PEAK_THRESHOLD = 1.0
HOLD_SECONDS = 0.03
READ_SIZE = 4096
DISCOVERY_INTERVAL = 0.4
LINK_DISCOVERY_TIMEOUT = 3.0
PORT_DISCOVERY_POLL_INTERVAL = 0.1
LINK_RETRY_ATTEMPTS = 12
LINK_RETRY_INTERVAL = 0.12
# Hard bound for every short-lived pw-cli/pw-link command: one hanging call
# must never beat the LINK_DISCOVERY_TIMEOUT deadline or stall the monitor
# task / relink path (which runs under the peak-monitor coordinator lock).
PEAK_MONITOR_COMMAND_TIMEOUT_SECONDS = 3.0
# Grace between terminate and kill when a bounded command child ignores
# SIGTERM; matches the project subprocess-stop convention.
PEAK_MONITOR_COMMAND_TERMINATE_GRACE_SECONDS = 1.0
# Grace before the monitor process is stopped/kept-alive after playback,
# Spotify or source-mode activity ends (pause/resume glitch avoidance).
PEAK_MONITOR_INACTIVE_GRACE_MS = 450
ERROR_RETRY_INTERVAL = 0.35
RESTART_SETTLE_SECONDS = 0.1
CONSECUTIVE_HITS_REQUIRED = 2
CAPTURE_NODE_NAME = "fxroute_peak_capture"
DSP_OUTPUT_NODE_NAME = "fxroute_dsp"
CAPTURE_NODE_SEQUENCE = count(1)
VU_FLOOR_DB = -60.0
VU_ATTACK_SECONDS = 0.18
VU_RELEASE_SECONDS = 0.85
VU_EMIT_INTERVAL = 0.25
CAPTURE_NO_DATA_TIMEOUT = 3.0
FALLBACK_CAPTURE_RATE = 48_000
# After the DSP engine is recreated the graph needs a settle window before
# the post_effect buffers deliver data again; a fresh capture linked during
# that window must not be killed by the normal no-data timeout.
REBUILD_SETTLE_GRACE_SECONDS = 10.0
# While a capture is silent, re-check the DSP node identity this often so a
# node recreation (same id, new serial) triggers an immediate rearm instead
# of waiting for the no-data timeout.
TARGET_RECHECK_INTERVAL = 1.0
# A capture armed during the rebuild settle window can negotiate a degraded
# stream (periodic 250-400 ms data gaps).  When repeated timeouts were
# observed during the grace, rearm the capture once after the grace so the
# fresh negotiation delivers a clean continuous stream.
SETTLE_REARM_MIN_TIMEOUTS = 2


def _resolve_capture_rate() -> int:
    """Keep the monitor from becoming an unintended 48 kHz graph driver."""
    try:
        status = get_samplerate_status()
    except Exception:
        return FALLBACK_CAPTURE_RATE
    force_rate = status.get("force_rate")
    if isinstance(force_rate, int) and force_rate > 0:
        return force_rate
    configured_rate = status.get("configured_default_rate")
    if isinstance(configured_rate, int) and configured_rate > 0:
        return configured_rate
    clock_rate = status.get("clock_rate")
    if isinstance(clock_rate, int) and clock_rate > 0:
        return clock_rate
    return FALLBACK_CAPTURE_RATE


async def _stop_bounded_command_child(proc) -> None:
    """Terminate a still-running command child and drain it terminally.

    terminate -> bounded communicate() (drains stdout+stderr) -> if the
    child ignores SIGTERM: kill -> bounded communicate().  Process and both
    pipes are thereby always worked off terminally; a final wait() guards
    against a pathological case where even the killed child's pipes never
    close.  Already-exited processes are handled cheaply.
    """
    if proc is None or proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(
            proc.communicate(), timeout=PEAK_MONITOR_COMMAND_TERMINATE_GRACE_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(
                proc.communicate(), timeout=PEAK_MONITOR_COMMAND_TERMINATE_GRACE_SECONDS
            )
        except asyncio.TimeoutError:
            await proc.wait()


async def _stop_bounded_command_child_cancellation_safe(proc) -> bool:
    """Stop and drain a command child shielded from caller cancellation.

    Runs the actual stop in its own task behind ``asyncio.shield``: even a
    second cancellation during the grace period cannot interrupt the
    terminate/grace/kill/pipe-drain sequence, so no child can be orphaned by
    caller cancellation.  Returns True when the caller was cancelled while
    draining; the caller must then propagate CancelledError (it wins over
    any timeout failure).  Cleanup errors are best-effort and swallowed.
    """
    if proc is None or proc.returncode is not None:
        return False
    cleanup_task = asyncio.create_task(_stop_bounded_command_child(proc))
    cancelled = False
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        cleanup_task.result()
    except Exception:
        logger.debug("Peak monitor command child cleanup failed", exc_info=True)
    return cancelled


async def _run_bounded_command(
    args: list[str],
    *,
    timeout: float | None = None,
) -> tuple[int, bytes, bytes]:
    """Run one short-lived pw-cli/pw-link command under a hard bound.

    On timeout the child is terminated, allowed a short grace period, then
    killed and fully reaped before the failure is raised.  Caller
    cancellation cleans up the child the same way and re-raises, so no
    command child can outlive the call.
    """
    if timeout is None:
        timeout = PEAK_MONITOR_COMMAND_TIMEOUT_SECONDS
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        if await _stop_bounded_command_child_cancellation_safe(proc):
            raise asyncio.CancelledError
        raise RuntimeError(f"{' '.join(args)} timed out after {timeout:g}s")
    except asyncio.CancelledError:
        await _stop_bounded_command_child_cancellation_safe(proc)
        raise
    return proc.returncode, stdout, stderr


@dataclass
class MonitorTarget:
    source_name: str
    source_id: int
    description: str
    serial: int = 0


@dataclass(frozen=True)
class StereoChunkMetrics:
    """Joint and per-channel peak/RMS for one frame-aligned stereo chunk."""

    peak: float
    rms: float
    peak_l: float
    rms_l: float
    peak_r: float
    rms_r: float


class DSPPeakMonitor:
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
        self._last_audio_sample_at: Optional[float] = None
        self._last_vu_emit_at = 0.0
        self._settle_until = 0.0
        self._settle_timeout_count = 0
        self._settle_rearmed = False
        self._hold_until_l = 0.0
        self._hold_until_r = 0.0
        self._last_over_at_l: Optional[float] = None
        self._last_over_at_r: Optional[float] = None
        self._consecutive_hits_l = 0
        self._consecutive_hits_r = 0
        self._vu_db_l: Optional[float] = None
        self._vu_db_r: Optional[float] = None
        self._last_vu_update_at_l: Optional[float] = None
        self._last_vu_update_at_r: Optional[float] = None
        self._pending_frame_bytes: bytes = b""

    async def start(self):
        if self._task and not self._task.done():
            logger.info("Peak monitor start skipped because task is already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="fxroute-dsp-peak-monitor")
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

    async def relink(self) -> bool:
        relink_started_at = time.monotonic()
        if not self._running or not self._proc or self._proc.returncode is not None:
            logger.info("Peak monitor relink skipped: process not running (running=%s proc_alive=%s)",
                        self._running,
                        bool(self._proc and self._proc.returncode is None))
            return False
        target = self._target
        capture_name = self._capture_node_name
        if not target or not capture_name:
            logger.info("Peak monitor relink skipped: no target/capture (target=%s capture=%s)",
                        target is not None, capture_name)
            return False
        try:
            logger.info("Peak monitor relink: repairing links for %s -> %s",
                        capture_name, target.source_name)
            await self._link_capture_stream(target, capture_name)
            self._last_error = None
            await self._emit_if_changed(force=True)
            logger.info("Peak monitor relink completed in %.3fs",
                        time.monotonic() - relink_started_at)
            return True
        except Exception as exc:
            logger.warning("Peak monitor relink failed: %s", exc)
            self._last_error = str(exc)
            await self._emit_if_changed(force=True)
            return False

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
        self._last_audio_sample_at = None
        self._last_vu_emit_at = 0.0
        self._settle_rearmed = False
        self._hold_until_l = 0.0
        self._hold_until_r = 0.0
        self._last_over_at_l = None
        self._last_over_at_r = None
        self._consecutive_hits_l = 0
        self._consecutive_hits_r = 0
        self._vu_db_l = None
        self._vu_db_r = None
        self._last_vu_update_at_l = None
        self._last_vu_update_at_r = None
        self._pending_frame_bytes = b""
        logger.info("Peak monitor stop completed in %.3fs (had_task=%s had_proc=%s)", time.monotonic() - stop_started_at, had_task, had_proc)

    def snapshot(self) -> dict:
        now = time.monotonic()
        active = now < self._hold_until
        hold_ms = max(0, int((self._hold_until - now) * 1000))
        sample_age_ms = (
            max(0, int((now - self._last_audio_sample_at) * 1000))
            if self._last_audio_sample_at is not None else None
        )
        return {
            "available": self._target is not None,
            "detected": active,
            "hold_ms": hold_ms,
            "threshold": PEAK_THRESHOLD,
            "vu_db": round(self._vu_db, 1) if self._vu_db is not None else None,
            "vu_db_l": round(self._vu_db_l, 1) if self._vu_db_l is not None else None,
            "vu_db_r": round(self._vu_db_r, 1) if self._vu_db_r is not None else None,
            "detected_l": now < self._hold_until_l,
            "detected_r": now < self._hold_until_r,
            "hold_ms_l": max(0, int((self._hold_until_l - now) * 1000)),
            "hold_ms_r": max(0, int((self._hold_until_r - now) * 1000)),
            "vu_fresh": bool(sample_age_ms is not None and sample_age_ms <= int(CAPTURE_NO_DATA_TIMEOUT * 1000)),
            "vu_age_ms": sample_age_ms,
            "target": {
                "source_id": self._target.source_id,
                "source_name": self._target.source_name,
                "description": self._target.description,
                "serial": self._target.serial,
            } if self._target else None,
            "last_over_at": self._last_over_at,
            "last_over_at_l": self._last_over_at_l,
            "last_over_at_r": self._last_over_at_r,
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
                logger.warning("FXRoute DSP peak monitor target discovery failed: %s", exc)
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
                previous_target = self._target
                was_recreated = (
                    previous_target is not None
                    and target.serial != previous_target.serial
                )
                first_target = previous_target is None
                self._target = target
                self._last_error = None
                if first_target or was_recreated:
                    self._settle_until = time.monotonic() + REBUILD_SETTLE_GRACE_SECONDS
                    if was_recreated:
                        logger.info(
                            "Peak monitor target node recreated (serial %s -> %s); "
                            "arming capture with rebuild settle grace",
                            previous_target.serial, target.serial,
                        )
                    else:
                        logger.info(
                            "Peak monitor first target armed with rebuild settle grace (serial %s)",
                            target.serial,
                        )
                await self._emit_if_changed(force=True)

            try:
                await self._capture_target(target)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("FXRoute DSP peak monitor capture failed: %s", exc)
                await self._emit_if_changed(force=True)
                await asyncio.sleep(ERROR_RETRY_INTERVAL)

    async def _capture_target(self, target: MonitorTarget):
        capture_started_at = time.monotonic()
        capture_node_name = f"{CAPTURE_NODE_NAME}_{next(CAPTURE_NODE_SEQUENCE)}"
        capture_rate = _resolve_capture_rate()
        self._capture_node_name = capture_node_name
        self._last_audio_sample_at = None
        self._pending_frame_bytes = b""
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
            "--rate",
            str(capture_rate),
            "-",
        ]
        logger.info(
            "Starting FXRoute DSP peak monitor on node %s (%s) at %s Hz",
            target.source_name,
            target.description,
            capture_rate,
        )
        self._last_error = None
        await self._emit_if_changed(force=True)
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info("Peak monitor pw-record spawned in %.3fs for capture node %s", time.monotonic() - capture_started_at, capture_node_name)
        assert self._proc.stdout is not None
        # Start the no-data clock before the initial link attempt.  A failed
        # first link is recoverable, but must still use the normal capture
        # timeout rather than reaching the read loop with an unbound clock.
        last_data_at = time.monotonic()
        try:
            try:
                link_started_at = time.monotonic()
                await self._link_capture_stream(target, capture_node_name)
                logger.info("Peak monitor link setup completed in %.3fs for capture node %s", time.monotonic() - link_started_at, capture_node_name)
                last_data_at = time.monotonic()
            except Exception as exc:
                logger.warning("Peak monitor link setup failed, continuing without capture links yet: %s", exc)
                self._last_error = str(exc)
                await self._emit_if_changed(force=True)
            self._settle_timeout_count = 0
            self._capture_armed_under_settle = self._settle_until > 0.0
            last_target_check_at = time.monotonic()
            while self._running:
                try:
                    chunk = await asyncio.wait_for(self._proc.stdout.read(READ_SIZE), timeout=0.25)
                except asyncio.TimeoutError:
                    chunk = b""
                now = time.monotonic()
                if chunk:
                    last_data_at = now
                    self._last_audio_sample_at = now
                    frames_bytes = self._align_stereo_frames(chunk)
                    if frames_bytes:
                        metrics = self._stereo_metrics(frames_bytes)
                        self._update_vu_db(self._linear_to_db(metrics.rms), now)
                        self._update_vu_db_l(self._linear_to_db(metrics.rms_l), now)
                        self._update_vu_db_r(self._linear_to_db(metrics.rms_r), now)
                        (
                            self._consecutive_hits,
                            self._hold_until,
                            joint_transitioned,
                        ) = self._update_peak_detection(
                            metrics.peak, now, self._consecutive_hits, self._hold_until
                        )
                        (
                            self._consecutive_hits_l,
                            self._hold_until_l,
                            left_transitioned,
                        ) = self._update_peak_detection(
                            metrics.peak_l, now, self._consecutive_hits_l, self._hold_until_l
                        )
                        (
                            self._consecutive_hits_r,
                            self._hold_until_r,
                            right_transitioned,
                        ) = self._update_peak_detection(
                            metrics.peak_r, now, self._consecutive_hits_r, self._hold_until_r
                        )
                        if joint_transitioned:
                            self._last_over_at = time.time()
                        if left_transitioned:
                            self._last_over_at_l = time.time()
                        if right_transitioned:
                            self._last_over_at_r = time.time()
                        if joint_transitioned:
                            await self._emit_if_changed(force=True)
                elif self._proc.returncode is not None:
                    break
                else:
                    if not (now < self._settle_until):
                        # A capture armed under the settle grace can develop
                        # periodic data gaps after the graph settled; rearm
                        # once so the fresh negotiation delivers a clean
                        # continuous stream.
                        if self._capture_armed_under_settle and not self._settle_rearmed:
                            self._settle_timeout_count += 1
                            if self._settle_timeout_count >= SETTLE_REARM_MIN_TIMEOUTS:
                                if await self._rate_change_in_progress():
                                    # The graph is still renegotiating a new
                                    # rate (sink rate lags the authoritative
                                    # rate, e.g. a rate-change transition that
                                    # also rebuilt the DSP).  Rearming now
                                    # would relaunch the capture at the stale
                                    # rate only to be re-armed again once the
                                    # node is recreated at the new rate; wait
                                    # for the sink to settle first.
                                    self._settle_timeout_count = 0
                                else:
                                    self._settle_rearmed = True
                                    self._settle_until = 0.0
                                    raise RuntimeError(
                                        "Peak monitor capture degraded after rebuild settle; "
                                        "rearming for a clean stream"
                                    )
                    if now - last_target_check_at >= TARGET_RECHECK_INTERVAL:
                        last_target_check_at = now
                        try:
                            current_target = await self._discover_target()
                        except Exception:
                            current_target = None
                        if current_target is not None and current_target != self._target:
                            raise RuntimeError(
                                "Peak monitor target node was recreated; rearming capture"
                            )
                    no_data_timeout = (
                        REBUILD_SETTLE_GRACE_SECONDS
                        if now < self._settle_until else CAPTURE_NO_DATA_TIMEOUT
                    )
                    if now - last_data_at >= no_data_timeout:
                        raise RuntimeError("Peak monitor received no audio data while pw-record remained running")
                    self._update_vu_db(VU_FLOOR_DB, now)
                    self._update_vu_db_l(VU_FLOOR_DB, now)
                    self._update_vu_db_r(VU_FLOOR_DB, now)
                hold_expired = False
                if self._hold_until and now >= self._hold_until:
                    self._hold_until = 0.0
                    hold_expired = True
                if self._hold_until_l and now >= self._hold_until_l:
                    self._hold_until_l = 0.0
                    hold_expired = True
                if self._hold_until_r and now >= self._hold_until_r:
                    self._hold_until_r = 0.0
                    hold_expired = True
                if hold_expired:
                    await self._emit_if_changed(force=True)
                elif now - self._last_vu_emit_at >= VU_EMIT_INTERVAL:
                    self._last_vu_emit_at = now
                    await self._emit_if_changed()
            now_exit = time.monotonic()
            exit_hold_expired = False
            if self._hold_until and now_exit >= self._hold_until:
                self._hold_until = 0.0
                exit_hold_expired = True
            if self._hold_until_l and now_exit >= self._hold_until_l:
                self._hold_until_l = 0.0
                exit_hold_expired = True
            if self._hold_until_r and now_exit >= self._hold_until_r:
                self._hold_until_r = 0.0
                exit_hold_expired = True
            if exit_hold_expired:
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
            self._consecutive_hits_l = 0
            self._consecutive_hits_r = 0
            if self._proc and self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=1)
                except Exception:
                    self._proc.kill()
            self._proc = None
            self._capture_node_name = None

    async def _rate_change_in_progress(self) -> bool:
        """True while the sink rate lags the authoritative rate.

        During a rate-change transition the DSP node is recreated at the new
        rate shortly after the force-rate is applied; the sink's active rate
        lags the authoritative rate until the renegotiation completes.  The
        settle-rearm defers while this is true so the fresh capture launches
        at the settled rate instead of the stale pre-transition rate.
        """
        try:
            status = get_samplerate_status()
        except Exception:
            return False
        authoritative = authoritative_sample_rate(status)
        sink_rate = status.get("active_rate")
        if not isinstance(authoritative, int) or authoritative <= 0:
            return False
        return not isinstance(sink_rate, int) or sink_rate != authoritative

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
            returncode, stdout, stderr = await _run_bounded_command(
                ["pw-cli", "ls", "Port"]
            )
            if returncode != 0:
                raise RuntimeError(stderr.decode(errors="ignore").strip() or "pw-cli ls Port failed")
            text = stdout.decode(errors="ignore")
            ports = list(self._iter_ports(text))
            for port in ports:
                alias = port["alias"]
                port_name = port["port_name"]
                node_id = port["node_id"]

                if alias == f"{capture_node_name}:input_FL":
                    capture_fl = alias
                elif alias == f"{capture_node_name}:input_FR":
                    capture_fr = alias

            target_fl, target_fr = self._target_output_ports(target, ports)

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
    def _target_output_ports(target: MonitorTarget, ports) -> tuple[str | None, str | None]:
        names = {
            port["port_name"]
            for port in ports
            if port["node_id"] == target.source_id
        }
        if {"post_effect_FL", "post_effect_FR"}.issubset(names):
            return (f"{target.source_name}:post_effect_FL",
                    f"{target.source_name}:post_effect_FR")
        return None, None

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
        returncode, _, stderr = await _run_bounded_command(
            ["pw-link", output_port, input_port]
        )
        if returncode != 0:
            message = stderr.decode(errors="ignore").strip() or f"pw-link failed: {output_port} -> {input_port}"
            lower = message.lower()
            if "file exists" in lower or "already linked" in lower:
                return
            raise RuntimeError(message)

    async def _discover_target(self) -> Optional[MonitorTarget]:
        discover_started_at = time.monotonic()
        returncode, stdout, stderr = await _run_bounded_command(
            ["pw-cli", "ls", "Node"]
        )
        if returncode != 0:
            raise RuntimeError(stderr.decode(errors="ignore").strip() or "pw-cli ls Node failed")

        text = stdout.decode(errors="ignore")
        candidates: list[tuple[int, MonitorTarget]] = []
        current_id = 0
        current_name = ""
        current_description = ""
        current_serial = 0

        def flush_current():
            nonlocal current_id, current_name, current_description, candidates
            node_name = current_name.strip()
            if not node_name:
                return
            if node_name != DSP_OUTPUT_NODE_NAME:
                return
            candidates.append((100, MonitorTarget(
                source_name=node_name,
                source_id=current_id,
                description=(current_description.strip() or node_name),
                serial=current_serial,
            )))

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("id ") and ", type PipeWire:Interface:Node" in line:
                flush_current()
                current_name = ""
                current_description = ""
                current_serial = 0
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
            if line.startswith('object.serial = "'):
                try:
                    current_serial = int(line.split('"', 1)[1].rsplit('"', 1)[0])
                except Exception:
                    current_serial = 0
        flush_current()

        if not candidates:
            logger.info("Peak monitor target discovery found no FXRoute DSP output node in %.3fs", time.monotonic() - discover_started_at)
            return None
        candidates.sort(key=lambda item: (-item[0], item[1].source_id))
        selected = candidates[0][1]
        logger.info("Peak monitor target discovery selected %s (id=%s) in %.3fs from %d candidate(s)", selected.source_name, selected.source_id, time.monotonic() - discover_started_at, len(candidates))
        return selected

    def _update_vu_db(self, target_db: float, now: float):
        self._vu_db, self._last_vu_update_at = self._smooth_vu_db(
            self._vu_db, self._last_vu_update_at, target_db, now
        )

    def _update_vu_db_l(self, target_db: float, now: float):
        self._vu_db_l, self._last_vu_update_at_l = self._smooth_vu_db(
            self._vu_db_l, self._last_vu_update_at_l, target_db, now
        )

    def _update_vu_db_r(self, target_db: float, now: float):
        self._vu_db_r, self._last_vu_update_at_r = self._smooth_vu_db(
            self._vu_db_r, self._last_vu_update_at_r, target_db, now
        )

    @staticmethod
    def _smooth_vu_db(
        current_db: Optional[float],
        last_update_at: Optional[float],
        target_db: float,
        now: float,
    ) -> tuple[float, float]:
        target_db = max(VU_FLOOR_DB, min(6.0, target_db))
        if current_db is None or last_update_at is None:
            return target_db, now
        elapsed = max(0.001, now - last_update_at)
        tau = VU_ATTACK_SECONDS if target_db > current_db else VU_RELEASE_SECONDS
        alpha = 1.0 - math.exp(-elapsed / tau)
        return current_db + ((target_db - current_db) * alpha), now

    @staticmethod
    def _linear_to_db(value: float) -> float:
        if not math.isfinite(value) or value <= 0.0:
            return VU_FLOOR_DB
        return 20.0 * math.log10(value)

    def _align_stereo_frames(self, chunk: bytes) -> bytes:
        """Buffer ``chunk`` and return the complete stereo frames available.

        PipeWire ``stdout.read()`` may end in the middle of an interleaved
        stereo frame; the trailing partial frame (fewer than 8 bytes) stays
        buffered for the next read so L/R never swap across arbitrary chunk
        boundaries.
        """
        self._pending_frame_bytes += chunk
        usable = len(self._pending_frame_bytes) - (len(self._pending_frame_bytes) % 8)
        if usable <= 0:
            return b""
        frames = self._pending_frame_bytes[:usable]
        self._pending_frame_bytes = self._pending_frame_bytes[usable:]
        return frames

    @staticmethod
    def _stereo_metrics(frame_bytes: bytes) -> StereoChunkMetrics:
        """Joint and per-channel peak/RMS for interleaved f32 stereo frames.

        ``frame_bytes`` must be a multiple of 8 bytes (complete L/R frames).
        Joint peak is the max absolute sample across both channels; joint RMS
        is over all samples, matching the pre-stereo single-stream behavior.
        """
        peak = 0.0
        sum_squares = 0.0
        count = 0
        peak_l = 0.0
        sum_squares_l = 0.0
        count_l = 0
        peak_r = 0.0
        sum_squares_r = 0.0
        count_r = 0
        for idx, (sample,) in enumerate(struct.iter_unpack("<f", frame_bytes)):
            if not math.isfinite(sample):
                continue
            value = abs(sample)
            square = float(sample) * float(sample)
            if value > peak:
                peak = value
            sum_squares += square
            count += 1
            if idx % 2 == 0:
                if value > peak_l:
                    peak_l = value
                sum_squares_l += square
                count_l += 1
            else:
                if value > peak_r:
                    peak_r = value
                sum_squares_r += square
                count_r += 1
        return StereoChunkMetrics(
            peak=peak,
            rms=math.sqrt(sum_squares / count) if count else 0.0,
            peak_l=peak_l,
            rms_l=math.sqrt(sum_squares_l / count_l) if count_l else 0.0,
            peak_r=peak_r,
            rms_r=math.sqrt(sum_squares_r / count_r) if count_r else 0.0,
        )

    @staticmethod
    def _update_peak_detection(
        peak: float,
        now: float,
        consecutive_hits: int,
        hold_until: float,
    ) -> tuple[int, float, bool]:
        """Advance the consecutive-hit/hold peak detector for one channel.

        Returns ``(consecutive_hits, hold_until, transitioned)`` where
        ``transitioned`` is True only when a fresh false->true hold starts.
        """
        if peak >= PEAK_THRESHOLD:
            consecutive_hits += 1
            if consecutive_hits >= CONSECUTIVE_HITS_REQUIRED:
                was_detected = now < hold_until
                hold_until = now + HOLD_SECONDS
                return consecutive_hits, hold_until, not was_detected
        else:
            consecutive_hits = 0
        return consecutive_hits, hold_until, False


@dataclass(frozen=True)
class PeakMonitorCoordinatorDeps:
    """Application services injected from main.py.

    All entries resolve the current runtime state at call time, so tests
    that patch ``main.runtime`` / ``main.playback_state`` attributes observe
    the patched services.
    """

    get_peak_monitor: Callable[[], Any]
    get_player_state: Callable[[], dict]
    get_current_track_info: Callable[[], Any]
    broadcast: Callable[[dict], Awaitable[Any]]
    get_spotify_ui_state: Callable[..., Awaitable[Any]]
    get_audio_source_overview: Callable[[], dict]
    capture_transition_epoch: Callable[[], int | None]
    transition_context_is_current: Callable[[int | None], bool]
    transition_is_active: Callable[[], bool]
    sleep: Callable[[float], Awaitable[None]]


class PeakMonitorCoordinator:
    """Own the peak-monitor state machine (armed/signature/lock).

    Decides when the :class:`DSPPeakMonitor` process is started, stopped or
    relinked based on the committed playback, Spotify and source-mode
    context.  The mutable state (``armed``, ``signature``, ``lock``) and the
    three sync entry points moved here from ``main.py`` so the monitor
    lifecycle has a single owner; ``main.py`` keeps thin wrappers for the
    existing dependency wiring and test patching contract.
    """

    def __init__(self, deps: PeakMonitorCoordinatorDeps) -> None:
        self._deps = deps
        self.armed = False
        self.signature: Any = None
        self.lock: asyncio.Lock | None = None

    def reset(self) -> None:
        """Clear the coordination state at startup.

        The lock is dropped so it rebinds to the running event loop; armed
        is cleared so the first playback/Spotify/source sync re-arms the
        monitor.
        """
        self.armed = False
        self.signature = None
        self.lock = None

    def set_signature(self, value: Any) -> None:
        """Forget the committed context so the next sync performs a restart."""
        self.signature = value

    def _peak_monitor(self) -> Any:
        return self._deps.get_peak_monitor()

    def _get_lock(self) -> asyncio.Lock:
        if self.lock is None:
            self.lock = asyncio.Lock()
        return self.lock

    async def _broadcast_snapshot(self) -> None:
        peak_monitor = self._peak_monitor()
        if peak_monitor is None:
            return
        await self._deps.broadcast(
            {"type": "playback_peak_warning", "data": peak_monitor.snapshot()}
        )

    async def sync_playback_state(
        self,
        state: dict,
        transition_generation: int | None = None,
    ) -> None:
        if self._peak_monitor() is None:
            return
        if transition_generation is None:
            transition_generation = self._deps.capture_transition_epoch()
        if not self._deps.transition_context_is_current(transition_generation):
            return
        async with self._get_lock():
            if not self._deps.transition_context_is_current(transition_generation):
                return
            is_active_playback = playback_state.is_local_playback_active(state)
            current_track = self._deps.get_current_track_info()
            source = (current_track or {}).get("source") or "unknown"
            state_matches_track = playback_state.playback_state_matches_track(
                state, current_track
            )
            if is_active_playback and not state_matches_track and self.armed:
                logger.info(
                    "Skipping peak monitor resync during unsettled player transition: source=%s state_file=%s track_url=%s track_id=%s",
                    source,
                    state.get("current_file"),
                    (current_track or {}).get("url"),
                    (current_track or {}).get("id"),
                )
                return
            desired_signature = f"player:{source}:{state.get('current_file') or ''}" if is_active_playback else None

            if is_active_playback:
                # Resume from pause/inactive with same source:
                # only restart the peak monitor — do NOT reload the DSP
                # preset or repair the output graph, which causes an audible crack.
                if not self.armed and self.signature == desired_signature:
                    self.armed = True
                    logger.info(
                        "Repairing peak monitor links after pause (same source, relink only): %s",
                        desired_signature,
                    )
                    # Peak monitor process was kept running but PipeWire links are
                    # dropped during pause. Repair links without restarting the
                    # pw-record process to avoid audible cracks.
                    relinked = await self._peak_monitor().relink()
                    if not relinked:
                        logger.warning(
                            "Peak monitor relink failed; falling back to full restart: %s",
                            desired_signature,
                        )
                        await self._peak_monitor().restart()
                    await self._broadcast_snapshot()
                elif self.signature != desired_signature:
                    self.armed = True
                    self.signature = desired_signature
                    if not self._deps.transition_context_is_current(transition_generation):
                        return
                    logger.info(
                        "Restarting peak monitor on committed playback context change; production graph remains coordinator-owned: %s",
                        desired_signature,
                    )
                    await self._peak_monitor().restart()
                    await self._broadcast_snapshot()
            elif (
                not is_active_playback
                and self.armed
                and str(self.signature or "").startswith("player:")
            ):
                await self._deps.sleep(PEAK_MONITOR_INACTIVE_GRACE_MS / 1000)
                refreshed_player_state = self._deps.get_player_state()
                if playback_state.is_local_playback_active(refreshed_player_state):
                    return
                spotify_state = await self._deps.get_spotify_ui_state()
                if spotify_state.get("available") and spotify_state.get("status") == "Playing":
                    return
                # Keep the peak monitor process running through pauses to avoid
                # pw-record restart + PipeWire link glitches on resume.
                # Mark as not armed so the resume path will trigger relink().
                logger.info(
                    "Peak monitor pausing (process stays alive, armed=False): signature=%s",
                    self.signature,
                )
                self.armed = False
                # self.signature is preserved for same-source resume detection.

    async def sync_spotify_state(self, data: dict) -> None:
        if self._peak_monitor() is None:
            return
        async with self._get_lock():
            player_state = self._deps.get_player_state()
            is_spotify_playing = playback_state.is_spotify_playback_active(data)
            desired_signature = "spotify:playing" if is_spotify_playing else None

            if is_spotify_playing and (not self.armed or self.signature != desired_signature):
                if self._deps.transition_is_active():
                    logger.info("Delaying peak monitor restart while Spotify samplerate recovery is active")
                    return
                self.armed = True
                self.signature = desired_signature
                logger.info(
                    "Starting peak monitor for committed Spotify playback; rate/graph mutations remain coordinator-owned",
                )
                await self._peak_monitor().restart()
                await self._broadcast_snapshot()
            elif (
                not is_spotify_playing
                and self.armed
                and str(self.signature or "").startswith("spotify:")
            ):
                if self._deps.transition_is_active():
                    logger.info("Keeping peak monitor armed while Spotify samplerate recovery is active")
                    return
                await self._deps.sleep(PEAK_MONITOR_INACTIVE_GRACE_MS / 1000)
                refreshed_player_state = self._deps.get_player_state()
                refreshed_spotify_state = await self._deps.get_spotify_ui_state()
                if self._deps.transition_is_active():
                    logger.info("Keeping peak monitor armed while Spotify samplerate recovery is still active")
                    return
                if playback_state.is_local_playback_active(refreshed_player_state):
                    return
                if playback_state.is_spotify_playback_active(refreshed_spotify_state):
                    return
                logger.info("Stopping peak monitor because Spotify is no longer actively playing")
                await self._peak_monitor().stop()
                self.armed = False
                self.signature = None
                await self._broadcast_snapshot()

    async def sync_source_mode_state(self, source_overview: dict | None = None) -> None:
        if self._peak_monitor() is None:
            return
        async with self._get_lock():
            overview = source_overview or self._deps.get_audio_source_overview()
            bluetooth = overview.get("bluetooth") or {}
            is_bt_streaming = bool(
                overview.get("mode") == SOURCE_MODE_BLUETOOTH_INPUT
                and bluetooth.get("state") == "streaming"
                and bluetooth.get("connected_device")
            )
            desired_signature = None
            if is_bt_streaming:
                desired_signature = f"bluetooth:{bluetooth.get('connected_device')}:{bluetooth.get('active_codec') or ''}"

            if is_bt_streaming and (not self.armed or self.signature != desired_signature):
                self.armed = True
                self.signature = desired_signature
                logger.info("Starting peak monitor for active Bluetooth input: %s", desired_signature)
                await self._peak_monitor().restart()
                await self._broadcast_snapshot()
            elif (not is_bt_streaming) and self.armed and str(self.signature or "").startswith("bluetooth:"):
                player_state = self._deps.get_player_state()
                spotify_state = await self._deps.get_spotify_ui_state()
                if not playback_state.is_local_playback_active(player_state) and not playback_state.is_spotify_playback_active(spotify_state):
                    logger.info("Stopping peak monitor because Bluetooth input is no longer actively streaming")
                    await self._peak_monitor().stop()
                    self.armed = False
                    self.signature = None
                    await self._broadcast_snapshot()
