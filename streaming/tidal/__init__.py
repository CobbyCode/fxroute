# SPDX-License-Identifier: AGPL-3.0-only

"""TIDAL provider declaration.

TIDAL is planned as a native integration via ``tidalapi`` / ``python-tidal``
(OAuth/device-flow, catalog, playback into the FXRoute playback path). This
module only declares the provider and its intended capability surface; no
backend is implemented yet and the provider never reports itself as
available or working.
"""

from __future__ import annotations

from typing import ClassVar

from streaming.base.capabilities import Capabilities
from streaming.base.provider import DeclaredStreamingProvider


class TidalProvider(DeclaredStreamingProvider):
    provider_id: ClassVar[str] = "tidal"
    display_name: ClassVar[str] = "TIDAL"

    def capabilities(self) -> Capabilities:
        # Intended surface: transport, full catalog/search plus hi-res stream
        # info (tidalapi exposes format/samplerate/bit-depth per track).
        return Capabilities(
            transport=True,
            seek=True,
            progress=True,
            search=True,
            library=True,
            favorites=True,
            playlists=True,
            recommendations=True,
            radio=True,
            cover=True,
            audio_format=True,
            sample_rate=True,
            bit_depth=True,
        )
