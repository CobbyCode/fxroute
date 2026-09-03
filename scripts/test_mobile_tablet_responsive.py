#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Static regression checks for the mobile/tablet frontend fixes (review items 1-9).

Item 10 (stereo meter hidden at <=390px) is deliberate behavior and is
guarded as unchanged.

Must run via: python3 scripts/test_mobile_tablet_responsive.py (supports -v)
"""

import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
RESPONSIVE = (ROOT / "static" / "css" / "_responsive.css").read_text(encoding="utf-8")


def _media_block(css: str, header: str) -> str:
    """Return the concatenated bodies of all @media blocks with this header."""
    bodies = []
    idx = 0
    while True:
        start = css.find(header, idx)
        if start == -1:
            break
        brace = css.find("{", start)
        if brace == -1:
            break
        depth = 0
        for j in range(brace, len(css)):
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    bodies.append(css[brace + 1 : j])
                    idx = j + 1
                    break
        else:
            break
    if not bodies:
        raise AssertionError(f"Missing @media block: {header}")
    return "\n".join(bodies)


class IosZoomGuardTests(unittest.TestCase):
    """Item 1: phone inputs stay at 16px so iOS Safari does not auto-zoom."""

    def test_phone_inputs_are_16px(self):
        phone = _media_block(RESPONSIVE, "@media (max-width: 760px)")
        for selector in (
            ".station-search-input",
            ".library-search-input",
            ".streaming-search-input",
            ".url-input",
            "select.url-input",
        ):
            self.assertIn(selector, phone, f"missing {selector} in 760px zoom guard")
        self.assertIn("font-size: 16px", phone)

    def test_desktop_compact_sizes_unchanged(self):
        radio = (ROOT / "static" / "css" / "_radio.css").read_text(encoding="utf-8")
        self.assertIn(".station-search-input", radio)
        self.assertRegex(radio, r"\.station-search-input\s*\{[^}]*font-size: 0\.86rem")


class SliderHitAreaTests(unittest.TestCase):
    """Item 2: larger touch target without changing the visible track."""

    def test_hit_area_expansion_without_visual_change(self):
        self.assertRegex(
            CSS,
            r"\.seek-slider,\s*\n\.volume-slider\s*\{[^}]*margin-block: -10px[^}]*padding-block: 10px",
        )
        self.assertIn("background-clip: content-box", CSS)
        self.assertIn("box-sizing: content-box", CSS)
        self.assertIn(".seek-slider { height: 5px; }", CSS)
        self.assertIn(".volume-slider { height: 5px; }", CSS)

    def test_thumb_geometry_unchanged(self):
        self.assertRegex(
            CSS,
            r"\.seek-slider::\-webkit-slider-thumb\s*\{\s*width: 13px; height: 13px",
        )
        self.assertRegex(
            CSS,
            r"\.volume-slider::\-webkit-slider-thumb\s*\{\s*width: 14px; height: 14px",
        )


class CoverDetailClearanceTests(unittest.TestCase):
    """Item 3: cover card clears the live footer height on every viewport."""

    def test_bottom_uses_dynamic_footer_space(self):
        self.assertIn("bottom: var(--playback-footer-space,", CSS)

    def test_no_hardcoded_footer_height(self):
        self.assertNotIn("bottom: calc(102px + 8px + 14px)", CSS)

    def test_cover_width_has_no_vw_overflow(self):
        self.assertIn("width: min(720px, calc(100% - 2rem))", CSS)
        self.assertIn("width: min(400px, calc(100% - 1.2rem))", CSS)


class LibraryPhoneLayoutTests(unittest.TestCase):
    """Item 4: search toolbar and playlist save row fit one- and two-button states."""

    def test_search_wrap_is_single_column(self):
        phone = _media_block(RESPONSIVE, "@media (max-width: 600px)")
        self.assertIn("grid-template-columns: minmax(0, 1fr);", phone)
        self.assertNotIn("grid-template-columns: minmax(0, 1fr) 104px;", phone)

    def test_toolbar_buttons_share_the_row(self):
        phone = _media_block(RESPONSIVE, "@media (max-width: 600px)")
        self.assertRegex(phone, r"\.library-selection-toolbar\s*\{[^}]*flex-wrap: wrap")
        self.assertRegex(
            phone, r"\.library-selection-toolbar \.btn-secondary\s*\{[^}]*flex: 1 1 0"
        )

    def test_playlist_save_row_fits_three_children(self):
        phone = _media_block(RESPONSIVE, "@media (max-width: 600px)")
        self.assertRegex(
            phone, r"\.playlist-save-controls\s*\{[^}]*grid-template-columns: minmax\(0, 1fr\) minmax\(0, 1fr\)"
        )
        self.assertRegex(phone, r"\.playlist-save-row \.url-input\s*\{[^}]*grid-column: 1 / -1")
        self.assertIn("#cancel-playlist-selection", phone)

    def test_playlist_markup_still_has_three_controls(self):
        match = re.search(
            r'<div class="playlist-save-controls">.*?</div>', HTML, re.DOTALL
        )
        self.assertIsNotNone(match, "missing .playlist-save-controls markup")
        assert match is not None
        controls = match.group(0)
        self.assertIn('id="playlist-name"', controls)
        self.assertIn('id="save-playlist"', controls)
        self.assertIn('id="cancel-playlist-selection"', controls)


class GraphScrollTests(unittest.TestCase):
    """Item 5: vertical page scroll still starts on the graphs."""

    def test_graphs_allow_vertical_pan(self):
        self.assertIn(".measurement-graph", CSS)
        self.assertIn(".effects-subwoofer-preview", CSS)
        self.assertNotRegex(CSS, r"touch-action:\s*none")

    def test_pan_y_present_for_both_graphs(self):
        self.assertEqual(CSS.count("touch-action: pan-y"), 2)


class ViewportUnitTests(unittest.TestCase):
    """Items 6+7: no vw-based fixed widths; dvh with vh fallback."""

    def test_no_vw_in_fixed_shell_widths(self):
        for bad in (
            "calc(100vw - 20px)",
            "calc(100vw - 16px)",
            "calc(100vw - 12px)",
            "calc(100vw - 1rem)",
            "calc(100vw - 1.2rem)",
        ):
            self.assertNotIn(bad, CSS, f"vw-based width still present: {bad}")
        # One intentional exception: the absolutely-positioned sweep menu
        # panel caps itself to the viewport; it is hidden by default and its
        # containing block is the 190px menu, so % cannot express the cap.
        self.assertIn(".measurement-workflow-menu-panel", CSS)

    def test_footer_centering_uses_percent(self):
        self.assertIn("left: 50%;", CSS)
        self.assertNotIn("left: 50vw", CSS)

    def test_dvh_with_vh_fallback(self):
        self.assertIn("min-height: 100vh;\n    min-height: 100dvh;", CSS)
        self.assertIn("max-height: calc(100vh - 2rem);\n    max-height: calc(100dvh - 2rem);", CSS)


class TouchTargetTests(unittest.TestCase):
    """Item 8: small controls reach ~44px without moving desktop layout."""

    def test_coarse_hit_area_expansion(self):
        coarse = _media_block(RESPONSIVE, "@media (hover: none), (pointer: coarse)")
        for selector in (
            ".control-btn-mode::after",
            ".control-btn-clear::after",
            ".track-favorite-btn::after",
            ".album-card-fav::after",
            ".stepper-btn::after",
            ".measurement-chip::after",
        ):
            self.assertIn(selector, coarse, f"missing {selector} hit expansion")
        self.assertIn("inset: -8px", coarse)

    def test_phone_direct_sizes(self):
        phone = _media_block(RESPONSIVE, "@media (max-width: 600px)")
        self.assertRegex(phone, r"\.catalog-station-action\s*\{[^}]*min-height: 44px")
        self.assertRegex(
            phone, r"\.dialog-header-corner-close \.btn-close-manage\s*\{[^}]*width: 44px"
        )
        self.assertRegex(phone, r"\.measurement-chip\s*\{[^}]*min-height: 44px")


class SafeAreaTests(unittest.TestCase):
    """Item 9: every footer range honors the home-indicator safe area."""

    def test_all_ranges_use_safe_area(self):
        self.assertIn("bottom: max(6px, env(safe-area-inset-bottom))", CSS)
        self.assertIn("bottom: max(5px, env(safe-area-inset-bottom))", CSS)
        self.assertIn("bottom: max(3px, env(safe-area-inset-bottom))", CSS)


class DeliberateBehaviorGuardTests(unittest.TestCase):
    """Item 10 stays as designed: meter hidden on very small phones."""

    def test_small_phone_meter_hidden(self):
        self.assertRegex(
            RESPONSIVE,
            r"@media \(max-width: 390px\)[\s\S]*?\.playback-meter\s*\{\s*display: none",
        )


if __name__ == "__main__":
    if "-v" in sys.argv:
        sys.argv.remove("-v")
        unittest.main(verbosity=2)
    else:
        unittest.main()
