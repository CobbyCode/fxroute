"""L/R repeat and Advanced Measurement execution owner."""

from __future__ import annotations

import logging
import math
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

logger = logging.getLogger(__name__)

SWEEP_V2_SECONDS = 11.0
SWEEP_V2_LEAD_IN_SECONDS = 0.5
SWEEP_V2_TAIL_SECONDS = 1.25
SWEEP_START_HZ = 10.0
SWEEP_END_HZ = 22_000.0
TRACE_COLORS = ["#6ee7b7", "#a78bfa", "#f59e0b", "#60a5fa", "#f472b6", "#f87171"]
MEASUREMENT_SCOPE_NOTE = (
    "FXRoute measures with a host-local sweep through the active PipeWire output and selected microphone input. "
    "The result is a practical response trace for comparison and PEQ drafting, independent of the active DSP preset."
)
LR_REPEAT_ELECTRICAL_TIMING_CLUSTER_MS = 0.35
LR_REPEAT_ACOUSTIC_TIMING_CLUSTER_MS = 0.75
LR_REPEAT_PAIRED_DELTA_CLUSTER_MS = 0.35
LR_REPEAT_PAIRED_MIN_CLUSTER_SIZE = 2


class MeasurementRepeatRunner:
    """Own L/R repeat sequencing, averaging, summaries, and temporary files."""

    def __init__(self, store):
        self._store = store

    def execute(self, job: dict[str, Any]) -> dict[str, Any]:
        return self._execute_lr_repeat_job(job)

    def _pre_average_er_captures(
        self,
        capture_paths: list[str],
        *,
        playback_path: str | Path,
        sample_rate: int,
        mic_input_channel_index: int,
        electrical_reference_channel_index: int,
        calibration_curve: tuple[np.ndarray, np.ndarray] | None,
        reference_sweep: np.ndarray,
        inverse_sweep: np.ndarray,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Pre-average ER-aligned captures before deconvolution.

        Returns (averaged_analysis, intermediate_debug) or raises RuntimeError.
        On failure, caller falls back to per-sweep analysis.
        """
        if len(capture_paths) < 2:
            raise RuntimeError("Pre-average needs at least 2 captures")

        loaded: list[tuple[int, np.ndarray]] = []
        for path in capture_paths:
            sr, data = self._store._load_wav_array(Path(path))
            if sr != sample_rate:
                raise RuntimeError(f"Unexpected sample rate in {path}: {sr} vs {sample_rate}")
            loaded.append((sr, data))

        # Extract reference (ER) channel from each capture
        er_signals: list[np.ndarray] = []
        mic_signals: list[np.ndarray] = []
        for _sr, data in loaded:
            er_ch = self._store._select_analysis_channel(data, channel="", channel_index=electrical_reference_channel_index)
            mic_ch = self._store._select_analysis_channel(data, channel="", channel_index=mic_input_channel_index)
            er_signals.append(er_ch)
            mic_signals.append(mic_ch)

        er_timings: list[dict[str, Any]] = []
        for er_ch in er_signals:
            coarse_start = self._store._find_sweep_start(er_ch, reference_sweep)
            er_timings.append(
                self._store._estimate_sweep_timing(
                    er_ch,
                    reference_sweep,
                    coarse_start,
                    sample_rate,
                    allow_drift_compensation=False,
                )
            )

        # Align all ER signals to the first one via cross-correlation.
        # The raw shifts can be large when the recorder starts each repeat
        # capture at a slightly different sample. That is correctable. What
        # matters for pre-averaging is the residual alignment after applying
        # the ER-derived shifts.
        ref_er = er_signals[0]
        aligned_mic = [mic_signals[0]]
        aligned_er = [er_signals[0]]
        valid_ranges: list[tuple[int, int]] = [(0, mic_signals[0].shape[0])]
        alignment_shifts: list[int] = [0]
        applied_shifts: list[int] = [0]

        for i in range(1, len(er_signals)):
            shift = self._store._compute_alignment_shift(ref_er, er_signals[i], sample_rate)
            alignment_shifts.append(shift)
            applied_shift = -shift
            applied_shifts.append(applied_shift)
            aligned_mic.append(self._store._shift_signal(mic_signals[i], applied_shift))
            aligned_er.append(self._store._shift_signal(er_signals[i], applied_shift))
            if applied_shift > 0:
                valid_ranges.append((applied_shift, mic_signals[i].shape[0]))
            elif applied_shift < 0:
                valid_ranges.append((0, mic_signals[i].shape[0] + applied_shift))
            else:
                valid_ranges.append((0, mic_signals[i].shape[0]))

        sample_spread = max(alignment_shifts) - min(alignment_shifts)
        spread_limit = self._store._er_pre_average_sample_spread_limit(sample_rate)
        overlap_start = max(start for start, _end in valid_ranges)
        overlap_end = min(end for _start, end in valid_ranges)
        if overlap_end <= overlap_start:
            raise RuntimeError(
                f"ER aligned captures have no valid overlap after shifts "
                f"{alignment_shifts} (ranges: {valid_ranges})"
            )
        overlap_len = overlap_end - overlap_start
        min_required = int(round(sample_rate * (SWEEP_V2_SECONDS + SWEEP_V2_TAIL_SECONDS)))
        if overlap_len < min_required:
            raise RuntimeError(
                f"ER aligned captures valid overlap {overlap_len} samples is too short "
                f"for pre-average QC (minimum {min_required}; shifts: {alignment_shifts}; "
                f"ranges: {valid_ranges})"
            )

        # Verify residual inter-capture alignment after correction on the same
        # valid overlap that will be averaged. Rejecting only the raw shift
        # spread would defeat ER-aligned pre-averaging when repeat captures
        # have harmless capture-start jitter.
        overlap_er = [s[overlap_start:overlap_end] for s in aligned_er]
        residual_shifts = [
            0,
            *[
                self._store._compute_alignment_shift(overlap_er[0], overlap_er[i], sample_rate)
                for i in range(1, len(overlap_er))
            ],
        ]
        residual_spread = max(residual_shifts) - min(residual_shifts)
        if residual_spread > spread_limit:
            raise RuntimeError(
                f"ER residual alignment spread {residual_spread} samples exceeds limit "
                f"{spread_limit} after shifts {alignment_shifts} "
                f"(residual shifts: {residual_shifts}; overlap: "
                f"{overlap_start}..{overlap_end})"
            )

        # All aligned — average only samples that are present in every shifted
        # capture. This keeps zero-padding from contaminating sweep start/end
        # regions used by the normal QC.
        averaged_mic = np.mean([s[overlap_start:overlap_end] for s in aligned_mic], axis=0)

        # Average the aligned ER signals over the same valid overlap (for
        # consistency check and downstream ER timing analysis).
        averaged_er = np.mean(overlap_er, axis=0)

        adjusted_timing_starts = [
            int(timing["aligned_start"]) + int(applied_shift) - int(overlap_start)
            for timing, applied_shift in zip(er_timings, applied_shifts)
        ]
        adjusted_timing_ends = [
            start + int(timing["observed_sweep_samples"])
            for start, timing in zip(adjusted_timing_starts, er_timings)
        ]
        override_start = int(round(float(np.median(adjusted_timing_starts))))
        override_sweep_samples = int(round(float(np.median([
            int(reference_sweep.size)
            for _timing in er_timings
        ]))))
        if override_start < 0:
            raise RuntimeError(
                f"ER pre-average timing override starts before valid overlap "
                f"({override_start}; adjusted starts: {adjusted_timing_starts}; "
                f"overlap: {overlap_start}..{overlap_end})"
            )
        if override_start + override_sweep_samples > averaged_mic.shape[0]:
            raise RuntimeError(
                f"ER pre-average timing override exceeds valid overlap "
                f"({override_start}+{override_sweep_samples}>{averaged_mic.shape[0]}; "
                f"adjusted starts: {adjusted_timing_starts}; adjusted ends: {adjusted_timing_ends})"
            )
        per_capture_start_scores = [float(timing.get("start_score") or 0.0) for timing in er_timings]
        per_capture_end_scores = [float(timing.get("end_score") or 0.0) for timing in er_timings]
        timing_override = {
            "alignment_samples": override_start,
            "observed_sweep_samples": override_sweep_samples,
            "stretch_ratio": 1.0,
            "drift_ppm": 0.0,
            "total_drift_samples": 0,
            "estimated_ppm": float(np.median([
                float(timing.get("estimated_ppm", timing.get("drift_ppm") or 0.0))
                for timing in er_timings
            ])),
            "estimated_total_drift_samples": int(round(float(np.median([
                int(timing.get("estimated_total_drift_samples", timing.get("total_drift_samples") or 0))
                for timing in er_timings
            ])))),
            "raw_global_ppm": float(np.median([
                float(timing.get("raw_global_ppm", timing.get("estimated_ppm", 0.0)))
                for timing in er_timings
            ])),
            "fit_start_before_samples": override_start,
            "fit_start_after_samples": override_start,
            "fit_end_before_samples": override_start + int(reference_sweep.size),
            "fit_end_after_samples": override_start + override_sweep_samples,
            "compensated": False,
            "anchor_seconds": float(np.median([
                float(timing.get("anchor_seconds") or 0.0)
                for timing in er_timings
            ])),
            "start_score": min(per_capture_start_scores),
            "end_score": min(per_capture_end_scores),
            "anchor_strategy": "ER pre-average central constant-delay override",
            "drift_compensation_policy": "constant-delay-er-reference",
            "anchor_matches": [],
        }

        # Write stereo WAV: ch0 = mic, ch1 = ER (so _analyze_sweep_capture
        # retains access to the ER timing reference)
        averaged_path = self._store.captures_dir / f"preavg-{uuid4().hex[:12]}.wav"
        try:
            stereo = np.column_stack([
                averaged_mic,
                averaged_er,
            ])
            self._store._write_stereo_wav(averaged_path, sample_rate, stereo)

            # Run standard analysis — mic on ch0, ER on ch1
            try:
                analysis = self._store._analyze_sweep_capture(
                    averaged_path,
                    expected_sample_rate=sample_rate,
                    channel="",
                    reference_sweep=reference_sweep,
                    inverse_sweep=inverse_sweep,
                    calibration_curve=calibration_curve,
                    capture_label="ER pre-averaged",
                    reference_channel_index=1,
                    analysis_channel_index=0,
                    reference_channel_label="reference",
                    timing_override=timing_override,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"ER pre-averaged QC failed after valid-overlap average "
                    f"(overlap {overlap_start}..{overlap_end}, "
                    f"{overlap_len} samples; shifts: {alignment_shifts}; "
                    f"residual shifts: {residual_shifts}; timing override: "
                    f"{override_start}+{override_sweep_samples}; adjusted starts: "
                    f"{adjusted_timing_starts}): {exc}"
                ) from exc

            shifts_ms = [round(s / sample_rate * 1000.0, 4) for s in alignment_shifts]
            sample_spread = max(alignment_shifts) - min(alignment_shifts) if len(alignment_shifts) > 1 else 0
            clock = analysis.get("clock") if isinstance(analysis.get("clock"), dict) else {}
            debug = {
                "alignment_shifts_samples": alignment_shifts,
                "alignment_shifts_ms": shifts_ms,
                "alignment_spread_samples": sample_spread,
                "alignment_spread_ms": round(sample_spread / sample_rate * 1000.0, 4),
                "residual_alignment_shifts_samples": residual_shifts,
                "residual_alignment_shifts_ms": [round(s / sample_rate * 1000.0, 4) for s in residual_shifts],
                "residual_alignment_spread_samples": residual_spread,
                "residual_alignment_spread_ms": round(residual_spread / sample_rate * 1000.0, 4),
                "alignment_spread_gate": "post-shift-residual",
                "valid_ranges_samples": [[int(start), int(end)] for start, end in valid_ranges],
                "valid_overlap_start_sample": int(overlap_start),
                "valid_overlap_end_sample": int(overlap_end),
                "valid_overlap_length_samples": int(overlap_len),
                "valid_overlap_start_ms": round(overlap_start / sample_rate * 1000.0, 4),
                "valid_overlap_end_ms": round(overlap_end / sample_rate * 1000.0, 4),
                "valid_overlap_length_ms": round(overlap_len / sample_rate * 1000.0, 4),
                "pre_average_timing_override_start_sample": int(override_start),
                "pre_average_timing_override_sweep_samples": int(override_sweep_samples),
                "pre_average_adjusted_timing_starts_samples": [int(item) for item in adjusted_timing_starts],
                "pre_average_adjusted_timing_ends_samples": [int(item) for item in adjusted_timing_ends],
                "pre_average_per_capture_start_scores": [round(item, 6) for item in per_capture_start_scores],
                "pre_average_per_capture_end_scores": [round(item, 6) for item in per_capture_end_scores],
                "pre_average_start_score": round(float(clock.get("start_score") or 0.0), 6),
                "pre_average_end_score": round(float(clock.get("end_score") or 0.0), 6),
                "capture_count": len(capture_paths),
                "pre_average_applied": True,
            }
            return analysis, debug
        finally:
            # The pre-average WAV is a temporary artifact: remove it on every
            # exit path, including write and analysis failures.
            try:
                averaged_path.unlink(missing_ok=True)
            except Exception:
                pass
    @staticmethod
    def _write_stereo_wav(path: Path, sample_rate: int, data: np.ndarray) -> None:
        """Write a stereo 16-bit PCM WAV file (ch0=mic, ch1=ER)."""
        samples = np.clip(data, -1.0, 1.0)
        int16 = (samples * 32767).astype(np.int16)
        with wave.open(str(path), 'wb') as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(int16.tobytes())

    @staticmethod
    def _er_pre_average_sample_spread_limit(sample_rate: int) -> int:
        """Maximum allowed ER alignment spread across captures, in samples.

        Sample-rate independent: always 3 samples regardless of rate.
        At 48 kHz: 3 samples ≈ 0.063 ms
        At 96 kHz: 3 samples ≈ 0.031 ms

        Deconvolution requires sample-exact alignment; even a few samples
        of inter-capture drift will smear the averaged impulse response.
        Spread = max(alignment_shifts) - min(alignment_shifts).
        """
        return 3

    @staticmethod
    def _compute_alignment_shift(reference: np.ndarray, other: np.ndarray, sample_rate: int) -> int:
        """Compute the integer sample shift to align `other` to `reference` via cross-correlation."""
        n = reference.shape[0] + other.shape[0] - 1
        fft_n = 1
        while fft_n < n:
            fft_n <<= 1
        ref_fft = np.fft.rfft(reference, n=fft_n)
        oth_fft = np.fft.rfft(other, n=fft_n)
        corr = np.fft.irfft(ref_fft.conj() * oth_fft, n=fft_n)
        peak = int(np.argmax(np.abs(corr)))
        if peak > fft_n // 2:
            peak -= fft_n
        return peak

    @staticmethod
    def _shift_signal(signal: np.ndarray, shift: int) -> np.ndarray:
        """Shift signal by integer samples (positive = delay, negative = advance)."""
        if shift == 0:
            return signal
        result = np.zeros_like(signal)
        if shift > 0:
            if shift < signal.shape[0]:
                result[shift:] = signal[:-shift]
        else:
            trim = -shift
            if trim < signal.shape[0]:
                result[:signal.shape[0] - trim] = signal[trim:]
        return result
    def _execute_lr_repeat_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job["id"])
        repeat_count = int(job.get("repeat_count") or 1)
        captures: dict[str, list[dict[str, Any]]] = {"left": [], "right": []}
        # Track raw capture metadata for ER pre-averaging
        raw_meta: dict[str, list[dict[str, Any]]] = {"left": [], "right": []}
        total_sweeps = repeat_count * 2
        try:
            return self._execute_lr_repeat_sweeps(
                job, job_id, repeat_count, captures, raw_meta, total_sweeps
            )
        finally:
            self._cleanup_lr_repeat_sweep_wavs(job_id)

    def _execute_lr_repeat_sweeps(
        self,
        job: dict[str, Any],
        job_id: str,
        repeat_count: int,
        captures: dict[str, list[dict[str, Any]]],
        raw_meta: dict[str, list[dict[str, Any]]],
        total_sweeps: int,
    ) -> dict[str, Any]:
        sweep_number = 0
        for repeat_index in range(repeat_count):
            for channel in ("left", "right"):
                if job_id in self._store._cancelled_jobs:
                    raise RuntimeError("Measurement cancelled.")
                sweep_number += 1
                with self._store._job_process_lock:
                    live_job = self._store._jobs.get(job_id)
                    if live_job is not None and not self._store._is_terminal_job_status(live_job.get("status")):
                        live_job["message"] = f"L/R repeat {sweep_number}/{total_sweeps}: {channel.upper()}{repeat_index + 1}…"
                        live_job["updated_at"] = self._store._utc_now()
                        self._store._persist_job(live_job)
                capture_job = deepcopy(job)
                # Unique ID per sweep so each capture gets its own WAV file
                sweep_id = f"{job_id}-repeat{repeat_index + 1}-{channel}"
                capture_job["id"] = sweep_id
                capture_job["_owner_job_id"] = job_id
                capture_job["channel"] = channel
                capture_job["capture_profile"] = "lr-repeat"
                result = self._store._execute_capture_job(capture_job)
                captures[channel].append(result["measurement"])
                raw_meta[channel].append({
                    "capture_path": result.get("_capture_path"),
                    "playback_path": result.get("_playback_path"),
                    "sample_rate": result.get("_sample_rate"),
                    "mic_input_channel_index": result.get("_mic_input_channel_index"),
                    "electrical_reference_channel_index": result.get("_electrical_reference_channel_index"),
                    "use_electrical_reference": result.get("_use_electrical_reference"),
                    "calibration_curve": result.get("_calibration_curve"),
                    "sweep_seconds": result.get("_sweep_seconds"),
                    "lead_in_seconds": result.get("_lead_in_seconds"),
                    "tail_seconds": result.get("_tail_seconds"),
                    "record_preroll_seconds": result.get("_record_preroll_seconds"),
                    "record_postroll_seconds": result.get("_record_postroll_seconds"),
                    "record_duration_seconds": result.get("_record_duration_seconds"),
                })

        # Try ER pre-averaging per side, fall back to standard per-sweep analysis
        pre_avg_debug: dict[str, Any] = {}
        for side_channel in ("left", "right"):
            side_meta = raw_meta[side_channel]
            # All sweeps must have actually used electrical reference (not just configured it)
            if not side_meta or not all(
                m.get("use_electrical_reference") and
                not (captures[side_channel][i].get("analysis") or {}).get("reference_path", {}).get("electrical_reference_fallback")
                for i, m in enumerate(side_meta)
            ):
                continue
            if not all(m.get("capture_path") for m in side_meta):
                continue
            try:
                er_idx = side_meta[0]["electrical_reference_channel_index"]
                mic_idx = side_meta[0]["mic_input_channel_index"]
                sr = side_meta[0]["sample_rate"]
                cal = side_meta[0].get("calibration_curve")
                pb_path = Path(side_meta[0]["playback_path"])
                _pb_sr, pb_data = self._store._load_wav_array(pb_path)
                if _pb_sr != sr:
                    raise RuntimeError(f"Playback sample rate mismatch: {_pb_sr} vs {sr}")
                if pb_data.ndim > 1:
                    # Stereo playback: select the correct sweep channel
                    pb_ch = 1 if side_channel == "right" else 0
                    pb_data = pb_data[:, pb_ch]
                _sweep_sec = float(side_meta[0].get("sweep_seconds") or SWEEP_V2_SECONDS)
                _lead_in_sec = float(side_meta[0].get("lead_in_seconds") or SWEEP_V2_LEAD_IN_SECONDS)
                _lead_in_samp = int(round(sr * _lead_in_sec))
                _sweep_samp = int(round(sr * _sweep_sec))
                reference_sweep = pb_data[_lead_in_samp:_lead_in_samp + _sweep_samp].astype(np.float64)
                start_hz = float(side_meta[0].get("start_hz") or SWEEP_START_HZ)
                end_hz = float(side_meta[0].get("end_hz") or SWEEP_END_HZ)
                inverse_sweep = self._store._build_inverse_sweep(
                    reference_sweep,
                    sample_rate=sr,
                    duration_seconds=_sweep_sec,
                    start_hz=start_hz,
                    end_hz=end_hz,
                )
                avg_analysis, debug = self._store._pre_average_er_captures(
                    [m["capture_path"] for m in side_meta],
                    playback_path=pb_path,
                    sample_rate=sr,
                    mic_input_channel_index=mic_idx,
                    electrical_reference_channel_index=er_idx,
                    calibration_curve=cal,
                    reference_sweep=reference_sweep,
                    inverse_sweep=inverse_sweep,
                )
                source_traces = [
                    self._store._select_merge_trace(item, "traces", preferred_role="trusted")
                    for item in captures[side_channel]
                ]
                source_trace_average = self._store._average_merge_traces(
                    source_traces,
                    label=f"{side_channel.title()} repeat · magnitude average of individual sweeps",
                    kind="lr-repeat-er-source-magnitude-average",
                    role="er-source-magnitude-average",
                    color=TRACE_COLORS[2],
                )
                source_review_traces = [
                    self._store._select_merge_trace(item, "review_traces", preferred_role="raw-review", required=False)
                    for item in captures[side_channel]
                ]
                source_review_average = None
                if all(source_review_traces):
                    source_review_average = self._store._average_merge_traces(
                        source_review_traces,
                        label=f"{side_channel.title()} repeat · raw magnitude average of individual sweeps",
                        kind="lr-repeat-er-source-review-magnitude-average",
                        role="er-source-review-magnitude-average",
                        color=TRACE_COLORS[3],
                    )
                # Build effective measurement from pre-averaged analysis
                # Use the averaged analysis traces (not the first capture's)
                effective = deepcopy(captures[side_channel][0])
                effective_analysis = effective.get("analysis") or {}
                effective_analysis.update(avg_analysis)
                effective["analysis"] = effective_analysis
                # Replace traces with those from the averaged analysis so the
                # displayed frequency response reflects the pre-averaged result
                if "traces" in avg_analysis:
                    effective["traces"] = avg_analysis["traces"]
                if "review_traces" in avg_analysis:
                    effective["review_traces"] = avg_analysis["review_traces"]
                effective["_pre_averaged"] = True
                effective["_pre_average_debug"] = debug
                effective["_pre_average_source_trace_average"] = source_trace_average
                if source_review_average is not None:
                    effective["_pre_average_source_review_average"] = source_review_average
                captures[side_channel] = [effective]
                pre_avg_debug[side_channel] = debug
            except Exception as exc:
                logger.warning("ER pre-average failed for %s, using per-sweep: %s", side_channel, exc)
                pre_avg_debug[side_channel] = {"pre_average_applied": False, "error": str(exc)}

        l_pre = pre_avg_debug.get("left", {}).get("pre_average_applied", False)
        r_pre = pre_avg_debug.get("right", {}).get("pre_average_applied", False)

        if l_pre and r_pre:
            # Both sides pre-averaged: build paired summary directly
            l_eff = captures["left"][0]
            r_eff = captures["right"][0]
            l_summary = self._store._build_pre_averaged_lr_summary(
                l_eff, r_eff,
                side="left",
                base_name=str(job.get("base_name") or "L/R Repeat"),
                repeat_count=repeat_count,
                pre_avg_debug=pre_avg_debug,
            )
            r_summary = self._store._build_pre_averaged_lr_summary(
                r_eff, l_eff,
                side="right",
                base_name=str(job.get("base_name") or "L/R Repeat"),
                repeat_count=repeat_count,
                pre_avg_debug=pre_avg_debug,
            )
        else:
            l_summary, r_summary = self._store.summarize_lr_repeat_paired(
                captures["left"],
                captures["right"],
                base_name=str(job.get("base_name") or "L/R Repeat"),
                repeat_count=repeat_count,
            )
            for s in (l_summary, r_summary):
                side = s.get("channel", "")
                dbg = pre_avg_debug.get(side, {})
                if dbg.get("pre_average_applied"):
                    s.setdefault("notes", []).append(
                        f"ER pre-averaged ({dbg.get('capture_count', '?')} captures, "
                        f"shifts: {dbg.get('alignment_shifts_samples', [])} samples)"
                    )

        return {
            "measurements": [l_summary, r_summary],
            "base_name": str(job.get("base_name") or "L/R Repeat"),
            "message": "L/R repeat finished. Review the combined L and R results, then save them together.",
            "scope_note": MEASUREMENT_SCOPE_NOTE,
        }

    def _cleanup_lr_repeat_sweep_wavs(self, job_id: str) -> None:
        """Delete the per-sweep capture/playback WAVs of an L/R repeat job.

        Runs on every exit path (success, failure, cancel). The glob is
        anchored on the unique job id, so it can never touch another job's
        files; sweeps that failed before their metadata was recorded are
        still covered by the name pattern.
        """
        for directory in (self._store.captures_dir, self._store.playbacks_dir):
            for path in directory.glob(f"{job_id}-repeat*.wav"):
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    logger.warning("Failed to remove measurement sweep WAV %s", path)
    def summarize_repeat_measurements(
        self,
        measurements: list[dict[str, Any]],
        *,
        base_name: str,
        channel: str,
        repeat_count: int,
    ) -> dict[str, Any]:
        if not measurements:
            raise ValueError("L/R repeat summary needs at least one measurement")
        normalized = [self._store._normalize_measurement(item) for item in measurements]
        timings = []
        for index, measurement in enumerate(normalized):
            analysis = measurement.get("analysis") if isinstance(measurement.get("analysis"), dict) else {}
            reference_path = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
            impulse = analysis.get("impulse_response") if isinstance(analysis.get("impulse_response"), dict) else {}
            timing_ms = reference_path.get("acoustic_arrival_corrected_ms", impulse.get("arrival_ms"))
            try:
                timing_ms = float(timing_ms)
            except (TypeError, ValueError):
                continue
            if math.isfinite(timing_ms):
                timings.append({
                    "index": index,
                    "timing_ms": timing_ms,
                    "electrical_reference_used": bool(reference_path.get("electrical_reference_used")),
                })
        electrical_reference_used = bool(timings) and all(item["electrical_reference_used"] for item in timings)
        cluster_limit_ms = (
            LR_REPEAT_ELECTRICAL_TIMING_CLUSTER_MS
            if electrical_reference_used
            else LR_REPEAT_ACOUSTIC_TIMING_CLUSTER_MS
        )
        accepted_indices, timing_center_ms, timing_spread_ms = self._store._select_repeat_timing_cluster(
            timings,
            repeat_count=repeat_count,
            cluster_limit_ms=cluster_limit_ms,
        )
        stable = bool(accepted_indices)
        magnitude_indices = accepted_indices or list(range(len(normalized)))
        accepted_measurements = [normalized[index] for index in magnitude_indices]
        side_label = "L" if channel == "left" else "R"
        summary_name = f"{str(base_name or 'L/R Repeat').strip()} · {side_label}"
        timestamp = datetime.now(timezone.utc).replace(microsecond=0)
        measurement_id = f"lr-repeat-{channel}-{timestamp.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
        payload = deepcopy(normalized[0])
        payload.update({
            "id": measurement_id,
            "name": summary_name,
            "created_at": timestamp.isoformat().replace("+00:00", "Z"),
            "channel": channel,
            "measurement_kind": "lr-repeat-summary",
            "traces": [
                self._store._average_merge_traces(
                    [self._store._select_merge_trace(item, "traces", preferred_role="trusted") for item in accepted_measurements],
                    label=f"{summary_name} · trusted average",
                    kind="lr-repeat-sweep-response",
                    role="trusted",
                    color=TRACE_COLORS[0],
                )
            ],
            "notes": [
                MEASUREMENT_SCOPE_NOTE,
                f"Same-position L/R repeat summary from {len(normalized)} {side_label} sweep(s).",
                "Intermediate repeat sweeps were processed internally and were not saved as normal measurements.",
            ],
        })
        review_traces = [
            self._store._select_merge_trace(item, "review_traces", preferred_role="raw-review", required=False)
            for item in accepted_measurements
        ]
        if all(review_traces):
            payload["review_traces"] = [
                self._store._average_merge_traces(
                    review_traces,
                    label=f"{summary_name} · raw/full-band review average",
                    kind="lr-repeat-sweep-response-review",
                    role="raw-review",
                    color=TRACE_COLORS[1],
                )
            ]
        else:
            payload.pop("review_traces", None)
        analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
        analysis = deepcopy(analysis)
        analysis["method"] = "same-position-lr-repeat-average"
        reference_path = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
        reference_path = deepcopy(reference_path)
        reference_source = ""
        if electrical_reference_used:
            reference_channel = reference_path.get("electrical_reference_input_channel")
            reference_source = f"electrical-input-channel-{reference_channel}" if reference_channel else "electrical-input"
        elif reference_path.get("capture_mode"):
            reference_source = str(reference_path["capture_mode"])
        analysis["lr_repeat"] = {
            "repeat_count": int(repeat_count),
            "accepted_runs": len(accepted_indices),
            "rejected_runs": len(normalized) - len(accepted_indices),
            "accepted_run_numbers": [index + 1 for index in accepted_indices],
            "rejected_run_numbers": [index + 1 for index in range(len(normalized)) if index not in accepted_indices],
            "timing_spread_ms": timing_spread_ms,
            "timing_method": "electrical-reference-cluster-median" if electrical_reference_used else "acoustic-cluster-median",
            "electrical_reference_used": electrical_reference_used,
            "reference_source": reference_source,
            "timing_stable": stable,
        }
        impulse = analysis.get("impulse_response") if isinstance(analysis.get("impulse_response"), dict) else {}
        impulse = deepcopy(impulse)
        sample_rate = int(analysis.get("sample_rate") or 0)
        if stable and timing_center_ms is not None:
            arrival_samples = int(round(timing_center_ms / 1000.0 * sample_rate)) if sample_rate > 0 else None
            reference_path.update({
                "timing_status": "lr-repeat",
                "timing_label": "L/R repeat timing",
                "stability": "stable",
                "acoustic_arrival_corrected_ms": timing_center_ms,
                "acoustic_arrival_corrected_seconds": round(timing_center_ms / 1000.0, 9),
                "acoustic_arrival_corrected_samples": arrival_samples,
            })
            impulse.update({
                "arrival_ms": timing_center_ms,
                "arrival_seconds": round(timing_center_ms / 1000.0, 9),
                "arrival_samples": arrival_samples,
            })
        else:
            reference_path.update({
                "timing_status": "lr-repeat-unstable",
                "timing_label": "L/R repeat timing unstable",
                "stability": "unstable",
            })
            for key in ("acoustic_arrival_corrected_ms", "acoustic_arrival_corrected_seconds", "acoustic_arrival_corrected_samples"):
                reference_path.pop(key, None)
            for key in ("arrival_ms", "arrival_seconds", "arrival_samples", "direct_arrival_index"):
                impulse.pop(key, None)
            analysis["direct_arrival_timing_available"] = False
            payload["notes"].append("No stable timing cluster was found; timing-sensitive L/R alignment must not use this summary.")
        analysis["reference_path"] = reference_path
        analysis["impulse_response"] = impulse
        payload["analysis"] = analysis
        return self._store._normalize_measurement(payload)

    def _extract_measurement_timing_ms(self, measurement: dict[str, Any]) -> float | None:
        """Extract corrected arrival timing from a measurement, or None unavailable."""
        analysis = measurement.get("analysis") if isinstance(measurement.get("analysis"), dict) else {}
        reference_path = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
        impulse = analysis.get("impulse_response") if isinstance(analysis.get("impulse_response"), dict) else {}
        timing_ms = reference_path.get("acoustic_arrival_corrected_ms", impulse.get("arrival_ms"))
        try:
            timing_ms = float(timing_ms)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(timing_ms):
            return None
        return timing_ms

    def _select_paired_delta_cluster(
        self,
        pair_deltas_ms: list[float],
        *,
        cluster_limit_ms: float,
        min_cluster_size: int,
    ) -> tuple[list[int], float | None, float | None]:
        """Cluster paired L/R deltas and return accepted pair indices, center delta, spread."""
        if not pair_deltas_ms:
            return [], None, None
        indexed = list(enumerate(pair_deltas_ms))
        candidates = []
        for anchor_idx, anchor_val in indexed:
            cluster = [
                (idx, val) for idx, val in indexed
                if abs(val - anchor_val) <= cluster_limit_ms
            ]
            if not cluster:
                continue
            values = sorted(val for _, val in cluster)
            spread = values[-1] - values[0]
            candidates.append((len(cluster), -spread, cluster))
        if not candidates:
            return [], None, None
        _size, _neg_spread, best = max(candidates, key=lambda item: (item[0], item[1]))
        if len(best) < min_cluster_size:
            return [], None, None
        best_values = sorted(val for _, val in best)
        center = float(np.median(best_values))
        spread = best_values[-1] - best_values[0]
        return sorted(int(idx) for idx, _ in best), round(center, 6), round(spread, 6)

    def summarize_lr_repeat_paired(
        self,
        left_measurements: list[dict[str, Any]],
        right_measurements: list[dict[str, Any]],
        *,
        base_name: str,
        repeat_count: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Summarize L/R Repeat using paired-delta clustering.

        For each pair (L_i, R_i), compute delta_i = R_i - L_i.
        Cluster the deltas, accept pairs in the best cluster,
        then derive L and R timing from accepted pairs only.

        Returns (left_summary, right_summary) with shared paired-delta metadata.
        """
        pair_deltas: list[tuple[int, float]] = []
        for idx in range(min(len(left_measurements), len(right_measurements), repeat_count)):
            l_timing = self._store._extract_measurement_timing_ms(left_measurements[idx])
            r_timing = self._store._extract_measurement_timing_ms(right_measurements[idx])
            if l_timing is not None and r_timing is not None:
                pair_deltas.append((idx, r_timing - l_timing))

        l_elec_all = all(
            self._store._extract_measurement_timing_ms(m) is not None and
            (m.get("analysis", {}).get("reference_path", {}) or {}).get("electrical_reference_used")
            for m in left_measurements if self._store._extract_measurement_timing_ms(m) is not None
        )
        r_elec_all = all(
            self._store._extract_measurement_timing_ms(m) is not None and
            (m.get("analysis", {}).get("reference_path", {}) or {}).get("electrical_reference_used")
            for m in right_measurements if self._store._extract_measurement_timing_ms(m) is not None
        )
        electrical_reference_used = l_elec_all and r_elec_all

        delta_cluster_limit = (
            LR_REPEAT_PAIRED_DELTA_CLUSTER_MS
            if electrical_reference_used
            else LR_REPEAT_ACOUSTIC_TIMING_CLUSTER_MS
        )
        min_cluster_size = LR_REPEAT_PAIRED_MIN_CLUSTER_SIZE

        accepted_pair_indices, delta_center, delta_spread = self._store._select_paired_delta_cluster(
            [d for _, d in pair_deltas],
            cluster_limit_ms=delta_cluster_limit,
            min_cluster_size=min_cluster_size,
        )

        if not accepted_pair_indices:
            # Fallback: accept all pairs, mark unstable
            accepted_pair_indices = [idx for idx, _ in pair_deltas]

        # Derive L and R timings from accepted pairs only
        if accepted_pair_indices is not None and len(accepted_pair_indices) > 0:
            l_accepted_timings = []
            r_accepted_timings = []
            for idx in accepted_pair_indices:
                lt = self._store._extract_measurement_timing_ms(left_measurements[idx])
                rt = self._store._extract_measurement_timing_ms(right_measurements[idx])
                if lt is not None:
                    l_accepted_timings.append(lt)
                if rt is not None:
                    r_accepted_timings.append(rt)
            l_final_ms = float(np.median(l_accepted_timings)) if l_accepted_timings else None
            r_final_ms = float(np.median(r_accepted_timings)) if r_accepted_timings else None
        else:
            l_final_ms = None
            r_final_ms = None

        l_summary = self._store._build_repeat_side_summary(
            left_measurements,
            accepted_pair_indices,
            base_name=base_name,
            channel="left",
            repeat_count=repeat_count,
            final_timing_ms=l_final_ms,
            electrical_reference_used=electrical_reference_used,
            paired_delta_center=delta_center,
            paired_delta_spread=delta_spread,
            paired_timing_stable=delta_center is not None,
            pair_count=len(pair_deltas),
        )
        r_summary = self._store._build_repeat_side_summary(
            right_measurements,
            accepted_pair_indices,
            base_name=base_name,
            channel="right",
            repeat_count=repeat_count,
            final_timing_ms=r_final_ms,
            electrical_reference_used=electrical_reference_used,
            paired_delta_center=delta_center,
            paired_delta_spread=delta_spread,
            paired_timing_stable=delta_center is not None,
            pair_count=len(pair_deltas),
        )
        return l_summary, r_summary

    def _build_pre_averaged_lr_summary(
        self,
        own_effective: dict[str, Any],
        other_effective: dict[str, Any],
        *,
        side: str,
        base_name: str,
        repeat_count: int,
        pre_avg_debug: dict[str, Any],
    ) -> dict[str, Any]:
        """Build one side of an L/R summary when ER pre-averaging was used."""
        analysis = own_effective.get("analysis") if isinstance(own_effective.get("analysis"), dict) else {}
        ref_path = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
        impulse = analysis.get("impulse_response") if isinstance(analysis.get("impulse_response"), dict) else {}
        other_analysis = other_effective.get("analysis") if isinstance(other_effective.get("analysis"), dict) else {}
        other_ref = other_analysis.get("reference_path") if isinstance(other_analysis.get("reference_path"), dict) else {}
        other_impulse = other_analysis.get("impulse_response") if isinstance(other_analysis.get("impulse_response"), dict) else {}

        own_timing = ref_path.get("acoustic_arrival_corrected_ms", impulse.get("arrival_ms"))
        other_timing = other_ref.get("acoustic_arrival_corrected_ms", other_impulse.get("arrival_ms"))
        try:
            own_timing = float(own_timing)
        except (TypeError, ValueError):
            own_timing = None
        try:
            other_timing = float(other_timing)
        except (TypeError, ValueError):
            other_timing = None
        delta = None
        if own_timing is not None and other_timing is not None:
            delta = round(other_timing - own_timing, 6)

        side_label = "L" if side == "left" else "R"
        summary_name = f"{str(base_name or 'L/R Repeat').strip()} · {side_label}"
        timestamp = datetime.now(timezone.utc).replace(microsecond=0)
        measurement_id = f"lr-repeat-paired-average-{side}-{timestamp.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"

        # Use the trace data from the first underlying capture, but with pre-averaged analysis
        trace_source = own_effective
        payload = deepcopy(trace_source)
        payload.update({
            "id": measurement_id,
            "name": summary_name,
            "created_at": timestamp.isoformat().replace("+00:00", "Z"),
            "channel": side,
            "measurement_kind": "lr-repeat-paired-average-summary",
            "notes": [
                MEASUREMENT_SCOPE_NOTE,
                f"Same-position L/R repeat with ER pre-averaging from {repeat_count} {side_label} sweep(s).",
                f"Electrical reference captures aligned and averaged before deconvolution.",
            ],
        })
        source_trace_average = trace_source.get("_pre_average_source_trace_average")
        source_review_average = trace_source.get("_pre_average_source_review_average")
        preavg_traces = deepcopy(trace_source.get("traces") or [])
        preavg_review_traces = deepcopy(trace_source.get("review_traces") or [])
        if isinstance(source_trace_average, dict):
            payload["traces"] = [deepcopy(source_trace_average)]
            if isinstance(source_review_average, dict):
                payload["review_traces"] = [deepcopy(source_review_average)]
            else:
                payload["review_traces"] = [deepcopy(source_trace_average)]
            for trace in preavg_traces:
                if isinstance(trace, dict):
                    debug_trace = deepcopy(trace)
                    debug_trace["role"] = "er-time-domain-preavg"
                    debug_trace["kind"] = "lr-repeat-er-time-domain-preavg"
                    debug_trace["label"] = f"{side_label} repeat · time-domain ER preavg"
                    payload.setdefault("review_traces", []).append(debug_trace)
            for trace in preavg_review_traces:
                if isinstance(trace, dict):
                    debug_trace = deepcopy(trace)
                    debug_trace["role"] = "er-time-domain-preavg-review"
                    debug_trace["kind"] = "lr-repeat-er-time-domain-preavg-review"
                    debug_trace["label"] = f"{side_label} repeat · raw time-domain ER preavg"
                    payload.setdefault("review_traces", []).append(debug_trace)
            payload.setdefault("notes", []).append(
                "Frequency response is stored as a magnitude-domain average of the individual repeat sweeps; ER pre-average is used for timing/L/R delta only."
            )

        own_dbg = pre_avg_debug.get(side, {})
        other_dbg = pre_avg_debug.get("right" if side == "left" else "left", {})
        own_shifts = own_dbg.get("alignment_shifts_samples", [0])
        sample_rate = int(analysis.get("sample_rate") or 0)
        sample_spread = max(own_shifts) - min(own_shifts) if len(own_shifts) > 1 else 0
        residual_shifts = own_dbg.get("residual_alignment_shifts_samples", [0])
        residual_spread = (
            max(residual_shifts) - min(residual_shifts)
            if len(residual_shifts) > 1 else 0
        )
        spread_limit = self._store._er_pre_average_sample_spread_limit(sample_rate)
        timing_stable = bool(own_dbg.get("pre_average_applied")) and residual_spread <= spread_limit

        if delta is not None:
            shifts_str = ", ".join(
                f"{s}s ({s / sample_rate * 1000:.2f}ms)" for s in own_shifts
            )
            payload["notes"].append(
                f"Paired delta: {delta:+.4f} ms "
                f"(ER pre-averaged, shifts: [{shifts_str}], "
                f"raw spread: {sample_spread}s/{sample_spread / sample_rate * 1000:.3f}ms, "
                f"residual spread: {residual_spread}s/{residual_spread / sample_rate * 1000:.3f}ms, "
                f"timing {'stable' if timing_stable else 'review'})"
            )
        else:
            payload["notes"].append("Paired delta: unavailable")

        analysis_out = deepcopy(analysis)
        analysis_out["method"] = "er-pre-averaged-lr-repeat"
        analysis_out["lr_repeat"] = {
            "repeat_count": repeat_count,
            "pre_averaged": True,
            "alignment_shifts_samples": own_shifts,
            "alignment_spread_samples": sample_spread,
            "residual_alignment_shifts_samples": residual_shifts,
            "residual_alignment_spread_samples": residual_spread,
            "alignment_spread_gate": own_dbg.get("alignment_spread_gate", "post-shift-residual"),
            "delta_ms": delta,
            "paired_timing_stable": timing_stable,
            "electrical_reference_used": True,
        }
        ref_path_out = deepcopy(ref_path)
        if own_timing is not None and timing_stable:
            arrival_samples = int(round(own_timing / 1000.0 * sample_rate)) if sample_rate > 0 else None
            ref_path_out.update({
                "timing_status": "lr-repeat",
                "timing_label": "L/R repeat timing (ER pre-averaged)",
                "stability": "stable",
                "acoustic_arrival_corrected_ms": own_timing,
                "acoustic_arrival_corrected_seconds": round(own_timing / 1000.0, 9),
                "acoustic_arrival_corrected_samples": arrival_samples,
            })
        analysis_out["reference_path"] = ref_path_out
        analysis_out["impulse_response"] = impulse
        payload["analysis"] = analysis_out
        return self._store._normalize_measurement(payload)

    def _build_repeat_side_summary(
        self,
        measurements: list[dict[str, Any]],
        accepted_pair_indices: list[int],
        *,
        base_name: str,
        channel: str,
        repeat_count: int,
        final_timing_ms: float | None,
        electrical_reference_used: bool,
        paired_delta_center: float | None,
        paired_delta_spread: float | None,
        paired_timing_stable: bool,
        pair_count: int,
    ) -> dict[str, Any]:
        """Build one side (L or R) of an L/R Repeat summary using pre-clustered pair indices."""
        if not measurements:
            raise ValueError("L/R repeat side summary needs at least one measurement")
        normalized = [self._store._normalize_measurement(item) for item in measurements]
        accepted_indices = accepted_pair_indices if accepted_pair_indices is not None else list(range(len(normalized)))
        accepted_measurements = [normalized[index] for index in accepted_indices]
        side_label = "L" if channel == "left" else "R"
        summary_name = f"{str(base_name or 'L/R Repeat').strip()} · {side_label}"
        timestamp = datetime.now(timezone.utc).replace(microsecond=0)
        measurement_id = f"lr-repeat-{channel}-{timestamp.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
        payload = deepcopy(normalized[0])
        payload.update({
            "id": measurement_id,
            "name": summary_name,
            "created_at": timestamp.isoformat().replace("+00:00", "Z"),
            "channel": channel,
            "measurement_kind": "lr-repeat-summary",
            "traces": [
                self._store._average_merge_traces(
                    [self._store._select_merge_trace(item, "traces", preferred_role="trusted") for item in accepted_measurements],
                    label=f"{summary_name} · trusted average",
                    kind="lr-repeat-sweep-response",
                    role="trusted",
                    color=TRACE_COLORS[0],
                )
            ],
            "notes": [
                MEASUREMENT_SCOPE_NOTE,
                f"Same-position L/R repeat summary from {len(normalized)} {side_label} sweep(s).",
                "Intermediate repeat sweeps were processed internally and were not saved as normal measurements.",
            ],
        })

        # Magnitude traces
        review_traces = [
            self._store._select_merge_trace(item, "review_traces", preferred_role="raw-review", required=False)
            for item in accepted_measurements
        ]
        if all(review_traces):
            payload["review_traces"] = [
                self._store._average_merge_traces(
                    review_traces,
                    label=f"{summary_name} · raw/full-band review average",
                    kind="lr-repeat-sweep-response-review",
                    role="raw-review",
                    color=TRACE_COLORS[1],
                )
            ]
        else:
            payload.pop("review_traces", None)

        analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
        analysis = deepcopy(analysis)
        analysis["method"] = "same-position-lr-repeat-paired-delta"
        reference_path = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
        reference_path = deepcopy(reference_path)
        reference_source = ""
        if electrical_reference_used:
            reference_channel = reference_path.get("electrical_reference_input_channel")
            reference_source = f"electrical-input-channel-{reference_channel}" if reference_channel else "electrical-input"
        elif reference_path.get("capture_mode"):
            reference_source = str(reference_path["capture_mode"])

        stable = paired_timing_stable and final_timing_ms is not None
        sample_rate = int(analysis.get("sample_rate") or 0)

        analysis["lr_repeat"] = {
            "repeat_count": int(repeat_count),
            "pair_count": pair_count,
            "accepted_runs": len(accepted_indices),
            "rejected_runs": len(normalized) - len(accepted_indices),
            "accepted_run_numbers": [index + 1 for index in accepted_indices],
            "rejected_run_numbers": [index + 1 for index in range(len(normalized)) if index not in accepted_indices],
            "delta_center_ms": paired_delta_center,
            "delta_spread_ms": paired_delta_spread,
            "timing_method": "paired-delta-cluster" if stable else "paired-delta-unstable",
            "electrical_reference_used": electrical_reference_used,
            "reference_source": reference_source,
            "timing_stable": stable,
        }

        impulse = analysis.get("impulse_response") if isinstance(analysis.get("impulse_response"), dict) else {}
        impulse = deepcopy(impulse)

        if stable:
            arrival_samples = int(round(final_timing_ms / 1000.0 * sample_rate)) if sample_rate > 0 else None
            reference_path.update({
                "timing_status": "lr-repeat",
                "timing_label": "L/R repeat timing (paired)",
                "stability": "stable",
                "acoustic_arrival_corrected_ms": final_timing_ms,
                "acoustic_arrival_corrected_seconds": round(final_timing_ms / 1000.0, 9),
                "acoustic_arrival_corrected_samples": arrival_samples,
            })
            impulse.update({
                "arrival_ms": final_timing_ms,
                "arrival_seconds": round(final_timing_ms / 1000.0, 9),
                "arrival_samples": arrival_samples,
            })
        else:
            reference_path.update({
                "timing_status": "lr-repeat-unstable",
                "timing_label": "L/R repeat timing unstable",
                "stability": "unstable",
            })
            analysis["direct_arrival_timing_available"] = False
            payload["notes"].append("No stable paired-delta cluster found; L/R alignment must not use this summary.")

        analysis["reference_path"] = reference_path
        analysis["impulse_response"] = impulse
        payload["analysis"] = analysis
        return self._store._normalize_measurement(payload)

    @staticmethod
    def _select_repeat_timing_cluster(
        timings: list[dict[str, Any]],
        *,
        repeat_count: int,
        cluster_limit_ms: float,
    ) -> tuple[list[int], float | None, float | None]:
        if not timings:
            return [], None, None
        minimum_cluster_size = 1 if repeat_count <= 1 else 2
        candidates = []
        for anchor in timings:
            cluster = [
                item for item in timings
                if abs(float(item["timing_ms"]) - float(anchor["timing_ms"])) <= cluster_limit_ms
            ]
            spread = max(item["timing_ms"] for item in cluster) - min(item["timing_ms"] for item in cluster)
            candidates.append((len(cluster), -spread, cluster))
        _size, _negative_spread, best = max(candidates, key=lambda item: (item[0], item[1]))
        if len(best) < minimum_cluster_size:
            return [], None, None
        values = sorted(float(item["timing_ms"]) for item in best)
        center = float(np.median(values))
        spread = max(values) - min(values)
        return sorted(int(item["index"]) for item in best), round(center, 6), round(spread, 6)
