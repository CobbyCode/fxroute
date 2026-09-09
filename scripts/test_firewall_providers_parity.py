#!/usr/bin/env python3
"""Providers-only firewall parity: same inputs, same rules/marks.

Contract for ensure_lan_firewall_rule (providers-only mode) over
ensure_ufw_rule's provider branch + ensure_firewalld_rule_providers_only:

- the two provider rule ids (mdns_5353_udp, spotifyd_zeroconf_4444_tcp)
  resolve through the shared firewall_rule_port table; anything else is
  ignored without side effects;
- an already-open rule marks ownership without calling fw-open;
- a closed rule is opened once via the privileged helper and then marked;
- a hard query failure warns but never aborts (return 0 everywhere);
- a successful open records the rich-priority format for the state file.

The privileged helper itself is stubbed (recorded calls + scripted
return codes); only the installer-side dispatch, marking and format
decisions are pinned. No production code changed here.
"""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
INSTALL_TEXT = INSTALL_SH.read_text()


def extract_function(text: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise AssertionError(f"missing {name}()")
    return f"{name}() {{\n{match.group(1)}\n}}"


FUNCS = "\n".join(
    extract_function(INSTALL_TEXT, name)
    for name in (
        "firewall_rule_port",
        "firewall_rule_state_var",
        "firewall_rule_owned",
        "mark_firewall_rule_owned",
        "ensure_ufw_rule",
        "ensure_firewalld_rule_providers_only",
        "ensure_lan_firewall_rule",
    )
)


def run_dispatch(*, rule_id: str, query_rc: int, open_rc: int, work: Path) -> dict:
    """Run the dispatcher with a scripted helper; return observed state."""
    call_log = work / "calls.log"
    harness = f"""
set -Eeuo pipefail
{FUNCS}
PROVIDERS_ONLY_MODE=1
SPOTIFYD_ZEROCONF_PORT=4444
FIREWALLD_LEGACY_PORT_MIGRATION=0
FIREWALLD_RULE_FORMAT=""
FIREWALLD_MDNS_5353_UDP_OPENED_BY_FXROUTE=0
FIREWALLD_SPOTIFYD_ZEROCONF_4444_TCP_OPENED_BY_FXROUTE=0
UFW_MDNS_5353_UDP_OPENED_BY_FXROUTE=0
UFW_SPOTIFYD_ZEROCONF_4444_TCP_OPENED_BY_FXROUTE=0
MDNS_OPENED_BY_FXROUTE=0
QUERY_RC={query_rc}
OPEN_RC={open_rc}
CALL_LOG={call_log}
provider_privileged() {{
  printf '%s\\n' "$*" >> "$CALL_LOG"
  if [[ "$1" == "fw-query" ]]; then return "$QUERY_RC"; fi
  if [[ "$1" == "fw-open" ]]; then return "$OPEN_RC"; fi
  return 0
}}
warn() {{ printf 'installer-warn: %s\\n' "$*" >&2; }}
pass() {{ :; }}
log() {{ :; }}
ensure_lan_firewall_rule "{rule_id}" "test-purpose"
printf 'rc=%s\\n' "$?"
printf 'format=%s\\n' "$FIREWALLD_RULE_FORMAT"
for var in FIREWALLD_MDNS_5353_UDP_OPENED_BY_FXROUTE FIREWALLD_SPOTIFYD_ZEROCONF_4444_TCP_OPENED_BY_FXROUTE UFW_MDNS_5353_UDP_OPENED_BY_FXROUTE UFW_SPOTIFYD_ZEROCONF_4444_TCP_OPENED_BY_FXROUTE MDNS_OPENED_BY_FXROUTE; do
  printf '%s=%s\\n' "$var" "${{!var}}"
done
"""
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    calls = call_log.read_text().splitlines() if call_log.exists() else []
    observed: dict = {"calls": calls, "stderr": result.stderr}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            observed[key] = value
    return observed


class ProvidersFirewallParityTests(unittest.TestCase):
    def test_open_rule_marks_ownership_without_open_call(self):
        with tempfile.TemporaryDirectory() as td:
            observed = run_dispatch(
                rule_id="mdns_5353_udp", query_rc=0, open_rc=0, work=Path(td)
            )
        self.assertEqual(observed["rc"], "0")
        self.assertNotIn("fw-open mdns_5353_udp test-purpose", observed["calls"])
        self.assertEqual(observed["FIREWALLD_MDNS_5353_UDP_OPENED_BY_FXROUTE"], "1")
        self.assertEqual(observed["UFW_MDNS_5353_UDP_OPENED_BY_FXROUTE"], "1")
        self.assertEqual(observed["MDNS_OPENED_BY_FXROUTE"], "1")
        self.assertEqual(observed["format"], "rich-priority")

    def test_closed_rule_opens_once_then_marks(self):
        with tempfile.TemporaryDirectory() as td:
            observed = run_dispatch(
                rule_id="spotifyd_zeroconf_4444_tcp",
                query_rc=1,
                open_rc=0,
                work=Path(td),
            )
        self.assertEqual(observed["rc"], "0")
        self.assertEqual(
            observed["calls"].count("fw-open spotifyd_zeroconf_4444_tcp test-purpose"),
            1,
        )
        self.assertEqual(
            observed["FIREWALLD_SPOTIFYD_ZEROCONF_4444_TCP_OPENED_BY_FXROUTE"], "1"
        )
        self.assertEqual(
            observed["UFW_SPOTIFYD_ZEROCONF_4444_TCP_OPENED_BY_FXROUTE"], "1"
        )

    def test_hard_query_failure_warns_without_aborting(self):
        with tempfile.TemporaryDirectory() as td:
            observed = run_dispatch(
                rule_id="mdns_5353_udp", query_rc=2, open_rc=0, work=Path(td)
            )
        self.assertEqual(observed["rc"], "0")
        self.assertNotIn("fw-open mdns_5353_udp test-purpose", observed["calls"])
        self.assertEqual(observed["FIREWALLD_MDNS_5353_UDP_OPENED_BY_FXROUTE"], "0")
        self.assertEqual(observed["UFW_MDNS_5353_UDP_OPENED_BY_FXROUTE"], "0")
        self.assertIn("installer-warn", observed["stderr"])

    def test_non_provider_rule_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            observed = run_dispatch(
                rule_id="http_80_tcp", query_rc=0, open_rc=0, work=Path(td)
            )
        self.assertEqual(observed["rc"], "0")
        self.assertEqual(observed["calls"], [])
        self.assertEqual(observed["format"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
