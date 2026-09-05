"""Retry and reference-fallback policy for host measurement captures."""

from __future__ import annotations

import logging
import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from measurement.hybrid import DIRECT_GATE_REPEAT_MAX_SPREAD_OCTAVES

logger = logging.getLogger(__name__)


@dataclass
class CapturePolicyResult:
    analysis: dict[str, Any] | None
    capture_info: dict[str, Any] | None
    playback_info: dict[str, Any] | None
    attempts_used: int
    final_capture_level_low: bool
    mic_auto_boosted: bool
    reference_warning: str


class MeasurementCapturePolicyRunner:
    """Sequence capture attempts without owning capture or QC implementation."""

    def __init__(
        self,
        *,
        capture_attempt: Callable[..., tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
        is_cancelled: Callable[[str, Exception | None], bool],
        evaluate_electrical_reference: Callable[[dict[str, Any]], dict[str, Any]],
        should_keep_electrical_reference: Callable[[dict[str, Any], str], bool],
        mark_electrical_reference_usable: Callable[[dict[str, Any], str], None],
        append_reference_fallback_warning: Callable[[dict[str, Any] | None, str], None],
        analysis_has_warning: Callable[[dict[str, Any] | None, str], bool],
        try_raise_mic: Callable[..., bool],
        cancel_aware_sleep: Callable[[str, float], None],
        retry_sleep: Callable[[float], None],
        should_retry_host_capture: Callable[[Exception], bool],
        max_attempts: int,
        retry_delay: float,
    ):
        self._capture_attempt = capture_attempt
        self._is_cancelled = is_cancelled
        self._evaluate_electrical_reference = evaluate_electrical_reference
        self._should_keep_electrical_reference = should_keep_electrical_reference
        self._mark_electrical_reference_usable = mark_electrical_reference_usable
        self._append_reference_fallback_warning = append_reference_fallback_warning
        self._analysis_has_warning = analysis_has_warning
        self._try_raise_mic = try_raise_mic
        self._cancel_aware_sleep = cancel_aware_sleep
        self._retry_sleep = retry_sleep
        self._should_retry_host_capture = should_retry_host_capture
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay

    def run(
        self,
        *,
        job_id: str,
        owner_job_id: str,
        use_electrical_reference: bool,
        electrical_reference: dict[str, Any] | None,
        host_reference: dict[str, Any],
        capture_channels: int,
        electrical_reference_channel_index: int | None,
        mic_target: str,
        measurement_scope: str = "",
        reference_warning: str = "",
        capture_kwargs: dict[str, Any] | None = None,
    ) -> CapturePolicyResult:
        capture_kwargs = dict(capture_kwargs or {})
        analysis = None
        capture_info = None
        playback_info = None
        attempts_used = 0
        final_capture_level_low = False
        mic_auto_boosted = False

        if self._is_cancelled(owner_job_id, None):
            logger.warning(
                "MEASUREMENT-CANCEL-DIAG retry aborted before first attempt: job_id=%s",
                job_id,
            )
            raise RuntimeError("Measurement cancelled.")

        for attempt_index in range(self._max_attempts):
            attempts_used = attempt_index + 1
            logger.info(
                "Measurement attempt %d/%d starting: job_id=%s reference=%s",
                attempts_used,
                self._max_attempts,
                job_id,
                "electrical" if use_electrical_reference else "acoustic",
            )
            try:
                if self._is_cancelled(owner_job_id, None):
                    logger.warning(
                        "MEASUREMENT-CANCEL-DIAG retry aborted before capture attempt %d/%d: job_id=%s",
                        attempts_used,
                        self._max_attempts,
                        job_id,
                    )
                    raise RuntimeError("Measurement cancelled.")

                attempt_reference = electrical_reference if use_electrical_reference else host_reference
                attempt_reference_channel = (
                    electrical_reference_channel_index if use_electrical_reference else None
                )
                analysis, capture_info, playback_info = self._capture_attempt(
                    reference_capture=attempt_reference,
                    capture_channels=capture_channels,
                    electrical_reference_channel_index=attempt_reference_channel,
                    **capture_kwargs,
                )

                if use_electrical_reference:
                    reference_status = self._evaluate_electrical_reference(analysis)

                    if self._is_cancelled(owner_job_id, None):
                        logger.warning(
                            "MEASUREMENT-CANCEL-DIAG retry aborted before ER fallback: job_id=%s",
                            job_id,
                        )
                        raise RuntimeError("Measurement cancelled.")

                    if not reference_status["usable"]:
                        reference_warning = reference_status["warning"]
                        if self._should_keep_electrical_reference(analysis, measurement_scope):
                            self._mark_electrical_reference_usable(analysis, warning=reference_warning)
                            logger.warning(
                                "Electrical measurement reference marginal for %s but kept for active 2.2 DSP path: %s",
                                job_id,
                                reference_warning,
                            )
                            reference_warning = ""
                        else:
                            logger.warning(
                                "Electrical measurement reference rejected for %s: %s",
                                job_id,
                                reference_warning,
                            )
                            logger.info(
                                "ER fallback capture within attempt %d/%d: reference quality rejected, retrying with host timing",
                                attempts_used,
                                self._max_attempts,
                            )
                            analysis, capture_info, playback_info = self._capture_attempt(
                                reference_capture=host_reference,
                                capture_channels=2,
                                electrical_reference_channel_index=None,
                                **capture_kwargs,
                            )
                            self._append_reference_fallback_warning(analysis, reference_warning)

                final_capture_level_low = self._analysis_has_warning(analysis, "capture-level-low")
                if self._try_raise_mic(
                    analysis,
                    mic_target=mic_target,
                    attempt_index=attempt_index,
                    mic_auto_boosted=mic_auto_boosted,
                ):
                    mic_auto_boosted = True
                    self._retry_sleep(self._retry_delay)
                    continue
                break
            except Exception as exc:
                if self._is_cancelled(owner_job_id, exc):
                    logger.warning(
                        "MEASUREMENT-CANCEL-DIAG retry aborted because job cancelled: job_id=%s attempt=%d/%d exc=%s",
                        job_id,
                        attempts_used,
                        self._max_attempts,
                        exc,
                    )
                    raise RuntimeError("Measurement cancelled.") from exc

                if use_electrical_reference:
                    reference_warning = f"Electrical reference unavailable; used host monitor timing fallback ({exc})."
                    logger.warning(
                        "Electrical measurement reference failed for %s; falling back to host monitor timing: %s",
                        job_id,
                        exc,
                    )
                    logger.info(
                        "ER fallback capture within attempt %d/%d: capture exception, retrying with host timing",
                        attempts_used,
                        self._max_attempts,
                    )

                    if self._is_cancelled(owner_job_id, None):
                        logger.warning(
                            "MEASUREMENT-CANCEL-DIAG retry aborted before ER exception fallback: job_id=%s",
                            job_id,
                        )
                        raise RuntimeError("Measurement cancelled.")

                    try:
                        analysis, capture_info, playback_info = self._capture_attempt(
                            reference_capture=host_reference,
                            capture_channels=2,
                            electrical_reference_channel_index=None,
                            **capture_kwargs,
                        )
                        self._append_reference_fallback_warning(analysis, reference_warning)
                        final_capture_level_low = self._analysis_has_warning(analysis, "capture-level-low")
                        if self._try_raise_mic(
                            analysis,
                            mic_target=mic_target,
                            attempt_index=attempt_index,
                            mic_auto_boosted=mic_auto_boosted,
                        ):
                            mic_auto_boosted = True
                            self._retry_sleep(self._retry_delay)
                            continue
                        break
                    except Exception:
                        logger.warning(
                            "Electrical reference fallback capture also failed for %s",
                            job_id,
                            exc_info=True,
                        )
                        if self._is_cancelled(owner_job_id, None):
                            logger.warning(
                                "MEASUREMENT-CANCEL-DIAG retry aborted after ER fallback failed (job cancelled): job_id=%s",
                                job_id,
                            )
                            raise RuntimeError("Measurement cancelled.")

                if self._is_cancelled(owner_job_id, None):
                    logger.warning(
                        "MEASUREMENT-CANCEL-DIAG retry aborted: job_id=%s",
                        job_id,
                    )
                    raise RuntimeError("Measurement cancelled.")

                if attempt_index >= self._max_attempts - 1 or not self._should_retry_host_capture(exc):
                    raise RuntimeError(
                        f"Measurement failed after {attempts_used}/{self._max_attempts} attempts: {exc}"
                    ) from exc

                logger.info(
                    "MEASUREMENT-CANCEL-DIAG retry sleep: job_id=%s attempt=%d/%d delay=%.2f",
                    job_id,
                    attempts_used,
                    self._max_attempts,
                    self._retry_delay,
                )
                self._cancel_aware_sleep(owner_job_id, self._retry_delay)
                if self._is_cancelled(owner_job_id, None):
                    logger.warning(
                        "MEASUREMENT-CANCEL-DIAG retry aborted after sleep: job_id=%s",
                        job_id,
                    )
                    raise RuntimeError("Measurement cancelled.")

        return CapturePolicyResult(
            analysis=analysis,
            capture_info=capture_info,
            playback_info=playback_info,
            attempts_used=attempts_used,
            final_capture_level_low=final_capture_level_low,
            mic_auto_boosted=mic_auto_boosted,
            reference_warning=reference_warning,
        )

    def run_direct_pair(
        self,
        *,
        job_id: str,
        owner_job_id: str,
        use_electrical_reference: bool,
        electrical_reference: dict[str, Any] | None,
        host_reference: dict[str, Any],
        capture_channels: int,
        electrical_reference_channel_index: int | None,
        mic_target: str,
        measurement_scope: str = "",
        reference_warning: str = "",
        capture_kwargs: dict[str, Any] | None = None,
    ) -> CapturePolicyResult:
        """Capture two direct sweeps at one microphone position and validate the gate."""
        shared = {
            "job_id": job_id,
            "owner_job_id": owner_job_id,
            "use_electrical_reference": use_electrical_reference,
            "electrical_reference": electrical_reference,
            "host_reference": host_reference,
            "capture_channels": capture_channels,
            "electrical_reference_channel_index": electrical_reference_channel_index,
            "mic_target": mic_target,
            "measurement_scope": measurement_scope,
            "reference_warning": reference_warning,
            "capture_kwargs": dict(capture_kwargs or {}),
        }
        first = self.run(**shared)
        first_direct = (first.analysis or {}).get("direct_response")
        if not self._direct_gate_observation(first.analysis):
            check = self._pair_check("single-unusable", first.analysis, None, 0.0, "first sweep has no usable gate")
            if isinstance(first_direct, dict):
                first_direct = deepcopy(first_direct)
                first_direct["repeatability"] = check
                analysis = deepcopy(first.analysis)
                analysis["direct_response"] = first_direct
                return CapturePolicyResult(
                    analysis=analysis,
                    capture_info=first.capture_info,
                    playback_info=first.playback_info,
                    attempts_used=first.attempts_used,
                    final_capture_level_low=first.final_capture_level_low,
                    mic_auto_boosted=first.mic_auto_boosted,
                    reference_warning=first.reference_warning,
                )
            return first
        if self._is_cancelled(owner_job_id, None):
            logger.warning(
                "MEASUREMENT-CANCEL-DIAG direct repeat aborted before second sweep: job_id=%s",
                job_id,
            )
            raise RuntimeError("Measurement cancelled.")
        second = self.run(**shared)
        return self._combine_direct_pair(first, second)

    @staticmethod
    def _direct_gate_limit(analysis: dict[str, Any] | None) -> float | None:
        if not isinstance(analysis, dict):
            return None
        direct = analysis.get("direct_response")
        if not isinstance(direct, dict):
            return None
        if direct.get("usable") is not True or direct.get("status") != "ok":
            return None
        try:
            limit = float(direct.get("gated_direct_lower_limit_hz"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(limit) or limit <= 0:
            return None
        return limit

    @staticmethod
    def _direct_gate_observation(analysis: dict[str, Any] | None) -> dict[str, Any] | None:
        limit = MeasurementCapturePolicyRunner._direct_gate_limit(analysis)
        if limit is None or not isinstance(analysis, dict):
            return None
        direct = analysis["direct_response"]
        reference = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
        return {
            "gated_direct_lower_limit_hz": float(direct.get("gated_direct_lower_limit_hz")),
            "gate_end_index": direct.get("gate_end_index"),
            "direct_arrival_index": direct.get("direct_arrival_index"),
            "first_reflection_index": direct.get("first_reflection_index"),
            "direct_confidence": direct.get("direct_confidence"),
            "status": direct.get("status"),
            "sample_rate": analysis.get("sample_rate"),
            "reference_channel": reference.get("channel"),
        }

    @staticmethod
    def _pair_check(
        status: str,
        first: dict[str, Any] | None,
        second: dict[str, Any] | None,
        spread_octaves: float,
        detail: str,
    ) -> dict[str, Any]:
        observations = [
            item for item in (
                MeasurementCapturePolicyRunner._direct_gate_observation(first),
                MeasurementCapturePolicyRunner._direct_gate_observation(second),
            )
            if item is not None
        ]
        limits = sorted(
            item["gated_direct_lower_limit_hz"]
            for item in observations
            if isinstance(item.get("gated_direct_lower_limit_hz"), (int, float))
        )
        return {
            "method": "same-position-direct-repeat",
            "status": status,
            "spread_octaves": round(float(spread_octaves), 6),
            "lower_limit_range_hz": limits,
            "max_spread_octaves": DIRECT_GATE_REPEAT_MAX_SPREAD_OCTAVES,
            "observations": observations,
            "detail": detail,
        }

    def _combine_direct_pair(
        self,
        first: CapturePolicyResult,
        second: CapturePolicyResult,
    ) -> CapturePolicyResult:
        first_analysis = first.analysis or {}
        second_analysis = second.analysis or {}
        first_key = (
            first_analysis.get("sample_rate"),
            ((first_analysis.get("reference_path") or {}) if isinstance(first_analysis.get("reference_path"), dict) else {}).get("channel"),
        )
        second_key = (
            second_analysis.get("sample_rate"),
            ((second_analysis.get("reference_path") or {}) if isinstance(second_analysis.get("reference_path"), dict) else {}).get("channel"),
        )
        attempts_used = first.attempts_used + second.attempts_used
        level_low = first.final_capture_level_low or second.final_capture_level_low
        boosted = first.mic_auto_boosted or second.mic_auto_boosted
        warning = second.reference_warning or first.reference_warning
        first_limit = self._direct_gate_limit(first_analysis)
        second_limit = self._direct_gate_limit(second_analysis)
        if first_key != second_key or first_limit is None or second_limit is None:
            if first_limit is None:
                cause = "first sweep has no usable gate"
            elif second_limit is None:
                cause = "second sweep has no usable gate"
            else:
                cause = "timing reference changed between sweeps"
            return self._reject_direct_pair(
                first, second, cause, attempts_used, level_low, boosted, warning,
            )
        spread = abs(math.log2(first_limit / second_limit))
        if spread > DIRECT_GATE_REPEAT_MAX_SPREAD_OCTAVES:
            low, high = sorted([first_limit, second_limit])
            return self._reject_direct_pair(
                first, second,
                f"reflection-free interval disagreed ({low:.1f} Hz vs {high:.1f} Hz, "
                f"about {low:.0f}/{high:.0f} Hz)",
                attempts_used, level_low, boosted, warning,
            )
        selected = first if first_limit >= second_limit else second
        analysis = deepcopy(selected.analysis)
        direct = analysis["direct_response"]
        direct["repeatability"] = self._pair_check(
            "consistent", first_analysis, second_analysis, spread,
            f"gates agree within {DIRECT_GATE_REPEAT_MAX_SPREAD_OCTAVES:.3f} octaves",
        )
        return CapturePolicyResult(
            analysis=analysis,
            capture_info=selected.capture_info,
            playback_info=selected.playback_info,
            attempts_used=attempts_used,
            final_capture_level_low=level_low,
            mic_auto_boosted=boosted,
            reference_warning=warning,
        )

    def _reject_direct_pair(
        self,
        first: CapturePolicyResult,
        second: CapturePolicyResult,
        cause: str,
        attempts_used: int,
        level_low: bool,
        boosted: bool,
        warning: str,
    ) -> CapturePolicyResult:
        candidates = []
        for result in (first, second):
            limit = self._direct_gate_limit(result.analysis)
            if limit is not None:
                candidates.append((limit, result))
        if candidates:
            base = deepcopy(max(candidates, key=lambda item: item[0])[1].analysis)
            confidence = base["direct_response"].get("direct_confidence", 0)
        else:
            base = deepcopy(second.analysis or first.analysis or {})
            base.setdefault("direct_response", {})
            confidence = 0
        direct = base["direct_response"]
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            confidence_value = 0.0
        direct.update({
            "status": "reflection-ambiguous",
            "usable": False,
            "points": [],
            "direct_confidence": confidence_value,
            "retry_reason": (
                f"Repeated direct measurements at the same microphone position {cause}. "
                "Keep the microphone about 1 m from the speaker, increase its distance "
                "from nearby walls or objects, and repeat the direct measurement."
            ),
        })
        direct["repeatability"] = self._pair_check(
            "ambiguous" if candidates else "invalid",
            first.analysis, second.analysis, 0.0, cause,
        )
        if not candidates:
            direct["repeatability"]["spread_octaves"] = 0.0
        else:
            limits = direct["repeatability"]["lower_limit_range_hz"]
            if len(limits) == 2:
                direct["repeatability"]["spread_octaves"] = round(
                    abs(math.log2(limits[1] / limits[0])), 6,
                )
        return CapturePolicyResult(
            analysis=base,
            capture_info=second.capture_info,
            playback_info=second.playback_info,
            attempts_used=attempts_used,
            final_capture_level_low=level_low,
            mic_auto_boosted=boosted,
            reference_warning=warning,
        )
