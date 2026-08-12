#!/usr/bin/env python3
"""Stale end-file Ownership-Guard Regressionen (P2).

Ein verspätetes MPV-Ended-/end-file-Callback darf den inzwischen committed
Queue-/Source-Kontext nicht mehr mutieren.  Der Guard bindet das Event an den
publizierten ``playback_context_commit_id``-Token (App-Commit-Boundary, nicht
die Coordinator-interne ``last_successful_commit_id``), der synchron beim
Event-Dispatch erfasst wird; bei laufender Transition wartet der Ended-
Callback auf den terminalen Zustand und vergleicht den Token erneut.

Diese Suite überführt die Diagnose-Szenarien aus
``scripts/diag_stale_endfile_autoadvance.py`` in positive Tests.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import playback_queue
import main
import player
from playback_queue_test_support import queue_state, restore_queue_state


TARGET_RATE = 48000
COMMIT_A = "commit-a"
COMMIT_B = "commit-b"


class _FakeManager:
    def __init__(self):
        self.broadcasts = []

    async def broadcast(self, message):
        self.broadcasts.append(message)


def _local(track_id: str, rate: int = 44100) -> dict:
    return {
        "id": track_id,
        "source": "local",
        "url": f"/music/{track_id}.flac",
        "title": track_id,
        "artist": "Test",
        "sample_rate_hz": rate,
    }


def _radio(track_id: str) -> dict:
    return {
        "id": f"radio_{track_id}",
        "source": "radio",
        "url": f"http://stream.example/{track_id}",
        "title": track_id,
        "artist": "Radio",
        "sample_rate_hz": TARGET_RATE,
    }


def _ended_snapshot(seq: int = 11, entry_id: int | None = 7) -> dict:
    """Eingefrorener Player-State eines natuerlichen EOF (end-file reason=eof)."""
    return {
        "_seq": seq,
        "playing": False,
        "paused": False,
        "position": 0.0,
        "duration": 0.0,
        "volume": 100,
        "current_file": None,
        "playlist_pos": None,
        "ended": True,
        "error": None,
        "end_reason": "eof",
        "end_entry_id": entry_id,
    }


class StaleEndfileOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """Gemeinsames Setup: committed Queue A=[a1,a2] unter Token COMMIT_A."""

    def _install(self, coordinator_token: str | None = COMMIT_A) -> None:
        self.transition_requests = []
        self.manager = _FakeManager()
        self.coordinator = SimpleNamespace(
            last_successful_commit_id=coordinator_token,
            transition_active=False,
        )

        async def no_peak_sync(*_args, **_kwargs):
            return None

        async def recording_transition(request):
            self.transition_requests.append(request)
            return SimpleNamespace(
                committed=True, target_rate=TARGET_RATE, transition_id="tr-test"
            )

        self._patchers = [
            patch.object(main, "playback_transition_coordinator", self.coordinator),
            patch.object(main, "_run_coordinated_transition", recording_transition),
            patch.object(main, "_coordinator_target_rate", lambda _s, _t=None: TARGET_RATE),
            patch.object(main, "_coordinator_rate_change", lambda _r: False),
            patch.object(main, "_sample_rate_policy_is_auto", lambda: False),
            patch.object(main, "_schedule_radio_reconnect_if_needed", lambda _s: None),
            patch.object(main, "sync_peak_monitor_for_playback_state", no_peak_sync),
            patch.object(main, "build_playback_payload", lambda state: dict(state)),
            patch.object(main, "manager", self.manager),
            patch.object(main, "player_instance", SimpleNamespace(state={})),
        ]
        for patcher in self._patchers:
            patcher.start()
        self.addCleanup(self._restore)

        self._saved_queue = queue_state()
        playback_queue.queue.tracks = [_local("a1", 44100), _local("a2", 96000)]
        playback_queue.queue.original = [dict(t) for t in playback_queue.queue.tracks]
        playback_queue.queue.index = 0
        playback_queue.queue.mode = "app_replace"
        playback_queue.queue.loop = False
        playback_queue.queue.shuffle = False
        playback_queue.queue.single_track_loop = False
        main.queue_advancing = False
        main.current_track_info = dict(playback_queue.queue.tracks[0])
        main.last_track_info = dict(playback_queue.queue.tracks[0])
        main.current_footer_owner = "local"
        main.latest_player_state_seq_seen = 0
        main.playback_transition_epoch = 0
        main.playback_transition_pending_attempts = 0
        main.playback_context_commit_id = COMMIT_A
        main._playback_settled_event().set()

    def _restore(self) -> None:
        restore_queue_state(self._saved_queue)
        for patcher in self._patchers:
            patcher.stop()

    def _commit_queue_b(self) -> None:
        """Simuliert den erfolgreichen User-Play-Commit von Queue B=[b1,b2]
        inkl. neuem Coordinator-Token (wie execute() -> _record_result)."""
        candidate = playback_queue.cleared_queue_candidate(_local("b1", 96000))
        candidate.queue = [_local("b1", 96000), _local("b2", 96000)]
        candidate.original = [dict(t) for t in candidate.queue]
        candidate.index = 0
        candidate.mode = "app_replace"
        playback_queue.queue.commit(candidate)
        main._commit_coordinated_track(
            candidate.queue[0], source="local", commit_token=COMMIT_B
        )

    def _request_targets(self) -> list[str]:
        return [request.target_track.get("id") for request in self.transition_requests]

    # -- A. Race 1 / Local->Local -----------------------------------------

    async def test_stale_eof_after_new_local_queue_commit_is_noop(self):
        self._install()
        self._commit_queue_b()
        before = (list(playback_queue.queue.tracks), playback_queue.queue.index,
                  main.current_track_info and main.current_track_info.get("id"))

        await main.on_player_state_change(
            _ended_snapshot(), event_commit_id=COMMIT_A
        )

        after = (list(playback_queue.queue.tracks), playback_queue.queue.index,
                 main.current_track_info and main.current_track_info.get("id"))
        self.assertEqual(before, after, "stale EOF must not mutate queue B")
        self.assertEqual(self._request_targets(), [],
                         "stale EOF must not start b2")
        self.assertEqual(self.manager.broadcasts, [],
                         "stale ended state must not be broadcast")

    # -- B. Queue-Ende ------------------------------------------------------

    async def test_stale_eof_at_queue_end_does_not_clear_queue(self):
        self._install()
        candidate = playback_queue.cleared_queue_candidate(_local("b1", 96000))
        candidate.queue = [_local("b1", 96000), _local("b2", 96000)]
        candidate.original = [dict(t) for t in candidate.queue]
        candidate.index = 1
        candidate.mode = "app_replace"
        playback_queue.queue.commit(candidate)
        main._commit_coordinated_track(
            candidate.queue[1], source="local", commit_token=COMMIT_B
        )

        await main.on_player_state_change(
            _ended_snapshot(), event_commit_id=COMMIT_A
        )

        self.assertEqual(
            [t.get("id") for t in playback_queue.queue.tracks], ["b1", "b2"],
            "stale EOF must not clear the committed queue",
        )
        self.assertEqual(playback_queue.queue.index, 1)
        self.assertEqual(self._request_targets(), [])

    async def test_stale_eof_with_loop_does_not_wrap(self):
        self._install()
        candidate = playback_queue.cleared_queue_candidate(_local("b1", 96000))
        candidate.queue = [_local("b1", 96000), _local("b2", 96000)]
        candidate.original = [dict(t) for t in candidate.queue]
        candidate.index = 1
        candidate.mode = "app_replace"
        candidate.loop = True
        playback_queue.queue.commit(candidate)
        main._commit_coordinated_track(
            candidate.queue[1], source="local", commit_token=COMMIT_B
        )

        await main.on_player_state_change(
            _ended_snapshot(), event_commit_id=COMMIT_A
        )

        self.assertEqual(playback_queue.queue.index, 1,
                         "stale EOF must not wrap the looped queue")
        self.assertEqual(self._request_targets(), [])

    # -- C. Local->Spotify ---------------------------------------------------

    async def test_stale_local_eof_cannot_displace_spotify(self):
        self._install()
        # Spotify-Handoff committet keine Track-Info: current_track_info
        # bleibt der alte Local-Track, die Queue bleibt committed.  Nur der
        # Commit-Token wechselt.
        main.playback_context_commit_id = "commit-spotify"
        main.current_footer_owner = "spotify"

        await main.on_player_state_change(
            _ended_snapshot(), event_commit_id=COMMIT_A
        )

        self.assertEqual(self._request_targets(), [],
                         "stale local EOF must not start local auto-advance")
        self.assertEqual(
            main.current_track_info and main.current_track_info.get("id"), "a1",
            "spotify context must stay untouched",
        )
        self.assertEqual(playback_queue.queue.index, 0)

    # -- D. Event waehrend erfolgreichem Attempt ------------------------------

    async def test_eof_waits_for_successful_attempt_then_noop(self):
        self._install()
        main._begin_playback_transition_attempt()  # B in flight, Token noch A
        task = asyncio.create_task(
            main.on_player_state_change(_ended_snapshot(), event_commit_id=COMMIT_A)
        )
        await asyncio.sleep(0)  # Task erreicht den Settle-Wait
        self.assertFalse(task.done(), "ended callback must wait for the attempt")

        main.playback_context_commit_id = COMMIT_B  # B boundary published
        main._end_playback_transition_attempt()
        await asyncio.wait_for(task, timeout=5)

        self.assertEqual(self._request_targets(), [],
                         "EOF(A) must be stale after B committed")
        self.assertEqual(playback_queue.queue.index, 0)
        self.assertEqual(self.manager.broadcasts, [])

    # -- E. Event waehrend fehlgeschlagenem Attempt ----------------------------

    async def test_eof_survives_failed_attempt_and_advances_once(self):
        self._install()
        main._begin_playback_transition_attempt()  # B attempt start
        task = asyncio.create_task(
            main.on_player_state_change(_ended_snapshot(), event_commit_id=COMMIT_A)
        )
        await asyncio.sleep(0)
        self.assertFalse(task.done())

        # B scheitert: Token bleibt COMMIT_A.
        main._end_playback_transition_attempt()
        await asyncio.wait_for(task, timeout=5)

        self.assertEqual(self._request_targets(), ["a2"],
                         "EOF(A) stays legit after a failed attempt")
        self.assertEqual(playback_queue.queue.index, 1)

    async def test_queued_attempt_after_failed_one_still_guards(self):
        self._install()
        main._begin_playback_transition_attempt()
        main._end_playback_transition_attempt()  # erster Versuch gescheitert
        main._begin_playback_transition_attempt()  # zweiter Versuch queued
        task = asyncio.create_task(
            main.on_player_state_change(_ended_snapshot(), event_commit_id=COMMIT_A)
        )
        await asyncio.sleep(0)
        self.assertFalse(task.done(), "must wait for the queued successor attempt")

        main.playback_context_commit_id = COMMIT_B
        main._end_playback_transition_attempt()
        await asyncio.wait_for(task, timeout=5)

        self.assertEqual(self._request_targets(), [],
                         "queued successor attempt committed -> EOF stale")

    async def test_non_source_changing_commit_does_not_invalidate_eof(self):
        self._install()
        # Nicht-source-changing Coordinator-Transition (z. B. output-mode-
        # switch, measurement-entry oder sample-rate-policy) laeuft, waehrend
        # ein EOF(A) captured ist.
        main._begin_playback_transition_attempt()
        task = asyncio.create_task(
            main.on_player_state_change(_ended_snapshot(), event_commit_id=COMMIT_A)
        )
        await asyncio.sleep(0)
        self.assertFalse(task.done(), "ended callback must wait for the attempt")

        # Der Coordinator-commit des output-mode-switch setzt nur seine
        # interne last_successful_commit_id; der publizierte Playback-Kontext
        # (COMMIT_A) bleibt unveraendert, weil keine App-Playback-Globals
        # publiziert wurden.
        self.coordinator.last_successful_commit_id = COMMIT_B
        main._end_playback_transition_attempt()
        await asyncio.wait_for(task, timeout=5)

        self.assertEqual(self._request_targets(), ["a2"],
                         "non-source-changing commit must not invalidate EOF(A)")
        self.assertEqual(playback_queue.queue.index, 1)

    async def test_eof_waiter_cannot_enter_between_coordinator_commit_and_app_publish(self):
        self._install()
        # Transition B: Coordinator-commit erfolgt, aber die App-Playback-
        # Globals (Queue B + Track B + Token B) sind noch nicht publiziert.
        main._begin_playback_transition_attempt()
        task = asyncio.create_task(
            main.on_player_state_change(_ended_snapshot(), event_commit_id=COMMIT_A)
        )
        await asyncio.sleep(0)
        self.assertFalse(task.done(), "ended callback must wait for the attempt")

        # Boundary-Fenster: der publizierte Token ist noch COMMIT_A, aber die
        # Transition ist noch in flight - der Waiter darf nicht mutieren.
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        self.assertEqual(self._request_targets(), [],
                         "waiter must not mutate between coordinator commit and app publish")

        # Vollstaendiger App-State-Commit (Queue B, Track B, Token B) und
        # erst danach endet die Attempt-Phase.
        candidate = playback_queue.cleared_queue_candidate(_local("b1", 96000))
        candidate.queue = [_local("b1", 96000), _local("b2", 96000)]
        candidate.original = [dict(t) for t in candidate.queue]
        candidate.index = 0
        candidate.mode = "app_replace"
        playback_queue.queue.commit(candidate)
        main._commit_coordinated_track(
            candidate.queue[0], source="local", commit_token=COMMIT_B
        )
        main._end_playback_transition_attempt()
        await asyncio.wait_for(task, timeout=5)

        self.assertEqual(self._request_targets(), [],
                         "EOF(A) is stale after the B context was fully published")
        self.assertEqual(
            main.current_track_info and main.current_track_info.get("id"), "b1",
            "the committed B context must stay untouched",
        )
        self.assertEqual(playback_queue.queue.index, 0)

    # -- E2. erfolgreicher Source-Wechsel B entwertet EOF(A) -------------------

    async def test_successful_source_switch_invalidates_eof(self):
        self._install()
        main._begin_playback_transition_attempt()
        task = asyncio.create_task(
            main.on_player_state_change(_ended_snapshot(), event_commit_id=COMMIT_A)
        )
        await asyncio.sleep(0)
        self.assertFalse(task.done())

        candidate = playback_queue.cleared_queue_candidate(_local("b1", 96000))
        candidate.queue = [_local("b1", 96000), _local("b2", 96000)]
        candidate.original = [dict(t) for t in candidate.queue]
        candidate.index = 0
        candidate.mode = "app_replace"
        playback_queue.queue.commit(candidate)
        main._commit_coordinated_track(
            candidate.queue[0], source="local", commit_token=COMMIT_B
        )
        main._end_playback_transition_attempt()
        await asyncio.wait_for(task, timeout=5)

        self.assertEqual(self._request_targets(), [],
                         "successful source switch B invalidates EOF(A)")
        self.assertEqual(playback_queue.queue.index, 0)

    # -- F. Normales aktuelles EOF ----------------------------------------------

    async def test_current_eof_advances_exactly_once(self):
        self._install()
        await main.on_player_state_change(
            _ended_snapshot(), event_commit_id=COMMIT_A
        )

        self.assertEqual(self._request_targets(), ["a2"],
                         "current EOF advances to the next queue track")
        self.assertEqual(playback_queue.queue.index, 1)

    async def test_current_eof_at_queue_end_clears_queue_and_broadcasts(self):
        self._install()
        playback_queue.queue.index = 1
        main.current_track_info = dict(playback_queue.queue.tracks[1])
        main.last_track_info = dict(playback_queue.queue.tracks[1])

        await main.on_player_state_change(
            _ended_snapshot(), event_commit_id=COMMIT_A
        )

        self.assertEqual(playback_queue.queue.tracks, [],
                         "EOF at the queue end commits the terminal end state")
        self.assertEqual(playback_queue.queue.index, -1)
        self.assertEqual(main.current_track_info.get("id"), "a2",
                         "track context must survive the terminal end state")
        self.assertEqual(self._request_targets(), [],
                         "queue end must not start a transition")
        self.assertEqual(
            [message.get("type") for message in self.manager.broadcasts],
            ["playback"],
            "the cleared end state must be broadcast",
        )

    # -- G. Native MPV ------------------------------------------------------------

    async def test_native_queue_guard_unchanged(self):
        self._install()
        playback_queue.queue.tracks = [_local("a1", 48000), _local("a2", 48000)]
        playback_queue.queue.original = [dict(t) for t in playback_queue.queue.tracks]
        playback_queue.queue.index = 0
        playback_queue.queue.mode = "native_mpv"

        await main.on_player_state_change(
            _ended_snapshot(), event_commit_id=COMMIT_A
        )

        self.assertEqual(self._request_targets(), [],
                         "native MPV owns playlist boundaries; no app advance")
        self.assertEqual(playback_queue.queue.index, 0)

    # -- H. Loop / Shuffle ---------------------------------------------------------

    async def test_current_eof_loop_wrap_still_works(self):
        self._install()
        playback_queue.queue.tracks = [_local("a1", 44100), _local("a2", 96000)]
        playback_queue.queue.original = [dict(t) for t in playback_queue.queue.tracks]
        playback_queue.queue.index = 1
        playback_queue.queue.loop = True

        await main.on_player_state_change(
            _ended_snapshot(), event_commit_id=COMMIT_A
        )

        self.assertEqual(self._request_targets(), ["a1"],
                         "current EOF wraps a looped queue")
        self.assertEqual(playback_queue.queue.index, 0)

    async def test_current_eof_shuffle_wrap_still_works(self):
        self._install()
        playback_queue.queue.tracks = [_local("a1", 44100), _local("a2", 96000)]
        playback_queue.queue.original = [dict(t) for t in playback_queue.queue.tracks]
        playback_queue.queue.index = 1
        playback_queue.queue.shuffle = True
        playback_queue.queue.loop = True

        await main.on_player_state_change(
            _ended_snapshot(), event_commit_id=COMMIT_A
        )

        # Shuffle-Wrap: die neu gemischte Queue beginnt mit dem bisherigen
        # Track (a2), der neue Ziel-Track a1 steht an Index 1.
        self.assertEqual(len(self._request_targets()), 1,
                         "current EOF reshuffles the looped queue once")
        self.assertEqual(self._request_targets(), ["a1"])
        self.assertEqual([t.get("id") for t in playback_queue.queue.tracks], ["a2", "a1"])
        self.assertEqual(playback_queue.queue.index, 1)

    async def test_stale_eof_shuffle_wrap_is_noop(self):
        self._install()
        candidate = playback_queue.cleared_queue_candidate(_local("b1", 96000))
        candidate.queue = [_local("b1", 96000), _local("b2", 96000)]
        candidate.original = [dict(t) for t in candidate.queue]
        candidate.index = 1
        candidate.mode = "app_replace"
        candidate.loop = True
        candidate.shuffle = True
        playback_queue.queue.commit(candidate)
        main._commit_coordinated_track(
            candidate.queue[1], source="local", commit_token=COMMIT_B
        )

        await main.on_player_state_change(
            _ended_snapshot(), event_commit_id=COMMIT_A
        )

        self.assertEqual(self._request_targets(), [])
        self.assertEqual(playback_queue.queue.index, 1)

    async def test_spotify_play_publishes_token_before_state_read(self):
        self._install()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking_spotify_state(data=None):
            entered.set()
            await release.wait()
            return {"status": "Playing", "footer_owner": "spotify"}

        async def spotify_transition(request):
            self.transition_requests.append(request)
            return SimpleNamespace(
                committed=True, transition_id=COMMIT_B, target_rate=TARGET_RATE
            )

        async def fake_broadcast(data=None):
            return data

        main._begin_playback_transition_attempt()
        eof_task = asyncio.create_task(
            main.on_player_state_change(_ended_snapshot(), event_commit_id=COMMIT_A)
        )
        await asyncio.sleep(0)
        self.assertFalse(eof_task.done())

        with patch.object(main, "_run_coordinated_transition", spotify_transition), patch.object(
            main, "get_spotify_ui_state", blocking_spotify_state
        ), patch.object(main, "broadcast_spotify_state", fake_broadcast):
            play_task = asyncio.create_task(main.api_spotify_play())
            await asyncio.wait_for(entered.wait(), timeout=5)
            self.assertEqual(main._current_playback_commit_id(), COMMIT_B)
            self.assertEqual(main.current_footer_owner, "spotify")
            release.set()
            main._end_playback_transition_attempt()
            await asyncio.gather(play_task, eof_task)

        self.assertNotIn("a2", self._request_targets())
        self.assertEqual(playback_queue.queue.index, 0)

    async def test_spotify_toggle_play_publishes_token_before_state_read(self):
        self._install()
        entered = asyncio.Event()
        release = asyncio.Event()
        state_calls = []

        async def spotify_state(data=None):
            state_calls.append(data)
            if len(state_calls) == 1:
                return {"status": "Paused", "footer_owner": "local"}
            entered.set()
            await release.wait()
            return {"status": "Playing", "footer_owner": "spotify"}

        async def spotify_transition(request):
            self.transition_requests.append(request)
            return SimpleNamespace(
                committed=True, transition_id=COMMIT_B, target_rate=TARGET_RATE
            )

        async def fake_broadcast(data=None):
            return data

        main._begin_playback_transition_attempt()
        eof_task = asyncio.create_task(
            main.on_player_state_change(_ended_snapshot(), event_commit_id=COMMIT_A)
        )
        await asyncio.sleep(0)
        self.assertFalse(eof_task.done())

        with patch.object(main, "_run_coordinated_transition", spotify_transition), patch.object(
            main, "get_spotify_ui_state", spotify_state
        ), patch.object(main, "broadcast_spotify_state", fake_broadcast):
            toggle_task = asyncio.create_task(main.api_spotify_toggle())
            await asyncio.wait_for(entered.wait(), timeout=5)
            self.assertEqual(main._current_playback_commit_id(), COMMIT_B)
            self.assertEqual(main.current_footer_owner, "spotify")
            release.set()
            main._end_playback_transition_attempt()
            await asyncio.gather(toggle_task, eof_task)

        self.assertNotIn("a2", self._request_targets())
        self.assertEqual(playback_queue.queue.index, 0)

    async def test_single_track_loop_replay_publishes_new_instance_token(self):
        self._install()
        playback_queue.queue.single_track_loop = True
        replay_requests = []

        async def replay_transition(request):
            replay_requests.append(request)
            return SimpleNamespace(
                committed=True, transition_id=COMMIT_B, target_rate=44100
            )

        with patch.object(main, "_run_coordinated_transition", replay_transition):
            await main.on_player_state_change(
                _ended_snapshot(), event_commit_id=COMMIT_A
            )
            self.assertEqual(main._current_playback_commit_id(), COMMIT_B)
            self.assertEqual(len(replay_requests), 1)

            await main.on_player_state_change(
                _ended_snapshot(seq=12), event_commit_id=COMMIT_A
            )
            self.assertEqual(main._current_playback_commit_id(), COMMIT_B)
            self.assertEqual(len(replay_requests), 1)

    # -- Dispatch-Kontrakt ----------------------------------------------------------

    async def test_dispatch_wrapper_captures_token_at_notify_time(self):
        self._install()
        seen = {}

        async def fake_callback(state, event_commit_id=None):
            seen["event_commit_id"] = event_commit_id

        with patch.object(main, "on_player_state_change", fake_callback):
            coroutine = main._dispatch_player_state_change({"ended": True})
            # Ein neuerer Commit darf die Erfassung nicht mehr aendern: der
            # Token wird beim Dispatch gelesen, nicht beim Task-Start.
            main.playback_context_commit_id = COMMIT_B
            await coroutine

        self.assertEqual(seen["event_commit_id"], COMMIT_A,
                         "token must be captured at dispatch time")


class PlayerDispatchContractTests(unittest.IsolatedAsyncioTestCase):
    """player.py: sync Callback mit Awaitable-Rückgabe wird als Task geplant."""

    async def test_sync_callback_awaitable_result_is_scheduled(self):
        wrapper = player.MPVWrapper()
        wrapper._running = True
        captured = {}

        def dispatcher(state):
            captured["state_seq"] = state.get("_seq")
            captured["awaitable_created"] = True

            async def coroutine():
                captured["task_ran"] = True

            return coroutine()

        wrapper.register_callbacks(dispatcher)
        wrapper._notify_callbacks()
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertTrue(captured.get("awaitable_created"))
        self.assertTrue(captured.get("task_ran"),
                        "sync callback awaitable must run as a task")
        await wrapper.shutdown_callbacks(dispatcher)

    async def test_playlist_entry_id_survives_ended_snapshot(self):
        wrapper = player.MPVWrapper()
        wrapper._running = True
        wrapper._last_end_reason = "eof"
        wrapper._last_end_entry_id = 42
        wrapper._state["current_file"] = "/music/a.flac"
        wrapper._state["ended"] = False
        wrapper._handle_event({"event": "property-change", "name": "path", "data": None})
        self.assertTrue(wrapper._state["ended"])
        self.assertEqual(wrapper._state["end_reason"], "eof")
        self.assertEqual(wrapper._state["end_entry_id"], 42)

        wrapper._handle_event({"event": "property-change", "name": "path", "data": "/music/b.flac"})
        self.assertFalse(wrapper._state["ended"])
        self.assertIsNone(wrapper._state["end_reason"])
        self.assertIsNone(wrapper._state["end_entry_id"])


class MarkerRemovalTests(unittest.TestCase):
    """Der tote queue_transition_target_url-Marker ist vollständig entfernt."""

    def test_marker_no_longer_exists(self):
        self.assertFalse(hasattr(main, "queue_transition_target_url"))


if __name__ == "__main__":
    unittest.main()
