# SPDX-License-Identifier: AGPL-3.0-only

"""The standard (single/combined) AutoSub optimize runner."""

from __future__ import annotations

import asyncio
import json
import logging

from audio.samplerate import (
    OUTPUT_MODE_SUBWOOFER_21,
    get_audio_output_overview,
    set_audio_output_mode,
)
from typing import Any
from ..candidates import (
    _auto_sub_apply_candidate,
    _auto_sub_clamped_delay,
    _auto_sub_coarse_winner_at_scan_edge,
    _auto_sub_fine_delay_candidates,
    _auto_sub_fine_trigger_reasons,
    _auto_sub_opposite_polarity,
    _auto_sub_polarity_decision,
    _restore_auto_sub_original_config,
    _auto_sub_step_ms,
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
    _AUTO_SUB_LOCAL_DIP_TOLERANCE_DB,
    _auto_sub_gain_deltas,
    _auto_sub_gain_log_line,
    _auto_sub_gain_log_score,
    _auto_sub_gain_response_correction,
    _auto_sub_gain_verdict,
    _auto_sub_local_dip_db,
    _auto_sub_local_dip_gate_sides,
    _calculate_auto_sub_gain,
    _capture_auto_sub_main_references,
    _measure_auto_sub_combined_candidate,
)
from ..scoring import (
    _auto_sub_anchor_shifted_points,
    _auto_sub_best_scan_result,
    _auto_sub_candidate_ledger,
    _auto_sub_display_anchor_reference_db,
    _auto_sub_has_points,
    _auto_sub_measurement_from_sweep,
    _auto_sub_result_meta,
    _auto_sub_rank_results,
    _auto_sub_result_for_delay,
    _auto_sub_select_accepted_winner,
    _auto_sub_select_polarity_shared_winner,
    _auto_sub_shared_bass_offset,
    _score_auto_sub_combined_candidates,
)

logger = logging.getLogger(__name__)


async def _run_auto_sub_optimize(
    job_id: str,
    input_id: str,
    channel: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    scan_delays: list[float],
    fc: int,
    current_alignment: float,
    original_polarity: str,
    original_level: float,
    original_highpass: bool,
    original_config_snapshot: dict[str, Any],
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

    async def _restore_original_config():
        """Restore subwoofer config from snapshot."""
        await _restore_auto_sub_original_config(original_config_snapshot)

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

        sweep_results: list[dict[str, Any]] = []
        total = len(scan_delays) * 2

        # AutoSub bass-focused sweep settings
        auto_sub_sweep_profile = _auto_sub_sweep_profile(fc)

        # Resolve sample rate once for all sweeps
        auto_sub_rate = _resolve_measurement_start_sample_rate()
        await _capture_auto_sub_main_references(
            job=job, fc=fc, input_id=input_id,
            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
            auto_sub_rate=auto_sub_rate, output_mode=OUTPUT_MODE_SUBWOOFER_21,
            original_config_snapshot=original_config_snapshot,
        )
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        coarse_total = len(scan_delays)
        coarse_sweep_total = coarse_total * 2
        balance_sweep_total = 2
        total = coarse_sweep_total + balance_sweep_total

        # Coarse level balance before any alignment decision: the incumbent
        # state is measured and its Target residual — via the same method the
        # later Gain stage uses — is applied as a bounded trim, so the
        # alignment/polarity optimization runs under a realistic sub/main
        # balance and the later Gain step becomes a fine trim.
        job["stage"] = "balance_check"
        job["message"] = "Auto Sub Optimize: measuring incumbent balance"
        balance_sweep = await _measure_auto_sub_combined_candidate(
            delay_ms=current_alignment, job=job, candidate_index=1, total=1,
            sweep_index_start=1, sweep_total=total, stage="balance_check", fc=fc,
            input_id=input_id, mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
            calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
            auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
            original_level=original_level, original_polarity=original_polarity,
            original_highpass=original_highpass,
        )
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return
        balance_diagnostics = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_21, target_curve=job.get("target_curve"),
            anchor=job.get("main_target_anchor"),
            winner_curves={
                "left": balance_sweep.get("calibrated_points_left") or [],
                "right": balance_sweep.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        balance_deltas = _auto_sub_gain_deltas(
            balance_diagnostics, OUTPUT_MODE_SUBWOOFER_21, max_abs_db=6.0,
        )
        balance_delta = balance_deltas.get("left", 0.0)
        balanced_level = max(-24.0, min(12.0, original_level + balance_delta))
        job["balance_check"] = {
            "deltas_db": {side: round(balance_delta, 3) for side in ("left", "right")},
            "residuals_db": {
                side: ((balance_diagnostics.get("channels", {}).get(side) or {}).get("raw_recommendation_db"))
                for side in ("left", "right")
            },
            "confidence": balance_diagnostics.get("confidence"),
            "level_before_db": original_level,
            "level_after_db": balanced_level,
            "applied": bool(balance_deltas),
        }
        logger.info("AUTOSUB_BALANCE job=%s mode=2.1 %s", job_id, json.dumps(job["balance_check"], sort_keys=True))

        for idx, delay_ms in enumerate(scan_delays):
            sweep_results.append(
                await _measure_auto_sub_combined_candidate(
                    delay_ms=delay_ms,
                    job=job,
                    candidate_index=idx + 1,
                    total=coarse_total,
                    sweep_index_start=balance_sweep_total + (idx * 2) + 1,
                    sweep_total=total,
                    stage="coarse",
                    fc=fc,
                    input_id=input_id,
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate,
                    original_level=balanced_level,
                    original_polarity=original_polarity,
                    original_highpass=original_highpass,
                )
            )
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

            # Live: push baseline measurement to job for frontend polling display.
            # The balance-stage sweep is the true Before state (original level);
            # the coarse incumbent candidate was measured at the balanced level.
            if round(float(delay_ms), 2) == round(float(current_alignment), 2):
                if _auto_sub_has_points(balance_sweep, "points_left") or _auto_sub_has_points(balance_sweep, "points_right"):
                    job["baseline_measurement"] = _auto_sub_measurement_from_sweep(
                        balance_sweep, "Before", f"AutoSub Baseline ({current_alignment:.1f} ms)"
                    )

        # Score candidates
        valid = [r for r in sweep_results if _auto_sub_has_points(r, "points_left") or _auto_sub_has_points(r, "points_right")]
        if not valid:
            job["status"] = "failed"
            job["message"] = "No valid sweep results to score"
            job["error"] = {"detail": "All sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return

        step_ms = _auto_sub_step_ms(fc)
        coarse_scoring = _score_auto_sub_combined_candidates(
            sweep_results,
            crossover_hz=fc,
            low_guard_reference_delay_ms=current_alignment,
        )
        valid = list(coarse_scoring.get("scored_candidates") or valid)
        coarse_winner = coarse_scoring["winner"]
        coarse_runner_up = coarse_scoring.get("runner_up")
        fine_trigger_reasons = _auto_sub_fine_trigger_reasons(coarse_scoring, scan_delays)
        fine_delays: list[float] = []
        fine_results: list[dict[str, Any]] = []
        fine_valid: list[dict[str, Any]] = []
        fine_winner: dict[str, Any] | None = None
        fine_scoring: dict[str, Any] | None = None

        fine_scan: dict[str, Any] = {
            "enabled": bool(fine_trigger_reasons),
            "triggered": False,
            "reasons": fine_trigger_reasons,
            "step_ms": step_ms,
            "fine_step_ms": step_ms / 4.0,
            "candidates": [],
            "sweep_count": 0,
            "valid_count": 0,
            "status": "skipped" if not fine_trigger_reasons else "pending",
            "coarse_winner": coarse_winner,
            "coarse_runner_up": coarse_runner_up,
        }
        final_decision_pool = list(sweep_results)

        if fine_trigger_reasons:
            coarse_winner_edge = _auto_sub_coarse_winner_at_scan_edge(
                float(coarse_winner.get("delay_ms", 0.0) or 0.0), scan_delays,
            )
            fine_delays = _auto_sub_fine_delay_candidates(
                coarse_winner, coarse_runner_up, step_ms,
                {round(float(delay), 2) for delay in scan_delays},
                scan_delays=scan_delays,
            )
            fine_scan.update({
                "triggered": True,
                "candidates": fine_delays,
                "coarse_winner_at_scan_edge": coarse_winner_edge,
                "status": "running" if fine_delays else "skipped",
            })
            job["fine_scan"] = fine_scan
            if fine_delays:
                fine_candidate_total = len(fine_delays)
                fine_sweep_total = fine_candidate_total * 2
                total = coarse_sweep_total + balance_sweep_total + fine_sweep_total
                reason_text = ", ".join(fine_trigger_reasons)
                job["stage"] = "fine_scan"
                job["message"] = f"Fine-Scan triggered ({reason_text}); {len(fine_delays)} candidates"
                job["progress"] = {
                    "current": coarse_sweep_total,
                    "total": total,
                    "sweep_current": coarse_sweep_total,
                    "sweep_total": total,
                    "candidate_current": 0,
                    "candidate_total": fine_candidate_total,
                    "stage": "fine",
                    "reason": reason_text,
                }
                if _auto_sub_cancel_requested(job):
                    job["message"] = "Auto Sub Optimize cancelled."
                    await _restore_original_config()
                    return
                for idx, delay_ms in enumerate(fine_delays):
                    fine_results.append(
                        await _measure_auto_sub_combined_candidate(
                            delay_ms=delay_ms,
                            job=job,
                            candidate_index=idx + 1,
                            total=fine_candidate_total,
                            sweep_index_start=coarse_sweep_total + (idx * 2) + 1,
                            sweep_total=total,
                            stage="fine",
                            fc=fc,
                            input_id=input_id,
                            mic_input_channel=mic_input_channel,
                            reference_input_channel=reference_input_channel,
                            calibration_ref=calibration_ref,
                            calibration_filename=calibration_filename,
                            calibration_bytes=calibration_bytes,
                            auto_sub_sweep_profile=auto_sub_sweep_profile,
                            auto_sub_rate=auto_sub_rate,
                            original_level=balanced_level,
                            original_polarity=original_polarity,
                            original_highpass=original_highpass,
                        )
                    )
                    if _auto_sub_cancel_requested(job):
                        job["message"] = "Auto Sub Optimize cancelled."
                        await _restore_original_config()
                        return

                fine_valid = [r for r in fine_results if _auto_sub_has_points(r, "points_left") or _auto_sub_has_points(r, "points_right")]
                if fine_valid:
                    fine_scoring = _score_auto_sub_combined_candidates(
                        fine_results,
                        crossover_hz=fc,
                        low_guard_reference_delay_ms=current_alignment,
                    )
                    fine_valid = list(fine_scoring.get("scored_candidates") or fine_valid)
                    fine_winner = fine_scoring["winner"]
                    fine_scan.update({
                        "status": "completed",
                        "candidate_count": len(fine_delays),
                        "sweep_count": len(fine_delays) * 2,
                        "valid_count": len(fine_valid),
                        "winner": fine_winner,
                        "runner_up": fine_scoring.get("runner_up"),
                        "results": fine_scoring["results"],
                    })
                    combined_valid = valid + fine_valid
                    final_decision_pool = list(combined_valid)
                    final_scoring = _score_auto_sub_combined_candidates(
                        combined_valid,
                        crossover_hz=fc,
                        low_guard_reference_delay_ms=current_alignment,
                    )
                    combined_valid = list(final_scoring.get("scored_candidates") or combined_valid)
                else:
                    fine_scan.update({
                        "status": "no_valid_results",
                        "candidate_count": len(fine_delays),
                        "sweep_count": len(fine_delays) * 2,
                        "valid_count": 0,
                        "winner": None,
                        "runner_up": None,
                        "results": fine_results,
                    })
                    combined_valid = valid
                    final_scoring = coarse_scoring
            else:
                fine_scan.update({
                    "status": "skipped",
                    "reason": "no fine candidates generated",
                })
                combined_valid = valid
                final_scoring = coarse_scoring
        else:
            combined_valid = valid
            final_scoring = coarse_scoring

        job["fine_scan"] = fine_scan
        _auto_sub_rank_results(final_scoring["results"])

        # Re-attach scan stage from original measured candidates (scoring creates fresh dicts)
        scan_by_delay: dict[float, str] = {}
        for result in valid:
            delay_key = round(float(result.get("delay_ms", 0.0)), 2)
            scan_by_delay[delay_key] = result.get("scan", "coarse")
        for result in fine_valid:
            delay_key = round(float(result.get("delay_ms", 0.0)), 2)
            scan_by_delay[delay_key] = result.get("scan", "fine")

        coarse_score_by_delay = {
            round(float(result.get("delay_ms", 0.0)), 2): result
            for result in coarse_scoring["results"]
        }
        for result in final_scoring["results"]:
            delay_key = round(float(result.get("delay_ms", 0.0)), 2)
            result["scan"] = scan_by_delay.get(delay_key, "coarse")
            if result["scan"] == "coarse":
                coarse_score = coarse_score_by_delay.get(delay_key)
                if coarse_score:
                    result["coarse_score"] = coarse_score.get("score")
                    result["coarse_score_pct"] = coarse_score.get("score_pct")
                    result["coarse_rank"] = coarse_score.get("rank")

        final_fine_winner = next(
            (result for result in final_scoring["results"] if result.get("scan") == "fine"),
            fine_winner,
        )
        if final_fine_winner is not None and fine_scan.get("status") == "completed":
            fine_scan["final_winner"] = final_fine_winner
        final_coarse_winner = _auto_sub_best_scan_result(final_scoring["results"], "coarse") or coarse_winner
        incumbent_winner = _auto_sub_result_for_delay(final_scoring["results"], current_alignment)
        acceptance = _auto_sub_select_accepted_winner(
            coarse_winner=final_coarse_winner,
            fine_winner=final_fine_winner if fine_scan.get("status") == "completed" else None,
            incumbent_winner=incumbent_winner,
            score_epsilon=0.001,
        )
        fine_scan["coarse_winner"] = final_coarse_winner
        fine_scan["fine_winner"] = final_fine_winner
        fine_scan["incumbent_winner"] = incumbent_winner
        fine_scan["incumbent_score"] = acceptance["incumbent_score"]
        fine_scan["accepted_winner"] = acceptance["accepted_winner"]
        fine_scan["fine_accepted"] = acceptance["fine_accepted"]
        fine_scan["reject_reason"] = acceptance["reject_reason"]

        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        winner = acceptance["accepted_winner"]
        best_delay = winner["delay_ms"]
        confidence = str(final_scoring.get("confidence") or "uncertain")
        runner_up = final_scoring.get("runner_up")
        winner_score_pct = float(winner.get("score_pct", 0.0) or 0.0)
        runner_score_pct = float(runner_up.get("score_pct", 0.0) or 0.0) if runner_up else 0.0
        winner_margin_pct = winner_score_pct - runner_score_pct if runner_up else 100.0
        original_score_pct = None
        original_delay_key = round(float(current_alignment), 2)
        for scored_result in final_scoring.get("results", []):
            if round(float(scored_result.get("delay_ms", 0.0)), 2) == original_delay_key:
                original_score_pct = float(scored_result.get("score_pct", 0.0) or 0.0)
                break
        score_gain_pct = winner_score_pct - original_score_pct if original_score_pct is not None else None

        auto_apply = False
        apply_decision = "not_applied_uncertain_confidence"
        if incumbent_winner is not None and round(float(best_delay), 2) == round(float(current_alignment), 2):
            apply_decision = "not_applied_incumbent_better"
        elif confidence == "clear":
            auto_apply = True
            apply_decision = "applied_clear_confidence"
        elif confidence == "close":
            if winner_margin_pct < 2.0:
                apply_decision = "not_applied_close_margin_below_2pp"
            elif score_gain_pct is not None and score_gain_pct < 3.0:
                apply_decision = "not_applied_close_gain_below_3pp"
            else:
                auto_apply = True
                apply_decision = "applied_close_confidence"
        elif (
            confidence == "uncertain"
            and score_gain_pct is not None
            and score_gain_pct >= 10.0
            and winner_score_pct >= 70.0
        ):
            auto_apply = True
            apply_decision = "applied_uncertain_large_gain"

        apply_ok = False
        applied_delay = current_alignment
        if auto_apply:
            try:
                sub_config = {
                    "crossover_frequency_hz": fc,
                    "sub_alignment_ms": best_delay,
                    "sub_level_db": balanced_level,
                    "sub_polarity": original_polarity,
                    "main_highpass_enabled": original_highpass,
                }
                apply_ok = await _auto_sub_apply_candidate(
                    output_mode=OUTPUT_MODE_SUBWOOFER_21,
                    global_config=sub_config,
                    subwoofers_config=None,
                    verify=lambda overview: float(overview.get("subwoofer", {}).get("sub_alignment_ms", -999)) == best_delay,
                    load_overview=_load_audio_output_mode,
                )
                if apply_ok:
                    applied_delay = best_delay
            except Exception:
                logger.exception("Auto-sub: failed to construct winner delay %.2f ms", best_delay)
        else:
            await _restore_original_config()
            apply_ok = True

        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        if not apply_ok:
            job["status"] = "failed"
            job["message"] = f"Scoring succeeded but failed to apply winner delay {best_delay} ms"
            job["error"] = {"detail": "Winner apply failed — original config restored"}
            await _restore_original_config()
            return

        stored_winner = winner if auto_apply else (incumbent_winner or winner)
        gain_winner = _auto_sub_result_for_delay(list(sweep_results) + list(fine_results), stored_winner.get("delay_ms", current_alignment)) or {}
        final_polarity = original_polarity
        polarity_check: dict[str, Any] = {
            "incumbent": original_polarity, "selected": original_polarity,
            "accepted": False, "reason": "incumbent_protected",
        }
        if gain_winner and auto_apply:
            opposite = _auto_sub_opposite_polarity(original_polarity)
            alt = await _measure_auto_sub_combined_candidate(
                delay_ms=applied_delay, job=job, candidate_index=1, total=1,
                sweep_index_start=total + 1, sweep_total=total + 2, stage="polarity_check", fc=fc,
                input_id=input_id, mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                original_level=balanced_level, original_polarity=opposite,
                original_highpass=original_highpass,
            )
            try:
                gate_scoring = _score_auto_sub_combined_candidates(
                    [dict(gain_winner, delay_ms=0.0), dict(alt, delay_ms=1.0)], crossover_hz=fc,
                    low_guard_reference_delay_ms=0.0,
                )
                scored_incumbent = _auto_sub_result_for_delay(gate_scoring["results"], 0.0) or {}
                scored_alt = _auto_sub_result_for_delay(gate_scoring["results"], 1.0) or {}
                # Gate only: decide whether inverted refinement candidates are
                # worth measuring. The flip itself is decided from the shared
                # set below.
                gate_decision = _auto_sub_polarity_decision(scored_incumbent, scored_alt)
                polarity_check["gate"] = gate_decision
                if gate_decision["accepted"]:
                    local_delays = [
                        _auto_sub_clamped_delay(applied_delay + offset)
                        for offset in (-_auto_sub_step_ms(fc) / 2.0, -_auto_sub_step_ms(fc) / 4.0,
                                       _auto_sub_step_ms(fc) / 4.0, _auto_sub_step_ms(fc) / 2.0)
                    ]
                    invert_rows: list[dict[str, Any]] = [dict(alt, delay_ms=1.0)]
                    measured_by_placeholder: dict[float, dict[str, Any]] = {1.0: alt}
                    for idx, delay in enumerate(local_delays):
                        measured = await _measure_auto_sub_combined_candidate(
                            delay_ms=delay, job=job, candidate_index=idx + 1, total=len(local_delays),
                            sweep_index_start=total + 3 + idx * 2, sweep_total=total + 2 + len(local_delays) * 2,
                            stage="polarity_fine", fc=fc, input_id=input_id,
                            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                            auto_sub_rate=auto_sub_rate, original_level=balanced_level,
                            original_polarity=opposite, original_highpass=original_highpass,
                        )
                        placeholder = 2.0 + idx
                        invert_rows.append(dict(measured, delay_ms=placeholder))
                        measured_by_placeholder[placeholder] = measured
                    shared_scoring = _score_auto_sub_combined_candidates(
                        [dict(gain_winner, delay_ms=0.0)] + invert_rows,
                        crossover_hz=fc, low_guard_reference_delay_ms=0.0,
                    )
                    shared_decision = _auto_sub_select_polarity_shared_winner(shared_scoring["results"])
                    polarity_check["shared_set"] = shared_decision
                    winner_placeholder = float(shared_decision["alternative_delay_ms"] or 1.0)
                    polarity_check["fine_scan"] = {
                        "candidates": local_delays,
                        "winner": measured_by_placeholder.get(winner_placeholder),
                    }
                    if shared_decision["accepted"]:
                        selected_measured = measured_by_placeholder.get(winner_placeholder) or alt
                        final_polarity = opposite
                        gain_winner = selected_measured
                        applied_delay = _auto_sub_clamped_delay(float(selected_measured.get("delay_ms", applied_delay) or applied_delay))
                        polarity_check["accepted"] = True
                        polarity_check["score_gain"] = shared_decision["score_gain"]
                        polarity_check["reason"] = shared_decision["reason"]
                        polarity_check["selected"] = opposite
                        polarity_check["selected_delay_ms"] = applied_delay
                    else:
                        polarity_check["reason"] = shared_decision["reason"]
                        polarity_check["selected"] = original_polarity
                else:
                    polarity_check.update(gate_decision)
                    polarity_check["selected"] = original_polarity
                polarity_check.update({
                    "alternative": opposite,
                    "incumbent_score": scored_incumbent.get("score"),
                    "alternative_score": scored_alt.get("score"),
                })
            except Exception as exc:
                logger.warning("Auto-sub polarity check unavailable; restoring incumbent: %s", exc)
                final_polarity = original_polarity
                polarity_check.update({"accepted": False, "selected": original_polarity, "reason": "measurement_or_scoring_failed"})

            await asyncio.to_thread(set_audio_output_mode, OUTPUT_MODE_SUBWOOFER_21, {
                "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                "sub_level_db": balanced_level, "sub_polarity": final_polarity,
                "main_highpass_enabled": original_highpass,
            })
            if _dsp_runtime() is not None:
                await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))
        job["polarity_check"] = polarity_check
        job["auto_gain"] = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_21,
            target_curve=job.get("target_curve"), anchor=job.get("main_target_anchor"),
            winner_curves={
                "left": gain_winner.get("calibrated_points_left") or [],
                "right": gain_winner.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        logger.info("AUTOSUB_GAIN mode=2.1 diagnostics=%s", json.dumps(job["auto_gain"], sort_keys=True))
        gain_deltas = _auto_sub_gain_deltas(job["auto_gain"], OUTPUT_MODE_SUBWOOFER_21, max_abs_db=6.0)
        applied_gain_delta = gain_deltas.get("left", 0.0)
        _auto_sub_gain_log_line("AUTOGAIN_INIT", {
            "mode": OUTPUT_MODE_SUBWOOFER_21, "xo_hz": fc,
            "target": (job.get("target_curve") or {}).get("label"),
            "anchor_hz": (job.get("main_target_anchor") or {}).get("usable_band_hz"),
            "target_offset_db": (job.get("main_target_anchor") or {}).get("target_vertical_offset_db"),
            "gain_before": original_level,
            "winner_delta_left": (job["auto_gain"].get("channels", {}).get("left") or {}).get("target_delta_db"),
            "winner_delta_right": (job["auto_gain"].get("channels", {}).get("right") or {}).get("target_delta_db"),
            "combined_delta_db": (job["auto_gain"].get("recommendation") or {}).get("raw_delta_db"),
            "first_step_db": applied_gain_delta,
        })
        gained_level = max(-24.0, min(12.0, balanced_level + applied_gain_delta))
        if gain_deltas:
            await asyncio.to_thread(set_audio_output_mode, OUTPUT_MODE_SUBWOOFER_21, {
                "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                "sub_level_db": gained_level, "sub_polarity": final_polarity,
                "main_highpass_enabled": original_highpass,
            })
            if _dsp_runtime() is not None:
                await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))
        gain_after_sweep = await _measure_auto_sub_combined_candidate(
            delay_ms=applied_delay, job=job, candidate_index=1, total=1,
            sweep_index_start=total + 1, sweep_total=total + 2, stage="gain_after", fc=fc,
            input_id=input_id, mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
            calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
            auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
            original_level=gained_level, original_polarity=final_polarity,
            original_highpass=original_highpass, output_mode=OUTPUT_MODE_SUBWOOFER_21,
            original_config_snapshot=original_config_snapshot,
        )
        gain_after = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_21, target_curve=job.get("target_curve"),
            anchor=job.get("main_target_anchor"), winner_curves={
                "left": gain_after_sweep.get("calibrated_points_left") or [],
                "right": gain_after_sweep.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        gain_verdict = _auto_sub_gain_verdict(job["auto_gain"], gain_after, OUTPUT_MODE_SUBWOOFER_21)
        final_gain_deltas = gain_deltas if gain_verdict["accepted"] else {"left": 0.0, "right": 0.0}
        final_gain_level = gained_level if gain_verdict["accepted"] else balanced_level
        final_gain_sweep = gain_after_sweep if gain_verdict["accepted"] else gain_winner
        correction_deltas: dict[str, float] = {}
        correction_plan = None
        correction_after = None
        correction_verdict = None
        if not gain_verdict["accepted"]:
            await asyncio.to_thread(set_audio_output_mode, OUTPUT_MODE_SUBWOOFER_21, {
                "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                "sub_level_db": balanced_level, "sub_polarity": final_polarity,
                "main_highpass_enabled": original_highpass,
            })
            if _dsp_runtime() is not None:
                await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))
        else:
            correction_plan = _auto_sub_gain_response_correction(
                job["auto_gain"], gain_after, gain_deltas, OUTPUT_MODE_SUBWOOFER_21,
            )
            correction_deltas = correction_plan.get("deltas_db") or {}
            correction_delta = correction_deltas.get("left", 0.0)
            corrected_level = max(-24.0, min(12.0, gained_level + correction_delta))
            if not correction_plan.get("available"):
                correction_verdict = {
                    "accepted": False,
                    "reason": correction_plan.get("reason"),
                    "channels": {},
                    "step1_retained": True,
                }
            elif abs(correction_delta) > 0.0005:
                await asyncio.to_thread(set_audio_output_mode, OUTPUT_MODE_SUBWOOFER_21, {
                    "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                    "sub_level_db": corrected_level, "sub_polarity": final_polarity,
                    "main_highpass_enabled": original_highpass,
                })
                if _dsp_runtime() is not None:
                    await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))
                correction_sweep = await _measure_auto_sub_combined_candidate(
                    delay_ms=applied_delay, job=job, candidate_index=1, total=1,
                    sweep_index_start=total + 3, sweep_total=total + 4, stage="gain_correction_after", fc=fc,
                    input_id=input_id, mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                    original_level=corrected_level, original_polarity=final_polarity,
                    original_highpass=original_highpass, output_mode=OUTPUT_MODE_SUBWOOFER_21,
                    original_config_snapshot=original_config_snapshot,
                )
                correction_after = _calculate_auto_sub_gain(
                    mode=OUTPUT_MODE_SUBWOOFER_21, target_curve=job.get("target_curve"),
                    anchor=job.get("main_target_anchor"), winner_curves={
                        "left": correction_sweep.get("calibrated_points_left") or [],
                        "right": correction_sweep.get("calibrated_points_right") or [],
                    }, crossover_hz=fc,
                )
                correction_verdict = _auto_sub_gain_verdict(gain_after, correction_after, OUTPUT_MODE_SUBWOOFER_21)
                if correction_verdict["accepted"]:
                    final_gain_deltas = {
                        "left": applied_gain_delta + correction_delta,
                        "right": applied_gain_delta + correction_delta,
                    }
                    final_gain_level = corrected_level
                    final_gain_sweep = correction_sweep
                else:
                    await asyncio.to_thread(set_audio_output_mode, OUTPUT_MODE_SUBWOOFER_21, {
                        "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                        "sub_level_db": gained_level, "sub_polarity": final_polarity,
                        "main_highpass_enabled": original_highpass,
                    })
                    if _dsp_runtime() is not None:
                        await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))
        _auto_sub_gain_log_line("AUTOGAIN_FEEDBACK", {
            "gain_after_step1": gained_level,
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
            "gain_final": final_gain_level, "score_final": _auto_sub_gain_log_score(score_final_source),
            "decision": decision, "reason": result_reason,
            "delay_final": applied_delay,
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
            "original_level_db": original_level,
            "balance_delta_db": balance_delta,
            "final_level_db": final_gain_level,
            "stage_output_peaks": (final_gain_sweep or {}).get("stage_output_peaks"),
        })

        # Final Before/After confirmation gate: the adopted state must not
        # introduce a clearly deeper local dip than the measured Before
        # state (see _auto_sub_local_dip_db for why a local metric is needed
        # here and how the tolerance was derived). On failure the incumbent
        # alignment under the balanced level is measured and adopted when it
        # passes; otherwise the original state is restored.
        confirmation_gate = None
        if auto_apply:
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
                job["message"] = "Auto Sub Optimize: final state regressed locally; measuring incumbent alignment at balanced level"
                recheck_sweep = await _measure_auto_sub_combined_candidate(
                    delay_ms=current_alignment, job=job, candidate_index=1, total=1,
                    sweep_index_start=total + 5, sweep_total=total + 7,
                    stage="confirmation_recheck", fc=fc, input_id=input_id,
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate, original_level=balanced_level,
                    original_polarity=original_polarity, original_highpass=original_highpass,
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
                    # Keep the balance fix, revert the alignment (and any
                    # polarity flip) to the incumbent state the balance was
                    # computed for.
                    applied_delay = current_alignment
                    final_polarity = original_polarity
                    final_gain_level = balanced_level
                    final_gain_sweep = recheck_sweep
                    final_gain_deltas = {"left": balance_delta, "right": balance_delta}
                    await asyncio.to_thread(set_audio_output_mode, OUTPUT_MODE_SUBWOOFER_21, {
                        "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                        "sub_level_db": final_gain_level, "sub_polarity": final_polarity,
                        "main_highpass_enabled": original_highpass,
                    })
                    if _dsp_runtime() is not None:
                        await _dsp_runtime().sync(await asyncio.to_thread(get_audio_output_overview))
                    confirmation_gate["action"] = "alignment_reverted_balance_kept"
                else:
                    await _restore_original_config()
                    final_gain_sweep = balance_sweep
                    final_gain_level = original_level
                    applied_delay = current_alignment
                    confirmation_gate["action"] = "reverted_to_original"
            job["confirmation_gate"] = confirmation_gate
            logger.info("AUTOSUB_CONF_GATE job=%s %s", job_id, json.dumps(confirmation_gate, sort_keys=True))
        stored_fine_accepted = bool(acceptance["fine_accepted"] and auto_apply)
        stored_reject_reason = acceptance["reject_reason"]
        if not auto_apply and stored_winner is incumbent_winner and round(float(best_delay), 2) != round(float(current_alignment), 2):
            stored_reject_reason = apply_decision
        fine_scan["accepted_winner"] = stored_winner
        fine_scan["fine_accepted"] = stored_fine_accepted
        fine_scan["reject_reason"] = stored_reject_reason
        candidate_ledger = (
            _auto_sub_candidate_ledger(
                sweep_results, final_scoring, mode="2.1", phase="coarse",
                roles={
                    "coarse_winner": final_coarse_winner,
                    "final_accepted_winner": stored_winner,
                },
                decision_pool=final_decision_pool,
                requested_incumbent={"delay_ms": current_alignment},
            )
            + _auto_sub_candidate_ledger(
                fine_results, final_scoring, mode="2.1", phase="fine",
                roles={"fine_winner": final_fine_winner, "final_accepted_winner": stored_winner},
                decision_pool=final_decision_pool,
                requested_incumbent={"delay_ms": current_alignment},
            )
        )

        job["status"] = "completed"
        gate_action = (job.get("confirmation_gate") or {}).get("action") if auto_apply else None
        gate_suffix = {
            "alignment_reverted_balance_kept": "; final state regressed locally - incumbent alignment kept, balance applied",
            "reverted_to_original": "; final state regressed locally - original state restored",
        }.get(gate_action)
        job["message"] = (
            f"Applied: {best_delay} ms (score {winner['score_pct']:.0f} %)"
            if auto_apply
            else f"Suggested: {best_delay} ms (not applied: {confidence})"
        ) + (gate_suffix or "")
        _log_auto_sub_timing_summary(job)

        # Build baseline and confirmation measurements for before/after graph display
        baseline_measurement = None
        confirmation_measurement = None
        all_sweep_results = list(sweep_results) + list(fine_results)
        # The balance-stage sweep is the true Before state (original level);
        # the coarse incumbent candidate was measured at the balanced level.
        baseline_sweep = balance_sweep

        # Chain-anchor display correction: pull each trace back to the run's
        # median 200-600 Hz main-only level so an occasional chain gain
        # excursion no longer fakes a Before/After level change. Relative
        # differences are preserved.
        _display_anchor_reference_db = _auto_sub_display_anchor_reference_db([
            points for sweep in list(all_sweep_results) + [final_gain_sweep]
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
            return adjusted

        baseline_sweep = _anchor_adjusted_combined_sweep(baseline_sweep)
        _offset_db = _auto_sub_shared_bass_offset(
            baseline_sweep.get("points_left") if baseline_sweep else [],
            baseline_sweep.get("points_right") if baseline_sweep else [],
        )
        if baseline_sweep and (_auto_sub_has_points(baseline_sweep, "points_left") or _auto_sub_has_points(baseline_sweep, "points_right")):
            baseline_measurement = _auto_sub_measurement_from_sweep(
                baseline_sweep, "Before", f"AutoSub Baseline ({current_alignment:.1f} ms)",
                offset_db=_offset_db,
            )
        confirm_delay = best_delay if auto_apply else current_alignment

        def _points_sweep(sweep: dict[str, Any] | None) -> dict[str, Any] | None:
            return sweep if sweep and (
                _auto_sub_has_points(sweep, "points_left") or _auto_sub_has_points(sweep, "points_right")
            ) else None

        # Prefer the final measured sweep (gain verification or polarity-refined
        # winner) so the confirmation always reflects the applied state; fall
        # back to the scan sweep at the confirmed delay.
        confirmation_sweep = _points_sweep(final_gain_sweep) or _points_sweep(
            _auto_sub_result_for_delay(all_sweep_results, confirm_delay)
        )
        confirmation_sweep = _anchor_adjusted_combined_sweep(confirmation_sweep)
        if confirmation_sweep:
            sweep_delay = confirmation_sweep.get("delay_ms")
            if sweep_delay is not None:
                confirm_delay = float(sweep_delay)
            confirm_label = "After" if auto_apply else "Current"
            confirmation_measurement = _auto_sub_measurement_from_sweep(
                confirmation_sweep, confirm_label, f"AutoSub {confirm_label} ({confirm_delay:.1f} ms)",
                offset_db=_offset_db,
            )

        _final_level = final_gain_level if auto_apply else balanced_level
        _autosub_meta = _auto_sub_result_meta(job, OUTPUT_MODE_SUBWOOFER_21, {"sub": _final_level})
        for _measurement in (baseline_measurement, confirmation_measurement):
            if _measurement is not None:
                _measurement["measurement_kind"] = "auto_sub"
                _measurement["autosub_meta"] = _autosub_meta

        job["result"] = {
            "original_alignment_ms": current_alignment,
            "suggested_alignment_ms": best_delay,
            "applied_alignment_ms": applied_delay,
            "applied_sub_alignment_ms": applied_delay,
            "applied": bool(auto_apply and gate_action != "reverted_to_original"),
            "auto_applied": bool(auto_apply and gate_action != "reverted_to_original"),
            "apply_decision": (
                apply_decision if gate_action is None
                else {
                    "final_kept": apply_decision,
                    "alignment_reverted_balance_kept": "fallback_incumbent_alignment_balance_kept",
                    "reverted_to_original": "reverted_to_original_state",
                }.get(gate_action, apply_decision)
            ),
            "balance_check": job.get("balance_check"),
            "confirmation_gate": job.get("confirmation_gate"),
            "winner_margin_pct": round(winner_margin_pct, 1),
            "score_gain_pct": round(score_gain_pct, 1) if score_gain_pct is not None else None,
            "original_score_pct": round(original_score_pct, 1) if original_score_pct is not None else None,
            "crossover_hz": fc,
            "confidence": confidence,
            "winner": winner,
            "coarse_winner": final_coarse_winner,
            "coarse_runner_up": coarse_runner_up,
            "fine_winner": final_fine_winner,
            "incumbent_winner": incumbent_winner,
            "incumbent_score": acceptance["incumbent_score"],
            "accepted_winner": stored_winner,
            "fine_accepted": stored_fine_accepted,
            "reject_reason": stored_reject_reason,
            "runner_up": final_scoring.get("runner_up"),
            "ranking": final_scoring["results"],
            "candidate_ledger": candidate_ledger,
            "sweep_count": total + 2,
            "candidate_count": coarse_total + len(fine_delays),
            "coarse_candidate_count": coarse_total,
            "fine_candidate_count": len(fine_delays),
            "coarse_sweep_count": coarse_sweep_total,
            "fine_sweep_count": len(fine_delays) * 2,
            "valid_count": len(combined_valid),
            "coarse_valid_count": len(valid),
            "fine_valid_count": len(fine_valid),
            "fine_scan": fine_scan,
            "coarse_winner_at_scan_edge": fine_scan.get("coarse_winner_at_scan_edge"),
            "display_anchor_reference_db": _display_anchor_reference_db,
            "baseline_measurement": baseline_measurement,
            "confirmation_measurement": confirmation_measurement,
        }

        logger.info(
            "Auto-sub optimize completed: fc=%sHz suggested=%.2fms applied=%s applied_delay=%.2fms combined_score=%.0f%% "
            "score_L=%.1f%% score_R=%.1f%% confidence=%s decision=%s fine_scan=%s",
            fc,
            best_delay,
            auto_apply,
            applied_delay,
            winner.get("score_pct", 0),
            winner.get("score_L_pct", 0) or 0,
            winner.get("score_R_pct", 0) or 0,
            confidence,
            apply_decision,
            fine_scan.get("status"),
        )

    except Exception as exc:
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return
        logger.exception("Auto-sub optimize failed")
        job["status"] = "failed"
        job["message"] = f"Auto Sub Optimize failed: {exc}"
        job["error"] = {"detail": str(exc)}
        await _restore_original_config()

    finally:
        await _finish_auto_sub_worker(job, job_id)
