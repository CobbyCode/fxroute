"""ZIP album extraction and archive-name helpers.

Extracted verbatim from main.py (REFACTOR-008). Behavior is identical to the
previous inline implementation: unique path/dir selection, ZIP path traversal
protection, metadata filtering (__MACOSX, .DS_Store), allowed file types,
extraction order, return payloads, exception types and HTTP status/messages are
unchanged. No runtime globals are used.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import HTTPException


UPLOAD_AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav", ".wma", ".webm", ".weba"}
PLAYLIST_FILE_EXTENSIONS = {".m3u", ".m3u8"}
ZIP_IGNORED_PARTS = {"__MACOSX"}
ZIP_IGNORED_FILENAMES = {".ds_store", "thumbs.db"}


def dedupe_archive_name(name: str, used_names: set[str]) -> str:
    candidate = Path(name or "track").name or "track"
    stem = Path(candidate).stem or "track"
    suffix = Path(candidate).suffix
    index = 2
    while candidate in used_names:
        candidate = f"{stem}-{index}{suffix}"
        index += 1
    used_names.add(candidate)
    return candidate


def choose_unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def choose_unique_dir(path: Path) -> Path:
    if not path.exists():
        return path

    counter = 2
    while True:
        candidate = path.with_name(f"{path.name}-{counter}")
        if not candidate.exists():
            return candidate
        counter += 1


def is_safe_relative_zip_path(name: str) -> Optional[Path]:
    normalized = name.replace("\\", "/").strip("/")
    if not normalized:
        return None

    candidate = Path(normalized)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    if any(part in ZIP_IGNORED_PARTS for part in candidate.parts):
        return None
    if candidate.name.lower() in ZIP_IGNORED_FILENAMES:
        return None
    return candidate


def extract_zip_album(zip_path: Path, target_root: Path) -> dict:
    extracted_files = []
    skipped_entries = []

    try:
        with zipfile.ZipFile(zip_path) as archive:
            if archive.testzip() is not None:
                raise HTTPException(status_code=400, detail="Invalid ZIP archive")

            for member in archive.infolist():
                safe_relative = is_safe_relative_zip_path(member.filename)
                if safe_relative is None:
                    skipped_entries.append(member.filename)
                    continue

                if member.is_dir():
                    (target_root / safe_relative).mkdir(parents=True, exist_ok=True)
                    continue

                destination = target_root / safe_relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.suffix.lower() in UPLOAD_AUDIO_EXTENSIONS:
                    destination = choose_unique_path(destination)

                with archive.open(member) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)

                extracted_files.append(destination)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid ZIP archive")

    audio_files = [path for path in extracted_files if path.suffix.lower() in UPLOAD_AUDIO_EXTENSIONS]
    playlist_files = [path for path in extracted_files if path.suffix.lower() in PLAYLIST_FILE_EXTENSIONS]
    return {
        "audio_files": audio_files,
        "playlist_files": playlist_files,
        "extracted_files": extracted_files,
        "skipped_entries": skipped_entries,
    }
