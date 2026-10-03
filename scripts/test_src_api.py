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
                state={"force_rate":0,"selected":wrong,"probes":0,"rebuilt":False}
                def overview():
                    state["probes"]+=1
                    outputs=[{"key":wrong}]
                    if state["probes"]>=3:
                        outputs.append({"key":returned})
                    return {"selected_output":{"key":state["selected"],"target_name":state["selected"]},"outputs":outputs}
                def select(key):
                    self.assertEqual(key,returned)
                    self.assertGreaterEqual(state["probes"],3)
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
                      patch.object(main.samplerate,"set_audio_output_selection",side_effect=select),
                      patch.object(main.samplerate,"set_pipewire_force_rate",side_effect=lambda rate:state.update(force_rate=rate)),
                      patch.object(main.dsp_orchestrator,"sync_runtime",side_effect=rebuild)):
                    await main._restart_src_graph({"force_rate":48000,"output_key":original})
                self.assertTrue(state["rebuilt"])
                self.assertEqual(state["selected"],returned)

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
