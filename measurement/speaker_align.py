# SPDX-License-Identifier: AGPL-3.0-only
"""One speaker's start-relative timing proposal from unshifted capture evidence."""

from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import Callable, Sequence

import numpy as np

from audio.output_state import validate_output_state
from measurement.constants import CAPTURE_CLIP_FAIL_DBFS
from measurement.target import REFERENCE_TAP_INGRESS, freeze_measurement_target, target_output_mask

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
    """Admit a stable electrical reference or the simultaneously recorded ingress."""
    reference = analysis.get("reference_path") or {}
    electrical = (reference.get("usable") is True
                  and reference.get("electrical_reference_used") is True
                  and reference.get("timing_status") == "electrical-reference"
                  and reference.get("stability") == "stable")
    host = (reference_node == REFERENCE_TAP_INGRESS
            and reference.get("electrical_reference_used") is False
            and reference.get("timing_status") == "acoustic-only"
            and reference.get("stability") == "host-reference"
            and reference.get("capture_mode") == "dual-channel"
            and reference.get("timing_applied_to_mic") is True
            and _finite(reference.get("start_score"), "reference start score") >= 0.90
            and _finite(reference.get("end_score"), "reference end score") >= 0.90
            and _finite(reference.get("ir_sharpness_db"), "reference sharpness") >= 18.0)
    if (reference.get("electrical_reference_fallback")
            or not (electrical or host)
            or not MIN_CONFIDENCE <= _finite(reference.get("confidence"), "reference confidence") <= 1):
        raise ValueError("Speaker Align requires a stable, confident upstream reference")


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

    def capture_requests(self) -> list[dict]:
        """Serial per-way requests in configured low-to-high role order."""
        return copy.deepcopy(self._requests)

    def _require_live_target(self, live_target: dict) -> None:
        if live_target != self._target:
            raise ValueError("Speaker Align target is stale; revision, device and processing must match")

    def _capture_evidence(self, capture: dict, request: dict) -> tuple[np.ndarray, int, int]:
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
        timing = analysis.get("impulse_response") or {}
        if (timing.get("timing_source") != "direct_arrival_minus_reference_peak"
                or not MIN_CONFIDENCE <= _finite(timing.get("direct_confidence"), "arrival confidence") <= 1):
            raise ValueError(f"Speaker Align direct arrival timing is not confident for {role}")
        arrival = timing.get("direct_arrival_index")
        origin = timing.get("reference_peak_index")
        if any(type(index) is not int or not 0 <= index < ir.size for index in (arrival, origin)):
            raise ValueError("Speaker Align arrival/reference timing indices must be inside the IR")
        if arrival < origin:
            raise ValueError("Speaker Align acoustic arrival precedes the upstream reference")
        return ir, arrival, origin

    def propose(self, captures: Sequence[dict], *, live_target: dict,
                cancel_requested: Callable[[], bool] | None = None) -> dict:
        """Add max(arrival)-arrival delays and equalize way gains in passbands.

        Timing stays level-independent (arrival detection only). Gain is the
        robust median level inside each way's usable crossover passband, never
        a single point and never total energy across differently wide ways.
        Captures without response points keep levels unchanged (legacy unit
        shape); captures with points on every way propose start-relative
        level corrections equalizing the side to its median way level.
        Polarity is never altered.
        """
        from measurement.alignment_backend import estimate_way_level, propose_way_gains, way_passband
        _check_cancel(cancel_requested)
        self._require_live_target(live_target)
        roles = [capture.get("role") for capture in captures]
        if len(roles) != len(self._roles) or set(roles) != set(self._roles):
            raise ValueError("Speaker Align requires exactly one capture of every speaker way")
        by_role = {capture["role"]: capture for capture in captures}
        arrivals = {}
        for request in self._requests:
            _check_cancel(cancel_requested)
            role = request["role"]
            _, arrival, origin = self._capture_evidence(by_role[role], request)
            arrivals[role] = (arrival - origin) * 1000.0 / self._rate
        reference_role = max(arrivals, key=arrivals.get)
        latest = arrivals[reference_role]
        delays = {role: latest - arrival for role, arrival in arrivals.items()}
        candidate = copy.deepcopy(self._state)
        for role, delay in delays.items():
            candidate["modes"][candidate["active_mode"]]["processing"][role]["alignment_ms"] += delay
        mode = candidate["active_mode"]
        processing = self._state["modes"][mode]["processing"]
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
                passband = way_passband(processing[role], sample_rate_hz=self._rate)
                estimate = estimate_way_level(by_role[role], passband,
                                              processing=processing[role],
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
                "reference_role": reference_role, "start_revision": self._target["revision"],
                "processing_fingerprint": self._target["processing_fingerprint"]}
