# SPDX-License-Identifier: AGPL-3.0-only
"""Trusted sweep-time latency evidence for calibration winner transactions."""
from __future__ import annotations

import copy

from dsp.latency import capture_basis, latency_signature


def calibrated_state(state: dict, runtime: dict, *, restored: bool, expected_bypass: bool) -> dict:
    """Enrich a validated candidate, never trust candidate-provided timing bases."""
    if not runtime.get('latency_tracking'):
        return copy.deepcopy(state)
    sweep = runtime.get('measurement_latency') or {}
    live = runtime.get('av_sync') or {}
    if (not sweep.get('ready') or sweep.get('verified') is not True or not live.get('ready')
            or bool(sweep.get('bypassed')) != expected_bypass):
        raise RuntimeError('Verified calibration latency evidence is unavailable')
    sweep_context, live_context = sweep.get('context') or {}, live.get('context') or {}
    if (not sweep_context.get('timing_key')
            or sweep_context.get('timing_key') != live_context.get('timing_key')
            or sweep_context.get('output_key') != live_context.get('output_key')
            or sweep.get('rate') != live.get('rate') or sweep.get('input') != live.get('input')):
        raise RuntimeError('Calibration timing changed after the successful sweep')
    if not restored and latency_signature(sweep) != latency_signature(live):
        raise RuntimeError('Calibration latency changed after the successful sweep')
    roles = sweep.get('measured_roles')
    if not isinstance(roles, list) or not roles:
        raise RuntimeError('Calibration latency evidence has no measured roles')
    evidence = copy.deepcopy(sweep)
    return capture_basis(state, evidence, origin='calibrated', roles=roles)
