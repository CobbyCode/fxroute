# SPDX-License-Identifier: AGPL-3.0-only
"""Atomic persistence for FXRoute-owned DSP presets and state."""

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            mode = None
        if mode is not None and stat.S_ISREG(mode):
            os.fchmod(fd, stat.S_IMODE(mode))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)
        raise


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


class DSPPresetStore:
    """Read and write versioned native presets under one owned root."""

    SCHEMA = "fxroute.dsp.preset"
    VERSION = 1

    def __init__(self, presets_dir: Path, irs_dir: Path):
        self.presets_dir = Path(presets_dir)
        self.irs_dir = Path(irs_dir)

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
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Preset metadata must be an object")
        return payload

    def read(self, name: str) -> Dict[str, Any]:
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
        path = self.path(name)
        atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return path

    def list(self, pinned: Iterable[str] = ()) -> List[dict]:
        order = {name: index for index, name in enumerate(pinned)}
        paths = sorted(
            self.presets_dir.glob("*.json"),
            key=lambda path: (order.get(path.stem, len(order)), path.stem.lower()),
        ) if self.presets_dir.exists() else []
        result = []
        for path in paths:
            payload = self.read(path.stem)
            sources = payload.get("metadata", {}).get("source_presets", [])
            result.append({"name": path.stem, "filename": path.name, "path": str(path),
                           "source_presets": list(sources) if isinstance(sources, list) else []})
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
        for entry in self.list():
            if entry["name"] != clean_name(excluded):
                result.update(self.kernels(self.read(entry["name"])))
        return result

    def find_ir_paths(self, kernel: str) -> List[Path]:
        target = kernel_name(kernel)
        if not target:
            return []
        return sorted(path for path in self.irs_dir.iterdir()
                      if path.is_file() and kernel_name(path.name) == target) if self.irs_dir.exists() else []


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
