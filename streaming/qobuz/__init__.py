# SPDX-License-Identifier: AGPL-3.0-only

"""Qobuz provider declaration.

Qobuz is planned via ``qbzd`` (Qobuz Connect / headless) with permanent audio
routing to ``fxroute_dsp_sink``. This module only declares the provider and
its intended capability surface; no backend is implemented yet and the
provider never reports itself as available or working.
"""

from __future__ import annotations

from typing import ClassVar

from streaming.base.capabilities import Capabilities
from streaming.base.provider import DeclaredStreamingProvider


class QobuzProvider(DeclaredStreamingProvider):
    provider_id: ClassVar[str] = "qobuz"
    display_name: ClassVar[str] = "Qobuz"

    def capabilities(self) -> Capabilities:
        # Intended surface: transport, search/catalog and hi-res stream info.
        # No volume/queue_editing until the qbzd control surface is confirmed.
        return Capabilities(
            transport=True,
            seek=True,
            progress=True,
            search=True,
            favorites=True,
            playlists=True,
            recommendations=True,
            lyrics=True,
            cover=True,
            audio_format=True,
            sample_rate=True,
            bit_depth=True,
        )
