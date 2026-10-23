#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""External-input monitor ticks must not mutate a measurement-owned graph."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import measurement.session as measurement_session
from audio.external_input import ExternalInputRouting, ExternalInputRoutingDependencies
from dsp.orchestration import DspOrchestrator
from dsp.runtime import CommandResult, DSPRuntime, DSPRuntimeConfig, PipeWireLink, _contains_link
from measurement.session import MeasurementSampleRateSession
from playback.orchestration import PlaybackOrchestrator
from playback.runtime.verification import _RuntimeVerificationMixin
from playback.transition import TransitionRequest


DECODED = tuple((f"fxroute_surround_input:output_{position}", f"fxroute_dsp:input_{index}")
                for index, position in enumerate(("FL", "FR", "FC", "LFE", "SL", "SR", "BL", "BR"), 1))
MAPPED = (("capture.test:capture_AUX8", "fxroute_dsp:input_1"),
          ("capture.test:capture_AUX9", "fxroute_dsp:input_2"),
          ("capture.test:capture_AUX2", "fxroute_dsp:input_3"))
OUTPUTS = (("fxroute_dsp:output_1", "hardware:playback_AUX0"),
           ("fxroute_dsp:output_2", "hardware:playback_AUX1"))


class IngressFixture:
    """Keep routing/session logic real; replace only PipeWire and engine IO."""

    def __init__(self):
        self.links = set(OUTPUTS)
        self.native = DSPRuntime(SimpleNamespace(), command_runner=self.command)
        self.native._process = SimpleNamespace(returncode=None)
        self.native._config = DSPRuntimeConfig(
            "surround", "hardware", 48000, ("playback_AUX0", "playback_AUX1"), ())
        self.native._links = [PipeWireLink(*edge) for edge in OUTPUTS]
        self.bypass = False
        self.session = MeasurementSampleRateSession()
        self.session.entry_in_progress = True
        self.overview = {"output_mode": {"mode": "surround", "effective_output_key": "hardware",
                                        "hardware_playback_ports": ["playback_AUX0", "playback_AUX1"]}}
        self.orchestrator = PlaybackOrchestrator(SimpleNamespace(
            get_audio_output_overview=lambda: self.overview,
            run_pw_link_command=self.read_ports,
            resolve_source_producer_ports=lambda source: (),
            get_dsp_snapshot=self.native.snapshot,
            output_mode_subwoofer_modes=frozenset(),
            helper_argument_sample_rate=lambda snapshot: snapshot["config"]["sample_rate"],
            contains_link=_contains_link,
        ))

    async def command(self, args):
        if args == ("pw-link", "-l"):
            return CommandResult(0, self.listing())
        if args[:2] == ("pw-link", "-d"):
            self.links.discard(tuple(args[2:]))
        elif args[0] == "pw-link" and len(args) == 3:
            self.links.add(tuple(args[1:]))
        else:
            raise AssertionError(f"Unexpected PipeWire command: {args}")
        return CommandResult(0)

    def listing(self):
        return "\n".join(f"{source} -> {target}" for source, target in sorted(self.links))

    async def read_ports(self, argument):
        if argument == "-l":
            return self.listing()
        if argument == "-io":
            edges = DECODED + MAPPED + OUTPUTS + (
                ("fxroute_dsp_sink:monitor_FL", "fxroute_dsp:input_1"),
                ("fxroute_dsp_sink:monitor_FR", "fxroute_dsp:input_2"))
            return "\n".join(sorted({port for edge in edges for port in edge}))
        raise AssertionError(f"Unexpected port query: {argument}")

    async def control(self, message, *, reply=False):
        if message == "effects bypass get":
            return str(int(self.bypass))
        if message in ("effects bypass 0", "effects bypass 1"):
            self.bypass = message.endswith("1")
            return "ok"
        raise AssertionError(f"Unexpected engine control: {message}")

    async def select(self, kind):
        if kind == "decoded":
            await self.native.set_surround_input(True)
        else:
            await self.native.set_mapped_ingress(MAPPED)

    async def monitor_tick(self, kind):
        if kind == "decoded":
            await main._sync_surround_input("capture.test", "AUX8", "AUX9", 3)
        else:
            await main._sync_mapped_input(MAPPED)


class MeasurementIngressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = IngressFixture()
        for target, name, value in (
            (main.runtime, "dsp_runtime", self.fixture.native),
            (main, "measurement_sr_session", self.fixture.session),
            (main, "_ingress_resync_pending", False),
            (self.fixture.native, "_control_unlocked", self.fixture.control),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def verifier(self, diagnosis):
        verifier = _RuntimeVerificationMixin()
        verifier._deps = SimpleNamespace(
            get_samplerate_status=lambda: {"active_rate": 48000, "force_rate": 48000, "clock_rate": 48000},
            playback_graph_diagnosis=diagnosis,
            log_playback_graph_diagnosis=lambda *args, **kwargs: None,
        )
        return verifier

    def request(self):
        return TransitionRequest(operation="measurement-entry", source="local", target_rate=48000,
                                 should_play=False, rate_change=False, reload_source=False)

    async def test_monitor_between_readbacks_keeps_entry_stable(self):
        for kind, edges in (("decoded", DECODED), ("mapped", MAPPED)):
            with self.subTest(kind=kind):
                await self.fixture.native.set_surround_input(False)
                await self.fixture.native.set_mapped_ingress(())
                await self.fixture.select(kind)
                readbacks = []

                async def diagnosis(**kwargs):
                    result = await self.fixture.orchestrator.playback_graph_diagnosis(**kwargs)
                    readbacks.append(result)
                    if len(readbacks) == 1:
                        await self.fixture.monitor_tick(kind)
                    return result

                result = await self.verifier(diagnosis).verify_measurement_entry(self.request())
                self.assertTrue(result["committed"])
                self.assertEqual(len(readbacks), 2)
                self.assertTrue(all(item["links_complete"] for item in readbacks))
                self.assertEqual(readbacks[0]["signature"], readbacks[1]["signature"])
                self.assertTrue(set(edges).issubset(self.fixture.links))
                self.assertTrue(main._ingress_resync_pending)

    async def test_scope_owns_suspension_and_restores_after_monitor_ticks(self):
        self.fixture.session.entry_in_progress = False
        self.fixture.session.active = True
        for raw in (False, True):
            for kind, edges in (("decoded", DECODED), ("mapped", MAPPED)):
                with self.subTest(raw=raw, kind=kind):
                    await self.fixture.native.set_surround_input(False)
                    await self.fixture.native.set_mapped_ingress(())
                    await self.fixture.select(kind)
                    original = set(self.fixture.links)
                    await self.fixture.monitor_tick(kind)
                    enter = self.fixture.native.enter_raw_measurement if raw else self.fixture.native.enter_active_measurement
                    exit_scope = self.fixture.native.exit_raw_measurement if raw else self.fixture.native.exit_active_measurement
                    previous = await enter()
                    try:
                        self.assertFalse(set(edges) & self.fixture.links)
                        await self.fixture.monitor_tick(kind)
                        self.assertFalse(set(edges) & self.fixture.links)
                    finally:
                        await exit_scope(previous)
                    await self.fixture.monitor_tick(kind)
                    self.assertEqual(self.fixture.links, original)
                    self.assertFalse(self.fixture.bypass)
                    self.assertFalse(self.fixture.native._measurement_scope_lock.locked())

    async def test_cancelled_sweep_restores_ingress_while_session_still_owns_graph(self):
        self.fixture.session.entry_in_progress = False
        self.fixture.session.active = True
        await self.fixture.select("decoded")
        original = set(self.fixture.links)
        entered = asyncio.Event()

        async def sweep():
            previous = await self.fixture.native.enter_raw_measurement()
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                await self.fixture.native.exit_raw_measurement(previous)

        task = asyncio.create_task(sweep())
        await entered.wait()
        try:
            await self.fixture.monitor_tick("decoded")
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await self.fixture.monitor_tick("decoded")
        self.assertEqual(self.fixture.links, original)
        self.assertFalse(self.fixture.bypass)

    async def test_incomplete_graph_is_still_rejected_without_dropping_ingress(self):
        await self.fixture.select("decoded")
        self.fixture.links.remove(OUTPUTS[1])

        async def diagnosis(**kwargs):
            result = await self.fixture.orchestrator.playback_graph_diagnosis(**kwargs)
            await self.fixture.monitor_tick("decoded")
            return result

        with self.assertRaisesRegex(RuntimeError, "did not reach two stable readbacks"):
            await self.verifier(diagnosis).verify_measurement_entry(self.request())
        self.assertTrue(set(DECODED).issubset(self.fixture.links))

    async def test_entry_drains_an_inflight_decoder_sync_before_suspending_ingress(self):
        await self.assert_inflight_sync_drained()

    async def test_entry_does_not_deadlock_an_inflight_carrier_rate_repair(self):
        await self.assert_inflight_sync_drained(carrier_repair=True)

    async def assert_inflight_sync_drained(self, *, carrier_repair=False):
        self.fixture.session.entry_in_progress = False
        await self.fixture.select("decoded")
        decoder_started = asyncio.Event()
        finish_decoder = asyncio.Event()
        handoff_reached = asyncio.Event()
        measurement_entered = asyncio.Event()
        finish_measurement = asyncio.Event()
        rate = {"active_rate": 44100 if carrier_repair else 48000, "force_rate": 48000, "clock_rate": 48000}

        async def render(rate, overview):
            self.assertEqual(rate, 48000)
            self.assertEqual(overview["output_mode"]["effective_output_rate"], 48000)
            return SimpleNamespace(rate=rate)

        async def sync_engine(target):
            self.assertEqual(target.rate, 48000)

        dsp_orchestrator = DspOrchestrator(SimpleNamespace(
            get_dsp_runtime=lambda: self.fixture.native,
            get_measurement_sr_session=lambda: self.fixture.session,
            get_audio_output_overview=lambda: self.fixture.overview,
            get_samplerate_status=lambda: dict(rate),
            try_render_v2_target=render,
        ))

        async def start_decoder(*args, **kwargs):
            if not carrier_repair:
                decoder_started.set()
                await finish_decoder.wait()

        async def carrier(*args):
            if carrier_repair:
                decoder_started.set()
                await finish_decoder.wait()
                rate["active_rate"] = 48000
                await dsp_orchestrator.sync_runtime(reason="coordinator-sample-rate-policy")

        async def stop_decoder():
            pass

        overview = {"mode": "external-input", "external_input_state": "available", "selected_input": {
            "key": "capture.test::pair:9-10", "source_key": "capture.test",
            "left_channel": "AUX8", "right_channel": "AUX9", "alsa_card": 3}}
        deps = ExternalInputRoutingDependencies(
            get_audio_source_overview=lambda: overview, get_output_mode=lambda: "surround",
            sync_digital_input=main._sync_surround_input, stop_digital_input=main._stop_surround_input)
        deps.measurement_audio_graph_owned = lambda: self.fixture.session.owns_audio_graph
        deps.defer_input_resync = main._mark_ingress_resync_pending
        routing = ExternalInputRouting(deps)

        @asynccontextmanager
        async def entry_guard():
            handoff_reached.set()
            async with routing.measurement_entry_guard():
                yield

        async def coordinated(request):
            result = await self.verifier(self.fixture.orchestrator.playback_graph_diagnosis).verify_measurement_entry(request)
            return SimpleNamespace(committed=result["committed"])

        async def context():
            return {"source": "local"}

        services = SimpleNamespace(
            input_entry_guard=entry_guard,
            get_samplerate_status=lambda: dict(rate),
            coordinator_current_playback_context=context,
            run_coordinated_transition=coordinated,
            get_playback_intent_generation=lambda: 0,
            get_player_audio_samplerate=lambda: None,
            get_current_track_info=lambda: None,
            spotify_target_track_from_state=lambda state: {},
            spotify_prearm_sample_rate_hz=48000,
            get_session=lambda: self.fixture.session,
            get_store=lambda: None,
            get_dsp_runtime=lambda: self.fixture.native,
        )

        async def measure():
            await self.fixture.session.register_auto_sub("inflight-test")
            previous = await self.fixture.native.enter_active_measurement()
            try:
                measurement_entered.set()
                handoff_reached.set()
                await finish_measurement.wait()
            finally:
                await self.fixture.native.exit_active_measurement(previous)

        with patch.object(main, "_surround_input", SimpleNamespace(start=start_decoder, stop=stop_decoder)), \
                patch.object(main, "_set_surround_carrier_owned", carrier), \
                patch.object(self.fixture.native, "sync_rendered", sync_engine), \
                patch.object(measurement_session, "_services", services), \
                patch.object(measurement_session, "_playback_state_before_measurement", None):
            monitor = asyncio.create_task(routing.sync())
            await decoder_started.wait()
            measurement = asyncio.create_task(measure())
            try:
                async with asyncio.timeout(2):
                    await handoff_reached.wait()
                self.assertFalse(measurement_entered.is_set(), "Entry must drain the old monitor before opening a measurement scope")
                finish_decoder.set()
                async with asyncio.timeout(2):
                    await monitor
                    await measurement_entered.wait()
                self.assertFalse(set(DECODED) & self.fixture.links)
                await routing.sync()
                self.assertFalse(set(DECODED) & self.fixture.links)
            finally:
                finish_decoder.set()
                finish_measurement.set()
                for task in (monitor, measurement):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(monitor, measurement, return_exceptions=True)
        self.assertTrue(set(DECODED).issubset(self.fixture.links))
        self.assertTrue(main._ingress_resync_pending)

    async def test_owned_monitor_preserves_decoder_identity_even_on_unavailability(self):
        await self.fixture.select("decoded")
        deps = ExternalInputRoutingDependencies(
            get_audio_source_overview=lambda: {}, get_output_mode=lambda: "surround",
            stop_digital_input=main._stop_surround_input)
        deps.measurement_audio_graph_owned = lambda: self.fixture.session.owns_audio_graph
        deps.defer_input_resync = main._mark_ingress_resync_pending
        routing = ExternalInputRouting(deps)
        routing.digital_source_name = "capture.test"
        routing.digital_selection_key = "capture.test::pair:9-10"
        await routing.sync({"mode": "external-input", "external_input_state": "unavailable"})
        self.assertEqual(routing.digital_source_name, "capture.test")
        self.assertEqual(routing.digital_selection_key, "capture.test::pair:9-10")
        self.assertTrue(set(DECODED).issubset(self.fixture.links))
        self.assertTrue(main._ingress_resync_pending)

    async def test_cancelled_input_drain_leaves_session_free_for_next_entry(self):
        await self.assert_waiting_entry_cleanup(cancel=True)

    async def test_close_invalidates_an_entry_waiting_for_input_routing(self):
        await self.assert_waiting_entry_cleanup(cancel=False)

    async def assert_waiting_entry_cleanup(self, *, cancel):
        session = self.fixture.session
        session.entry_in_progress = False
        routing = ExternalInputRouting(ExternalInputRoutingDependencies(get_audio_source_overview=lambda: {}))
        transitions = []

        async def context():
            return {"source": "local"}

        async def coordinated(request):
            transitions.append(request)
            return SimpleNamespace(committed=True)

        services = replace(main._make_measurement_services(),
            get_current_track_info=lambda: None, get_store=lambda: None,
            get_samplerate_status=lambda: {"active_rate": 48000, "force_rate": 48000},
            coordinator_current_playback_context=context, run_coordinated_transition=coordinated,
            stdin_measurement_acquire=None, stdin_measurement_release=None)
        with patch.object(main, "external_input", routing), \
                patch.object(measurement_session, "_services", services), \
                patch.object(measurement_session, "_playback_state_before_measurement", None):
            async with routing._sync_lock:
                token = session.capture_entry_epoch()
                pending = asyncio.create_task(session.register_auto_sub("waiting-entry", entry_epoch=token))
                await asyncio.sleep(0)
                try:
                    self.assertFalse(pending.done())
                    self.assertFalse(session.lock.locked())
                    self.assertFalse(session.owns_audio_graph)
                    if cancel:
                        pending.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await pending
                    else:
                        async with asyncio.timeout(2):
                            await session.request_close()
                except BaseException:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    raise
            if not cancel:
                with self.assertRaises(measurement_session.MeasurementEntryInvalidated):
                    await pending
            self.assertFalse(session.owns_audio_graph)
            self.assertFalse(session.has_active_jobs)
            self.assertFalse(session.lock.locked())
            self.assertFalse(routing._sync_lock.locked())
            self.assertEqual(transitions, [])
            await session.register_auto_sub("fresh-entry", entry_epoch=session.capture_entry_epoch())
            self.assertTrue(session.active)
            self.assertEqual(session.active_auto_sub_job_id, "fresh-entry")
            self.assertEqual(len(transitions), 1)


if __name__ == "__main__":
    unittest.main()
