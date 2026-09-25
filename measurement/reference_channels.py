# SPDX-License-Identifier: AGPL-3.0-only
"""Resolve the electrical reference input channels of one measurement take.

The store and the Speaker Align start check share this resolution, so a start
is judged on the channel indexes the take will actually record, not on the raw
request fields.

Rules (all indexes 0-based):

* Neither side field set: the shared field serves both sides.
* A side whose reference equals the microphone channel loses it.
* Every configured non-microphone channel is a candidate recorded in the same
  take; a side left without a reference takes the first candidate. The capture
  evidence later decides which candidate actually carries the sweep.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ReferenceChannels:
    left: int | None
    right: int | None
    # Distinct non-microphone candidates in request order.
    candidates: tuple[int, ...]
    # A side whose own reference was the microphone channel and was dropped.
    collided_left: bool
    collided_right: bool
    # Both sides named the same channel before the microphone check.
    shared: bool


def resolve_reference_channels(*, mic: int, shared: int | None, left: int | None,
                               right: int | None,
                               candidates: Iterable[int | None] = ()) -> ReferenceChannels:
    if left is None and right is None:
        left = right = shared
    same_channel = left is not None and left == right
    collided_left = left is not None and left == mic
    collided_right = right is not None and right == mic
    if collided_left:
        left = None
    if collided_right:
        right = None
    usable: list[int] = []
    for index in candidates:
        if index is None or index == mic or index in usable:
            continue
        usable.append(index)
    if usable:
        if left is None:
            left = usable[0]
        if right is None:
            right = usable[0]
    return ReferenceChannels(left, right, tuple(usable), collided_left, collided_right,
                             same_channel)


def reference_candidate_channels(*values: str | int | None) -> list[str]:
    """List the distinct configured loopback channels a Speaker Align take records.

    Every candidate is recorded in the same capture and the store's
    electrical-reference evaluation decides which channel actually carries the
    way.  Nothing here maps a side, a way or an output role to a specific
    input channel. The Speaker Align takes and their start check both use it.
    """
    candidates: list[str] = []
    for value in values:
        token = str(value).strip() if value is not None else ""
        if token and token not in candidates:
            candidates.append(token)
    return candidates


def _channel_index(value: str | int | None, field_name: str) -> int | None:
    raw = str(value if value is not None else "").strip()
    if not raw:
        return None
    try:
        number = int(raw)
    except ValueError:
        raise ValueError(f"{field_name} must be an input channel number") from None
    if number < 1:
        raise ValueError(f"{field_name} must be an input channel number")
    return number - 1


# UI names of the reference fields, so an error points at the setting to fix.
_FIELD_LABELS = {
    "shared": "Electrical reference input",
    "left": "Electrical Ref L",
    "right": "Electrical Ref R",
}


def require_speaker_align_reference(side: str, *, mic_input_channel: str | int | None,
                                    reference_input_channel: str | int | None,
                                    reference_input_channel_left: str | int | None,
                                    reference_input_channel_right: str | int | None) -> int:
    """Return the 1-based reference input the store resolves for a Speaker Align side.

    The decision is the store's own: the same candidate list the takes pass
    (``reference_candidate_channels``) and the same ``resolve_reference_channels``
    the store runs per take. Only the input's channel count is not known here;
    the store still range-checks it.

    Refused before any sweep: a reference configured for the run's side (or
    the shared one when no side is configured) that is the microphone channel
    -- the store would drop it and record another loopback or none -- and a
    side the store resolves to no reference at all. A side left unconfigured
    uses the take's configured loopbacks, exactly as the store records them.
    """
    mic = _channel_index(mic_input_channel, "mic_input_channel")
    mic = 0 if mic is None else mic
    raw = {"shared": reference_input_channel, "left": reference_input_channel_left,
           "right": reference_input_channel_right}
    fields = {
        "shared": _channel_index(raw["shared"], "reference_input_channel"),
        "left": _channel_index(raw["left"], "reference_input_channel_left"),
        "right": _channel_index(raw["right"], "reference_input_channel_right"),
    }
    candidates = [_channel_index(token, "reference_candidate_channels")
                  for token in reference_candidate_channels(
                      raw["shared"], raw["left"], raw["right"])]
    resolved = resolve_reference_channels(
        mic=mic, shared=fields["shared"], left=fields["left"], right=fields["right"],
        candidates=candidates)
    side_key = "right" if side == "right" else "left"
    index = resolved.right if side_key == "right" else resolved.left
    collided = resolved.collided_right if side_key == "right" else resolved.collided_left
    on_mic = [key for key, value in fields.items() if value == mic]
    if collided or (index is None and on_mic):
        # The field that fed this side: its own, else the shared fallback.
        key = side_key if fields[side_key] == mic else on_mic[0]
        raise ValueError(
            f"Speaker Align {side_key}: {_FIELD_LABELS[key]} is input {mic + 1}, the "
            "microphone input; choose a different reference channel")
    if index is None:
        raise ValueError(
            f"Speaker Align {side_key} has no electrical reference input channel; "
            "its verification cannot run on the microphone alone")
    return index + 1
