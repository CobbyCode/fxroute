# SPDX-License-Identifier: AGPL-3.0-only
"""One speaker's start-relative timing proposal from unshifted capture evidence.

The relative way delays are planned from one shared take of the side: every way
plays at once, the rest of the plan muted, and each way's arrival is read from
its own isolated band on that take's single capture time base. Planning every
way from its own take instead would decide the delay from one direct-arrival
pick per way, which is a property of that take's noise and lobe structure
rather than of the way's arrival. ``planning_request``/``planning`` produce and
read that take; ``verification_request``/``confirmation`` do the same for the
post-apply proof, unchanged.
"""

from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import Callable, Sequence

import numpy as np

from audio.output_state import validate_output_state
from dsp.processing_plan import rendered_crossover_filters
from measurement.constants import CAPTURE_CLIP_FAIL_DBFS
from measurement.speaker_verification import side_confirmation
from measurement.target import (
    REFERENCE_TAP_INGRESS,
    freeze_measurement_target,
    narrow_measured_target,
    target_output_mask,
)

MIN_CONFIDENCE = 0.75


def _finite(value: object, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"Speaker Align {label} must be finite")
    return float(value)


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"Speaker Align requires a {label} identity")
    return value


def _check_cancel(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise asyncio.CancelledError("Speaker Align was cancelled")


def require_timing_reference(analysis: dict, reference_node: str | None = None) -> None:
    """Admit a store-accepted electrical reference or the simultaneously recorded ingress.

    The electrical branch defers to the store's own verdict
    (``MeasurementStore._evaluate_electrical_reference_status`` and its
    tolerated active-2.2 twin): ``usable`` plus ``electrical_reference_used``
    plus the electrical timing status is the admission, whatever stability
    mark the store recorded.  No extra fixed confidence floor is applied there:
    the store derived ``confidence = min(alignment_score, sharpness/60)`` from
    its own 0.84 / 18 dB admission, so re-demanding 0.75 would implicitly
    require 45 dB sharpness and reject every band-limited per-way reference the
    store already accepted.  The ingress branch keeps its confidence gate:
    there confidence is the arrival-detection score on a different scale.
    """
    reference = analysis.get("reference_path") or {}
    electrical = (reference.get("usable") is True
                  and reference.get("electrical_reference_used") is True
                  and reference.get("timing_status") == "electrical-reference")
    host = (reference_node == REFERENCE_TAP_INGRESS
            and reference.get("electrical_reference_used") is False
            and reference.get("timing_status") == "acoustic-only"
            and reference.get("stability") == "host-reference"
            and reference.get("capture_mode") == "dual-channel"
            and reference.get("timing_applied_to_mic") is True
            and _finite(reference.get("start_score"), "reference start score") >= 0.90
            and _finite(reference.get("end_score"), "reference end score") >= 0.90
            and _finite(reference.get("ir_sharpness_db"), "reference sharpness") >= 18.0
            and MIN_CONFIDENCE <= _finite(reference.get("confidence"), "reference confidence") <= 1)
    if reference.get("electrical_reference_fallback") or not (electrical or host):
        raise ValueError("Speaker Align requires a stable, confident upstream reference")
    if electrical:
        # Sanity only: the store already decided the reference is usable.  A
        # missing or out-of-range confidence is malformed evidence, not a
        # band-limited one.
        if not 0.0 <= _finite(reference.get("confidence"), "reference confidence") <= 1:
            raise ValueError("Speaker Align reference confidence must be between 0 and 1")


class SpeakerAlignment:
    """Freeze one speaker's context and add only positive relative way delays."""

    def __init__(self, state: dict, *, side: str, output_key: str, channels: int,
                 sample_rate_hz: int, fingerprint: str, reference_id: str,
                 microphone_position_id: str):
        if side not in ("left", "right"):
            raise ValueError("Speaker Align side must be left or right")
        self._state = validate_output_state(state)
        config = self._state["modes"][self._state["active_mode"]]
        if not config["crossover_enabled"]:
            raise ValueError("Speaker Align requires crossover speaker ways")
        self._side = side
        self._reference_id = _identity(reference_id, "upstream reference")
        self._position_id = _identity(microphone_position_id, "microphone position")
        context = dict(output_key=output_key, channels=channels,
                       sample_rate_hz=sample_rate_hz, fingerprint=fingerprint)
        self._target = freeze_measurement_target(self._state, bank_id="global", **context)
        self._rate = sample_rate_hz
        self._roles = [role for role in self._target["roles"] if role.startswith(f"{side}_")]
        self._requests = []
        for role in self._roles:
            target = freeze_measurement_target(self._state, bank_id=role, **context)
            self._requests.append({
                "role": role, "channel": side, "measurement_target": target,
                "output_mask": target_output_mask(target, roles=self._target["roles"]),
                "reference_id": self._reference_id, "microphone_position_id": self._position_id,
                "reference_tap": REFERENCE_TAP_INGRESS,
            })
        for low_role, high_role in zip(self._roles, self._roles[1:]):
            lowpass = config["processing"][low_role]["lowpass"]
            highpass = config["processing"][high_role]["highpass"]
            if lowpass is None or highpass is None:
                raise ValueError("Speaker Align requires adjacent lowpass and highpass filters")
            if max(lowpass["frequency_hz"], highpass["frequency_hz"]) >= sample_rate_hz / 2:
                raise ValueError("Speaker Align crossover must be below Nyquist")
        # Band isolation and passband levels model each way with every filter
        # the plan renders on it, the bass-management Main high-pass included.
        rendered = rendered_crossover_filters(self._state, output_key=output_key, channels=channels)
        self._way_models = {role: {"crossover": rendered[role]} for role in self._roles}

    def capture_requests(self) -> list[dict]:
        """Serial per-way requests in configured low-to-high role order."""
        return copy.deepcopy(self._requests)

    def way_models(self) -> dict:
        """Rendered crossover of every way of this side, keyed by role."""
        return copy.deepcopy(self._way_models)

    def _side_take_request(self, label: str) -> dict:
        """One shared take of this side: every way of the side plays at once.

        The rest of the plan stays muted, so the take carries one acoustic time
        base for the whole side.  The target narrows to this side's roles;
        nothing here maps a side, a way or an output to a loopback channel.
        """
        if len(self._roles) < 2:
            raise ValueError(f"Speaker Align {label} needs at least two speaker ways")
        target = narrow_measured_target(self._target, roles=self._roles)
        return {
            "side": self._side,
            "channel": self._side,
            "roles": list(self._roles),
            "measurement_target": target,
            "output_mask": target_output_mask(target, roles=target["roles"]),
            "reference_id": self._reference_id,
            "microphone_position_id": self._position_id,
            "reference_tap": REFERENCE_TAP_INGRESS,
        }

    def planning_request(self) -> dict:
        """The one shared planning take of this side.

        The relative way delays are planned from a single take in which every
        way of the side plays at once, so the low/high offset comes from one
        capture time base instead of from one direct-arrival decision per way.
        """
        return self._side_take_request("planning")

    def verification_request(self) -> dict:
        """The one shared verification take of this side."""
        return self._side_take_request("verification")

    def _side_take(self, take: object, *, request: dict, label: str,
                   require_electrical: bool = False) -> None:
        """Gate one shared side take: identity, quality, reference, no clipping.

        The same gate for planning and verification, so both read the same kind
        of evidence: the frozen target and reference, the microphone position,
        an unshifted deconvolved sweep, usable quality, no clipping and a
        reference the store admits. ``require_electrical`` is the verification
        gate: the post-apply proof needs the electrical reference, while the
        planning take keeps the per-way admission, where a host/ingress monitor
        reference is legitimate evidence for a microphone-only setup.
        """
        if not isinstance(take, dict):
            raise ValueError(f"Speaker Align {label} requires one shared take")
        if take.get("measurement_target") != request["measurement_target"]:
            raise ValueError(f"Speaker Align {label} take measured different roles or processing")
        if (take.get("reference_id") != self._reference_id
                or take.get("reference_tap") != REFERENCE_TAP_INGRESS):
            raise ValueError(f"Speaker Align {label} take lost its upstream reference")
        if take.get("microphone_position_id") != self._position_id:
            raise ValueError(f"Speaker Align {label} microphone position changed")
        if take.get("time_reference") != "deconvolved-sweep-origin":
            raise ValueError(
                f"Speaker Align {label} IR origin must be the unshifted deconvolved sweep")
        analysis = take.get("analysis") or {}
        if analysis.get("sample_rate") != self._rate:
            raise ValueError(f"Speaker Align {label} capture sample rate changed")
        quality = analysis.get("quality_checks") or {}
        if (quality.get("status") not in ("pass", "warn")
                or not isinstance(quality.get("items"), list)
                or any(item.get("level") == "error" for item in quality["items"])):
            raise ValueError(f"Speaker Align {label} capture quality is not usable")
        if _finite(analysis.get("peak_dbfs"), f"{label} microphone peak") >= CAPTURE_CLIP_FAIL_DBFS:
            raise ValueError(f"Speaker Align {label} microphone capture clipped")
        reference = analysis.get("reference_path") or {}
        if (reference.get("clipped") is not False
                or _finite(reference.get("peak_dbfs"), f"{label} reference peak") >= CAPTURE_CLIP_FAIL_DBFS):
            raise ValueError(f"Speaker Align {label} reference capture clipped")
        if require_electrical and reference.get("electrical_reference_used") is not True:
            raise ValueError(f"Speaker Align {label} requires the electrical reference")
        require_timing_reference(analysis, take.get("reference_node"))

    def _side_arrivals(self, take: dict) -> dict:
        """Band-limited way arrivals of one shared take, on its one time base.

        The take's microphone calibration corrects its band levels, so they
        compare with the per-way levels the gain proposal reads.
        """
        return side_confirmation(
            impulse_response=take.get("impulse_response"),
            processing=self._way_models,
            roles=self._roles,
            sample_rate_hz=self._rate,
            start_revision=self._target["revision"],
            processing_fingerprint=self._target["processing_fingerprint"],
            calibration_curve=take.get("calibration_curve"),
        )

    def planning(self, take: dict) -> dict:
        """Read the side's relative way arrivals from the shared planning take.

        Returns the same document shape as ``confirmation``: band-limited
        arrivals of every way of this side on the take's single capture time
        base, plus the isolation margin each arrival had against the neighbours
        in the same band. Nothing here decides whether the take is good enough
        to plan from; ``propose`` gates the isolation, like
        ``verify_confirmation`` gates the confirmation's.
        """
        request = self.planning_request()
        self._side_take(take, request=request, label="planning")
        return self._side_arrivals(take)

    def confirmation(self, take: dict) -> dict:
        """Judge one shared verification take of this side acoustically.

        The residual comes from the side's own band-limited arrivals on that
        take's single time base -- never from a per-way reference peak, which
        moves with the delay under test.
        """
        request = self.verification_request()
        self._side_take(take, request=request, label="verification", require_electrical=True)
        return self._side_arrivals(take)

    def _require_live_target(self, live_target: dict) -> None:
        if live_target != self._target:
            raise ValueError("Speaker Align target is stale; revision, device and processing must match")

    def _require_way_capture(self, capture: dict, request: dict) -> None:
        """Gate one way's own capture: target, reference, quality, no clipping.

        Timing is deliberately not read here any more.  The relative way delays
        come from the shared planning take, so a band-limited way's per-take
        direct-arrival estimate can neither veto the alignment nor move the
        planned delay: its own early candidates are a lobe decision of that one
        take, not the way's arrival on the side's common time base.
        """
        role = request["role"]
        if capture.get("measurement_target") != request["measurement_target"]:
            raise ValueError(f"Speaker Align capture target differs for {role}")
        if (capture.get("reference_id") != self._reference_id
                or capture.get("reference_tap") != REFERENCE_TAP_INGRESS):
            raise ValueError("Speaker Align captures require the same actual upstream reference")
        if capture.get("microphone_position_id") != self._position_id:
            raise ValueError("Speaker Align microphone position changed")
        if capture.get("time_reference") != "deconvolved-sweep-origin":
            raise ValueError("Speaker Align IR origin must be the unshifted deconvolved sweep")
        ir = np.asarray(capture.get("impulse_response"), dtype=np.float64)
        if ir.ndim != 1 or ir.size < 64 or not np.all(np.isfinite(ir)) or not np.any(ir):
            raise ValueError("Speaker Align IR must be finite, non-silent and full resolution")
        analysis = capture.get("analysis") or {}
        if analysis.get("sample_rate") != self._rate:
            raise ValueError("Speaker Align capture sample rate changed")
        quality = analysis.get("quality_checks") or {}
        if (quality.get("status") not in ("pass", "warn")
                or not isinstance(quality.get("items"), list)
                or any(item.get("level") == "error" for item in quality["items"])):
            raise ValueError(f"Speaker Align capture quality is not usable for {role}")
        if _finite(analysis.get("peak_dbfs"), "microphone peak") >= CAPTURE_CLIP_FAIL_DBFS:
            raise ValueError("Speaker Align microphone capture clipped")
        reference = analysis.get("reference_path") or {}
        if (reference.get("clipped") is not False
                or _finite(reference.get("peak_dbfs"), "reference peak") >= CAPTURE_CLIP_FAIL_DBFS):
            raise ValueError("Speaker Align reference capture clipped")
        require_timing_reference(analysis, capture.get("reference_node"))

    def _planning_arrivals(self, planning: object, *, cancel_requested=None) -> dict:
        """Per-way arrivals of the shared planning take, or fail closed.

        An arrival from a band the side's neighbouring way owns is not this
        way's arrival, so a take that cannot separate the ways must abort the
        plan instead of guessing a delay from it. Two arrivals inside one lobe
        are one event and report no margin; that is the aligned case, not an
        ambiguity.
        """
        from measurement.speaker_verification import MIN_WAY_ISOLATION_DB
        if not isinstance(planning, dict):
            raise ValueError("Speaker Align planning requires one shared planning take")
        roles = set(self._roles)
        arrivals = planning.get("arrival_ms")
        isolation = planning.get("way_isolation_db")
        if not isinstance(arrivals, dict) or set(arrivals) != roles:
            raise ValueError("Speaker Align planning take must carry exactly this side's ways")
        if not isinstance(isolation, dict) or set(isolation) != roles:
            raise ValueError("Speaker Align planning take carries no isolation evidence")
        margins = []
        for role in self._roles:
            _check_cancel(cancel_requested)
            margin = isolation[role]
            if margin is not None:
                margins.append((role, _finite(margin, "planning way isolation")))
            _finite(arrivals[role], "planning arrival")
        weakest = min(margins, key=lambda item: item[1]) if margins else None
        if weakest is not None and weakest[1] < MIN_WAY_ISOLATION_DB:
            raise ValueError(
                f"Speaker Align planning take cannot separate the ways: "
                f"{weakest[0]} isolation {weakest[1]:.3f} dB is below "
                f"{MIN_WAY_ISOLATION_DB:.3f} dB"
            )
        return {role: float(arrivals[role]) for role in self._roles}

    def propose(self, captures: Sequence[dict], *, planning: dict, live_target: dict,
                cancel_requested: Callable[[], bool] | None = None) -> dict:
        """Add max(arrival)-arrival delays and equalize way gains in passbands.

        The relative way delays come from one shared planning take of this side
        (``planning_request`` / ``planning``): both ways play into one capture
        time base and each arrival is read from its own isolated band, so the
        plan cannot be decided by a per-take direct-arrival pick, and a take
        that cannot separate the ways plans nothing. The per-way captures keep
        their job as level, reference-quality and provenance evidence; their
        own direct-arrival metadata is never read.  Timing stays
        level-independent (arrival detection only). Gain is the robust median
        level inside each way's usable crossover passband, never a single point
        and never total energy across differently wide ways.  Captures without
        response points keep levels unchanged (legacy unit shape); captures
        with points on every way propose start-relative level corrections
        equalizing the side to its median way level. Polarity is never altered.
        """
        from measurement.alignment_backend import estimate_way_level, propose_way_gains, way_passband
        _check_cancel(cancel_requested)
        self._require_live_target(live_target)
        roles = [capture.get("role") for capture in captures]
        if len(roles) != len(self._roles) or set(roles) != set(self._roles):
            raise ValueError("Speaker Align requires exactly one capture of every speaker way")
        by_role = {capture["role"]: capture for capture in captures}
        for request in self._requests:
            _check_cancel(cancel_requested)
            self._require_way_capture(by_role[request["role"]], request)
        arrivals = self._planning_arrivals(planning, cancel_requested=cancel_requested)
        isolation = {
            role: planning["way_isolation_db"][role] for role in self._roles
        }
        reference_role = max(arrivals, key=arrivals.get)
        latest = arrivals[reference_role]
        delays = {role: latest - arrival for role, arrival in arrivals.items()}
        candidate = copy.deepcopy(self._state)
        for role, delay in delays.items():
            candidate["modes"][candidate["active_mode"]]["processing"][role]["alignment_ms"] += delay
        mode = candidate["active_mode"]
        point_shapes = []
        for role in self._roles:
            analysis = (by_role[role].get("analysis") or {})
            has_points = isinstance(analysis.get("review_points"), list) or isinstance(
                analysis.get("trusted_points"), list)
            point_shapes.append(has_points)
        way_levels: dict[str, float] = {}
        added_gains: dict[str, float] = {}
        if any(point_shapes) and not all(point_shapes):
            raise ValueError("Speaker Align gain needs response points on every way or none")
        if all(point_shapes):
            measured = {}
            for role in self._roles:
                _check_cancel(cancel_requested)
                model = self._way_models[role]
                passband = way_passband(model, sample_rate_hz=self._rate)
                estimate = estimate_way_level(by_role[role], passband, processing=model,
                                              sample_rate_hz=self._rate)
                measured[role] = estimate["level_db"]
            way_levels = dict(measured)
            added_gains = propose_way_gains(measured)
            for role, correction in added_gains.items():
                candidate["modes"][mode]["processing"][role]["level_db"] += correction
        else:
            for role in self._roles:
                way_levels[role] = 0.0
                added_gains[role] = 0.0
        candidate = validate_output_state(candidate)
        _check_cancel(cancel_requested)
        return {"candidate_state": candidate, "arrival_ms": arrivals, "added_delay_ms": delays,
                "way_levels_db": way_levels, "added_gain_db": added_gains,
                "reference_role": reference_role, "arrival_source": "shared-planning-take",
                "planning_isolation_db": isolation,
                "start_revision": self._target["revision"],
                "processing_fingerprint": self._target["processing_fingerprint"]}
