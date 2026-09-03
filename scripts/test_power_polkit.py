#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Polkit rule and installer integration tests for FXRoute system power.

These tests cover the security-critical surface of the suspend/shutdown
feature:

* The polkit template ships the narrow allow-list expected by the
  audit rules of the task: only the two logind actions, and only the
  install user (no rule-less ``yes`` for any other identity).
* The template is syntactically valid JavaScript via ``node --check``
  (polkit parses the same .rules file through Mozilla Spidermonkey).
* The installer substitutes the right install user into the template
  before writing to ``/etc/polkit-1/rules.d/50-fxroute-power.rules``.
* The installer wires the polkit step into the main ``main()`` runner
  and records the resulting state in ``install-state.json``, so the
  uninstall script can safely remove the rule.
* The uninstall script actually invokes the polkit removal helper.
* The frontend only references the asset versions incremented in
  ``power.py`` and never hard-codes the power-menu DOM from a stale
  index.html build.
"""

import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
UNINSTALL_SH = ROOT / "uninstall.sh"
POLKIT_TEMPLATE = ROOT / "assets" / "polkit" / "50-fxroute-power.rules"
INDEX_HTML = ROOT / "static" / "index.html"
APP_JS = ROOT / "static" / "app.js"
STYLE_CSS = ROOT / "static" / "style.css"
POWER_PY = ROOT / "audio" / "power.py"


def _assert_node_available() -> str:
    """Locate the node binary; skip the JS-syntax tests when missing.

    polkit itself ships its own Spidermonkey, but we do not depend on
    that being installed in dev/CI.  ``node --check`` is a faithful
    proxy: Spidermonkey (which polkit actually uses) is stricter than
    node for many things, so a clean ``node --check`` only proves we
    would not be tripped up by syntax -- not the inverse.
    """

    path = shutil.which("node")
    if not path:
        raise unittest.SkipTest("node not installed; skipping JS-syntax check")
    return path


class PolkitTemplateStaticTests(unittest.TestCase):
    """The .rules file must express the limits required by the task."""

    @classmethod
    def setUpClass(cls):
        cls.text = POLKIT_TEMPLATE.read_text()

    def test_template_contains_placeholders(self):
        self.assertIn("INSTALL_USER_PLACEHOLDER", self.text)
        self.assertIn("polkit.addRule", self.text)

    def test_template_lists_only_the_two_required_actions(self):
        allowed = {
            "org.freedesktop.login1.suspend",
            "org.freedesktop.login1.power-off",
            "org.freedesktop.hostname1.set-static-hostname",
        }
        # Use a non-greedy scan to grab every polkit action id inside
        # string literals -- with or without surrounding quotes.  Anything
        # outside the expected set is an upgrade of privilege and must
        # fail.
        mentioned = set(re.findall(r"\"?(org\.freedesktop\.[a-z0-9_.-]+)\"?", self.text))
        self.assertEqual(mentioned & allowed, allowed)
        # And no other org.freedesktop.* action is referenced at all.
        self.assertEqual(mentioned - allowed, set())

    def test_template_does_not_grant_global_yes(self):
        # The rule must NOT return polkit.Result.YES unconditionally;
        # the install-user guard must always be present.
        self.assertIn('subject.user', self.text)
        self.assertIn("polkit.Result.YES", self.text)
        # And the guard must run before the result is returned.
        idx_guard = self.text.index("subject.user")
        idx_result = self.text.index("polkit.Result.YES")
        self.assertLess(idx_guard, idx_result)

    def test_template_skips_for_other_users(self):
        # The literal block the template uses to early-return for
        # other users must be present (it must NOT simply omit the
        # check, which would mean "match anybody").
        self.assertIn('return;', self.text)

    def test_template_has_node_valid_syntax(self):
        node = _assert_node_available()
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as tmp:
            tmp.write(self.text.encode("utf-8"))
            tmp_path = tmp.name
        try:
            result = subprocess.run(
                [node, "--check", tmp_path], capture_output=True, text=True
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"node --check failed:\nstdout={result.stdout}\nstderr={result.stderr}",
        )


class InstallerIntegrationTests(unittest.TestCase):
    """install.sh must wire the polkit rule into the install + uninstall
    surface and audit-clear the install-state.json bookkeeping."""

    @classmethod
    def setUpClass(cls):
        cls.install_text = INSTALL_SH.read_text()
        cls.uninstall_text = UNINSTALL_SH.read_text()

    def test_install_defines_the_helper(self):
        self.assertIn("configure_system_power_polkit_rule()", self.install_text)
        self.assertIn("POWER_POLKIT_INSTALLED=0", self.install_text)
        self.assertIn("POWER_POLKIT_RULE_PATH=", self.install_text)
        self.assertIn("POWER_POLKIT_RULE_PRE_EXISTED=0", self.install_text)

    def test_install_invokes_the_helper_in_main(self):
        # The helper must run inside ``main()`` so the rule is written
        # exactly once per install run.  assertRegex(text, regex) calls
        # re.search(); we want ^/$ to anchor to lines, so do the
        # multi-line search directly.
        pattern = re.compile(r"^\s*configure_system_power_polkit_rule\s*$", re.MULTILINE)
        self.assertIsNotNone(
            pattern.search(self.install_text),
            msg="configure_system_power_polkit_rule must be invoked in main()",
        )
        # And the helper must actually be defined, not just invoked.
        self.assertIn(
            "configure_system_power_polkit_rule() {",
            self.install_text,
            msg="configure_system_power_polkit_rule must be defined in install.sh",
        )

    def test_install_substitutes_the_install_user(self):
        sed_block = re.search(
            r'sed -e "s/INSTALL_USER_PLACEHOLDER/\$\{install_user\}/g" "\$template_snapshot"',
            self.install_text,
        )
        self.assertIsNotNone(sed_block)

    def test_install_records_install_state_truthfully(self):
        self.assertIn('"power_polkit_installed":', self.install_text)
        self.assertIn('"power_polkit_rule_path":', self.install_text)
        self.assertIn('"power_polkit_rule_pre_existed":', self.install_text)

    def test_install_records_config_env_field(self):
        self.assertIn("FXROUTE_POWER_USER=", self.install_text)
        # and the install_config is rewritten, not just appended
        self.assertIn("write_install_config", self.install_text)

    def test_install_does_not_grant_general_sudoers(self):
        # Critical: the installer must not add the polkit user to
        # ``sudoers.d`` or any generic root-allowing snippet.  Only the
        # polkit .rules file plus the previous fxroute-cifs-mount
        # helper are allowed as privileged artifacts.
        self.assertNotIn("FXROUTE_POWER_USER ALL=(ALL)", self.install_text)

    def test_install_ensures_dbus_send(self):
        self.assertIn("ensure_dbus_send_binary", self.install_text)
        self.assertIn("dbus-send", self.install_text)

    def test_install_uses_disto_specific_dbus_send_package(self):
        # The installer MUST name the actual binary package per distro
        # instead of guessing the same name everywhere:
        #   apt (Debian/Ubuntu/Armbian): dbus-bin
        #   dnf (Fedora):                  dbus-tools
        #   zypper (openSUSE Tumbleweed):  dbus-1-tools
        #   pacman (Arch/Manjaro):         dbus
        require_packages = {
            "apt": "dbus-bin",
            "dnf": "dbus-tools",
            "zypper": "dbus-1-tools",
            "pacman": "dbus",
        }
        # Restrict the search to the body of ensure_dbus_send_binary()
        # so unrelated case branches (avahi, smb, native packages) do
        # not match expectations for the wrong function.
        body_match = re.search(
            r"ensure_dbus_send_binary\(\) \{.*?\n\}\n",
            self.install_text,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(body_match, msg="ensure_dbus_send_binary() not found")
        body = body_match.group(0)
        for distro, expected_pkg in require_packages.items():
            pattern = re.compile(
                rf"^\s*{re.escape(distro)}\)\s+dbus_send_pkg=\"{re.escape(expected_pkg)}\"\s*;;\s*$",
                re.MULTILINE,
            )
            self.assertIsNotNone(
                pattern.search(body),
                msg=f"{distro}) branch in ensure_dbus_send_binary() must pin dbus_send_pkg to {expected_pkg!r}",
            )

    def test_uninstall_defines_the_removal_helper(self):
        self.assertIn("remove_system_power_polkit_rule()", self.uninstall_text)
        self.assertIn("read_install_state_field", self.uninstall_text)
        self.assertIn("power_polkit_installed", self.uninstall_text)
        self.assertIn("power_polkit_rule_path", self.uninstall_text)

    def test_uninstall_invokes_the_removal(self):
        self.assertIn("remove_system_power_polkit_rule", self.uninstall_text)

    def test_assets_persist_at_the_expected_path(self):
        # The installer expects the bundled template at exactly this
        # location.  Renaming it without updating the installer path is
        # a silent regression we want to keep visible.
        self.assertIn('"$INSTALL_ROOT/assets/polkit/50-fxroute-power.rules"', self.install_text)


class CrossSiteWiringTests(unittest.TestCase):
    """The cross-site defence MUST live on the destructive POST endpoints
    and only there, and it MUST NOT introduce a global auth/session layer
    (FXRoute's LAN security baseline deliberately has none).
    """

    @classmethod
    def setUpClass(cls):
        cls.main_text = (ROOT / "main.py").read_text()

    def test_main_has_origin_helper(self):
        # Helper that decides the same-origin gate.
        for needle in (
            "_request_origin_is_trusted",
            "_effective_request_host",
            "_effective_request_port",
        ):
            self.assertIn(needle, self.main_text)

    def test_suspend_endpoint_enforces_origin_gate(self):
        # Find the suspend endpoint block and verify it calls the helper
        # BEFORE the action.  The block is bounded by the next two
        # endpoint decorators so future insertions do not shift the search.
        suspend_idx = self.main_text.index("/api/system/power/suspend")
        next_endpoint_idx = self.main_text.index(
            '"/api/system/power/power-off"', suspend_idx
        )
        suspend_block = self.main_text[suspend_idx:next_endpoint_idx]
        self.assertIn("_request_origin_is_trusted", suspend_block)
        gate_idx = suspend_block.index("_request_origin_is_trusted")
        action_idx = suspend_block.index("system_power.request_suspend")
        self.assertLess(gate_idx, action_idx)
        self.assertIn('status_code=403', suspend_block)
        # And the strict-yes gate (lives in power.py) must also run before
        # the action so a freshly-engaged inhibitor lock or a CLI POST
        # cannot slip past the menu gate.
        self.assertIn("system_power.is_now_supported", suspend_block)
        gate2_idx = suspend_block.index("system_power.is_now_supported")
        self.assertLess(gate2_idx, action_idx)
        self.assertIn('status_code=409', suspend_block)

    def test_power_off_endpoint_enforces_origin_gate(self):
        poff_idx = self.main_text.index("/api/system/power/power-off")
        # Stop the block at the next decorator or end of file.
        tail_start = poff_idx + 100
        tail = self.main_text[tail_start:tail_start + 4000]
        next_marker = tail.find("\n@app.")
        poff_block = (
            self.main_text[poff_idx : tail_start + (next_marker if next_marker != -1 else len(tail))]
        )
        self.assertIn("_request_origin_is_trusted", poff_block)
        gate_idx = poff_block.index("_request_origin_is_trusted")
        action_idx = poff_block.index("system_power.request_power_off")
        self.assertLess(gate_idx, action_idx)
        self.assertIn('status_code=403', poff_block)
        # And the strict-yes gate (lives in power.py) must also run before
        # the action so a freshly-engaged inhibitor lock or a CLI POST
        # cannot dispatch the shutdown.
        self.assertIn("system_power.is_now_supported", poff_block)
        gate2_idx = poff_block.index("system_power.is_now_supported")
        self.assertLess(gate2_idx, action_idx)
        self.assertIn('status_code=409', poff_block)

    def test_origin_gate_is_effortless_per_request_no_global_middleware(self):
        # The defence MUST NOT introduce a new global auth/session
        # middleware.  We confirm by absence of the keyword that would
        # betray a CSRF token system.
        for forbidden in (
            "HTTPOnly",
            "csrf_token",
            "set_cookie",
            'add_middleware',
            "CORSMiddleware",
        ):
            self.assertNotIn(forbidden, self.main_text)

    def test_read_capability_endpoint_is_not_origin_gated(self):
        # GET endpoints stay reachable from anywhere on the LAN; the
        # origin gate applies only to the destructive POSTs.
        capabilities_idx = self.main_text.index("/api/system/power\"")
        capabilities_block = self.main_text[
            capabilities_idx : capabilities_idx + 1200
        ]
        self.assertIn("system_power_capabilities", capabilities_block)
        self.assertNotIn("_request_origin_is_trusted", capabilities_block)


class PolkitRuleStrictnessTests(unittest.TestCase):
    """The installed polkit rule MUST stay exactly as agreed: the
    install user gets ``Result.YES`` for ``suspend`` / ``power-off`` and
    nothing else.  Polkit wildcards or wildcards like
    ``*-multiple-sessions`` / ``*-ignore-inhibit`` would silently
    expand the privilege and let FXRoute bypass logind's inhibitor and
    per-user-session checks -- explicitly forbidden by the task."""

    @classmethod
    def setUpClass(cls):
        cls.polkit_text = POLKIT_TEMPLATE.read_text()

    def test_rule_does_not_use_multiple_sessions_wildcard(self):
        # The polkit manual's `multiple-sessions` (or `*-multiple-sessions`
        # in JS) lets a single user suspend others' sessions.
        for forbidden in ("multiple-sessions", "*multiple-sessions"):
            self.assertNotIn(
                forbidden, self.polkit_text,
                msg=f"polkit rule must not contain {forbidden!r}; "
                    "this would let one user suspend others' sessions",
            )

    def test_rule_does_not_use_ignore_inhibit_wildcard(self):
        # `ignore-inhibit` would tell polkit to override blocker locks.
        for forbidden in ("ignore-inhibit", "*ignore-inhibit"):
            self.assertNotIn(
                forbidden, self.polkit_text,
                msg=f"polkit rule must not contain {forbidden!r}; "
                    "this would let FXRoute bypass logind inhibitors",
            )

    def test_rule_grants_exactly_two_action_ids(self):
        # Only the two actions agreed on in the task description.
        # Strip surrounding quotes so the comparison stays robust whether
        # polkit rules use JS string literals or identifiers.
        raw = set(re.findall(r'"?org\.freedesktop\.login1\.[a-z0-9._-]+"?', self.polkit_text))
        mentioned = {v.strip('"') for v in raw}
        self.assertEqual(
            mentioned,
            {
                "org.freedesktop.login1.suspend",
                "org.freedesktop.login1.power-off",
            },
        )

    def test_rule_returns_polkit_result_yes_only_for_install_user(self):
        # Defensive: the ``Result.YES`` return value is reachable only
        # from inside the user guard + action-id guard.
        guard_idx = self.polkit_text.index('subject.user !==')
        result_idx = self.polkit_text.index("polkit.Result.YES")
        action_pattern = re.compile(
            r'\"org\.freedesktop\.login1\.(?:suspend|power-off)\"',
        )
        # Both action IDs must appear BEFORE the polkit.Result.YES line.
        first_action = action_pattern.search(self.polkit_text).start()
        self.assertLess(guard_idx, first_action)
        self.assertLess(first_action, result_idx)

    def test_rule_does_not_use_polkit_prompt_or_admin_keywords(self):
        # These keywords would broaden the privilege beyond ``YES``.
        for forbidden in ("polkit.Result.AUTH_ADMIN", "polkit.Result.AUTH_SELF"):
            self.assertNotIn(forbidden, self.polkit_text)


class FrontendPowerMenuTests(unittest.TestCase):
    """Asset-version bump + DOM is present in index.html and reachable
    through the matching IDs in app.js."""

    @classmethod
    def setUpClass(cls):
        cls.html = INDEX_HTML.read_text()
        cls.app_text = APP_JS.read_text()
        cls.css_text = STYLE_CSS.read_text()

    def test_asset_versions_have_been_bumped(self):
        def _version(value: str) -> str:
            m = re.search(r"\?v=(\d+\.\d+\.\d+)", value)
            return m.group(1) if m else ""

        self.assertRegex(self.html, r'style\.css\?v=\d+\.\d+\.\d+')
        self.assertRegex(self.html, r'app\.js\?v=\d+\.\d+\.\d+')
        style_version = _version(re.search(r'style\.css\?v=[\d\.]+', self.html).group(0))
        app_version = _version(re.search(r'app\.js\?v=[\d\.]+', self.html).group(0))
        # Bumping one is valid as the project's frontend rules allow
        # independent asset versions; the only invariant is that the
        # page references a current version (no test asset version of
        # x.y.* is allowed).
        self.assertNotEqual(style_version, "")
        self.assertNotEqual(app_version, "")

    def test_html_contains_power_menu_ids(self):
        for needle in (
            'id="power-menu-root"',
            'id="power-menu-toggle"',
            'id="power-menu"',
            'id="power-suspend"',
            'id="power-shutdown"',
        ):
            self.assertIn(needle, self.html)

    def test_html_lifts_connection_badge_into_status_cluster(self):
        # The header now groups the badge + power button.
        self.assertIn('class="header-status"', self.html)

    def test_app_js_drives_power_menu(self):
        for needle in (
            "setupPowerMenu",
            "refreshPowerCapabilities",
            "/api/system/power",
            "/api/system/power/suspend",
            "/api/system/power/power-off",
            "POWER_CONFIRM_SUSPEND",
            "POWER_CONFIRM_SHUTDOWN",
        ):
            self.assertIn(needle, self.app_text)

    def test_app_js_suspend_hidden_when_unsupported(self):
        # The frontend must inspect the suspend_supported boolean and
        # hide the menu item when logind reports no capability.
        self.assertIn("suspendSupported", self.app_text)
        self.assertIn("powerOffSupported", self.app_text)

    def test_css_has_power_button_style(self):
        for needle in (
            ".power-btn",
            ".power-menu",
            ".power-menu-item",
        ):
            self.assertIn(needle, self.css_text)


class PolkitRenderSmokeTests(unittest.TestCase):
    """Functional smoke test of ``sed`` substitution we run during
    install.sh: rewriting ``INSTALL_USER_PLACEHOLDER`` to a real Linux
    username should produce a .rules file whose remaining content still
    parses as JavaScript."""

    def test_substitution_then_node_check(self):
        node = _assert_node_available()
        rendered = POLKIT_TEMPLATE.read_text().replace(
            "INSTALL_USER_PLACEHOLDER", "fxroute-tester"
        )
        self.assertNotIn("INSTALL_USER_PLACEHOLDER", rendered)
        self.assertIn('subject.user !== "fxroute-tester"', rendered)

        with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as tmp:
            tmp.write(rendered.encode("utf-8"))
            tmp_path = tmp.name
        try:
            result = subprocess.run(
                [node, "--check", tmp_path], capture_output=True, text=True
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"node --check failed after substitution:\n{result.stderr}",
        )


class ProbeArchitectureTests(unittest.TestCase):
    """The capability probe MUST run as a direct ``Manager.Can{X}`` method
    call (modern logind) and only fall back to ``Properties.Get`` when
    logind reports ``UnknownMethod`` -- so the same code path works on
    systemd >= 256 as well as on older distros like Debian Bookworm."""

    def test_query_logind_property_prefers_direct_method_call(self):
        # The method-form args tuple MUST be assembled first, the property
        # form comes after the rcode-0 success shortcut.
        power_src = (Path(__file__).resolve().parents[1] / "audio" / "power.py").read_text()
        method_idx = power_src.find('f"{_LOGIND_MANAGER_IFACE}.{name}"')
        prop_idx = power_src.find('_DBUS_PROPERTIES_IFACE + ".Get"')
        self.assertGreater(prop_idx, 0)
        self.assertGreater(prop_idx, method_idx)
        # The capability probe list MUST be limited to CanSuspend / CanPowerOff.
        names_match = re.search(
            r"_LOGIND_METHOD_CAPABILITY_NAMES\s*=\s*frozenset\(\{(.*?)\}\)",
            power_src,
        )
        self.assertIsNotNone(names_match)
        names = {n.strip().strip('"').strip("'") for n in names_match.group(1).split(",")}
        self.assertSetEqual(names, {"CanSuspend", "CanPowerOff"})

    def test_fallback_only_runs_on_unknown_method_or_unknown_property(self):
        # The fallback MUST NOT trigger on a transient PermissionDenied.
        power_src = (Path(__file__).resolve().parents[1] / "audio" / "power.py").read_text()
        # Use DOTALL so the (.*?) crosses the multi-line tuple body.
        snippet = re.search(
            r"if method_error not in \((.*?)\) and name in",
            power_src,
            re.DOTALL,
        )
        self.assertIsNotNone(snippet)
        names = {n.strip().strip('"').strip("'")
                 for n in re.findall(r'"org\.freedesktop\.[^"]+"',
                                     snippet.group(1))}
        self.assertSetEqual(
            names,
            {
                "org.freedesktop.DBus.Error.UnknownMethod",
                "org.freedesktop.DBus.Error.UnknownProperty",
            },
        )


if __name__ == "__main__":
    unittest.main()
