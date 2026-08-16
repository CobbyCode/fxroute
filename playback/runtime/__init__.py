# SPDX-License-Identifier: AGPL-3.0-only

"""FXRoute-specific implementation of the playback TransitionRuntime contract.

This package owns the concrete runtime adapter for the generic
``PlaybackTransitionCoordinator`` from ``playback/transition``.  It is
deliberately decoupled from ``main.py``: every application-shell dependency
(player, DSP manager, queue/track state, shared helpers) arrives
through the explicit ``PlaybackRuntimeDependencies`` wiring, resolved
late-bound so production wiring and test mocks observe the same attributes.

Module boundary: must never import ``main`` (enforced by
``scripts/check_router_structure.py``).

The implementation is split into focused modules:

* ``deps``          - late-bound dependency contract
* ``helpers``       - sink/mute primitives and constants
* ``mute``          - hardware and DSP-sink mute operations
* ``snapshot``      - snapshot, abort, and committed-source restore
* ``source``        - source handoff operations
* ``verification``  - commit and readback verification
* ``output_mode``   - output-mode lifecycle operations
* ``adapter``       - the runtime adapter composing the mixins above
"""

from playback.runtime.adapter import FxrouteTransitionRuntime
from playback.runtime.deps import PlaybackRuntimeDependencies
from playback.runtime.helpers import (
    RADIO_EXPECTED_SAMPLE_RATE_HZ,
    SOURCE_HANDOFF_SETTLE_MS,
    _hardware_sink_for_transition,
    _playback_gate_state_path,
    _read_hardware_sink_mute,
    _read_sink_mute,
    _set_hardware_sink_mute,
    _set_sink_mute,
)

__all__ = [
    "FxrouteTransitionRuntime",
    "PlaybackRuntimeDependencies",
]
