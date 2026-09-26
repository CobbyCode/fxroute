#!/usr/bin/env python3
"""Regression guard for the UI polish pass.

Covers the concrete frontend findings that were fixed:

1. Toast column clears the whole sticky top stack (header row + tab strip).
2. The active main tab is scrolled back into the strip on narrow screens.
3. ``--warning`` clears WCAG AA on every surface of the dark theme.
4. The A/B compare card fills the grid row instead of trailing dead space.
5. The streaming seek bar keeps a ~4px track but a ~24px hit area.
6. Primary controls reach the ~44px touch minimum.
7. Long A/B preset names stay reachable through the app tooltip.
8. One tooltip system: ``data-tooltip`` everywhere, no native ``title=``.
9. Phone chrome budget for the header/tab stack and the playback footer.

The CSS assertions read the per-part sources in ``static/css/`` (the
canonical build input) plus the built ``static/style.css`` artifact, so a
change that only lands in one of the two is caught.
"""

import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSS_DIR = ROOT / "static" / "css"
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
RESPONSIVE = (CSS_DIR / "_responsive.css").read_text(encoding="utf-8")
TOKENS = (CSS_DIR / "_tokens.css").read_text(encoding="utf-8")
OVERLAYS = (CSS_DIR / "_overlays.css").read_text(encoding="utf-8")
LAYOUT = (CSS_DIR / "_layout.css").read_text(encoding="utf-8")
EFFECTS = (CSS_DIR / "_effects.css").read_text(encoding="utf-8")
STREAMING = (CSS_DIR / "_streaming.css").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

# Every surface a token can land on, matching the audit that found the
# original --warning failing on all of them.
THEME_SURFACES = {
    "bg-primary": "#0d0d0f",
    "bg-surface": "#161619",
    "bg-elevated": "#1e1e22",
    "bg-input": "#1c1c20",
    "bg-hover": "#26262b",
    "canvas-deep": "#08111f",
}

SURFACE_TOKENS = (
    "--bg-primary", "--bg-surface", "--bg-elevated",
    "--bg-input", "--bg-hover", "--canvas-deep",
)


def _relative_luminance(hex_colour: str) -> float:
    value = hex_colour.lstrip("#")
    channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]

    def linear(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: str, bg: str) -> float:
    a, b = _relative_luminance(fg), _relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def token_value(name: str, source: str = TOKENS) -> str:
    match = re.search(rf"^\s*{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})\s*;", source, re.M)
    if not match:
        raise AssertionError(f"token {name} not found")
    return match.group(1)


def _media_block(source: str, header: str) -> str:
    start = source.find(header)
    if start < 0:
        raise AssertionError(f"media block not found: {header}")
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError(f"unterminated media block: {header}")


class ToastPlacementTests(unittest.TestCase):
    """1. Toasts anchor below the whole sticky top stack."""

    def test_stack_height_token_exists(self):
        self.assertIn("--header-stack-height", TOKENS)
        self.assertIn("--header-stack-height", CSS)

    def test_toast_top_uses_the_stack_token(self):
        self.assertRegex(
            OVERLAYS,
            r"\.toast-container\s*\{[^}]*top:\s*calc\(var\(--header-stack-height\)",
        )

    def test_toast_no_longer_pins_to_a_fixed_inset(self):
        body = re.search(r"\.toast-container\s*\{([^}]*)\}", OVERLAYS).group(1)
        self.assertNotRegex(body, r"top:\s*1rem")

    def test_app_publishes_the_measured_stack_height(self):
        self.assertIn("function updateHeaderStackHeight()", APP_JS)
        self.assertIn("--header-stack-height", APP_JS)
        # Both sticky rows have to be measured; the tab strip is a separate
        # row below 1180px and shares the header row at/above it.
        self.assertIn("'.header'", APP_JS)
        self.assertIn("'.tabs'", APP_JS)

    def test_stack_height_is_scroll_independent(self):
        body = re.search(
            r"function updateHeaderStackHeight\(\)\s*\{([\s\S]*?)\n\}", APP_JS
        ).group(1)
        # Neither element is sticky, so a viewport-relative bottom turns
        # negative while scrolling and would pull the toast off screen.
        self.assertNotIn("getBoundingClientRect", body)
        self.assertIn("offsetTop", body)
        self.assertIn("offsetHeight", body)

    def test_resize_and_tab_switch_refresh_the_offset(self):
        self.assertIn("window.addEventListener('resize', updateTabsScrollAffordance)", APP_JS)
        self.assertRegex(
            APP_JS,
            r"function updateTabsScrollAffordance\(\)[\s\S]*?updateHeaderStackHeight\(\)",
        )


class ActiveTabVisibilityTests(unittest.TestCase):
    """2. The active tab is scrolled back into the strip."""

    def test_helper_exists_and_targets_the_strip(self):
        self.assertIn("function keepActiveTabInView()", APP_JS)
        body = re.search(
            r"function keepActiveTabInView\(\)\s*\{([\s\S]*?)\n\}", APP_JS
        ).group(1)
        # Only the strip may scroll, never the page.
        self.assertNotIn("scrollIntoView", body)
        self.assertIn("nav.scrollLeft", body)
        self.assertIn("navBox", body)
        self.assertIn("tabBox", body)

    def test_no_op_when_the_strip_does_not_overflow(self):
        body = re.search(
            r"function keepActiveTabInView\(\)\s*\{([\s\S]*?)\n\}", APP_JS
        ).group(1)
        self.assertRegex(body, r"if \(nav\.scrollWidth <= nav\.clientWidth \+ 2\) return")

    def test_switch_tab_calls_the_helper(self):
        body = re.search(r"function switchTab\(tabId\)\s*\{([\s\S]*?)\n\}", APP_JS).group(1)
        self.assertIn("keepActiveTabInView()", body)

    def test_resize_path_calls_the_helper(self):
        self.assertRegex(
            APP_JS,
            r"function updateTabsScrollAffordance\(\)[\s\S]*?keepActiveTabInView\(\)",
        )


class WarningContrastTests(unittest.TestCase):
    """3. --warning clears WCAG AA (4.5:1) on every theme surface."""

    def test_warning_is_a_hex_token(self):
        self.assertRegex(TOKENS, r"--warning:\s*#[0-9a-fA-F]{6};")

    def test_warning_passes_aa_on_every_surface(self):
        fg = token_value("--warning")
        failures = {
            name: round(contrast_ratio(fg, value), 2)
            for name, value in THEME_SURFACES.items()
            if contrast_ratio(fg, value) < 4.5
        }
        self.assertEqual(failures, {}, f"--warning {fg} below AA: {failures}")

    def test_all_declared_surfaces_have_a_literal(self):
        for name in SURFACE_TOKENS:
            self.assertRegex(
                TOKENS, rf"(?m)^\s*{re.escape(name)}:\s*#[0-9a-fA-F]{{6}}\s*;"
            )

    def test_text_muted_still_reads_on_the_hover_surface(self):
        ratio = contrast_ratio(token_value("--text-muted"), THEME_SURFACES["bg-hover"])
        self.assertGreaterEqual(ratio, 4.5, f"--text-muted on --bg-hover is {ratio:.2f}")


class AbCompareLayoutTests(unittest.TestCase):
    """4. The A/B card is not padded out with dead space."""

    def test_grid_does_not_stretch_cards_to_the_row_height(self):
        grid = re.search(r"\.effects-grid\s*\{([^}]*)\}", EFFECTS).group(1)
        # stretch was the source of the ~140px void in the A/B card.
        self.assertRegex(grid, r"align-items:\s*start")
        self.assertNotIn("align-items: stretch", grid)

    def test_tile_starts_a_little_higher(self):
        row = re.search(r"\.effects-compare-row\s*\{([^}]*)\}", EFFECTS).group(1)
        padding = re.search(r"padding:\s*([\d.]+)rem", row)
        self.assertIsNotNone(padding)
        self.assertLessEqual(float(padding.group(1)), 0.9)

    def test_compare_controls_keep_their_natural_height(self):
        slot = re.search(r"\.effects-compare-slot\s*\{([^}]*)\}", EFFECTS).group(1)
        self.assertNotIn("flex: 1", slot)
        self.assertNotIn("justify-content", slot)


class StreamingSeekTargetTests(unittest.TestCase):
    """5. The streaming seek bar keeps a slim track with a real hit area."""

    def test_track_stays_slim(self):
        self.assertRegex(STREAMING, r"\.streaming-progress\s*\{[^}]*height:\s*4px")

    def test_hit_area_is_expanded_without_changing_the_track(self):
        body = re.search(r"\.streaming-progress\s*\{([^}]*)\}", STREAMING).group(1)
        self.assertRegex(body, r"padding-block:\s*10px")
        self.assertRegex(body, r"margin-block:\s*-10px")
        self.assertRegex(body, r"box-sizing:\s*content-box")
        self.assertIn("background-clip", body)

    def test_coarse_pointer_thumb_reaches_the_touch_minimum(self):
        coarse = _media_block(STREAMING, "@media (hover: none), (pointer: coarse)")
        self.assertRegex(coarse, r"\.streaming-progress::\-webkit-slider-thumb\s*\{[^}]*width:\s*20px")


class TouchTargetTests(unittest.TestCase):
    """6. Primary controls reach the ~44px touch minimum."""

    def test_main_tabs(self):
        self.assertRegex(LAYOUT, r"\.tab-btn\s*\{[^}]*min-height:\s*44px")

    def test_power_button(self):
        self.assertRegex(LAYOUT, r"\.power-btn\s*\{[^}]*width:\s*44px[^}]*height:\s*44px")

    def test_settings_trigger(self):
        self.assertRegex(
            LAYOUT, r"\.brand-lockup-button\s*\{[^}]*min-height:\s*44px"
        )

    def test_crossover_chips_and_link_toggle_grow_on_coarse_pointers(self):
        coarse = _media_block(RESPONSIVE, "@media (hover: none), (pointer: coarse)")
        self.assertRegex(
            coarse, r"\.crossover-tab,\s*\.sub-side-tab\s*\{[^}]*min-height:\s*44px"
        )
        self.assertRegex(coarse, r"\.crossover-link-label\s*\{[^}]*min-height:\s*44px")
        # Chips must grow their own box, not an overlay: neighbours sit 0.4rem
        # apart and expanded overlays would overlap and steal taps.
        self.assertNotIn(".crossover-tab::before", coarse)
        self.assertNotIn(".crossover-tab::after", coarse)

    def test_brand_mark_keeps_its_rendered_size(self):
        self.assertRegex(LAYOUT, r"\.brand-lockup \.brand-mark\s*\{[^}]*width:\s*32px[^}]*height:\s*32px")


class TooltipTests(unittest.TestCase):
    """7 + 8. One tooltip system, and long names reachable through it."""

    def test_generic_attribute_selector_drives_the_tooltip(self):
        self.assertRegex(LAYOUT, r"\[data-tooltip\]::after\s*\{[^}]*content:\s*attr\(data-tooltip\)")
        self.assertRegex(LAYOUT, r"\[data-tooltip\]::after\s*\{[^}]*position:\s*absolute")

    def test_legacy_header_tooltip_contracts_still_hold(self):
        # test_frontend_accessibility_polish.py asserts these exact shapes.
        self.assertIn(".brand-lockup-button[data-tooltip]::after", LAYOUT)
        self.assertIn(".brand-lockup-button:focus-visible::after", LAYOUT)
        self.assertIn(".power-btn[data-tooltip]::after", LAYOUT)
        self.assertIn(".power-btn[data-tooltip][aria-expanded=\"true\"]::after", LAYOUT)

    def test_no_tooltip_on_a_replaced_element(self):
        # A select/input is a replaced element: Chromium does not render
        # ::before/::after on it, so a tooltip placed on the control itself is
        # silently invisible. Long-name selects use .select-tooltip-wrap.
        replaced = {"select", "input", "textarea", "img", "canvas"}
        offenders = []
        for tag in re.findall(r"<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*data-tooltip=[^>]*>", HTML, re.S):
            if tag.lower() in replaced:
                offenders.append(tag)
        self.assertEqual(offenders, [], f"tooltip on a replaced element: {offenders}")
        self.assertIn(".select-tooltip-wrap", LAYOUT)
        self.assertRegex(
            LAYOUT, r"\.select-tooltip-wrap\s*\{[^}]*display:\s*block[^}]*width:\s*100%"
        )

    def test_long_name_selects_use_the_wrapper(self):
        for control in ("effects-compare-a", "effects-compare-b", "station-delete-select"):
            self.assertRegex(
                HTML,
                rf'class="select-tooltip-wrap"[^>]*data-tooltip-wrap-for="{control}"',
                f"{control} needs a tooltip wrapper",
            )

    def test_power_tooltip_anchors_to_the_right_edge(self):
        self.assertRegex(LAYOUT, r"\.power-btn\[data-tooltip\]::after\s*\{[^}]*right:\s*0")

    def test_right_edge_controls_anchor_the_bubble(self):
        # A left-anchored bubble on a control at the right edge of a toolbar
        # or the footer runs past the viewport.
        anchored = set(re.findall(r'id="([a-z0-9-]+)"[^>]*data-tooltip-anchor="end"', HTML))
        self.assertTrue(anchored, "no control uses the end anchor")
        for control in ("refresh-library", "library-view-mode-grid", "delete-selected-tracks",
                        "source-next", "footer-shuffle"):
            self.assertIn(control, anchored)
        # The anchor shares one rule with the power button, so the selector
        # list may continue past the attribute selector.
        self.assertRegex(
            LAYOUT,
            r'\[data-tooltip-anchor="end"\]::after[^{}]*\{[^}]*left:\s*auto[^}]*right:\s*0',
        )

    def test_long_tooltips_wrap_instead_of_overflowing(self):
        body = re.search(r"\[data-tooltip\]::after\s*\{([^}]*)\}", LAYOUT).group(1)
        self.assertRegex(body, r"max-width:\s*min\(260px")
        self.assertIn("overflow-wrap: anywhere", body)
        self.assertNotIn("white-space: nowrap", body)

    def test_tooltip_host_anchor_does_not_outrank_positioned_hosts(self):
        # :where() keeps zero specificity so the absolutely positioned cover
        # favorite hearts keep their own placement.
        self.assertRegex(
            LAYOUT, r":where\(\[data-tooltip\]\)\s*\{[^}]*position:\s*relative"
        )

    def test_no_native_title_tooltips_remain(self):
        offenders = []
        for path in sorted((ROOT / "static").glob("*.js")):
            for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if 'title="' in line and "data-tooltip" not in line:
                    offenders.append(f"{path.name}:{num}")
        self.assertEqual(offenders, [], f"native title= left behind: {offenders}")
        self.assertNotIn('title="', HTML)

    def test_migrated_hints_keep_their_accessible_name(self):
        # A data-tooltip is a visual hint only; the accessible name must come
        # from aria-label or element content, never from the removed title.
        # Checked per tag, because several buttons wrap their attributes
        # across lines.
        for tag in re.findall(r"<[a-zA-Z][^>]*data-tooltip=[^>]*>", HTML, re.S):
            if "aria-label=" in tag:
                continue
            self.assertRegex(
                tag, r">\s*[A-Za-z0-9]", f"tooltip without an accessible name: {tag[:120]}"
            )

    def test_ab_preset_names_are_reachable(self):
        source = (ROOT / "static" / "output_bank_ui.js").read_text(encoding="utf-8")
        self.assertIn("function syncCompareSelectTooltips()", source)
        self.assertIn("setAttribute('data-tooltip'", source)
        body = re.search(
            r"function syncCompareSelectTooltips\(\)\s*\{([\s\S]*?)\n    \}", source
        ).group(1)
        # The full option text, not the compacted label.
        self.assertIn("option.textContent", body)
        # On the wrapper, never on the select itself.
        self.assertIn("select.closest('.select-tooltip-wrap')", body)
        self.assertNotIn("select.setAttribute('data-tooltip'", body)
        # Wired to both the option rebuild and the user changing the select.
        self.assertGreaterEqual(source.count("syncCompareSelectTooltips()"), 3)

    def test_radio_station_select_keeps_its_full_name(self):
        source = (ROOT / "static" / "radio.js").read_text(encoding="utf-8")
        self.assertIn("function syncStationSelectTooltip()", source)
        self.assertIn("fullStationTitles", source)
        # A native title on <option> is not rendered by Chromium, so the
        # untruncated name has to live on the select itself.
        body = re.search(
            r"function syncStationSelectTooltip\(\)\s*\{([\s\S]*?)\n    \}", source
        ).group(1)
        self.assertIn("select.closest('.select-tooltip-wrap')", body)
        self.assertIn("wrap.setAttribute('data-tooltip'", body)
        # The option list is rebuilt from a full-title mirror, without a
        # per-option title.
        options = re.search(
            r"\.concat\(state\.stations\.map\(station => \{[\s\S]*?\}\)\)\n", source
        )
        self.assertIsNotNone(options, "station options block not found")
        self.assertIn("compactOptionTitle(fullTitle)", options.group(0))
        self.assertNotIn("title=", options.group(0))


class PhoneChromeBudgetTests(unittest.TestCase):
    """9. The permanent phone chrome stays inside a sane share of the screen."""

    def test_toast_column_is_re_anchored_for_phones(self):
        phones = _media_block(RESPONSIVE, "@media (max-width: 600px)")
        # The phone column is centered; it must not reintroduce a fixed top.
        self.assertIn("left: 50%", phones)
        self.assertNotRegex(phones, r"\.toast-container\s*\{[^}]*top:\s*1rem")

    def test_phone_tabs_keep_the_touch_height(self):
        phones = _media_block(RESPONSIVE, "@media (max-width: 760px)")
        self.assertRegex(phones, r"\.tab-btn\s*\{[^}]*min-height:\s*44px")

    def test_footer_reserves_the_playback_space_it_needs(self):
        self.assertIn("--playback-footer-space", TOKENS)
        self.assertRegex(CSS, r"body\s*\{[^}]*padding-bottom:\s*var\(--playback-footer-space\)")

    def test_safe_area_is_honoured_in_every_footer_range(self):
        self.assertGreaterEqual(CSS.count("env(safe-area-inset-bottom)"), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
