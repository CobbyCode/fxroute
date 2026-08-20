# SPDX-License-Identifier: AGPL-3.0-only

"""Late-bound application-shell dependency contract for the runtime adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from playback.queue import PlaybackQueue


@dataclass(frozen=True)
class PlaybackRuntimeDependencies:
    """Late-bound accessors into the FXRoute application shell (main.py).

    Every field resolves the current shell state at call time, mirroring the
    existing ``configure_*`` dependency pattern: production wiring and test
    mocks observe the same module attributes.  No service locator, no global
    registry; the runtime constructor takes exactly this typed structure.
    """

    # Runtime services
    player: Callable[[], Any]
    dsp_manager: Callable[[], Any]
    dsp_runtime: Callable[[], Any]

    # Playback-context state owned by the application shell
    get_current_track_info: Callable[[], dict | None]
    get_playback_intent_generation: Callable[[], int]
    get_transition_epoch: Callable[[], int]
    set_track_and_owner: Callable[[dict | None, str | None], Awaitable[None]]
    clear_track_and_owner: Callable[[], Awaitable[None]]
    queue: Callable[[], PlaybackQueue]

    # Player / transport primitives (main.py)
    player_is_running: Callable[..., bool]
    load_player_paused: Callable[..., None]
    wait_for_player_current_file: Callable[..., Awaitable[bool]]
    wait_for_player_audio_samplerate: Callable[..., Awaitable[Any]]
    get_player_audio_samplerate: Callable[[], int | None]
    wait_for_radio_live_rate_after_load: Callable[..., Awaitable[Any]]
    wait_for_pipewire_mpv_release: Callable[..., Awaitable[bool]]
    wait_for_pipewire_spotify_release: Callable[..., Awaitable[bool]]
    wait_for_spotify_sink_input_samplerate: Callable[..., Awaitable[Any]]

    # Samplerate / coordinator helpers (main.py)
    get_samplerate_status: Callable[..., dict]
    get_audio_output_overview: Callable[..., dict]
    ensure_playback_samplerate_force: Callable[..., Awaitable[bool]]
    persist_audio_output_mode: Callable[..., dict]
    trigger_idle_sink_renegotiation: Callable[..., Awaitable[bool]]
    reconcile_transition_sink_rate: Callable[..., Awaitable[bool]]
    coordinator_source_rate: Callable[..., int | None]
    coordinator_target_rate: Callable[..., int | None]

    # Spotify / playback-state helpers (main.py)
    spotify_pause: Callable[..., Awaitable[Any]]
    get_spotify_ui_state: Callable[..., Awaitable[dict]]
    is_spotify_playback_active: Callable[..., bool]
    has_local_footer_context: Callable[..., bool]
    pause_spotify_for_local_playback_broadcast: Callable[[], Awaitable[None]]
    pause_local_playback_for_spotify_broadcast: Callable[[], Awaitable[None]]

    # Qobuz / qbzd external-renderer helpers (main.py)
    get_qobuz_ui_state: Callable[..., Awaitable[dict]]
    is_qobuz_playback_active: Callable[..., bool]
    qobuz_play: Callable[..., Awaitable[Any]]
    qobuz_pause: Callable[..., Awaitable[Any]]
    wait_for_pipewire_qobuz_release: Callable[..., Awaitable[bool]]
    wait_for_qobuz_sink_input_samplerate: Callable[..., Awaitable[Any]]
    mark_player_state_authoritative: Callable[..., None]
    spotify_snapshot_identity_values: Callable[..., set]
    measurement_restore_intent_matches_live_state: Callable[..., Awaitable[bool]]
    measurement_audio_graph_owned: Callable[[], bool]

    # DSP / worker helpers (main.py)
    dsp_mutation_lock: Callable[[], Any]
    drain_worker: Callable[..., Awaitable[Any]]
    load_dsp_preset: Callable[..., Awaitable[None]]
    sync_dsp_runtime: Callable[..., Awaitable[dict]]
    helper_argument_sample_rate: Callable[..., int | None]

    # Graph / effects-helper primitives (main.py)
    playback_graph_diagnosis: Callable[..., Awaitable[dict]]
    measurement_session_link_loss_is_repairable: Callable[..., bool]
    coordinator_reconcile_subwoofer_links_only: Callable[[], Awaitable[None]]
    repair_stereo_output_links_once: Callable[..., Awaitable[None]]
    coordinator_establish_effects_and_helper: Callable[..., Awaitable[dict]]
    ensure_mpv_to_dsp_links: Callable[[], Awaitable[bool]]
    playback_graph_links_complete: Callable[..., Awaitable[bool]]
    log_playback_graph_diagnosis: Callable[..., None]
    coordinator_reconcile_post_start_graph: Callable[..., Awaitable[dict]]
