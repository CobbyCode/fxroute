"""Locale-stable environment for PipeWire/pulse CLI tool invocations.

pactl localizes field names, values and decimal separators on
non-English systems (e.g. ``Mute: nein``, ``Standard-Ziel``,
``0,00 dB``), which breaks the English-only output parsers across the
codebase.  wpctl/pw-link/pw-cli output is not localized, but pinning
the locale for every tool call keeps the parse contract stable
regardless of the host locale.
"""

from __future__ import annotations

import os


def c_locale_env() -> dict[str, str]:
    """Return the current environment with ``LC_ALL`` pinned to ``C``."""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    return env
