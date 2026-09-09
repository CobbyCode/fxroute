#!/usr/bin/env python3
"""Install config/state parity: runtime pointers match the ownership ledger.

The two writers serve different contracts and must stay that way:

- write_install_config writes 4 runtime pointers (install-config.env),
  consumed by the service (install_info), the updater and the
  uninstaller for discovery;
- write_install_state writes the ~100-key ownership ledger
  (install-state.json), consumed by installer reload decisions.

Where they overlap (install root, user, state path) both must derive
from the same values in the same run; the service-name default both
sides rely on ("fxroute") is pinned as well. No new fields, no
semantics: this test snapshots today's cross-file consistency.
"""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
INSTALL_TEXT = INSTALL_SH.read_text()


def slice_function(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


WRITE_CONFIG = slice_function(INSTALL_TEXT, "write_install_config() {", "\ncheckpoint_install_state_on_exit() {")
WRITE_STATE = slice_function(INSTALL_TEXT, "write_install_state() {", "\nwrite_install_config() {")


def extract_function(text: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise AssertionError(f"missing {name}()")
    return f"{name}() {{\n{match.group(1)}\n}}"


READER_FN = extract_function(INSTALL_TEXT, "previous_install_state_field")


BOOL_VARS = sorted(
    set(re.findall(r"\[\[ \$([A-Z][A-Z0-9_]*) -eq 1 \]\]", WRITE_STATE))
    - {"PROVIDERS_ONLY_MODE"}
)
STR_VARS = sorted(
    set(re.findall(r"\$[{]?([A-Z][A-Z0-9_]*)[}]?", WRITE_STATE))
    - set(BOOL_VARS)
    - {
        "INSTALL_STATE_FILE",
        "ROOT_INSTALL_STATE_FILE",
        "INSTALL_CONFIG_FILE",
        "PROVIDERS_ONLY_MODE",
    }
)


class InstallConfigStateParityTests(unittest.TestCase):
    def _run_writers(self, work: Path) -> tuple[Path, Path]:
        config_file = work / "install-config.env"
        state_file = work / "install-state.json"
        harness = f"""
set -Eeuo pipefail
{READER_FN}
{WRITE_CONFIG}
{WRITE_STATE}
run_as_target_user() {{ "$@"; }}
pass() {{ :; }}
warn() {{ printf '%s\\n' "$*" >&2; }}
SUDO_CMD=()
PROVIDERS_ONLY_MODE=1
""" + "\n".join(f"{var}=0" for var in BOOL_VARS) + "\n" + "\n".join(
    f"{var}=''" for var in STR_VARS
) + f"""
INSTALL_ROOT=/opt/fxroute-test
SERVICE_NAME=fxroute
INSTALL_STATE_FILE={state_file}
ROOT_INSTALL_STATE_FILE={work}/root-state.json
INSTALL_CONFIG_FILE={config_file}
FXROUTE_TARGET_USER=tester
FXROUTE_TARGET_UID=1000
FXROUTE_RUNTIME_DIR=/run/user/1000
LOCAL_PROJECT_MODE=0
SELECT_SPOTIFY_DESKTOP=1
SELECT_SPOTIFYD=0
SELECT_QOBUZ=1
write_install_config
write_install_state
"""
        result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return config_file, state_file

    def _read_key(self, state_file: Path, key: str) -> str:
        harness = f"""
set -Eeuo pipefail
{READER_FN}
INSTALL_STATE_FILE={state_file}
ROOT_INSTALL_STATE_FILE={state_file}.root
previous_install_state_field {key}
"""
        result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"key {key} unreadable: {result.stderr}")
        return result.stdout.strip()

    def test_config_snapshot_and_cross_file_consistency(self):
        with tempfile.TemporaryDirectory() as td:
            config_file, state_file = self._run_writers(Path(td))
            config = config_file.read_text().splitlines()
            self.assertEqual(
                config,
                [
                    "FXROUTE_INSTALL_ROOT=/opt/fxroute-test",
                    "FXROUTE_SERVICE_NAME=fxroute",
                    f"FXROUTE_INSTALL_STATE={state_file}",
                    "FXROUTE_POWER_USER=tester",
                ],
            )
            state = json.loads(state_file.read_text())
            self.assertEqual(state["install_root"], "/opt/fxroute-test")
            self.assertEqual(state["install_user"], "tester")
            self.assertEqual(state["install_uid"], "1000")
            self.assertEqual(
                self._read_key(state_file, "install_root"), "/opt/fxroute-test"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
