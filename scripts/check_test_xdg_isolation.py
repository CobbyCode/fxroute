#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Audit that no test file can write real product configuration.

Background: persistence resolves $XDG_CONFIG_HOME/fxroute (config),
$XDG_STATE_HOME (measurement/autosub state) and $XDG_DATA_HOME with a $HOME
fallback. A test that touches those paths without its own sandbox would
overwrite the invoking user's live product files (e.g. the tuned
2.1/2.2/2.2-stereo subwoofer levels in audio-output-mode.json) whenever the
suite runs on that machine.

run_tests.sh already sandboxes the whole run; this check enforces the same
property per test file so future tests cannot regress it, including when a
file is executed standalone (python3 scripts/test_x.py):

* discover every scripts/test_*.py that references product-config entry
  points (output-mode persistence, XDG roots, the output-mode route),
* run each in a subprocess with canary XDG roots (HOME stays untouched so
  the user site-packages keep resolving),
* fail if anything lands in the canary roots afterwards.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path, PurePath

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

# Write entry points that can reach real product configuration on disk.
# (Files that merely mention XDG_* to sandbox themselves need no audit.)
TRIGGERS = (
    "_audio_output_mode_path",
    "persist_audio_output_mode",
    "set_audio_output_mode",
    "_save_audio_output_selection",
    "persist_sample_rate_policy",
    "save_audio_output_mode_route",
    "audio-output-mode.json",
    "audio-output-selection.json",
    "sample-rate-policy.json",
)

# Writes that are not FXRoute product configuration and therefore out of
# scope for this audit: libpulse creates its client auth cookie at
# $XDG_CONFIG_HOME/pulse/cookie on first audio-client contact
# (auto-recreated, no user tuning lost). Everything under fxroute/ is
# product state.
IGNORED_SUFFIXES = (
    ("pulse", "cookie"),
)


def _is_ignored(relative: str) -> bool:
    parts = PurePath(relative).parts
    return any(parts[-len(suffix):] == suffix for suffix in IGNORED_SUFFIXES)

IGNORE_FILES = {
    # This audit itself only reads test sources.
    "check_test_xdg_isolation.py",
}


def candidate_files() -> list[Path]:
    found = []
    for path in sorted(SCRIPTS.glob("test_*.py")):
        if path.name in IGNORE_FILES:
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        if any(trigger in text for trigger in TRIGGERS):
            found.append(path)
    return found


def run_with_canary(path: Path) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="fxroute-xdg-audit-") as sandbox:
        canary = Path(sandbox)
        env = dict(os.environ)
        # NOTE: HOME stays untouched — project dependencies resolve through
        # the user site-packages under $HOME/.local.
        env["XDG_CONFIG_HOME"] = str(canary / "config")
        env["XDG_STATE_HOME"] = str(canary / "state")
        env["XDG_DATA_HOME"] = str(canary / "data")
        for key in ("FXROUTE_TEST_SANDBOX",):
            env.pop(key, None)
        try:
            proc = subprocess.run(
                [sys.executable, str(path)],
                cwd=ROOT,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return False, f"{path.name}: timed out after 300s"
        # The test itself may pass or fail here; this audit only cares
        # whether it wrote outside its own sandbox.
        leftovers = sorted(
            str(p.relative_to(canary))
            for p in canary.rglob("*")
            if p.is_file() and not _is_ignored(str(p.relative_to(canary)))
        )
        if leftovers:
            return False, f"{path.name}: wrote to product-config paths: {leftovers[:10]}"
        return True, f"{path.name}: clean (exit={proc.returncode})"


def main() -> int:
    candidates = candidate_files()
    if not candidates:
        print("xdg isolation audit: no candidate test files found")
        return 0
    failures = []
    for path in candidates:
        ok, message = run_with_canary(path)
        print(("ok  " if ok else "FAIL") + f"  {message}")
        if not ok:
            failures.append(path.name)
    if failures:
        print(f"xdg isolation audit: {len(failures)} file(s) wrote product-config paths")
        return 1
    print(f"xdg isolation audit: ok ({len(candidates)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
