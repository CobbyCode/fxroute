# SPDX-License-Identifier: AGPL-3.0-only
"""Individual-way sweep profiles; shared timing takes retain the full sweep."""

import math

from measurement.alignment_backend import way_passband
from measurement.constants import SWEEP_END_HZ, SWEEP_START_HZ, SWEEP_V2_SECONDS


def speaker_way_sweep_profile(processing: dict, *, sample_rate_hz: int) -> dict[str, float]:
    """Cover the entire level passband plus two octaves on each available edge.

    Keeping seconds per octave preserves excitation energy and resolution in
    the consumed band. The two-second floor leaves room for reference anchors;
    all capture padding and the shared planning/verification sweeps stay intact.
    Ways narrower than half an octave keep the full sweep: their shortened
    takes lose the electrical reference admission in dry probes.
    """
    low, high = way_passband(processing, sample_rate_hz=sample_rate_hz)
    if math.log2(high / low) < 0.5:
        return {"sweep_start_hz": SWEEP_START_HZ, "sweep_end_hz": SWEEP_END_HZ,
                "sweep_seconds": SWEEP_V2_SECONDS}
    # A single octave changes low-way levels through finite-sweep/window edge
    # effects. Two octaves keep those edges outside the consumed passband.
    start = max(SWEEP_START_HZ, low / 4.0)
    end = min(SWEEP_END_HZ, sample_rate_hz * 0.499, high * 4.0)
    seconds = SWEEP_V2_SECONDS * math.log(end / start) / math.log(SWEEP_END_HZ / SWEEP_START_HZ)
    return {"sweep_start_hz": start, "sweep_end_hz": end,
            "sweep_seconds": max(2.0, seconds)}
