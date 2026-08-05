# SPDX-License-Identifier: AGPL-3.0-only

"""FastAPI routes for radio station management."""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from stations import (
    add_catalog_station,
    add_station,
    delete_station,
    find_saved_catalog_station,
    get_station_catalog,
    get_stations,
    update_station,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
RADIO_BROWSER_BASE_URL = os.environ.get("FXROUTE_RADIO_BROWSER_URL", "https://de1.api.radio-browser.info").rstrip("/")
RADIO_BROWSER_MIRRORS = (
    "https://de2.api.radio-browser.info",
    "https://all.api.radio-browser.info",
)
RADIO_BROWSER_TIMEOUT = (2, 5)
RADIO_BROWSER_RETRY_TIMEOUT = (1.5, 3)
RADIO_BROWSER_MAX_ATTEMPTS = 2
RADIO_BROWSER_RETRY_DELAY_SECONDS = 0.3
RADIO_BROWSER_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RADIO_BROWSER_RESULT_LIMIT = 30
RADIO_BROWSER_QUERY_LIMIT = 12
router = APIRouter()


class StationUpsertRequest(BaseModel):
    name: Optional[str] = None
    stream_url: str
    custom_image_url: Optional[str] = None


class StationImportItem(BaseModel):
    name: str = ""
    url: str = ""
    logo: str = ""
    genre: str = ""


def _path_within_root(path: Path, root: Path) -> bool:
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
    except Exception:
        return False
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def _station_art_url_if_available(url: Optional[str]) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    if value.startswith("/static/station-art/"):
        art_path = (STATIC_DIR / "station-art" / Path(value).name).resolve()
        if not _path_within_root(art_path, STATIC_DIR / "station-art") or not art_path.is_file():
            return ""
    return value


def _station_api_payload(station):
    image_url = _station_art_url_if_available(station.image_url)
    custom_image_url = _station_art_url_if_available(station.custom_image_url)
    cached_custom_image_url = _station_art_url_if_available(getattr(station, "cached_custom_image_url", None))
    return {
        "id": station.id,
        "title": station.name,
        "image": cached_custom_image_url or custom_image_url or image_url or "",
        "image_url": image_url,
        "custom_image_url": custom_image_url,
        "cached_custom_image_url": cached_custom_image_url,
        "stream_url": station.stream_url,
        "input_url": station.input_url or station.stream_url,
        "artist": "Radio",
    }


def _fxroute_user_agent() -> str:
    try:
        version = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        version = "unknown"
    return f"FXRoute/{version or 'unknown'}"


def _radio_browser_mirrors() -> list:
    """Primary mirror first, then fallbacks (deduplicated)."""
    mirrors = [RADIO_BROWSER_BASE_URL]
    for mirror in RADIO_BROWSER_MIRRORS:
        if mirror not in mirrors:
            mirrors.append(mirror)
    return mirrors


def _radio_browser_request(path: str, params: dict) -> list:
    """Fetch from radio-browser.info with mirror fallback and one retry round.

    Retryable failures (5xx, 429, connection errors, timeouts) move on to the
    next mirror; after all mirrors a short backoff precedes one retry round.
    Non-retryable HTTP errors fail fast. Only when every mirror and attempt
    failed is an HTTPException raised.
    """
    mirrors = _radio_browser_mirrors()
    last_error: Optional[Exception] = None
    for attempt in range(RADIO_BROWSER_MAX_ATTEMPTS):
        timeout = RADIO_BROWSER_TIMEOUT if attempt == 0 else RADIO_BROWSER_RETRY_TIMEOUT
        for base_url in mirrors:
            try:
                response = requests.get(
                    f"{base_url}{path}",
                    params=params,
                    headers={"User-Agent": _fxroute_user_agent()},
                    timeout=timeout,
                )
                if response.status_code == 200:
                    data = response.json()
                    if not isinstance(data, list):
                        raise ValueError("Radio Browser returned an invalid response")
                    return data
                if response.status_code in RADIO_BROWSER_RETRYABLE_STATUS:
                    last_error = HTTPException(
                        status_code=502,
                        detail=f"Radio Browser HTTP {response.status_code}",
                    )
                    retry_after = (response.headers or {}).get("Retry-After")
                    if (
                        retry_after
                        and retry_after.isdigit()
                        and attempt + 1 < RADIO_BROWSER_MAX_ATTEMPTS
                    ):
                        time.sleep(min(int(retry_after), 2))
                    continue
                raise HTTPException(
                    status_code=502,
                    detail=f"Radio Browser HTTP {response.status_code}",
                )
            except requests.Timeout as e:
                last_error = HTTPException(status_code=504, detail="Radio Browser request timed out")
            except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
                last_error = HTTPException(status_code=502, detail="Radio Browser provider is unavailable")
        if attempt + 1 < RADIO_BROWSER_MAX_ATTEMPTS:
            time.sleep(RADIO_BROWSER_RETRY_DELAY_SECONDS)
    raise last_error or HTTPException(status_code=502, detail="Radio Browser provider is unavailable")


def _saved_station_for_browser_item(item: dict):
    input_url = str(item.get("url") or "").strip()
    resolved_url = str(item.get("url_resolved") or "").strip()
    for station in get_stations():
        saved_input = str(station.input_url or station.stream_url or "").strip()
        saved_stream = str(station.stream_url or "").strip()
        if input_url and saved_input == input_url:
            return station
        if resolved_url and saved_stream == resolved_url:
            return station
    return None


def _browser_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _browser_stream_meets_quality(payload: dict) -> bool:
    codec = str(payload.get("codec") or "").strip().upper()
    bitrate = _browser_int(payload.get("bitrate"))
    if codec.startswith("AAC"):
        return bitrate >= 96
    if codec == "MP3":
        return bitrate >= 128
    if "VORBIS" in codec or codec == "OGG" or codec.startswith("OGG "):
        return bitrate >= 96
    return True


def _browser_http_url(value) -> str:
    cleaned = str(value or "").strip()
    parsed = urlparse(cleaned)
    return cleaned if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _radio_browser_payload(item: dict) -> Optional[dict]:
    station_uuid = str(item.get("stationuuid") or "").strip()
    name = str(item.get("name") or "").strip()
    input_url = _browser_http_url(item.get("url"))
    resolved_url = _browser_http_url(item.get("url_resolved"))
    if not station_uuid or not name or not input_url or not resolved_url:
        return None
    saved_station = _saved_station_for_browser_item(item)
    favicon = _browser_http_url(item.get("favicon"))
    return {
        "stationuuid": station_uuid,
        "title": name,
        "url": input_url,
        "url_resolved": resolved_url,
        "favicon": favicon,
        "image": favicon,
        "country": str(item.get("country") or "").strip(),
        "countrycode": str(item.get("countrycode") or "").strip(),
        "language": str(item.get("language") or "").strip(),
        "tags": str(item.get("tags") or "").strip(),
        "codec": str(item.get("codec") or "").strip(),
        "bitrate": _browser_int(item.get("bitrate")),
        "lastcheckok": _browser_int(item.get("lastcheckok")),
        "clickcount": _browser_int(item.get("clickcount")),
        "is_saved": saved_station is not None,
        "saved_station_id": saved_station.id if saved_station else None,
    }


@router.get("/api/stations")
async def list_stations():
    return [_station_api_payload(station) for station in get_stations()]


@router.get("/api/station-catalog")
async def list_station_catalog():
    saved_stations = get_stations()
    result = []
    for catalog_station in get_station_catalog():
        saved_station = find_saved_catalog_station(catalog_station, saved_stations)
        payload = _station_api_payload(catalog_station)
        payload["provider"] = catalog_station.provider
        payload["is_saved"] = saved_station is not None
        payload["saved_station_id"] = saved_station.id if saved_station else None
        result.append(payload)
    return result


@router.post("/api/station-catalog/{catalog_id}/selection")
async def add_station_catalog_selection(catalog_id: str):
    try:
        station = add_catalog_station(catalog_id)
        return {"status": "ok", "station": _station_api_payload(station)}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/api/station-browser/search")
async def search_station_browser(
    query: str,
):
    cleaned_query = query.strip()
    if not cleaned_query:
        raise HTTPException(status_code=400, detail="Station search query is required")

    search_fields = ("name", "country", "language", "tag")
    common_params = {
        "hidebroken": "true",
        "order": "clickcount",
        "reverse": "true",
        "limit": RADIO_BROWSER_QUERY_LIMIT,
    }
    requests_result = await asyncio.gather(*(
        asyncio.to_thread(
            _radio_browser_request,
            "/json/stations/search",
            {**common_params, field: cleaned_query},
        )
        for field in search_fields
    ), return_exceptions=True)

    successful = [items for items in requests_result if isinstance(items, list)]
    if not successful:
        error = next((item for item in requests_result if isinstance(item, HTTPException)), None)
        raise error or HTTPException(status_code=502, detail="Radio Browser provider is unavailable")

    ranked = []
    seen_uuids = set()
    seen_urls = set()
    for field_rank, items in enumerate(requests_result):
        if not isinstance(items, list):
            continue
        for item in items[:RADIO_BROWSER_QUERY_LIMIT]:
            if not isinstance(item, dict):
                continue
            payload = _radio_browser_payload(item)
            if payload is None:
                continue
            if not _browser_stream_meets_quality(payload):
                continue
            station_uuid = payload["stationuuid"]
            urls = {payload["url"], payload["url_resolved"]} - {""}
            if station_uuid in seen_uuids or urls & seen_urls:
                continue
            seen_uuids.add(station_uuid)
            seen_urls.update(urls)
            name_match = cleaned_query.casefold() in payload["title"].casefold()
            ranked.append((0 if name_match else field_rank + 1, -payload["clickcount"], payload))

    ranked.sort(key=lambda entry: (entry[0], entry[1], entry[2]["title"].casefold()))
    return [payload for _, _, payload in ranked[:RADIO_BROWSER_RESULT_LIMIT]]


@router.post("/api/station-browser/{station_uuid}/selection")
async def add_station_browser_selection(station_uuid: str):
    items = await asyncio.to_thread(
        _radio_browser_request,
        "/json/stations/byuuid",
        {"uuids": station_uuid},
    )
    item = next(
        (
            candidate
            for candidate in items
            if isinstance(candidate, dict)
            and str(candidate.get("stationuuid") or "").strip() == station_uuid
        ),
        None,
    )
    payload = _radio_browser_payload(item) if item else None
    if payload is None:
        raise HTTPException(status_code=404, detail="Radio Browser station not found")
    saved_station = _saved_station_for_browser_item(item)
    if saved_station is None:
        try:
            saved_station = await asyncio.to_thread(
                add_station,
                payload["title"],
                payload["url"],
                payload["favicon"],
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "station": _station_api_payload(saved_station)}


@router.post("/api/stations")
async def create_station(req: StationUpsertRequest):
    try:
        station = add_station(req.name, req.stream_url, req.custom_image_url)
        return {"status": "ok", "station": _station_api_payload(station)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/api/stations/{station_id}")
async def edit_station(station_id: str, req: StationUpsertRequest):
    try:
        station = update_station(station_id, req.name, req.stream_url, req.custom_image_url)
        return {"status": "ok", "station": _station_api_payload(station)}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api/stations/{station_id}")
async def remove_station(station_id: str):
    try:
        delete_station(station_id)
        return {"status": "ok", "deleted": station_id}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/api/stations/import")
async def import_stations(items: list[StationImportItem]):
    results = []
    for item in items:
        name = (item.name or "").strip()
        stream_url = (item.url or "").strip()
        custom_image_url = (item.logo or "").strip()
        if not stream_url:
            results.append({"status": "skipped", "reason": "missing url", "name": name})
            continue
        if not name:
            parsed = urlparse(stream_url)
            name = parsed.netloc or "Unknown Station"
        try:
            add_station(name, stream_url, custom_image_url)
            results.append({"status": "ok", "name": name})
        except ValueError as e:
            results.append({"status": "error", "name": name, "reason": str(e)})
    return {"results": results}
