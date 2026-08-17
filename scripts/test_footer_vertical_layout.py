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
        self.assertIn("width: min(1560px, calc(100% - 20px))", playback_bar)

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

    def test_mobile_separates_seek_meter_and_volume(self):
        self.assertRegex(
            CSS,
            r"@media \(max-width: 600px\)[\s\S]*?grid-template-areas:\s*"
            r'\s*"track track"\s*"transport transport"\s*"meter volume"',
        )
        self.assertIn(".playback-bar:not(.has-media)", CSS)
        self.assertIn(".playback-bar:not(.has-media) .playback-center", CSS)

    def test_portrait_tablet_gets_roomier_multiline_footer(self):
        self.assertRegex(
            CSS,
            r"@media \(max-width: 1100px\) and \(orientation: portrait\)"
            r"[\s\S]*?\"track meter\"[\s\S]*?\"transport transport\""
            r"[\s\S]*?\"volume volume\"",
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
