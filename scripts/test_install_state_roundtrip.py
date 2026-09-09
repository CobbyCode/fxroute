#!/usr/bin/env python3
"""Install-state write/read roundtrip contract.

Verifies that every dotted key the installer reads back via
``previous_install_state_field`` is actually emitted by
``write_install_state`` with the documented normalization:

- ``0``/``1`` shell flags round-trip as ``false``/``true``;
- string values round-trip verbatim (including an explicitly empty
  string, which reads back as present-but-empty, not missing);
- fixed literals (``"native"``, ``3``, ...) round-trip unchanged.

Reader keys are discovered from install.sh at runtime, so a key added on
either side without its counterpart fails loudly instead of drifting
silently (the failure mode that produced the legacy firewall/mdns shims).
A legacy-shape state file (pre-schema keys) additionally pins why those
shims exist: new keys read as missing, legacy keys stay readable.

No installer semantics are changed here; on failure, fix writer/reader,
not this test.
"""

import json
import os
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


WRITE_BODY = slice_function(INSTALL_TEXT, "write_install_state() {", "\nwrite_install_config() {")


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


def template_fields(body: str) -> dict:
    """Map dotted JSON path -> ("bool", VAR) | ("str", VAR) | ("fixed", value)."""
    fields: dict = {}
    stack: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        opened = re.match(r'^"([^"]+)": \{$', line)
        if opened:
            stack.append(opened.group(1))
            continue
        if re.match(r'^\},?$', line):
            if stack:
                stack.pop()
            continue
        leaf = re.match(r'^"([^"]+)": (.*?),?\s*$', line)
        if not leaf:
            continue
        key, value = leaf.group(1), leaf.group(2)
        bool_var = re.search(r'\[\[ \$([A-Z][A-Z0-9_]*) -eq 1 \]\]', value)
        if bool_var and "echo true" in value:
            fields[".".join([*stack, key])] = ("bool", bool_var.group(1))
            continue
        str_var = re.search(r'\$[{]?([A-Z][A-Z0-9_]*)[}]?', value)
        if str_var and ("echo true" not in value):
            fields[".".join([*stack, key])] = ("str", str_var.group(1))
            continue
        literal = value.strip().rstrip(",").strip()
        if literal.startswith('"') and literal.endswith('"'):
            literal = literal[1:-1]
        fields[".".join([*stack, key])] = ("fixed", literal)
    return fields


TEMPLATE = template_fields(WRITE_BODY)


def reader_keys() -> list[str]:
    return sorted(
        set(re.findall(r"previous_install_state_field ([a-zA-Z0-9_.]+)", INSTALL_TEXT))
    )


class InstallStateRoundtripTests(unittest.TestCase):
    def _run_writer(self, work: Path) -> Path:
        state_file = work / "install-state.json"
        root_state_file = work / "root-state.json"
        bool_vars = sorted(
            {var for kind, var in TEMPLATE.values() if kind == "bool"}
        )
        str_vars = sorted(
            {var for kind, var in TEMPLATE.values() if kind == "str"}
        )
        assignments = []
        for index, var in enumerate(bool_vars):
            assignments.append(f"{var}={index % 2}")
        for var in str_vars:
            assignments.append(f"{var}='{self._str_value(var)}'")
        harness = f"""
set -Eeuo pipefail
{READER_FN}
{WRITE_BODY}
run_as_target_user() {{ "$@"; }}
pass() {{ :; }}
warn() {{ printf '%s\\n' "$*" >&2; }}
SUDO_CMD=()
PROVIDERS_ONLY_MODE=1
INSTALL_STATE_FILE={state_file}
ROOT_INSTALL_STATE_FILE={root_state_file}
""" + "\n".join(assignments) + "\nwrite_install_state\n"
        result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(state_file.exists(), "writer must produce the state file")
        return state_file

    def _read_key(self, state_file: Path, key: str) -> tuple[int, str]:
        harness = f"""
set -Eeuo pipefail
{READER_FN}
INSTALL_STATE_FILE={state_file}
ROOT_INSTALL_STATE_FILE={state_file}.root
if previous_install_state_field {key} 2>/dev/null; then
  printf '\\nREAD_OK\\n'
else
  printf '\\nREAD_MISSING\\n'
fi
"""
        result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
        out = result.stdout
        if out.rstrip("\n").endswith("READ_OK"):
            value = out[: out.rindex("READ_OK")].rstrip("\n")
            if value.endswith("\n"):
                value = value[:-1]
            return 0, value
        return 1, ""

    def test_every_reader_key_roundtrips(self):
        self.assertGreater(len(reader_keys()), 50, "reader key discovery must not run empty")
        self.assertGreater(len(TEMPLATE), 50, "writer template parsing must not run empty")
        with tempfile.TemporaryDirectory() as td:
            state_file = self._run_writer(Path(td))
            payload = json.loads(state_file.read_text())
            self.assertIsInstance(payload, dict)
            missing: list[str] = []
            mismatched: list[str] = []
            for key in reader_keys():
                if key not in TEMPLATE:
                    missing.append(key)
                    continue
                kind, ref = TEMPLATE[key]
                if kind == "bool":
                    index = sorted(
                        {var for k, var in TEMPLATE.values() if k == "bool"}
                    ).index(ref)
                    expected = "true" if index % 2 else "false"
                elif kind == "str":
                    expected = self._str_value(ref)
                else:
                    expected = ref
                rc, actual = self._read_key(state_file, key)
                if rc != 0:
                    mismatched.append(f"{key}: unreadable")
                elif actual != expected:
                    mismatched.append(f"{key}: got {actual!r}, want {expected!r}")
            self.assertEqual(missing, [], f"reader keys missing from writer output: {missing}")
            self.assertEqual(mismatched, [], f"roundtrip mismatches: {mismatched}")

    def _str_value(self, var: str) -> str:
        as_root = os.geteuid() == 0
        if var == "SPOTIFYD_BINARY_PATH":
            return ""
        if var == "FXROUTE_TARGET_USER":
            return "root" if as_root else "tester"
        if var == "FXROUTE_TARGET_UID":
            return "0" if as_root else "1000"
        if var == "FXROUTE_RUNTIME_DIR":
            return "/run/user/0" if as_root else "/run/user/1000"
        if var == "INSTALL_ROOT":
            return "/opt/fxroute"
        if var == "LAN_HOSTNAME_AFTER":
            return "fxroute-test"
        return f"value-{var.lower().replace('_', '-')}"

    def test_legacy_shape_explains_shims(self):
        """Pre-schema state: new keys read as missing, legacy keys readable."""
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "install-state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "install_root": "/opt/fxroute",
                        "providers": {
                            "spotifyd": {"installed_by_fxroute": True},
                        },
                        "lan_comfort": {
                            "http_opened_by_fxroute": True,
                            "legacy_firewall_ownership_present": True,
                        },
                    }
                )
            )
            for new_key in (
                "lan_comfort.firewall_ownership_schema",
                "lan_comfort.mdns_guard_owned_by_fxroute",
            ):
                rc, _ = self._read_key(state_file, new_key)
                self.assertNotEqual(rc, 0, f"{new_key} must read as missing on legacy files")
            rc, actual = self._read_key(
                state_file, "lan_comfort.legacy_firewall_ownership_present"
            )
            self.assertEqual(rc, 0)
            self.assertEqual(actual, "true")
            rc, actual = self._read_key(state_file, "providers.spotifyd.installed_by_fxroute")
            self.assertEqual(rc, 0)
            self.assertEqual(actual, "true")


if __name__ == "__main__":
    unittest.main(verbosity=2)
