# SPDX-License-Identifier: AGPL-3.0-only
"""Atomic persistence for FXRoute-owned DSP presets and state."""

import json
import logging
from pathlib import Path
from audio.output_topology import BANK_IDS, MODES, SURROUND_GROUPS
from common.atomic_write import atomic_write_text
from dsp.surround_global import (BUILTIN_PRESETS, LAYOUT as SURROUND_LAYOUT, group_filters, preset_layout,
                                 validate_preset_layout)
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


def _basename(value: Any) -> str:
    return Path(str(value or "").strip()).name.strip()


def clean_name(value: Any, fallback: str = "") -> str:
    name = _basename(value)
    if name.lower().endswith(".json"):
        name = name[:-5].strip()
    return name or fallback


# Real IR file suffixes.  A convolver *kernel name* is the IR basename with
# exactly one of these suffixes removed.  Only these are treated as the file
# extension: dots elsewhere in a kernel name (for example the decimal
# auto-gain label in generated names such as "Conv LR ... -1.5dB") are plain
# name characters and must survive resolution.  pathlib's stem/suffix logic
# cannot be used for this because it treats the last dot of the basename as
# the suffix boundary, silently truncating such names at the first dot.
IR_FILE_SUFFIXES = (".irs", ".wav")


def kernel_name(value: Any, fallback: str = "") -> str:
    """IR basename with one trailing .irs/.wav suffix removed.

    A kernel name may keep dots inside it (e.g. "-1.5dB"), so only a real IR
    file suffix is ever stripped.  Idempotent for names without such suffix.
    """
    name = _basename(value)
    if not name:
        return fallback
    for suffix in IR_FILE_SUFFIXES:
        if name.lower().endswith(suffix):
            return name[: -len(suffix)].strip() or fallback
    return name


def clean_bank(value: Any) -> str | None:
    """Filterbank id of a library folder (Global or a bank a mode can define), or None."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned == "global" or cleaned in BANK_IDS else None


class DSPPresetStore:
    """Versioned native presets of one filterbank, with its IR files.

    A preset name is unique within its store only. Neutral exists in every
    store without a file; Direct exists only in Global.
    """

    SCHEMA = "fxroute.dsp.preset"
    VERSION = 1

    def __init__(self, presets_dir: Path, irs_dir: Path, bank: str = "global", mode: Optional[str] = "stereo"):
        self.presets_dir = Path(presets_dir)
        self.irs_dir = Path(irs_dir)
        self.bank = bank
        self.mode = mode

    @property
    def surround(self) -> bool:
        """Surround Global holds speaker-group presets, every other store stereo ones."""
        return self.bank == "global" and self.mode == "surround"

    def path(self, name: str) -> Path:
        normalized = clean_name(name)
        if not normalized:
            raise ValueError("Invalid preset name")
        return self.presets_dir / f"{normalized}.json"

    def validate(self, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Preset must be a JSON object")
        if payload.get("schema") != self.SCHEMA or payload.get("version") != self.VERSION:
            raise ValueError("Unsupported FXRoute DSP preset schema or version")
        chain = payload.get("chain")
        if not isinstance(chain, list):
            raise ValueError("Preset chain must be an array")
        identifiers = set()
        for index, plugin in enumerate(chain):
            if not isinstance(plugin, dict):
                raise ValueError(f"Preset chain[{index}] must be an object")
            if not isinstance(plugin.get("type"), str) or not plugin["type"]:
                raise ValueError(f"Preset chain[{index}].type is required")
            identifier = plugin.get("id")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError(f"Preset chain[{index}].id is required")
            if identifier in identifiers:
                raise ValueError(f"Duplicate plugin id: {identifier}")
            identifiers.add(identifier)
            if not isinstance(plugin.get("params", {}), dict):
                raise ValueError(f"Preset chain[{index}].params must be an object")
            if "group" in plugin and plugin["group"] not in SURROUND_GROUPS:
                raise ValueError(f"Preset chain[{index}].group must be one of {', '.join(SURROUND_GROUPS)}")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Preset metadata must be an object")
        if metadata.get("layout") not in (None, SURROUND_LAYOUT):
            raise ValueError("Preset metadata.layout must be surround when present")
        return payload

    def read(self, name: str) -> Dict[str, Any]:
        if clean_name(name) == "Direct" and self.bank != "global":
            raise ValueError("Direct is available only in the Global bank")
        if clean_name(name) in BUILTIN_PRESETS:
            return {"schema": self.SCHEMA, "version": self.VERSION, "chain": [], "metadata": {}}
        path = self.path(name)
        if not path.is_file():
            raise FileNotFoundError(f"Preset not found: {clean_name(name)}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid preset JSON: {path.name}: {exc}") from exc
        return self.validate(payload)

    def write(self, name: str, payload: Dict[str, Any]) -> Path:
        self.validate(payload)
        validate_preset_layout(payload)
        if (preset_layout(payload) == SURROUND_LAYOUT) != self.surround:
            raise ValueError("Surround Global presets need a speaker group" if self.surround
                             else "A Surround preset fits only the Surround Global bank")
        path = self.path(name)
        atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return path

    def list(self) -> List[dict]:
        """Stored presets by name; the built-ins are not files and not listed."""
        result = []
        for path in sorted(self.presets_dir.glob("*.json"), key=lambda path: path.stem.lower()):
            try:
                payload = self.read(path.stem)
            except (OSError, ValueError, RuntimeError) as exc:
                logger.warning("Skipping unreadable preset %s: %s", path.name, exc)
                continue
            sources = payload.get("metadata", {}).get("source_presets", [])
            entry = {"name": path.stem, "filename": path.name, "path": str(path),
                     "bank": self.bank, "mode": self.mode, "layout": preset_layout(payload),
                     "source_presets": list(sources) if isinstance(sources, list) else []}
            if entry["layout"] == SURROUND_LAYOUT:
                # The UI names the filters each speaker group of the set holds.
                entry["group_filters"] = group_filters(payload)
            result.append(entry)
        return result

    @staticmethod
    def kernels(payload: Optional[Dict[str, Any]]) -> Set[str]:
        names = set()
        for plugin in payload.get("chain", []) if isinstance(payload, dict) else []:
            if isinstance(plugin, dict) and plugin.get("type") == "convolver":
                kernel = plugin.get("params", {}).get("kernel")
                if isinstance(kernel, str) and kernel:
                    names.add(kernel_name(kernel))
        return names

    def referenced_kernels_except(self, excluded: str) -> Set[str]:
        result = set()
        # Cleanup must fail closed when an unreadable preset could own an IR.
        for path in self.presets_dir.glob("*.json"):
            if path.stem != clean_name(excluded):
                result.update(self.kernels(self.read(path.stem)))
        return result

    def find_ir_paths(self, kernel: str) -> List[Path]:
        target = kernel_name(kernel)
        if not target:
            return []
        return sorted(path for path in self.irs_dir.iterdir()
                      if path.is_file() and kernel_name(path.name) == target) if self.irs_dir.is_dir() else []

    def irs(self) -> List[Path]:
        return sorted(path for path in self.irs_dir.iterdir() if path.is_file()) if self.irs_dir.is_dir() else []


class DSPPresetLibrary:
    """The preset stores under one root: one per filterbank, Global one per output mode.

    Global lives in ``global/<mode>``, an area bank in ``<bank id>``, below
    both the presets and the IR root.
    """

    def __init__(self, presets_dir: Path, irs_dir: Path):
        self.presets_dir = Path(presets_dir)
        self.irs_dir = Path(irs_dir)

    def store(self, bank: Any = None, mode: Any = None) -> DSPPresetStore:
        """Store of one bank; without a bank the Stereo Global one.

        The mode selects the Global store and is ignored for an area bank.
        """
        bank_id = "global" if bank is None or (isinstance(bank, str) and not bank.strip()) else clean_bank(bank)
        if bank_id is None:
            raise ValueError(f"Invalid preset bank: {bank!r}")
        if bank_id != "global":
            return DSPPresetStore(self.presets_dir / bank_id, self.irs_dir / bank_id, bank_id, None)
        mode = "stereo" if mode is None or (isinstance(mode, str) and not mode.strip()) else mode
        if mode not in MODES:
            raise ValueError(f"Unsupported output mode: {mode}")
        return DSPPresetStore(self.presets_dir / "global" / mode, self.irs_dir / "global" / mode, "global", mode)

    def stores(self) -> List[DSPPresetStore]:
        """Every Global store, then the area banks that have a folder.

        Only bank ids a mode can define count: a directory that is none of
        them (a library backup, say) is not a bank and is not listed.
        """
        banks = {path.name for root in (self.presets_dir, self.irs_dir) if root.is_dir()
                 for path in root.iterdir() if path.is_dir() and path.name in BANK_IDS}
        return [self.store("global", mode) for mode in MODES] + [self.store(bank) for bank in sorted(banks)]


class DSPStateStore:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)

    def read(self, filename: str, default: Any) -> Any:
        path = self.state_dir / filename
        if not path.is_file():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def write(self, filename: str, payload: Any) -> None:
        atomic_write_text(self.state_dir / filename,
                          json.dumps(payload, indent=2, sort_keys=True) + "\n")
