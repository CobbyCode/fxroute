"""ZIP album extraction and archive-name helpers.

Extracted verbatim from main.py (REFACTOR-008). Behavior is identical to the
previous inline implementation: unique path/dir selection, ZIP path traversal
protection, metadata filtering (__MACOSX, .DS_Store), allowed file types,
extraction order, return payloads, exception types and HTTP status/messages are
unchanged. No runtime globals are used.

Hardening pass: every ZIP import is checked before any extraction - member
count, total and per-member uncompressed size, traversal / absolute /
Windows-drive / UNC paths, symlink and special-file entries, encrypted
entries, and the central-directory size are all bounded. Extraction reads
are chunked and counted, so declared sizes cannot be bypassed by a
manipulated archive.
"""

from __future__ import annotations

import stat
import struct
import zipfile
from pathlib import Path
from typing import List, Optional

from fastapi import HTTPException

UPLOAD_AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav", ".wma", ".webm", ".weba"}
PLAYLIST_FILE_EXTENSIONS = {".m3u", ".m3u8"}
ZIP_IGNORED_PARTS = {"__MACOSX"}
ZIP_IGNORED_FILENAMES = {".ds_store", "thumbs.db"}

ZIP_COPY_CHUNK_BYTES = 256 * 1024

# Library album ZIP hardening limits (generous for legit albums).
ALBUM_ZIP_MAX_MEMBERS = 4096
ALBUM_ZIP_MAX_TOTAL_UNCOMPRESSED_BYTES = 4 * 1024 ** 3
ALBUM_ZIP_MAX_MEMBER_BYTES = 2 * 1024 ** 3
ALBUM_ZIP_MAX_CENTRAL_DIRECTORY_BYTES = 32 * 1024 * 1024

ZIP_ENCRYPTED_FLAG = 0x1


class ZipLimitError(ValueError):
    """ZIP archive exceeds a defined hardening limit."""


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
    """Return the normalized relative member path, or None if unsafe.

    Rejects empty names, absolute POSIX paths, UNC/leading-slash paths,
    Windows drive-letter paths, and any component that is ``.`` or ``..``.
    """
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return None
    stripped = normalized.strip("/")
    if not stripped:
        return None
    if len(stripped) >= 2 and stripped[1] == ":" and stripped[0].isalpha():
        return None

    candidate = Path(stripped)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    if any(part in ZIP_IGNORED_PARTS for part in candidate.parts):
        return None
    if candidate.name.lower() in ZIP_IGNORED_FILENAMES:
        return None
    return candidate


def zip_member_hardening_reason(member: zipfile.ZipInfo) -> Optional[str]:
    """Return a rejection reason for encrypted / symlink / special entries."""
    if member.flag_bits & ZIP_ENCRYPTED_FLAG:
        return "encrypted entries are not supported"
    is_symlink = getattr(member, "is_symlink", None)
    if callable(is_symlink) and is_symlink():
        return "symbolic-link entries are not supported"
    if member.external_attr >> 16:
        file_type = stat.S_IFMT(member.external_attr >> 16)
        if file_type == stat.S_IFLNK:
            return "symbolic-link entries are not supported"
        if file_type in (stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK):
            return "special-file entries are not supported"
    return None


def check_zip_file_before_open(
    zip_path: Path,
    *,
    max_members: int,
    max_central_directory_bytes: int,
) -> None:
    """Validate the End-Of-Central-Directory record before opening the archive.

    zipfile.ZipFile loads the whole central directory into memory on open;
    this pre-check bounds that allocation and the member count before
    anything is parsed.
    """
    size = zip_path.stat().st_size
    if size < 22:
        raise zipfile.BadZipFile("Invalid ZIP archive")
    with zip_path.open("rb") as handle:
        tail_size = min(size, 22 + 65535)
        handle.seek(size - tail_size)
        tail = handle.read(tail_size)
    eocd_offset = tail.rfind(b"PK\x05\x06")
    if eocd_offset < 0:
        raise zipfile.BadZipFile("Invalid ZIP archive")
    try:
        entry_count = struct.unpack_from("<H", tail, eocd_offset + 10)[0]
        central_directory_bytes = struct.unpack_from("<I", tail, eocd_offset + 12)[0]
    except struct.error:
        raise zipfile.BadZipFile("Invalid ZIP archive") from None
    if entry_count == 0xFFFF or central_directory_bytes == 0xFFFFFFFF:
        raise ZipLimitError("ZIP64 archives are not supported")
    if central_directory_bytes > max_central_directory_bytes:
        raise ZipLimitError(
            f"ZIP central directory too large ({central_directory_bytes} bytes)"
        )
    if entry_count > max_members:
        raise ZipLimitError(f"ZIP contains too many entries ({entry_count} > {max_members})")


def check_zip_limits(
    archive: zipfile.ZipFile,
    *,
    max_members: int,
    max_total_uncompressed_bytes: int,
    max_member_bytes: int,
) -> int:
    """Validate declared member count and uncompressed sizes. Returns total."""
    members = archive.infolist()
    if len(members) > max_members:
        raise ZipLimitError(f"ZIP contains too many entries ({len(members)} > {max_members})")
    total = 0
    for member in members:
        if member.is_dir():
            continue
        if member.file_size > max_member_bytes:
            raise ZipLimitError(
                f"ZIP member {member.filename!r} is too large "
                f"({member.file_size} > {max_member_bytes} bytes)"
            )
        total += member.file_size
        if total > max_total_uncompressed_bytes:
            raise ZipLimitError(
                f"ZIP uncompressed size exceeds {max_total_uncompressed_bytes} bytes"
            )
    return total


def copy_member_bounded(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    dest_path: Path,
    *,
    remaining_bytes: int,
) -> int:
    """Extract one member to dest_path with a counted, bounded read.

    Returns the number of bytes written.  Raises ZipLimitError once the
    remaining budget is exceeded and removes the partial destination file.
    """
    written = 0
    try:
        with archive.open(member) as source, dest_path.open("wb") as target:
            while True:
                chunk = source.read(ZIP_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > remaining_bytes:
                    raise ZipLimitError(
                        f"ZIP member {member.filename!r} exceeds the allowed extraction budget"
                    )
                target.write(chunk)
    except BaseException:
        dest_path.unlink(missing_ok=True)
        raise
    return written


def read_member_bounded(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    max_bytes: int,
) -> bytes:
    """Read one member into memory, chunked and bounded by max_bytes."""
    chunks: List[bytes] = []
    total = 0
    with archive.open(member) as source:
        while True:
            chunk = source.read(ZIP_COPY_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ZipLimitError(
                    f"ZIP member {member.filename!r} exceeds the allowed read budget"
                )
            chunks.append(chunk)
    return b"".join(chunks)


def extract_zip_album(
    zip_path: Path,
    target_root: Path,
    *,
    max_members: Optional[int] = None,
    max_total_uncompressed_bytes: Optional[int] = None,
    max_member_bytes: Optional[int] = None,
    max_central_directory_bytes: Optional[int] = None,
) -> dict:
    extracted_files = []
    skipped_entries = []

    if max_members is None:
        max_members = ALBUM_ZIP_MAX_MEMBERS
    if max_total_uncompressed_bytes is None:
        max_total_uncompressed_bytes = ALBUM_ZIP_MAX_TOTAL_UNCOMPRESSED_BYTES
    if max_member_bytes is None:
        max_member_bytes = ALBUM_ZIP_MAX_MEMBER_BYTES
    if max_central_directory_bytes is None:
        max_central_directory_bytes = ALBUM_ZIP_MAX_CENTRAL_DIRECTORY_BYTES

    try:
        check_zip_file_before_open(
            zip_path,
            max_members=max_members,
            max_central_directory_bytes=max_central_directory_bytes,
        )
        with zipfile.ZipFile(zip_path) as archive:
            check_zip_limits(
                archive,
                max_members=max_members,
                max_total_uncompressed_bytes=max_total_uncompressed_bytes,
                max_member_bytes=max_member_bytes,
            )

            extracted_total = 0
            for member in archive.infolist():
                safe_relative = is_safe_relative_zip_path(member.filename)
                if safe_relative is None:
                    skipped_entries.append(member.filename)
                    continue
                if zip_member_hardening_reason(member):
                    skipped_entries.append(member.filename)
                    continue

                if member.is_dir():
                    (target_root / safe_relative).mkdir(parents=True, exist_ok=True)
                    continue

                destination = target_root / safe_relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.suffix.lower() in UPLOAD_AUDIO_EXTENSIONS:
                    destination = choose_unique_path(destination)

                remaining = min(
                    max_total_uncompressed_bytes - extracted_total,
                    max_member_bytes,
                )
                written = copy_member_bounded(
                    archive, member, destination, remaining_bytes=remaining
                )
                extracted_total += written
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
