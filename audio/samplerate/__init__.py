# SPDX-License-Identifier: AGPL-3.0-only

"""Helpers for PipeWire samplerate status and conservative settings inventory.

Also owns the sink suspend/resume and force-rate reconciliation family moved
out of ``main.py``: pactl sink pulses, ``pw-metadata`` force-rate writes,
alignment polling, the silent-stream idle-sink renegotiation trigger and the
``reconcile_playback_samplerate`` orchestration shell.

The implementation is split into focused modules:

* ``constants``    - shared constants
* ``parsing``      - command execution and output parsing
* ``persistence``  - config paths, persistence, and normalization
* ``bluetooth``    - bluetooth overview and actions
* ``overview``     - status, overviews, and selection actions
* ``alignment``    - sink suspend/resume and force-rate reconciliation
* ``recovery``     - startup recovery for a degraded WirePlumber card probe

The full public surface stays importable from ``audio.samplerate``.
"""

import logging

logger = logging.getLogger(__name__)

from audio.samplerate.constants import (
    COMMAND_TIMEOUT_SECONDS,
    FXROUTE_MAX_PROCESSING_RATE,
    NON_SELECTABLE_INPUT_KEYS,
    NON_SELECTABLE_OUTPUT_KEYS,
    OUTPUT_MODE_STEREO,
    OUTPUT_MODE_SUBWOOFER_21,
    OUTPUT_MODE_SUBWOOFER_22,
    OUTPUT_MODE_SUBWOOFER_22_MODES,
    OUTPUT_MODE_SUBWOOFER_22_STEREO,
    OUTPUT_MODE_SUBWOOFER_MODES,
    OUTPUT_MODES,
    PIPEWIRE_ALLOWED_RATES,
    PIPEWIRE_DEFAULT_RATE_OPTIONS,
    SAMPLE_RATE_CANDIDATES,
    SOURCE_MODE_APP_PLAYBACK,
    SOURCE_MODE_BLUETOOTH_INPUT,
    SOURCE_MODE_EXTERNAL_INPUT,
    effective_supported_rates,
)

from audio.samplerate.parsing import (
    _bluetooth_device_id,
    _bluetooth_profile_from_node_name,
    _build_sink_output_label,
    _command_available,
    _extract_bluetooth_address,
    _humanize_sink_name,
    _humanize_source_name,
    _infer_bluetooth_codec,
    _is_bluetooth_sink_name,
    _is_bluetooth_source_name,
    _normalize_pipewire_default_rate,
    _parse_active_rate,
    _parse_bluetoothctl_devices,
    _parse_bluetoothctl_info,
    _parse_bluetoothctl_show,
    _parse_default_rate,
    _parse_default_sink,
    _parse_enum_format_supported_rates,
    _parse_fraction_rate,
    _parse_pactl_sinks_detailed,
    _parse_pactl_sinks_short,
    _parse_pactl_sources_detailed,
    _parse_pactl_sources_short,
    _parse_pipewire_clock_rate_dropin,
    _parse_pw_metadata_settings,
    _parse_pw_node_ids,
    _parse_sample_spec_channels,
    _parse_wpctl_inspect,
    _parse_wpctl_status_bluetooth_streams,
    _pipewire_bluez_plugin_available,
    _prefer_output_port_label,
    _run_command,
    _safe_int,
    _strip_quoted_value,
)

from audio.samplerate.persistence import (
    _audio_output_mode_path,
    _audio_output_selection_path,
    _audio_source_selection_path,
    _build_audio_output_mode_payload,
    _load_audio_output_mode,
    _load_audio_output_selection,
    _load_audio_source_selection,
    _load_device_output_modes,
    _load_pipewire_clock_rate_config,
    _load_raw_audio_output_mode,
    _normalize_single_sub_config,
    _normalize_subwoofer_22_config,
    _normalize_subwoofer_config,
    _pipewire_clock_rate_dropin_path,
    _sample_rate_policy_path,
    _save_audio_output_selection,
    _save_audio_source_selection,
    _subwoofer_22_storage_key,
    effective_playback_rate,
    load_sample_rate_policy,
    normalize_sample_rate_policy,
    persist_sample_rate_policy,
)

from audio.samplerate.bluetooth import (
    disconnect_connected_bluetooth_audio_sources,
    get_bluetooth_audio_overview,
    set_bluetooth_receiver_enabled,
)

from audio.samplerate.overview import (
    _build_selected_output_payload,
    _build_source_selection_key,
    _parse_default_source_name,
    _select_relevant_sink,
    _set_default_sink,
    _set_source_port,
    _split_source_selection_key,
    apply_persisted_audio_output_selection,
    audio_output_overview_with_effective_rate,
    authoritative_sample_rate,
    get_audio_output_overview,
    get_audio_source_overview,
    get_samplerate_status,
    measurement_helper_snapshot_summary,
    overview_sample_rate,
    persist_audio_output_mode,
    playback_rate_aligned,
    prepare_audio_output_mode,
    set_audio_output_mode,
    set_audio_output_selection,
    set_audio_source_selection,
)

from audio.samplerate.alignment import (
    RATE_RENEGOTIATION_TRIGGER_WAIT_MS,
    SAMPLERATE_ALIGNMENT_POLL_INTERVAL_MS,
    SAMPLERATE_ALIGNMENT_TIMEOUT_MS,
    SINK_SUSPEND_COOLDOWN_SECONDS,
    _last_sink_suspend_at,
    _last_sink_suspend_reason,
    clear_auto_policy_force_rate,
    ensure_playback_samplerate_force,
    ensure_rate_renegotiation_trigger_file,
    get_current_pipewire_force_rate,
    pulse_suspend_sink_for_samplerate,
    rate_renegotiation_trigger_path,
    reconcile_transition_sink_rate,
    set_pipewire_force_rate,
    suspend_resume_playback_sink,
    trigger_idle_sink_renegotiation,
    wait_for_samplerate_alignment,
)

from audio.samplerate.recovery import (
    RECOVERY_POLL_SECONDS,
    RECOVERY_WAIT_SECONDS,
    recover_saved_output_sink,
)

__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "FXROUTE_MAX_PROCESSING_RATE",
    "NON_SELECTABLE_INPUT_KEYS",
    "NON_SELECTABLE_OUTPUT_KEYS",
    "OUTPUT_MODE_STEREO",
    "OUTPUT_MODE_SUBWOOFER_21",
    "OUTPUT_MODE_SUBWOOFER_22",
    "OUTPUT_MODE_SUBWOOFER_22_MODES",
    "OUTPUT_MODE_SUBWOOFER_22_STEREO",
    "OUTPUT_MODE_SUBWOOFER_MODES",
    "OUTPUT_MODES",
    "PIPEWIRE_ALLOWED_RATES",
    "PIPEWIRE_DEFAULT_RATE_OPTIONS",
    "RATE_RENEGOTIATION_TRIGGER_WAIT_MS",
    "RECOVERY_POLL_SECONDS",
    "RECOVERY_WAIT_SECONDS",
    "SAMPLERATE_ALIGNMENT_POLL_INTERVAL_MS",
    "SAMPLERATE_ALIGNMENT_TIMEOUT_MS",
    "SAMPLE_RATE_CANDIDATES",
    "SINK_SUSPEND_COOLDOWN_SECONDS",
    "SOURCE_MODE_APP_PLAYBACK",
    "SOURCE_MODE_BLUETOOTH_INPUT",
    "SOURCE_MODE_EXTERNAL_INPUT",
    "apply_persisted_audio_output_selection",
    "audio_output_overview_with_effective_rate",
    "authoritative_sample_rate",
    "clear_auto_policy_force_rate",
    "disconnect_connected_bluetooth_audio_sources",
    "effective_playback_rate",
    "effective_supported_rates",
    "ensure_playback_samplerate_force",
    "ensure_rate_renegotiation_trigger_file",
    "get_audio_output_overview",
    "get_audio_source_overview",
    "get_bluetooth_audio_overview",
    "get_current_pipewire_force_rate",
    "get_samplerate_status",
    "load_sample_rate_policy",
    "measurement_helper_snapshot_summary",
    "normalize_sample_rate_policy",
    "overview_sample_rate",
    "persist_audio_output_mode",
    "persist_sample_rate_policy",
    "playback_rate_aligned",
    "prepare_audio_output_mode",
    "pulse_suspend_sink_for_samplerate",
    "rate_renegotiation_trigger_path",
    "recover_saved_output_sink",
    "reconcile_transition_sink_rate",
    "set_audio_output_mode",
    "set_audio_output_selection",
    "set_audio_source_selection",
    "set_bluetooth_receiver_enabled",
    "set_pipewire_force_rate",
    "suspend_resume_playback_sink",
    "trigger_idle_sink_renegotiation",
    "wait_for_samplerate_alignment",
]
