#!/usr/bin/env python3
"""Audio source overview pushes: revision order, lock scope, all sources."""

import asyncio
import inspect
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.bluetooth as bluetooth_module
import audio.samplerate.overview as overview_mod
import audio.source_monitor as source_monitor
import audio.source_transitions as source_transitions_mod
from audio.source_feed import SourceOverviewFeed
import main
from scripts.test_peak_monitor_line_source_arming import make_coordinator


def _stdin(**overrides):
    status = {
        "available": True, "selectable": True, "state": "waiting",
        "selected": False, "session_id": None, "format": None,
        "rate": None, "channels": None, "left": None, "right": None,
        "routed": False, "frames_received": 0, "error": None,
        "measurement_active": False,
    }
    status.update(overrides)
    return status


def _overview(mode="app-playback", **overrides):
    overview = {
        "mode": mode,
        "modes": [
            {"key": "app-playback", "label": "App playback", "selectable": True},
            {"key": "stdin-input", "label": "STDIN", "selectable": False},
        ],
        "default_input": None, "selected_input": None, "current_input": None,
        "inputs": [{"key": "scarlett::pair:1-2"}], "bluetooth": {},
        "stdin": _stdin(selectable=False), "notes": [],
    }
    overview.update(overrides)
    return overview


class SourceOverviewFeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_process_stamps_its_own_epoch(self):
        """A restart restarts the counter under a new epoch, never a clock seed."""
        feed = SourceOverviewFeed(AsyncMock())
        first = feed.record(_overview())
        second = feed.record({**first, "mode": "stdin-input"})
        self.assertEqual((first["revision"], second["revision"]), (1, 2))
        self.assertEqual(first["epoch"], second["epoch"])
        self.assertEqual(feed.latest, second)
        restarted = SourceOverviewFeed(AsyncMock()).record(_overview())
        self.assertEqual(restarted["revision"], 1)
        self.assertNotEqual(restarted["epoch"], first["epoch"])

    async def test_partial_refreshes_keep_the_age_of_their_full_build(self):
        now = [100.0]
        feed = SourceOverviewFeed(AsyncMock(), monotonic=lambda: now[0])
        self.assertIsNone(feed.fresh_latest(5.0))
        feed.record(_overview())
        now[0] = 104.0
        feed.record(_overview("stdin-input"), full_build=False)
        self.assertIsNotNone(feed.fresh_latest(5.0))
        now[0] = 105.5
        self.assertIsNone(feed.fresh_latest(5.0), "a partial refresh must not renew the base age")
        feed.record(_overview())
        self.assertEqual(feed.fresh_latest(5.0)["mode"], "app-playback")

    async def test_publish_skips_an_unchanged_overview(self):
        broadcast = AsyncMock()
        feed = SourceOverviewFeed(broadcast)
        first = await feed.publish(_overview())
        again = await feed.publish(_overview())
        changed = await feed.publish(_overview("stdin-input"))
        self.assertEqual([call.args[0]["data"]["revision"] for call in broadcast.await_args_list],
                         [first["revision"], changed["revision"]])
        self.assertGreater(again["revision"], first["revision"])
        self.assertEqual(broadcast.await_args_list[0].args[0]["type"], "source")

    async def test_failed_push_is_retried_by_the_next_identical_publish(self):
        broadcast = AsyncMock(side_effect=[RuntimeError("socket"), None])
        feed = SourceOverviewFeed(broadcast)
        with self.assertLogs("audio.source_feed", level="ERROR"):
            await feed.publish(_overview())
        await feed.publish(_overview())
        self.assertEqual(broadcast.await_count, 2)


class StdinStatusMergeTests(unittest.TestCase):
    def test_merge_refreshes_only_stdin_parts(self):
        overview_mod.configure_stdin_input_status(lambda: _stdin(selectable=True, state="streaming"))
        self.addCleanup(lambda: overview_mod.configure_stdin_input_status(None))
        base = _overview("stdin-input")
        merged = overview_mod.with_current_stdin_status(base)
        self.assertEqual(merged["stdin"]["state"], "streaming")
        self.assertTrue(next(m for m in merged["modes"] if m["key"] == "stdin-input")["selectable"])
        self.assertTrue(next(m for m in merged["modes"] if m["key"] == "app-playback")["selectable"])
        self.assertEqual(merged["inputs"], base["inputs"])
        self.assertFalse(base["stdin"]["selectable"], "the recorded overview is not mutated")


class SourcePushTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._playback_snapshot = dict(main.playback_state.__dict__)
        self._lock = main.runtime.source_transition_lock
        main.runtime.source_transition_lock = None
        main.source_overview_feed.reset()
        self.broadcast = AsyncMock()
        self.coordinator, self.monitor = make_coordinator()
        for patcher in (
            patch.object(main.manager, "broadcast", self.broadcast),
            patch.object(main, "peak_monitor_coordinator", self.coordinator),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        overview_mod.configure_stdin_input_status(
            lambda: _stdin(session_id="abc", state="streaming", routed=True, selected=True))
        self.addCleanup(lambda: overview_mod.configure_stdin_input_status(None))

    async def asyncTearDown(self):
        main.playback_state.__dict__.clear()
        main.playback_state.__dict__.update(self._playback_snapshot)
        main.runtime.source_transition_lock = self._lock
        main.source_overview_feed.reset()

    def _pushed(self):
        return [call.args[0]["data"] for call in self.broadcast.await_args_list
                if call.args[0]["type"] == "source"]

    async def test_stdin_hook_merges_into_the_newest_overview_without_a_rebuild(self):
        main.source_overview_feed.record(_overview("stdin-input"))
        build = Mock(side_effect=AssertionError("full rebuild"))
        with patch.object(main, "get_audio_source_overview", build):
            await main.source_transitions.publish_stdin_state()
        pushed = self._pushed()
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0]["stdin"]["state"], "streaming")
        self.assertEqual(self.coordinator.signature, "stdin:abc")

    async def test_stdin_hook_rebuilds_a_stale_base_once(self):
        """An old base is rebuilt, so a STDIN push also refreshes the inputs."""
        main.source_overview_feed.record(_overview("stdin-input", inputs=[]))
        fresh = _overview("stdin-input", inputs=[{"key": "usb::pair:1-2"}])
        lock_held = []

        def build():
            lock_held.append(main._source_transition_lock().locked())
            return fresh

        with patch.object(source_transitions_mod, "STDIN_OVERVIEW_BASE_MAX_AGE_SECONDS", -1.0), patch.object(
            main, "get_audio_source_overview", Mock(side_effect=build)
        ):
            await main.source_transitions.publish_stdin_state()
        self.assertEqual(lock_held, [False], "one rebuild, outside the source-transition lock")
        self.assertEqual(self._pushed()[0]["inputs"], [{"key": "usb::pair:1-2"}])

    async def test_stdin_hook_merges_into_the_newest_overview_when_every_build_is_disturbed(self):
        main.source_overview_feed.record(_overview("stdin-input"))
        lock = main._source_transition_lock()

        def disturbed_build():
            lock.generation += 1  # another holder ran during this build
            return _overview("app-playback")

        with patch.object(source_transitions_mod, "STDIN_OVERVIEW_BASE_MAX_AGE_SECONDS", -1.0), patch.object(
            main, "get_audio_source_overview", Mock(side_effect=disturbed_build)
        ):
            await main.source_transitions.publish_stdin_state()
        pushed = self._pushed()
        self.assertEqual([(item["mode"], item["stdin"]["state"]) for item in pushed],
                         [("stdin-input", "streaming")])

    async def test_stdin_hook_builds_once_before_any_overview_exists(self):
        with patch.object(main, "get_audio_source_overview",
                          Mock(return_value=_overview("stdin-input"))) as build:
            await main.source_transitions.publish_stdin_state()
        build.assert_called_once()
        self.assertEqual(len(self._pushed()), 1)

    async def test_stdin_hook_cannot_overtake_an_in_flight_switch(self):
        """A notify during a switch pushes the committed state, after it."""
        main.source_overview_feed.record(_overview("app-playback"))
        async with main._source_transition_lock():
            hook = asyncio.create_task(main.source_transitions.publish_stdin_state())
            await asyncio.sleep(0)
            self.assertFalse(hook.done(), "the hook waits for the source-transition lock")
            committed = await main.source_overview_feed.publish(_overview("external-input"))
        await hook
        pushed = self._pushed()
        self.assertEqual([item["mode"] for item in pushed], ["external-input", "external-input"])
        self.assertGreater(pushed[-1]["revision"], committed["revision"])

    async def test_failure_inside_the_fresh_overview_block_releases_the_lock(self):
        """A caller raising while it holds the fresh overview must not leak the lock."""
        self.monitor.restart = AsyncMock(side_effect=RuntimeError("capture target missing"))
        with patch.object(source_transitions_mod, "STDIN_OVERVIEW_BASE_MAX_AGE_SECONDS", -1.0), patch.object(
            main, "get_audio_source_overview", Mock(return_value=_overview(
                "stdin-input", stdin=_stdin(session_id="abc", state="streaming", routed=True)))
        ):
            with self.assertRaises(RuntimeError):
                await main.source_transitions.publish_stdin_state()
        self.assertFalse(main._source_transition_lock().locked())
        self.assertEqual(len(self._pushed()), 1, "the push still ran before the failure")

    async def test_stdin_event_leaves_a_bluetooth_armed_monitor_alone(self):
        """A STDIN event carrying an unconfirmed Bluetooth blip must not stop/restart its monitor."""
        self.coordinator.armed = True
        self.coordinator.signature = "bluetooth:Phone:aac"
        main.source_overview_feed.record(_overview(
            "bluetooth-input", bluetooth={"selectable": False, "state": "unavailable"}))
        await main.source_transitions.publish_stdin_state()
        self.assertEqual((self.monitor.restarts, self.monitor.stops), (0, 0))
        self.assertTrue(self.coordinator.armed)
        self.assertEqual(self.coordinator.signature, "bluetooth:Phone:aac")
        self.assertEqual(len(self._pushed()), 1, "the STDIN state itself is still pushed")

    async def test_stdin_event_still_releases_a_stdin_armed_monitor(self):
        """Leaving STDIN: its own events keep releasing the STDIN-armed monitor."""
        self.coordinator.armed = True
        self.coordinator.signature = "stdin:abc"
        overview_mod.configure_stdin_input_status(lambda: _stdin(state="waiting"))
        main.source_overview_feed.record(_overview("app-playback"))
        await main.source_transitions.publish_stdin_state()
        self.assertEqual(self.monitor.stops, 1)
        self.assertFalse(self.coordinator.armed)

    async def test_push_survives_a_failed_peak_monitor_restart(self):
        main.source_overview_feed.record(_overview("stdin-input"))
        self.monitor.restart = AsyncMock(side_effect=RuntimeError("capture target missing"))
        with self.assertRaises(RuntimeError):
            await main.source_transitions.publish_stdin_state()
        self.assertEqual(self._pushed()[0]["stdin"]["state"], "streaming")

    async def test_monitor_loops_push_what_they_arm(self):
        for monitor in (main.external_input, main.bluetooth_input):
            main.source_overview_feed.reset()
            self.broadcast.reset_mock()
            await monitor._deps.sync_peak_monitor_for_source_mode_state(_overview("external-input"))
            self.assertEqual([item["mode"] for item in self._pushed()], ["external-input"])

    async def test_bluetooth_fallback_before_any_stream_is_pushed(self):
        """Bluetooth selected, loss confirmed before a stream: push the fallback once.

        Without a running agent there is nothing to clean up. The
        confirmation itself is covered by test_bluetooth_unavailability.py.
        """
        await main.source_overview_feed.publish(
            _overview("bluetooth-input", bluetooth={"selectable": True, "state": "idle"}))
        self.broadcast.reset_mock()
        fallback = _overview(
            bluetooth={"selectable": False, "state": "unavailable"},
            notes=["Bluetooth input is not currently available on this host; staying on App playback."],
        )
        disable = AsyncMock()
        with patch.object(main.samplerate, "_load_audio_source_selection",
                          return_value={"mode": "bluetooth-input", "selected_input_key": None}), patch.object(
            bluetooth_module, "get_audio_source_overview", Mock(return_value=fallback)
        ), patch.object(main.bluetooth_input, "input_source_name", None), patch.object(
            main.bluetooth_input, "agent_process", None
        ), patch.object(main.bluetooth_input, "disable", disable):
            await main.bluetooth_input._monitor_once()
            await main.bluetooth_input._monitor_once()
        pushed = self._pushed()
        self.assertEqual([(item["mode"], item["bluetooth"]["state"]) for item in pushed],
                         [("app-playback", "unavailable")],
                         "the first tick pushes the fallback, the unchanged second tick does not")
        disable.assert_not_awaited()

    async def test_external_fallback_without_a_loopback_is_pushed(self):
        """External selected but never linked, inputs gone: push the fallback once, then recover."""
        selected = {"key": "scarlett::pair:1-2", "name": "scarlett", "source_key": "scarlett"}
        await main.source_overview_feed.publish(_overview("external-input", selected_input=selected))
        self.broadcast.reset_mock()
        fallback = _overview(inputs=[], notes=["No real external inputs detected; staying on App playback."])
        builds = [fallback, fallback, _overview("external-input", selected_input=selected)]
        link = AsyncMock()
        with patch.object(main.samplerate, "_load_audio_source_selection",
                          return_value={"mode": "external-input", "selected_input_key": selected["key"]}), patch.object(
            main, "get_audio_source_overview", Mock(side_effect=builds)
        ), patch.object(main.external_input, "loopback_source_name", None), patch.object(
            main.external_input, "_ensure_loopback", link
        ):
            for _tick in range(3):
                await main.external_input._monitor_once()
        self.assertEqual([item["mode"] for item in self._pushed()], ["app-playback", "external-input"],
                         "the fallback is pushed once, the returning input once")
        link.assert_awaited_once()

    async def test_committed_switch_is_pushed_to_every_client(self):
        overview = _overview("external-input")
        with patch.object(main.samplerate, "_load_audio_source_selection",
                          return_value={"mode": "app-playback", "selected_input_key": None}), patch.object(
            main, "set_audio_source_selection", return_value=dict(overview)
        ), patch.object(main.external_input, "sync", AsyncMock(side_effect=lambda result: result)), patch.object(
            main.bluetooth_input, "sync", AsyncMock(side_effect=lambda result: result)
        ), patch.object(main, "_pause_all_app_playback_for_external_input", AsyncMock()), patch.object(
            main.runtime, "stdin_input", None
        ):
            result = await main.source_transitions.apply_selection("external-input", None)
        self.assertEqual(self._pushed(), [result])
        self.assertIn("revision", result)

    async def test_failed_switch_pushes_nothing(self):
        with patch.object(main.samplerate, "_load_audio_source_selection",
                          return_value={"mode": "app-playback", "selected_input_key": None}), patch.object(
            main, "set_audio_source_selection", side_effect=[ValueError("Unknown source mode"), _overview()]
        ), patch.object(main.external_input, "sync", AsyncMock(side_effect=lambda result: result)), patch.object(
            main.bluetooth_input, "sync", AsyncMock(side_effect=lambda result: result)
        ), patch.object(main.runtime, "stdin_input", None):
            with self.assertRaises(ValueError):
                await main.source_transitions.apply_selection("bogus", None)
        self.assertEqual(self._pushed(), [])

    async def test_undisturbed_poll_builds_once_without_the_lock(self):
        started, release = threading.Event(), threading.Event()

        def slow_build():
            started.set()
            release.wait(5)
            return _overview()

        with patch.object(main, "get_audio_source_overview", Mock(side_effect=slow_build)) as build:
            poll = asyncio.create_task(main.audio_source_overview())
            await asyncio.to_thread(started.wait, 5)
            self.assertFalse(main._source_transition_lock().locked(),
                             "a switch never waits for a poll's overview build")
            release.set()
            polled = await poll
        build.assert_called_once()
        self.assertEqual(main.source_overview_feed.latest, polled)

    async def test_poll_overlapping_a_switch_is_rebuilt_after_it(self):
        """A pre-switch build must not outrank the switch's push."""
        started, release = threading.Event(), threading.Event()
        builds = [_overview("app-playback"), _overview("external-input")]
        lock_held = []

        def build():
            overview = builds.pop(0)
            if overview["mode"] == "app-playback":
                started.set()
                release.wait(5)
            else:
                lock_held.append(main._source_transition_lock().locked())
            return overview

        with patch.object(main, "get_audio_source_overview", Mock(side_effect=build)) as builder:
            poll = asyncio.create_task(main.audio_source_overview())
            await asyncio.to_thread(started.wait, 5)
            async with main._source_transition_lock():
                pushed = await main.source_overview_feed.publish(_overview("external-input"))
            release.set()
            polled = await poll
        self.assertEqual(builder.call_count, 2)
        self.assertEqual(lock_held, [False], "the retry also builds outside the lock")
        self.assertEqual(polled["mode"], "external-input")
        self.assertGreater(polled["revision"], pushed["revision"])

    async def test_poll_started_during_a_switch_builds_after_it(self):
        with patch.object(main, "get_audio_source_overview",
                          Mock(return_value=_overview("external-input"))) as builder:
            async with main._source_transition_lock():
                poll = asyncio.create_task(main.audio_source_overview())
                await asyncio.sleep(0.05)
                self.assertEqual(builder.call_count, 0, "no build while the switch holds the lock")
                pushed = await main.source_overview_feed.publish(_overview("external-input"))
            polled = await poll
        builder.assert_called_once()
        self.assertGreater(polled["revision"], pushed["revision"])

    async def test_poll_never_builds_under_the_lock_and_falls_back_to_the_newest(self):
        """Holders overlapping every attempt (player callbacks) cost retries, never a locked build."""
        latest = main.source_overview_feed.record(_overview("external-input"))
        lock = main._source_transition_lock()
        lock_held = []

        def disturbed_build():
            lock_held.append(lock.locked())
            lock.generation += 1  # another holder ran during this build
            return _overview("app-playback")

        with patch.object(main, "get_audio_source_overview", Mock(side_effect=disturbed_build)):
            polled = await main.audio_source_overview()
        self.assertEqual(lock_held, [False] * source_monitor.SOURCE_OVERVIEW_BUILD_ATTEMPTS)
        self.assertEqual(polled, latest, "the newest recorded overview, not a stale build")
        self.assertEqual(main.source_overview_feed.latest, latest)

    def test_stdin_service_is_wired_to_the_publish_hook(self):
        source = inspect.getsource(main.lifespan)
        self.assertIn("on_state_changed=lambda: source_transitions.publish_stdin_state()", source)
        self.assertIn("await source_transitions.reapply_persisted()", source)


class SourcePushCoalescingTests(unittest.TestCase):
    def test_source_snapshots_share_one_pending_slot(self):
        self.assertIn("source", main.ConnectionManager()._coalesced_message_types)


if __name__ == "__main__":
    unittest.main()
