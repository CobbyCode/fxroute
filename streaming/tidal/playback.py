# SPDX-License-Identifier: AGPL-3.0-only

"""TIDAL stream resolution for the FXRoute native playback path.

Resolves a playable stream from a ``tidalapi`` Track and normalizes the audio
info into FXRoute units:

* ``sample_rate``: Hz (integer)
* ``bit_depth``: integer
* ``audio_format``: ``"flac"`` or ``"aac"``

The stream URL is resolved as late as possible and is never persisted as track
metadata: callers store the TIDAL track id and call
:func:`resolve_stream_for_track` only at play time.

* A direct FLAC/AAC URL is preferred when the session grants one (the
  link/device-login flow, capped at HIGH/320k AAC by TIDAL).
* PKCE sessions (lossless/Hi-Res) only expose a DASH manifest.  MPV's ffmpeg
  DASH demuxer cannot fetch the HTTPS segments under FXRoute's hardened
  ``protocol_whitelist``, so the manifest is materialized into a single local
  FLAC-in-MP4 file with ``ffmpeg -c:a copy`` (no re-encode), which MPV then
  plays directly.  The temporary file is cache-scoped and pruned on later
  resolutions.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from streaming.tidal import auth

logger = logging.getLogger(__name__)

# Error kinds surfaced to the API so callers can map them to HTTP statuses
# without depending on tidalapi's exception types.
KIND_RIGHTS = "rights"
KIND_UNAVAILABLE = "unavailable"
KIND_AUTH = "auth"
KIND_NETWORK = "network"
KIND_UNSUPPORTED = "unsupported"

_DASH_MAX_AGE_SEC = 3600.0
_DASH_DOWNLOAD_TIMEOUT = 300.0


class TidalStreamError(RuntimeError):
    """Normalized failure while resolving a TIDAL stream."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _stream_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "fxroute" / "tidal"


def _safe_unlink(path: str | Path | None) -> None:
    try:
        if path and Path(path).exists():
            Path(path).unlink()
    except OSError:
        pass


def _prune_old_files(directory: Path) -> None:
    try:
        now = time.time()
        for entry in directory.iterdir():
            if entry.is_file() and now - entry.stat().st_mtime > _DASH_MAX_AGE_SEC:
                _safe_unlink(entry)
    except OSError:
        pass


def _download_dash(manifest: str, *, directory: Path | None = None) -> str:
    """Materialize a DASH manifest into a single local FLAC-in-MP4 file.

    Returns the path MPV should play.  Raises :class:`TidalStreamError` when
    ffmpeg is unavailable or the download/mux fails.
    """
    if not shutil.which("ffmpeg"):
        raise TidalStreamError(KIND_UNSUPPORTED, "ffmpeg is required to play TIDAL DASH streams")
    directory = directory or _stream_dir()
    directory.mkdir(parents=True, exist_ok=True)
    _prune_old_files(directory)

    fd, mpd_path = tempfile.mkstemp(prefix="tidal-", suffix=".mpd", dir=str(directory))
    with os.fdopen(fd, "w") as handle:
        handle.write(manifest)
    out_path = mpd_path[:-4] + ".mp4"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-protocol_whitelist", "file,http,https,tcp,tls",
        "-i", mpd_path,
        "-c:a", "copy",
        "-f", "mp4",
        "-y", out_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_DASH_DOWNLOAD_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - subprocess raises broad types
        _safe_unlink(mpd_path)
        _safe_unlink(out_path)
        raise TidalStreamError(KIND_NETWORK, f"TIDAL stream download failed: {exc}") from exc
    _safe_unlink(mpd_path)
    if result.returncode != 0 or not Path(out_path).exists():
        stderr = (result.stderr or "").strip()
        logger.warning("ffmpeg failed to materialize TIDAL stream: %s", stderr[:300])
        _safe_unlink(out_path)
        raise TidalStreamError(KIND_UNSUPPORTED, "ffmpeg could not materialize the TIDAL stream")
    return out_path


def _format_from_codecs(codecs: str) -> str:
    return "flac" if "flac" in str(codecs or "").lower() else "aac"


def _map_exception(exc: Exception) -> TidalStreamError:
    name = type(exc).__name__
    # Lazy import keeps this module importable without tidalapi.
    try:
        import tidalapi.exceptions as tex
    except ImportError:
        tex = None  # type: ignore[assignment]

    if tex is not None:
        if isinstance(exc, tex.StreamNotAvailable):
            return TidalStreamError(KIND_RIGHTS, "stream not available for this track")
        if isinstance(exc, tex.URLNotAvailable):
            return TidalStreamError(KIND_UNSUPPORTED, "direct stream URL not available for this quality")
        if isinstance(exc, tex.ObjectNotFound):
            return TidalStreamError(KIND_UNAVAILABLE, "track not found")
        if isinstance(exc, tex.AuthenticationError):
            return TidalStreamError(KIND_AUTH, "TIDAL session is not authenticated")
        if isinstance(exc, tex.TooManyRequests):
            return TidalStreamError(KIND_NETWORK, "TIDAL rate limited")
        if isinstance(exc, (tex.ManifestDecodeError, tex.UnknownManifestFormat, tex.MPDNotAvailableError)):
            return TidalStreamError(KIND_UNSUPPORTED, f"unsupported stream manifest: {exc}")
    return TidalStreamError(KIND_NETWORK, f"TIDAL stream resolution failed: {name}")


def _normalize_sample_rate_hz(value: Any) -> int | None:
    """Normalize a TIDAL sample rate to Hz.

    TIDAL/DASH reports Hz natively (44100, 88200, 96000, 192000); a few paths
    expose kHz.  Values below 1000 are treated as kHz and scaled up, mirroring
    the Qobuz normalization.
    """
    try:
        if value is None:
            return None
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if rate <= 0:
        return None
    if rate < 1000:
        rate *= 1000
    return int(rate)


def _normalize_bit_depth(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def resolve_stream_for_track(track: Any, *, directory: Path | None = None) -> dict:
    """Resolve a playable stream plus audio info for a tidalapi Track.

    Returns ``{"url", "sample_rate", "bit_depth", "audio_format", "dash"}``.
    Raises :class:`TidalStreamError` when the track has no playable stream.
    """
    session = getattr(track, "session", None)
    if session is None:
        raise TidalStreamError(KIND_UNAVAILABLE, "track has no TIDAL session")

    # Authoritative audio info comes from the Stream object (manifest-derived
    # for Hi-Res).  Resolve it first; a rights-restricted track raises here
    # before we ever produce a URL.
    try:
        stream = track.get_stream()
    except Exception as exc:  # noqa: BLE001
        raise _map_exception(exc) from exc

    sample_rate = _normalize_sample_rate_hz(getattr(stream, "sample_rate", None))
    bit_depth = _normalize_bit_depth(getattr(stream, "bit_depth", None))

    stream_manifest = None
    audio_format: str | None = None
    try:
        stream_manifest = stream.get_stream_manifest()
        manifest_rate = _normalize_sample_rate_hz(getattr(stream_manifest, "sample_rate", None))
        if manifest_rate:
            sample_rate = manifest_rate
        audio_format = _format_from_codecs(getattr(stream_manifest, "codecs", ""))
    except Exception:
        # Manifest parsing is best-effort for info; URL resolution below still
        # has the direct-URL path to fall back on.
        stream_manifest = None

    if audio_format is None:
        lossless = bool(getattr(track, "is_lossless", False) or getattr(track, "is_hi_res_lossless", False))
        audio_format = "flac" if lossless else "aac"

    # Prefer the direct URL (non-PKCE / device-login sessions).
    try:
        url = track.get_url()
        if url:
            return {
                "url": str(url),
                "sample_rate": sample_rate,
                "bit_depth": bit_depth,
                "audio_format": audio_format,
                "dash": False,
            }
    except Exception:
        pass

    # DASH fallback: materialize the manifest into a local file for MPV.
    try:
        manifest = stream.get_manifest_data()
    except Exception as exc:  # noqa: BLE001
        raise _map_exception(exc) from exc
    if not manifest:
        raise TidalStreamError(KIND_UNSUPPORTED, "empty DASH manifest for track")
    path = _download_dash(manifest, directory=directory)
    return {
        "url": path,
        "sample_rate": sample_rate,
        "bit_depth": bit_depth,
        "audio_format": audio_format,
        "dash": True,
    }


def resolve_stream_for_id(track_id: str, *, directory: Path | None = None) -> dict:
    """Fetch a track by id and resolve its stream in one step."""
    session = auth.manager.session()
    if session is None:
        raise TidalStreamError(KIND_AUTH, "TIDAL is not authenticated")
    try:
        track = session.track(track_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_exception(exc) from exc
    return resolve_stream_for_track(track, directory=directory)
