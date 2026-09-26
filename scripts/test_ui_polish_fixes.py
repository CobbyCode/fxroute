#!/usr/bin/env python3
"""Regression guard for the UI polish pass.

Covers the concrete frontend findings that were fixed:

1. Toast column clears the whole sticky top stack (header row + tab strip).
2. The active main tab is scrolled back into the strip on narrow screens,
   only on a tab switch or a width change, and clear of the scroll fade.
3. ``--warning`` clears WCAG AA on every surface of the dark theme.
4. The A/B compare card fills the grid row instead of trailing dead space.
5. The streaming seek bar keeps a ~4px track but a ~24px hit area, with a
   centred thumb.
6. Primary controls reach the ~44px touch minimum.
7. Long A/B preset and station names stay reachable through the app
   tooltip, whatever set the select's value.
8. One tooltip system: ``data-tooltip`` everywhere, no native ``title=``,
   rendered by the fixed tooltip layer (static/tooltip.js).
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
STREAMING_JS = (ROOT / "static" / "streaming.js").read_text(encoding="utf-8")
TOOLTIP_JS = (ROOT / "static" / "tooltip.js").read_text(encoding="utf-8")

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


def _function_body(source: str, name: str, indent: str = "") -> str:
    match = re.search(
        rf"(?m)^{indent}function {re.escape(name)}\([^)]*\)\s*\{{([\s\S]*?)\n{indent}\}}", source
    )
    if not match:
        raise AssertionError(f"function {name} not found")
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

    def test_resize_refreshes_the_offset(self):
        self.assertIn("window.addEventListener('resize', handleTabsResize)", APP_JS)
        self.assertIn("updateHeaderStackHeight()", _function_body(APP_JS, "handleTabsResize"))


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

    def test_resize_only_recentres_on_a_width_change(self):
        # A height-only resize (mobile URL bar) must not undo the user's own
        # swipe along the strip.
        body = _function_body(APP_JS, "handleTabsResize")
        self.assertRegex(
            body,
            r"if \(stripWidth === lastTabsStripWidth\) return;[\s\S]*keepActiveTabInView\(\)",
        )

    def test_provider_visibility_path_never_scrolls_the_strip(self):
        # streaming.js calls updateTabsScrollAffordance() from the 10s
        # discovery poll; it must neither scroll the strip nor force the
        # stack-height layout read.
        body = _function_body(APP_JS, "updateTabsScrollAffordance")
        self.assertNotIn("keepActiveTabInView", body)
        self.assertNotIn("updateHeaderStackHeight", body)

    def test_provider_polls_refresh_only_on_a_visibility_flip(self):
        body = _function_body(STREAMING_JS, "applyTabVisibility", indent="    ")
        self.assertIn("const tabWasShown = isTabShown(entry.tabBtn);", body)
        self.assertRegex(
            body,
            r"if \(tabWasShown !== isTabShown\(entry\.tabBtn\)[^)]*\)\s*\{\s*updateTabsScrollAffordance\(\);",
        )

    def test_active_tab_clears_the_scroll_fade(self):
        body = _function_body(APP_JS, "keepActiveTabInView")
        self.assertIn("getComputedStyle(nav, '::after').width", body)
        self.assertIn("navBox.right - fadeWidth - pad", body)


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
    """4. The A/B card shares the row height with Output extras.

    Reverted on purpose. `align-items: start` did remove the ~140px void
    inside the card, but it only relocated it: the card ended 122px above its
    neighbour, leaving black page background inside the grid row. The
    acceptance criterion is the opposite of that -- the row must end flush,
    so the cards keep the shared height. The void inside the A/B card is the
    honest remaining cost of that choice and is not papered over here.
    """

    def test_grid_stretches_cards_to_the_row_height(self):
        grid = re.search(r"\.effects-grid\s*\{([^}]*)\}", EFFECTS).group(1)
        self.assertRegex(grid, r"align-items:\s*stretch")
        # Scoped to .effects-grid: the subwoofer and REW-dual grids keep
        # their own pre-existing `align-items: start`, which is correct there
        # (cards hug their content) and unrelated to the A/B row.
        self.assertNotRegex(EFFECTS, r"\.effects-grid[^{]*\{[^}]*align-items:\s*start")

    def test_only_one_effects_grid_rule_exists(self):
        # A leftover duplicate override silently re-introduces the ragged row.
        rules = re.findall(r"(?m)^\.effects-grid\s*\{", EFFECTS)
        self.assertEqual(len(rules), 1, f"expected one .effects-grid rule, found {len(rules)}")

    def test_tile_keeps_its_original_padding(self):
        row = re.search(r"\.effects-compare-row\s*\{([^}]*)\}", EFFECTS).group(1)
        padding = re.search(r"padding:\s*([\d.]+)rem", row)
        self.assertIsNotNone(padding)
        self.assertEqual(float(padding.group(1)), 1.0)

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

    def test_webkit_thumb_is_not_shifted_off_the_track(self):
        # With the padded content-box track the thumb is centred natively; a
        # margin-top lifted it 2px (fine) / 4px (coarse) above the track.
        for body in re.findall(r"::-webkit-slider-thumb\s*\{([^}]*)\}", STREAMING):
            self.assertNotIn("margin-top", body)


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

    def test_layer_is_loaded_by_the_page(self):
        self.assertRegex(HTML, r'<script src="/static/tooltip\.js\?v=\d+\.\d+\.\d+"></script>')

    def test_bubble_is_one_fixed_element_outside_every_host(self):
        # A per-host ::after bubble is clipped by overflow/scroll containers
        # and adds scrollable overflow to them even while hidden.
        body = re.search(r"\.app-tooltip\s*\{([^}]*)\}", LAYOUT).group(1)
        self.assertIn("position: fixed", body)
        self.assertIn("z-index: var(--z-tooltip)", body)
        # Shrink-to-fit would squeeze the bubble to the host width.
        self.assertIn("width: max-content", body)
        self.assertRegex(body, r"max-width:\s*min\(260px")
        self.assertIn("overflow-wrap: anywhere", body)
        self.assertIn("doc.body.appendChild(bubble)", TOOLTIP_JS)
        for source in (CSS, LAYOUT, RESPONSIVE):
            self.assertNotIn("attr(data-tooltip)", source)
            self.assertNotRegex(source, r"\[data-tooltip\][^{]*::after")

    def test_no_tooltip_on_a_replaced_element(self):
        # Long-name selects keep their wrapper as the tooltip host.
        replaced = {"select", "input", "textarea", "img", "canvas"}
        offenders = []
        for tag in re.findall(r"<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*data-tooltip=[^>]*>", HTML, re.S):
            if tag.lower() in replaced:
                offenders.append(tag)
        self.assertEqual(offenders, [], f"tooltip on a replaced element: {offenders}")
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
        self.assertIn('data-tooltip-wrap-for="effects-compare-a" data-tooltip-prefix="Preset A"', HTML)
        self.assertIn('data-tooltip-wrap-for="effects-compare-b" data-tooltip-prefix="Preset B"', HTML)
        self.assertIn(".select-tooltip-wrap", TOOLTIP_JS)

    def test_select_names_are_read_when_the_bubble_opens(self):
        # Reset paths and programmatic value changes fire no event, so a
        # sync-on-change tooltip went stale; the layer reads the selected
        # option itself, and the placeholder option has nothing to reveal.
        body = _function_body(TOOLTIP_JS, "selectTooltipText", indent="    ")
        self.assertIn("select.options[select.selectedIndex]", body)
        self.assertIn("option.value === ''", body)
        self.assertIn("option.getAttribute('aria-label') || option.textContent", body)
        for name in ("output_bank_ui.js", "radio.js"):
            source = (ROOT / "static" / name).read_text(encoding="utf-8")
            self.assertNotIn("syncCompareSelectTooltips", source)
            self.assertNotIn("syncStationSelectTooltip", source)
            self.assertNotIn("wrap.setAttribute('data-tooltip'", source)

    def test_station_names_have_one_source(self):
        # The option's aria-label carries the full name; no mirror map.
        source = (ROOT / "static" / "radio.js").read_text(encoding="utf-8")
        self.assertNotIn("fullStationTitles", source)
        options = re.search(
            r"\.concat\(state\.stations\.map\(station => \{[\s\S]*?\}\)\)\n", source
        )
        self.assertIsNotNone(options, "station options block not found")
        self.assertIn('aria-label="${escapeHtml(fullTitle)}"', options.group(0))
        self.assertIn("compactOptionTitle(fullTitle)", options.group(0))
        self.assertNotIn("title=", options.group(0))

    def test_select_wrapper_opens_on_keyboard_focus(self):
        # The wrapper cannot take focus; the focused select inside it opens
        # the wrapper's bubble.
        self.assertRegex(TOOLTIP_JS, r"focusin[\s\S]*?hostOf\(event\.target\)[\s\S]*?isKeyboardFocus")
        self.assertIn("target.closest(HOST_SELECTOR)", TOOLTIP_JS)

    def test_right_edge_controls_anchor_the_bubble(self):
        anchored = set(re.findall(r'id="([a-z0-9-]+)"[^>]*data-tooltip-anchor="end"', HTML))
        for control in ("refresh-library", "library-view-mode-grid", "delete-selected-tracks",
                        "source-next", "footer-shuffle", "power-menu-toggle"):
            self.assertIn(control, anchored)
        body = _function_body(TOOLTIP_JS, "placeTooltip", indent="    ")
        self.assertIn("anchor === 'end' ? hostRect.right - size.width", body)

    def test_touch_never_opens_a_bubble(self):
        self.assertIn("if (event.pointerType !== 'mouse') return;", TOOLTIP_JS)

    def test_no_native_title_tooltips_remain(self):
        offenders = []
        for path in sorted((ROOT / "static").glob("*.js")):
            for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if 'title="' in line and "data-tooltip" not in line:
                    offenders.append(f"{path.name}:{num}")
        self.assertEqual(offenders, [], f"native title= left behind: {offenders}")
        self.assertNotIn('title="', HTML)

    def test_runtime_hints_on_tooltip_hosts_update_the_bubble(self):
        """A host with a markup data-tooltip must be relabelled through it.

        Writing `.title` on such a host leaves the bubble on the old label
        and adds a second, native tooltip with the new one.
        """
        sites = {
            "library_ui.js": ("button", "albumFavoriteToggle", "btn"),
            "playback_ui.js": ("shuffleBtn", "loopBtn"),
            "streaming.js": ("els.toggle", "els.shuffle", "els.loop", "btn"),
            "settings_system.js": ("powerMenuToggle",),
        }
        for name, expressions in sites.items():
            source = (ROOT / "static" / name).read_text(encoding="utf-8")
            for expression in expressions:
                self.assertNotRegex(
                    source, rf"(?m)(?:^|[\s.]){re.escape(expression)}\.title =",
                    f"{name}: {expression}.title on a tooltip host",
                )
        # Source prev/next arrows carry a markup data-tooltip too.
        self.assertIn("button.setAttribute('data-tooltip', guard || button.getAttribute('aria-label') || '')", APP_JS)
        self.assertNotIn("button.title = guard", APP_JS)

    def test_migrated_hints_keep_their_accessible_name(self):
        # A data-tooltip is a visual hint only; the accessible name must come
        # from aria-label or text content. Checked for the page shell and
        # for the markup the modules generate (string joints removed so a
        # tag split across concatenated literals is read as one).
        sources = [("index.html", HTML)]
        for path in sorted((ROOT / "static").glob("*.js")):
            text = path.read_text(encoding="utf-8")
            text = re.sub(r"(['\"`])\s*\+\s*\n\s*\1", "", text)
            sources.append((path.name, text))
        for name, text in sources:
            for match in re.finditer(r"<button\b([^>]*data-tooltip=[^>]*)>([\s\S]*?)</button>", text):
                attrs, content = match.groups()
                if "aria-label=" in attrs:
                    continue
                visible = re.sub(r"<[^>]+>", "", content)
                self.assertRegex(
                    visible, r"[A-Za-z0-9]",
                    f"{name}: glyph-only tooltip button without aria-label: {match.group(0)[:120]}",
                )

    def test_visible_label_is_part_of_the_accessible_name(self):
        # WCAG 2.5.3: the Freq/IR chips are named by their visible text; the
        # longer hint stays in the tooltip.
        for view in ("freq", "ir"):
            tag = re.search(rf'<button[^>]*data-measurement-view="{view}"[^>]*>', HTML).group(0)
            self.assertNotIn("aria-label=", tag)


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
