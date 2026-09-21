#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Speaker Align release registration: committed plans survive session release.

After a speaker job commits its winner, a measurement-session release with a
rate change must rebuild the committed output plan at the restore rate
instead of re-syncing the stale legacy overview. The speaker finalizer
registers one adapter per committed job; the release invokes it at the
measurement-only boundary (and after a committed coordinator restore) and
clears the slot when release completes. Anything uncommitted or unconfigured
keeps the legacy path byte-identical.
"""

import asyncio
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import measurement.session as measurement_session
from measurement.session import MeasurementSampleRateSession, MeasurementServices
from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state import default_output_state, set_mode_routing, set_crossover, switch_mode
from audio.output_state_store import OutputStateStore
from dsp.manager import DSPManager
from measurement.speaker_commit import create_speaker_release_adapter


RATE = 48000
RESTORE_RATE = 44100


class ReleaseServices:
    """Fake measurement services with an unreadable legacy overview."""

    def __init__(self):
        self.sync_at_rate_calls = []
        self.coordinator_restores = []
        self.coordinator_committed = True
        self.end_attempts = 0
        self.track_info = None
        self.intent_current = True

    def fail_overview(self, *args, **kwargs):
        raise AssertionError("legacy overview read during committed-plan release")

    async def sync_runtime_at_rate(self, rate, **kwargs):
        self.sync_at_rate_calls.append(rate)

    async def restore_measurement(self, **kwargs):
        self.coordinator_restores.append(kwargs)
        return SimpleNamespace(committed=self.coordinator_committed)

    async def intent_matches(self, **kwargs):
        return self.intent_current

    def build(self):
        return MeasurementServices(
            get_store=lambda: None,
            get_session=lambda: None,
            auto_sub_active=lambda: False,
            get_dsp_runtime=lambda: None,
            get_player=lambda: None,
            get_samplerate_status=lambda: {
                "clock_rate": RESTORE_RATE, "active_rate": RESTORE_RATE,
                "force_rate": RESTORE_RATE},
            get_audio_output_overview=self.fail_overview,
            get_current_track_info=lambda: self.track_info,
            get_playback_transition_coordinator=lambda: SimpleNamespace(
                restore_measurement=self.restore_measurement),
            get_dsp_orchestrator=lambda: SimpleNamespace(
                sync_runtime_at_rate=self.sync_runtime_at_rate),
            get_playback_intent_generation=lambda: 0,
            run_coordinated_transition=lambda *a, **k: None,
            coordinator_current_playback_context=lambda: None,
            begin_playback_transition_attempt=lambda: 7,
            end_playback_transition_attempt=self.end_attempt,
            get_current_pipewire_force_rate=lambda: RESTORE_RATE,
            set_pipewire_force_rate=lambda *a, **k: None,
            ensure_playback_samplerate_force=lambda *a, **k: None,
            wait_for_samplerate_alignment=lambda *a, **k: None,
            reconcile_transition_sink_rate=lambda *a, **k: None,
            playback_graph_diagnosis=lambda *a, **k: None,
            log_playback_graph_diagnosis=lambda *a, **k: None,
            measurement_restore_intent_matches_live_state=self.intent_matches,
            spotify_snapshot_identity_values=lambda snapshot: set(),
            spotify_target_track_from_state=lambda *a, **k: {},
            get_player_audio_samplerate=lambda *a, **k: None,
            pulse_suspend_sink_for_samplerate=lambda *a, **k: None,
            audio_output_overview_with_effective_rate=lambda *a, **k: {},
            spotify_prearm_sample_rate_hz=44100,
            pipewire_handoff_poll_interval_ms=50,
        )

    def end_attempt(self):
        self.end_attempts += 1


def hold_manual_window(session, job_id="speaker-manual-job"):
    session.active = True
    session.measurement_rate = RATE
    session.active_manual_job_ids.add(job_id)
    return job_id


class SpeakerSessionReleaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fakes = ReleaseServices()
        measurement_session.configure_services(self.fakes.build())
        self.addCleanup(setattr, measurement_session, "_services", None)
        self.addCleanup(setattr, measurement_session,
                        "_playback_state_before_measurement", None)
        measurement_session._playback_state_before_measurement = None
        self.session = MeasurementSampleRateSession()
        self.adapter_rates = []

        async def adapter(restore_rate_hz):
            self.adapter_rates.append(restore_rate_hz)
            return {"revision": 8, "plan_fingerprint": "fp-speaker",
                    "sample_rate_hz": restore_rate_hz}

        self.adapter = adapter

    async def release_with_speaker_adapter(self, job_id="speaker-manual-job"):
        hold_manual_window(self.session, job_id)
        await self.session.request_close()
        await self.session.register_speaker_align_release_adapter(self.adapter)
        await self.session.unregister_manual_job(job_id)

    async def test_measurement_only_release_invokes_speaker_adapter(self):
        await self.release_with_speaker_adapter()
        self.assertEqual(self.adapter_rates, [RESTORE_RATE])
        self.assertEqual(self.fakes.sync_at_rate_calls, [])
        self.assertEqual(self.fakes.coordinator_restores, [])

    async def test_release_without_adapter_keeps_legacy_sync(self):
        job_id = hold_manual_window(self.session)
        await self.session.request_close()
        await self.session.unregister_manual_job(job_id)
        self.assertEqual(self.adapter_rates, [])
        self.assertEqual(self.fakes.sync_at_rate_calls, [RESTORE_RATE])

    async def test_coordinator_release_also_consumes_speaker_plan(self):
        self.fakes.track_info = {"source": "local"}
        measurement_session._playback_state_before_measurement = {
            "source": "local", "expected_rate": RESTORE_RATE,
            "was_playing": True, "url": "file:///test.flac",
            "path": "/test.flac", "id": "t-1", "title": "Test",
            "position": 10.0, "intent_generation": 0,
        }
        await self.release_with_speaker_adapter()
        self.assertEqual(len(self.fakes.coordinator_restores), 1)
        self.assertEqual(self.adapter_rates, [RESTORE_RATE])
        self.assertEqual(self.fakes.sync_at_rate_calls, [])

    async def test_adapter_cleared_on_completed_release(self):
        await self.release_with_speaker_adapter()
        self.assertEqual(self.adapter_rates, [RESTORE_RATE])
        self.assertIsNone(self.session._speaker_align_release_adapter)
        job_id = hold_manual_window(self.session, "speaker-manual-job-2")
        await self.session.request_close()
        await self.session.unregister_manual_job(job_id)
        self.assertEqual(self.adapter_rates, [RESTORE_RATE])
        self.assertEqual(self.fakes.sync_at_rate_calls, [RESTORE_RATE])

    async def test_register_adapter_rejects_non_callable(self):
        with self.assertRaises(ValueError):
            await self.session.register_speaker_align_release_adapter(None)

    async def test_autosub_adapter_still_invoked_when_present(self):
        autosub_rates = []

        async def autosub_adapter(restore_rate_hz):
            autosub_rates.append(restore_rate_hz)
            return {"revision": 7, "plan_fingerprint": "fp-autosub",
                    "sample_rate_hz": restore_rate_hz}

        job_id = hold_manual_window(self.session)
        await self.session.request_close()
        await self.session.register_autosub_release_adapter(autosub_adapter)
        await self.session.register_speaker_align_release_adapter(self.adapter)
        await self.session.unregister_manual_job(job_id)
        self.assertEqual(autosub_rates, [RESTORE_RATE])
        self.assertEqual(self.adapter_rates, [RESTORE_RATE])


class SpeakerServiceHookTests(unittest.IsolatedAsyncioTestCase):
    """The application service notifies exactly one committed job."""

    def setUp(self):
        from measurement.speaker_service import SpeakerAlignService
        self.Service = SpeakerAlignService
        self.hooks = []

        async def on_committed(context):
            self.hooks.append(dict(context))

        self.on_committed = on_committed

    def crossover_state(self):
        from audio.output_state import default_output_state, set_mode_routing, switch_mode
        from audio.output_state import set_crossover
        state = set_crossover(default_output_state(), "stereo-sub", True)
        routes = [f"{side}_{way}" for side in ("right", "left") for way in ("high", "low")]
        routes += ["sub1", "left_low"]
        state = set_mode_routing(state, "stereo-sub", "dev", routes)
        state = switch_mode(state, "stereo-sub")
        state["revision"] = 7
        processing = state["modes"]["stereo-sub"]["processing"]
        for side in ("left", "right"):
            processing[f"{side}_low"]["lowpass"] = {
                "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000,
            }
            processing[f"{side}_high"]["highpass"] = {
                "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000,
            }
        return state

    def captures_for(self, alignment, arrivals):
        import numpy as np
        from measurement.target import REFERENCE_TAP_INGRESS
        rate = RATE

        def lowpass_kernel(cutoff):
            offsets = np.arange(-256, 257, dtype=float)
            sigma = np.sqrt(2 * np.log(2)) * rate / (2 * np.pi * cutoff)
            kernel = np.exp(-0.5 * (offsets / sigma) ** 2)
            return kernel / np.sum(kernel)

        delta = np.zeros(513)
        delta[256] = 1
        lowpass = lowpass_kernel(2000)
        boundaries = [np.zeros(513), lowpass, delta]
        captures = []
        for index, request in enumerate(alignment.capture_requests()):
            reference = 700 + index * 131
            arrival = reference + arrivals[index]
            ir = np.zeros(4096)
            ir[arrival - 256:arrival + 257] = boundaries[index + 1] - boundaries[index]
            captures.append({
                "role": request["role"],
                "measurement_target": request["measurement_target"],
                "reference_id": request["reference_id"],
                "microphone_position_id": request["microphone_position_id"],
                "reference_tap": REFERENCE_TAP_INGRESS,
                "time_reference": "deconvolved-sweep-origin",
                "impulse_response": ir,
                "analysis": {
                    "sample_rate": rate, "peak_dbfs": -12.0,
                    "quality_checks": {"status": "pass", "items": []},
                    "reference_path": {
                        "usable": True, "electrical_reference_used": True,
                        "timing_status": "electrical-reference", "stability": "stable",
                        "confidence": 0.95, "clipped": False, "peak_dbfs": -9.0,
                    },
                    "impulse_response": {
                        "direct_arrival_index": arrival, "reference_peak_index": reference,
                        "direct_confidence": 0.95,
                        "timing_source": "direct_arrival_minus_reference_peak",
                    },
                },
            })
        return captures

    def make_service(self, **overrides):
        state = self.crossover_state()

        async def acquire(alignment, **kwargs):
            return {"captures": self.captures_for(alignment, (96, 240)),
                    "provenance": {}}

        async def confirm(alignment, **kwargs):
            request = alignment.verification_request()
            return {"confirmation": {
                        "start_revision": request["measurement_target"]["revision"],
                        "processing_fingerprint": request["measurement_target"]["processing_fingerprint"],
                        "arrival_ms": {role: 0.0 for role in request["roles"]},
                        "way_levels_db": {role: 0.0 for role in request["roles"]}},
                    "provenance": {}}

        def create_session(start_state, **kwargs):
            class Session:
                async def stage_candidate(self, candidate):
                    pass

                def measurement_context(self):
                    return {}

                async def confirm_and_commit(self, **kwargs):
                    return {"confirmed": True,
                            "check": {"confirmed": True, "reasons": [],
                                      "max_residual_ms": 0.05, "before_spread_ms": 3.0,
                                      "after_arrival_ms": {"left_low": 5, "left_high": 5.05},
                                      "tolerance_ms": 0.25, "pairs": []},
                            "confirmation": {}, "committed": {"revision": 8}}

            return Session()

        from measurement.target import freeze_measurement_target
        kwargs = dict(
            get_state=lambda: __import__("copy").deepcopy(state),
            describe=lambda state: {"output_key": "dev", "channels": 6,
                                    "sample_rate_hz": RATE, "fingerprint": "frozen-plan"},
            acquire=acquire,
            confirm=confirm,
            create_session=create_session,
            freeze_live=lambda state, **ctx: freeze_measurement_target(
                state, bank_id="global", output_key=ctx["output_key"],
                channels=ctx["channels"], sample_rate_hz=ctx["sample_rate_hz"],
                fingerprint=ctx["fingerprint"]),
            on_committed=self.on_committed,
        )
        kwargs.update(overrides)
        return self.Service(**kwargs)

    async def test_on_committed_optional_by_default(self):
        service = self.make_service(on_committed=None)
        self.assertIsNone(service._on_committed)

    async def test_committed_calls_hook_with_device_context(self):
        service = self.make_service()
        job_id = service.start(
            "left", input_id="mic", reference_input_channel="2",
            reference_id="interface:input-2:upstream",
            microphone_position_id="seat-1-fixed")
        job = await service.wait_for(job_id, timeout_seconds=10)
        self.assertEqual(job["status"], "committed", job)
        self.assertEqual(len(self.hooks), 1)
        self.assertEqual(self.hooks[0]["output_key"], "dev")
        self.assertEqual(self.hooks[0]["channels"], 6)
        self.assertEqual(self.hooks[0]["committed_revision"], 8)
        self.assertEqual(self.hooks[0]["job_id"], job_id)

    async def test_hook_failure_never_fails_the_job(self):
        async def failing_hook(context):
            raise RuntimeError("registration blew up")

        service = self.make_service(on_committed=failing_hook)
        job_id = service.start(
            "left", input_id="mic", reference_input_channel="2",
            reference_id="interface:input-2:upstream",
            microphone_position_id="seat-1-fixed")
        job = await service.wait_for(job_id, timeout_seconds=10)
        self.assertEqual(job["status"], "committed", job)

    async def test_unconfirmed_never_calls_hook(self):
        def create_session(start_state, **kwargs):
            class Session:
                async def stage_candidate(self, candidate):
                    pass

                def measurement_context(self):
                    return {}

                async def confirm_and_commit(self, **kwargs):
                    return {"confirmed": False,
                            "check": {"confirmed": False, "reasons": ["residual"],
                                      "max_residual_ms": 1.0, "before_spread_ms": 3.0,
                                      "after_arrival_ms": {"left_low": 5, "left_high": 6},
                                      "tolerance_ms": 0.25, "pairs": []},
                            "confirmation": {}, "restored": True}

            return Session()

        service = self.make_service(create_session=create_session)
        job_id = service.start(
            "left", input_id="mic", reference_input_channel="2",
            reference_id="interface:input-2:upstream",
            microphone_position_id="seat-1-fixed")
        job = await service.wait_for(job_id, timeout_seconds=10)
        self.assertEqual(job["status"], "unconfirmed", job)
        self.assertEqual(self.hooks, [])


class SpeakerCompositionTests(unittest.IsolatedAsyncioTestCase):
    """The composition root registers the adapter for committed jobs only."""

    async def test_build_accepts_session_and_factory_hooks(self):
        import tempfile
        from pathlib import Path as FsPath
        from audio.output_service import OutputService, OutputServiceDeps
        from audio.output_state import default_output_state
        from audio.output_state_store import OutputStateStore
        from dsp.manager import DSPManager
        from measurement.speaker_api import build_speaker_align_service

        directory = tempfile.TemporaryDirectory(prefix="speaker-release-compose-")
        self.addCleanup(directory.cleanup)
        home = FsPath(directory.name)
        manager = DSPManager(home=home / "dsp")
        manager.preset_store.write("Room", {
            "schema": "fxroute.dsp.preset", "version": 1, "chain": []})
        store = OutputStateStore(home / "output-state.json")
        output_service = OutputService(OutputServiceDeps(
            store=store, preset_loader=manager.preset_store.read,
            resolve_ir=lambda name: (_ for _ in ()).throw(AssertionError(name)),
            measurement_active=lambda: False))
        output_service.ensure_state()

        registered = []

        class Session:
            async def register_speaker_align_release_adapter(self, adapter):
                registered.append(adapter)

        def factory(*, output_key, channels):
            async def adapter(rate):
                return {"revision": 1, "plan_fingerprint": "fp",
                        "sample_rate_hz": rate}
            return adapter

        service = build_speaker_align_service(
            output_service=output_service, measurement_store=None,
            dsp_manager=manager,
            get_native_runtime=lambda: None,
            describe_device=lambda state: {"output_key": "dev", "channels": 2,
                                           "hardware_ports": ["a", "b"]},
            get_measurement_rate=lambda: RATE,
            capture_runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no capture")),
            verification_runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no verification")),
            get_measurement_session=lambda: Session(),
            build_release_adapter=factory)
        self.assertIsNotNone(service._on_committed)
        await service._on_committed({"output_key": "dev", "channels": 2,
                                     "committed_revision": 1})
        self.assertEqual(len(registered), 1)

    async def test_composition_without_session_keeps_legacy_path(self):
        import tempfile
        from pathlib import Path as FsPath
        from audio.output_service import OutputService, OutputServiceDeps
        from audio.output_state import default_output_state
        from audio.output_state_store import OutputStateStore
        from dsp.manager import DSPManager
        from measurement.speaker_api import build_speaker_align_service

        directory = tempfile.TemporaryDirectory(prefix="speaker-release-legacy-")
        self.addCleanup(directory.cleanup)
        home = FsPath(directory.name)
        manager = DSPManager(home=home / "dsp")
        manager.preset_store.write("Room", {
            "schema": "fxroute.dsp.preset", "version": 1, "chain": []})
        store = OutputStateStore(home / "output-state.json")
        output_service = OutputService(OutputServiceDeps(
            store=store, preset_loader=manager.preset_store.read,
            resolve_ir=lambda name: (_ for _ in ()).throw(AssertionError(name)),
            measurement_active=lambda: False))
        output_service.ensure_state()
        service = build_speaker_align_service(
            output_service=output_service, measurement_store=None,
            dsp_manager=manager,
            get_native_runtime=lambda: None,
            describe_device=lambda state: {"output_key": "dev", "channels": 2,
                                           "hardware_ports": ["a", "b"]},
            get_measurement_rate=lambda: RATE,
            capture_runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no capture")),
            verification_runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no verification")))
        self.assertIsNone(service._on_committed)


PORTS = ["playback_AUX0", "playback_AUX1"]


class SpeakerReleaseAdapterLiveDeviceTests(unittest.IsolatedAsyncioTestCase):
    """An orphaned speaker adapter refuses a switched device, like AutoSub."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="speaker-release-")
        self.addCleanup(directory.cleanup)
        self.manager = DSPManager(home=Path(directory.name) / "dsp")
        self.store = OutputStateStore(Path(directory.name) / "output-state.json")
        self.service = OutputService(OutputServiceDeps(
            store=self.store, preset_loader=self.manager.preset_store.read,
            resolve_ir=lambda name: (_ for _ in ()).throw(AssertionError(name)),
            measurement_active=lambda: True))
        state = switch_mode(set_mode_routing(default_output_state(), "stereo-sub", "dev",
                                 ["main_l", "main_r"]), "stereo-sub")
        self.service.commit(state, expected_revision=0)
        self.targets = []

        class FakeRuntime:
            async def sync_rendered(inner_self, target):
                self.targets.append(target)

        self.runtime = FakeRuntime()
        self.live = {"output_key": "dev", "channels": 2,
                     "hardware_ports": list(PORTS)}

    def build_adapter(self, **kwargs):
        args = dict(service=self.service, dsp_manager=self.manager,
                    hardware_ports=list(PORTS),
                    get_native_runtime=lambda: self.runtime,
                    output_key="dev", channels=2,
                    resolve_live_device=lambda: dict(self.live))
        args.update(kwargs)
        return create_speaker_release_adapter(**args)

    async def test_matching_live_device_renders(self):
        result = await self.build_adapter()(RESTORE_RATE)
        self.assertEqual(result["sample_rate_hz"], RESTORE_RATE)
        self.assertEqual(len(self.targets), 1)

    async def test_switched_ports_refuse_before_touching_runtime(self):
        adapter = self.build_adapter()
        self.live = {"output_key": "dev", "channels": 2,
                     "hardware_ports": ["playback_AUX0"]}
        with self.assertRaisesRegex(RuntimeError, "hardware_ports"):
            await adapter(RESTORE_RATE)
        self.assertEqual(self.targets, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
