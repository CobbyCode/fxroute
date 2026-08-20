#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Contract tests: master/Loudness separation and spotifyd producer detection.

The global FXRoute master is the single user-facing volume; Loudness volumeDb
is only the ISO-226 work point.  Spotify Desktop and spotifyd are one logical
source whose producer ports are resolved from the live sink-input identity.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
from streaming.spotify import mpris


def _players(names):
    async def fake_list_players(timeout=2.0):
        return list(names)
    return fake_list_players


class SpotifydMprisDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_is_spotifyd_player_matches_instance_pid(self):
        self.assertTrue(mpris.is_spotifyd_player("spotifyd"))
        self.assertTrue(mpris.is_spotifyd_player("spotifyd.instance1681535"))
        self.assertFalse(mpris.is_spotifyd_player("spotify"))
        self.assertFalse(mpris.is_spotifyd_player("spotifyd_helper"))

    async def test_detect_running_backend_recognizes_instance_pid(self):
        with mock.patch("streaming.spotify.mpris.list_players", new=_players(["spotifyd.instance1681535"])):
            self.assertEqual(await mpris.detect_running_backend(), "spotifyd")

    async def test_desktop_wins_only_when_desktop_is_present(self):
        with mock.patch("streaming.spotify.mpris.list_players", new=_players(["spotify", "spotifyd.instance5"])):
            self.assertEqual(await mpris.detect_running_backend(), "desktop")

    async def test_resolve_player_name_returns_running_instance(self):
        with mock.patch("streaming.spotify.mpris.list_players", new=_players(["spotifyd.instance1681535"])):
            self.assertEqual(await mpris.resolve_player_name("spotifyd"), "spotifyd.instance1681535")
        self.assertEqual(await mpris.resolve_player_name("desktop"), "spotify")
        self.assertEqual(await mpris.resolve_player_name(None), "spotify")


class SpotifyProducerResolutionTests(unittest.TestCase):
    def _entry(self, *, node_name=None, application_name=None, binary=None, media=None, rate=44100):
        props = {}
        if node_name is not None:
            props["node.name"] = node_name
        if application_name is not None:
            props["application.name"] = application_name
        if binary is not None:
            props["application.process.binary"] = binary
        if media is not None:
            props["media.name"] = media
        return {"id": "1", "sample_rate": rate, "corked": False, "properties": props}

    def test_spotifyd_producer_derived_from_node_name(self):
        with mock.patch.object(main, "_list_spotify_sink_inputs", return_value=[
            self._entry(node_name="spotifyd", binary="spotifyd"),
        ]):
            self.assertEqual(
                main._spotify_producer_for_coordinator(),
                ("spotifyd:output_FL", "spotifyd:output_FR"),
            )

    def test_desktop_producer_derived_from_node_name(self):
        with mock.patch.object(main, "_list_spotify_sink_inputs", return_value=[
            self._entry(node_name="spotify", media="Spotify"),
        ]):
            self.assertEqual(
                main._spotify_producer_for_coordinator(),
                ("spotify:output_FL", "spotify:output_FR"),
            )

    def test_anonymous_spotifyd_producer_uses_empty_node_ports(self):
        with mock.patch.object(main, "_list_spotify_sink_inputs", return_value=[
            self._entry(binary="spotifyd"),
        ]):
            self.assertEqual(
                main._spotify_producer_for_coordinator(),
                (":output_FL", ":output_FR"),
            )

    def test_anonymous_unknown_producer_is_not_guessed(self):
        with mock.patch.object(main, "_list_spotify_sink_inputs", return_value=[
            self._entry(),
        ]):
            self.assertIsNone(main._spotify_producer_for_coordinator())

    def test_anonymous_ports_match_pipewire_sink_link_blocks(self):
        link_text = "\n".join((
            "fxroute_dsp_sink:playback_FL",
            "  |<- :output_FL",
            "fxroute_dsp_sink:playback_FR",
            "  |<- :output_FR",
        ))
        self.assertTrue(main._contains_link(
            link_text, ":output_FL", "fxroute_dsp_sink:playback_FL"
        ))
        self.assertTrue(main._contains_link(
            link_text, ":output_FR", "fxroute_dsp_sink:playback_FR"
        ))

    def test_no_sink_input_returns_none(self):
        with mock.patch.object(main, "_list_spotify_sink_inputs", return_value=[]):
            self.assertIsNone(main._spotify_producer_for_coordinator())

    def test_resolver_falls_back_to_static_for_non_spotify(self):
        self.assertEqual(
            main._resolve_playback_source_producer_ports("local"),
            ("mpv:output_FL", "mpv:output_FR"),
        )


class MasterLoudnessContractTests(unittest.IsolatedAsyncioTestCase):
    def test_get_output_volume_safe_always_reports_the_master(self):
        manager = SimpleNamespace(
            load_global_extras=lambda: {
                "loudness": {"enabled": True, "params": {"volumeDb": -20.0}}
            }
        )
        with mock.patch.object(main, "dsp_manager", manager), mock.patch.object(
            main, "get_status_volume", return_value=45
        ):
            self.assertEqual(main.get_output_volume_safe(), 45)

    async def test_slider_writes_master_across_loudness_on_and_off(self):
        writes = []
        manager = SimpleNamespace(
            load_global_extras=lambda: {"loudness": {"enabled": True, "params": {"volumeDb": -20.0}}},
            get_active_preset=lambda: "Neutral",
        )
        with mock.patch.object(main, "dsp_manager", manager), mock.patch.object(
            main, "set_output_volume", side_effect=lambda value: writes.append(value) or value
        ):
            for percent in (0, 25, 50, 100):
                result = await main._set_canonical_output_volume(percent)
                self.assertEqual(result, {"volume": percent})
        self.assertEqual(writes, [0, 25, 50, 100])

    async def test_spotify_state_reports_master_not_provider_volume(self):
        data = {"status": "Playing", "volume": 100, "trackId": "spotify:track:x"}
        with mock.patch.object(main, "get_output_volume_safe", return_value=38), mock.patch.object(
            main, "_resolve_playback_owner", return_value="spotify"
        ):
            state = await main.get_spotify_ui_state(data)
        self.assertEqual(state["volume"], 38)
        self.assertEqual(state["source_volume"], 100)

    async def test_qobuz_state_reports_master_not_provider_volume(self):
        async def status():
            return {"status": "Playing", "volume": 100, "trackId": "q:1"}

        provider = SimpleNamespace(status=status)
        with mock.patch.object(main.streaming, "get_provider", return_value=provider), mock.patch.object(
            main, "get_output_volume_safe", return_value=55
        ), mock.patch.object(
            main, "_resolve_playback_owner", return_value="qobuz"
        ), mock.patch.object(
            main, "qobuz_unity_pin_state", {"ok": True}
        ):
            state = await main.get_qobuz_ui_state()
        self.assertEqual(state["volume"], 55)
        self.assertEqual(state["source_volume"], 100)


if __name__ == "__main__":
    unittest.main()
