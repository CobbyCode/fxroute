# SPDX-License-Identifier: AGPL-3.0-only
"""Silent-active playback diagnosis watcher.

Owns the in-flight watch tasks and the diagnosed-signature dedupe set,
together with the settle/check/snapshot behavior moved out of ``main.py``.
No imports from ``main``: live services are injected by the composition
root; PipeWire/samplerate helpers come from the audio and dsp packages.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from audio import pw_link
from audio import sink_inputs
from audio.samplerate import OUTPUT_MODE_STEREO, get_audio_output_overview
from audio.system_volume import get_output_volume
from dsp.runtime import _contains_link
from playback.state import is_local_playback_active, is_spotify_playback_active

logger = logging.getLogger(__name__)

SILENT_ACTIVE_SETTLE_SECONDS = 8.0
SILENT_ACTIVE_FLOOR_DB = -58.0


@dataclass
class SilentActiveDependencies:
    """Live services the silent-active watcher needs."""

    get_peak_monitor: Callable[[], Any]
    get_player_instance: Callable[[], Any]
    get_current_track_info: Callable[[], dict[str, Any] | None]
    get_current_footer_owner: Callable[[], str]
    get_spotify_ui_state: Callable[..., Any]
    list_mpv_sink_inputs: Callable[[], list[dict]]
    list_spotify_sink_inputs: Callable[[], list[dict]]
    list_all_sink_inputs: Callable[[], list[dict]]
    get_output_volume_safe: Callable[[int], int]
    run_debug_command: Callable[[list[str], float], dict]
    is_measurement_window_open: Callable[[], bool]
    dsp_preset_load_locked: Callable[[], bool]
    current_track_matches: Callable[[dict[str, Any] | None], bool]


class SilentActiveRecovery:
    """Single owner of silent-active watch/recovery bookkeeping and behavior."""

    def __init__(self, deps: SilentActiveDependencies) -> None:
        self._deps = deps
        self.watch_tasks: dict[str, asyncio.Task] = {}
        self.recovery_attempts: set[str] = set()

    def _source_links_present(self, source: str, links_text: str, output_mode: dict) -> bool:
        if source == "spotify":
            source_link_ok = _contains_link(links_text, "spotify:output_FL", "fxroute_dsp_sink:playback_FL")
        else:
            source_link_ok = _contains_link(links_text, "mpv:output_FL", "fxroute_dsp_sink:playback_FL")
        if not source_link_ok:
            return False

        mode = output_mode.get("mode") or OUTPUT_MODE_STEREO
        if mode != OUTPUT_MODE_STEREO:
            return True
        output_key = str(output_mode.get("effective_output_key") or "").strip()
        if not output_key:
            return True
        # Native stereo topology: the source reaches the DSP ingress sink and
        # the DSP output reaches the selected hardware output.
        return (
            _contains_link(links_text, "fxroute_dsp:output_1", f"{output_key}:playback_FL")
            and _contains_link(links_text, "fxroute_dsp:output_2", f"{output_key}:playback_FR")
        )

    def _snapshot(
        self,
        *,
        source: str,
        owner: str,
        track: dict | None,
        player_state: dict,
        spotify_state: dict,
        source_inputs: list[dict],
        all_inputs: list[dict],
        links_text: str,
        overview: dict,
        peak_snapshot: dict,
    ) -> dict:
        output_mode = overview.get("output_mode") or {}
        return {
            "source": source,
            "owner": owner,
            "track": {
                "id": (track or {}).get("id"),
                "title": (track or {}).get("title"),
                "url": (track or {}).get("url"),
            },
            "playback": {
                "playing": player_state.get("playing"),
                "paused": player_state.get("paused"),
                "current_file": player_state.get("current_file"),
                "source_volume": player_state.get("volume"),
                "output_volume": self._deps.get_output_volume_safe(100),
            },
            "spotify": {
                "status": spotify_state.get("status"),
                "title": spotify_state.get("title"),
                "source_volume": spotify_state.get("source_volume"),
                "output_volume": spotify_state.get("volume"),
            } if spotify_state else {},
            "output_mode": {
                "mode": output_mode.get("mode"),
                "effective_output_key": output_mode.get("effective_output_key"),
                "effective_output_rate": output_mode.get("effective_output_rate"),
                "runtime": (output_mode.get("runtime") or {}) if isinstance(output_mode.get("runtime"), dict) else {},
            },
            "source_inputs": sink_inputs.brief_sink_inputs(source_inputs),
            "all_sink_inputs": sink_inputs.brief_sink_inputs(all_inputs),
            "source_link_present": self._source_links_present(source, links_text, output_mode),
            "links_excerpt": "\n".join(
                line for line in links_text.splitlines()
                if any(
                    token in line
                    for token in (
                        "mpv",
                        "spotify",
                        "fxroute_dsp_sink",
                        "fxroute_dsp",
                        str(output_mode.get("effective_output_key") or "").strip(),
                    )
                    if token
                )
            )[:4000],
            "levels": {
                "output_peak": peak_snapshot,
                "pre_level": None,
                "post_level": peak_snapshot.get("vu_db"),
            },
        }

    def schedule(
        self,
        *,
        source: str,
        signature: str,
        track: dict | None = None,
        spotify_state: dict | None = None,
    ) -> None:
        if not signature:
            return
        existing = self.watch_tasks.get(signature)
        if existing and not existing.done():
            return
        task = asyncio.create_task(
            self._watch_after_settle(source=source, signature=signature, track=track, spotify_state=spotify_state),
            name=f"silent-active-watch:{source}",
        )
        self.watch_tasks[signature] = task

    async def _watch_after_settle(
        self,
        *,
        source: str,
        signature: str,
        track: dict | None = None,
        spotify_state: dict | None = None,
    ) -> None:
        try:
            await asyncio.sleep(SILENT_ACTIVE_SETTLE_SECONDS)
            await self._check_and_recover(source=source, signature=signature, track=track, spotify_state=spotify_state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Silent-active watch failed: source=%s signature=%s error=%s", source, signature, exc)
        finally:
            task = self.watch_tasks.get(signature)
            if task is asyncio.current_task():
                self.watch_tasks.pop(signature, None)

    async def _check_and_recover(
        self,
        *,
        source: str,
        signature: str,
        track: dict | None = None,
        spotify_state: dict | None = None,
    ) -> None:
        deps = self._deps
        if signature in self.recovery_attempts:
            return
        if not deps.get_peak_monitor():
            return

        player_instance = deps.get_player_instance()
        player_state = player_instance.state if player_instance and player_instance._running else {}
        live_track = deps.get_current_track_info() or {}
        owner = deps.get_current_footer_owner() or source
        if source in {"local", "radio"}:
            if not track or not deps.current_track_matches(track):
                return
            if not is_local_playback_active(player_state):
                return
            source_inputs = deps.list_mpv_sink_inputs()
            source_volume = player_state.get("volume")
        elif source == "spotify":
            spotify_state = await deps.get_spotify_ui_state()
            if not is_spotify_playback_active(spotify_state):
                return
            source_inputs = deps.list_spotify_sink_inputs()
            source_volume = spotify_state.get("source_volume")
            live_track = {
                "id": spotify_state.get("trackId"),
                "title": spotify_state.get("title"),
                "artist": spotify_state.get("artist"),
                "source": "spotify",
            }
        else:
            return

        if not sink_inputs.active_unmuted_sink_inputs(source_inputs):
            return
        try:
            if int(round(float(source_volume if source_volume is not None else 100))) <= 0:
                return
        except (TypeError, ValueError):
            pass
        try:
            live_volume = await asyncio.to_thread(get_output_volume)
        except Exception as exc:
            logger.warning(
                "Failed to read live output volume for silent-active check: %s", exc
            )
            live_volume = 100
        if live_volume <= 0:
            return

        overview = await asyncio.to_thread(get_audio_output_overview)
        output_mode = overview.get("output_mode") or {}
        links_result = await asyncio.to_thread(deps.run_debug_command, ["pw-link", "-l"], 2.0)
        links_text = links_result.get("stdout") or ""
        if not self._source_links_present(source, links_text, output_mode):
            return

        peak_snapshot = deps.get_peak_monitor().snapshot()
        vu_db = peak_snapshot.get("vu_db")
        if not isinstance(vu_db, (int, float)) or vu_db > SILENT_ACTIVE_FLOOR_DB:
            return

        # Skip when no current sample is available: vu_db then only reflects
        # the technical -60 dB floor, not real silence. Freshness (vu_fresh)
        # is the sample-validity signal of the peak monitor snapshot; the
        # peak-hold "detected" flag is unrelated to sample validity and must
        # not gate the diagnosis.
        if not peak_snapshot.get("vu_fresh"):
            logger.info(
                "SILENT-ACTIVE-DIAG skip: peak_samples_stale vu_db=%s source=%s signature=%s",
                vu_db, source, signature,
            )
            return

        # Skip during measurement window or while a DSP preset is actively
        # loading. The audio path is in transition; not a real silent-active
        # condition.
        if deps.is_measurement_window_open() or deps.dsp_preset_load_locked():
            logger.info(
                "SILENT-ACTIVE-DIAG skip: transition_window measurement_open=%s dsp_preset_loading=%s source=%s signature=%s",
                deps.is_measurement_window_open(),
                deps.dsp_preset_load_locked(),
                source, signature,
            )
            return

        all_inputs = deps.list_all_sink_inputs()
        snapshot = self._snapshot(
            source=source,
            owner=owner,
            track=live_track,
            player_state=player_state,
            spotify_state=spotify_state or {},
            source_inputs=source_inputs,
            all_inputs=all_inputs,
            links_text=links_text,
            overview=overview,
            peak_snapshot=peak_snapshot,
        )
        logger.warning(
            "Silent-active playback detected (log-only, recovery disabled): %s",
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
        )
        # Automatic loadfile() / spotify-handoff recovery is disabled. It was
        # breaking normal library starts by reloading mid-playback. Existing
        # peak-monitor / link-watch / owner logic remains the source of truth
        # for state corrections. recovery_attempts is still recorded so
        # duplicate triggers for the same source/url are naturally suppressed.
        self.recovery_attempts.add(signature)
        logger.warning(
            "SILENT-ACTIVE-DIAG recovery_suppressed: would_have_recovered source=%s signature=%s vu_db=%s action=log_only",
            source, signature, vu_db,
        )

    async def stop(self) -> None:
        for task in list(self.watch_tasks.values()):
            if not task.done():
                task.cancel()
        self.watch_tasks.clear()
