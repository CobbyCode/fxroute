"""PipeWire-native subwoofer Stage-3 runtime controller.

Supports 2.1 (Out 1/2 = highpassed L/R, Out 3/4 = lowpassed (L+R)*0.5)
and 2.2 mono/stereo bass modes (Out 1/2 = highpassed L/R, Out 3/4 = subs
with independent level/alignment/polarity).

Stereo mode does not use this module; the existing EasyEffects/stereo path stays
unchanged. The old subprocess bridge has been removed. This runtime owns
only the native helper lifecycle and graph links.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import select
import signal
import shlex
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Awaitable, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

OUTPUT_MODE_SUBWOOFER_21 = "subwoofer-2.1"
OUTPUT_MODE_SUBWOOFER_22 = "subwoofer-2.2"
OUTPUT_MODE_SUBWOOFER_22_STEREO = "subwoofer-2.2-stereo"
SUBWOOFER_22_MODES = {OUTPUT_MODE_SUBWOOFER_22, OUTPUT_MODE_SUBWOOFER_22_STEREO}
SUBWOOFER_MODES = {OUTPUT_MODE_SUBWOOFER_21, *SUBWOOFER_22_MODES}
DEFAULT_SAMPLE_RATE = 48_000
NATIVE_HELPER_PENDING_MESSAGE = "PipeWire-native subwoofer helper binary is not available"
NATIVE_HELPER_NODE_NAME = "fxroute_21_stage1"
# Bounded tail of the native helper stderr diagnostics (~64 KiB).
HELPER_STDERR_TAIL_LIMIT = 64 * 1024
NATIVE_HELPER_PORTS = ("input_L", "input_R", "output_1", "output_2", "output_3", "output_4")
EASYEFFECTS_SINK_NAME = "easyeffects_sink"
PIPEWIRE_LINK_RETRY_ATTEMPTS = 8
PIPEWIRE_LINK_RETRY_DELAY_SECONDS = 0.15

# Hard bound for every short-lived pw-link/pgrep/pkill command spawned by
# the runtime controller: a wedged PipeWire/BlueZ daemon must never hold
# ``_sync_lock``/``_reclean_lock`` (and through them playback transitions and
# output-mode changes) indefinitely.  Matches samplerate._run_command's
# project-wide 5 s query bound.
RUNTIME_COMMAND_TIMEOUT_SECONDS = 5.0
# Grace between terminate and kill when a bounded command child ignores
# SIGTERM; matches the project subprocess-stop convention.
RUNTIME_COMMAND_TERMINATE_GRACE_SECONDS = 2.0
# Synthetic returncode for a timed-out command: every caller treats a
# nonzero returncode as failure, so the timeout degrades exactly like a
# normal command failure instead of success.
RUNTIME_COMMAND_TIMEOUT_RETURNCODE = -1


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class PipeWireLink:
    source: str
    target: str


CommandRunner = Callable[[Sequence[str]], Awaitable[CommandResult]]
ProcessLauncher = Callable[[Sequence[str]], Awaitable[Any]]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class SubwooferRuntimeConfig:
    output_mode: str
    output_key: str
    output_label: str
    output_channels: int
    sample_rate: int
    crossover_frequency_hz: int
    main_highpass_enabled: bool
    sub_level_db: float
    sub_alignment_ms: float
    sub_polarity: str
    # 2.2 sub2 fields (default to sub1 values for 2.1 compatibility)
    sub2_level_db: float = 0.0
    sub2_alignment_ms: float = 0.0
    sub2_polarity: str = "normal"

    @property
    def derived_main_delay_ms(self) -> float:
        """
        Combined main delay from both sub alignments.

        2.1: positive delays sub, negative delays main -> max(0, -sub_alignment_ms)
        2.2: resolves from the most negative alignment across both subs:
             max(0, -min(sub1_alignment_ms, sub2_alignment_ms))

        Example (2.2): Sub1=-2, Sub2=-5 -> Main=5
        """
        if self.output_mode in SUBWOOFER_22_MODES:
            return max(0.0, -min(self.sub_alignment_ms, self.sub2_alignment_ms))
        return max(0.0, -self.sub_alignment_ms)

    @property
    def derived_sub_delay_ms(self) -> float:
        """
        Legacy sub delay for 2.1 backward compatibility.
        2.1: max(0, sub_alignment_ms)
        2.2: same as derived_sub1_delay_ms (maps to existing schema).
        """
        if self.output_mode in SUBWOOFER_22_MODES:
            return self.derived_main_delay_ms + self.sub_alignment_ms
        return max(0.0, self.sub_alignment_ms)

    @property
    def derived_sub1_delay_ms(self) -> float:
        """
        2.2: sub1 delay = combined_main_delay + sub1_alignment
        2.1: same as derived_sub_delay_ms (# same formula)
        """
        if self.output_mode in SUBWOOFER_22_MODES:
            return self.derived_main_delay_ms + self.sub_alignment_ms
        return max(0.0, self.sub_alignment_ms)

    @property
    def derived_sub2_delay_ms(self) -> float:
        """
        2.2: sub2 delay = combined_main_delay + sub2_alignment
        2.1: mirrors sub1 so helper output_3 and output_4 stay identical
        """
        if self.output_mode in SUBWOOFER_22_MODES:
            return self.derived_main_delay_ms + self.sub2_alignment_ms
        return self.derived_sub_delay_ms

    @property
    def bass_routing(self) -> str:
        return "stereo" if self.output_mode == OUTPUT_MODE_SUBWOOFER_22_STEREO else "mono"

    @classmethod
    def from_overview(cls, overview: dict[str, Any]) -> "SubwooferRuntimeConfig":
        output_mode = overview.get("output_mode") or {}
        output = overview.get("selected_output") or overview.get("current_output") or {}
        output_key = str(output_mode.get("effective_output_key") or output.get("key") or output.get("name") or "").strip()
        output_label = str(output.get("label") or output.get("target_label") or output_key or "unknown output").strip()
        channels = _coerce_int(output_mode.get("effective_output_channels") or output.get("channels"), 0)
        sample_rate = _coerce_int(output_mode.get("effective_output_rate") or output.get("active_rate") or overview.get("active_rate"), DEFAULT_SAMPLE_RATE)
        if sample_rate <= 0:
            sample_rate = DEFAULT_SAMPLE_RATE

        mode = str(output_mode.get("mode") or "stereo")

        if mode in SUBWOOFER_22_MODES:
            # 2.2: global fields at top level, per-sub in subwoofers
            subwoofers = output_mode.get("subwoofers") or {}
            sub1 = subwoofers.get("sub1") or {}
            sub2 = subwoofers.get("sub2") or {}
            return cls(
                output_mode=mode,
                output_key=output_key,
                output_label=output_label,
                output_channels=channels,
                sample_rate=sample_rate,
                crossover_frequency_hz=_clamp_int(output_mode.get("crossover_frequency_hz"), 40, 200, 80),
                main_highpass_enabled=bool(output_mode.get("main_highpass_enabled", True)),
                sub_level_db=_clamp_float(sub1.get("level_db"), -80.0, 12.0, 0.0),
                sub_alignment_ms=_clamp_float_alignment(sub1, key="alignment_ms"),
                sub_polarity=_normalize_polarity(sub1.get("polarity")),
                sub2_level_db=_clamp_float(sub2.get("level_db"), -80.0, 12.0, 0.0),
                sub2_alignment_ms=_clamp_float_alignment(sub2, key="alignment_ms"),
                sub2_polarity=_normalize_polarity(sub2.get("polarity")),
            )

        # 2.1 or stereo: read from subwoofer block
        subwoofer = output_mode.get("subwoofer") or {}
        sub_level_db = _clamp_float(subwoofer.get("sub_level_db"), -24.0, 12.0, 0.0)
        sub_alignment_ms = _clamp_float_alignment(subwoofer)
        sub_polarity = _normalize_polarity(subwoofer.get("sub_polarity"))
        return cls(
            output_mode=mode,
            output_key=output_key,
            output_label=output_label,
            output_channels=channels,
            sample_rate=sample_rate,
            crossover_frequency_hz=_clamp_int(subwoofer.get("crossover_frequency_hz"), 40, 200, 80),
            main_highpass_enabled=bool(subwoofer.get("main_highpass_enabled", True)),
            sub_level_db=sub_level_db,
            sub_alignment_ms=sub_alignment_ms,
            sub_polarity=sub_polarity,
            sub2_level_db=sub_level_db,
            sub2_alignment_ms=sub_alignment_ms,
            sub2_polarity=sub_polarity,
        )


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(low, min(high, parsed))


def _normalize_polarity(raw: Any) -> str:
    return "invert" if str(raw or "").lower() in {"invert", "inverted", "180"} else "normal"


def _clamp_float_alignment(subwoofer: dict[str, Any], key: str = "sub_alignment_ms") -> float:
    """Parse signed alignment_ms from payload."""
    raw_alignment = subwoofer.get(key)
    try:
        parsed = float(raw_alignment)
    except (TypeError, ValueError):
        parsed = 0.0
    if not math.isfinite(parsed):
        parsed = 0.0
    return max(-40.0, min(40.0, round(parsed, 2)))


def _contains_link(text: str, source: str, target: str) -> bool:
    if source not in text or target not in text:
        return False
    direct = f"{source} -> {target}"
    reverse_pw_link_io = f"{target}\n  |<- {source}"
    forward_pw_link_io = f"{source}\n  |-> {target}"
    return direct in text or reverse_pw_link_io in text or forward_pw_link_io in text


async def _stop_command_child(process: Any, *, grace_seconds: float) -> None:
    """Terminate a still-running command child and drain it terminally.

    terminate -> bounded communicate() (drains stdout+stderr) -> if the
    child ignores SIGTERM: kill -> bounded communicate().  Process and both
    pipes are thereby always worked off terminally; a final wait() guards
    against a pathological case where even the killed child's pipes never
    close.  Already-exited processes are handled cheaply.
    """
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.communicate(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        process.kill()
        try:
            await asyncio.wait_for(process.communicate(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            await process.wait()


async def _stop_command_child_cancellation_safe(process: Any, *, grace_seconds: float) -> bool:
    """Stop and drain a command child shielded from caller cancellation.

    Runs the actual stop in its own task behind ``asyncio.shield``: even a
    second cancellation during the grace period cannot interrupt the
    terminate/grace/kill/pipe-drain sequence, so no child can be orphaned by
    caller cancellation.  Returns True when the caller was cancelled while
    draining; the caller must then propagate CancelledError (it wins over
    any timeout failure).  Cleanup errors are best-effort and swallowed.
    """
    if process is None or process.returncode is not None:
        return False
    cleanup_task = asyncio.create_task(
        _stop_command_child(process, grace_seconds=grace_seconds)
    )
    cancelled = False
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        cleanup_task.result()
    except Exception:
        logger.debug("Subwoofer command child cleanup failed", exc_info=True)
    return cancelled


class Subwoofer21Runtime:
    """Own the native subwoofer helper process and PipeWire graph links.

    Handles both 2.1 (single sub) and 2.2 (dual sub) configurations.
    The helper binary receives all sub parameters; for 2.1 mode sub2 mirrors sub1.
    """

    def __init__(
        self,
        *,
        helper_binary: str | Path | None = None,
        command_runner: CommandRunner | None = None,
        process_launcher: ProcessLauncher | None = None,
        sleeper: Sleeper | None = None,
        helper_node_name: str = NATIVE_HELPER_NODE_NAME,
        helper_quantum: int = 1024,
    ):
        self._config: Optional[SubwooferRuntimeConfig] = None
        self._last_error: Optional[str] = None
        self._helper_binary = Path(
            helper_binary
            or os.environ.get("FXROUTE_21_HELPER_BINARY")
            or Path(__file__).resolve().parent / "pipewire_stage1" / "build" / "fxroute_21_passthrough"
        )
        self._command_runner = command_runner or self._run_command
        self._process_launcher = process_launcher or self._launch_process
        self._sleep = sleeper or asyncio.sleep
        self._helper_node_name = helper_node_name
        self._helper_quantum = helper_quantum
        self._process: Any = None
        self._last_started_at: Optional[float] = None
        self._last_helper_args: Optional[list[str]] = None
        self._links_configured = False
        self._removed_direct_front_links = 0
        self._current_stream_key: Optional[tuple[Any, ...]] = None
        self._linked_output_key: Optional[str] = None
        self._needs_measurement_prime = False
        self._sync_lock = asyncio.Lock()
        self._reclean_lock = asyncio.Lock()
        self._repair_counter = 0
        self._active_repairs = 0
        self._pending_config: Optional[SubwooferRuntimeConfig] = None
        self._exact_sub_mute = False
        self._exact_sub_mute_ack_socket: socket.socket | None = None
        self._exact_sub_mute_ack_path: str | None = None
        self._exact_sub_mute_ack_dir: str | None = None
        self._peak_control_socket: socket.socket | None = None
        self._peak_control_path: str | None = None
        self._peak_control_dir: str | None = None
        self._peak_client_path: str | None = None
        self._helper_stderr_drain_task: Optional[asyncio.Task[Any]] = None
        self._helper_stderr_tail: bytes = b""

    def __del__(self) -> None:
        self._close_exact_sub_mute_ack_socket()
        self._close_peak_control_socket()

    async def reset_output_peaks(self) -> None:
        """Reset native Stage1 Output 1-4 peaks for the next sweep."""
        # The helper's process callback may be dormant until sweep playback
        # starts. Queue the reset now; the callback applies it before
        # accounting its first output block.
        await self._peak_control_request(b"R", expect_reply=False)

    async def read_output_peaks(self) -> dict[str, float]:
        """Read native Stage1 Output 1-4 linear full-scale peaks."""
        payload = await self._peak_control_request(b"G", expect_reply=True)
        decoded = json.loads(payload.decode("utf-8"))
        return {key: float(decoded[key]) for key in ("output_1", "output_2", "output_3", "output_4")}

    async def _peak_control_request(self, command: bytes, *, expect_reply: bool) -> bytes:
        control_socket = self._peak_control_socket
        control_path = self._peak_control_path
        process = self._process
        if control_socket is None or not control_path or process is None or getattr(process, "returncode", None) is not None:
            raise RuntimeError("Subwoofer helper peak control is unavailable")
        while True:
            try:
                control_socket.recv(512)
            except BlockingIOError:
                break
        control_socket.sendto(command, control_path)
        if not expect_reply:
            return b""
        return await asyncio.to_thread(self._recv_peak_control_reply, control_socket)

    @staticmethod
    def _recv_peak_control_reply(control_socket: socket.socket) -> bytes:
        readable, _, _ = select.select([control_socket], [], [], 1.0)
        if not readable:
            raise RuntimeError("timeout reading native Stage1 output peaks")
        return control_socket.recv(512)

    def _close_peak_control_socket(self) -> None:
        control_socket = self._peak_control_socket
        client_path = self._peak_client_path
        control_path = self._peak_control_path
        directory = self._peak_control_dir
        self._peak_control_socket = None
        self._peak_client_path = None
        self._peak_control_path = None
        self._peak_control_dir = None
        if control_socket is not None:
            control_socket.close()
        for path in (client_path, control_path):
            if path:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
        if directory:
            try:
                os.rmdir(directory)
            except OSError:
                pass

    def _stream_key(self, config: SubwooferRuntimeConfig) -> tuple[Any, ...]:
        return (
            config.output_mode,
            config.bass_routing,
            config.output_key,
            config.output_channels,
            config.sample_rate,
            config.crossover_frequency_hz,
            1 if config.main_highpass_enabled else 0,
            config.sub_level_db,
            config.sub_alignment_ms,
            config.sub_polarity,
            config.sub2_level_db,
            config.sub2_alignment_ms,
            config.sub2_polarity,
        )

    def snapshot(self) -> dict[str, Any]:
        process_running = self._process is not None and getattr(self._process, "returncode", None) is None
        active = process_running and self._links_configured and self._last_error is None
        return {
            "active": active,
            "last_error": self._last_error,
            "last_started_at": self._last_started_at,
            "config": self._config.__dict__ if self._config else None,
            "stage": "stage4_sub_controls",
            "engine": "pipewire_native_helper",
            "implemented": True,
            "inactive_reason": self._last_error,
            "helper_binary": str(self._helper_binary),
            "helper_node": self._helper_node_name,
            "helper_args": self._last_helper_args,
            "helper_pid": getattr(self._process, "pid", None) if process_running else None,
            "links_configured": self._links_configured,
            "removed_direct_front_links": self._removed_direct_front_links,
            "exact_sub_mute": self._exact_sub_mute,
        }

    async def set_exact_sub_mute(self, enabled: bool) -> bool:
        """Set measurement-only digital zero on both sub outputs without graph churn.

        Returns the preceding state so callers can restore it transactionally.
        SIGUSR1/SIGUSR2 are handled by the running helper and only update the
        atomic DSP mute flag; the helper process and PipeWire graph stay intact.
        """
        process = self._process
        if process is None or getattr(process, "returncode", None) is not None:
            raise RuntimeError("Subwoofer helper is not running; exact sub mute unavailable")
        previous = self._exact_sub_mute
        requested = bool(enabled)
        if requested == previous:
            return previous
        pid = int(getattr(process, "pid", 0) or 0)
        if pid <= 0:
            raise RuntimeError("Subwoofer helper PID unavailable; exact sub mute unavailable")
        try:
            await self._signal_exact_sub_mute_and_wait_ack(process, requested)
        except Exception as exc:
            # A lost enable acknowledgement can mean that the helper applied the
            # mute but Python did not observe it. Actively restore the preceding
            # state; if that cannot be acknowledged, stop the helper so a hidden
            # partial mute can never survive into candidate scanning.
            try:
                await self._signal_exact_sub_mute_and_wait_ack(process, previous)
                self._exact_sub_mute = previous
            except Exception:
                await self._stop_helper()
            raise RuntimeError(f"Exact sub mute was not acknowledged by helper RT path: {exc}") from exc
        self._exact_sub_mute = requested
        logger.info("Subwoofer helper exact measurement mute=%s pid=%s", requested, pid)
        return previous

    async def _signal_exact_sub_mute_and_wait_ack(self, process: Any, requested: bool) -> None:
        ack_socket = self._exact_sub_mute_ack_socket
        if ack_socket is None:
            raise RuntimeError("exact-sub-mute acknowledgement socket unavailable")
        while True:
            try:
                ack_socket.recv(32)
            except BlockingIOError:
                break
        os.kill(int(process.pid), signal.SIGUSR1 if requested else signal.SIGUSR2)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 1.0
        expected = b"1" if requested else b"0"
        while loop.time() < deadline:
            if getattr(process, "returncode", None) is not None or self._process is not process:
                raise RuntimeError("helper process changed before acknowledgement")
            try:
                value = await asyncio.wait_for(loop.sock_recv(ack_socket, 1), timeout=max(0.001, deadline - loop.time()))
            except asyncio.TimeoutError:
                break
            if value == expected:
                return
        raise RuntimeError(f"timeout waiting for helper acknowledgement={expected.decode()}")

    def _close_exact_sub_mute_ack_socket(self) -> None:
        ack_socket = self._exact_sub_mute_ack_socket
        path = self._exact_sub_mute_ack_path
        directory = self._exact_sub_mute_ack_dir
        self._exact_sub_mute_ack_socket = None
        self._exact_sub_mute_ack_path = None
        self._exact_sub_mute_ack_dir = None
        if ack_socket is not None:
            ack_socket.close()
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        if directory:
            try:
                os.rmdir(directory)
            except FileNotFoundError:
                pass
            except OSError:
                logger.debug("Subwoofer helper ack directory %s left behind", directory)

    async def sync(self, config: SubwooferRuntimeConfig) -> None:
        self._pending_config = config
        if self._sync_lock.locked():
            logger.info(
                "2.1 runtime sync coalesced while reconfig is active: output_mode=%s crossover_hz=%s sample_rate=%s",
                config.output_mode,
                config.crossover_frequency_hz,
                config.sample_rate,
            )
            return

        async with self._sync_lock:
            while self._pending_config is not None:
                next_config = self._pending_config
                self._pending_config = None
                await self._sync_once(next_config)

    def _mode_label(self) -> str:
        if self._config and self._config.output_mode == OUTPUT_MODE_SUBWOOFER_22_STEREO:
            return "2.2 Stereo Bass"
        return "2.2" if self._config and self._config.output_mode == OUTPUT_MODE_SUBWOOFER_22 else "2.1"

    async def _sync_once(self, config: SubwooferRuntimeConfig) -> None:
        if config.output_mode not in SUBWOOFER_MODES:
            self._config = config
            await self.stop()
            await self._stop_orphan_helpers()
            self._last_error = None
            logger.info("Subwoofer runtime inactive: selected output mode is stereo")
            return
        if not config.output_key:
            self._config = config
            await self.stop()
            await self._stop_orphan_helpers()
            self._last_error = "Subwoofer requires a selected hardware output device"
            logger.warning("Subwoofer runtime inactive: %s", self._last_error)
            return
        elif config.output_channels < 4:
            self._config = config
            await self.stop()
            await self._stop_orphan_helpers()
            mode_num = "2.2 Stereo Bass" if config.output_mode == OUTPUT_MODE_SUBWOOFER_22_STEREO else "2.2" if config.output_mode == OUTPUT_MODE_SUBWOOFER_22 else "2.1"
            self._last_error = f"{mode_num} Subwoofer requires a selected output device with at least 4 channels"
            logger.warning("Subwoofer runtime inactive: %s", self._last_error)
            return
        if not self._helper_binary.exists():
            self._config = config
            await self.stop()
            await self._stop_orphan_helpers()
            self._last_error = f"{NATIVE_HELPER_PENDING_MESSAGE}: {self._helper_binary}"
            logger.warning("Subwoofer runtime inactive: %s", self._last_error)
            return

        stream_key = self._stream_key(config)
        if self._process is None or getattr(self._process, "returncode", None) is not None or self._current_stream_key != stream_key:
            is_dsp_reconfig = (
                self._links_configured
                and self._config is not None
                and self._config.output_mode in SUBWOOFER_MODES
            )
            if is_dsp_reconfig:
                self._needs_measurement_prime = True
                await self._stop_for_21_reconfig()
            else:
                await self.stop()
            await self._stop_orphan_helpers()
            try:
                await self._start_helper(config)
            except Exception as exc:
                mode_num = "2.2 Stereo Bass" if config.output_mode == OUTPUT_MODE_SUBWOOFER_22_STEREO else "2.2" if config.output_mode == OUTPUT_MODE_SUBWOOFER_22 else "2.1"
                if "did not expose expected ports" in str(exc):
                    # A transient PipeWire registry/name conflict can keep the
                    # node's ports from registering right after a graph
                    # handoff.  Retry the start once after a short settle
                    # before surfacing the failure.
                    logger.warning(
                        "%s helper ports did not appear in time; retrying once: %s",
                        mode_num, exc,
                    )
                    await self._stop_helper()
                    await self._sleep(0.5)
                    try:
                        await self._start_helper(config)
                    except Exception as retry_exc:
                        logger.error("Failed to start %s native helper (retry): %s", mode_num, retry_exc)
                        await self._stop_for_21_reconfig()
                        await self._stop_orphan_helpers()
                        self._config = config
                        self._current_stream_key = None
                        self._last_error = str(retry_exc)
                        return
                else:
                    logger.exception(f"Failed to start {mode_num} native helper")
                    await self._stop_for_21_reconfig()
                    await self._stop_orphan_helpers()
                    self._config = config
                    self._current_stream_key = None
                    self._last_error = str(exc)
                    return
            needs_configure = True
        else:
            needs_configure = (
                not self._links_configured
                or self._linked_output_key != config.output_key
            )

        if needs_configure:
            try:
                await self._configure_graph(config)
                try:
                    await self._verify_graph(config)
                except Exception:
                    process = self._process
                    if process is None or getattr(process, "returncode", None) is not None:
                        raise
                    await self._reclean_guarded(skip_if_locked=False)
                    await self._verify_graph(config)
                self._config = config
                self._current_stream_key = stream_key
                self._last_error = None
            except Exception as exc:
                logger.exception("Failed to configure native helper graph")
                await self._stop_for_21_reconfig()
                await self._stop_orphan_helpers()
                self._config = config
                self._current_stream_key = None
                self._last_error = str(exc)
                return
        else:
            self._config = config
            self._current_stream_key = stream_key
            self._last_error = None
            logger.info(
                "Subwoofer runtime no-op: matching helper already running output_mode=%s "
                "hardware_output=%s sample_rate=%s helper_pid=%s",
                config.output_mode,
                config.output_key,
                config.sample_rate,
                getattr(self._process, "pid", None),
            )

        mode_num = "2.2 Stereo Bass" if config.output_mode == OUTPUT_MODE_SUBWOOFER_22_STEREO else "2.2" if config.output_mode == OUTPUT_MODE_SUBWOOFER_22 else "2.1"
        routing_note = (
            "Out 1/2=LR24 highpassed L/R, Out 3=Left Sub lowpassed L, Out 4=Right Sub lowpassed R"
            if config.output_mode == OUTPUT_MODE_SUBWOOFER_22_STEREO
            else
            "Out 1/2=LR24 highpassed L/R, Out 3=Sub 1 (L+R)*0.5, Out 4=Sub 2 (L+R)*0.5"
            if config.output_mode == OUTPUT_MODE_SUBWOOFER_22
            else "Out 1/2=optional LR24 highpassed L/R, Out 3/4=LR24 lowpassed (L+R)*0.5"
        )
        logger.info(
            "%s runtime active: output_mode=%s hardware_output=%s sample_rate=%s "
            "crossover_hz=%s main_highpass_enabled=%s "
            "fixed_routing='%s'",
            mode_num,
            config.output_mode,
            config.output_key,
            config.sample_rate,
            config.crossover_frequency_hz,
            config.main_highpass_enabled,
            routing_note,
        )

    @property
    def needs_measurement_prime(self) -> bool:
        return self._needs_measurement_prime

    @property
    def sync_in_progress(self) -> bool:
        """Return whether a helper/link reconfiguration or link repair is running.

        The preset-load path repairs EE->helper links under ``_reclean_lock``
        after an EasyEffects pipeline rebuild dropped them; watchers must not
        start a full recovery against that in-flight repair.
        """
        return bool(
            self._sync_lock.locked()
            or self._reclean_lock.locked()
            or self._pending_config is not None
        )

    def clear_measurement_prime(self) -> None:
        self._needs_measurement_prime = False

    async def _stop_helper_and_links_only(self) -> None:
        """Remove helper graph links and stop the helper process.

        Does NOT restore direct EasyEffects -> hardware front links.
        Used for in-place 2.1 DSP reconfiguration where the output
        mode stays subwoofer-2.1 and only parameters change.
        Restoring direct front links during a reconfig can create a
        stale PipeWire graph transient that interferes with the next
        measurement capture path.
        """
        await self._remove_graph_links()
        await self._stop_helper()
        self._links_configured = False
        self._linked_output_key = None
        self._removed_direct_front_links = 0

    async def _stop_for_21_reconfig(self) -> None:
        """Stop helper and links only; keep 2.1 output mode context.

        Does not clear self._last_error or self._current_stream_key
        so the caller owns the transition state.
        """
        await self._stop_helper_and_links_only()

    async def stop(self) -> None:
        """Full stop for Stereo mode switch or shutdown.

        Removes helper links, stops the helper, and restores
        direct EasyEffects -> hardware front links so stereo
        playback works through the normal signal chain.
        """
        t0 = time.monotonic()
        output_key = self._linked_output_key or (self._config.output_key if self._config else "")
        logger.info("SUB-STOP begin: output_key=%s has_linked=%s", output_key, bool(self._linked_output_key))
        t1 = time.monotonic()
        cleanup_error: Exception | None = None
        cancelled = False
        try:
            await self._remove_graph_links()
        except asyncio.CancelledError:
            cancelled = True
        except Exception as exc:
            cleanup_error = exc
            logger.warning("Failed to remove subwoofer graph links during stop: %s", exc)
        t2 = time.monotonic()
        logger.info("SUB-STOP _remove_graph_links: %.0f ms", (t2 - t1) * 1000)
        try:
            await self._stop_helper()
        except asyncio.CancelledError:
            cancelled = True
        except Exception as exc:
            cleanup_error = cleanup_error or exc
            logger.warning("Failed to stop subwoofer helper: %s", exc)
        t3 = time.monotonic()
        logger.info("SUB-STOP _stop_helper: %.0f ms", (t3 - t2) * 1000)
        if output_key:
            try:
                await self._restore_direct_easyeffects_front_links(output_key)
                t4 = time.monotonic()
                logger.info("SUB-STOP _restore_direct_ee_links: %.0f ms", (t4 - t3) * 1000)
            except asyncio.CancelledError:
                cancelled = True
            except Exception as exc:
                logger.warning("Failed to restore Stereo EasyEffects front links during 2.1 stop (output_key=%s): %s", output_key, exc)
        self._last_error = None
        self._links_configured = False
        self._linked_output_key = None
        self._current_stream_key = None
        self._removed_direct_front_links = 0
        t_end = time.monotonic()
        logger.info("SUB-STOP total: %.0f ms", (t_end - t0) * 1000)
        if cancelled:
            raise asyncio.CancelledError
        if cleanup_error is not None:
            raise cleanup_error

    def _stage1_links(self, output_key: str) -> list[PipeWireLink]:
        return [
            PipeWireLink("ee_soe_output_level:output_FL", f"{self._helper_node_name}:input_L"),
            PipeWireLink("ee_soe_output_level:output_FR", f"{self._helper_node_name}:input_R"),
            PipeWireLink(f"{self._helper_node_name}:output_1", f"{output_key}:playback_FL"),
            PipeWireLink(f"{self._helper_node_name}:output_2", f"{output_key}:playback_FR"),
            PipeWireLink(f"{self._helper_node_name}:output_3", f"{output_key}:playback_RL"),
            PipeWireLink(f"{self._helper_node_name}:output_4", f"{output_key}:playback_RR"),
        ]

    def _direct_easyeffects_front_links(self, output_key: str) -> list[PipeWireLink]:
        return [
            PipeWireLink("ee_soe_output_level:output_FL", f"{output_key}:playback_FL"),
            PipeWireLink("ee_soe_output_level:output_FR", f"{output_key}:playback_FR"),
        ]

    async def _start_helper(self, config: SubwooferRuntimeConfig) -> None:
        self._close_exact_sub_mute_ack_socket()
        ack_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        ack_socket.setblocking(False)
        ack_dir = tempfile.mkdtemp(prefix="fxroute-exact-mute-")
        ack_path = str(Path(ack_dir) / "ack.sock")
        ack_socket.bind(ack_path)
        self._exact_sub_mute_ack_socket = ack_socket
        self._exact_sub_mute_ack_path = ack_path
        self._exact_sub_mute_ack_dir = ack_dir
        self._close_peak_control_socket()
        peak_dir = tempfile.mkdtemp(prefix="fxroute-stage-peaks-")
        peak_path = str(Path(peak_dir) / "control.sock")
        peak_client_path = str(Path(peak_dir) / "client.sock")
        peak_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        peak_socket.setblocking(False)
        peak_socket.bind(peak_client_path)
        self._peak_control_socket = peak_socket
        self._peak_control_path = peak_path
        self._peak_client_path = peak_client_path
        self._peak_control_dir = peak_dir
        args = [
            str(self._helper_binary),
            "--node-name",
            self._helper_node_name,
            "--rate",
            str(config.sample_rate),
            "--quantum",
            str(self._helper_quantum),
            "--lowpass-hz",
            str(config.crossover_frequency_hz),
            "--highpass-hz",
            str(config.crossover_frequency_hz if config.main_highpass_enabled else 0),
            "--bass-routing",
            config.bass_routing,
            "--sub-level-db",
            str(config.sub_level_db),
            "--sub-polarity",
            config.sub_polarity,
            "--main-delay-ms",
            str(config.derived_main_delay_ms),
            "--sub-delay-ms",
            str(config.derived_sub_delay_ms),
            "--sub2-level-db",
            str(config.sub2_level_db),
            "--sub2-polarity",
            config.sub2_polarity,
            "--sub2-delay-ms",
            str(config.derived_sub2_delay_ms),
            "--exact-sub-mute-ack",
            ack_path,
            "--peak-control",
            peak_path,
        ]
        mode_num = "2.2 Stereo Bass" if config.output_mode == OUTPUT_MODE_SUBWOOFER_22_STEREO else "2.2" if config.output_mode == OUTPUT_MODE_SUBWOOFER_22 else "2.1"
        self._last_helper_args = list(args)
        self._exact_sub_mute = False
        self._process = await self._process_launcher(args)
        self._last_started_at = time.time()
        self._start_helper_stderr_drain()
        logger.info(
            "Starting %s helper: pid=%s command=%s",
            mode_num,
            getattr(self._process, "pid", None),
            shlex.join(args),
        )
        await self._wait_for_helper_ports()

    async def _configure_graph(self, config: SubwooferRuntimeConfig) -> None:
        self._removed_direct_front_links = 0
        self._linked_output_key = config.output_key
        await self._remove_direct_easyeffects_front_links(config.output_key)
        for link in self._stage1_links(config.output_key):
            await self._unlink(link, ignore_errors=True)
            await self._link(link)
        # EasyEffects can reconnect its front outputs when the sink becomes active.
        # Remove them once more after the helper graph exists; steady-state event
        # repair belongs to a later graph-watch pass, not a polling loop here.
        await self._remove_direct_easyeffects_front_links(config.output_key)
        self._links_configured = True

    async def _verify_graph(self, config: SubwooferRuntimeConfig) -> None:
        result = await self._command_runner(["pw-link", "-l"])
        if result.returncode != 0:
            raise RuntimeError(f"pw-link -l failed while verifying 2.1 graph: {result.stderr or result.stdout}".strip())
        text = result.stdout or ""
        missing = [link for link in self._stage1_links(config.output_key) if not _contains_link(text, link.source, link.target)]
        if missing:
            formatted = ", ".join(f"{link.source} -> {link.target}" for link in missing)
            raise RuntimeError(f"2.1 helper graph verification failed; missing links: {formatted}")
        direct = [link for link in self._direct_easyeffects_front_links(config.output_key) if _contains_link(text, link.source, link.target)]
        if direct:
            formatted = ", ".join(f"{link.source} -> {link.target}" for link in direct)
            raise RuntimeError(f"2.1 helper graph verification failed; direct EasyEffects links remain: {formatted}")

    async def _remove_graph_links(self) -> None:
        output_key = self._linked_output_key or (self._config.output_key if self._config else "")
        if not output_key:
            return
        for link in self._stage1_links(output_key):
            await self._unlink(link, ignore_errors=True)
        self._links_configured = False
        self._linked_output_key = None

    async def _remove_direct_easyeffects_front_links(self, output_key: str) -> None:
        t_start = time.monotonic()
        direct_link_ids = await self._find_direct_easyeffects_front_link_ids(output_key)
        removed_links: list[str] = []
        noop_links: list[str] = []
        for item in direct_link_ids:
            link_id = str(item.get("link_id") or "").strip()
            if not link_id:
                continue
            source = str(item.get("source_port") or "")
            target = str(item.get("target_port") or "")
            result = await self._command_runner(["pw-link", "-d", link_id])
            if result.returncode == 0:
                self._removed_direct_front_links += 1
                removed_links.append(f"{source} -> {target} (id={link_id})")
            else:
                fallback = await self._unlink(
                    PipeWireLink(source, target),
                    ignore_errors=True,
                )
                if fallback.returncode == 0:
                    self._removed_direct_front_links += 1
                    removed_links.append(f"{source} -> {target} (fallback)")
                else:
                    message = (result.stderr or result.stdout or fallback.stderr or fallback.stdout or "").strip()
                    if "No such file or directory" in message:
                        noop_links.append(f"{source} -> {target}")
                        continue
                    logger.warning(
                        "Failed to remove direct EasyEffects front link by id=%s (%s -> %s): %s",
                        link_id,
                        source,
                        target,
                        message,
                    )
        for link in self._direct_easyeffects_front_links(output_key):
            source, target = link.source, link.target
            result = await self._unlink(link, ignore_errors=True)
            if result.returncode == 0:
                self._removed_direct_front_links += 1
                desc = f"{source} -> {target}"
                if desc not in removed_links:
                    removed_links.append(desc)
            elif "No such file or directory" in (result.stderr or result.stdout or ""):
                desc = f"{source} -> {target}"
                if desc not in noop_links:
                    noop_links.append(desc)
        t_ms = (time.monotonic() - t_start) * 1000
        if removed_links or noop_links:
            logger.info(
                "Subwoofer link repair _remove_direct: output_key=%s found=%d removed=%s noop=%s (%.1fms)",
                output_key,
                len(direct_link_ids),
                removed_links,
                noop_links,
                t_ms,
            )
        else:
            logger.debug(
                "Subwoofer link repair _remove_direct: output_key=%s found=%d no links to remove (%.1fms)",
                output_key,
                len(direct_link_ids),
                t_ms,
            )

    @staticmethod
    def _parse_pw_link_id_links(text: str) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        current_port = ""
        port_line = re.compile(r"^\s*(\d+)\s+(\S.*)$")
        link_line = re.compile(r"^\s*(\d+)\s+\|(<-|->)\s+(\d+)\s+(\S.*)$")
        for raw_line in (text or "").splitlines():
            match = link_line.match(raw_line)
            if match and current_port:
                link_id, direction, _other_id, other_port = match.groups()
                other_port = other_port.strip()
                if direction == "->":
                    source_port, target_port = current_port, other_port
                else:
                    source_port, target_port = other_port, current_port
                links.append(
                    {
                        "link_id": link_id,
                        "source_port": source_port,
                        "target_port": target_port,
                    }
                )
                continue
            match = port_line.match(raw_line)
            if match and "|" not in raw_line:
                current_port = match.group(2).strip()
        return links

    async def _find_direct_easyeffects_front_link_ids(self, output_key: str) -> list[dict[str, str]]:
        if not output_key:
            return []
        expected = {
            ("ee_soe_output_level:output_FL", f"{output_key}:playback_FL"),
            ("ee_soe_output_level:output_FR", f"{output_key}:playback_FR"),
        }
        result = await self._command_runner(["pw-link", "-lI"])
        if result.returncode != 0:
            logger.warning("Failed to list PipeWire link ids: %s", (result.stderr or result.stdout or "").strip())
            return []
        matches: list[dict[str, str]] = []
        for item in self._parse_pw_link_id_links(result.stdout or ""):
            if (item.get("source_port"), item.get("target_port")) in expected:
                matches.append({**item, "role": "direct-easyeffects-to-hardware"})
        return matches

    async def direct_easyeffects_front_links_present(self) -> bool:
        output_key = self._linked_output_key or (self._config.output_key if self._config else "")
        if not output_key:
            return False
        return bool(await self._find_direct_easyeffects_front_link_ids(output_key))

    async def _restore_direct_easyeffects_front_links(self, output_key: str) -> None:
        for link in self._direct_easyeffects_front_links(output_key):
            await self._unlink(link, ignore_errors=True)
            await self._link(link)

    async def _reclean_guarded(self, skip_if_locked: bool = False) -> bool:
        """Run reclean_direct_easyeffects_links under _reclean_lock.

        When skip_if_locked=True (watch-loop), returns False immediately if
        the lock is held by another repair. Otherwise blocks until available.

        Both caller paths use this same helper so they share one serialization
        point.
        """
        if skip_if_locked and self._reclean_lock.locked():
            return False

        async with self._reclean_lock:
            repair_id = self._repair_counter
            self._repair_counter += 1
            self._active_repairs += 1
            if self._active_repairs > 1:
                logger.warning(
                    "SUBLINK active_repairs=%d repair_id=%d",
                    self._active_repairs,
                    repair_id,
                )
            logger.info(
                "SUBLINK repair start repair_id=%d active_repairs=%d",
                repair_id,
                self._active_repairs,
            )
            try:
                await self.reclean_direct_easyeffects_links()
            finally:
                self._active_repairs -= 1
                logger.info(
                    "SUBLINK repair done repair_id=%d active_repairs=%d",
                    repair_id,
                    self._active_repairs,
                )
        return True

    async def reclean_direct_easyeffects_links(self) -> None:
        """Remove any direct EE front links that EasyEffects may have re-created
        after a preset load while the 2.1 helper graph owns the output.

        Removes direct ee_soe_output_level -> hardware playback links and
        re-verifies the helper input links from ee_soe_output_level ports.
        Does not touch helper output links or the helper process.
        """
        t_start = time.monotonic()
        if not self._links_configured or not self._linked_output_key:
            logger.debug(
                "Subwoofer link repair skipped: links_configured=%s linked_output_key=%s (%.1fms)",
                self._links_configured,
                self._linked_output_key,
                (time.monotonic() - t_start) * 1000,
            )
            return

        # --- check phase: snapshot PipeWire links before repair ---
        t_check_start = time.monotonic()
        direct_before = await self._find_direct_easyeffects_front_link_ids(self._linked_output_key)
        helper_input_left_before = await self._link_present(
            PipeWireLink(f"ee_soe_output_level:output_FL", f"{self._helper_node_name}:input_L")
        )
        helper_input_right_before = await self._link_present(
            PipeWireLink(f"ee_soe_output_level:output_FR", f"{self._helper_node_name}:input_R")
        )
        t_check_ms = (time.monotonic() - t_check_start) * 1000

        # --- repair phase ---
        t_repair_start = time.monotonic()
        removed_before = self._removed_direct_front_links
        await self._remove_direct_easyeffects_front_links(self._linked_output_key)
        removed_count = self._removed_direct_front_links - removed_before

        created_count = 0
        already_linked_count = 0
        # Re-verify helper input links (link-only; no unlink to avoid
        # tearing down active audio path). pw-link on existing link is a no-op;
        # missing links (e.g. after preset reload) are re-created.
        for channel, helper_input in [("FL", "input_L"), ("FR", "input_R")]:
            before = await self._link_present(
                PipeWireLink(f"ee_soe_output_level:output_{channel}", f"{self._helper_node_name}:{helper_input}")
            )
            link = PipeWireLink(f"ee_soe_output_level:output_{channel}", f"{self._helper_node_name}:{helper_input}")
            await self._link(link)
            after = await self._link_present(
                PipeWireLink(f"ee_soe_output_level:output_{channel}", f"{self._helper_node_name}:{helper_input}")
            )
            if not before and after:
                created_count += 1
            elif before and after:
                already_linked_count += 1
        t_repair_ms = (time.monotonic() - t_repair_start) * 1000

        # --- verify phase: snapshot PipeWire links after repair ---
        t_verify_start = time.monotonic()
        direct_after = await self._find_direct_easyeffects_front_link_ids(self._linked_output_key)
        t_verify_ms = (time.monotonic() - t_verify_start) * 1000

        t_total_ms = (time.monotonic() - t_start) * 1000
        logger.info(
            "Subwoofer link repair complete: output=%s direct_before=%d direct_after=%d removed=%d "
            "helper_left_before=%s helper_right_before=%s created=%d already_linked=%d "
            "check=%.1fms repair=%.1fms verify=%.1fms total=%.1fms",
            self._linked_output_key,
            len(direct_before),
            len(direct_after),
            removed_count,
            helper_input_left_before,
            helper_input_right_before,
            created_count,
            already_linked_count,
            t_check_ms,
            t_repair_ms,
            t_verify_ms,
            t_total_ms,
        )

    async def _wait_for_helper_ports(self) -> None:
        expected = [f"{self._helper_node_name}:{port}" for port in NATIVE_HELPER_PORTS]
        for _ in range(40):
            if self._process is not None and getattr(self._process, "returncode", None) is not None:
                raise RuntimeError(f"2.1 helper exited before exposing ports: returncode={self._process.returncode}")
            result = await self._command_runner(["pw-link", "-io"])
            if result.returncode == 0 and all(port in result.stdout for port in expected):
                return
            await self._sleep(0.1)
        detail = ""
        stderr = await self._read_helper_stderr()
        if stderr:
            detail += f"; helper stderr: {stderr.strip()}"
        similar = await self._similar_helper_ports()
        if similar:
            detail += f"; similar ports present: {similar}"
        raise RuntimeError(
            f"2.1 helper did not expose expected ports: {', '.join(expected)}{detail}"
        )

    async def _read_helper_stderr(self) -> str:
        """Return the tail of the helper stderr kept by the drain task.

        The continuous drain task owns the stderr pipe; this diagnostic
        read only consumes the bounded tail buffer so it can never race the
        drain or block on the pipe.
        """
        tail = self._helper_stderr_tail
        if not tail:
            return ""
        return tail.decode("utf-8", "replace")

    def _start_helper_stderr_drain(self) -> None:
        """Own a continuous stderr drain for the running helper process.

        The native helper writes diagnostics to stderr; without a
        continuous reader a sufficiently large stderr output can fill the
        kernel pipe buffer and block the real-time helper.  Only a bounded
        tail is retained, so the pipe never stalls and RAM stays limited.
        """
        process = self._process
        stderr = getattr(process, "stderr", None)
        if stderr is None:
            return
        self._helper_stderr_tail = b""
        task = asyncio.create_task(
            self._drain_helper_stderr(),
            name=f"subwoofer-helper-stderr-drain:{self._helper_node_name}",
        )
        self._helper_stderr_drain_task = task
        task.add_done_callback(self._helper_stderr_drain_done)

    def _helper_stderr_drain_done(self, task: asyncio.Task[Any]) -> None:
        if self._helper_stderr_drain_task is task:
            self._helper_stderr_drain_task = None

    async def _drain_helper_stderr(self) -> None:
        """Read helper stderr continuously into the bounded tail buffer."""
        process = self._process
        stderr = getattr(process, "stderr", None)
        if stderr is None:
            return
        try:
            while True:
                chunk = await stderr.read(4096)
                if not chunk:
                    break
                self._helper_stderr_tail = (
                    self._helper_stderr_tail + chunk
                )[-HELPER_STDERR_TAIL_LIMIT:]
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to drain 2.1 helper stderr")

    async def _stop_helper_stderr_drain(self) -> None:
        """Cancel and drain the owned helper stderr drain task.

        Safe to call from a cancellation path: the drain task is cancelled
        synchronously, and a re-entrant CancelledError here does not leave
        any task behind (the event loop reaps cancelled tasks).
        """
        task = self._helper_stderr_drain_task
        self._helper_stderr_drain_task = None
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.gather(task, return_exceptions=True)
        except asyncio.CancelledError:
            pass

    async def _similar_helper_ports(self) -> list[str]:
        try:
            result = await self._command_runner(["pw-link", "-io"])
        except Exception:
            return []
        return sorted({
            line.strip()
            for line in (result.stdout or "").splitlines()
            if self._helper_node_name in line
        })

    async def _link_present(self, link: PipeWireLink) -> bool:
        result = await self._command_runner(["pw-link", "-l"])
        if result.returncode != 0:
            return False
        return _contains_link(result.stdout or "", link.source, link.target)

    async def _link(self, link: PipeWireLink) -> CommandResult:
        last_result: CommandResult | None = None
        for attempt in range(1, PIPEWIRE_LINK_RETRY_ATTEMPTS + 1):
            result = await self._command_runner(["pw-link", link.source, link.target])
            last_result = result
            if result.returncode == 0:
                logger.info("Subwoofer link repair created new link: %s -> %s", link.source, link.target)
                return result

            message = f"{result.stderr or result.stdout}".strip()
            if "File exists" in message:
                logger.debug("Subwoofer link repair link already exists: %s -> %s", link.source, link.target)
                return result

            # During a sample-rate handoff PipeWire can expose the helper
            # ports before EasyEffects has recreated its output ports.  A
            # transient ENOENT from pw-link is therefore retryable; other
            # errors remain fatal and are surfaced immediately.
            if "No such file or directory" not in message or attempt >= PIPEWIRE_LINK_RETRY_ATTEMPTS:
                raise RuntimeError(
                    f"pw-link failed: {link.source} -> {link.target}: {result.stderr or result.stdout}"
                .strip())
            logger.info(
                "Subwoofer link pending PipeWire port recreation; retrying: "
                "attempt=%s/%s link=%s -> %s error=%s",
                attempt, PIPEWIRE_LINK_RETRY_ATTEMPTS, link.source, link.target, message,
            )
            await self._sleep(PIPEWIRE_LINK_RETRY_DELAY_SECONDS)

        # The loop always returns or raises; retain a defensive failure for
        # type-checkers and unexpected future changes to the retry policy.
        assert last_result is not None
        raise RuntimeError(
            f"pw-link failed: {link.source} -> {link.target}: "
            f"{last_result.stderr or last_result.stdout}".strip()
        )

    async def _unlink(self, link: PipeWireLink, *, ignore_errors: bool = False) -> CommandResult:
        result = await self._command_runner(["pw-link", "-d", link.source, link.target])
        if result.returncode != 0 and not ignore_errors:
            raise RuntimeError(f"pw-link -d failed: {link.source} -> {link.target}: {result.stderr or result.stdout}".strip())
        return result

    async def _stop_helper(self) -> None:
        process = self._process
        self._exact_sub_mute = False
        self._last_helper_args = None
        try:
            if process is not None and getattr(process, "returncode", None) is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        except asyncio.CancelledError:
            if process is not None and getattr(process, "returncode", None) is None:
                process.kill()
                await process.wait()
            raise
        finally:
            await self._stop_helper_stderr_drain()
            if self._process is process:
                self._process = None
            self._close_exact_sub_mute_ack_socket()
            self._close_peak_control_socket()

    async def _stop_orphan_helpers(self) -> None:
        pattern = f"{self._helper_binary}.*--node-name {self._helper_node_name}"
        result = await self._command_runner(["pgrep", "-f", pattern])
        orphan_pids = result.stdout.strip()
        if not orphan_pids:
            return
        logger.info("Found orphan 2.1 helpers (pids: %s), cleaning up", orphan_pids.replace("\n", ", "))
        await self._command_runner(["pkill", "-TERM", "-f", pattern])
        await self._sleep(0.3)

    @staticmethod
    async def _run_command(args: Sequence[str]) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=RUNTIME_COMMAND_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # A wedged PipeWire registry must not hold the runtime locks
            # forever.  Terminate, allow a short grace, then kill and fully
            # reap the child so no command survives the call; report the
            # timeout as a command failure exactly like a nonzero exit.
            # If the caller is cancelled while the cleanup drains, the
            # cancellation wins over the timeout failure.
            if await _stop_command_child_cancellation_safe(
                process, grace_seconds=RUNTIME_COMMAND_TERMINATE_GRACE_SECONDS
            ):
                raise asyncio.CancelledError
            return CommandResult(
                RUNTIME_COMMAND_TIMEOUT_RETURNCODE,
                "",
                f"Command timed out after {RUNTIME_COMMAND_TIMEOUT_SECONDS}s: {' '.join(args)}",
            )
        except asyncio.CancelledError:
            # Caller cancellation must not leave the child behind; the
            # shielded cleanup terminates/kills/drains it even under further
            # cancellation, then the original cancellation is re-raised so
            # the lock holders release ownership.
            await _stop_command_child_cancellation_safe(
                process, grace_seconds=RUNTIME_COMMAND_TERMINATE_GRACE_SECONDS
            )
            raise
        return CommandResult(
            process.returncode,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )

    @staticmethod
    async def _launch_process(args: Sequence[str]) -> Any:
        return await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
