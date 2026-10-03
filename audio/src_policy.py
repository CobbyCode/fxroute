# SPDX-License-Identifier: AGPL-3.0-only
"""One SRC policy for native, Pulse and device adapter boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

from common.atomic_write import atomic_write_text
from audio.tool_env import c_locale_env

MODES = ("low", "standard", "high")
FACTORY = "fxroute.src.convert"
RUNTIME = Path(__file__).resolve().parents[1] / "native_src/build"


def config_root() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def policy_path() -> Path:
    return config_root() / "fxroute/src-policy.json"


def normalize_mode(mode: object) -> str:
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("SRC mode must be low, standard or high")
    return mode


def load_policy() -> dict:
    path = policy_path()
    if not path.exists():
        return {"mode": "standard"}
    try:
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate SRC policy key")
                result[key] = value
            return result
        payload = json.loads(path.read_text(), object_pairs_hook=unique_object)
        if not isinstance(payload, dict) or set(payload) != {"mode"}:
            raise ValueError("SRC policy must be an object")
        return {"mode": normalize_mode(payload.get("mode"))}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Cannot read SRC policy; repair src-policy.json") from exc


def persist_policy(mode: object) -> dict:
    policy = {"mode": normalize_mode(mode)}
    atomic_write_text(policy_path(), json.dumps(policy, indent=2) + "\n")
    return policy


def plugin_locations() -> list[Path]:
    return [Path(p) / "libspa-fxroute-src.so" for p in (
        "/usr/lib64/spa-0.2", "/usr/lib/x86_64-linux-gnu/spa-0.2", "/usr/lib/spa-0.2") if Path(p).is_dir()]


def install_plugin(runtime: Path | None = None) -> Path:
    runtime = runtime or RUNTIME
    library = runtime.absolute() / "libspa-fxroute-src.so"
    if not library.is_file():
        raise RuntimeError("Build the common SRC runtime before installing its plugin")
    paths = plugin_locations()
    if not paths:
        raise RuntimeError("No standard SPA plugin directory is installed")
    target = paths[0]
    if target.exists() and not target.is_symlink():
        raise RuntimeError(f"Refusing to replace an unmanaged SPA library: {target}")
    if target.is_symlink() and target.readlink() != library:
        raise RuntimeError(f"SPA plugin belongs to another installation: {target}")
    link = target.with_name(".fxroute-src-" + uuid.uuid4().hex)
    try:
        link.symlink_to(library)
        os.replace(link, target)
    finally:
        link.unlink(missing_ok=True)
    return target


def plugin_installed(runtime: Path | None = None) -> bool:
    library = (runtime or RUNTIME) / "libspa-fxroute-src.so"
    return any(p.is_file() and p.resolve() == library.resolve() for p in plugin_locations())


def plugin_directories(runtime: Path | None = None) -> str:
    candidates = [str(runtime or RUNTIME), *os.environ.get("SPA_PLUGIN_DIR", "").split(":"),
                  "/usr/lib64/spa-0.2", "/usr/lib/x86_64-linux-gnu/spa-0.2", "/usr/lib/spa-0.2"]
    return ":".join(dict.fromkeys(p for p in candidates if p))


def client_env(base: dict | None = None) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    environment["SPA_PLUGIN_DIR"] = plugin_directories()
    return environment


def activate_environment() -> None:
    subprocess.run(["systemctl", "--user", "set-environment", f"SPA_PLUGIN_DIR={plugin_directories()}"],
                   check=True, timeout=5, capture_output=True)


def configuration(runtime: Path | None = None) -> dict[Path, str]:
    runtime = runtime or RUNTIME
    library = "libspa-fxroute-src"
    mapping = f"context.spa-libs = {{ {FACTORY} = {library} }}\n"
    streams = f"""stream.rules = [
    {{ matches = [ {{ media.class = Stream/Output/Audio }} ]
      actions = {{ update-props = {{ audio.adapt.converter = {FACTORY} }} }} }}
]
"""
    devices = f"""monitor.alsa.rules = [
    {{ matches = [ {{ node.name = "~alsa_input.*" }} {{ node.name = "~alsa_output.*" }} ]
      actions = {{ update-props = {{ audio.adapt.converter = {FACTORY} }} }} }}
]
monitor.bluez.rules = [
    {{ matches = [ {{ node.name = "~bluez_input.*" }} {{ node.name = "~bluez_output.*" }} ]
      actions = {{ update-props = {{ audio.adapt.converter = {FACTORY} }} }} }}
]
"""
    root = config_root()
    return {
        root / "environment.d/80-fxroute-src.conf": "SPA_PLUGIN_DIR=" + plugin_directories(runtime) + "\n",
        root / "pipewire/client.conf.d/80-fxroute-src.conf": mapping + streams,
        root / "pipewire/pipewire-pulse.conf.d/80-fxroute-src.conf": mapping + streams,
        root / "pipewire/pipewire.conf.d/80-fxroute-src.conf": mapping,
        root / "wireplumber/wireplumber.conf.d/80-fxroute-src.conf": mapping + devices,
    }


def configure(runtime: Path | None = None) -> list[Path]:
    runtime = runtime or RUNTIME
    if not (runtime / "libspa-fxroute-src.so").is_file() or not os.access(runtime / "sox", os.X_OK):
        raise RuntimeError("Common SRC runtime is missing; run native_src/build.py")
    if not plugin_installed(runtime):
        raise RuntimeError("Common SRC plugin is not installed in the standard SPA path; rerun the installer")
    load_policy()
    files = configuration(runtime)
    for path, content in files.items():
        atomic_write_text(path, content)
    return list(files)


def runtime_error() -> str | None:
    try:
        manifest = json.loads((RUNTIME / "manifest.json").read_text())
        if manifest.get("sox_version") != "16.2.1":
            return "Pinned SoX 16.2.1 runtime is missing"
        if not (RUNTIME / "libspa-fxroute-src.so").is_file() or not os.access(RUNTIME / "sox", os.X_OK):
            return "Common SRC runtime is missing; run native_src/build.py"
        if not plugin_installed():
            return "Common SRC plugin is not installed in the standard SPA path; rerun the installer"
        for path, expected in configuration().items():
            if not path.is_file() or path.read_text() != expected:
                return "Common SRC bindings are missing; run scripts/configure_src.py"
    except (OSError, ValueError):
        return "Common SRC runtime is missing; run native_src/build.py"
    return None


def nodes() -> list[dict]:
    result = subprocess.run(["pw-dump"], capture_output=True, text=True,
                            timeout=5, check=True, env=c_locale_env())
    observed = []
    for item in json.loads(result.stdout):
        if item.get("type") != "PipeWire:Interface:Node":
            continue
        info = item.get("info") or {}
        props = info.get("props") or {}
        params = {}
        for entry in (info.get("params") or {}).get("Props", []):
            values = entry.get("params") or {}
            if isinstance(values, dict):
                params.update(values)
            elif isinstance(values, list):
                params.update(zip(values[::2], values[1::2]))
        bound = props.get("audio.adapt.converter") == FACTORY or "fxroute.src.mode" in params
        graph_loopback = (props.get("node.virtual") is True and props.get("node.passive") is True
                          and str(props.get("node.link-group", "")).startswith("loopback-"))
        expected = ((props.get("device.api") in {"alsa", "bluez5"} and props.get("media.class") in {"Audio/Sink", "Audio/Source"})
                    or (props.get("media.class") == "Stream/Output/Audio" and not graph_loopback))
        if bound or expected:
            observed.append({"id": item["id"], "name": props.get("node.name"),
                             "state": info.get("state"), "mode": params.get("fxroute.src.mode"),
                             "bound": bound, "expected": expected,
                             "generation": params.get("fxroute.src.generation"),
                             "input_rate": params.get("fxroute.src.input-rate"),
                             "output_rate": params.get("fxroute.src.output-rate"),
                             "nominal_bypass": params.get("fxroute.src.nominal-bypass"),
                             "error": params.get("fxroute.src.error", 0)})
    return observed


def status() -> dict:
    error = runtime_error()
    runtime_ready = not bool(error)
    try:
        selected = load_policy()["mode"]
    except ValueError as exc:
        selected = None
        error = str(exc)
    observed = []
    try:
        observed = nodes()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        error = error or f"Cannot inspect common SRC adapters: {exc}"
    mismatches = [n["name"] for n in observed if n["mode"] not in {"unconfigured", "peaks", selected}
                  or (n.get("expected") and not n.get("bound"))
                  or (n["state"] == "running" and n["mode"] == "unconfigured")]
    restart_required = bool(mismatches) or not observed
    try:
        generation = json.loads((RUNTIME / "manifest.json").read_text()).get("generation")
    except (OSError, ValueError):
        generation = None
    if generation and any(n["generation"] != generation for n in observed if n.get("bound")):
        restart_required = True
    if restart_required:
        error = error or "Common SRC activation is pending; restart audio while playback is stopped"
    failures = [n["name"] for n in observed if n["error"]]
    if failures:
        error = f"SRC adapter failed: {', '.join(failures)}"
    return {"mode": selected, "modes": list(MODES), "available": not bool(error),
            "error": error, "runtime_ready": runtime_ready, "effective_nodes": observed, "restart_required": restart_required,
            "factory": FACTORY, "startup_reserve_ms": 750, "startup_timeout_ms": 3750, "fifo_frames": 65536,
            "generation": generation,
            "sizing_provisional": True, "clock_matching": "native PipeWire",
            "apply_requires_stopped_playback": True}
