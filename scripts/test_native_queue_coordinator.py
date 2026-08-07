#!/usr/bin/env python3
"""Focused contracts for homogeneous native MPV queues."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from playback_transition import TransitionRequest


def _track(track_id: str, rate: int | None, *, source: str = "local") -> dict:
    return {
        "id": track_id,
        "source": source,
        "url": f"/music/{track_id}.flac",
        "sample_rate_hz": rate,
    }


class NativeQueueSelectionTests(unittest.TestCase):
    def test_homogeneous_44100_and_48000_queues_are_native(self):
        for rate in (44100, 48000):
            self.assertTrue(main._can_use_native_local_queue([_track("a", rate), _track("b", rate)]))

    def test_mixed_or_unknown_queue_stays_app_replace(self):
        self.assertFalse(main._can_use_native_local_queue([_track("a", 44100), _track("b", 48000)]))
        self.assertFalse(main._can_use_native_local_queue([_track("a", 44100), _track("b", None)]))

    def test_loudness_volume_uses_canonical_curve_and_master_100(self):
        class LoudnessManager:
            def load_global_extras(self):
                return {"loudness": {"enabled": True}}

            def loudness_db_from_percent(self, percent):
                return -float(percent)

            def set_loudness_volume_db(self, volume_db):
                return {"extras": {"loudness": {"params": {"volumeDb": volume_db}}}}

        with patch.object(main, "easyeffects_manager", LoudnessManager()), \
             patch.object(main, "set_output_volume", return_value=100) as set_master:
            result = main._set_canonical_output_volume(32)
        self.assertEqual(result["volume"], 32)
        self.assertEqual(result["loudnessVolumeDb"], -32.0)
        set_master.assert_called_once_with(100)


class _QueuePlayer:
    _running = True

    def __init__(self):
        self.state = {
            "current_file": None,
            "paused": True,
            "playing": False,
            "ended": False,
            "position": 0.0,
            "volume": 100,
            "playlist_pos": None,
        }
        self.calls = []
        self.playlist = []

    def set_pause(self, paused):
        self.calls.append(("pause", bool(paused)))
        self.state["paused"] = bool(paused)
        self.state["playing"] = not bool(paused) and bool(self.state.get("current_file"))

    def loadfile(self, path, mode="replace", start_paused=None):
        self.calls.append(("loadfile", path, mode, start_paused))
        if mode == "append":
            self.playlist.append(path)
        else:
            self.playlist = [path]
            self.state["current_file"] = path
            self.state["paused"] = True if start_paused is None else bool(start_paused)
            self.state["playing"] = False

    def set_playlist_pos(self, index):
        self.calls.append(("playlist-pos", index))
        self.state["playlist_pos"] = index
        if 0 <= index < len(self.playlist):
            self.state["current_file"] = self.playlist[index]

    def set_loop_playlist(self, enabled):
        self.calls.append(("loop-playlist", bool(enabled)))

    def set_loop_file(self, enabled):
        self.calls.append(("loop-file", bool(enabled)))

    def set_shuffle(self, enabled):
        self.calls.append(("shuffle", bool(enabled)))

    def set_volume(self, volume):
        self.calls.append(("volume", volume))
        self.state["volume"] = volume

    def remove_playlist_index(self, index):
        self.calls.append(("remove-playlist", index))
        if 0 <= index < len(self.playlist):
            del self.playlist[index]
            current = self.state.get("playlist_pos")
            if isinstance(current, int) and index < current:
                self.state["playlist_pos"] = current - 1

    def seek(self, position):
        self.calls.append(("seek", position))
        self.state["position"] = position

    def get_property(self, name):
        if name == "playlist-pos":
            return self.state.get("playlist_pos")
        if name == "playlist-count":
            return len(self.playlist)
        if name == "audio-params":
            return {"samplerate": 48000}
        if name == "time-pos":
            return self.state.get("position", 0.0)
        raise KeyError(name)


class NativeQueueRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_coordinator_runtime_loads_one_paused_playlist_and_controls(self):
        fake = _QueuePlayer()
        request = TransitionRequest(
            operation="play",
            source="local",
            target_rate=44100,
            target_url="/music/b.flac",
            should_play=True,
            reload_source=True,
            native_queue=(_track("a", 44100), _track("b", 44100)),
            native_queue_index=1,
            native_queue_loop=True,
            native_queue_shuffle=True,
        )
        with patch.object(main, "player_instance", fake), \
             patch.object(main, "_ensure_mpv_to_easyeffects_links", new=_true_async):
            runtime = main.FxrouteTransitionRuntime()
            await runtime.prepare_target_source(request)

        load_calls = [call for call in fake.calls if call[0] == "loadfile"]
        self.assertEqual(load_calls[0][1:3], ("/music/a.flac", "replace"))
        self.assertEqual(load_calls[1][1:3], ("/music/b.flac", "append"))
        self.assertIn(("playlist-pos", 1), fake.calls)
        self.assertIn(("loop-playlist", True), fake.calls)
        self.assertIn(("shuffle", True), fake.calls)
        self.assertNotIn(("loadfile", "/music/b.flac", "replace", True), fake.calls)

    async def test_start_uses_live_ipc_even_when_cached_position_is_stale(self):
        class LivePlayer(_QueuePlayer):
            def get_property(self, name):
                return {
                    "path": "/music/a.flac",
                    "pause": False,
                    "idle-active": False,
                    "time-pos": 0.0,
                    "audio-params": {"samplerate": 44100, "format": "f32"},
                }[name]

        fake = LivePlayer()
        fake.state.update({"current_file": "/music/a.flac", "playing": False, "paused": True, "position": 99.0})
        request = TransitionRequest(
            operation="play",
            source="local",
            target_rate=44100,
            target_url="/music/a.flac",
            should_play=True,
            reload_source=False,
        )
        with patch.object(main, "player_instance", fake):
            runtime = main.FxrouteTransitionRuntime()
            await runtime.start_target_source(request)

    async def test_missing_local_metadata_rate_is_resolved_from_paused_mpv(self):
        fake = _QueuePlayer()
        request = TransitionRequest(
            operation="play",
            source="local",
            target_rate=None,
            target_url="/music/unknown.flac",
            should_play=True,
            reload_source=True,
        )
        with patch.object(main, "player_instance", fake):
            runtime = main.FxrouteTransitionRuntime()
            rate = await runtime.resolve_target_rate(request)

        self.assertEqual(rate, 48000)
        self.assertEqual(fake.state["current_file"], "/music/unknown.flac")
        self.assertTrue(fake.state["paused"])
        self.assertEqual(fake.state["volume"], 0)
        self.assertEqual(
            [call[:3] for call in fake.calls if call[0] == "loadfile"],
            [("loadfile", "/music/unknown.flac", "replace")],
        )

    async def test_missing_local_metadata_rate_aborts_when_mpv_has_no_rate(self):
        fake = _QueuePlayer()
        request = TransitionRequest(
            operation="play",
            source="local",
            target_rate=None,
            target_url="/music/unknown.flac",
            should_play=True,
            reload_source=True,
        )
        with patch.object(main, "player_instance", fake), patch.object(
            main, "_wait_for_player_audio_samplerate", new=AsyncMock(return_value=None)
        ):
            runtime = main.FxrouteTransitionRuntime()
            with self.assertRaisesRegex(RuntimeError, "audio-params"):
                await runtime.resolve_target_rate(request)


class NativeQueueCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_path_and_playlist_pos_update_context_without_transition(self):
        names = (
            "playback_queue", "playback_queue_mode", "playback_queue_index",
            "current_track_info", "last_track_info", "queue_transition_target_url",
            "latest_player_state_seq_seen", "queue_advancing", "manager",
            "peak_monitor", "source_transition_lock", "playback_transition_coordinator",
            "sync_peak_monitor_for_playback_state", "_schedule_radio_reconnect_if_needed",
            "build_playback_payload", "playback_intent_generation",
        )
        originals = {name: getattr(main, name) for name in names}
        try:
            main.playback_queue = [_track("a", 48000), _track("b", 48000), _track("c", 48000)]
            main.playback_queue_mode = "native_mpv"
            main.playback_queue_index = 0
            main.current_track_info = dict(main.playback_queue[0])
            main.last_track_info = dict(main.playback_queue[0])
            main.queue_transition_target_url = None
            main.latest_player_state_seq_seen = 0
            main.queue_advancing = False
            main.manager = SimpleNamespace(broadcast=_noop_async)
            main.peak_monitor = None
            main.source_transition_lock = None
            main.playback_transition_coordinator = SimpleNamespace(transition_active=False)
            main.sync_peak_monitor_for_playback_state = _noop_async
            main._schedule_radio_reconnect_if_needed = lambda _state: None
            main.build_playback_payload = lambda state: state

            for seq, track_id in enumerate(("b", "c"), start=1):
                await main.on_player_state_change({
                    "_seq": seq,
                    "current_file": f"/music/{track_id}.flac",
                    # Deliberately provide the shuffled/native position. The
                    # app context must use the URL and keep its stable index.
                    "playlist_pos": 0,
                    "paused": False,
                    "playing": True,
                    "ended": False,
                })
                expected_index = 1 if track_id == "b" else 2
                self.assertEqual(main.playback_queue_index, expected_index)
                self.assertEqual(main.current_track_info["id"], track_id)
        finally:
            for name, value in originals.items():
                setattr(main, name, value)


class NativeQueueMutationTests(unittest.TestCase):
    def test_clear_reduces_native_playlist_to_current_and_resets_controls(self):
        fake = _QueuePlayer()
        fake.playlist = ["/music/a.flac", "/music/b.flac", "/music/c.flac"]
        fake.state.update({
            "current_file": "/music/b.flac",
            "playlist_pos": 1,
            "paused": False,
            "playing": True,
        })
        names = (
            "player_instance", "playback_queue", "playback_queue_original",
            "playback_queue_index", "playback_queue_mode", "playback_queue_loop",
            "playback_queue_shuffle", "single_track_loop",
        )
        originals = {name: getattr(main, name) for name in names}
        try:
            main.player_instance = fake
            main.playback_queue = [_track("a", 48000), _track("b", 48000), _track("c", 48000)]
            main.playback_queue_original = list(main.playback_queue)
            main.playback_queue_index = 1
            main.playback_queue_mode = "native_mpv"
            main.playback_queue_loop = True
            main.playback_queue_shuffle = True
            main.single_track_loop = False

            main._clear_playback_queue()

            self.assertEqual(fake.playlist, ["/music/b.flac"])
            self.assertEqual(fake.state["current_file"], "/music/b.flac")
            self.assertEqual(main.playback_queue, [])
            self.assertFalse(main.playback_queue_loop)
            self.assertFalse(main.playback_queue_shuffle)
            self.assertIn(("remove-playlist", 2), fake.calls)
            self.assertIn(("remove-playlist", 0), fake.calls)
            self.assertIn(("loop-playlist", False), fake.calls)
            self.assertIn(("shuffle", False), fake.calls)
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

    def test_native_loop_updates_mpv_playlist_loop(self):
        fake = _QueuePlayer()
        names = "player_instance", "current_track_info", "playback_queue", "playback_queue_mode", "playback_queue_loop", "single_track_loop"
        originals = {name: getattr(main, name) for name in names}
        try:
            main.player_instance = fake
            main.current_track_info = _track("a", 48000)
            main.playback_queue = [_track("a", 48000), _track("b", 48000)]
            main.playback_queue_mode = "native_mpv"
            main.playback_queue_loop = False
            main.single_track_loop = False

            self.assertTrue(main._set_queue_loop(True))
            self.assertTrue(main.playback_queue_loop)
            self.assertIn(("loop-playlist", True), fake.calls)
            self.assertTrue(main._set_queue_loop(False))
            self.assertIn(("loop-playlist", False), fake.calls)
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

    def test_native_shuffle_toggles_mpv_without_reordering_fxroute_queue(self):
        fake = _QueuePlayer()
        names = "player_instance", "playback_queue", "playback_queue_mode", "playback_queue_shuffle"
        originals = {name: getattr(main, name) for name in names}
        try:
            main.player_instance = fake
            main.playback_queue = [_track("a", 48000), _track("b", 48000), _track("c", 48000)]
            main.playback_queue_mode = "native_mpv"
            main.playback_queue_shuffle = False
            original_ids = [track["id"] for track in main.playback_queue]

            self.assertTrue(main._set_queue_shuffle(True))
            self.assertTrue(main.playback_queue_shuffle)
            self.assertEqual([track["id"] for track in main.playback_queue], original_ids)
            self.assertIn(("shuffle", True), fake.calls)
            self.assertTrue(main._set_queue_shuffle(False))
            self.assertIn(("shuffle", False), fake.calls)
        finally:
            for name, value in originals.items():
                setattr(main, name, value)


async def _noop_async(*_args, **_kwargs):
    return None


async def _true_async(*_args, **_kwargs):
    return True


if __name__ == "__main__":
    unittest.main()
