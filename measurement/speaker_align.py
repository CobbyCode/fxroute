# SPDX-License-Identifier: AGPL-3.0-only
"""Pure Speaker Align evidence gates and start-relative delay proposals.

Captures must contain full-resolution unshifted deconvolved IRs and the existing
analyzer's quality/reference/direct-arrival metadata. Stored peak-zeroed previews
are not suitable evidence. The capture adapter must attest the actual common
upstream reference and fixed microphone position, not infer them from a bank name.

This boundary does not capture, stage or persist. Its candidate still needs a
guarded runtime apply, combined acoustic confirmation and revision-checked commit
under measurement ownership. A good predicted sum is not that confirmation.
"""

from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import Callable, Sequence

import numpy as np

from audio.output_state import validate_output_state
from measurement.constants import CAPTURE_CLIP_FAIL_DBFS
from measurement.hybrid import build_complex_response
from measurement.target import REFERENCE_TAP_INGRESS, freeze_measurement_target, target_output_mask


# Conservative proposal gates, pending qualification on real 2/3-way captures.
MIN_CONFIDENCE = 0.75
MIN_OVERLAP_OCTAVES = 1.0 / 3.0
MIN_RESPONSE_RELATIVE_DB = -24.0
MAX_WAY_IMBALANCE_DB = 12.0
MAX_PHASE_RMS_DEGREES = 45.0
MAX_RESIDUAL_TIMING_CYCLES = 0.1
MIN_SUM_DB = -3.0
MAX_SUM_REGRESSION_DB = 0.5


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


def _sum_db(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    relative = np.abs(left + right) / (np.abs(left) + np.abs(right))
    return 20.0 * np.log10(np.maximum(relative, 1e-12))


class SpeakerAlignment:
    """Freeze one speaker's analysis context; never rebase or accumulate proposals.

    ``fingerprint`` must describe ``state`` at the given device/rate. The caller
    supplies a freshly frozen Global ``live_target`` to each proposal and must
    enforce the same context again at the later staging/commit boundaries.
    Reference and microphone-position IDs belong to the acquisition session.
    """

    def __init__(self, state: dict, *, side: str, output_key: str, channels: int,
                 sample_rate_hz: int, fingerprint: str, reference_id: str,
                 microphone_position_id: str):
        if side not in ("left", "right"):
            raise ValueError("Speaker Align side must be left or right")
        self._state = validate_output_state(state)
        if self._state["active_mode"] != "crossover":
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
        processing = self._state["modes"]["crossover"]["processing"]
        self._bands = []
        for low_role, high_role in zip(self._roles, self._roles[1:]):
            lowpass = processing[low_role]["lowpass"]
            highpass = processing[high_role]["highpass"]
            if lowpass is None or highpass is None:
                raise ValueError("Speaker Align overlap requires adjacent lowpass and highpass filters")
            cutoffs = (lowpass["frequency_hz"], highpass["frequency_hz"])
            if max(cutoffs) >= sample_rate_hz / 2:
                raise ValueError("Speaker Align crossover must be below Nyquist")
            lower = max(cutoffs) / math.sqrt(2)
            upper = min(min(cutoffs) * math.sqrt(2), sample_rate_hz * 0.45)
            if upper / lower < 2 ** MIN_OVERLAP_OCTAVES:
                raise ValueError("Speaker Align crossover filters have no usable overlap band")
            self._bands.append((low_role, high_role, np.geomspace(lower, upper, 64)))

    def capture_requests(self) -> list[dict]:
        """Serial per-way acquisition requests, in canonical low-to-high order."""
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
            raise ValueError("Speaker Align capture quality is not usable")
        if _finite(analysis.get("peak_dbfs"), "microphone peak") >= CAPTURE_CLIP_FAIL_DBFS:
            raise ValueError("Speaker Align microphone capture clipped")
        reference = analysis.get("reference_path") or {}
        if (reference.get("clipped") is not False
                or _finite(reference.get("peak_dbfs"), "reference peak") >= CAPTURE_CLIP_FAIL_DBFS):
            raise ValueError("Speaker Align reference capture clipped")
        if (reference.get("usable") is not True
                or reference.get("electrical_reference_used") is not True
                or reference.get("electrical_reference_fallback")
                or reference.get("timing_status") != "electrical-reference"
                or reference.get("stability") != "stable"
                or not MIN_CONFIDENCE <= _finite(reference.get("confidence"), "reference confidence") <= 1):
            raise ValueError("Speaker Align requires a stable, confident electrical reference")
        timing = analysis.get("impulse_response") or {}
        if (timing.get("timing_source") != "direct_arrival_minus_reference_peak"
                or not MIN_CONFIDENCE <= _finite(timing.get("direct_confidence"), "arrival confidence") <= 1):
            raise ValueError("Speaker Align direct arrival timing is not confident")
        arrival = timing.get("direct_arrival_index")
        origin = timing.get("reference_peak_index")
        if any(type(index) is not int or not 0 <= index < ir.size for index in (arrival, origin)):
            raise ValueError("Speaker Align arrival/reference timing indices must be inside the IR")
        if arrival < origin:
            raise ValueError("Speaker Align acoustic arrival precedes the upstream reference")
        return ir, arrival, origin

    def _overlap_check(self, low_role: str, high_role: str, frequencies: np.ndarray,
                       responses: dict, grid: np.ndarray, delays: dict) -> dict:
        indices = np.searchsorted(grid, frequencies)
        low = responses[low_role][indices]
        high = responses[high_role][indices]
        low_magnitude, high_magnitude = np.abs(low), np.abs(high)
        floor = 10 ** (MIN_RESPONSE_RELATIVE_DB / 20)
        supported = ((low_magnitude >= np.max(np.abs(responses[low_role])) * floor)
                     & (high_magnitude >= np.max(np.abs(responses[high_role])) * floor))
        ratio = low_magnitude / np.maximum(high_magnitude, 1e-30)
        comparable = (ratio >= 10 ** (-MAX_WAY_IMBALANCE_DB / 20)) & (ratio <= 10 ** (MAX_WAY_IMBALANCE_DB / 20))
        valid_indices = np.flatnonzero(supported & comparable)
        runs = np.split(valid_indices, np.flatnonzero(np.diff(valid_indices) != 1) + 1)
        usable = [run for run in runs if run.size >= 8
                  and frequencies[run[-1]] / frequencies[run[0]] >= 2 ** MIN_OVERLAP_OCTAVES]
        if not usable:
            raise ValueError(f"Speaker Align has no measured overlap for {low_role}/{high_role}")
        # Validate all materially supported bins, not only the most favorable run.
        frequencies = frequencies[valid_indices]
        low, high = low[valid_indices], high[valid_indices]
        after_low = low * np.exp(-2j * np.pi * frequencies * delays[low_role] / 1000)
        after_high = high * np.exp(-2j * np.pi * frequencies * delays[high_role] / 1000)
        phase = np.angle(after_low * np.conj(after_high))
        phase_rms = float(np.sqrt(np.mean(phase ** 2)) * 180 / np.pi)
        before_db = float(np.median(_sum_db(low, high)))
        after_values = _sum_db(after_low, after_high)
        after_db = float(np.median(after_values))
        if phase_rms > MAX_PHASE_RMS_DEGREES:
            raise ValueError(f"Speaker Align overlap phase/polarity is ambiguous for {low_role}/{high_role}")
        # A half-cycle timing error can mask a polarity reversal at crossover;
        # a full-cycle error can look aligned in a narrow, steep overlap. Both
        # retain a phase slope across frequency even when RMS phase and sum pass.
        # Fit each contiguous usable band independently: never unwrap over gaps.
        residual_delays = []
        for run in usable:
            positions = np.searchsorted(valid_indices, run)
            run_frequencies = frequencies[positions]
            unwrapped = np.unwrap(phase[positions])
            centered = run_frequencies - np.mean(run_frequencies)
            slope = float(np.dot(centered, unwrapped - np.mean(unwrapped)) / np.dot(centered, centered))
            residual_seconds = -slope / (2 * np.pi)
            center_hz = math.sqrt(run_frequencies[0] * run_frequencies[-1])
            tolerance_seconds = max(2 / self._rate, MAX_RESIDUAL_TIMING_CYCLES / center_hz)
            if abs(residual_seconds) > tolerance_seconds:
                raise ValueError(f"Speaker Align residual phase timing is ambiguous for {low_role}/{high_role}")
            residual_delays.append(residual_seconds * 1000)
        if float(np.percentile(after_values, 10)) < MIN_SUM_DB or after_db < before_db - MAX_SUM_REGRESSION_DB:
            raise ValueError(f"Speaker Align predicted combined response deteriorates for {low_role}/{high_role}")
        return {"roles": [low_role, high_role], "lower_hz": float(frequencies[0]),
                "upper_hz": float(frequencies[-1]), "phase_rms_degrees": phase_rms,
                "residual_delay_ms": max(residual_delays, key=abs),
                "before_sum_db": before_db, "after_sum_db": after_db}

    def propose(self, captures: Sequence[dict], *, live_target: dict,
                cancel_requested: Callable[[], bool] | None = None) -> dict:
        """Return an in-memory candidate after every timing/phase/stale gate passes.

        Captures include existing DSP delays, so ``max(t)-t`` is ADDED to each
        frozen way's alignment. Other ways, subs, banks, bass and Global retain
        their exact start values. Out-of-range alignment is rejected, not clamped.
        """
        _check_cancel(cancel_requested)
        self._require_live_target(live_target)
        roles = [capture.get("role") for capture in captures]
        if len(roles) != len(self._roles) or set(roles) != set(self._roles):
            raise ValueError("Speaker Align requires exactly one capture of every speaker way")
        by_role = {capture["role"]: capture for capture in captures}
        evidence = {}
        arrivals = {}
        for request in self._requests:
            _check_cancel(cancel_requested)
            role = request["role"]
            evidence[role] = self._capture_evidence(by_role[role], request)
            _, arrival, origin = evidence[role]
            arrivals[role] = (arrival - origin) * 1000.0 / self._rate
        latest = max(arrivals.values())
        delays = {role: latest - arrival for role, arrival in arrivals.items()}
        candidate = copy.deepcopy(self._state)
        for role, delay in delays.items():
            candidate["modes"]["crossover"]["processing"][role]["alignment_ms"] += delay
        candidate = validate_output_state(candidate)
        grid = np.unique(np.concatenate([
            np.geomspace(20, self._rate * 0.45, 192), *(band for _, _, band in self._bands),
        ]))
        responses = {}
        for role, (ir, arrival, origin) in evidence.items():
            _check_cancel(cancel_requested)
            response = build_complex_response(
                ir, self._rate, frequencies_hz=grid, reference_sample=origin,
                direct_arrival_sample=arrival,
            )
            responses[role] = np.array([complex(point[1], point[2]) for point in response["points"]])
        checks = []
        for low_role, high_role, band in self._bands:
            _check_cancel(cancel_requested)
            checks.append(self._overlap_check(low_role, high_role, band, responses, grid, delays))
        _check_cancel(cancel_requested)
        self._require_live_target(live_target)
        return {"candidate_state": candidate, "arrival_ms": arrivals, "added_delay_ms": delays,
                "overlap_checks": checks, "start_revision": self._target["revision"],
                "processing_fingerprint": self._target["processing_fingerprint"]}
