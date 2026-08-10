"""Bounded upload reading for file upload endpoints.

Every FXRoute upload endpoint reads through this module so that no upload
is ever loaded into memory or written to disk without an explicit,
per-class size limit.  Reading is chunked and counted; a limit breach
aborts the read immediately.  A present Content-Length is only used as a
fast pre-reject, never as a trust signal for the counted read.
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, Union

UPLOAD_READ_CHUNK_BYTES = 1024 * 1024

# Named, generous per-class limits (legit FXRoute usage stays far below).
EASYEEFFECTS_IR_MAX_BYTES = 64 * 1024 * 1024          # single/dual convolver IR (.irs/.wav)
EASYEEFFECTS_PRESET_TEXT_MAX_BYTES = 8 * 1024 * 1024  # preset JSON / REW text import
EASYEEFFECTS_BUNDLE_MAX_BYTES = 256 * 1024 * 1024     # preset bundle ZIP upload
LIBRARY_UPLOAD_MAX_BYTES = 2 * 1024 ** 3              # library audio file / album ZIP upload
TEXT_UPLOAD_MAX_BYTES = 8 * 1024 * 1024               # playlist (m3u/m3u8) upload
MEASUREMENT_TEXT_MAX_BYTES = 2 * 1024 * 1024          # calibration / house-curve text


class UploadTooLargeError(ValueError):
    """Raised when an upload exceeds its class limit."""


def _fast_reject(upload, max_bytes: int) -> None:
    content_length = getattr(upload, "content_length", None)
    if content_length is None:
        content_length = getattr(upload, "size", None)
    if isinstance(content_length, int) and content_length > max_bytes:
        raise UploadTooLargeError(
            f"Upload too large (max {max_bytes // (1024 * 1024)} MiB)"
        )


async def read_upload(upload, max_bytes: int) -> bytes:
    """Read an upload into memory, chunked and bounded by max_bytes."""
    _fast_reject(upload, max_bytes)
    chunks = []
    total = 0
    while True:
        chunk = await upload.read(UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise UploadTooLargeError(
                f"Upload too large (max {max_bytes // (1024 * 1024)} MiB)"
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def save_upload_to_file(upload, dest: Union[Path, BinaryIO], max_bytes: int) -> int:
    """Stream an upload into a destination file, chunked and bounded.

    ``dest`` may be an open binary file or a path that is opened for
    writing here.  Returns the number of bytes written.  On a limit
    breach the read stops immediately and the error propagates, so the
    caller's existing temp-file cleanup (finally blocks) still runs.
    """
    _fast_reject(upload, max_bytes)
    total = 0
    if isinstance(dest, Path):
        with dest.open("wb") as target:
            return await save_upload_to_file(upload, target, max_bytes)
    while True:
        chunk = await upload.read(UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise UploadTooLargeError(
                f"Upload too large (max {max_bytes // (1024 * 1024)} MiB)"
            )
        dest.write(chunk)
    return total
