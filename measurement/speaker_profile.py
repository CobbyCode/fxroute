# SPDX-License-Identifier: AGPL-3.0-only
"""Individual-way sweep profiles; shared timing takes retain the full sweep."""

import math

from measurement.alignment_backend import way_crossover_specs, way_passband
from measurement.constants import SWEEP_END_HZ, SWEEP_START_HZ, SWEEP_V2_SECONDS


def speaker_way_sweep_profile(processing: dict, *, sample_rate_hz: int) -> dict[str, float]:
    """Cover the entire level passband plus two octaves on each available edge.

    Keeping seconds per octave preserves excitation energy and resolution in
    the consumed band. The two-second floor leaves room for reference anchors;
    all capture padding and the shared planning/verification sweeps stay intact.
    Band-pass ways narrower than half an octave keep the full sweep: their
    shortened takes lose the electrical reference admission in dry probes.
    A way with an open edge (the lowest way has no high-pass, the highest no
    low-pass) is never narrow in that sense: its passband only ends at the
    measurement range there, so a 120 Hz low way of a 3-way speaker reads as
    50-71 Hz. It takes the band-derived sweep like a 2-way low or high way;
    the full sweep would spread the deconvolution over a band the way does not
    play and bury its low-frequency arrival in that noise.
    """
    low, high = way_passband(processing, sample_rate_hz=sample_rate_hz)
    kinds = {spec["kind"] for spec in way_crossover_specs(processing)}
    band_pass = {"highpass", "lowpass"} <= kinds
    if band_pass and math.log2(high / low) < 0.5:
        return {"sweep_start_hz": SWEEP_START_HZ, "sweep_end_hz": SWEEP_END_HZ,
                "sweep_seconds": SWEEP_V2_SECONDS}
    # A single octave changes low-way levels through finite-sweep/window edge
    # effects. Two octaves keep those edges outside the consumed passband.
    start = max(SWEEP_START_HZ, low / 4.0)
    end = min(SWEEP_END_HZ, sample_rate_hz * 0.499, high * 4.0)
    seconds = SWEEP_V2_SECONDS * math.log(end / start) / math.log(SWEEP_END_HZ / SWEEP_START_HZ)
    return {"sweep_start_hz": start, "sweep_end_hz": end,
            "sweep_seconds": max(2.0, seconds)}
