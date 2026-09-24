#!/usr/bin/env python3
"""Caddy proxy contract: admin endpoint disabled, no admin-API dependency.

The optional FXRoute Caddy step must not use the Caddy admin API
(127.0.0.1:2019): nothing reloads the proxy via admin (config changes
apply through a service restart), and SELinux httpd_t denies a name_bind
on unreserved ports since selinux-policy 20260914, which crash-loops
fxroute-caddy.service with "bind: permission denied" whenever the admin
endpoint is enabled. The tests also pin the surrounding contracts the
corrected path must keep intact: health check, certificate copy, and the
Settings certificate download endpoint.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INSTALL_SH = ROOT / "install.sh"
UNINSTALL_SH = ROOT / "uninstall.sh"
SESSION_PY = ROOT / "measurement" / "session.py"


def extract_function(text: str, name: str) -> str:
    """Return the full shell function, skipping heredoc bodies.

    A plain "up to the first bare }" scan breaks on templates that emit
    their own closing braces (e.g. the rendered Caddyfile), so heredoc
    regions are skipped until their delimiter line.
    """
    marker = f"{name}() {{"
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if line.rstrip("\n") == marker), None
    )
    if start is None:
        raise AssertionError(f"missing {name}()")
    out = [lines[start]]
    heredoc: str | None = None
    for line in lines[start + 1 :]:
        stripped = line.rstrip("\n")
        if heredoc is not None:
            out.append(line)
            if stripped == heredoc:
                heredoc = None
            continue
        match = re.search(r"<<-?'?([A-Za-z_][A-Za-z0-9_]*)'?\s*$", stripped)
        if match:
            heredoc = match.group(1)
            out.append(line)
            continue
        out.append(line)
        if stripped == "}":
            return "".join(out)
    raise AssertionError(f"unterminated {name}()")


class CaddyAdminEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = INSTALL_SH.read_text()
        cls.uninstall = UNINSTALL_SH.read_text()
        cls.session = SESSION_PY.read_text()
        cls.caddy_step = extract_function(cls.install, "offer_optional_caddy_proxy")

    def test_caddyfile_template_disables_admin_endpoint(self):
        # The rendered Caddyfile must open with a global options block
        # containing "admin off", ahead of every site address/proxy line.
        global_block = re.search(r"\{\s*\n\s*admin off\s*\n\s*\}", self.caddy_step)
        self.assertIsNotNone(
            global_block, "Caddyfile template lacks a global 'admin off' block"
        )
        self.assertLess(
            global_block.start(),
            self.caddy_step.index("reverse_proxy"),
            "'admin off' must be a global option (before the site blocks)",
        )
        self.assertEqual(self.install.count("admin off"), 1)

    def test_no_admin_port_reference_anywhere(self):
        # Match the functional endpoint reference, not incidental digit
        # runs inside pinned sha256 constants.
        self.assertNotIn(":2019", self.install)
        self.assertNotIn(":2019", self.uninstall)

    def test_unit_template_has_no_admin_dependent_reload(self):
        # "caddy reload" talks to the admin API; with admin off it can only
        # fail, so the unit must not offer a reload verb at all. Config
        # changes apply via systemctl restart (the only documented path).
        self.assertNotIn("reload --config", self.caddy_step)
        self.assertNotIn("ExecReload", self.caddy_step)
        self.assertNotIn("caddy reload", self.install)
        # The mdns-guard unit keeps its own, unrelated ExecReload marker.
        self.assertIn(
            "ExecReload=/usr/local/sbin/fxroute-mdns-guard.sh apply", self.install
        )

    def test_reverse_proxy_targets_app_port(self):
        self.assertIn("reverse_proxy 127.0.0.1:${port}", self.caddy_step)

    def test_unit_template_runs_unconfined_on_selinux_hosts(self):
        # /usr/bin/caddy is labeled httpd_exec_t; confined httpd_t may
        # neither bind unreserved ports, nor connect to the app port, nor
        # write the data dir since selinux-policy 20260914 (denials are
        # dontaudit-hidden). unconfined_service_t fails the entrypoint
        # check for httpd_exec_t (status 203/EXEC), so the unit must pin
        # the unconfined exec domain (a no-op without SELinux).
        directive = "SELinuxContext=unconfined_u:unconfined_r:unconfined_t:s0"
        self.assertIn(directive, self.caddy_step)
        self.assertLess(
            self.caddy_step.index("[Service]"),
            self.caddy_step.index(directive),
        )
        self.assertLess(
            self.caddy_step.index(directive),
            self.caddy_step.index("ExecStart="),
        )

    def test_data_dir_guard_accepts_legacy_fxroute_owned_state(self):
        # A root-owned data dir predating the ownership flag must not block
        # the corrected path: the hard dir checks stay, and legacy-false is
        # accepted only when the service/config ownership state exists.
        self.assertEqual(
            self.caddy_step.count("Refusing to use a pre-existing Caddy data directory"),
            2,
        )
        self.assertIn(
            '[[ -z "$CADDY_SERVICE_SHA256" || -z "$CADDY_CONFIG_SHA256" ]]',
            self.caddy_step,
        )
        self.assertGreaterEqual(
            self.caddy_step.count("CADDY_DATA_DIR_CREATED_BY_FXROUTE=1"), 2
        )
        # The hard checks keep their order: shape/owner before the flag.
        shape = self.caddy_step.index('[[ ! -d "$caddy_data_dir"')
        legacy = self.caddy_step.index('"$CADDY_DATA_DIR_CREATED_BY_FXROUTE" -ne 1')
        self.assertLess(shape, legacy)

    def test_https_health_check_and_certificate_copy_preserved(self):
        # The rerun health check and the root-CA copy are what keeps the
        # Settings certificate download alive.
        self.assertIn("https://${lan_ip}/api/status", self.caddy_step)
        self.assertIn("fxroute-local-root.crt", self.caddy_step)
        self.assertIn("caddy/pki/authorities/local/root.crt", self.caddy_step)
        self.assertIn('config_dir="/etc/fxroute"', self.caddy_step)

    def test_settings_certificate_endpoint_matches_installer_path(self):
        # Installer copy target and download endpoint must name the same file.
        self.assertIn("/etc/fxroute/certs/fxroute-local-root.crt", self.session)
        self.assertIn("@router.get(\"/api/certificate/local-root\")", self.session)


if __name__ == "__main__":
    unittest.main(verbosity=2)
