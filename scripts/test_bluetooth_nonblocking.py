#!/usr/bin/env python3
"""Bluetooth checks never block the event loop or the source-transition lock.

- The BlueZ presence probe asks the bus daemon (NameHasOwner), never
  org.bluez itself: a message to org.bluez D-Bus-activates a stopped
  bluetoothd and may hang while bluetoothd shuts down.
- bluetoothctl work (receiver toggle, device disconnect, overviews) runs in
  worker threads, not on the event loop.
- The monitor tick probes outside the source-transition lock, so a hanging
  probe never delays a source switch.
- bluetoothctl actions (receiver toggle, device disconnect) are only
  recorded under the source-transition lock and run off it, so a command
  hanging on a vanished BlueZ never delays a source switch either.
- A recorded bluetoothctl action is never lost before it ran: a tick that
  failed before finishing it, or an interrupted finish, leaves it pending
  and the monitor's next tick runs it.
"""

import asyncio
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.bluetooth as bluetooth_module
import audio.samplerate.bluetooth as bluez_mod
from audio.bluetooth import BluetoothInputDependencies, BluetoothInputMonitor
from audio.source_feed import SourceOverviewFeed, SourceTransitionLock
from audio.source_transitions import SourceTransitionDependencies, SourceTransitions
import main

MAIN_THREAD = threading.main_thread()


class PresenceProbeTests(unittest.TestCase):
    def _probe(self, output=None, error=None, tools=("busctl", "dbus-send")):
        calls = []

        def run(args, timeout=None):
            calls.append((list(args), timeout))
            if error:
                raise error
            return output

        with patch.object(bluez_mod, "_run_command", side_effect=run), patch.object(
            bluez_mod, "_command_available", side_effect=lambda name: name in tools
        ):
            return bluez_mod._bluetooth_daemon_reachable(), calls

    def test_asks_the_bus_daemon_never_org_bluez(self):
        for tools in (("busctl", "dbus-send"), ("dbus-send",)):
            _result, calls = self._probe(output="b true", tools=tools)
            (args, timeout), = calls
            self.assertIn("NameHasOwner", " ".join(args))
            self.assertTrue(any("org.freedesktop.DBus" in arg for arg in args[:5]))
            self.assertFalse(any(arg in ("--dest=org.bluez", "status") for arg in args),
                             "a message addressed to org.bluez would D-Bus-activate bluetoothd")
            self.assertLessEqual(timeout, 1.0)

    def test_reads_the_owner_answer(self):
        self.assertTrue(self._probe(output="b true")[0])
        self.assertFalse(self._probe(output="b false")[0])
        self.assertTrue(self._probe(output="   boolean true", tools=("dbus-send",))[0])
        self.assertFalse(self._probe(output="   boolean false", tools=("dbus-send",))[0])
        self.assertFalse(self._probe(error=RuntimeError("Command timed out"))[0])

    def test_a_failing_probe_tool_falls_through_to_the_next(self):
        calls = []

        def run(args, timeout=None):
            calls.append(args[0])
            if args[0] == "busctl":
                raise RuntimeError("Command timed out")
            return "   boolean true"

        with patch.object(bluez_mod, "_run_command", side_effect=run), patch.object(
            bluez_mod, "_command_available", return_value=True
        ):
            self.assertTrue(bluez_mod._bluetooth_daemon_reachable())
        self.assertEqual(calls, ["busctl", "dbus-send"])


class OffLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.threads = []

    def _record(self, value=None):
        def call(*_args, **_kwargs):
            self.threads.append(threading.current_thread() is MAIN_THREAD)
            return value
        return call

    def _monitor(self, persisted="bluetooth-input"):
        return BluetoothInputMonitor(BluetoothInputDependencies(
            sync_peak_monitor_for_source_mode_state=AsyncMock(),
            get_persisted_source_mode=lambda: persisted,
        ))

    async def test_receiver_toggle_and_device_disconnect_run_off_the_loop(self):
        monitor = self._monitor()
        with patch.object(bluetooth_module, "set_bluetooth_receiver_enabled", self._record()), patch.object(
            bluetooth_module, "disconnect_connected_bluetooth_audio_sources", self._record([])
        ), patch.object(monitor, "_ensure_agent", AsyncMock()):
            await monitor.sync({"mode": "bluetooth-input", "bluetooth": {"selectable": True, "discoverable": False}})
            await monitor.sync({"mode": "app-playback"})
            # sync() only records the bluetoothctl actions; they run off the
            # source-transition lock when the caller finishes them.
            await monitor.finish_bluetoothctl_actions()
        self.assertTrue(self.threads)
        self.assertNotIn(True, self.threads, "bluetoothctl work ran on the event loop")

    async def test_bluetooth_overview_route_runs_off_the_loop(self):
        with patch.object(main, "get_bluetooth_audio_overview", self._record({"available": True})):
            self.assertEqual(await main.audio_bluetooth_overview(), {"available": True})
        self.assertEqual(self.threads, [False])

    async def test_source_switch_and_startup_build_off_the_loop(self):
        lock = main.runtime.source_transition_lock
        main.runtime.source_transition_lock = None
        self.addCleanup(setattr, main.runtime, "source_transition_lock", lock)
        overview = {"mode": "app-playback", "selected_input": None}
        with patch.object(main.samplerate, "_load_audio_source_selection",
                          return_value={"mode": "app-playback", "selected_input_key": None}), patch.object(
            main, "set_audio_source_selection", self._record(overview)
        ), patch.object(main, "get_audio_source_overview", self._record(overview)), patch.object(
            main.external_input, "sync", AsyncMock(side_effect=lambda result: result)
        ), patch.object(main.bluetooth_input, "sync", AsyncMock(side_effect=lambda result: result)), patch.object(
            main.peak_monitor_coordinator, "sync_source_mode_state", AsyncMock()
        ), patch.object(main.manager, "broadcast", AsyncMock()), patch.object(main.runtime, "stdin_input", None):
            await main.source_transitions.save_selection("app-playback", None)
            await main.source_transitions.reapply_persisted()
        self.assertEqual(self.threads, [False, False])


class LockHoldTests(unittest.IsolatedAsyncioTestCase):
    async def test_bluetooth_decision_reads_cannot_block_or_overtake_a_source_switch(self):
        """A blocked probe or redundant sync read cannot hold up another commit."""
        for blocked_read in ("initial-overview", "receiver-session", "refresh-overview"):
            with self.subTest(blocked_read=blocked_read):
                lock = SourceTransitionLock()
                release = threading.Event()
                reached_read_or_applied = asyncio.Event()
                tick_finished = asyncio.Event()
                loop = asyncio.get_running_loop()
                persisted = {"mode": "bluetooth-input", "selected_input_key": None}
                published, receiver_modes, links = [], [], []
                builds = []

                def overview():
                    return {"mode": persisted["mode"], "selected_input": None,
                            "bluetooth": {"selectable": True, "discoverable": False,
                                          "pairable": False, "source_name": "bluez_input.phone"}}

                def hang():
                    loop.call_soon_threadsafe(reached_read_or_applied.set)
                    if not release.wait(5):
                        raise RuntimeError("Bluetooth test read was not released")

                def build():
                    snapshot = overview()
                    builds.append(snapshot)
                    if (blocked_read == "initial-overview" and len(builds) == 1
                            or blocked_read == "refresh-overview" and len(builds) == 2):
                        hang()
                    return snapshot

                def bluetooth_presence():
                    if blocked_read == "receiver-session":
                        hang()
                    return False

                def select(mode, input_key):
                    persisted.update(mode=mode, selected_input_key=input_key)
                    return overview()

                async def broadcast(message):
                    published.append(message["data"]["mode"])

                async def publish_tick(snapshot):
                    await transitions.sync_source_mode_state(snapshot)
                    reached_read_or_applied.set()

                monitor = BluetoothInputMonitor(BluetoothInputDependencies(
                    sync_peak_monitor_for_source_mode_state=publish_tick,
                    get_persisted_source_mode=lambda: persisted["mode"],
                    get_source_transition_lock=lambda: lock,
                ))
                monitor.input_source_name = "bluez_input.previous"
                finish_actions = monitor.finish_bluetoothctl_actions

                async def finish_tick():
                    await finish_actions()
                    tick_finished.set()

                transitions = SourceTransitions(SourceTransitionDependencies(
                    get_lock=lambda: lock,
                    feed=SourceOverviewFeed(broadcast),
                    build_overview=overview,
                    save_selection=select,
                    load_selection=lambda: dict(persisted),
                    with_current_stdin_status=lambda snapshot: snapshot,
                    sync_external_input=AsyncMock(side_effect=lambda snapshot: snapshot),
                    sync_bluetooth_input=monitor.sync,
                    get_stdin_input=lambda: None,
                    note_source_selection=lambda mode: None,
                    pause_app_playback=AsyncMock(),
                    sync_peak_monitor=AsyncMock(),
                    sync_peak_monitor_for_stdin=AsyncMock(),
                    finish_bluetoothctl_actions=finish_actions,
                ))
                with patch.object(bluetooth_module, "get_audio_source_overview", build), patch.object(
                    bluez_mod, "_bluetooth_daemon_reachable", bluetooth_presence
                ), patch.object(bluez_mod, "_command_available", return_value=False), patch.object(
                    monitor, "_ensure_agent", AsyncMock()
                ), patch.object(
                    bluetooth_module.pw_link, "connect_ports", AsyncMock(side_effect=lambda *args: links.append(args))
                ), patch.object(bluetooth_module.pw_link, "disconnect_ports", AsyncMock()), patch.object(
                    bluetooth_module, "input_links_present", AsyncMock(return_value=True)
                ), patch.object(bluetooth_module, "set_bluetooth_receiver_enabled", receiver_modes.append), patch.object(
                    bluetooth_module, "disconnect_connected_bluetooth_audio_sources", return_value=[]
                ), patch.object(monitor, "finish_bluetoothctl_actions", finish_tick):
                    task = asyncio.create_task(monitor.run_monitor_loop())
                    try:
                        # A removed redundant read reaches publication instead.
                        await asyncio.wait_for(reached_read_or_applied.wait(), 2)
                        try:
                            result = await asyncio.wait_for(transitions.save_selection("app-playback", None), 0.5)
                        except asyncio.TimeoutError:
                            self.fail("a Bluetooth decision read held the source-transition lock")
                        self.assertEqual(result["mode"], "app-playback")
                        self.assertFalse(release.is_set(), "the switch must finish before the read returns")
                        links_after_switch = list(links)
                        release.set()
                        await asyncio.wait_for(tick_finished.wait(), 2)
                        self.assertEqual(links, links_after_switch, "a stale read must not reconnect Bluetooth")
                        self.assertIsNone(monitor.input_source_name)
                        self.assertEqual(published[-1], "app-playback")
                        self.assertEqual(transitions.feed.latest["mode"], "app-playback")
                        self.assertEqual(receiver_modes[-1], False)
                        if blocked_read == "initial-overview":
                            self.assertGreaterEqual(len(builds), 2, "an overlapped snapshot must be rebuilt")
                    finally:
                        release.set()
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def test_a_hanging_probe_does_not_hold_the_source_transition_lock(self):
        """While the Bluetooth tick's probe hangs, a source switch gets the lock at once."""
        lock = SourceTransitionLock()
        started, release = threading.Event(), threading.Event()

        def hanging_probe():
            started.set()
            release.wait(5)
            return {"mode": "bluetooth-input", "bluetooth": {"selectable": False}}

        monitor = BluetoothInputMonitor(BluetoothInputDependencies(
            sync_peak_monitor_for_source_mode_state=AsyncMock(),
            get_persisted_source_mode=lambda: "bluetooth-input",
            get_source_transition_lock=lambda: lock,
        ))
        with patch.object(bluetooth_module, "get_audio_source_overview", hanging_probe):
            loop_task = asyncio.create_task(monitor.run_monitor_loop())
            try:
                await asyncio.to_thread(started.wait, 5)
                await asyncio.wait_for(lock.acquire(), 0.5)
                lock.release()
            finally:
                release.set()
                loop_task.cancel()
                await asyncio.gather(loop_task, return_exceptions=True)

    async def test_hanging_receiver_toggle_does_not_hold_the_source_transition_lock(self):
        """sync() records the bluetoothctl toggle; it runs after the lock is free.

        BlueZ can disappear after the daemon-reachable check inside the
        receiver toggle; the command then hangs until its ~5 s command
        timeout. It runs off the source-transition lock, so it never delays
        a source switch.
        """
        lock = SourceTransitionLock()
        started, release = threading.Event(), threading.Event()
        calls = []

        def hanging_toggle(enabled):
            calls.append(enabled)
            started.set()
            release.wait(5)

        overview = {"mode": "bluetooth-input",
                    "bluetooth": {"selectable": True, "discoverable": False, "pairable": False}}
        monitor = BluetoothInputMonitor(BluetoothInputDependencies(
            sync_peak_monitor_for_source_mode_state=AsyncMock(),
            get_persisted_source_mode=lambda: "bluetooth-input",
            get_source_transition_lock=lambda: lock,
        ))
        with patch.object(bluetooth_module, "get_audio_source_overview", return_value=overview), patch.object(
            monitor, "_ensure_agent", AsyncMock()
        ), patch.object(
            bluetooth_module, "set_bluetooth_receiver_enabled", side_effect=hanging_toggle
        ):
            loop_task = asyncio.create_task(monitor.run_monitor_loop())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5),
                                "the recorded receiver toggle never ran")
                await asyncio.wait_for(lock.acquire(), 0.5)
                lock.release()
                self.assertEqual(calls, [True], "the toggle runs once, off the lock")
            finally:
                release.set()
                loop_task.cancel()
                await asyncio.gather(loop_task, return_exceptions=True)

    async def test_hanging_device_disconnect_does_not_hold_the_source_transition_lock(self):
        """Same for the device disconnect recorded while leaving Bluetooth input."""
        lock = SourceTransitionLock()
        started, release = threading.Event(), threading.Event()

        def hanging_disconnect():
            started.set()
            release.wait(5)
            return []

        overview = {"mode": "app-playback", "bluetooth": {"selectable": False}}
        monitor = BluetoothInputMonitor(BluetoothInputDependencies(
            sync_peak_monitor_for_source_mode_state=AsyncMock(),
            get_persisted_source_mode=lambda: "app-playback",
            get_source_transition_lock=lambda: lock,
        ))
        monitor.input_source_name = "bluez_input.phone"
        with patch.object(bluetooth_module, "get_audio_source_overview", return_value=overview), patch.object(
            bluetooth_module, "disconnect_connected_bluetooth_audio_sources", side_effect=hanging_disconnect
        ), patch.object(bluetooth_module.pw_link, "disconnect_ports", AsyncMock()):
            loop_task = asyncio.create_task(monitor.run_monitor_loop())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5),
                                "the recorded device disconnect never ran")
                await asyncio.wait_for(lock.acquire(), 0.5)
                lock.release()
            finally:
                release.set()
                loop_task.cancel()
                await asyncio.gather(loop_task, return_exceptions=True)


class DeferredActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_actions_skipped_by_a_failed_tick_run_on_the_next_tick(self):
        """A tick failing after disable() skips its finish; the next tick runs it.

        Without the pending actions the monitor is idle once Bluetooth input
        is left, so nothing else would ever run the device disconnect.
        """
        lock = SourceTransitionLock()
        loop = asyncio.get_running_loop()
        kicked = asyncio.Event()
        publish = AsyncMock(side_effect=RuntimeError("publish failed"))

        def disconnect():
            loop.call_soon_threadsafe(kicked.set)
            return []

        monitor = BluetoothInputMonitor(BluetoothInputDependencies(
            sync_peak_monitor_for_source_mode_state=publish,
            get_persisted_source_mode=lambda: "app-playback",
            get_source_transition_lock=lambda: lock,
        ))
        monitor.input_source_name = "bluez_input.phone"
        overview = {"mode": "app-playback", "bluetooth": {"selectable": False}}
        with patch.object(bluetooth_module, "BLUETOOTH_INPUT_MONITOR_INTERVAL_SECONDS", 0.01), patch.object(
            bluetooth_module, "get_audio_source_overview", return_value=overview
        ), patch.object(
            bluetooth_module, "disconnect_connected_bluetooth_audio_sources", side_effect=disconnect
        ), patch.object(bluetooth_module.pw_link, "disconnect_ports", AsyncMock()):
            task = asyncio.create_task(monitor.run_monitor_loop())
            try:
                try:
                    await asyncio.wait_for(kicked.wait(), 2)
                except asyncio.TimeoutError:
                    self.fail("the device disconnect recorded by the failed tick never ran")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        publish.assert_awaited_once()
        self.assertIsNone(monitor.input_source_name)

    async def test_an_interrupted_finish_puts_back_what_it_had_not_run(self):
        """A cancel during the device disconnect keeps receiver-off pending.

        A newer decision recorded meanwhile (entering Bluetooth input again)
        wins over the interrupted leftovers.
        """
        for newer_decision in (False, True):
            with self.subTest(newer_decision=newer_decision):
                started, release = threading.Event(), threading.Event()
                kicks, receiver_modes = [], []

                def hanging_disconnect():
                    kicks.append(True)
                    started.set()
                    release.wait(5)
                    return []

                monitor = BluetoothInputMonitor(BluetoothInputDependencies(
                    sync_peak_monitor_for_source_mode_state=AsyncMock(),
                    get_persisted_source_mode=lambda: "app-playback",
                ))
                with patch.object(
                    bluetooth_module, "disconnect_connected_bluetooth_audio_sources", side_effect=hanging_disconnect
                ), patch.object(bluetooth_module, "set_bluetooth_receiver_enabled", receiver_modes.append):
                    # Leaving Bluetooth input records the device disconnect and receiver off.
                    await monitor.sync({"mode": "app-playback"})
                    finish = asyncio.create_task(monitor.finish_bluetoothctl_actions())
                    try:
                        self.assertTrue(await asyncio.to_thread(started.wait, 5))
                        if newer_decision:
                            monitor._record_bluetoothctl_action(receiver=True)
                        finish.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await finish
                    finally:
                        release.set()
                    self.assertEqual(receiver_modes, [], "the cancel landed before receiver off ran")
                    self.assertFalse(monitor._idle(), "pending actions must keep the monitor ticking")
                    await monitor.finish_bluetoothctl_actions()
                if newer_decision:
                    self.assertEqual((len(kicks), receiver_modes), (1, [True]))
                else:
                    self.assertEqual((len(kicks), receiver_modes), (2, [False]))
                self.assertTrue(monitor._idle())


if __name__ == "__main__":
    unittest.main()
