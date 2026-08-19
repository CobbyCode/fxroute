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

    def test_player_panels_have_no_provider_specific_vertical_offset(self):
        # Spotify and Qobuz must start at the same height: the legacy
        # `#tab-spotify { padding-top: 1.5rem }` rule would push the Spotify
        # player below the Qobuz one and is banned.
        match = re.search(
            r"^[ \t]*#tab-spotify\s*\{([^}]+)\}", CSS, re.MULTILINE
        )
        self.assertIsNone(
            match,
            "#tab-spotify must not carry a panel-level offset; player pages "
            "start at the same height via the shared provider stage",
        )
        shell = re.search(
            r"^[ \t]*\.streaming-shell\s*\{([^}]+)\}", CSS, re.MULTILINE
        )
        self.assertIsNotNone(shell, "missing .streaming-shell rule")
        self.assertNotIn("padding-top", shell.group(1))

    def test_player_status_is_integrated_into_the_card(self):
        # The floating status pill above the player card is gone; the status
        # is one shared dot-led pill inside the card header and the TIDAL
        # browse toolbar (no per-provider status variant).
        player_pill = re.search(
            r"^[ \t]*\.streaming-provider-player \.streaming-status-line\s*\{", CSS, re.MULTILINE
        )
        self.assertIsNone(
            player_pill,
            "player providers must not have a standalone status pill above the card",
        )
        self.assertNotIn(
            ".streaming-status-line",
            CSS,
            "the old per-provider status line must be gone; one shared .streaming-status pill remains",
        )
        card = re.search(
            r"^[ \t]*\.streaming-now-playing\s*\{([^}]+)\}", CSS, re.MULTILINE
        )
        self.assertIsNotNone(card, "missing .streaming-now-playing rule")
        self.assertIn("position: relative", card.group(1))
        # The status pill now sits in-flow in the card header row next to the
        # provider name; it must not float absolutely over the card anymore.
        header = re.search(
            r"^[ \t]*\.streaming-card-header\s*\{([^}]+)\}", CSS, re.MULTILINE
        )
        self.assertIsNotNone(header, "missing .streaming-card-header rule")
        self.assertIn("grid-area: header", header.group(1))
        chip = re.search(
            r"^[ \t]*\.streaming-status\s*\{([^}]+)\}", CSS, re.MULTILINE
        )
        self.assertIsNotNone(chip, "missing shared .streaming-status rule")
        self.assertNotIn("position: absolute", chip.group(1))
        self.assertIn("display: inline-flex", chip.group(1))
        streaming_js = (ROOT / "static" / "streaming.js").read_text(encoding="utf-8")
        self.assertIn("streaming-status", streaming_js)
        self.assertNotIn("streaming-status-chip", streaming_js)
        self.assertIn("streaming-provider-name", streaming_js)

    def test_provider_layout_has_player_width_and_footer_safe_area(self):
        # The shared player card is bounded, centered, and generous on desktop.
        now_playing = re.search(
            r"^[ \t]*\.streaming-now-playing\s*\{([^}]+)\}", CSS, re.MULTILINE
        )
        self.assertIsNotNone(now_playing, "missing .streaming-now-playing rule")
        self.assertIn("max-width:", now_playing.group(1))
        self.assertIn("margin:", now_playing.group(1))
        cover = re.search(
            r"^[ \t]*\.streaming-cover\s*\{([^}]+)\}", CSS, re.MULTILINE
        )
        self.assertIsNotNone(cover, "missing .streaming-cover rule")
        self.assertRegex(
            cover.group(1), r"width:\s*2\d\dpx", "desktop cover must be generous (200px+)"
        )
        self.assertRegex(
            cover.group(1), r"height:\s*2\d\dpx", "desktop cover must be generous (200px+)"
        )
        # Player pages get a top-anchored stage: the card starts under the
        # status line on every player page, so Spotify and Qobuz share the
        # same vertical geometry regardless of their metadata heights.
        player = re.search(
            r"^[ \t]*\.streaming-provider-player\s*\{([^}]+)\}", CSS, re.MULTILINE
        )
        self.assertIsNotNone(player, "missing .streaming-provider-player rule")
        self.assertIn("justify-content: flex-start", player.group(1))
        self.assertIn("padding-bottom: var(--playback-footer-space)", CSS)
        self.assertIn(".streaming-content", CSS)
        streaming_js = (ROOT / "static" / "streaming.js").read_text(encoding="utf-8")
        self.assertIn("catalogProvider", streaming_js)


if __name__ == "__main__":
    unittest.main()
