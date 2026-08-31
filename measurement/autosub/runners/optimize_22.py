# SPDX-License-Identifier: AGPL-3.0-only

"""The 2.2 subwoofer AutoSub optimize runner."""

from __future__ import annotations

import asyncio
import json
import logging

from audio.samplerate import (
    OUTPUT_MODE_SUBWOOFER_22,
    get_audio_output_overview,
    set_audio_output_mode,
)
from dsp.runtime import BassManagementConfig
from typing import Any
from ..candidates import (
    _AUTO_SUB_MIN_POLARITY_ACCEPT_GAIN,
    _auto_sub_22_candidate_subwoofers,
    _auto_sub_22_global_config,
    _auto_sub_22_sub,
    _auto_sub_22_verify_alignment,
    _auto_sub_apply_candidate,
    _auto_sub_clamped_delay,
    _auto_sub_opposite_polarity,
    _auto_sub_polarity_decision,
    _restore_auto_sub_original_config,
    _auto_sub_snapshot_copy,
    _auto_sub_sweep_profile,
)
from ..deps import (
    _AUTO_SUB_JOBS,
    _auto_sub_cancel_requested,
    _auto_sub_lock,
    _dsp_runtime,
    _measurement_session,
)
from ..jobs import (
    _finish_auto_sub_worker,
    _log_auto_sub_timing_summary,
)
from ..measurement import (
    _AUTO_SUB_ALIGNMENT_CHANGE_TOLERANCE_MS,
    _AUTO_SUB_LOCAL_DIP_TOLERANCE_DB,
    _auto_sub_22_snapshot_with_gain,
    _auto_sub_balance_transfer_deltas,
    _auto_sub_gain_deltas,
    _auto_sub_gain_log_line,
    _auto_sub_gain_log_score,
    _auto_sub_gain_response_correction,
    _auto_sub_gain_verdict,
    _auto_sub_local_dip_db,
    _auto_sub_local_dip_gate_sides,
    _auto_sub_target_residual_raw_db,
    _calculate_auto_sub_gain,
    _capture_auto_sub_main_references,
    _measure_auto_sub_combined_candidate,
)
from ..scoring import (
    _auto_sub_anchor_shifted_points,
    _auto_sub_candidate_ledger,
    _auto_sub_display_anchor_reference_db,
    _auto_sub_gate_candidate_rows,
    _auto_sub_has_points,
    _auto_sub_measurement_from_sweep,
    _auto_sub_result_meta,
    _auto_sub_result_for_delay,
    _auto_sub_shared_bass_offset,
    _score_auto_sub_combined_candidates,
    _score_auto_sub_matrix_candidates,
)

logger = logging.getLogger(__name__)


async def _run_auto_sub_22_optimize(
    job_id: str,
    input_id: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    sub1_scan_delays: list[float],
    sub2_scan_delays: list[float],
    fc: int,
    original_config_snapshot: dict[str, Any],
    fine_step_ms: float,
    entry_epoch: int | None = None,
) -> None:
    measurement_sr_session = _measurement_session()
    from measurement.session import (
        MeasurementEntryInvalidated,
        _resolve_measurement_start_sample_rate,
    )
    global _auto_sub_lock
    from audio.samplerate import _load_audio_output_mode, set_audio_output_mode

    job = _AUTO_SUB_JOBS.get(job_id)
    if not job:
        _auto_sub_lock.release()
        return

    async def _restore_original_config() -> None:
        await _restore_auto_sub_original_config(original_config_snapshot)

    original_sub1 = _auto_sub_22_sub(original_config_snapshot, "sub1")
    original_sub2 = _auto_sub_22_sub(original_config_snapshot, "sub2")
    original_sub1_alignment = float(original_sub1.get("alignment_ms", 0.0) or 0.0)
    original_sub2_alignment = float(original_sub2.get("alignment_ms", 0.0) or 0.0)

    def _matrix_delays(center: float) -> list[float]:
        return [_auto_sub_clamped_delay(center + offset) for offset in (-fine_step_ms, 0.0, fine_step_ms)]

    def _valid_lr(result: dict[str, Any]) -> bool:
        return _auto_sub_has_points(result, "points_left") or _auto_sub_has_points(result, "points_right")

    def _same_pair(pair: tuple[float, float], sub1_alignment: float, sub2_alignment: float) -> bool:
        return abs(pair[0] - sub1_alignment) <= 0.05 and abs(pair[1] - sub2_alignment) <= 0.05

    try:
        if measurement_sr_session is not None:
            try:
                await measurement_sr_session.register_auto_sub(job_id, entry_epoch=entry_epoch)
            except MeasurementEntryInvalidated:
                logger.info(
                    "AUTOSUB job=%s entry invalidated by measurement window close",
                    job_id,
                )
                job["status"] = "cancelled"
                job["message"] = "Auto Sub Optimize cancelled because the measurement window was closed."
                return
        if _auto_sub_cancel_requested(job):
            logger.info("AUTOSUB job=%s cancel observed (before sweeps)", job_id)
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        auto_sub_sweep_profile = _auto_sub_sweep_profile(fc)
        auto_sub_rate = _resolve_measurement_start_sample_rate()
        await _capture_auto_sub_main_references(
            job=job, fc=fc, input_id=input_id,
            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes, auto_sub_rate=auto_sub_rate,
            output_mode=OUTPUT_MODE_SUBWOOFER_22,
            original_config_snapshot=original_config_snapshot,
        )
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        # Coarse level balance before the matrix optimization: measure the
        # incumbent pair (both subs, original alignments/levels) and apply
        # its common Target residual as a bounded trim, so the matrix is
        # scored under a realistic sub/main balance and the later Gain step
        # becomes a fine trim.
        job["stage"] = "balance_check"
        job["message"] = "Auto Sub Optimize: measuring incumbent balance"
        balance_sweep_total = 2
        balance_sweep = await _measure_auto_sub_combined_candidate(
            delay_ms=original_sub1_alignment, job=job, candidate_index=1, total=1,
            sweep_index_start=1, sweep_total=balance_sweep_total, stage="balance_check", fc=fc,
            input_id=input_id, mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
            calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
            auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
            original_level=0.0, original_polarity="normal", original_highpass=True,
            output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=original_config_snapshot,
            sub1_alignment_ms=original_sub1_alignment, sub2_alignment_ms=original_sub2_alignment,
            active_subs=("sub1", "sub2"),
        )
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return
        balance_diagnostics = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_22, target_curve=job.get("target_curve"),
            anchor=job.get("main_target_anchor"),
            winner_curves={
                "left": balance_sweep.get("calibrated_points_left") or [],
                "right": balance_sweep.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        balance_deltas = _auto_sub_gain_deltas(
            balance_diagnostics, OUTPUT_MODE_SUBWOOFER_22, max_abs_db=6.0,
        )
        balanced_snapshot = _auto_sub_22_snapshot_with_gain(
            original_config_snapshot,
            left_delta_db=balance_deltas.get("left", 0.0),
            right_delta_db=balance_deltas.get("right", 0.0),
        )
        job["balance_check"] = {
            "deltas_db": {side: round(value, 3) for side, value in balance_deltas.items()},
            "residuals_db": {
                side: ((balance_diagnostics.get("channels", {}).get(side) or {}).get("raw_recommendation_db"))
                for side in ("left", "right")
            },
            "confidence": balance_diagnostics.get("confidence"),
            "applied": bool(balance_deltas),
        }
        logger.info("AUTOSUB_BALANCE job=%s mode=2.2_mono %s", job_id, json.dumps(job["balance_check"], sort_keys=True))
        if _dsp_runtime() is not None:
            await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))

        coarse1_results: list[dict[str, Any]] = []
        coarse2_results: list[dict[str, Any]] = []
        matrix_results: list[dict[str, Any]] = []
        sub1_sweep_total = len(sub1_scan_delays) * 2
        sub2_sweep_total = len(sub2_scan_delays) * 2
        matrix_sweep_start = sub1_sweep_total + sub2_sweep_total
        matrix_sweep_total = matrix_sweep_start + 18

        job["stage"] = "sub1_coarse"
        for idx, delay_ms in enumerate(sub1_scan_delays):
            coarse1_results.append(await _measure_auto_sub_combined_candidate(
                delay_ms=delay_ms,
                job=job,
                candidate_index=idx + 1,
                total=len(sub1_scan_delays),
                sweep_index_start=(idx * 2) + 1,
                sweep_total=matrix_sweep_total,
                stage="sub1_coarse",
                fc=fc,
                input_id=input_id,
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_ref=calibration_ref,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile,
                auto_sub_rate=auto_sub_rate,
                original_level=0.0,
                original_polarity="normal",
                original_highpass=True,
                output_mode=OUTPUT_MODE_SUBWOOFER_22,
                original_config_snapshot=balanced_snapshot,
                sub1_alignment_ms=delay_ms,
                sub2_alignment_ms=original_sub2_alignment,
                active_subs=("sub1",),
            ))
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        coarse1_valid = [result for result in coarse1_results if _valid_lr(result)]
        if not coarse1_valid:
            job["status"] = "failed"
            job["message"] = "No valid Sub 1 coarse sweep results to score"
            job["error"] = {"detail": "Sub 1 coarse sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return
        gated_coarse1, _ = _auto_sub_gate_candidate_rows(coarse1_results, fc, context="sub1_coarse")
        sub1_scoring = _score_auto_sub_combined_candidates(
            gated_coarse1,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_sub1_alignment,
        )
        sub1_winner = sub1_scoring["winner"]
        sub1_winner_delay = _auto_sub_clamped_delay(float(sub1_winner.get("delay_ms", original_sub1_alignment) or original_sub1_alignment))

        job["stage"] = "sub2_coarse"
        for idx, delay_ms in enumerate(sub2_scan_delays):
            coarse2_results.append(await _measure_auto_sub_combined_candidate(
                delay_ms=delay_ms,
                job=job,
                candidate_index=idx + 1,
                total=len(sub2_scan_delays),
                sweep_index_start=sub1_sweep_total + (idx * 2) + 1,
                sweep_total=matrix_sweep_total,
                stage="sub2_coarse",
                fc=fc,
                input_id=input_id,
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_ref=calibration_ref,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile,
                auto_sub_rate=auto_sub_rate,
                original_level=0.0,
                original_polarity="normal",
                original_highpass=True,
                output_mode=OUTPUT_MODE_SUBWOOFER_22,
                original_config_snapshot=balanced_snapshot,
                sub1_alignment_ms=original_sub1_alignment,
                sub2_alignment_ms=delay_ms,
                active_subs=("sub2",),
            ))
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        coarse2_valid = [result for result in coarse2_results if _valid_lr(result)]
        if not coarse2_valid:
            job["status"] = "failed"
            job["message"] = "No valid Sub 2 coarse sweep results to score"
            job["error"] = {"detail": "Sub 2 coarse sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return
        gated_coarse2, _ = _auto_sub_gate_candidate_rows(coarse2_results, fc, context="sub2_coarse")
        sub2_scoring = _score_auto_sub_combined_candidates(
            gated_coarse2,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_sub2_alignment,
        )
        sub2_winner = sub2_scoring["winner"]
        sub2_winner_delay = _auto_sub_clamped_delay(float(sub2_winner.get("delay_ms", original_sub2_alignment) or original_sub2_alignment))

        sub1_matrix = _matrix_delays(sub1_winner_delay)
        sub2_matrix = _matrix_delays(sub2_winner_delay)
        matrix_pairs = [(sub1_delay, sub2_delay) for sub1_delay in sub1_matrix for sub2_delay in sub2_matrix]
        incumbent_pair = (
            _auto_sub_clamped_delay(original_sub1_alignment),
            _auto_sub_clamped_delay(original_sub2_alignment),
        )
        incumbent_in_matrix = any(_same_pair(pair, incumbent_pair[0], incumbent_pair[1]) for pair in matrix_pairs)
        if not incumbent_in_matrix:
            matrix_pairs.append(incumbent_pair)
        job["combined_matrix"] = {
            "status": "running",
            "fine_step_ms": fine_step_ms,
            "sub1_candidates": sub1_matrix,
            "sub2_candidates": sub2_matrix,
            "incumbent_pair": {"sub1_alignment_ms": incumbent_pair[0], "sub2_alignment_ms": incumbent_pair[1]},
            "incumbent_in_matrix": incumbent_in_matrix,
            "candidates": [
                {
                    "sub1_alignment_ms": a,
                    "sub2_alignment_ms": b,
                    "incumbent_pair": _same_pair((a, b), incumbent_pair[0], incumbent_pair[1]),
                }
                for a, b in matrix_pairs
            ],
        }
        matrix_sweep_total = matrix_sweep_start + (len(matrix_pairs) * 2)

        job["stage"] = "combined_matrix"
        for idx, (sub1_delay, sub2_delay) in enumerate(matrix_pairs):
            matrix_results.append(await _measure_auto_sub_combined_candidate(
                delay_ms=sub1_delay,
                job=job,
                candidate_index=idx + 1,
                total=len(matrix_pairs),
                sweep_index_start=matrix_sweep_start + (idx * 2) + 1,
                sweep_total=matrix_sweep_total,
                stage="combined_matrix",
                fc=fc,
                input_id=input_id,
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_ref=calibration_ref,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile,
                auto_sub_rate=auto_sub_rate,
                original_level=0.0,
                original_polarity="normal",
                original_highpass=True,
                output_mode=OUTPUT_MODE_SUBWOOFER_22,
                original_config_snapshot=balanced_snapshot,
                sub1_alignment_ms=sub1_delay,
                sub2_alignment_ms=sub2_delay,
                active_subs=("sub1", "sub2"),
            ))
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        matrix_valid = [result for result in matrix_results if _valid_lr(result)]
        if not matrix_valid:
            job["status"] = "failed"
            job["message"] = "No valid Combined Matrix sweep results to score"
            job["error"] = {"detail": "Combined Matrix sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return

        gated_matrix, _ = _auto_sub_gate_candidate_rows(matrix_results, fc, context="combined_matrix")
        matrix_scoring = _score_auto_sub_matrix_candidates(
            gated_matrix,
            crossover_hz=fc,
            original_sub1_alignment_ms=original_sub1_alignment,
            original_sub2_alignment_ms=original_sub2_alignment,
        )
        winner = matrix_scoring["accepted_winner"]
        gain_winner = next(
            (candidate for candidate in matrix_results
             if round(float(candidate.get("sub1_alignment_ms", 0.0)), 2) == round(float(winner.get("sub1_alignment_ms", 0.0)), 2)
             and round(float(candidate.get("sub2_alignment_ms", 0.0)), 2) == round(float(winner.get("sub2_alignment_ms", 0.0)), 2)),
            {},
        )
        best_sub1 = _auto_sub_clamped_delay(float(winner.get("sub1_alignment_ms", sub1_winner_delay) or sub1_winner_delay))
        best_sub2 = _auto_sub_clamped_delay(float(winner.get("sub2_alignment_ms", sub2_winner_delay) or sub2_winner_delay))
        incumbent_polarities = (str(original_sub1.get("polarity", "normal")), str(original_sub2.get("polarity", "normal")))
        selected_polarities = incumbent_polarities
        polarity_candidates: list[dict[str, Any]] = [dict(gain_winner, delay_ms=0.0)]
        alternative_polarities = [
            (_auto_sub_opposite_polarity(incumbent_polarities[0]), incumbent_polarities[1]),
            (incumbent_polarities[0], _auto_sub_opposite_polarity(incumbent_polarities[1])),
            (_auto_sub_opposite_polarity(incumbent_polarities[0]), _auto_sub_opposite_polarity(incumbent_polarities[1])),
        ]
        for idx, polarities in enumerate(alternative_polarities, 1):
            measured = await _measure_auto_sub_combined_candidate(
                delay_ms=best_sub1, job=job, candidate_index=idx, total=3,
                sweep_index_start=matrix_sweep_total + (idx - 1) * 2 + 1,
                sweep_total=matrix_sweep_total + 6, stage="polarity_check", fc=fc,
                input_id=input_id, mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                original_level=0.0, original_polarity="normal", original_highpass=True,
                output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=balanced_snapshot,
                sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                active_subs=("sub1", "sub2"), sub1_polarity=polarities[0], sub2_polarity=polarities[1],
            )
            polarity_candidates.append(dict(measured, delay_ms=float(idx), tested_polarities=polarities))
        polarity_scoring = _score_auto_sub_combined_candidates(polarity_candidates, crossover_hz=fc, low_guard_reference_delay_ms=0.0)
        polarity_winner = polarity_scoring["winner"]
        incumbent_scored = _auto_sub_result_for_delay(polarity_scoring["results"], 0.0) or {}
        alternative_scored = polarity_winner if float(polarity_winner.get("delay_ms", 0.0)) != 0.0 else {}
        # The candidate set is already scored in one shared normalization
        # pass; the flip still requires the raised acceptance margin.
        polarity_decision = _auto_sub_polarity_decision(
            incumbent_scored, alternative_scored, min_score_gain=_AUTO_SUB_MIN_POLARITY_ACCEPT_GAIN,
        ) if alternative_scored else {
            "accepted": False, "reason": "incumbent_best", "score_gain": 0.0,
            "min_score_gain": _AUTO_SUB_MIN_POLARITY_ACCEPT_GAIN,
        }
        if polarity_decision["accepted"]:
            selected_idx = int(round(float(alternative_scored["delay_ms"])))
            selected_polarities = alternative_polarities[selected_idx - 1]
            selected_measurement = polarity_candidates[selected_idx]
            refinement: list[dict[str, Any]] = []
            refinements = [(a, b) for a in _matrix_delays(best_sub1) for b in _matrix_delays(best_sub2)]
            for idx, (delay1, delay2) in enumerate(refinements):
                refinement.append(await _measure_auto_sub_combined_candidate(
                    delay_ms=delay1, job=job, candidate_index=idx + 1, total=9,
                    sweep_index_start=matrix_sweep_total + 7 + idx * 2,
                    sweep_total=matrix_sweep_total + 24, stage="polarity_refine", fc=fc,
                    input_id=input_id, mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                    original_level=0.0, original_polarity="normal", original_highpass=True,
                    output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=balanced_snapshot,
                    sub1_alignment_ms=delay1, sub2_alignment_ms=delay2, active_subs=("sub1", "sub2"),
                    sub1_polarity=selected_polarities[0], sub2_polarity=selected_polarities[1],
                ))
            refined_scoring = _score_auto_sub_matrix_candidates(refinement, crossover_hz=fc)
            refined_winner = refined_scoring["winner"]
            best_sub1 = float(refined_winner["sub1_alignment_ms"])
            best_sub2 = float(refined_winner["sub2_alignment_ms"])
            gain_winner = next((row for row in refinement if abs(float(row.get("sub1_alignment_ms", 0))-best_sub1)<0.01 and abs(float(row.get("sub2_alignment_ms", 0))-best_sub2)<0.01), selected_measurement)
            polarity_decision["refinement"] = {"winner": refined_winner, "candidate_count": 9}
        polarity_snapshot = _auto_sub_snapshot_copy(balanced_snapshot)
        polarity_snapshot.setdefault("subwoofers", {}).setdefault("sub1", {})["polarity"] = selected_polarities[0]
        polarity_snapshot.setdefault("subwoofers", {}).setdefault("sub2", {})["polarity"] = selected_polarities[1]
        job["polarity_check"] = {**polarity_decision, "incumbent": incumbent_polarities, "selected": selected_polarities, "alternatives_tested": alternative_polarities}
        job["auto_gain"] = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_22,
            target_curve=job.get("target_curve"), anchor=job.get("main_target_anchor"),
            winner_curves={
                "left": gain_winner.get("calibrated_points_left") or [],
                "right": gain_winner.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        logger.info("AUTOSUB_GAIN mode=2.2_mono diagnostics=%s", json.dumps(job["auto_gain"], sort_keys=True))
        gain_deltas = _auto_sub_gain_deltas(job["auto_gain"], OUTPUT_MODE_SUBWOOFER_22, max_abs_db=6.0)
        # The balance trim was measured at the original sub pair. When the
        # accepted matrix pair differs, transfer the trim to the accepted
        # configuration (plus its own measured level delta) instead of
        # re-closing the old configuration's residual on top of the old trim.
        alignment_changed = (
            abs(best_sub1 - original_sub1_alignment) > _AUTO_SUB_ALIGNMENT_CHANGE_TOLERANCE_MS
            or abs(best_sub2 - original_sub2_alignment) > _AUTO_SUB_ALIGNMENT_CHANGE_TOLERANCE_MS
        )
        incumbent_residual_common: float | None = None
        if alignment_changed:
            incumbent_candidate = _auto_sub_result_for_delay(list(coarse1_results), original_sub1_alignment) or {}
            try:
                residual_left, _mad_l, _u_l = _auto_sub_target_residual_raw_db(
                    incumbent_candidate.get("calibrated_points_left") or [], job.get("target_curve"),
                    job.get("main_target_anchor"), fc,
                )
                residual_right, _mad_r, _u_r = _auto_sub_target_residual_raw_db(
                    incumbent_candidate.get("calibrated_points_right") or [], job.get("target_curve"),
                    job.get("main_target_anchor"), fc,
                )
                incumbent_residual_common = (residual_left + residual_right) / 2.0
            except (ValueError, TypeError, KeyError, IndexError):
                incumbent_residual_common = None
        winner_residual_common = (job["auto_gain"].get("recommendation") or {}).get("raw_delta_db")
        balance_transfer = _auto_sub_balance_transfer_deltas(
            balance_deltas_db={"left": balance_deltas.get("left", 0.0), "right": balance_deltas.get("left", 0.0)},
            winner_residuals_db={"left": winner_residual_common, "right": winner_residual_common},
            incumbent_residuals_db={"left": incumbent_residual_common, "right": incumbent_residual_common},
            alignment_changed={"left": alignment_changed, "right": alignment_changed},
        )
        if balance_transfer.get("available"):
            gain_deltas = dict(balance_transfer["deltas_db"])
            job["auto_gain"]["configuration_transfer"] = json.loads(json.dumps(balance_transfer))
            logger.info(
                "AUTOSUB_TRANSFER job=%s mode=2.2_mono %s", job_id,
                json.dumps(job["auto_gain"]["configuration_transfer"], sort_keys=True),
            )
        else:
            job["auto_gain"]["configuration_transfer"] = {
                "available": False, "reason": balance_transfer.get("reason"),
                "alignment_changed": balance_transfer.get("alignment_changed"),
            }
        _auto_sub_gain_log_line("AUTOGAIN_INIT", {
            "mode": OUTPUT_MODE_SUBWOOFER_22, "xo_hz": fc,
            "target": (job.get("target_curve") or {}).get("label"),
            "anchor_hz": (job.get("main_target_anchor") or {}).get("usable_band_hz"),
            "target_offset_db": (job.get("main_target_anchor") or {}).get("target_vertical_offset_db"),
            "gain_before": {"sub1": float(original_sub1.get("level_db", 0.0)), "sub2": float(original_sub2.get("level_db", 0.0))},
            "winner_delta_left": (job["auto_gain"].get("channels", {}).get("left") or {}).get("target_delta_db"),
            "winner_delta_right": (job["auto_gain"].get("channels", {}).get("right") or {}).get("target_delta_db"),
            "combined_delta_db": (job["auto_gain"].get("recommendation") or {}).get("raw_delta_db"),
            "first_step_db": gain_deltas.get("left"),
        })
        gain_snapshot = _auto_sub_22_snapshot_with_gain(
            polarity_snapshot,
            left_delta_db=gain_deltas.get("left", 0.0), right_delta_db=gain_deltas.get("right", 0.0),
        )
        candidate_ledger = (
            _auto_sub_candidate_ledger(
                coarse1_results, sub1_scoring, mode="2.2_mono", phase="sub1_coarse",
                roles={"coarse_winner": sub1_winner},
            )
            + _auto_sub_candidate_ledger(
                coarse2_results, sub2_scoring, mode="2.2_mono", phase="sub2_coarse",
                roles={"coarse_winner": sub2_winner},
            )
            + _auto_sub_candidate_ledger(
                matrix_results, matrix_scoring, mode="2.2_mono", phase="matrix",
                roles={
                    "matrix_winner": matrix_scoring.get("matrix_winner"),
                    "final_accepted_winner": winner,
                },
                requested_incumbent={
                    "sub1_alignment_ms": original_sub1_alignment,
                    "sub2_alignment_ms": original_sub2_alignment,
                },
            )
        )

        sub_config = _auto_sub_22_global_config(gain_snapshot)
        subwoofers_config = _auto_sub_22_candidate_subwoofers(
            gain_snapshot,
            sub1_alignment_ms=best_sub1,
            sub2_alignment_ms=best_sub2,
            active_subs=("sub1", "sub2"),
        )
        apply_ok = await _auto_sub_apply_candidate(
            output_mode=OUTPUT_MODE_SUBWOOFER_22,
            global_config=sub_config,
            subwoofers_config=subwoofers_config,
            verify=lambda overview: _auto_sub_22_verify_alignment(overview, best_sub1, best_sub2),
            load_overview=_load_audio_output_mode,
        )

        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        if not apply_ok:
            job["status"] = "failed"
            job["message"] = f"Scoring succeeded but failed to apply winner pair {best_sub1:.2f} / {best_sub2:.2f} ms"
            job["error"] = {"detail": "Winner apply failed - original config restored"}
            await _restore_original_config()
            return

        gain_after_sweep = await _measure_auto_sub_combined_candidate(
            delay_ms=best_sub1, job=job, candidate_index=1, total=1,
            sweep_index_start=matrix_sweep_total + 1, sweep_total=matrix_sweep_total + 2,
            stage="gain_after", fc=fc, input_id=input_id,
            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
            auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
            original_highpass=bool(_auto_sub_22_global_config(gain_snapshot).get("main_highpass_enabled", True)),
            output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=gain_snapshot,
            sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2, active_subs=("sub1", "sub2"),
        )
        gain_after = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_22, target_curve=job.get("target_curve"),
            anchor=job.get("main_target_anchor"), winner_curves={
                "left": gain_after_sweep.get("calibrated_points_left") or [],
                "right": gain_after_sweep.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        gain_verdict = _auto_sub_gain_verdict(job["auto_gain"], gain_after, OUTPUT_MODE_SUBWOOFER_22)
        final_gain_deltas = gain_deltas if gain_verdict["accepted"] else {"left": 0.0, "right": 0.0}
        final_gain_snapshot = gain_snapshot if gain_verdict["accepted"] else polarity_snapshot
        final_gain_sweep = gain_after_sweep if gain_verdict["accepted"] else gain_winner
        correction_deltas: dict[str, float] = {}
        correction_plan = None
        correction_after = None
        correction_verdict = None
        if not gain_verdict["accepted"]:
            rollback_subs = _auto_sub_22_candidate_subwoofers(
                polarity_snapshot, sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                active_subs=("sub1", "sub2"),
            )
            await asyncio.to_thread(
                set_audio_output_mode, OUTPUT_MODE_SUBWOOFER_22, _auto_sub_22_global_config(polarity_snapshot), rollback_subs,
            )
            if _dsp_runtime() is not None:
                await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))
        elif (job["auto_gain"].get("configuration_transfer") or {}).get("available"):
            # The transferred trim is the final single-stage trim for the
            # accepted configuration; the response-correction step must not
            # re-close the balance stage's unrealized residual on top of it.
            correction_verdict = {
                "accepted": False,
                "reason": "Balance trim transferred to the accepted alignment; residual re-closure skipped",
                "channels": {},
                "step1_retained": True,
            }
        else:
            correction_plan = _auto_sub_gain_response_correction(
                job["auto_gain"], gain_after, gain_deltas, OUTPUT_MODE_SUBWOOFER_22,
            )
            correction_deltas = correction_plan.get("deltas_db") or {}
            correction_delta = correction_deltas.get("left", 0.0)
            if not correction_plan.get("available"):
                correction_verdict = {
                    "accepted": False,
                    "reason": correction_plan.get("reason"),
                    "channels": {},
                    "step1_retained": True,
                }
            elif abs(correction_delta) > 0.0005:
                correction_snapshot = _auto_sub_22_snapshot_with_gain(
                    gain_snapshot, left_delta_db=correction_delta, right_delta_db=correction_delta,
                )
                await asyncio.to_thread(
                    set_audio_output_mode, OUTPUT_MODE_SUBWOOFER_22, _auto_sub_22_global_config(correction_snapshot),
                    _auto_sub_22_candidate_subwoofers(
                        correction_snapshot, sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                        active_subs=("sub1", "sub2"),
                    ),
                )
                if _dsp_runtime() is not None:
                    await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))
                correction_sweep = await _measure_auto_sub_combined_candidate(
                    delay_ms=best_sub1, job=job, candidate_index=1, total=1,
                    sweep_index_start=matrix_sweep_total + 3, sweep_total=matrix_sweep_total + 4,
                    stage="gain_correction_after", fc=fc, input_id=input_id,
                    mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
                    original_highpass=bool(_auto_sub_22_global_config(correction_snapshot).get("main_highpass_enabled", True)),
                    output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=correction_snapshot,
                    sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2, active_subs=("sub1", "sub2"),
                )
                correction_after = _calculate_auto_sub_gain(
                    mode=OUTPUT_MODE_SUBWOOFER_22, target_curve=job.get("target_curve"),
                    anchor=job.get("main_target_anchor"), winner_curves={
                        "left": correction_sweep.get("calibrated_points_left") or [],
                        "right": correction_sweep.get("calibrated_points_right") or [],
                    }, crossover_hz=fc,
                )
                correction_verdict = _auto_sub_gain_verdict(gain_after, correction_after, OUTPUT_MODE_SUBWOOFER_22)
                if correction_verdict["accepted"]:
                    final_gain_deltas = {
                        "left": gain_deltas.get("left", 0.0) + correction_delta,
                        "right": gain_deltas.get("right", 0.0) + correction_delta,
                    }
                    final_gain_snapshot = correction_snapshot
                    final_gain_sweep = correction_sweep
                else:
                    await asyncio.to_thread(
                        set_audio_output_mode, OUTPUT_MODE_SUBWOOFER_22, _auto_sub_22_global_config(gain_snapshot),
                        _auto_sub_22_candidate_subwoofers(
                            gain_snapshot, sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                            active_subs=("sub1", "sub2"),
                        ),
                    )
                    if _dsp_runtime() is not None:
                        await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))
        _auto_sub_gain_log_line("AUTOGAIN_FEEDBACK", {
            "gain_after_step1": {
                "sub1": float(_auto_sub_22_sub(gain_snapshot, "sub1").get("level_db", 0.0)),
                "sub2": float(_auto_sub_22_sub(gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "score_before": _auto_sub_gain_log_score(job["auto_gain"]),
            "score_after_step1": _auto_sub_gain_log_score(gain_after),
            "response_per_db_left": (((correction_plan or {}).get("channels") or {}).get("left") or {}).get("response_change_per_db"),
            "response_per_db_right": (((correction_plan or {}).get("channels") or {}).get("right") or {}).get("response_change_per_db"),
            "remaining_error_left": (gain_after.get("channels", {}).get("left") or {}).get("target_delta_db"),
            "remaining_error_right": (gain_after.get("channels", {}).get("right") or {}).get("target_delta_db"),
            "raw_correction_db": ((correction_plan or {}).get("raw_deltas_db") or {}).get("left"),
            "applied_correction_db": ((correction_plan or {}).get("applied_deltas_db") or {}).get("left"),
            "correction_step_db": correction_deltas.get("left") if correction_deltas else None,
        })
        decision = "accepted_step2" if correction_verdict and correction_verdict.get("accepted") else (
            "accepted_step1" if gain_verdict.get("accepted") else "restored"
        )
        score_final_source = correction_after if decision == "accepted_step2" else (gain_after if decision == "accepted_step1" else job["auto_gain"])
        result_reason = ((correction_verdict or gain_verdict) or {}).get("reason")
        if (
            decision == "accepted_step1" and correction_verdict
            and not correction_verdict.get("accepted")
            and "step1_retained" not in correction_verdict
        ):
            # Make explicit which step the rejection reason belongs to.
            result_reason = f"Step-1 retained; step-2 correction rejected ({correction_verdict.get('reason')})"
        _auto_sub_gain_log_line("AUTOGAIN_RESULT", {
            "gain_final": {
                "sub1": float(_auto_sub_22_sub(final_gain_snapshot, "sub1").get("level_db", 0.0)),
                "sub2": float(_auto_sub_22_sub(final_gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "score_final": _auto_sub_gain_log_score(score_final_source), "decision": decision,
            "reason": result_reason,
            "delay_final": {"sub1_ms": best_sub1, "sub2_ms": best_sub2},
        })
        job["auto_gain"].update({
            "applied": bool(gain_verdict["accepted"] and gain_deltas),
            "reverted": bool(gain_deltas and not gain_verdict["accepted"]),
            "verification": gain_after, "verification_verdict": gain_verdict,
            "response_correction": correction_plan,
            "correction_deltas_db": correction_deltas,
            "correction_verification": correction_after,
            "correction_verdict": correction_verdict,
            "final_deltas_db": final_gain_deltas,
            "original_levels_db": {
                "sub1": float(original_sub1.get("level_db", 0.0)), "sub2": float(original_sub2.get("level_db", 0.0)),
            },
            "final_levels_db": {
                "sub1": float(_auto_sub_22_sub(final_gain_snapshot, "sub1").get("level_db", 0.0)),
                "sub2": float(_auto_sub_22_sub(final_gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "stage_output_peaks": (final_gain_sweep or {}).get("stage_output_peaks"),
        })

        # Final Before/After confirmation gate (see the 2.1/2.2-stereo
        # runners): the adopted matrix winner must not introduce a clearly
        # deeper local dip than the measured Before state. On failure the
        # incumbent pair under the balanced levels is measured and adopted
        # when it passes; otherwise the original state is restored.
        gate_band_low, gate_band_high = fc * 0.5, fc * 2.0
        gate_before_dips = {
            "left": _auto_sub_local_dip_db(balance_sweep.get("points_left") or [], gate_band_low, gate_band_high),
            "right": _auto_sub_local_dip_db(balance_sweep.get("points_right") or [], gate_band_low, gate_band_high),
        }
        gate_final_dips = {
            "left": _auto_sub_local_dip_db((final_gain_sweep or {}).get("points_left") or [], gate_band_low, gate_band_high),
            "right": _auto_sub_local_dip_db((final_gain_sweep or {}).get("points_right") or [], gate_band_low, gate_band_high),
        }
        gate_failed_sides = _auto_sub_local_dip_gate_sides(
            gate_before_dips, gate_final_dips, _AUTO_SUB_LOCAL_DIP_TOLERANCE_DB,
        )
        confirmation_gate = {
            "band_hz": [gate_band_low, gate_band_high],
            "tolerance_db": _AUTO_SUB_LOCAL_DIP_TOLERANCE_DB,
            "before_local_dip_db": gate_before_dips,
            "final_local_dip_db": gate_final_dips,
            "failed_sides": gate_failed_sides,
            "action": "final_kept",
        }
        if gate_failed_sides:
            job["stage"] = "confirmation_recheck"
            job["message"] = "Auto Sub Optimize: final state regressed locally; measuring incumbent pair at balanced levels"
            recheck_sweep = await _measure_auto_sub_combined_candidate(
                delay_ms=original_sub1_alignment, job=job, candidate_index=1, total=1,
                sweep_index_start=matrix_sweep_total + 5, sweep_total=matrix_sweep_total + 7,
                stage="confirmation_recheck", fc=fc, input_id=input_id,
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
                original_highpass=bool(_auto_sub_22_global_config(balanced_snapshot).get("main_highpass_enabled", True)),
                output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=balanced_snapshot,
                sub1_alignment_ms=original_sub1_alignment, sub2_alignment_ms=original_sub2_alignment,
                active_subs=("sub1", "sub2"),
            )
            recheck_dips = {
                "left": _auto_sub_local_dip_db(recheck_sweep.get("points_left") or [], gate_band_low, gate_band_high),
                "right": _auto_sub_local_dip_db(recheck_sweep.get("points_right") or [], gate_band_low, gate_band_high),
            }
            recheck_passed = all(
                recheck_dips[side] is None or gate_before_dips[side] is None
                or recheck_dips[side] <= gate_before_dips[side] + _AUTO_SUB_LOCAL_DIP_TOLERANCE_DB
                for side in ("left", "right")
            ) and (
                _auto_sub_has_points(recheck_sweep, "points_left") or _auto_sub_has_points(recheck_sweep, "points_right")
            )
            confirmation_gate.update({"recheck_local_dip_db": recheck_dips, "recheck_passed": recheck_passed})
            if recheck_passed:
                # Keep the balance fix, revert the pair (and any polarity
                # change) to the incumbent state the balance was computed for.
                final_gain_snapshot = balanced_snapshot
                final_gain_sweep = recheck_sweep
                best_sub1 = original_sub1_alignment
                best_sub2 = original_sub2_alignment
                selected_polarities = list(incumbent_polarities)
                await asyncio.to_thread(
                    set_audio_output_mode, OUTPUT_MODE_SUBWOOFER_22,
                    _auto_sub_22_global_config(final_gain_snapshot),
                    _auto_sub_22_candidate_subwoofers(
                        final_gain_snapshot, sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                        active_subs=("sub1", "sub2"),
                    ),
                )
                if _dsp_runtime() is not None:
                    await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))
                confirmation_gate["action"] = "alignment_reverted_balance_kept"
            else:
                await _restore_original_config()
                final_gain_snapshot = original_config_snapshot
                final_gain_sweep = balance_sweep
                best_sub1 = original_sub1_alignment
                best_sub2 = original_sub2_alignment
                confirmation_gate["action"] = "reverted_to_original"
        job["confirmation_gate"] = confirmation_gate
        logger.info("AUTOSUB_CONF_GATE job=%s %s", job_id, json.dumps(confirmation_gate, sort_keys=True))

        derived_delays: dict[str, Any] = {}
        try:
            config = BassManagementConfig.from_overview(await asyncio.to_thread(get_audio_output_overview))
            derived_delays = {
                "derived_main_delay_ms": round(config.derived_main_delay_ms, 2),
                "derived_sub1_delay_ms": round(config.derived_sub1_delay_ms, 2),
                "derived_sub2_delay_ms": round(config.derived_sub2_delay_ms, 2),
            }
        except Exception:
            derived_delays = {}

        job["combined_matrix"].update({
            "status": "completed",
            "winner": winner,
            "matrix_winner": matrix_scoring.get("matrix_winner"),
            "incumbent_winner": matrix_scoring.get("incumbent_winner"),
            "incumbent_score": matrix_scoring.get("incumbent_score"),
            "accepted_winner": matrix_scoring.get("accepted_winner"),
            "incumbent_accepted": matrix_scoring.get("incumbent_accepted"),
            "reject_reason": matrix_scoring.get("reject_reason"),
            "runner_up": matrix_scoring.get("runner_up"),
            "results": matrix_scoring["results"],
            "valid_count": len(matrix_valid),
        })
        _log_auto_sub_timing_summary(job)

        # Build baseline and confirmation measurements for before/after graph display
        all_22_sweeps = list(coarse1_results) + list(coarse2_results) + list(matrix_results)
        # The balance-stage sweep is the true Before state (both subs, original
        # alignments/levels); matrix candidates were measured balanced.
        baseline_22_sweep = balance_sweep
        # Prefer the final measured sweep (gain verification or polarity-refined
        # winner) so the confirmation always reflects the applied pair; fall
        # back to the scan sweep at the accepted winner pair.
        confirm_22_sweep = None
        if final_gain_sweep and (
            _auto_sub_has_points(final_gain_sweep, "points_left") or _auto_sub_has_points(final_gain_sweep, "points_right")
        ):
            confirm_22_sweep = final_gain_sweep
        else:
            confirm_22_sweep = next(
                (r for r in all_22_sweeps
                 if round(float(r.get("sub1_alignment_ms", r.get("delay_ms", 0.0))), 2) == round(float(best_sub1), 2)
                 and round(float(r.get("sub2_alignment_ms", 0.0)), 2) == round(float(best_sub2), 2)
                 and (_auto_sub_has_points(r, "points_left") or _auto_sub_has_points(r, "points_right"))),
                None,
            )
        baseline_measurement = None
        confirmation_measurement = None
        # Chain-anchor display correction: pull each trace back to the run's
        # median 200-600 Hz main-only level so an occasional chain gain
        # excursion no longer fakes a Before/After level change.
        _display_anchor_reference_db = _auto_sub_display_anchor_reference_db([
            points for sweep in list(all_22_sweeps)
            for points in (sweep.get("points_left") or [], sweep.get("points_right") or [])
        ])

        def _anchor_adjusted_combined_sweep(sweep: dict[str, Any] | None) -> dict[str, Any] | None:
            if not sweep:
                return sweep
            adjusted = dict(sweep)
            adjusted["points_left"] = _auto_sub_anchor_shifted_points(
                sweep.get("points_left") or [], _display_anchor_reference_db,
            )
            adjusted["points_right"] = _auto_sub_anchor_shifted_points(
                sweep.get("points_right") or [], _display_anchor_reference_db,
            )
            # Record applied per-side shifts so the frontend can place the
            # scored target in the exact display coordinate of each trace.
            adjusted["display_anchor_shift_db_left"] = _auto_sub_applied_anchor_shift(
                sweep.get("points_left") or [], _display_anchor_reference_db,
            )
            adjusted["display_anchor_shift_db_right"] = _auto_sub_applied_anchor_shift(
                sweep.get("points_right") or [], _display_anchor_reference_db,
            )
            return adjusted

        baseline_22_sweep = _anchor_adjusted_combined_sweep(baseline_22_sweep)
        confirm_22_sweep = _anchor_adjusted_combined_sweep(confirm_22_sweep)
        _offset_db = _auto_sub_shared_bass_offset(
            baseline_22_sweep.get("points_left") if baseline_22_sweep else [],
            baseline_22_sweep.get("points_right") if baseline_22_sweep else [],
        )
        if baseline_22_sweep:
            baseline_measurement = _auto_sub_measurement_from_sweep(
                baseline_22_sweep, "Before", f"AutoSub 2.2 Baseline (S1 {original_sub1_alignment:.1f} / S2 {original_sub2_alignment:.1f} ms)",
                offset_db=_offset_db,
            )
        if confirm_22_sweep:
            confirm_sub1 = float(confirm_22_sweep.get("sub1_alignment_ms", best_sub1))
            confirm_sub2 = float(confirm_22_sweep.get("sub2_alignment_ms", best_sub2))
            confirmation_measurement = _auto_sub_measurement_from_sweep(
                confirm_22_sweep, "After", f"AutoSub 2.2 Optimized (S1 {confirm_sub1:.1f} / S2 {confirm_sub2:.1f} ms)",
                offset_db=_offset_db,
            )

        job["status"] = "completed"
        gate_action = (job.get("confirmation_gate") or {}).get("action", "final_kept")
        _final_levels = {
            "sub1": float(_auto_sub_22_sub(final_gain_snapshot, "sub1").get("level_db", 0.0)),
            "sub2": float(_auto_sub_22_sub(final_gain_snapshot, "sub2").get("level_db", 0.0)),
        }
        # Run's scored anchor offset (calibrated coords); the frontend combines
        # it with each trace's display_offset_db to place the target exactly.
        _target_anchor = job.get("main_target_anchor") if isinstance(job.get("main_target_anchor"), dict) else None
        _tvo = _target_anchor.get("target_vertical_offset_db") if _target_anchor else None
        _autosub_meta = _auto_sub_result_meta(
            job, OUTPUT_MODE_SUBWOOFER_22, _final_levels,
            target_vertical_offset_db=float(_tvo) if isinstance(_tvo, (int, float)) else None,
        )
        for _measurement in (baseline_measurement, confirmation_measurement):
            if _measurement is not None:
                _measurement["measurement_kind"] = "auto_sub"
                _measurement["autosub_meta"] = _autosub_meta
        gate_suffix = {
            "alignment_reverted_balance_kept": "; final state regressed locally - incumbent pair kept, balance applied",
            "reverted_to_original": "; final state regressed locally - original state restored",
        }.get(gate_action)
        decision_label = "Kept 2.2 incumbent" if matrix_scoring.get("incumbent_accepted") else "Applied 2.2"
        job["message"] = (
            f"{decision_label}: Sub 1 {best_sub1:.2f} ms / Sub 2 {best_sub2:.2f} ms "
            f"(score {winner['score_pct']:.0f} %, {matrix_scoring.get('reject_reason')})"
        ) + (gate_suffix or "")
        job["result"] = {
            "mode": OUTPUT_MODE_SUBWOOFER_22,
            "original_sub1_alignment_ms": original_sub1_alignment,
            "original_sub2_alignment_ms": original_sub2_alignment,
            "suggested_sub1_alignment_ms": best_sub1,
            "suggested_sub2_alignment_ms": best_sub2,
            "applied_sub1_alignment_ms": best_sub1,
            "applied_sub2_alignment_ms": best_sub2,
            "applied": gate_action != "reverted_to_original",
            "auto_applied": gate_action != "reverted_to_original",
            "apply_decision": (
                "reverted_to_original_state" if gate_action == "reverted_to_original"
                else "fallback_incumbent_pair_balance_kept" if gate_action == "alignment_reverted_balance_kept"
                else "kept_22_incumbent"
                if matrix_scoring.get("incumbent_accepted")
                else "applied_22_combined_matrix"
            ),
            "balance_check": job.get("balance_check"),
            "confirmation_gate": job.get("confirmation_gate"),
            "crossover_hz": fc,
            "confidence": matrix_scoring.get("confidence", "uncertain"),
            "winner": winner,
            "matrix_winner": matrix_scoring.get("matrix_winner"),
            "incumbent_winner": matrix_scoring.get("incumbent_winner"),
            "incumbent_score": matrix_scoring.get("incumbent_score"),
            "accepted_winner": matrix_scoring.get("accepted_winner"),
            "incumbent_accepted": matrix_scoring.get("incumbent_accepted"),
            "reject_reason": matrix_scoring.get("reject_reason"),
            "sub1_coarse_winner": sub1_winner,
            "sub2_coarse_winner": sub2_winner,
            "runner_up": matrix_scoring.get("runner_up"),
            "ranking": matrix_scoring["results"],
            "combined_matrix": job["combined_matrix"],
            "candidate_ledger": candidate_ledger,
            "sweep_count": matrix_sweep_total + 2,
            "candidate_count": len(sub1_scan_delays) + len(sub2_scan_delays) + len(matrix_pairs),
            "sub1_coarse_candidate_count": len(sub1_scan_delays),
            "sub2_coarse_candidate_count": len(sub2_scan_delays),
            "matrix_candidate_count": len(matrix_pairs),
            "valid_count": len(matrix_valid),
            "sub1_coarse_valid_count": len(coarse1_valid),
            "sub2_coarse_valid_count": len(coarse2_valid),
            "baseline_measurement": baseline_measurement,
            "confirmation_measurement": confirmation_measurement,
            **derived_delays,
        }
        logger.info(
            "Auto-sub 2.2 optimize completed: fc=%sHz sub1 %.2f->%.2fms sub2 %.2f->%.2fms "
            "combined_score=%.0f%% score_L=%.1f%% score_R=%.1f%% confidence=%s",
            fc,
            original_sub1_alignment,
            best_sub1,
            original_sub2_alignment,
            best_sub2,
            winner.get("score_pct", 0),
            winner.get("score_L_pct", 0) or 0,
            winner.get("score_R_pct", 0) or 0,
            matrix_scoring.get("confidence", "uncertain"),
        )

    except Exception as exc:
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return
        logger.exception("Auto-sub 2.2 optimize failed")
        job["status"] = "failed"
        job["message"] = f"Auto Sub Optimize 2.2 failed: {exc}"
        job["error"] = {"detail": str(exc)}
        await _restore_original_config()

    finally:
        await _finish_auto_sub_worker(job, job_id)
