# SPDX-License-Identifier: AGPL-3.0-only
"""Live PipeWire input-link readback shared by Bluetooth and external input."""

from typing import Iterable

from audio import pw_link
from dsp.runtime import _contains_link


async def input_links_present(source_name: str, channels: tuple[tuple[str, str], ...]) -> bool:
    """Check each source channel against its DSP sink side in the live graph."""
    links = await pw_link.run_pw_link_command("-l")
    return all(
        any(
            _contains_link(links, f"{source_name}:{prefix}_{channel}", f"fxroute_dsp_sink:playback_{side}")
            for prefix in ("capture", "output")
        )
        for channel, side in channels
    )


async def ingress_links_present(edges: Iterable[tuple[str, str]]) -> bool:
    """Check explicit (source port, target port) links in the live graph."""
    links = await pw_link.run_pw_link_command("-l")
    return all(_contains_link(links, source, target) for source, target in edges)
