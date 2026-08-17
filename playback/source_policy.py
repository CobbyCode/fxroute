# SPDX-License-Identifier: AGPL-3.0-only
"""Central playback source policy.

Single home for the source vocabulary and the per-source engine/graph facts
that were previously scattered as ``source == "spotify"`` /
``source in {"local", "radio", "tidal"}`` assumptions across the playback
stack. TIDAL is deliberately a distinct source even though it rides the same
MPV engine as ``local``/``radio``; Spotify and Qobuz are external renderers
that FXRoute only controls and observes.
"""

from __future__ import annotations

# Sources that ride FXRoute's own MPV engine.
MPV_SOURCES = frozenset({"local", "radio", "tidal"})

# External renderers FXRoute controls/observes but does not own.
EXTERNAL_SOURCES = frozenset({"spotify", "qobuz"})

ALL_SOURCES = frozenset((*MPV_SOURCES, *EXTERNAL_SOURCES))

# Playback engine per source. ``tidal`` uses the MPV engine but stays its own
# source for footer/debug/queue/measurement-restore semantics.
ENGINE_BY_SOURCE = {
    "local": "mpv",
    "radio": "mpv",
    "tidal": "mpv",
    "spotify": "spotify",
    "qobuz": "qbzd",
}

# PipeWire node-name prefix used to identify the producing node in graph
# diagnosis. MPV and the Spotify client expose ``<node>:output_FL`` output
# ports; qbzd is an ALSA-backed node whose ports are named
# ``alsa_playback.qbzd:playback_FL``.
GRAPH_NODE_BY_SOURCE = {
    "local": "mpv",
    "radio": "mpv",
    "tidal": "mpv",
    "spotify": "spotify",
    "qobuz": "alsa_playback.qbzd",
}

# Port naming per engine. MPV/Spotify use ``output_<ch>``; the qbzd ALSA node
# uses ``playback_<ch>``.
_MPV_OUTPUT_CHANNELS = ("FL", "FR")
_QBZD_OUTPUT_CHANNELS = ("FL", "FR")


def is_known_source(source: str | None) -> bool:
    """Return whether ``source`` is one of the modeled playback sources."""
    return source in ALL_SOURCES


def is_mpv_source(source: str | None) -> bool:
    """Return whether ``source`` is played through FXRoute's MPV engine."""
    return source in MPV_SOURCES


def is_external_source(source: str | None) -> bool:
    """Return whether ``source`` is an external renderer (spotify/qobuz)."""
    return source in EXTERNAL_SOURCES


def engine_for(source: str | None) -> str | None:
    """Return the playback engine a source rides, or None for an unknown one."""
    if source is None:
        return None
    return ENGINE_BY_SOURCE.get(source)


def graph_node_for(source: str | None) -> str | None:
    """Return the PipeWire node-name prefix for a source, or None."""
    if source is None:
        return None
    return GRAPH_NODE_BY_SOURCE.get(source)


def graph_port_names(source: str | None) -> tuple[str, str] | None:
    """Return the two source output port names used in graph diagnosis.

    ``None`` for an unknown source. The channel set is stereo for every
    modeled source; subwoofer topologies derive their four channels from the
    DSP stage, never from the source.
    """
    node = graph_node_for(source)
    if not node:
        return None
    if source == "qobuz":
        return (
            f"{node}:playback_{_QBZD_OUTPUT_CHANNELS[0]}",
            f"{node}:playback_{_QBZD_OUTPUT_CHANNELS[1]}",
        )
    return (
        f"{node}:output_{_MPV_OUTPUT_CHANNELS[0]}",
        f"{node}:output_{_MPV_OUTPUT_CHANNELS[1]}",
    )
