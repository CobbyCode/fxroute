#!/usr/bin/env python3
"""Regression checks for the streaming hidden-state CSS contract.

The JS toggles the `hidden` attribute to switch between the empty state and
the now-playing card.  The per-element display rules (.streaming-empty flex,
.streaming-now-playing grid) have higher specificity than the browser's
[hidden] rule, so without an explicit scoped rule both states render at the
same time.  This test pins the shared fix.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


class StreamingHiddenCssTests(unittest.TestCase):
    def test_streaming_provider_hidden_always_wins(self):
        match = re.search(
            r"^[ \t]*\.streaming-provider \[hidden\]\s*\{([^}]+)\}",
            CSS,
            re.MULTILINE,
        )
        self.assertIsNotNone(
            match,
            "missing .streaming-provider [hidden] rule; the JS-toggled hidden "
            "attribute must always win inside the streaming shell",
        )
        self.assertIn("display: none", match.group(1))
        self.assertIn("!important", match.group(1))

    def test_display_rules_still_define_their_layouts(self):
        # The layout rules keep their flex/grid display; the hidden contract
        # above must override them only while the hidden attribute is set.
        empty = re.search(
            r"^[ \t]*\.streaming-empty\s*\{([^}]+)\}", CSS, re.MULTILINE
        )
        now_playing = re.search(
            r"^[ \t]*\.streaming-now-playing\s*\{([^}]+)\}", CSS, re.MULTILINE
        )
        self.assertIsNotNone(empty)
        self.assertIsNotNone(now_playing)
        self.assertIn("display: flex", empty.group(1))
        self.assertIn("display: grid", now_playing.group(1))

    def test_streaming_components_use_the_hidden_attribute(self):
        # The JS contract: both states are toggled exclusively via `hidden`.
        streaming_js = (ROOT / "static" / "streaming.js").read_text(encoding="utf-8")
        self.assertIn("els.empty.hidden = true", streaming_js)
        self.assertIn("els.nowPlaying.hidden = false", streaming_js)
        self.assertIn("els.nowPlaying.hidden = true", streaming_js)
        self.assertIn("els.empty.hidden = false", streaming_js)

    def test_provider_layout_has_compact_desktop_and_footer_safe_area(self):
        self.assertIsNotNone(
            re.search(
                r"\.streaming-now-playing:not\(\.streaming-now-playing-compact\).*?max-width:",
                CSS,
                re.DOTALL,
            ),
            "Spotify/Qobuz now-playing needs a bounded desktop width",
        )
        self.assertIsNotNone(
            re.search(
                r"\.streaming-now-playing:not\(\.streaming-now-playing-compact\).*?margin:\s*[^;]+auto",
                CSS,
                re.DOTALL,
            ),
            "Spotify/Qobuz now-playing must be horizontally centered",
        )
        self.assertIsNotNone(
            re.search(
                r"\.streaming-now-playing-compact.*?\.streaming-controls",
                CSS,
                re.DOTALL,
            ),
            "Tidal compact now-playing must suppress duplicated transport controls",
        )
        self.assertIn("padding-bottom: var(--playback-footer-space)", CSS)
        self.assertIn(".streaming-content", CSS)


if __name__ == "__main__":
    unittest.main()
