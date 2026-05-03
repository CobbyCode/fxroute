"""Conservative DAC keep-awake helper using a silent PipeWire playback stream."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

logger = logging.getLogger(__name__)


class DACKeepAlive:
    """Keep the downstream audio path awake for a short post-playback cooldown."""

    def __init__(
        self,
        *,
        target: str = "easyeffects_sink",
        cooldown_seconds: float = 900.0,
        sample_rate: int = 48000,
        channels: int = 2,
        chunk_frames: int = 4800,
    ):
        self.target = target
        self.cooldown_seconds = max(float(cooldown_seconds), 0.0)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.chunk_frames = max(int(chunk_frames), 256)
        self._bytes_per_frame = self.channels * 2  # s16le stereo by default
        self._cooldown_task: asyncio.Task | None = None
        self._writer_task: asyncio.Task | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._armed_after_activity = False
        self._last_state: tuple[bool, bool] | None = None
        self._cooldown_until_monotonic: float | None = None

    async def update(self, *, playback_active: bool, enabled: bool) -> None:
        async with self._lock:
            next_state = (bool(playback_active), bool(enabled))
            if next_state == self._last_state and (playback_active or enabled):
                if playback_active and (self._cooldown_task or self._process):
                    await self._cancel_cooldown_locked()
                    await self._stop_stream_locked()
                return
            self._last_state = next_state

            if playback_active:
                self._armed_after_activity = True
                await self._cancel_cooldown_locked()
                await self._stop_stream_locked()
                return

            if not enabled:
                self._armed_after_activity = False
                await self._cancel_cooldown_locked()
                await self._stop_stream_locked()
                return

            if self._armed_after_activity and self._cooldown_task is None and not self._stream_running_locked():
                self._armed_after_activity = False
                self._cooldown_until_monotonic = time.monotonic() + self.cooldown_seconds
                self._cooldown_task = asyncio.create_task(self._cooldown_then_start())
                logger.info("Scheduled DAC keep-awake cooldown for %.0fs", self.cooldown_seconds)

    async def stop(self) -> None:
        async with self._lock:
            self._armed_after_activity = False
            self._last_state = None
            await self._cancel_cooldown_locked()
            await self._stop_stream_locked()

    async def status(self) -> dict:
        async with self._lock:
            cooldown_remaining = None
            if self._cooldown_until_monotonic is not None:
                cooldown_remaining = max(0.0, self._cooldown_until_monotonic - time.monotonic())
            enabled = bool(self._last_state[1]) if self._last_state else False
            playback_active = bool(self._last_state[0]) if self._last_state else False
            state = "idle"
            if not enabled:
                state = "off"
            elif playback_active:
                state = "playing"
            elif self._stream_running_locked():
                state = "active"
            elif self._cooldown_task is not None:
                state = "cooldown"
            return {
                "state": state,
                "enabled": enabled,
                "playback_active": playback_active,
                "cooldown_seconds": self.cooldown_seconds,
                "cooldown_remaining_seconds": cooldown_remaining,
                "target": self.target,
            }

    def _stream_running_locked(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def _cooldown_then_start(self) -> None:
        try:
            await asyncio.sleep(self.cooldown_seconds)
            async with self._lock:
                self._cooldown_task = None
                self._cooldown_until_monotonic = None
                if self._stream_running_locked():
                    return
                await self._start_stream_locked()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self._lock:
                self._cooldown_task = None
                self._cooldown_until_monotonic = None
            logger.warning("Failed to start DAC keep-awake stream: %s", exc)

    async def _start_stream_locked(self) -> None:
        if self._stream_running_locked():
            return

        process = await asyncio.create_subprocess_exec(
            "pw-play",
            "--target",
            self.target,
            "--rate",
            str(self.sample_rate),
            "--channels",
            str(self.channels),
            "--format",
            "s16",
            "--raw",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._process = process
        self._writer_task = asyncio.create_task(self._writer_loop(process))
        logger.info("Started DAC keep-awake stream on %s", self.target)

    async def _stop_stream_locked(self) -> None:
        writer_task = self._writer_task
        process = self._process
        self._writer_task = None
        self._process = None

        if writer_task is not None:
            writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer_task

        if process is None:
            return

        if process.stdin:
            with contextlib.suppress(Exception):
                process.stdin.close()

        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        logger.info("Stopped DAC keep-awake stream")

    async def _cancel_cooldown_locked(self) -> None:
        cooldown_task = self._cooldown_task
        self._cooldown_task = None
        self._cooldown_until_monotonic = None
        if cooldown_task is None:
            return
        cooldown_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cooldown_task

    async def _writer_loop(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdin is not None
        chunk = b"\x00" * (self.chunk_frames * self._bytes_per_frame)
        try:
            while True:
                process.stdin.write(chunk)
                await process.stdin.drain()
                if process.returncode is not None:
                    break
        except asyncio.CancelledError:
            raise
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            logger.warning("DAC keep-awake stream writer stopped unexpectedly: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                if process.stdin and not process.stdin.is_closing():
                    process.stdin.close()
            with contextlib.suppress(Exception):
                if process.returncode is None:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=1.0)
