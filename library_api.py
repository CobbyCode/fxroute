# SPDX-License-Identifier: AGPL-3.0-only

"""FastAPI routes for the local music library: tracks, albums, playlists, covers."""

import asyncio
import hashlib
import logging
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from mutagen import File as MutagenFile
from starlette.background import BackgroundTask

import playlist_io
import zip_album
from library import cleanup_track_parent_folder, path_within_root
from uploads import (
    LIBRARY_UPLOAD_MAX_BYTES,
    TEXT_UPLOAD_MAX_BYTES,
    UploadTooLargeError,
    read_upload,
    save_upload_to_file,
)
from models import (
    DeleteFolderRequest,
    DeleteTracksRequest,
    DownloadTracksRequest,
    PlaylistSaveRequest,
)
from playlists import delete_playlist, get_playlists, save_playlist
from zip_album import PLAYLIST_FILE_EXTENSIONS, UPLOAD_AUDIO_EXTENSIONS

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
COVER_CACHE_DIR = BASE_DIR / "media" / "cache" / "covers"
TOP40_COVER_IMAGE = STATIC_DIR / "Top40.png"
ALBUM_COVER_CACHE_DIR = BASE_DIR / "media" / "cache" / "album-covers"

router = APIRouter()


@dataclass(frozen=True)
class LibraryApiRuntime:
    get_scanner: Callable[[], Any]
    get_settings: Callable[[], Any]


_runtime: LibraryApiRuntime | None = None


def configure_runtime(runtime: LibraryApiRuntime) -> None:
    global _runtime
    _runtime = runtime


def _library_runtime() -> tuple[Any, Any]:
    if _runtime is None:
        raise RuntimeError("Library API runtime is not configured")
    return _runtime.get_scanner(), _runtime.get_settings()


def _cover_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "application/octet-stream"


def _serve_cover_image(image_path: Path, size: int = 256) -> FileResponse:
    """Serve an album cover, using cached thumbnails when Pillow is available."""
    image_path = image_path.resolve()
    if not image_path.is_file():
        raise FileNotFoundError(str(image_path))

    media_type = _cover_media_type(image_path)
    try:
        from PIL import Image
    except ModuleNotFoundError:
        logger.debug("Pillow is not installed; serving original cover image for %s", image_path)
        return FileResponse(str(image_path), media_type=media_type)

    cache_key = hashlib.sha256(
        f"{image_path}:{image_path.stat().st_mtime_ns}:{size}".encode()
    ).hexdigest()[:16]
    suffix = image_path.suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
        suffix = ".jpg"
    cached = ALBUM_COVER_CACHE_DIR / f"{cache_key}{suffix}"
    if cached.is_file():
        return FileResponse(str(cached), media_type=_cover_media_type(cached))

    ALBUM_COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with Image.open(str(image_path)) as img:
        img = img.convert("RGB")
        img.thumbnail((size, size), Image.LANCZOS)
        save_kwargs = {"quality": 85} if suffix in (".jpg", ".jpeg") else {}
        # Keep the image suffix last so Pillow can infer the encoder for the
        # atomic temporary write (e.g. ``cover.tmp.jpg`` rather than
        # ``cover.jpg.tmp``).
        tmp = cached.with_name(f"{cached.stem}.tmp{cached.suffix}")
        img.save(str(tmp), **save_kwargs)
        tmp.replace(cached)
    return FileResponse(str(cached), media_type=_cover_media_type(cached))


def _folder_cover_for_track(track_path: Path) -> Optional[Path]:
    """Find a cover image in the track's folder.
    Priority: exact names (cover.jpg etc.) > any image with cover/folder/art in name.
    """
    parent = track_path.parent
    # Fast path: exact names
    for name in (
        "cover.jpg", "cover.jpeg", "cover.png", "cover.webp",
        "folder.jpg", "folder.jpeg", "folder.png", "folder.webp",
        "front.jpg", "front.jpeg", "front.png", "front.webp",
    ):
        candidate = parent / name
        if candidate.is_file():
            return candidate
    # Fallback: any image with cover/folder/front/album/art in the filename
    try:
        for f in sorted(parent.iterdir()):
            if not f.is_file():
                continue
            fl = f.name.lower()
            if any(kw in fl for kw in ("cover", "folder", "front", "album", "art")) and fl.endswith((".jpg", ".jpeg", ".png", ".webp")):
                return f
    except OSError:
        pass
    return None


def _embedded_cover_bytes(track_path: Path) -> tuple[Optional[bytes], Optional[str]]:
    try:
        audio = MutagenFile(str(track_path), easy=False)
    except Exception as exc:
        logger.debug("Cover metadata read failed for %s: %s", track_path, exc)
        return None, None
    if not audio:
        return None, None

    for picture in getattr(audio, "pictures", []) or []:
        data = getattr(picture, "data", None)
        mime = getattr(picture, "mime", None) or "image/jpeg"
        if data:
            return bytes(data), mime

    tags = getattr(audio, "tags", None)
    if not tags:
        return None, None

    for key, value in tags.items():
        if str(key).startswith("APIC"):
            data = getattr(value, "data", None)
            mime = getattr(value, "mime", None) or "image/jpeg"
            if data:
                return bytes(data), mime

    covers = tags.get("covr") if hasattr(tags, "get") else None
    if covers:
        first = covers[0]
        image_format = getattr(first, "imageformat", None)
        mime = "image/png" if image_format == 14 else "image/jpeg"
        return bytes(first), mime

    return None, None


def _cached_embedded_cover(track_id: str, track_path: Path) -> tuple[Optional[Path], Optional[str]]:
    try:
        stat = track_path.stat()
    except FileNotFoundError:
        return None, None
    cache_key = hashlib.sha256(f"{track_id}:{track_path}:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")).hexdigest()
    for suffix, media_type in ((".jpg", "image/jpeg"), (".png", "image/png"), (".webp", "image/webp")):
        cached = COVER_CACHE_DIR / f"{cache_key}{suffix}"
        if cached.is_file():
            return cached, media_type

    data, mime = _embedded_cover_bytes(track_path)
    if not data:
        return None, None
    suffix = ".png" if mime == "image/png" else ".webp" if mime == "image/webp" else ".jpg"
    COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = COVER_CACHE_DIR / f"{cache_key}{suffix}"
    tmp = cached.with_suffix(cached.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(cached)
    return cached, mime or _cover_media_type(cached)


def _track_cover_available(track_id: str) -> bool:
    library_scanner, settings = _library_runtime()

    tracks_by_id = {track.id: track for track in library_scanner.get_tracks(refresh=False)}
    track = tracks_by_id.get(track_id)
    if not track or not track.path:
        return False
    track_path = track.path.resolve()
    if not path_within_root(track_path, settings.MUSIC_ROOT) or not track_path.is_file():
        return False
    folder_cover = _folder_cover_for_track(track_path)
    if folder_cover and path_within_root(folder_cover.resolve(), settings.MUSIC_ROOT):
        return True
    cached_cover, _media_type = _cached_embedded_cover(track_id, track_path)
    return bool(cached_cover and cached_cover.is_file())


def _record_local_track_started(track_info: Optional[dict]) -> None:
    library_scanner, _settings = _library_runtime()

    if not library_scanner or not track_info:
        return
    if track_info.get("source") != "local":
        return
    track_id = str(track_info.get("id") or "").strip()
    if not track_id:
        return
    try:
        library_scanner.record_track_play(track_id)
    except Exception as exc:
        logger.debug("Failed to update local track play stats for %s: %s", track_id, exc)


def _cleanup_temp_file(path: Path):
    path.unlink(missing_ok=True)


def _resolve_library_folder(folder: str, music_root: Path) -> Path:
    requested = Path(str(folder or "").strip().lstrip("/"))
    if not str(requested):
        raise HTTPException(status_code=400, detail="folder is required")
    if requested.is_absolute() or ".." in requested.parts:
        raise HTTPException(status_code=400, detail="Invalid folder path")
    folder_path = (music_root / requested).resolve()
    if folder_path == music_root.resolve() or not path_within_root(folder_path, music_root):
        raise HTTPException(status_code=403, detail="Folder path outside music root")
    if not folder_path.is_dir():
        raise HTTPException(status_code=404, detail="Folder not found")
    return folder_path


@router.get("/api/tracks")
async def list_tracks():
    library_scanner, _settings = _library_runtime()
    tracks = library_scanner.get_tracks()
    return [t.to_dict() for t in tracks]


@router.get("/api/tracks/file/{track_id:path}")
async def download_track_file(track_id: str):
    library_scanner, settings = _library_runtime()
    tracks_by_id = {track.id: track for track in library_scanner.get_tracks(refresh=True)}
    track = tracks_by_id.get(track_id)
    if not track or not track.path:
        raise HTTPException(status_code=404, detail="Track not found")
    track_path = track.path.resolve()
    if not path_within_root(track_path, settings.MUSIC_ROOT):
        raise HTTPException(status_code=403, detail="Track path outside music root")
    if not track_path.is_file():
        raise HTTPException(status_code=404, detail="Track file missing")
    return FileResponse(track_path, filename=track_path.name)


@router.get("/api/tracks/cover/{track_id:path}")
async def get_track_cover(track_id: str):
    library_scanner, settings = _library_runtime()
    tracks_by_id = {track.id: track for track in library_scanner.get_tracks(refresh=False)}
    track = tracks_by_id.get(track_id)
    if not track or not track.path:
        raise HTTPException(status_code=404, detail="Track not found")
    track_path = track.path.resolve()
    if not path_within_root(track_path, settings.MUSIC_ROOT):
        raise HTTPException(status_code=403, detail="Track path outside music root")
    if not track_path.is_file():
        raise HTTPException(status_code=404, detail="Track file missing")

    folder_cover = _folder_cover_for_track(track_path)
    if folder_cover:
        cover_path = folder_cover.resolve()
        if path_within_root(cover_path, settings.MUSIC_ROOT):
            return FileResponse(cover_path, media_type=_cover_media_type(cover_path))

    cached_cover, media_type = _cached_embedded_cover(track_id, track_path)
    if cached_cover and cached_cover.is_file():
        return FileResponse(cached_cover, media_type=media_type or _cover_media_type(cached_cover))

    raise HTTPException(status_code=404, detail="Cover not found")


@router.get("/api/tracks/cover-info/{track_id:path}")
async def get_track_cover_info(track_id: str):
    return {"available": _track_cover_available(track_id)}


@router.get("/api/smart/top-tracks")
async def get_smart_top_tracks(limit: int = 40):
    library_scanner, _settings = _library_runtime()
    if not library_scanner:
        raise HTTPException(status_code=503, detail="Library not available")
    return library_scanner.get_top_played_tracks(limit=limit)


@router.get("/api/smart/top40/cover")
async def get_smart_top40_cover():
    if not TOP40_COVER_IMAGE.is_file():
        raise HTTPException(status_code=404, detail="Top 40 cover not found")
    return FileResponse(TOP40_COVER_IMAGE, media_type="image/png")


@router.get("/api/albums")
async def list_albums(query: Optional[str] = None):
    """List albums grouped from the local library, optionally filtered by search query."""
    library_scanner, _settings = _library_runtime()
    if not library_scanner:
        raise HTTPException(status_code=503, detail="Library not available")
    albums = library_scanner.get_albums()
    if query:
        q = query.strip().lower()
        filtered = []
        for album in albums:
            # Search in album name, artist, genre, year, and track metadata.
            match = (
                q in album["name"].lower()
                or q in album["artist"].lower()
                or q in " ".join(album.get("genres") or []).lower()
                or q in " ".join(str(year) for year in (album.get("years") or [])).lower()
            )
            if not match:
                album_tracks = library_scanner.get_album_tracks(album["id"])
                match = any(
                    q in " ".join(
                        str(value)
                        for value in (
                            t.title,
                            t.artist,
                            t.album,
                            t.album_artist,
                            t.genre,
                            t.year,
                        )
                        if value
                    ).lower()
                    for t in album_tracks
                )
            if match:
                filtered.append(album)
        albums = filtered
    return albums


@router.get("/api/albums/{album_id}/tracks")
async def get_album_tracks(album_id: str):
    """Return tracks for a specific album, sorted by disc/track number."""
    library_scanner, _settings = _library_runtime()
    if not library_scanner:
        raise HTTPException(status_code=503, detail="Library not available")
    tracks = library_scanner.get_album_tracks(album_id)
    if not tracks:
        raise HTTPException(status_code=404, detail="Album not found")
    return [t.to_dict() for t in tracks]


@router.post("/api/albums/{album_id}/favorite")
async def set_album_favorite(album_id: str, request: Request):
    """Persist album favorite state in the smart metadata cache."""
    library_scanner, _settings = _library_runtime()
    if not library_scanner:
        raise HTTPException(status_code=503, detail="Library not available")
    tracks = library_scanner.get_album_tracks(album_id)
    if not tracks:
        raise HTTPException(status_code=404, detail="Album not found")
    body = await request.json()
    favorite = bool(body.get("favorite"))
    metadata = library_scanner.set_album_favorite(album_id, favorite)
    return {"status": "ok", "album_id": album_id, "favorite": bool(metadata.get("favorite"))}


@router.get("/api/albums/{album_id}/discover")
async def get_album_discover(album_id: str, refresh: bool = False):
    """Return cached similar-music suggestions for an album."""
    library_scanner, _settings = _library_runtime()
    if not library_scanner:
        raise HTTPException(status_code=503, detail="Library not available")
    tracks = library_scanner.get_album_tracks(album_id)
    if not tracks:
        raise HTTPException(status_code=404, detail="Album not found")
    result = library_scanner.get_album_discover(album_id, force=refresh)
    return {
        "album_id": album_id,
        "items": result.get("items") or [],
        "source": result.get("source"),
        "cached": bool(result.get("cached")),
        "error": result.get("error"),
    }


@router.get("/api/albums/{album_id}/cover")
async def get_album_cover(album_id: str, size: int = 256):
    """Return cover image for an album, resized to thumbnail.
    Priority: folder cover > embedded cover > external cover > 404.
    """
    library_scanner, _settings = _library_runtime()
    if not library_scanner:
        raise HTTPException(status_code=503, detail="Library not available")
    tracks = library_scanner.get_album_tracks(album_id)
    if not tracks:
        raise HTTPException(status_code=404, detail="Album not found")

    # Try folder cover first (from any track in the album)
    for track in tracks:
        if not track.path:
            continue
        folder_cover = _folder_cover_for_track(track.path)
        if folder_cover:
            try:
                return _serve_cover_image(folder_cover, size)
            except Exception as exc:
                logger.warning("Failed to serve folder album cover %s for album %s: %s", folder_cover, album_id, exc)

    # Try embedded cover from first track that has one
    for track in tracks:
        if not track.path:
            continue
        cached_cover, media_type = _cached_embedded_cover(track.id, track.path)
        if cached_cover and cached_cover.is_file():
            try:
                return _serve_cover_image(cached_cover, size)
            except Exception as exc:
                logger.warning("Failed to serve embedded album cover %s for album %s: %s", cached_cover, album_id, exc)

    external_cover = library_scanner.get_album_external_cover(album_id)
    if external_cover and external_cover.is_file():
        try:
            return _serve_cover_image(external_cover, size)
        except Exception as exc:
            logger.warning("Failed to serve external album cover %s for album %s: %s", external_cover, album_id, exc)

    raise HTTPException(status_code=404, detail="Cover not found")


@router.post("/api/tracks/download")
async def download_tracks(req: DownloadTracksRequest):
    library_scanner, settings = _library_runtime()
    if not library_scanner or not settings:
        raise HTTPException(status_code=503, detail="Library not available")
    if not req.track_ids:
        raise HTTPException(status_code=400, detail="track_ids is required")

    tracks_by_id = {track.id: track for track in library_scanner.get_tracks(refresh=True)}
    selected_tracks = []
    seen_ids = set()
    for track_id in req.track_ids:
        if not track_id or track_id in seen_ids:
            continue
        seen_ids.add(track_id)
        track = tracks_by_id.get(track_id)
        if not track or not track.path:
            raise HTTPException(status_code=404, detail=f"Track not found: {track_id}")
        track_path = track.path.resolve()
        if not path_within_root(track_path, settings.MUSIC_ROOT):
            raise HTTPException(status_code=403, detail="Track path outside music root")
        if not track_path.is_file():
            raise HTTPException(status_code=404, detail=f"Track file missing: {track_path.name}")
        selected_tracks.append((track, track_path))

    if not selected_tracks:
        raise HTTPException(status_code=404, detail="No downloadable tracks found")
    if len(selected_tracks) == 1:
        _, track_path = selected_tracks[0]
        return FileResponse(track_path, filename=track_path.name)

    with tempfile.NamedTemporaryFile(prefix="fxroute-library-selection-", suffix=".zip", delete=False) as temp_file:
        temp_zip_path = Path(temp_file.name)

    used_names = set()
    try:
        with zipfile.ZipFile(temp_zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for _, track_path in selected_tracks:
                archive.write(track_path, arcname=zip_album.dedupe_archive_name(track_path.name, used_names))
    except Exception:
        temp_zip_path.unlink(missing_ok=True)
        raise

    return FileResponse(
        temp_zip_path,
        filename="fxroute-library-selection.zip",
        media_type="application/zip",
        background=BackgroundTask(_cleanup_temp_file, temp_zip_path),
    )


@router.get("/api/playlists")
async def list_playlists():
    return [
        {
            "id": playlist.id,
            "name": playlist.name,
            "track_ids": playlist.track_ids,
            "track_count": len(playlist.track_ids),
        }
        for playlist in get_playlists()
    ]


@router.post("/api/playlists")
async def create_or_update_playlist(req: PlaylistSaveRequest):
    try:
        playlist = save_playlist(req.name, req.track_ids)
        return {
            "status": "ok",
            "playlist": {
                "id": playlist.id,
                "name": playlist.name,
                "track_ids": playlist.track_ids,
                "track_count": len(playlist.track_ids),
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/playlists/{playlist_id}/export")
async def export_playlist(playlist_id: str):
    library_scanner, settings = _library_runtime()
    if not library_scanner or not settings:
        raise HTTPException(status_code=503, detail="Library not available")
    playlist = next((item for item in get_playlists() if item.id == playlist_id), None)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    content = playlist_io.build_m3u_for_playlist(
        playlist,
        library_scanner.get_tracks(refresh=True),
        settings.MUSIC_ROOT,
    )
    filename = playlist_io.playlist_download_filename(playlist.name)
    return Response(
        content=content,
        media_type="audio/x-mpegurl; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/api/playlists/{playlist_id}")
async def remove_playlist(playlist_id: str):
    try:
        delete_playlist(playlist_id)
        return {"status": "ok", "deleted": playlist_id}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/api/library/upload")
async def upload_track(file: UploadFile = File(...)):
    library_scanner, settings = _library_runtime()
    if not library_scanner or not settings:
        raise HTTPException(status_code=503, detail="Library not available")

    filename = Path(file.filename or "").name.strip()
    if not filename:
        raise HTTPException(status_code=400, detail="A filename is required")

    suffix = Path(filename).suffix.lower()
    if suffix not in UPLOAD_AUDIO_EXTENSIONS and suffix not in PLAYLIST_FILE_EXTENSIONS and suffix != ".zip":
        raise HTTPException(status_code=400, detail="Unsupported file type")

    target_dir = settings.download_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = None
    album_dir = None
    temp_zip_path = None

    try:
        if suffix == ".zip":
            temp_zip_path = zip_album.choose_unique_path(target_dir / filename)
            with temp_zip_path.open("wb") as buffer:
                await save_upload_to_file(file, buffer, LIBRARY_UPLOAD_MAX_BYTES)

            album_dir = zip_album.choose_unique_dir(target_dir / Path(filename).stem)
            album_dir.mkdir(parents=True, exist_ok=False)

            try:
                extraction = zip_album.extract_zip_album(temp_zip_path, album_dir)
                audio_files = extraction["audio_files"]
                playlist_files = extraction["playlist_files"]
                if not audio_files and not playlist_files:
                    shutil.rmtree(album_dir, ignore_errors=True)
                    raise HTTPException(status_code=400, detail="ZIP contains no supported audio or playlist files")
            except Exception:
                shutil.rmtree(album_dir, ignore_errors=True)
                raise
            finally:
                temp_zip_path.unlink(missing_ok=True)

            tracks = library_scanner.refresh(force=True)
            imported_playlists = []
            for playlist_path in playlist_files:
                imported = playlist_io.import_m3u_playlist(
                    playlist_path.name,
                    playlist_path.read_text(encoding="utf-8", errors="replace"),
                    settings.MUSIC_ROOT,
                    base_dir=playlist_path.parent,
                    tracks=tracks,
                )
                if imported:
                    imported_playlists.append(imported)
            if not audio_files and not imported_playlists:
                shutil.rmtree(album_dir, ignore_errors=True)
                raise HTTPException(status_code=400, detail="Playlist did not match any library tracks")
            playlist_part = f" and {len(imported_playlists)} playlist{'s' if len(imported_playlists) != 1 else ''}" if imported_playlists else ""
            return {
                "status": "imported",
                "kind": "zip",
                "filename": filename,
                "folder": album_dir.name,
                "path": str(album_dir),
                "track_count": len(tracks),
                "imported_track_count": len(audio_files),
                "imported_playlist_count": len(imported_playlists),
                "playlists": imported_playlists,
                "skipped_entry_count": len(extraction["skipped_entries"]),
                "message": f"Imported {len(audio_files)} track{'s' if len(audio_files) != 1 else ''}{playlist_part} from {filename}",
            }

        if suffix in PLAYLIST_FILE_EXTENSIONS:
            content = (await read_upload(file, TEXT_UPLOAD_MAX_BYTES)).decode("utf-8", errors="replace")
            tracks = library_scanner.get_tracks(refresh=True)
            imported = playlist_io.import_m3u_playlist(
                filename, content, settings.MUSIC_ROOT, tracks=tracks
            )
            if not imported:
                raise HTTPException(status_code=400, detail="Playlist did not match any library tracks")
            return {
                "status": "imported",
                "kind": "playlist",
                "filename": filename,
                "track_count": len(tracks),
                "imported_playlist_count": 1,
                "playlist": imported,
                "message": f"Imported playlist {imported['name']} with {imported['track_count']} track{'s' if imported['track_count'] != 1 else ''}",
            }

        target_path = target_dir / filename
        if target_path.exists():
            raise HTTPException(status_code=409, detail="A file with that name already exists")

        with target_path.open("wb") as buffer:
            await save_upload_to_file(file, buffer, LIBRARY_UPLOAD_MAX_BYTES)
        tracks = library_scanner.refresh(force=True)
        return {
            "status": "uploaded",
            "kind": "audio",
            "filename": filename,
            "path": str(target_path),
            "track_count": len(tracks),
            "message": f"Uploaded {filename}",
        }
    except asyncio.CancelledError:
        # A cancelled request must not leave partial files created by this
        # request behind; the cancellation itself propagates unchanged.
        if temp_zip_path and temp_zip_path.exists():
            temp_zip_path.unlink(missing_ok=True)
        if target_path and target_path.exists():
            target_path.unlink(missing_ok=True)
        if album_dir and album_dir.exists():
            shutil.rmtree(album_dir, ignore_errors=True)
        raise
    except UploadTooLargeError as e:
        logger.warning("Upload rejected: %s", e)
        if temp_zip_path and temp_zip_path.exists():
            temp_zip_path.unlink(missing_ok=True)
        if target_path and target_path.exists():
            target_path.unlink(missing_ok=True)
        if album_dir and album_dir.exists():
            shutil.rmtree(album_dir, ignore_errors=True)
        raise HTTPException(status_code=413, detail=str(e))
    except zip_album.ZipLimitError as e:
        logger.warning("ZIP upload rejected: %s", e)
        if temp_zip_path and temp_zip_path.exists():
            temp_zip_path.unlink(missing_ok=True)
        if album_dir and album_dir.exists():
            shutil.rmtree(album_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        if temp_zip_path and temp_zip_path.exists():
            temp_zip_path.unlink(missing_ok=True)
        if album_dir and album_dir.exists() and not any(album_dir.iterdir()):
            album_dir.rmdir()
        raise
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        if temp_zip_path and temp_zip_path.exists():
            temp_zip_path.unlink(missing_ok=True)
        if target_path and target_path.exists():
            target_path.unlink(missing_ok=True)
        if album_dir and album_dir.exists():
            shutil.rmtree(album_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Upload failed")
    finally:
        await file.close()


@router.post("/api/tracks/delete")
async def delete_tracks(req: DeleteTracksRequest):
    library_scanner, settings = _library_runtime()
    if not library_scanner or not settings:
        raise HTTPException(status_code=503, detail="Library not available")

    if not req.track_ids:
        raise HTTPException(status_code=400, detail="track_ids is required")

    tracks_by_id = {track.id: track for track in library_scanner.get_tracks(refresh=True)}
    deleted = []
    errors = []
    affected_folders = set()
    music_root = settings.MUSIC_ROOT.resolve()

    for track_id in req.track_ids:
        track = tracks_by_id.get(track_id)
        if not track or not track.path:
            errors.append({"track_id": track_id, "error": "Track not found"})
            continue

        try:
            path = track.path.resolve()
            if not path_within_root(path, music_root) or not path.is_file():
                errors.append({"track_id": track_id, "error": "Track path outside music root"})
                continue
            parent = path.parent
            path.unlink()
            deleted.append(track_id)
            affected_folders.add(parent)
        except Exception as e:
            errors.append({"track_id": track_id, "error": str(e)})

    cleanup = [
        cleanup_track_parent_folder(folder, music_root, {settings.download_dir.resolve()})
        for folder in sorted(affected_folders)
    ]
    tracks = library_scanner.refresh(force=True)
    return {
        "status": "ok",
        "deleted": deleted,
        "errors": errors,
        "cleanup": cleanup,
        "track_count": len(tracks),
    }


@router.post("/api/library/folders/delete")
async def delete_library_folder(req: DeleteFolderRequest):
    library_scanner, settings = _library_runtime()
    if not library_scanner or not settings:
        raise HTTPException(status_code=503, detail="Library not available")

    music_root = settings.MUSIC_ROOT.resolve()
    folder_path = _resolve_library_folder(req.folder, music_root)
    if folder_path == settings.download_dir.resolve():
        raise HTTPException(status_code=400, detail="Cannot delete the managed imports container")
    rel_folder = folder_path.relative_to(music_root).as_posix()

    deleted_track_ids = []
    for track in library_scanner.get_tracks(refresh=True):
        if not track.path:
            continue
        try:
            if track.path.resolve().is_relative_to(folder_path):
                deleted_track_ids.append(track.id)
        except (OSError, ValueError):
            continue

    try:
        shutil.rmtree(folder_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete folder: {exc}") from exc

    tracks = library_scanner.refresh(force=True)
    return {
        "status": "ok",
        "folder": rel_folder,
        "deleted": deleted_track_ids,
        "folder_removed": not folder_path.exists(),
        "track_count": len(tracks),
    }

