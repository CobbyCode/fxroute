# SPDX-License-Identifier: AGPL-3.0-only

"""Short unique Spotify Connect device name, independent of Caddy/DNS.

Derives the spotifyd ``device_name`` from the system hostname (read-only).
Hostname, Avahi, and Caddy state are never modified here; ``.local`` never
appears in the result and the bare ``FXRoute`` default is never returned.

Naming rules:

* ``fxroute-wohnzimmer`` -> ``FXRoute Wohnzimmer`` (meaningful suffix kept)
* ``fxroute-1af688`` (image auto name, 6 hex chars) -> ``FXRoute 1AF6``
* bare ``fxroute``/``localhost``/empty -> ``FXRoute XXXX`` from machine-id
* no usable hostname and no machine-id -> persistent ``FXRoute XXXX``
  suffix stored under ``~/.config/fxroute/device-suffix``
"""

from __future__ import annotations

import logging
import re
import secrets
import socket
from pathlib import Path

logger = logging.getLogger(__name__)

CONNECT_NAME_PREFIX = "FXRoute"
CONNECT_NAME_SUFFIX_CHARS = 4
CONNECT_NAME_MAX_LABEL_CHARS = 20

# Image installs derive the hostname as fxroute-<6 machine-id chars>.
AUTO_HOSTNAME_RE = re.compile(r"^fxroute-([0-9a-f]{6})$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")
_PERSISTED_SUFFIX_RE = re.compile(r"^[0-9A-F]{4}$")
_DEVICE_NAME_LINE_RE = re.compile(r"""^(?P<prefix>\s*device_name\s*=\s*)(?P<quote>["'])(?P<value>.*?)(?P=quote)(?P<rest>.*)$""")

_RESERVED_HOSTNAMES = frozenset({"", "localhost", "fxroute"})

# Sentinel returned by derive_spotify_connect_name when neither hostname nor
# machine-id yields a suffix and no fallback was supplied.
_UNRESOLVED = f"{CONNECT_NAME_PREFIX} 0000"


def normalize_hostname(raw: str | None) -> str:
    """Return the first hostname label, lowercased, without any .local."""
    text = (raw or "").strip().lower().strip(".")
    if text.endswith(".local"):
        text = text[: -len(".local")].strip(".")
    return text.split(".")[0].strip() if text else ""


def humanize_label(part: str | None) -> str:
    """Turn a hostname fragment into a short display label."""
    words = re.sub(r"[-_]+", " ", (part or "").lower()).split()
    label = " ".join(word[:1].upper() + word[1:] for word in words if word)
    return label[:CONNECT_NAME_MAX_LABEL_CHARS].rstrip()


def machine_suffix(machine_id_text: str | None) -> str | None:
    """Return the stable 4-char suffix from machine-id, if usable."""
    cleaned = re.sub(r"[^0-9a-f]", "", (machine_id_text or "").lower())
    if len(cleaned) >= CONNECT_NAME_SUFFIX_CHARS:
        return cleaned[:CONNECT_NAME_SUFFIX_CHARS].upper()
    return None


def valid_persisted_suffix(text: str | None) -> str | None:
    """Return the persisted suffix when it matches the 4-char contract."""
    candidate = (text or "").strip().upper()
    if _PERSISTED_SUFFIX_RE.fullmatch(candidate):
        return candidate
    return None


def derive_spotify_connect_name(
    hostname: str | None = None,
    machine_id_text: str | None = None,
    fallback_suffix: str | None = None,
) -> str:
    """Derive the Connect name from already-read inputs (no I/O)."""
    host = normalize_hostname(hostname)
    if host and host not in _RESERVED_HOSTNAMES:
        auto = AUTO_HOSTNAME_RE.fullmatch(host)
        if auto:
            return f"{CONNECT_NAME_PREFIX} {auto.group(1)[:CONNECT_NAME_SUFFIX_CHARS].upper()}"
        rest = host
        if host.startswith("fxroute-"):
            rest = host[len("fxroute-") :].strip("-")
        elif host.startswith("fxroute"):
            rest = host[len("fxroute") :].lstrip("-_")
        label = humanize_label(rest)
        if label:
            return f"{CONNECT_NAME_PREFIX} {label}"
    suffix = machine_suffix(machine_id_text)
    if suffix:
        return f"{CONNECT_NAME_PREFIX} {suffix}"
    persisted = valid_persisted_suffix(fallback_suffix)
    if persisted:
        return f"{CONNECT_NAME_PREFIX} {persisted}"
    return _UNRESOLVED


def default_suffix_file() -> Path:
    """Return the persistent device-suffix path for the current user."""
    return Path.home() / ".config" / "fxroute" / "device-suffix"


def default_spotifyd_config() -> Path:
    """Return the default spotifyd config path for the current user."""
    return Path.home() / ".config" / "spotifyd" / "spotifyd.conf"


def read_hostname() -> str:
    """Return the current system hostname (read-only)."""
    try:
        return socket.gethostname()
    except OSError:
        return ""


def read_machine_id(path: Path | None = None) -> str:
    """Return the machine-id content, or empty when unavailable."""
    target = path or Path("/etc/machine-id")
    try:
        if target.is_symlink():
            return ""
        return target.read_text(encoding="utf-8")
    except OSError:
        return ""


def load_or_create_persisted_suffix(path: Path | None = None) -> str:
    """Load the persistent 4-char suffix, creating it once when needed."""
    target = path or default_suffix_file()
    try:
        candidate = valid_persisted_suffix(target.read_text(encoding="utf-8"))
        if candidate:
            return candidate
    except OSError:
        pass
    suffix = secrets.token_hex(2).upper()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(suffix + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("Device suffix file not writable: %s", exc)
    return suffix


def resolve_spotify_connect_name(
    hostname: str | None = None,
    machine_id_text: str | None = None,
    suffix_file: Path | None = None,
) -> str:
    """Resolve the Connect name, creating the fallback suffix only if needed."""
    host = read_hostname() if hostname is None else hostname
    machine = read_machine_id() if machine_id_text is None else machine_id_text
    name = derive_spotify_connect_name(host, machine, None)
    if name != _UNRESOLVED:
        return name
    return derive_spotify_connect_name(host, machine, load_or_create_persisted_suffix(suffix_file))


def is_managed_spotify_name(name: str | None) -> bool:
    """Return whether a spotifyd device name is FXRoute-managed."""
    if name is None:
        return True
    text = name.strip()
    return text == "" or text == CONNECT_NAME_PREFIX or text.startswith(CONNECT_NAME_PREFIX + " ")


def read_spotifyd_device_name(config_path: Path | None = None) -> str | None:
    """Return the configured spotifyd device name, if present."""
    target = config_path or default_spotifyd_config()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        match = _DEVICE_NAME_LINE_RE.match(line)
        if match:
            return match.group("value")
    return None


def write_spotifyd_device_name(config_path: Path | None, name: str) -> bool:
    """Set the spotifyd device name, preserving all other settings."""
    target = config_path or default_spotifyd_config()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _DEVICE_NAME_LINE_RE.match(line)
        if match:
            quote = match.group("quote")
            lines[index] = f"{match.group('prefix')}{quote}{name}{quote}{match.group('rest')}"
            break
    else:
        entry = f'device_name = "{name}"'
        for index, line in enumerate(lines):
            if line.strip() == "[global]":
                lines.insert(index + 1, entry)
                break
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(["[global]", entry])
    try:
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("Spotify device name not writable: %s", exc)
        return False
    return True


def sync_spotifyd_device_name(
    config_path: Path | None = None,
    desired: str | None = None,
    suffix_file: Path | None = None,
) -> dict:
    """Align an FXRoute-managed spotifyd name with the derived Connect name."""
    target = config_path or default_spotifyd_config()
    if not target.is_file():
        return {"changed": False, "previous": None, "desired": None, "config": str(target)}
    wanted = desired or resolve_spotify_connect_name(suffix_file=suffix_file)
    try:
        previous = read_spotifyd_device_name(target)
    except OSError as exc:
        logger.warning("Spotify device name not readable: %s", exc)
        return {"changed": False, "previous": None, "desired": wanted, "config": str(target)}
    if previous == wanted or not is_managed_spotify_name(previous):
        return {"changed": False, "previous": previous, "desired": wanted, "config": str(target)}
    if not write_spotifyd_device_name(target, wanted):
        return {"changed": False, "previous": previous, "desired": wanted, "config": str(target)}
    logger.info("Spotify Connect name set to %s", wanted)
    return {"changed": True, "previous": previous, "desired": wanted, "config": str(target)}
