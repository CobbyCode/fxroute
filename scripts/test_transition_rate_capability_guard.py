#!/usr/bin/env python3
"""Fail-fast sample-rate capability guard tests.

Covers the FXRoute DSP processing cap (384 kHz) and the pre-transition
rejection of target rates the selected output cannot carry:

- effective supported rates end at 384 kHz even when the device reports more
- a transition to an unsupported target rate is rejected before any
  PipeWire/DSP state is mutated (no gate close, no quiet, no rate switch)
- supported rates keep flowing through the normal transition path
- rate-neutral operations (measurement, output-mode switch, graph repair)
  are exempt
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from playback.transition import (
    PlaybackTransitionCoordinator,
    TransitionRequest,
    UnsupportedTransitionRateError,
)


class _FakeRuntime:
    """Minimal coordinator runtime recording which stages were entered."""

    def __init__(self, supported_rates: list[int] | None = None) -> None:
        self.supported_rates = supported_rates
        self.events: list[str] = []
        self.rate = 44_100
        self.muted = False

    async def _stage(self, name: str) -> None:
        self.events.append(name)

    async def read_hardware_mute(self) -> bool:
        self.events.append("read-mute")
        return self.muted

    async def set_hardware_mute(self, muted: bool, transition_id: str) -> None:
        self.muted = bool(muted)
        self.events.append(f"mute:{self.muted}")

    async def read_sink_mute(self, sink_name: str) -> bool:
        self.events.append("read-sink-mute")
        return self.muted

    async def set_sink_mute(self, sink_name: str, muted: bool, transition_id: str) -> None:
        self.muted = bool(muted)
        self.events.append(f"sink-mute:{self.muted}")

    async def read_transition_snapshot(self, request: TransitionRequest) -> dict:
        self.events.append("snapshot")
        snapshot: dict = {"active_rate": self.rate, "force_rate": self.rate}
        if self.supported_rates is not None:
            snapshot["audio_overview"] = {
                "selected_output": {"supported_rates": self.supported_rates},
            }
        return snapshot

    async def quiet_old_source(self, request: TransitionRequest) -> None:
        self.events.append("quiet")

    async def resolve_target_rate(self, request: TransitionRequest) -> int | None:
        self.events.append("resolve-rate")
        return request.target_rate

    async def establish_target_rate(self, request: TransitionRequest) -> None:
        self.events.append("rate")
        self.rate = request.target_rate

    async def establish_effects_and_helper(self, request: TransitionRequest) -> dict:
        self.events.append("effects-helper-links")
        return {"dsp_reinitialized": False, "helper_rebuilt": False}

    async def prepare_target_source(self, request: TransitionRequest) -> None:
        self.events.append("prepare")

    async def start_target_source(self, request: TransitionRequest) -> None:
        self.events.append("start")

    async def stabilize_effects_after_rate_change(
        self, request: TransitionRequest, *, dsp_reinitialized: bool = False
    ) -> dict:
        self.events.append("dsp-stabilize")
        return {
            "stabilized": True,
            "no_op": False,
            "active_rate": self.rate,
            "force_rate": self.rate,
            "graph_complete": True,
            "links_complete": True,
            "signature": "stable",
        }

    async def set_source_volume(self, volume: int, transition_id: str) -> None:
        self.events.append(f"source-volume:{volume}")

    async def verify_transition_graph(self, request: TransitionRequest) -> dict:
        self.events.append("verify-graph")
        return {
            "committed": True,
            "active_rate": self.rate,
            "links_complete": True,
            "signature": "stable",
        }

    async def verify_committed_transition(self, request: TransitionRequest) -> dict:
        self.events.append("verify")
        return {"committed": True, "active_rate": self.rate}

    async def pause_source_after_failure(self, request: TransitionRequest) -> None:
        self.events.append("pause-after-failure")

    async def wait_for_pipewire_spotify_release(self) -> bool:
        return True

    async def reconcile_post_start_graph(self, request: TransitionRequest) -> dict:
        self.events.append("reconcile-post-start-graph")
        return {"graph_complete": True, "committed": True}

    def target_source_staged(self, request: TransitionRequest) -> bool:
        return False

    async def abort_failed_transition(self, request, snapshot, *, target_staged: bool):
        return None

    async def publish_restored_source(self, request: TransitionRequest) -> None:
        self.events.append("publish-restored-source")

    async def normalize_queue_after_native_loss(self) -> None:
        self.events.append("normalize-queue-after-native-loss")


def _request(target_rate: int, *, operation: str = "play") -> TransitionRequest:
    return TransitionRequest(
        operation=operation,
        source="local",
        target_rate=target_rate,
        target_url="/music/target.wav",
        target_track={"source": "local", "url": "/music/target.wav"},
        should_play=True,
        rate_change=True,
        reload_source=True,
    )


class RateCapabilityGuardUnitTests(unittest.TestCase):
    """Direct checks of the coordinator's pre-transition validation."""

    def _validate(self, request: TransitionRequest) -> None:
        PlaybackTransitionCoordinator._validate_transition_target_rate(request)

    def test_fxroute_cap_rejects_rates_above_384_khz_without_overview(self):
        # SMSL-like: hardware supports 768 kHz but FXRoute processes only
        # up to 384 kHz.  The rejection needs no device overview at all.
        with self.assertRaises(UnsupportedTransitionRateError) as ctx:
            self._validate(_request(768000))
        self.assertIn("384000", str(ctx.exception))
        with self.assertRaises(UnsupportedTransitionRateError):
            self._validate(_request(705600))

    def test_device_cap_rejects_384_khz_when_device_max_is_192_khz(self):
        # UMC-like: target 384000, device effective maximum 192000.
        request = _request(384000)
        request = type(request)(**{
            **request.__dict__,
            "audio_overview": {"selected_output": {"supported_rates": [44100, 48000, 96000, 192000]}},
        })
        with self.assertRaises(UnsupportedTransitionRateError) as ctx:
            self._validate(request)
        self.assertIn("192000", str(ctx.exception))

    def test_device_above_384_khz_is_rejected_against_effective_list(self):
        # The overview already reports the FXRoute-capped list, so a native
        # 768 kHz device is rejected through the same device check.
        request = _request(768000)
        request = type(request)(**{
            **request.__dict__,
            "audio_overview": {
                "selected_output": {"supported_rates": [44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000]}
            },
        })
        with self.assertRaises(UnsupportedTransitionRateError):
            self._validate(request)

    def test_supported_rates_pass_unchanged(self):
        for target, supported in (
            (192000, [44100, 48000, 96000, 192000]),
            (384000, [44100, 48000, 96000, 192000, 384000]),
            (44100, [44100, 48000]),
            (48000, [44100, 48000]),
            (88200, [44100, 48000, 88200]),
            (96000, [44100, 48000, 96000]),
            (176400, [44100, 48000, 96000, 176400]),
        ):
            request = _request(target)
            request = type(request)(**{
                **request.__dict__,
                "audio_overview": {"selected_output": {"supported_rates": supported}},
            })
            self._validate(request)  # must not raise

    def test_missing_or_empty_capability_list_fails_open(self):
        # No overview, no selected output, or an empty list must never block.
        self._validate(_request(192000))
        request = _request(192000)
        request = type(request)(**{
            **request.__dict__,
            "audio_overview": {"current_output": {"supported_rates": []}},
        })
        self._validate(request)
        request = _request(192000)
        request = type(request)(**{
            **request.__dict__,
            "audio_overview": {},
        })
        self._validate(request)

    def test_non_positive_and_missing_target_rates_pass(self):
        for target in (None, 0, -1):
            base = _request(44100)
            request = TransitionRequest(**{**base.__dict__, "target_rate": target})
            self._validate(request)

    def test_rate_neutral_operations_are_exempt(self):
        # Measurement, output-mode switch and graph repair carry the current
        # committed rate by contract and are not rate-targeted.
        for operation in ("measurement-entry", "measurement-restore", "output-mode-switch", "graph-reconcile"):
            request = _request(768000, operation=operation)
            request = type(request)(**{
                **request.__dict__,
                "audio_overview": {"selected_output": {"supported_rates": [44100]}},
            })
            self._validate(request)  # must not raise


class RateCapabilityGuardTransitionTests(unittest.IsolatedAsyncioTestCase):
    """The coordinator rejects before mutating any transition state."""

    async def _run(self, target_rate: int, supported_rates: list[int] | None):
        runtime = _FakeRuntime(supported_rates=supported_rates)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        try:
            await coordinator.execute(_request(target_rate))
        except UnsupportedTransitionRateError:
            pass
        else:
            self.fail("execute must reject the unsupported target rate")
        return runtime

    async def test_384_khz_target_on_192_khz_device_is_rejected_before_stages(self):
        runtime = await self._run(384000, [44100, 48000, 96000, 192000])
        # Only the snapshot is read; no gate close, no quiet, no rate switch,
        # no effects/helper stage may run.
        self.assertEqual(runtime.events, ["snapshot"])

    async def test_768_khz_target_on_768_khz_device_is_rejected_before_stages(self):
        # Effective list already capped by the overview layer.
        runtime = await self._run(768000, [44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000])
        self.assertEqual(runtime.events, ["snapshot"])

    async def test_768_khz_target_without_overview_is_rejected_by_fxroute_cap(self):
        runtime = await self._run(768000, None)
        self.assertEqual(runtime.events, ["snapshot"])

    async def test_supported_rate_flows_through_normal_transition_path(self):
        for target in (192000, 384000, 96000):
            runtime = _FakeRuntime(supported_rates=[44100, 48000, 96000, 192000, 384000])
            coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
            result = await coordinator.execute(_request(target))
            self.assertTrue(result.committed)
            self.assertIn("rate", runtime.events)
            self.assertIn("effects-helper-links", runtime.events)
            self.assertNotIn("pause-after-failure", runtime.events)


if __name__ == "__main__":
    unittest.main()
