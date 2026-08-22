# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for the spotifyd remote volume watch.

spotifyd runs with ``volume_controller = "none"`` (unity contract): its
Connect volume is a reported value only, and this watch routes changes onto
master deltas through the shared translator. The observable is polled via
playerctl because spotifyd 0.4.x emits no reliable MPRIS Volume change signal.

The five contract scenarios mirror scripts/test_qbzd_volume_watch.py:
connect/reconnect, initial value 100 and status reads never move the master;
the first real gesture applies exactly its delta; there is no feedback loop;
the Loudness work point is structurally out of reach.

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

    def __init__(self, *, active=True, player="spotifyd.instance123", values):
        self.applied = []
        self.players_polled = 0

        async def apply_delta(delta):
            self.applied.append(delta)

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
                apply_volume_delta=apply_delta,
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
    async def test_connect_sync_and_initial_100_do_not_move_master(self):
        # Session appears with the controller's restored/synced value: anchor.
        scripted = _ScriptedWatch(values=[100])
        await scripted.poll()
        self.assertEqual(scripted.applied, [])

    async def test_first_real_gesture_applies_its_delta(self):
        scripted = _ScriptedWatch(values=[100, 96])
        await scripted.poll()
        await scripted.poll()
        self.assertEqual(scripted.applied, [-4])

    async def test_value_seen_during_master_write_is_drained_without_next_poll(self):
        applied = []
        write_started = asyncio.Event()
        release_write = asyncio.Event()
        values = iter([40, 70, 100])

        async def apply_delta(delta):
            applied.append(delta)
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
                apply_volume_delta=apply_delta,
                resolve_player=resolve_player,
                read_source_volume=read_source_volume,
            ),
            debounce_seconds=0.0,
        )

        self.assertFalse(await watch.poll_once())  # anchor at 40
        self.assertTrue(await watch.poll_once())   # pending 40 -> 70
        watch._schedule_drain()
        drain_task = watch._drain_task
        self.assertIsNotNone(drain_task)
        await write_started.wait()

        self.assertTrue(await watch.poll_once())  # final 70 -> 100 arrives mid-write
        watch._schedule_drain()  # the active drain must retain this pending value
        release_write.set()
        await drain_task

        self.assertEqual(applied, [30, 30])

    async def test_failed_master_write_is_retried(self):
        applied = []
        attempts = 0

        async def resolve_player():
            return "spotifyd.instance123"

        async def read_source_volume(_player):
            return 40

        async def apply_delta(delta):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary volume failure")
            applied.append(delta)

        watch = SpotifydVolumeWatch(
            SpotifydVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_delta=apply_delta,
                resolve_player=resolve_player,
                read_source_volume=read_source_volume,
            ),
            debounce_seconds=0.0,
        )

        watch._translator.submit(40)
        watch._translator.submit(70)
        watch._schedule_drain()
        await watch._drain_task

        self.assertEqual(applied, [30])

    async def test_owner_loss_cancels_inflight_master_write(self):
        active = True
        applied = []
        write_started = asyncio.Event()
        write_cancelled = asyncio.Event()

        async def resolve_player():
            return "spotifyd.instance123"

        async def read_source_volume(_player):
            return 40

        async def apply_delta(delta):
            write_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                write_cancelled.set()
                raise
            applied.append(delta)

        watch = SpotifydVolumeWatch(
            SpotifydVolumeWatchDependencies(
                is_active=lambda: active,
                apply_volume_delta=apply_delta,
                resolve_player=resolve_player,
                read_source_volume=read_source_volume,
            ),
            debounce_seconds=0.0,
        )
        watch._translator.submit(40)
        watch._translator.submit(70)
        watch._schedule_drain()
        drain_task = watch._drain_task
        await write_started.wait()

        active = False
        await asyncio.wait_for(write_cancelled.wait(), timeout=1.0)
        await drain_task

        self.assertEqual(applied, [])
        self.assertFalse(watch._translator.anchored)
        self.assertIsNone(watch._translator.pending)

    async def test_stop_resets_translator_after_cancelling_drain(self):
        write_started = asyncio.Event()

        async def resolve_player():
            return "spotifyd.instance123"

        async def read_source_volume(_player):
            return 40

        async def apply_delta(_delta):
            write_started.set()
            await asyncio.Event().wait()

        watch = SpotifydVolumeWatch(
            SpotifydVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_delta=apply_delta,
                resolve_player=resolve_player,
                read_source_volume=read_source_volume,
            ),
            debounce_seconds=0.0,
        )
        watch._translator.submit(40)
        watch._translator.submit(70)
        watch._schedule_drain()
        await write_started.wait()

        await watch.stop()

        self.assertFalse(watch._translator.anchored)
        self.assertIsNone(watch._translator.pending)

    async def test_drag_between_polls_nets_into_one_write(self):
        # A drag that completes inside one poll interval is observed only at
        # its end state: one observation, one canonical write.
        scripted = _ScriptedWatch(values=[100, 98])
        await scripted.poll()
        await scripted.poll()
        self.assertEqual(scripted.applied, [-2])

    async def test_stepwise_observations_apply_per_observed_step(self):
        # Values observed by separate polls are separate observed states; each
        # contributes its own relative step.
        scripted = _ScriptedWatch(values=[100, 99, 98])
        for _ in range(3):
            await scripted.poll()
        self.assertEqual(scripted.applied, [-1, -1])

    async def test_reconnect_reanchors_without_touching_master(self):
        # Session ends (player vanishes) and returns with a different restored
        # value: both transitions must be invisible on the master.
        scripted = _ScriptedWatch(player=None, values=[45])
        await scripted.poll()
        scripted2 = _ScriptedWatch(values=[45])
        await scripted2.poll()
        self.assertEqual(scripted.applied, [])
        self.assertEqual(scripted2.applied, [])

    async def test_no_feedback_loop_from_repeated_reads(self):
        # The master write never reaches spotifyd's reported value; repeats of
        # an already-applied value dedup to nothing.
        scripted = _ScriptedWatch(values=[50, 45, 45])
        for _ in range(3):
            await scripted.poll()
        self.assertEqual(scripted.applied, [-5])

    async def test_inactive_owner_resets_anchor(self):
        scripted = _ScriptedWatch(active=False, values=[70])
        await scripted.poll()
        self.assertEqual(scripted.applied, [])
        self.assertFalse(scripted.watch._translator.anchored)

    async def test_unreadable_value_is_skipped(self):
        applied = []

        async def apply_delta(delta):
            applied.append(delta)

        async def resolve():
            return "spotifyd.instance123"

        calls = []

        async def read_source(player_name):
            calls.append(player_name)
            return None

        watch = SpotifydVolumeWatch(
            SpotifydVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_delta=apply_delta,
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

        async def apply_delta(delta):
            applied.append(delta)

        async def resolve():
            return None

        async def read_source(player_name):
            raise AssertionError("must not poll without a spotifyd instance")

        watch = SpotifydVolumeWatch(
            SpotifydVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_delta=apply_delta,
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

    async def test_garbage_yields_none(self):
        async def fake_none(*args, timeout=4.0):
            return None

        with mock.patch("streaming.spotify.mpris._run", side_effect=fake_none):
            self.assertIsNone(await read_source_volume("spotifyd.instance7"))

        async def fake_nan(*args, timeout=4.0):
            return "not-a-number"

        with mock.patch("streaming.spotify.mpris._run", side_effect=fake_nan):
            self.assertIsNone(await read_source_volume("spotifyd.instance7"))


if __name__ == "__main__":
    unittest.main()
