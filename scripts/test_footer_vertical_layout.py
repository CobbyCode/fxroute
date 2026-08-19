#!/usr/bin/env python3
"""Static regression checks for the responsive playback footer."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


def rule(selector: str) -> str:
    match = re.search(
        r"^[ \t]*" + re.escape(selector) + r"\s*\{([^}]+)\}",
        CSS,
        re.MULTILINE,
    )
    if not match:
        raise AssertionError(f"Missing CSS rule: {selector}")
    return match.group(1)


def footer_markup() -> str:
    match = re.search(
        r'<footer id="playback-bar" class="playback-bar">.*?</footer>',
        HTML,
        re.DOTALL,
    )
    if not match:
        raise AssertionError("Missing #playback-bar markup")
    return match.group(0)


class FooterResponsiveLayoutTests(unittest.TestCase):
    def test_footer_has_four_priority_zones(self):
        playback_bar = rule(".playback-bar")
        self.assertIn('grid-template-areas: "track transport meter volume"', playback_bar)
        self.assertIn("min-height: 112px", playback_bar)
        self.assertNotRegex(playback_bar, r"(?m)^\s*height\s*:")
        # Window-based centering: vw keeps the outer gaps equal even when a
        # classic scrollbar narrows the fixed-position containing block.
        self.assertIn("left: 50vw", playback_bar)
        self.assertIn("width: min(1560px, calc(100vw - 20px))", playback_bar)

    def test_desktop_track_info_has_a_hard_boundary_before_transport(self):
        desktop_blocks = re.findall(
            r"@media \(min-width: 1181px\) \{[\s\S]*?\.playback-bar\s*\{([^}]+)\}",
            CSS,
        )
        self.assertTrue(desktop_blocks)
        self.assertTrue(
            any("minmax(280px, 430px)" in block and "minmax(390px, 1fr)" in block
                for block in desktop_blocks)
        )
        self.assertIn("grid-template-areas: \"track transport meter volume\"", rule(".playback-bar"))

        track_info = rule(".playback-bar .track-info")
        self.assertIn("min-width: 0", track_info)
        self.assertIn("max-width: 100%", track_info)
        for selector in (".track-copy", ".track-copy-main", ".playback-bar .song-info"):
            self.assertIn("min-width: 0", rule(selector))
        self.assertRegex(CSS, r"\.track-fallback,\s*\n\.playback-bar \.song-info\s*\{[^}]*min-width: 0")

    def test_metadata_ellipsis_is_scoped_to_track_info(self):
        self.assertIn(".playback-bar .track-title", CSS)
        self.assertIn(".playback-bar .track-artist", CSS)
        self.assertNotIn(".playback-bar .seek-row", rule(".playback-bar .track-info"))

    def test_desktop_and_tablet_compact_without_touching_phone_layout(self):
        self.assertIn("Playback footer refinement v7", CSS)
        self.assertRegex(CSS, r"@media \(min-width: 1181px\)[\s\S]*?min-height: 98px")
        self.assertRegex(
            CSS,
            r"@media \(min-width: 901px\) and \(max-width: 1180px\)[\s\S]*?min-height: 116px",
        )
        # Bottom insets sit a couple px closer to the viewport edge; the phone
        # inset respects the home-indicator safe area while keeping a 3px min.
        self.assertIn("bottom: 5px", CSS)
        self.assertIn("bottom: 6px", CSS)
        self.assertRegex(CSS, r"bottom: max\(3px, env\(safe-area-inset-bottom\)\)")
        self.assertRegex(CSS, r"\.control-btn\s*\{\s*width:\s*46px")

    def test_desktop_tablet_sliders_get_visual_refinement_only(self):
        self.assertIn("Playback footer refinement v8", CSS)
        self.assertRegex(
            CSS,
            r"@media \(min-width: 901px\)[\s\S]*?\.seek-slider::\-webkit-slider-runnable-track[\s\S]*?height: 4\.5px",
        )
        self.assertRegex(
            CSS,
            r"@media \(min-width: 901px\)[\s\S]*?\.seek-slider::\-webkit-slider-thumb\s*\{\s*width: 12\.5px; height: 12\.5px",
        )
        self.assertRegex(
            CSS,
            r"@media \(min-width: 901px\)[\s\S]*?\.volume-slider::\-webkit-slider-thumb\s*\{\s*width: 13\.5px; height: 13\.5px",
        )
        self.assertIn(".seek-slider { height: 5px; }", CSS)
        self.assertIn(".volume-slider { height: 5px; }", CSS)
        # Chromium top-anchors the thumb when the 4.5px track is shorter than
        # the thumb; the geometry-derived margin re-centers it (webkit only).
        self.assertRegex(
            CSS,
            r"@media \(min-width: 901px\)[\s\S]*?"
            r"\.seek-slider::\-webkit-slider-thumb\s*\{\s*margin-top: calc\(\(4\.5px - 12\.5px\) / 2\)",
        )
        self.assertRegex(
            CSS,
            r"@media \(min-width: 901px\)[\s\S]*?"
            r"\.volume-slider::\-webkit-slider-thumb\s*\{\s*margin-top: calc\(\(4\.5px - 13\.5px\) / 2\)",
        )

    def test_mobile_slider_polish_preserves_input_box_and_volume_hierarchy(self):
        self.assertRegex(
            CSS,
            r"@media \(max-width: 700px\)[\s\S]*?\.seek-slider::\-webkit-slider-runnable-track[\s\S]*?height: 4\.5px",
        )
        self.assertRegex(
            CSS,
            r"@media \(max-width: 700px\)[\s\S]*?\.seek-slider::\-webkit-slider-thumb\s*\{\s*width: 12\.5px; height: 12\.5px",
        )
        self.assertRegex(
            CSS,
            r"@media \(max-width: 700px\)[\s\S]*?\.volume-slider::\-webkit-slider-thumb\s*\{\s*width: 15\.5px; height: 15\.5px",
        )
        self.assertIn(".seek-slider { height: 5px; }", CSS)
        self.assertIn(".volume-slider { height: 5px; }", CSS)
        # Same centering correction for the phone thumb sizes (15.5px volume).
        self.assertRegex(
            CSS,
            r"@media \(max-width: 700px\)[\s\S]*?"
            r"\.volume-slider::\-webkit-slider-thumb\s*\{\s*margin-top: calc\(\(4\.5px - 15\.5px\) / 2\)",
        )

    def test_desktop_transport_nudge_is_positive_and_footer_height_unchanged(self):
        self.assertRegex(
            CSS,
            r"@media \(min-width: 1181px\)[\s\S]*?\.transport-controls\s*\{\s*margin-top: 1px",
        )
        self.assertIn("min-height: 98px", CSS)

    def test_cover_and_track_favorite_are_in_track_zone(self):
        markup = footer_markup()
        self.assertEqual(markup.count('id="playback-cover"'), 1)
        self.assertEqual(markup.count('id="track-favorite-btn"'), 1)
        self.assertIn("renderTrackFavoriteButton(current_track)", APP)
        self.assertIn("toggleCurrentTrackFavorite", APP)
        self.assertIn("/favorite`,", APP)

    def test_stereo_meter_uses_real_backend_fields(self):
        markup = footer_markup()
        self.assertEqual(markup.count('id="meter-l"'), 1)
        self.assertEqual(markup.count('id="meter-r"'), 1)
        self.assertIn("warning?.vu_db_l", APP)
        self.assertIn("warning?.vu_db_r", APP)
        self.assertIn("warning?.detected_l", APP)
        self.assertIn("warning?.detected_r", APP)
        self.assertNotIn('id="playback-eq"', markup)

    def test_progress_and_volume_have_explicit_active_fill(self):
        self.assertIn("--range-progress", CSS)
        self.assertIn("background: linear-gradient(to right", CSS)
        self.assertIn("setRangeProgress(elements.seekSlider", APP)
        self.assertIn("setRangeProgress(elements.volumeSlider", APP)
        self.assertIn('aria-label="Playback position"', footer_markup())
        self.assertIn('aria-label="Volume"', footer_markup())

    def test_track_metadata_remains_data_driven(self):
        markup = footer_markup()
        for element_id in (
            "sc-artist",
            "sc-title",
            "sc-album",
            "samplerate-status",
            "queue-status",
            "output-level-badge",
            "seek-slider",
        ):
            self.assertIn(f'id="{element_id}"', markup)
        self.assertIn('<div class="seek-row hidden">', markup)
        self.assertIn("setFooterProgressState(hasProgress, radioTimed)", APP)

    def test_phone_recomposes_into_three_rows(self):
        self.assertRegex(
            CSS,
            r"@media \(max-width: 700px\)[\s\S]*?grid-template-areas:\s*"
            r'\s*"track meter"\s*"transport transport"\s*"volume volume"',
        )
        self.assertIn(".playback-bar:not(.has-media)", CSS)
        self.assertIn(".playback-bar:not(.has-media) .playback-center", CSS)

    def test_compact_tablet_spans_two_rows(self):
        self.assertRegex(
            CSS,
            r"@media \(min-width: 701px\) and \(max-width: 1180px\)"
            r"[\s\S]*?grid-template-areas:\s*"
            r'\s*"track transport meter"\s*"track transport volume"',
        )

    def test_page_end_clearance_tracks_real_footer_height(self):
        self.assertIn("padding-bottom: var(--playback-footer-space)", rule("body"))
        self.assertIn("new ResizeObserver(schedulePlaybackFooterSpaceSync)", APP)
        self.assertIn("rect.height + bottomInset + 16", APP)

    def test_all_control_ids_are_preserved_once(self):
        markup = footer_markup()
        for element_id in (
            "btn-previous",
            "btn-play-pause",
            "btn-next",
            "btn-clear-queue",
            "volume-slider",
            "seek-slider",
        ):
            self.assertEqual(markup.count(f'id="{element_id}"'), 1)


if __name__ == "__main__":
    unittest.main()
