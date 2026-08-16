# SPDX-License-Identifier: AGPL-3.0-only

"""Normalization of live stream audio facts (mpv property values, not URL guesses)."""

from __future__ import annotations

import re
from typing import Optional

# Normalization of live stream facts (mpv property values, not URL guesses).
LOSSLESS_CODECS = {"flac", "alac", "ape", "wavpack", "tta"}
STREAM_CODEC_LABELS = {
    "aac": "AAC", "mp3": "MP3", "flac": "FLAC", "vorbis": "Vorbis",
    "opus": "Opus", "alac": "ALAC", "pcm": "PCM",
}
# Decoded sample format -> source bit depth for lossless codecs (ffmpeg
# decoder convention: 16-bit FLAC decodes to s16, 24-bit to s32, 32-bit to s64;
# PCM depth is parsed from the codec name instead).
FORMAT_BIT_DEPTH = {"s16": 16, "s24": 24, "s32": 24, "s64": 32}


def _bit_depth_from_codec_name(codec_name: str) -> Optional[int]:
    """Parse an explicit bit depth from the mpv codec name (e.g. PCM)."""
    m = re.search(r"(\d+)-bit", codec_name)
    return int(m.group(1)) if m else None


def normalize_stream_info(raw: dict) -> Optional[dict]:
    """Reduce raw mpv stream audio facts to the compact display form.

    Only values mpv actually delivered are kept. Unknown parts are omitted
    entirely; no placeholder text and no URL/file-extension guessing.
    """
    if not raw:
        return None
    codec_name = str(raw.get("codec") or "").strip()
    short = codec_name.split()[0].lower() if codec_name else ""
    if not short:
        # No format anchor: a bare bitrate would produce a misleading line.
        return None
    info: dict = {"codec": STREAM_CODEC_LABELS.get(short, short.upper())}
    if short in LOSSLESS_CODECS:
        info["profile"] = "Lossless"
    bitrate = raw.get("bitrate_bps")
    if isinstance(bitrate, (int, float)) and bitrate > 0:
        info["bitrate_kbps"] = int(round(bitrate / 1000))
    samplerate = raw.get("samplerate_hz")
    if isinstance(samplerate, int) and samplerate > 0:
        info["samplerate_hz"] = samplerate
    # Bit depth: explicit in PCM codec names; for lossless codecs derive from
    # the decoded sample format. Lossy decodes (floatp) have no source depth.
    depth = _bit_depth_from_codec_name(codec_name)
    if depth is None and short in LOSSLESS_CODECS:
        depth = FORMAT_BIT_DEPTH.get(str(raw.get("format") or ""))
    if depth and depth > 0:
        info["bit_depth"] = depth
    return info if info else None
