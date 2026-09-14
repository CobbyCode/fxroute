# SPDX-License-Identifier: AGPL-3.0-only

"""Spotify provider (playerctl/MPRIS, desktop and spotifyd backends).

Consumers import the leaf modules directly: ``streaming.spotify.mpris`` for
playerctl/MPRIS access, ``streaming.spotify.provider`` for the provider class
and its flat-dict transport functions, and ``streaming.spotify.connect_name``
for the Connect device name. The package re-exports nothing; provider
registration lives in ``streaming/__init__.py``.
"""
