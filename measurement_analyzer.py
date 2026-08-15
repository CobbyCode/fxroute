"""Pure measurement sweep and impulse-response analysis."""

from __future__ import annotations

import logging
import math
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np

from hybrid_measurement import analyze_direct_window, build_complex_response, build_gated_response
from measurement_constants import (
    CAPTURE_CLIP_FAIL_DBFS,
    DISPLAY_POINT_COUNT,
    EDGE_STABILITY_MAX_DELTA_DB,
    EDGE_STABILITY_MAX_SPAN_DB,
    EDGE_STABILITY_WINDOW_POINTS,
    IR_DEBUG_SEGMENT_ENABLED,
    IR_DEBUG_SEGMENT_RADIUS_SAMPLES,
    IR_DIRECT_CANDIDATE_FLOOR_RELATIVE,
    IR_DIRECT_CANDIDATE_LIMIT,
    IR_DIRECT_NEARBY_WINDOW_SECONDS,
    IR_DIRECT_PROMINENCE_REFERENCE,
    IR_DIRECT_PROMOTION_ENERGY_RATIO,
    IR_DIRECT_PROMOTION_SCORE_RATIO,
    IR_DIRECT_PROMOTION_SUPPORT_RATIO,
    IR_DIRECT_PROMOTION_WINDOW_SECONDS,
    IR_DIRECT_RELATIVE_THRESHOLD,
    IR_DIRECT_SEARCH_PRE_SECONDS,
    IR_DIRECT_SUPPORT_WINDOW_SECONDS,
    IR_DIRECT_THRESHOLD_EDGE_SAMPLES,
    IR_DIRECT_WEAK_EARLY_MIN_GAP_SAMPLES,
    IR_DIRECT_WEAK_EARLY_NEXT_RATIO,
    IR_DIRECT_WEAK_EARLY_RELATIVE,
    IR_WINDOW_FADE_SECONDS,
    IR_WINDOW_POST_HIGH_SECONDS,
    IR_WINDOW_POST_LOW_SECONDS,
    IR_WINDOW_POST_SECONDS,
    IR_WINDOW_PRE_SECONDS,
    IR_WINDOW_VARIABLE_HIGH_HZ,
    IR_WINDOW_VARIABLE_LOW_HZ,
    MIN_TRUSTED_POINTS,
    RESPONSE_OUTLIER_FAIL_DB,
    RESPONSE_OUTLIER_MIN_HZ,
    RESPONSE_OUTLIER_NEIGHBOR_RADIUS,
    RESPONSE_OUTLIER_WARN_DB,
    SWEEP_END_HZ,
    SWEEP_START_HZ,
    SWEEP_TIMING_ANCHOR_LAYOUT,
    SWEEP_TIMING_ANCHOR_SECONDS,
    SWEEP_TIMING_CENTRAL_ANCHORS,
    SWEEP_TIMING_CLUSTER_REJECT_SAMPLES,
    SWEEP_TIMING_EDGE_ANCHORS,
    SWEEP_TIMING_EDGE_INSET_SECONDS,
    SWEEP_TIMING_MAX_ABS_PPM,
    SWEEP_TIMING_MIN_ANCHOR_SCORE,
    SWEEP_TIMING_MIN_COMPENSATION_PPM,
    SWEEP_TIMING_MULTI_ANCHOR_SECONDS,
    SWEEP_TIMING_SEARCH_SECONDS,
    TRUSTED_MAX_HZ,
    TRUSTED_MIN_HZ,
)

logger = logging.getLogger(__name__)


def _detailed_measurement_diagnostics_enabled() -> bool:
    return logger.isEnabledFor(logging.DEBUG)


class MeasurementAnalyzer:
    """Own pure sweep timing, response, and impulse-response analysis."""

    def __init__(self, store, capture_quality_error):
        self._store = store
        self._capture_quality_error = capture_quality_error

    def _build_impulse_response_debug_segment(
        self,
        impulse_response: np.ndarray,
        *,
        sample_rate: int,
        channel: str,
        reference_channel: str,
        alignment_samples: int,
        direct_timing_meta: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not IR_DEBUG_SEGMENT_ENABLED or not _detailed_measurement_diagnostics_enabled():
            return None
        ir64 = impulse_response.astype(np.float64)
        if not ir64.size:
            return None
        ir_abs = np.abs(ir64)
        global_peak_sample = int(np.argmax(ir_abs))
        peak_value = float(ir_abs[global_peak_sample])
        if peak_value <= 0.0:
            return None

        first_threshold_sample = direct_timing_meta.get("first_threshold_index")
        selected_direct_sample = int(direct_timing_meta["direct_arrival_index"])
        reference_peak_sample = int(direct_timing_meta["reference_peak_index"])
        marker_samples = [global_peak_sample, selected_direct_sample]
        if first_threshold_sample is not None:
            marker_samples.append(int(first_threshold_sample))
        radius = int(IR_DEBUG_SEGMENT_RADIUS_SAMPLES)
        start = max(0, min(marker_samples) - radius)
        end = min(ir64.size, max(marker_samples) + radius + 1)

        candidates = [
            {
                "sample": int(item["sample"]),
                "offset_from_peak_samples": int(item.get("offset_from_peak_samples") or 0),
                "score": float(item.get("score") or 0.0),
                "support_score": float(item.get("support_score") or 0.0),
                "local_energy_relative": float(item.get("local_energy_relative") or 0.0),
                "prominence_relative": float(item.get("prominence_relative") or 0.0),
                "weak_threshold_edge": bool(item.get("weak_threshold_edge")),
                "stronger_impulse_region": bool(item.get("stronger_impulse_region")),
                "in_window": start <= int(item["sample"]) < end,
            }
            for item in (direct_timing_meta.get("candidates_chronological") or [])
            if isinstance(item, dict) and item.get("sample") is not None
        ]

        segment = []
        for sample in range(start, end):
            value = float(ir64[sample])
            normalized = value / peak_value
            segment.append(
                {
                    "sample": int(sample),
                    "offset_from_global_peak_samples": int(sample - global_peak_sample),
                    "offset_from_selected_direct_samples": int(sample - selected_direct_sample),
                    "value_normalized": round(float(normalized), 8),
                    "abs_normalized": round(abs(float(normalized)), 8),
                }
            )

        return {
            "schema": "fxroute.ir-debug-segment.v1",
            "channel": channel,
            "reference_channel": reference_channel,
            "sample_rate": int(sample_rate),
            "alignment_samples": int(alignment_samples),
            "window_radius_samples": radius,
            "window_start_sample": int(start),
            "window_end_sample": int(end),
            "window_sample_count": int(end - start),
            "normalization": {
                "mode": "signed impulse response divided by global absolute IR peak",
                "global_peak_abs": peak_value,
            },
            "markers": {
                "first_threshold_sample": int(first_threshold_sample) if first_threshold_sample is not None else None,
                "selected_direct_sample": selected_direct_sample,
                "global_peak_sample": global_peak_sample,
                "reference_peak_sample": reference_peak_sample,
                "arrival_samples": int(direct_timing_meta["relative_samples"]),
                "selection_rule": str(direct_timing_meta.get("selection_rule") or ""),
            },
            "candidate_markers": candidates,
            "segment": segment,
        }
    def _build_ir_preview(
        self,
        impulse_response: np.ndarray,
        sample_rate: int,
        direct_timing_meta: dict[str, Any],
        ir_meta: dict[str, Any],
    ) -> dict[str, Any] | None:
        """
        Build a lightweight time-domain preview for diagnostics only.
        The full impulse response is never stored in measurement JSON.
        """
        ir64 = impulse_response.astype(np.float64)
        if not ir64.size:
            return None
        alignment_index = int(direct_timing_meta.get("direct_arrival_index", ir_meta.get("peak_index", 0)))
        alignment_index = max(0, min(ir64.size - 1, alignment_index))

        pre_samples = max(1, int(round(sample_rate * 0.002)))
        post_samples = max(1, int(round(sample_rate * 0.030)))
        start = max(0, alignment_index - pre_samples)
        end = min(ir64.size, alignment_index + post_samples + 1)
        if start >= end:
            return None

        windowed = ir64[start:end].copy()
        peak_value = float(np.max(np.abs(windowed))) if windowed.size else 0.0
        if peak_value <= 1e-12:
            return None
        windowed = windowed / peak_value

        max_points = 500
        if len(windowed) > max_points:
            indices = np.linspace(0, len(windowed) - 1, max_points, dtype=int)
            windowed = windowed[indices]
        else:
            indices = np.arange(len(windowed), dtype=int)

        points = []
        for relative_index, value in zip(indices, windowed):
            sample_offset = int(start + int(relative_index) - alignment_index)
            time_ms = (sample_offset / float(sample_rate)) * 1000.0
            if -2.0001 <= time_ms <= 30.0001:
                points.append([round(float(time_ms), 3), round(float(value), 6)])
        if not points:
            return None

        return {
            "schema": "fxroute.ir-preview.v1",
            "points": points,
            "sample_rate": int(sample_rate),
            "alignment_index": int(alignment_index),
            "pre_samples": pre_samples,
            "post_samples": post_samples,
            "window_ms": [-2.0, 30.0],
            "normalization": "max_abs_in_preview_window",
        }
    @staticmethod
    def _uses_electrical_reference_timing(reference_channel_label: str) -> bool:
        label = str(reference_channel_label or "").lower()
        return label == "reference" or "electrical_reference" in label

    @staticmethod
    def _hybrid_analysis_requirements(measurement_role: str) -> tuple[bool, bool]:
        role = str(measurement_role or "").strip().lower()
        return role == "direct", role in {"direct", "mlp", "integration"}

    def _analyze_sweep_capture(
        self,
        capture_path: Path,
        *,
        expected_sample_rate: int,
        channel: str,
        reference_sweep: np.ndarray,
        inverse_sweep: np.ndarray,
        calibration_curve: tuple[np.ndarray, np.ndarray] | None,
        capture_label: str = "Capture",
        reference_channel_index: int | None = None,
        analysis_channel_index: int | None = None,
        reference_channel_label: str = "reference",
        timing_override: dict[str, Any] | None = None,
        is_21_dsp_active: bool = False,
        measurement_role: str = "",
    ) -> dict[str, Any]:
        sample_rate, raw_signal = self._store._load_wav_array(capture_path)
        signal = self._store._select_analysis_channel(raw_signal, channel=channel, channel_index=analysis_channel_index)
        timing_signal = self._store._select_analysis_channel(raw_signal, channel=channel, channel_index=reference_channel_index)
        if sample_rate != expected_sample_rate:
            raise RuntimeError(f"Unexpected capture sample rate: {sample_rate} Hz (expected {expected_sample_rate} Hz)")
        if signal.size < reference_sweep.size or timing_signal.size < reference_sweep.size:
            raise RuntimeError("Capture is too short for sweep analysis")

        rms = float(np.sqrt(np.mean(np.square(signal, dtype=np.float64))))
        peak = float(np.max(np.abs(signal)))
        rms_dbfs = 20.0 * math.log10(max(rms, 1e-9))
        peak_dbfs = 20.0 * math.log10(max(peak, 1e-9))
        if peak_dbfs <= -90.0 and rms_dbfs <= -100.0:
            raise RuntimeError("Recorded sweep was effectively silent")

        reference_rms = float(np.sqrt(np.mean(np.square(timing_signal, dtype=np.float64))))
        reference_peak = float(np.max(np.abs(timing_signal)))
        reference_rms_dbfs = 20.0 * math.log10(max(reference_rms, 1e-9))
        reference_peak_dbfs = 20.0 * math.log10(max(reference_peak, 1e-9))
        if reference_peak_dbfs <= -90.0 and reference_rms_dbfs <= -100.0:
            raise RuntimeError(f"{reference_channel_label.capitalize()} channel was effectively silent")

        allow_drift_compensation = not self._uses_electrical_reference_timing(reference_channel_label)
        if timing_override is None:
            coarse_start = self._find_sweep_start(timing_signal, reference_sweep)
            timing = self._estimate_sweep_timing(
                timing_signal,
                reference_sweep,
                coarse_start,
                sample_rate,
                allow_drift_compensation=allow_drift_compensation,
            )
        else:
            timing = {
                "aligned_start": int(timing_override["alignment_samples"]),
                "aligned_end": int(timing_override["alignment_samples"] + timing_override["observed_sweep_samples"]),
                "observed_sweep_samples": int(timing_override["observed_sweep_samples"]),
                "stretch_ratio": float(timing_override.get("stretch_ratio") or 1.0),
                "drift_ppm": float(timing_override.get("drift_ppm") or 0.0),
                "total_drift_samples": int(timing_override.get("total_drift_samples") or 0),
                "estimated_ppm": float(timing_override.get("estimated_ppm", timing_override.get("drift_ppm") or 0.0)),
                "estimated_total_drift_samples": int(
                    timing_override.get("estimated_total_drift_samples", timing_override.get("total_drift_samples") or 0)
                ),
                "raw_global_ppm": float(timing_override.get("raw_global_ppm", timing_override.get("estimated_ppm", 0.0))),
                "fit_start_before_samples": int(timing_override.get("fit_start_before_samples") or timing_override["alignment_samples"]),
                "fit_start_after_samples": int(timing_override.get("fit_start_after_samples") or timing_override["alignment_samples"]),
                "fit_end_before_samples": int(
                    timing_override.get("fit_end_before_samples")
                    or (timing_override["alignment_samples"] + timing_override["observed_sweep_samples"])
                ),
                "fit_end_after_samples": int(
                    timing_override.get("fit_end_after_samples")
                    or (timing_override["alignment_samples"] + timing_override["observed_sweep_samples"])
                ),
                "compensated": bool(timing_override.get("compensated")),
                "anchor_seconds": float(timing_override.get("anchor_seconds") or 0.0),
                "start_score": float(timing_override.get("start_score") or 0.0),
                "end_score": float(timing_override.get("end_score") or 0.0),
                "anchor_strategy": str(timing_override.get("anchor_strategy") or "timing override"),
                "anchor_matches": deepcopy(timing_override.get("anchor_matches") or []),
                "drift_compensation_policy": str(
                    timing_override.get("drift_compensation_policy")
                    or ("constant-delay-er-reference" if not allow_drift_compensation else "auto")
                ),
            }
        aligned_start = int(timing["aligned_start"])
        aligned_end = int(timing["aligned_end"])
        if aligned_end > signal.size or aligned_end > timing_signal.size:
            raise RuntimeError("Aligned sweep window exceeded recorded capture")

        analysis_segment = signal[aligned_start:].astype(np.float64)
        reference_segment = timing_signal[aligned_start:].astype(np.float64)
        if analysis_segment.size < max(2048, reference_sweep.size // 4):
            raise RuntimeError("Aligned sweep segment was too short after timing estimation")
        stretch_ratio = float(timing.get("stretch_ratio") or 1.0)
        corrected_segment_size = max(reference_sweep.size, int(round(analysis_segment.size / max(stretch_ratio, 1e-9))))
        corrected_segment = self._resample_signal(analysis_segment, corrected_segment_size)
        corrected_reference_segment = self._resample_signal(reference_segment, corrected_segment_size)
        captured_tail_samples = max(0, analysis_segment.size - int(timing["observed_sweep_samples"]))

        timing_impulse_response = self._fft_convolve(corrected_segment, inverse_sweep.astype(np.float64))
        reference_impulse_response = self._fft_convolve(corrected_reference_segment, inverse_sweep.astype(np.float64))
        magnitude_impulse_response = self._fft_convolve(analysis_segment, inverse_sweep.astype(np.float64))
        windowed_ir, ir_meta = self._window_impulse_response(timing_impulse_response, sample_rate)
        direct_timing_meta = self._estimate_impulse_direct_arrival(
            timing_impulse_response,
            reference_impulse_response,
            sample_rate,
        )
        response_frequencies, response_magnitude, variable_window_meta = self._build_variable_window_response(
            magnitude_impulse_response,
            sample_rate,
        )
        variable_window_meta["magnitude_resampling_policy"] = "drift-estimate-not-applied"
        variable_window_meta["magnitude_drift_resampling_applied"] = False
        reference_ir_peak = float(np.max(np.abs(reference_impulse_response))) if reference_impulse_response.size else 0.0
        reference_ir_rms = float(np.sqrt(np.mean(np.square(reference_impulse_response, dtype=np.float64)))) if reference_impulse_response.size else 0.0
        reference_ir_peak_db = 20.0 * math.log10(max(reference_ir_peak, 1e-9))
        reference_ir_rms_db = 20.0 * math.log10(max(reference_ir_rms, 1e-9))
        reference_ir_sharpness_db = reference_ir_peak_db - reference_ir_rms_db
        timing_candidates_by_score = direct_timing_meta.get("candidates_by_score") or direct_timing_meta.get("candidates") or []
        timing_candidates_chronological = direct_timing_meta.get("candidates_chronological") or []
        top_timing_candidates_by_score = [
            f"{item.get('offset_from_peak_samples')}spl/{item.get('relative_db')}dB/s={item.get('score')}/e={item.get('local_energy_relative')}/p={item.get('prominence_relative')}/support={item.get('support_score')}"
            for item in timing_candidates_by_score[:5]
        ]
        top_timing_candidates_chronological = [
            f"{item.get('offset_from_peak_samples')}spl/{item.get('relative_db')}dB/s={item.get('score')}/e={item.get('local_energy_relative')}/p={item.get('prominence_relative')}/support={item.get('support_score')}/edge={item.get('weak_threshold_edge')}"
            for item in timing_candidates_chronological[:8]
        ]
        logger.info(
            "Measurement timing summary: channel=%s reference_channel=%s relative_samples=%s relative_ms=%.3f sample_rate=%s selected_score=%.5f selection=%s",
            channel,
            reference_channel_label,
            int(direct_timing_meta["relative_samples"]),
            float(direct_timing_meta["relative_seconds"]) * 1000.0,
            sample_rate,
            float(direct_timing_meta.get("selected_score") or 0.0),
            str(direct_timing_meta.get("selection_rule") or ""),
        )
        logger.info(
            "Measurement drift fit: channel=%s reference_channel=%s estimated_ppm=%.2f estimated_total_drift_samples=%s raw_global_ppm=%.2f "
            "applied_ppm=%.2f applied_total_drift_samples=%s start_before=%s start_after=%s end_before=%s end_after=%s "
            "compensated=%s policy=%s magnitude_resampling=%s",
            channel,
            reference_channel_label,
            float(timing.get("estimated_ppm", timing.get("drift_ppm") or 0.0)),
            int(timing.get("estimated_total_drift_samples", timing.get("total_drift_samples") or 0)),
            float(timing.get("raw_global_ppm", timing.get("estimated_ppm", 0.0))),
            float(timing.get("drift_ppm") or 0.0),
            int(timing.get("total_drift_samples") or 0),
            int(timing.get("fit_start_before_samples", aligned_start)),
            int(timing.get("fit_start_after_samples", aligned_start)),
            int(timing.get("fit_end_before_samples", aligned_end)),
            int(timing.get("fit_end_after_samples", aligned_end)),
            bool(timing.get("compensated")),
            str(timing.get("drift_compensation_policy") or ""),
            "disabled",
        )
        logger.debug(
            "Measurement timing detection: channel=%s reference_channel=%s mic_peak_sample=%s direct_sample=%s reference_peak_sample=%s relative_samples=%s relative_ms=%.3f sample_rate=%s alignment_samples=%s selected_score=%.5f selected_db=%.2f selection=%s first_threshold_sample=%s candidates_by_score=%s candidates_chronological=%s",
            channel,
            reference_channel_label,
            int(ir_meta["peak_index"]),
            int(direct_timing_meta["direct_arrival_index"]),
            int(direct_timing_meta["reference_peak_index"]),
            int(direct_timing_meta["relative_samples"]),
            float(direct_timing_meta["relative_seconds"]) * 1000.0,
            sample_rate,
            aligned_start,
            float(direct_timing_meta.get("selected_score") or 0.0),
            float(direct_timing_meta.get("direct_relative_to_peak_db") or -120.0),
            str(direct_timing_meta.get("selection_rule") or ""),
            direct_timing_meta.get("first_threshold_index"),
            top_timing_candidates_by_score,
            top_timing_candidates_chronological,
        )
        impulse_response_debug_segment = self._build_impulse_response_debug_segment(
            timing_impulse_response,
            sample_rate=sample_rate,
            channel=channel,
            reference_channel=reference_channel_label,
            alignment_samples=aligned_start,
            direct_timing_meta=direct_timing_meta,
        )
        display_data = self._build_display_points(
            frequencies=response_frequencies,
            magnitude=response_magnitude,
            calibration_curve=calibration_curve,
        )
        needs_direct_response, needs_complex_response = self._hybrid_analysis_requirements(measurement_role)
        direct_window = None
        complex_response = None
        if needs_direct_response:
            direct_window = analyze_direct_window(
                timing_impulse_response,
                sample_rate,
                int(direct_timing_meta["direct_arrival_index"]),
                timing_metadata=direct_timing_meta,
            )
            if direct_window["usable"]:
                direct_frequencies, direct_magnitude = build_gated_response(
                    timing_impulse_response,
                    sample_rate,
                    direct_window,
                )
                direct_display = self._build_display_points(
                    frequencies=direct_frequencies,
                    magnitude=direct_magnitude,
                    calibration_curve=calibration_curve,
                )
                direct_lower_hz = float(direct_window["gated_direct_lower_limit_hz"])
                direct_window["points"] = [
                    point for point in direct_display["review_points"]
                    if float(point[0]) >= direct_lower_hz
                ]
            else:
                direct_window["points"] = []
        if needs_complex_response:
            complex_response = build_complex_response(
                timing_impulse_response,
                sample_rate,
                calibration_curve=calibration_curve,
            )
        capture_audit = self._store._build_capture_audit(
            raw_signal=raw_signal,
            sample_rate=sample_rate,
        )
        selected_mic_channel_index = int(analysis_channel_index) if analysis_channel_index is not None else 0
        capture_audit.update({
            "selected_mic_channel_index": selected_mic_channel_index,
            "selected_mic_channel_number": selected_mic_channel_index + 1,
            "selected_mic_peak_dbfs": round(peak_dbfs, 2),
            "selected_mic_rms_dbfs": round(rms_dbfs, 2),
            "reference_channel_index": int(reference_channel_index) if reference_channel_index is not None else None,
        })
        quality_checks = self._store._build_capture_quality_checks(
            capture_audit=capture_audit,
            timing=timing,
            peak_dbfs=peak_dbfs,
            rms_dbfs=rms_dbfs,
            trusted_band_meta=display_data["trusted_band_meta"],
            trusted_max_hz=display_data["trusted_band"][1],
            response_outliers=display_data.get("response_outliers") or [],
            capture_label=capture_label,
            expect_dual_mono_channels=reference_channel_index is None,
            is_21_dsp_active=is_21_dsp_active,
        )
        analysis = {
            "method": "inverse log-sweep deconvolution with anchor timing compensation and IR windowing",
            "sample_rate": int(sample_rate),
            "trusted_points": display_data["trusted_points"],
            "review_points": display_data["review_points"],
            "normalized_by_db": round(display_data["normalized_by"], 3),
            "rms_dbfs": round(rms_dbfs, 2),
            "peak_dbfs": round(peak_dbfs, 2),
            "window_count": 1,
            "alignment_samples": int(aligned_start),
            "alignment_seconds": round(aligned_start / sample_rate, 6),
            "trusted_min_hz": round(display_data["trusted_band"][0], 3),
            "trusted_max_hz": round(display_data["trusted_band"][1], 3),
            "raw_point_count": display_data["raw_point_count"],
            "review_point_count": len(display_data["review_points"]),
            "display_point_count": len(display_data["trusted_points"]),
            "trusted_band_meta": display_data["trusted_band_meta"],
            "review_band_meta": display_data["review_band_meta"],
            "quality_checks": quality_checks,
            "capture_audit": capture_audit,
            "clock": {
                "observed_sweep_samples": int(timing["observed_sweep_samples"]),
                "reference_sweep_samples": int(reference_sweep.size),
                "analysis_segment_samples": int(analysis_segment.size),
                "corrected_segment_samples": int(corrected_segment.size),
                "magnitude_segment_samples": int(analysis_segment.size),
                "magnitude_drift_resampling_applied": False,
                "magnitude_resampling_policy": "disabled-linear-resampler-artifact",
                "captured_tail_samples": int(captured_tail_samples),
                "captured_tail_seconds": round(float(captured_tail_samples) / sample_rate, 6),
                "stretch_ratio": round(float(timing["stretch_ratio"]), 8),
                "drift_ppm": round(float(timing["drift_ppm"]), 2),
                "estimated_ppm": round(float(timing.get("estimated_ppm", timing["drift_ppm"])), 2),
                "estimated_total_drift_samples": int(
                    timing.get("estimated_total_drift_samples", timing.get("total_drift_samples") or 0)
                ),
                "raw_global_ppm": round(float(timing.get("raw_global_ppm", timing.get("estimated_ppm", 0.0))), 2),
                "applied_total_drift_samples": int(timing.get("total_drift_samples") or 0),
                "drift_compensation_policy": str(timing.get("drift_compensation_policy") or "auto"),
                "drift_compensation_applied": bool(timing["compensated"]),
                "fit_start_before_samples": int(timing.get("fit_start_before_samples", aligned_start)),
                "fit_start_after_samples": int(timing.get("fit_start_after_samples", aligned_start)),
                "fit_end_before_samples": int(timing.get("fit_end_before_samples", aligned_end)),
                "fit_end_after_samples": int(timing.get("fit_end_after_samples", aligned_end)),
                "compensated": bool(timing["compensated"]),
                "anchor_seconds": round(float(timing["anchor_seconds"]), 4),
                "anchor_strategy": str(timing.get("anchor_strategy") or "edge anchors"),
                "anchor_matches": timing.get("anchor_matches") or [],
                "start_score": round(float(timing["start_score"]), 5),
                "end_score": round(float(timing["end_score"]), 5),
                "selected_lag": timing.get("selected_lag"),
                "timing_channel": reference_channel_label,
            },
            "reference_path": {
                "channel": reference_channel_label,
                "peak_dbfs": round(reference_peak_dbfs, 2),
                "rms_dbfs": round(reference_rms_dbfs, 2),
                "alignment_score": round(min(float(timing["start_score"]), float(timing["end_score"])), 5),
                "start_score": round(float(timing["start_score"]), 5),
                "end_score": round(float(timing["end_score"]), 5),
                "drift_ppm": round(float(timing["drift_ppm"]), 2),
                "ir_peak_dbfs": round(reference_ir_peak_db, 2),
                "ir_sharpness_db": round(reference_ir_sharpness_db, 2),
                "clipped": bool(reference_peak_dbfs >= CAPTURE_CLIP_FAIL_DBFS),
            },
            "impulse_response": {
                "peak_index": int(ir_meta["peak_index"]),
                "peak_seconds": round(float(ir_meta["peak_seconds"]), 6),
                "direct_arrival_index": int(direct_timing_meta["direct_arrival_index"]),
                "direct_seconds": round(float(direct_timing_meta["direct_seconds"]), 6),
                "direct_relative_to_peak_db": direct_timing_meta["direct_relative_to_peak_db"],
                "direct_threshold_relative": round(float(direct_timing_meta["direct_threshold_relative"]), 4),
                "direct_selection_rule": direct_timing_meta["selection_rule"],
                "direct_selected_score": round(float(direct_timing_meta["selected_score"]), 6),
                "direct_selected_support_score": round(float(direct_timing_meta["selected_support_score"]), 6),
                "direct_confidence": round(float(direct_timing_meta["confidence"]), 6),
                "direct_first_threshold_index": direct_timing_meta["first_threshold_index"],
                "direct_first_threshold_offset_from_peak_samples": direct_timing_meta["first_threshold_offset_from_peak_samples"],
                "direct_candidate_count": int(direct_timing_meta["candidate_count"]),
                "direct_candidates": direct_timing_meta["candidates_by_score"],
                "direct_candidates_by_score": direct_timing_meta["candidates_by_score"],
                "direct_candidates_chronological": direct_timing_meta["candidates_chronological"],
                "reference_peak_index": int(direct_timing_meta["reference_peak_index"]),
                "reference_peak_seconds": round(float(direct_timing_meta["reference_peak_seconds"]), 6),
                "arrival_samples": int(direct_timing_meta["relative_samples"]),
                "arrival_seconds": round(float(direct_timing_meta["relative_seconds"]), 6),
                "arrival_ms": round(float(direct_timing_meta["relative_seconds"]) * 1000.0, 6),
                "timing_source": "direct_arrival_minus_reference_peak",
                "window_start_index": int(ir_meta["window_start_index"]),
                "window_end_index": int(ir_meta["window_end_index"]),
                "window_seconds": round(float(ir_meta["window_seconds"]), 6),
                "pre_window_seconds": round(float(ir_meta["pre_window_seconds"]), 6),
                "post_window_seconds": round(float(ir_meta["post_window_seconds"]), 6),
                "peak_dbfs": round(float(ir_meta["peak_dbfs"]), 2),
                "preview": self._build_ir_preview(timing_impulse_response, sample_rate, direct_timing_meta, ir_meta),
            },
            "variable_window": variable_window_meta,
            "_impulse_response_debug_segment": impulse_response_debug_segment,
        }
        if direct_window is not None:
            analysis["direct_response"] = direct_window
        if complex_response is not None:
            analysis["complex_response"] = complex_response
        hard_failures = [item["message"] for item in quality_checks["items"] if item.get("level") == "error"]

        if hard_failures:
            raise self._capture_quality_error(capture_label, quality_checks["items"], analysis=analysis)
        # Track last successful lag for multi-peak continuity bonus
        clock = (analysis.get("clock") or {})
        if clock.get("selected_lag") is not None:
            self._store._last_successful_lag = int(clock["selected_lag"])
        return analysis
















    def _build_display_points(
        self,
        *,
        frequencies: np.ndarray,
        magnitude: np.ndarray,
        calibration_curve: tuple[np.ndarray, np.ndarray] | None,
    ) -> dict[str, Any]:
        analysis_limit_hz = min(float(frequencies[-1]) - 1.0, SWEEP_END_HZ)
        display_max_hz = min(analysis_limit_hz, TRUSTED_MAX_HZ)
        nyquist = max(TRUSTED_MIN_HZ + 1.0, display_max_hz)
        centers = self._store._log_spaced_frequencies(TRUSTED_MIN_HZ, nyquist, DISPLAY_POINT_COUNT)
        smoothing_ratio = 2 ** (1 / 12)
        corrected_magnitude = magnitude.astype(np.float64, copy=True)
        if calibration_curve is not None:
            cal_freqs, cal_offsets = calibration_curve
            log_freqs = np.log(np.clip(frequencies, 1e-9, None))
            log_cal_freqs = np.log(cal_freqs)
            interpolated_offsets = np.interp(
                log_freqs,
                log_cal_freqs,
                cal_offsets,
                left=float(cal_offsets[0]),
                right=float(cal_offsets[-1]),
            )
            corrected_magnitude *= np.power(10.0, -interpolated_offsets / 20.0)

        raw_points: list[list[float]] = []
        raw_db_values: list[float] = []
        for center in centers:
            lower = center / smoothing_ratio
            upper = min(center * smoothing_ratio, analysis_limit_hz)
            if lower >= analysis_limit_hz:
                continue
            mask = (frequencies >= lower) & (frequencies <= upper)
            if not np.any(mask):
                continue
            band_mag = float(np.sqrt(np.mean(np.square(corrected_magnitude[mask], dtype=np.float64))))
            db = 20.0 * math.log10(max(band_mag, 1e-12))
            raw_points.append([round(center, 3), round(db, 3)])
            raw_db_values.append(db)
        if not raw_points:
            raise RuntimeError("Sweep analysis produced no displayable trace points")

        trusted_min_hz, trusted_max_hz, trusted_band_meta = self._select_trusted_band(
            raw_points,
        )
        trusted_points = [point for point in raw_points if trusted_min_hz <= point[0] <= trusted_max_hz]
        if not trusted_points:
            raise RuntimeError("Sweep analysis produced no trusted trace points")

        response_outliers = self._find_response_outliers(
            raw_points,
            min_hz=max(RESPONSE_OUTLIER_MIN_HZ, trusted_min_hz),
            max_hz=trusted_max_hz,
        )

        reference_values = [db for freq, db in trusted_points if 120.0 <= freq <= 8_000.0] or raw_db_values
        normalized_by = float(np.median(reference_values)) if reference_values else 0.0
        normalized_trusted_points = [[freq, round(db - normalized_by, 3)] for freq, db in trusted_points]
        normalized_review_points = [[freq, round(db - normalized_by, 3)] for freq, db in raw_points]
        return {
            "trusted_points": normalized_trusted_points,
            "review_points": normalized_review_points,
            "normalized_by": normalized_by,
            "trusted_band": (trusted_min_hz, trusted_max_hz),
            "raw_point_count": len(raw_points),
            "trusted_band_meta": trusted_band_meta,
            "review_band_meta": {
                "selection": "full-band raw review",
                "min_hz": round(float(raw_points[0][0]), 3),
                "max_hz": round(float(raw_points[-1][0]), 3),
                "point_count": len(raw_points),
                "normalization_reference": "shared with trusted trace",
                "excluded_from_trusted_below_hz": round(float(trusted_min_hz), 3),
                "excluded_from_trusted_above_hz": round(float(trusted_max_hz), 3),
                "trusted_comparison_upper_hz": round(float(trusted_max_hz), 3),
            },
            "response_outliers": response_outliers,
        }

    def _select_trusted_band(
        self,
        raw_points: list[list[float]],
    ) -> tuple[float, float, dict[str, Any]]:
        freqs = [float(point[0]) for point in raw_points]
        if not freqs:
            raise RuntimeError("Sweep analysis produced no points for full-band display")

        levels = [float(point[1]) for point in raw_points]
        total_points = len(freqs)
        window_points = min(EDGE_STABILITY_WINDOW_POINTS, total_points)
        min_trusted_points = min(MIN_TRUSTED_POINTS, total_points)
        low_index = 0
        high_index = total_points - 1

        while (high_index - low_index + 1) > min_trusted_points and not self._edge_window_is_stable(levels[low_index : low_index + window_points]):
            low_index += 1
        while (high_index - low_index + 1) > min_trusted_points and not self._edge_window_is_stable(levels[high_index - window_points + 1 : high_index + 1]):
            high_index -= 1

        low_stable = self._edge_window_is_stable(levels[low_index : low_index + window_points])
        high_stable = self._edge_window_is_stable(levels[high_index - window_points + 1 : high_index + 1])
        edge_trimmed = low_index > 0 or high_index < total_points - 1

        trimmed = low_index > 0 or high_index < total_points - 1
        selection_reasons = []
        if edge_trimmed:
            selection_reasons.append("edge-stability")
        if trimmed and (high_index - low_index + 1) >= min_trusted_points:
            selection = "+".join(selection_reasons) + "-trimmed" if selection_reasons else "trimmed"
        elif not trimmed:
            selection = "full-band stable"
        else:
            selection = "minimum-point fallback"

        return freqs[low_index], freqs[high_index], {
            "selection": selection,
            "edge_window_points": window_points,
            "low_rejected_points": low_index,
            "high_rejected_points": total_points - high_index - 1,
            "min_trusted_points": min_trusted_points,
            "stable_low_edge": bool(low_stable),
            "stable_high_edge": bool(high_stable),
            "trusted_point_count": high_index - low_index + 1,
        }


    @staticmethod
    def _find_response_outliers(
        raw_points: list[list[float]],
        *,
        min_hz: float,
        max_hz: float,
    ) -> list[dict[str, float | str]]:
        if len(raw_points) < (RESPONSE_OUTLIER_NEIGHBOR_RADIUS * 2 + 1):
            return []

        outliers: list[dict[str, float | str]] = []
        radius = RESPONSE_OUTLIER_NEIGHBOR_RADIUS
        for index in range(radius, len(raw_points) - radius):
            frequency = float(raw_points[index][0])
            if frequency < min_hz or frequency > max_hz:
                continue
            neighbor_levels = [
                float(raw_points[neighbor_index][1])
                for neighbor_index in range(index - radius, index + radius + 1)
                if neighbor_index != index
            ]
            if not neighbor_levels:
                continue
            local_median = float(np.median(neighbor_levels))
            deviation_db = abs(float(raw_points[index][1]) - local_median)
            if deviation_db < RESPONSE_OUTLIER_WARN_DB:
                continue
            outliers.append(
                {
                    "frequency_hz": round(frequency, 3),
                    "deviation_db": round(deviation_db, 3),
                    "severity": "fail" if deviation_db >= RESPONSE_OUTLIER_FAIL_DB else "warn",
                }
            )
        return outliers

    @staticmethod
    def _edge_window_is_stable(window_levels: list[float]) -> bool:
        if len(window_levels) < 2:
            return True
        deltas = [abs(window_levels[index + 1] - window_levels[index]) for index in range(len(window_levels) - 1)]
        span = max(window_levels) - min(window_levels)
        return max(deltas) <= EDGE_STABILITY_MAX_DELTA_DB and span <= EDGE_STABILITY_MAX_SPAN_DB

    def _estimate_sweep_timing(
        self,
        signal: np.ndarray,
        reference_sweep: np.ndarray,
        coarse_start: int,
        sample_rate: int,
        *,
        allow_drift_compensation: bool = True,
    ) -> dict[str, Any]:
        edge_anchor_samples = min(
            reference_sweep.size // 2,
            max(4096, int(round(sample_rate * SWEEP_TIMING_ANCHOR_SECONDS))),
        )
        anchor_samples = min(
            edge_anchor_samples,
            max(2048, int(round(sample_rate * SWEEP_TIMING_MULTI_ANCHOR_SECONDS))),
        )
        edge_inset = min(
            max(0, reference_sweep.size // 8),
            max(0, int(round(sample_rate * SWEEP_TIMING_EDGE_INSET_SECONDS))),
        )
        search_margin = max(edge_anchor_samples // 2, int(round(sample_rate * SWEEP_TIMING_SEARCH_SECONDS)))

        anchors = self._build_sweep_timing_anchors(
            reference_sweep=reference_sweep,
            anchor_samples=anchor_samples,
            edge_inset=edge_inset,
        )
        # ── Multi-peak global lag selection ──
        # Find top-5 correlation peaks per anchor region, then pick a single
        # globally-consistent lag instead of letting each anchor lock onto
        # independent (possibly wrong) peaks.  This prevents oscillation between
        # sub-path and main-path lags in 2.1 DSP measurements.
        TOP_N_PEAKS = 5
        LAG_CLUSTER_SAMPLES = max(SWEEP_TIMING_CLUSTER_REJECT_SAMPLES * 20, 480)
        anchor_peaks: list[dict[str, Any]] = []
        for anchor in anchors:
            expected_start = coarse_start + int(anchor["offset_samples"])
            search_start = max(0, expected_start - search_margin)
            search_end = min(signal.size, expected_start + anchor_samples + search_margin)
            top = self._find_top_n_alignments_in_region(
                signal[search_start:search_end],
                anchor["template"],
                top_n=TOP_N_PEAKS,
            )
            anchor_peaks.append(
                {
                    "name": anchor["name"],
                    "offset_samples": anchor["offset_samples"],
                    "expected_start": int(expected_start),
                    "search_start": search_start,
                    "top_peaks": top,
                }
            )

        selected_lag, per_anchor_pick, lag_debug = self._select_global_lag_from_peaks(
            anchor_peaks,
            cluster_threshold=LAG_CLUSTER_SAMPLES,
            previous_successful_lag=self._store._last_successful_lag,
        )
        logger.info(
            "LAG_SELECT: lag=%d prev=%s anchors=%d score=%.3f",
            lag_debug.get("selected_lag"),
            lag_debug.get("previous_successful_lag"),
            lag_debug.get("candidate_scores")[0].get("anchors", 0) if lag_debug.get("candidate_scores") else 0,
            lag_debug.get("candidate_scores")[0].get("total", 0) if lag_debug.get("candidate_scores") else 0,
        )

        matches = []
        for ap in anchor_peaks:
            pick_idx = per_anchor_pick.get(ap["name"], 0)
            peak = ap["top_peaks"][pick_idx]
            observed_start = ap["search_start"] + int(peak["index"])
            matches.append(
                {
                    "name": ap["name"],
                    "offset_samples": ap["offset_samples"],
                    "template": None,  # filled by _build_sweep_timing_anchors but not needed downstream
                    "expected_start": int(ap["expected_start"]),
                    "observed_start": int(observed_start),
                    "residual_samples": int(observed_start - ap["expected_start"]),
                    "score": float(peak["score"]),
                    "raw_score": float(peak["raw_score"]),
                    "polarity": float(peak["polarity"]),
                }
            )

        fit = self._fit_sweep_timing_from_matches(
            matches=matches,
            reference_sweep_samples=reference_sweep.size,
            sample_rate=sample_rate,
            allow_drift_compensation=allow_drift_compensation,
        )
        aligned_start = int(fit["aligned_start"])
        observed_sweep_samples = int(fit["observed_sweep_samples"])
        stretch_ratio = float(fit["stretch_ratio"])
        drift_ppm = float(fit.get("drift_ppm", (stretch_ratio - 1.0) * 1_000_000.0))
        total_drift_samples = int(fit.get("total_drift_samples", round(observed_sweep_samples - reference_sweep.size)))
        estimated_ppm = float(fit.get("estimated_ppm", drift_ppm))
        estimated_total_drift_samples = int(
            fit.get("estimated_total_drift_samples", round(reference_sweep.size * estimated_ppm / 1_000_000.0))
        )
        fit_start_before = int(coarse_start)
        fit_end_before = int(coarse_start + reference_sweep.size)
        fit_start_after = int(aligned_start)
        fit_end_after = int(aligned_start + observed_sweep_samples)
        compensated = bool(fit.get("compensated"))
        if not compensated:
            observed_sweep_samples = int(reference_sweep.size)
            stretch_ratio = 1.0
            drift_ppm = 0.0
            total_drift_samples = 0
            fit_end_after = int(aligned_start + observed_sweep_samples)

        return {
            "aligned_start": int(aligned_start),
            "aligned_end": int(aligned_start + observed_sweep_samples),
            "observed_sweep_samples": int(observed_sweep_samples),
            "stretch_ratio": stretch_ratio,
            "drift_ppm": drift_ppm,
            "total_drift_samples": total_drift_samples,
            "selected_lag": selected_lag,  # injected for continuity tracking
            "estimated_ppm": estimated_ppm,
            "estimated_total_drift_samples": estimated_total_drift_samples,
            "raw_global_ppm": float(fit.get("raw_global_ppm", estimated_ppm)),
            "fit_start_before_samples": fit_start_before,
            "fit_start_after_samples": fit_start_after,
            "fit_end_before_samples": fit_end_before,
            "fit_end_after_samples": fit_end_after,
            "compensated": compensated,
            "anchor_seconds": anchor_samples / sample_rate,
            "start_score": float(fit["start_score"]),
            "end_score": float(fit["end_score"]),
            "anchor_strategy": str(fit.get("anchor_strategy") or "multi-anchor weighted fit"),
            "drift_compensation_policy": str(fit.get("drift_compensation_policy") or "auto"),
            "anchor_matches": fit["anchor_matches"],
        }

    def _build_sweep_timing_anchors(
        self,
        *,
        reference_sweep: np.ndarray,
        anchor_samples: int,
        edge_inset: int,
    ) -> list[dict[str, Any]]:
        max_start = max(0, reference_sweep.size - anchor_samples)
        min_start = min(max_start, max(0, edge_inset))
        max_start = max(min_start, max_start - edge_inset)
        anchors = []
        for name, fraction in SWEEP_TIMING_ANCHOR_LAYOUT:
            center = int(round(reference_sweep.size * fraction))
            start = center - (anchor_samples // 2)
            start = max(min_start, min(max_start, start))
            end = start + anchor_samples
            anchors.append(
                {
                    "name": name,
                    "offset_samples": int(start),
                    "template": reference_sweep[start:end],
                }
            )
        return anchors

    def _fit_sweep_timing_from_matches(
        self,
        *,
        matches: list[dict[str, Any]],
        reference_sweep_samples: int,
        sample_rate: int,
        allow_drift_compensation: bool = True,
    ) -> dict[str, Any]:
        if len(matches) < 2:
            raise RuntimeError("Sweep timing fit did not have enough anchors")

        offsets = np.array([float(item["offset_samples"]) for item in matches], dtype=np.float64)
        observed = np.array([float(item["observed_start"]) for item in matches], dtype=np.float64)
        scores = np.array([max(float(item.get("score") or 0.0), 1e-6) for item in matches], dtype=np.float64)

        raw_intercept, raw_slope, raw_residuals = self._weighted_anchor_line_fit(offsets, observed, scores)
        raw_slope = min(max(raw_slope, 0.5), 1.5)
        raw_ppm = (raw_slope - 1.0) * 1_000_000.0

        central_indices = [
            index
            for index, item in enumerate(matches)
            if str(item.get("name")) in SWEEP_TIMING_CENTRAL_ANCHORS
        ]
        if central_indices:
            central_starts = [float(matches[index]["observed_start"]) - float(matches[index]["offset_samples"]) for index in central_indices]
            central_start = float(np.median(np.array(central_starts, dtype=np.float64)))
            central_residual = float(np.median(np.array([float(matches[index]["residual_samples"]) for index in central_indices], dtype=np.float64)))
        else:
            central_start = float(raw_intercept)
            central_residual = 0.0

        fit_candidate_mask = np.zeros(offsets.size, dtype=bool)
        diagnostic_rows: list[dict[str, Any]] = []
        for index, item in enumerate(matches):
            name = str(item["name"])
            score = float(item.get("score") or 0.0)
            polarity = int(-1 if float(item.get("polarity") or 1.0) < 0 else 1)
            coarse_residual = float(item.get("residual_samples") or 0.0)
            reasons: list[str] = []
            if polarity < 0:
                reasons.append("negative_polarity")
            if score < SWEEP_TIMING_MIN_ANCHOR_SCORE:
                reasons.append("low_correlation")
            if name in SWEEP_TIMING_EDGE_ANCHORS:
                reasons.append("edge_proximity")
            if abs(coarse_residual - central_residual) > SWEEP_TIMING_CLUSTER_REJECT_SAMPLES:
                reasons.append("central_cluster_deviation")
            if not allow_drift_compensation and name not in SWEEP_TIMING_CENTRAL_ANCHORS:
                reasons.append("constant_delay_policy")
            accepted = not reasons
            fit_candidate_mask[index] = accepted
            fraction = float(item["offset_samples"]) / max(1.0, float(reference_sweep_samples))
            approx_frequency = SWEEP_START_HZ * ((SWEEP_END_HZ / SWEEP_START_HZ) ** max(0.0, min(1.0, fraction)))
            diagnostic_rows.append(
                {
                    "name": name,
                    "time_fraction": round(fraction, 5),
                    "time_seconds": round(float(item["offset_samples"]) / sample_rate, 6),
                    "approx_frequency_hz": round(float(approx_frequency), 2),
                    "region": self._sweep_timing_anchor_region(name),
                    "offset_samples": int(item["offset_samples"]),
                    "score": round(score, 5),
                    "raw_score": round(float(item["raw_score"]), 5),
                    "polarity": polarity,
                    "coarse_expected_start": int(item["expected_start"]),
                    "coarse_residual_samples": int(item["residual_samples"]),
                    "observed_start": int(item["observed_start"]),
                    "accepted": accepted,
                    "rejected": not accepted,
                    "reject_reasons": reasons,
                    "used_for_fit": accepted,
                }
            )

        accepted_count = int(np.count_nonzero(fit_candidate_mask))
        if allow_drift_compensation and accepted_count >= 3:
            fit_intercept, fit_slope, fit_residuals = self._weighted_anchor_line_fit(
                offsets[fit_candidate_mask],
                observed[fit_candidate_mask],
                scores[fit_candidate_mask],
            )
            fit_slope = min(max(fit_slope, 0.5), 1.5)
            estimated_ppm = (fit_slope - 1.0) * 1_000_000.0
            compensation_allowed_by_ppm = (
                SWEEP_TIMING_MIN_COMPENSATION_PPM <= abs(estimated_ppm) <= SWEEP_TIMING_MAX_ABS_PPM
            )
            compensated = bool(compensation_allowed_by_ppm)
            slope = fit_slope if compensated else 1.0
            intercept = fit_intercept
            drift_compensation_policy = "robust-accepted-anchor-fit"
            anchor_strategy = "robust accepted-anchor weighted fit"
        else:
            intercept = central_start
            slope = 1.0
            fit_slope = 1.0
            fit_residuals = observed[fit_candidate_mask] - (intercept + fit_slope * offsets[fit_candidate_mask])
            estimated_ppm = 0.0
            compensated = False
            drift_compensation_policy = (
                "constant-delay-er-reference" if not allow_drift_compensation else "constant-delay-insufficient-anchors"
            )
            anchor_strategy = "central-anchor constant delay"

        observed_sweep_samples = int(round(reference_sweep_samples * slope))
        total_drift_samples = int(round(observed_sweep_samples - reference_sweep_samples))
        estimated_total_drift_samples = int(round(reference_sweep_samples * estimated_ppm / 1_000_000.0))
        fitted_all_residuals = observed - (intercept + slope * offsets)

        inlier_mask = fit_candidate_mask
        start_score = self._aggregate_anchor_region_score(matches, inlier_mask, region="start")
        end_score = self._aggregate_anchor_region_score(matches, inlier_mask, region="end")
        anchor_matches = []
        for index, item in enumerate(matches):
            fitted_expected_start = float(intercept + slope * offsets[index])
            row = diagnostic_rows[index]
            anchor_matches.append(
                {
                    **row,
                    "fitted_expected_start": int(round(fitted_expected_start)),
                    "fitted_residual_samples": int(round(float(fitted_all_residuals[index]))),
                    "residual_samples": int(round(float(fitted_all_residuals[index]))),
                    "raw_global_fitted_expected_start": int(round(float(raw_intercept + raw_slope * offsets[index]))),
                    "raw_global_fitted_residual_samples": int(round(float(raw_residuals[index]))),
                    "inlier": bool(inlier_mask[index]),
                }
            )

        return {
            "aligned_start": int(round(intercept)),
            "observed_sweep_samples": int(observed_sweep_samples),
            "stretch_ratio": float(slope),
            "drift_ppm": float((slope - 1.0) * 1_000_000.0),
            "total_drift_samples": int(total_drift_samples),
            "estimated_ppm": float(estimated_ppm),
            "estimated_total_drift_samples": int(estimated_total_drift_samples),
            "raw_global_ppm": float(raw_ppm),
            "compensated": compensated,
            "anchor_strategy": anchor_strategy,
            "drift_compensation_policy": drift_compensation_policy,
            "start_score": float(start_score),
            "end_score": float(end_score),
            "anchor_matches": anchor_matches,
        }

    @staticmethod
    def _weighted_anchor_line_fit(
        offsets: np.ndarray,
        observed: np.ndarray,
        scores: np.ndarray,
    ) -> tuple[float, float, np.ndarray]:
        weights = np.square(np.maximum(scores, 1e-6))
        design = np.column_stack([np.ones(offsets.size, dtype=np.float64), offsets])
        sqrt_weights = np.sqrt(weights)
        coeffs, *_ = np.linalg.lstsq(design * sqrt_weights[:, None], observed * sqrt_weights, rcond=None)
        intercept = float(coeffs[0])
        slope = float(coeffs[1])
        residuals = observed - (intercept + slope * offsets)
        return intercept, slope, residuals

    @staticmethod
    def _sweep_timing_anchor_region(name: str) -> str:
        if name.startswith("start"):
            return "start-edge" if name.endswith("inner") else "start-body"
        if name.startswith("end"):
            return "end-edge" if name.endswith("inner") else "end-body"
        return "central"

    def _aggregate_anchor_region_score(
        self,
        matches: list[dict[str, Any]],
        inlier_mask: np.ndarray,
        *,
        region: str,
    ) -> float:
        if region == "start":
            labels = {"start-inner", "start-body", "mid-low"}
        else:
            labels = {"mid-high", "end-body", "end-inner"}

        def collect_scores(valid_labels: set[str], only_inliers: bool = True) -> list[float]:
            values = []
            for index, item in enumerate(matches):
                if str(item.get("name")) not in valid_labels:
                    continue
                if only_inliers and not bool(inlier_mask[index]):
                    continue
                values.append(float(item.get("score") or 0.0))
            return values

        region_scores = collect_scores(labels, only_inliers=True)
        if not region_scores:
            region_scores = collect_scores(labels, only_inliers=False)
        if not region_scores:
            return 0.0
        region_scores.sort(reverse=True)
        top_scores = region_scores[:2]
        return float(sum(top_scores) / len(top_scores))

    def _find_top_n_alignments_in_region(
        self,
        region: np.ndarray,
        template: np.ndarray,
        *,
        top_n: int = 5,
    ) -> list[dict[str, float]]:
        """Return top-N correlation peaks from a search region.

        Each result dict contains ``index``, ``score``, ``raw_score`` and
        ``polarity``.
        """
        region64 = region.astype(np.float64)
        template64 = template.astype(np.float64)
        if region64.size < template64.size:
            raise RuntimeError("Timing search region was too short for sweep alignment")
        corr = self._fft_correlate(region64, template64[::-1])
        valid = corr[template64.size - 1 : region64.size]
        if valid.size == 0:
            raise RuntimeError("Unable to refine sweep timing")
        abs_valid = np.abs(valid)

        # Find local maxima in the correlation envelope
        peak_mask = np.zeros(abs_valid.size, dtype=bool)
        for i in range(1, abs_valid.size - 1):
            if abs_valid[i] > abs_valid[i - 1] and abs_valid[i] >= abs_valid[i + 1]:
                peak_mask[i] = True
        # Always include the endpoints
        if abs_valid.size >= 2:
            if abs_valid[0] >= abs_valid[1]:
                peak_mask[0] = True
            if abs_valid[-1] >= abs_valid[-2]:
                peak_mask[-1] = True
        elif abs_valid.size == 1:
            peak_mask[0] = True

        peak_indices = np.where(peak_mask)[0]
        if peak_indices.size == 0:
            peak_indices = np.array([int(np.argmax(abs_valid))])

        # Sort by absolute correlation, descending
        peak_values = abs_valid[peak_indices]
        sort_order = np.argsort(peak_values)[::-1]
        top_k = min(top_n, peak_indices.size)
        top_indices = peak_indices[sort_order[:top_k]]

        results: list[dict[str, float]] = []
        for index in top_indices:
            idx = int(index)
            snippet = region64[idx : idx + template64.size]
            denom = float(np.linalg.norm(snippet) * np.linalg.norm(template64))
            raw_score = 0.0 if denom <= 1e-12 else float(np.dot(snippet, template64) / denom)
            results.append(
                {
                    "index": float(idx),
                    "score": abs(raw_score),
                    "raw_score": raw_score,
                    "polarity": -1.0 if raw_score < 0 else 1.0,
                }
            )
        return results

    def _select_global_lag_from_peaks(
        self,
        anchor_peaks: list[dict[str, Any]],
        *,
        cluster_threshold: int,
        previous_successful_lag: int | None = None,
    ) -> tuple[int, dict[str, int], dict[str, Any]]:
        """Pick a single globally-consistent lag from multi-anchor peak candidates.

        Returns ``(selected_lag, per_anchor_pick, debug_info)``.

        Strategy:
        1.  Collect all peaks from all anchors, normalised to their aligned start.
        2.  Cluster peaks within *cluster_threshold* samples.
        3.  Score clusters by anchor diversity, correlation quality and tightness.
        4.  Bonus for proximity to *previous_successful_lag* (continuity).
        5.  Select the highest-scoring cluster's median lag.
        6.  For each anchor, pick the peak closest to the selected lag.
        """
        # ── Build flat candidate list ──
        candidates: list[dict[str, Any]] = []
        for ap in anchor_peaks:
            for pi, peak in enumerate(ap["top_peaks"]):
                obs_start = ap["search_start"] + int(peak["index"])
                lag = obs_start - ap["offset_samples"]
                candidates.append(
                    {
                        "lag": lag,
                        "anchor": ap["name"],
                        "peak_index": pi,
                        "score": peak["score"],
                        "observed_start": obs_start,
                    }
                )

        if not candidates:
            return 0, {}, {"selected_lag": 0, "previous_successful_lag": previous_successful_lag, "candidate_scores": [], "rejected": "no candidates"}

        # ── Cluster by lag proximity ──
        clusters: list[list[dict[str, Any]]] = []
        used: set[int] = set()
        for ci, cand in enumerate(candidates):
            if ci in used:
                continue
            cluster = [cand]
            used.add(ci)
            for cj, other in enumerate(candidates):
                if cj in used:
                    continue
                if abs(other["lag"] - cand["lag"]) <= cluster_threshold:
                    cluster.append(other)
                    used.add(cj)
            clusters.append(cluster)

        # ── Score each cluster ──
        scored: list[dict[str, Any]] = []
        for cluster in clusters:
            lags = [c["lag"] for c in cluster]
            median_lag = float(np.median(np.array(lags, dtype=np.float64)))
            unique_anchors = len({c["anchor"] for c in cluster})
            mean_score = float(np.mean([c["score"] for c in cluster]))
            lag_spread = float(np.std(np.array(lags, dtype=np.float64))) if len(lags) > 1 else 0.0

            prev_bonus = 0.0
            if previous_successful_lag is not None:
                dist = abs(median_lag - previous_successful_lag)
                prev_bonus = max(0.0, 1.0 - dist / max(cluster_threshold * 4.0, 1.0))

            total = (
                unique_anchors * 0.4
                + mean_score * 0.3
                + (1.0 - min(lag_spread / max(cluster_threshold, 1.0), 1.0)) * 0.2
                + prev_bonus * 0.1
            )
            scored.append(
                {
                    "median_lag": median_lag,
                    "unique_anchors": unique_anchors,
                    "mean_score": mean_score,
                    "lag_spread": lag_spread,
                    "prev_bonus": prev_bonus,
                    "score": total,
                    "members": cluster,
                }
            )

        scored.sort(key=lambda c: c["score"], reverse=True)
        best = scored[0]
        selected_lag = int(round(best["median_lag"]))

        # ── Pick each anchor's closest peak to selected lag ──
        per_anchor_pick: dict[str, int] = {}
        for ap in anchor_peaks:
            best_idx = 0
            best_dist = float("inf")
            for pi, peak in enumerate(ap["top_peaks"]):
                obs_start = ap["search_start"] + int(peak["index"])
                lag = obs_start - ap["offset_samples"]
                dist = abs(lag - selected_lag)
                if dist < best_dist:
                    best_dist = dist
                    best_idx = pi
            per_anchor_pick[ap["name"]] = best_idx

        debug = {
            "selected_lag": selected_lag,
            "previous_successful_lag": previous_successful_lag,
            "candidate_scores": [
                {
                    "lag": int(round(c["median_lag"])),
                    "anchors": c["unique_anchors"],
                    "mean_score": round(c["mean_score"], 5),
                    "spread": round(c["lag_spread"], 1),
                    "prev_bonus": round(c["prev_bonus"], 3),
                    "total": round(c["score"], 3),
                }
                for c in scored[:5]
            ],
            "rejected": f"{len(scored) - 1} lower-scoring clusters" if len(scored) > 1 else "only one cluster",
        }
        return selected_lag, per_anchor_pick, debug

    def _build_variable_window_response(self, impulse_response: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
        low_windowed, low_meta = self._window_impulse_response(
            impulse_response,
            sample_rate,
            post_seconds=IR_WINDOW_POST_LOW_SECONDS,
        )
        mid_windowed, mid_meta = self._window_impulse_response(
            impulse_response,
            sample_rate,
            post_seconds=IR_WINDOW_POST_SECONDS,
        )
        high_windowed, high_meta = self._window_impulse_response(
            impulse_response,
            sample_rate,
            post_seconds=IR_WINDOW_POST_HIGH_SECONDS,
        )
        fft_size = self._store._next_pow2(max(sample_rate, low_windowed.size * 2, mid_windowed.size * 2, high_windowed.size * 2))
        frequencies = np.fft.rfftfreq(fft_size, d=1.0 / sample_rate)
        low_magnitude = np.abs(np.fft.rfft(low_windowed, n=fft_size))
        mid_magnitude = np.abs(np.fft.rfft(mid_windowed, n=fft_size))
        high_magnitude = np.abs(np.fft.rfft(high_windowed, n=fft_size))

        blend = np.clip(
            (frequencies - IR_WINDOW_VARIABLE_LOW_HZ) / max(IR_WINDOW_VARIABLE_HIGH_HZ - IR_WINDOW_VARIABLE_LOW_HZ, 1.0),
            0.0,
            1.0,
        )
        blend = blend * blend * (3.0 - (2.0 * blend))
        upper_blend = np.clip(
            (frequencies - IR_WINDOW_VARIABLE_HIGH_HZ) / max(IR_WINDOW_VARIABLE_HIGH_HZ, 1.0),
            0.0,
            1.0,
        )
        upper_blend = upper_blend * upper_blend * (3.0 - (2.0 * upper_blend))
        low_mid = (low_magnitude * (1.0 - blend)) + (mid_magnitude * blend)
        magnitude = (low_mid * (1.0 - upper_blend)) + (high_magnitude * upper_blend)
        return frequencies, magnitude, {
            "method": "frequency-dependent IR window blend",
            "low_post_window_seconds": round(float(low_meta["post_window_seconds"]), 6),
            "mid_post_window_seconds": round(float(mid_meta["post_window_seconds"]), 6),
            "high_post_window_seconds": round(float(high_meta["post_window_seconds"]), 6),
            "low_to_mid_hz": round(float(IR_WINDOW_VARIABLE_LOW_HZ), 3),
            "mid_to_high_hz": round(float(IR_WINDOW_VARIABLE_HIGH_HZ), 3),
        }

    def _window_impulse_response(
        self,
        impulse_response: np.ndarray,
        sample_rate: int,
        *,
        post_seconds: float = IR_WINDOW_POST_SECONDS,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        ir64 = impulse_response.astype(np.float64)
        peak_index = int(np.argmax(np.abs(ir64)))
        pre_samples = max(32, int(round(sample_rate * IR_WINDOW_PRE_SECONDS)))
        post_samples = max(pre_samples * 2, int(round(sample_rate * post_seconds)))
        fade_samples = max(16, int(round(sample_rate * IR_WINDOW_FADE_SECONDS)))
        start = max(0, peak_index - pre_samples)
        end = min(ir64.size, peak_index + post_samples)
        window = np.zeros(ir64.size, dtype=np.float64)
        core = end - start
        if core <= 0:
            raise RuntimeError("Impulse-response window could not be constructed")
        shaped = np.ones(core, dtype=np.float64)
        rise = min(fade_samples, peak_index - start)
        fall = min(fade_samples, end - peak_index)
        if rise > 1:
            shaped[:rise] = 0.5 - 0.5 * np.cos(np.linspace(0.0, math.pi, rise, dtype=np.float64))
        if fall > 1:
            shaped[-fall:] = 0.5 + 0.5 * np.cos(np.linspace(0.0, math.pi, fall, dtype=np.float64))
        window[start:end] = shaped
        windowed = ir64 * window
        peak = float(np.max(np.abs(windowed)))
        return windowed, {
            "peak_index": peak_index,
            "peak_seconds": peak_index / sample_rate,
            "window_start_index": start,
            "window_end_index": end,
            "window_seconds": (end - start) / sample_rate,
            "pre_window_seconds": (peak_index - start) / sample_rate,
            "post_window_seconds": (end - peak_index) / sample_rate,
            "peak_dbfs": 20.0 * math.log10(max(peak, 1e-12)),
        }

    def _estimate_impulse_direct_arrival(
        self,
        impulse_response: np.ndarray,
        reference_impulse_response: np.ndarray,
        sample_rate: int,
    ) -> dict[str, Any]:
        ir_abs = np.abs(impulse_response.astype(np.float64))
        ref_abs = np.abs(reference_impulse_response.astype(np.float64))
        peak_index = int(np.argmax(ir_abs)) if ir_abs.size else 0
        reference_peak_index = int(np.argmax(ref_abs)) if ref_abs.size else 0
        peak = float(ir_abs[peak_index]) if ir_abs.size else 0.0
        threshold = peak * IR_DIRECT_RELATIVE_THRESHOLD
        search_pre_samples = max(1, int(round(sample_rate * IR_DIRECT_SEARCH_PRE_SECONDS)))
        search_start = max(0, peak_index - search_pre_samples)
        search_end = min(ir_abs.size, peak_index + 1)
        search_values = ir_abs[search_start:search_end]
        first_threshold_index: int | None = None
        if threshold > 0:
            threshold_crossings = np.flatnonzero(search_values >= threshold)
            if threshold_crossings.size:
                first_threshold_index = search_start + int(threshold_crossings[0])

        candidate_floor = peak * IR_DIRECT_CANDIDATE_FLOOR_RELATIVE
        candidates: list[dict[str, Any]] = []
        support_radius = max(4, int(round(sample_rate * IR_DIRECT_SUPPORT_WINDOW_SECONDS)))
        nearby_radius = max(support_radius + 4, int(round(sample_rate * IR_DIRECT_NEARBY_WINDOW_SECONDS)))
        if peak > 0 and search_values.size:
            for local_index in range(1, max(1, search_values.size - 1)):
                value = float(search_values[local_index])
                if value < candidate_floor:
                    continue
                if value < float(search_values[local_index - 1]) or value < float(search_values[local_index + 1]):
                    continue
                absolute_index = search_start + local_index
                relative = value / max(peak, 1e-12)
                support_start = max(search_start, absolute_index - support_radius)
                support_end = min(search_end, absolute_index + support_radius + 1)
                support_values = ir_abs[support_start:support_end]
                local_energy = float(np.sum(np.square(support_values, dtype=np.float64))) if support_values.size else 0.0
                earlier_start = max(search_start, absolute_index - nearby_radius)
                earlier_end = max(earlier_start, absolute_index - support_radius)
                later_start = min(search_end, absolute_index + support_radius + 1)
                later_end = min(search_end, absolute_index + nearby_radius + 1)
                earlier_values = ir_abs[earlier_start:earlier_end]
                later_values = ir_abs[later_start:later_end]
                nearby_reference = max(
                    float(np.max(earlier_values)) if earlier_values.size else 0.0,
                    float(np.max(later_values)) if later_values.size else 0.0,
                )
                prominence_relative = max(0.0, (value - nearby_reference) / max(peak, 1e-12))
                prominence_ratio = min(value / max(nearby_reference, 1e-12), 999.0)
                threshold_distance_samples = (
                    int(absolute_index - first_threshold_index) if first_threshold_index is not None else None
                )
                candidates.append(
                    {
                        "sample": int(absolute_index),
                        "seconds": round(float(absolute_index) / float(sample_rate), 6),
                        "offset_from_peak_samples": int(absolute_index - peak_index),
                        "offset_from_peak_ms": round(float(absolute_index - peak_index) / float(sample_rate) * 1000.0, 6),
                        "score": round(float(relative), 6),
                        "_score": float(relative),
                        "_local_energy": local_energy,
                        "_prominence_relative": float(prominence_relative),
                        "relative_db": round(20.0 * math.log10(max(relative, 1e-12)), 2),
                        "local_energy": round(local_energy, 8),
                        "prominence_relative": round(float(prominence_relative), 6),
                        "prominence_ratio": round(float(prominence_ratio), 3),
                        "distance_from_first_threshold_samples": threshold_distance_samples,
                    }
                )

        max_local_energy = max([float(item["_local_energy"]) for item in candidates] + [1e-12])
        for item in candidates:
            local_energy_relative = float(item["_local_energy"]) / max_local_energy
            prominence_score = min(float(item["_prominence_relative"]) / max(IR_DIRECT_PROMINENCE_REFERENCE, 1e-12), 1.0)
            support_score = (
                float(item["_score"]) * 0.45
                + local_energy_relative * 0.35
                + prominence_score * 0.20
            )
            threshold_distance_samples = item.get("distance_from_first_threshold_samples")
            weak_threshold_edge = (
                threshold_distance_samples is not None
                and 0 <= int(threshold_distance_samples) <= IR_DIRECT_THRESHOLD_EDGE_SAMPLES
                and float(item["_score"]) <= max(IR_DIRECT_WEAK_EARLY_RELATIVE, IR_DIRECT_RELATIVE_THRESHOLD)
                and local_energy_relative < 0.22
                and prominence_score < 0.45
            )
            stronger_impulse_region = (
                float(item["_score"]) >= 0.12
                or local_energy_relative >= 0.35
                or support_score >= 0.22
            )
            item["peak_score"] = round(float(item["_score"]), 6)
            item["local_energy_relative"] = round(local_energy_relative, 6)
            item["prominence_score"] = round(prominence_score, 6)
            item["support_score"] = round(float(support_score), 6)
            item["weak_threshold_edge"] = bool(weak_threshold_edge)
            item["stronger_impulse_region"] = bool(stronger_impulse_region)

        eligible_candidates = [
            item
            for item in candidates
            if float(item["_score"]) >= IR_DIRECT_RELATIVE_THRESHOLD
        ]
        if eligible_candidates:
            selected_candidate = eligible_candidates[0]
            skipped_early_candidate = None
            promotion_window_samples = max(
                IR_DIRECT_WEAK_EARLY_MIN_GAP_SAMPLES,
                int(round(sample_rate * IR_DIRECT_PROMOTION_WINDOW_SECONDS)),
            )
            first_sample = int(selected_candidate["sample"])
            early_candidate_is_weak = bool(selected_candidate.get("weak_threshold_edge")) or (
                float(selected_candidate["_score"]) <= IR_DIRECT_WEAK_EARLY_RELATIVE
                and (
                    selected_candidate.get("distance_from_first_threshold_samples") is None
                    or int(selected_candidate.get("distance_from_first_threshold_samples") or 0) <= IR_DIRECT_THRESHOLD_EDGE_SAMPLES
                )
            )
            if early_candidate_is_weak and len(eligible_candidates) > 1:
                selected_support = float(selected_candidate["support_score"])
                selected_score_candidate = float(selected_candidate["_score"])
                selected_energy = float(selected_candidate["local_energy_relative"])
                selected_prominence = float(selected_candidate["prominence_relative"])
                for candidate in eligible_candidates[1:]:
                    sample_gap = int(candidate["sample"]) - first_sample
                    if sample_gap > promotion_window_samples:
                        break
                    if sample_gap < IR_DIRECT_WEAK_EARLY_MIN_GAP_SAMPLES:
                        continue
                    candidate_support = float(candidate["support_score"])
                    candidate_score = float(candidate["_score"])
                    candidate_energy = float(candidate["local_energy_relative"])
                    candidate_prominence = float(candidate["prominence_relative"])
                    clearly_better_support = candidate_support >= selected_support * IR_DIRECT_PROMOTION_SUPPORT_RATIO
                    stronger_shape = (
                        candidate_score >= selected_score_candidate * IR_DIRECT_PROMOTION_SCORE_RATIO
                        or candidate_energy >= selected_energy * IR_DIRECT_PROMOTION_ENERGY_RATIO
                        or candidate_prominence >= selected_prominence * IR_DIRECT_PROMOTION_SCORE_RATIO
                    )
                    if clearly_better_support and stronger_shape and not bool(candidate.get("weak_threshold_edge")):
                        skipped_early_candidate = selected_candidate
                        selected_candidate = candidate
                        break
            direct_arrival_index = int(selected_candidate["sample"])
            selection_rule = (
                "skipped_weak_threshold_edge_for_stronger_impulse_region"
                if skipped_early_candidate is not None
                else "first_local_peak_above_threshold"
            )
        elif first_threshold_index is not None:
            direct_arrival_index = int(first_threshold_index)
            selection_rule = "first_threshold_crossing_fallback"
        else:
            direct_arrival_index = peak_index
            selection_rule = "global_peak_fallback"
        selected_score = float(ir_abs[direct_arrival_index]) / max(peak, 1e-12) if ir_abs.size else 0.0
        strongest_score = max([float(item["_score"]) for item in candidates] + [selected_score, 1e-12])
        if selection_rule == "first_local_peak_above_threshold" and len(candidates) > 1:
            best_by_score = max(candidates, key=lambda c: float(c["score"]))
            if best_by_score is not None and float(best_by_score["sample"]) != direct_arrival_index:
                best_score_val = float(ir_abs[int(best_by_score["sample"])]) / max(peak, 1e-12) if ir_abs.size and int(best_by_score["sample"]) < ir_abs.size else 0.0
                if 0.0 < best_score_val and best_score_val >= selected_score and best_score_val > selected_score * 1.02:
                    promotion_gap = int(best_by_score["sample"]) - direct_arrival_index
                    if 0 < promotion_gap <= int(round(sample_rate * 0.012)):
                        reference_peak_sample_backup = int(reference_peak_index)
                        direct_arrival_index = int(best_by_score["sample"])
                        selected_score = best_score_val
                        selection_rule = "promoted_to_best_score_in_window"
                        relative_samples = int(direct_arrival_index - reference_peak_sample_backup)
        selected_support = next(
            (float(item["support_score"]) for item in candidates if int(item["sample"]) == int(direct_arrival_index)),
            selected_score,
        )
        for item in candidates:
            item.pop("_score", None)
            item.pop("_local_energy", None)
            item.pop("_prominence_relative", None)
        candidate_summary_by_score = sorted(
            candidates,
            key=lambda item: (-float(item["score"]), -float(item["prominence_score"]), -float(item["support_score"]), int(item["sample"])),
        )[:IR_DIRECT_CANDIDATE_LIMIT]
        candidate_summary_chronological = sorted(
            candidates,
            key=lambda item: int(item["sample"]),
        )[:IR_DIRECT_CANDIDATE_LIMIT]
        relative_samples = int(direct_arrival_index - reference_peak_index)
        return {
            "direct_arrival_index": int(direct_arrival_index),
            "direct_seconds": float(direct_arrival_index) / float(sample_rate),
            "direct_relative_to_peak_db": round(
                20.0 * math.log10(max(float(ir_abs[direct_arrival_index]) / max(peak, 1e-12), 1e-12)),
                2,
            ),
            "direct_threshold_relative": float(IR_DIRECT_RELATIVE_THRESHOLD),
            "selection_rule": selection_rule,
            "selected_score": selected_score,
            "selected_support_score": selected_support,
            "confidence": selected_score / max(strongest_score, 1e-12),
            "first_threshold_index": first_threshold_index,
            "first_threshold_offset_from_peak_samples": (
                int(first_threshold_index - peak_index) if first_threshold_index is not None else None
            ),
            "weak_early_relative": float(IR_DIRECT_WEAK_EARLY_RELATIVE),
            "weak_early_min_gap_samples": int(IR_DIRECT_WEAK_EARLY_MIN_GAP_SAMPLES),
            "weak_early_next_ratio": float(IR_DIRECT_WEAK_EARLY_NEXT_RATIO),
            "promotion_window_samples": int(max(IR_DIRECT_WEAK_EARLY_MIN_GAP_SAMPLES, int(round(sample_rate * IR_DIRECT_PROMOTION_WINDOW_SECONDS)))),
            "promotion_support_ratio": float(IR_DIRECT_PROMOTION_SUPPORT_RATIO),
            "candidate_count": len(candidates),
            "candidates": candidate_summary_by_score,
            "candidates_by_score": candidate_summary_by_score,
            "candidates_chronological": candidate_summary_chronological,
            "reference_peak_index": int(reference_peak_index),
            "reference_peak_seconds": float(reference_peak_index) / float(sample_rate),
            "relative_samples": relative_samples,
            "relative_seconds": float(relative_samples) / float(sample_rate),
            "ir_peak_index": int(peak_index),
            "ir_peak_relative_to_reference_samples": int(peak_index - reference_peak_index),
            "promotion_applied": selection_rule == "promoted_to_best_score_in_window" or selection_rule == "skipped_weak_threshold_edge_for_stronger_impulse_region",
        }

    def _resample_signal(self, signal: np.ndarray, target_size: int) -> np.ndarray:
        signal64 = signal.astype(np.float64)
        if target_size <= 0:
            raise RuntimeError("Invalid resample target size")
        if signal64.size == target_size:
            return signal64
        if signal64.size < 2:
            raise RuntimeError("Sweep segment was too short to resample")
        source_positions = np.linspace(0.0, 1.0, signal64.size, dtype=np.float64)
        target_positions = np.linspace(0.0, 1.0, target_size, dtype=np.float64)
        return np.interp(target_positions, source_positions, signal64)

    def _find_sweep_start(self, signal: np.ndarray, reference_sweep: np.ndarray) -> int:
        signal64 = signal.astype(np.float64)
        sweep64 = reference_sweep.astype(np.float64)
        corr = self._fft_correlate(signal64, sweep64[::-1])
        valid = corr[sweep64.size - 1 : signal64.size]
        if valid.size == 0:
            raise RuntimeError("Unable to align recorded sweep")
        return int(np.argmax(np.abs(valid)))

    def _fft_correlate(self, signal: np.ndarray, kernel: np.ndarray) -> np.ndarray:
        fft_size = self._store._next_pow2(signal.size + kernel.size - 1)
        signal_fft = np.fft.rfft(signal, n=fft_size)
        kernel_fft = np.fft.rfft(kernel, n=fft_size)
        corr = np.fft.irfft(signal_fft * kernel_fft, n=fft_size)
        return corr[: signal.size + kernel.size - 1]

    def _fft_convolve(self, signal: np.ndarray, kernel: np.ndarray) -> np.ndarray:
        fft_size = self._store._next_pow2(signal.size + kernel.size - 1)
        signal_fft = np.fft.rfft(signal, n=fft_size)
        kernel_fft = np.fft.rfft(kernel, n=fft_size)
        convolved = np.fft.irfft(signal_fft * kernel_fft, n=fft_size)
        return convolved[: signal.size + kernel.size - 1]
