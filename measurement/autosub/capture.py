# SPDX-License-Identifier: AGPL-3.0-only

"""Run-constant AutoSub capture parameters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AutoSubCaptureContext:
    """Immutable run-constant capture plumbing for one AutoSub run.

    Bundles device selection, reference channels, calibration, sample rate,
    sweep profile and crossover so runners no longer thread them through
    every capture call. Candidate configuration, active subs and progress
    stay explicit at each call site.
    """

    input_id: str
    mic_input_channel: str
    reference_input_channel: str
    calibration_ref: str
    calibration_filename: str | None
    calibration_bytes: bytes | None
    auto_sub_rate: int
    auto_sub_sweep_profile: dict[str, Any]
    fc: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "auto_sub_sweep_profile", dict(self.auto_sub_sweep_profile))

    def base_kwargs(self) -> dict[str, Any]:
        """Run-constant kwargs for references and candidates."""
        return {**self.device_kwargs(), "fc": self.fc}

    def device_kwargs(self) -> dict[str, Any]:
        """Device, reference, calibration and rate bundle (no fc/profile)."""
        return {
            "input_id": self.input_id,
            "mic_input_channel": self.mic_input_channel,
            "reference_input_channel": self.reference_input_channel,
            "calibration_ref": self.calibration_ref,
            "calibration_filename": self.calibration_filename,
            "calibration_bytes": self.calibration_bytes,
            "auto_sub_rate": self.auto_sub_rate,
        }

    def sweep_kwargs(self) -> dict[str, Any]:
        """Run-constant kwargs for candidate sweeps (base plus profile)."""
        return {**self.base_kwargs(), "auto_sub_sweep_profile": self.auto_sub_sweep_profile}
