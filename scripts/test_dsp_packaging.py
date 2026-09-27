#!/usr/bin/env python3
"""Packaging contract for the FXRoute-owned native DSP path."""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


LV2INFO_EXCERPT = (ROOT / "scripts" / "fixtures" / "lv2info_lsp_para_equalizer_excerpt.txt").read_text()
REQUIRED_LV2_URIS = (
    "http://lsp-plug.in/plugins/lv2/para_equalizer_x32_lr",
    "http://lsp-plug.in/plugins/lv2/loud_comp_stereo",
    "http://lsp-plug.in/plugins/lv2/sc_limiter_stereo",
    "urn:zamaudio:ZaMaximX2",
    "http://calf.sourceforge.net/plugins/BassEnhancer",
)


def shell_function(script, name):
    start = script.index(f"{name}() {{")
    return script[start:script.index("\n}\n", start) + 3]


def run_install_functions(call, *, lv2info=None, lv2info_exit=0, awk="gawk"):
    """Run install.sh's LV2 checks with only awk/grep/cat and stubbed tools.

    lv2info None leaves it absent; lv2ls always lists every required URI.
    """
    script = (ROOT / "install.sh").read_text()
    functions = "\n".join(shell_function(script, name)
                          for name in ("lsp_peq_apo_dr_status", "verify_lv2_plugins"))
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir()
        program, *flags = awk.split()
        (bin_dir / "awk").write_text(
            f"#!/bin/sh\nexec {shutil.which(program)} {' '.join(flags)} \"$@\"\n")
        for tool in ("grep", "cat"):
            (bin_dir / tool).symlink_to(shutil.which(tool))
        uris = " ".join(f"'{uri}'" for uri in REQUIRED_LV2_URIS)
        (bin_dir / "lv2ls").write_text(f"#!/bin/sh\nprintf '%s\\n' {uris}\n")
        if lv2info is not None:
            listing = Path(tmp) / "lv2info.txt"
            listing.write_bytes(lv2info.encode())
            (bin_dir / "lv2info").write_text(
                f"#!/bin/sh\n{shutil.which('cat')} '{listing}'\nexit {lv2info_exit}\n")
        for stub in bin_dir.iterdir():
            if not stub.is_symlink():
                stub.chmod(0o755)
        harness = ("set -euo pipefail\n"
                   'pass() { echo "PASS: $*"; }\nfail() { echo "FAIL: $*"; }\n'
                   'die() { echo "DIE: $*"; exit 1; }\n' + functions + "\n" + call + "\n")
        return subprocess.run([shutil.which("bash"), "-c", harness],
                              env={"PATH": str(bin_dir)}, capture_output=True, text=True)


class DspPackagingTests(unittest.TestCase):
    def test_fxroute_service_leaves_realtime_policy_to_pipewire(self):
        service = (ROOT / "fxroute.service").read_text()
        installer = (ROOT / "install.sh").read_text()
        for text in (service, installer):
            self.assertNotIn("LimitRTPRIO", text)
            self.assertNotIn("LimitMEMLOCK", text)

    def test_native_dsp_leaves_realtime_thread_policy_to_pipewire(self):
        source = (ROOT / "native_dsp/pipewire_engine.c").read_text()
        self.assertNotIn("configure_realtime_thread", source)
        self.assertNotIn("pthread_setschedparam", source)

    def test_installer_builds_native_dsp_without_easyeffects_runtime(self):
        script = (ROOT / "install.sh").read_text()
        self.assertIn('build_native_dsp_engine()', script)
        self.assertIn('native_dsp/build.sh', script)
        self.assertNotIn('flatpak install --user', script)
        self.assertNotIn('install_watchdog_if_needed', script)
        self.assertNotIn('setup_easyeffects_autostart', script)

    def test_pipewire_build_does_not_apply_pedantic_to_system_headers(self):
        script = (ROOT / "native_dsp/build.sh").read_text().splitlines()
        pipewire_command = next(line for line in script if "pipewire_engine.c" in line)
        self.assertIn("-std=gnu11", pipewire_command)
        self.assertNotIn("-pedantic", pipewire_command)

    def test_installer_provisions_native_dsp_plugin_dependencies(self):
        script = (ROOT / "install.sh").read_text()
        for dependency in ("liblilv-0-devel", "lv2-lsp-plugins",
                           "lv2-zam-plugins", "libebur128-devel",
                           "libsamplerate0-dev", "libsamplerate-devel",
                           "libspeexdsp-dev", "speexdsp-devel",
                           "libebur128 libsamplerate speexdsp", "calf-plugins",
                           "lv2-calf-plugins", "zam-plugins calf"):
            self.assertIn(dependency, script)

    def test_debian_native_dsp_provisions_standard_c_headers(self):
        script = (ROOT / "install.sh").read_text()
        self.assertRegex(script, r"apt\) dsp_packages=\([^)]*\blibc6-dev\b")

    def test_fedora_dsp_packages_use_dnf_names(self):
        # Fedora ships the ZamAudio LV2 bundle as 'lv2-zam-plugins';
        # 'zam-plugins-lv2' (Debian-style) does not exist in dnf and made
        # the whole install transaction fail with 'No match for argument'.
        script = (ROOT / "install.sh").read_text()
        dnf_list = re.search(r"dnf\) dsp_packages=\(([^)]*)\)", script).group(1)
        self.assertNotIn("zam-plugins-lv2", dnf_list)
        self.assertIn("lv2-zam-plugins", dnf_list)

    def test_opensuse_builds_pinned_calf_lv2_when_package_is_unavailable(self):
        script = (ROOT / "install.sh").read_text()
        self.assertIn('install_calf_lv2_from_source()', script)
        self.assertIn('version="0.90.9"', script)
        self.assertIn('2d304eed88e87438b2b8857a2f4480046bf4003bce2e17a042abdbbf7d59122f', script)
        self.assertIn('-DWANT_GUI=OFF', script)
        self.assertIn('-DWANT_JACK=OFF', script)
        self.assertIn('"$HOME/.lv2/calf.lv2"', script)
        self.assertIn('mv "$candidate" "$HOME/.lv2/calf.lv2"', script)

    def test_source_build_cleanup_does_not_expand_local_work_after_return(self):
        script = (ROOT / "install.sh").read_text()
        self.assertNotIn("trap 'rm -rf \"$work\"' RETURN", script)
        self.assertIn("cleanup_active_temp_dir", script)
        self.assertIn("FXROUTE_ACTIVE_TEMP_DIR", script)

    def test_installer_verifies_required_lv2_plugin_uris(self):
        script = (ROOT / "install.sh").read_text()
        self.assertGreaterEqual(script.count("verify_lv2_plugins"), 2)
        self.assertIn("lv2ls", script)
        self.assertIn('discovered="$(lv2ls 2>/dev/null || true)"', script)
        self.assertIn('die "FXRoute DSP effects need these LV2 plugins:', script)
        self.assertIn('die "LV2 plugin verification needs lv2ls', script)
        for uri in (
            "http://lsp-plug.in/plugins/lv2/para_equalizer_x32_lr",
            "http://lsp-plug.in/plugins/lv2/loud_comp_stereo",
            "http://lsp-plug.in/plugins/lv2/sc_limiter_stereo",
            "urn:zamaudio:ZaMaximX2",
            "http://calf.sourceforge.net/plugins/BassEnhancer",
        ):
            self.assertIn(uri, script)
            self.assertIn('grep -Fxq "$uri" <<<"$discovered"', script)

    def test_installer_requires_the_lsp_apo_dr_filter_mode(self):
        # Global PEQ sends LSP filter mode 6, APO (DR); a plugin without it
        # would clamp the mode to another filter design, so install fails.
        script = (ROOT / "install.sh").read_text()
        verify = shell_function(script, "verify_lv2_plugins")
        self.assertIn("lsp_peq_apo_dr_status || apo_status=$?", verify)
        self.assertIn('die "FXRoute Global PEQ needs LSP Plugins 1.1.7 or newer', verify)

    def test_apo_dr_check_reads_real_and_variant_lv2info_formats(self):
        """Port-scoped, numeric and whitespace/CR tolerant, awk-portable."""
        real = LV2INFO_EXCERPT
        apo = '\t\t\t6 = "APO (DR)"\n'
        self.assertIn(apo, real)
        cases = {
            "real lilv 0.28 output": (real, 0),
            "CRLF line endings": (real.replace("\n", "\r\n"), 0),
            "spaces and a float value": (real.replace("\t", "    ").replace(
                '6 = "APO (DR)"', '6.000000 = "APO (DR)"'), 0),
            "LSP without APO (DR), other ports keep value 6": (real.replace(apo, ""), 1),
            "APO (DR) only on a non filter-mode port": (
                real.replace(apo, "").replace('\t\t\t6 = "Notch"\n', '\t\t\t6 = "APO (DR)"\n'), 1),
            "APO (DR) under another value": (
                real.replace(apo, '\t\t\t7 = "APO (DR)"\n'), 1),
        }
        for awk in ("gawk", "gawk --posix"):
            for label, (listing, expected) in cases.items():
                with self.subTest(awk=awk, case=label):
                    result = run_install_functions(
                        "status=0; lsp_peq_apo_dr_status || status=$?; echo status=$status",
                        lv2info=listing, awk=awk)
                    self.assertIn(f"status={expected}", result.stdout, result.stderr)

    def test_apo_dr_check_diagnoses_a_missing_or_failing_lv2info(self):
        call = "status=0; lsp_peq_apo_dr_status || status=$?; echo status=$status"
        self.assertIn("status=2", run_install_functions(call).stdout)
        self.assertIn("status=3", run_install_functions(call, lv2info=LV2INFO_EXCERPT,
                                                          lv2info_exit=1).stdout)
        self.assertIn("status=3", run_install_functions(call, lv2info="").stdout)

    def test_verify_lv2_plugins_names_the_actual_apo_dr_problem(self):
        ok = run_install_functions("verify_lv2_plugins", lv2info=LV2INFO_EXCERPT)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("PASS: LSP Parametric Equalizer offers filter mode APO (DR)", ok.stdout)
        for kwargs, expected in (
            ({}, "DIE: LSP filter mode verification needs lv2info from lilv-utils"),
            ({"lv2info": LV2INFO_EXCERPT, "lv2info_exit": 1}, "DIE: lv2info could not describe"),
            ({"lv2info": LV2INFO_EXCERPT.replace('\t\t\t6 = "APO (DR)"\n', "")},
             "DIE: FXRoute Global PEQ needs LSP Plugins 1.1.7 or newer"),
        ):
            with self.subTest(expected=expected):
                result = run_install_functions("verify_lv2_plugins", **kwargs)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(expected, result.stdout)
                self.assertEqual(result.stdout.count("DIE:"), 1, result.stdout)

    def test_native_dsp_links_libsamplerate(self):
        script = (ROOT / "native_dsp/build.sh").read_text()
        self.assertIn("pkg-config --cflags samplerate", script)
        self.assertIn("pkg-config --libs samplerate", script)

    def test_native_dsp_links_speexdsp_for_crystalizer(self):
        script = (ROOT / "native_dsp/build.sh").read_text()
        self.assertIn("pkg-config --cflags speexdsp", script)
        self.assertIn("pkg-config --libs speexdsp", script)
        crystalizer_commands = [line for line in script.splitlines()
                                if "crystalizer.c" in line]
        self.assertTrue(crystalizer_commands)
        for command in crystalizer_commands:
            self.assertIn("$SPEEXDSP_CFLAGS", command)
            self.assertIn("$SPEEXDSP_LIBS", command)

    def test_updater_rebuilds_native_dsp_from_all_engine_sources(self):
        script = (ROOT / "scripts/update_fxroute.sh").read_text()
        self.assertIn('build_native_dsp_if_needed()', script)
        self.assertIn('native_dsp/build/fxroute-dsp', script)
        self.assertIn('find "$source_dir" -type f -newer "$binary"', script)

    def test_runtime_integration_uses_fxroute_owned_nodes(self):
        transition = (ROOT / "playback/transition/models.py").read_text()
        peak_monitor = (ROOT / "dsp/peak_monitor.py").read_text()
        samplerate = (ROOT / "audio/samplerate/constants.py").read_text()
        self.assertIn('DSP_TRANSPORT_SINK = "fxroute_dsp_sink"', transition)
        self.assertIn('DSP_OUTPUT_NODE_NAME = "fxroute_dsp"', peak_monitor)
        self.assertIn('"fxroute_dsp_sink"', samplerate)
        self.assertNotIn('"easyeffects_sink"', samplerate)

    def test_legacy_watchdog_assets_are_removed(self):
        self.assertFalse((ROOT / "scripts/easyeffects-stale-watchdog.sh").exists())
        self.assertFalse((ROOT / "systemd-user/easyeffects-stale-watchdog.service").exists())
        self.assertFalse((ROOT / "systemd-user/easyeffects-stale-watchdog.timer").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
