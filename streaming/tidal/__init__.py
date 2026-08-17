# SPDX-License-Identifier: AGPL-3.0-only

"""TIDAL provider (native FXRoute integration via ``tidalapi``).

Catalog/auth live here; playback rides the shared FXRoute playback owner
(MPV -> fxroute_dsp_sink -> DSP), so this package never owns its own transport
or queue.  ``tidalapi`` is imported lazily and the provider reports
``available=false`` when the dependency is missing.
"""

from streaming.tidal.provider import TIDAL_BACKEND, TidalProvider

__all__ = ["TIDAL_BACKEND", "TidalProvider"]
