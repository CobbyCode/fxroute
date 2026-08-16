# SPDX-License-Identifier: AGPL-3.0-only

"""AutoSub subwoofer alignment optimization: jobs, sweeps, scoring, gain.

The implementation is split into focused modules:

* ``deps``         - dependency injection and shared job/task state
* ``candidates``   - sweep profiles, candidate construction, delay math
* ``scoring``      - result ranking, winner selection, ledger
* ``jobs``         - job API, peak safety, timing, finalization, router
* ``measurement``  - candidate measurement, target analysis, gain analysis
* ``runners``      - the three optimize runners and the start route

The full public surface stays importable from ``measurement.autosub``.
"""

import logging

logger = logging.getLogger(__name__)

from measurement.autosub.deps import (
    AutoSubDependencies,
    _AUTO_SUB_CLEANUP_TASKS,
    _AUTO_SUB_JOBS,
    _AUTO_SUB_WORKER_TASKS,
    _auto_sub_cancel_requested,
    _auto_sub_lock,
    _autosub_dependencies,
    _autosub_deps,
    _cleanup_stale_autosub_cancelling_jobs,
    _dsp_manager,
    _dsp_runtime,
    _measurement_session,
    _measurement_store,
    _start_auto_sub_worker,
    configure_dependencies,
    is_optimization_active,
    shutdown,
)

from measurement.autosub.candidates import (
    _auto_sub_22_candidate_subwoofers,
    _auto_sub_22_global_config,
    _auto_sub_22_name,
    _auto_sub_22_stereo_name,
    _auto_sub_22_sub,
    _auto_sub_22_verify_alignment,
    _auto_sub_apply_candidate,
    _auto_sub_cancelled_candidate,
    _auto_sub_clamped_delay,
    _auto_sub_direct_neighbors,
    _auto_sub_fine_delay_candidates,
    _auto_sub_fine_trigger_reasons,
    _auto_sub_opposite_polarity,
    _auto_sub_polarity_decision,
    _auto_sub_score_value,
    _auto_sub_snapshot_copy,
    _auto_sub_step_ms,
    _auto_sub_sweep_profile,
    _auto_sub_sync_dsp_runtime,
    _restore_auto_sub_original_config,
)

from measurement.autosub.scoring import (
    _AUTO_SUB_MIN_ALIGNMENT_SCORE_GAIN,
    _auto_sub_best_scan_result,
    _auto_sub_candidate_ledger,
    _auto_sub_delay_key,
    _auto_sub_has_points,
    _auto_sub_measurement_from_sweep,
    _auto_sub_rank_results,
    _auto_sub_result_for_delay,
    _auto_sub_score_single_channel_fallback,
    _auto_sub_scoring_confidence,
    _auto_sub_select_accepted_winner,
    _auto_sub_shared_bass_offset,
    _score_auto_sub_combined_candidates,
    _score_auto_sub_matrix_candidates,
    _validate_auto_sub_target_curve_snapshot,
)

from measurement.autosub.jobs import (
    AutoSubPeakSafetyError,
    _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS,
    _AUTO_SUB_STAGE_PEAK_MISMATCH_DB,
    _AUTO_SUB_TIMING_MARKS,
    _append_auto_sub_sweep_timing,
    _auto_sub_job_playback_gain,
    _auto_sub_stage_peak_comparison,
    _auto_sub_stage_peak_prediction,
    _auto_sub_timing_durations,
    _capture_auto_sub_playback_gain,
    _finalize_autosub_job,
    _finish_auto_sub_worker,
    _log_auto_sub_timing_summary,
    cancel_auto_sub_optimize_job,
    get_auto_sub_optimize_job,
    router,
)

from measurement.autosub.measurement import (
    _analyze_auto_sub_main_target_anchor,
    _auto_sub_22_snapshot_with_gain,
    _auto_sub_gain_deltas,
    _auto_sub_gain_log_line,
    _auto_sub_gain_log_score,
    _auto_sub_gain_response_correction,
    _auto_sub_gain_verdict,
    _auto_sub_log_interpolate_points,
    _auto_sub_lr24_frequency_for_attenuation,
    _auto_sub_lr24_highpass_attenuation_db,
    _auto_sub_one_octave_smooth,
    _auto_sub_reconstruct_calibrated_points,
    _auto_sub_stereo_corridor_violation,
    _auto_sub_stereo_probe_plan,
    _auto_sub_third_octave_smooth,
    _calculate_auto_sub_gain,
    _capture_auto_sub_main_references,
    _measure_auto_sub_candidate,
    _measure_auto_sub_combined_candidate,
)

from measurement.autosub.runners import (
    _AUTO_SUB_MAX_CALIBRATION_BYTES,
    _run_auto_sub_22_optimize,
    _run_auto_sub_22_stereo_optimize,
    _run_auto_sub_optimize,
    start_auto_sub_optimize,
)

__all__ = [
    "AutoSubDependencies",
    "AutoSubPeakSafetyError",
    "cancel_auto_sub_optimize_job",
    "configure_dependencies",
    "get_auto_sub_optimize_job",
    "is_optimization_active",
    "router",
    "shutdown",
    "start_auto_sub_optimize",
]
