#!/usr/bin/env python3
"""Media keys control only the active FXRoute source.

Before the MPRIS bridge, GNOME sent Play/Pause to a paused Spotify Desktop,
which started Spotify and made FXRoute claim it while radio, TIDAL, Qobuz or
local playback was the selected source. The bridge state follows the
committed playback owner, routes keys through the footer's global transport
and does nothing for line inputs.
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playback.media_keys import MediaKeyBridge, MediaKeyDependencies, mpris_state, route_command

PAUSED_SPOTIFY = {"available": True, "status": "Paused", "title": "Guitarra", "artist": "Akshin Alizadeh"}
RADIO_TRACK = {"source": "radio", "title": "Groove Salad", "url": "http://ice.somafm.com/groovesalad"}


def state_for(owner, *, source_mode="app-playback", player=None, track=None, last_track=None,
              spotify=None, qobuz=None, queue_count=0):
    return mpris_state(
        source_mode=source_mode,
        owner=owner,
        player_state=player or {},
        track=track,
        last_track=last_track,
        spotify_state=spotify,
        qobuz_state=qobuz,
        queue_count=queue_count,
    )


PLAYING = {"playing": True, "paused": False, "current_file": "http://stream", "ended": False}
PAUSED = {"playing": False, "paused": True, "current_file": "http://stream", "ended": False}
ENDED = {"playing": False, "paused": False, "current_file": None, "ended": True}


class MprisStateTests(unittest.TestCase):
    def test_radio_owner_ignores_a_paused_spotify_client(self):
        state = state_for("radio", player=PLAYING, track=RADIO_TRACK, spotify=PAUSED_SPOTIFY)
        self.assertEqual(state["source"], "radio")
        self.assertEqual(state["status"], "Playing")
        self.assertEqual(state["title"], "Groove Salad")
        self.assertEqual(route_command("toggle", state), "toggle")

    def test_spotify_owner_reports_spotify_transport(self):
        state = state_for("spotify", player=ENDED, spotify=PAUSED_SPOTIFY)
        self.assertEqual((state["source"], state["status"]), ("spotify", "Paused"))
        self.assertTrue(state["can_play"])
        self.assertEqual(route_command("toggle", state), "toggle")
        self.assertEqual(route_command("next", state), "next")

    def test_streaming_owner_without_media_has_nothing_to_play(self):
        state = state_for("qobuz", qobuz={"available": True, "status": "Stopped", "title": ""})
        self.assertFalse(state["can_play"])
        self.assertIsNone(route_command("toggle", state))
        self.assertIsNone(route_command("next", state))

    def test_line_inputs_have_no_transport(self):
        for mode in ("external-input", "bluetooth-input", "stdin-input"):
            state = state_for("radio", source_mode=mode, player=PAUSED, track=RADIO_TRACK)
            self.assertEqual(state["status"], "Stopped")
            for command in ("toggle", "play", "pause", "stop", "next", "previous"):
                self.assertIsNone(route_command(command, state), (mode, command))

    def test_paused_and_ended_native_playback(self):
        paused = state_for("local", player=PAUSED, track={"source": "local", "title": "Breathe"})
        self.assertEqual(paused["status"], "Paused")
        self.assertTrue(paused["can_play"])
        ended = state_for("local", player=ENDED, track={"source": "local", "title": "Breathe"})
        self.assertEqual(ended["status"], "Stopped")
        self.assertTrue(ended["can_play"])  # the toggle replays the last track
        empty = state_for(None, player=ENDED)
        self.assertFalse(empty["can_play"])
        self.assertIsNone(route_command("toggle", empty))

    def test_paused_flag_wins_over_a_stale_playing_flag(self):
        state = state_for("tidal", player={**PAUSED, "playing": True}, track={"source": "tidal", "title": "T"})
        self.assertEqual(state["status"], "Paused")

    def test_queue_controls_need_a_queue(self):
        single = state_for("local", player=PLAYING, track={"title": "One"}, queue_count=1)
        self.assertIsNone(route_command("next", single))
        queued = state_for("local", player=PLAYING, track={"title": "One"}, queue_count=3)
        self.assertEqual(route_command("next", queued), "next")
        self.assertEqual(route_command("previous", queued), "previous")


class RouteCommandTests(unittest.TestCase):
    def test_play_and_pause_never_invert_the_state(self):
        playing = state_for("radio", player=PLAYING, track=RADIO_TRACK)
        paused = state_for("radio", player=PAUSED, track=RADIO_TRACK)
        self.assertIsNone(route_command("play", playing))
        self.assertEqual(route_command("pause", playing), "toggle")
        self.assertEqual(route_command("stop", playing), "toggle")
        self.assertEqual(route_command("play", paused), "toggle")
        self.assertIsNone(route_command("pause", paused))
        self.assertIsNone(route_command("stop", paused))
        self.assertIsNone(route_command("raise", paused))


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def bridge(self, state, run_action, **kwargs):
        return MediaKeyBridge(MediaKeyDependencies(get_state=lambda: state, run_action=run_action), **kwargs)

    async def test_line_input_key_runs_nothing(self):
        run_action = AsyncMock()
        bridge = self.bridge(state_for("radio", source_mode="external-input", player=PAUSED), run_action)
        bridge.handle_command("toggle")
        await bridge.action_task
        run_action.assert_not_awaited()

    async def test_second_press_during_an_action_is_dropped(self):
        release = asyncio.Event()
        calls = []

        async def run_action(action):
            calls.append(action)
            await release.wait()

        bridge = self.bridge(state_for("radio", player=PLAYING, track=RADIO_TRACK), run_action)
        bridge.handle_command("toggle")
        first = bridge.action_task
        await asyncio.sleep(0)
        bridge.handle_command("toggle")
        self.assertIs(bridge.action_task, first)
        release.set()
        await first
        self.assertEqual(calls, ["toggle"])

    async def test_failed_action_is_logged_not_raised(self):
        bridge = self.bridge(state_for("radio", player=PLAYING, track=RADIO_TRACK),
                             AsyncMock(side_effect=RuntimeError("409: transition")))
        with self.assertLogs("playback.media_keys", "INFO") as logs:
            bridge.handle_command("toggle")
            await bridge.action_task
        self.assertTrue(any("toggle failed" in line for line in logs.output))

    async def test_helper_process_commands_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            received = Path(tmp) / "states.jsonl"
            script = Path(tmp) / "helper.py"
            script.write_text(
                "import json, sys\n"
                "print(json.dumps({'command': 'toggle'}), flush=True)\n"
                "print('mpris-helper: owns org.mpris.MediaPlayer2.fxroute', file=sys.stderr, flush=True)\n"
                f"with open({str(received)!r}, 'a') as out:\n"
                "    for line in sys.stdin:\n"
                "        out.write(line)\n"
                "        out.flush()\n"
            )
            done = asyncio.Event()
            actions = []

            async def run_action(action):
                actions.append(action)
                done.set()

            state = state_for("radio", player=PLAYING, track=RADIO_TRACK, spotify=PAUSED_SPOTIFY)
            bridge = self.bridge(state, run_action, command=[sys.executable, str(script)], poll_seconds=0.05)
            with self.assertLogs("playback.media_keys", "INFO") as logs:
                bridge.start()
                await asyncio.wait_for(done.wait(), 10)
                for _ in range(200):
                    if received.exists() and received.read_text():
                        break
                    await asyncio.sleep(0.02)
                await bridge.stop()
            lines = received.read_text().splitlines()
        self.assertEqual(actions, ["toggle"])
        self.assertEqual(json.loads(lines[0])["status"], "Playing")
        self.assertEqual(json.loads(lines[0])["source"], "radio")
        self.assertTrue(any("source=radio" in line and "action=toggle" in line for line in logs.output))
        self.assertIsNone(bridge.task)

    async def test_missing_helper_is_reported_once(self):
        bridge = self.bridge({}, AsyncMock(), command=["/nonexistent/mpris-helper"],
                             unavailable_retry_seconds=0.01)
        with self.assertLogs("playback.media_keys", "DEBUG") as logs:
            bridge.start()
            await asyncio.sleep(0.15)
            await bridge.stop()
        warnings = [line for line in logs.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 1)


class MainWiringTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        import main
        cls.main = main

    async def test_actions_use_the_footer_transport_endpoints(self):
        main = self.main
        for action, endpoint in (("toggle", "toggle_playback"), ("next", "next_playback"),
                                 ("previous", "previous_playback")):
            with patch.object(main, endpoint, AsyncMock()) as mocked:
                await main._run_media_key_action(action)
            mocked.assert_awaited_once()

    async def test_state_follows_the_committed_owner(self):
        main = self.main
        player = SimpleNamespace(state=PLAYING)
        with patch.object(main, "_resolve_playback_owner", return_value="radio"), \
                patch.object(main.runtime, "player_instance", player), \
                patch.object(main.playback_state, "source_mode", "app-playback"), \
                patch.object(main.playback_state, "current_track_info", RADIO_TRACK), \
                patch.object(main.playback_state, "latest_spotify_state", PAUSED_SPOTIFY):
            state = main._media_key_state()
        self.assertEqual((state["source"], state["status"], state["title"]), ("radio", "Playing", "Groove Salad"))

    def test_bridge_starts_with_the_lifespan_and_stops_in_cleanup(self):
        source = Path(self.main.__file__).read_text()
        self.assertIn("media_key_bridge.start()", source)
        self.assertIn('cleanup("media-key-bridge", media_key_bridge.stop)', source)


if __name__ == "__main__":
    unittest.main()
