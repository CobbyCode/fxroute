"""Discovery and activation for local and mounted network music libraries."""

import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlparse


_SYSTEM_SHARES = {"admin$", "ipc$", "print$", "profiles", "users"}


def default_discovery_hosts() -> list[str]:
    configured_hosts = os.environ.get("MUSIC_LIBRARY_SMB_HOSTS")
    raw_hosts = configured_hosts or "openclaw,openclaw.local"
    hosts = [host.strip() for host in raw_hosts.split(",") if host.strip()]
    if configured_hosts:
        return hosts
    try:
        result = subprocess.run(
            ["ip", "neigh", "show"], capture_output=True, text=True, timeout=2, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return hosts
    for line in result.stdout.splitlines():
        address = line.split(maxsplit=1)[0]
        if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", address) and address not in hosts:
            hosts.append(address)
    return hosts


def _server_label(server: str) -> str:
    name = server.split(".", 1)[0]
    return "OpenClaw" if name.lower() == "openclaw" else name


def _smb_entry(server: str, share: str, display_server: str | None = None) -> dict[str, str]:
    server = server.strip()
    share = share.strip().strip("/")
    return {
        "id": f"smb:{server}:{share}",
        "type": "smb",
        "label": f"SMB — {display_server or _server_label(server)} / {share}",
        "server": server,
        "share": share,
    }


def discover_smb_shares(hosts: list[str]) -> list[dict[str, str]]:
    """List guest-visible disk shares on known SMB hosts."""
    shares: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for server in hosts:
        server = server.strip()
        if not server:
            continue
        display_server, marker, address = server.partition("@")
        if marker:
            server = address
        else:
            display_server = _server_label(server)
        try:
            result = subprocess.run(
                ["smbclient", "-g", "-N", "-L", f"//{server}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            kind, separator, rest = line.partition("|")
            share, separator2, _comment = rest.partition("|")
            key = (server.lower(), share.lower())
            if kind != "Disk" or not separator or not separator2 or share.lower() in _SYSTEM_SHARES or key in seen:
                continue
            try:
                access = subprocess.run(
                    ["smbclient", "-N", f"//{server}/{share}", "-c", "ls"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
            if access.returncode != 0:
                continue
            seen.add(key)
            shares.append(_smb_entry(server, share, display_server))
    return shares


class MusicLibraryManager:
    """Own the immutable local root and typed network library choices."""

    def __init__(
        self,
        local_root: Path,
        *,
        mount_root: Path | None = None,
        discovery_hosts: list[str] | None = None,
    ):
        self.local_root = local_root.expanduser().resolve(strict=False)
        self.mount_root = mount_root or (Path.home() / ".cache" / "fxroute" / "music-libraries")
        if discovery_hosts is None:
            discovery_hosts = default_discovery_hosts()
        self.discovery_hosts = discovery_hosts
        self.active_id = "local"
        self.active_type = "local"
        self.active_root = self.local_root
        self._manual: dict[str, dict[str, str]] = {}
        self._discovered: list[dict[str, str]] = []
        self._discovered_at = 0.0

    def list_libraries(self) -> list[dict[str, str]]:
        local = {"id": "local", "type": "local", "label": f"Local — {self.local_root.name or 'Music'}"}
        if time.monotonic() - self._discovered_at > 30:
            self._discovered = discover_smb_shares(self.discovery_hosts)
            self._discovered_at = time.monotonic()
        discovered = self._discovered
        merged = {entry["id"]: entry for entry in discovered}
        merged.update(self._manual)
        return [local, *merged.values(), {
            "id": "manual",
            "type": "action",
            "label": "Add network share manually…",
        }]

    def add_manual_url(self, url: str) -> dict[str, str]:
        parsed = urlparse(url.strip())
        if parsed.scheme.lower() != "smb" or not parsed.hostname:
            raise ValueError("Enter an SMB URL such as smb://server/share")
        share = unquote(parsed.path).strip("/")
        if not share or "/" in share:
            raise ValueError("The SMB URL must identify one share")
        return self.add_manual_share(parsed.hostname, share)

    def add_manual_share(self, server: str, share: str) -> dict[str, str]:
        if (
            not re.fullmatch(r"[A-Za-z0-9._-]+", server or "")
            or not re.fullmatch(r"[A-Za-z0-9 ._()$-]+", share or "")
            or share in {".", ".."}
        ):
            raise ValueError("Invalid SMB server or share")
        entry = _smb_entry(server, share)
        self._manual[entry["id"]] = entry
        return entry

    def _mounted_share_path(self, server: str, share: str) -> Path | None:
        configured = self.mount_root / server / share
        if configured.is_dir():
            return configured.resolve()
        gvfs = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "gvfs"
        for candidate in (
            gvfs / f"smb-share:server={server},share={share}",
            gvfs / f"smb-share:server={server.lower()},share={share.lower()}",
        ):
            if candidate.is_dir():
                return candidate.resolve()
        return None

    def activate(self, library_id: str) -> Path:
        if library_id == "local":
            self.active_id = "local"
            self.active_type = "local"
            self.active_root = self.local_root
            return self.active_root
        if not library_id.startswith("smb:"):
            raise ValueError("Unsupported music library type")
        entries = {entry["id"]: entry for entry in self.list_libraries() if entry["type"] == "smb"}
        if library_id not in entries:
            raise ValueError("Unknown music library")
        _kind, server, share = library_id.split(":", 2)
        root = self._mounted_share_path(server, share)
        if root is None:
            try:
                subprocess.run(
                    ["gio", "mount", f"smb://{server}/{share}"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            root = self._mounted_share_path(server, share)
        if root is None:
            raise FileNotFoundError(f"SMB share is not mounted: {_server_label(server)} / {share}")
        self.active_id = library_id
        self.active_type = "smb"
        self.active_root = root
        return root

    def status(self) -> dict:
        return {
            "active_id": self.active_id,
            "active_type": self.active_type,
            "libraries": self.list_libraries(),
        }
