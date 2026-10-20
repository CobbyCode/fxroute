#!/usr/bin/env python3
"""MPRIS helper: player properties and media-key re-announce decisions.

gsd-media-keys (GNOME 50, plugins/media-keys/mpris-controller.c) sends media
keys to one MPRIS player. ``GsdModel`` mirrors its three rules; the scenarios
drive it together with the helper's AnnounceTracker and check which player
receives Play/Pause. The bug: a paused Spotify Desktop kept the keys while
another FXRoute source was active, so Play/Pause started Spotify.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playback import mpris_helper
from playback.mpris_helper import AnnounceTracker, PlayerModel, normalize_state, parse_lines

FXROUTE = mpris_helper.BUS_NAME
SPOTIFY = "org.mpris.MediaPlayer2.spotify"
SPOTIFYD = "org.mpris.MediaPlayer2.spotifyd.instance42"


class GsdModel:
    """gsd-media-keys MprisController selection rules."""

    def __init__(self):
        self.current = None
        self.others = []
        self.status = {}

    def _playing(self, name):
        return self.status.get(name) == "Playing"

    def appeared(self, name, status):
        # mpris_proxy_ready_cb
        self.status[name] = status
        if self.current is not None:
            if self._playing(self.current):
                self.others.insert(0, name)
                return
            self.others.insert(0, self.current)
        self.current = name

    def vanished(self, name):
        # mpris_client_notify_name_owner_cb
        self.status.pop(name, None)
        if name == self.current:
            self.current = self.others.pop(0) if self.others else None
        elif name in self.others:
            self.others.remove(name)

    def properties_changed(self, name, status):
        # mpris_client_properties_changed_cb
        self.status[name] = status
        if name == self.current:
            return
        if self.current is not None and self._playing(self.current):
            return
        if self._playing(name):
            if name in self.others:
                self.others.remove(name)
            if self.current is not None:
                self.others.insert(0, self.current)
            self.current = name


class Desktop:
    """Session bus events seen by gsd and by the helper's tracker."""

    def __init__(self):
        self.gsd = GsdModel()
        self.tracker = AnnounceTracker()
        self.fxroute_status = "Stopped"
        self.fxroute_running = False
        self.announce_pending = False

    def add_player(self, name, status):
        self.gsd.appeared(name, status)
        self.tracker.player_added(name, ":" + name)
        if self.tracker.status_known(":" + name, status):
            self.announce_pending = True

    def remove_player(self, name):
        self.gsd.vanished(name)
        self.tracker.player_removed(name)
        self.announce_pending = True

    def player_status(self, name, status):
        self.gsd.properties_changed(name, status)
        if self.tracker.status_changed(":" + name, status):
            self.announce_pending = True

    def start_fxroute(self):
        self.fxroute_running = True
        self.gsd.appeared(FXROUTE, self.fxroute_status)

    def fxroute(self, status):
        self.fxroute_status = status
        self.gsd.properties_changed(FXROUTE, status)

    def settle(self):
        """Run the debounced re-announce: release and request the name."""
        if self.fxroute_running and self.announce_pending and not self.tracker.blocked():
            self.gsd.vanished(FXROUTE)
            self.gsd.appeared(FXROUTE, self.fxroute_status)
        self.announce_pending = False

    def media_key_target(self):
        return self.gsd.current


class ReannounceScenarios(unittest.TestCase):
    def test_paused_spotify_loses_the_keys_when_fxroute_starts(self):
        desktop = Desktop()
        desktop.add_player(SPOTIFY, "Paused")
        desktop.settle()
        self.assertEqual(desktop.media_key_target(), SPOTIFY)  # the bug state
        desktop.start_fxroute()
        self.assertEqual(desktop.media_key_target(), FXROUTE)

    def test_spotify_returns_the_keys_after_it_stops_playing(self):
        desktop = Desktop()
        desktop.add_player(SPOTIFY, "Paused")
        desktop.start_fxroute()
        desktop.player_status(SPOTIFY, "Playing")  # Spotify was the source
        desktop.fxroute("Playing")
        desktop.settle()
        self.assertEqual(desktop.media_key_target(), SPOTIFY)
        # The user switches FXRoute to radio: Spotify is paused first, the
        # radio state arrives while gsd still holds Spotify.
        desktop.player_status(SPOTIFY, "Paused")
        desktop.fxroute("Paused")
        desktop.settle()
        self.assertEqual(desktop.media_key_target(), FXROUTE)

    def test_spotify_paused_while_fxroute_already_reports_playing(self):
        desktop = Desktop()
        desktop.add_player(SPOTIFY, "Paused")
        desktop.start_fxroute()
        desktop.player_status(SPOTIFY, "Playing")
        desktop.fxroute("Playing")  # ignored by gsd: Spotify is playing
        desktop.player_status(SPOTIFY, "Paused")
        self.assertEqual(desktop.media_key_target(), SPOTIFY)
        desktop.settle()
        self.assertEqual(desktop.media_key_target(), FXROUTE)

    def test_restarted_spotify_client_does_not_keep_the_keys(self):
        desktop = Desktop()
        desktop.start_fxroute()
        desktop.add_player(SPOTIFY, "Paused")
        self.assertEqual(desktop.media_key_target(), SPOTIFY)
        desktop.settle()
        self.assertEqual(desktop.media_key_target(), FXROUTE)

    def test_playing_player_keeps_the_keys_until_it_stops(self):
        desktop = Desktop()
        desktop.start_fxroute()
        desktop.add_player(SPOTIFYD, "Playing")
        desktop.settle()
        self.assertEqual(desktop.media_key_target(), SPOTIFYD)
        desktop.player_status(SPOTIFYD, "Stopped")
        desktop.settle()
        self.assertEqual(desktop.media_key_target(), FXROUTE)

    def test_no_reannounce_while_another_player_plays(self):
        desktop = Desktop()
        desktop.add_player(SPOTIFY, "Paused")
        desktop.start_fxroute()
        desktop.fxroute("Playing")
        desktop.player_status(SPOTIFY, "Playing")  # gsd keeps FXRoute
        desktop.add_player(SPOTIFYD, "Paused")  # remembered behind FXRoute
        self.assertEqual(desktop.media_key_target(), FXROUTE)
        # A release now would hand the keys to the playing Spotify.
        self.assertTrue(desktop.tracker.blocked())
        desktop.settle()
        self.assertEqual(desktop.media_key_target(), FXROUTE)

    def test_vanished_current_player_falls_back_to_fxroute(self):
        desktop = Desktop()
        desktop.add_player(SPOTIFYD, "Paused")
        desktop.start_fxroute()
        desktop.add_player(SPOTIFY, "Paused")
        desktop.remove_player(SPOTIFY)
        desktop.settle()
        self.assertEqual(desktop.media_key_target(), FXROUTE)


class AnnounceTrackerTests(unittest.TestCase):
    def test_pending_status_blocks_until_known(self):
        tracker = AnnounceTracker()
        tracker.player_added(SPOTIFY, ":1.5")
        self.assertTrue(tracker.blocked())
        self.assertTrue(tracker.status_known(":1.5", "Paused"))
        self.assertFalse(tracker.blocked())

    def test_only_a_playing_to_other_transition_triggers(self):
        tracker = AnnounceTracker()
        tracker.player_added(SPOTIFY, ":1.5")
        tracker.status_known(":1.5", "Paused")
        self.assertFalse(tracker.status_changed(":1.5", "Paused"))
        self.assertFalse(tracker.status_changed(":1.5", "Playing"))
        self.assertTrue(tracker.status_changed(":1.5", "Paused"))
        self.assertFalse(tracker.status_changed(":1.99", "Paused"))  # unknown sender

    def test_own_name_and_foreign_names_are_not_players(self):
        tracker = AnnounceTracker()
        self.assertFalse(tracker.is_other_player(FXROUTE))
        self.assertFalse(tracker.is_other_player("org.gnome.SettingsDaemon.MediaKeys"))
        self.assertTrue(tracker.is_other_player(SPOTIFY))

    def test_removing_a_name_forgets_its_status(self):
        tracker = AnnounceTracker()
        tracker.player_added(SPOTIFY, ":1.5")
        tracker.player_removed(SPOTIFY)
        self.assertEqual(tracker.statuses, {})
        self.assertFalse(tracker.blocked())


class PlayerModelTests(unittest.TestCase):
    def test_default_player_is_stopped_without_track(self):
        properties = PlayerModel().properties()
        self.assertEqual(properties["PlaybackStatus"], "Stopped")
        self.assertEqual(properties["Metadata"], {"mpris:trackid": mpris_helper.NO_TRACK})
        self.assertTrue(properties["CanControl"])
        self.assertFalse(properties["CanSeek"])

    def test_update_reports_only_changed_properties(self):
        model = PlayerModel()
        changed = model.update({"status": "Playing", "title": "Groove Salad", "can_pause": True})
        self.assertEqual(changed["PlaybackStatus"], "Playing")
        self.assertEqual(changed["Metadata"]["xesam:title"], "Groove Salad")
        self.assertTrue(changed["CanPause"])
        self.assertEqual(model.update({"status": "Playing", "title": "Groove Salad", "can_pause": True}), {})

    def test_new_track_gets_a_new_track_id(self):
        model = PlayerModel()
        model.update({"title": "One", "artist": "A"})
        first = model.metadata()["mpris:trackid"]
        model.update({"title": "Two", "artist": "A"})
        self.assertNotEqual(model.metadata()["mpris:trackid"], first)
        self.assertEqual(model.metadata()["xesam:artist"], ["A"])

    def test_invalid_status_is_stopped(self):
        self.assertEqual(normalize_state({"status": "Buffering"})["status"], "Stopped")
        self.assertEqual(normalize_state(None)["status"], "Stopped")


class CommandAndLineTests(unittest.TestCase):
    def test_every_transport_method_maps_to_a_command(self):
        self.assertEqual(mpris_helper.COMMANDS["PlayPause"], "toggle")
        self.assertEqual(set(mpris_helper.COMMANDS),
                         {"PlayPause", "Play", "Pause", "Stop", "Next", "Previous"})

    def test_parse_lines_keeps_the_partial_tail(self):
        states, rest = parse_lines(b'{"status": "Paused"}\nnot json\n{"status": "Pl')
        self.assertEqual(states, [{"status": "Paused"}])
        self.assertEqual(rest, b'{"status": "Pl')


if __name__ == "__main__":
    unittest.main()
