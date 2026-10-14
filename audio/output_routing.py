# SPDX-License-Identifier: AGPL-3.0-only
"""Device identity shared by the per-device output-state routing."""

from __future__ import annotations


def device_key(output_key: str) -> str:
    """Keep the USB PCM identity across ACP multichannel/Pro Audio profiles."""
    normalized = str(output_key or "").strip()
    if normalized.startswith("alsa_output.usb-") and normalized.endswith((".multichannel-output", ".pro-output-0")):
        return normalized.rsplit(".", 1)[0]
    if normalized.startswith("alsa_output.usb-") and ".HiFi__" in normalized:
        # UCM HiFi split siblings (Line1/Line2) and their virtual combined key
        # share one routing identity so assignments survive profile switches.
        base, _, _profile = normalized[len("alsa_output."):].rpartition(".")
        if base:
            return f"alsa_output.{base}"
    return normalized
