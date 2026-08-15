"""Measurement-specific playback and PipeWire routing policy."""

from __future__ import annotations

import logging
import math
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from dsp.runtime import DSPRuntimeConfig
from measurement.constants import (
    MEASUREMENT_SCOPE_ACTIVE_CHAIN,
    MEASUREMENT_SCOPE_RAW_HELPER,
)

logger = logging.getLogger(__name__)


class MeasurementRouting:
    """Own Measurement playback target, link policy, and routing diagnostics."""

    def __init__(self, store):
        self._store = store
        self._run = store._routing_command_runner
        self._get_output_overview = store._routing_output_overview

    def _resolve_playback_target(
        self,
        *,
        measurement_scope: str = MEASUREMENT_SCOPE_ACTIVE_CHAIN,
        overview: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        overview = overview or self._get_output_overview()
        self._store._normalize_measurement_scope(measurement_scope)
        return self._resolve_active_chain_playback_target(overview)

    def _resolve_active_chain_playback_target(self, overview: dict[str, Any]) -> dict[str, Any]:
        target_name = "fxroute_dsp_sink"
        ports = self._store._list_pw_ports(target_name)
        if f"{target_name}:playback_FL" not in ports or f"{target_name}:playback_FR" not in ports:
            raise RuntimeError(
                "Active-chain measurement route unavailable: DSP input sink ports are missing "
                f"for {target_name} (ports={ports})"
            )
        current_output = overview.get("current_output") or {}
        selected_output = overview.get("selected_output") or {}
        default_output = overview.get("default_output") or {}
        return {
            "target_name": target_name,
            "target_label": f"Active listening chain ({target_name})",
            "active_rate": current_output.get("active_rate") or selected_output.get("active_rate") or default_output.get("active_rate"),
        }

    def _resolve_host_reference_capture(
        self,
        *,
        playback_target: dict[str, Any],
        mic_source_node_name: str,
        requested_channel: str,
    ) -> dict[str, str]:
        sink_node_name = str(playback_target.get("target_name") or "").strip()
        if not sink_node_name:
            raise RuntimeError("No active output sink is available for host-reference capture")
        if not mic_source_node_name or mic_source_node_name.endswith(".monitor"):
            raise RuntimeError("Host-reference capture requires a real microphone source")
        monitor_source_node_name = f"{sink_node_name}.monitor"
        monitor_channel = "right" if requested_channel == "right" else "left"
        monitor_ports = self._list_source_output_ports(monitor_source_node_name)
        preferred_suffixes = [":monitor_FR", ":output_FR", ":capture_FR", ":capture_MONO", ":output_MONO"] if monitor_channel == "right" else [":monitor_FL", ":output_FL", ":capture_FL", ":capture_MONO", ":output_MONO"]
        if not self._pick_port(monitor_ports, preferred_suffixes):
            raise RuntimeError(f"Active sink monitor for {requested_channel} is not available on {sink_node_name}")
        return {
            "source_node_name": monitor_source_node_name,
            "sink_node_name": sink_node_name,
            "channel": monitor_channel,
            "channel_label": f"monitor_{'FR' if monitor_channel == 'right' else 'FL'}",
        }

    def _link_host_reference_capture(
        self,
        *,
        reference_source_node_name: str,
        mic_source_node_name: str,
        record_node_name: str,
        requested_channel: str,
        mic_input_channel_index: int = 0,
        record_process: subprocess.Popen[str],
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 4.0
        reference_ports: list[str] = []
        mic_ports: list[str] = []
        record_inputs: list[str] = []
        while time.monotonic() < deadline:
            reference_ports = self._list_source_output_ports(reference_source_node_name)
            mic_ports = self._list_source_output_ports(mic_source_node_name)
            record_ports = self._store._list_pw_ports(record_node_name)
            record_inputs = [port for port in record_ports if ":input_" in port]
            if reference_ports and mic_ports and record_inputs:
                break
            returncode = record_process.poll()
            if returncode is not None:
                record_stdout, record_stderr = record_process.communicate()
                missing = [
                    name
                    for name, ports in (
                        ("reference_ports", reference_ports),
                        ("mic_ports", mic_ports),
                        ("record_inputs", record_inputs),
                    )
                    if not ports
                ]
                detail = (record_stderr or record_stdout or "no process output").strip()
                raise RuntimeError(
                    f"pw-record exited during PipeWire port discovery with returncode {returncode}: {detail}; "
                    f"missing port groups: {', '.join(missing) or 'none'}"
                )
            time.sleep(0.1)
        else:
            missing = [
                name
                for name, ports in (
                    ("reference_ports", reference_ports),
                    ("mic_ports", mic_ports),
                    ("record_inputs", record_inputs),
                )
                if not ports
            ]
            returncode = record_process.poll()
            process_status = "running" if returncode is None else f"exited with returncode {returncode}"
            process_detail = ""
            if returncode is not None:
                record_stdout, record_stderr = record_process.communicate()
                detail = (record_stderr or record_stdout or "no process output").strip()
                process_detail = f", output={detail}"
            raise RuntimeError(
                f"Unable to discover PipeWire ports for host-reference capture into {record_node_name}; "
                f"missing port groups: {', '.join(missing) or 'none'}; pw-record={process_status}{process_detail}; "
                f"reference_ports={reference_ports}; mic_ports={mic_ports}; record_inputs={record_inputs}"
            )

        reference_suffixes = [":monitor_FR", ":output_FR", ":capture_FR", ":capture_MONO", ":output_MONO", ":monitor_FL", ":output_FL", ":capture_FL"] if requested_channel == "right" else [":monitor_FL", ":output_FL", ":capture_FL", ":capture_MONO", ":output_MONO", ":monitor_FR", ":output_FR", ":capture_FR"]
        mic_suffixes = self._port_suffixes_for_channel_index(mic_input_channel_index) + [":capture_MONO", ":output_MONO"]
        reference_port = self._pick_port(reference_ports, reference_suffixes)
        mic_port = self._pick_port(mic_ports, mic_suffixes)
        input_left = self._pick_port(record_inputs, [":input_FL", ":input_MONO"])
        input_right = self._pick_port(record_inputs, [":input_FR", ":input_MONO", ":input_FL"])
        if not reference_port or not mic_port or not input_left or not input_right:
            raise RuntimeError("Could not resolve PipeWire ports for host-reference capture")

        self._cleanup_fxroute_links(
            source_node_name=mic_source_node_name,
            record_node_name=record_node_name,
        )
        link_errors: list[str] = []
        for src, dst, label in [
            (reference_port, input_left, "reference-to-record-left"),
            (mic_port, input_right, "microphone-to-record-right"),
        ]:
            try:
                self._run(["pw-link", src, dst], capture_output=True, text=True, timeout=3, check=True)
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip()
                stdout = (exc.stdout or "").strip()
                if "already exists" in stderr.lower() or "already exists" in stdout.lower():
                    logger.info("Link already exists for %s (%s -> %s), skipping", label, src, dst)
                else:
                    link_errors.append(f"{label}: {stderr or stdout or exc}")
            except Exception as exc:
                link_errors.append(f"{label}: {exc}")
        if link_errors:
            logger.warning("Host-reference link issues: %s", link_errors)
            # If mic link failed, abort measurement — audio path would be incomplete
            if any("microphone-to-record" in err for err in link_errors):
                raise RuntimeError(
                    "Measurement audio path could not be prepared. Please retry."
                )
        time.sleep(0.15)
        result = {
            "reference_source_node": reference_source_node_name,
            "microphone_source_node": mic_source_node_name,
            "record_node": record_node_name,
            "links": [
                {"source_port": reference_port, "target_port": input_left, "role": "reference-monitor-to-record-left"},
                {"source_port": mic_port, "target_port": input_right, "role": "microphone-to-record-right"},
            ],
            "record_inputs": record_inputs,
            "reference_ports": reference_ports,
            "microphone_ports": mic_ports,
        }
        if link_errors and not any("microphone-to-record" in err for err in link_errors):
            result["link_warning"] = " ".join(link_errors)
        return result

    def _link_capture_channels_to_record_stream(
        self,
        *,
        source_node_name: str,
        record_node_name: str,
        channel_indices: list[int],
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 4.0
        source_ports: list[str] = []
        record_ports: list[str] = []
        while time.monotonic() < deadline:
            source_ports = self._list_source_output_ports(source_node_name)
            record_ports = self._store._list_pw_ports(record_node_name)
            record_inputs = [port for port in record_ports if ":input_" in port]
            if source_ports and record_inputs:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"Unable to discover PipeWire ports for selected input channels on {source_node_name}")

        record_inputs = [port for port in record_ports if ":input_" in port]
        links: list[dict[str, str | int]] = []
        used_pairs: set[tuple[str, str]] = set()
        for channel_index in channel_indices:
            source_port = self._pick_preferred_port(source_ports, self._port_suffixes_for_channel_index(channel_index))
            input_port = self._pick_preferred_port(record_inputs, self._record_input_suffixes_for_channel_index(channel_index))
            if not source_port or not input_port:
                raise RuntimeError(f"Could not resolve PipeWire ports for Input {channel_index + 1}")
            pair = (source_port, input_port)
            if pair in used_pairs:
                continue
            link_error = None
            try:
                self._run(["pw-link", source_port, input_port], capture_output=True, text=True, timeout=3, check=True)
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip()
                if "already exists" in stderr.lower():
                    logger.info("Link already exists for channel %d (%s -> %s), skipping", channel_index + 1, source_port, input_port)
                else:
                    link_error = stderr or str(exc)
            except Exception as exc:
                link_error = str(exc)
            if link_error is not None:
                raise RuntimeError(
                    f"Could not create measurement audio link for input channel {channel_index + 1}: {link_error}"
                )
            used_pairs.add(pair)
            links.append(
                {
                    "source_port": source_port,
                    "target_port": input_port,
                    "input_channel": channel_index + 1,
                    "role": "selected-capture-channel-to-record",
                }
            )

        time.sleep(0.15)
        return {
            "source_node": source_node_name,
            "record_node": record_node_name,
            "links": links,
            "source_ports": source_ports,
            "record_inputs": record_inputs,
        }

    def _list_source_output_ports(self, source_node_name: str) -> list[str]:
        candidate_source_names = [source_node_name]
        if source_node_name.endswith(".monitor"):
            candidate_source_names.append(source_node_name[: -len(".monitor")])
        for candidate_name in candidate_source_names:
            source_ports = self._store._list_pw_ports(candidate_name)
            source_outputs = [port for port in source_ports if ":capture_" in port or ":output_" in port or ":monitor_" in port]
            if source_outputs:
                return source_outputs
        return []

    def _build_measurement_playback_route(
        self,
        play_node_name: str,
        playback_target: dict[str, Any],
        *,
        measurement_scope: str = MEASUREMENT_SCOPE_ACTIVE_CHAIN,
        overview: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        measurement_scope = self._store._normalize_measurement_scope(measurement_scope)
        overview = overview or self._get_output_overview()
        output_mode = overview.get("output_mode") if isinstance(overview.get("output_mode"), dict) else {}
        mode = str(output_mode.get("mode") or "")
        return {
            "route": "direct-sink",
            "measurement_scope": measurement_scope,
            "output_mode": mode or "stereo",
            "play_node_name": play_node_name,
            "playback_target_name": str(playback_target.get("target_name") or ""),
            "expected_native_layout": [dict(channel) for channel in DSPRuntimeConfig.from_overview(overview).layout],
        }

    @staticmethod
    def _build_measurement_play_command(
        *,
        play_node_name: str,
        playback_path: Path,
        playback_target: dict[str, Any],
        playback_route: dict[str, Any],
        playback_gain: float | None = None,
    ) -> list[str]:
        # Measurement playback must not rely on pw-play --target
        # autoconnect: PipeWire resolves it non-deterministically (observed
        # live: the sweep node linked output_FL to the sink FR input and
        # output_FR to a DSP internal output port, producing a
        # silent/wrong sweep).  Mirror the subwoofer routes: disable
        # autoconnect and link the play node explicitly after its ports
        # exist.
        command = [
            "pw-play",
            "-P",
            "node.autoconnect=false",
            "-P",
            f"node.name={play_node_name}",
            "--target",
            "0",
            str(playback_path),
        ]
        if playback_route.get("measurement_scope") == MEASUREMENT_SCOPE_RAW_HELPER and playback_gain is not None:
            try:
                normalized_gain = float(playback_gain)
            except (TypeError, ValueError) as exc:
                raise ValueError("playback_gain must be a finite non-negative number") from exc
            if not math.isfinite(normalized_gain) or normalized_gain < 0.0:
                raise ValueError("playback_gain must be a finite non-negative number")
            command.insert(-1, f"--volume={normalized_gain:.9g}")
        return command

    @staticmethod
    def _new_measurement_playback_route_diagnostics(playback_route: dict[str, Any]) -> dict[str, Any]:
        return {
            "measurement_playback_route": playback_route.get("route") or "direct-sink",
            "measurement_scope": playback_route.get("measurement_scope") or MEASUREMENT_SCOPE_ACTIVE_CHAIN,
            "output_mode": playback_route.get("output_mode") or "",
            "temporary_playback_links": [],
            "play_node_helper_links": [],
            "active_chain_input_links": [],
            "active_chain_output_links": [],
            "direct_hardware_links_removed": [],
            "direct_hardware_links_remaining": [],
            "play_node_links_after_manual_link": [],
        }

    def _wait_for_measurement_play_ports(self, play_node_name: str) -> dict[str, str]:
        deadline = time.monotonic() + 4.0
        play_ports: list[str] = []
        while time.monotonic() < deadline:
            play_ports = self._store._list_pw_ports(play_node_name)
            output_l = f"{play_node_name}:output_FL"
            output_r = f"{play_node_name}:output_FR"
            if output_l in play_ports and output_r in play_ports:
                return {"left": output_l, "right": output_r}
            time.sleep(0.05)
        raise RuntimeError(
            "Subwoofer measurement playback route unavailable: measurement play FL/FR outputs missing "
            f"for {play_node_name} (ports={play_ports})"
        )

    def _link_measurement_playback_to_direct_sink(
        self,
        *,
        play_node_name: str,
        playback_target: dict[str, Any],
        playback_route: dict[str, Any],
    ) -> dict[str, Any]:
        """Deterministically link the sweep playback to the resolved sink.

        The direct-sink route previously relied on pw-play --target
        autoconnect, which PipeWire resolves non-deterministically (observed
        live: the sweep node linked output_FL to the sink FR input and
        output_FR to a DSP internal output port).  Mirror the
        subwoofer routes: disable autoconnect, wait for the play node ports
        and link explicitly to the resolved playback target.  With
        ACTIVE_CHAIN that target is fxroute_dsp_sink, so the sweep passes
        through the full FXRoute DSP gain chain.
        """
        diagnostics = self._new_measurement_playback_route_diagnostics(playback_route)
        play_ports = self._wait_for_measurement_play_ports(play_node_name)
        sink_name = str(playback_route.get("playback_target_name") or playback_target.get("target_name") or "").strip()
        sink_ports = self._store._list_pw_ports(sink_name)
        input_left = f"{sink_name}:playback_FL"
        input_right = f"{sink_name}:playback_FR"
        if input_left not in sink_ports or input_right not in sink_ports:
            raise RuntimeError(
                "Direct-sink measurement playback route unavailable: sink playback ports missing "
                f"for {sink_name} (ports={sink_ports})"
            )

        temporary_links = [
            {"source_port": play_ports["left"], "target_port": input_left, "role": "measurement-play-left-to-direct-sink"},
            {"source_port": play_ports["right"], "target_port": input_right, "role": "measurement-play-right-to-direct-sink"},
        ]
        created_links: list[dict[str, str]] = []
        try:
            for link in temporary_links:
                self._store._create_pipewire_link(str(link["source_port"]), str(link["target_port"]))
                created_links.append(link)
        except Exception:
            self._cleanup_measurement_playback_links(
                play_node_name=play_node_name,
                temporary_links=created_links,
            )
            raise

        diagnostics["temporary_playback_links"] = temporary_links
        diagnostics["active_chain_input_links"] = list(temporary_links)
        diagnostics["play_node_links_after_manual_link"] = self._list_relevant_pw_links([play_node_name, sink_name])
        logger.info(
            "Direct-sink measurement playback manually linked: play_node=%s input_links=%s",
            play_node_name,
            diagnostics["active_chain_input_links"],
        )
        return diagnostics

    def _cleanup_measurement_playback_links(
        self,
        *,
        play_node_name: str,
        temporary_links: list[Any] | None = None,
    ) -> list[str]:
        removed: list[str] = []
        for link in temporary_links or []:
            if not isinstance(link, dict):
                continue
            source_port = str(link.get("source_port") or "")
            target_port = str(link.get("target_port") or "")
            if source_port and target_port and self._store._disconnect_link(source_port, target_port):
                removed.append(f"{source_port} -> {target_port}")

        try:
            completed = self._run(["pw-link", "-l"], capture_output=True, text=True, timeout=3)
        except Exception:
            return removed
        if completed.returncode != 0:
            return removed
        for line in (completed.stdout or "").splitlines():
            line = line.strip()
            if "->" not in line or play_node_name not in line:
                continue
            left, right = line.split("->", 1)
            source_port = left.strip()
            target_port = right.strip()
            if "(id:" in target_port:
                target_port = target_port.partition("(id:")[0].strip()
            if not source_port or not target_port:
                continue
            if self._store._disconnect_link(source_port, target_port):
                removed.append(f"{source_port} -> {target_port}")
        if removed:
            logger.info("Cleaned up %d measurement playback link(s) for %s", len(removed), play_node_name)
            time.sleep(0.1)
        return removed

    def _build_measurement_routing_snapshot(
        self,
        *,
        label: str,
        playback_target: dict[str, Any],
        mic_source_node_name: str,
        reference_capture: dict[str, Any],
        record_node_name: str,
        play_node_name: str,
    ) -> dict[str, Any]:
        relevant_nodes = [
            str(playback_target.get("target_name") or ""),
            mic_source_node_name,
            str(reference_capture.get("source_node_name") or ""),
            str(reference_capture.get("sink_node_name") or ""),
            record_node_name,
            play_node_name,
            "fxroute_dsp",
            "fxroute_dsp_sink",
        ]
        relevant_nodes = [node for node in relevant_nodes if node]
        snapshot = {
            "label": label,
            "captured_at": self._store._utc_now(),
            "default_sink": self._pactl_info_value("Default Sink"),
            "default_source": self._pactl_info_value("Default Source"),
            "sinks": self._list_pactl_short_nodes("sinks", relevant_nodes),
            "sources": self._list_pactl_short_nodes("sources", relevant_nodes),
            "ports": {node: self._store._list_pw_ports(node) for node in relevant_nodes},
            "links": self._list_relevant_pw_links(relevant_nodes),
        }
        snapshot["monitor_sources_involved"] = [
            node
            for node in relevant_nodes
            if node.endswith(".monitor") or any(".monitor" in port for port in snapshot["ports"].get(node, []))
        ]
        snapshot["fxroute_dsp_sink_inputs"] = [
            line
            for line in snapshot["links"]
            if "fxroute_dsp_sink:playback_" in line and ("|<-" in line or "|->" in line)
        ]
        return snapshot

    def _lookup_pipewire_audio_node(self, node_name: str) -> dict[str, Any]:
        if not node_name:
            return {}
        for kind in ("sinks", "sources"):
            for item in self._list_pactl_short_nodes(kind, [node_name]):
                if item.get("name") == node_name:
                    item["kind"] = kind[:-1]
                return item
        return {"name": node_name}

    def _build_pre_sweep_state_snapshot(
        self,
        *,
        job_id: str,
        sample_rate: int,
        playback_route: dict,
    ):
        """Capture comprehensive helper/config state before sweep.

        Returns a dict with validation_failure key set when the state is
        inconsistent and the sweep must be refused.

        Mode-aware: stereo/direct-sink routes do not require a 2.x helper.
        """
        route_name = str(playback_route.get("route") or "")
        output_mode = str(playback_route.get("output_mode") or "unknown")
        route_needs_helper = True

        now = time.monotonic()
        snapshot = {
            "job_id": job_id,
            "measurement_rate": sample_rate,
            "playback_route": route_name,
            "output_mode": output_mode,
            "helper_required": route_needs_helper,
            "timestamp": now,
            "validation_failure": None,
        }

        runtime = self._store.runtime_snapshot_provider() if callable(self._store.runtime_snapshot_provider) else {}
        snapshot["native_runtime"] = runtime
        config = runtime.get("config") if isinstance(runtime, dict) else None
        failures = []
        if not runtime.get("active"):
            failures.append("native DSP runtime is inactive")
        if not isinstance(config, dict):
            failures.append("native DSP runtime config is unavailable")
        else:
            if int(config.get("sample_rate") or 0) != sample_rate:
                failures.append(f"native DSP rate {config.get('sample_rate')} != measurement rate {sample_rate}")
            if str(config.get("output_mode") or "") != output_mode:
                failures.append(f"native DSP output mode {config.get('output_mode')} != measurement mode {output_mode}")
            expected_outputs = 4 if output_mode.startswith("subwoofer-2.") else 2
            runtime_layout = config.get("layout") or []
            expected_layout = playback_route.get("expected_native_layout") or []
            if len(runtime_layout) != expected_outputs:
                failures.append(f"native DSP layout does not expose {expected_outputs} outputs")
            if expected_layout and runtime_layout != expected_layout:
                failures.append("native DSP routing/crossover/alignment layout does not match measurement output mode")
        if playback_route.get("measurement_scope") == MEASUREMENT_SCOPE_RAW_HELPER and not runtime.get("effect_bypass"):
            failures.append("native DSP effects are not bypassed for raw-helper measurement")
        if failures:
            snapshot["validation_failure"] = "; ".join(failures)

        # Sink suspend/resume history
        try:
            last_suspend = self._run(
                ["journalctl", "--user", "-u", "fxroute.service", "--no-pager",
                 "--since", "60 seconds ago", "-o", "cat"],
                capture_output=True, text=True, timeout=3,
            )
            suspend_lines = [l for l in (last_suspend.stdout or "").splitlines()
                           if "suspend" in l.lower() or "Suspend" in l]
            snapshot["recent_suspend_count"] = len(suspend_lines)
            if suspend_lines:
                snapshot["last_suspend_line"] = suspend_lines[-1][:300]
            else:
                snapshot["last_suspend_line"] = None
        except Exception as exc:
            snapshot["suspend_log_error"] = str(exc)

        # PipeWire rate check
        try:
            pw_rate = self._run(
                ["pw-metadata", "-n", "settings", "0", "clock.force-rate"],
                capture_output=True, text=True, timeout=2,
            )
            pw_rate_val = (pw_rate.stdout or "").strip()
            snapshot["pipewire_force_rate"] = pw_rate_val
        except Exception as exc:
            snapshot["pipewire_force_rate"] = f"error: {exc}"

        # Pactl sink info
        try:
            pactl = self._run(
                ["pactl", "list", "sinks", "short"],
                capture_output=True, text=True, timeout=3,
            )
            sink_lines = [l for l in (pactl.stdout or "").splitlines() if l.strip()]
            for line in sink_lines:
                if "RUNNING" in line or "IDLE" in line:
                    parts_line = line.split()
                    if len(parts_line) >= 2:
                        snapshot["pactl_sink_name"] = parts_line[1]
                        if len(parts_line) >= 5:
                            snapshot["pactl_sink_sample_rate"] = parts_line[4]
                    break
        except Exception as exc:
            snapshot["pactl_error"] = str(exc)

        # Bassgain-relevant: master volume / mute via pactl
        try:
            volume = self._run(
                ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                capture_output=True, text=True, timeout=2,
            )
            vol_line = (volume.stdout or "").strip()
            snapshot["pactl_master_volume"] = vol_line[:200] if vol_line else None

            mute = self._run(
                ["pactl", "get-sink-mute", "@DEFAULT_SINK@"],
                capture_output=True, text=True, timeout=2,
            )
            mute_line = (mute.stdout or "").strip()
            snapshot["pactl_master_mute"] = mute_line[:200] if mute_line else None
        except Exception as exc:
            snapshot["pactl_volume_error"] = str(exc)

        return snapshot
    @staticmethod
    def _extract_pipewire_warning_lines(outputs: dict[str, str]) -> list[dict[str, str]]:
        warning_patterns = ("xrun", "underrun", "overrun", "buffer", "warning", "warn", "error", "failed")
        items: list[dict[str, str]] = []
        for stream_name, text_value in outputs.items():
            for raw_line in (text_value or "").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                lowered = line.lower()
                if any(pattern in lowered for pattern in warning_patterns):
                    items.append({"stream": stream_name, "line": line[:500]})
        return items[:40]

    def _pactl_info_value(self, key: str) -> str | None:
        try:
            completed = self._run(["pactl", "info"], capture_output=True, text=True, timeout=3)
        except Exception:
            return None
        if completed.returncode != 0:
            return None
        prefix = f"{key}:"
        for raw_line in (completed.stdout or "").splitlines():
            if raw_line.startswith(prefix):
                value = raw_line.split(":", 1)[1].strip()
                return value or None
        return None

    def _list_pactl_short_nodes(self, kind: str, relevant_nodes: list[str]) -> list[dict[str, Any]]:
        if kind not in {"sinks", "sources"}:
            return []
        try:
            completed = self._run(["pactl", "list", "short", kind], capture_output=True, text=True, timeout=3)
        except Exception:
            return []
        if completed.returncode != 0:
            return []
        relevant = {node for node in relevant_nodes if node}
        items: list[dict[str, Any]] = []
        for line in (completed.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            name = parts[1].strip()
            if relevant and name not in relevant:
                continue
            sample_spec = parts[3].strip()
            rate_match = re.search(r"(\d+)Hz", sample_spec)
            items.append(
                {
                    "id": parts[0].strip(),
                    "name": name,
                    "driver": parts[2].strip(),
                    "sample_spec": sample_spec,
                    "sample_rate": int(rate_match.group(1)) if rate_match else None,
                    "state": parts[4].strip(),
                }
            )
        return items

    def _list_relevant_pw_links(self, relevant_nodes: list[str]) -> list[str]:
        try:
            completed = self._run(["pw-link", "-l"], capture_output=True, text=True, timeout=3)
        except Exception:
            return []
        if completed.returncode != 0:
            return []
        relevant = [node for node in relevant_nodes if node]
        lines = (completed.stdout or "").splitlines()
        kept: list[str] = []
        current_header = ""
        current_block: list[str] = []

        def flush_block() -> None:
            if not current_block:
                return
            block_text = "\n".join(current_block)
            if any(node in block_text for node in relevant):
                kept.extend(line[:500] for line in current_block)

        for raw_line in lines:
            line = raw_line.rstrip()
            if not line.startswith((" ", "\t", "|")):
                flush_block()
                current_header = line
                current_block = [current_header]
            else:
                current_block.append(line)
        flush_block()
        return kept[:240]

    def _cleanup_fxroute_links(
        self,
        *,
        source_node_name: str,
        record_node_name: str,
    ) -> list[str]:
        """Remove any measurement links involving the given source/record nodes.
        Returns list of removed link descriptions for diagnostics."""
        removed: list[str] = []
        try:
            completed = self._run(["pw-link", "-l"], capture_output=True, text=True, timeout=3)
        except Exception:
            return removed
        if completed.returncode != 0:
            return removed
        nodes_of_interest = {source_node_name, record_node_name}
        for node in list(nodes_of_interest):
            if node.endswith(".monitor"):
                nodes_of_interest.add(node[: -len(".monitor")])
            else:
                nodes_of_interest.add(f"{node}.monitor")
        for line in (completed.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("->")
            if len(parts) != 2:
                continue
            in_port = parts[0].strip()
            out_port = parts[1].strip()
            # strip optional (id: ...)
            link_id = None
            if "(id:" in out_port:
                out_port, link_id_part, _ = out_port.partition("(id:")
                out_port = out_port.strip()
                link_id = link_id_part.strip().rstrip(")").strip()
            in_node = in_port.rsplit(":", 1)[0] if ":" in in_port else ""
            out_node = out_port.rsplit(":", 1)[0] if ":" in out_port else ""
            if in_node not in nodes_of_interest and out_node not in nodes_of_interest:
                continue
            unlinked = False
            if link_id and link_id.isdigit():
                try:
                    self._run(
                        ["pw-link", "-d", link_id],
                        capture_output=True, text=True, timeout=3,
                    )
                    unlinked = True
                except Exception:
                    pass
            if not unlinked:
                self._store._disconnect_link(out_port, in_port)
            removed.append(f"{out_port} -> {in_port}")
        if removed:
            logger.info("Cleaned up %d stale fxroute link(s)", len(removed))
            time.sleep(0.1)
        return removed

    @staticmethod
    def _pick_port(ports: list[str], preferred_suffixes: list[str]) -> str | None:
        for suffix in preferred_suffixes:
            for port in ports:
                if port.endswith(suffix):
                    return port
        return ports[0] if ports else None

    @staticmethod
    def _pick_preferred_port(ports: list[str], preferred_suffixes: list[str]) -> str | None:
        for suffix in preferred_suffixes:
            for port in ports:
                if port.endswith(suffix):
                    return port
        return None

    @staticmethod
    def _port_suffixes_for_channel_index(channel_index: int) -> list[str]:
        surround_names = [
            "FL",
            "FR",
            "RL",
            "RR",
            "FC",
            "LFE",
            "SL",
            "SR",
            "AUX0",
            "AUX1",
            "AUX2",
            "AUX3",
            "AUX4",
            "AUX5",
        ]
        aux_name = f"AUX{channel_index}"
        names = []
        if 0 <= channel_index < len(surround_names):
            names.append(surround_names[channel_index])
        names.append(aux_name)
        suffixes = []
        for name in dict.fromkeys(names):
            suffixes.extend([f":capture_{name}", f":output_{name}", f":monitor_{name}"])
        return suffixes

    @staticmethod
    def _record_input_suffixes_for_channel_index(channel_index: int) -> list[str]:
        surround_names = [
            "FL",
            "FR",
            "RL",
            "RR",
            "FC",
            "LFE",
            "SL",
            "SR",
            "AUX0",
            "AUX1",
            "AUX2",
            "AUX3",
            "AUX4",
            "AUX5",
        ]
        aux_name = f"AUX{channel_index}"
        names = []
        if 0 <= channel_index < len(surround_names):
            names.append(surround_names[channel_index])
        names.append(aux_name)
        return [f":input_{name}" for name in dict.fromkeys(names)]
