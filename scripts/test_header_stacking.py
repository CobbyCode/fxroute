#!/usr/bin/env python3
"""Regression checks for the header popup stacking contract.

The header popups (custom tooltips, power menu) escape the header only
because the header itself creates no stacking context: they carry their own
z-index levels (--z-tooltip, --z-header-menu) and compete directly in the
root stacking context, above the tab bar (--z-tabs).

Any stacking-context property on `.header` (backdrop-filter, a z-index
raise, an :has() escalation, ...) either traps the popups behind the tab
bar or covers the navigation entirely. This test pins the popup-only
stacking model: popups stack, the header never does.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

# Properties that create a stacking context on the element they are set on.
# Checked on every `.header {...}` rule, including responsive overrides.
FORBIDDEN_KEYS = (
    "backdrop-filter",
    "filter",
    "transform",
    "translate",
    "rotate",
    "scale",
    "perspective",
    "isolation",
    "mix-blend-mode",
    "clip-path",
    "mask",
    "contain",
    "content-visibility",
    "will-change",
    "container-type",
)
# Values that make one of the above a no-op (no stacking context created).
BENIGN_VALUES = {"none", "normal", "auto"}


def header_rules():
    """Bodies of all `.header {...}` rules, base and responsive overrides."""
    return [m.group(1) for m in re.finditer(r"(?m)^[ \t]*\.header\s*\{([^}]*)\}", CSS)]


def rule_body(selector):
    match = re.search(
        r"(?m)^[ \t]*" + re.escape(selector) + r"\s*\{([^}]*)\}", CSS
    )
    return match.group(1) if match else None


def declarations(body):
    for decl in body.split(";"):
        if ":" in decl:
            prop, value = decl.split(":", 1)
            yield prop.strip().lower(), value.strip()


def token_value(name):
    match = re.search(r"--" + re.escape(name) + r"\s*:\s*([-\d.]+)", CSS)
    return float(match.group(1)) if match else None


class HeaderStackingTests(unittest.TestCase):
    def test_header_rule_exists(self):
        self.assertTrue(header_rules(), "missing .header rule in style.css")

    def test_header_creates_no_stacking_context(self):
        violations = []
        for body in header_rules():
            for prop, value in declarations(body):
                lowered = value.lower()
                if prop == "opacity":
                    # opacity < 1 creates a stacking context.
                    try:
                        if float(lowered) < 1:
                            violations.append(f"opacity: {value}")
                    except ValueError:
                        pass
                    continue
                if prop == "z-index" and lowered != "auto":
                    # A raised header covers the tab bar or traps the popups.
                    violations.append(f"z-index: {value}")
                    continue
                if prop in FORBIDDEN_KEYS and lowered not in BENIGN_VALUES:
                    violations.append(f"{prop}: {value}")
        self.assertEqual(
            violations,
            [],
            ".header must not create a stacking context; popups must stack "
            "via their own root-context levels instead. Offending: "
            + ", ".join(violations),
        )

    def test_no_has_based_header_escalation(self):
        # The removed regression: raising the whole header while the power
        # menu is open covers the tab bar. The menu must stack on its own.
        self.assertIsNone(
            re.search(r"\.header[^{}]*:has\([^)]*\)[^{]*\{[^}]*z-index", CSS),
            "header :has() escalation with z-index found; raise the popup "
            "level instead of the header",
        )

    def test_popups_carry_their_own_root_levels(self):
        # The z-index is declared on the shared grouped rule for both header
        # tooltips; the per-button rules only anchor them horizontally.
        tooltip = re.search(
            r"\.brand-lockup-button\[data-tooltip\]::after,\s*"
            r"\.power-btn\[data-tooltip\]::after\s*\{([^}]*)\}", CSS
        )
        self.assertIsNotNone(tooltip, "missing shared header tooltip rule")
        self.assertIn("var(--z-tooltip)", tooltip.group(1))

        menu = rule_body(".power-menu")
        self.assertIsNotNone(menu, "missing .power-menu rule")
        self.assertIn("var(--z-header-menu)", menu)

    def test_popup_levels_order_above_tabs_and_toasts(self):
        tabs = token_value("z-tabs")
        toasts = token_value("z-toast")
        menu = token_value("z-header-menu")
        tooltip = token_value("z-tooltip")
        for label, value in (("--z-tabs", tabs), ("--z-toast", toasts),
                             ("--z-header-menu", menu), ("--z-tooltip", tooltip)):
            self.assertIsNotNone(value, f"missing {label} token")
        self.assertGreater(menu, tabs, "power menu must clear the tab bar")
        self.assertGreater(menu, toasts, "power menu must clear toasts")
        self.assertGreater(tooltip, tabs, "tooltips must clear the tab bar")
        self.assertGreater(tooltip, menu, "tooltips are the topmost layer")


if __name__ == "__main__":
    unittest.main()
