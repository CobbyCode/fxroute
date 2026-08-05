"""Install/version metadata helpers.

Extracted verbatim from main.py (REFACTOR-009). Behavior is identical to the
previous inline implementation: VERSION/BUILD_ID reading with strip(),
empty-string fallback on errors, unknown-version fallback, BUILD_ID priority,
exact build-id output formats, git fallback via `rev-parse --short HEAD` with
1.5s timeout, line-wise install-config parsing (comments, blank lines, lines
without '=', split on first '=', whitespace trimming, last value wins), empty
result on read errors, and the `fxroute` service-name fallback. Every call
re-reads from disk; there is no caching.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INSTALL_CONFIG_FILE = Path.home() / ".config" / "fxroute" / "install-config.env"


def read_version_file() -> str:
    try:
        return (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def read_build_id() -> str:
    version = read_version_file() or "unknown-version"
    try:
        deployed_build = (BASE_DIR / "BUILD_ID").read_text(encoding="utf-8").strip()
        if deployed_build:
            return f"{version} {deployed_build}"
    except Exception:
        pass
    try:
        completed = subprocess.run(
            ["git", "-C", str(BASE_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.5,
        )
        commit = completed.stdout.strip() if completed.returncode == 0 else ""
    except Exception:
        commit = ""
    return f"{version} commit={commit or 'unknown'}"


def read_install_config() -> dict:
    data: dict[str, str] = {}
    try:
        for raw_line in INSTALL_CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()
    except Exception:
        pass
    return data


def configured_service_name() -> str:
    return read_install_config().get("FXROUTE_SERVICE_NAME") or "fxroute"
