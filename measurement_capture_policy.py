"""Retry and reference-fallback policy for host measurement captures."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

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
