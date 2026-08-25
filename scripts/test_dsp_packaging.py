#!/usr/bin/env python3
"""Packaging contract for the FXRoute-owned native DSP path."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DspPackagingTests(unittest.TestCase):
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
