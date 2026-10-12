# SPDX-License-Identifier: AGPL-3.0-only
"""AV Sync state contracts and translation of verified native latency evidence."""
from __future__ import annotations

import copy
import math

from audio.output_topology import SURROUND_POSITIONS

AV_SYNC_MAX_MS = 20000


def default_av_sync() -> dict:
    return {'target_ms': 0.0, 'basis': {}, 'source_basis': {},
            'basis_origin': 'unavailable', 'basis_context': {}}


def finite_ms(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= AV_SYNC_MAX_MS


def validate_av_sync(value: object, roles, *, surround: bool) -> None:
    if not isinstance(value, dict) or set(value) != set(default_av_sync()):
        raise ValueError('AV Sync requires target, output/source bases, origin and context')
    if not finite_ms(value['target_ms']):
        raise ValueError('AV Sync target must be finite and between 0 and 20000 ms')
    for key, allowed in (('basis', set(roles)), ('source_basis', set(SURROUND_POSITIONS if surround else ('FL', 'FR')))):
        entries = value[key]
        if not isinstance(entries, dict) or set(entries) - allowed or any(not finite_ms(ms) for ms in entries.values()):
            raise ValueError(f'AV Sync {key} must contain valid roles and finite milliseconds')
    if value['basis_origin'] not in ('unavailable', 'adopted', 'calibrated') or not isinstance(value['basis_context'], dict):
        raise ValueError('Invalid AV Sync basis origin or context')


def capture_basis(state: dict, snapshot: dict, *, origin: str, roles=None) -> dict:
    """Capture effective timing in one common epoch, excluding align/common delay."""
    if (not snapshot.get('ready') or snapshot.get('balance_limited')
            or snapshot.get('balance_unavailable')):
        raise ValueError('AV Sync calibration requires a ready, uncompromised latency readback')
    rate = snapshot.get('rate')
    if type(rate) is not int or rate <= 0:
        raise ValueError('AV Sync latency rate is unavailable')
    result = copy.deepcopy(state)
    config = result['modes'][result['active_mode']]['av_sync']
    normalization = {}
    measured_positions = snapshot.get('measured_positions')
    has_positions = isinstance(measured_positions, list)
    for section, name, key in (('outputs', 'basis', 'role'), ('sources', 'source_basis', 'position')):
        stored = config[name]
        rows = [row for row in snapshot.get(section, []) if row.get(key) is not None
                and row.get('active', True)]
        effective = {row[key]: (snapshot['input'] + row['dsp'] + row['balance']) * 1000.0 / rate
                     for row in rows if not row.get('unknown')}
        # A partial capture must rebase retained and dormant entries by the
        # same common offset; mixing epochs would add the old balance twice.
        epoch = max((value - stored[role] for role, value in effective.items() if role in stored), default=0)
        offset = max(0.0, -min((value + epoch for value in stored.values()), default=0.0))
        normalization[section] = offset
        for role in stored:
            stored[role] += epoch + offset
        for row in rows:
            if section == 'outputs':
                measured = roles is None or row[key] in roles
            else:
                measured = roles is None or bool(set(row.get('roles', [])) & set(roles))
                if has_positions:
                    measured = measured and row.get('position') in measured_positions
            if not measured:
                continue
            if row.get('unknown'):
                stored.pop(row[key], None)
            else:
                stored[row[key]] = effective[row[key]] + offset
    config['basis_origin'] = origin
    config['basis_context'] = {'sample_rate_hz': rate, 'normalization_ms': normalization,
                               **snapshot.get('context', {})}
    return result


def render_av_sync(layout: list[dict], settings: dict) -> list[str]:
    """Translate role-based persistence to the current native output ordering."""
    reference = next((i for i, row in enumerate(layout)
                      if row['name'] in ('main_l', 'left_low', 'FL')), 0)
    target = settings.get('target_ms', 0)
    if not finite_ms(target):
        raise ValueError('Invalid AV Sync target')
    lines = [f'avsync {target:.9g} {reference}']
    for index, row in enumerate(layout):
        basis = settings.get('basis', {}).get(row['name'])
        if basis is not None:
            if not finite_ms(basis):
                raise ValueError('Invalid AV Sync output basis')
            lines.append(f'latency_basis {index} {basis:.9g}')
    positions = SURROUND_POSITIONS if any('input_positions' in row for row in layout) else ('FL', 'FR')
    for index, position in enumerate(positions):
        basis = settings.get('source_basis', {}).get(position)
        if basis is not None:
            if not finite_ms(basis):
                raise ValueError('Invalid AV Sync source basis')
            lines.append(f'source_basis {index} {basis:.9g}')
    return lines


def validate_ir_latency(value: object, channels: int) -> dict:
    if not isinstance(value, dict) or set(value) != {'version', 'algorithmic_frames', 'intentional_frames'} or value['version'] != 1:
        raise ValueError('Invalid IR latency metadata')
    for key in ('algorithmic_frames', 'intentional_frames'):
        entries = value[key]
        if not isinstance(entries, list) or len(entries) != channels or any(
                type(frame) not in (int, float) or not math.isfinite(frame) or not 0 <= frame <= 15360000 for frame in entries):
            raise ValueError('IR latency requires finite non-negative frames per channel')
    return copy.deepcopy(value)


def ir_latency(params: dict) -> dict:
    value = params.get('latency')
    if value is None:
        return {'algorithmic_ms': [0.0] * params['channels'],
                'intentional_ms': [0.0] * params['channels'], 'known': False}
    value = validate_ir_latency(value, params['channels'])
    return {'algorithmic_ms': [frame * 1000.0 / params['rate'] for frame in value['algorithmic_frames']],
            'intentional_ms': [frame * 1000.0 / params['rate'] for frame in value['intentional_frames']], 'known': True}


def _rendered_dsp(row: dict):
    """DSP frames the rendering fixes, without the share that follows the programme."""
    dsp, adaptive = row.get('dsp'), row.get('adaptive', 0)
    return dsp - adaptive if type(dsp) is int and type(adaptive) is int else dsp


def latency_signature(snapshot: dict) -> tuple:
    """Audible timing identity; excludes measurement-owned align/gain/mute changes.

    The upmixer's delay comes and goes with the programme on every path alike,
    so it is no change of the rendering.
    """
    return (snapshot.get('rate'), snapshot.get('input'), snapshot.get('bypassed'),
            tuple((_rendered_dsp(row), row.get('intentional'), row.get('balance'), row.get('unknown'), row.get('mixed'))
                  for row in snapshot.get('outputs', [])),
            tuple((_rendered_dsp(row), row.get('intentional'), row.get('balance'), row.get('unknown'))
                  for row in snapshot.get('sources', [])),
            snapshot.get('balance_limited'), snapshot.get('balance_unavailable'))
