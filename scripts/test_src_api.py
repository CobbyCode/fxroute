#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""SRC application is serialized, rejects busy sessions and rolls back failures."""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio import src_policy


class SrcApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("audio.src_api"), "Common SRC API is missing")
        from audio.src_api import SrcController
        self.Controller = SrcController
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        runtime = root / "runtime"
        runtime.mkdir()
        (runtime / "libspa-fxroute-src.so").touch()
        (runtime / "sox").touch(mode=0o755)
        (runtime / "manifest.json").write_text('{"sox_version":"16.2.1"}')
        spa = root / "system-spa"
        spa.mkdir()
        for p in (patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root)}),
                  patch.object(src_policy, "plugin_locations", return_value=[spa / "libspa-fxroute-src.so"]),
                  patch.object(src_policy, "RUNTIME", runtime), patch.object(src_policy, "nodes", side_effect=lambda:[{
                      "name":"test-hardware", "mode":src_policy.load_policy()["mode"], "state":"running", "error":0}])):
            p.start()
            self.addCleanup(p.stop)
        src_policy.install_plugin(runtime)
        src_policy.configure()
        import main
        self.real_provider_release = {name: getattr(main.media_readiness, f"wait_for_pipewire_{name}_release")
                                      for name in ("spotify", "qobuz")}
        for p in (patch.object(main, "get_spotify_ui_state", AsyncMock(return_value={"status": "Stopped"})),
                  patch.object(main, "get_qobuz_ui_state", AsyncMock(return_value={"status": "Paused"})),
                  patch.object(main, "_measurement_session_owns_live_rate", return_value=False),
                  patch.object(main, "measurement_sr_session", None),
                  patch.object(main.runtime, "stdin_input", None),
                  patch.object(main.playback_state, "current_track_info", None),
                  patch.object(main.playback_state, "current_playback_owner", None),
                  patch.object(main.radio_reconnect, "reset", AsyncMock()),
                  patch.object(main.media_readiness, "wait_for_pipewire_spotify_release", AsyncMock(return_value=True)),
                  patch.object(main.media_readiness, "wait_for_pipewire_qobuz_release", AsyncMock(return_value=True)),
                  patch.object(main.playback_queue.queue, "reset"),
                  patch.object(main.playback_queue.queue, "reset_mpv_loop_state")):
            p.start()
            self.addCleanup(p.stop)

    async def test_settings_selection_applies_each_mode_from_playing_and_paused_playback(self):
        import main
        from fastapi.testclient import TestClient
        for mode in ("low", "standard", "high"):
            for paused in (False, True):
                with self.subTest(mode=mode, paused=paused):
                    events = []
                    player_state = {"playing": not paused, "paused": paused,
                                    "current_file": "/music/current.flac", "file_loaded": True}
                    def stop_playback():
                        events.append("stop-playback")
                        player_state.update(playing=False, paused=False, current_file=None, file_loaded=False)
                    def stop_player():
                        events.append("stop-player")
                    def start_player():
                        self.assertIsNone(player_state["current_file"])
                        events.append("start-player")
                    async def stop_dsp():
                        self.assertFalse(player_state["playing"])
                        self.assertFalse(player_state["paused"])
                        self.assertIsNone(player_state["current_file"])
                        events.append("stop-dsp")
                    async def restart_services(*args, **kwargs):
                        self.assertEqual(events[:3], ["stop-playback", "stop-dsp", "stop-player"])
                        self.assertEqual(src_policy.load_policy()["mode"], mode)
                        events.append("restart-services")
                        return SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))
                    player = SimpleNamespace(state=player_state, is_running=lambda: True,
                        stop_playback=stop_playback, stop=stop_player, start=start_player)
                    dsp = SimpleNamespace(stop=stop_dsp, verify=AsyncMock(return_value=True))
                    overview = {"selected_output": {"key": "test", "target_name": "test"},
                                "outputs": [{"key": "test"}]}
                    with (patch.object(main.runtime, "player_instance", player),
                          patch.object(main.runtime, "dsp_runtime", dsp),
                          patch.object(main.playback_state, "current_track_info", {"source": "local", "url": "/music/current.flac"}),
                          patch.object(main.playback_state, "current_playback_owner", "local"),
                          patch.object(main, "_src_graph_lock", asyncio.Lock),
                          patch.object(src_policy, "activate_environment"),
                          patch.object(main.asyncio, "create_subprocess_exec", side_effect=restart_services),
                          patch.object(main, "get_audio_output_overview", return_value=overview),
                          patch.object(main.samplerate, "get_current_pipewire_force_rate", return_value=48000),
                          patch.object(main.samplerate, "set_pipewire_force_rate") as pin,
                          patch.object(main.samplerate, "prepare_audio_output_selection", return_value=overview["selected_output"]),
                          patch.object(main.samplerate, "set_audio_output_selection", return_value=overview),
                          patch.object(main.dsp_orchestrator, "sync_runtime", AsyncMock()),
                          patch.object(main, "_mark_player_state_authoritative")):
                        response = await asyncio.to_thread(TestClient(main.app).post,
                            "/api/audio/src", json={"mode": mode}, headers={"Accept-Encoding": "identity"})
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.json()["mode"], mode)
                        self.assertTrue(response.json()["available"])
                        self.assertFalse(response.json()["apply_requires_stopped_playback"])
                        self.assertIsNone(main.playback_state.current_track_info)
                        self.assertIsNone(main.playback_state.current_playback_owner)
                        self.assertEqual(events[-1], "start-player")
                        pin.assert_called_once_with(48000)

    async def test_src_guard_allows_ordinary_external_renderers(self):
        import main
        with (patch.object(main.runtime, "dsp_runtime", object()),
              patch.object(main, "get_spotify_ui_state", AsyncMock(return_value={"available": True, "status": "Playing"})),
              patch.object(main, "get_qobuz_ui_state", AsyncMock(return_value={"available": True, "status": "Playing"}))):
            await main._guard_src_change()

    async def test_src_guard_retains_measurement_and_input_session_protection(self):
        import main
        with patch.object(main.runtime, "dsp_runtime", object()):
            with patch.object(main, "_measurement_session_owns_live_rate", return_value=True):
                with self.assertRaises(main.HTTPException) as error:
                    await main._guard_src_change()
                self.assertEqual(error.exception.status_code, 423)
            for mode, session in (("external-input", None), ("bluetooth-input", None),
                                  ("stdin-input", None), ("app-playback", "active-stdin")):
                with (patch.object(main.samplerate, "_load_audio_source_selection", return_value={"mode": mode}),
                      patch.object(main.runtime, "stdin_input", SimpleNamespace(snapshot=lambda: {"session_id": session}))):
                    with self.assertRaises(main.HTTPException) as error:
                        await main._guard_src_change()
                    self.assertEqual(error.exception.status_code, 409)

    async def test_unreleased_provider_prevents_audio_graph_teardown(self):
        import main
        for provider in ("spotify", "qobuz"):
            with self.subTest(provider=provider):
                dsp = SimpleNamespace(stop=AsyncMock(), verify=AsyncMock(return_value=True))
                process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))
                overview = {"selected_output": {"key": "test", "target_name": "test"}, "outputs": [{"key": "test"}]}
                with (patch.object(main.runtime, "dsp_runtime", dsp),
                      patch.object(main.runtime, "player_instance", None),
                      patch.object(main, f"get_{provider}_ui_state", AsyncMock(return_value={"available": True, "status": "Playing"})),
                      patch.object(main, f"{provider}_pause", AsyncMock(return_value={"available": True, "status": "Paused"})),
                      patch.object(main.media_readiness, f"wait_for_pipewire_{provider}_release", AsyncMock(return_value=False)),
                      patch.object(main, "_commit_spotify_state_direct"),
                      patch.object(src_policy, "activate_environment"),
                      patch.object(main.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as restart,
                      patch.object(main, "get_audio_output_overview", return_value=overview),
                      patch.object(main.samplerate, "set_pipewire_force_rate"),
                      patch.object(main.samplerate, "prepare_audio_output_selection", return_value=overview["selected_output"]),
                      patch.object(main.samplerate, "set_audio_output_selection", return_value=overview),
                      patch.object(main.dsp_orchestrator, "sync_runtime", AsyncMock())):
                    with self.assertRaisesRegex(RuntimeError, "did not quiesce"):
                        await main._restart_src_graph({"force_rate": 48000, "output_key": "test"})
                    dsp.stop.assert_not_awaited()
                    restart.assert_not_awaited()

    async def test_src_holds_playback_attempt_ownership_until_graph_lock_exits(self):
        import main
        with (patch.object(main, "_source_transition_lock", return_value=asyncio.Lock()),
              patch.object(main, "_coordinator_lock_for_source_pause", return_value=asyncio.Lock()),
              patch.object(main, "_dsp_mutation_lock", return_value=asyncio.Lock())):
            self.assertFalse(main._playback_transition_is_active())
            async with main._src_graph_lock():
                self.assertTrue(main._playback_transition_is_active())
            self.assertFalse(main._playback_transition_is_active())

    async def test_failed_provider_release_is_checked_again_on_rollback(self):
        import main
        for provider in ("spotify", "qobuz"):
            with self.subTest(provider=provider):
                transport = {"available": True, "status": "Playing"}
                async def pause():
                    transport["status"] = "Paused"
                    return dict(transport)
                dsp = SimpleNamespace(stop=AsyncMock(), verify=AsyncMock(return_value=True))
                process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))
                overview = {"selected_output": {"key": "test", "target_name": "test"}, "outputs": [{"key": "test"}]}
                with (patch.object(main.runtime, "dsp_runtime", dsp),
                      patch.object(main.runtime, "player_instance", None),
                      patch.object(main, f"get_{provider}_ui_state", AsyncMock(side_effect=lambda: dict(transport))),
                      patch.object(main, f"{provider}_pause", side_effect=pause),
                      patch.object(main.media_readiness, f"wait_for_pipewire_{provider}_release", AsyncMock(return_value=False)) as release,
                      patch.object(main, "_commit_spotify_state_direct"),
                      patch.object(src_policy, "activate_environment"),
                      patch.object(main.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as restart,
                      patch.object(main, "get_audio_output_overview", return_value=overview),
                      patch.object(main.samplerate, "set_pipewire_force_rate"),
                      patch.object(main.samplerate, "prepare_audio_output_selection", return_value=overview["selected_output"]),
                      patch.object(main.samplerate, "set_audio_output_selection", return_value=overview),
                      patch.object(main.dsp_orchestrator, "sync_runtime", AsyncMock())):
                    controller = self.Controller(AsyncMock(), main._restart_src_graph, asyncio.Lock(),
                        snapshot=AsyncMock(return_value={"force_rate": 48000, "output_key": "test"}))
                    with self.assertRaises(RuntimeError):
                        await controller.apply("high")
                    dsp.stop.assert_not_awaited()
                    restart.assert_not_awaited()
                    self.assertEqual(release.await_count, 2)
                    self.assertEqual(src_policy.load_policy()["mode"], "standard")

    async def test_src_does_not_treat_failed_stream_inspection_as_quiescence(self):
        import main
        import subprocess
        from audio import sink_inputs
        for provider in ("spotify", "qobuz"):
            for probe in (subprocess.TimeoutExpired("pactl", 1.5),
                          SimpleNamespace(returncode=1, stdout="", stderr="Connection failure")):
                with self.subTest(provider=provider, probe=probe):
                    observations = [SimpleNamespace(returncode=0, stdout="")] if provider == "qobuz" else []
                    observations.append(probe)
                    with (patch.object(main.runtime, "player_instance", None),
                          patch.object(main.media_readiness, "wait_for_pipewire_spotify_release", self.real_provider_release["spotify"]),
                          patch.object(main.media_readiness, "wait_for_pipewire_qobuz_release", self.real_provider_release["qobuz"]),
                          patch.object(sink_inputs.subprocess, "run", side_effect=observations)):
                        with self.assertRaisesRegex(RuntimeError, "Cannot inspect"):
                            await main._stop_app_playback_for_src()


    async def test_native_queue_cleanup_runs_off_the_event_loop(self):
        import main
        import threading
        from playback.queue import PlaybackQueue
        loop_thread = threading.get_ident()
        commands = []
        def ipc(*args):
            commands.append((args, threading.get_ident()))
        player = SimpleNamespace(state={"playing": True, "paused": False}, _running=True,
            is_running=lambda: True, stop_playback=lambda: None, clear_playlist=lambda: ipc("clear"),
            set_loop_playlist=lambda value: ipc("loop-playlist", value),
            set_loop_file=lambda value: ipc("loop-file", value),
            set_shuffle=lambda value: ipc("shuffle", value))
        queue = PlaybackQueue(SimpleNamespace(player=lambda: player, player_is_running=lambda: True))
        queue.mode = "native_mpv"
        queue.tracks = [{"id": "one"}, {"id": "two"}]
        with (patch.object(main.runtime, "player_instance", player),
              patch.object(main.playback_queue, "queue", queue),
              patch.object(main, "_mark_player_state_authoritative"),
              patch.object(main.manager, "broadcast", AsyncMock())):
            await main._stop_app_playback_for_src()
        self.assertEqual(queue.tracks, [])
        self.assertEqual(queue.mode, "app_replace")
        self.assertTrue(commands)
        self.assertTrue(all(thread != loop_thread for _, thread in commands), commands)

    async def test_settings_request_waits_for_transient_graph_contention_before_applying(self):
        import main
        from contextlib import asynccontextmanager
        from fastapi.testclient import TestClient
        attempts = []
        @asynccontextmanager
        async def graph_lock():
            attempts.append(src_policy.load_policy()["mode"])
            if len(attempts) == 1:
                raise main.HTTPException(409, "Audio configuration is busy; retry SRC after it finishes")
            yield
        with (patch.object(main, "_src_graph_lock", graph_lock),
              patch.object(main, "_guard_src_change", AsyncMock()),
              patch.object(main, "_snapshot_src_graph", AsyncMock(return_value={"force_rate": 48000})),
              patch.object(main, "_restart_src_graph", AsyncMock()) as restart):
            response = await asyncio.to_thread(TestClient(main.app).post, "/api/audio/src", json={"mode": "high"},
                                              headers={"Accept-Encoding": "identity"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["mode"], "high")
        self.assertEqual(src_policy.load_policy()["mode"], "high")
        self.assertEqual(attempts, ["standard", "standard"])
        restart.assert_awaited_once()

    async def test_persistent_graph_contention_is_bounded_without_policy_mutation(self):
        import main
        from contextlib import asynccontextmanager
        from fastapi.testclient import TestClient
        @asynccontextmanager
        async def graph_lock():
            raise main.HTTPException(409, "Audio configuration is busy; retry SRC after it finishes")
            yield
        with (patch.object(main, "_src_graph_lock", graph_lock),
              patch.object(main.asyncio, "sleep", AsyncMock()) as wait,
              patch.object(main, "_restart_src_graph", AsyncMock()) as restart):
            response = await asyncio.to_thread(TestClient(main.app).post, "/api/audio/src", json={"mode": "high"},
                                              headers={"Accept-Encoding": "identity"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(src_policy.load_policy()["mode"], "standard")
        self.assertEqual(wait.await_count, 30)
        restart.assert_not_awaited()

    async def test_input_ownership_conflict_is_not_retried(self):
        import main
        from fastapi.testclient import TestClient
        with (patch.object(main, "_src_graph_lock", asyncio.Lock),
              patch.object(main, "_guard_src_change", AsyncMock(side_effect=main.HTTPException(409, "Input is active"))) as guard,
              patch.object(main, "_restart_src_graph", AsyncMock()) as restart):
            response = await asyncio.to_thread(TestClient(main.app).post, "/api/audio/src", json={"mode": "high"},
                                              headers={"Accept-Encoding": "identity"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(src_policy.load_policy()["mode"], "standard")
        guard.assert_awaited_once()
        restart.assert_not_awaited()

    async def test_rejection_does_not_persist_or_restart(self):
        async def busy():
            raise RuntimeError("Stop playback before changing SRC")
        async def restart():
            self.fail("Rejected mode change restarted audio")
        controller = self.Controller(busy, restart, asyncio.Lock())
        with self.assertRaises(ValueError):
            await controller.apply("invalid")
        with self.assertRaises(RuntimeError):
            await controller.apply("high")
        self.assertEqual(src_policy.load_policy(), {"mode": "standard"})

    async def test_applied_mode_is_persisted_before_adapter_construction(self):
        async def idle():
            return None
        async def restart():
            self.assertEqual(src_policy.load_policy(), {"mode": "high"})
        result = await self.Controller(idle, restart, asyncio.Lock()).apply("high")
        self.assertEqual(result["mode"], "high")
        self.assertTrue(result["available"])

    async def test_failed_apply_rebuilds_previous_policy(self):
        modes = []
        async def idle():
            return None
        async def restart():
            modes.append(src_policy.load_policy()["mode"])
            if len(modes) == 1:
                raise RuntimeError("new graph failed")
        with self.assertRaisesRegex(RuntimeError, "new graph failed"):
            await self.Controller(idle, restart, asyncio.Lock()).apply("high")
        self.assertEqual(modes, ["high", "standard"])
        self.assertEqual(src_policy.load_policy(), {"mode": "standard"})

    async def test_cancelled_request_finishes_apply_before_releasing_lock(self):
        entered = asyncio.Event()
        finish = asyncio.Event()
        async def idle():
            return None
        async def restart():
            entered.set()
            await finish.wait()
        lock = asyncio.Lock()
        task = asyncio.create_task(self.Controller(idle, restart, lock).apply("high"))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertTrue(lock.locked())
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(lock.locked())
        self.assertEqual(src_policy.load_policy(), {"mode": "high"})

    async def test_audio_restart_preserves_the_independent_fixed_rate(self):
        import main
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        metadata = {"force_rate": 48000}
        async def communicate():
            metadata["force_rate"] = 0
            return b"", b""
        process = SimpleNamespace(returncode=0, communicate=communicate)
        def set_rate(rate):
            metadata["force_rate"] = rate
        overview = {"selected_output": {"key":"test","target_name": "test"}, "outputs":[{"key":"test"}], "available": True}
        dsp = SimpleNamespace(stop=AsyncMock(), verify=AsyncMock(return_value=True))
        with (patch.object(main.runtime, "dsp_runtime", dsp), patch.object(main.runtime, "player_instance", None),
              patch.object(src_policy, "activate_environment"),
              patch.object(main.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)),
              patch.object(main, "get_audio_output_overview", return_value=overview),
              patch.object(main.samplerate, "get_current_pipewire_force_rate", return_value=48000),
              patch.object(main.samplerate, "set_pipewire_force_rate", side_effect=set_rate),
              patch.object(main.samplerate, "prepare_audio_output_selection", return_value=overview["selected_output"]),
              patch.object(main.samplerate, "set_audio_output_selection", return_value=overview),
              patch.object(main.dsp_orchestrator, "sync_runtime", AsyncMock())):
            await main._restart_src_graph()
        self.assertEqual(metadata["force_rate"], 48000, "SRC change discarded the independent rate policy")

    async def test_rollback_reuses_the_original_graph_snapshot(self):
        from unittest.mock import AsyncMock
        snapshot = AsyncMock(return_value={"force_rate":48000})
        seen = []
        async def idle():
            pass
        async def restart(state):
            seen.append(state)
            if len(seen)==1:
                raise RuntimeError("metadata transient failure")
        with self.assertRaisesRegex(RuntimeError,"metadata transient failure"):
            await self.Controller(idle,restart,asyncio.Lock(),snapshot=snapshot).apply("high")
        self.assertEqual(seen,[{"force_rate":48000},{"force_rate":48000}])
        snapshot.assert_awaited_once()

    async def test_src_rejects_a_competing_preset_without_waiting_on_its_locks(self):
        import main
        from types import SimpleNamespace
        dsp = asyncio.Lock()
        measured = asyncio.Lock()
        preset_acquired = asyncio.Event()
        preset_continue = asyncio.Event()
        async def preset():
            async with dsp:
                preset_acquired.set()
                await preset_continue.wait()
                async with measured:
                    pass
        async def change():
            with self.assertRaises(main.HTTPException) as error:
                async with main._src_graph_lock():
                    self.fail("SRC acquired a graph undergoing preset mutation")
            self.assertEqual(error.exception.status_code,409)
        with (patch.object(main,"_dsp_mutation_lock",return_value=dsp),
              patch.object(main,"_coordinator_lock_for_source_pause",return_value=asyncio.Lock()),
              patch.object(main,"_source_transition_lock",return_value=asyncio.Lock()),
              patch.object(main,"measurement_sr_session",SimpleNamespace(lock=measured))):
            a=asyncio.create_task(preset())
            await preset_acquired.wait()
            b=asyncio.create_task(change())
            await asyncio.sleep(0)
            preset_continue.set()
            await asyncio.wait_for(asyncio.gather(a,b),1)

    async def test_src_rejects_measurement_entry_without_holding_the_coordinator(self):
        import main
        from measurement import session as measurement
        from types import SimpleNamespace
        entered = asyncio.Event()
        proceed = asyncio.Event()
        coordinator = asyncio.Lock()
        session = measurement.MeasurementSampleRateSession()
        async def transition(*args, **kwargs):
            entered.set()
            await proceed.wait()
            async with coordinator:
                return SimpleNamespace(generation=1,committed=True,snapshot={"original_force_rate":0})
        services = SimpleNamespace(get_samplerate_status=lambda:{"policy":{"mode":"auto"}},
            coordinator_current_playback_context=lambda:asyncio.sleep(0,result={}),
            run_coordinated_transition=transition,measurement_session_snapshot=lambda:{})
        with (patch.object(measurement,"_measurement_services",return_value=services),
              patch.object(measurement,"_capture_playback_state_before_measurement"),
              patch.object(main,"measurement_sr_session",session),
              patch.object(main,"_source_transition_lock",return_value=asyncio.Lock()),
              patch.object(main,"_coordinator_lock_for_source_pause",return_value=coordinator),
              patch.object(main,"_dsp_mutation_lock",return_value=asyncio.Lock())):
            entry=asyncio.create_task(session.start(48000))
            await asyncio.wait_for(entered.wait(),1)
            try:
                with self.assertRaises(main.HTTPException) as error:
                    async with asyncio.timeout(1):
                        async with main._src_graph_lock():
                            self.fail("SRC entered during measurement registration")
                self.assertEqual(error.exception.status_code,423)
            finally:
                proceed.set()
                await asyncio.wait_for(entry,1)

    async def test_src_does_not_accept_an_unrelated_output_during_restart(self):
        import main
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        original="alsa_output.usb-Original.stereo"
        wrong={"selected_output":{"key":"alsa_output.pci-Onboard.stereo","target_name":"alsa_output.pci-Onboard.stereo"},"outputs":[]}
        dsp=SimpleNamespace(stop=AsyncMock(),verify=AsyncMock(return_value=True))
        process=SimpleNamespace(returncode=0,communicate=AsyncMock(return_value=(b"",b"")))
        with (patch.object(main.runtime,"dsp_runtime",dsp),patch.object(main.runtime,"player_instance",None),
              patch.object(src_policy,"activate_environment"),
              patch.object(main.asyncio,"create_subprocess_exec",AsyncMock(return_value=process)),
              patch.object(main.asyncio,"sleep",AsyncMock()),
              patch.object(main,"get_audio_output_overview",return_value=wrong),
              patch.object(main.samplerate,"set_pipewire_force_rate"),
              patch.object(main.samplerate,"apply_persisted_audio_output_selection",return_value=None),
              patch.object(main.dsp_orchestrator,"sync_runtime",AsyncMock()) as sync):
            with self.assertRaisesRegex(RuntimeError,"output"):
                await main._restart_src_graph({"force_rate":48000,"output_key":original})
            sync.assert_not_awaited()

    async def test_src_waits_for_the_original_hardware_or_its_unique_successor(self):
        import main
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        original="alsa_output.usb-Original.stereo"
        wrong="alsa_output.pci-Onboard.stereo"
        for returned in (original,"alsa_output.usb-Original.pro-output-0"):
            with self.subTest(returned=returned):
                state={"force_rate":0,"selected":wrong,"probes":0,"rebuilt":False,"selections":0}
                def overview():
                    state["probes"]+=1
                    outputs=[{"key":wrong}]
                    if state["probes"]>=3:
                        outputs.append({"key":returned})
                    return {"selected_output":{"key":state["selected"],"target_name":state["selected"]},"outputs":outputs}
                def prepare(key):
                    # Listed before it is selectable: polling must stay read-only.
                    self.assertEqual(key,returned)
                    if state["probes"]<5:
                        raise ValueError(f"Output is not selectable: {key}")
                    return {"key":key}
                def select(key):
                    state["selections"]+=1
                    self.assertEqual(key,returned)
                    self.assertGreaterEqual(state["probes"],5)
                    state["selected"]=key
                    return {"selected_output":{"key":key,"target_name":key}}
                async def rebuild(**kwargs):
                    self.assertEqual(state["selected"],returned)
                    self.assertEqual(state["force_rate"],48000)
                    state["rebuilt"]=True
                dsp=SimpleNamespace(stop=AsyncMock(),verify=AsyncMock(return_value=True))
                process=SimpleNamespace(returncode=0,communicate=AsyncMock(return_value=(b"",b"")))
                with (patch.object(main.runtime,"dsp_runtime",dsp),patch.object(main.runtime,"player_instance",None),
                      patch.object(src_policy,"activate_environment"),
                      patch.object(main.asyncio,"create_subprocess_exec",AsyncMock(return_value=process)),
                      patch.object(main.asyncio,"sleep",AsyncMock()),
                      patch.object(main,"get_audio_output_overview",side_effect=overview),
                      patch.object(main.samplerate,"prepare_audio_output_selection",side_effect=prepare),
                      patch.object(main.samplerate,"set_audio_output_selection",side_effect=select),
                      patch.object(main.samplerate,"set_pipewire_force_rate",side_effect=lambda rate:state.update(force_rate=rate)),
                      patch.object(main.dsp_orchestrator,"sync_runtime",side_effect=rebuild)):
                    await main._restart_src_graph({"force_rate":48000,"output_key":original})
                self.assertTrue(state["rebuilt"])
                self.assertEqual(state["selected"],returned)
                self.assertEqual(state["selections"],1,"Output selection must be committed once, not polled")

    async def test_src_restart_does_not_retry_a_failed_output_commit(self):
        import main
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock
        original="alsa_output.usb-Original.stereo"
        overview={"selected_output":{"key":original,"target_name":original},"outputs":[{"key":original}]}
        select=MagicMock(return_value={"selected_output":{"key":original,"target_name":"fxroute_dsp_sink"}})
        dsp=SimpleNamespace(stop=AsyncMock(),verify=AsyncMock(return_value=True))
        process=SimpleNamespace(returncode=0,communicate=AsyncMock(return_value=(b"",b"")))
        with (patch.object(main.runtime,"dsp_runtime",dsp),patch.object(main.runtime,"player_instance",None),
              patch.object(src_policy,"activate_environment"),
              patch.object(main.asyncio,"create_subprocess_exec",AsyncMock(return_value=process)),
              patch.object(main.asyncio,"sleep",AsyncMock()),
              patch.object(main,"get_audio_output_overview",return_value=overview),
              patch.object(main.samplerate,"set_pipewire_force_rate"),
              patch.object(main.samplerate,"prepare_audio_output_selection",return_value={"key":original}),
              patch.object(main.samplerate,"set_audio_output_selection",select),
              patch.object(main.dsp_orchestrator,"sync_runtime",AsyncMock()) as sync):
            with self.assertRaisesRegex(RuntimeError,"could not be reselected"):
                await main._restart_src_graph({"force_rate":48000,"output_key":original})
        select.assert_called_once_with(original)
        sync.assert_not_awaited()

    async def test_unwritable_policy_returns_a_structured_actionable_error(self):
        import main
        from unittest.mock import AsyncMock
        from fastapi.testclient import TestClient
        restart=AsyncMock()
        with (patch.object(main,"_guard_src_change",AsyncMock()),
              patch.object(main,"_src_graph_lock",asyncio.Lock),
              patch.object(main,"_snapshot_src_graph",AsyncMock(return_value={"force_rate":48000})),
              patch.object(main,"_restart_src_graph",restart),
              patch.object(src_policy,"atomic_write_text",side_effect=OSError(30,"Read-only file system"))):
            response=await asyncio.to_thread(TestClient(main.app,raise_server_exceptions=False).post,
                                             "/api/audio/src",json={"mode":"high"},headers={"Accept-Encoding":"identity"})
        self.assertEqual(response.status_code,503)
        self.assertIn("Cannot save SRC policy",response.json()["detail"])
        restart.assert_not_awaited()
        self.assertEqual(src_policy.load_policy(),{"mode":"standard"})

    async def test_failed_rollback_persistence_reports_both_causes(self):
        from audio import src_api
        calls=[]
        real_persist=src_policy.persist_policy
        def persist(mode):
            calls.append(mode)
            if len(calls)>1:
                raise RuntimeError("Cannot save SRC policy: disk full")
            return real_persist(mode)
        async def idle():
            pass
        async def restart():
            raise RuntimeError("new graph failed")
        with patch.object(src_api.src_policy,"persist_policy",side_effect=persist):
            with self.assertRaisesRegex(RuntimeError,"new graph failed; previous graph could not be restored: Cannot save SRC policy"):
                await self.Controller(idle,restart,asyncio.Lock()).apply("high")
        self.assertEqual(calls,["high","standard"])

    async def test_src_rejects_a_released_measurement_lock_reserved_for_a_waiter(self):
        import main
        from measurement import session as measurement
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        session=measurement.MeasurementSampleRateSession()
        coordinator=asyncio.Lock()
        async def transition(*args,**kwargs):
            async with coordinator:
                return SimpleNamespace(committed=True,snapshot={"original_force_rate":0})
        services=SimpleNamespace(get_samplerate_status=lambda:{"policy":{"mode":"auto"}},
            coordinator_current_playback_context=AsyncMock(return_value={}),run_coordinated_transition=transition)
        async def change():
            with self.assertRaises(main.HTTPException) as error:
                async with main._src_graph_lock():
                    self.fail("SRC stole a lock reserved for measurement entry")
            self.assertEqual(error.exception.status_code,423)
        with (patch.object(measurement,"_measurement_services",return_value=services),
              patch.object(measurement,"_capture_playback_state_before_measurement"),
              patch.object(main,"measurement_sr_session",session),
              patch.object(main,"_source_transition_lock",return_value=asyncio.Lock()),
              patch.object(main,"_coordinator_lock_for_source_pause",return_value=coordinator),
              patch.object(main,"_dsp_mutation_lock",return_value=asyncio.Lock())):
            await session.lock.acquire()
            entry=asyncio.create_task(session.start(48000))
            await asyncio.sleep(0)
            apply=asyncio.create_task(change())
            session.lock.release()
            await asyncio.wait_for(asyncio.gather(apply,entry),1)


if __name__ == "__main__":
    unittest.main()
