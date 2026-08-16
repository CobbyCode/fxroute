# SPDX-License-Identifier: AGPL-3.0-only

"""The concrete runtime adapter composing the responsibility mixins."""

from __future__ import annotations

from typing import Any

from playback.transition import TransitionRuntime

from .deps import PlaybackRuntimeDependencies
from .mute import _RuntimeMuteMixin
from .output_mode import _RuntimeOutputModeMixin
from .snapshot import _RuntimeSnapshotMixin
from .source import _RuntimeSourceMixin
from .verification import _RuntimeVerificationMixin


class FxrouteTransitionRuntime(
    _RuntimeMuteMixin,
    _RuntimeSnapshotMixin,
    _RuntimeSourceMixin,
    _RuntimeVerificationMixin,
    _RuntimeOutputModeMixin,
    TransitionRuntime,
):
    """Concrete runtime adapter; graph mutations only enter via the coordinator."""


    def __init__(self, deps: PlaybackRuntimeDependencies) -> None:
        self._deps = deps
        self._output_key: str | None = None
        self._staged_target_url: str | None = None

    @property
    def _player(self) -> Any:
        """MPV wrapper resolved late-bound through the app-shell wiring."""
        return self._deps.player()

    @property
    def _dsp_manager(self) -> Any:
        """DSP manager resolved late-bound through the app-shell wiring."""
        return self._deps.dsp_manager()

    @property
    def _dsp_runtime(self) -> Any:
        """Subwoofer helper runtime resolved late-bound through the wiring."""
        return self._deps.dsp_runtime()

