# SPDX-License-Identifier: AGPL-3.0-only
"""Bluetooth input monitoring, agent lifecycle and DSP link routing.

Owns the linked Bluetooth source name, the BlueZ agent subprocess and the
3-second monitor loop, moved out of ``main.py``.  No imports from ``main``:
the peak-monitor sync callback is injected by the composition root.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from audio import pw_link
from audio.input_links import input_links_present
from audio.samplerate import (
    SOURCE_MODE_BLUETOOTH_INPUT,
    _extract_bluetooth_address,
    disconnect_connected_bluetooth_audio_sources,
    get_audio_source_overview,
    reconnect_bluetooth_audio_devices,
    set_bluetooth_receiver_enabled,
)
from audio.source_monitor import UnavailabilityConfirmation, run_source_monitor_loop

logger = logging.getLogger(__name__)
BLUETOOTH_INPUT_MONITOR_INTERVAL_SECONDS = 3
# Leaving Bluetooth input mode kicks the connected A2DP source (the peer keeps
# playing to its own speakers), so selecting the mode again must bring that
# device back by itself. The tries are bounded and spaced: a peer that is
# switched off must not be probed by bluetoothctl on every 3 s monitor tick.
BLUETOOTH_RECONNECT_ATTEMPTS = 6
BLUETOOTH_RECONNECT_INTERVAL_SECONDS = 10.0
# Devices remembered for a reconnect (kicked or streamed from); a small bound
# keeps a long-running session from growing the list without limit.
BLUETOOTH_RECONNECT_MAX_TARGETS = 4

SourceModePeakSync = Callable[[dict[str, Any] | None], Awaitable[None]]


@dataclass
class BluetoothInputDependencies:
    """Live services the Bluetooth input monitor needs."""

    sync_peak_monitor_for_source_mode_state: SourceModePeakSync
    get_persisted_source_mode: Callable[[], str] | None = None
    get_source_transition_lock: Callable[[], asyncio.Lock] | None = None


class BluetoothInputMonitor:
    """Single owner of the Bluetooth input link, agent process and monitor loop."""

    def __init__(self, deps: BluetoothInputDependencies) -> None:
        self._deps = deps
        self.input_source_name: str | None = None
        self.agent_process: asyncio.subprocess.Process | None = None
        self.monitor_task: asyncio.Task | None = None
        self._sync_lock = asyncio.Lock()
        # bluetoothctl actions recorded under the caller's source-transition
        # lock and executed off it by finish_bluetoothctl_actions().
        self._pending_device_kick = False
        self._pending_device_reconnect = False
        self._pending_receiver_mode: bool | None = None
        self._bluetoothctl_actions_lock = asyncio.Lock()
        # Paired audio sources this session kicked or streamed from; they are
        # reconnected when Bluetooth input is selected again.
        self._reconnect_addresses: list[str] = []
        self._reconnect_attempts = 0
        self._last_reconnect_at: float | None = None
        # Observed once per monitor tick; the source overview reads confirmed().
        self.availability = UnavailabilityConfirmation()

    async def _disconnect_source(self, source_name: str | None) -> None:
        normalized = (source_name or "").strip()
        if not normalized:
            return
        try:
            await self._link_source_to_dsp(normalized, disconnect=True)
        except (RuntimeError, OSError) as exc:
            logger.debug("Bluetooth source disconnect failed for %s: %s", normalized, exc)

    async def stop_agent(self) -> None:
        proc = self.agent_process
        if not proc:
            return
        try:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        finally:
            if self.agent_process is proc:
                self.agent_process = None

    async def _ensure_agent(self) -> None:
        proc = self.agent_process
        if proc and proc.returncode is None:
            return
        agent_script = Path(__file__).resolve().parent / "bluez_agent.py"
        self.agent_process = await asyncio.create_subprocess_exec(
            str(agent_script),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.sleep(0.4)
        except BaseException:
            await self.stop_agent()
            raise
        if self.agent_process.returncode is not None:
            stderr = await self.agent_process.stderr.read()
            self.agent_process = None
            message = (stderr or b"BlueZ audio agent exited immediately").decode(errors="ignore").strip()
            # A dead agent leaves receiver mode visibly ready while every
            # incoming A2DP/HFP connection is rejected ("Authentication
            # attempt without agent"), so never swallow this silently.
            logger.warning("BlueZ audio agent exited immediately; Bluetooth pairing/authorization will fail: %s", message)
            raise RuntimeError(message)

    async def clear_links(self) -> None:
        previous_source = self.input_source_name
        self.input_source_name = None
        await self._disconnect_source(previous_source)

    async def _link_source_to_dsp(self, source_name: str, disconnect: bool = False) -> None:
        normalized = (source_name or "").strip()
        if not normalized:
            return
        failures: list[str] = []
        for channel in ("FL", "FR"):
            sink_port = f"fxroute_dsp_sink:playback_{channel}"
            source_ports = (f"{normalized}:capture_{channel}", f"{normalized}:output_{channel}")
            try:
                if disconnect:
                    await pw_link.disconnect_ports(source_ports, sink_port)
                else:
                    await pw_link.connect_ports(source_ports, sink_port)
            except Exception as exc:
                failures.append(f"{channel}: {exc}")

        if failures:
            raise RuntimeError("failed to link ports: " + "; ".join(failures))

    async def disable(self) -> None:
        await self.clear_links()
        await self.stop_agent()
        # Entering the mode again starts a fresh reconnect budget.
        self._reset_reconnect_progress()
        self._record_bluetoothctl_action(kick_devices=True)

    def _record_bluetoothctl_action(self, *, receiver: bool | None = None,
                                    kick_devices: bool = False,
                                    reconnect_devices: bool = False) -> None:
        """Record a bluetoothctl action for execution off the caller's lock.

        Callers hold the source-transition lock; a bluetoothctl command can
        block until its command timeout when BlueZ disappears after the
        daemon-reachable check, so only the decision is made here and
        finish_bluetoothctl_actions() runs the commands once the lock has
        been released. Newer decisions win: entering Bluetooth input drops a
        device disconnect recorded while leaving it, and leaving it drops a
        device reconnect recorded while entering.
        """
        if receiver is not None:
            self._pending_receiver_mode = receiver
            if receiver:
                self._pending_device_kick = False
        if kick_devices:
            self._pending_device_reconnect = False
        if reconnect_devices and self._reconnect_addresses:
            self._pending_device_reconnect = True
        self._pending_device_kick = self._pending_device_kick or kick_devices

    async def finish_bluetoothctl_actions(self) -> None:
        """Run the recorded bluetoothctl actions off the source-transition lock.

        Idempotent and safe to call concurrently: each recorded action runs
        once under a private lock, so a command hanging on a vanished BlueZ
        only delays this caller, never a source transition. The recorded
        state is re-read after every command, so a decision made while an
        earlier command hung still wins. An interrupted run (cancelled
        caller or monitor task) puts back what it had not finished unless a
        newer decision was recorded meanwhile; the monitor tick then runs it.
        A failed command is logged and dropped, never retried.
        """
        async with self._bluetoothctl_actions_lock:
            while True:
                kick_devices = self._pending_device_kick
                reconnect_devices = self._pending_device_reconnect
                receiver = self._pending_receiver_mode
                self._pending_device_kick = False
                self._pending_device_reconnect = False
                self._pending_receiver_mode = None
                if not kick_devices and not reconnect_devices and receiver is None:
                    return
                try:
                    if kick_devices:
                        try:
                            disconnected = await asyncio.to_thread(disconnect_connected_bluetooth_audio_sources)
                            if disconnected:
                                logger.info(
                                    "Disconnected Bluetooth audio source devices while leaving bluetooth-input mode: %s",
                                    ", ".join(disconnected),
                                )
                                self._remember_reconnect_targets(disconnected)
                        except Exception as exc:
                            logger.warning("Failed to disconnect Bluetooth audio source devices: %s", exc)
                        kick_devices = False
                    if receiver is not None:
                        try:
                            await asyncio.to_thread(set_bluetooth_receiver_enabled, receiver)
                        except Exception as exc:
                            logger.warning("Failed to %s Bluetooth receiver mode: %s",
                                           "enable" if receiver else "disable", exc)
                    if reconnect_devices:
                        await self._reconnect_devices()
                        reconnect_devices = False
                except BaseException:
                    if not self._bluetoothctl_actions_pending():
                        self._pending_device_kick = kick_devices
                        self._pending_device_reconnect = reconnect_devices
                        self._pending_receiver_mode = receiver
                    raise

    def _bluetoothctl_actions_pending(self) -> bool:
        return (self._pending_device_kick or self._pending_device_reconnect
                or self._pending_receiver_mode is not None)

    def _remember_reconnect_targets(self, addresses: Any) -> None:
        """Keep paired audio sources that should come back on the next entry."""
        for value in list(addresses or []):
            address = _extract_bluetooth_address(str(value or ""))
            if not address or address in self._reconnect_addresses:
                continue
            if len(self._reconnect_addresses) >= BLUETOOTH_RECONNECT_MAX_TARGETS:
                self._reconnect_addresses.pop(0)
            self._reconnect_addresses.append(address)

    def _forget_reconnect_targets(self, addresses: Any) -> None:
        forgotten = {_extract_bluetooth_address(str(value or "")) for value in list(addresses or [])}
        self._reconnect_addresses = [address for address in self._reconnect_addresses
                                     if address not in forgotten]

    def _reset_reconnect_progress(self) -> None:
        """Drop the retry budget and a recorded reconnect for the next entry."""
        self._reconnect_attempts = 0
        self._last_reconnect_at = None
        self._pending_device_reconnect = False

    def _record_reconnect_attempt(self) -> None:
        """Record one bounded, spaced reconnect try for the remembered devices.

        Called on every Bluetooth monitor tick while the mode is selected and
        no peer streams: the first try of a mode entry runs right away, later
        ones are spaced out and capped, and a peer that streams again retires
        the budget (``_reset_reconnect_progress``).
        """
        if not self._reconnect_addresses:
            return
        if self._reconnect_attempts >= BLUETOOTH_RECONNECT_ATTEMPTS:
            return
        now = time.monotonic()
        if self._last_reconnect_at is not None and \
                now - self._last_reconnect_at < BLUETOOTH_RECONNECT_INTERVAL_SECONDS:
            return
        self._reconnect_attempts += 1
        self._last_reconnect_at = now
        self._record_bluetoothctl_action(reconnect_devices=True)

    async def _reconnect_devices(self) -> None:
        """Connect the remembered audio sources off the source-transition lock."""
        targets = list(self._reconnect_addresses)
        if not targets:
            return
        try:
            connected = await asyncio.to_thread(reconnect_bluetooth_audio_devices, targets)
        except Exception as exc:
            logger.warning("Failed to reconnect Bluetooth audio source devices: %s", exc)
            return
        if connected:
            logger.info("Reconnected Bluetooth audio source devices: %s", ", ".join(connected))
            self._forget_reconnect_targets(connected)

    async def _ensure_loopback(self, source_name: str) -> None:
        normalized = (source_name or "").strip()
        if not normalized:
            raise RuntimeError("Missing Bluetooth source name for monitoring")
        if self.input_source_name == normalized:
            if await input_links_present(normalized, (("FL", "FL"), ("FR", "FR"))):
                return
        await self.clear_links()
        try:
            await self._link_source_to_dsp(normalized)
            if not await input_links_present(normalized, (("FL", "FL"), ("FR", "FR"))):
                raise RuntimeError("Bluetooth input links missing after reconnect")
        except BaseException:
            await self._disconnect_source(normalized)
            raise
        self.input_source_name = normalized
        logger.info("Enabled Bluetooth input monitoring from %s to fxroute_dsp_sink", normalized)

    async def sync(self, source_overview: dict[str, Any]) -> dict[str, Any]:
        """Apply a complete routing snapshot under the caller's transition lock.

        Monitor snapshots, including the receiver source, are built outside
        the lock and generation-validated before reaching here. Switch and
        rollback callers supply their transaction's overview. Never re-probe
        Bluetooth here: a slow read would hold up unrelated source switches.
        """
        async with self._sync_lock:
            return await self._sync_unlocked(source_overview)

    async def _sync_unlocked(self, overview: dict[str, Any]) -> dict[str, Any]:
        if not self._bluetooth_persisted():
            # Left Bluetooth input: an old outage must not count later.
            self.availability.reset()
        if overview.get("mode") != SOURCE_MODE_BLUETOOTH_INPUT:
            await self.disable()
            self._record_bluetoothctl_action(receiver=False)
            return overview

        bt_state = overview.get("bluetooth") or {}
        if not bt_state.get("selectable"):
            # Unconfirmed loss: get_audio_source_overview keeps Bluetooth
            # input selected until the loss is confirmed. Keep agent and
            # links; an explicit switch is rejected earlier by
            # set_audio_source_selection.
            return overview

        await self._ensure_agent()
        if not bt_state.get("discoverable") or not bt_state.get("pairable"):
            self._record_bluetoothctl_action(receiver=True)
        source_name = bt_state.get("source_name")
        if not source_name:
            await self.clear_links()
            # The peer is gone: kicked when we left the mode, or dropped on
            # its own. Bring the remembered device back by itself, bounded.
            self._record_reconnect_attempt()
        else:
            self._remember_reconnect_targets([_extract_bluetooth_address(str(source_name))])
            # Streaming again: the retry budget is spent and a recorded
            # reconnect for a device that is already back must not run.
            self._reset_reconnect_progress()
            await self._ensure_loopback(str(source_name))
        return overview

    async def run_monitor_loop(self) -> None:
        await run_source_monitor_loop(
            name="Bluetooth input",
            interval=BLUETOOTH_INPUT_MONITOR_INTERVAL_SECONDS,
            idle=self._idle,
            build=lambda: get_audio_source_overview(),
            act=self._act,
            after=self.finish_bluetoothctl_actions,
            lock_provider=self._deps.get_source_transition_lock,
        )

    def _idle(self) -> bool:
        # Not selected, nothing active and no bluetoothctl action pending:
        # the tick has no cleanup, sync or deferred duty. Pending actions
        # keep it running, so one whose finish was skipped (failed tick) or
        # interrupted (cancelled caller) still runs on the next tick.
        mode_provider = self._deps.get_persisted_source_mode
        return (
            mode_provider is not None
            and mode_provider() != SOURCE_MODE_BLUETOOTH_INPUT
            and self.input_source_name is None
            and self.agent_process is None
            and not self._bluetoothctl_actions_pending()
        )

    async def _monitor_once(self) -> None:
        """One tick outside the loop: build, act, then run deferred actions."""
        await self._act(await asyncio.to_thread(get_audio_source_overview))
        await self.finish_bluetoothctl_actions()

    async def _act(self, overview: dict[str, Any]) -> None:
        # The overview only falls back from Bluetooth input once this
        # monitor's observations confirm the loss (self.availability), so
        # every teardown below acts on a confirmed loss only.
        self._observe_availability(overview)
        if overview.get("mode") == SOURCE_MODE_BLUETOOTH_INPUT:
            if not (overview.get("bluetooth") or {}).get("selectable"):
                # Unconfirmed: one failed probe. Keep agent, links and the
                # published state until a later probe confirms or clears it.
                return
            overview = await self.sync(overview)
            await self._deps.sync_peak_monitor_for_source_mode_state(overview)
        elif self.input_source_name:
            await self.disable()
            await self._deps.sync_peak_monitor_for_source_mode_state(overview)
        elif self._bluetooth_persisted():
            # Bluetooth stays selected but its loss is confirmed before any
            # stream linked a source. Stop only the leftover agent: nothing
            # is linked, and disable() would also disconnect paired devices.
            # The persisted selection is untouched; the first branch restarts
            # the agent once the adapter returns. Unchanged ticks are
            # deduplicated by the shared publish path.
            if self.agent_process is not None:
                await self.stop_agent()
            await self._deps.sync_peak_monitor_for_source_mode_state(overview)

    def _observe_availability(self, overview: dict[str, Any]) -> None:
        """Record this tick's raw probe (bluetooth.selectable)."""
        if self._bluetooth_persisted():
            self.availability.observe(bool((overview.get("bluetooth") or {}).get("selectable")))
        else:
            self.availability.reset()

    def _bluetooth_persisted(self) -> bool:
        mode_provider = self._deps.get_persisted_source_mode
        return mode_provider is not None and mode_provider() == SOURCE_MODE_BLUETOOTH_INPUT

    async def stop(self) -> None:
        task = self.monitor_task
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self.monitor_task is task:
                self.monitor_task = None
        # The input may never have been active; the disable side effects
        # (link disconnect, agent stop, BlueZ source disconnect) only apply
        # then.  The monitor task above is always stopped.
        if self.agent_process is not None or self.input_source_name is not None:
            await self.disable()
        # Shutdown is the one caller that needs the deferred bluetoothctl
        # actions to land before the process exits.
        await self.finish_bluetoothctl_actions()
