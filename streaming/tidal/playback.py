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
  DASH demuxer cannot fetch the HTTPS segments under this mpv build (no
  protocol-whitelist escape is possible upstream), so the manifest is
  materialized into a single local fragmented-MP4 file: the init segment
  followed by every media segment in order.  Segments are fetched
  concurrently (TIDAL CDN is latency-bound per request; ~5x the throughput of
  sequential ffmpeg on .104), which keeps the pre-roll download in the
  single-digit seconds instead of dominating play start latency.  The file is
  cached per track/quality and pruned on later resolutions.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
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
_DASH_FETCH_WORKERS = 16
_DASH_FETCH_RETRIES = 2
_DASH_USER_AGENT = "Mozilla/5.0"
_MPD_NS = "{urn:mpeg:dash:schema:mpd:2011}"


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


def _dash_template(manifest: str) -> tuple[str, str, int]:
    """Extract init URL, media template and segment count from a TIDAL MPD.

    TIDAL lossless/Hi-Res manifests use a single ``SegmentTemplate`` with a
    ``SegmentTimeline`` (``$Number$``-based media URLs).  Raises
    :class:`TidalStreamError` when the manifest is not a shape we can
    materialize.
    """
    try:
        root = ET.fromstring(manifest)
    except ET.ParseError as exc:
        raise TidalStreamError(KIND_UNSUPPORTED, "invalid DASH manifest") from exc
    template = root.find(f".//{_MPD_NS}SegmentTemplate")
    init_url = ""
    media_url = ""
    if template is not None:
        init_url = str(template.get("initialization") or "")
        media_url = str(template.get("media") or "")
    if not init_url or not media_url:
        raise TidalStreamError(KIND_UNSUPPORTED, "unsupported DASH manifest: missing segment URLs")
    timeline = root.find(f".//{_MPD_NS}SegmentTimeline")
    count = 0
    if timeline is not None:
        for s in timeline.findall(f"{_MPD_NS}S"):
            count += 1 + max(0, int(s.get("r") or 0))
    if count <= 0:
        raise TidalStreamError(KIND_UNSUPPORTED, "unsupported DASH manifest: no media segments")
    return init_url, media_url, count


def _segment_url(template: str, number: int) -> str:
    """Expand a DASH media template for one segment number."""
    if "$Number$" in template:
        return template.replace("$Number$", str(number))
    if "$Time$" in template:
        # TIDAL uses $Number$; a $Time$ template gets a best-effort position.
        return template.replace("$Time$", str(number))
    raise TidalStreamError(KIND_UNSUPPORTED, "unsupported DASH media template")


def _http_download(url: str, dest: Path, *, timeout: float = 30.0) -> None:
    """Stream one URL body to ``dest``, raising on HTTP/network failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _DASH_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        with dest.open("wb") as handle:
            shutil.copyfileobj(resp, handle, length=1 << 16)


def _fetch_segment(template: str, number: int, dest: Path) -> None:
    """Fetch one media segment with bounded retries on transient failures."""
    url = _segment_url(template, number)
    last_error: Exception | None = None
    for attempt in range(_DASH_FETCH_RETRIES + 1):
        try:
            _http_download(url, dest)
            return
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            last_error = exc
            if attempt < _DASH_FETCH_RETRIES:
                time.sleep(0.3 * (attempt + 1))
            _safe_unlink(dest)
    raise TidalStreamError(
        KIND_NETWORK, f"TIDAL stream segment {number} download failed: {last_error}"
    ) from last_error


def _materialize_dash(
    init_url: str,
    media_url: str,
    count: int,
    *,
    directory: Path,
    name: str,
) -> Path:
    """Fetch init + all segments concurrently and concatenate them in order.

    Every media segment is an independent fMP4 fragment (``styp``/``moof``
    boxes), so the playable file is the init segment followed by each media
    segment in order.  Returns the path MPV should play.
    """
    tmp_root = directory / f".{name}-parts"
    tmp_root.mkdir(parents=True, exist_ok=True)
    out_path = directory / f"{name}.mp4"
    try:
        init_tmp = tmp_root / "init.mp4"
        try:
            _http_download(init_url, init_tmp)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            raise TidalStreamError(
                KIND_NETWORK, f"TIDAL stream init download failed: {exc}"
            ) from exc
        part_files: dict[int, Path] = {}
        with ThreadPoolExecutor(max_workers=_DASH_FETCH_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_segment, media_url, number, tmp_root / f"seg-{number:05d}.mp4"): number
                for number in range(1, count + 1)
            }
            for future in as_completed(futures):
                number = futures[future]
                future.result()  # re-raise fetch failures
                part_files[number] = tmp_root / f"seg-{number:05d}.mp4"
        with out_path.open("wb") as out:
            for src in [init_tmp] + [part_files[number] for number in sorted(part_files)]:
                with src.open("rb") as handle:
                    shutil.copyfileobj(handle, out, length=1 << 16)
        logger.info(
            "TIDAL DASH materialized: segments=%s output=%s size=%s",
            count,
            out_path,
            out_path.stat().st_size,
        )
        return out_path
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _download_dash(
    manifest: str,
    *,
    directory: Path | None = None,
    cache_key: str | None = None,
) -> str:
    """Materialize a DASH manifest into a single local fragmented-MP4 file.

    The media segments are fetched concurrently instead of sequentially by
    ffmpeg, which is the dominant part of TIDAL play start latency (measured
    ~5x faster on .104).  A content-stable ``cache_key`` (track id + quality)
    reuses a fresh materialization on repeat plays.

    Returns the path MPV should play.  Raises :class:`TidalStreamError` when
    the manifest is unsupported or a segment download fails.
    """
    init_url, media_url, count = _dash_template(manifest)
    directory = directory or _stream_dir()
    directory.mkdir(parents=True, exist_ok=True)
    _prune_old_files(directory)

    digest = hashlib.sha1(cache_key.encode("utf-8") if cache_key else manifest.encode("utf-8")).hexdigest()[:12]
    name = f"tidal-{digest}"
    cached = directory / f"{name}.mp4"
    try:
        if (
            cache_key
            and cached.exists()
            and time.time() - cached.stat().st_mtime < _DASH_MAX_AGE_SEC
        ):
            logger.info("TIDAL DASH cache hit: %s", cached)
            return str(cached)
    except OSError:
        pass

    out_path = _materialize_dash(init_url, media_url, count, directory=directory, name=name)
    if cache_key and out_path != cached:
        try:
            os.replace(out_path, cached)
            return str(cached)
        except OSError:
            return str(out_path)
    return str(out_path)


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
    cache_key = "-n".join(
        str(part) for part in (getattr(track, "id", ""), sample_rate, bit_depth) if part
    )
    path = _download_dash(manifest, directory=directory, cache_key=cache_key)
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
