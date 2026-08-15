"""Shared immutable constants for FXRoute measurement processing.

Single authoritative definitions for constants used by more than one
measurement module.  Modules import from here instead of redefining values,
so a constant cannot silently exist in one module and be missing from
another.
"""

from __future__ import annotations

import os


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


# Measurement scope identifiers and user-facing scope note.
MEASUREMENT_SCOPE_ACTIVE_CHAIN = "active_chain"
MEASUREMENT_SCOPE_RAW_HELPER = "raw_helper"
MEASUREMENT_SCOPES = {
    MEASUREMENT_SCOPE_ACTIVE_CHAIN,
    MEASUREMENT_SCOPE_RAW_HELPER,
}
MEASUREMENT_SCOPE_NOTE = (
    "FXRoute measures with a host-local sweep through the active PipeWire output and selected microphone input. "
    "The result is a practical response trace for comparison and PEQ drafting, independent of the active DSP preset."
)

# Saved-measurement display defaults and trace palette.
DISPLAY_DEFAULTS = {
    "normalize": True,
    "smoothing": "1/6-oct",
    "target_db": 0,
    "x_range_hz": [20, 20000],
}
TRACE_COLORS = [
    "#6ee7b7",
    "#a78bfa",
    "#f59e0b",
    "#60a5fa",
    "#f472b6",
    "#f87171",
]

# Sweep definition shared by playback, capture, and analysis.
SWEEP_START_HZ = 10.0
SWEEP_END_HZ = 22_000.0
SWEEP_V2_SECONDS = 11.0
SWEEP_V2_LEAD_IN_SECONDS = 0.5
SWEEP_V2_TAIL_SECONDS = 1.25

# Capture quality thresholds shared by the store and the analyzer.
CAPTURE_CLIP_FAIL_DBFS = -0.2

# Job lifecycle and retention policy shared by the store and persistence.
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})
JOB_RECORD_RETENTION_DAYS = 30
IR_DEBUG_SEGMENT_RETENTION_SEGMENTS = 10

# Analyzer trusted-band and response-outlier thresholds.
TRUSTED_MIN_HZ = 20.0
TRUSTED_MAX_HZ = 20_000.0
DISPLAY_POINT_COUNT = 192
MIN_TRUSTED_POINTS = 24
EDGE_STABILITY_WINDOW_POINTS = 4
EDGE_STABILITY_MAX_DELTA_DB = 6.0
EDGE_STABILITY_MAX_SPAN_DB = 9.0
RESPONSE_OUTLIER_NEIGHBOR_RADIUS = 2
RESPONSE_OUTLIER_WARN_DB = 8.0
RESPONSE_OUTLIER_FAIL_DB = 12.0
RESPONSE_OUTLIER_MIN_HZ = 250.0

# Analyzer sweep-timing thresholds.
SWEEP_TIMING_ANCHOR_SECONDS = 0.35
SWEEP_TIMING_MULTI_ANCHOR_SECONDS = 0.18
SWEEP_TIMING_EDGE_INSET_SECONDS = 0.08
SWEEP_TIMING_SEARCH_SECONDS = 0.35
SWEEP_TIMING_MAX_ABS_PPM = 12_000.0
SWEEP_TIMING_MIN_COMPENSATION_PPM = 75.0
SWEEP_TIMING_MIN_ANCHOR_SCORE = 0.995
SWEEP_TIMING_CLUSTER_REJECT_SAMPLES = 24
SWEEP_TIMING_CENTRAL_ANCHORS = {"mid-low", "mid-high"}
SWEEP_TIMING_EDGE_ANCHORS = {"start-inner", "end-inner"}
SWEEP_TIMING_ANCHOR_LAYOUT = (
    ("start-inner", 0.06),
    ("start-body", 0.18),
    ("mid-low", 0.38),
    ("mid-high", 0.62),
    ("end-body", 0.82),
    ("end-inner", 0.94),
)

# Analyzer impulse-response windowing thresholds.
IR_WINDOW_PRE_SECONDS = 0.004
IR_WINDOW_POST_SECONDS = 0.35
IR_WINDOW_POST_LOW_SECONDS = 0.50
IR_WINDOW_POST_HIGH_SECONDS = 0.18
IR_WINDOW_FADE_SECONDS = 0.012
IR_WINDOW_VARIABLE_LOW_HZ = 250.0
IR_WINDOW_VARIABLE_HIGH_HZ = 1_200.0

# Analyzer impulse-response direct-arrival thresholds.
IR_DIRECT_SEARCH_PRE_SECONDS = 0.12
IR_DIRECT_RELATIVE_THRESHOLD = 0.05
IR_DIRECT_CANDIDATE_FLOOR_RELATIVE = 0.02
IR_DIRECT_CANDIDATE_LIMIT = 24
IR_DIRECT_WEAK_EARLY_RELATIVE = 0.12
IR_DIRECT_WEAK_EARLY_MIN_GAP_SAMPLES = 20
IR_DIRECT_WEAK_EARLY_NEXT_RATIO = 1.75
IR_DIRECT_SUPPORT_WINDOW_SECONDS = 0.00035
IR_DIRECT_NEARBY_WINDOW_SECONDS = 0.0009
IR_DIRECT_THRESHOLD_EDGE_SAMPLES = 12
IR_DIRECT_PROMOTION_WINDOW_SECONDS = 0.010
IR_DIRECT_PROMOTION_SUPPORT_RATIO = 1.4
IR_DIRECT_PROMOTION_SCORE_RATIO = 1.05
IR_DIRECT_PROMOTION_ENERGY_RATIO = 1.3
IR_DIRECT_PROMINENCE_REFERENCE = 0.15

# Analyzer impulse-response debug segment controls.
IR_DEBUG_SEGMENT_ENABLED = _env_flag("FXROUTE_MEASUREMENT_IR_DEBUG_SEGMENT", True)
IR_DEBUG_SEGMENT_RADIUS_SAMPLES = _env_int(
    "FXROUTE_MEASUREMENT_IR_DEBUG_SEGMENT_RADIUS_SAMPLES",
    250,
    minimum=64,
    maximum=5000,
)
