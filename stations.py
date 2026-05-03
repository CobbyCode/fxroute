"""Editable radio station storage and stream URL resolution."""

import json
import logging
import mimetypes
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import requests

from models import Track

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

_cached_stations: Optional[List[Station]] = None


def _config_dir() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return root / "fxroute"


def _stations_file() -> Path:
    return _config_dir() / "stations.json"


def _legacy_stations_file() -> Path:
    return BASE_DIR / "stations.json"


def _ensure_storage() -> Path:
    path = _stations_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        legacy_path = _legacy_stations_file()
        if legacy_path.exists():
            path.write_text(legacy_path.read_text(encoding="utf-8"), encoding="utf-8")
            logger.info("Migrated stations storage to %s", path)
        else:
            path.write_text(json.dumps(DEFAULT_STATIONS, indent=2) + "\n", encoding="utf-8")
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
    global _cached_stations
    path = _ensure_storage()
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
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
        resp = requests.get(page_url, timeout=5)
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
            img_resp = requests.get(url, timeout=8)
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


def _is_somafm_source(input_url: str, stream_url: Optional[str] = None) -> bool:
    return _extract_somafm_slug("", input_url, stream_url) is not None


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
            resp = requests.get(candidate, timeout=5)
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
            resp = requests.get(normalized, timeout=5)
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
            resp = requests.get(normalized, timeout=5)
            if not resp.ok:
                raise ValueError(f"Playlist URL returned {resp.status_code}")
            resolved = _parse_m3u(resp.text)
            if not resolved:
                raise ValueError("Could not read a playable stream from the playlist")
            return resolved
        except requests.RequestException as e:
            raise ValueError(f"Could not fetch the playlist URL: {e}") from e

    return normalized


def get_stations() -> List[Station]:
    global _cached_stations
    if _cached_stations is not None:
        return _cached_stations

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
        if not image_url:
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
    _cached_stations = stations
    return stations


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


def station_to_track(station: Station) -> Track:
    return Track(
        id=f"radio_{station.id}",
        title=station.name,
        artist="Radio",
        source="radio",
        url=station.stream_url,
        duration=None,
    )
