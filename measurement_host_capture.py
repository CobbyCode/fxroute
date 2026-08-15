"""Execution of one host-local measurement capture attempt."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


def _detailed_measurement_diagnostics_enabled() -> bool:
    return logging.getLogger("measurement").isEnabledFor(logging.DEBUG)


class HostCaptureRunner:
    """Own process, routing, monitoring, and analysis work for one attempt."""

    def __init__(self, store):
        self._store = store

    def execute(
        self,
        *,
        job_id: str,
        owner_job_id: str,
        mic_source_node_name: str,
        reference_capture: dict[str, Any],
        channel: str,
        capture_channels: int,
        capture_path: Path,
        playback_path: Path,
        playback_target: dict[str, Any],
        measurement_scope: str,
        measurement_role: str,
        playback_gain: float | None,
        sweep_meta: dict[str, Any],
        sample_rate: int,
        duration_seconds: float,
        sweep_seconds: float,
        lead_in_seconds: float,
        tail_seconds: float,
        record_preroll_seconds: float,
        record_postroll_seconds: float,
        record_duration_seconds: float,
        calibration_curve: tuple[np.ndarray, np.ndarray] | None,
        mic_input_channel_index: int,
        electrical_reference_channel_index: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        store = self._store
        record_node_name = f"fxroute-measure-record-{job_id}"
        play_node_name = f"fxroute-measure-play-{job_id}"
        sample_count = int(round(sample_rate * record_duration_seconds))
        record_command = [
            "pw-record",
            "-P",
            "node.autoconnect=false",
            "-P",
            f"node.name={record_node_name}",
            "--target",
            "0",
            "--rate",
            str(sample_rate),
            "--channels",
            str(capture_channels),
            "--format",
            "s16",
        ]
        if store._pw_record_supports_option("--container"):
            record_command.extend(["--container", "wav"])
        if store._pw_record_supports_option("--sample-count"):
            record_command.extend(["--sample-count", str(sample_count)])
        record_command.append(str(capture_path))
        playback_route = store._build_measurement_playback_route(
            play_node_name,
            playback_target,
            measurement_scope=measurement_scope,
        )
        play_command = store._build_measurement_play_command(
            play_node_name=play_node_name,
            playback_path=playback_path,
            playback_target=playback_target,
            playback_route=playback_route,
            playback_gain=playback_gain,
        )

        record_process = store._start_job_process(owner_job_id, record_command)
        monitored_channel_index = store._recorded_mic_channel_index(
            mic_input_channel_index,
            has_electrical_reference=electrical_reference_channel_index is not None,
        )
        level_monitor_stop = threading.Event()
        level_monitor_thread = threading.Thread(
            target=store._monitor_capture_input_level,
            args=(job_id, capture_path, capture_channels, monitored_channel_index, level_monitor_stop),
            daemon=True,
        )
        level_monitor_thread.start()
        play_process: subprocess.Popen[str] | None = None
        play_stdout = ""
        play_stderr = ""
        play_timed_out = False
        record_stdout = ""
        record_stderr = ""
        helper_process_snapshots: list[dict[str, Any]] = []
        detailed_diagnostics_enabled = _detailed_measurement_diagnostics_enabled()
        routing_snapshots: list[dict[str, Any]] = []
        link_diagnostics: dict[str, Any] = {}
        playback_route_diagnostics = store._new_measurement_playback_route_diagnostics(playback_route)
        if detailed_diagnostics_enabled:
            routing_snapshots.append(
                store._build_measurement_routing_snapshot(
                    label="before-record-link",
                    playback_target=playback_target,
                    mic_source_node_name=mic_source_node_name,
                    reference_capture=reference_capture,
                    record_node_name=record_node_name,
                    play_node_name=play_node_name,
                )
            )
        try:
            store._cleanup_fxroute_links(
                source_node_name=mic_source_node_name,
                record_node_name=record_node_name,
            )
            if electrical_reference_channel_index is not None:
                link_diagnostics = store._link_capture_channels_to_record_stream(
                    source_node_name=mic_source_node_name,
                    record_node_name=record_node_name,
                    channel_indices=sorted({mic_input_channel_index, electrical_reference_channel_index}),
                )
            else:
                link_diagnostics = store._link_host_reference_capture(
                    reference_source_node_name=str(reference_capture["source_node_name"]),
                    mic_source_node_name=mic_source_node_name,
                    record_node_name=record_node_name,
                    requested_channel=channel,
                    mic_input_channel_index=mic_input_channel_index,
                    record_process=record_process,
                )
            if detailed_diagnostics_enabled:
                routing_snapshots.append(
                    store._build_measurement_routing_snapshot(
                        label="after-record-link",
                        playback_target=playback_target,
                        mic_source_node_name=mic_source_node_name,
                        reference_capture=reference_capture,
                        record_node_name=record_node_name,
                        play_node_name=play_node_name,
                    )
                )
            time.sleep(record_preroll_seconds)

            helper_process_snapshots.append(store._snapshot_fxroute_21_helper_processes("before-capture-start"))
            logger.info(
                "Measurement 2.1 helper pgrep before capture start: job_id=%s sample_rate=%s helper_processes=%s",
                job_id,
                sample_rate,
                helper_process_snapshots[-1].get("processes"),
            )

            pre_sweep_state = store._build_pre_sweep_state_snapshot(
                job_id=job_id,
                sample_rate=sample_rate,
                playback_route=playback_route,
            )
            logger.warning(
                "MEASUREMENT-STATE-CHECK pre-sweep state: %s",
                json.dumps(pre_sweep_state, sort_keys=True, default=str),
            )
            pre_sweep_failure = pre_sweep_state.get("validation_failure")
            if pre_sweep_failure:
                raise RuntimeError(
                    f"Measurement pre-sweep state check failed: {pre_sweep_failure}. "
                    "Helper/config not in consistent state - sweep refused."
                )

            play_process = store._start_job_process(owner_job_id, play_command)
            if playback_route["route"] == "direct-sink":
                playback_route_diagnostics = store._link_measurement_playback_to_direct_sink(
                    play_node_name=play_node_name,
                    playback_target=playback_target,
                    playback_route=playback_route,
                )
            time.sleep(0.2)
            if detailed_diagnostics_enabled:
                routing_snapshots.append(
                    store._build_measurement_routing_snapshot(
                        label="during-playback",
                        playback_target=playback_target,
                        mic_source_node_name=mic_source_node_name,
                        reference_capture=reference_capture,
                        record_node_name=record_node_name,
                        play_node_name=play_node_name,
                    )
                )
            try:
                play_stdout, play_stderr = play_process.communicate(timeout=duration_seconds + 8)
            except subprocess.TimeoutExpired:
                play_timed_out = True
                if play_process.poll() is None:
                    play_process.terminate()
                try:
                    play_stdout, play_stderr = play_process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    if play_process.poll() is None:
                        play_process.kill()
                    play_stdout, play_stderr = play_process.communicate(timeout=2)

            if store._pw_record_supports_option("--sample-count"):
                record_stdout, record_stderr = record_process.communicate(timeout=record_duration_seconds + 8)
            else:
                time.sleep(max(0.0, record_postroll_seconds))
                if record_process.poll() is None:
                    record_process.terminate()
                try:
                    record_stdout, record_stderr = record_process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    record_process.kill()
                    record_stdout, record_stderr = record_process.communicate(timeout=2)
        except Exception:
            if play_process is not None and play_process.poll() is None:
                play_process.kill()
                try:
                    play_process.communicate(timeout=2)
                except Exception:
                    pass
            if record_process.poll() is None:
                record_process.kill()
            try:
                record_process.communicate(timeout=2)
            except Exception:
                pass
            raise
        finally:
            level_monitor_stop.set()
            if level_monitor_thread.is_alive():
                level_monitor_thread.join(timeout=1.0)
            store._cleanup_measurement_playback_links(
                play_node_name=play_node_name,
                temporary_links=playback_route_diagnostics.get("temporary_playback_links", []),
            )
            store._cleanup_fxroute_links(
                source_node_name=mic_source_node_name,
                record_node_name=record_node_name,
            )
            if detailed_diagnostics_enabled:
                routing_snapshots.append(
                    store._build_measurement_routing_snapshot(
                        label="after-capture",
                        playback_target=playback_target,
                        mic_source_node_name=mic_source_node_name,
                        reference_capture=reference_capture,
                        record_node_name=record_node_name,
                        play_node_name=play_node_name,
                    )
                )

        if owner_job_id in store._cancelled_jobs:
            raise RuntimeError("Measurement cancelled.")

        capture_usable = capture_path.exists() and capture_path.stat().st_size > 44
        if play_process is None:
            raise RuntimeError("Sweep playback did not start")
        if play_process.returncode != 0 and not play_timed_out:
            detail = (play_stderr or play_stdout or f"pw-play exited with {play_process.returncode}").strip()
            raise RuntimeError(f"Sweep playback failed: {detail}")
        if record_process.returncode != 0 and not capture_usable:
            detail = (record_stderr or record_stdout or f"pw-record exited with {record_process.returncode}").strip()
            raise RuntimeError(f"Capture failed: {detail}")
        if not capture_usable:
            raise RuntimeError("Capture finished but no usable host-reference WAV data was produced")

        reference_channel_label = str(reference_capture.get("channel_label") or "reference")
        analysis_channel_index = store._recorded_mic_channel_index(
            mic_input_channel_index,
            has_electrical_reference=electrical_reference_channel_index is not None,
        )
        reference_channel_index = electrical_reference_channel_index if electrical_reference_channel_index is not None else 0
        store._update_measurement_job_message(owner_job_id, "Processing measurement…")
        try:
            is_21_active = any(bool(snap.get("processes")) for snap in helper_process_snapshots)
            analysis = store._analyze_sweep_capture(
                capture_path,
                expected_sample_rate=sample_rate,
                channel=channel,
                reference_sweep=sweep_meta["analysis_sweep"],
                inverse_sweep=sweep_meta["inverse_sweep"],
                calibration_curve=calibration_curve,
                capture_label="Host-local capture",
                reference_channel_index=reference_channel_index,
                analysis_channel_index=analysis_channel_index,
                reference_channel_label=reference_channel_label,
                is_21_dsp_active=is_21_active,
                measurement_role=measurement_role,
            )
        except Exception:
            helper_process_snapshots.append(store._snapshot_fxroute_21_helper_processes("after-capture-analysis-failure"))
            logger.exception(
                "Measurement 2.1 helper pgrep after capture analysis failure: job_id=%s sample_rate=%s helper_processes=%s",
                job_id,
                sample_rate,
                helper_process_snapshots[-1].get("processes"),
            )
            raise
        analysis["method"] = (
            "inverse log-sweep deconvolution with electrical reference input timing"
            if electrical_reference_channel_index is not None
            else "inverse log-sweep deconvolution with host-reference dual-channel capture"
        )
        analysis_clock = analysis.get("clock") if isinstance(analysis.get("clock"), dict) else {}
        analysis_clock.update({
            "timing_channel": reference_channel_label,
            "reference_capture_mode": "electrical-input" if electrical_reference_channel_index is not None else "dual-channel",
            "reference_channel": reference_channel_label,
        })
        analysis["clock"] = analysis_clock
        reference_path = analysis.get("reference_path") if isinstance(analysis.get("reference_path"), dict) else {}
        reference_path.update({
            "timing_applied_to_mic": True,
            "capture_mode": "electrical-input" if electrical_reference_channel_index is not None else "dual-channel",
            "mic_input_channel": mic_input_channel_index + 1,
            "electrical_reference_input_channel": electrical_reference_channel_index + 1 if electrical_reference_channel_index is not None else None,
        })
        if electrical_reference_channel_index is not None:
            impulse_meta = analysis.get("impulse_response") if isinstance(analysis.get("impulse_response"), dict) else {}
            reference_path.update({
                "timing_status": "electrical-reference-candidate",
                "timing_label": "Electrical reference candidate",
                "electrical_reference_used": False,
                "electrical_reference_delay_samples": impulse_meta.get("reference_peak_index"),
                "electrical_reference_delay_seconds": impulse_meta.get("reference_peak_seconds"),
                "electrical_reference_delay_ms": round(float(impulse_meta.get("reference_peak_seconds") or 0.0) * 1000.0, 6),
                "acoustic_arrival_delay_samples": impulse_meta.get("direct_arrival_index"),
                "acoustic_arrival_delay_seconds": impulse_meta.get("direct_seconds"),
                "acoustic_arrival_delay_ms": round(float(impulse_meta.get("direct_seconds") or 0.0) * 1000.0, 6),
                "acoustic_arrival_corrected_samples": impulse_meta.get("arrival_samples"),
                "acoustic_arrival_corrected_seconds": impulse_meta.get("arrival_seconds"),
                "acoustic_arrival_corrected_ms": impulse_meta.get("arrival_ms"),
                "confidence": impulse_meta.get("direct_confidence"),
                "stability": "candidate",
            })
        else:
            impulse_meta = analysis.get("impulse_response") if isinstance(analysis.get("impulse_response"), dict) else {}
            reference_path.update({
                "timing_status": "acoustic-only",
                "timing_label": "Acoustic-only timing",
                "electrical_reference_used": False,
                "acoustic_arrival_delay_samples": impulse_meta.get("direct_arrival_index"),
                "acoustic_arrival_delay_seconds": impulse_meta.get("direct_seconds"),
                "acoustic_arrival_delay_ms": round(float(impulse_meta.get("direct_seconds") or 0.0) * 1000.0, 6),
                "acoustic_arrival_corrected_samples": impulse_meta.get("arrival_samples"),
                "acoustic_arrival_corrected_seconds": impulse_meta.get("arrival_seconds"),
                "acoustic_arrival_corrected_ms": impulse_meta.get("arrival_ms"),
                "confidence": impulse_meta.get("direct_confidence"),
                "stability": "host-reference",
            })
        analysis["reference_path"] = reference_path
        pipewire_warnings = store._extract_pipewire_warning_lines({
            "pw-play.stdout": play_stdout,
            "pw-play.stderr": play_stderr,
            "pw-record.stdout": record_stdout,
            "pw-record.stderr": record_stderr,
        })
        playback_node = store._lookup_pipewire_audio_node(playback_target["target_name"])
        capture_node = store._lookup_pipewire_audio_node(mic_source_node_name)
        reference_node = store._lookup_pipewire_audio_node(str(reference_capture.get("source_node_name") or ""))
        uses_monitor_source = str(reference_capture.get("source_node_name") or "").endswith(".monitor")
        routing_diagnostics = {
            "schema": "fxroute.measurement-routing-diagnostics.v1",
            "detail_enabled": detailed_diagnostics_enabled,
            "measurement_scope": measurement_scope,
            "playback_sink": {
                "target_name": playback_target["target_name"],
                "target_label": playback_target["target_label"],
                "active_rate": playback_target.get("active_rate"),
                "node": playback_node,
            },
            "capture_source": {"node_name": mic_source_node_name, "node": capture_node},
            "reference_capture": {
                "source_node_name": str(reference_capture.get("source_node_name") or ""),
                "sink_node_name": str(reference_capture.get("sink_node_name") or ""),
                "channel": str(reference_capture.get("channel") or ""),
                "channel_label": str(reference_capture.get("channel_label") or ""),
                "uses_monitor_source": uses_monitor_source,
                "node": reference_node,
            },
            "record_node": record_node_name,
            "play_node": play_node_name,
            "measurement_playback_route": playback_route_diagnostics.get("measurement_playback_route"),
            "temporary_playback_links": playback_route_diagnostics.get("temporary_playback_links", []),
            "play_node_helper_links": playback_route_diagnostics.get("play_node_helper_links", []),
            "direct_hardware_links_removed": playback_route_diagnostics.get("direct_hardware_links_removed", []),
            "direct_hardware_links_remaining": playback_route_diagnostics.get("direct_hardware_links_remaining", []),
            "play_node_links_after_manual_link": playback_route_diagnostics.get("play_node_links_after_manual_link", []),
            "requested_channel": channel,
            "sample_rate": sample_rate,
            "sweep": {
                key: sweep_meta.get(key)
                for key in (
                    "sample_rate", "samples", "channels", "peak_linear", "peak_dbfs",
                    "rms_dbfs", "per_channel_peak_dbfs", "would_clip_before_write",
                )
                if key in sweep_meta
            },
            "link_diagnostics": link_diagnostics,
            "snapshots": routing_snapshots,
            "process": {
                "pw_play_returncode": play_process.returncode if play_process is not None else None,
                "pw_record_returncode": record_process.returncode,
                "pw_play_timed_out": bool(play_timed_out),
                "pipewire_warning_lines": pipewire_warnings,
            },
            "fxroute_21_helper_processes": helper_process_snapshots,
        }
        if pipewire_warnings:
            logger.warning("Measurement PipeWire warnings: %s", pipewire_warnings)
        if not playback_node.get("id"):
            logger.warning("Measurement playback sink not found in PipeWire/PulseAudio node list: %s", playback_target["target_name"])
        if not capture_node.get("id"):
            logger.warning("Measurement capture source not found in PipeWire/PulseAudio node list: %s", mic_source_node_name)
        if not reference_node.get("id"):
            logger.warning("Measurement reference source not found in PipeWire/PulseAudio node list: %s", reference_capture.get("source_node_name"))
        if not uses_monitor_source:
            logger.warning("Measurement reference capture is not using a monitor source: %s", reference_capture.get("source_node_name"))
        if routing_diagnostics["sweep"].get("would_clip_before_write"):
            logger.warning("Measurement sweep would clip before playback: peak_dbfs=%s", routing_diagnostics["sweep"].get("peak_dbfs"))
        logger.info(
            "Measurement summary: playback_route=%s playback_target=%s play_node=%s capture_source=%s reference_source=%s monitor=%s sample_rate=%s sweep_peak_dbfs=%s sweep_rms_dbfs=%s pipewire_warnings=%d detail=%s",
            routing_diagnostics["measurement_playback_route"],
            routing_diagnostics["playback_sink"]["target_name"],
            routing_diagnostics["play_node"],
            routing_diagnostics["capture_source"]["node_name"],
            routing_diagnostics["reference_capture"]["source_node_name"],
            routing_diagnostics["reference_capture"]["uses_monitor_source"],
            sample_rate,
            routing_diagnostics["sweep"].get("peak_dbfs"),
            routing_diagnostics["sweep"].get("rms_dbfs"),
            len(pipewire_warnings),
            "debug" if detailed_diagnostics_enabled else "off",
        )
        logger.debug("Measurement routing diagnostics: %s", json.dumps(routing_diagnostics, sort_keys=True))
        return (
            analysis,
            {
                "path": str(capture_path),
                "duration_seconds": round(duration_seconds, 3),
                "sample_rate": sample_rate,
                "channels": capture_channels,
                "input_node": mic_source_node_name,
                "microphone_node": mic_source_node_name,
                "mic_input_channel": mic_input_channel_index + 1,
                "electrical_reference_input_channel": electrical_reference_channel_index + 1 if electrical_reference_channel_index is not None else None,
                "reference_node": str(reference_capture.get("source_node_name") or ""),
                "reference_channel": str(reference_capture.get("channel_label") or "reference"),
                "reference_path": str(capture_path),
                "record_node": record_node_name,
                "routing_diagnostics": routing_diagnostics,
            },
            {
                "path": str(playback_path),
                "duration_seconds": round(duration_seconds, 3),
                "sweep_seconds": round(sweep_seconds, 3),
                "lead_in_seconds": round(lead_in_seconds, 3),
                "tail_seconds": round(tail_seconds, 3),
                "play_node": play_node_name,
                "target_name": playback_target["target_name"],
                "target_label": playback_target["target_label"],
                "timed_out": bool(play_timed_out),
                "routing_diagnostics": routing_diagnostics,
            },
        )
