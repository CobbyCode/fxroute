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


def require_speaker_align_reference(side: str, *, mic_input_channel: str | int | None,
                                    reference_input_channel: str | int | None,
                                    reference_input_channel_left: str | int | None,
                                    reference_input_channel_right: str | int | None) -> int:
    """Return the 1-based reference input a Speaker Align side records first.

    Speaker Align takes pass every configured field as a candidate, like the
    acquisition does. A reference configured for the run's side must not be
    the microphone channel: the store would drop it and record another
    loopback, or none. A side left unconfigured uses the configured loopbacks
    of the take, the same as a shared reference. With no non-microphone
    channel left the verification cannot run, so the start is refused.
    """
    mic = _channel_index(mic_input_channel, "mic_input_channel")
    mic = 0 if mic is None else mic
    fields = {
        "reference_input_channel": _channel_index(reference_input_channel, "reference_input_channel"),
        "reference_input_channel_left": _channel_index(
            reference_input_channel_left, "reference_input_channel_left"),
        "reference_input_channel_right": _channel_index(
            reference_input_channel_right, "reference_input_channel_right"),
    }
    resolved = resolve_reference_channels(
        mic=mic, shared=fields["reference_input_channel"],
        left=fields["reference_input_channel_left"],
        right=fields["reference_input_channel_right"], candidates=fields.values())
    collided = resolved.collided_right if side == "right" else resolved.collided_left
    index = resolved.right if side == "right" else resolved.left
    if collided or (index is None and mic in fields.values()):
        raise ValueError(
            f"Speaker Align electrical reference input {mic + 1} is the microphone input; "
            "choose a different reference channel")
    if index is None:
        raise ValueError(
            "Speaker Align requires an electrical reference input channel; "
            "its verification cannot run on the microphone alone")
    return index + 1
