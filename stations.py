"""Editable radio station storage and stream URL resolution."""

import json
import logging
import mimetypes
import os
import re
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from safe_http import (
    RADIO_PLAYLIST_FETCH_MAX_BYTES,
    SOMAFM_ARTWORK_FETCH_MAX_BYTES,
    SOMAFM_PAGE_FETCH_MAX_BYTES,
    safe_get,
)

import requests

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATION_ART_DIR = BASE_DIR / "static" / "station-art"
SOMAFM_NAME_TO_SLUG = {
    "groove salad": "groovesalad",
    "suburbs of goa": "suburbsofgoa",
    "the trip": "thetrip",
    "poptron": "poptron",
    "dub step beyond": "dubstep",
    "dubstep beyond": "dubstep",
    "somafm live": "live",
    "groove salad classic": "gsclassic",
    "seven inch soul": "7soul",
}
SOMAFM_SLUG_TO_NAME = {
    "groovesalad": "Groove Salad",
    "suburbsofgoa": "Suburbs of Goa",
    "thetrip": "The Trip",
    "poptron": "PopTron",
    "dubstep": "Dub Step Beyond",
    "live": "SomaFM Live",
    "gsclassic": "Groove Salad Classic",
    "7soul": "Seven Inch Soul",
}


@dataclass
class Station:
    id: str
    name: str
    stream_url: str
    input_url: Optional[str] = None
    image_url: Optional[str] = None
    custom_image_url: Optional[str] = None
    provider: Optional[str] = None


DEFAULT_STATIONS = [
    {
        "id": "groovesalad",
        "name": "Groove Salad",
        "input_url": "https://somafm.com/groovesalad130.pls",
        "stream_url": "https://ice4.somafm.com/groovesalad-256-mp3",
        "image_url": "https://somafm.com/logos/groovesalad.png",
    },
    {
        "id": "suburbsofgoa",
        "name": "Suburbs of Goa",
        "input_url": "https://somafm.com/suburbsofgoa130.pls",
        "stream_url": "https://ice4.somafm.com/suburbsofgoa-128-aac",
        "image_url": "https://somafm.com/logos/suburbsofgoa.png",
    },
    {
        "id": "thetrip",
        "name": "The Trip",
        "input_url": "https://somafm.com/thetrip130.pls",
        "stream_url": "https://ice4.somafm.com/thetrip-128-aac",
        "image_url": "https://somafm.com/logos/thetrip.png",
    },
    {
        "id": "poptron",
        "name": "PopTron",
        "input_url": "https://somafm.com/poptron130.pls",
        "stream_url": "https://ice4.somafm.com/poptron-128-aac",
        "image_url": "https://somafm.com/logos/poptron.png",
    },
    {
        "id": "dubstep",
        "name": "Dub Step Beyond",
        "input_url": "https://somafm.com/dubstep256.pls",
        "stream_url": "https://ice4.somafm.com/dubstep-256-mp3",
        "image_url": "https://somafm.com/logos/dubstep.png",
    },
    {
        "id": "live",
        "name": "SomaFM Live",
        "input_url": "https://somafm.com/live130.pls",
        "stream_url": "https://ice4.somafm.com/live-128-aac",
        "image_url": "https://somafm.com/logos/live.png",
    },
    {
        "id": "gsclassic",
        "name": "Groove Salad Classic",
        "input_url": "https://somafm.com/gsclassic130.pls",
        "stream_url": "https://ice4.somafm.com/gsclassic-128-aac",
        "image_url": "https://somafm.com/logos/gsclassic.png",
    },
]

# The read-only catalog is deliberately independent from DEFAULT_STATIONS.
# Personal selection remains stored exclusively in stations.json.
_SOMAFM_CATALOG_NAMES = {
    "7soul": "Seven Inch Soul",
    "beatblender": "Beat Blender",
    "bootliquor": "Boot Liquor",
    "brfm": "Black Rock FM",
    "cliqhop": "cliqhop idm",
    "covers": "Covers",
    "deepspaceone": "Deep Space One",
    "digitalis": "Digitalis",
    "doomed": "Doomed",
    "dronezone": "Drone Zone",
    "dz2": "Drone Zone 2",
    "dubstep": "Dub Step Beyond",
    "fluid": "Fluid",
    "folkfwd": "Folk Forward",
    "groovesalad": "Groove Salad",
    "groovesalad2": "Groove Salad 2",
    "gsclassic": "Groove Salad Classic",
    "illstreet": "Illinois Street Lounge",
    "indiepop": "Indie Pop Rocks!",
    "lush": "Lush",
    "missioncontrol": "Mission Control",
    "poptron": "PopTron",
    "secretagent": "Secret Agent",
    "seventies": "Left Coast 70s",
    "sonicuniverse": "Sonic Universe",
    "spacestation": "Space Station Soma",
    "suburbsofgoa": "Suburbs of Goa",
    "thetrip": "The Trip",
    "thistle": "ThistleRadio",
    "u80s": "Underground 80s",
    "metal": "Metal Detector",
    "reggae": "Heavyweight Reggae",
    "vaporwaves": "Vaporwaves",
    "synphaera": "Synphaera Radio",
    "darkzone": "The Dark Zone",
    "tikitime": "Tiki Time",
    "bossa": "Bossa Beyond",
    "insound": "The In-Sound",
}

_SOMAFM_CATALOG_IMAGE_URLS = {
    "brfm": "https://api.somafm.com/logos/512/brfm512.jpg",
    "fluid": "https://api.somafm.com/logos/512/fluid512.jpg",
    "gsclassic": "https://api.somafm.com/logos/512/gsclassic512.jpg",
    "missioncontrol": "https://api.somafm.com/logos/512/missioncontrol512.jpg",
    "seventies": "https://api.somafm.com/logos/512/seventies512.jpg",
    "thetrip": "https://api.somafm.com/logos/512/thetrip512.jpg",
    "thistle": "https://api.somafm.com/logos/512/thistle512.jpg",
    "synphaera": "https://api.somafm.com/logos/512/synphaera512.jpg",
    "darkzone": "https://api.somafm.com/logos/512/darkzone512.jpg",
    "tikitime": "https://api.somafm.com/logos/512/tikitime512.jpg",
    "bossa": "https://api.somafm.com/logos/512/bossa-512.jpg",
    "insound": "https://api.somafm.com/logos/512/insound-512.jpg",
}

_SOMAFM_EXISTING_CATALOG = {
    item["id"]: dict(item)
    for item in DEFAULT_STATIONS
    if item["id"] != "live"
}

STATION_CATALOG = (
    {
        "id": "rp-main",
        "name": "Radio Paradise Main Mix",
        "input_url": "https://stream.radioparadise.com/aac-320",
        "stream_url": "https://stream.radioparadise.com/aac-320",
        "image_url": "https://img.radioparadise.com/channels/0/0/cover_512x512/0.jpg",
        "provider": "Radio Paradise",
    },
    {
        "id": "rp-mellow",
        "name": "Radio Paradise Mellow Mix",
        "input_url": "https://stream.radioparadise.com/mellow-320",
        "stream_url": "https://stream.radioparadise.com/mellow-320",
        "image_url": "https://img.radioparadise.com/channels/0/1/cover_512x512/0.jpg",
        "provider": "Radio Paradise",
    },
    {
        "id": "rp-rock",
        "name": "Radio Paradise Rock Mix",
        "input_url": "https://stream.radioparadise.com/rock-320",
        "stream_url": "https://stream.radioparadise.com/rock-320",
        "image_url": "https://img.radioparadise.com/channels/0/2/cover_512x512/0.jpg",
        "provider": "Radio Paradise",
    },
    {
        "id": "rp-global",
        "name": "Radio Paradise Global Mix",
        "input_url": "https://stream.radioparadise.com/global-320",
        "stream_url": "https://stream.radioparadise.com/global-320",
        "image_url": "https://img.radioparadise.com/channels/0/3/cover_512x512/0.jpg",
        "provider": "Radio Paradise",
    },
    *(
        {
            **{
                "id": station_id,
                "name": station_name,
                "input_url": f"https://api.somafm.com/{station_id}130.pls",
                "stream_url": f"https://ice5.somafm.com/{station_id}-128-aac",
            },
            **_SOMAFM_EXISTING_CATALOG.get(station_id, {}),
            "image_url": _SOMAFM_CATALOG_IMAGE_URLS.get(
                station_id,
                f"https://api.somafm.com/logos/512/{station_id}512.png",
            ),
            "provider": "SomaFM",
        }
        for station_id, station_name in _SOMAFM_CATALOG_NAMES.items()
    ),
    *(
        {
            "id": station_id,
            "name": station_name,
            "input_url": f"https://icecast.radiofrance.fr/{slug}-midfi.mp3?id=openapi",
            "stream_url": f"https://icecast.radiofrance.fr/{slug}-midfi.mp3?id=openapi",
            "image_url": "https://www.radiofrance.fr/pikapi/images/a8903fd7-01e2-45a1-b768-61e3d8e1ff6a/512x512",
            "provider": "FIP",
        }
        for station_id, station_name, slug in (
            ("fip-main", "FIP", "fip"),
            ("fip-rock", "FIP Rock", "fiprock"),
            ("fip-jazz", "FIP Jazz", "fipjazz"),
            ("fip-groove", "FIP Groove", "fipgroove"),
            ("fip-world", "FIP Monde", "fipworld"),
            ("fip-nouveautes", "FIP Nouveautés", "fipnouveautes"),
            ("fip-reggae", "FIP Reggae", "fipreggae"),
            ("fip-electro", "FIP Electro", "fipelectro"),
            ("fip-metal", "FIP Metal", "fipmetal"),
            ("fip-pop", "FIP Pop", "fippop"),
            ("fip-hiphop", "FIP Hip-Hop", "fiphiphop"),
        )
    ),
    *(
        {
            "id": station_id,
            "name": station_name,
            "input_url": stream_url,
            "stream_url": stream_url,
            "image_url": image_url,
            "provider": "Other Stations",
        }
        for station_id, station_name, stream_url, image_url in (
            (
                "kexp-main", "KEXP Main", "https://kexp.streamguys1.com/kexp160.aac",
                "https://www.kexp.org/static/assets/img/logo-header.svg",
            ),
            (
                "wfmu-main", "WFMU Main", "http://stream0.wfmu.org/freeform-128k.mp3",
                "https://wfmu.org/images/wfmu-logo.svg",
            ),
            (
                "radio-calico", "Radio Calico", "https://stream.radio-calico.com/calico.mp3",
                "https://www.radio-calico.com/wp-content/uploads/2023/03/RadioCalicoLogo-green-300px.png",
            ),
            (
                "jb-radio-2", "JB Radio-2", "https://mediacp.jb-radio.net:8001/aac",
                "https://jb-radio.net/sites/all/themes/radio4b/favicon.ico",
            ),
            (
                "radio-swiss-jazz", "Radio Swiss Jazz", "https://stream.srg-ssr.ch/srgssr/rsj/aac/96",
                "https://www.radioswissjazz.ch/social-media/rsj-web.png",
            ),
            (
                "radio-swiss-pop", "Radio Swiss Pop", "https://stream.srg-ssr.ch/srgssr/rsp/aac/96",
                "https://www.radioswisspop.ch/social-media/rsp-web.png",
            ),
            (
                "radio-swiss-classic", "Radio Swiss Classic", "https://stream.srg-ssr.ch/srgssr/rsc_de/aac/96",
                "https://www.radioswissclassic.ch/social-media/rsc-web.png",
            ),
            (
                "kcrw-eclectic24", "KCRW Eclectic24", "https://streams.kcrw.com/e24_aac",
                "https://pressroom.kcrw.com/wp-content/uploads/sites/7/2012/05/KCRW_LOGO-Hero400.jpg",
            ),
        )
    ),
)

_cached_stations: Optional[List[Station]] = None
# Generation counter for the station cache.  A writer bumps it after every
# successful store commit; readers only publish a freshly loaded snapshot
# when the generation is unchanged, so a read that overlapped a commit can
# never publish stale state as the current cache.  The threading lock below
# guards ONLY the cache pointer and this counter (pure Python assignments);
# no network or file I/O ever happens under it.
_cache_generation = 0
_cache_lock = threading.Lock()


def _config_dir() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return root / "fxroute"


def _stations_file() -> Path:
    return _config_dir() / "stations.json"


def _legacy_stations_file() -> Path:
    return BASE_DIR / "stations.json"


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` without exposing partial content.

    The text is written to a temp file in the same directory, flushed,
    fsynced and closed, then atomically renamed over the target.  Readers
    observe either the old or the new complete JSON, never a truncated or
    partial file (the P1-4 worker offload makes store writes concurrent
    with side-effect-free readers on other threads).

    An existing regular target keeps its permission mode (fchmod, no symlink
    following); a new target keeps the safe mkstemp default (0600).  The
    process umask is never modified.  The descriptor is closed on every error
    path and the temp file is removed best-effort.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            try:
                existing_mode = os.lstat(path).st_mode
            except OSError:
                existing_mode = None
            if existing_mode is not None and stat.S_ISREG(existing_mode):
                os.fchmod(fd, stat.S_IMODE(existing_mode))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = None
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _ensure_storage() -> Path:
    path = _stations_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        legacy_path = _legacy_stations_file()
        if legacy_path.exists():
            _atomic_write_text(path, legacy_path.read_text(encoding="utf-8"))
            logger.info("Migrated stations storage to %s", path)
        else:
            _atomic_write_text(path, json.dumps(DEFAULT_STATIONS, indent=2) + "\n")
    return path


def _load_raw_stations() -> List[dict]:
    path = _ensure_storage()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to load stations.json: {e}")
        data = DEFAULT_STATIONS
    if not isinstance(data, list):
        raise ValueError("stations.json must contain a JSON array")
    return data


def _save_raw_stations(data: List[dict]) -> None:
    global _cache_generation, _cached_stations
    path = _ensure_storage()
    _atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    with _cache_lock:
        _cache_generation += 1
        _cached_stations = None


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "station"


def _make_unique_id(name: str, existing_ids: set[str], preferred_id: Optional[str] = None) -> str:
    candidate = _slugify(preferred_id or name)
    if candidate not in existing_ids:
        return candidate
    index = 2
    while f"{candidate}-{index}" in existing_ids:
        index += 1
    return f"{candidate}-{index}"


def _normalize_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        raise ValueError("Stream URL is required")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http:// or https:// URLs are supported")
    return value


def _normalize_optional_image_url(url: Optional[str]) -> Optional[str]:
    value = (url or "").strip()
    if not value:
        return None
    return _normalize_url(value)


def _extract_somafm_slug(name: str, input_url: str, stream_url: Optional[str] = None) -> Optional[str]:
    candidates = [input_url or "", stream_url or ""]
    for value in candidates:
        parsed = urlparse(value)
        host = (parsed.netloc or "").lower()
        if "somafm.com" not in host:
            continue

        path = parsed.path.strip("/")
        if not path:
            continue

        parts = [part for part in path.split("/") if part]
        probe_values = []
        if parts:
            probe_values.append(parts[0].lower())
            probe_values.append(parts[-1].lower())

        for probe in probe_values:
            candidate = probe
            candidate = re.sub(r"\.(png|jpg|jpeg|webp|gif)$", "", candidate)
            candidate = re.sub(r"(256|130)?\.pls$", "", candidate)
            candidate = re.sub(r"(32|64|128|256|400|512)$", "", candidate)
            candidate = re.sub(r"-(32|64|128|256)-(aac|mp3)$", "", candidate)
            candidate = re.sub(r"-(aac|mp3)$", "", candidate)
            candidate = candidate.strip(" -_")
            if candidate and candidate not in {"logos", "img3", "img", "channels", "banner"}:
                return candidate

    return SOMAFM_NAME_TO_SLUG.get(name.strip().lower())


def _existing_station_art_path(slug: str) -> Optional[Path]:
    STATION_ART_DIR.mkdir(parents=True, exist_ok=True)
    for suffix in (".png", ".jpg", ".jpeg", ".webp"):
        candidate = STATION_ART_DIR / f"{slug}{suffix}"
        if candidate.exists():
            return candidate
    return None


def _download_somafm_art(slug: str) -> Optional[Path]:
    STATION_ART_DIR.mkdir(parents=True, exist_ok=True)
    existing = _existing_station_art_path(slug)
    if existing:
        return existing

    page_url = f"https://somafm.com/{slug}/"
    try:
        resp = safe_get(page_url, timeout=5, max_bytes=SOMAFM_PAGE_FETCH_MAX_BYTES)
        if not resp.ok:
            return None
        html = resp.text
    except Exception as e:
        logger.debug(f"Failed to fetch SomaFM page for {slug}: {e}")
        return None

    candidates = []
    for pattern in [
        r'property=["\']og:image["\'] content=["\']([^"\']+)',
        r'twitter:image["\'] content=["\']([^"\']+)',
        rf'src=["\']([^"\']*{re.escape(slug)}[^"\']+)["\']',
    ]:
        for match in re.finditer(pattern, html, re.IGNORECASE):
            url = urljoin(page_url, match.group(1))
            if url not in candidates:
                candidates.append(url)

    for url in candidates:
        try:
            img_resp = safe_get(url, timeout=8, max_bytes=SOMAFM_ARTWORK_FETCH_MAX_BYTES)
            if not img_resp.ok:
                continue
            content_type = (img_resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            if not content_type.startswith("image/"):
                continue
            suffix = mimetypes.guess_extension(content_type) or Path(urlparse(url).path).suffix or ".jpg"
            if suffix == ".jpe":
                suffix = ".jpg"
            target = STATION_ART_DIR / f"{slug}{suffix}"
            target.write_bytes(img_resp.content)
            return target
        except Exception as e:
            logger.debug(f"Failed to download SomaFM art {url}: {e}")
    return None


def _titleize_station_slug(slug: str) -> str:
    if slug in SOMAFM_SLUG_TO_NAME:
        return SOMAFM_SLUG_TO_NAME[slug]
    slug = re.sub(r"(?<=\d)(?=[a-z])", " ", slug)
    slug = re.sub(r"(?<=[a-z])(?=\d)", " ", slug)
    slug = slug.replace("_", " ").replace("-", " ")
    parts = [part for part in slug.split() if part]
    return " ".join(part.capitalize() for part in parts) or "SomaFM"


def _auto_station_name(name: str, input_url: str, stream_url: str) -> str:
    cleaned = (name or "").strip()
    if cleaned:
        return cleaned
    slug = _extract_somafm_slug(cleaned, input_url, stream_url)
    if not slug:
        raise ValueError("Station name is required for non-SomaFM streams")
    return _titleize_station_slug(slug)


def _auto_station_image_url(name: str, input_url: str, stream_url: str) -> Optional[str]:
    slug = _extract_somafm_slug(name, input_url, stream_url)
    if not slug:
        return None
    art_path = _existing_station_art_path(slug) or _download_somafm_art(slug)
    if not art_path:
        return None
    return f"/static/station-art/{art_path.name}"


def _parse_pls(text: str) -> Optional[str]:
    match = re.search(r"File\d+=(https?://\S+)", text)
    return match.group(1).strip() if match else None


def _parse_m3u(text: str) -> Optional[str]:
    for line in text.splitlines():
        value = line.strip()
        if value and not value.startswith("#") and value.startswith(("http://", "https://")):
            return value
    return None


def _resolve_somafm_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    if "somafm.com" not in parsed.netloc:
        return None

    path = parsed.path.strip("/")
    slug = path.split("/")[0] if path else ""
    slug = re.sub(r"(256|130)?\.pls$", "", slug)
    slug = slug.strip()
    if not slug:
        return None

    variants = [
        f"https://somafm.com/{slug}256.pls",
        f"https://somafm.com/{slug}130.pls",
        f"https://somafm.com/{slug}.pls",
    ]
    for candidate in variants:
        try:
            resp = safe_get(candidate, timeout=5, max_bytes=RADIO_PLAYLIST_FETCH_MAX_BYTES)
            if resp.ok:
                resolved = _parse_pls(resp.text)
                if resolved:
                    return resolved
        except Exception as e:
            logger.debug(f"SomaFM resolve failed for {candidate}: {e}")
    return None


def resolve_stream_url(url: str) -> str:
    normalized = _normalize_url(url)

    somafm_resolved = _resolve_somafm_url(normalized)
    if somafm_resolved:
        return somafm_resolved

    lower = normalized.lower()
    if lower.endswith(".pls"):
        try:
            resp = safe_get(normalized, timeout=5, max_bytes=RADIO_PLAYLIST_FETCH_MAX_BYTES)
            if not resp.ok:
                raise ValueError(f"Playlist URL returned {resp.status_code}")
            resolved = _parse_pls(resp.text)
            if not resolved:
                raise ValueError("Could not read a playable stream from the .pls file")
            return resolved
        except requests.RequestException as e:
            raise ValueError(f"Could not fetch the .pls URL: {e}") from e

    if lower.endswith(".m3u") or lower.endswith(".m3u8"):
        try:
            resp = safe_get(normalized, timeout=5, max_bytes=RADIO_PLAYLIST_FETCH_MAX_BYTES)
            if not resp.ok:
                raise ValueError(f"Playlist URL returned {resp.status_code}")
            resolved = _parse_m3u(resp.text)
            if not resolved:
                raise ValueError("Could not read a playable stream from the playlist")
            return resolved
        except requests.RequestException as e:
            raise ValueError(f"Could not fetch the playlist URL: {e}") from e

    return normalized


def get_stations(enrich_missing_art: bool = False) -> List[Station]:
    """Load saved stations from stations.json.

    Read-only by default: a missing ``image_url`` is returned as ``None``
    and no network access, state mutation or persistence happens.  With
    ``enrich_missing_art=True`` missing SomaFM artwork is resolved lazily
    (network) and the result is persisted back into stations.json; that
    mode is only used by the API routes that need it and must run under
    the station mutation ownership (see radio_api).

    The cache is publication-safe: the file is read outside the cache
    lock, and a snapshot is only published when no writer committed while
    it was being loaded; otherwise the current cache wins or the state is
    reloaded.  Callers can therefore never observe a stale snapshot that
    was published after a newer commit.
    """
    global _cached_stations
    while True:
        if not enrich_missing_art:
            with _cache_lock:
                if _cached_stations is not None:
                    return _cached_stations
                generation_before = _cache_generation
        else:
            with _cache_lock:
                generation_before = _cache_generation

        raw = _load_raw_stations()
        changed = False
        stations: List[Station] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            station_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            stream_url = str(item.get("stream_url") or "").strip()
            input_url = str(item.get("input_url") or stream_url).strip()
            if not station_id or not name or not stream_url:
                continue
            image_url = str(item.get("image_url") or "").strip() or None
            custom_image_url = str(item.get("custom_image_url") or "").strip() or None
            if not image_url and enrich_missing_art:
                image_url = _auto_station_image_url(name, input_url, stream_url)
                if image_url:
                    item["image_url"] = image_url
                    changed = True
            stations.append(
                Station(
                    id=station_id,
                    name=name,
                    stream_url=stream_url,
                    input_url=input_url,
                    image_url=image_url,
                    custom_image_url=custom_image_url,
                )
            )

        if changed:
            _save_raw_stations(raw)

        with _cache_lock:
            if _cache_generation == generation_before:
                _cached_stations = stations
                return stations
            current = _cached_stations
        if current is not None:
            return current


def get_station_catalog() -> List[Station]:
    return [Station(**dict(item)) for item in STATION_CATALOG]


def find_saved_catalog_station(catalog_station: Station, stations: Optional[List[Station]] = None) -> Optional[Station]:
    saved_stations = get_stations() if stations is None else stations
    catalog_input_url = (catalog_station.input_url or catalog_station.stream_url or "").strip()
    catalog_stream_url = (catalog_station.stream_url or "").strip()
    for station in saved_stations:
        saved_input_url = (station.input_url or station.stream_url or "").strip()
        saved_stream_url = (station.stream_url or "").strip()
        if catalog_input_url and saved_input_url == catalog_input_url:
            return station
        if catalog_stream_url and saved_stream_url == catalog_stream_url:
            return station
    return None


def add_catalog_station(catalog_id: str) -> Station:
    catalog_station = next((station for station in get_station_catalog() if station.id == catalog_id), None)
    if catalog_station is None:
        raise FileNotFoundError(f"Catalog station not found: {catalog_id}")

    saved_station = find_saved_catalog_station(catalog_station)
    if saved_station is not None:
        return saved_station

    raw = _load_raw_stations()
    existing_ids = {str(item.get("id") or "").strip() for item in raw}
    station = {
        "id": _make_unique_id(catalog_station.name, existing_ids, preferred_id=catalog_station.id),
        "name": catalog_station.name,
        "input_url": catalog_station.input_url or catalog_station.stream_url,
        "stream_url": catalog_station.stream_url,
        "image_url": catalog_station.image_url,
        "custom_image_url": None,
    }
    raw.append(station)
    _save_raw_stations(raw)
    return Station(**station)


def add_station(name: str, input_url: str, custom_image_url: Optional[str] = None) -> Station:
    raw = _load_raw_stations()
    existing_ids = {str(item.get("id") or "").strip() for item in raw}
    normalized_input_url = _normalize_url(input_url)
    normalized_custom_image_url = _normalize_optional_image_url(custom_image_url)
    stream_url = resolve_stream_url(input_url)
    resolved_name = _auto_station_name(name, normalized_input_url, stream_url)
    station = {
        "id": _make_unique_id(resolved_name, existing_ids),
        "name": resolved_name,
        "input_url": normalized_input_url,
        "stream_url": stream_url,
        "image_url": _auto_station_image_url(resolved_name, normalized_input_url, stream_url),
        "custom_image_url": normalized_custom_image_url,
    }
    raw.append(station)
    _save_raw_stations(raw)
    return Station(**station)


def update_station(station_id: str, name: str, input_url: str, custom_image_url: Optional[str] = None) -> Station:
    raw = _load_raw_stations()
    for item in raw:
        if str(item.get("id") or "").strip() != station_id:
            continue
        normalized_input_url = _normalize_url(input_url)
        normalized_custom_image_url = _normalize_optional_image_url(custom_image_url)
        stream_url = resolve_stream_url(input_url)
        resolved_name = _auto_station_name(name, normalized_input_url, stream_url)
        item["name"] = resolved_name
        item["input_url"] = normalized_input_url
        item["stream_url"] = stream_url
        item["image_url"] = _auto_station_image_url(item["name"], normalized_input_url, stream_url)
        item["custom_image_url"] = normalized_custom_image_url
        _save_raw_stations(raw)
        return Station(
            id=item["id"],
            name=item["name"],
            stream_url=item["stream_url"],
            input_url=item.get("input_url"),
            image_url=item.get("image_url"),
            custom_image_url=item.get("custom_image_url"),
        )
    raise FileNotFoundError(f"Station not found: {station_id}")


def delete_station(station_id: str) -> None:
    raw = _load_raw_stations()
    filtered = [item for item in raw if str(item.get("id") or "").strip() != station_id]
    if len(filtered) == len(raw):
        raise FileNotFoundError(f"Station not found: {station_id}")
    _save_raw_stations(filtered)
