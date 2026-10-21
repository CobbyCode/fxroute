#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""File PCM positions, exclusive DSP ingress and playback graph recovery."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.runtime import CommandResult, DSPRuntime, DSPRuntimeConfig, mapped_ingress_edges
from playback import source_policy
from playback.player import MPVWrapper

SIDE = {"FL": "FL", "FR": "FR", "FC": "FC", "LFE": "LFE", "SL": "SL", "SR": "SR"}
BACK = {"FL": "FL", "FR": "FR", "FC": "FC", "LFE": "LFE", "BL": "RL", "BR": "RR"}
ALL = SIDE | BACK
MONITORS = {("fxroute_dsp_sink:monitor_FL", "fxroute_dsp:input_1"),
            ("fxroute_dsp_sink:monitor_FR", "fxroute_dsp:input_2")}


class PortTests(unittest.TestCase):
    def test_semantic_positions_do_not_confuse_side_and_back(self):
        for suffixes in (SIDE, BACK, ALL):
            with self.subTest(suffixes=suffixes):
                ports = "\n".join(f"mpv:output_{suffix}" for suffix in reversed(list(suffixes.values())))
                self.assertEqual(source_policy.mpv_port_positions(ports, len(suffixes)), suffixes)

    def test_partial_or_unknown_layout_is_not_ready(self):
        for ports, count in (("mpv:output_FL\nmpv:output_FR", 6),
                             ("mpv:output_AUX0\nmpv:output_AUX1", 2),
                             ("other:output_FL\nother:output_FR", 2),
                             ("mpv:output_FL_extra\nmpv:output_FR", 2)):
            self.assertEqual(source_policy.mpv_port_positions(ports, count), {})

    def test_mono_center_does_not_invent_a_front_pair(self):
        self.assertEqual(source_policy.mpv_port_positions("mpv:output_FC", 1), {"FC": "FC"})


class Graph:
    """In-memory PipeWire boundary; the real runtime owns link mutations."""

    def __init__(self):
        self.links = set(MONITORS)
        self.mutations = []
        self.ports = set()

    async def run(self, args):
        if args == ("pw-link", "-o"):
            return CommandResult(0, "\n".join(sorted(self.ports)))
        if args == ("pw-link", "-l"):
            return CommandResult(0, "\n".join(f"{a} -> {b}" for a, b in sorted(self.links)))
        self.mutations.append(args)
        if args[1] == "-d":
            self.links.discard(tuple(args[2:]))
        else:
            self.links.add(tuple(args[1:]))
        if any(a.startswith("mpv:") and b.startswith("fxroute_dsp:input_") for a, b in self.links):
            assert not self.links & MONITORS, "parallel stereo and MPV ingress"
        return CommandResult(0)

    async def command(self, *args):
        if args == ("-io",):
            return "\n".join(sorted(self.ports))
        return (await self.run(("pw-link", *args))).stdout

    async def connect(self, sources, target):
        await self.run(("pw-link", sources[0], target))


def native(graph, mode="surround"):
    result = DSPRuntime(SimpleNamespace(), command_runner=graph.run)
    result._process = SimpleNamespace(returncode=None)
    result._config = DSPRuntimeConfig(mode, "hardware", 48000, ("playback_FL", "playback_FR"), ())
    return result


class IngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_layout_has_only_its_real_bus_inputs_and_no_stereo_feed(self):
        for suffixes, inputs in ((SIDE, (1, 2, 3, 4, 5, 6)),
                                 (BACK, (1, 2, 3, 4, 7, 8)),
                                 (ALL, (1, 2, 3, 4, 5, 6, 7, 8))):
            with self.subTest(suffixes=suffixes):
                graph = Graph()
                engine = native(graph)
                await engine.set_mapped_ingress(mapped_ingress_edges("mpv", suffixes, port_kind="output"))
                actual = {(a, b) for a, b in graph.links if b.startswith("fxroute_dsp:input_")}
                self.assertEqual({b for _, b in actual}, {f"fxroute_dsp:input_{i}" for i in inputs})
                self.assertFalse(graph.links & MONITORS)
                self.assertEqual(len(actual), len(suffixes))

    async def test_layout_change_removes_old_channels_before_stereo_returns(self):
        graph = Graph()
        engine = native(graph)
        await engine.set_mapped_ingress(mapped_ingress_edges("mpv", ALL, port_kind="output"))
        await engine.set_mapped_ingress(mapped_ingress_edges("mpv", {"FL": "FL", "FR": "FR"}, port_kind="output"))
        self.assertEqual(graph.links, {("mpv:output_FL", "fxroute_dsp:input_1"),
                                       ("mpv:output_FR", "fxroute_dsp:input_2")})
        await engine.set_mapped_ingress(())
        self.assertEqual(graph.links, MONITORS)

    async def test_two_input_engine_does_not_keep_a_parallel_direct_front_pair(self):
        graph = Graph()
        engine = native(graph)
        await engine.set_mapped_ingress(mapped_ingress_edges("mpv", SIDE, port_kind="output"))
        engine._config = DSPRuntimeConfig("stereo", "hardware", 48000, ("playback_FL", "playback_FR"), ())
        await engine._reconcile_ingress_links()
        self.assertEqual(graph.links, MONITORS)

    async def test_capture_ingress_still_retains_the_measurement_stereo_tap(self):
        graph = Graph()
        engine = native(graph)
        await engine.set_mapped_ingress(mapped_ingress_edges("capture", BACK))
        self.assertEqual(graph.links & MONITORS, MONITORS)

    async def test_native_playlist_layout_change_is_seen_and_repaired_by_the_runtime_watch(self):
        graph = Graph()
        engine = native(graph)
        graph.ports = {"mpv:output_FL", "mpv:output_FR"}
        await engine.set_mapped_ingress(mapped_ingress_edges("mpv", {"FL": "FL", "FR": "FR"}, port_kind="output"))
        self.assertTrue(await engine.verify())
        graph.ports = {f"mpv:output_{suffix}" for suffix in ALL.values()}
        self.assertFalse(await engine.verify(), "new channels require ingress reconciliation")
        await engine._reconcile_ingress_links()
        self.assertEqual({b for a, b in graph.links if a.startswith("mpv:")},
                         {f"fxroute_dsp:input_{i}" for i in range(1, 9)})
        self.assertTrue(await engine.verify())

    async def test_runtime_verification_rejects_a_recreated_stereo_monitor_link(self):
        graph = Graph()
        engine = native(graph)
        graph.ports = {f"mpv:output_{suffix}" for suffix in SIDE.values()}
        await engine.set_mapped_ingress(mapped_ingress_edges("mpv", SIDE, port_kind="output"))
        graph.links |= MONITORS
        self.assertFalse(await engine.verify())

    async def test_link_repair_adopts_a_changed_playlist_layout_without_a_rebuild(self):
        graph = Graph()
        engine = native(graph)
        graph.ports = {"mpv:output_FL", "mpv:output_FR"}
        await engine.set_mapped_ingress(mapped_ingress_edges("mpv", {"FL": "FL", "FR": "FR"}, port_kind="output"))
        graph.ports = {f"mpv:output_{suffix}" for suffix in ALL.values()}
        self.assertFalse(await engine.verify(), "new playlist layout needs reconciliation")
        await engine.reclean_direct_dsp_links()
        self.assertTrue(await engine.verify())
        self.assertEqual({target for source, target in graph.links if source.startswith("mpv:")},
                          {f"fxroute_dsp:input_{index}" for index in range(1, 9)})

    async def test_output_commit_does_not_veto_a_playlist_ingress_change(self):
        for output_missing in (False, True):
            with self.subTest(output_missing=output_missing):
                graph = Graph()
                engine = native(graph)
                graph.ports = {'mpv:output_FL', 'mpv:output_FR'}
                await engine.set_mapped_ingress(mapped_ingress_edges(
                    'mpv', {'FL': 'FL', 'FR': 'FR'}, port_kind='output'))
                await engine._reconcile_output_links(engine._config)
                completed = []

                @asynccontextmanager
                async def scope(*keys):
                    yield
                    completed.append(True)

                async def commit(*args, **kwargs):
                    graph.ports = {f'mpv:output_{suffix}' for suffix in ALL.values()}
                    if output_missing:
                        graph.links.discard(('fxroute_dsp:output_1', 'hardware:playback_FL'))

                engine.configure_output_level_tracking(scope)
                with patch.object(engine, '_apply_sync', side_effect=commit):
                    if output_missing:
                        with self.assertRaisesRegex(RuntimeError, 'output links did not verify'):
                            await engine._run_sync(engine._config, '')
                        self.assertFalse(completed)
                    else:
                        await engine._run_sync(engine._config, '')
                        self.assertEqual(completed, [True])
                        self.assertTrue(engine.snapshot()['active'])
                        self.assertFalse(await engine.verify(), 'Full verification must still see ingress drift')


class PlayerTests(unittest.TestCase):
    def test_supported_layouts_keep_decoder_positions(self):
        player = MPVWrapper()
        with patch.object(player, 'set_property') as write:
            player.set_surround_channels(True)
            write.reset_mock()
            for layout in ('mono', 'stereo', 'quad', '4.1(alsa)', '5.1', '5.1(side)',
                           '7.1', '7.1(alsa)', 'fl-fr-fc-lfe-sl-sr'):
                params = {'channels': layout}
                player._handle_event({'event': 'property-change', 'name': 'audio-out-params', 'data': params})
                self.assertEqual(player.state['audio_output_params'], params)
            write.assert_not_called()

    def test_unsupported_layouts_downmix_but_next_playlist_track_can_use_surround(self):
        for layout in ('6.1', '6.1(back)', '7.1(wide)', 'fl-fr-bc', 'fl-fr-fwl-fwr'):
            with self.subTest(layout=layout):
                player = MPVWrapper()
                calls = []
                with patch.object(player, 'set_property', side_effect=lambda *args: calls.append(args)):
                    player.set_surround_channels(True)
                    player._handle_event({'event': 'property-change', 'name': 'audio-out-params',
                                          'data': {'channels': layout, 'channel-count': 7}})
                    self.assertEqual(calls[-1], ('audio-channels', 'stereo'))
                    self.assertIsNone(player.state['audio_output_params'], 'Wait for actual downmix ports')
                    player._handle_event({'event': 'property-change', 'name': 'audio-out-params',
                                          'data': {'channels': 'stereo', 'channel-count': 2}})
                    self.assertEqual(player.state['audio_output_params']['channel-count'], 2)
                    player._handle_event({'event': 'start-file', 'playlist_entry_id': 2})
                    self.assertEqual(calls[-1], ('audio-channels', 'auto'))
                    player._handle_event({'event': 'property-change', 'name': 'audio-out-params',
                                          'data': {'channels': '5.1(side)', 'channel-count': 6}})
                    self.assertEqual(len(calls), 3)

    def test_channel_mode_changes_only_when_needed(self):
        player = MPVWrapper()
        calls = []
        with patch.object(player, "set_property", side_effect=lambda *a: calls.append(a)):
            player.set_surround_channels(True)
            player.set_surround_channels(True)
            player.set_surround_channels(False)
        self.assertEqual(calls, [("audio-channels", "auto"), ("audio-channels", "stereo")])

    def test_output_channel_facts_are_observed_and_cleared_on_stop(self):
        player = MPVWrapper()
        params = {"channel-count": 6, "channels": "5.1(side)", "samplerate": 48000, "format": "s16"}
        player._handle_event({"event": "property-change", "name": "audio-out-params", "data": params})
        self.assertEqual(player.state.get("audio_output_params"), params)
        with patch.object(player, "_send_command", return_value={}):
            player.stop_playback()
        self.assertIsNone(player.state.get("audio_output_params"))


class TransitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_readiness_checks_the_complete_decoded_layout(self):
        from playback import media_readiness
        io = "mpv:output_FL\nmpv:output_FR\nfxroute_dsp_sink:playback_FL\nfxroute_dsp_sink:playback_FR"
        with patch.object(media_readiness.pw_link, "run_pw_link_command", AsyncMock(return_value=io)):
            self.assertFalse(await media_readiness.mpv_source_ports_present(channel_count=6))
        io += "\nmpv:output_FC\nmpv:output_LFE\nmpv:output_SL\nmpv:output_SR"
        with patch.object(media_readiness.pw_link, "run_pw_link_command", AsyncMock(return_value=io)):
            self.assertTrue(await media_readiness.mpv_source_ports_present(channel_count=6))

    async def test_mode_transport_restore_reconfigures_channels_before_unpausing(self):
        import main
        from playback.runtime import FxrouteTransitionRuntime
        from playback.transition import TransitionRequest
        for surround in (True, False):
            with self.subTest(surround=surround):
                calls = []
                player = SimpleNamespace(state={"playing": True, "paused": True, "current_file": "track.flac"})
                def channels(value):
                    calls.append(("channels", value))
                    return True
                def pause(value):
                    calls.append(("pause", value))
                    player.state.update(paused=value, playing=not value)
                player.set_surround_channels = channels
                player.set_pause = pause
                engine = SimpleNamespace(surround_engine=surround)
                async def ensure():
                    calls.append(("links",))
                    return True
                deps = replace(main.make_playback_runtime_deps(), player=lambda: player,
                               dsp_runtime=lambda: engine, player_is_running=lambda: True,
                               ensure_mpv_to_dsp_links=ensure)
                adapter = FxrouteTransitionRuntime(deps)
                await adapter.restore_output_mode_transport(
                    TransitionRequest(operation="output-mode-switch", source="local"),
                    {"player": {"current_file": "track.flac", "playing": True, "paused": False}}, "test")
                self.assertEqual(calls, [("channels", surround), ("links",), ("pause", False)])

    async def test_idle_mpv_releases_the_direct_surround_ingress(self):
        import main
        dsp = SimpleNamespace(
            snapshot=lambda: {"mapped_ingress": [
                ["mpv:output_FL", "fxroute_dsp:input_1"],
                ["mpv:output_FR", "fxroute_dsp:input_2"]]},
            release_mapped_ingress=AsyncMock(),
        )
        with patch.object(main.runtime, "dsp_runtime", dsp):
            await main._release_idle_mpv_ingress({"current_file": None, "idle_active": True})
        dsp.release_mapped_ingress.assert_awaited_once_with([
            ["mpv:output_FL", "fxroute_dsp:input_1"],
            ["mpv:output_FR", "fxroute_dsp:input_2"]])
        dsp.release_mapped_ingress.reset_mock()
        for state in ({"current_file": "/music/a.flac", "idle_active": False},
                      {"current_file": None, "idle_active": False}):
            with self.subTest(state=state):
                with patch.object(main.runtime, "dsp_runtime", dsp):
                    await main._release_idle_mpv_ingress(state)
                dsp.release_mapped_ingress.assert_not_awaited()

    async def test_external_source_claim_releases_only_the_mpv_ingress(self):
        import main
        from playback.runtime import FxrouteTransitionRuntime
        from playback.transition import TransitionRequest
        graph = Graph()
        engine = native(graph)
        await engine.set_mapped_ingress(mapped_ingress_edges("mpv", SIDE, port_kind="output"))
        deps = replace(main.make_playback_runtime_deps(), dsp_runtime=lambda: engine)
        await FxrouteTransitionRuntime(deps).prepare_target_source(TransitionRequest(source="spotify", operation="spotify-play"))
        self.assertEqual(graph.links, MONITORS)
        capture = mapped_ingress_edges("capture", BACK)
        await engine.set_mapped_ingress(capture)
        await FxrouteTransitionRuntime(deps).prepare_target_source(TransitionRequest(source="spotify", operation="spotify-play"))
        self.assertEqual(engine._mapped_ingress, capture)


class OrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_selection_commit_survives_a_shrinking_playlist_layout(self):
        import main
        import os
        import tempfile
        from audio.samplerate import _load_audio_output_selection
        await self.engine.set_mapped_ingress(mapped_ingress_edges('mpv', SIDE, port_kind='output'))
        await self.engine._reconcile_output_links(self.engine._config)
        overview = {**self.overview, 'selected_output': {'key': 'hardware'},
                    'default_output': {'key': 'hardware'}}
        async def sync(*args, **kwargs):
            self.suffixes = {'FL': 'FL', 'FR': 'FR'}
            self.player.state['audio_output_params'] = {'channel-count': 2}
            self.refresh_ports()
            self.graph.links = {edge for edge in self.graph.links
                                if not edge[0].startswith('mpv:') or edge[0] in ('mpv:output_FL', 'mpv:output_FR')}
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'XDG_CONFIG_HOME': directory}), \
                patch.object(main.runtime, 'dsp_runtime', self.engine), \
                patch.object(main, 'get_audio_output_overview', return_value=overview), \
                patch.object(main.samplerate, 'prepare_audio_output_selection', return_value={'name': 'hardware'}), \
                patch.object(main.samplerate, '_set_default_sink'), \
                patch.object(main.dsp_orchestrator, 'sync_runtime', side_effect=sync), \
                patch.object(main.playback_orchestration, 'configured', return_value=self.orchestrator):
            await main._apply_audio_output_selection('hardware', seed_routing=False)
            self.assertEqual(_load_audio_output_selection()['selected_key'], 'hardware')
        diagnosis = await self.orchestrator.playback_graph_diagnosis(overview)
        self.assertFalse(diagnosis['links_complete'], 'Ingress drift remains visible to the watch')

    async def asyncSetUp(self):
        import main
        from playback.orchestration import PlaybackOrchestrator
        self.graph = Graph()
        self.engine = native(self.graph)
        self.suffixes = dict(SIDE)
        self.player = SimpleNamespace(state={"audio_output_params": {"channel-count": 6}})
        self.overview = {"output_mode": {"mode": "surround", "effective_output_key": "hardware",
                                        "planned_routes": [[1, "playback_FL"], [2, "playback_FR"]]}}
        deps = replace(main._make_playback_orchestration_deps(),
                       get_runtime_player=lambda: self.player, get_dsp_runtime=lambda: self.engine,
                       get_dsp_snapshot=lambda: self.engine.snapshot(),
                       helper_argument_sample_rate=lambda _: 48000,
                       get_audio_output_overview=lambda: self.overview,
                       run_pw_link_command=self.graph.command, connect_ports=self.graph.connect,
                       mpv_source_ports_present=None)
        self.orchestrator = PlaybackOrchestrator(deps)
        self.refresh_ports()
        self.graph.links |= {("fxroute_dsp:output_1", "hardware:playback_FL"),
                             ("fxroute_dsp:output_2", "hardware:playback_FR")}

    def refresh_ports(self):
        self.graph.ports = {f"mpv:output_{s}" for s in self.suffixes.values()}
        self.graph.ports |= {f"fxroute_dsp:input_{i}" for i in range(1, 9)}
        self.graph.ports |= {"fxroute_dsp:output_1", "fxroute_dsp:output_2",
                             "hardware:playback_FL", "hardware:playback_FR",
                             "fxroute_dsp_sink:playback_FL", "fxroute_dsp_sink:playback_FR"}

    async def test_ensure_registers_all_six_positions_and_repairs_a_missing_rear(self):
        self.assertTrue(await self.orchestrator.ensure_mpv_to_dsp_links(timeout_ms=100))
        self.assertEqual(set(self.engine._mapped_ingress), {
            ("mpv:output_FL", "fxroute_dsp:input_1"), ("mpv:output_FR", "fxroute_dsp:input_2"),
            ("mpv:output_FC", "fxroute_dsp:input_3"), ("mpv:output_LFE", "fxroute_dsp:input_4"),
            ("mpv:output_SL", "fxroute_dsp:input_5"), ("mpv:output_SR", "fxroute_dsp:input_6")})
        self.graph.links.remove(("mpv:output_SR", "fxroute_dsp:input_6"))
        diagnosis = await self.orchestrator.playback_graph_diagnosis(source="local", require_source=True)
        self.assertFalse(diagnosis["source_links_complete"])
        self.assertTrue(await self.orchestrator.ensure_mpv_to_dsp_links(timeout_ms=100))
        diagnosis = await self.orchestrator.playback_graph_diagnosis(source="local", require_source=True)
        self.assertTrue(diagnosis["links_complete"], diagnosis)
        self.assertEqual(len(diagnosis["source_links"]), 6)

    async def test_diagnosis_rejects_a_parallel_stereo_monitor_feed(self):
        await self.engine.set_mapped_ingress(mapped_ingress_edges("mpv", SIDE, port_kind="output"))
        self.graph.links |= MONITORS
        diagnosis = await self.orchestrator.playback_graph_diagnosis(source="local", require_source=True)
        self.assertFalse(diagnosis["links_complete"])

    async def test_incomplete_six_channel_ports_do_not_pass_as_stereo(self):
        self.suffixes = {"FL": "FL", "FR": "FR"}
        self.refresh_ports()
        self.assertFalse(await self.orchestrator.ensure_mpv_to_dsp_links(timeout_ms=0))
        self.assertEqual(self.graph.mutations, [])


if __name__ == "__main__":
    unittest.main()
