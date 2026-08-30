#!/usr/bin/env bash
set -Eeuo pipefail

"${FXROUTE_REAL_ISOHYBRID:-/usr/bin/isohybrid}" --id 12345678 "$@"

image="${!#}"
python3 - "$image" <<'PY'
import sys


SECTOR_SIZE = 2048
CHUNK_SIZE = 8 * 1024 * 1024
EFI_BOOT_MARKER = b"\xeb\x3c\x90MTOO4049"
EFI_VOLUME_LABEL = b"EFIBOOT    "
REPRODUCIBLE_SERIAL = b"\x78\x56\x34\x12"


with open(sys.argv[1], "r+b") as image:
    image_size = image.seek(0, 2)
    serial_offsets = []
    chunk_start = 0
    while chunk_start < image_size:
        image.seek(chunk_start)
        chunk = image.read(min(CHUNK_SIZE, image_size - chunk_start))
        for offset in range(0, len(chunk) - 511, SECTOR_SIZE):
            sector = chunk[offset : offset + 512]
            if (
                sector[:11] == EFI_BOOT_MARKER
                and sector[43:54] == EFI_VOLUME_LABEL
                and sector[510:512] == b"\x55\xaa"
            ):
                serial_offsets.append(chunk_start + offset + 39)
        chunk_start += len(chunk)

    for offset in serial_offsets:
        image.seek(offset)
        image.write(REPRODUCIBLE_SERIAL)
PY
