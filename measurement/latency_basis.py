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
    expected_input = sweep.get('input')
    ingress = sweep.get('ingress_restore')
    if restored and ingress is not None:
        # Injected sweeps bypass program ingress. Certify only its exact,
        # settled pre-suspension destination, not arbitrary input drift.
        retained_input = ingress.get('sweep_input', 0) if isinstance(ingress, dict) else None
        if (sweep.get('ingress_restored') is not True
                or not isinstance(ingress, dict) or type(ingress.get('input')) is not int
                or type(retained_input) is not int or not 0 <= retained_input <= ingress['input']
                or sweep.get('input') != retained_input
                or type(ingress.get('surround_input')) is not bool
                or not isinstance(ingress.get('mapped_ingress'), list)
                or not (ingress['surround_input'] or ingress['mapped_ingress'])
                or ingress['surround_input'] != runtime.get('surround_input')
                or ingress['mapped_ingress'] != runtime.get('mapped_ingress')):
            raise RuntimeError('Calibration ingress restoration is not verified')
        expected_input = ingress['input']
    if (not sweep_context.get('timing_key')
            or sweep_context.get('timing_key') != live_context.get('timing_key')
            or sweep_context.get('output_key') != live_context.get('output_key')
            or sweep.get('rate') != live.get('rate') or expected_input != live.get('input')):
        raise RuntimeError('Calibration timing changed after the successful sweep')
    if not restored and latency_signature(sweep) != latency_signature(live):
        raise RuntimeError('Calibration latency changed after the successful sweep')
    roles = sweep.get('measured_roles')
    if not isinstance(roles, list) or not roles:
        raise RuntimeError('Calibration latency evidence has no measured roles')
    evidence = copy.deepcopy(sweep)
    return capture_basis(state, evidence, origin='calibrated', roles=roles)
