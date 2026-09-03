# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for the spotifyd remote volume watch.

spotifyd runs with ``volume_controller = "none"`` (unity contract): its
Connect volume is a reported value only, and the watch routes it through the
same pickup contract as the Qobuz bridge. The observable is polled via
playerctl because spotifyd 0.4.x emits no reliable MPRIS Volume change
signal.

Pickup contract (2026-08-28, live-verified on .104): the Spotify app pushes
the phone's media volume as mid-session absolute jumps (42% -> 100% and
53% -> 91% within one update were observed in the spotifyd journal), so the
former delta bridge jumped the master by the push delta. Now the session's
first value only anchors; a gesture writes only when it crosses the current
master level with a bounded overshoot; after the pickup the master tracks
the controller value absolutely.

The watch's poll pass is exercised directly (``poll_once`` + explicit drain),
so the tests are fully deterministic without real timers.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playback.spotifyd_volume_watch import (
    SpotifydVolumeWatch,
    SpotifydVolumeWatchDependencies,
    read_source_volume,
)


class _ScriptedWatch:
    """Deterministic driver: scripted observations, explicit drains."""

    def __init__(self, *, active=True, player="spotifyd.instance123", values, master=44):
        self.applied = []
        self.players_polled = 0

        async def apply_value(value):
            self.applied.append(value)

        async def resolve():
            self.players_polled += 1
            return player

        queue = list(values)

        async def read_source(player_name):
            if not queue:
                raise AssertionError("scripted values exhausted")
            return queue.pop(0)

        self.watch = SpotifydVolumeWatch(
            SpotifydVolumeWatchDependencies(
                is_active=lambda: active,
                apply_volume_value=apply_value,
                current_master=lambda: master,
                resolve_player=resolve,
                read_source_volume=read_source,
            ),
            poll_interval_seconds=0.0,
            debounce_seconds=0.0,
        )

    async def poll(self):
        if await self.watch.poll_once():
            await self.watch._drain_pending()


class SpotifydVolumeWatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_value_anchors_and_never_writes(self):
        # Session appears with the controller's restored/synced value: anchor
        # only — the connect push must never move the master.
        scripted = _ScriptedWatch(values=[100])
        await scripted.poll()
        self.assertEqual(scripted.applied, [])
        self.assertFalse(scripted.watch._translator.picked_up)

    async def test_aligned_gesture_picks_up_immediately_and_tracks(self):
        # Aligned scale (reported == master): every gesture crosses the
        # master level and is adopted 1:1.
        scripted = _ScriptedWatch(values=[44, 45, 46], master=44)
        for _ in range(3):
            await scripted.poll()
        self.assertEqual(scripted.applied, [45, 46])

    async def test_mid_session_app_push_is_ignored(self):
        # Live trace 2026-08-28 21:56: the app pushed 42 -> 100 (phone media
        # volume) while the master sat at 44. The push leaps far across the
        # master: it must be ignored, not adopted.
        scripted = _ScriptedWatch(values=[42, 100], master=44)
        await scripted.poll()
        await scripted.poll()
        self.assertEqual(scripted.applied, [])

    async def test_push_then_drag_down_picks_up_at_master_level(self):
        # After the ignored push (100), the user drags down; the pickup
        # happens when the gesture reaches the master level (within the
        # overshoot bound) and tracks absolutely from there.
        scripted = _ScriptedWatch(values=[42, 100, 95, 60, 47, 42], master=44)
        for _ in range(6):
            await scripted.poll()
        self.assertEqual(scripted.applied, [42])

    async def test_upward_catch_when_controller_starts_below_master(self):
        scripted = _ScriptedWatch(values=[20, 30, 43, 45], master=44)
        for _ in range(4):
            await scripted.poll()
        self.assertEqual(scripted.applied, [45])

    async def test_overshoot_bound_allows_step_and_rejects_leap(self):
        applied = []

        async def apply_value(value):
            applied.append(value)

        watch = SpotifydVolumeWatch(
            SpotifydVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: 44,
            ),
            poll_interval_seconds=0.0,
            debounce_seconds=0.0,
        )
        watch._translator.submit(42)      # anchor
        watch._translator.submit(52)      # crossing with +8 overshoot: within bound
        self.assertTrue(watch._translator.picked_up)
        await watch._translator.flush()
        self.assertEqual(applied, [52])
        watch._translator.observe_activation()
        watch._translator.submit(42)      # re-anchor after reactivation
        watch._translator.submit(56)      # crossing with +12 overshoot: rejected
        self.assertFalse(watch._translator.picked_up)
        await watch._translator.flush()
        self.assertEqual(applied, [52])

    async def test_value_seen_during_master_write_is_drained_without_next_poll(self):
        applied = []
        write_started = asyncio.Event()
        release_write = asyncio.Event()
        values = iter([42, 44, 46])

        async def apply_value(value):
            applied.append(value)
            if len(applied) == 1:
                write_started.set()
                await release_write.wait()

        async def resolve_player():
            return "spotifyd.instance123"

        async def read_source_volume(_player):
            return next(values)

        watch = SpotifydVolumeWatch(
            SpotifydVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: 44,
                resolve_player=resolve_player,
                read_source_volume=read_source_volume,
            ),
            debounce_seconds=0.0,
        )

        self.assertFalse(await watch.poll_once())  # anchor at 42
        self.assertTrue(await watch.poll_once())   # crossing: pending 44
        watch._schedule_drain()
        drain_task = watch._drain_task
        self.assertIsNotNone(drain_task)
        await write_started.wait()

        self.assertTrue(await watch.poll_once())   # 46 arrives mid-write
        watch._schedule_drain()  # the active drain must retain this pending value
        release_write.set()
        await drain_task

        self.assertEqual(applied, [44, 46])

    async def test_failed_master_write_is_retried(self):
        applied = []
        attempts = 0

        async def resolve_player():
            return "spotifyd.instance123"

        async def read_source_volume(_player):
            return 44

        async def apply_value(value):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary volume failure")
            applied.append(value)

        watch = SpotifydVolumeWatch(
            SpotifydVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: 44,
                resolve_player=resolve_player,
                read_source_volume=read_source_volume,
            ),
            debounce_seconds=0.0,
        )

        watch._translator.submit(42)   # anchor
        watch._translator.submit(44)   # crossing: pending 44
        watch._schedule_drain()
        await watch._drain_task

        self.assertEqual(applied, [44])

    async def test_owner_loss_cancels_inflight_master_write(self):
        active = True
        applied = []
        write_started = asyncio.Event()
        write_cancelled = asyncio.Event()

        async def resolve_player():
            return "spotifyd.instance123"

        async def read_source_volume(_player):
            return 44

        async def apply_value(value):
            write_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                write_cancelled.set()
                raise
            applied.append(value)

        watch = SpotifydVolumeWatch(
            SpotifydVolumeWatchDependencies(
                is_active=lambda: active,
                apply_volume_value=apply_value,
                current_master=lambda: 44,
                resolve_player=resolve_player,
                read_source_volume=read_source_volume,
            ),
            debounce_seconds=0.0,
        )
        watch._translator.submit(42)   # anchor
        watch._translator.submit(44)   # crossing: pending 44
        watch._schedule_drain()
        drain_task = watch._drain_task
        await write_started.wait()

        active = False
        await asyncio.wait_for(write_cancelled.wait(), timeout=1.0)
        await drain_task

        self.assertEqual(applied, [])
        self.assertFalse(watch._translator.picked_up)
        self.assertIsNone(watch._translator.pending)

    async def test_stop_resets_translator_after_cancelling_drain(self):
        write_started = asyncio.Event()

        async def resolve_player():
            return "spotifyd.instance123"

        async def read_source_volume(_player):
            return 44

        async def apply_value(_value):
            write_started.set()
            await asyncio.Event().wait()

        watch = SpotifydVolumeWatch(
            SpotifydVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: 44,
                resolve_player=resolve_player,
                read_source_volume=read_source_volume,
            ),
            debounce_seconds=0.0,
        )
        watch._translator.submit(42)
        watch._translator.submit(44)
        watch._schedule_drain()
        await write_started.wait()

        await watch.stop()

        self.assertFalse(watch._translator.picked_up)
        self.assertIsNone(watch._translator.pending)

    async def test_reconnect_rearms_pickup_without_touching_master(self):
        # Session ends (player vanishes) and returns with a different restored
        # value: both transitions must be invisible on the master, and the
        # fresh session needs a fresh pickup.
        scripted = _ScriptedWatch(player=None, values=[45])
        await scripted.poll()
        scripted2 = _ScriptedWatch(values=[45], master=44)
        await scripted2.poll()
        scripted2.watch._translator.submit(43)   # drag crosses the master: pickup
        await scripted2.watch._drain_pending()
        self.assertEqual(scripted.applied, [])
        self.assertEqual(scripted2.applied, [43])

    async def test_no_feedback_loop_from_repeated_reads(self):
        # The master write never reaches spotifyd's reported value; repeats of
        # an already-applied value dedup to nothing.
        scripted = _ScriptedWatch(values=[50, 45, 45], master=45)
        for _ in range(3):
            await scripted.poll()
        self.assertEqual(scripted.applied, [45])

    async def test_inactive_owner_resets_pickup(self):
        scripted = _ScriptedWatch(active=False, values=[70], master=44)
        await scripted.poll()
        self.assertEqual(scripted.applied, [])
        self.assertFalse(scripted.watch._translator.picked_up)

    async def test_unreadable_value_is_skipped(self):
        applied = []

        async def apply_value(value):
            applied.append(value)

        async def resolve():
            return "spotifyd.instance123"

        calls = []

        async def read_source(player_name):
            calls.append(player_name)
            return None

        watch = SpotifydVolumeWatch(
            SpotifydVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: 44,
                resolve_player=resolve,
                read_source_volume=read_source,
            ),
            poll_interval_seconds=0.0,
            debounce_seconds=0.0,
        )
        self.assertFalse(await watch.poll_once())
        self.assertEqual(applied, [])
        self.assertEqual(calls, ["spotifyd.instance123"])

    async def test_desktop_backend_is_not_bridged(self):
        # resolve returning None (desktop client or no session) must keep the
        # watch idle even when observations would be available.
        applied = []

        async def apply_value(value):
            applied.append(value)

        async def resolve():
            return None

        async def read_source(player_name):
            raise AssertionError("must not poll without a spotifyd instance")

        watch = SpotifydVolumeWatch(
            SpotifydVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=apply_value,
                current_master=lambda: 44,
                resolve_player=resolve,
                read_source_volume=read_source,
            ),
            poll_interval_seconds=0.0,
            debounce_seconds=0.0,
        )
        self.assertFalse(await watch.poll_once())
        self.assertEqual(applied, [])


class ReadSourceVolumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_playerctl_fraction_into_percent(self):
        async def fake_run(*args, timeout=4.0):
            assert args[0] == "--player=spotifyd.instance7"
            assert args[1] == "volume"
            return "0.370000"

        with mock.patch("streaming.spotify.mpris._run", side_effect=fake_run):
            self.assertEqual(await read_source_volume("spotifyd.instance7"), 37)

    async def test_unreadable_output_yields_none(self):
        async def fake_none(*args, timeout=4.0):
            return None

        with mock.patch("streaming.spotify.mpris._run", side_effect=fake_none):
            self.assertIsNone(await read_source_volume("spotifyd.instance7"))

    async def test_garbage_yields_none(self):
        async def fake_nan(*args, timeout=4.0):
            return "not-a-number"

        with mock.patch("streaming.spotify.mpris._run", side_effect=fake_nan):
            self.assertIsNone(await read_source_volume("spotifyd.instance7"))


if __name__ == "__main__":
    unittest.main()
