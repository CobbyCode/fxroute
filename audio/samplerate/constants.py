# SPDX-License-Identifier: AGPL-3.0-only

"""Shared constants of the PipeWire samplerate and audio-selection domain."""


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
SAMPLE_RATE_CANDIDATES = [
    44100, 48000, 88200, 96000, 176400, 192000,
    352800, 384000, 705600, 768000,
]
PIPEWIRE_DEFAULT_RATE_OPTIONS = SAMPLE_RATE_CANDIDATES
PIPEWIRE_ALLOWED_RATES = SAMPLE_RATE_CANDIDATES


# Conservative bound for every pactl/wpctl/pw-cli/pw-metadata/bluetoothctl
# invocation: a wedged PipeWire/BlueZ daemon must never block a caller
# indefinitely.  Commands that legitimately need more time can pass an
# explicit per-call timeout where the need is proven.
COMMAND_TIMEOUT_SECONDS = 5.0


