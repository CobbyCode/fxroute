# SPDX-License-Identifier: AGPL-3.0-only

"""Single-owner playback transition coordination.

This package deliberately contains no FXRoute imports.  The application
supplies the runtime adapter, while the coordinator owns transition
serialization, the hardware-output gate contract, commit ordering, and
failure latching.  Keeping the state machine independent makes the safety
rules testable without MPV, PipeWire, the FXRoute DSP engine, or a live
hardware sink.

The public import surface stays ``from playback.transition import ...``;
the implementation is split into focused modules:

* ``models``       - core types and constants
* ``readbacks``    - stable graph readback helper
* ``protocol``     - the application-owned runtime contract
* ``stages``       - per-transition stage tracking
* ``gate``         - output-gate ownership and startup reconciliation
* ``cleanup``      - uncommitted-transition cleanup contract
* ``coordinator``  - the transition coordinator itself
"""

from playback.transition.cleanup import _TransitionCleanupMixin
from playback.transition.coordinator import PlaybackTransitionCoordinator
from playback.transition.gate import _OutputGateMixin
from playback.transition.models import (
    DSP_TRANSPORT_SINK,
    OutputGateState,
    PlaybackTransitionFailure,
    RecoveryExecutor,
    RecoveryValidator,
    TransitionRequest,
    TransitionResult,
)
from playback.transition.protocol import TransitionRuntime
from playback.transition.readbacks import stable_graph_readbacks
from playback.transition.stages import _TransitionStages

__all__ = [
    "OutputGateState",
    "PlaybackTransitionCoordinator",
    "PlaybackTransitionFailure",
    "TransitionRequest",
    "TransitionResult",
]
