# SPDX-License-Identifier: AGPL-3.0-only

"""EasyEffects preset support for native and Flatpak installs."""

import configparser
import json
import logging
import os
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EasyEffectsRuntime:
    mode: Literal["native", "flatpak"]
    socket_path: Path
    socket_candidates: List[Path]
    output_dir: Path
    irs_dir: Path
    db_file: Path
    global_extras_file: Path
    compare_state_file: Path
    cli_command: List[str]
    native_available: bool
    flatpak_available: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "socket": str(self.socket_path),
            "socket_candidates": [str(path) for path in self.socket_candidates],
            "output": str(self.output_dir),
            "irs": str(self.irs_dir),
            "db": str(self.db_file),
            "global_extras": str(self.global_extras_file),
            "compare_state": str(self.compare_state_file),
            "cli_command": list(self.cli_command),
            "native_available": self.native_available,
            "flatpak_available": self.flatpak_available,
        }


class EasyEffectsManager:
    PURE_PRESET = "Direct"
    PROTECTED_PRESETS = {"Direct", "Neutral"}
    EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}

    LIMITER_DEFAULTS = {
        "enabled": True,
        "params": {
            "thresholdDb": -1.0,
            "attackMs": 5.0,
            "releaseMs": 50.0,
            "lookaheadMs": 5.0,
            "stereoLinkPercent": 100.0,
        },
    }

    BASS_ENHANCER_DEFAULTS = {
        "enabled": False,
        "params": {
            "amount": 0.0,
            "harmonics": 8.5,
            "scope": 100.0,
            "blend": 0.0,
        },
    }

    DELAY_DEFAULTS = {
        "enabled": False,
        "params": {
            "leftMs": 0.0,
            "rightMs": 0.0,
        },
    }

    HEADROOM_DEFAULTS = {
        "enabled": False,
        "params": {
            "gainDb": -3.0,
        },
    }

    def __init__(self, home: Optional[Path] = None):
        self.home = Path(home or Path.home())
        self.runtime = self._detect_runtime()
        self.base_dir = self.runtime.output_dir.parents[1]
        self.output_dir = self.runtime.output_dir
        self.irs_dir = self.runtime.irs_dir
        self.db_file = self.runtime.db_file
        self.global_extras_file = self.runtime.global_extras_file
        self.compare_state_file = self.runtime.compare_state_file

    def _runtime_dir(self) -> Path:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if runtime_dir:
            return Path(runtime_dir)
        return Path(f"/run/user/{os.getuid()}")

    def _has_flatpak_install(self) -> bool:
        if not shutil.which("flatpak"):
            return False
        result = subprocess.run(
            ["flatpak", "info", "com.github.wwmm.easyeffects"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def _build_runtime(
        self,
        mode: Literal["native", "flatpak"],
        *,
        native_available: Optional[bool] = None,
        flatpak_available: Optional[bool] = None,
    ) -> EasyEffectsRuntime:
        native_available = bool(shutil.which("easyeffects")) if native_available is None else native_available
        flatpak_available = self._has_flatpak_install() if flatpak_available is None else flatpak_available
        runtime_dir = self._runtime_dir()
        flatpak_runtime = runtime_dir / ".flatpak/com.github.wwmm.easyeffects/xdg-run/EasyEffectsServer"
        flatpak_tmp_runtime = runtime_dir / ".flatpak/com.github.wwmm.easyeffects/tmp/EasyEffectsServer"
        host_runtime = runtime_dir / "EasyEffectsServer"

        if mode == "flatpak":
            base_dir = self.home / ".var/app/com.github.wwmm.easyeffects"
            socket_candidates = [flatpak_runtime, flatpak_tmp_runtime, host_runtime]
            cli_command = ["flatpak", "run", "--command=easyeffects", "com.github.wwmm.easyeffects"]
            output_dir = base_dir / "data/easyeffects/output"
            irs_dir = base_dir / "data/easyeffects/irs"
            db_file = base_dir / "config/easyeffects/db/easyeffectsrc"
            global_extras_file = base_dir / "config/easyeffects/agent-output-extras.json"
            compare_state_file = base_dir / "config/easyeffects/agent-compare-state.json"
        else:
            config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (self.home / ".config"))
            data_root = Path(os.environ.get("XDG_DATA_HOME") or (self.home / ".local/share"))
            socket_candidates = [host_runtime, flatpak_runtime, flatpak_tmp_runtime]
            cli_command = ["easyeffects"]
            output_dir = data_root / "easyeffects/output"
            irs_dir = data_root / "easyeffects/irs"
            db_file = config_root / "easyeffects/db/easyeffectsrc"
            global_extras_file = config_root / "easyeffects/agent-output-extras.json"
            compare_state_file = config_root / "easyeffects/agent-compare-state.json"

        unique_candidates = []
        seen = set()
        for path in socket_candidates:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            unique_candidates.append(path)

        socket_path = next((candidate for candidate in unique_candidates if candidate.exists()), unique_candidates[0])
        return EasyEffectsRuntime(
            mode=mode,
            socket_path=socket_path,
            socket_candidates=unique_candidates,
            output_dir=output_dir,
            irs_dir=irs_dir,
            db_file=db_file,
            global_extras_file=global_extras_file,
            compare_state_file=compare_state_file,
            cli_command=cli_command,
            native_available=native_available,
            flatpak_available=flatpak_available,
        )

    def _runtime_score(self, runtime: EasyEffectsRuntime) -> int:
        return sum(1 for path in [runtime.socket_path, runtime.output_dir, runtime.irs_dir, runtime.db_file] if path.exists())

    def _detect_runtime(self) -> EasyEffectsRuntime:
        native_available = bool(shutil.which("easyeffects"))
        flatpak_available = self._has_flatpak_install()
        native_runtime = self._build_runtime("native", native_available=native_available, flatpak_available=flatpak_available)
        flatpak_runtime = self._build_runtime("flatpak", native_available=native_available, flatpak_available=flatpak_available)

        if flatpak_runtime.socket_path.exists() and not native_runtime.socket_path.exists():
            selected = flatpak_runtime
        elif native_runtime.socket_path.exists() and not flatpak_runtime.socket_path.exists():
            selected = native_runtime
        elif native_runtime.socket_path.exists() and flatpak_runtime.socket_path.exists():
            selected = native_runtime if native_available else flatpak_runtime
            logger.warning("Both native and Flatpak EasyEffects sockets are present, selecting %s mode", selected.mode)
        else:
            native_score = self._runtime_score(native_runtime)
            flatpak_score = self._runtime_score(flatpak_runtime)
            if native_score > flatpak_score:
                selected = native_runtime
            elif flatpak_score > native_score:
                selected = flatpak_runtime
            elif native_available:
                selected = native_runtime
            elif flatpak_available:
                selected = flatpak_runtime
            else:
                selected = flatpak_runtime

        selected = EasyEffectsRuntime(
            mode=selected.mode,
            socket_path=selected.socket_path,
            socket_candidates=selected.socket_candidates,
            output_dir=selected.output_dir,
            irs_dir=selected.irs_dir,
            db_file=selected.db_file,
            global_extras_file=selected.global_extras_file,
            compare_state_file=selected.compare_state_file,
            cli_command=selected.cli_command,
            native_available=native_available,
            flatpak_available=flatpak_available,
        )
        logger.info("EasyEffects runtime selected: %s", json.dumps(selected.as_dict(), sort_keys=True))
        return selected

    def _socket_candidates(self) -> List[Path]:
        return list(self.runtime.socket_candidates)

    def _socket_path(self) -> Path:
        return self.runtime.socket_path

    def _send_socket_command(self, command: str, timeout: float = 2.0) -> str:
        last_error = None
        for socket_path in self._socket_candidates():
            if not socket_path.exists():
                continue
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(timeout)
                    client.connect(str(socket_path))
                    client.sendall((command + "\n").encode())
                    try:
                        response = client.recv(4096)
                    except socket.timeout:
                        return ""
                return response.decode(errors="ignore").strip()
            except Exception as e:
                last_error = e
                logger.warning("EasyEffects socket command failed via %s: %s", socket_path, e)
        if last_error:
            raise RuntimeError(f"EasyEffects socket command failed: {last_error}")
        raise FileNotFoundError(
            "EasyEffects control socket not found in any candidate path: "
            + ", ".join(str(path) for path in self._socket_candidates())
        )

    def list_presets(self) -> List[dict]:
        if not self.output_dir.exists():
            return []

        self._ensure_pure_preset_exists()

        pinned_order = {
            "Direct": 0,
            "Neutral": 1,
            self.PURE_PRESET: 2,
        }
        preset_paths = sorted(
            self.output_dir.glob("*.json"),
            key=lambda path: (pinned_order.get(path.stem, 100), path.stem.lower()),
        )

        presets = []
        for path in preset_paths:
            presets.append(
                {
                    "name": path.stem,
                    "filename": path.name,
                    "path": str(path),
                }
            )
        return presets

    def _get_active_preset_from_db(self) -> Optional[str]:
        if not self.db_file.exists():
            return None

        parser = configparser.ConfigParser()
        try:
            parser.read(self.db_file)
            for section in parser.sections():
                if parser.has_option(section, "lastLoadedOutputPreset"):
                    value = parser.get(section, "lastLoadedOutputPreset").strip()
                    return value or None
        except Exception as e:
            logger.warning(f"Failed to read EasyEffects db file: {e}")

        try:
            for line in self.db_file.read_text().splitlines():
                if line.startswith("lastLoadedOutputPreset="):
                    value = line.split("=", 1)[1].strip()
                    return value or None
        except Exception as e:
            logger.warning(f"Failed fallback read of EasyEffects db file: {e}")

        return None

    def get_active_preset(self) -> Optional[str]:
        try:
            response = self._send_socket_command("get_last_loaded_preset:output", timeout=2.0)
            if response:
                return response
        except Exception as e:
            logger.warning("Failed to get active EasyEffects preset via socket, falling back to db file: %s", e)

        return self._get_active_preset_from_db()

    def load_preset(self, preset_name: str) -> None:
        available = {preset["name"] for preset in self.list_presets()}
        if preset_name not in available:
            raise FileNotFoundError(f"Preset not found: {preset_name}")

        if preset_name not in self.EXCLUDED_GLOBAL_EXTRAS_PRESETS:
            try:
                self._apply_global_extras_to_preset_name(preset_name, self.load_global_extras())
            except Exception as e:
                logger.warning("Failed to sync global extras into preset '%s' before load: %s", preset_name, e)

        socket_command = f"load_preset:output:{preset_name}"
        try:
            response = self._send_socket_command(socket_command, timeout=2.0)
            active_after_load = self.get_active_preset()
            if active_after_load == preset_name:
                logger.info(
                    "Loaded EasyEffects preset via control socket: %s (response=%r, verified_active=%r)",
                    preset_name,
                    response,
                    active_after_load,
                )
                return
            logger.warning(
                "EasyEffects socket load returned without verification, falling back to CLI: requested=%s response=%r verified_active=%r",
                preset_name,
                response,
                active_after_load,
            )
        except Exception as socket_error:
            logger.warning("EasyEffects socket preset load failed, falling back to CLI: %s", socket_error)

        cmd = [*self.runtime.cli_command, "--load-preset", preset_name]

        env = os.environ.copy()
        if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
            env.setdefault("QT_QPA_PLATFORM", "offscreen")

        logger.info(
            "Loading EasyEffects preset via %s CLI fallback: %s (cmd=%s DISPLAY=%s, WAYLAND_DISPLAY=%s, QT_QPA_PLATFORM=%s)",
            self.runtime.mode,
            preset_name,
            cmd,
            bool(env.get("DISPLAY")),
            bool(env.get("WAYLAND_DISPLAY")),
            env.get("QT_QPA_PLATFORM"),
        )
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "Unknown error").strip()
            if "could not connect to display" in stderr.lower() or "qt.qpa.plugin" in stderr.lower():
                raise RuntimeError(
                    "EasyEffects preset load failed: no graphical session available for EasyEffects and socket control was unavailable. "
                    f"Raw error: {stderr}"
                )
            raise RuntimeError(f"EasyEffects preset load failed: {stderr}")

        active_after_cli = self.get_active_preset()
        if active_after_cli and active_after_cli != preset_name:
            logger.warning(
                "EasyEffects CLI load completed but active preset differs: requested=%s active=%s",
                preset_name,
                active_after_cli,
            )

    def list_irs(self) -> List[dict]:
        if not self.irs_dir.exists():
            return []

        irs = []
        for path in sorted(self.irs_dir.iterdir()):
            if not path.is_file():
                continue
            irs.append(
                {
                    "name": path.name,
                    "basename": path.stem,
                    "path": str(path),
                    "size": path.stat().st_size,
                }
            )
        return irs

    def _read_preset_payload(self, preset_name: str) -> Optional[Dict[str, Any]]:
        clean_name = Path(preset_name).stem.strip()
        if not clean_name:
            return None

        preset_path = self.output_dir / f"{clean_name}.json"
        if not preset_path.exists():
            return None

        try:
            payload = json.loads(preset_path.read_text())
            return payload if isinstance(payload, dict) else None
        except Exception as e:
            logger.warning("Failed to parse EasyEffects preset '%s' for IR reference scan: %s", clean_name, e)
            return None

    def _extract_kernel_names_from_payload(self, payload: Optional[Dict[str, Any]]) -> Set[str]:
        if not isinstance(payload, dict):
            return set()

        output = payload.get("output")
        if not isinstance(output, dict):
            return set()

        kernel_names: Set[str] = set()
        for plugin_payload in output.values():
            if not isinstance(plugin_payload, dict):
                continue
            kernel_name = plugin_payload.get("kernel-name")
            if isinstance(kernel_name, str):
                normalized = kernel_name.strip()
                if normalized:
                    kernel_names.add(normalized)
        return kernel_names

    def _get_preset_kernel_names(self, preset_name: str) -> Set[str]:
        return self._extract_kernel_names_from_payload(self._read_preset_payload(preset_name))

    def _get_other_referenced_kernel_names(self, excluded_preset_name: str) -> Set[str]:
        excluded_clean = Path(excluded_preset_name).stem.strip()
        referenced: Set[str] = set()
        for preset in self.list_presets():
            preset_name = preset.get("name")
            if not isinstance(preset_name, str) or preset_name == excluded_clean:
                continue
            referenced.update(self._get_preset_kernel_names(preset_name))
        return referenced

    def _find_ir_paths_for_kernel_name(self, kernel_name: str) -> List[Path]:
        if not kernel_name or not self.irs_dir.exists():
            return []

        preferred_path = self.irs_dir / f"{Path(kernel_name).stem}.irs"
        if preferred_path.exists() and preferred_path.is_file():
            return [preferred_path]

        matching_paths: List[Path] = []
        for path in self.irs_dir.iterdir():
            if path.is_file() and path.stem == kernel_name:
                matching_paths.append(path)
        return sorted(matching_paths)

    def _convert_wav_to_irs(self, source_path: Path, destination: Path) -> None:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-f",
            "wav",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_f32le",
            str(destination),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "Unknown ffmpeg error").strip()
            raise RuntimeError(f"IR conversion failed: {stderr}")

    def upload_ir(self, source_path: Path, filename: str, stored_name: Optional[str] = None) -> dict:
        self.irs_dir.mkdir(parents=True, exist_ok=True)
        source_safe_name = Path(filename).name
        if not source_safe_name:
            raise ValueError("Invalid filename")

        suffix = Path(source_safe_name).suffix.lower()
        if suffix not in {".irs", ".wav"}:
            raise ValueError("Unsupported IR file type. Please upload .irs or .wav")

        target_name = Path(stored_name).name if stored_name else source_safe_name
        if not target_name:
            raise ValueError("Invalid stored IR filename")
        destination = self.irs_dir / f"{Path(target_name).stem}.irs"

        if suffix == ".wav":
            self._convert_wav_to_irs(source_path, destination)
            stored_format = "irs"
        else:
            shutil.copyfile(source_path, destination)
            stored_format = "irs"

        return {
            "name": destination.name,
            "basename": destination.stem,
            "path": str(destination),
            "size": destination.stat().st_size,
            "format": stored_format,
        }

    def _merge_ir_pair_to_irs(self, left_path: Path, right_path: Path, destination: Path) -> None:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(left_path),
            "-i",
            str(right_path),
            "-filter_complex",
            "[0:a]pan=mono|c0=c0[left];[1:a]pan=mono|c0=c0[right];[left][right]join=inputs=2:channel_layout=stereo[aout]",
            "-map",
            "[aout]",
            "-f",
            "wav",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_f32le",
            str(destination),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "Unknown ffmpeg error").strip()
            raise RuntimeError(f"Dual IR merge failed: {stderr}")

    def upload_ir_pair(self, left_source_path: Path, left_filename: str, right_source_path: Path, right_filename: str, merged_name: str) -> dict:
        self.irs_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(merged_name).name
        if not safe_name:
            raise ValueError("Invalid merged IR filename")

        left_suffix = Path(left_filename).suffix.lower()
        right_suffix = Path(right_filename).suffix.lower()
        valid_suffixes = {".irs", ".wav"}
        if left_suffix not in valid_suffixes or right_suffix not in valid_suffixes:
            raise ValueError("Dual convolver import supports only .irs or .wav on both sides")

        destination = self.irs_dir / f"{Path(safe_name).stem}.irs"
        self._merge_ir_pair_to_irs(left_source_path, right_source_path, destination)
        return {
            "name": destination.name,
            "basename": destination.stem,
            "path": str(destination),
            "size": destination.stat().st_size,
            "format": "irs",
        }

    def _normalize_limiter_v1(self, limiter_definition: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if limiter_definition is None:
            limiter_definition = self.LIMITER_DEFAULTS
        if not isinstance(limiter_definition, dict):
            raise ValueError("limiter must be an object")

        params = limiter_definition.get("params") if isinstance(limiter_definition.get("params"), dict) else {}
        threshold = float(params.get("thresholdDb", self.LIMITER_DEFAULTS["params"]["thresholdDb"]))
        attack = float(params.get("attackMs", self.LIMITER_DEFAULTS["params"]["attackMs"]))
        release = float(params.get("releaseMs", self.LIMITER_DEFAULTS["params"]["releaseMs"]))
        lookahead = float(params.get("lookaheadMs", self.LIMITER_DEFAULTS["params"]["lookaheadMs"]))
        stereo_link = float(params.get("stereoLinkPercent", self.LIMITER_DEFAULTS["params"]["stereoLinkPercent"]))

        if not -24.0 <= threshold <= 0.0:
            raise ValueError("limiter thresholdDb must be between -24 and 0")
        if not 0.1 <= attack <= 100.0:
            raise ValueError("limiter attackMs must be between 0.1 and 100")
        if not 1.0 <= release <= 1000.0:
            raise ValueError("limiter releaseMs must be between 1 and 1000")
        if not 0.0 <= lookahead <= 20.0:
            raise ValueError("limiter lookaheadMs must be between 0 and 20")
        if not 0.0 <= stereo_link <= 100.0:
            raise ValueError("limiter stereoLinkPercent must be between 0 and 100")

        return {
            "enabled": bool(limiter_definition.get("enabled", True)),
            "params": {
                "thresholdDb": threshold,
                "attackMs": attack,
                "releaseMs": release,
                "lookaheadMs": lookahead,
                "stereoLinkPercent": stereo_link,
            },
        }

    def _normalize_bass_enhancer_v1(self, bass_enh_definition: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if bass_enh_definition is None:
            bass_enh_definition = self.BASS_ENHANCER_DEFAULTS
        if not isinstance(bass_enh_definition, dict):
            raise ValueError("bass_enhancer must be an object")
        params = bass_enh_definition.get("params") if isinstance(bass_enh_definition.get("params"), dict) else {}
        return {
            "enabled": bool(bass_enh_definition.get("enabled", False)),
            "params": {
                "amount": max(-20.0, min(float(params.get("amount", 0.0)), 20.0)),
                "harmonics": max(1.0, min(float(params.get("harmonics", 8.5)), 20.0)),
                "scope": max(20.0, min(float(params.get("scope", 100.0)), 500.0)),
                "blend": max(-100.0, min(float(params.get("blend", 0.0)), 100.0)),
            },
        }

    def _normalize_delay_v1(self, delay_definition: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if delay_definition is None:
            delay_definition = self.DELAY_DEFAULTS
        if not isinstance(delay_definition, dict):
            raise ValueError("delay must be an object")

        params = delay_definition.get("params") if isinstance(delay_definition.get("params"), dict) else {}
        left_ms = float(params.get("leftMs", self.DELAY_DEFAULTS["params"]["leftMs"]))
        right_ms = float(params.get("rightMs", self.DELAY_DEFAULTS["params"]["rightMs"]))

        if not 0.0 <= left_ms <= 500.0:
            raise ValueError("delay leftMs must be between 0 and 500")
        if not 0.0 <= right_ms <= 500.0:
            raise ValueError("delay rightMs must be between 0 and 500")

        return {
            "enabled": bool(delay_definition.get("enabled", False)),
            "params": {
                "leftMs": left_ms,
                "rightMs": right_ms,
            },
        }

    def _normalize_headroom_v1(self, headroom_definition: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if headroom_definition is None:
            headroom_definition = self.HEADROOM_DEFAULTS
        if not isinstance(headroom_definition, dict):
            raise ValueError("headroom must be an object")
        params = headroom_definition.get("params") if isinstance(headroom_definition.get("params"), dict) else {}
        gain_db = float(params.get("gainDb", self.HEADROOM_DEFAULTS["params"]["gainDb"]))
        if not float(gain_db).is_integer():
            raise ValueError("headroom.params.gainDb must be a whole dB value")
        gain_db = int(gain_db)
        if gain_db < -9 or gain_db > 0:
            raise ValueError("headroom.params.gainDb must be between -9 and 0")
        return {
            "enabled": bool(headroom_definition.get("enabled", False)),
            "params": {
                "gainDb": float(gain_db),
            },
        }

    def normalize_effects_extras(self, extras: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        extras = extras or {}
        return {
            "limiter": self._normalize_limiter_v1(extras.get("limiter")) if extras.get("limiter", {}).get("enabled") else {
                "enabled": False,
                "params": dict(self.LIMITER_DEFAULTS["params"]),
            },
            "headroom": self._normalize_headroom_v1(extras.get("headroom")) if extras.get("headroom") else self._normalize_headroom_v1(None),
            "delay": self._normalize_delay_v1(extras.get("delay")) if extras.get("delay") else self._normalize_delay_v1(None),
            "bass_enhancer": self._normalize_bass_enhancer_v1(extras.get("bass_enhancer")) if extras.get("bass_enhancer") else self._normalize_bass_enhancer_v1(None),
        }

    def load_global_extras(self) -> Dict[str, Any]:
        if not self.global_extras_file.exists():
            return self.normalize_effects_extras(None)
        try:
            payload = json.loads(self.global_extras_file.read_text())
        except Exception as e:
            logger.warning("Failed to read global extras config, using defaults: %s", e)
            return self.normalize_effects_extras(None)
        return self.normalize_effects_extras(payload)

    def save_global_extras(self, extras: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized = self.normalize_effects_extras(extras)
        self.global_extras_file.parent.mkdir(parents=True, exist_ok=True)
        self.global_extras_file.write_text(json.dumps(normalized, indent=2) + "\n")
        return normalized

    def normalize_compare_state(self, compare: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        compare = compare or {}
        available = {preset["name"] for preset in self.list_presets()}
        preset_a = compare.get("presetA") if isinstance(compare.get("presetA"), str) else ""
        preset_b = compare.get("presetB") if isinstance(compare.get("presetB"), str) else ""
        active_side = compare.get("activeSide") if compare.get("activeSide") in {"A", "B"} else None
        active_preset = self.get_active_preset()

        if preset_a not in available:
            preset_a = ""
        if preset_b not in available:
            preset_b = ""
        if preset_a and preset_b and preset_a == preset_b:
            preset_b = ""
            if active_side == "B":
                active_side = "A" if active_preset == preset_a else None

        if not preset_a and active_preset in available:
            preset_a = active_preset

        inferred_active_side = None
        if active_preset and preset_a == active_preset:
            inferred_active_side = "A"
        elif active_preset and preset_b == active_preset:
            inferred_active_side = "B"

        if inferred_active_side is not None:
            active_side = inferred_active_side

        return {
            "presetA": preset_a,
            "presetB": preset_b,
            "activeSide": active_side,
        }

    def load_compare_state(self) -> Dict[str, Any]:
        if not self.compare_state_file.exists():
            return self.normalize_compare_state(None)
        try:
            payload = json.loads(self.compare_state_file.read_text())
        except Exception as e:
            logger.warning("Failed to read compare state config, using defaults: %s", e)
            return self.normalize_compare_state(None)
        return self.normalize_compare_state(payload)

    def save_compare_state(self, compare: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized = self.normalize_compare_state(compare)
        self.compare_state_file.parent.mkdir(parents=True, exist_ok=True)
        self.compare_state_file.write_text(json.dumps(normalized, indent=2) + "\n")
        return normalized

    def _delay_plugin_payload(self, delay: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "bypass": not delay.get("enabled", False),
            "centimeters-l": 0.0,
            "centimeters-r": 0.0,
            "dry-l": -80.01,
            "dry-r": -80.01,
            "input-gain": 0.0,
            "invert-phase-l": False,
            "invert-phase-r": False,
            "meters-l": 0.0,
            "meters-r": 0.0,
            "mode-l": "Time",
            "mode-r": "Time",
            "output-gain": 0.0,
            "sample-l": 0.0,
            "sample-r": 0.0,
            "temperature-l": 20.0,
            "temperature-r": 20.0,
            "time-l": delay["params"]["leftMs"],
            "time-r": delay["params"]["rightMs"],
            "wet-l": 0.0,
            "wet-r": 0.0,
        }

    def _bass_enhancer_plugin_payload(self, bass_enh: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "amount": bass_enh["params"]["amount"],
            "blend": bass_enh["params"]["blend"],
            "bypass": not bass_enh.get("enabled", False),
            "floor": 20.0,
            "floor-active": False,
            "harmonics": bass_enh["params"]["harmonics"],
            "input-gain": 0.0,
            "output-gain": 0.0,
            "scope": bass_enh["params"]["scope"],
        }

    def _limiter_plugin_payload(self, limiter: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "alr": False,
            "alr-attack": 5.0,
            "alr-knee": 0.0,
            "alr-knee-smooth": -5.0,
            "alr-release": 50.0,
            "attack": limiter["params"]["attackMs"],
            "bypass": not limiter.get("enabled", False),
            "dithering": "None",
            "gain-boost": True,
            "input-gain": 0.0,
            "input-to-link": -80.01,
            "input-to-sidechain": -80.01,
            "link-to-input": -80.01,
            "link-to-sidechain": -80.01,
            "lookahead": limiter["params"]["lookaheadMs"],
            "mode": "Herm Thin",
            "output-gain": 0.0,
            "oversampling": "None",
            "release": limiter["params"]["releaseMs"],
            "sidechain-preamp": 0.0,
            "sidechain-to-input": -80.01,
            "sidechain-to-link": -80.01,
            "sidechain-type": "Internal",
            "stereo-link": limiter["params"]["stereoLinkPercent"],
            "threshold": limiter["params"]["thresholdDb"],
        }

    def _apply_extras_to_output(self, output: Dict[str, Any], extras: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized = self.normalize_effects_extras(extras)
        result = dict(output or {})
        helper_plugin_names = ["delay#0", "bass_enhancer#0", "limiter#0"]
        plugins_order = list(result.get("plugins_order", []))

        result["delay#0"] = self._delay_plugin_payload(normalized["delay"])
        result["bass_enhancer#0"] = self._bass_enhancer_plugin_payload(normalized["bass_enhancer"])
        result["limiter#0"] = self._limiter_plugin_payload(normalized["limiter"])

        plugins_order = [entry for entry in plugins_order if entry not in helper_plugin_names]
        plugins_order.extend(helper_plugin_names)

        target_plugin = None

        def is_helper_plugin(plugin_name: str) -> bool:
            return plugin_name in {"limiter#0", "bass_enhancer#0"} or plugin_name.startswith("delay#")

        for plugin_name in plugins_order:
            if is_helper_plugin(plugin_name):
                continue
            plugin_payload = result.get(plugin_name)
            if isinstance(plugin_payload, dict):
                target_plugin = plugin_payload
                break
        if target_plugin is None:
            for plugin_name, plugin_payload in result.items():
                if is_helper_plugin(plugin_name):
                    continue
                if "#" in plugin_name and isinstance(plugin_payload, dict):
                    target_plugin = plugin_payload
                    break
        for plugin_name in plugins_order:
            if is_helper_plugin(plugin_name):
                continue
            plugin_payload = result.get(plugin_name)
            if isinstance(plugin_payload, dict) and "output-gain" in plugin_payload:
                plugin_payload["output-gain"] = 0.0

        if isinstance(target_plugin, dict) and "output-gain" in target_plugin:
            target_plugin["output-gain"] = normalized["headroom"]["params"]["gainDb"] if normalized["headroom"].get("enabled") else 0.0

        result["plugins_order"] = plugins_order
        return result

    def _build_effects_output(self, base_plugins: Dict[str, Any], base_order: List[str], extras: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        output = {
            "blocklist": [],
            **base_plugins,
            "plugins_order": list(base_order),
        }
        output = self._apply_extras_to_output(output, extras)
        return {"output": output}

    def _apply_global_extras_to_preset_name(self, preset_name: str, extras: Optional[Dict[str, Any]] = None) -> bool:
        if not preset_name or preset_name in self.EXCLUDED_GLOBAL_EXTRAS_PRESETS:
            return False

        preset_path = self.output_dir / f"{preset_name}.json"
        if not preset_path.exists():
            return False

        normalized = self.normalize_effects_extras(extras if extras is not None else self.load_global_extras())
        payload = json.loads(preset_path.read_text())
        payload["output"] = self._apply_extras_to_output(payload.get("output", {}), normalized)
        preset_path.write_text(json.dumps(payload, indent=2) + "\n")
        return True

    def apply_global_extras_to_all_presets(self, extras: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized = self.save_global_extras(extras)
        updated = 0
        skipped = []
        for preset in self.list_presets():
            preset_name = preset.get("name")
            if not preset_name:
                continue
            if preset_name in self.EXCLUDED_GLOBAL_EXTRAS_PRESETS:
                skipped.append(preset_name)
                continue
            if self._apply_global_extras_to_preset_name(preset_name, normalized):
                updated += 1
            else:
                skipped.append(preset_name)
        return {"extras": normalized, "updated": updated, "skipped": sorted(set(skipped))}

    def apply_global_extras_to_active_preset(self, extras: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized = self.save_global_extras(extras)
        active_preset = self.get_active_preset()
        if not active_preset:
            return {"extras": normalized, "updated": 0, "skipped": []}
        if active_preset in self.EXCLUDED_GLOBAL_EXTRAS_PRESETS:
            return {"extras": normalized, "updated": 0, "skipped": [active_preset]}
        if not self._apply_global_extras_to_preset_name(active_preset, normalized):
            return {"extras": normalized, "updated": 0, "skipped": [active_preset]}
        return {"extras": normalized, "updated": 1, "skipped": []}

    def _read_preset_payload(self, preset_name: str) -> Dict[str, Any]:
        clean_name = Path(preset_name).stem.strip()
        if not clean_name:
            raise ValueError("Invalid preset name")
        preset_path = self.output_dir / f"{clean_name}.json"
        if not preset_path.exists():
            raise FileNotFoundError(f"Preset not found: {clean_name}")
        try:
            payload = json.loads(preset_path.read_text())
        except Exception as e:
            raise RuntimeError(f"Failed to read preset '{clean_name}': {e}") from e
        if not isinstance(payload, dict):
            raise RuntimeError(f"Preset '{clean_name}' is not a valid JSON object")
        return payload

    def combine_presets(self, preset_name: str, source_presets: List[str], extras: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)

        clean_preset_name = Path(preset_name).stem.strip()
        if not clean_preset_name:
            raise ValueError("Invalid preset name")
        if not isinstance(source_presets, list):
            raise ValueError("source_presets must be an array")

        normalized_sources = [Path(str(name)).stem.strip() for name in source_presets if str(name).strip()]
        if len(normalized_sources) < 2:
            raise ValueError("Select at least two presets to combine")
        if len(set(normalized_sources)) != len(normalized_sources):
            raise ValueError("Selected presets must be different")
        if clean_preset_name in set(normalized_sources):
            raise ValueError("New preset name must differ from the source presets")

        helper_bases = {"limiter", "delay", "bass_enhancer"}
        base_plugins: Dict[str, Any] = {}
        base_order: List[str] = []
        plugin_counters: Dict[str, int] = {}

        for source_name in normalized_sources:
            payload = self._read_preset_payload(source_name)
            output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
            ordered_plugin_names = []
            seen_plugin_names = set()

            for plugin_name in output.get("plugins_order", []):
                if isinstance(plugin_name, str) and plugin_name in output and plugin_name not in seen_plugin_names:
                    ordered_plugin_names.append(plugin_name)
                    seen_plugin_names.add(plugin_name)

            for plugin_name, plugin_payload in output.items():
                if plugin_name in {"plugins_order", "blocklist"} or not isinstance(plugin_payload, dict):
                    continue
                if plugin_name not in seen_plugin_names and "#" in plugin_name:
                    ordered_plugin_names.append(plugin_name)
                    seen_plugin_names.add(plugin_name)

            for plugin_name in ordered_plugin_names:
                plugin_payload = output.get(plugin_name)
                if not isinstance(plugin_payload, dict):
                    continue
                plugin_base = plugin_name.split("#", 1)[0]
                if plugin_base in helper_bases:
                    continue
                plugin_index = plugin_counters.get(plugin_base, 0)
                plugin_counters[plugin_base] = plugin_index + 1
                combined_name = f"{plugin_base}#{plugin_index}"
                combined_payload = json.loads(json.dumps(plugin_payload))
                if "output-gain" in combined_payload:
                    combined_payload["output-gain"] = 0.0
                base_plugins[combined_name] = combined_payload
                base_order.append(combined_name)

        combined_payload = self._build_effects_output(base_plugins, base_order, extras if extras is not None else self.load_global_extras())
        preset_path = self.output_dir / f"{clean_preset_name}.json"
        preset_path.write_text(json.dumps(combined_payload, indent=2) + "\n")
        return {
            "name": clean_preset_name,
            "filename": preset_path.name,
            "path": str(preset_path),
            "source_presets": normalized_sources,
            "plugin_count": len(base_order),
        }

    def create_convolver_preset(self, preset_name: str, ir_filename: str, extras: Optional[Dict[str, Any]] = None) -> dict:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        available_irs = {entry["name"]: entry for entry in self.list_irs()}
        if ir_filename not in available_irs:
            raise FileNotFoundError(f"IR file not found: {ir_filename}")

        clean_preset_name = Path(preset_name).stem.strip()
        if not clean_preset_name:
            raise ValueError("Invalid preset name")

        kernel_name = Path(ir_filename).stem
        preset_path = self.output_dir / f"{clean_preset_name}.json"
        payload = self._build_effects_output(
            {
                "convolver#0": {
                    "autogain": False,
                    "bypass": False,
                    "dry": -100.0,
                    "input-gain": 0.0,
                    "ir-width": 100,
                    "kernel-name": kernel_name,
                    "output-gain": 0.0,
                    "sofa": {
                        "azimuth": 0.0,
                        "elevation": 0.0,
                        "radius": 1.0,
                    },
                    "wet": 0.0,
                },
            },
            ["convolver#0"],
            extras,
        )
        preset_path.write_text(json.dumps(payload, indent=2) + "\n")
        return {
            "name": clean_preset_name,
            "filename": preset_path.name,
            "path": str(preset_path),
            "kernel_name": kernel_name,
        }

    def _normalize_peq_band_list(self, bands: Any, field_path: str) -> List[Dict[str, Any]]:
        allowed_filter_types = {
            "bell": "Bell",
            "gain": None,
            "low_shelf": "Lo-shelf",
            "high_shelf": "Hi-shelf",
            "low_pass": "Lo-pass",
            "high_pass": "Hi-pass",
        }

        if not isinstance(bands, list):
            raise ValueError(f"{field_path} must be an array")
        if len(bands) > 20:
            raise ValueError(f"{field_path} supports at most 20 bands in v1")

        normalized_bands = []
        for index, band in enumerate(bands):
            if not isinstance(band, dict):
                raise ValueError(f"{field_path}[{index}] must be an object")

            try:
                frequency_hz = float(band["frequencyHz"])
                gain_db = float(band["gainDb"])
                q_value = float(band["q"])
            except KeyError as e:
                raise ValueError(f"{field_path}[{index}] missing required field: {e.args[0]}") from e
            except (TypeError, ValueError) as e:
                raise ValueError(f"{field_path}[{index}] has invalid numeric value") from e

            filter_type = str(band.get("filterType", "")).strip().lower()
            if filter_type not in allowed_filter_types:
                raise ValueError(
                    f"{field_path}[{index}].filterType must be one of: {', '.join(sorted(allowed_filter_types))}"
                )

            if not 20.0 <= frequency_hz <= 20000.0:
                raise ValueError(f"{field_path}[{index}].frequencyHz must be between 20 and 20000")
            if not -24.0 <= gain_db <= 24.0:
                raise ValueError(f"{field_path}[{index}].gainDb must be between -24 and 24")
            if not 0.1 <= q_value <= 20.0:
                raise ValueError(f"{field_path}[{index}].q must be between 0.1 and 20")

            normalized_bands.append(
                {
                    "frequencyHz": frequency_hz,
                    "gainDb": gain_db,
                    "q": q_value,
                    "filterType": filter_type,
                    "easyEffectsType": allowed_filter_types[filter_type],
                    "enabled": bool(band.get("enabled", True)),
                }
            )

        return normalized_bands

    def _split_peq_bands_and_gain(self, bands: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], float]:
        eq_bands: List[Dict[str, Any]] = []
        gain_db_total = 0.0
        for band in bands:
            if band.get("filterType") == "gain":
                if band.get("enabled", True):
                    gain_db_total += float(band.get("gainDb", 0.0))
                continue
            eq_bands.append(band)
        return eq_bands, gain_db_total

    def validate_peq_v1(self, peq_definition: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(peq_definition, dict):
            raise ValueError("peq must be a JSON object")

        params = peq_definition.get("params")
        if not isinstance(params, dict):
            raise ValueError("peq.params must be an object")

        channel_mode = params.get("channelMode") or "stereo-linked"
        mix = peq_definition.get("mix") if isinstance(peq_definition.get("mix"), dict) else {}
        input_gain = float(mix.get("inputGainDb", 0.0))
        output_gain = float(mix.get("outputGainDb", 0.0))

        normalized_params: Dict[str, Any] = {"channelMode": channel_mode}
        if channel_mode == "stereo-linked":
            normalized_params["bands"] = self._normalize_peq_band_list(params.get("bands"), "peq.params.bands")
        elif channel_mode == "dual":
            left_bands = self._normalize_peq_band_list(params.get("leftBands", []), "peq.params.leftBands")
            right_bands = self._normalize_peq_band_list(params.get("rightBands", []), "peq.params.rightBands")
            normalized_params["leftBands"] = left_bands
            normalized_params["rightBands"] = right_bands
        else:
            raise ValueError("peq.params.channelMode currently supports only 'stereo-linked' or 'dual'")

        return {
            "enabled": bool(peq_definition.get("enabled", True)),
            "mix": {
                "inputGainDb": input_gain,
                "outputGainDb": output_gain,
            },
            "params": normalized_params,
        }

    def create_peq_preset(self, preset_name: str, peq_definition: Dict[str, Any], extras: Optional[Dict[str, Any]] = None) -> dict:
        self.output_dir.mkdir(parents=True, exist_ok=True)

        clean_preset_name = Path(preset_name).stem.strip()
        if not clean_preset_name:
            raise ValueError("Invalid preset name")

        normalized = self.validate_peq_v1(peq_definition)
        channel_mode = normalized["params"]["channelMode"]

        def build_channel_bands(bands: List[Dict[str, Any]], slot_count: int) -> Dict[str, Dict[str, Any]]:
            channel = {}
            for index in range(slot_count):
                band = bands[index] if index < len(bands) else None
                channel[f"band{index}"] = {
                    "frequency": band["frequencyHz"] if band else 1000.0,
                    "gain": band["gainDb"] if band else 0.0,
                    "mode": "RLC (BT)",
                    "mute": (not band["enabled"]) if band else True,
                    "q": band["q"] if band else 1.0,
                    "slope": "x1",
                    "solo": False,
                    "type": band["easyEffectsType"] if band else "Bell",
                    "width": 4.0,
                }
            return channel

        if channel_mode == "dual":
            left_source_bands = normalized["params"].get("leftBands", [])
            right_source_bands = normalized["params"].get("rightBands", [])
        else:
            shared_bands = normalized["params"].get("bands", [])
            left_source_bands = shared_bands
            right_source_bands = shared_bands

        left_bands, left_gain_trim_db = self._split_peq_bands_and_gain(left_source_bands)
        right_bands, right_gain_trim_db = self._split_peq_bands_and_gain(right_source_bands)
        num_bands = max(len(left_bands), len(right_bands))

        if channel_mode == "stereo-linked":
            shared_gain_trim_db = left_gain_trim_db
        else:
            if abs(left_gain_trim_db - right_gain_trim_db) <= 1e-9:
                shared_gain_trim_db = left_gain_trim_db
            elif abs(left_gain_trim_db) <= 1e-9:
                shared_gain_trim_db = right_gain_trim_db
            elif abs(right_gain_trim_db) <= 1e-9:
                shared_gain_trim_db = left_gain_trim_db
            else:
                raise ValueError(
                    "Gain filter currently supports only shared stereo trim in dual mode; use the same Gain on both sides or a single shared Gain value"
                )

        needs_neutral_eq = num_bands == 0 and (
            abs(shared_gain_trim_db) > 1e-9
            or abs(normalized["mix"]["inputGainDb"]) > 1e-9
            or abs(normalized["mix"]["outputGainDb"]) > 1e-9
        )
        eq_slot_count = max(num_bands, 1 if needs_neutral_eq else 0)
        left = build_channel_bands(left_bands, eq_slot_count)
        right = build_channel_bands(right_bands, eq_slot_count)

        base_plugins: Dict[str, Any] = {}
        base_order: List[str] = []

        if eq_slot_count > 0:
            base_plugins["equalizer#0"] = {
                "balance": 0.0,
                "bypass": not normalized["enabled"],
                "input-gain": normalized["mix"]["inputGainDb"] + shared_gain_trim_db,
                "left": left,
                "mode": "IIR",
                "num-bands": eq_slot_count,
                "output-gain": normalized["mix"]["outputGainDb"],
                "pitch-left": 0.0,
                "pitch-right": 0.0,
                "right": right,
                "split-channels": channel_mode == "dual",
            }
            base_order.append("equalizer#0")

        preset_path = self.output_dir / f"{clean_preset_name}.json"
        payload = self._build_effects_output(base_plugins, base_order, extras)
        preset_path.write_text(json.dumps(payload, indent=2) + "\n")
        return {
            "name": clean_preset_name,
            "filename": preset_path.name,
            "path": str(preset_path),
            "band_count": max(len(left_source_bands), len(right_source_bands)),
            "channel_mode": channel_mode,
            "left_band_count": len(left_source_bands),
            "right_band_count": len(right_source_bands),
            "eq_band_count": num_bands,
            "left_gain_trim_db": left_gain_trim_db,
            "right_gain_trim_db": right_gain_trim_db,
        }

    def import_rew_peq_text(self, rew_text: str) -> Dict[str, Any]:
        if not isinstance(rew_text, str) or not rew_text.strip():
            raise ValueError("REW import file is empty")

        text = rew_text.replace("\r\n", "\n")
        type_map = {
            "PK": "bell",
            "LS": "low_shelf",
            "HS": "high_shelf",
            "LP": "low_pass",
            "HP": "high_pass",
        }

        def parse_structured_lines(lines: List[str]) -> List[Dict[str, Any]]:
            bands: List[Dict[str, Any]] = []
            for raw_line in lines:
                line = raw_line.strip()
                if not line or not re.match(r"^\d+\s+", line):
                    continue

                parts = line.split()
                if len(parts) < 4:
                    continue

                rew_type = parts[3].upper()
                if rew_type == "NONE":
                    continue
                if rew_type not in type_map:
                    continue

                # Configurable/text and formatted Generic text both start with:
                # <num> <enabled> <control> <type> ...
                # After that, Configurable uses direct numeric columns, while formatted
                # Generic includes labels like Frequency(Hz) in the header but the rows are
                # still compact: <num> True Auto PK 46.30 -19.80 3.387 ...
                if len(parts) < 7:
                    continue

                enabled_token = parts[1]
                freq_token = parts[4]
                gain_token = parts[5]
                q_token = parts[6]
                try:
                    band = {
                        "enabled": enabled_token.lower() == "true",
                        "filterType": type_map[rew_type],
                        "frequencyHz": float(freq_token),
                        "gainDb": float(gain_token),
                        "q": float(q_token),
                    }
                except ValueError as e:
                    raise ValueError(f"Invalid numeric value in REW filter line: {line}") from e

                bands.append(band)
            return bands

        bands = parse_structured_lines(text.splitlines())
        source = None
        if bands:
            if "Configurable_PEQ" in text or "Configurable PEQ" in text:
                source = "rew-configurable-peq-text"
            elif "Number Enabled Control Type Frequency(Hz) Gain(dB) Q" in text or "Equaliser: Generic" in text:
                source = "rew-generic-formatted-text"
            else:
                source = "rew-structured-text"
        else:
            raise ValueError(
                "No supported REW PEQ bands found. Supported now: formatted REW text / clipboard export and Configurable PEQ text export"
            )

        peq_definition = {
            "enabled": True,
            "params": {
                "channelMode": "stereo-linked",
                "bands": bands,
            },
        }
        normalized = self.validate_peq_v1(peq_definition)
        return {
            "source": source,
            "peq": normalized,
            "band_count": len(normalized["params"]["bands"]),
        }

    def create_peq_preset_from_rew_text(self, preset_name: str, rew_text: str, extras: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        imported = self.import_rew_peq_text(rew_text)
        created = self.create_peq_preset(preset_name, imported["peq"], extras=extras)
        return {
            **created,
            "import_source": imported["source"],
        }

    def create_dual_peq_preset_from_rew_texts(
        self,
        preset_name: str,
        left_rew_text: str,
        right_rew_text: str,
        extras: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        imported_left = self.import_rew_peq_text(left_rew_text)
        imported_right = self.import_rew_peq_text(right_rew_text)
        created = self.create_peq_preset(
            preset_name,
            {
                "enabled": True,
                "params": {
                    "channelMode": "dual",
                    "leftBands": imported_left["peq"]["params"].get("bands", []),
                    "rightBands": imported_right["peq"]["params"].get("bands", []),
                },
            },
            extras=extras,
        )
        return {
            **created,
            "import_source": {
                "left": imported_left["source"],
                "right": imported_right["source"],
            },
        }

    def _ensure_pure_preset_exists(self) -> None:
        """Ensure the built-in Direct fallback preset exists in true helper-free form."""
        if not self.output_dir.exists():
            return

        preset_path = self.output_dir / f"{self.PURE_PRESET}.json"
        desired_payload = {"output": {"blocklist": [], "plugins_order": []}}

        if preset_path.exists():
            try:
                existing_payload = json.loads(preset_path.read_text())
                if existing_payload == desired_payload:
                    return
            except Exception:
                pass
            logger.info("Direct preset outdated or invalid, rewriting helper-free version...")
        else:
            logger.info("Direct preset missing, recreating helper-free version...")

        preset_path.write_text(json.dumps(desired_payload, indent=2) + "\n")

    def delete_preset(self, preset_name: str) -> None:
        clean_name = Path(preset_name).stem.strip()
        if not clean_name:
            raise ValueError("Invalid preset name")
        if clean_name in self.PROTECTED_PRESETS:
            raise ValueError(f"Preset \"{clean_name}\" is a built-in preset and cannot be deleted")
        preset_path = self.output_dir / f"{clean_name}.json"
        if not preset_path.exists():
            raise FileNotFoundError(f"Preset not found: {clean_name}")

        preset_kernel_names = self._get_preset_kernel_names(clean_name)
        referenced_elsewhere = self._get_other_referenced_kernel_names(clean_name)
        was_active = self.get_active_preset() == clean_name
        preset_path.unlink()

        orphaned_kernel_names = preset_kernel_names - referenced_elsewhere
        for kernel_name in sorted(orphaned_kernel_names):
            for ir_path in self._find_ir_paths_for_kernel_name(kernel_name):
                try:
                    ir_path.unlink()
                    logger.info("Deleted orphaned IR '%s' after removing preset '%s'", ir_path.name, clean_name)
                except Exception as e:
                    logger.warning(
                        "Failed to delete orphaned IR '%s' after removing preset '%s': %s",
                        ir_path,
                        clean_name,
                        e,
                    )

        if was_active:
            logger.info("Deleted active preset '%s', switching to '%s'", clean_name, self.PURE_PRESET)
            self._ensure_pure_preset_exists()
            self.load_preset(self.PURE_PRESET)

    def create_convolver_preset_with_upload(self, preset_name: str, source_path: Path, filename: str, extras: Optional[Dict[str, Any]] = None) -> dict:
        stored_ir_name = f"{Path(preset_name).stem or 'convolver'}.irs"
        uploaded = self.upload_ir(source_path, filename, stored_name=stored_ir_name)
        preset = self.create_convolver_preset(preset_name, uploaded["name"], extras=extras)
        return {"ir": uploaded, "preset": preset}

    def create_convolver_preset_with_dual_uploads(
        self,
        preset_name: str,
        left_source_path: Path,
        left_filename: str,
        right_source_path: Path,
        right_filename: str,
        extras: Optional[Dict[str, Any]] = None,
    ) -> dict:
        merged_name = f"{Path(preset_name).stem or 'dual-convolver'}.irs"
        uploaded = self.upload_ir_pair(left_source_path, left_filename, right_source_path, right_filename, merged_name)
        preset = self.create_convolver_preset(preset_name, uploaded["name"], extras=extras)
        return {"ir": uploaded, "preset": preset}

    def get_status(self) -> dict:
        presets = self.list_presets()
        irs = self.list_irs()
        active_preset = self.get_active_preset()
        return {
            "available": self.output_dir.exists(),
            "mode": self.runtime.mode,
            "runtime": self.runtime.as_dict(),
            "preset_count": len(presets),
            "active_preset": active_preset,
            "presets": presets,
            "irs": irs,
            "compare": self.load_compare_state(),
            "global_extras": self.load_global_extras(),
            "global_extras_excluded_presets": sorted(self.EXCLUDED_GLOBAL_EXTRAS_PRESETS),
            "paths": {
                "output": str(self.output_dir),
                "irs": str(self.irs_dir),
                "db": str(self.db_file),
                "global_extras": str(self.global_extras_file),
                "compare_state": str(self.compare_state_file),
                "socket": str(self._socket_path()),
                "socket_candidates": [str(path) for path in self._socket_candidates()],
                "cli_command": list(self.runtime.cli_command),
            },
        }
