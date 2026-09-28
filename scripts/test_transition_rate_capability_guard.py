#!/usr/bin/env python3
"""Sample-rate capability fallback and guard tests.

Covers the FXRoute DSP processing cap (384 kHz) and the target-rate fit to
the selected output:

- a supported target rate plays natively (unchanged request)
- an unsupported target rate falls back to the highest supported rate below
  it (96 -> 48 kHz on a 48 kHz-only card, 384 -> 192 kHz on a 192 kHz
  device) and runs through the normal transition path at that rate
- effective supported rates end at 384 kHz even when the device reports more
- only a rate above 384 kHz with an unknown capability is still rejected
  before any PipeWire/DSP state is mutated
- rate-neutral operations (measurement, output-mode switch, graph repair)
  are exempt
"""

from __future__ import annotations

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


def _with_rates(request: TransitionRequest, supported: list[int]) -> TransitionRequest:
    return type(request)(**{
        **request.__dict__,
        "audio_overview": {"selected_output": {"supported_rates": supported}},
    })


class RateCapabilityGuardUnitTests(unittest.TestCase):
    """Direct checks of the coordinator's target-rate fit and guard."""

    def _fit(self, request: TransitionRequest) -> TransitionRequest:
        fitted = PlaybackTransitionCoordinator._fit_transition_target_rate(request)
        PlaybackTransitionCoordinator._validate_transition_target_rate(fitted)
        return fitted

    def test_fxroute_cap_rejects_rates_above_384_khz_without_overview(self):
        # SMSL-like: hardware supports 768 kHz but FXRoute processes only
        # up to 384 kHz.  Without a capability list there is nothing to
        # fall back to, so the rejection stays.
        with self.assertRaises(UnsupportedTransitionRateError) as ctx:
            self._fit(_request(768000))
        self.assertIn("384000", str(ctx.exception))
        with self.assertRaises(UnsupportedTransitionRateError):
            self._fit(_request(705600))

    def test_unsupported_rate_falls_back_to_highest_supported_below(self):
        for target, supported, expected in (
            (96000, [44100, 48000], 48000),                       # 48 kHz-only card
            (384000, [44100, 48000, 96000, 192000], 192000),      # 192 kHz device
            (176400, [44100, 48000, 96000], 96000),
            (88200, [44100, 48000], 48000),
            (22050, [44100, 48000], 44100),                       # nothing lower
        ):
            fitted = self._fit(_with_rates(_request(target), supported))
            self.assertEqual(fitted.target_rate, expected, (target, supported))
            self.assertTrue(fitted.rate_change)

    def test_device_above_384_khz_falls_back_within_the_effective_list(self):
        # The overview reports the FXRoute-capped list, so a 768 kHz source
        # on a native 768 kHz device plays at 384 kHz.
        request = _with_rates(
            _request(768000),
            [44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000],
        )
        self.assertEqual(self._fit(request).target_rate, 384000)

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
            request = _with_rates(_request(target), supported)
            self.assertIs(self._fit(request), request)

    def test_missing_or_empty_capability_list_fails_open(self):
        # No overview, no selected output, or an empty list never changes
        # or blocks the target.
        request = _request(192000)
        self.assertIs(self._fit(request), request)
        request = type(request)(**{
            **request.__dict__,
            "audio_overview": {"current_output": {"supported_rates": []}},
        })
        self.assertIs(self._fit(request), request)
        request = type(request)(**{**request.__dict__, "audio_overview": {}})
        self.assertIs(self._fit(request), request)

    def test_non_positive_and_missing_target_rates_pass(self):
        for target in (None, 0, -1):
            base = _request(44100)
            request = TransitionRequest(**{**base.__dict__, "target_rate": target})
            self.assertIs(self._fit(request), request)

    def test_rate_neutral_operations_are_exempt(self):
        # Measurement, output-mode switch and graph repair carry the current
        # committed rate by contract and are not rate-targeted.
        for operation in ("measurement-entry", "measurement-restore", "output-mode-switch", "graph-reconcile"):
            request = _with_rates(_request(768000, operation=operation), [44100])
            self.assertIs(self._fit(request), request)


class RateCapabilityGuardTransitionTests(unittest.IsolatedAsyncioTestCase):
    """Unsupported rates run at the fallback rate; only the cap rejects."""

    async def test_384_khz_target_on_192_khz_device_runs_at_192_khz(self):
        runtime = _FakeRuntime(supported_rates=[44100, 48000, 96000, 192000])
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        result = await coordinator.execute(_request(384000))
        self.assertTrue(result.committed)
        self.assertEqual(runtime.rate, 192000)
        self.assertIn("rate", runtime.events)
        self.assertIn("effects-helper-links", runtime.events)
        self.assertNotIn("pause-after-failure", runtime.events)

    async def test_96_khz_target_on_48_khz_card_runs_at_48_khz(self):
        runtime = _FakeRuntime(supported_rates=[44100, 48000])
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        result = await coordinator.execute(_request(96000))
        self.assertTrue(result.committed)
        self.assertEqual(runtime.rate, 48000)
        self.assertNotIn("pause-after-failure", runtime.events)

    async def test_768_khz_target_without_overview_is_rejected_by_fxroute_cap(self):
        runtime = _FakeRuntime(supported_rates=None)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        with self.assertRaises(UnsupportedTransitionRateError):
            await coordinator.execute(_request(768000))
        # Only the snapshot is read; no gate close, no quiet, no rate switch,
        # no effects/helper stage may run.
        self.assertEqual(runtime.events, ["snapshot"])

    async def test_supported_rate_flows_through_normal_transition_path(self):
        for target in (192000, 384000, 96000):
            runtime = _FakeRuntime(supported_rates=[44100, 48000, 96000, 192000, 384000])
            coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
            result = await coordinator.execute(_request(target))
            self.assertTrue(result.committed)
            self.assertEqual(runtime.rate, target)
            self.assertIn("rate", runtime.events)
            self.assertIn("effects-helper-links", runtime.events)
            self.assertNotIn("pause-after-failure", runtime.events)


if __name__ == "__main__":
    unittest.main()
