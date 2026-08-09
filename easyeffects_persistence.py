# SPDX-License-Identifier: AGPL-3.0-only

"""Filesystem ownership for EasyEffects output presets and impulse responses."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class EasyEffectsPresetStore:
    def __init__(self, output_dir: Path, irs_dir: Path):
        self.output_dir = Path(output_dir)
        self.irs_dir = Path(irs_dir)

    @staticmethod
    def clean_preset_name(value: Any, fallback: str = "") -> str:
        name = Path(str(value or "").strip()).name.strip()
        if name.lower().endswith(".json"):
            name = name[:-5].strip()
        return name or fallback

    @classmethod
    def normalize_source_presets(cls, source_presets: Optional[List[str]]) -> List[str]:
        normalized = [
            cls.clean_preset_name(name)
            for name in (source_presets or [])
            if str(name).strip()
        ]
        return [name for name in normalized if name]

    @classmethod
    def extract_source_presets(cls, payload: Dict[str, Any]) -> List[str]:
        fxroute_meta = payload.get("fxroute") if isinstance(payload.get("fxroute"), dict) else {}
        normalized = cls.normalize_source_presets(
            fxroute_meta.get("source_presets") or fxroute_meta.get("sourcePresets") or []
        )
        if normalized:
            return normalized
        return cls.normalize_source_presets(
            payload.get("source_presets") or payload.get("sourcePresets") or []
        )

    def preset_path(self, preset_name: str) -> Path:
        return self.output_dir / f"{self.clean_preset_name(preset_name)}.json"

    def read_preset(self, preset_name: str) -> Dict[str, Any]:
        clean_name = self.clean_preset_name(preset_name)
        if not clean_name:
            raise ValueError("Invalid preset name")
        preset_path = self.output_dir / f"{clean_name}.json"
        if not preset_path.exists():
            raise FileNotFoundError(f"Preset not found: {clean_name}")
        try:
            payload = json.loads(preset_path.read_text())
        except Exception as exc:
            raise RuntimeError(f"Failed to read preset '{clean_name}': {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Preset '{clean_name}' is not a valid JSON object")
        return payload

    def try_read_preset(self, preset_name: str, *, context: str) -> Optional[Dict[str, Any]]:
        try:
            return self.read_preset(preset_name)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            logger.warning("Failed to read EasyEffects preset '%s' for %s: %s", preset_name, context, exc)
            return None

    def write_preset(self, preset_name: str, payload: Dict[str, Any]) -> Path:
        clean_name = self.clean_preset_name(preset_name)
        if not clean_name:
            raise ValueError("Invalid preset name")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        preset_path = self.output_dir / f"{clean_name}.json"
        preset_path.write_text(json.dumps(payload, indent=2) + "\n")
        return preset_path

    def list_presets(self, *, pinned_names: List[str]) -> List[dict]:
        if not self.output_dir.exists():
            return []
        pinned_order = {name: index for index, name in enumerate(pinned_names)}
        preset_paths = sorted(
            self.output_dir.glob("*.json"),
            key=lambda path: (pinned_order.get(path.stem, 100), path.stem.lower()),
        )
        presets = []
        for path in preset_paths:
            payload = self.try_read_preset(path.stem, context="preset listing")
            presets.append(
                {
                    "name": path.stem,
                    "filename": path.name,
                    "path": str(path),
                    "source_presets": self.extract_source_presets(payload) if payload else [],
                }
            )
        return presets

    def list_irs(self) -> List[dict]:
        if not self.irs_dir.exists():
            return []
        return [
            {
                "name": path.name,
                "basename": path.stem,
                "path": str(path),
                "size": path.stat().st_size,
            }
            for path in sorted(self.irs_dir.iterdir())
            if path.is_file()
        ]

    @staticmethod
    def extract_kernel_names(payload: Optional[Dict[str, Any]]) -> Set[str]:
        output = payload.get("output") if isinstance(payload, dict) else None
        if not isinstance(output, dict):
            return set()
        names = set()
        for plugin_payload in output.values():
            if not isinstance(plugin_payload, dict):
                continue
            kernel_name = plugin_payload.get("kernel-name")
            if isinstance(kernel_name, str) and kernel_name.strip():
                names.add(kernel_name.strip())
        return names

    def referenced_kernels_except(self, excluded_preset_name: str) -> Tuple[Set[str], bool]:
        excluded_clean = self.clean_preset_name(excluded_preset_name)
        referenced: Set[str] = set()
        complete = True
        for preset_path in self.output_dir.glob("*.json") if self.output_dir.exists() else []:
            if preset_path.stem == excluded_clean:
                continue
            payload = self.try_read_preset(preset_path.stem, context="IR reference scan")
            if payload is None:
                complete = False
                continue
            referenced.update(self.extract_kernel_names(payload))
        return referenced, complete

    def find_ir_paths(self, kernel_name: str) -> List[Path]:
        if not kernel_name or not self.irs_dir.exists():
            return []
        preferred_path = self.irs_dir / f"{Path(kernel_name).stem}.irs"
        if preferred_path.exists() and preferred_path.is_file():
            return [preferred_path]
        return sorted(
            path for path in self.irs_dir.iterdir()
            if path.is_file() and path.stem == kernel_name
        )
