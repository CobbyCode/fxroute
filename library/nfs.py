"""NFS export identities, bounded discovery, and local mount inspection."""

import hashlib
import ipaddress
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


MOUNT_HELPER = "/usr/local/sbin/fxroute-cifs-mount"


def nfs_entry(server: str, export: str, display_server: str | None = None) -> dict[str, str]:
    if not isinstance(server, str) or not isinstance(export, str):
        raise ValueError("Invalid NFS server or export path")
    if server.startswith("[") and server.endswith("]"):
        try:
            ipaddress.IPv6Address(server[1:-1])
        except ValueError as exc:
            raise ValueError("Invalid NFS server") from exc
    elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", server):
        raise ValueError("Invalid NFS server")
    if (not re.fullmatch(r"/[A-Za-z0-9/ ._()$-]*", export)
            or len(export) > 4096 or any(part in {".", ".."} for part in export.split("/"))):
        raise ValueError("Invalid NFS export path")
    export = re.sub(r"/+", "/", export).rstrip("/") or "/"
    return {
        "id": f"nfs:{server}:{export}",
        "type": "nfs",
        "label": f"NFS — {display_server or server}:{export}",
        "server": server,
        "share": export,
    }


def parse_nfs_url(url: str) -> dict[str, str]:
    match = re.fullmatch(r"(\[[0-9A-Fa-f:]+\]|[^:]+):(/.*)", url)
    if not match:
        raise ValueError("Enter smb://server/share or server:/export/path")
    return nfs_entry(match[1], match[2])


def mount_path(mount_root: Path, server: str, export: str) -> Path:
    entry = nfs_entry(server, export)
    digest = hashlib.sha256(entry["share"].encode()).hexdigest()
    return mount_root.with_name(mount_root.name + "-nfs") / server / digest


def read_mounts() -> dict[str, tuple[str, str]]:
    """Read kernel state without touching a potentially unavailable remote FS."""
    def unescape(value: str) -> str:
        return re.sub(r"\\(040|011|012|134)", lambda m: chr(int(m[1], 8)), value)

    mounts = {}
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return mounts
    for line in lines:
        before, separator, after = line.partition(" - ")
        fields, details = before.split(), after.split()
        if separator and len(fields) >= 5 and len(details) >= 2:
            mounts[unescape(fields[4])] = (details[0], unescape(details[1]))
    return mounts


def mounted_path(mount_root: Path, server: str, export: str) -> Path | None:
    path = mount_path(mount_root, server, export)
    kind, source = read_mounts().get(str(path), ("", ""))
    return path if kind in {"nfs", "nfs4"} and source == f"{server}:{export}" else None


def _discover_host_exports(host: str) -> list[dict[str, str]]:
    display, marker, server = host.partition("@")
    if not marker:
        server = display
    try:
        nfs_entry(server, "/")
    except ValueError:
        return []
    exports = []
    try:
        result = subprocess.run(
            ["showmount", "--exports", "--no-headers", server],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=4, check=False,
        )
        if result.returncode == 0:
            exports = [line.rsplit(None, 1)[0] for line in result.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.TimeoutExpired):
        pass
    # mountd lists physical paths, which can differ from the v4 namespace.
    # Probe both even when mountd answers; inaccessible namespaces stay manual.
    try:
        result = subprocess.run(
            ["sudo", "-n", MOUNT_HELPER, "--discover-nfs", server],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, check=False,
        )
        if result.returncode == 0:
            exports.extend(result.stdout.splitlines())
    except (OSError, subprocess.TimeoutExpired):
        pass
    entries = {}
    for export in exports[:256]:
        try:
            entry = nfs_entry(server, export, display if marker else None)
        except ValueError:
            continue
        entries[entry["id"]] = entry
    return list(entries.values())


def discover_nfs_shares(hosts: list[str]) -> list[dict[str, str]]:
    servers = list(dict.fromkeys(host.strip() for host in hosts if host and host.strip()))
    if not servers:
        return []
    entries = {}
    with ThreadPoolExecutor(max_workers=min(8, len(servers))) as pool:
        for shares in pool.map(_discover_host_exports, servers):
            for entry in shares:
                entries.setdefault(entry["id"], entry)
    return list(entries.values())
