# SPDX-License-Identifier: AGPL-3.0-only

"""Shared constants of the PipeWire samplerate and audio-selection domain."""

from collections.abc import Iterable


NON_SELECTABLE_OUTPUT_KEYS = {"fxroute_dsp_sink"}
NON_SELECTABLE_INPUT_KEYS: set[str] = set()
SOURCE_MODE_APP_PLAYBACK = "app-playback"
SOURCE_MODE_EXTERNAL_INPUT = "external-input"
SOURCE_MODE_BLUETOOTH_INPUT = "bluetooth-input"
OUTPUT_MODE_STEREO = "stereo"
OUTPUT_MODE_SUBWOOFER_21 = "subwoofer-2.1"
OUTPUT_MODE_SUBWOOFER_22 = "subwoofer-2.2"
OUTPUT_MODE_SUBWOOFER_22_STEREO = "subwoofer-2.2-stereo"
OUTPUT_MODE_SUBWOOFER_22_MODES = {OUTPUT_MODE_SUBWOOFER_22, OUTPUT_MODE_SUBWOOFER_22_STEREO}
OUTPUT_MODE_SUBWOOFER_MODES = {OUTPUT_MODE_SUBWOOFER_21, *OUTPUT_MODE_SUBWOOFER_22_MODES}
OUTPUT_MODES = {OUTPUT_MODE_STEREO, *OUTPUT_MODE_SUBWOOFER_MODES}
SAMPLE_RATE_CANDIDATES = [
    44100, 48000, 88200, 96000, 176400, 192000,
    352800, 384000, 705600, 768000,
]
PIPEWIRE_DEFAULT_RATE_OPTIONS = SAMPLE_RATE_CANDIDATES
PIPEWIRE_ALLOWED_RATES = SAMPLE_RATE_CANDIDATES

# Maximum sample rate the FXRoute DSP chain can process.  This is an FXRoute
# processing limit (the LV2 plugins, including the LSP limiter, refuse rates
# above 384 kHz), not a hardware or PipeWire limit: a device reporting a
# higher native rate keeps that capability in ``native_supported_rates`` but
# FXRoute never advertises or switches above this cap.
FXROUTE_MAX_PROCESSING_RATE = 384000


def effective_supported_rates(rates: Iterable[int]) -> list[int]:
    """Cap a device rate list at the FXRoute DSP processing maximum.

    The native device capability stays available to callers that need it
    (see ``native_supported_rates``); the returned list is what FXRoute can
    actually process and therefore what the API and UI may offer.
    """
    return [
        rate
        for rate in rates
        if isinstance(rate, int) and 0 < rate <= FXROUTE_MAX_PROCESSING_RATE
    ]


# Conservative bound for every pactl/wpctl/pw-cli/pw-metadata/bluetoothctl
# invocation: a wedged PipeWire/BlueZ daemon must never block a caller
# indefinitely.  Commands that legitimately need more time can pass an
# explicit per-call timeout where the need is proven.
COMMAND_TIMEOUT_SECONDS = 5.0


