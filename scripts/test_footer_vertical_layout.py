#!/usr/bin/env python3
"""Static regression checks for the data-driven responsive playback footer."""

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
    def test_footer_uses_intrinsic_grid_instead_of_fixed_height(self):
        playback_bar = rule(".playback-bar")
        self.assertIn('grid-template-areas: "visual content controls"', playback_bar)
        self.assertIn("min-height: 100px", playback_bar)
        self.assertNotRegex(playback_bar, r"(?m)^\s*height\s*:")
        self.assertIn("width: min(1560px, calc(100% - 16px))", playback_bar)

    def test_cover_and_activity_share_one_visual_unit(self):
        markup = footer_markup()
        visual = re.search(
            r'<div class="playback-visual">(.*?)</div>', markup, re.DOTALL
        )
        self.assertIsNotNone(visual)
        self.assertIn('id="playback-cover"', visual.group(1))
        self.assertIn('id="playback-eq"', visual.group(1))
        overlay = rule(
            ".playback-visual:has(.playback-cover:not(.hidden)) .playback-eq"
        )
        self.assertIn("position: absolute", overlay)
        self.assertIn("right: 3px", overlay)
        self.assertIn("bottom: 3px", overlay)
        visual_with_cover = rule(
            ".playback-visual:has(.playback-cover:not(.hidden))"
        )
        self.assertIn("overflow: hidden", visual_with_cover)

    def test_optional_metadata_and_progress_start_collapsed(self):
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
        self.assertNotIn("radio-has-progress", CSS)
        self.assertNotIn("seekRow.style.display", APP)

    def test_progress_is_a_separate_inset_grid(self):
        seek = rule(".playback-center .seek-row")
        self.assertIn("display: grid", seek)
        self.assertIn("grid-template-columns:", seek)
        self.assertIn("border-radius:", seek)
        self.assertIn("background:", seek)
        self.assertNotIn("margin-top: auto", seek)

    def test_tablet_keeps_visual_and_metadata_on_the_same_row(self):
        tablet_blocks = re.findall(
            r"@media \(max-width: 980px\) \{(.*?)(?=\n\}\n)", CSS, re.DOTALL
        )
        block = next(
            (candidate for candidate in tablet_blocks if ".playback-bar" in candidate),
            "",
        )
        self.assertTrue(block, "Missing playback footer tablet block")
        self.assertIn('"visual content"', block)
        self.assertIn('"controls controls"', block)
        self.assertIn("grid-template-columns: minmax(0, auto) minmax(0, 1fr)", block)

    def test_mobile_collapses_absent_media_without_source_rules(self):
        self.assertIn(".playback-bar:not(.has-media)", CSS)
        self.assertIn(".playback-bar:not(.has-media) .playback-center", CSS)
        self.assertIn("elements.playbackBar?.classList.toggle('has-media'", APP)
        self.assertNotIn(".source-radio .playback-bar", CSS)
        self.assertRegex(
            CSS,
            r"@media \(max-width: 560px\)[\s\S]*?"
            r"\.playback-meta-row\s*\{[^}]*justify-content:\s*center",
        )

    def test_volume_has_a_long_track_and_transient_readout(self):
        volume = rule(".volume-slider")
        self.assertIn("width: clamp(145px, 14vw, 210px)", volume)
        self.assertIn("min-width: 145px", volume)
        self.assertIn("showVolumeDisplayTemporarily()", APP)
        self.assertIn(".controls.is-adjusting-volume .volume-display", CSS)
        self.assertIn('aria-label="Volume"', footer_markup())

    def test_page_end_clearance_tracks_real_footer_height(self):
        self.assertIn("padding-bottom: var(--playback-footer-space)", rule("body"))
        self.assertIn("new ResizeObserver(schedulePlaybackFooterSpaceSync)", APP)
        self.assertIn("rect.height + bottomInset + 16", APP)

    def test_all_control_ids_and_seek_accessibility_are_preserved(self):
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
        self.assertIn('aria-label="Playback position"', markup)


if __name__ == "__main__":
    unittest.main()
