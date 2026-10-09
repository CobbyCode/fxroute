# SPDX-License-Identifier: AGPL-3.0-only
"""Post-activation adoption of a legacy electronic timing basis."""
from __future__ import annotations

from audio.output_service import MeasurementActiveError
from audio.output_state_store import StateConflictError
from dsp.latency import capture_basis, latency_signature


async def adopt_latency_basis(*, service, native, build_target, latency: dict,
                             measurement_active) -> bool:
    """Keep legacy alignment intact; never infer its historical DSP rendering."""
    if (measurement_active() or not latency.get('ready') or latency.get('balance_limited')
            or latency.get('balance_unavailable')):
        return False
    state = service.load()
    if state['modes'][state['active_mode']]['av_sync']['basis_origin'] != 'unavailable':
        return False
    runtime = native.snapshot()
    config = runtime.get('config') or {}
    if (runtime.get('active') is not True or config.get('output_mode') != state['active_mode']
            or not config.get('hardware_ports')):
        return False
    plan = service.compile_plan(state, output_key=config['output_key'],
                                channels=len(config['hardware_ports']), sample_rate_hz=config['sample_rate'])
    fingerprint = service.fingerprint_plan(plan)
    if (config.get('plan_fingerprint') != fingerprint
            or (latency.get('context') or {}).get('plan_fingerprint') != fingerprint
            or not await native.verify()):
        return False
    current = await native.settled_latency()
    if (measurement_active() or latency_signature(current) != latency_signature(latency)
            or (current.get('context') or {}).get('plan_fingerprint') != fingerprint):
        return False
    candidate = capture_basis(state, current, origin='adopted')
    settings = candidate['modes'][candidate['active_mode']]['av_sync']
    if not settings['basis'] and not settings['source_basis']:
        return False
    new_plan = service.compile_plan(candidate, output_key=config['output_key'],
                                    channels=len(config['hardware_ports']), sample_rate_hz=config['sample_rate'])
    new_fingerprint = service.fingerprint_plan(new_plan)
    previous = build_target(plan, fingerprint=fingerprint)
    target = build_target(new_plan, fingerprint=new_fingerprint)

    def require_start():
        if measurement_active():
            raise MeasurementActiveError('Measurement owns the AV Sync rendering')
        if service.load()['revision'] != state['revision']:
            raise StateConflictError('Output state changed before AV Sync basis adoption')

    async def verify_candidate():
        require_start()
        value = await native.settled_latency()
        if (not await native.verify() or latency_signature(value) != latency_signature(current)
                or (value.get('context') or {}).get('plan_fingerprint') != new_fingerprint):
            raise RuntimeError('AV Sync basis adoption changed the verified electronic timing')

    def commit():
        require_start()
        service.commit_unowned(candidate, expected_revision=state['revision'])

    require_start()
    gain = runtime['output_gain_db']
    await native.guarded_rebuild_rendered(
        target, previous=previous, guard_db=min(-18.0, gain),
        apply_candidate=commit, apply_previous=require_start,
        before_ramp=verify_candidate, ramp_target_db=gain, rollback_ramp_target_db=gain,
        settle_seconds=0.0)
    return True
