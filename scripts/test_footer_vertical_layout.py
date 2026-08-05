#!/usr/bin/env python3
"""Static regression checks for the playback footer's vertical layout."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def rule(selector: str) -> str:
    match = re.search(
        r"^[ \t]*" + re.escape(selector) + r"\s*\{([^}]+)\}",
        CSS,
        re.MULTILINE,
    )
    if not match:
        raise AssertionError(f"Missing CSS rule: {selector}")
    return match.group(1)


class FooterVerticalLayoutTests(unittest.TestCase):
    def test_footer_height_and_columns_remain_stable(self):
        playback_bar = rule(".playback-bar")
        self.assertIn("height: 102px", playback_bar)
        self.assertIn(
            "grid-template-columns: minmax(0, 1fr) 1.6fr minmax(0, 1fr)",
            playback_bar,
        )

    def test_cover_and_eq_container_are_centered(self):
        self.assertIn("align-items: center", rule(".playback-bar .track-info"))
        self.assertIn(
            "align-items: center",
            rule(".playback-bar .track-info > div:first-of-type"),
        )

    def test_center_distributes_content_and_anchors_seek_row(self):
        center = rule(".playback-center")
        seek = rule(".playback-center .seek-row")
        self.assertIn("align-self: stretch", center)
        self.assertIn("justify-content: flex-start", center)
        self.assertIn("margin-top: auto", seek)

    def test_metadata_variants_share_the_same_center_column(self):
        center_markup = re.search(
            r'<div class="playback-center">(.*?)</div>\s*<div class="controls">',
            HTML,
            re.DOTALL,
        )
        self.assertIsNotNone(center_markup)
        markup = center_markup.group(1)
        for element_id in (
            "sc-album",
            "samplerate-status",
            "queue-status",
            "output-level-badge",
            "seek-slider",
        ):
            self.assertIn(f'id="{element_id}"', markup)

    def test_mobile_and_radio_seek_overrides_are_preserved(self):
        self.assertIn(
            ".source-radio .playback-bar .playback-center .seek-row {\n"
            "    display: none !important;",
            CSS,
        )
        self.assertIn(
            ".source-radio.radio-has-progress .playback-bar .playback-center .seek-row {\n"
            "    display: flex !important;",
            CSS,
        )
        desktop_seek = rule(".playback-center .seek-row")
        self.assertIn("margin-top: auto", desktop_seek)
        self.assertIn("margin-top: 0", CSS)

    def test_tablet_radio_progress_uses_natural_height_and_centered_seek(self):
        self.assertIn(
            "@media (min-width: 601px) and (max-width: 980px) {\n"
            "    .source-radio.radio-has-progress .playback-bar .playback-center {\n"
            "        min-height: 0;\n"
            "        justify-content: center;",
            CSS,
        )
        self.assertIn(
            ".source-radio.radio-has-progress .playback-bar .playback-center .seek-row {\n"
            "        margin-top: 0.12rem;\n"
            "        padding-bottom: 0;",
            CSS,
        )

    def test_desktop_three_line_metadata_uses_intrinsic_footer_height(self):
        # Layout behavior: at desktop width (>= 981px) the playback bar must
        # switch from its fixed 102px height to an intrinsic height as soon as
        # the third metadata line (sc-album) has content.  The :has() rule is
        # the mechanism; the structural anchor below is what makes it apply.
        # (Historical cache-buster version strings must NOT be asserted here;
        # they change with every deploy and say nothing about layout.)
        self.assertIn(
            "@media (min-width: 981px) {\n"
            "    .playback-bar:has(.playback-center .sc-album:not(:empty)) {\n"
            "        height: auto;\n"
            "        min-height: 102px;",
            CSS,
        )
        # Structural anchor: #sc-album must live inside .playback-center inside
        # #playback-bar so the :has() selector above can match at runtime.
        playback_bar = re.search(
            r'<footer id="playback-bar" class="playback-bar">.*?</footer>',
            HTML,
            re.DOTALL,
        )
        self.assertIsNotNone(playback_bar)
        center = re.search(
            r'<div class="playback-center">.*?</div>\s*<div class="controls">',
            playback_bar.group(0),
            re.DOTALL,
        )
        self.assertIsNotNone(center)
        self.assertIn('<span class="sc-album" id="sc-album">', center.group(0))


if __name__ == "__main__":
    unittest.main()
